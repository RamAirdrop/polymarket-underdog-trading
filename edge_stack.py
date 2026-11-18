"""
Pre-match quantitative stack  (strategy = "stack")

One decision function (`decide`) is shared by the live scanner (tennis_core.analyse) and the backtest
(backtest.run_backtest), so what you test is what you trade.

  Layer 1  value      model probability (surface Elo + serve/return points, shrunk toward the market) must beat
                      the entry price by MIN_EDGE.  Already existed in tennis_core; unchanged.
  Layer 2  fatigue    workload from recent match dates / durations.  Heavy fatigue on our pick = VETO.
                      Clear rest advantage = size x REST_BOOST.  Unknown data = neutral (never a boost, never a veto).
  Layer 3  sharp line vig-free price from a sharp book (Pinnacle via The Odds API, or a CSV you fill in).
                      Sharp disagrees = VETO.  Sharp agrees = full conviction.  No sharp line = lower grade.
  Entry    resting limit order 1c above the best bid (never crosses the spread), auto-expiring at the start.
  Sizing   quarter-Kelly, capped by grade (A / B / C) and by a hard per-match cap.
  Gate     ask-side depth within 1c must be >= 3x the order size.

EVERY number below is a heuristic from the strategy write-up, not a validated parameter. Backtest before using.
"""
import bisect
import json
import math
import os
import re
import time
import unicodedata
from datetime import date

import pandas as pd
import requests

# ----------------------------------------------------------------------------- parameters (tune after backtest)
STACK_MIN_PRICE, STACK_MAX_PRICE = 0.15, 0.80   # entry price band (either side of the match)
MIN_EDGE = 0.05                                 # B-grade threshold: p_final - (entry + fee)
A_EDGE = 0.07                                   # A-grade threshold
EDGE_BASIS = "limit"                            # "limit": edge at our resting-order price (maker, no fee)
                                                # "ask"  : edge at the ask incl. taker fee (stricter, ignores maker benefit)
MAKER_FEE_RATE = 0.0                            # Polymarket makers are not charged the taker fee - VERIFY
LIMIT_IMPROVE = 0.01                            # rest this far above the best bid
KELLY_FRAC = 0.25
CAP_A, CAP_B, CAP_C = 0.03, 0.015, 0.0075       # max stake as share of bankroll by grade (A / B / C)
MAX_MATCH_FRAC = 0.05                           # hard cap per match, even after the rest boost
REST_BOOST = 1.25
LIQ_MULT, LIQ_WINDOW = 3.0, 0.01                # ask depth within LIQ_WINDOW of best ask must be >= LIQ_MULT x stake

# fatigue (calendar days only: Polymarket / Sackmann give dates, not clock times)
LOOKBACK_DAYS = 14                              # no match in this window = rest unknown (could be a data gap)
HEAVY_REST_DAYS, HEAVY_MINUTES = 1, 180         # played yesterday/today AND that match lasted >= 3h
HEAVY_MATCHES_4D = 3                            # or 3+ matches in the last 4 days
REST_EDGE_DAYS = 2                              # our pick has >= this many more rest days than a tired opponent
COVERAGE_DAYS = 7                               # if the newest match in the log is older than this, call fatigue unknown

# sharp line
SHARP_WEIGHT = 0.6                              # p_final = w*sharp + (1-w)*adjusted model when a sharp line exists
SHARP_GAP = 0.02                                # sharp must exceed our entry price by this to count as "agree"
REQUIRE_SHARP = False                           # True = no sharp line -> PASS

EPOCH = date(1970, 1, 1)


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalpha() or c in " -" else " " for c in s).split())


def day_of(ts):
    """UTC calendar day as days since 1970-01-01."""
    ts = pd.Timestamp(ts)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return (ts.date() - EPOCH).days


# ============================================================================= Layer 2: workload / fatigue
# Sackmann rows only carry the tournament START date + round, so match days are estimated from the round.
_SLAM_DAY = {"R128": 0, "R64": 2, "R32": 4, "R16": 6, "QF": 8, "SF": 11, "F": 13}
_TOUR_DAY = {"R128": 0, "R64": 0, "R32": 1, "R16": 3, "QF": 4, "SF": 5, "F": 6}


def _round_offset(rnd, draw):
    table = _SLAM_DAY if (pd.notna(draw) and draw >= 96) else _TOUR_DAY
    return table.get(str(rnd), 0)


class Workload:
    """Per-player match log (date, minutes). Rows from Polymarket results (tour == 'pm') have exact dates and no
    duration; Sackmann rows have estimated dates and durations."""

    def __init__(self):
        self._raw, self._days, self._rows, self.latest = {}, {}, {}, None

    def add(self, player, day, minutes=float("nan"), exact=False):
        self._raw.setdefault(player, []).append((int(day), float(minutes), bool(exact)))

    def finalize(self):
        self._rows = {p: sorted(v) for p, v in self._raw.items()}
        self._days = {p: [r[0] for r in v] for p, v in self._rows.items()}
        self.latest = max((v[-1][0] for v in self._rows.values()), default=None)
        return self

    @classmethod
    def from_matches(cls, df):
        w = cls()
        base = pd.to_datetime(df.tourney_date.astype(str), format="%Y%m%d", errors="coerce")
        days = (base - pd.Timestamp("1970-01-01")).dt.days.to_numpy(dtype="float64")
        nan = pd.Series(float("nan"), index=df.index)
        rnd = df["round"] if "round" in df else pd.Series("", index=df.index)
        draw = pd.to_numeric(df["draw_size"], errors="coerce") if "draw_size" in df else nan
        mins = pd.to_numeric(df["minutes"], errors="coerce") if "minutes" in df else nan
        tour = df["tour"] if "tour" in df else pd.Series("", index=df.index)
        for d, r, ds, m, t, wn, ln in zip(days, rnd, draw, mins, tour, df.winner_name, df.loser_name):
            if math.isnan(d):
                continue
            exact = t == "pm"
            if not exact:
                d += _round_offset(r, ds)
            w.add(wn, d, m, exact); w.add(ln, d, m, exact)
        return w.finalize()

    def coverage_ok(self, as_of_day):
        return self.latest is not None and as_of_day - self.latest <= COVERAGE_DAYS

    def snapshot(self, player, as_of_day, exact_only=False):
        """Workload from matches strictly BEFORE as_of_day.
        exact_only=True ignores Sackmann rows (estimated days). The backtest uses it: with estimated days a match
        could count itself as 'yesterday's match', which is look-ahead."""
        empty = dict(known=False, rest=None, last_min=float("nan"), n4=0, min4=0.0)
        rows, days = self._rows.get(player), self._days.get(player)
        if not rows:
            return empty
        i = bisect.bisect_left(days, as_of_day)
        recent = [r for r in rows[:i] if r[0] >= as_of_day - LOOKBACK_DAYS and (r[2] or not exact_only)]
        if not recent:
            return empty
        last_day = recent[-1][0]
        last_min = max((r[1] for r in recent if r[0] == last_day and not math.isnan(r[1])), default=float("nan"))
        last4 = [r for r in recent if r[0] >= as_of_day - 4]
        return dict(known=True, rest=as_of_day - last_day, last_min=last_min, n4=len(last4),
                    min4=float(sum(r[1] for r in last4 if not math.isnan(r[1]))))


def fatigue_flag(pick, opp, coverage_ok=True):
    """-> (flag, note). flag in heavy | rest_edge | neutral | unknown. 'unknown' is treated as neutral."""
    if not coverage_ok:
        return "unknown", "workload log is stale"
    if not pick["known"] and not opp["known"]:
        return "unknown", "no recent matches logged for either player"
    lm = "?" if math.isnan(pick["last_min"]) else f"{pick['last_min']:.0f}min"
    note = (f"pick: rest {pick['rest']}d, last {lm}, {pick['n4']} in 4d"
            if pick["known"] else "pick: no recent match logged")
    note += (f" | opp: rest {opp['rest']}d, {opp['n4']} in 4d" if opp["known"] else " | opp: no recent match logged")
    if pick["known"] and ((pick["rest"] <= HEAVY_REST_DAYS and pick["last_min"] >= HEAVY_MINUTES)
                          or pick["n4"] >= HEAVY_MATCHES_4D):
        return "heavy", note
    if pick["known"] and opp["known"] and opp["rest"] <= HEAVY_REST_DAYS and pick["rest"] - opp["rest"] >= REST_EDGE_DAYS:
        return "rest_edge", note
    return "neutral", note


# ============================================================================= Layer 3: sharp line
def devig(o1, o2):
    """Two decimal odds -> vig-free probabilities (proportional method)."""
    a, b = 1 / o1, 1 / o2
    return a / (a + b), b / (a + b)


class SharpFeed:
    """Vig-free two-way probabilities from a sharp book.
    Sources (both optional):
      * env ODDS_API_KEY  -> The Odds API, bookmaker 'pinnacle' (UNTESTED here - check your plan's quota/regions)
      * data/sharp_odds.csv with columns player1,player2,odds1,odds2 (decimal odds you copy from Pinnacle)"""

    def __init__(self, data_dir, ttl=600):
        self.csv = os.path.join(data_dir, "sharp_odds.csv")
        self.ttl, self.t, self.rows, self.source, self.error = ttl, 0.0, [], "none", ""

    @staticmethod
    def _last(name):
        t = _norm(name).split()
        return t[-1] if t else ""

    def _add(self, n1, n2, o1, o2):
        try:
            o1, o2 = float(o1), float(o2)
            if o1 > 1 and o2 > 1 and "/" not in str(n1) + str(n2):
                self.rows.append((self._last(n1), self._last(n2), *devig(o1, o2)))
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def refresh(self, force=False):
        if not force and time.time() - self.t < self.ttl:
            return
        self.t, self.rows, srcs, self.error = time.time(), [], [], ""
        if os.path.exists(self.csv):
            try:
                for r in pd.read_csv(self.csv).itertuples():
                    self._add(r.player1, r.player2, r.odds1, r.odds2)
                srcs.append("csv")
            except Exception as e:
                self.error = f"csv: {type(e).__name__}"
        key = os.environ.get("ODDS_API_KEY")
        if key:
            try:
                base = "https://api.the-odds-api.com/v4/sports"
                sports = requests.get(base, params={"apiKey": key}, timeout=20).json()
                for s in sports:
                    if not str(s.get("key", "")).startswith("tennis_") or not s.get("active"):
                        continue
                    evs = requests.get(f"{base}/{s['key']}/odds", timeout=20, params={
                        "apiKey": key, "regions": "eu", "markets": "h2h", "bookmakers": "pinnacle",
                        "oddsFormat": "decimal"}).json()
                    for ev in evs if isinstance(evs, list) else []:
                        for bk in ev.get("bookmakers", []):
                            for mk in bk.get("markets", []):
                                o = mk.get("outcomes", [])
                                if mk.get("key") == "h2h" and len(o) == 2:
                                    self._add(o[0]["name"], o[1]["name"], o[0]["price"], o[1]["price"])
                srcs.append("odds-api")
            except Exception as e:
                self.error = (self.error + f" odds-api: {type(e).__name__}").strip()
        self.source = "+".join(srcs) or "none"

    def lookup(self, name_a, name_b):
        """-> (p_a, p_b) vig-free, or None when the sharp book has no line for this pair."""
        self.refresh()
        la, lb = self._last(name_a), self._last(name_b)
        for l1, l2, p1, p2 in reversed(self.rows):
            if (l1, l2) == (la, lb):
                return p1, p2
            if (l1, l2) == (lb, la):
                return p2, p1
        return None


# ============================================================================= entry + sizing + liquidity
def limit_price(bid, ask, tick=0.01, improve=LIMIT_IMPROVE):
    """Resting buy price: `improve` above the best bid, but never at/through the ask (it must rest, not take)."""
    if not ask:
        return None
    if not bid or bid <= 0:
        return round(max(ask - tick, tick), 2)
    p = bid + improve
    if p >= ask - 1e-9:
        p = bid                      # spread too tight to improve: join the bid
    return round(min(max(p, tick), ask), 2)


def kelly_fraction(p, cost):
    """Full-Kelly stake share for a binary share bought at `cost` (<1) that pays 1."""
    if not (0 < cost < 1):
        return 0.0
    b = 1 / cost - 1
    return max(0.0, (p * b - (1 - p)) / b)


def depth_ok(asks, stake_usd, mult=LIQ_MULT, window=LIQ_WINDOW):
    """asks = [(price, size)] ascending. USD resting within `window` of the best ask vs mult x stake."""
    if not asks:
        return False, 0.0
    best = asks[0][0]
    depth = sum(p * s for p, s in asks if p <= best + window + 1e-9)
    return depth >= mult * stake_usd, depth


def decide(p_adj, entry, fee, sharp_p=None, fatigue="unknown", *, use_fatigue=True, require_sharp=None):
    """One side of one match. p_adj = model probability already shrunk toward the market.
    Returns dict(ok, reason, grade, p, edge, frac, sharp_flag). `frac` is the share of bankroll to stake."""
    require_sharp = REQUIRE_SHARP if require_sharp is None else require_sharp
    out = dict(ok=False, reason="", grade="", p=p_adj, edge=None, frac=0.0, sharp_flag="none")
    if not (STACK_MIN_PRICE <= entry <= STACK_MAX_PRICE):
        out["reason"] = f"price {entry:.2f} outside {STACK_MIN_PRICE:.2f}-{STACK_MAX_PRICE:.2f}"
        return out
    p = p_adj if sharp_p is None else SHARP_WEIGHT * sharp_p + (1 - SHARP_WEIGHT) * p_adj      # Layer 3 blend
    out["p"] = p
    out["edge"] = edge = p - (entry + fee)
    if edge < MIN_EDGE:
        out["reason"] = f"edge {edge:+.1%} < {MIN_EDGE:.0%}"
        return out
    if use_fatigue and fatigue == "heavy":                                                   # Layer 2 veto
        out["reason"] = "VETO: heavy fatigue on our pick"
        return out
    if sharp_p is None:
        if require_sharp:
            out["reason"] = "VETO: no sharp line"
            return out
    else:
        gap = sharp_p - entry
        out["sharp_flag"] = "agree" if gap >= SHARP_GAP else ("weak" if gap >= 0 else "disagree")
        if out["sharp_flag"] == "disagree":                                                  # Layer 3 veto
            out["reason"] = f"VETO: sharp {sharp_p:.0%} below our entry {entry:.0%}"
            return out
    rest = use_fatigue and fatigue == "rest_edge"
    if out["sharp_flag"] == "agree" and edge >= A_EDGE and rest:
        grade, cap = "A", CAP_A
    elif out["sharp_flag"] in ("agree", "weak"):
        grade, cap = "B", CAP_B
    else:                                                                                    # no sharp line available
        grade, cap = "C", CAP_C
    frac = min(KELLY_FRAC * kelly_fraction(p, entry + fee), cap)
    if rest:
        frac *= REST_BOOST
    out.update(ok=frac > 0, grade=grade, frac=min(frac, MAX_MATCH_FRAC))
    if not out["ok"]:
        out["reason"] = "zero Kelly stake"
    return out

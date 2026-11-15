"""
Polymarket tennis tracker + model favorite engine.

  python tennis_core.py scan --bankroll 1000      # table of live markets + signals
  python tennis_core.py calib                     # how good is the Elo model (history only)
  python tennis_core.py sim --price 0.33 --win 0.40   # payoff maths for your 3x example
"""
import argparse, io, json, math, os, re, time, unicodedata
from collections import deque
from datetime import datetime, timezone

import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com"
SOURCES = [
    "https://raw.githubusercontent.com/Aneeshers/tennis-sackmann-archive/main/{tour}/{tour}_matches_{year}.csv",
    "https://huggingface.co/datasets/Aneeshers/tennis-sackmann-archive/resolve/main/{tour}/{tour}_matches_{year}.csv",
    "https://raw.githubusercontent.com/JeffSackmann/tennis_{tour}/master/{tour}_matches_{year}.csv",
]
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)


# ----------------------------------------------------------------- history data
def load_matches(years=None, tours=("atp", "wta"), max_age_h=24, gap_fill=True):
    """Download (and cache) match results; then top up with Polymarket-resolved matches the mirror lacks."""
    this = datetime.now().year
    years = years or [this - 3, this - 2, this - 1, this]
    frames = []
    for t in tours:
        for y in years:
            path = os.path.join(DATA_DIR, f"{t}_{y}.csv")
            fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600
            if not fresh:
                for tpl in SOURCES:
                    try:
                        r = requests.get(tpl.format(tour=t, year=y), timeout=40)
                        if r.status_code == 200 and r.text.startswith("tourney_id"):
                            open(path, "wb").write(r.content)
                            break
                    except requests.RequestException:
                        pass
            if os.path.exists(path):
                df = pd.read_csv(path, usecols=lambda c: c in
                                 {"tourney_date", "surface", "winner_name", "loser_name", "match_num", *STAT_COLS})
                df["tour"] = t
                frames.append(df)
    if not frames:
        raise RuntimeError("Could not download match history from any source (check internet / run diagnose.py).")
    df = pd.concat(frames, ignore_index=True).dropna(subset=["winner_name", "loser_name", "tourney_date"])
    df["tourney_date"] = pd.to_numeric(df.tourney_date, errors="coerce")
    df = df.dropna(subset=["tourney_date"]).astype({"tourney_date": int})
    df["surface"] = (df.surface.fillna("Hard").astype(str).str.strip().str.title()
                     .replace({"Carpet": "Hard", "Nan": "Hard", "": "Hard"}))
    df["match_num"] = pd.to_numeric(df.match_num, errors="coerce").fillna(0)
    df = df.sort_values(["tourney_date", "match_num"]).reset_index(drop=True)
    return add_polymarket_gap(df) if gap_fill else df


def add_polymarket_gap(df, max_age_h=6):
    """The mirror stops in May 2026. Resolved Polymarket tennis matches after that date are used as results."""
    path = os.path.join(DATA_DIR, "pm_results.csv")
    last = int(df.tourney_date.max())
    cutoff = pd.to_datetime(str(last), format="%Y%m%d")
    cols = ["tourney_date", "surface", "winner_name", "loser_name", "match_num"]
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600
    if not fresh:
        try:
            rows = []
            for ev in fetch_events(4000, closed=True, stop_before=cutoff):
                m = pick_moneyline(ev)
                if not m:
                    continue
                outs = _j(m.get("outcomes")); px = [float(x) for x in _j(m.get("outcomePrices"))]
                if len(outs) != 2 or len(px) != 2 or max(px) < 0.99:
                    continue
                start = pd.to_datetime(m.get("gameStartTime") or ev.get("startTime") or ev.get("endDate"),
                                       utc=True, errors="coerce")
                if pd.isna(start) or start.tz_convert(None) < cutoff:
                    continue
                w = 0 if px[0] >= 0.99 else 1
                rows.append(dict(tourney_date=int(start.strftime("%Y%m%d")), surface=detect_surface(ev),
                                 winner_name=outs[w], loser_name=outs[1 - w], match_num=100000 + len(rows)))
            pd.DataFrame(rows, columns=cols).to_csv(path, index=False)
        except Exception as e:
            print(f"[warn] Polymarket gap-fill skipped ({type(e).__name__}); ratings end at {last}.")
            return df
    gap = pd.read_csv(path)
    gap = gap[gap.tourney_date >= last] if len(gap) else gap
    if gap.empty:
        return df
    idx = Ratings()
    for n in set(df.winner_name) | set(df.loser_name):
        idx.o[n] = 0.0
    idx.finalize()
    for c in ("winner_name", "loser_name"):
        gap[c] = gap[c].map(lambda n: idx.find(n) or n)
    gap = gap[gap.winner_name != gap.loser_name].assign(tour="pm")
    return pd.concat([df, gap], ignore_index=True).sort_values(["tourney_date", "match_num"]).reset_index(drop=True)


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalpha() or c in " -" else " " for c in s).split())


# ----------------------------------------------------------------- skill model
STAT_COLS = ["w_svpt", "w_1stWon", "w_2ndWon", "l_svpt", "l_1stWon", "l_2ndWon"]


def p_game(p):
    q = 1 - p
    return p**4 * (1 + 4 * q + 10 * q * q) + 20 * p**3 * q**3 * p * p / (1 - 2 * p * q)


def p_tiebreak(pt):
    q = 1 - pt
    return (sum(math.comb(6 + k, k) * pt**7 * q**k for k in range(6))
            + math.comb(12, 6) * pt**6 * q**6 * pt * pt / (pt * pt + q * q))


def p_match_from_points(pa, pb, best_of=3):
    from functools import lru_cache
    gA, gB, tb = p_game(pa), p_game(pb), p_tiebreak((pa + 1 - pb) / 2)

    def set_prob(a_first):
        @lru_cache(None)
        def f(i, j):
            if i == 7 or (i == 6 and j <= 4): return 1.0
            if j == 7 or (j == 6 and i <= 4): return 0.0
            if i == 6 and j == 6: return tb
            pg = gA if ((i + j) % 2 == 0) == a_first else 1 - gB
            return pg * f(i + 1, j) + (1 - pg) * f(i, j + 1)
        return f(0, 0)

    s = 0.5 * (set_prob(True) + set_prob(False))
    return s**3 * (10 - 15 * s + 6 * s * s) if best_of == 5 else s * s * (3 - 2 * s)


def points_for_prob(p, mu=0.62, best_of=3):
    p = min(max(p, 0.02), 0.98); lo, hi = -0.2, 0.2
    for _ in range(40):
        d = (lo + hi) / 2
        if p_match_from_points(mu + d, mu - d, best_of) < p:
            lo = d
        else:
            hi = d
    d = (lo + hi) / 2
    return mu + d, mu - d


def live_win_prob(sa, sb, SA, SB, ga, gb, best_of=3, a_serving=None):
    from functools import lru_cache
    if a_serving is None:
        return 0.5 * (live_win_prob(sa, sb, SA, SB, ga, gb, best_of, True)
                      + live_win_prob(sa, sb, SA, SB, ga, gb, best_of, False))
    need = 3 if best_of == 5 else 2
    if SA >= need: return 1.0
    if SB >= need: return 0.0
    gA, gB, tb = p_game(sa), p_game(sb), p_tiebreak((sa + 1 - sb) / 2)

    @lru_cache(None)
    def f(i, j, a_srv):
        if i >= 7 or (i == 6 and j <= 4): return 1.0
        if j >= 7 or (j == 6 and i <= 4): return 0.0
        if i == 6 and j == 6: return tb
        pg = gA if a_srv else 1 - gB
        return pg * f(i + 1, j, not a_srv) + (1 - pg) * f(i, j + 1, not a_srv)

    s_fresh = 0.5 * (f(0, 0, True) + f(0, 0, False))

    @lru_cache(None)
    def M(x, y):
        if x == need: return 1.0
        if y == need: return 0.0
        return s_fresh * M(x + 1, y) + (1 - s_fresh) * M(x, y + 1)

    s_cur = f(ga, gb, a_serving)
    return s_cur * M(SA + 1, SB) + (1 - s_cur) * M(SA, SB + 1)


def parse_score(score):
    toks = []
    for t in str(score or "").split(","):
        m = re.match(r"\s*(\d+)\s*-\s*(\d+)", re.sub(r"\(\d+\)", "", t))
        if m:
            toks.append((int(m.group(1)), int(m.group(2))))
    if not toks:
        return None
    done = lambda x, y: (max(x, y) >= 6 and abs(x - y) >= 2) or max(x, y) == 7
    sh = sa = 0
    for x, y in toks[:-1]:
        sh += x > y; sa += y > x
    x, y = toks[-1]
    if done(x, y):
        return sh + (x > y), sa + (y > x), 0, 0
    return sh, sa, x, y


def _rows(df):
    cols = [df.winner_name, df.loser_name, df.surface, df.tour, df.tourney_date]
    cols += [df[c] if c in df else pd.Series(float("nan"), index=df.index) for c in STAT_COLS]
    for w, l, s, t, d, a, b, c, e, f, g in zip(*cols):
        st = None
        if not any(pd.isna(x) for x in (a, b, c, e, f, g)) and a > 0 and e > 0:
            st = (b + c, a, f + g, e)
        yield w, l, s, t, d, st


class Ratings:
    DECAY, PRIOR, SURF_PRIOR, MIN_PTS = 0.97, 250, 150, 300

    def __init__(self):
        self.o, self.s, self.n, self.ns, self.form = {}, {}, {}, {}, {}
        self.pt, self.tour, self.mu = {}, {}, {}
        self.index, self.last = {}, {}

    @staticmethod
    def _k(n):
        return 250 / ((n + 5) ** 0.4)

    @staticmethod
    def _p(ra, rb):
        return 1 / (1 + 10 ** ((rb - ra) / 400))

    def _add_pts(self, p, surf, sw, st, rw, rt):
        for key in ((p, None), (p, surf)):
            c = self.pt.setdefault(key, [0.0, 0.0, 0.0, 0.0])
            for i in range(4):
                c[i] *= self.DECAY
            c[0] += sw; c[1] += st; c[2] += rw; c[3] += rt

    def update(self, w, l, surf, stats=None, tour=None):
        for p in (w, l):
            self.o.setdefault(p, 1500.0); self.s.setdefault((p, surf), 1500.0)
            self.n.setdefault(p, 0); self.ns.setdefault((p, surf), 0)
            self.form.setdefault(p, deque(maxlen=10))
        ew = self._p(self.o[w], self.o[l])
        kw, kl = self._k(self.n[w]), self._k(self.n[l])
        self.o[w] += kw * (1 - ew); self.o[l] -= kl * (1 - ew)
        ew = self._p(self.s[(w, surf)], self.s[(l, surf)])
        kw, kl = self._k(self.ns[(w, surf)]), self._k(self.ns[(l, surf)])
        self.s[(w, surf)] += kw * (1 - ew); self.s[(l, surf)] -= kl * (1 - ew)
        for p in (w, l):
            self.n[p] += 1; self.ns[(p, surf)] += 1
        self.form[w].append(1); self.form[l].append(0)
        if stats:
            wsw, wst, lsw, lst = stats
            self._add_pts(w, surf, wsw, wst, lst - lsw, lst)
            self._add_pts(l, surf, lsw, lst, wst - wsw, wst)
            if tour in ("atp", "wta"):
                self.tour[w] = self.tour[l] = tour
                m = self.mu.setdefault(tour, [0.0, 0.0]); m[0] += wsw + lsw; m[1] += wst + lst

    def finalize(self):
        self.index, self.last = {}, {}
        for name in self.o:
            n = norm(name); self.index[n] = name
            self.last.setdefault(n.split()[-1], []).append(name)

    def _mu(self, p):
        m = self.mu.get(self.tour.get(p))
        return m[0] / m[1] if m and m[1] else 0.62

    def tour_mu(self, tour):
        m = self.mu.get(tour)
        return m[0] / m[1] if m and m[1] else 0.62

    def pts(self, p, surf):
        o = self.pt.get((p, None))
        if not o or o[1] < self.MIN_PTS:
            return None
        mu = self._mu(p)
        spw0 = (o[0] + self.PRIOR * mu) / (o[1] + self.PRIOR)
        rpw0 = (o[2] + self.PRIOR * (1 - mu)) / (o[3] + self.PRIOR)
        sf = self.pt.get((p, surf), [0.0, 0.0, 0.0, 0.0]); K = self.SURF_PRIOR
        return (sf[0] + K * spw0) / (sf[1] + K), (sf[2] + K * rpw0) / (sf[3] + K), mu

    def _eff(self, p, surf, w_surf, form_pts):
        ws = w_surf * min(1.0, self.ns.get((p, surf), 0) / 15)
        base = (1 - ws) * self.o[p] + ws * self.s.get((p, surf), self.o[p])
        f = self.form[p]
        fm = sum(f) / len(f) if len(f) >= 3 else 0.5
        return base + form_pts * (fm - 0.5) * 2, fm

    def predict_parts(self, a, b, surf, w_surf=0.65, form_pts=55, best_of=3):
        ra, fa = self._eff(a, surf, w_surf, form_pts)
        rb, fb = self._eff(b, surf, w_surf, form_pts)
        pe = self._p(ra, rb)
        A, B = self.pts(a, surf), self.pts(b, surf)
        pp = None
        if A and B:
            mu = (A[2] + B[2]) / 2
            sa = min(max(A[0] - (B[1] - (1 - mu)), 0.35), 0.85)
            sb = min(max(B[0] - (A[1] - (1 - mu)), 0.35), 0.85)
            pp = p_match_from_points(sa, sb, best_of)
        return pe, pp, fa, fb

    def predict(self, a, b, surf, w_surf=0.65, form_pts=55, w_point=0.35, best_of=3):
        pe, pp, fa, fb = self.predict_parts(a, b, surf, w_surf, form_pts, best_of)
        return (pe if pp is None else (1 - w_point) * pe + w_point * pp), fa, fb

    def explain(self, a, b, surf):
        s = f"Elo {self.o[a]:.0f} v {self.o[b]:.0f} | {surf} Elo {self.s.get((a, surf), 1500):.0f} v {self.s.get((b, surf), 1500):.0f}"
        A, B = self.pts(a, surf), self.pts(b, surf)
        if A and B:
            s += f" | serve pts won {A[0]:.1%} v {B[0]:.1%} | return pts won {A[1]:.1%} v {B[1]:.1%}"
        return s

    def find(self, name):
        n = norm(name)
        if n in self.index:
            return self.index[n]
        toks = n.split()
        if not toks:
            return None
        cands = self.last.get(toks[-1], [])
        if len(toks) > 1:
            cands = [c for c in cands if norm(c).split()[0][0] == toks[0][0]] or []
            if not cands:
                cands = [c for c in self.last.get(toks[-1], []) if norm(c).endswith(" " + n)]
        if cands:
            return max(cands, key=lambda c: self.o[c])
        import difflib
        m = difflib.get_close_matches(n, list(self.index), n=1, cutoff=0.88)
        return self.index[m[0]] if m else None


def build_ratings(df=None):
    df = load_matches() if df is None else df
    R = Ratings()
    for w, l, s, t, d, st in _rows(df):
        R.update(w, l, s, st, t)
    R.finalize()
    return R


def calibration(df=None):
    df = load_matches() if df is None else df
    cutoff = df.tourney_date.max() - 10000
    R = Ratings(); acc = {"elo": [], "pts": [], "blend": []}
    for w, l, s, t, d, st in _rows(df):
        if d >= cutoff and R.n.get(w, 0) >= 10 and R.n.get(l, 0) >= 10:
            pe, pp, _, _ = R.predict_parts(w, l, s)
            if pp is not None:
                acc["elo"].append(pe); acc["pts"].append(pp); acc["blend"].append(0.5 * pe + 0.5 * pp)
        R.update(w, l, s, st, t)
    ll = lambda ps: -sum(math.log(max(p, 1e-6)) for p in ps) / len(ps)
    n = len(acc["blend"])
    return {"matches": n, "accuracy": sum(p > .5 for p in acc["blend"]) / n, "log_loss": ll(acc["blend"]),
            "log_loss_elo_only": ll(acc["elo"]), "log_loss_points_only": ll(acc["pts"]),
            "acc_elo_only": sum(p > .5 for p in acc["elo"]) / n, "acc_points_only": sum(p > .5 for p in acc["pts"]) / n,
            "coin_flip_log_loss": math.log(2)}


# ----------------------------------------------------------------- Polymarket
CLOB = "https://clob.polymarket.com"

# ----------------------------------------------------------------- trading rules (tune after backtesting)
FEE_RATE = 0.05                 # Polymarket sports taker rate: fee = rate * p * (1-p) per share. VERIFY on Polymarket's fee page.
MIN_EDGE = 0.05                 # required (model_p - price - fee); the old code bought at any edge > 0
MIN_PRICE, MAX_PRICE = 0.30, 0.80
STAKE_FRAC = 0.02               # default stake as share of bankroll (was 5%)
ALLOW_LIVE_WITHOUT_SCORE = False  # live match but no reliable score feed -> PASS

# --- strategy selection ("favourite" | "underdog" | "both")
STRATEGY = "underdog"
MODEL_WEIGHT = 0.5              # adjusted_p = w*model + (1-w)*market_fair. The raw model is ~3-5 pts overconfident on
                                # favourites (walk-forward check), so it is pulled toward the market before any edge is computed.
UNDERDOG_MIN_PRICE, UNDERDOG_MAX_PRICE = 0.15, 0.40   # market underdog = the cheaper side, bought inside this band
UNDERDOG_STAKE_FRAC = 0.01      # underdog losing streaks are long: 1% of bankroll per bet

def adjusted_prob(p_model, p_market, w=None):
    """Model probability shrunk toward the (overround-free) market probability."""
    w = MODEL_WEIGHT if w is None else w
    return w * p_model + (1 - w) * p_market

def taker_fee(price, rate=None):
    """Taker fee per share (USD) at a given price."""
    r = FEE_RATE if rate is None else rate
    return r * price * (1 - price)


def get_book(token_id):
    r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=15)
    r.raise_for_status()
    j = r.json()
    asks = sorted((float(x["price"]), float(x["size"])) for x in j.get("asks", []))
    bids = sorted(((float(x["price"]), float(x["size"])) for x in j.get("bids", [])), reverse=True)
    return bids, asks


def fill(levels, usd=None, shares=None):
    s = c = 0.0
    for price, size in levels:
        rem = (usd - c) / price if usd is not None else shares - s
        if rem <= 1e-9:
            break
        take = min(size, rem)
        s += take; c += take * price
    return s, c, (c / s if s else None)


def _j(x):
    return json.loads(x) if isinstance(x, str) else (x or [])


def tennis_series():
    try:
        data = requests.get(f"{GAMMA}/sports", timeout=20).json()
    except Exception:
        return []
    return [str(s["series"]) for s in data if isinstance(data, list) and isinstance(s, dict)
            and str(s.get("sport", "")).lower() in ("atp", "wta", "tennis") and s.get("series")]


def _paginate(params, max_events, stop_before=None):
    out, off, p = [], 0, dict(params)
    while off < max_events:
        p.update(limit=100, offset=off)
        r = requests.get(f"{GAMMA}/events", params=p, timeout=25)
        if r.status_code != 200 and "order" in p:
            p.pop("order", None); p.pop("ascending", None)
            r = requests.get(f"{GAMMA}/events", params=p, timeout=25)
        if r.status_code != 200:
            break
        b = r.json()
        if not isinstance(b, list):
            break
        out += b
        if len(b) < 100:
            break
        if stop_before is not None and "order" in p:
            d = [pd.to_datetime(e.get("endDate"), utc=True, errors="coerce") for e in b]
            if all(pd.notna(x) and x.tz_convert(None) < stop_before for x in d):
                break
        off += 100
    return out


def fetch_events(max_events=400, closed=False, stop_before=None):
    base = {"closed": str(closed).lower(), "ascending": "false",
            "order": "endDate" if closed else "volume24hr"}
    if not closed:
        base["active"] = "true"
    routes = [{"series_id": s} for s in tennis_series()] + [{"tag_slug": t} for t in ("atp", "wta", "tennis")]
    seen, err = {}, None
    for r in routes:
        try:
            for e in _paginate({**base, **r}, max_events, stop_before):
                seen.setdefault(e.get("id"), e)
        except requests.RequestException as ex:
            err = ex
    if not seen and err:
        raise err
    evs = [e for e in seen.values() if _is_match(e)]
    return sorted(evs, key=lambda e: -float(e.get("volume24hr") or 0))


BAD = ("over", "under", "yes", "no")
NOT_ML = re.compile(r"\b(sets?|handicap|spread|o/u|total|games?)\b|\([+-]\d", re.I)


def pick_moneyline(ev):
    ms = ev.get("markets", [])
    for strict in (True, False):
        for m in ms:
            outs = [str(o).lower() for o in _j(m.get("outcomes"))]
            if len(outs) != 2 or any(o in BAD for o in outs):
                continue
            if strict and m.get("sportsMarketType") == "moneyline":
                return m
            if not strict and not NOT_ML.search(m.get("question") or ""):
                return m
    return None


def _is_match(ev):
    m = pick_moneyline(ev)
    return bool(m) and bool(re.search(r"\bvs\.?\b", f"{ev.get('title', '')} {m.get('question', '')}", re.I))


SURF_KEYS = {
    "Clay": ["roland garros", "french open", "monte carlo", "monte-carlo", "madrid", "rome", "italian open",
             "barcelona", "hamburg", "clay", "estoril", "bucharest", "geneva", "lyon", "umag", "gstaad", "bastad", "kitzbuhel"],
    "Grass": ["wimbledon", "halle", "queen", "grass", "eastbourne", "mallorca", "s-hertogenbosch", "newport", "stuttgart"],
}

SLAMS = ("australian open", "roland garros", "french open", "wimbledon", "us open")


def guess_best_of(ev):
    t = norm(f"{ev.get('title', '')} {ev.get('slug', '')}")
    return 5 if any(k in t for k in SLAMS) and str(ev.get("slug", "")).startswith("atp") else 3


def _has_kw(text, k):
    """Whole-word match (the old substring match let 'rome' hit 'Jerome')."""
    return re.search(r"(?<![a-z])" + re.escape(norm(k)) + r"(?![a-z])", text) is not None


def detect_surface(ev, override=None):
    if override:
        return override
    text = norm(f"{ev.get('title','')}" f" {ev.get('slug','')} {ev.get('seriesSlug','')} {ev.get('description','')[:300]}")
    for surf, keys in SURF_KEYS.items():
        if any(_has_kw(text, k) for k in keys):
            return surf
    return "Hard"


def analyse(events, R, bankroll=1000, surface=None, allow_live=True, min_games=5, live_scores=None, target_date=None, strategy=None):
    now = pd.Timestamp.now(tz="UTC")
    rows = []
    for ev in events:
        m = pick_moneyline(ev)
        if not m:
            continue
        if m.get("closed") or ev.get("closed") or ev.get("ended"):
            continue

        start = pd.to_datetime(m.get("gameStartTime") or ev.get("startTime"), utc=True, errors="coerce")
        end_time = pd.to_datetime(m.get("endDate") or ev.get("endDate"), utc=True, errors="coerce")

        if pd.notna(end_time) and end_time < now:
            continue
        if pd.notna(start) and start < now - pd.Timedelta(hours=5) and not ev.get("live"):
            continue

        if target_date is not None:
            if pd.isna(start) or start.tz_convert("UTC").date() != target_date:
                continue

        outs = _j(m.get("outcomes")); px = [float(x) for x in _j(m.get("outcomePrices")) or [0, 0]]
        if len(px) != 2 or min(px) <= 0.01 or max(px) >= 0.99:
            continue

        stt = live_scores.lookup(ev.get("slug"), outs) if live_scores else None
        if stt and stt.get("ended"):
            continue

        bid, ask = m.get("bestBid"), m.get("bestAsk")
        ask0 = float(ask) if ask else px[0]
        ask1 = 1 - float(bid) if bid else px[1]
        asks = [min(ask0, 0.99), min(ask1, 0.99)]
        live = bool(ev.get("live")) or (pd.notna(start) and start <= now)
        surf = detect_surface(ev, surface)
        a, b = R.find(outs[0]), R.find(outs[1])
        chg = float(m.get("oneDayPriceChange") or 0)
        row = {"match": f"{outs[0]} vs {outs[1]}", "surface": surf, "live": live,
               "vol_24h": float(m.get("volume24hr") or ev.get("volume24hr") or 0),
               "vol_total": float(m.get("volumeNum") or 0), "liquidity": float(m.get("liquidityNum") or 0),
               "p1": px[0], "p2": px[1], "chg_24h_p1": chg, "start": start,
               "signal": "", "pick": "", "price": None, "model_p": None, "edge": None, "ev": None,
               "stake": None, "payout_x": None, "form": "", "url": f"https://polymarket.com/event/{ev.get('slug','')}",
               "market_id": m.get("id"), "tokens": _j(m.get("clobTokenIds")), "outs": outs,
               "asks": asks, "blend": None, "side": None, "why": "", "role": "", "score": "", "live_check": "", "adj": None, "strategy": "", "model_raw": None}

        if "/" in outs[0] or "/" in outs[1]:
            row["signal"] = "doubles - skipped"
        elif a and b and a != b and min(R.n[a], R.n[b]) >= min_games:
            # 1. Pre-match foundational model prediction (surface, skill, Elo)
            p0, fa, fb = R.predict(a, b, surf, best_of=guess_best_of(ev))
            pm = [p0, 1 - p0]; form = [fa, fb]
            
            fav_i = 0 if p0 >= 0.5 else 1               # pre-match model favourite (locked; live score only moves its probability)
            und_i = 0 if asks[0] <= asks[1] else 1      # market underdog = the cheaper side
            live_ok = True

            if live:
                if not stt and ev.get("score"):
                    stt = {"score": ev["score"], "a_is_home": True, "oriented": False, "ended": bool(ev.get("ended"))}
                ps = parse_score(stt["score"]) if stt else None
                if ps and not stt.get("ended"):
                    sh_, sw_, gh_, gw_ = ps
                    SA, SB, GA, GB = (sh_, sw_, gh_, gw_) if stt["a_is_home"] else (sw_, sh_, gw_, gh_)
                    bo = guess_best_of(ev)
                    mu = R.tour_mu("wta" if str(ev.get("slug", "")).startswith("wta") else "atp")
                    spa, spb = points_for_prob(p0, mu, bo)
                    pl = live_win_prob(spa, spb, SA, SB, GA, GB, bo)
                    pm = [pl, 1 - pl]
                    row["score"] = f"{outs[0]} {SA}-{SB} {outs[1]} (sets) \u00b7 games {GA}-{GB}"
                    if not stt.get("oriented", True):
                        row["live_check"] = "score order unknown - skipped"
                        live_ok = False
                else:
                    row["live_check"] = "live, no score feed"
                    live_ok = ALLOW_LIVE_WITHOUT_SCORE

            tot = asks[0] + asks[1]
            mkt = [asks[0] / tot, asks[1] / tot]                       # market probability without the spread
            adj = [adjusted_prob(pm[k], mkt[k]) for k in (0, 1)]       # model shrunk toward market

            strat = strategy or STRATEGY
            cands = []
            if strat in ("favourite", "both"):
                cands.append(("favourite", fav_i, MIN_PRICE, MAX_PRICE, STAKE_FRAC))
            if strat in ("underdog", "both"):
                cands.append(("underdog", und_i, UNDERDOG_MIN_PRICE, UNDERDOG_MAX_PRICE, UNDERDOG_STAKE_FRAC))
            recs = []
            for kind, k, lo, hi, frac in cands:
                price = asks[k]; fee = taker_fee(price); edge = adj[k] - (price + fee)
                if not live_ok:
                    why = "PASS (live score unreliable)"
                elif not (lo <= price <= hi):
                    why = f"PASS ({kind}: price {price:.2f} outside {lo:.2f}-{hi:.2f})"
                elif edge < MIN_EDGE:
                    why = f"PASS ({kind}: edge {edge:+.1%} < {MIN_EDGE:.0%})"
                else:
                    why = None
                recs.append(dict(kind=kind, k=k, price=price, fee=fee, edge=edge, frac=frac, why=why))
            passing = [c for c in recs if c["why"] is None]
            best = max(passing, key=lambda c: c["edge"]) if passing else recs[0]
            k = best["k"]
            sig = (f"BUY {outs[k]} ({best['kind']}: adj {adj[k]:.0%} vs price {best['price']:.0%})"
                   if best["why"] is None else best["why"])

            row["blend"] = pm
            row["why"] = R.explain(a, b, surf)
            row["role"] = "underdog" if best["price"] < 0.5 else "favourite"
            row.update(side=k, pick=outs[k], price=best["price"], model_p=adj[k], model_raw=pm[k], adj=adj,
                       strategy=best["kind"], edge=best["edge"], ev=adj[k] / (best["price"] + best["fee"]) - 1,
                       payout_x=1 / best["price"], form=f"{form[0]:.0%} / {form[1]:.0%}",
                       stake=round(bankroll * best["frac"], 2), signal=sig)
        else:
            row["signal"] = "thin data - skipped" if (a and b and a != b) else "no data"
        rows.append(row)
    df = pd.DataFrame(rows)
    return df.sort_values("vol_24h", ascending=False).reset_index(drop=True) if len(df) else df


def log_signals(df, path=os.path.join(DATA_DIR, "signals_log.csv")):
    s = df[df.signal.str.startswith("BUY")].copy()
    if s.empty:
        return
    s["logged_at"] = datetime.now(timezone.utc).isoformat(); s["won"] = ""
    keep = ["logged_at", "match", "pick", "surface", "price", "model_p", "edge", "stake", "url", "won"]
    s[keep].to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def health_check():
    print("== 1. Match history (Sackmann) ==")
    df = load_matches()
    for t, g in df.groupby("tour"):
        print(f"  {t.upper()}: {len(g):,} matches, latest tournament start {g.tourney_date.max()}")
    R = build_ratings(df)
    top = sorted(R.o.items(), key=lambda x: -x[1])[:8]
    print("  Top Elo:", ", ".join(f"{n} {r:.0f}" for n, r in top))
    evs = fetch_events()
    d = analyse(evs, R)
    print(f"  Analysed {len(d)} matches; BUY signals now: {d.signal.str.startswith('BUY').sum()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "calib", "sim", "check"])
    ap.add_argument("--bankroll", type=float, default=1000)
    ap.add_argument("--price", type=float, default=1 / 3)
    ap.add_argument("--win", type=float, default=0.40)
    ap.add_argument("--surface", default=None)
    a = ap.parse_args()
    pd.set_option("display.width", 220, "display.max_columns", 30)

    if a.cmd == "check":
        health_check()
    elif a.cmd == "sim":
        stake, n = 100, 10
        w = round(n * a.win)
        profit = w * stake * (1 / a.price - 1) - (n - w) * stake
        print(f"Price {a.price:.2f} -> payout {1/a.price:.2f}x. Break-even win rate = {a.price:.1%}")
        print(f"{n} bets x ${stake}, {w} wins: profit ${profit:,.0f} (ROI {profit/(stake*n):.1%})")
    elif a.cmd == "calib":
        print(calibration())
    else:
        R = build_ratings()
        df = analyse(fetch_events(), R, bankroll=a.bankroll, surface=a.surface)
        cols = ["match", "surface", "live", "vol_24h", "p1", "pick", "price", "model_p", "edge", "signal"]
        print(df[cols].head(40).to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
        log_signals(df)
        print(f"\n{(df.signal.str.startswith('BUY')).sum()} BUY signals logged to data/signals_log.csv")


if __name__ == "__main__":
    main()
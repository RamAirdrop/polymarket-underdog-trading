"""
Backtest engine for the Polymarket tennis model  (v3 - fixed + underdog strategy).

What changed vs v1
  * p1/p2 are now the PRE-MATCH prices (CLOB price history, last trade >= PRICE_LAG_MIN before start).
    v1 stored the final settlement prices (1.0 / 0.0), so every "bet" was bought at ~0c on the losing side.
  * Ratings are built incrementally with a real date cutoff (start - LAG_DAYS). v1 subtracted 1400 from a
    YYYYMMDD integer, which froze ratings at the end of the previous year.
  * The model is the same blended predictor used live (Ratings.predict), not Elo-only.
  * Taker fee (tennis_core.FEE_RATE) is charged; stakes can't exceed the bankroll; ROI uses real stakes.
  * Matches without a real scheduled start time are skipped (v1 fell back to endDate = look-ahead).
  * Data goes to backtest_v2.csv, so the broken v1 file is never reused.

v4: strategy "stack" (edge_stack.py) = value + fatigue veto/boost + grade-based Kelly sizing.  The sharp-line layer has no
historical data here, so it is OFF in the backtest (grades are therefore capped at C/B; compare flat-stake ROI instead).
Fatigue is only measured on matches whose dates are exact (Polymarket-era rows), see edge_stack.Workload.snapshot.

NOT tested against the live API from the environment this was written in - run `python backtest.py --fetch 300`
first and inspect data/backtest_v2.csv (p1+p2 should be ~1.0 and strictly between 0.03 and 0.97).
"""
import os, math, time
import numpy as np
import pandas as pd
import requests
import edge_stack as es
import tennis_core as tc

DS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest_v2.csv")
FAT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest_v2_fatigue.csv")
COLS = ["event_id", "market_id", "match", "surface", "start", "p1", "p2", "model_p1", "model_p2", "winner", "n1", "n2"]
LAG_DAYS = 21          # Sackmann dates are tournament START dates: 21d keeps a 2-week Slam's own results out
PRICE_LAG_MIN = 30     # price must be from at least this many minutes before the start


# ------------------------------------------------------------------ pre-match price
def pre_match_price(token_id, start_ts, minutes_before=PRICE_LAG_MIN):
    """Last traded price at least `minutes_before` minutes before start; None if no history."""
    end = int(start_ts) - minutes_before * 60
    for window_h, fidelity in ((6, 5), (24, 30), (72, 60)):
        try:
            r = requests.get(f"{tc.CLOB}/prices-history",
                             params={"market": token_id, "startTs": end - window_h * 3600,
                                     "endTs": end, "fidelity": fidelity}, timeout=20)
            if r.status_code != 200:
                continue
            hist = [h for h in r.json().get("history", []) if h.get("t", 0) <= end]
            if hist:
                return float(hist[-1]["p"])
        except (requests.RequestException, ValueError, KeyError):
            continue
    return None


def _candidates(evs):
    """Resolved single matches with a REAL scheduled start time, oldest first."""
    out = []
    for ev in evs:
        m = tc.pick_moneyline(ev)
        if not m:
            continue
        raw = m.get("gameStartTime") or ev.get("startTime")      # no endDate fallback (would leak the result)
        start = pd.to_datetime(raw, utc=True, errors="coerce")
        if pd.isna(start):
            continue
        outs = tc._j(m.get("outcomes"))
        px = [float(x) for x in tc._j(m.get("outcomePrices")) or [0, 0]]
        toks = tc._j(m.get("clobTokenIds"))
        if len(outs) != 2 or len(px) != 2 or len(toks) != 2 or max(px) < 0.99:
            continue
        if "/" in outs[0] or "/" in outs[1]:                       # doubles
            continue
        out.append((start, ev, m, outs, 0 if px[0] >= 0.99 else 1, toks))
    return sorted(out, key=lambda x: x[0])


def fetch_dataset(max_events=1500, progress=None):
    sack = tc.load_matches()
    rows = list(tc._rows(sack))                  # (winner, loser, surface, tour, date_int, stats), already date-sorted
    existing = set(pd.read_csv(DS).event_id.astype(str)) if os.path.exists(DS) else set()
    cands = _candidates(tc.fetch_events(max_events=max_events, closed=True))
    R, ptr, buf, total = tc.Ratings(), 0, [], len(cands)

    def flush():
        nonlocal buf
        if buf:
            pd.DataFrame(buf)[COLS].to_csv(DS, mode="a", header=not os.path.exists(DS), index=False)
            buf = []

    for idx, (start, ev, m, outs, w_idx, toks) in enumerate(cands):
        if progress:
            progress(idx + 1, total)
        eid = str(ev.get("id"))
        if eid in existing:
            continue
        # advance ratings to matches whose tournament started on/before (start - LAG_DAYS)
        cutoff = int((start - pd.Timedelta(days=LAG_DAYS)).strftime("%Y%m%d"))
        while ptr < len(rows) and rows[ptr][4] <= cutoff:
            w, l, s, t, d, st = rows[ptr]
            R.update(w, l, s, st, t)
            ptr += 1
        if ptr < 1000:
            continue
        R.finalize()
        a, b = R.find(outs[0]), R.find(outs[1])
        if not a or not b or a == b:
            continue

        p1 = pre_match_price(toks[0], start.timestamp()); time.sleep(0.05)
        p2 = pre_match_price(toks[1], start.timestamp()); time.sleep(0.05)
        if p1 is None or p2 is None:
            continue
        if not (0.90 <= p1 + p2 <= 1.10) or min(p1, p2) < 0.03 or max(p1, p2) > 0.97:
            continue                                                 # stale / already-decided / bad data

        surf = tc.detect_surface(ev)
        p0, _, _ = R.predict(a, b, surf, best_of=tc.guess_best_of(ev))    # same blended model as live
        buf.append(dict(event_id=eid, market_id=m.get("id"), match=f"{outs[0]} vs {outs[1]}",
                        surface=surf, start=start.isoformat(), p1=p1, p2=p2,
                        model_p1=p0, model_p2=1 - p0, winner=outs[w_idx],
                        n1=R.n.get(a, 0), n2=R.n.get(b, 0)))
        if len(buf) >= 50:
            flush()
    flush()
    return total


def load_dataset():
    ds = pd.read_csv(DS)
    ds["start"] = pd.to_datetime(ds.start, utc=True, errors="coerce")
    ds = ds.dropna(subset=["start"]).drop_duplicates("event_id")
    if os.path.exists(FAT):                                   # fatigue flags per side, built by enrich_dataset()
        fat = pd.read_csv(FAT).drop_duplicates("event_id")
        ds = ds.merge(fat, on="event_id", how="left")
    for c in ("fat1", "fat2"):
        ds[c] = ds[c].fillna("unknown") if c in ds else "unknown"
    return ds.sort_values("start").reset_index(drop=True)


def enrich_dataset(hist=None):
    """Add fatigue flags (fat1 / fat2 = flag if we back outcome 1 / 2) for rows that lack them. Resumable."""
    ds = pd.read_csv(DS)
    ds["start"] = pd.to_datetime(ds.start, utc=True, errors="coerce")
    have = set(pd.read_csv(FAT).event_id.astype(str)) if os.path.exists(FAT) else set()
    todo = ds[~ds.event_id.astype(str).isin(have)].dropna(subset=["start"])
    if todo.empty:
        return 0
    hist = tc.load_matches() if hist is None else hist
    R, W, out = tc.build_ratings(hist), es.Workload.from_matches(hist), []
    for r in todo.itertuples():
        outs = r.match.split(" vs ")
        a, b = (R.find(outs[0]), R.find(outs[1])) if len(outs) == 2 else (None, None)
        f1 = f2 = "unknown"
        if a and b and a != b:
            day = es.day_of(r.start)
            s1, s2 = (W.snapshot(x, day, exact_only=True) for x in (a, b))
            f1, f2 = es.fatigue_flag(s1, s2)[0], es.fatigue_flag(s2, s1)[0]
        out.append(dict(event_id=r.event_id, fat1=f1, fat2=f2))
    pd.DataFrame(out).to_csv(FAT, mode="a", header=not os.path.exists(FAT), index=False)
    return len(out)


# ------------------------------------------------------------------ strategy replay
DEFAULTS = dict(min_edge=tc.MIN_EDGE, min_ev=0.0, min_prob=0.25, max_price=tc.MAX_PRICE, min_price=tc.MIN_PRICE,
                slip=0.01, fee_rate=tc.FEE_RATE, fee=0.0,   # fee_rate = taker rate; fee = extra % of profit (legacy)
                min_games=15, mode="flat", stake=20.0, bankroll=1000.0, kelly_frac=0.25, cap=0.05,
                shrink=tc.MODEL_WEIGHT,      # weight on the model vs the market (1.0 = raw model)
                strategy="any",              # "any" (both sides) | "favourite" | "underdog" | "stack" (edge_stack.decide)
                use_fatigue=True)            # stack only: apply the fatigue veto / rest boost

# ready-made settings that mirror the live strategies in tennis_core.analyse
PRESETS = {
    "underdog":  dict(strategy="underdog", min_price=tc.UNDERDOG_MIN_PRICE, max_price=tc.UNDERDOG_MAX_PRICE,
                      min_prob=0.0, min_edge=tc.MIN_EDGE, min_ev=0.0, shrink=tc.MODEL_WEIGHT),
    "favourite": dict(strategy="favourite", min_price=tc.MIN_PRICE, max_price=tc.MAX_PRICE,
                      min_prob=0.0, min_edge=tc.MIN_EDGE, min_ev=0.0, shrink=tc.MODEL_WEIGHT),
    # stack, maker entry (no taker fee). OPTIMISTIC: assumes every limit order fills at last trade + slip.
    "stack":       dict(strategy="stack", min_prob=0.0, shrink=tc.MODEL_WEIGHT, fee_rate=es.MAKER_FEE_RATE, use_fatigue=True),
    # stack, taker entry (pays taker fee): the conservative bound for the same signals.
    "stack_taker": dict(strategy="stack", min_prob=0.0, shrink=tc.MODEL_WEIGHT, fee_rate=tc.FEE_RATE, use_fatigue=True),
}

_COLS_OUT = ["start", "match", "pick", "ask", "model_p", "stake", "won", "pnl", "bankroll", "surface", "grade", "fatigue"]


def run_backtest(ds, **kw):
    """Replay: bet the side with enough edge AFTER slippage and taker fee. Unknown kwargs are ignored."""
    P = {**DEFAULTS, **kw}
    bank0 = bank = P["bankroll"]; bets = []
    for r in ds.sort_values("start").itertuples():
        if pd.isna(r.model_p1) or r.n1 < P["min_games"] or r.n2 < P["min_games"]:
            continue
        outs = r.match.split(" vs ")
        if len(outs) != 2:
            continue
        tot = r.p1 + r.p2
        sides = [(outs[0], r.model_p1, r.p1, r.p1 / tot), (outs[1], r.model_p2, r.p2, r.p2 / tot)]
        if P["strategy"] == "underdog":
            sides = [min(sides, key=lambda x: x[2])]                       # the cheaper side only
        elif P["strategy"] == "favourite":
            sides = [max(sides, key=lambda x: x[1])]                       # the model favourite only
        best, stack_d = None, None
        if P["strategy"] == "stack":
            for idx, (p, model_p, ask, mkt_p) in enumerate(sides):
                eff = min(ask + P["slip"], 0.99)
                adj_p = tc.adjusted_prob(model_p, mkt_p, P["shrink"])
                fee_ps = tc.taker_fee(eff, P["fee_rate"])
                flag = getattr(r, "fat1" if idx == 0 else "fat2", "unknown")
                d = es.decide(adj_p, eff, fee_ps, None, flag, use_fatigue=P["use_fatigue"], require_sharp=False)
                if d["ok"] and (best is None or d["edge"] > best[0]):
                    best, stack_d = (d["edge"], p, adj_p, eff, eff + fee_ps), {**d, "fatigue": flag}
            sides = []                                                    # skip the generic loop below
        for p, model_p, ask, mkt_p in sides:
            eff = min(ask + P["slip"], 0.99)
            adj_p = tc.adjusted_prob(model_p, mkt_p, P["shrink"])           # model pulled toward the market
            if model_p < P["min_prob"] or not (P["min_price"] <= ask <= P["max_price"]):
                continue
            cost_ps = eff + tc.taker_fee(eff, P["fee_rate"])               # all-in price per share
            edge = adj_p - cost_ps
            if edge < P["min_edge"] or adj_p / cost_ps - 1 < P["min_ev"]:
                continue
            if best is None or edge > best[0]:
                best = (edge, p, adj_p, eff, cost_ps)
        if best is None:
            continue
        edge, p, adj_p, eff, cost_ps = best
        stake = P["stake"]
        if stack_d and P["use_fatigue"] and stack_d["fatigue"] == "rest_edge":
            stake *= es.REST_BOOST
        if stack_d and P["mode"] == "kelly":
            stake = bank * stack_d["frac"]                                 # quarter-Kelly, grade cap, rest boost already applied
        elif P["mode"] == "kelly":
            b_odds = 1 / cost_ps - 1
            f = (adj_p * b_odds - (1 - adj_p)) / b_odds
            stake = min(max(0.0, bank * P["kelly_frac"] * f), bank * P["cap"])
        stake = min(stake, bank)                                           # cannot bet more than you have
        if stake < 1:
            continue
        won = r.winner.strip().lower() == p.strip().lower()
        shares = stake / cost_ps
        gross = shares if won else 0.0
        pnl = gross - stake - ((gross - stake) * P["fee"] if won and P["fee"] > 0 else 0.0)
        bank += pnl
        bets.append(dict(start=r.start, match=r.match, pick=p, ask=eff, model_p=adj_p, stake=stake,
                         won=won, pnl=pnl, bankroll=bank, surface=r.surface,
                         grade=stack_d["grade"] if stack_d else "", fatigue=stack_d["fatigue"] if stack_d else ""))
    out = pd.DataFrame(bets, columns=_COLS_OUT)
    out.attrs["bankroll0"] = bank0
    return out


def summarize(bets):
    base = dict(bets=0, hit_rate=0, roi=0, pnl=0, max_drawdown=0, roi_95ci=(0, 0), **{"avg_price(break-even)": 0})
    if bets.empty:
        return base
    n = len(bets)
    staked = bets.stake.sum()
    pnl = bets.pnl.sum()
    roi = pnl / staked                                             # on money actually staked
    rets = (bets.pnl / bets.stake).values
    se = rets.std(ddof=1) / math.sqrt(n) if n > 1 else 0.0
    bank0 = bets.attrs.get("bankroll0", 1000.0)
    curve = np.concatenate([[bank0], bets.bankroll.values])
    dd = float(((curve - np.maximum.accumulate(curve)) / np.maximum.accumulate(curve)).min())
    return dict(bets=n, hit_rate=float(bets.won.mean()), roi=float(roi), pnl=float(pnl), max_drawdown=dd,
                roi_95ci=(roi - 1.96 * se, roi + 1.96 * se), **{"avg_price(break-even)": float(bets.ask.mean())})


def _y(ds):
    return np.array([1 if w.strip().lower() == m.split(" vs ")[0].strip().lower() else 0
                     for w, m in zip(ds.winner, ds.match)])


def diagnostics(ds):
    """Model vs market log-loss on the same matches. Lower is better; market < model => no standalone edge."""
    from sklearn.metrics import log_loss
    valid = ds.dropna(subset=["model_p1", "p1", "winner"]).copy()
    if valid.empty:
        return {}, pd.DataFrame()
    valid["y"] = _y(valid)
    mk = (valid.p1 / (valid.p1 + valid.p2)).clip(1e-6, 1 - 1e-6)       # remove the small overround
    md = valid.model_p1.clip(1e-6, 1 - 1e-6)
    metrics = {"matches": len(valid),
               "model_log_loss": log_loss(valid.y, md), "market_log_loss": log_loss(valid.y, mk),
               "coin_flip_log_loss": math.log(2)}
    valid["bucket"] = pd.cut(valid.model_p1, bins=[0, .3, .4, .5, .6, .7, .8, 1])
    cal = valid.groupby("bucket", observed=True).agg(
        matches=("y", "size"), model_prob=("model_p1", "mean"),
        market_prob=("p1", "mean"), actual_win=("y", "mean")).reset_index()
    return metrics, cal


def baseline(ds, lo, hi, slip, fee_rate=tc.FEE_RATE):
    """Blindly bet EVERY side whose pre-match price (+slip) is in [lo, hi], after taker fees."""
    pnl = n = 0
    for r in ds.itertuples():
        first = r.match.split(" vs ")[0].strip().lower()
        for side_name, px in ((first, r.p1), (None, r.p2)):
            eff = px + slip
            if not (lo <= eff <= hi):
                continue
            won = (r.winner.strip().lower() == first) if side_name else (r.winner.strip().lower() != first)
            cost = eff + tc.taker_fee(eff, fee_rate)
            pnl += (1 / cost - 1) if won else -1
            n += 1
    return {"bets": n, "roi": pnl / n if n else 0}


def sweep(ds, mode="flat", stake=100.0, slip=0.01, fee=0.0):
    cut = ds.start.iloc[int(len(ds) * 0.7)]
    train, test = ds[ds.start < cut], ds[ds.start >= cut]
    roi = lambda b: (b.pnl.sum() / b.stake.sum()) if len(b) else 0
    results = []
    for min_edge in [0.0, 0.02, 0.05, 0.10]:
        for min_prob in [0.30, 0.40, 0.50, 0.60]:
            kw = dict(min_edge=min_edge, min_prob=min_prob, mode=mode, stake=stake, slip=slip, fee=fee)
            tr, te = run_backtest(train, **kw), run_backtest(test, **kw)
            results.append(dict(min_edge=min_edge, min_prob=min_prob, train_bets=len(tr), train_roi=roi(tr),
                                test_bets=len(te), test_roi=roi(te)))
    # NB: sorting by test_roi and picking the winner overfits the test set; treat as exploration only.
    return pd.DataFrame(results).sort_values("test_roi", ascending=False), cut


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", type=int, default=1500)
    a = ap.parse_args()
    fetch_dataset(a.fetch, progress=lambda i, t: print(f"\r{i}/{t}", end="", flush=True))
    print(f"\nfatigue flags added for {enrich_dataset()} matches")
    ds = load_dataset()
    print(f"\n{len(ds)} matches, {ds.start.min():%Y-%m-%d} .. {ds.start.max():%Y-%m-%d}")
    print("fatigue flags (pick side) in dataset:", pd.concat([ds.fat1, ds.fat2]).value_counts().to_dict())
    print("model vs market:", diagnostics(ds)[0])
    show = lambda S: {k: (tuple(round(float(x), 3) for x in v) if isinstance(v, tuple) else round(float(v), 3)) for k, v in S.items()}
    for name, kw in PRESETS.items():
        print(f"\n[{name}]", show(summarize(run_backtest(ds, stake=20, **kw))))
    print("\n[stack WITHOUT fatigue layer]", show(summarize(run_backtest(ds, stake=20, **{**PRESETS["stack"], "use_fatigue": False}))))
    print("\nBLIND underdogs (every side priced .15-.40, after fees):", baseline(ds, .15, .40, .01))
    print("BLIND favourites (every side priced .60-.85, after fees):", baseline(ds, .60, .85, .01))
    print("\nRead the 95% CI: if it includes 0 (or negative values) there is NO proven edge.")

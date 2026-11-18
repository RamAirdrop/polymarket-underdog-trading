"""Offline tests for the pre-match stack.   python -m pytest -q test_stack.py   (or: python test_stack.py)
No network: the order book is mocked and history is synthetic."""
import math
import os
import tempfile

import pandas as pd

import backtest as bt
import edge_stack as es
import paper
import tennis_core as tc


# ------------------------------------------------------------------ Layer 1/3 decision matrix
def test_decide_grades_and_vetoes():
    # B: edge >= 5%, sharp agrees, neutral rest
    d = es.decide(0.50, 0.42, 0.0, sharp_p=0.50, fatigue="neutral")
    assert d["ok"] and d["grade"] == "B" and d["sharp_flag"] == "agree"
    # A: edge >= 7% + sharp agrees + rest advantage -> bigger cap and 1.25x
    a = es.decide(0.55, 0.42, 0.0, sharp_p=0.55, fatigue="rest_edge")
    assert a["ok"] and a["grade"] == "A" and a["frac"] > d["frac"]
    assert a["frac"] <= es.MAX_MATCH_FRAC
    # trap: big model edge but heavy fatigue -> veto
    t = es.decide(0.60, 0.42, 0.0, sharp_p=0.60, fatigue="heavy")
    assert not t["ok"] and "fatigue" in t["reason"]
    assert es.decide(0.60, 0.42, 0.0, sharp_p=0.60, fatigue="heavy", use_fatigue=False)["ok"]   # layer can be switched off
    # trap: sharp book disagrees (sharp below our entry) -> veto even with a big model edge
    s = es.decide(0.70, 0.45, 0.0, sharp_p=0.40, fatigue="neutral")
    assert not s["ok"] and "sharp" in s["reason"]
    # no sharp line: allowed but smallest grade; required -> veto
    n = es.decide(0.50, 0.42, 0.0, sharp_p=None, fatigue="neutral")
    assert n["ok"] and n["grade"] == "C" and n["frac"] <= es.CAP_C
    assert not es.decide(0.50, 0.42, 0.0, None, "neutral", require_sharp=True)["ok"]
    # edge too small / price outside band
    assert not es.decide(0.45, 0.42, 0.0, None, "neutral")["ok"]
    assert not es.decide(0.95, 0.90, 0.0, None, "neutral")["ok"]
    # unknown fatigue is neutral: never boosts
    u = es.decide(0.55, 0.42, 0.0, sharp_p=0.55, fatigue="unknown")
    assert u["grade"] == "B"


def test_kelly_and_sizing_caps():
    assert es.kelly_fraction(0.5, 0.5) == 0.0
    assert es.kelly_fraction(0.6, 0.5) > 0
    d = es.decide(0.90, 0.45, 0.0, sharp_p=0.90, fatigue="rest_edge")
    assert d["frac"] <= es.MAX_MATCH_FRAC + 1e-12        # hard cap holds even for absurd edges


def test_devig_and_limit_and_depth():
    p1, p2 = es.devig(1.50, 2.80)
    assert math.isclose(p1 + p2, 1.0) and p1 > 0.6
    assert es.limit_price(0.40, 0.44) == 0.41            # 1c above the bid
    assert es.limit_price(0.40, 0.41) == 0.40            # spread of 1 tick: join the bid, never cross
    assert es.limit_price(None, 0.44) == 0.43            # no bid: one tick under the ask
    assert es.limit_price(0.40, None) is None
    ok, depth = es.depth_ok([(0.44, 100), (0.45, 100), (0.50, 1000)], stake_usd=10)
    assert ok and math.isclose(depth, 0.44 * 100 + 0.45 * 100)       # 0.50 is outside the 1c window
    assert not es.depth_ok([(0.44, 20)], stake_usd=10)[0]
    assert not es.depth_ok([], 10)[0]


# ------------------------------------------------------------------ Layer 2 workload
def _hist(rows):
    return pd.DataFrame(rows, columns=["tourney_date", "winner_name", "loser_name", "round", "draw_size", "minutes", "tour"])


def test_workload_fatigue_flags_and_no_lookahead():
    day = es.day_of("2026-10-10")
    rows = [  # exact-dated (tour='pm') matches
        (20261009, "Tired Tom", "X1", "", None, None, "pm"),     # played yesterday
        (20261008, "Tired Tom", "X2", "", None, None, "pm"),
        (20261007, "Tired Tom", "X3", "", None, None, "pm"),     # 3 matches in 4 days
        (20261001, "Fresh Fred", "X4", "", None, None, "pm"),    # 9 days ago
        (20261009, "Opp Ollie", "X5", "", None, None, "pm"),     # opp also played yesterday
        (20261010, "Tired Tom", "X6", "", None, None, "pm"),     # SAME day as target: must be ignored
    ]
    W = es.Workload.from_matches(_hist(rows))
    tom, fred, ollie = (W.snapshot(n, day) for n in ("Tired Tom", "Fresh Fred", "Opp Ollie"))
    assert tom["rest"] == 1 and tom["n4"] == 3                       # the same-day row did not leak in
    assert fred["rest"] == 9 and ollie["rest"] == 1
    assert es.fatigue_flag(tom, fred)[0] == "heavy"                  # 3 matches in 4 days
    assert es.fatigue_flag(fred, ollie)[0] == "rest_edge"            # 9d vs 1d: >= 2 days more rest than a tired opp
    assert es.fatigue_flag(fred, ollie)[0] != es.fatigue_flag(ollie, fred)[0]
    nobody = W.snapshot("Nobody", day)
    assert es.fatigue_flag(nobody, fred)[0] == "neutral"             # pick unknown -> never a boost
    assert es.fatigue_flag(nobody, W.snapshot("Nobody2", day))[0] == "unknown"
    assert es.fatigue_flag(tom, fred, coverage_ok=False)[0] == "unknown"
    # 3h+ match yesterday on a Sackmann (estimated-date) row -> heavy, but only when estimated rows are allowed
    W2 = es.Workload.from_matches(_hist([(20261005, "Marathon Mo", "Y1", "R32", 32, 200, "atp")]))   # R32 -> +1 day = Oct 6
    mo_loose = W2.snapshot("Marathon Mo", es.day_of("2026-10-07"))
    assert mo_loose["rest"] == 1 and es.fatigue_flag(mo_loose, W2.snapshot("Y1", es.day_of("2026-10-07")))[0] == "heavy"
    assert not W2.snapshot("Marathon Mo", es.day_of("2026-10-07"), exact_only=True)["known"]   # backtest ignores it
    assert W.coverage_ok(day) and not W.coverage_ok(day + 30)


# ------------------------------------------------------------------ end-to-end: analyse(strategy="stack")
def _ratings_and_hist():
    rows, d0 = [], 20260101
    for i in range(40):                                              # Alice beats Bob repeatedly -> model favours Alice
        rows.append((d0 + i, "Alice Aaa", "Bob Bbb", "", None, None, "atp"))
    for i in range(10):
        rows.append((d0 + 50 + i, "Bob Bbb", "Cara Ccc", "", None, None, "atp"))
        rows.append((d0 + 60 + i, "Alice Aaa", "Cara Ccc", "", None, None, "atp"))
    h = _hist(rows)
    h["surface"] = "Hard"; h["match_num"] = range(len(h))
    for c in tc.STAT_COLS:
        h[c] = float("nan")
    return tc.build_ratings(h), h


def _event(start, bid, ask, title="Alice Aaa vs Bob Bbb"):
    return {"id": "1", "slug": "atp-alice-bob", "title": title, "startTime": start.isoformat(), "live": False,
            "markets": [{"id": "m1", "question": title, "sportsMarketType": "moneyline",
                         "outcomes": '["Alice Aaa","Bob Bbb"]', "outcomePrices": f'["{(bid + ask) / 2}","{1 - (bid + ask) / 2}"]',
                         "clobTokenIds": '["tokA","tokB"]', "gameStartTime": start.isoformat(),
                         "bestBid": bid, "bestAsk": ask, "liquidityNum": 5000, "volume24hr": 1000}]}


def test_analyse_stack_end_to_end(monkeypatch=None):
    R, hist = _ratings_and_hist()
    W = es.Workload.from_matches(hist)
    start = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=6)
    book = {"tokA": ([(0.40, 500)], [(0.44, 500), (0.45, 500)]), "tokB": ([(0.55, 500)], [(0.60, 500)])}
    orig = tc.get_book
    tc.get_book = lambda tok: book[tok]
    try:
        # Alice is the model favourite but market gives her only ~42% -> value on Alice, resting at 41c
        df = tc.analyse([_event(start, 0.40, 0.44)], R, bankroll=1000, strategy="stack", workload=W, sharp=None)
        r = df.iloc[0]
        assert r.signal.startswith("BUY Alice Aaa (stack"), r.signal
        assert r.grade == "C" and math.isclose(r.limit, 0.41) and r.stake <= 1000 * es.CAP_C + 1e-9
        assert r.strategy == "stack" and r.edge >= es.MIN_EDGE
        # sharp book disagrees (says Alice is only 38%) -> veto
        class Sharp:                                                     # stand-in for SharpFeed
            def lookup(self, a, b): return (0.38, 0.62)
        df = tc.analyse([_event(start, 0.40, 0.44)], R, bankroll=1000, strategy="stack", workload=W, sharp=Sharp())
        assert not df.iloc[0].signal.startswith("BUY") and "sharp" in df.iloc[0].signal, df.iloc[0].signal
        # sharp agrees (Alice 60%) -> grade B (no rest edge) with a bigger stake than C
        class Sharp2:
            def lookup(self, a, b): return (0.60, 0.40)
        df = tc.analyse([_event(start, 0.40, 0.44)], R, bankroll=1000, strategy="stack", workload=W, sharp=Sharp2())
        assert df.iloc[0].grade == "B" and df.iloc[0].stake > 1000 * es.CAP_C
        # thin book: asks only $20 deep vs 3x stake -> PASS
        book["tokA"] = ([(0.40, 500)], [(0.44, 5)])
        df = tc.analyse([_event(start, 0.40, 0.44)], R, bankroll=1000, strategy="stack", workload=W, sharp=Sharp2())
        assert "thin book" in df.iloc[0].signal, df.iloc[0].signal
        # live match: stack refuses
        ev = _event(start - pd.Timedelta(hours=8), 0.40, 0.44); ev["live"] = True
        df = tc.analyse([ev], R, bankroll=1000, strategy="stack", workload=W, sharp=None, check_liquidity=False)
        assert df.empty or "pre-match only" in df.iloc[0].signal
    finally:
        tc.get_book = orig


# ------------------------------------------------------------------ backtest replay
def _bt_ds():
    base = pd.Timestamp("2026-06-01", tz="UTC")
    rows = []
    for i in range(60):
        # model says player 1 wins 62% but the market prices him at 0.45; players 1 wins in 70% of these
        rows.append(dict(event_id=str(i), market_id=str(i), match="Aa vs Bb", surface="Hard", start=base + pd.Timedelta(days=i),
                         p1=0.45, p2=0.55, model_p1=0.62, model_p2=0.38, winner="Aa" if i % 10 < 7 else "Bb", n1=50, n2=50,
                         fat1="rest_edge" if i % 3 == 0 else ("heavy" if i % 3 == 1 else "neutral"), fat2="neutral"))
    return pd.DataFrame(rows)


def test_backtest_stack_fatigue_layer_changes_bets():
    ds = _bt_ds()
    on = bt.run_backtest(ds, stake=20, **bt.PRESETS["stack"])
    off = bt.run_backtest(ds, stake=20, **{**bt.PRESETS["stack"], "use_fatigue": False})
    assert len(off) > len(on) > 0                                    # 'heavy' rows are vetoed with the layer on
    assert set(on.fatigue) <= {"rest_edge", "neutral"} and "heavy" not in set(on.fatigue)
    assert on[on.fatigue == "rest_edge"].stake.iloc[0] == 20 * es.REST_BOOST   # flat stake gets the 1.25x boost
    tk = bt.run_backtest(ds, stake=20, **bt.PRESETS["stack_taker"])
    assert tk.pnl.sum() <= on.pnl.sum() + 1e-9 or len(tk) <= len(on)  # taker fee can only make it worse or filter bets
    kel = bt.run_backtest(ds, mode="kelly", bankroll=1000.0, **bt.PRESETS["stack"])
    assert (kel.stake <= 1000 * es.MAX_MATCH_FRAC * 1.5).all()       # stakes respect the cap (bankroll grows a little)
    assert bt.summarize(on)["bets"] == len(on)


# ------------------------------------------------------------------ paper limit orders
def test_paper_limit_orders_fill_expire_and_clv():
    tmp = tempfile.mkdtemp()
    paper.DB = os.path.join(tmp, "p.db")
    paper.reset(1000)
    state = {"bids": [(0.40, 100)], "asks": [(0.46, 100)]}
    orig = tc.get_book
    tc.get_book = lambda tok: (state["bids"], state["asks"])
    try:
        start = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=3)
        oid = paper.place_limit("m1", "tokA", "A vs B", "A", "Hard", "u", 50, 0.41, start=start, model_p=.5, edge=.07, grade="B", fatigue="neutral")
        assert paper.reserved_cash(paper.con()) == 50
        try:
            paper.place_limit("m2", "t", "C vs D", "C", "Hard", "u", 5000, 0.41, start=start)
            assert False, "should refuse: not enough free cash"
        except ValueError:
            pass
        assert paper.process_orders() == dict(filled=0, expired=0)    # ask 0.46 > limit 0.41: still resting
        assert math.isclose(paper.orders_df().iloc[0].last_mid, 0.43)
        state["asks"] = [(0.41, 100)]                                 # market sold down to our price
        assert paper.process_orders()["filled"] == 1
        tdf = paper.trades_df(mark=False)
        t = tdf.iloc[0]
        assert t.entry_price == 0.41 and math.isclose(t.shares, 50 / 0.41) and t.grade == "B" and t.source == "limit"
        state["bids"], state["asks"] = [(0.47, 10)], [(0.49, 10)]     # market moved up: we beat the close
        paper.process_orders()
        st_ = paper.limit_stats()
        assert st_["filled"] == 1 and st_["fill_rate"] == 1.0 and math.isclose(st_["clv_filled"], 0.48 - 0.41)
        # an order that never fills expires at the start and shows its would-have-been CLV
        start2 = pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=1)
        paper.place_limit("m3", "tokC", "E vs F", "E", "Hard", "u", 20, 0.30, start=start2)
        state["bids"], state["asks"] = [(0.40, 10)], [(0.44, 10)]
        paper.process_orders()                                        # records last_mid 0.42 before the start
        import time; time.sleep(1.5)
        assert paper.process_orders()["expired"] == 1
        st_ = paper.limit_stats()
        assert st_["expired"] == 1 and math.isclose(st_["fill_rate"], 0.5) and math.isclose(st_["clv_expired"], 0.42 - 0.30)
        paper.settle = lambda: 0                                      # no network
    finally:
        tc.get_book = orig



# ------------------------------------------------------------------ run every strategy + compare
def test_auto_all_and_strategy_report():
    tmp = tempfile.mkdtemp(); paper.DB = os.path.join(tmp, "p.db"); paper.reset(5000)
    start = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=4)

    def row(mid, pick, price, strat, **kw):
        base = dict(market_id=mid, tokens=["t" + mid + "a", "t" + mid + "b"], side=0, match=f"{pick} vs Z", pick=pick,
                    surface="Hard", url="u", price=price, model_p=0.55, edge=0.08, start=start, limit=price - 0.01,
                    grade="B", fatigue="neutral", sharp_p=float("nan"), signal=f"BUY {pick}")
        return {**base, **kw}

    fake = {"underdog": [row("m1", "Und", 0.30, "underdog"), dict(row("m2", "NoBuy", 0.2, "underdog"), signal="PASS (x)")],
            "favourite": [row("m1", "Fav", 0.60, "favourite")],           # SAME market as the underdog position
            "stack": [row("m1", "Stk", 0.45, "stack")]}
    orig_an, orig_book = tc.analyse, tc.get_book
    tc.analyse = lambda events, R, bankroll=1000, strategy=None, workload=None, sharp=None, **k: pd.DataFrame(fake[strategy])
    tc.get_book = lambda tok: ([(0.28, 100)], [(0.30, 1000)])
    try:
        out = paper.auto_all([], None, None, None, stake=25)
        assert out == {"underdog": 1, "favourite": 1, "stack": 1}, out     # one market, three independent positions
        assert paper.auto_all([], None, None, None, stake=25) == {"underdog": 0, "favourite": 0, "stack": 0}   # no double buys
        tdf = paper.trades_df(mark=False)
        assert sorted(tdf.strategy) == ["favourite", "underdog"] and set(tdf.cost.round(2)) == {25.0}   # same stake each
        assert paper.orders_df().iloc[0].strategy == "stack"
        # settle: underdog wins, favourite loses; stack order is still resting
        c = paper.con()
        for _id, won in ((int(tdf[tdf.strategy == "underdog"].id.iloc[0]), True), (int(tdf[tdf.strategy == "favourite"].id.iloc[0]), False)):
            c.execute("UPDATE trades SET status='settled', proceeds=shares*?, pnl=shares*?-cost WHERE id=?", (1.0 if won else 0.0,) * 2 + (_id,))
        c.commit()
        rep = paper.strategy_report().set_index("strategy")
        u, f = rep.loc["underdog"], rep.loc["favourite"]
        assert u.settled == 1 and f.settled == 1 and u.roi > 0 > f.roi
        gross = (25 / 0.30) - 25                                            # shares * 1 - cost, entry 0.30
        fee = tc.taker_fee(0.30) * (25 / 0.30)
        assert math.isclose(u.pnl_net, gross - fee, rel_tol=1e-6)           # taker fee is deducted in the comparison
        assert "stack" in rep.index and rep.loc["stack"].bets == 0
    finally:
        tc.analyse, tc.get_book = orig_an, orig_book


# ------------------------------------------------------------------ separate accounts, curves, verdict
def test_separate_accounts_curves_and_verdict():
    tmp = tempfile.mkdtemp(); paper.DB = os.path.join(tmp, "p.db"); paper.reset(5000)
    paper.set_strategy_cash(100)
    orig = tc.get_book
    tc.get_book = lambda tok: ([(0.28, 100)], [(0.30, 1000)])
    try:
        # underdog account can only afford $100; favourite account is untouched by it
        paper.open_trade("m1", "t1", "A vs B", "A", "Hard", "u", 80, strategy="underdog")
        try:
            paper.open_trade("m2", "t2", "C vs D", "C", "Hard", "u", 40, strategy="underdog")
            assert False, "underdog account should be out of cash"
        except ValueError as e:
            assert "underdog" in str(e)
        paper.open_trade("m2", "t2", "C vs D", "C", "Hard", "u", 40, strategy="favourite")      # separate pot: fine
        c = paper.con()
        assert math.isclose(paper.account_cash(c, "underdog"), 20) and math.isclose(paper.account_cash(c, "favourite"), 60)
        assert paper.account_start(c, "manual") == 5000                                          # legacy balance kept
        # settle: underdog wins, favourite loses; curves and accounts reflect each separately
        ids = {r["strategy"]: r["id"] for r in c.execute("SELECT id, strategy FROM trades")}
        c.execute("UPDATE trades SET status='settled', proceeds=shares, pnl=shares-cost, closed_at='2026-11-01T10:00:00+00:00' WHERE id=?", (ids["underdog"],))
        c.execute("UPDATE trades SET status='settled', proceeds=0, pnl=-cost, closed_at='2026-11-02T10:00:00+00:00' WHERE id=?", (ids["favourite"],))
        c.commit()
        cv = paper.strategy_curves("time")
        assert list(cv.columns) == ["favourite", "underdog"] and cv.underdog.iloc[-1] > 0 > cv.favourite.iloc[-1]
        assert cv.favourite.iloc[0] == 0.0                                      # forward/back fill, no gaps
        cb = paper.strategy_curves("bets")
        assert list(cb.index) == [1]
        assert math.isclose(cb.loc[1, "favourite"], -40 - tc.taker_fee(0.30) * (40 / 0.30))     # net of taker fee
        acc = paper.account_summary().set_index("strategy")
        assert acc.loc["underdog"].balance > 100 > acc.loc["favourite"].balance
        assert math.isclose(acc.loc["favourite"].balance, 100 - 40 - tc.taker_fee(0.30) * (40 / 0.30))
        assert set(["underdog", "favourite", "stack"]) <= set(acc.index)
        # verdict refuses to call a winner on 1 bet each, and calls one only with enough evidence
        v = paper.verdict(paper.strategy_report())
        assert not v["conclusive"] and v["leader"] == "underdog" and "Too early" in v["text"]
        big = pd.DataFrame([dict(strategy="underdog", settled=400, roi=0.10, roi_lo=0.03, roi_hi=0.17),
                            dict(strategy="favourite", settled=400, roi=-0.02, roi_lo=-0.09, roi_hi=0.05)])
        assert paper.verdict(big)["conclusive"]
        close = pd.DataFrame([dict(strategy="underdog", settled=400, roi=0.06, roi_lo=0.01, roi_hi=0.11),
                              dict(strategy="favourite", settled=400, roi=0.05, roi_lo=0.00, roi_hi=0.10)])
        assert not paper.verdict(close)["conclusive"]                           # gap inside noise
        assert paper.verdict(pd.DataFrame())["leader"] is None
        # wiping one strategy leaves the others alone
        paper.reset_strategy("underdog")
        assert len(paper.trades_df(mark=False)) == 1 and paper.trades_df(mark=False).iloc[0].strategy == "favourite"
    finally:
        tc.get_book = orig

# ------------------------------------------------------------------ sharp feed
def test_sharp_feed_csv_lookup():
    tmp = tempfile.mkdtemp()
    pd.DataFrame([dict(player1="Carlos Alcaraz", player2="Jannik Sinner", odds1=2.10, odds2=1.80)]).to_csv(
        os.path.join(tmp, "sharp_odds.csv"), index=False)
    os.environ.pop("ODDS_API_KEY", None)
    f = es.SharpFeed(tmp)
    pa, pb = f.lookup("Jannik Sinner", "Carlos Alcaraz")
    assert math.isclose(pa + pb, 1.0) and pa > 0.5 > pb               # Sinner is the favourite, orientation respected
    assert f.lookup("Foo Bar", "Baz Qux") is None
    assert f.source == "csv"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok  ", name)

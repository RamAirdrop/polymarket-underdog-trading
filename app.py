"""Streamlit dashboard:  python -m streamlit run app.py"""
import os
from datetime import datetime
import pandas as pd
import streamlit as st

import backtest as bt
import live_trade
import livescores
import paper
import tennis_core as tc

st.set_page_config(page_title="Polymarket Tennis", layout="wide", initial_sidebar_state="collapsed")
st.markdown(
    """
    <style>
        [data-testid="stSidebar"] {display: none;}
        [data-testid="collapsedControl"] {display: none;}
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("🎾 Polymarket Tennis — Markets · Paper/Real buy · Daily P&L")


@st.cache_resource(ttl=6 * 3600, show_spinner="Downloading history & building ratings…")
def ratings():
    return tc.build_ratings()


@st.cache_data(ttl=20, show_spinner="Fetching Polymarket…")
def events():
    return tc.fetch_events()


@st.cache_resource
def scores():
    s = livescores.LiveScores(); s.start(); return s


LS = scores()


def get_live_ask(token_id, fallback_ask):
    """Fetch the latest best ask directly from the Polymarket CLOB order book."""
    if not token_id:
        return fallback_ask
    try:
        _, asks = tc.get_book(token_id)
        if asks:
            return float(asks[0][0])
    except Exception:
        pass
    return fallback_ask


# ------------------------------------------------------------------ today's fixed stake
amt, locked = paper.get_stake()
box = st.container(border=True)
c1, c2 = box.columns([4, 1])
if locked and amt:
    c1.success(f"Today's stake is locked at **${amt:.0f}** per bet — every Paper/Buy button uses this amount.")
    if c2.button("Unlock"):
        paper.unlock_stake(); st.rerun()
else:
    choice = c1.radio("Choose today's fixed stake per bet", [10, 25, 50, 100, 150], index=1, horizontal=True,
                      format_func=lambda x: f"${x}")
    if c2.button("Set & lock for today"):
        paper.set_stake(choice); st.rerun()
    amt = None
    box.caption("Pick an amount and lock it; Paper/Buy buttons stay disabled until you do.")

# ------------------------------------------------------------------ pending REAL buy confirmation
pend = st.session_state.get("pending_real")
if pend:
    with st.container(border=True):
        st.error(f"💵 REAL MONEY — buy **${pend['usd']:.0f}** of **{pend['pick']}** in {pend['match']} "
                 f"at about {pend['ask']:.2f}?")
        k1, k2, _ = st.columns([1, 1, 4])
        if k1.button("✅ Confirm real buy"):
            tdf = paper.trades_df(mark=False)
            spent = float(tdf[(tdf["mode"] == "real") & (tdf.day == paper.today())].cost.sum()) if not tdf.empty else 0.0
            if not live_trade.enabled():
                st.warning("Real trading is switched off. Follow the setup notes at the top of live_trade.py "
                           "(environment variables), then restart the dashboard.")
            elif spent + pend["usd"] > live_trade.daily_limit():
                st.warning(f"Daily real-spend limit ${live_trade.daily_limit():.0f} would be exceeded (spent ${spent:.0f}).")
            else:
                try:
                    res = live_trade.real_buy(pend["token"], pend["usd"], pend["ask"])
                    if res["ok"]:
                        sh = res["shares"] or pend["usd"] / pend["ask"]; cost = res["usd"] or pend["usd"]
                        paper.open_trade(pend["market_id"], pend["token"], pend["match"], pend["pick"], pend["surface"],
                                         pend["url"], pend["usd"], model_p=pend["model_p"], edge=pend["edge"], mode="real",
                                         note=res["raw"], fill_override=(sh, cost, cost / sh))
                        st.success(f"Order placed: {sh:.1f} shares for ${cost:.2f}. Order id {res['order_id']}")
                        del st.session_state["pending_real"]
                    else:
                        st.error(f"Order not filled: {res['raw']}")
                except Exception as e:
                    st.error(f"Order failed: {e}")
        if k2.button("Cancel"):
            del st.session_state["pending_real"]; st.rerun()

tab1, tab2, tab3 = st.tabs(["📡 Markets", "📒 Positions & daily P&L", "🧪 Backtest"])

# ------------------------------------------------------------------ tab 1: markets with buy buttons & toggleable refresh
with tab1:
    strat_label = st.radio("Strategy", ["Underdog", "Favourite", "Both"], horizontal=True, key="strategy_radio",
                           help="Underdog: cheaper side priced 15-40c. Favourite: model favourite priced 30-80c. "
                                "Both use the model shrunk toward the market and need a 5-point edge after fees.")
    STRAT = {"Underdog": "underdog", "Favourite": "favourite", "Both": "both"}[strat_label]
    top = st.columns([1.2, 1.8, 1.8, 1.8, 1.6, 2.8])
    if top[0].button("🔄 Refresh"):
        events.clear()
        st.rerun()

    auto_refresh = top[1].toggle("Auto-refresh (20s)", value=False)
    run_interval = 20 if auto_refresh else None

    today_local = datetime.now().astimezone().date()
    date_mode = top[2].selectbox("Date Filter", ["Today", "All Dates", "Pick Date"], label_visibility="collapsed")
    if date_mode == "Today":
        selected_date = today_local
    elif date_mode == "Pick Date":
        selected_date = top[3].date_input("Select Date", value=today_local, label_visibility="collapsed")
    else:
        selected_date = None

    surface = top[4].selectbox("Surface", ["Auto", "Hard", "Clay", "Grass"], label_visibility="collapsed")
    status_txt = "Auto-refresh ON (20s)" if auto_refresh else "Manual refresh only"
    top[5].caption(f"Live feed: {'🟢 connected' if LS.connected else '🔴 offline'} ({LS.frames} updates) · {status_txt}")

    @st.fragment(run_every=run_interval)
    def render_live_markets():
        if auto_refresh:
            events.clear()
        df = tc.analyse(events(), ratings(), 1000, surface=None if surface == "Auto" else surface, live_scores=LS, target_date=selected_date, strategy=STRAT)
        if df.empty:
            st.warning("No tennis match markets found for the selected filter or date.")
            return

        tdf0 = paper.trades_df(mark=False)
        held = {(r.market_id, r.mode) for r in tdf0[tdf0.status == "open"].itertuples()} if not tdf0.empty else set()

        f1, f2, f3, f4 = st.columns([3, 3, 2, 1])
        show = f1.radio("Show", ["All", "BUY signals", "Live now", "Pre-match"], horizontal=True, key="show_radio")
        q = f3.text_input("Search player", key="search_q").strip().lower()
        nrows = f4.number_input("Rows", 5, 100, 25, key="nrows_input")
        buys = df[df.signal.str.startswith("BUY")]
        f2.write(f"**{len(df)}** matches · **{len(buys)}** BUY signals · 24h volume **${df.vol_24h.sum():,.0f}**")

        view = df
        if show == "BUY signals": view = buys
        elif show == "Live now": view = df[df.live]
        elif show == "Pre-match": view = df[~df.live]
        if q:
            view = view[view.match.str.lower().str.contains(q)]

        for r in view.head(int(nrows)).to_dict("records"):
            mid = str(r["market_id"]); outs = list(r["outs"]); asks = list(r["asks"])
            tokens = list(r["tokens"]) if r["tokens"] else []
            with st.container(border=True):
                cA, cB, cC = st.columns([3, 3, 3])
                with cC:
                    side = st.selectbox("Player", outs, index=int(r["side"]) if pd.notna(r["side"]) else 0,
                                        key=f"side_{mid}", label_visibility="collapsed")
                    i = outs.index(side)
                    tok = tokens[i] if i < len(tokens) else None
                    ask = get_live_ask(tok, asks[i])
                    asks[i] = ask
                    adjp = r.get("adj")
                    p_model = adjp[i] if isinstance(adjp, (list, tuple)) else None     # model shrunk toward the market
                    edge = None if p_model is None else p_model - ask - tc.taker_fee(ask)
                    b1, b2 = st.columns(2)
                    lab = f"${amt:.0f}" if amt else ""
                    do_paper = b1.button(f"📄 Paper buy {lab}", key=f"p_{mid}", disabled=amt is None)
                    do_real = b2.button(f"💵 Buy {lab}", key=f"r_{mid}", disabled=amt is None)
                with cA:
                    st.markdown(f"**{r['match']}** · [Polymarket ↗]({r['url']})")
                    start_str = r["start"].tz_convert("UTC").strftime("%b %d, %I:%M %p") if pd.notna(r["start"]) else "TBD"
                    st.caption(("🔴 LIVE" if r["live"] else f"⏳ {start_str}") + f" · {r['surface']} · vol ${r['vol_24h']:,.0f}"
                               f" · liq ${r['liquidity']:,.0f}")
                    if r["score"]:
                        st.caption("📟 " + r["score"])
                    if r["live_check"]:
                        st.caption("⚠️ " + r["live_check"])
                    tags = [t for t, k in (("📄 paper position open", "paper"), ("💵 real position open", "real"))
                            if (r["market_id"], k) in held]
                    if tags:
                        st.caption(" · ".join(tags))
                with cB:
                    st.write(f"{outs[0]} **{asks[0]:.2f}** · {outs[1]} **{asks[1]:.2f}**")
                    if p_model is not None:
                        st.write(f"Adjusted prob for {side}: **{p_model:.0%}** · edge after fee **{edge:+.1%}** · pays **{1/ask:.2f}x**")
                    if r["signal"]:
                        st.caption("Signal: " + r["signal"])
                    if r["why"]:
                        st.caption(r["why"])

                if do_paper:
                    try:
                        res = paper.open_trade(r["market_id"], tok, r["match"], side, r["surface"], r["url"], amt,
                                               model_p=p_model, edge=edge, fallback_ask=ask, mode="paper")
                        st.toast(f"Paper bought {res['shares']:.1f} {side} shares @ {res['avg_price']:.3f} (${res['cost']:.2f})")
                    except ValueError as e:
                        st.error(str(e))
                if do_real:
                    st.session_state["pending_real"] = dict(market_id=r["market_id"], token=tok, match=r["match"],
                                                            pick=side, surface=r["surface"], url=r["url"], usd=amt, ask=ask,
                                                            model_p=p_model, edge=edge)
                    st.rerun()

    render_live_markets()

# ------------------------------------------------------------------ tab 2: positions & daily P&L
with tab2:
    h1, h2 = st.columns([1, 5])
    if h1.button("🔄 Settle & refresh"):
        st.toast(f"Settled {paper.settle()} trades")
    tdf = paper.trades_df()
    if tdf.empty:
        st.info("No trades yet. Use 📄 Paper buy (fake money) or 💵 Buy (real) on the Markets tab.")
    else:
        rep = paper.daily_report(tdf)
        d_hdr1, d_hdr2 = st.columns([3, 1])
        d_hdr1.subheader("Today")
        td = rep[rep.day == paper.today()] if not rep.empty and "day" in rep.columns else pd.DataFrame()

        if not td.empty and "mode" in td.columns and not td[td["mode"] == "paper"].empty:
            if d_hdr2.button("🗑️ Clear Today's Paper Trades"):
                paper.clear_daily_trades(paper.today(), mode="paper")
                st.success(f"Cleared paper trades for {paper.today()}")
                st.rerun()

        if td.empty:
            st.write("No bets placed today.")
        for r in td.itertuples():
            cols = st.columns(6)
            cols[0].markdown(f"**{'📄 Paper' if r.mode == 'paper' else '💵 Real'}**")
            cols[1].metric("Bets", r.bets, f"${r.staked:,.0f} staked", delta_color="off")
            cols[2].metric("Settled", f"{r.wins}/{r.settled} won")
            cols[3].metric("Still open", r.still_open)
            cols[4].metric("Realised P&L", f"${r.realised_pnl:,.2f}")
            cols[5].metric("Total P&L (incl. open)", f"${r.total_pnl:,.2f}", f"{r.roi:+.1%}")
        st.caption("Total P&L = settled results + open positions valued at what they'd sell for into the order book now. "
                   "Matches that finish after midnight still count toward the day the bet was placed.")
        st.subheader("Every day")
        st.dataframe(rep, hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="$%.2f")
                                    for c in ("staked", "realised_pnl", "unrealised_pnl", "total_pnl")}
                     | {"roi": st.column_config.NumberColumn(format="%.1f%%")})
        st.subheader("All trades")
        mode_f = st.radio("Mode", ["all", "paper", "real"], horizontal=True)
        t = tdf if mode_f == "all" else tdf[tdf["mode"] == mode_f]
        st.dataframe(t[["id", "day", "mode", "match", "pick", "surface", "status", "entry_price", "shares", "cost", "mtm",
                        "exit_price", "pnl", "model_p", "edge", "url"]], hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("Open", display_text="↗")})
        open_paper = tdf[(tdf.status == "open") & (tdf["mode"] == "paper")].id.tolist()
        if open_paper:
            x1, x2 = st.columns([1, 4])
            cid = x1.selectbox("Sell paper position #", open_paper)
            if x2.button("🔴 Sell at current bids"):
                try:
                    st.success(f"Closed, P&L ${paper.close_trade(cid):,.2f}")
                except ValueError as e:
                    st.error(str(e))
        done = t[t.status != "open"].sort_values("id")
        if len(done) > 1:
            st.line_chart(pd.DataFrame({"cumulative settled P&L ($)": done.pnl.cumsum().values}))
        st.caption("Real positions are tracked here as an estimate; actual proceeds are redeemed on Polymarket.")

    with st.expander("Paper account & Clear Options"):
        _, S = paper.portfolio(mark=False)
        st.write(f"Paper cash ${S['cash']:,.2f} of ${S['start_cash']:,.0f} · equity ${S['equity']:,.2f}")
        cl1, cl2, cl3 = st.columns(3)
        avail_days = sorted(tdf[tdf["mode"] == "paper"].day.unique().tolist(), reverse=True) if not tdf.empty else []
        if avail_days:
            del_day = cl1.selectbox("Select day to clear paper trades", avail_days, label_visibility="collapsed")
            if cl2.button(f"🗑️ Clear {del_day} paper trades"):
                paper.clear_daily_trades(del_day, mode="paper")
                st.success(f"Cleared paper trades for {del_day}")
                st.rerun()
        if cl3.button("⚠️ Reset entire paper account"):
            paper.reset(5000); st.rerun()

# ------------------------------------------------------------------ tab 3: backtest
with tab3:
    st.caption("Replays your rules on finished Polymarket tennis matches using the price ~30 min before the start "
               "and ratings that only saw results ≥ 14 days older (pre-match model only; no look-ahead).")
    f1, f2, _ = st.columns(3)
    n_ev = f1.number_input("Closed events to fetch", 200, 10000, 1500, 100)
    if f2.button("⬇️ Fetch / extend backtest data"):
        bar = st.progress(0.0, "Downloading…")
        added = bt.fetch_dataset(int(n_ev), progress=lambda i, t: bar.progress(i / t, f"{i}/{t}"))
        st.success(f"Added {added} matches"); st.rerun()
    if not os.path.exists(bt.DS):
        st.info("No backtest data yet. Click fetch (can take several minutes; it is resumable).")
    else:
        ds = bt.load_dataset()
        st.write(f"**{len(ds):,} matches** · {ds.start.min():%Y-%m-%d} → {ds.start.max():%Y-%m-%d}")
        s1, s2, s3, s4 = st.columns(4)
        mode = s1.selectbox("Staking", ["flat", "kelly"])
        stake = s2.number_input("Flat stake ($)", 1.0, 1000.0, 20.0)
        slip = s3.slider("Slippage (¢ paid above price)", 0.0, 0.05, 0.01, 0.005)
        fee = s4.slider("Fee on profit", 0.0, 0.10, 0.0, 0.01)
        bt_strat = st.radio("Backtest strategy", ["Underdog", "Favourite", "Every model favourite (no filter)"], horizontal=True)
        base_kw = dict(slip=slip, fee=fee, mode=mode, stake=stake, bankroll=1000.0)
        if bt_strat == "Underdog":
            kw = {**bt.PRESETS["underdog"], **base_kw}
        elif bt_strat == "Favourite":
            kw = {**bt.PRESETS["favourite"], **base_kw}
        else:
            kw = dict(min_edge=-1.0, min_ev=-1.0, min_prob=0.50, min_price=0.01, max_price=0.99, shrink=1.0, **base_kw)
        bets = bt.run_backtest(ds, **kw); S = bt.summarize(bets)
        if S["bets"] == 0:
            st.warning("No bets triggered with these settings.")
        else:
            q = st.columns(6)
            q[0].metric("Bets", S["bets"]); q[1].metric("Hit-rate", f"{S['hit_rate']:.1%}")
            q[2].metric("Avg price (break-even)", f"{S['avg_price(break-even)']:.1%}")
            q[3].metric("ROI", f"{S['roi']:+.1%}", f"95% CI {S['roi_95ci'][0]:+.0%} … {S['roi_95ci'][1]:+.0%}", delta_color="off")
            q[4].metric("P&L", f"${S['pnl']:,.0f}"); q[5].metric("Max drawdown", f"{S['max_drawdown']:.0%}")
            st.line_chart(pd.DataFrame({"equity ($)": 1000.0 + bets.pnl.cumsum().values}))
            if S["roi_95ci"][0] < 0:
                st.warning("The ROI confidence interval includes losses → not statistically convincing yet.")
            with st.expander("By surface / price bucket"):
                bets["price_bucket"] = pd.cut(bets.ask, [0, .3, .4, .5, .6, .8, 1])
                st.dataframe(bets.groupby("surface").agg(bets=("pnl", "size"), hit=("won", "mean"), pnl=("pnl", "sum")))
                st.dataframe(bets.groupby("price_bucket", observed=True).agg(bets=("pnl", "size"), hit=("won", "mean"), pnl=("pnl", "sum")))
        with st.expander("🔬 Does the model beat the market? (diagnostics)", expanded=True):
            o, cal = bt.diagnostics(ds)
            st.write({k: (round(v, 4) if isinstance(v, float) else v) for k, v in o.items()})
            st.caption("Log-loss: lower is better. If the market's number is lower than the model's, the model alone can't beat the market.")
            st.dataframe(cal, hide_index=True)
        with st.expander("Benchmarks: bet every side in a price band"):
            st.write("All underdogs 0.15–0.40:", bt.baseline(ds, .15, .40, slip))
            st.write("All favourites 0.60–0.85:", bt.baseline(ds, .60, .85, slip))
        with st.expander("Parameter sweep (train vs unseen test)"):
            if st.button("Run sweep"):
                t, cut = bt.sweep(ds, mode=mode, stake=stake, slip=slip, fee=fee)
                st.caption(f"Train < {cut:%Y-%m-%d} ≤ Test. Judge settings by test_roi.")
                st.dataframe(t.head(20), hide_index=True)
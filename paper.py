"""
Trade ledger: PAPER trades (fake money, real order book) and REAL trades (recorded after a live order).
Also holds today's fixed stake and the end-of-day P&L report.

  python paper.py daily      # profit / loss per day
  python paper.py settle     # settle resolved markets
  python paper.py report
  python paper.py close 5    # sell a paper trade at the current bids
  python paper.py reset --cash 5000
  python paper.py autoall --stake 25 --loop 300   # paper-trade EVERY strategy, same flat $25 each
  python paper.py compare    # side-by-side results per strategy
  python paper.py orders     # fill / expire resting limit orders (stack strategy) and show fill-rate + CLV
"""
import argparse, os, sqlite3, time
from datetime import datetime, timezone

import pandas as pd
import requests

import tennis_core as tc

DB = os.path.join(tc.DATA_DIR, "paper.db")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today():
    return datetime.now().astimezone().date().isoformat()          # local calendar day


def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS trades(
      id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT, market_id TEXT, token_id TEXT,
      match TEXT, pick TEXT, surface TEXT, url TEXT, entry_price REAL, shares REAL, cost REAL,
      model_p REAL, edge REAL, source TEXT, status TEXT DEFAULT 'open',
      exit_price REAL, closed_at TEXT, proceeds REAL, pnl REAL);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, mode TEXT DEFAULT 'paper', market_id TEXT, token_id TEXT,
      match TEXT, pick TEXT, surface TEXT, url TEXT, limit_price REAL, usd REAL, model_p REAL, edge REAL,
      start TEXT, grade TEXT, fatigue TEXT, sharp_p REAL, status TEXT DEFAULT 'open', filled_at TEXT,
      trade_id INTEGER, last_mid REAL, ext_id TEXT);
    INSERT OR IGNORE INTO meta VALUES('start_cash','5000');""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
    for name, ddl in (("mode", "TEXT DEFAULT 'paper'"), ("note", "TEXT"), ("grade", "TEXT"), ("fatigue", "TEXT"),
                      ("sharp_p", "REAL"), ("start", "TEXT"), ("close_mid", "REAL"), ("strategy", "TEXT")):
        if name not in cols:
            c.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")
    ocols = {r[1] for r in c.execute("PRAGMA table_info(orders)")}
    if "strategy" not in ocols:
        c.execute("ALTER TABLE orders ADD COLUMN strategy TEXT")
    c.commit()
    return c


def reset(cash=5000.0):
    if os.path.exists(DB):
        os.remove(DB)
    c = con()
    c.execute("UPDATE meta SET v=? WHERE k='start_cash'", (str(cash),))
    c.commit()


def clear_daily_trades(day_str, mode="paper"):
    """Delete all trades for a specific local calendar day and mode ('paper' or 'real')."""
    df = trades_df(mark=False)
    if df.empty:
        return 0
    target_ids = df[(df.day == day_str) & (df["mode"] == mode)].id.tolist()
    if not target_ids:
        return 0
    c = con()
    placeholders = ",".join(["?"] * len(target_ids))
    c.execute(f"DELETE FROM trades WHERE id IN ({placeholders})", target_ids)
    c.commit()
    return len(target_ids)


# ------------------------------------------------------------ today's fixed stake
def get_stake():
    c = con()
    a = c.execute("SELECT v FROM meta WHERE k=?", (f"stake:{today()}",)).fetchone()
    l = c.execute("SELECT v FROM meta WHERE k=?", (f"locked:{today()}",)).fetchone()
    return (float(a[0]) if a else None), bool(l and l[0] == "1")


def set_stake(amount, lock=True):
    c = con()
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"stake:{today()}", str(float(amount))))
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"locked:{today()}", "1" if lock else "0"))
    c.commit()


def unlock_stake():
    c = con()
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"locked:{today()}", "0"))
    c.commit()


# ------------------------------------------------------------ trading
ACCOUNT_CASH = 1000.0           # default starting balance of each strategy's own paper account


def account_start(c, strategy):
    """Starting balance of one strategy's paper account. 'manual' (buttons you click) keeps the legacy global balance."""
    strategy = strategy or "manual"
    r = c.execute("SELECT v FROM meta WHERE k=?", (f"acct:{strategy}",)).fetchone()
    if r:
        return float(r[0])
    if strategy == "manual":
        return float(c.execute("SELECT v FROM meta WHERE k='start_cash'").fetchone()[0])
    r = c.execute("SELECT v FROM meta WHERE k='strategy_cash'").fetchone()
    return float(r[0]) if r else ACCOUNT_CASH


def set_strategy_cash(amount):
    """Starting balance for every strategy account that has not been set individually."""
    c = con()
    c.execute("INSERT OR REPLACE INTO meta VALUES('strategy_cash',?)", (str(float(amount)),))
    c.commit()


def account_cash(c, strategy):
    """Free cash of one strategy's account: start - money in positions + proceeds returned (open limit orders excluded)."""
    strategy = strategy or "manual"
    s = c.execute("""SELECT COALESCE(SUM(cost),0), COALESCE(SUM(proceeds),0) FROM trades
                     WHERE COALESCE(mode,'paper')='paper' AND COALESCE(strategy,'manual')=?""", (strategy,)).fetchone()
    return account_start(c, strategy) - s[0] + s[1]


def reset_strategy(strategy):
    """Wipe ONE strategy's paper trades and orders (other strategies untouched)."""
    c = con()
    c.execute("DELETE FROM trades WHERE COALESCE(mode,'paper')='paper' AND COALESCE(strategy,'manual')=?", (strategy,))
    c.execute("DELETE FROM orders WHERE mode='paper' AND COALESCE(strategy,'stack')=?", (strategy,))
    c.commit()


def cash_left(c):
    start = float(c.execute("SELECT v FROM meta WHERE k='start_cash'").fetchone()[0])
    s = c.execute("""SELECT COALESCE(SUM(cost),0), COALESCE(SUM(proceeds),0) FROM trades
                     WHERE COALESCE(mode,'paper')='paper'""").fetchone()
    return start - s[0] + s[1]


def open_trade(market_id, token_id, match, pick, surface, url, usd, model_p=None, edge=None,
               source="manual", fallback_ask=None, mode="paper", note=None, fill_override=None, extra=None, strategy=None):
    """mode='paper': fills against the live order book with paper cash.
       mode='real' : records a trade that was really placed (pass fill_override=(shares, cost, avg))."""
    c = con()
    if fill_override:
        sh, cost, avg = fill_override
    else:
        acct = strategy or (extra or {}).get("strategy") or "manual"
        free = account_cash(c, acct) - reserved_cash(c, acct)
        if mode == "paper" and usd > free + 1e-9:
            raise ValueError(f"Not enough cash in the '{acct}' paper account (${free:,.2f} free)")
        try:
            _, asks = tc.get_book(token_id)
            sh, cost, avg = tc.fill(asks, usd=usd)
            if sh <= 0:
                raise ValueError("empty book")
        except Exception:
            if not fallback_ask:
                raise ValueError("Could not read order book")
            avg, sh, cost = fallback_ask, usd / fallback_ask, usd
    c.execute("""INSERT INTO trades(opened_at,market_id,token_id,match,pick,surface,url,entry_price,shares,cost,
                 model_p,edge,source,mode,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (_now(), str(market_id), token_id, match, pick, surface, url, avg, sh, cost, model_p, edge,
               source, mode, note))
    tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    extra = {**(extra or {}), **({"strategy": strategy} if strategy else {})}
    for k, v in extra.items():                       # grade / fatigue / sharp_p / start (stack metadata)
        if k in ("grade", "fatigue", "sharp_p", "start", "strategy"):
            c.execute(f"UPDATE trades SET {k}=? WHERE id=?", (v, tid))
    c.commit()
    return {"avg_price": avg, "shares": sh, "cost": cost, "trade_id": tid}


# ------------------------------------------------------------ resting limit orders (stack strategy)
def reserved_cash(c, strategy=None):
    q, a = "SELECT COALESCE(SUM(usd),0) FROM orders WHERE status='open' AND mode='paper'", ()
    if strategy:
        q, a = q + " AND COALESCE(strategy,'stack')=?", (strategy,)
    return float(c.execute(q, a).fetchone()[0])


def place_limit(market_id, token_id, match, pick, surface, url, usd, limit_price, start=None, model_p=None,
                edge=None, grade=None, fatigue=None, sharp_p=None, mode="paper", ext_id=None, strategy="stack"):
    """Queue a resting buy. mode='paper': simulated here. mode='real': the order already exists on Polymarket
    (ext_id = its order id) and process_orders() reconciles it."""
    c = con()
    free = account_cash(c, strategy) - reserved_cash(c, strategy)
    if mode == "paper" and usd > free + 1e-9:
        raise ValueError(f"Not enough free cash in the '{strategy}' paper account (${free:,.2f} after open orders)")
    st = pd.to_datetime(start, utc=True, errors="coerce") if start is not None else pd.NaT
    c.execute("""INSERT INTO orders(created_at,mode,market_id,token_id,match,pick,surface,url,limit_price,usd,model_p,
                 edge,start,grade,fatigue,sharp_p,ext_id,strategy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (_now(), mode, str(market_id), token_id, match, pick, surface, url, float(limit_price), float(usd),
               model_p, edge, None if pd.isna(st) else st.isoformat(), grade, fatigue, sharp_p, ext_id, strategy))
    c.commit()
    return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def cancel_order(order_id):
    c = con()
    c.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='open' AND mode='paper'", (order_id,))
    c.commit()


def _mid(token_id):
    bids, asks = tc.get_book(token_id)
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2, asks
    return None, asks


def process_orders():
    """Call on every refresh. (1) paper orders fill when the best ask trades down to the limit (fill at the limit,
    queue position ignored) and expire at the scheduled start; (2) real orders are reconciled against the exchange;
    (3) the last pre-start mid is recorded on open orders/trades = the 'closing line' used for CLV.
    Returns dict(filled, expired)."""
    import live_trade
    c = con(); now = pd.Timestamp.now(tz="UTC"); res = dict(filled=0, expired=0)
    for o in c.execute("SELECT * FROM orders WHERE status='open'").fetchall():
        start = pd.to_datetime(o["start"], utc=True, errors="coerce") if o["start"] else pd.NaT
        started = pd.notna(start) and start <= now
        if o["mode"] == "real":
            try:
                st = live_trade.order_status(o["ext_id"])
            except Exception:
                st = None
            if st and st["matched"] > 0:
                sh, px = st["matched"], st["price"] or o["limit_price"]
                c.commit()                                   # release our write lock: open_trade uses its own connection
                tid = open_trade(o["market_id"], o["token_id"], o["match"], o["pick"], o["surface"], o["url"], sh * px,
                                 model_p=o["model_p"], edge=o["edge"], source="limit", mode="real",
                                 fill_override=(sh, sh * px, px), note=f"ext {o['ext_id']}",
                                 extra=dict(grade=o["grade"], fatigue=o["fatigue"], sharp_p=o["sharp_p"],
                                            start=o["start"], strategy=o["strategy"]))["trade_id"]
                c.execute("UPDATE orders SET status='filled', filled_at=?, trade_id=? WHERE id=?", (_now(), tid, o["id"]))
                res["filled"] += 1
            elif started or (st and st["closed"]):
                c.execute("UPDATE orders SET status='expired' WHERE id=?", (o["id"],)); res["expired"] += 1
            continue
        try:
            mid, asks = _mid(o["token_id"])
        except Exception:
            mid, asks = None, []
        if mid is not None and not started:
            c.execute("UPDATE orders SET last_mid=? WHERE id=?", (mid, o["id"]))
        if not started and asks and asks[0][0] <= o["limit_price"] + 1e-9:
            lp = o["limit_price"]
            c.commit()                                       # release our write lock: open_trade uses its own connection
            tid = open_trade(o["market_id"], o["token_id"], o["match"], o["pick"], o["surface"], o["url"], o["usd"],
                             model_p=o["model_p"], edge=o["edge"], source="limit", mode="paper",
                             fill_override=(o["usd"] / lp, o["usd"], lp),
                             extra=dict(grade=o["grade"], fatigue=o["fatigue"], sharp_p=o["sharp_p"],
                                        start=o["start"], strategy=o["strategy"]))["trade_id"]
            c.execute("UPDATE orders SET status='filled', filled_at=?, trade_id=? WHERE id=?", (_now(), tid, o["id"]))
            res["filled"] += 1
        elif started:
            c.execute("UPDATE orders SET status='expired' WHERE id=?", (o["id"],)); res["expired"] += 1
    # closing-line capture for positions that are filled but not started yet
    for t in c.execute("SELECT id, token_id, start FROM trades WHERE status='open' AND start IS NOT NULL").fetchall():
        if pd.to_datetime(t["start"], utc=True, errors="coerce") > now:
            try:
                mid, _ = _mid(t["token_id"])
                if mid is not None:
                    c.execute("UPDATE trades SET close_mid=? WHERE id=?", (mid, t["id"]))
            except Exception:
                pass
    c.commit()
    return res


def limit_stats():
    """Fill rate and closing-line value. CLV = last pre-start mid - our price (positive = we beat the close).
    Compare CLV on filled vs expired orders to see adverse selection: if filled << expired, you only get filled
    when the market is moving against you."""
    c = con()
    o = pd.read_sql_query("SELECT * FROM orders WHERE mode IN ('paper','real')", c)
    t = pd.read_sql_query("SELECT * FROM trades WHERE close_mid IS NOT NULL AND source='limit'", c)
    done = o[o.status.isin(["filled", "expired"])]
    exp = o[(o.status == "expired") & o.last_mid.notna()]
    return dict(orders=len(o), open=int((o.status == "open").sum()), filled=int((o.status == "filled").sum()),
                expired=int((o.status == "expired").sum()),
                fill_rate=float((done.status == "filled").mean()) if len(done) else None,
                clv_filled=float((t.close_mid - t.entry_price).mean()) if len(t) else None,
                clv_expired=float((exp.last_mid - exp.limit_price).mean()) if len(exp) else None,
                n_clv_filled=len(t), n_clv_expired=len(exp))


def orders_df():
    return pd.read_sql_query("SELECT * FROM orders ORDER BY id DESC", con())


def close_trade(trade_id):
    """Sell a PAPER trade at the current bids."""
    c = con()
    t = c.execute("SELECT * FROM trades WHERE id=? AND status='open' AND COALESCE(mode,'paper')='paper'",
                  (trade_id,)).fetchone()
    if not t:
        raise ValueError("No such open paper trade (real positions are sold on Polymarket itself)")
    bids, _ = tc.get_book(t["token_id"])
    sh, pr, avg = tc.fill(bids, shares=t["shares"])
    if sh < t["shares"] * 0.999:
        raise ValueError("Not enough bid depth to exit fully right now")
    c.execute("UPDATE trades SET status='closed',exit_price=?,closed_at=?,proceeds=?,pnl=? WHERE id=?",
              (avg, _now(), pr, pr - t["cost"], trade_id))
    c.commit()
    return pr - t["cost"]


def settle():
    """Settle every open trade (paper and real) whose market has resolved."""
    c = con(); n = 0
    for t in c.execute("SELECT * FROM trades WHERE status='open'").fetchall():
        try:
            r = requests.get(f"{tc.GAMMA}/markets/{t['market_id']}", timeout=15)
            r.raise_for_status(); m = r.json()
        except Exception:
            continue
        if not m.get("closed"):
            continue
        px = [float(x) for x in tc._j(m.get("outcomePrices"))]
        toks = tc._j(m.get("clobTokenIds"))
        if t["token_id"] not in toks or not px:
            continue
        if not (max(px) >= 0.99 or all(abs(x - 0.5) < 0.02 for x in px)):       # win/loss or void
            continue
        p = px[toks.index(t["token_id"])]
        proceeds = t["shares"] * p
        c.execute("UPDATE trades SET status='settled',exit_price=?,closed_at=?,proceeds=?,pnl=? WHERE id=?",
                  (p, _now(), proceeds, proceeds - t["cost"], t["id"]))
        n += 1
    c.commit()
    return n


def trades_df(mark=True):
    """All trades (paper + real). 'mtm' = what an open position would fetch if sold into the bids now."""
    c = con()
    df = pd.read_sql_query("SELECT * FROM trades ORDER BY id DESC", c)
    if df.empty:
        return df
    df["mode"] = df["mode"].fillna("paper")
    tz = datetime.now().astimezone().tzinfo
    df["day"] = pd.to_datetime(df.opened_at, utc=True).dt.tz_convert(tz).dt.strftime("%Y-%m-%d")
    df["mtm"] = float("nan")
    if mark:
        for i, t in df[df.status == "open"].iterrows():
            try:
                bids, _ = tc.get_book(t["token_id"])
                df.at[i, "mtm"] = tc.fill(bids, shares=t["shares"])[1]
            except Exception:
                df.at[i, "mtm"] = t["cost"]
    return df


def daily_report(df=None):
    """Profit / loss per day and per mode (paper / real)."""
    df = trades_df() if df is None else df
    if df.empty:
        return pd.DataFrame()
    rows = []
    for (day, mode), g in df.groupby(["day", "mode"]):
        done, op = g[g.status != "open"], g[g.status == "open"]
        real = float(done.pnl.sum()) if len(done) else 0.0
        unreal = float((op.mtm.fillna(op.cost) - op.cost).sum()) if len(op) else 0.0
        rows.append(dict(day=day, mode=mode, bets=len(g), staked=float(g.cost.sum()), settled=len(done),
                         wins=int((done.pnl > 0).sum()) if len(done) else 0, still_open=len(op),
                         realised_pnl=real, unrealised_pnl=unreal, total_pnl=real + unreal,
                         roi=(real + unreal) / float(g.cost.sum())))
    return pd.DataFrame(rows).sort_values(["day", "mode"], ascending=[False, True]).reset_index(drop=True)


def portfolio(mark=True):
    """Paper account summary: (paper trades DataFrame, summary dict)."""
    c = con()
    df = trades_df(mark)
    df = df[df["mode"] == "paper"] if not df.empty else df
    op = df[df.status == "open"] if not df.empty else df
    done = df[df.status != "open"] if not df.empty else df
    cash = cash_left(c)
    open_val = float(op.mtm.fillna(op.cost).sum()) if len(op) else 0.0
    start = float(c.execute("SELECT v FROM meta WHERE k='start_cash'").fetchone()[0])
    S = {"start_cash": start, "cash": cash, "open_positions": len(op),
         "open_cost": float(op.cost.sum()) if len(op) else 0.0, "open_value": open_val, "equity": cash + open_val,
         "realised_pnl": float(done.pnl.sum()) if len(done) else 0.0, "closed_trades": len(done),
         "hit_rate": float((done.pnl > 0).mean()) if len(done) else None,
         "roi_on_closed": float(done.pnl.sum() / done.cost.sum()) if len(done) else None}
    S["total_return"] = S["equity"] / start - 1
    return df, S


def auto_trade(df, max_open=10, max_exposure=0.5):
    """(CLI only) open paper trades for every BUY signal not already held."""
    c = con()
    held = {r[0] for r in c.execute("SELECT market_id FROM trades")} | {r[0] for r in c.execute("SELECT market_id FROM orders WHERE status='open'")}
    opened = []
    for r in df[df.signal.str.startswith("BUY")].itertuples():
        _, S = portfolio(mark=False)
        if S["open_positions"] >= max_open or S["open_cost"] >= max_exposure * S["equity"]:
            break
        if str(r.market_id) in held or not r.stake:
            continue
        usd = min(r.stake, S["cash"])
        if usd < 1:
            continue
        try:
            if getattr(r, "strategy", "") == "stack":        # stack = resting limit order, never a market buy
                place_limit(r.market_id, r.tokens[r.side], r.match, r.pick, r.surface, r.url, usd, r.limit,
                            start=r.start, model_p=r.model_p, edge=r.edge, grade=r.grade, fatigue=r.fatigue,
                            sharp_p=r.sharp_p)
                opened.append((r.match, r.pick, {"cost": usd, "avg_price": r.limit}))
                continue
            res = open_trade(r.market_id, r.tokens[r.side], r.match, r.pick, r.surface, r.url, usd,
                             model_p=r.model_p, edge=r.edge, source="auto", fallback_ask=r.price)
            opened.append((r.match, r.pick, res))
        except ValueError:
            continue
    return opened


STRATEGIES = ("underdog", "favourite", "stack")


def auto_all(events, R, W, SF, stake, bankroll=1000.0, strategies=STRATEGIES):
    """Paper-trade the BUY signals of EVERY strategy with the SAME flat stake, so results are comparable.
    Each strategy keeps its own position in a market (a market can be held by several strategies, once each).
    underdog / favourite: market buy at the live book (as the dashboard button does).
    stack: resting limit order (fills only if the market trades down to it).
    Returns {strategy: number of new positions/orders}."""
    c = con(); out = {}
    for strat in strategies:
        out[strat] = 0
        df = tc.analyse(events, R, bankroll=bankroll, strategy=strat,
                        workload=W if strat == "stack" else None, sharp=SF if strat == "stack" else None)
        if df.empty:
            continue
        held = {r[0] for r in c.execute("SELECT market_id FROM trades WHERE strategy=? AND COALESCE(mode,'paper')='paper'", (strat,))}
        held |= {r[0] for r in c.execute("SELECT market_id FROM orders WHERE strategy=? AND mode='paper'", (strat,))}
        for r in df[df.signal.str.startswith("BUY")].itertuples():
            if str(r.market_id) in held or not r.tokens:
                continue
            try:
                if strat == "stack":
                    place_limit(r.market_id, r.tokens[r.side], r.match, r.pick, r.surface, r.url, stake, r.limit,
                                start=r.start, model_p=r.model_p, edge=r.edge, grade=r.grade, fatigue=r.fatigue,
                                sharp_p=None if pd.isna(r.sharp_p) else r.sharp_p, strategy="stack")
                else:
                    open_trade(r.market_id, r.tokens[r.side], r.match, r.pick, r.surface, r.url, stake,
                               model_p=r.model_p, edge=r.edge, source="auto", fallback_ask=r.price,
                               strategy=strat, extra=dict(start=None if pd.isna(r.start) else r.start.isoformat()))
                out[strat] += 1; held.add(str(r.market_id))
            except ValueError:
                continue                                         # not enough paper cash / empty book
    return out


def _paper_trades(c):
    t = pd.read_sql_query("SELECT * FROM trades WHERE COALESCE(mode,'paper')='paper'", c)
    if t.empty:
        return t
    t["strategy"] = t.strategy.fillna("manual")
    fee = [0.0 if src == "limit" else tc.taker_fee(p) * sh for src, p, sh in zip(t.source, t.entry_price, t.shares)]
    t["pnl_net"] = t.pnl - pd.Series(fee, index=t.index)          # net of the taker fee on market buys
    return t


def strategy_curves(by="time"):
    """Cumulative NET profit per strategy, one column each.
    by='time': x = settlement time (forward-filled).  by='bets': x = bet number 1,2,3... (equal-sample comparison:
    a strategy that bets less often is not penalised for having fewer bets so far)."""
    t = _paper_trades(con())
    if t.empty:
        return pd.DataFrame()
    t = t[(t.status != "open") & t.pnl_net.notna() & t.closed_at.notna()].copy()
    if t.empty:
        return pd.DataFrame()
    t["when"] = pd.to_datetime(t.closed_at, utc=True)
    t = t.sort_values(["when", "id"])
    t["cum"] = t.groupby("strategy").pnl_net.cumsum()
    if by == "bets":
        t["n"] = t.groupby("strategy").cumcount() + 1
        return t.pivot_table(index="n", columns="strategy", values="cum", aggfunc="last")
    out = t.pivot_table(index="when", columns="strategy", values="cum", aggfunc="last").sort_index().ffill().fillna(0.0)
    return out


def account_summary():
    """Per-strategy paper account: starting balance, realised net P&L, balance, money in open positions/orders."""
    c = con(); t = _paper_trades(c)
    o = pd.read_sql_query("SELECT * FROM orders WHERE mode='paper' AND status='open'", c)
    names = set(t.strategy) if len(t) else set()
    names |= set(STRATEGIES) | set(o.strategy.fillna("stack") if len(o) else [])
    rows = []
    for k in sorted(names, key=lambda x: (STRATEGIES.index(x) if x in STRATEGIES else 99, x)):
        g = t[t.strategy == k] if len(t) else t
        done = g[(g.status != "open") & g.pnl_net.notna()] if len(g) else g
        start = account_start(c, k)
        pnl = float(done.pnl_net.sum()) if len(done) else 0.0
        rows.append(dict(strategy=k, start=start, realised_net_pnl=pnl, balance=start + pnl,
                         free_cash=account_cash(c, k) - reserved_cash(c, k),
                         in_open_positions=float(g[g.status == "open"].cost.sum()) if len(g) else 0.0,
                         in_open_orders=reserved_cash(c, k)))
    return pd.DataFrame(rows)


def strategy_report():
    """One row per strategy (paper trades only). P&L is NET of the taker fee for market buys (the ledger itself does
    not charge it), so Underdog/Favourite are compared with Stack on equal terms. Stack limit fills pay no fee."""
    c = con()
    t = _paper_trades(c)
    o = pd.read_sql_query("SELECT * FROM orders WHERE mode='paper'", c)
    if t.empty and o.empty:
        return pd.DataFrame()
    if t.empty:
        t = pd.DataFrame(columns=["strategy", "status", "cost", "pnl", "pnl_net", "shares", "entry_price", "source", "close_mid"])
    if len(o):
        o["strategy"] = o.strategy.fillna("stack")
    rows = []
    for strat in sorted(set(t.strategy) | set(o.strategy if len(o) else [])):
        g = t[t.strategy == strat]; done = g[(g.status != "open") & g.pnl.notna()]
        n = len(done); staked = float(done.cost.sum()) if n else 0.0
        rets = (done.pnl_net / done.cost).to_numpy() if n else []
        roi = float(done.pnl_net.sum() / staked) if n and staked else None
        se = float(pd.Series(rets).std(ddof=1) / n ** 0.5) if n > 1 else None
        clv = g[g.close_mid.notna()]
        og = o[o.strategy == strat] if len(o) else o
        rows.append(dict(strategy=strat, bets=len(g), settled=n, open=int((g.status == "open").sum()),
                         hit_rate=float((done.pnl > 0).mean()) if n else None,
                         avg_entry=float(g.entry_price.mean()) if len(g) else None,
                         staked=staked, pnl_net=float(done.pnl_net.sum()) if n else 0.0, roi=roi,
                         roi_lo=None if roi is None or se is None else roi - 1.96 * se,
                         roi_hi=None if roi is None or se is None else roi + 1.96 * se,
                         clv_cents=float((clv.close_mid - clv.entry_price).mean() * 100) if len(clv) else None,
                         orders_expired=int((og.status == "expired").sum()) if len(og) else 0))
    return pd.DataFrame(rows)


def verdict(rep, min_bets=200):
    """Plain-language answer to 'which strategy is better'. Refuses to call noise a winner.
    Returns dict(text, conclusive, leader)."""
    import math
    r = rep[rep.strategy.isin(STRATEGIES) & (rep.settled > 0) & rep.roi.notna()] if len(rep) else rep
    if r is None or r.empty:
        return dict(text="No settled bets yet. Nothing to compare.", conclusive=False, leader=None)
    r = r.sort_values("roi", ascending=False).reset_index(drop=True)
    L = r.iloc[0]
    head = f"**{L.strategy}** leads on ROI ({L.roi:+.1%} on {int(L.settled)} settled bets)"
    if len(r) > 1:
        head += f", ahead of {r.iloc[1].strategy} ({r.iloc[1].roi:+.1%} on {int(r.iloc[1].settled)})"
    if L.settled < min_bets:
        return dict(text=head + f". Too early to call: at least ~{min_bets} settled bets are needed, a lead now is mostly luck.",
                    conclusive=False, leader=L.strategy)
    if pd.isna(L.roi_lo) or L.roi_lo <= 0:
        return dict(text=head + ". But its ROI range still includes zero or losses, so no proven edge yet.", conclusive=False, leader=L.strategy)
    if len(r) > 1 and r.iloc[1].settled >= 30 and pd.notna(r.iloc[1].roi_lo):
        se1, se2 = (L.roi - L.roi_lo) / 1.96, (r.iloc[1].roi - r.iloc[1].roi_lo) / 1.96
        if L.roi - r.iloc[1].roi <= 1.96 * math.sqrt(se1 ** 2 + se2 ** 2):
            return dict(text=head + ". The gap to the runner-up is within noise, so you cannot separate them yet.",
                        conclusive=False, leader=L.strategy)
    return dict(text=head + ". Profitable with the ROI range above zero and clearly ahead of the others.",
                conclusive=True, leader=L.strategy)


def _print_report():
    df, S = portfolio()
    print(f"PAPER equity ${S['equity']:,.2f} (start ${S['start_cash']:,.0f}, {S['total_return']:+.1%}) | cash ${S['cash']:,.2f} "
          f"| open {S['open_positions']} | closed {S['closed_trades']}")
    if not df.empty:
        print(df[["id", "match", "pick", "status", "entry_price", "shares", "cost", "mtm", "pnl"]].head(25)
              .to_string(index=False, float_format=lambda x: f"{x:,.3f}"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["auto", "settle", "report", "close", "reset", "daily", "orders", "compare", "autoall"])
    ap.add_argument("id", nargs="?", type=int)
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--cash", type=float, default=5000)
    ap.add_argument("--stake", type=float, default=25.0, help="flat stake per bet for autoall")
    a = ap.parse_args()
    pd.set_option("display.width", 200, "display.max_columns", 30)
    if a.cmd == "reset":
        reset(a.cash); print("Paper account reset.")
    elif a.cmd == "close":
        print(f"Closed, P&L ${close_trade(a.id):,.2f}")
    elif a.cmd == "settle":
        print(f"Settled {settle()} trades"); _print_report()
    elif a.cmd == "daily":
        settle(); print(daily_report().to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    elif a.cmd == "compare":
        settle(); print(strategy_report().to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
    elif a.cmd == "autoall":
        hist = tc.load_matches()
        R, W, SF = tc.build_ratings(hist), tc.build_workload(hist), tc.es.SharpFeed(tc.DATA_DIR)
        while True:
            settle(); process_orders()
            print(f"[{_now()}] new positions:", auto_all(tc.fetch_events(), R, W, SF, a.stake))
            if not a.loop:
                break
            time.sleep(a.loop)
    elif a.cmd == "orders":
        print(process_orders(), limit_stats()); print(orders_df().head(25).to_string(index=False))
    elif a.cmd == "report":
        _print_report()
    else:
        hist = tc.load_matches()
        R, W, SF = tc.build_ratings(hist), tc.build_workload(hist), tc.es.SharpFeed(tc.DATA_DIR)
        while True:
            settle(); process_orders()
            _, S = portfolio(mark=False)
            df = tc.analyse(tc.fetch_events(), R, bankroll=S["equity"], workload=W, sharp=SF)
            for m, p, res in auto_trade(df):
                print(f"[{_now()}] BOUGHT {p} in {m}: ${res['cost']:.2f} @ {res['avg_price']:.3f}")
            _print_report()
            if not a.loop:
                break
            time.sleep(a.loop)

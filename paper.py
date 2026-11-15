"""
Trade ledger: PAPER trades (fake money, real order book) and REAL trades (recorded after a live order).
Also holds today's fixed stake and the end-of-day P&L report.

  python paper.py daily      # profit / loss per day
  python paper.py settle     # settle resolved markets
  python paper.py report
  python paper.py close 5    # sell a paper trade at the current bids
  python paper.py reset --cash 5000
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
    INSERT OR IGNORE INTO meta VALUES('start_cash','5000');""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
    for name, ddl in (("mode", "TEXT DEFAULT 'paper'"), ("note", "TEXT")):
        if name not in cols:
            c.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")
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
def cash_left(c):
    start = float(c.execute("SELECT v FROM meta WHERE k='start_cash'").fetchone()[0])
    s = c.execute("""SELECT COALESCE(SUM(cost),0), COALESCE(SUM(proceeds),0) FROM trades
                     WHERE COALESCE(mode,'paper')='paper'""").fetchone()
    return start - s[0] + s[1]


def open_trade(market_id, token_id, match, pick, surface, url, usd, model_p=None, edge=None,
               source="manual", fallback_ask=None, mode="paper", note=None, fill_override=None):
    """mode='paper': fills against the live order book with paper cash.
       mode='real' : records a trade that was really placed (pass fill_override=(shares, cost, avg))."""
    c = con()
    if fill_override:
        sh, cost, avg = fill_override
    else:
        if mode == "paper" and usd > cash_left(c) + 1e-9:
            raise ValueError(f"Not enough paper cash (${cash_left(c):,.2f} left)")
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
    c.commit()
    return {"avg_price": avg, "shares": sh, "cost": cost}


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
    held = {r[0] for r in c.execute("SELECT market_id FROM trades")}
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
            res = open_trade(r.market_id, r.tokens[r.side], r.match, r.pick, r.surface, r.url, usd,
                             model_p=r.model_p, edge=r.edge, source="auto", fallback_ask=r.price)
            opened.append((r.match, r.pick, res))
        except ValueError:
            continue
    return opened


def _print_report():
    df, S = portfolio()
    print(f"PAPER equity ${S['equity']:,.2f} (start ${S['start_cash']:,.0f}, {S['total_return']:+.1%}) | cash ${S['cash']:,.2f} "
          f"| open {S['open_positions']} | closed {S['closed_trades']}")
    if not df.empty:
        print(df[["id", "match", "pick", "status", "entry_price", "shares", "cost", "mtm", "pnl"]].head(25)
              .to_string(index=False, float_format=lambda x: f"{x:,.3f}"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["auto", "settle", "report", "close", "reset", "daily"])
    ap.add_argument("id", nargs="?", type=int)
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--cash", type=float, default=5000)
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
    elif a.cmd == "report":
        _print_report()
    else:
        R = tc.build_ratings()
        while True:
            settle()
            _, S = portfolio(mark=False)
            df = tc.analyse(tc.fetch_events(), R, bankroll=S["equity"])
            for m, p, res in auto_trade(df):
                print(f"[{_now()}] BOUGHT {p} in {m}: ${res['cost']:.2f} @ {res['avg_price']:.3f}")
            _print_report()
            if not a.loop:
                break
            time.sleep(a.loop)

"""
REAL-MONEY buying on Polymarket (used by the dashboard's "Buy" button).  OFF by default.

Setup (once, in the same terminal before starting the dashboard):
    pip install py-clob-client
    set LIVE_TRADING_ENABLED=1                (Windows; use `export` on Mac/Linux)
    set POLY_PRIVATE_KEY=0x...                (your wallet / exported Polymarket key - never share or commit it)
    set POLY_FUNDER=0x...                     (the address that holds your USDC on Polymarket = profile address)
    set POLY_SIGNATURE_TYPE=1                 (1 = email/Magic login, 2 = browser-wallet proxy, 0 = plain wallet)
    set DAILY_REAL_LIMIT_USD=300              (optional safety cap on real spend per day; default 300)

Orders are Fill-Or-Kill market buys with a worst-price limit (best ask + 3 cents), so they either fill
at an acceptable price or do nothing.  START WITH $1-$5 to verify everything before using $50-$150.
This module has not been tested against the live exchange by its author - treat the first orders as a test.
"""
import os


def enabled():
    return os.environ.get("LIVE_TRADING_ENABLED") == "1"


def daily_limit():
    return float(os.environ.get("DAILY_REAL_LIMIT_USD", "300"))


def _client():
    from py_clob_client.client import ClobClient
    c = ClobClient("https://clob.polymarket.com", key=os.environ["POLY_PRIVATE_KEY"], chain_id=137,
                   signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "1")),
                   funder=os.environ.get("POLY_FUNDER"))
    c.set_api_creds(c.create_or_derive_api_creds())
    return c


def real_buy(token_id, usd, best_ask):
    """Place the order. Returns dict(ok, order_id, usd, shares, avg, raw)."""
    if not enabled():
        raise RuntimeError("Real trading is switched off. See the setup notes at the top of live_trade.py.")
    for k in ("POLY_PRIVATE_KEY", "POLY_FUNDER"):
        if not os.environ.get(k):
            raise RuntimeError(f"Environment variable {k} is not set.")
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    c = _client()
    worst = round(min(best_ask + 0.03, 0.99), 2)
    signed = c.create_market_order(MarketOrderArgs(token_id=token_id, amount=float(usd), side=BUY, price=worst))
    resp = c.post_order(signed, OrderType.FOK)
    ok = bool(resp.get("success")) and str(resp.get("status", "")).lower() in ("matched", "live", "delayed", "")
    usd_spent, shares = None, None
    try:
        usd_spent, shares = float(resp.get("makingAmount")), float(resp.get("takingAmount"))
    except (TypeError, ValueError):
        pass
    return {"ok": ok, "order_id": resp.get("orderID"), "usd": usd_spent, "shares": shares,
            "avg": (usd_spent / shares) if usd_spent and shares else None, "raw": str(resp)[:400]}

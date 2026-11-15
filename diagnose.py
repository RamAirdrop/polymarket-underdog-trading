"""Run:  python diagnose.py   -> tests each piece separately. Paste the full output back if anything fails."""
import sys, platform, importlib, json

print("Python", sys.version.split()[0], platform.platform())
for p in ("requests", "pandas", "numpy", "streamlit"):
    try:
        m = importlib.import_module(p); print(f"  [OK]   {p} {getattr(m, '__version__', '')}")
    except Exception as e:
        print(f"  [FAIL] {p}: {e}   -> pip install {p}")

import requests
import tennis_core as tc

def probe(name, url, **kw):
    try:
        r = requests.get(url, timeout=25, **kw)
        print(f"  [{'OK' if r.status_code == 200 else 'FAIL'}]   {name}: HTTP {r.status_code}, {len(r.content):,} bytes")
        return r
    except Exception as e:
        print(f"  [FAIL] {name}: {type(e).__name__}: {str(e)[:120]}")

print("\n1. History data sources (original Sackmann repos were deleted in Sept 2026)")
for i, tpl in enumerate(tc.SOURCES):
    probe(f"source {i+1} ATP 2025", tpl.format(tour="atp", year=2025))

print("\n2. Polymarket")
s = probe("Gamma /sports", f"{tc.GAMMA}/sports")
ids = tc.tennis_series()
print("      tennis series ids from /sports:", ids)
if s is not None and s.status_code == 200 and not ids:
    print("      /sports entries mentioning tennis:", [x for x in s.json() if "tennis" in json.dumps(x).lower()][:3])
for label, params in [(f"series_id={i}", {"series_id": i}) for i in ids] + [(f"tag_slug={t}", {"tag_slug": t}) for t in ("atp", "wta", "tennis")]:
    r = probe(f"events {label}", f"{tc.GAMMA}/events", params={**params, "active": "true", "closed": "false", "limit": 100})
    if r is not None and r.status_code == 200:
        ev = r.json()
        n_match = sum(tc._is_match(e) for e in ev)
        print(f"        {len(ev)} events, {n_match} look like single matches")
        for e in [e for e in ev if tc._is_match(e)][:3]:
            print("        -", e.get("title"))

print("\n3. Full pipeline")
try:
    evs = tc.fetch_events()
    print(f"  [{'OK' if evs else 'FAIL'}]   fetch_events: {len(evs)} live match events")
    for e in evs[:5]:
        m = tc.pick_moneyline(e); print("        -", e.get("title"), "|", m.get("outcomes"), m.get("outcomePrices"))
    if evs:
        tok = tc._j(tc.pick_moneyline(evs[0]).get("clobTokenIds"))[0]
        try:
            b, a = tc.get_book(tok)
            print(f"  [OK]   order book: best bid {b[0][0] if b else None}, best ask {a[0][0] if a else None}")
        except Exception as e:
            print("  [FAIL] order book:", str(e)[:120], "(some markets have no book yet; not fatal)")
except Exception as e:
    print("  [FAIL] fetch_events:", type(e).__name__, str(e)[:150])
try:
    df = tc.load_matches()
    print(f"  [OK]   history: {len(df):,} matches through {df.tourney_date.max()} "
          f"({(df.tour == 'pm').sum()} filled from Polymarket)")
    R = tc.build_ratings(df)
    for n in ("Jannik Sinner", "Alcaraz", "De Minaur", "Swiatek"):
        print(f"        {n} -> {R.find(n)}")
except Exception as e:
    print("  [FAIL] history:", type(e).__name__, str(e)[:150])

print("\n4. Live score feed (listening 15 seconds)")
try:
    import time, livescores
    L = livescores.LiveScores(); L.start(); time.sleep(15)
    print(f"  [{'OK' if L.connected else 'FAIL'}]   connected={L.connected}, tennis score updates received={L.frames}, error={L.err}")
    for slug, rec in list(L.by_slug.items())[:5]:
        print("        -", slug, "|", rec["home"], "vs", rec["away"], "|", rec["score"], rec["period"], "| live:", rec["live"])
    if L.connected and not L.frames:
        print("        (connected, but no tennis match changed score in 15s - normal if nothing is live right now)")
except Exception as e:
    print("  [FAIL] live feed:", type(e).__name__, str(e)[:150])

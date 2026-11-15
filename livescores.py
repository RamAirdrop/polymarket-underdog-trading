"""Live tennis scores from Polymarket's public Sports WebSocket (no login needed).
Keeps the latest score per match in memory; the dashboard looks scores up by event slug / player names."""
import json, threading, time
import tennis_core as tc

WS = "wss://sports-api.polymarket.com/ws"


def _last(name):
    t = tc.norm(name).split()
    return t[-1] if t else ""


class LiveScores:
    def __init__(self):
        self.by_slug, self.by_names = {}, {}
        self.frames, self.err, self.connected = 0, None, False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            import websocket
        except ImportError:
            self.err = "pip install websocket-client"; return
        while True:
            try:
                ws = websocket.WebSocketApp(WS, on_open=lambda w: setattr(self, "connected", True),
                                            on_message=self._msg,
                                            on_error=lambda w, e: setattr(self, "err", str(e)[:150]),
                                            on_close=lambda *a: setattr(self, "connected", False))
                ws.run_forever()
            except Exception as e:
                self.err = str(e)[:150]
            time.sleep(3)

    def _msg(self, ws, msg):
        if str(msg).strip().lower() == "ping":
            ws.send("pong"); return
        try:
            data = json.loads(msg)
        except ValueError:
            return
        for fr in (data if isinstance(data, list) else [data]):
            if isinstance(fr, dict):
                self.ingest(fr)

    def ingest(self, fr):
        st = fr.get("eventState") if isinstance(fr.get("eventState"), dict) else {}
        slug = str(fr.get("slug") or st.get("slug") or "")
        league = str(fr.get("leagueAbbreviation") or "").lower()
        if not (st.get("type") == "tennis" or league in ("atp", "wta", "itf", "tennis", "challenger")
                or slug.startswith(("atp-", "wta-", "itf-"))):
            return
        score = st.get("score") or fr.get("score")
        if not score:
            return
        rec = {"score": score, "period": st.get("period") or fr.get("period") or "",
               "live": st.get("live", fr.get("live")), "ended": bool(st.get("ended", fr.get("ended"))),
               "home": fr.get("homeTeam") or "", "away": fr.get("awayTeam") or "", "ts": time.time()}
        self.frames += 1
        if slug:
            self.by_slug[slug] = rec
        if rec["home"] and rec["away"]:
            self.by_names[frozenset((_last(rec["home"]), _last(rec["away"])))] = rec

    def lookup(self, slug, outs):
        rec = self.by_slug.get(str(slug or "")) or self.by_names.get(frozenset((_last(outs[0]), _last(outs[1]))))
        if not rec:
            return None
        h = _last(rec["home"])
        oriented = bool(h) and h in (_last(outs[0]), _last(outs[1]))
        return {**rec, "a_is_home": (h == _last(outs[0])) if oriented else True, "oriented": oriented,
                "age_s": time.time() - rec["ts"]}

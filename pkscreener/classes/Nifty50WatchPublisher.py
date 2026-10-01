"""Optional WATCH-only NIFTY50 milestone delivery; no trading dependencies."""
from __future__ import annotations
import json
from pathlib import Path

class Nifty50WatchPublisher:
    def __init__(self, state_root, transport, enabled=False): self.state_root,self.transport,self.enabled=Path(state_root),transport,bool(enabled)
    def process(self, events):
        if not self.enabled: return {"status":"notification_disabled"}
        state_path=self.state_root/"nifty50_watch_sent.json"; state=json.loads(state_path.read_text()) if state_path.exists() else {"sent":[]}; sent=set(state["sent"]); pending=[e for e in events if e.get("event") in {"first_nifty50_top10_at","first_nifty50_top5_at"} and e["event_id"] not in sent]
        if not pending:return {"status":"up_to_date"}
        if self.transport is None:return {"status":"no_transport","pending":len(pending)}
        for e in pending:
            result=self.transport.post(f"👀 **NIFTY50 WATCH ONLY** — **{e['symbol']}** entered NIFTY50 {e['event'].replace('first_nifty50_','').replace('_at','').upper()}. Not a BUY signal.")
            if not result.delivered:return {"status":"delivery_failed","pending":len(pending)}
            sent.add(e["event_id"])
        state_path.parent.mkdir(parents=True,exist_ok=True); state_path.write_text(json.dumps({"sent":sorted(sent)})); return {"status":"delivered","sent":len(pending)}

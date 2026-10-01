"""Durable, optional WATCH-only NIFTY50 outbox; no trading dependencies."""
from __future__ import annotations
import json, os, tempfile, time
from pathlib import Path
def _atomic(path,value):
    fd,tmp=tempfile.mkstemp(dir=Path(path).parent,suffix=".tmp")
    with os.fdopen(fd,"w") as h: json.dump(value,h,sort_keys=True)
    os.replace(tmp,path)
class Nifty50WatchPublisher:
    def __init__(self,state_root,transport,enabled=False,clock=time.time): self.state_root,self.transport,self.enabled,self.clock=Path(state_root),transport,bool(enabled),clock
    def process(self,events):
        if not self.enabled:return {"status":"notification_disabled"}
        path=self.state_root/"outbox.json"; state=json.loads(path.read_text()) if path.exists() else {"pending":{},"delivered":[]}; delivered=set(state["delivered"])
        for e in events:
            if e.get("event") in {"first_nifty50_top10_at","first_nifty50_top5_at"} and e["event_id"] not in delivered: state["pending"].setdefault(e["event_id"],e)
        self.state_root.mkdir(parents=True,exist_ok=True); _atomic(path,state)
        if self.transport is None:return {"status":"no_transport","pending":len(state["pending"])}
        sent=0
        for event_id,e in list(state["pending"].items()):
            if float(e.get("retry_after_epoch",0))>self.clock():continue
            try: result=self.transport.post(f"👀 **NIFTY50 WATCH ONLY** — **{e['symbol']}** entered NIFTY50 {e['event'].replace('first_nifty50_','').replace('_at','').upper()}. Not a BUY signal.")
            except Exception:
                _atomic(path,state); return {"status":"delivery_failed","pending":len(state["pending"])}
            if not result.delivered:
                e["retry_after_epoch"]=self.clock()+max(1,float(getattr(result,"retry_after_seconds",0) or 0)); state["pending"][event_id]=e; _atomic(path,state); return {"status":"delivery_failed","pending":len(state["pending"])}
            state["pending"].pop(event_id); delivered.add(event_id); state["delivered"]=sorted(delivered); _atomic(path,state); sent+=1
        return {"status":"delivered" if sent else "up_to_date","sent":sent,"pending":len(state["pending"])}

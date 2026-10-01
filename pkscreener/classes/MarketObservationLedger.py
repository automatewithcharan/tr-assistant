"""Append-only, WATCH-only evidence from atomic/raw Kite snapshots."""
from __future__ import annotations
import csv, json, os, tempfile, hashlib
from datetime import datetime
from pathlib import Path
from pkscreener.classes.NGP5EarlyWatchPublisher import IST

BROAD_CONFIRMATION_COVERAGE_PCT, CLOCK_SKEW_SECONDS = 80.0, 10.0
def _symbols(path, column, eq=False):
    with Path(path).open(newline="", encoding="utf-8-sig") as h: return {str(r.get(column) or "").strip().upper() for r in csv.DictReader(h) if r.get(column) and (not eq or str(r.get("SERIES") or "").upper()=="EQ")}
def _atomic(path, value):
    fd,tmp=tempfile.mkstemp(dir=Path(path).parent,suffix=".tmp")
    with os.fdopen(fd,"w",encoding="utf-8") as h: json.dump(value,h,sort_keys=True)
    os.replace(tmp,path)

class MarketObservationLedger:
    """No alert/order methods: raw evidence only."""
    def __init__(self, root, eq_path, nifty_path, tokens_path, clock=datetime.now, confirmation_coverage_pct=BROAD_CONFIRMATION_COVERAGE_PCT):
        self.root,self.eq_path,self.nifty_path,self.tokens_path=map(Path,(root,eq_path,nifty_path,tokens_path)); self.clock=clock; self.confirmation_coverage_pct=float(confirmation_coverage_pct)
    def _ticks(self,path):
        path=Path(path); atomic=path.with_suffix(".json")
        if atomic.exists():
            x=json.loads(atomic.read_text()); return list(x.values()) if isinstance(x,dict) else []
        before=path.stat(); rows=list(csv.DictReader(path.open(newline="",encoding="utf-8"))); after=path.stat()
        if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size): raise ValueError("ticker CSV changed during read")
        return rows
    def process(self,ticks_path):
        now=self.clock().astimezone(IST); base=self.root/now.date().isoformat(); base.mkdir(parents=True,exist_ok=True)
        eq,nifty=_symbols(self.eq_path,"SYMBOL",True),_symbols(self.nifty_path,"Symbol"); subscribed=_symbols(self.tokens_path,"Symbol") if self.tokens_path.exists() else set(); changes={}
        for r in self._ticks(ticks_path):
            s=str(r.get("symbol") or "").strip().upper()
            if s not in eq or str(r.get("exchange") or "").upper()!="NSE": continue
            try: received=datetime.fromisoformat(str(r["received_at"]).replace("Z","+00:00")).astimezone(IST); traded=datetime.fromisoformat(str(r["last_trade_time"])).date(); price,close=float(r["last_price"]),float(r["close"])
            except (KeyError,TypeError,ValueError): continue
            age=(now-received).total_seconds()
            if traded==now.date() and -CLOCK_SKEW_SECONDS<=age<=120 and close>0: changes[s]=(price/close-1)*100
        broad={s:i+1 for i,(s,_) in enumerate(sorted(changes.items(),key=lambda x:(-x[1],x[0])))}; nr={s:i+1 for i,(s,_) in enumerate(sorted(((s,v) for s,v in changes.items() if s in nifty),key=lambda x:(-x[1],x[0])))}
        coverage=len(changes)/max(1,len(eq))*100; qualified=coverage>=self.confirmation_coverage_pct; nifty_coverage=len(nr)/max(1,len(nifty))*100; nifty_qualified=nifty_coverage>=100.0; state_path=base/"state.json"; state=json.loads(state_path.read_text()) if state_path.exists() else {"symbols":{}}; events=[]
        for s in sorted(eq):
            p=state["symbols"].get(s,{}); rank,nrank=broad.get(s),nr.get(s); fresh=s in changes; record={"expected":True,"token_mapped":s in subscribed,"fresh":fresh,"provisional_rank":rank,"rank_universe_size":len(changes),"expected_universe_size":len(eq),"broad_rank_coverage_pct":coverage,"broad_rank_required_coverage_pct":self.confirmation_coverage_pct,"broad_rank_coverage_qualified":qualified,"provisional_nifty50_rank":nrank,"nifty50_rank_universe_size":len(nr),"expected_nifty50_size":len(nifty),"nifty50_rank_coverage_pct":nifty_coverage,"previous_observation_at":p.get("observation_at"),"previous_provisional_rank":p.get("provisional_rank"),"previous_nifty50_rank":p.get("provisional_nifty50_rank"),"observation_at":now.isoformat()}
            for t in (100,50,20,10,5):
                for key,ok,flag in ((f"first_provisional_top{t}_at",bool(rank and rank<=t),False),(f"first_coverage_qualified_top{t}_at",bool(qualified and rank and rank<=t),True)):
                    if ok and not p.get(key): record[key]=now.isoformat(); events.append({"symbol":s,"event":key,"at":now.isoformat(),"rank":rank,"confirmed":flag})
                    elif p.get(key): record[key]=p[key]
            for t in (10,5):
                for key,ok in ((f"first_provisional_nifty50_top{t}_at",bool(nrank and nrank<=t)),(f"first_nifty50_top{t}_at",bool(nifty_qualified and nrank and nrank<=t))):
                    if ok and not p.get(key): record[key]=now.isoformat(); events.append({"symbol":s,"event":key,"at":now.isoformat(),"nifty50_rank":nrank,"coverage_qualified":nifty_qualified})
                    elif p.get(key): record[key]=p[key]
            if fresh and not p.get("first_fresh_at"): record["first_fresh_at"]=now.isoformat(); events.append({"symbol":s,"event":"first_fresh_at","at":now.isoformat()})
            elif p.get("first_fresh_at"): record["first_fresh_at"]=p["first_fresh_at"]
            if p.get("fresh")!=fresh: events.append({"symbol":s,"event":"fresh" if fresh else "stale","at":now.isoformat()})
            for prefix,a,b in (("rank",p.get("provisional_rank"),rank),("nifty50_rank",p.get("provisional_nifty50_rank"),nrank)):
                if a and b and p.get("observation_at"):
                    minutes=max((now-datetime.fromisoformat(p["observation_at"])).total_seconds()/60,1e-9); record[f"{prefix}_improvement"]=a-b; record[f"{prefix}_velocity_per_minute"]=(a-b)/minutes
            state["symbols"][s]=record
        with (base/"events.jsonl").open("a",encoding="utf-8") as h:
            for e in events:
                e["event_id"]=hashlib.sha256(f"{e['symbol']}|{e['event']}|{e['at']}".encode()).hexdigest(); h.write(json.dumps(e,sort_keys=True)+"\n")
            h.flush(); os.fsync(h.fileno())
        _atomic(state_path,state)
        out={"at":now.isoformat(),"expected_nse_eq":len(eq),"mapped_nse_eq":len(eq&subscribed),"fresh_nse_eq":len(changes),"expected_nifty50":len(nifty),"mapped_nifty50":len(nifty&subscribed),"fresh_nifty50":len(nifty&set(changes)),"missing_nse_eq":len(eq-subscribed),"stale_nse_eq":len(eq-set(changes)),"missing_nifty50":len(nifty-subscribed),"stale_nifty50":len(nifty-set(changes)),"rank_universe_size":len(changes),"broad_rank_coverage_pct":coverage,"broad_rank_coverage_qualified":qualified,"nifty50_rank_coverage_pct":nifty_coverage,"nifty50_rank_coverage_qualified":nifty_qualified}
        with (base/"coverage.jsonl").open("a",encoding="utf-8") as h:h.write(json.dumps(out,sort_keys=True)+"\n")
        return {"status":"observed",**out,"events":len(events),"event_records":events}

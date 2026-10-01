"""Compact, shadow-only causal recorder for emerging-leader research."""
from __future__ import annotations
import csv,json,os,shutil,sqlite3,statistics,time
from collections import defaultdict,deque
from datetime import datetime
from pathlib import Path
from typing import Any
from pkscreener.classes.NGP5EarlyWatchPublisher import IST

SCHEMA_VERSION="emerging_leader_radar_v1_1"; SNAPSHOT_SECONDS=60; FRESH_SECONDS=120; CLOCK_SKEW_SECONDS=10
RANK_MILESTONES=(1500,1000,750,500,400,300,250,200,150,125,100,75,50,20,10,5)
FEATURE_COLUMNS=("last_price previous_close current_pct_change broad_rank broad_percentile rank_1m_ago rank_3m_ago rank_5m_ago rank_10m_ago rank_improvement_1m rank_improvement_3m rank_improvement_5m rank_improvement_10m rank_velocity_1m rank_velocity_5m rank_acceleration return_1m return_3m return_5m return_10m cumulative_volume volume_change_1m volume_change_3m volume_change_5m volume_rate_1m volume_rate_5m volume_rate_acceleration approx_turnover turnover_change_1m turnover_change_5m turnover_acceleration distance_from_day_high_pct distance_from_day_low_pct distance_from_open_pct best_bid best_ask spread_bps causal_vwap price_vs_vwap_pct rs_vs_nifty rs_change_1m rs_change_5m rvol_at_time price_momentum_percentile rank_velocity_percentile volume_rate_percentile volume_acceleration_percentile turnover_percentile turnover_acceleration_percentile rs_percentile pct_change_gap_to_top100 pct_change_gap_to_top50 pct_change_gap_to_top20 pct_change_gap_to_top10").split()

def _symbols(path,column,eq=False):
    with Path(path).open(newline="",encoding="utf-8-sig") as h:return {str(r.get(column) or "").strip().upper() for r in csv.DictReader(h) if r.get(column) and (not eq or str(r.get("SERIES") or "").upper()=="EQ")}
def _number(v):
    try:v=float(v)
    except (TypeError,ValueError):return None
    return v if v==v and abs(v)!=float("inf") else None
def _utcish(v):
    try:return datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(IST)
    except (TypeError,ValueError):return None
def _percentile(values):
    ordered,result=sorted(values.items(),key=lambda x:(x[1],x[0])),{}; index=0
    while index<len(ordered):
        end=index
        while end+1<len(ordered) and ordered[end+1][1]==ordered[index][1]:end+=1
        value=100.0 if len(ordered)==1 else ((index+end)/2)/(len(ordered)-1)*100
        result.update({s:value for s,_ in ordered[index:end+1]});index=end+1
    return result

class EmergingLeaderRecorder:
    """Fresh-only SQLite rows. No publisher, order, or qualification dependency."""
    def __init__(self,root,eq_path,tokens_path,recorder_git_sha,clock=datetime.now):
        self.root,self.eq_path,self.tokens_path=map(Path,(root,eq_path,tokens_path));self.recorder_git_sha,self.clock=recorder_git_sha,clock
        self.history=defaultdict(lambda:deque(maxlen=80));self.previous_rank={};self.last_snapshot_at=None;self.source_failures=[];self._disk_paused=False
    def _ticks(self,path):
        atomic=Path(path).with_suffix(".json")
        if not atomic.exists():raise FileNotFoundError(f"atomic Kite JSON snapshot unavailable: {atomic}")
        x=json.loads(atomic.read_text());return list(x.values()) if isinstance(x,dict) else x if isinstance(x,list) else (_ for _ in ()).throw(ValueError("unsupported Kite snapshot"))
    @staticmethod
    def _past(history,now,seconds):
        target=now.timestamp()-seconds;c=[x for x in history if x["at"].timestamp()<=target]
        return c[-1] if c and target-c[-1]["at"].timestamp()<=30 else None
    @staticmethod
    def _change(a,b):return None if a is None or b is None else a-b
    @staticmethod
    def _return(a,b):return None if a is None or b in (None,0) else (a/b-1)*100
    @staticmethod
    def _epoch(now):return int(now.timestamp()*1000)
    def _db(self,date,expected,now_ms):
        day=self.root/date;day.mkdir(parents=True,exist_ok=True);c=sqlite3.connect(day/"emerging_leader.sqlite");c.execute("pragma journal_mode=wal");c.execute("pragma synchronous=normal")
        c.executescript("""create table if not exists session_metadata(session_id integer primary key,session_date text unique not null,schema_version text not null,recorder_git_sha text not null,expected_symbol_count integer not null,started_epoch_ms integer not null,cycle_count integer not null default 0,source_failure_count integer not null default 0);create table if not exists symbols(symbol_id integer primary key,symbol text unique not null);create table if not exists session_symbol_metadata(session_id integer not null,symbol_id integer not null,token_mapped_at_session_start integer not null,primary key(session_id,symbol_id));create table if not exists mapping_events(session_id integer not null,symbol_id integer not null,timestamp_epoch_ms integer not null,token_mapped integer not null,primary key(session_id,symbol_id,timestamp_epoch_ms));create table if not exists snapshot_times(snapshot_id integer primary key,session_id integer not null,timestamp_epoch_ms integer not null,fresh_count integer not null,expected_count integer not null,coverage_pct real not null,nifty_fresh integer not null,nifty_pct_change real,positive_count integer not null,negative_count integer not null,median_pct_change real,top100_cutoff_pct_change real,top50_cutoff_pct_change real,top20_cutoff_pct_change real,top10_cutoff_pct_change real,return_dispersion real,unique(session_id,timestamp_epoch_ms));create table if not exists rank_events(session_id integer not null,symbol_id integer not null,timestamp_epoch_ms integer not null,milestone integer not null,broad_rank integer not null,pct_change real,rank_universe_size integer not null,expected_universe_size integer not null,coverage_pct real not null,last_price real,cumulative_volume real,approx_turnover real,primary key(session_id,symbol_id,milestone));create table if not exists symbol_state(session_id integer not null,symbol_id integer not null,last_rank integer not null,last_observation_epoch_ms integer not null,token_mapped integer not null,primary key(session_id,symbol_id));""")
        c.execute("create table if not exists features(snapshot_id integer not null,symbol_id integer not null,data_age_seconds real,"+",".join(f"{x} real" for x in FEATURE_COLUMNS)+",primary key(snapshot_id,symbol_id))");c.execute("create index if not exists idx_features_symbol_snapshot on features(symbol_id,snapshot_id)")
        row=c.execute("select session_id from session_metadata where session_date=?",(date,)).fetchone()
        if not row:c.execute("insert into session_metadata(session_date,schema_version,recorder_git_sha,expected_symbol_count,started_epoch_ms) values(?,?,?,?,?)",(date,SCHEMA_VERSION,self.recorder_git_sha,len(expected),now_ms));sid=c.execute("select last_insert_rowid()").fetchone()[0]
        else:sid=row[0]
        return c,sid
    def _ids(self,c,sid,expected,mapped,now_ms):
        c.executemany("insert or ignore into symbols(symbol) values(?)",[(s,) for s in expected]);ids={s:i for i,s in c.execute("select symbol_id,symbol from symbols where symbol in ("+','.join('?'*len(expected))+")",tuple(expected))}
        for s,i in ids.items():
            flag=int(s in mapped);old=c.execute("select token_mapped_at_session_start from session_symbol_metadata where session_id=? and symbol_id=?",(sid,i)).fetchone()
            if old is None:c.execute("insert into session_symbol_metadata values(?,?,?)",(sid,i,flag))
            elif old[0]!=flag:c.execute("insert or ignore into mapping_events values(?,?,?,?)",(sid,i,now_ms,flag))
        return ids
    def _disk_ok(self):
        free=shutil.disk_usage(self.root if self.root.exists() else self.root.parent).free/1024**3;low=float(os.getenv("NG_EMERGING_LEADER_MIN_FREE_GB","5"));resume=float(os.getenv("NG_EMERGING_LEADER_RESUME_FREE_GB","8"))
        self._disk_paused=free<resume if self._disk_paused else free<low;return not self._disk_paused,free
    def _manifest(self,date,c,sid,now):
        path=self.root/date/"manifest.json";meta=c.execute("select cycle_count,source_failure_count,expected_symbol_count from session_metadata where session_id=?",(sid,)).fetchone();ctx=c.execute("select min(coverage_pct),avg(coverage_pct),max(coverage_pct),count(*) from snapshot_times where session_id=?",(sid,)).fetchone()
        counts={"features":c.execute("select count(*) from features f join snapshot_times s on s.snapshot_id=f.snapshot_id where s.session_id=?",(sid,)).fetchone()[0],"rank_events":c.execute("select count(*) from rank_events where session_id=?",(sid,)).fetchone()[0],"mapping_events":c.execute("select count(*) from mapping_events where session_id=?",(sid,)).fetchone()[0]}
        value={"date":date,"schema_version":SCHEMA_VERSION,"recorder_git_sha":self.recorder_git_sha,"recorder_end_time":now.isoformat(),"expected_cycles":1500,"completed_cycles":meta[0],"missed_cycles":max(0,1500-meta[0]),"symbols_expected":meta[2],"fresh_coverage_min":ctx[0],"fresh_coverage_median":ctx[1],"fresh_coverage_max":ctx[2],"snapshot_count":ctx[3],"database_row_counts":counts,"source_failure_count":meta[1],"valid_for_research":bool(ctx[2] and ctx[2]>=80),"fresh_row_contract":"absence of feature row means no valid fresh observation at snapshot T"};tmp=path.with_suffix(".tmp");tmp.write_text(json.dumps(value,indent=2,sort_keys=True));os.replace(tmp,path)
    def process(self,ticks_path):
        started=time.perf_counter();now=self.clock().astimezone(IST);date=now.date().isoformat();now_ms=self._epoch(now)
        try:ticks=self._ticks(ticks_path)
        except Exception as e:self.source_failures.append(str(e));return {"status":"source_unavailable","error":str(e)}
        expected=_symbols(self.eq_path,"SYMBOL",True);mapped=_symbols(self.tokens_path,"Symbol") if self.tokens_path.exists() else set();raw={};nifty=None
        for t in ticks:
            s=str(t.get("symbol") or "").strip().upper();received=_utcish(t.get("received_at"))
            if s=="NIFTY 50":nifty=t
            if s not in expected or str(t.get("exchange") or "").upper()!="NSE":continue
            trade,price,close=_utcish(t.get("last_trade_time")),_number(t.get("last_price")),_number(t.get("close"));age=(now-received).total_seconds() if received else None
            if received and trade and trade.date()==now.date() and age is not None and -CLOCK_SKEW_SECONDS<=age<=FRESH_SECONDS and price is not None and close not in (None,0):raw[s]={"tick":t,"age":age,"price":price,"close":close,"pct":(price/close-1)*100}
        ranked=sorted(raw.items(),key=lambda x:(-x[1]["pct"],x[0]));ranks={s:i+1 for i,(s,_) in enumerate(ranked)};cut={n:ranked[n-1][1]["pct"] if len(ranked)>=n else None for n in (100,50,20,10)};nifty_pct=None;nifty_fresh=False
        if nifty:
            r,tr,p,cl=_utcish(nifty.get("received_at")),_utcish(nifty.get("last_trade_time")),_number(nifty.get("last_price")),_number(nifty.get("close"));age=(now-r).total_seconds() if r else None
            if r and tr and tr.date()==now.date() and age is not None and -CLOCK_SKEW_SECONDS<=age<=FRESH_SECONDS and p is not None and cl not in (None,0):nifty_pct,nifty_fresh=(p/cl-1)*100,True
        current={}
        for s,v in raw.items():
            vol=_number(v["tick"].get("volume_traded"));current[s]={"at":now,"rank":ranks[s],"price":v["price"],"volume":vol,"turnover":v["price"]*vol if vol is not None else None,"pct":v["pct"],"rs":v["pct"]-nifty_pct if nifty_pct is not None else None};self.history[s].append(current[s])
        coverage=len(raw)/max(1,len(expected))*100;c,sid=self._db(date,expected,now_ms)
        try:
            with c:
                c.execute("update session_metadata set cycle_count=cycle_count+1 where session_id=?",(sid,));ids=self._ids(c,sid,expected,mapped,now_ms);persisted={i:r for i,r in c.execute("select symbol_id,last_rank from symbol_state where session_id=?",(sid,))};events=0
                for s,v in current.items():
                    i=ids[s];previous=self.previous_rank.get(s,persisted.get(i))
                    if previous is not None:
                        for m in RANK_MILESTONES:
                            if previous>m and v["rank"]<=m:events+=c.execute("insert or ignore into rank_events values(?,?,?,?,?,?,?,?,?,?,?,?)",(sid,i,now_ms,m,v["rank"],v["pct"],len(raw),len(expected),coverage,v["price"],v["volume"],v["turnover"])).rowcount
                    self.previous_rank[s]=v["rank"];c.execute("insert or replace into symbol_state values(?,?,?,?,?)",(sid,i,v["rank"],now_ms,int(s in mapped)))
            if self.last_snapshot_at and (now-self.last_snapshot_at).total_seconds()<SNAPSHOT_SECONDS:self._manifest(date,c,sid,now);return {"status":"events_only","events":events,"fresh":len(raw),"expected":len(expected),"processing_ms":round((time.perf_counter()-started)*1000,3)}
            ok,free=self._disk_ok()
            if not ok:self._manifest(date,c,sid,now);return {"status":"research_paused_low_disk","free_gb":round(free,3),"events":events,"fresh":len(raw),"expected":len(expected)}
            self.last_snapshot_at=now;rows={}
            for s,v in current.items():
                t=raw[s]["tick"];past={m:self._past(self.history[s],now,m*60) for m in (1,3,5,10)};imp={m:self._change(past[m]["rank"] if past[m] else None,v["rank"]) for m in past};vel={m:(imp[m]/m if imp[m] is not None else None) for m in (1,5)};high,low,op,bid,ask,vwap=(_number(t.get(k)) for k in ("high","low","open","best_bid","best_ask","average_traded_price"));spread=(ask-bid)/((ask+bid)/2)*10000 if bid and ask and ask>=bid else None
                row={"data_age_seconds":raw[s]["age"],"last_price":v["price"],"previous_close":raw[s]["close"],"current_pct_change":v["pct"],"broad_rank":v["rank"],"broad_percentile":((len(raw)-v["rank"])/(len(raw)-1)*100 if len(raw)>1 else None),"rank_acceleration":((vel[1]-vel[5])/4 if vel[1] is not None and vel[5] is not None else None),"cumulative_volume":v["volume"],"approx_turnover":v["turnover"],"distance_from_day_high_pct":self._return(v["price"],high),"distance_from_day_low_pct":self._return(v["price"],low),"distance_from_open_pct":self._return(v["price"],op),"best_bid":bid,"best_ask":ask,"spread_bps":spread,"causal_vwap":vwap,"price_vs_vwap_pct":self._return(v["price"],vwap),"rs_vs_nifty":v["rs"],"rvol_at_time":None}
                for m in (1,3,5,10):
                    old=past[m];row[f"rank_{m}m_ago"]=old["rank"] if old else None;row[f"rank_improvement_{m}m"]=imp[m];row[f"return_{m}m"]=self._return(v["price"],old["price"] if old else None)
                    if m in (1,3,5):row[f"volume_change_{m}m"]=self._change(v["volume"],old["volume"] if old else None)
                row["rank_velocity_1m"],row["rank_velocity_5m"]=vel[1],vel[5];row["volume_rate_1m"]=row.get("volume_change_1m");row["volume_rate_5m"]=(row.get("volume_change_5m")/5 if row.get("volume_change_5m") is not None else None);row["volume_rate_acceleration"]=(row["volume_rate_1m"]-row["volume_rate_5m"])/4 if row["volume_rate_1m"] is not None and row["volume_rate_5m"] is not None else None;row["turnover_change_1m"]=self._change(v["turnover"],past[1]["turnover"] if past[1] else None);row["turnover_change_5m"]=self._change(v["turnover"],past[5]["turnover"] if past[5] else None);row["turnover_acceleration"]=(row["turnover_change_1m"]-row["turnover_change_5m"]/5)/4 if row["turnover_change_1m"] is not None and row["turnover_change_5m"] is not None else None;row["rs_change_1m"]=self._change(v["rs"],past[1]["rs"] if past[1] else None);row["rs_change_5m"]=self._change(v["rs"],past[5]["rs"] if past[5] else None)
                for n in (100,50,20,10):row[f"pct_change_gap_to_top{n}"]=v["pct"]-cut[n] if cut[n] is not None else None
                rows[s]=row
            for dst,src in {"price_momentum_percentile":"current_pct_change","rank_velocity_percentile":"rank_velocity_1m","volume_rate_percentile":"volume_rate_1m","volume_acceleration_percentile":"volume_rate_acceleration","turnover_percentile":"approx_turnover","turnover_acceleration_percentile":"turnover_acceleration","rs_percentile":"rs_vs_nifty"}.items():
                for s,value in _percentile({s:r[src] for s,r in rows.items() if r.get(src) is not None}).items():rows[s][dst]=value
            changes=[v["pct"] for v in current.values()]
            with c:
                c.execute("insert into snapshot_times(session_id,timestamp_epoch_ms,fresh_count,expected_count,coverage_pct,nifty_fresh,nifty_pct_change,positive_count,negative_count,median_pct_change,top100_cutoff_pct_change,top50_cutoff_pct_change,top20_cutoff_pct_change,top10_cutoff_pct_change,return_dispersion) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(sid,now_ms,len(raw),len(expected),coverage,int(nifty_fresh),nifty_pct,sum(x>0 for x in changes),sum(x<0 for x in changes),statistics.median(changes) if changes else None,cut[100],cut[50],cut[20],cut[10],statistics.pstdev(changes) if len(changes)>1 else None));snapshot=c.execute("select last_insert_rowid()").fetchone()[0];cols=["snapshot_id","symbol_id","data_age_seconds"]+FEATURE_COLUMNS;c.executemany("insert into features("+','.join(cols)+") values("+','.join('?'*len(cols))+")",[tuple([snapshot,ids[s]]+[row.get(k) for k in cols[2:]]) for s,row in rows.items()])
            self._manifest(date,c,sid,now);return {"status":"recorded","events":events,"rows":len(rows),"fresh":len(raw),"expected":len(expected),"free_gb":round(free,3),"processing_ms":round((time.perf_counter()-started)*1000,3)}
        finally:c.close()

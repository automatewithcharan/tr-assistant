#!/usr/bin/env python3
"""Offline-only future labels; raw feature evidence is never modified."""
from __future__ import annotations
import argparse,sqlite3
from datetime import date,datetime,time as clock_time
from pathlib import Path
from pkscreener.classes.NGP5EarlyWatchPublisher import IST
REQUIRED_COVERAGE=80.0; MAX_PERSISTENCE_GAP_MINUTES=2.0; LEVELS=(100,50,20,10)
def _eod(read,sid):
    rows=read.execute("select snapshot_id,timestamp_epoch_ms,fresh_count,expected_count,coverage_pct from snapshot_times where session_id=? and coverage_pct>=? order by timestamp_epoch_ms",(sid,REQUIRED_COVERAGE)).fetchall()
    return next((r for r in reversed(rows) if clock_time(15,28)<=datetime.fromtimestamp(r[1]/1000,IST).time()<=clock_time(15,30)),None)
def _max_gap(rows):return max(((b[1]-a[1])/60000 for a,b in zip(rows,rows[1:])),default=None)
def label_session(root:Path,session_date:str,now:datetime|None=None)->int:
    now=now or datetime.now(IST)
    if date.fromisoformat(session_date)>=now.date():raise ValueError("labels may only be generated after the completed session date")
    source=root/session_date/"emerging_leader.sqlite";destination=root/session_date/"emerging_leader_labels.sqlite";read=sqlite3.connect(f"file:{source}?mode=ro",uri=True);write=sqlite3.connect(destination);write.execute("drop table if exists future_labels")
    write.execute("""create table future_labels(session_id integer,snapshot_id integer,symbol_id integer,eligible_top100_entry integer,eligible_top50_entry integer,eligible_top20_entry integer,eligible_top10_entry integer,entered_top100_later integer,entered_top50_later integer,entered_top20_later integer,entered_top10_later integer,minutes_to_top100 real,minutes_to_top50 real,minutes_to_top20 real,minutes_to_top10 real,future_qualified_snapshot_count integer,last_future_qualified_timestamp integer,max_future_qualified_gap_minutes real,future_label_valid integer,best_future_rank integer,eod_rank integer,eod_top100 integer,eod_top50 integer,eod_top20 integer,symbol_eod_rank_valid integer,top100_persistence_observed_minutes real,top100_persistence_max_gap_minutes real,eod_coverage_pct real,eod_expected_count integer,eod_fresh_count integer,eod_label_timestamp_epoch_ms integer,eod_label_valid integer,forward_return_5m real,forward_return_10m real,forward_return_20m real,forward_return_30m real,forward_return_60m real,return_to_eod real,mfe_5m real,mfe_10m real,mfe_20m real,mfe_30m real,mfe_60m real,mae_5m real,mae_10m real,mae_20m real,mae_30m real,mae_60m real,primary key(snapshot_id,symbol_id))""")
    sid=read.execute("select session_id from session_metadata where session_date=?",(session_date,)).fetchone()[0];snapshots=read.execute("select snapshot_id,timestamp_epoch_ms from snapshot_times where session_id=? and coverage_pct>=? order by timestamp_epoch_ms",(sid,REQUIRED_COVERAGE)).fetchall();eod=_eod(read,sid)
    by_symbol={}
    for row in read.execute("select f.snapshot_id,f.symbol_id,s.timestamp_epoch_ms,f.broad_rank from features f join snapshot_times s on s.snapshot_id=f.snapshot_id where s.session_id=? order by f.symbol_id,s.timestamp_epoch_ms",(sid,)):by_symbol.setdefault(row[1],[]).append(row)
    output=[]
    for symbol,series in by_symbol.items():
        by_snapshot={r[0]:r for r in series}
        for snapshot,_,at,rank in series:
            future_snapshots=[x for x in snapshots if x[1]>at];future_rows=[by_snapshot.get(sid) for sid,_ in future_snapshots];present=[r for r in future_rows if r is not None and r[3] is not None];complete=bool(future_snapshots) and len(present)==len(future_snapshots);eligible={level:(None if rank is None else int(rank>level)) for level in LEVELS};entered={};minutes={}
            for level in LEVELS:
                hit=next((r for r in present if r[3]<=level),None)
                entered[level],minutes[level]=(None,None) if eligible[level]!=1 else ((1,(hit[2]-at)/60000) if hit else ((0,None) if complete else (None,None)))
            eod_row=by_snapshot.get(eod[0]) if eod else None;eod_rank=eod_row[3] if eod_row and eod_row[3] is not None else None;qualified=[by_snapshot[sid] for sid,timestamp in snapshots if timestamp>=at and sid in by_snapshot and by_snapshot[sid][3] is not None];pairs=[(a,b) for a,b in zip(qualified,qualified[1:]) if a[3]<=100 and b[3]<=100];gaps=[(b[2]-a[2])/60000 for a,b in pairs];observed=sum(g for g in gaps if g<=MAX_PERSISTENCE_GAP_MINUTES)
            output.append((sid,snapshot,symbol,*(eligible[l] for l in LEVELS),*(entered[l] for l in LEVELS),*(minutes[l] for l in LEVELS),len(future_snapshots),future_snapshots[-1][1] if future_snapshots else None,_max_gap(future_snapshots),int(complete),min((r[3] for r in present),default=None),eod_rank,*(int(eod_rank<=l) if eod_rank is not None else None for l in (100,50,20)),int(eod_rank is not None),observed if future_snapshots else None,max(gaps,default=None),eod[4] if eod else None,eod[3] if eod else None,eod[2] if eod else None,eod[1] if eod else None,int(eod is not None),*([None]*16)))
    with write:write.executemany("insert into future_labels values("+','.join('?'*48)+")",output)
    read.close();write.close();return len(output)
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--root",type=Path,required=True);p.add_argument("--session-date",required=True);a=p.parse_args();print({"labels":label_session(a.root,a.session_date)});return 0
if __name__=="__main__":raise SystemExit(main())

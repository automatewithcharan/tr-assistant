#!/usr/bin/env python3
"""Offline-only future rank labels for completed normalized recorder sessions."""
from __future__ import annotations
import argparse,sqlite3
from datetime import date,datetime
from pathlib import Path
from pkscreener.classes.NGP5EarlyWatchPublisher import IST
REQUIRED_COVERAGE=80.0

def label_session(root:Path,session_date:str,now:datetime|None=None)->int:
    now=now or datetime.now(IST)
    if date.fromisoformat(session_date)>=now.date():raise ValueError("labels may only be generated after the completed session date")
    source=root/session_date/"emerging_leader.sqlite";destination=root/session_date/"emerging_leader_labels.sqlite"
    read=sqlite3.connect(f"file:{source}?mode=ro",uri=True);write=sqlite3.connect(destination)
    write.execute("create table if not exists future_labels(session_id integer,snapshot_id integer,symbol_id integer,entered_top100_later integer,entered_top50_later integer,entered_top20_later integer,entered_top10_later integer,minutes_to_top100 real,minutes_to_top50 real,minutes_to_top20 real,minutes_to_top10 real,best_future_rank integer,eod_rank integer,eod_top100 integer,eod_top50 integer,eod_top20 integer,top100_persistence_minutes real,eod_coverage_pct real,eod_expected_count integer,eod_fresh_count integer,eod_label_timestamp_epoch_ms integer,eod_label_valid integer,forward_return_5m real,forward_return_10m real,forward_return_20m real,forward_return_30m real,forward_return_60m real,return_to_eod real,mfe_5m real,mfe_10m real,mfe_20m real,mfe_30m real,mfe_60m real,mae_5m real,mae_10m real,mae_20m real,mae_30m real,mae_60m real,primary key(snapshot_id,symbol_id))")
    sid=read.execute("select session_id from session_metadata where session_date=?",(session_date,)).fetchone()[0]
    # EOD is valid only in the explicit closing window and only under qualified coverage.
    eod=read.execute("select snapshot_id,timestamp_epoch_ms,fresh_count,expected_count,coverage_pct from snapshot_times where session_id=? and coverage_pct>=? and time(timestamp_epoch_ms/1000,'unixepoch','+5 hours','+30 minutes') between '15:28:00' and '15:30:59' order by timestamp_epoch_ms desc limit 1",(sid,REQUIRED_COVERAGE)).fetchone()
    rows=read.execute("select f.snapshot_id,f.symbol_id,s.timestamp_epoch_ms,f.broad_rank from features f join snapshot_times s on s.snapshot_id=f.snapshot_id where s.session_id=? and s.coverage_pct>=? order by f.symbol_id,s.timestamp_epoch_ms",(sid,REQUIRED_COVERAGE)).fetchall();by_symbol={}
    for row in rows:by_symbol.setdefault(row[1],[]).append(row)
    output=[]
    for symbol,series in by_symbol.items():
        for index,(snapshot,_,at,rank) in enumerate(series):
            later=series[index+1:]
            def hit(level):
                if rank is not None and rank<=level:return None,None
                value=next((x for x in later if x[3] is not None and x[3]<=level),None)
                return (1,(value[2]-at)/60000) if value else (None,None)
            h100,m100=hit(100);h50,m50=hit(50);h20,m20=hit(20);h10,m10=hit(10);future=[x[3] for x in later if x[3] is not None]
            eod_rank=next((x[3] for x in reversed(series) if eod and x[2]<=eod[1]),None) if eod else None
            # Real elapsed minutes between consecutive qualified snapshots inside Top100.
            persist=sum((series[i+1][2]-entry[2])/60000 for i,entry in enumerate(later) if i+1<len(later) and entry[3] is not None and entry[3]<=100)
            output.append((sid,snapshot,symbol,h100,h50,h20,h10,m100,m50,m20,m10,min(future) if future else None,eod_rank,int(eod_rank<=100) if eod_rank is not None else None,int(eod_rank<=50) if eod_rank is not None else None,int(eod_rank<=20) if eod_rank is not None else None,persist if later else None,eod[4] if eod else None,eod[3] if eod else None,eod[2] if eod else None,eod[1] if eod else None,int(eod is not None),*([None]*16)))
    with write:write.executemany("insert or replace into future_labels values("+','.join('?'*38)+")",output)
    read.close();write.close();return len(output)
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--session-date',required=True);a=p.parse_args();print({'labels':label_session(a.root,a.session_date)});return 0
if __name__=='__main__':raise SystemExit(main())

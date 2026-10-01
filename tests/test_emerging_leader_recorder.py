import json,sqlite3
from datetime import datetime,timedelta
from pkscreener.classes.EmergingLeaderRecorder import EmergingLeaderRecorder,IST,SCHEMA_VERSION
from scripts.label_emerging_leader_sessions import label_session

def setup(tmp_path,n=101):
    syms=['CAND']+[f'S{i:04d}' for i in range(n-1)]
    (tmp_path/'eq.csv').write_text('SYMBOL,SERIES\n'+'\n'.join(f'{s},EQ' for s in syms));(tmp_path/'tokens.csv').write_text('Symbol\n'+'\n'.join(syms));return syms
def ticks(syms,now,cand=101,stale=()):
    out={}
    for i,s in enumerate(syms):
        rank=cand if s=='CAND' else i+(i>=cand);pct=1000-rank;stamp=now-timedelta(minutes=3) if s in stale else now
        out[s]={'symbol':s,'exchange':'NSE','received_at':stamp.isoformat(),'last_trade_time':stamp.isoformat(),'last_price':100*(1+pct/100),'close':100,'volume_traded':1000+i,'open':100,'high':1200,'low':99,'best_bid':100,'best_ask':101,'average_traded_price':100}
    out['NIFTY']={'symbol':'NIFTY 50','exchange':'NSE','received_at':now.isoformat(),'last_trade_time':now.isoformat(),'last_price':101,'close':100};return out
def record(tmp_path,syms,moments,ranks,stale=()):
    current=[moments[0]];r=EmergingLeaderRecorder(tmp_path/'out',tmp_path/'eq.csv',tmp_path/'tokens.csv','sha',lambda:current[0])
    for now,rank in zip(moments,ranks):current[0]=now;(tmp_path/'ticks.json').write_text(json.dumps(ticks(syms,now,rank,stale)));r.process(tmp_path/'ticks.csv')
    return r
def db(tmp_path):return sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader.sqlite')

def test_normalized_fresh_only_schema_and_missingness(tmp_path):
    syms=setup(tmp_path);now=datetime(2026,9,30,10,tzinfo=IST);record(tmp_path,syms,[now],[101],{'S0000'});c=db(tmp_path)
    assert c.execute('select schema_version from session_metadata').fetchone()==(SCHEMA_VERSION,)
    assert c.execute('select count(*) from features').fetchone()==(100,)
    assert c.execute("select count(*) from features f join symbols y on y.symbol_id=f.symbol_id where y.symbol='S0000'").fetchone()==(0,)
    assert c.execute('select fresh_count,expected_count from snapshot_times').fetchone()==(100,101)

def test_trajectory_percentiles_cutoffs_rs_and_no_future_interpolation(tmp_path):
    syms=setup(tmp_path,900);start=datetime(2026,9,30,9,30,tzinfo=IST);record(tmp_path,syms,[start+timedelta(minutes=i) for i in range(7)],[850,800,750,700,650,600,550]);c=db(tmp_path)
    row=c.execute("select f.broad_rank,f.rank_velocity_1m,f.rank_acceleration,f.rs_vs_nifty,f.price_momentum_percentile,s.top100_cutoff_pct_change from features f join symbols y on y.symbol_id=f.symbol_id join snapshot_times s on s.snapshot_id=f.snapshot_id where y.symbol='CAND' order by s.timestamp_epoch_ms desc limit 1").fetchone()
    assert row[0]==550 and row[1]==50 and row[2] is not None and row[3] is not None and row[4] is not None and row[5] is not None

def test_no_fabricated_first_crossing_and_persisted_crossing_after_restart(tmp_path):
    syms=setup(tmp_path,900);start=datetime(2026,9,30,9,30,tzinfo=IST);record(tmp_path,syms,[start],[850]);c=db(tmp_path);assert c.execute('select count(*) from rank_events').fetchone()==(0,);c.close()
    record(tmp_path,syms,[start+timedelta(minutes=1)],[400]);c=db(tmp_path);events={x[0] for x in c.execute("select milestone from rank_events e join symbols y on y.symbol_id=e.symbol_id where y.symbol='CAND'")};assert {750,500,400}.issubset(events)

def test_disk_guard_pauses_then_resumes_without_watcher_dependency(tmp_path,monkeypatch):
    syms=setup(tmp_path);now=datetime(2026,9,30,10,tzinfo=IST);monkeypatch.setenv('NG_EMERGING_LEADER_MIN_FREE_GB','999');r=record(tmp_path,syms,[now],[101]);assert r.process(tmp_path/'ticks.csv')['status']=='research_paused_low_disk';monkeypatch.setenv('NG_EMERGING_LEADER_MIN_FREE_GB','0');monkeypatch.setenv('NG_EMERGING_LEADER_RESUME_FREE_GB','0');r.last_snapshot_at=None;assert r.process(tmp_path/'ticks.csv')['status']=='recorded'

def test_contract_has_no_trading_dependency():
    text=open('pkscreener/classes/EmergingLeaderRecorder.py').read().lower();assert 'signalmonitor' not in text and 'kiteordermanager' not in text

def test_eod_label_requires_coverage_qualified_closing_window(tmp_path):
    syms=setup(tmp_path);start=datetime(2026,9,30,15,29,tzinfo=IST);record(tmp_path,syms,[start,start+timedelta(minutes=1)],[101,90]);assert label_session(tmp_path/'out','2026-09-30',datetime(2026,10,1,tzinfo=IST))==202
    c=sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader_labels.sqlite');assert c.execute('select distinct eod_label_valid from future_labels').fetchall()==[(1,)]

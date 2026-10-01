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
    syms=setup(tmp_path);now=datetime(2026,9,30,10,tzinfo=IST);monkeypatch.setenv('NG_EMERGING_LEADER_MIN_FREE_GB','999');monkeypatch.setenv('NG_EMERGING_LEADER_RESUME_FREE_GB','999');r=EmergingLeaderRecorder(tmp_path/'out',tmp_path/'eq.csv',tmp_path/'tokens.csv','sha',lambda:now);(tmp_path/'ticks.json').write_text(json.dumps(ticks(syms,now,101)));assert r.process(tmp_path/'ticks.csv')['status']=='research_paused_low_disk';assert not (tmp_path/'out').exists();monkeypatch.setenv('NG_EMERGING_LEADER_MIN_FREE_GB','0');monkeypatch.setenv('NG_EMERGING_LEADER_RESUME_FREE_GB','0');assert r.process(tmp_path/'ticks.csv')['status']=='recorded'

def test_contract_has_no_trading_dependency():
    text=open('pkscreener/classes/EmergingLeaderRecorder.py').read().lower();assert 'signalmonitor' not in text and 'kiteordermanager' not in text

def test_eod_label_requires_coverage_qualified_closing_window(tmp_path):
    syms=setup(tmp_path);start=datetime(2026,9,30,15,29,tzinfo=IST);record(tmp_path,syms,[start,start+timedelta(minutes=1)],[101,90]);assert label_session(tmp_path/'out','2026-09-30',datetime(2026,10,1,tzinfo=IST))==202
    c=sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader_labels.sqlite');assert c.execute('select distinct eod_label_valid from future_labels').fetchall()==[(1,)]

def test_session_gate_has_exact_ist_boundaries_and_no_writes(tmp_path):
    syms=setup(tmp_path);current=[datetime(2026,9,30,8,30,tzinfo=IST)];r=EmergingLeaderRecorder(tmp_path/'out',tmp_path/'eq.csv',tmp_path/'tokens.csv','sha',lambda:current[0]);(tmp_path/'ticks.json').write_text(json.dumps(ticks(syms,current[0])))
    for moment,allowed in [(datetime(2026,9,30,8,30,tzinfo=IST),False),(datetime(2026,9,30,9,14,59,tzinfo=IST),False),(datetime(2026,9,30,9,15,tzinfo=IST),True),(datetime(2026,9,30,12,tzinfo=IST),True),(datetime(2026,9,30,15,30,tzinfo=IST),True),(datetime(2026,9,30,15,30,1,tzinfo=IST),False),(datetime(2026,9,30,17,tzinfo=IST),False)]:
        current[0]=moment;(tmp_path/'ticks.json').write_text(json.dumps(ticks(syms,moment)));status=r.process(tmp_path/'ticks.csv')['status'];assert (status!='outside_market_session') is allowed
    c=db(tmp_path);assert c.execute('select count(*) from snapshot_times').fetchone()[0]==3

def test_next_session_clears_rank_history_and_cannot_fake_crossing(tmp_path):
    syms=setup(tmp_path,900);current=[datetime(2026,9,30,15,30,tzinfo=IST)];r=EmergingLeaderRecorder(tmp_path/'out',tmp_path/'eq.csv',tmp_path/'tokens.csv','sha',lambda:current[0]);(tmp_path/'ticks.json').write_text(json.dumps(ticks(syms,current[0],850)));r.process(tmp_path/'ticks.csv');current[0]=datetime(2026,10,1,9,15,tzinfo=IST);(tmp_path/'ticks.json').write_text(json.dumps(ticks(syms,current[0],90)));r.process(tmp_path/'ticks.csv');c=sqlite3.connect(tmp_path/'out/2026-10-01/emerging_leader.sqlite');assert c.execute('select count(*) from rank_events').fetchone()==(0,)

def test_source_failure_is_not_success_cycle_and_is_persisted(tmp_path):
    syms=setup(tmp_path);now=datetime(2026,9,30,10,tzinfo=IST);r=EmergingLeaderRecorder(tmp_path/'out',tmp_path/'eq.csv',tmp_path/'tokens.csv','sha',lambda:now);result=r.process(tmp_path/'missing.csv');assert result['status']=='source_unavailable';c=db(tmp_path);assert c.execute('select cycle_count,source_failure_count from session_metadata').fetchone()==(0,1)

def test_label_uses_only_qualified_future_ranks(tmp_path):
    syms=setup(tmp_path,300);start=datetime(2026,9,30,10,tzinfo=IST);record(tmp_path,syms,[start,start+timedelta(minutes=1),start+timedelta(minutes=2)],[250,50,180]);c=db(tmp_path);symbol=c.execute("select symbol_id from symbols where symbol='CAND'").fetchone()[0];first=c.execute('select snapshot_id from snapshot_times order by snapshot_id limit 1').fetchone()[0];c.execute('update snapshot_times set coverage_pct=40 where timestamp_epoch_ms=?',(int((start+timedelta(minutes=1)).timestamp()*1000),));c.commit();c.close();label_session(tmp_path/'out','2026-09-30',datetime(2026,10,1,tzinfo=IST));c=sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader_labels.sqlite');assert c.execute('select entered_top100_later from future_labels where snapshot_id=? and symbol_id=?',(first,symbol)).fetchone()==(0,)

def test_label_eligibility_distinguishes_inside_top100(tmp_path):
    syms=setup(tmp_path,200);start=datetime(2026,9,30,10,tzinfo=IST);record(tmp_path,syms,[start,start+timedelta(minutes=1)],[90,80]);c=db(tmp_path);symbol=c.execute("select symbol_id from symbols where symbol='CAND'").fetchone()[0];c.close();label_session(tmp_path/'out','2026-09-30',datetime(2026,10,1,tzinfo=IST));c=sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader_labels.sqlite');row=c.execute('select eligible_top100_entry,entered_top100_later from future_labels where symbol_id=? order by snapshot_id limit 1',(symbol,)).fetchone();assert row==(0,None)

def test_label_missing_future_symbol_is_unknown_not_negative(tmp_path):
    syms=setup(tmp_path,200);start=datetime(2026,9,30,10,tzinfo=IST);record(tmp_path,syms,[start,start+timedelta(minutes=1)],[250,180]);c=db(tmp_path);snapshot=c.execute('select snapshot_id from snapshot_times order by snapshot_id desc limit 1').fetchone()[0];first=c.execute('select snapshot_id from snapshot_times order by snapshot_id limit 1').fetchone()[0];symbol=c.execute("select symbol_id from symbols where symbol='CAND'").fetchone()[0];c.execute('delete from features where snapshot_id=? and symbol_id=?',(snapshot,symbol));c.commit();c.close();label_session(tmp_path/'out','2026-09-30',datetime(2026,10,1,tzinfo=IST));c=sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader_labels.sqlite');assert c.execute('select entered_top100_later,future_label_valid from future_labels where snapshot_id=? and symbol_id=?',(first,symbol)).fetchone()==(None,0)

def test_eod_rank_requires_symbol_row_at_exact_eod_snapshot(tmp_path):
    syms=setup(tmp_path,101);start=datetime(2026,9,30,15,28,tzinfo=IST);record(tmp_path,syms,[start,start+timedelta(minutes=1)],[90,80]);c=db(tmp_path);eod=c.execute('select snapshot_id from snapshot_times order by timestamp_epoch_ms desc limit 1').fetchone()[0];symbol=c.execute("select symbol_id from symbols where symbol='CAND'").fetchone()[0];first=c.execute('select snapshot_id from snapshot_times order by snapshot_id limit 1').fetchone()[0];c.execute('delete from features where snapshot_id=? and symbol_id=?',(eod,symbol));c.commit();c.close();label_session(tmp_path/'out','2026-09-30',datetime(2026,10,1,tzinfo=IST));c=sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader_labels.sqlite');assert c.execute('select eod_rank,symbol_eod_rank_valid from future_labels where snapshot_id=? and symbol_id=?',(first,symbol)).fetchone()==(None,0)

def test_persistence_does_not_bridge_large_gaps(tmp_path):
    syms=setup(tmp_path,200);start=datetime(2026,9,30,10,tzinfo=IST);record(tmp_path,syms,[start,start+timedelta(minutes=1),start+timedelta(minutes=8)],[90,80,70]);c=db(tmp_path);symbol=c.execute("select symbol_id from symbols where symbol='CAND'").fetchone()[0];c.close();label_session(tmp_path/'out','2026-09-30',datetime(2026,10,1,tzinfo=IST));c=sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader_labels.sqlite');row=c.execute('select top100_persistence_observed_minutes,top100_persistence_max_gap_minutes from future_labels where symbol_id=? order by snapshot_id limit 1',(symbol,)).fetchone();assert row==(1.0,7.0)

def test_persistence_from_nonfirst_observation_never_uses_prior_interval(tmp_path):
    syms=setup(tmp_path,200);start=datetime(2026,9,30,10,tzinfo=IST);record(tmp_path,syms,[start,start+timedelta(minutes=1),start+timedelta(minutes=2)],[90,80,70]);c=db(tmp_path);symbol=c.execute("select symbol_id from symbols where symbol='CAND'").fetchone()[0];second=c.execute('select snapshot_id from snapshot_times order by snapshot_id limit 1 offset 1').fetchone()[0];c.close();label_session(tmp_path/'out','2026-09-30',datetime(2026,10,1,tzinfo=IST));c=sqlite3.connect(tmp_path/'out/2026-09-30/emerging_leader_labels.sqlite');assert c.execute('select top100_persistence_observed_minutes from future_labels where snapshot_id=? and symbol_id=?',(second,symbol)).fetchone()==(1.0,)

def test_manifest_exposes_objective_quality_fields(tmp_path):
    syms=setup(tmp_path);now=datetime(2026,9,30,10,tzinfo=IST);record(tmp_path,syms,[now],[101]);manifest=json.loads((tmp_path/'out/2026-09-30/manifest.json').read_text());assert {'completed_cycles','expected_cycles','cycle_completion_pct','snapshot_count','expected_snapshot_count','coverage_qualified_snapshot_count','coverage_qualified_snapshot_pct','coverage_min','coverage_median','coverage_max','near_close_eod_snapshot_valid','source_failure_count','database_row_counts'}<=set(manifest)

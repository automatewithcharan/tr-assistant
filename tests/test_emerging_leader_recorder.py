import json
import sqlite3
from datetime import datetime, timedelta
import scripts.ng_nse_top_gainers_watch as watcher

from pkscreener.classes.EmergingLeaderRecorder import EmergingLeaderRecorder, IST
from scripts.label_emerging_leader_sessions import label_session


def _write(path, text): path.write_text(text, encoding="utf-8")

def _setup(tmp_path, count=101):
    symbols = ["CAND"] + [f"S{i:04d}" for i in range(count - 1)]
    _write(tmp_path / "eq.csv", "SYMBOL,SERIES\n" + "\n".join(f"{s},EQ" for s in symbols) + "\n")
    _write(tmp_path / "tokens.csv", "Symbol\n" + "\n".join(symbols) + "\n")
    return symbols


def _ticks(symbols, now, candidate_rank=101, stale=()):
    data = {}
    for index, symbol in enumerate(symbols):
        rank = candidate_rank if symbol == "CAND" else index + (1 if index >= candidate_rank else 0)
        pct = 1000 - rank
        stamp = now - timedelta(minutes=3) if symbol in stale else now
        data[symbol] = {"symbol": symbol, "exchange": "NSE", "received_at": stamp.isoformat(),
                        "last_trade_time": stamp.isoformat(), "last_price": 100 * (1 + pct / 100), "close": 100,
                        "volume_traded": 1000 + index, "open": 100, "high": 1200, "low": 99,
                        "best_bid": 100, "best_ask": 101, "average_traded_price": 100}
    data["INDEX"] = {"symbol": "NIFTY 50", "exchange": "NSE", "received_at": now.isoformat(),
                     "last_trade_time": now.isoformat(), "last_price": 101, "close": 100}
    return data


def _record(tmp_path, symbols, moments, ranks):
    current = [moments[0]]
    recorder = EmergingLeaderRecorder(tmp_path / "out", tmp_path / "eq.csv", tmp_path / "tokens.csv", "testsha", lambda: current[0])
    for now, rank in zip(moments, ranks):
        current[0] = now
        (tmp_path / "ticks.json").write_text(json.dumps(_ticks(symbols, now, rank)), encoding="utf-8")
        recorder.process(tmp_path / "ticks.csv")
    return recorder


def test_causal_trajectory_and_threshold_crossings_before_top100(tmp_path):
    symbols = _setup(tmp_path, 900)
    start = datetime(2026, 9, 30, 9, 20, tzinfo=IST)
    _record(tmp_path, symbols, [start + timedelta(minutes=5*i) for i in range(5)], [850, 600, 400, 250, 90])
    db = sqlite3.connect(tmp_path / "out/2026-09-30/emerging_leader.sqlite")
    ranks = [r[0] for r in db.execute("SELECT broad_rank FROM snapshots WHERE symbol='CAND' ORDER BY timestamp")]
    events = {r[0] for r in db.execute("SELECT milestone FROM rank_events WHERE symbol='CAND'")}
    assert ranks == [850, 600, 400, 250, 90]
    assert {750, 500, 400, 300, 250, 200, 150, 125, 100}.issubset(events)


def test_only_fresh_symbols_define_percentiles_and_top100_cutoff(tmp_path):
    symbols = _setup(tmp_path, 101)
    now = datetime(2026, 9, 30, 10, tzinfo=IST); current = [now]
    recorder = EmergingLeaderRecorder(tmp_path / "out", tmp_path / "eq.csv", tmp_path / "tokens.csv", "testsha", lambda: current[0])
    payload = _ticks(symbols, now, 101, stale={"S0000"})
    (tmp_path / "ticks.json").write_text(json.dumps(payload), encoding="utf-8")
    recorder.process(tmp_path / "ticks.csv")
    db = sqlite3.connect(tmp_path / "out/2026-09-30/emerging_leader.sqlite")
    fresh, gap, pct = db.execute("SELECT fresh,pct_change_gap_to_top100,price_momentum_percentile FROM snapshots WHERE symbol='CAND'").fetchone()
    assert fresh == 1 and gap is not None and pct is not None
    assert db.execute("SELECT fresh,price_momentum_percentile FROM snapshots WHERE symbol='S0000'").fetchone() == (0, None)


def test_missing_history_is_null_not_interpolated_and_future_not_used(tmp_path):
    symbols = _setup(tmp_path, 101)
    start = datetime(2026, 9, 30, 9, 30, tzinfo=IST)
    _record(tmp_path, symbols, [start, start + timedelta(minutes=2)], [850, 400])
    db = sqlite3.connect(tmp_path / "out/2026-09-30/emerging_leader.sqlite")
    row = db.execute("SELECT rank_1m_ago,rank_improvement_1m,return_1m FROM snapshots WHERE symbol='CAND' ORDER BY timestamp DESC LIMIT 1").fetchone()
    assert row == (None, None, None)


def test_rank_velocity_and_acceleration_only_use_past_observations(tmp_path):
    symbols = _setup(tmp_path, 900)
    start = datetime(2026, 9, 30, 9, 30, tzinfo=IST)
    _record(tmp_path, symbols, [start + timedelta(minutes=i) for i in range(7)], [850, 800, 750, 700, 650, 600, 550])
    db = sqlite3.connect(tmp_path / "out/2026-09-30/emerging_leader.sqlite")
    velocity, acceleration = db.execute("SELECT rank_velocity_1m,rank_acceleration FROM snapshots WHERE symbol='CAND' ORDER BY timestamp DESC LIMIT 1").fetchone()
    assert velocity == 50 and acceleration is not None


def test_offline_labels_mark_invalid_eod_coverage_unknown(tmp_path):
    symbols = _setup(tmp_path, 10)
    start = datetime(2026, 9, 30, 9, 30, tzinfo=IST)
    _record(tmp_path, symbols, [start], [10])
    db = sqlite3.connect(tmp_path / "out/2026-09-30/emerging_leader.sqlite")
    db.execute("UPDATE market_context SET broad_coverage_pct=50"); db.commit(); db.close()
    assert label_session(tmp_path / "out", "2026-09-30", datetime(2026, 10, 1, tzinfo=IST)) == 10
    labels = sqlite3.connect(tmp_path / "out/2026-09-30/emerging_leader_labels.sqlite")
    assert labels.execute("SELECT eod_label_valid,eod_rank FROM future_labels WHERE symbol='CAND'").fetchone() == (0, None)


def test_current_top100_is_not_an_early_top100_entry_positive(tmp_path):
    symbols = _setup(tmp_path, 101)
    start = datetime(2026, 9, 30, 9, 30, tzinfo=IST)
    _record(tmp_path, symbols, [start, start + timedelta(minutes=1)], [90, 80])
    label_session(tmp_path / "out", "2026-09-30", datetime(2026, 10, 1, tzinfo=IST))
    labels = sqlite3.connect(tmp_path / "out/2026-09-30/emerging_leader_labels.sqlite")
    assert labels.execute("SELECT entered_top100_later FROM future_labels WHERE symbol='CAND' ORDER BY timestamp LIMIT 1").fetchone() == (None,)


def test_recorder_has_no_trading_or_future_label_dependency():
    source = ("pkscreener/classes/EmergingLeaderRecorder.py")
    text = open(source, encoding="utf-8").read().lower()
    assert "from pkscreener.classes.signalmonitor" not in text and "kiteordermanager" not in text and "future_labels" not in text


def test_disabled_feature_has_no_watcher_recorder_construction(tmp_path, monkeypatch):
    class Publisher:
        transport = None
        def __init__(self, *args, **kwargs): pass
        def process(self): return {"status": "up_to_date"}
    class Broad:
        def __init__(self, *args, **kwargs): pass
        def process(self): return {"status": "up_to_date"}
    class Ledger:
        def __init__(self, *args, **kwargs): pass
        def process(self, *args): return {"event_records": [], "status": "observed"}
    class Nifty:
        def __init__(self, *args, **kwargs): pass
        def process(self, *args): return {"status": "up_to_date"}
    class MustNotConstruct:
        def __init__(self, *args, **kwargs): raise AssertionError("disabled recorder constructed")
    monkeypatch.setattr(watcher, "NSETopGainersWatch", Publisher); monkeypatch.setattr(watcher, "NGTop100GainersWatch", Broad)
    monkeypatch.setattr(watcher, "MarketObservationLedger", Ledger); monkeypatch.setattr(watcher, "Nifty50WatchPublisher", Nifty)
    monkeypatch.setattr(watcher, "EmergingLeaderRecorder", MustNotConstruct); monkeypatch.setenv("NG_EMERGING_LEADER_RESEARCH_ENABLED", "0")
    assert watcher.main(["--once", "--state-root", str(tmp_path / "state"), "--ticks-path", str(tmp_path / "ticks.csv")]) == 0


def test_recorder_exception_fails_open_in_watcher(tmp_path, monkeypatch):
    class Publisher:
        transport = None
        def __init__(self, *args, **kwargs): pass
        def process(self): return {"status": "up_to_date"}
    class Broad:
        def __init__(self, *args, **kwargs): pass
        def process(self): return {"status": "up_to_date"}
    class Ledger:
        def __init__(self, *args, **kwargs): pass
        def process(self, *args): return {"event_records": [], "status": "observed"}
    class Nifty:
        def __init__(self, *args, **kwargs): pass
        def process(self, *args): return {"status": "up_to_date"}
    class BrokenRecorder:
        def __init__(self, *args, **kwargs): pass
        def process(self, *args): raise RuntimeError("research disk unavailable")
    monkeypatch.setattr(watcher, "NSETopGainersWatch", Publisher); monkeypatch.setattr(watcher, "NGTop100GainersWatch", Broad)
    monkeypatch.setattr(watcher, "MarketObservationLedger", Ledger); monkeypatch.setattr(watcher, "Nifty50WatchPublisher", Nifty)
    monkeypatch.setattr(watcher, "EmergingLeaderRecorder", BrokenRecorder); monkeypatch.setattr(watcher.subprocess, "check_output", lambda *args, **kwargs: "sha\n")
    monkeypatch.setenv("NG_EMERGING_LEADER_RESEARCH_ENABLED", "1")
    assert watcher.main(["--once", "--state-root", str(tmp_path / "state"), "--ticks-path", str(tmp_path / "ticks.csv")]) == 0

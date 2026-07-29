import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from scripts.run_full_rank_evidence_recorder import (
    StandaloneFullRankEvidenceWorker,
    _market_open,
    _tick_snapshot_age_seconds,
)


class FakeRecorder:
    interval_seconds = 15.0

    def __init__(self):
        self.calls = []

    def record(self, ticks, alerted_symbols=None, now=None):
        self.calls.append((ticks, alerted_symbols, now))
        return pd.DataFrame([{"Symbol": "TEST", "LastPrice": 100.0}])


class FakePaper:
    def __init__(self):
        self.calls = []

    def process(self, rows, ticks, now=None):
        self.calls.append((rows, ticks, now))
        return [{"event": "entry", "Symbol": "TEST"}]


class FakeObserver:
    def __init__(self, event):
        self.event = event
        self.calls = []

    def process(self, events, ticks, now=None):
        self.calls.append((events, ticks, now))
        return [{"event": self.event, "Symbol": "TEST"}]


class FakeFrozenRanker:
    def __init__(self):
        self.calls = []

    def process(self, rows, ticks, now=None):
        self.calls.append((rows, ticks, now))
        return [{"event": "paper_entry", "symbol": "TEST"}]

    def contract_entry_events(self, ticks=None):
        return [{"event": "entry", "EventId": "frozen:TEST"}]


def test_worker_advances_all_paper_observers_and_publishes_safe_status(tmp_path):
    recorder = FakeRecorder()
    paper = FakePaper()
    contracts = FakeObserver("contract_exit")
    timing = FakeObserver("timing_exit")
    worker = StandaloneFullRankEvidenceWorker(
        output_dir=tmp_path,
        universe="all",
        poll_seconds=5,
        recorder=recorder,
        paper=paper,
        contracts=contracts,
        timing=timing,
    )
    now = datetime(2026, 7, 27, 10, 0, 0)
    ticks = {"TEST": {"last_price": 100.0}}

    payload = worker.cycle(
        ticks,
        now=now,
        alerted_symbols={"TEST"},
    )

    assert len(recorder.calls) == 1
    assert len(paper.calls) == 1
    assert len(contracts.calls) == 1
    assert len(timing.calls) == 1
    assert contracts.calls[0][0] == [
        {"event": "entry", "Symbol": "TEST"}
    ]
    assert payload["owner"] == "standalone"
    assert payload["feature_rows"] == 1
    assert payload["paper_events"] == 1
    assert payload["poll_interval_seconds"] == 5.0
    assert payload["paper_only"] is True
    assert payload["alerts_allowed"] is False
    assert payload["real_orders_allowed"] is False
    stored = json.loads(worker.status_path.read_text(encoding="utf-8"))
    assert stored == payload


def test_worker_owns_frozen_ranker_when_scanner_recorder_is_standalone(
    tmp_path,
):
    frozen = FakeFrozenRanker()
    frozen_contracts = FakeObserver("contract_exit")
    worker = StandaloneFullRankEvidenceWorker(
        output_dir=tmp_path,
        recorder=FakeRecorder(),
        paper=FakePaper(),
        contracts=FakeObserver("contract_exit"),
        timing=FakeObserver("timing_exit"),
        frozen_ranker=frozen,
        frozen_contracts=frozen_contracts,
    )

    payload = worker.cycle(
        {"TEST": {"last_price": 100.0}},
        now=datetime(2026, 7, 27, 10, 0, 0),
    )

    assert len(frozen.calls) == 1
    assert frozen_contracts.calls[0][0] == [
        {"event": "entry", "EventId": "frozen:TEST"}
    ]
    assert payload["frozen_ranker_enabled"] is True
    assert payload["frozen_ranker_events"] == 1
    assert payload["frozen_prediction_entries"] == 1
    assert payload["frozen_contract_events"] == 1
    assert payload["real_orders_allowed"] is False


def test_worker_advances_stop_avoidance_paper_and_contract_ledgers(tmp_path):
    stop_ranker = FakeFrozenRanker()
    stop_contracts = FakeObserver("contract_exit")
    worker = StandaloneFullRankEvidenceWorker(
        output_dir=tmp_path,
        recorder=FakeRecorder(),
        paper=FakePaper(),
        contracts=FakeObserver("contract_exit"),
        timing=FakeObserver("timing_exit"),
        stop_avoidance_ranker=stop_ranker,
        stop_avoidance_contracts=stop_contracts,
    )

    payload = worker.cycle(
        {"TEST": {"last_price": 100.0}},
        now=datetime(2026, 7, 30, 10, 0, 0),
    )

    assert len(stop_ranker.calls) == 1
    assert stop_contracts.calls[0][0] == [
        {"event": "entry", "EventId": "frozen:TEST"}
    ]
    assert payload["stop_avoidance_enabled"] is True
    assert payload["stop_avoidance_events"] == 1
    assert payload["stop_avoidance_prediction_entries"] == 1
    assert payload["stop_avoidance_contract_events"] == 1
    assert payload["paper_only"] is True
    assert payload["alerts_allowed"] is False
    assert payload["real_orders_allowed"] is False


def test_stale_tick_status_does_not_call_any_observer(tmp_path):
    recorder = FakeRecorder()
    paper = FakePaper()
    contracts = FakeObserver("contract_exit")
    timing = FakeObserver("timing_exit")
    worker = StandaloneFullRankEvidenceWorker(
        output_dir=tmp_path,
        recorder=recorder,
        paper=paper,
        contracts=contracts,
        timing=timing,
    )

    worker.stale_tick_status(
        now=datetime(2026, 7, 27, 10, 0, 0),
        age_seconds=42.0,
    )

    assert recorder.calls == []
    assert paper.calls == []
    assert contracts.calls == []
    assert timing.calls == []
    payload = json.loads(worker.status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "waiting_for_fresh_tick_snapshot"
    assert payload["tick_snapshot_age_seconds"] == 42.0
    assert payload["real_orders_allowed"] is False


def test_tick_snapshot_age_uses_atomic_snapshot_mtime(tmp_path):
    path = tmp_path / "kiteticker_all_ticks_latest.json"
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (100.0, 100.0))

    assert _tick_snapshot_age_seconds(
        tmp_path,
        "all",
        now_ts=112.5,
    ) == 12.5
    assert _tick_snapshot_age_seconds(
        tmp_path,
        "missing",
        now_ts=112.5,
    ) == float("inf")


def test_missing_tick_snapshot_publishes_json_null_age(tmp_path):
    worker = StandaloneFullRankEvidenceWorker(
        output_dir=tmp_path,
        recorder=FakeRecorder(),
        paper=FakePaper(),
        contracts=FakeObserver("contract_exit"),
        timing=FakeObserver("timing_exit"),
    )

    worker.stale_tick_status(
        now=datetime(2026, 7, 27, 10, 0, 0),
        age_seconds=float("inf"),
    )

    payload = json.loads(worker.status_path.read_text(encoding="utf-8"))
    assert payload["tick_snapshot_age_seconds"] is None


def test_worker_market_window_is_weekday_intraday_only():
    assert _market_open(datetime(2026, 7, 27, 9, 15))
    assert _market_open(datetime(2026, 7, 27, 15, 30))
    assert not _market_open(datetime(2026, 7, 27, 9, 14))
    assert not _market_open(datetime(2026, 7, 26, 10, 0))


def test_signal_monitor_skips_duplicate_inline_snapshot_in_standalone_mode():
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.enable_live_feature_recorder = True
    runner.standalone_live_feature_recorder = True
    runner.use_live_kite = True

    class ExplodingRecorder:
        def record(self, *args, **kwargs):
            raise AssertionError("inline recorder must not run")

    runner._live_feature_recorder = ExplodingRecorder()
    runner._record_live_feature_snapshot(pd.DataFrame())


def test_worker_source_has_no_alert_or_broker_order_path():
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_full_rank_evidence_recorder.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "KiteOrderManager",
        "place_order",
        "auto_buy",
        "auto_sell",
        "_send_discord",
        "requests.post",
    ):
        assert forbidden not in source


def test_normal_launcher_enables_single_standalone_evidence_owner():
    launcher = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "start_live_assistant.sh"
    ).read_text(encoding="utf-8")

    assert (
        'SIGNALMONITOR_STANDALONE_EVIDENCE="${'
        'SIGNALMONITOR_STANDALONE_EVIDENCE:-1}"'
    ) in launcher
    assert (
        'SIGNALMONITOR_STANDALONE_EVIDENCE_POLL_SECONDS="${'
        'SIGNALMONITOR_STANDALONE_EVIDENCE_POLL_SECONDS:-5}"'
    ) in launcher

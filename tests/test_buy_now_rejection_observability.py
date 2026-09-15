from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.testing import assert_frame_equal

from pkscreener.classes.ActionableReport import ActionableReportRunner
from pkscreener.classes.BuyNowRejectionObservability import (
    ENV_FLAG,
    BuyNowRejectionObserver,
    build_selector_event,
    enabled_from_environment,
    load_events,
    summarize_by_date_symbol,
)
from pkscreener.classes.UniversePreFilter import UniversePreFilter
from pkscreener.classes.screening.signals import SignalStrength


IST = ZoneInfo("Asia/Kolkata")


class _SignalResult:
    signal = SignalStrength.BUY
    confidence = 30.0
    reasons = ["bullish alignment", "above average volume"]
    indicators = {"RSI": 55.0, "CCI": 80.0, "MFI": 60.0}


def test_buy_now_observability_requires_explicit_environment_opt_in():
    assert enabled_from_environment({}) is False
    assert enabled_from_environment({ENV_FLAG: "0"}) is False
    assert enabled_from_environment({ENV_FLAG: "1"}) is True


class _Signals:
    def analyze(self, data, incr_indicators=None):
        return _SignalResult()


class _Quality:
    def evaluate(self, closes):
        return {
            "quality_score": 65.0,
            "kalman_trend": "up",
            "vol_regime": "normal",
            "noise_spike": False,
        }


class _Garch:
    def adaptive_atr_multiplier(self, returns, base_multiplier=2.0):
        return base_multiplier


class _CaptureObserver:
    def __init__(self, *, mutate=False, fail=False):
        self.events = []
        self.mutate = mutate
        self.fail = fail

    def record(self, **event):
        if self.mutate:
            event["selector_inputs"]["confidence"] = -999
            event["thresholds"]["min_confidence"] = 999
        if self.fail:
            raise OSError("disk unavailable")
        self.events.append(event)
        return True


def _runner(observer=None) -> ActionableReportRunner:
    runner = ActionableReportRunner.__new__(ActionableReportRunner)
    runner.signals = _Signals()
    runner._signal_quality_filter = _Quality()
    runner._garch = _Garch()
    runner.min_confidence = 10.0
    runner.min_trade_value = 10_000_000.0
    runner.min_volume_ratio = 1.0
    runner.buy_ready_max_gap_pct = 0.60
    runner.strong_buy_max_gap_pct = 0.35
    runner.momentum_buy_min_score = 75.0
    runner.momentum_watch_min_score = 60.0
    runner.buy_now_min_volume_ratio = 1.3
    runner.early_momentum_buy_min_score = 90.0
    runner.early_momentum_buy_max_gap_pct = 1.10
    runner.early_momentum_buy_min_volume_ratio = 1.05
    runner.early_momentum_buy_max_change_pct = 7.0
    runner.early_momentum_buy_max_rsi = 75.0
    runner.stretched_momentum_buy_min_change_pct = 8.0
    runner.stretched_momentum_buy_min_rsi = 78.0
    runner.strong_buy_min_confidence = 22.5
    runner.strong_buy_min_volume_ratio = 1.35
    runner.strong_buy_min_trade_value = 15_000_000.0
    runner.strong_buy_only = False
    runner._pre_filter = UniversePreFilter(
        min_price=10.0,
        max_price=50_000.0,
        min_avg_volume=50_000,
        min_avg_trade_value=5_000_000.0,
        min_candles=60,
    )
    runner._buy_now_rejection_observer = observer
    return runner


def _data(last_close=102.0) -> pd.DataFrame:
    rows = 60
    closes = [100.0 + index * 0.02 for index in range(rows - 1)] + [last_close]
    return pd.DataFrame(
        {
            "open": [value - 0.2 for value in closes],
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "close": closes,
            "volume": [200_000.0] * rows,
        }
    )


def _analyze(runner, symbol="AAA", last_close=102.0):
    data = _data(last_close)
    return runner.analyze_prepared_data(
        symbol,
        data,
        run_at_text="2026-09-14 10:00:00",
        prefilter_inputs=runner._buy_now_prefilter_inputs(data),
    )


def test_selector_output_is_identical_with_observability_disabled_or_enabled():
    disabled = _analyze(_runner(None))
    capture = _CaptureObserver()
    enabled = _analyze(_runner(capture))

    assert enabled == disabled
    assert len(capture.events) == 1
    assert capture.events[0]["final_action"] == disabled["NowAction"]


def test_observer_cannot_mutate_candidate_state_or_selector_inputs():
    baseline = _analyze(_runner(None))
    mutated = _analyze(_runner(_CaptureObserver(mutate=True)))
    assert mutated == baseline
    assert mutated["ConfidencePct"] == 30.0


def test_observer_receives_a_deeply_detached_snapshot(tmp_path):
    observer = BuyNowRejectionObserver(tmp_path)
    runner = _runner(observer)
    snapshot = {
        "symbol": "AAA",
        "source": "actionable_report",
        "event_timestamp": datetime(2026, 9, 14, 10, 0, tzinfo=IST),
        "selector_inputs": {"nested": {"value": 1}},
        "thresholds": {"nested": {"value": 2}},
        "final_action": "Watch for Buy",
    }

    runner._record_buy_now_shadow(**snapshot)
    snapshot["selector_inputs"]["nested"]["value"] = 999
    snapshot["thresholds"]["nested"]["value"] = 999
    assert observer.close(timeout=1.0)
    path = tmp_path / "buy_now_rejection_observability" / "2026-09-14.jsonl"
    event = json.loads(path.read_text())
    assert event["inputs"]["nested"]["value"] == 1
    assert event["thresholds"]["nested"]["value"] == 2

    # Later caller mutation cannot change the retained event.
    assert snapshot["selector_inputs"]["nested"]["value"] == 999
    assert snapshot["thresholds"]["nested"]["value"] == 999


def test_observability_does_not_affect_report_ranking():
    disabled_runner = _runner(None)
    enabled_runner = _runner(_CaptureObserver(mutate=True))
    disabled_rows = [
        _analyze(disabled_runner, "AAA", 102.0),
        _analyze(disabled_runner, "BBB", 103.0),
    ]
    enabled_rows = [
        _analyze(enabled_runner, "AAA", 102.0),
        _analyze(enabled_runner, "BBB", 103.0),
    ]

    disabled_full, disabled_actionable = disabled_runner._build_from_rows(disabled_rows)
    enabled_full, enabled_actionable = enabled_runner._build_from_rows(enabled_rows)
    assert_frame_equal(enabled_full, disabled_full)
    assert_frame_equal(enabled_actionable, disabled_actionable)


def test_logging_failure_is_fail_open_for_selector_behavior():
    baseline = _analyze(_runner(None))
    with_failure = _analyze(_runner(_CaptureObserver(fail=True)))
    assert with_failure == baseline


def test_cached_evaluation_uses_current_evaluation_time_and_retains_source_time():
    capture = _CaptureObserver()
    runner = _runner(capture)
    row = _analyze(_runner(None))
    evaluated_at = datetime(2026, 9, 14, 10, 5, tzinfo=IST)

    runner._record_cached_buy_now_shadow(
        "AAA",
        row,
        runner._buy_now_prefilter_inputs(_data()),
        evaluated_at,
    )

    assert capture.events[0]["event_timestamp"] == evaluated_at
    assert capture.events[0]["selector_inputs"]["cached_result_run_at"] == row["RunAt"]


def test_missing_gate_value_is_unknown_and_never_fabricated():
    event = build_selector_event(
        symbol="AAA",
        source="actionable_report",
        event_timestamp=datetime(2026, 9, 14, 10, 0, tzinfo=IST),
        selector_inputs={
            "adjusted_action": "BUY",
            "volume_ratio": None,
            "volume_ok": False,
            "kalman_quality": 60.0,
            "kalman_trend": "up",
            "bearish_trend": False,
            "stretched_chase": False,
            "kalman_safety_ok": True,
            "volatility_regime_ok": True,
        },
        thresholds={"min_volume_ratio": 1.0},
        final_action="Watch for Buy",
        base_action="Watch for Buy",
    )
    gate = event["gates"]["actionable_relative_volume"]
    assert gate["evaluated"] is True
    assert gate["status"] == "unknown"
    assert gate["observed_value"] is None


def test_repeated_evaluations_are_distinct_and_group_by_date_symbol(tmp_path):
    observer = BuyNowRejectionObserver(tmp_path)
    common = {
        "symbol": "AAA",
        "source": "actionable_report",
        "selector_inputs": {"adjusted_action": "HOLD"},
        "thresholds": {},
        "final_action": "Hold / Do Nothing",
    }
    assert observer.record(
        **common,
        event_timestamp=datetime(2026, 9, 14, 10, 0, tzinfo=IST),
    )
    assert observer.record(
        **common,
        event_timestamp=datetime(2026, 9, 14, 10, 5, tzinfo=IST),
    )
    assert observer.record(
        **common,
        event_timestamp=datetime(2026, 9, 13, 10, 0, tzinfo=IST),
    )
    assert observer.close(timeout=1.0)

    paths = sorted((tmp_path / "buy_now_rejection_observability").glob("*.jsonl"))
    events = load_events(paths)
    assert len(events) == 3
    assert len({event["event_id"] for event in events}) == 3
    sep14 = [event for event in events if event["trading_date"] == "2026-09-14"]
    sep13 = [event for event in events if event["trading_date"] == "2026-09-13"]
    assert sep14[0]["candidate_id"] == sep14[1]["candidate_id"]
    assert sep14[0]["candidate_id"] != sep13[0]["candidate_id"]

    groups = summarize_by_date_symbol(events)
    assert [(row["trading_date"], row["symbol"], row["evaluation_count"]) for row in groups] == [
        ("2026-09-13", "AAA", 1),
        ("2026-09-14", "AAA", 2),
    ]


def test_prefilter_failure_records_the_actual_observed_value():
    runner = _runner(None)
    data = _data()
    data.loc[:, "volume"] = 10.0
    prefilter = runner._buy_now_prefilter_inputs(data)
    event = build_selector_event(
        symbol="LOWVOL",
        source="actionable_report",
        event_timestamp=datetime(2026, 9, 14, 10, 0, tzinfo=IST),
        selector_inputs={},
        thresholds=runner._buy_now_shadow_thresholds(),
        final_action=None,
        prefilter_inputs=prefilter,
        decision_override="REJECTED",
        primary_rejection_reason="prefilter:avg volume too low",
    )
    gate = event["gates"]["prefilter_average_volume"]
    assert gate["status"] == "fail"
    assert gate["observed_value"] == 10.0
    assert gate["threshold"]["value"] == 50_000.0


def test_journal_schema_contains_decision_inputs_gates_and_stable_identity(tmp_path):
    observer = BuyNowRejectionObserver(tmp_path)
    assert observer.record(
        symbol="AAA",
        source="actionable_report",
        event_timestamp=datetime(2026, 9, 14, 10, 0, tzinfo=IST),
        selector_inputs={"adjusted_action": "BUY"},
        thresholds={},
        final_action="Buy Now",
    )
    assert observer.close(timeout=1.0)
    path = tmp_path / "buy_now_rejection_observability" / "2026-09-14.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["final_decision"] == "QUALIFIED"
    assert row["selector_version"] == "actionable-report-buy-now-v1"
    assert row["ranking_is_selector_gate"] is False
    assert isinstance(row["inputs"], dict)
    assert isinstance(row["gates"], dict)
    assert len(row["candidate_id"]) == 64


def _observer_snapshot(symbol="AAA"):
    return {
        "symbol": symbol,
        "source": "actionable_report",
        "event_timestamp": datetime(2026, 9, 14, 10, 0, tzinfo=IST),
        "selector_inputs": {"adjusted_action": "BUY", "nested": {"value": 1}},
        "thresholds": {"nested": {"value": 2}},
        "final_action": "Buy Now",
    }


def test_disk_stall_never_stalls_selector_submission(tmp_path, monkeypatch):
    observer = BuyNowRejectionObserver(tmp_path, buffer_size=8)
    started = threading.Event()
    release = threading.Event()

    def stalled_write(_path, _payload):
        started.set()
        release.wait(1.0)

    monkeypatch.setattr(observer, "_append_payload", stalled_write)
    assert observer.record(**_observer_snapshot("STALLING"))
    assert started.wait(1.0)

    runner = _runner(observer)
    began = time.perf_counter()
    result = _analyze(runner, "NONBLOCKING")
    elapsed = time.perf_counter() - began
    assert result is not None
    assert elapsed < 0.4

    release.set()
    assert observer.close(timeout=1.0)


def test_full_buffer_drops_without_blocking_and_counts_drop(tmp_path, monkeypatch):
    observer = BuyNowRejectionObserver(tmp_path, buffer_size=1, batch_size=1)
    started = threading.Event()
    release = threading.Event()

    def stalled_write(_path, _payload):
        started.set()
        release.wait(1.0)

    monkeypatch.setattr(observer, "_append_payload", stalled_write)
    assert observer.record(**_observer_snapshot("FIRST"))
    assert started.wait(1.0)
    assert observer.record(**_observer_snapshot("QUEUED"))
    began = time.perf_counter()
    assert observer.record(**_observer_snapshot("DROPPED")) is False
    assert time.perf_counter() - began < 0.05
    assert observer.health()["dropped_buffer_full"] == 1

    release.set()
    assert observer.close(timeout=1.0)


def test_writer_failure_is_counted_and_never_propagates(tmp_path, monkeypatch):
    observer = BuyNowRejectionObserver(tmp_path)

    def fail_write(_path, _payload):
        raise OSError("disk unavailable")

    monkeypatch.setattr(observer, "_append_payload", fail_write)
    assert observer.record(**_observer_snapshot())
    assert observer.close(timeout=1.0)
    health = observer.health()
    assert health["write_errors"] == 1
    assert health["last_error_type"] == "OSError"


def test_retained_async_events_are_ordered_valid_jsonl(tmp_path):
    observer = BuyNowRejectionObserver(tmp_path, batch_size=8)
    for minute in range(3):
        snapshot = _observer_snapshot("ORDERED")
        snapshot["event_timestamp"] = datetime(
            2026, 9, 14, 10, minute, tzinfo=IST
        )
        assert observer.record(**snapshot)
    assert observer.close(timeout=1.0)

    path = tmp_path / "buy_now_rejection_observability" / "2026-09-14.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["timestamp_ist"] for row in rows] == [
        "2026-09-14T10:00:00.000000+05:30",
        "2026-09-14T10:01:00.000000+05:30",
        "2026-09-14T10:02:00.000000+05:30",
    ]
    assert len({row["event_id"] for row in rows}) == 3
    assert len({row["candidate_id"] for row in rows}) == 1
    assert observer.health()["written"] == 3


def test_shutdown_flush_is_bounded_when_disk_is_stalled(tmp_path, monkeypatch):
    observer = BuyNowRejectionObserver(
        tmp_path, buffer_size=2, flush_timeout_seconds=0.02
    )
    started = threading.Event()
    release = threading.Event()

    def stalled_write(_path, _payload):
        started.set()
        release.wait(1.0)

    monkeypatch.setattr(observer, "_append_payload", stalled_write)
    assert observer.record(**_observer_snapshot())
    assert started.wait(1.0)
    began = time.perf_counter()
    assert observer.close() is False
    assert time.perf_counter() - began < 0.2
    assert observer.health()["flush_timeouts"] == 1

    release.set()
    assert observer.close(timeout=1.0)


def test_offline_summary_measures_gate_failure_until_first_later_pass():
    timestamps = [
        datetime(2026, 9, 14, 10, 0, tzinfo=IST),
        datetime(2026, 9, 14, 10, 5, tzinfo=IST),
        datetime(2026, 9, 14, 10, 10, tzinfo=IST),
    ]
    events = []
    for index, timestamp in enumerate(timestamps):
        passed = index == 2
        events.append(
            build_selector_event(
                symbol="AAA",
                source="actionable_report",
                event_timestamp=timestamp,
                selector_inputs={
                    "adjusted_action": "BUY",
                    "bearish_trend": False,
                    "stretched_chase": False,
                    "kalman_safety_ok": True,
                    "volatility_regime_ok": True,
                    "confidence": 30.0,
                    "confidence_ok": True,
                    "entry_gap_pct": 0.2,
                    "near_trigger": True,
                    "avg_trade_value_20": 20_000_000.0,
                    "liquidity_ok": True,
                    "volume_ratio": 1.5 if passed else 0.8,
                    "volume_ok": passed,
                    "kalman_quality": 60.0,
                    "actionable_kalman_ok": True,
                },
                thresholds={
                    "min_confidence": 10.0,
                    "buy_ready_max_gap_pct": 0.6,
                    "min_trade_value": 10_000_000.0,
                    "min_volume_ratio": 1.0,
                    "actionable_min_kalman_quality": 35.0,
                },
                final_action="Buy Now" if passed else "Watch for Buy",
                base_action="Watch for Buy",
            )
        )

    summary = summarize_by_date_symbol(events)[0]
    failure = summary["gate_failures"]["actionable_relative_volume"]
    assert failure["failed_evaluations"] == 2
    assert failure["first_passed_after_failure_at"] == timestamps[2].isoformat(
        timespec="microseconds"
    )
    assert failure["minutes_until_pass"] == 10.0
    assert failure["unresolved"] is False

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import pkscreener.classes.AlertTimingObservability as telemetry_module
import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.AlertTimingObservability import (
    AlertTimingObserver,
    deliver_with_observability,
)
from pkscreener.classes.SignalMonitor import SignalMonitorRunner


NOW = datetime(2026, 9, 9, 9, 31, 12, 123456, tzinfo=ZoneInfo("Asia/Kolkata"))


def _observer(tmp_path):
    return AlertTimingObserver(str(tmp_path), clock=lambda: NOW)


def _records(tmp_path):
    path = Path(tmp_path) / "alert_timing_observability" / "2026-09-09.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _event(source="anomaly", symbol="ABC", **context):
    return {
        "event_type": "SOURCE_QUALIFIED",
        "source": source,
        "symbol": symbol,
        "event_timestamp": NOW,
        "trading_date": "2026-09-09",
        "observation_id": "obs-1",
        "context": context,
    }


def test_schema_partition_timezone_and_missing_values_are_causal(tmp_path):
    observer = _observer(tmp_path)

    assert observer.emit(
        "SOURCE_QUALIFIED",
        "abc",
        "anomaly",
        observation_id="obs-1",
        context={"price": 101.5, "rsi": None},
    )

    row = _records(tmp_path)[0]
    assert row["schema_version"] == 1
    assert row["trading_date"] == "2026-09-09"
    assert row["event_timestamp"].endswith("+05:30")
    assert row["symbol"] == "ABC"
    assert row["price"] == 101.5
    assert row["rsi"] is None
    assert "session_high" not in row


def test_naive_runtime_timestamp_is_documented_as_ist(tmp_path):
    observer = _observer(tmp_path)
    observer.emit(
        "SOURCE_EVALUATED",
        "ABC",
        "early_breakout",
        event_timestamp=datetime(2026, 9, 9, 10, 0),
    )
    assert _records(tmp_path)[0]["event_timestamp"].endswith("+05:30")


def test_unknown_fields_and_secrets_never_enter_journal(tmp_path):
    observer = _observer(tmp_path)
    observer.emit(
        "DISCORD_FAILED",
        "ABC",
        "anomaly",
        context={
            "failure_category": "configuration",
            "webhook_url": "https://discord.com/api/webhooks/123/SECRET",
            "token": "SECRET",
            "raw_error": "secret details",
            "price": 100.0,
        },
    )
    encoded = json.dumps(_records(tmp_path)[0]).lower()
    assert "discord.com/api/webhooks" not in encoded
    assert "secret" not in encoded
    assert "webhook_url" not in encoded
    assert "token" not in encoded
    assert "raw_error" not in encoded


def test_attempt_precedes_send_and_success_follows_confirmed_true(tmp_path):
    observer = _observer(tmp_path)

    def send(_message):
        assert [row["event_type"] for row in _records(tmp_path)] == ["DISCORD_ATTEMPTED"]
        return True

    result = deliver_with_observability(send, observer, "payload", [_event()])

    assert result is True
    assert [row["event_type"] for row in _records(tmp_path)] == [
        "DISCORD_ATTEMPTED",
        "DISCORD_SUCCESS",
    ]


def test_failed_send_is_failed_and_never_success(tmp_path):
    result = deliver_with_observability(
        lambda _message: False,
        _observer(tmp_path),
        "payload",
        [_event(source="pre_move")],
    )
    assert result is False
    rows = _records(tmp_path)
    assert [row["event_type"] for row in rows] == [
        "DISCORD_ATTEMPTED",
        "DISCORD_FAILED",
    ]
    assert rows[-1]["failure_category"] == "send_returned_false"


def test_unconfirmed_none_result_is_not_fabricated_as_success(tmp_path):
    assert deliver_with_observability(
        lambda _message: None,
        _observer(tmp_path),
        "payload",
        [_event()],
    ) is None
    assert [row["event_type"] for row in _records(tmp_path)] == ["DISCORD_ATTEMPTED"]


def test_send_exception_is_observed_then_preserved(tmp_path):
    def fail(_message):
        raise RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        deliver_with_observability(fail, _observer(tmp_path), "payload", [_event()])
    assert [row["event_type"] for row in _records(tmp_path)] == [
        "DISCORD_ATTEMPTED",
        "DISCORD_FAILED",
    ]


def test_direct_candidate_http_success_is_recorded_after_post(tmp_path, monkeypatch):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    runner._alert_timing_observer = _observer(tmp_path)
    runner.tradingview_candidate_max_total_attempts = 1
    runner.tradingview_candidate_retry_ttl_seconds = 600
    runner.tradingview_discord_max_attempts = 1
    runner.tradingview_discord_retry_backoff_seconds = 0
    runner._tradingview_delivery_now = lambda: NOW
    runner._resolve_discord_webhook_url = lambda: "https://discord.invalid/webhook"

    def post(*_args, **_kwargs):
        assert [row["event_type"] for row in _records(tmp_path)] == ["DISCORD_ATTEMPTED"]
        return SimpleNamespace(status_code=204, text="", headers={})

    monkeypatch.setattr(signal_monitor_module.requests, "post", post)
    runner._deliver_tradingview_candidate(
        {
            "trading_date": "2026-09-09",
            "symbol": "ABC",
            "source_lane": "early_breakout",
            "detected_at": "2026-09-09T09:31:00+05:30",
            "lifecycle_observation_id": "obs-direct",
        },
        "candidate",
        telemetry_context={"day_change_pct": 1.4},
    )

    rows = _records(tmp_path)
    assert [row["event_type"] for row in rows] == [
        "DISCORD_ATTEMPTED",
        "DISCORD_SUCCESS",
    ]
    assert rows[-1]["http_status"] == 204
    assert rows[-1]["day_change_pct"] == 1.4


def test_telemetry_write_failure_does_not_stop_delivery(tmp_path, monkeypatch):
    observer = _observer(tmp_path)
    monkeypatch.setattr(telemetry_module.os, "open", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    calls = []

    result = deliver_with_observability(
        lambda message: calls.append(message) or True,
        observer,
        "payload",
        [_event()],
    )

    assert result is True
    assert calls == ["payload"]


def test_telemetry_failure_does_not_change_qualification_result(tmp_path, monkeypatch):
    observer = _observer(tmp_path)
    monkeypatch.setattr(telemetry_module.os, "open", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    qualified = {"accepted": True, "reason": "existing qualification passed"}

    assert observer.emit(
        "SOURCE_QUALIFIED", "ABC", "early_breakout", context={"price": 100.0}
    ) is False
    assert qualified == {"accepted": True, "reason": "existing qualification passed"}


def test_rejection_preserves_existing_reason(tmp_path):
    observer = _observer(tmp_path)
    reason = "volume below radar floor"
    observer.emit(
        "SOURCE_REJECTED",
        "ABC",
        "top_gainer_radar",
        context={"rejection_reason": reason, "volume_ratio": 0.7},
    )
    row = _records(tmp_path)[0]
    assert row["event_type"] == "SOURCE_REJECTED"
    assert row["rejection_reason"] == reason
    assert row["volume_ratio"] == 0.7


def test_multiple_sources_for_same_symbol_are_independent(tmp_path):
    observer = _observer(tmp_path)
    sources = [
        "anomaly",
        "pre_move",
        "early_breakout",
        "top_gainer_radar",
        "buy_now_board",
    ]
    observer.emit_many(_event(source=source) for source in sources)
    assert [row["source"] for row in _records(tmp_path)] == sources


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"NowAction": "🔥 Grade-A Runner"}, "anomaly"),
        ({"NowAction": "👀 Pre-Move Watch"}, "pre_move"),
        ({"Lane": "early_breakout"}, "early_breakout"),
        ({"Lane": "top_gainer_radar"}, "top_gainer_radar"),
        ({"NowAction": "Buy Now"}, "buy_now_board"),
    ],
)
def test_alert_families_map_without_cross_source_suppression(row, expected):
    assert SignalMonitorRunner._alert_timing_source_for_row(row) == expected


def test_restart_construction_does_not_create_events(tmp_path):
    AlertTimingObserver(str(tmp_path), clock=lambda: NOW)
    assert not (Path(tmp_path) / "alert_timing_observability").exists()


def test_journal_namespace_is_separate_from_experiments(tmp_path):
    _observer(tmp_path).emit("SOURCE_QUALIFIED", "ABC", "buy_now_board")
    paths = [str(path.relative_to(tmp_path)) for path in Path(tmp_path).rglob("*") if path.is_file()]
    assert paths == ["alert_timing_observability/2026-09-09.jsonl"]
    assert all("lifecycle" not in path and "confirmation" not in path for path in paths)


def test_timing_helper_initialization_failure_is_fail_open(tmp_path, monkeypatch):
    class BrokenObserver:
        def __init__(self, _output_dir):
            raise OSError("telemetry unavailable")

    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    monkeypatch.setattr(signal_monitor_module, "AlertTimingObserver", BrokenObserver)

    runner._initialize_runtime_helpers()

    assert runner._alert_timing_observer is None
    assert runner._tradingview_delivery_available is True
    assert runner._manual_discord_guard_required is True

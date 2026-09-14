import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from pkscreener.classes.TickFreshnessDiagnostics import TickFreshnessDiagnostics


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_diagnostics_disabled_by_default_and_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv(TickFreshnessDiagnostics.ENABLE_ENV, raising=False)
    destination = tmp_path / "diagnostics.jsonl"
    recorder = TickFreshnessDiagnostics(tmp_path, path=destination)
    recorder.record(
        evaluation_timestamp=datetime.now(timezone.utc),
        cycle_timestamp=datetime.now(timezone.utc),
        lane="signal_monitor",
        symbol="ABC",
        tick={},
        snapshot_context={},
        live_price_used=100,
        live_tick_applied=False,
        report_cache_fallback=True,
        candidate_result="Buy Now",
    )
    assert recorder.enabled is False
    assert not destination.exists()


def test_enabled_records_all_fields_and_normalizes_aware_timestamps(tmp_path):
    destination = tmp_path / "diagnostics.jsonl"
    recorder = TickFreshnessDiagnostics(tmp_path, enabled=True, path=destination)
    recorder.record(
        evaluation_timestamp="2026-08-17T09:15:03+05:30",
        cycle_timestamp="2026-08-17T09:15:03+05:30",
        lane="early_breakout",
        symbol="abc",
        tick={
            "received_at": "2026-08-17T03:45:02+00:00",
            "exchange_timestamp": "2026-08-17T09:15:02+05:30",
            "last_trade_time": "2026-08-17T09:15:01+05:30",
        },
        snapshot_context={
            "snapshot_file_mtime": "2026-08-17T03:45:02Z",
            "ticker_status": {"connected": True},
        },
        live_price_used=101.5,
        live_tick_applied=True,
        report_cache_fallback=False,
        candidate_result="Buy Now",
        lifecycle_observation_id="obs-1",
    )
    row = _records(destination)[0]
    assert row["schema_version"] == "signalmonitor_tick_freshness.v1"
    assert row["lane"] == "early_breakout"
    assert row["symbol"] == "ABC"
    assert row["received_at"]["raw"] == "2026-08-17T03:45:02+00:00"
    assert row["received_at"]["normalized_utc"] == "2026-08-17T03:45:02.000000Z"
    assert row["exchange_timestamp"]["normalized_utc"] == "2026-08-17T03:45:02.000000Z"
    received_at = datetime.fromisoformat(
        row["received_at"]["normalized_utc"].replace("Z", "+00:00")
    )
    cycle_at = datetime.fromisoformat(
        row["cycle_timestamp"]["normalized_utc"].replace("Z", "+00:00")
    )
    assert (cycle_at - received_at).total_seconds() == 1
    assert row["live_tick_applied"] is True
    assert row["report_cache_fallback"] is False
    assert row["candidate_qualified"] is True
    assert row["lifecycle_observation_id"] == "obs-1"


def test_all_lanes_append_and_bad_timestamps_remain_explicit(tmp_path):
    destination = tmp_path / "diagnostics.jsonl"
    recorder = TickFreshnessDiagnostics(tmp_path, enabled=True, path=destination)
    for lane in ("signal_monitor", "early_breakout", "top_gainer_radar"):
        recorder.record(
            evaluation_timestamp="2026-08-17 09:15:00",
            cycle_timestamp="2026-08-17 09:15:00",
            lane=lane,
            symbol="ABC",
            tick={"received_at": "not-a-time", "exchange_timestamp": "", "last_trade_time": None},
            snapshot_context={},
            live_price_used=100,
            live_tick_applied=False,
            report_cache_fallback=True,
            candidate_result="Watch",
        )
    rows = _records(destination)
    assert [row["lane"] for row in rows] == ["signal_monitor", "early_breakout", "top_gainer_radar"]
    assert all(row["received_at"]["parse_status"] == "malformed" for row in rows)
    assert all(row["received_at"]["normalized_utc"] is None for row in rows)
    assert all(row["exchange_timestamp"]["missing"] is True for row in rows)
    assert all(row["last_trade_time"]["missing"] is True for row in rows)
    assert all(row["evaluation_timestamp"]["parse_status"] == "naive_timezone_ambiguous" for row in rows)


def test_missing_received_at_is_preserved_as_missing_not_rewritten(tmp_path):
    destination = tmp_path / "diagnostics.jsonl"
    recorder = TickFreshnessDiagnostics(tmp_path, enabled=True, path=destination)
    recorder.record(
        evaluation_timestamp="2026-08-17T03:45:00Z",
        cycle_timestamp="2026-08-17T03:45:00Z",
        lane="top_gainer_radar",
        symbol="ABC",
        tick={"exchange_timestamp": "2026-08-17T09:15:00+05:30"},
        snapshot_context={},
        live_price_used=100,
        live_tick_applied=True,
        report_cache_fallback=False,
        candidate_result="Top Gainer Radar",
    )
    received_at = _records(destination)[0]["received_at"]
    assert received_at == {
        "missing": True,
        "normalized_utc": None,
        "parse_status": "missing",
        "parsed_successfully": False,
        "raw": "",
    }


def test_repeated_records_append_instead_of_overwrite(tmp_path):
    destination = tmp_path / "diagnostics.jsonl"
    recorder = TickFreshnessDiagnostics(tmp_path, enabled=True, path=destination)
    common = dict(
        cycle_timestamp="2026-08-17T03:45:00Z",
        lane="signal_monitor",
        symbol="ABC",
        tick={},
        snapshot_context={},
        live_price_used=100,
        live_tick_applied=False,
        report_cache_fallback=True,
        candidate_result="Buy Now",
    )
    recorder.record(evaluation_timestamp="2026-08-17T03:45:01Z", **common)
    recorder.record(evaluation_timestamp="2026-08-17T03:45:02Z", **common)
    assert len(_records(destination)) == 2


def test_write_failure_is_contained_and_candidate_frame_is_unchanged(tmp_path):
    destination = tmp_path / "not-a-file"
    destination.mkdir()
    recorder = TickFreshnessDiagnostics(tmp_path, enabled=True, path=destination)
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._tick_freshness_diagnostics = recorder
    runner._diagnostic_live_tick_applied_symbols = {"ABC"}
    runner.output_dir = tmp_path
    runner.universe = "all"
    runner.run_at = datetime(2026, 8, 17, 9, 15, tzinfo=timezone.utc)
    runner.actionable_runner = SimpleNamespace(
        live_ticks={"ABC": {"last_price": 100, "received_at": "bad"}}
    )
    candidates = pd.DataFrame(
        [{"When": "2026-08-17T09:15:00Z", "Symbol": "ABC", "NowAction": "Buy Now", "PriceNow": 100}]
    )
    expected = candidates.copy(deep=True)
    runner._record_tick_freshness_diagnostics(candidates, "signal_monitor")
    pd.testing.assert_frame_equal(candidates, expected)


def test_snapshot_context_is_local_optional_and_bounded(tmp_path):
    (tmp_path / "kiteticker_all_ticks_latest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "kiteticker_all_status_latest.json").write_text(
        json.dumps(
            {
                "connected": True,
                "last_tick_at": "2026-08-17T09:15:00",
                "large_unneeded_field": [1, 2, 3],
            }
        ),
        encoding="utf-8",
    )
    recorder = TickFreshnessDiagnostics(tmp_path, enabled=True, path=tmp_path / "out.jsonl")
    context = recorder.snapshot_context(tmp_path, "all")
    assert context["snapshot_file_mtime"].endswith("Z")
    assert context["ticker_status"] == {"connected": True, "last_tick_at": "2026-08-17T09:15:00"}


def test_diagnostic_call_does_not_change_canonical_candidate_or_lifecycle_shape(tmp_path):
    destination = tmp_path / "diagnostics.jsonl"
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._tick_freshness_diagnostics = TickFreshnessDiagnostics(
        tmp_path, enabled=True, path=destination
    )
    runner._diagnostic_live_tick_applied_symbols = set()
    runner.output_dir = tmp_path
    runner.universe = "all"
    runner.run_at = datetime(2026, 8, 17, 9, 15, tzinfo=timezone.utc)
    runner.actionable_runner = SimpleNamespace(live_ticks={})
    candidates = pd.DataFrame(
        [{"When": "2026-08-17T09:15:00Z", "Symbol": "ABC", "NowAction": "Buy Now", "PriceNow": 100}]
    )
    columns_before = list(candidates.columns)
    values_before = candidates.to_dict(orient="records")
    runner._record_tick_freshness_diagnostics(candidates, "signal_monitor")
    assert list(candidates.columns) == columns_before
    assert candidates.to_dict(orient="records") == values_before
    diagnostic = _records(destination)[0]
    assert "schema_version" in diagnostic
    assert all(field not in candidates.columns for field in diagnostic)


def test_candidate_output_is_identical_when_diagnostics_are_enabled_or_disabled(tmp_path):
    candidates = pd.DataFrame(
        [{"When": "2026-08-17T09:15:00Z", "Symbol": "ABC", "NowAction": "Buy Now", "PriceNow": 100}]
    )

    def run_observer(enabled):
        runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
        runner._tick_freshness_diagnostics = TickFreshnessDiagnostics(
            tmp_path,
            enabled=enabled,
            path=tmp_path / f"{enabled}.jsonl",
        )
        runner._diagnostic_live_tick_applied_symbols = {"ABC"}
        runner.output_dir = tmp_path
        runner.universe = "all"
        runner.run_at = datetime(2026, 8, 17, 9, 15, tzinfo=timezone.utc)
        runner.actionable_runner = SimpleNamespace(
            live_ticks={"ABC": {"last_price": 100, "received_at": "2026-08-17T09:15:00Z"}}
        )
        output = candidates.copy(deep=True)
        runner._record_tick_freshness_diagnostics(output, "signal_monitor")
        return output

    pd.testing.assert_frame_equal(run_observer(False), run_observer(True))

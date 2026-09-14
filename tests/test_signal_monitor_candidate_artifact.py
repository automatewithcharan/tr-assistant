import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.SignalMonitor import SignalMonitorRunner


def _runner(tmp_path) -> SignalMonitorRunner:
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    runner.universe = "all"
    runner.fast_mode = True
    runner.save_full_outputs = True
    runner.enable_dashboard = False
    runner._candidate_source_revision_cache = (
        "b1b0af10593aa7b6a3ed88a4200efc9142942119",
        False,
    )
    runner._purge_old_daily_snapshots = lambda keep_days: None
    return runner


def _empty_frames():
    return (
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
    )


def test_completed_export_adds_one_real_aware_cohort_timestamp_without_changing_rows(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    generated_at = datetime(2026, 8, 21, 15, 25, 8, 123456, tzinfo=ZoneInfo("Asia/Kolkata"))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return generated_at.astimezone(tz) if tz is not None else generated_at.replace(tzinfo=None)

    monkeypatch.setattr(
        "pkscreener.classes.SignalMonitor.PKDateUtilities.currentDateTime",
        lambda: generated_at,
    )
    monkeypatch.setattr(signal_monitor_module, "datetime", FrozenDatetime)
    monkeypatch.setenv("FINEDGE_API_KEY", "must-not-be-written")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "also-must-not-be-written")

    report = pd.DataFrame(
        [
            {"Symbol": "RELIANCE", "RunAt": "existing-one", "Signal": "A"},
            {"Symbol": "ITC", "RunAt": "existing-two", "Signal": "B"},
        ]
    )
    original = report.copy(deep=True)
    actionable, simple, state, alerts, summary = _empty_frames()

    runner._save_outputs(report, actionable, simple, state, alerts, summary)

    path = tmp_path / "signalmonitor_all_signals_latest.csv"
    saved = pd.read_csv(path)
    timestamp_values = saved["candidate_generated_at"].unique().tolist()
    parsed = datetime.fromisoformat(timestamp_values[0])

    assert timestamp_values == [generated_at.isoformat(timespec="microseconds")]
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None
    assert saved["Symbol"].tolist() == original["Symbol"].tolist()
    assert saved["RunAt"].tolist() == original["RunAt"].tolist()
    assert saved["Signal"].tolist() == original["Signal"].tolist()
    assert len(saved.index) == len(original.index)
    assert saved["candidate_artifact_row_count"].tolist() == [2, 2]
    assert saved["candidate_artifact_schema_version"].unique().tolist() == [
        SignalMonitorRunner.CANDIDATE_ARTIFACT_SCHEMA_VERSION
    ]
    assert saved["candidate_source_runtime_commit"].unique().tolist() == [
        "b1b0af10593aa7b6a3ed88a4200efc9142942119"
    ]
    assert saved["candidate_source_runtime_dirty"].tolist() == [False, False]
    pd.testing.assert_frame_equal(report, original)

    dated = pd.read_csv(tmp_path / "signalmonitor_all_signals_20260821.csv")
    assert dated["candidate_generated_at"].unique().tolist() == timestamp_values
    assert dated["Symbol"].tolist() == original["Symbol"].tolist()

    # Artifact time is data captured at cohort serialization, not filesystem metadata.
    os.utime(path, (2_000_000_000, 2_000_000_000))
    assert pd.read_csv(path)["candidate_generated_at"].unique().tolist() == timestamp_values
    payload = path.read_text(encoding="utf-8")
    assert "must-not-be-written" not in payload
    assert "also-must-not-be-written" not in payload


def test_candidate_artifact_rejects_naive_generation_time(tmp_path):
    runner = _runner(tmp_path)

    with pytest.raises(ValueError, match="timezone-aware"):
        runner._prepare_candidate_artifact(
            pd.DataFrame([{"Symbol": "ITC"}]),
            generated_at=datetime(2026, 8, 21, 10, 7, 8),
        )


def test_partial_and_existing_legacy_exports_are_not_synthesized(tmp_path):
    runner = _runner(tmp_path)
    legacy_path = tmp_path / "signalmonitor_all_signals_20260819.csv"
    legacy_bytes = b"Symbol,RunAt\nRELIANCE,2026-08-19 15:20:00\n"
    legacy_path.write_bytes(legacy_bytes)

    report = pd.DataFrame([{"Symbol": "ITC", "RunAt": "unchanged"}])
    actionable, simple, state, alerts, summary = _empty_frames()
    runner._save_latest_outputs_only(report, actionable, simple, state, alerts, summary)

    partial = pd.read_csv(tmp_path / "signalmonitor_all_signals_latest.csv")
    assert "candidate_generated_at" not in partial.columns
    assert legacy_path.read_bytes() == legacy_bytes
    assert "candidate_generated_at" not in pd.read_csv(legacy_path).columns

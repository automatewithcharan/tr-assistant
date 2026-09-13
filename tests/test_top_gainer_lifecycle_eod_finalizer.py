from __future__ import annotations

import pytest

from pkscreener.classes.TopGainerLifecycleRecorder import TopGainerLifecycleRecorder
from scripts.analyze_top_gainer_lifecycle import summarize
from scripts.finalize_top_gainer_lifecycle_eod import _session_finalization


OBSERVED_AT = "2026-08-04T09:30:00+05:30"
FINALIZED_AT = "2026-08-04T15:29:00+05:30"


def _recorder(tmp_path):
    recorder = TopGainerLifecycleRecorder(str(tmp_path / "evidence"))
    lifecycle_id = recorder.record_observation(
        {
            "symbol": "AAA",
            "current_price": 100.0,
            "feature_available_at": OBSERVED_AT,
            "missing_reasons": [],
        },
        "early_breakout",
        OBSERVED_AT,
        "broad_nse",
    )
    return recorder, lifecycle_id


def _strict_sessions(recorder):
    events = recorder.replay()
    observations = [
        row for row in events if row.get("event_type") == "candidate_observed"
    ]
    return _session_finalization(events, observations)


def test_legacy_terminal_without_completeness_status_remains_analyzable(tmp_path):
    recorder, lifecycle_id = _recorder(tmp_path)
    recorder.enrich_session(
        lifecycle_id,
        FINALIZED_AT,
        "early_breakout",
        {"mfe_pct": 4.0, "reached_5pct": False},
    )

    result = summarize(recorder.events_path)

    assert result["incomplete_outcome_count"] == 0
    assert result["lane_comparison"]["early_breakout"]["complete_outcomes"] == 1
    assert result["session_finalization"]["2026-08-04"]["complete_outcomes"] == 1


def test_generic_incomplete_recorder_outcome_remains_analyzable(tmp_path):
    recorder, lifecycle_id = _recorder(tmp_path)
    recorder.enrich_session(
        lifecycle_id,
        FINALIZED_AT,
        "early_breakout",
        {"completeness_status": "incomplete", "mfe_pct": None},
    )

    result = summarize(recorder.events_path)

    assert result["incomplete_outcome_count"] == 1
    assert result["lane_comparison"]["early_breakout"]["incomplete_outcomes"] == 1


def test_invalid_present_completeness_status_still_fails(tmp_path):
    recorder, lifecycle_id = _recorder(tmp_path)
    recorder.enrich_session(
        lifecycle_id,
        FINALIZED_AT,
        "early_breakout",
        {"completeness_status": "partial"},
    )

    with pytest.raises(ValueError, match="invalid completeness status"):
        summarize(recorder.events_path)


def test_strict_eod_finalizer_rejects_missing_explicit_completeness_status(tmp_path):
    recorder, lifecycle_id = _recorder(tmp_path)
    recorder.enrich_session(
        lifecycle_id,
        FINALIZED_AT,
        "eod_enrichment",
        {"mfe_pct": 4.0},
    )

    with pytest.raises(ValueError, match="invalid completeness status"):
        _strict_sessions(recorder)


def test_strict_eod_finalizer_rejects_incomplete_without_diagnostic(tmp_path):
    recorder, lifecycle_id = _recorder(tmp_path)
    recorder.enrich_session(
        lifecycle_id,
        FINALIZED_AT,
        "eod_enrichment",
        {"completeness_status": "incomplete", "mfe_pct": None},
    )

    with pytest.raises(ValueError, match="one matching EOD data_incomplete diagnostic"):
        _strict_sessions(recorder)


def test_strict_eod_finalizer_accepts_linked_incomplete_diagnostic(tmp_path):
    recorder, lifecycle_id = _recorder(tmp_path)
    recorder.record_event(
        lifecycle_id,
        "data_incomplete",
        FINALIZED_AT,
        "eod_enrichment",
        completeness_status="incomplete",
        missing_reason="bar_file_missing",
    )
    recorder.enrich_session(
        lifecycle_id,
        FINALIZED_AT,
        "eod_enrichment",
        {"completeness_status": "incomplete", "mfe_pct": None},
    )

    sessions = _strict_sessions(recorder)

    assert sessions["2026-08-04"]["incomplete_outcomes"] == 1


def test_strict_eod_finalizer_accepts_valid_complete_outcome(tmp_path):
    recorder, lifecycle_id = _recorder(tmp_path)
    recorder.enrich_session(
        lifecycle_id,
        FINALIZED_AT,
        "eod_enrichment",
        {"completeness_status": "complete", "mfe_pct": 4.0},
    )

    sessions = _strict_sessions(recorder)

    assert sessions["2026-08-04"]["complete_outcomes"] == 1

from __future__ import annotations

import json
from datetime import datetime

import pytest
import pandas as pd

from pkscreener.classes.TopGainerLifecycleRecorder import TopGainerLifecycleRecorder
from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from scripts.analyze_top_gainer_lifecycle import summarize


def _snapshot(symbol="AAA", when="2026-08-03T09:30:00+05:30"):
    return {
        "symbol": symbol, "feature_available_at": when, "current_price": 101.0,
        "previous_close": 100.0, "day_open": 100.5, "return_from_previous_close": 1.0,
        "return_from_open": 0.5, "current_rank": 2, "rank_percentile": 99.0,
        "missing_reasons": [],
    }


def test_observation_immutability_and_multiple_same_symbol_observations(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    first = rec.record_observation(_snapshot(), "early_breakout", "2026-08-03T09:30:00+05:30", "nifty500")
    second = rec.record_observation(_snapshot(), "early_breakout", "2026-08-03T09:45:00+05:30", "nifty500")
    assert first != second
    assert len(rec.replay()) == 2
    rec.record_observation({**_snapshot(), "current_price": 999}, "early_breakout", "2026-08-03T09:30:00+05:30", "nifty500")
    assert json.loads(rec.events_path.read_text().splitlines()[0])["signal_time_fields"]["current_price"] == 101.0


def test_event_linking_and_duplicate_idempotency(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    lifecycle = rec.record_observation(_snapshot(), "top_gainer_radar", "2026-08-03T09:30:00+05:30")
    event_id = rec.record_event(lifecycle, "alert_emitted", "2026-08-03T09:31:00+05:30", "top_gainer_radar", alert_type="radar")
    assert rec.record_event(lifecycle, "alert_emitted", "2026-08-03T09:31:00+05:30", "top_gainer_radar", alert_type="radar") == event_id
    assert rec.replay()[1]["origin_source_lane"] == "top_gainer_radar"
    assert len(rec.replay()) == 2
    with pytest.raises(ValueError, match="unknown lifecycle"):
        rec.record_event("missing-observation", "alert_emitted", "2026-08-03T09:31:00+05:30", "top_gainer_radar")


def test_downstream_event_carries_observation_identity_and_microseconds(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    observation_id = rec.record_observation(_snapshot(), "early_breakout", "2026-08-03T09:30:00+05:30")
    rec.record_event(observation_id, "alert_emitted", "2026-08-03T09:31:00+05:30", "early_breakout")
    event = rec.replay()[1]
    assert event["observation_id"] == observation_id
    assert event["observation_timestamp"].endswith("00.000000+05:30")
    assert event["origin_source_lane"] == "early_breakout"


def test_causal_timestamp_and_future_label_isolation(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    lifecycle = rec.record_observation(_snapshot(), "anomaly", "2026-08-03T09:30:00+05:30")
    with pytest.raises(ValueError):
        rec.record_event(lifecycle, "confirmation_armed", "2026-08-03T09:29:00+05:30", "anomaly")
    rec.enrich_session(lifecycle, "2026-08-03T15:30:00+05:30", "anomaly", {"mfe_pct": 5, "reached_3pct": True})
    observation = rec.replay()[0]
    assert observation["future_labels"] is None
    assert rec.replay()[1]["future_labels"]["mfe_pct"] == 5
    assert rec.replay()[1]["origin_source_lane"] == "anomaly"


def test_corrupt_replay_and_deterministic_derived_snapshot(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    lifecycle = rec.record_observation(_snapshot(), "full_universe_rank", "2026-08-03T09:30:00+05:30")
    rec.enrich_session(lifecycle, "2026-08-03T15:30:00+05:30", "full_universe_rank", {
        "mfe_pct": 6, "mae_pct": -1, "reached_3pct": True, "reached_5pct": True, "reached_10pct": False,
    })
    first = rec.derive_joined_snapshot().read_text()
    second = rec.derive_joined_snapshot().read_text()
    assert first == second
    rec.events_path.write_text(rec.events_path.read_text() + "{bad\n")
    with pytest.raises(ValueError, match="corrupt"):
        rec.replay()


def test_missing_fields_and_stale_data_are_explicit(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    lifecycle = rec.record_observation({"symbol": "AAA", "missing_reasons": ["spread", "vwap"], "stale_data": True},
                                       "live_feature_recorder", "2026-08-03T09:30:00+05:30")
    row = rec.replay()[0]["signal_time_fields"]
    assert row["missing_reasons"] == ["spread", "vwap"]
    assert row["stale_data"] is True
    assert lifecycle


def test_analysis_metrics_separate_future_labels(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    lifecycle = rec.record_observation(_snapshot(), "leader_ignition", "2026-08-03T09:30:00+05:30")
    rec.record_event(lifecycle, "alert_emitted", "2026-08-03T09:31:00+05:30", "leader_ignition")
    rec.enrich_session(lifecycle, "2026-08-03T15:30:00+05:30", "leader_ignition", {
        "mfe_pct": 4, "mae_pct": -1, "reached_3pct": True, "reached_5pct": False, "reached_10pct": False,
        "detection_time_minutes": 9999,
    })
    result = summarize(rec.events_path)
    assert result["observations"] == 1
    assert result["alerts_by_lane"]["leader_ignition"] == 1
    assert result["reaching_3_pct"] == 100.0
    assert result["future_labels_used_for_ranking"] is False
    assert result["median_detection_time_minutes"] == "not_available"


def test_eod_enrichment_uses_only_post_observation_bars_and_is_idempotent(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    observation_id = rec.record_observation(_snapshot(), "live_feature_recorder", "2026-08-03T09:30:00+05:30")
    bars = [
        {"timestamp": "2026-08-03T09:29:00+05:30", "high": 500, "low": 50, "close": 500},
        {"timestamp": "2026-08-03T09:45:00+05:30", "high": 106, "low": 98, "close": 105},
    ]
    rec.enrich_from_bars(observation_id, bars)
    rec.enrich_from_bars(observation_id, bars)
    outcomes = [row for row in rec.replay() if row["event_type"] == "session_outcome_finalized"]
    assert len(outcomes) == 1
    assert outcomes[0]["future_labels"]["later_intraday_high"] == 106.0
    assert outcomes[0]["future_labels"]["mfe_pct"] < 6.0


def test_alert_boundary_links_same_symbol_lanes_without_newest_inference(tmp_path):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(str(tmp_path))
    runner.run_at = datetime.fromisoformat("2026-08-03T09:31:00+05:30")
    runner.universe = "nifty500"
    first_id = runner._top_gainer_lifecycle_recorder.record_observation(
        _snapshot(), "early_breakout", "2026-08-03T09:30:00+05:30", "nifty500"
    )
    second_id = runner._top_gainer_lifecycle_recorder.record_observation(
        _snapshot(), "top_gainer_radar", "2026-08-03T09:31:00+05:30", "nifty500"
    )
    alerts = pd.DataFrame([
        {"Symbol": "AAA", "Lane": "early_breakout", "LifecycleObservationId": first_id, "LifecycleTimestamp": "2026-08-03T09:30:00.123456+05:30", "When": "2026-08-03T09:30:00+05:30", "PriceNow": 101, "ChangePct": 1.0},
        {"Symbol": "AAA", "Lane": "top_gainer_radar", "LifecycleObservationId": second_id, "LifecycleTimestamp": "2026-08-03T09:31:00.123456+05:30", "When": "2026-08-03T09:31:00+05:30", "PriceNow": 102, "ChangePct": 2.0},
    ])
    runner._record_lifecycle_alerts(alerts)
    assert alerts["LifecycleObservationId"].nunique() == 2
    rows = runner._top_gainer_lifecycle_recorder.replay()
    assert [row["source_lane"] for row in rows if row["event_type"] == "alert_emitted"] == ["early_breakout", "top_gainer_radar"]
    assert all(row["event_timestamp"].endswith(".123456+05:30") for row in rows if row["event_type"] == "alert_emitted")


def test_ranked_paper_event_uses_explicit_observation_id(tmp_path):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(str(tmp_path))
    observation_id = runner._top_gainer_lifecycle_recorder.record_observation(
        _snapshot(), "ranked_confirmation_paper", "2026-08-03T09:30:00+05:30"
    )
    runner._record_ranked_paper_lifecycle_event({
        "event": "candidate_registered", "observation_id": observation_id,
        "timestamp": "2026-08-03T09:30:01+05:30", "reason": "armed",
    })
    assert runner._top_gainer_lifecycle_recorder.replay()[-1]["event_type"] == "confirmation_armed"
    assert runner._top_gainer_lifecycle_recorder.replay()[-1]["origin_source_lane"] == "ranked_confirmation_paper"


def test_missing_direct_alert_id_records_data_incomplete_without_inference(tmp_path):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(str(tmp_path))
    runner.run_at = datetime.fromisoformat("2026-08-03T09:31:00+05:30")
    runner.universe = "nifty500"
    alerts = pd.DataFrame([{
        "Symbol": "AAA", "Lane": "early_breakout",
        "When": "2026-08-03T09:31:00+05:30", "PriceNow": 101, "ChangePct": 1.0,
    }])
    runner._record_lifecycle_alerts(alerts)
    rows = runner._top_gainer_lifecycle_recorder.replay()
    assert rows[-1]["event_type"] == "data_incomplete"
    assert rows[-1]["observation_id"] is None


def test_lane_analysis_uses_complete_labels_and_date_symbol_windows(tmp_path):
    rec = TopGainerLifecycleRecorder(str(tmp_path))
    ids = []
    for symbol in ("AAA", "BBB"):
        for lane in ("early_breakout", "top_gainer_radar"):
            identifier = rec.record_observation(
                _snapshot(symbol), lane, "2026-08-03T09:30:00+05:30"
            )
            ids.append(identifier)
            rec.record_event(identifier, "alert_emitted", "2026-08-03T09:30:01+05:30", lane)
            rec.enrich_session(identifier, "2026-08-03T15:30:00+05:30", lane, {
                "completeness_status": "complete", "mfe_pct": 4,
                "remaining_upside_pct": 4, "reached_3pct": True,
                "reached_5pct": False, "reached_10pct": False,
            })
    incomplete = rec.record_observation(_snapshot("CCC"), "early_breakout", "2026-08-03T09:30:00+05:30")
    rec.enrich_session(incomplete, "2026-08-03T15:30:00+05:30", "early_breakout", {
        "completeness_status": "incomplete", "reached_5pct": True,
    })
    result = summarize(rec.events_path)
    assert result["duplicate_lane_overlap_count"] == 2
    assert result["lane_comparison"]["early_breakout"]["complete_outcomes"] == 2
    assert result["lane_comparison"]["early_breakout"]["incomplete_outcomes"] == 1
    assert result["lane_comparison"]["early_breakout"]["reaching_5_pct"] == 0.0
    assert result["median_detection_time_minutes"] == "not_available"

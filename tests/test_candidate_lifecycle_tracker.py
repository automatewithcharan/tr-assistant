import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pkscreener.classes.CandidateLifecycleTracker import (
    CandidateLifecycleTracker,
    format_daily_report,
    normalize_symbol,
)
from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from scripts.candidate_lifecycle import main as lifecycle_cli


IST = ZoneInfo("Asia/Kolkata")
DETECTED = datetime(2026, 9, 27, 9, 31, tzinfo=IST)


def snapshot(symbol="RELIANCE", **overrides):
    row = {
        "Symbol": symbol,
        "Exchange": "NSE",
        "Lane": "early_breakout",
        "Lanes": "early_breakout",
        "CandidateRank": 1,
        "PriceNow": 100.0,
        "ChangePct": 2.0,
        "PriceChange1m": 0.2,
        "PriceChange3m": 0.5,
        "PriceChange5m": 0.7,
        "ShortTermVelocity": 0.2,
        "MediumTermVelocity": 0.16,
        "MomentumAcceleration": 0.04,
        "CurrentVolume": 100_000,
        "Avg1mVolume": 2_000,
        "VolumeRatio": 1.8,
        "RVOLSlope": 0.3,
        "VolumeAcceleration": 0.2,
        "Current1mTradedValue": 200_000,
        "Avg1mTradedValue": 180_000,
        "LiquidityStatus": "PASS",
        "SuggestedQty": 10,
        "EstimatedCapacityQty": 200,
        "IntendedParticipationPct": 0.5,
        "LiquidityDataSource": "observed_tick_volume_rate",
        "RSI": 65,
        "BuyerSellerRatio": 1.8,
        "PullbackPct": 0.2,
        "Week52Context": "approaching 52-week high with confirming momentum",
        "CandidateState": "STRENGTHENING",
        "ScoreTotal": 68,
        "ScoreMomentum": 22,
        "ScoreVolume": 18,
        "ScoreLiquidity": 10,
        "ScoreStructure": 18,
        "ScorePenalties": 0,
    }
    row.update(overrides)
    return row


def create(tracker, symbol="RELIANCE", at=DETECTED, **overrides):
    row = snapshot(symbol, **overrides)
    return tracker.create_candidate(row, at, "cycle-1", {"caps": {"early": 4}})


def test_selected_candidate_creates_one_durable_record_and_unique_ids(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    first = create(tracker)
    second = create(tracker, at=DETECTED + timedelta(seconds=1))

    assert first != second
    assert first.startswith("NSE_RELIANCE_20260927_093100_")
    assert tracker.inspect_candidate(first)["discord_selected"] == 1
    with tracker._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2


def test_signal_monitor_selected_frame_gets_traceable_candidate_id(tmp_path):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._candidate_lifecycle_tracker = CandidateLifecycleTracker(str(tmp_path))
    runner.run_at = DETECTED
    runner.run_at_str = "2026-09-27 09:31:00"
    runner.timestamp = "20260927_093100"
    runner._cycle_number = 4
    selected = runner._record_selected_candidate_lifecycle(pd.DataFrame([snapshot()]))
    candidate_id = selected.iloc[0]["CandidateId"]
    assert candidate_id
    assert runner._candidate_lifecycle_tracker.inspect_candidate(candidate_id)["cycle_id"].endswith("cycle-4")


def test_suppressed_empty_frame_creates_no_candidate(tmp_path):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._candidate_lifecycle_tracker = CandidateLifecycleTracker(str(tmp_path))
    result = runner._record_selected_candidate_lifecycle(pd.DataFrame())
    assert result.empty
    with runner._candidate_lifecycle_tracker._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_score_liquidity_and_configuration_snapshot_are_preserved(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker)
    record = tracker.inspect_candidate(candidate_id)
    assert record["score_total"] == 68
    assert record["snapshot"]["ScoreMomentum"] == 22
    assert record["snapshot"]["LiquidityDataSource"] == "observed_tick_volume_rate"
    assert record["configuration"] == {"caps": {"early": 4}}
    assert len(record["config_hash"]) == 16


def test_missing_metrics_remain_null_not_fabricated(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(
        tracker,
        PriceChange1m=None,
        PriceChange3m=None,
        MomentumAcceleration=None,
        VolumeRatio=None,
        RVOLSlope=None,
        LiquidityStatus="UNKNOWN",
    )
    stored = tracker.inspect_candidate(candidate_id)["snapshot"]
    assert stored["PriceChange1m"] is None
    assert stored["VolumeRatio"] is None
    assert stored["LiquidityStatus"] == "UNKNOWN"


def test_price_observations_calculate_mfe_mae_horizons_and_times(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path), horizons=(1, 3, 5))
    candidate_id = create(tracker)
    tracker.observe_prices({"RELIANCE": {"last_price": 102}}, DETECTED + timedelta(minutes=1))
    tracker.observe_prices({"NSE:RELIANCE": {"last_price": 97}}, DETECTED + timedelta(minutes=3))
    tracker.observe_prices({"RELIANCE": {"last_price": 101}}, DETECTED + timedelta(minutes=5))
    record = tracker.inspect_candidate(candidate_id)

    assert record["price_outcome"]["mfe_pct"] == pytest.approx(2.0)
    assert record["price_outcome"]["mae_pct"] == pytest.approx(-3.0)
    assert record["price_outcome"]["time_to_highest_seconds"] == 60
    assert record["price_outcome"]["time_to_lowest_seconds"] == 180
    assert [row["price"] for row in record["horizons"]] == [102, 97, 101]


def test_missing_horizons_and_eod_finalization_are_explicit(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path), horizons=(1, 30))
    candidate_id = create(tracker)
    tracker.observe_prices({"RELIANCE": {"last_price": 101}}, DETECTED + timedelta(minutes=1))
    assert tracker.finalize_eod("2026-09-27", DETECTED.replace(hour=15, minute=30)) == 1
    record = tracker.inspect_candidate(candidate_id)
    assert [row["status"] for row in record["horizons"]] == ["OBSERVED", "MISSING"]
    assert record["price_outcome"]["eod_price"] == 101
    assert record["status"] == "EOD_UNCONFIRMED"


def test_exact_candidate_id_match_and_timezone_normalization(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker)
    result = tracker.ingest_pine_event({
        "event": "ENTRY", "candidate_id": candidate_id, "symbol": "NSE:RELIANCE-EQ",
        "timestamp": "2026-09-27T04:03:00Z", "assumed_entry": 101,
    })
    assert result["candidate_id"] == candidate_id
    assert result["match_method"] == "candidate_id"
    assert result["match_confidence"] == "exact"
    assert normalize_symbol("NSE:RELIANCE-EQ") == "RELIANCE"


def test_symbol_time_window_chooses_most_recent_deterministically(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path), pine_match_max_age_minutes=30)
    older = create(tracker, at=DETECTED)
    newer = create(tracker, at=DETECTED + timedelta(minutes=5))
    result = tracker.ingest_pine_event({
        "event": "ENTRY", "symbol": "RELIANCE",
        "timestamp": (DETECTED + timedelta(minutes=8)).isoformat(), "signal_price": 102,
    })
    assert result["candidate_id"] == newer
    assert result["match_method"] == "symbol_time_window"
    assert result["match_confidence"] == "likely"
    assert result["alternatives"] == [older]


def test_same_timestamp_candidates_are_ambiguous_and_not_silently_linked(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    create(tracker)
    create(tracker)
    result = tracker.ingest_pine_event({
        "event": "ENTRY", "symbol": "RELIANCE",
        "timestamp": (DETECTED + timedelta(minutes=2)).isoformat(), "signal_price": 101,
    })
    assert result["candidate_id"] is None
    assert result["match_confidence"] == "ambiguous"
    assert len(result["alternatives"]) == 2


def test_stale_and_unknown_symbol_events_remain_durable_unmatched(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path), pine_match_max_age_minutes=5)
    create(tracker)
    stale = tracker.ingest_pine_event({
        "event": "ENTRY", "symbol": "RELIANCE",
        "timestamp": (DETECTED + timedelta(minutes=10)).isoformat(), "signal_price": 102,
    })
    unknown = tracker.ingest_pine_event({
        "event": "ENTRY", "symbol": "UNKNOWN",
        "timestamp": (DETECTED + timedelta(minutes=1)).isoformat(), "signal_price": 10,
    })
    assert stale["match_confidence"] == unknown["match_confidence"] == "unmatched"
    with tracker._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM pine_events").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_pine_links WHERE candidate_id IS NULL"
        ).fetchone()[0] == 2


def test_entry_delay_pre_entry_move_and_pre_entry_mfe(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker)
    tracker.observe_prices({"RELIANCE": {"last_price": 102}}, DETECTED + timedelta(minutes=2))
    result = tracker.ingest_pine_event({
        "event": "BUY", "symbol": "RELIANCE",
        "timestamp": (DETECTED + timedelta(minutes=5)).isoformat(),
        "signal_price": 102.5, "assumed_entry": 103, "qty": 10,
        "initial_stop": 101, "target": 107,
    })
    record = tracker.inspect_candidate(candidate_id)
    assert result["seconds_from_candidate"] == 300
    assert result["pre_entry_price_change"] == 3
    assert result["pre_entry_price_change_pct"] == 3
    assert result["pre_entry_mfe_pct"] == pytest.approx(2)
    assert record["status"] == "PINE_CONFIRMED"
    assert record["trade"]["entry_at_utc"] == "2026-09-27T04:06:00.000000Z"


def test_stop_update_is_preserved_and_symbol_matches_open_trade(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path), pine_match_max_age_minutes=1)
    candidate_id = create(tracker)
    tracker.ingest_pine_event({
        "event": "ENTRY", "candidate_id": candidate_id, "timestamp": DETECTED.isoformat(),
        "assumed_entry": 100, "initial_stop": 98,
    })
    result = tracker.ingest_pine_event({
        "event": "STOP_UPDATE", "symbol": "RELIANCE",
        "timestamp": (DETECTED + timedelta(minutes=20)).isoformat(),
        "stop_price": 101, "reason": "PROFIT LOCK",
    })
    assert result["candidate_id"] == candidate_id
    assert result["match_method"] == "symbol_open_trade"
    updates = json.loads(tracker.inspect_candidate(candidate_id)["trade"]["stop_updates_json"])
    assert updates[0]["stop_price"] == 101


@pytest.mark.parametrize(
    "reason,exit_price,expected_r",
    [
        ("TARGET", 106, 3.0),
        ("HARD STOP", 98, -1.0),
        ("EOD", 101, 0.5),
        ("AMBIGUOUS SL/TARGET", 99, -0.5),
    ],
)
def test_exit_reasons_and_theoretical_r_are_analytics_only(
    tmp_path, reason, exit_price, expected_r
):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker)
    tracker.ingest_pine_event({
        "event": "ENTRY", "candidate_id": candidate_id, "timestamp": DETECTED.isoformat(),
        "assumed_entry": 100, "qty": 10, "initial_stop": 98,
    })
    tracker.ingest_pine_event({
        "event": "EXIT", "candidate_id": candidate_id,
        "timestamp": (DETECTED + timedelta(minutes=15)).isoformat(),
        "exit_price": exit_price, "reason": reason,
    })
    trade = tracker.inspect_candidate(candidate_id)["trade"]
    assert trade["exit_reason"] == reason
    assert trade["theoretical_r"] == pytest.approx(expected_r)
    assert trade["theoretical_pnl"] == pytest.approx((exit_price - 100) * 10)


def test_low_liquidity_skip_updates_status_without_inventing_trade(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker)
    tracker.ingest_pine_event({
        "event": "LOW_LIQUIDITY_SKIP", "candidate_id": candidate_id,
        "timestamp": (DETECTED + timedelta(minutes=2)).isoformat(),
    })
    record = tracker.inspect_candidate(candidate_id)
    assert record["status"] == "PINE_LOW_LIQUIDITY_SKIP"
    assert record["trade"] is None


def test_raw_payload_is_retained_with_secret_fields_redacted(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker)
    result = tracker.ingest_pine_event({
        "event": "BUY_READY", "candidate_id": candidate_id,
        "timestamp": DETECTED.isoformat(), "custom": "kept", "token": "do-not-store",
    })
    with tracker._connect() as connection:
        raw = json.loads(connection.execute(
            "SELECT raw_payload_json FROM pine_events WHERE pine_event_id=?",
            (result["pine_event_id"],),
        ).fetchone()[0])
    assert raw["custom"] == "kept"
    assert raw["token"] == "[REDACTED]"


def test_delivery_outcomes_update_candidate(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker)
    tracker.record_delivery({
        "candidate_id": candidate_id, "delivery_status": "delivered",
        "delivered_at": (DETECTED + timedelta(seconds=3)).isoformat(),
        "discord_message_id": "123",
    })
    record = tracker.inspect_candidate(candidate_id)
    assert record["discord_sent"] == 1
    assert record["discord_message_id"] == "123"
    assert record["discord_delivery_result"] == "delivered"


def test_lifecycle_persistence_failure_does_not_block_candidate_frame(tmp_path):
    class BrokenTracker:
        def create_candidate(self, *_args, **_kwargs):
            raise OSError("disk unavailable")

    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._candidate_lifecycle_tracker = BrokenTracker()
    runner.run_at = DETECTED
    runner.run_at_str = "2026-09-27 09:31:00"
    runner.timestamp = "20260927_093100"
    runner._cycle_number = 1
    frame = pd.DataFrame([snapshot()])
    result = runner._record_selected_candidate_lifecycle(frame)
    assert list(result["Symbol"]) == ["RELIANCE"]
    assert result.iloc[0]["CandidateId"] == ""


def test_daily_report_is_descriptive_and_queryable(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker, Lanes="early_breakout,top_gainer_radar")
    tracker.observe_prices({"RELIANCE": {"last_price": 102}}, DETECTED + timedelta(minutes=2))
    tracker.ingest_pine_event({
        "event": "ENTRY", "candidate_id": candidate_id,
        "timestamp": (DETECTED + timedelta(minutes=3)).isoformat(), "assumed_entry": 101,
    })
    report = tracker.daily_report("2026-09-27")
    rendered = format_daily_report(report)
    assert report["candidates_selected"] == 1
    assert report["lane_distribution"] == {"early_breakout,top_gainer_radar": 1}
    assert report["movement_state_distribution"] == {"STRENGTHENING": 1}
    assert report["analytics_by"]["lane"]["early_breakout,top_gainer_radar"]["pine_confirmed"] == 1
    assert report["analytics_by"]["score_bucket"]["60-69"]["median_mfe_pct"] == pytest.approx(2)
    assert "Descriptive research only" in rendered
    assert candidate_id in rendered

    json_path, text_path = tracker.write_daily_report("2026-09-27")
    assert json.loads(json_path.read_text())["candidates_selected"] == 1
    assert "Descriptive research only" in text_path.read_text()


def test_local_cli_inspects_and_reports_without_starting_server(tmp_path, capsys):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    candidate_id = create(tracker)
    assert lifecycle_cli([
        "--output-dir", str(tmp_path), "inspect", candidate_id,
    ]) == 0
    assert candidate_id in capsys.readouterr().out
    assert lifecycle_cli([
        "--output-dir", str(tmp_path), "report", "--date", "2026-09-27", "--json",
    ]) == 0
    assert '"candidates_selected": 1' in capsys.readouterr().out


def test_retention_prunes_only_old_candidates(tmp_path):
    tracker = CandidateLifecycleTracker(str(tmp_path), retention_days=30)
    old = create(tracker, at=datetime(2026, 7, 1, 10, 0, tzinfo=IST))
    current = create(tracker)
    assert tracker.prune(datetime(2026, 9, 27, tzinfo=IST).date()) == 1
    with pytest.raises(KeyError):
        tracker.inspect_candidate(old)
    assert tracker.inspect_candidate(current)["candidate_id"] == current

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scripts.analyze_prospective_timing_funnel import (
    _stage_event,
    analyze,
    events_from_candidate_delivery,
    events_from_lifecycle,
    events_from_timing,
    load_timing_compact,
    main,
    opportunity_id,
    runtime_coverage,
    stage_outcome,
)


IST = ZoneInfo("Asia/Kolkata")


def event(stage, day="2026-09-14", symbol="AAA", minute=0, **kwargs):
    return _stage_event(
        stage,
        day,
        symbol,
        f"{day}T10:{minute:02d}:00+05:30",
        "synthetic",
        **kwargs,
    )


def bars(day="2026-09-14", start=(10, 1), end=(15, 29), base=100.0):
    current = datetime.fromisoformat(f"{day}T{start[0]:02d}:{start[1]:02d}:00+05:30")
    finish = datetime.fromisoformat(f"{day}T{end[0]:02d}:{end[1]:02d}:00+05:30")
    rows = []
    while current <= finish:
        minutes = (current.hour * 60 + current.minute) - (10 * 60 + 1)
        close = base + minutes * 0.01
        rows.append({"datetime": current.isoformat(), "high": close + 0.2, "low": close - 0.2, "close": close})
        current += timedelta(minutes=1)
    return rows


def test_identity_is_date_scoped_and_symbol_normalized():
    assert opportunity_id("2026-09-14", "aaa") == opportunity_id("2026-09-14", "AAA")
    assert opportunity_id("2026-09-14", "AAA") != opportunity_id("2026-09-15", "AAA")


def test_invalid_identity_date_symbol_join_fails_closed():
    row = event("SOURCE_EVALUATED")
    row["symbol"] = "BBB"
    with pytest.raises(ValueError, match="identity"):
        analyze([row])


def test_later_day_same_symbol_stays_a_separate_opportunity():
    result = analyze([
        event("SOURCE_EVALUATED", day="2026-09-14"),
        event("SOURCE_EVALUATED", day="2026-09-15"),
    ])
    assert result["opportunities"] == 2


def test_stage_order_is_causal_and_later_precursor_is_not_back_joined():
    result = analyze([
        event("BUY_NOW_BOARD_QUALIFIED", minute=0),
        event("PRECURSOR_QUALIFIED", minute=5),
    ])
    row = result["diagnostic_rows"][0]
    assert row["stage_order_status"] == "causal"
    assert row["first_precursor_qualified_at"] is None
    assert row["stage_order_violations"] == []
    assert result["stage_counts"]["PRECURSOR_QUALIFIED"]["opportunities_reached"] == 0
    assert result["stage_counts"]["PRECURSOR_QUALIFIED"]["raw_opportunities_present"] == 1
    assert result["stage_counts"]["PRECURSOR_QUALIFIED"]["raw_events"] == 1
    assert result["stage_counts"]["PRECURSOR_QUALIFIED"]["repeated_events"] == 0


def test_later_only_source_evaluation_is_raw_but_not_causal_reach():
    result = analyze([
        event("BUY_NOW_BOARD_QUALIFIED", minute=0),
        event("SOURCE_EVALUATED", minute=5),
    ])
    row = result["diagnostic_rows"][0]
    assert row["first_source_evaluated_at"] is None
    assert result["stage_counts"]["SOURCE_EVALUATED"]["opportunities_reached"] == 0
    assert result["stage_counts"]["SOURCE_EVALUATED"]["raw_opportunities_present"] == 1
    assert result["stage_counts"]["SOURCE_EVALUATED"]["raw_events"] == 1


def test_valid_ordered_stages_are_counted_as_causal_reach():
    result = analyze([
        event("SOURCE_EVALUATED", minute=0),
        event("PRECURSOR_QUALIFIED", minute=1),
        event("BUY_NOW_BOARD_QUALIFIED", minute=2),
    ])
    for stage in (
        "SOURCE_EVALUATED", "PRECURSOR_QUALIFIED", "BUY_NOW_BOARD_QUALIFIED"
    ):
        assert result["stage_counts"][stage]["opportunities_reached"] == 1
        assert result["stage_counts"][stage]["raw_events"] == 1


def test_delayed_delivery_is_valid():
    result = analyze([
        event("BUY_NOW_BOARD_QUALIFIED", minute=0),
        event("DISCORD_SUCCESSFUL", minute=3, message_kind="buy_now_board"),
    ])
    row = result["diagnostic_rows"][0]
    assert row["stage_order_status"] == "causal"
    assert row["buy_now_board_qualified_to_discord_successful_seconds"] == 180.0


def test_outcomes_start_after_actual_stage_timestamp_and_exclude_predelivery_bar():
    stage = _stage_event(
        "DISCORD_SUCCESSFUL", "2026-09-14", "AAA",
        "2026-09-14T10:03:30+05:30", "synthetic", price=100,
    )
    tape = bars(start=(10, 3))
    # This containing candle is not strictly after 10:03:30 and has a huge high.
    tape[0]["high"] = 999.0
    outcome = stage_outcome(stage, tape)
    assert outcome["completeness_status"] == "complete"
    assert outcome["later_intraday_high"] < 999.0
    assert outcome["coverage_issues"] == []


def test_reference_fallback_uses_only_a_completed_candle():
    stage = _stage_event(
        "DISCORD_SUCCESSFUL", "2026-09-14", "AAA",
        "2026-09-14T10:03:30+05:30", "synthetic",
    )
    tape = bars(start=(10, 1))
    tape[1]["close"] = 101.0  # 10:02 candle completes at 10:03 and is known.
    tape[2]["close"] = 500.0  # 10:03 candle is still open at the event.
    outcome = stage_outcome(stage, tape)
    assert outcome["reference_price"] == 101.0
    assert outcome["reference_price_source"] == "last_completed_minute_close"


def test_horizon_return_uses_only_candle_completed_by_cutoff():
    stage = _stage_event(
        "DISCORD_SUCCESSFUL", "2026-09-14", "AAA",
        "2026-09-14T10:03:30+05:30", "synthetic", price=100,
    )
    tape = bars(start=(10, 4))
    # At the 10:08:30 five-minute cutoff, the 10:07 candle is complete but the
    # 10:08 candle is not. Its later close must not enter the 5m return.
    tape[3]["close"] = 102.0
    tape[4]["close"] = 150.0
    outcome = stage_outcome(stage, tape)
    assert outcome["return_5m_pct"] == pytest.approx(2.0)


def test_incomplete_minute_tape_stays_incomplete():
    tape = bars()
    del tape[10]
    outcome = stage_outcome(event("SOURCE_EVALUATED", price=100), tape)
    assert outcome["completeness_status"] == "incomplete"
    assert "internal_minute_gap" in outcome["coverage_issues"]
    assert outcome["mfe_pct"] is None


def test_runtime_gap_is_unknown_or_observed_with_gaps_not_rejection():
    timing = [
        {"trading_date": "2026-09-14", "event_type": "SOURCE_EVALUATED", "event_timestamp": "2026-09-14T09:15:00+05:30"},
        {"trading_date": "2026-09-14", "event_type": "SOURCE_EVALUATED", "event_timestamp": "2026-09-14T09:20:00+05:30"},
    ]
    coverage = runtime_coverage("2026-09-14", timing)
    assert coverage["signal_monitor_timing"]["status"] == "observed_with_gaps"
    assert coverage["goal_mode_configuration"] == "unknown"


def test_repeated_board_messages_remain_raw_but_not_new_opportunities():
    events = [event("BUY_NOW_BOARD_QUALIFIED")]
    events += [event("DISCORD_SUCCESSFUL", minute=m, message_kind="buy_now_board") for m in (1, 2, 3)]
    result = analyze(events)
    row = result["diagnostic_rows"][0]
    assert result["opportunities"] == 1
    assert result["stage_counts"]["DISCORD_SUCCESSFUL"]["raw_events"] == 3
    assert row["repeated_board_message_count"] == 2
    assert row["successful_discord_delivery_count"] == 3


def test_lifecycle_alert_is_distinct_from_successful_discord_delivery():
    observation = {
        "event_type": "candidate_observed", "lifecycle_id": "obs-1",
        "trading_date": "2026-09-14", "symbol": "AAA",
        "signal_time_fields": {"current_price": 100},
    }
    alert = {
        "event_type": "alert_emitted", "observation_id": "obs-1",
        "event_timestamp": "2026-09-14T10:00:00+05:30",
        "payload": {"alert_type": "Pre-Move Watch"},
    }
    events, _ = events_from_lifecycle([observation, alert])
    assert [row["stage"] for row in events] == ["PRECURSOR_QUALIFIED"]
    assert all(row["stage"] != "DISCORD_SUCCESSFUL" for row in events)


def test_direct_candidate_delivery_is_precursor_not_pine_eligibility_or_alert():
    rows = [{
        "trading_date": "2026-09-14", "symbol": "AAA",
        "lifecycle_observation_id": "obs-1",
        "detected_at": "2026-09-14T10:00:00+05:30",
        "delivery_status": "delivered",
        "send_attempt_at": "2026-09-14T10:01:00+05:30",
        "delivered_at": "2026-09-14T10:01:01+05:30",
    }]
    stages = [row["stage"] for row in events_from_candidate_delivery(rows)]
    assert "PRECURSOR_QUALIFIED" in stages
    assert "TRADINGVIEW_CANDIDATE_ELIGIBLE" not in stages
    assert "TRADINGVIEW_ALERT" not in stages


def test_earlier_manual_candidate_delivery_does_not_replace_board_delivery():
    result = analyze([
        event("PRECURSOR_QUALIFIED", minute=0),
        event(
            "DISCORD_SUCCESSFUL", minute=1,
            message_kind="manual_tradingview_candidate",
        ),
        event("BUY_NOW_BOARD_QUALIFIED", minute=5),
        event("DISCORD_SUCCESSFUL", minute=6, message_kind="buy_now_board"),
    ])
    row = result["diagnostic_rows"][0]
    assert row["first_discord_successful_at"].startswith("2026-09-14T10:06:00")
    assert row["stage_order_status"] == "causal"
    assert row["successful_board_delivery_count"] == 1
    assert row["successful_manual_candidate_delivery_count"] == 1


def test_future_labels_cannot_change_stage_selection():
    first = event("PRECURSOR_QUALIFIED", minute=0)
    later = event("PRECURSOR_QUALIFIED", minute=5)
    first["future_labels"] = {"mfe_pct": -5}
    later["future_labels"] = {"mfe_pct": 50}
    result = analyze([later, first])
    row = result["diagnostic_rows"][0]
    assert row["first_precursor_qualified_at"].startswith("2026-09-14T10:00:00")


def test_timing_mapping_keeps_board_qualification_and_delivery_separate():
    rows = [
        {"trading_date": "2026-09-14", "symbol": "AAA", "source": "buy_now_board", "event_type": "SOURCE_QUALIFIED", "event_timestamp": "2026-09-14T10:00:00+05:30"},
        {"trading_date": "2026-09-14", "symbol": "AAA", "source": "buy_now_board", "event_type": "DISCORD_SUCCESS", "event_timestamp": "2026-09-14T10:01:00+05:30"},
    ]
    mapped = events_from_timing(rows)
    assert [row["stage"] for row in mapped] == ["BUY_NOW_BOARD_QUALIFIED", "DISCORD_SUCCESSFUL"]
    assert mapped[1]["message_kind"] == "buy_now_board"


def test_streaming_timing_loader_keeps_first_event_and_exact_raw_counts(tmp_path):
    path = tmp_path / "timing.jsonl"
    rows = [
        {"trading_date": "2026-09-14", "symbol": "AAA", "source": "buy_now_board", "event_type": "SOURCE_QUALIFIED", "event_timestamp": f"2026-09-14T10:0{minute}:00+05:30"}
        for minute in (2, 0, 1)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    compact, _coverage, stage_counts, opportunity_counts = load_timing_compact([path])

    assert len(compact) == 1
    assert compact[0]["event_timestamp"].startswith("2026-09-14T10:00:00")
    assert stage_counts["BUY_NOW_BOARD_QUALIFIED"] == 3
    oid = opportunity_id("2026-09-14", "AAA")
    assert opportunity_counts[(oid, "BUY_NOW_BOARD_QUALIFIED", "buy_now_board")] == 3


@pytest.mark.parametrize(
    "input_option",
    [
        "--timing-glob",
        "--lifecycle-events",
        "--goal-journal-glob",
        "--candidate-delivery-glob",
        "--tradingview-alert-glob",
        "--live-feature-glob",
    ],
)
def test_cli_rejects_output_overwriting_any_input_evidence(
    tmp_path, input_option
):
    evidence = tmp_path / "immutable-evidence.dat"
    original = b"immutable evidence bytes\n"
    evidence.write_bytes(original)

    with pytest.raises(SystemExit):
        main([input_option, str(evidence), "--output", str(evidence)])

    assert evidence.read_bytes() == original


def test_cli_rejects_output_inside_minute_bar_evidence_root(tmp_path):
    bars_root = tmp_path / "minute-bars"
    bars_root.mkdir()
    evidence = bars_root / "existing-evidence.json"
    original = b"do not replace\n"
    evidence.write_bytes(original)

    with pytest.raises(SystemExit):
        main(["--bars-root", str(bars_root), "--output", str(evidence)])

    assert evidence.read_bytes() == original

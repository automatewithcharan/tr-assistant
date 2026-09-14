from __future__ import annotations

import json
from datetime import datetime

import pytest

from pkscreener.classes.TopGainerLifecycleRecorder import (
    IST,
    TopGainerLifecycleRecorder,
)
from scripts.analyze_buy_now_timing import analyze, main


def _finalized_at(timestamp: str) -> str:
    observed = datetime.fromisoformat(timestamp).astimezone(IST)
    return observed.replace(hour=15, minute=29, second=0, microsecond=0).isoformat()


def _event(
    recorder: TopGainerLifecycleRecorder,
    symbol: str,
    timestamp: str,
    alert_type: str,
    *,
    move: float | None = None,
    completeness: str | None = "complete",
    mfe: float | None = None,
    mae: float | None = None,
    time_to_high: float | None = None,
    returns: tuple[float | None, ...] = (None, None, None, None),
    finalize: bool = True,
    source_lane: str = "top_gainer_radar",
    alert_timestamp: str | None = None,
) -> str:
    lifecycle_id = recorder.record_observation(
        {
            "symbol": symbol,
            "current_price": 100.0,
            "return_from_previous_close": move,
            "feature_available_at": timestamp,
            "missing_reasons": [],
        },
        source_lane,
        timestamp,
        "synthetic_universe",
    )
    payload = {"alert_type": alert_type}
    if move is not None:
        payload["return_already_achieved"] = move
    recorder.record_event(
        lifecycle_id,
        "alert_emitted",
        alert_timestamp or timestamp,
        source_lane,
        **payload,
    )
    if finalize:
        labels = {
            "mfe_pct": mfe,
            "mae_pct": mae,
            "time_to_later_high_minutes": time_to_high,
            **{
                f"return_{minutes}m_pct": value
                for minutes, value in zip((15, 30, 60, 120), returns)
            },
        }
        if completeness is not None:
            labels["completeness_status"] = completeness
        recorder.enrich_session(
            lifecycle_id,
            _finalized_at(timestamp),
            "eod_enrichment",
            labels,
        )
    return lifecycle_id


@pytest.fixture
def synthetic_events(tmp_path):
    recorder = TopGainerLifecycleRecorder(str(tmp_path / "evidence"))
    _event(recorder, "AAA", "2026-09-11T09:15:00+05:30", "Sell / Exit", move=-1.0)
    _event(recorder, "AAA", "2026-09-11T09:20:00+05:30", "👀 Pre-Move Watch", move=0.5)
    _event(
        recorder,
        "AAA",
        "2026-09-11T09:30:00+05:30",
        "Buy Now",
        move=1.5,
        mfe=2.0,
        mae=-0.5,
        time_to_high=35.0,
        returns=(0.2, 0.4, 0.8, 1.2),
    )
    _event(
        recorder,
        "AAA",
        "2026-09-11T10:00:00+05:30",
        "Buy Now",
        move=2.5,
        mfe=0.4,
        mae=-1.0,
        time_to_high=10.0,
        returns=(-0.1, -0.2, -0.3, -0.4),
    )
    incomplete_id = _event(
        recorder,
        "BBB",
        "2026-09-11T10:00:00+05:30",
        "Buy Now",
        move=3.0,
        completeness="incomplete",
        mfe=99.0,
        mae=-99.0,
    )
    recorder.record_event(
        incomplete_id,
        "data_incomplete",
        _finalized_at("2026-09-11T10:00:00+05:30"),
        "eod_enrichment",
        completeness_status="incomplete",
        missing_reason="session_gap",
        coverage_issues=["internal_minute_gap"],
    )
    # A later precursor must never be joined back to BBB's Buy Now.
    _event(recorder, "BBB", "2026-09-11T10:05:00+05:30", "⚡ Grade-B Anomaly", move=3.2)
    # Same symbol on another date must not join to BBB's Sep 11 Buy Now.
    _event(recorder, "BBB", "2026-09-10T09:25:00+05:30", "👀 Pre-Move Watch", move=0.3)
    # Same date but another symbol must not join to CCC's Buy Now.
    _event(recorder, "DDD", "2026-09-11T10:10:00+05:30", "⚡ Grade-B Anomaly", move=0.8)
    _event(
        recorder,
        "CCC",
        "2026-09-11T10:15:00+05:30",
        "Buy Now",
        move=None,
        completeness=None,
        mfe=None,
        mae=None,
    )
    _event(recorder, "EEE", "2026-09-11T10:20:00+05:30", "Momentum Buy", move=1.0)
    _event(recorder, "FFF", "2026-09-11T10:25:00+05:30", "🚀 Runner Continuation", move=4.0)
    _event(recorder, "GGG", "2026-09-11T10:30:00+05:30", "🔥 Grade-A Runner", move=5.0)
    _event(recorder, "HHH", "2026-09-11T10:35:00+05:30", "custom alert", move=0.1)
    _event(
        recorder,
        "III",
        "2026-09-11T10:40:00+05:30",
        "Buy Now",
        move=1.1,
        finalize=False,
    )
    return recorder.events_path


def test_taxonomy_keeps_alert_types_separate_and_excludes_sell(synthetic_events):
    result = analyze(synthetic_events)
    counts = result["alert_taxonomy"]["counts_by_alert_type"]

    assert counts["Sell / Exit"] == 1
    assert counts["Buy Now"] == 5
    assert counts["Momentum Buy"] == 1
    assert counts["Pre-Move Watch"] == 2
    assert counts["Grade-B Anomaly"] == 2
    assert counts["Runner Continuation"] == 1
    assert counts["Grade-A Runner"] == 1
    assert counts["other"] == 1
    assert result["raw_buy_now_event_statistics"]["events"] == 5


def test_earliest_buy_is_primary_and_repeats_do_not_inflate_it(synthetic_events):
    result = analyze(synthetic_events)
    unique = result["unique_first_buy_now_per_date_symbol"]

    assert unique["raw_buy_now_events"] == 5
    assert unique["unique_date_symbol_first_buys"] == 4
    assert unique["repeated_buy_now_count"] == 1
    aaa = next(row for row in result["per_symbol_first_buy_now"] if row["symbol"] == "AAA")
    assert aaa["first_buy_now_timestamp"].startswith("2026-09-11T09:30:00")
    assert aaa["first_buy_now_move_pct"] == 1.5
    assert aaa["buy_now_alert_count"] == 2


def test_precursor_join_is_strictly_earlier_and_same_date_symbol(synthetic_events):
    result = analyze(synthetic_events)
    transitions = result["precursor_to_buy_now"]["transitions"]

    assert [row["symbol"] for row in transitions] == ["AAA"]
    assert transitions[0]["precursor_type"] == "Pre-Move Watch"
    assert transitions[0]["delay_minutes"] == 10.0
    assert transitions[0]["additional_move_consumed_pct"] == 1.0
    assert result["precursor_to_buy_now"]["precursor_coverage_pct"] == 25.0


def test_same_timestamp_precursor_is_not_earlier(tmp_path):
    recorder = TopGainerLifecycleRecorder(str(tmp_path / "evidence"))
    stamp = "2026-09-11T09:30:00+05:30"
    _event(recorder, "AAA", stamp, "👀 Pre-Move Watch", move=0.5, source_lane="precursor")
    _event(recorder, "AAA", stamp, "Buy Now", move=1.0, source_lane="buy_now")

    result = analyze(recorder.events_path)

    assert result["precursor_to_buy_now"]["with_strictly_earlier_precursor"] == 0


def test_incomplete_and_missing_outcomes_are_reported_but_excluded(synthetic_events):
    result = analyze(synthetic_events)
    unique = result["unique_first_buy_now_per_date_symbol"]
    audit = result["incomplete_data_audit"]

    # Only explicit-complete AAA is eligible. CCC is unknown legacy, BBB is
    # incomplete, and III has no terminal outcome.
    assert unique["complete_outcomes"] == 1
    assert unique["incomplete_outcomes"] == 1
    assert unique["unknown_legacy_outcomes"] == 1
    assert unique["missing_terminal_outcomes"] == 1
    assert unique["outcome_metric_eligible"] == 1
    assert unique["median_post_alert_mfe_pct"] == 2.0
    assert unique["median_post_alert_mae_pct"] == -0.5
    assert audit["missing_reason_counts"] == {"session_gap": 1}
    assert audit["coverage_issue_counts"] == {"internal_minute_gap": 1}
    assert audit["unknown_legacy_count"] == 1


def test_delayed_alert_links_by_identity_without_claiming_observation_metrics(tmp_path):
    recorder = TopGainerLifecycleRecorder(str(tmp_path / "evidence"))
    _event(
        recorder,
        "AAA",
        "2026-09-11T10:00:00+05:30",
        "Buy Now",
        alert_timestamp="2026-09-11T10:03:00+05:30",
        move=1.0,
        mfe=4.0,
        mae=-0.5,
    )

    result = analyze(recorder.events_path)
    unique = result["unique_first_buy_now_per_date_symbol"]

    assert unique["unique_date_symbol_first_buys"] == 1
    assert unique["first_timestamp"].startswith("2026-09-11T10:03:00")
    assert unique["complete_outcomes"] == 1
    assert unique["outcome_metric_eligible"] == 0
    assert unique["outcome_not_post_alert_anchored_count"] == 1
    assert unique["median_post_alert_mfe_pct"] is None


def test_alert_linked_observation_timestamp_must_match_original(tmp_path):
    recorder = TopGainerLifecycleRecorder(str(tmp_path / "evidence"))
    _event(
        recorder,
        "AAA",
        "2026-09-11T10:00:00+05:30",
        "Buy Now",
        move=1.0,
        mfe=2.0,
    )
    rows = [json.loads(line) for line in recorder.events_path.read_text().splitlines()]
    alert = next(row for row in rows if row["event_type"] == "alert_emitted")
    alert["observation_timestamp"] = "2026-09-11T09:59:00+05:30"
    recorder.events_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="alert observation timestamp does not match"):
        analyze(recorder.events_path)


def test_timestamps_hours_and_missing_optional_fields_are_stable(synthetic_events):
    result = analyze(synthetic_events)
    unique = result["unique_first_buy_now_per_date_symbol"]

    assert unique["first_timestamp"].startswith("2026-09-11T09:30:00")
    assert unique["last_timestamp"].startswith("2026-09-11T10:40:00")
    assert result["unique_first_buy_now_by_hour"]["09:00-09:59"]["count"] == 1
    assert result["unique_first_buy_now_by_hour"]["10:00-10:59"]["count"] == 3
    ccc = next(row for row in result["per_symbol_first_buy_now"] if row["symbol"] == "CCC")
    assert ccc["first_buy_now_move_pct"] is None
    assert ccc["buy_now_mfe_pct"] is None


def test_cli_writes_separate_output_without_modifying_input(synthetic_events, tmp_path):
    before = synthetic_events.read_bytes()
    output = tmp_path / "analysis.json"

    assert main(["--events", str(synthetic_events), "--output", str(output)]) == 0

    assert synthetic_events.read_bytes() == before
    assert output.is_file()


def test_cli_refuses_to_overwrite_evidence(synthetic_events):
    with pytest.raises(SystemExit):
        main(["--events", str(synthetic_events), "--output", str(synthetic_events)])


def test_repetition_analysis_is_scoped_to_trading_date_and_symbol(tmp_path):
    recorder = TopGainerLifecycleRecorder(str(tmp_path / "evidence"))
    for timestamp in (
        "2026-09-10T09:30:00+05:30",
        "2026-09-10T10:00:00+05:30",
        "2026-09-11T09:31:00+05:30",
        "2026-09-11T09:40:00+05:30",
    ):
        _event(recorder, "AAA", timestamp, "Buy Now", move=1.0)

    repetitions = analyze(recorder.events_path)["buy_now_repetition_analysis"]

    assert [(row["trading_date"], row["symbol"]) for row in repetitions] == [
        ("2026-09-10", "AAA"),
        ("2026-09-11", "AAA"),
    ]
    assert [row["elapsed_minutes"] for row in repetitions] == [30.0, 9.0]

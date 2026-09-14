from __future__ import annotations

import csv
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from pkscreener.classes.NGP5EarlyWatchPublisher import (
    IST,
    NGP5EarlyWatchPublisher,
    P5_MIN_TURNOVER,
    evaluate_p5,
)
from pkscreener.classes.NGPublisherHealth import (
    summarize_publisher_health,
    write_publisher_health_snapshot,
)
from pkscreener.classes.NGTradedValueShadowExperiment import (
    EXPERIMENT_ID,
    NGTradedValueShadowOutcomeFinalizer,
    NGTradedValueShadowRecorder,
    is_sole_traded_value_failure,
)


HEADER = [
    "When", "RecorderGeneration", "Exchange", "InstrumentToken", "Symbol", "Sector",
    "LastPrice", "PrevClose", "Open", "DayHigh", "DayChangePct", "UniverseSize",
    "DayChangeRank", "DayChangeRankPct", "PreviousDayChangeRank", "RankImprovement",
    "RankVelocityPerMinute", "DayChangeDeltaPct", "EnteredTop100", "EnteredTop50",
    "RapidRankClimber", "OpenChangePct", "PullbackFromHighPct", "Volume", "TradedValue",
    "BuyQty", "SellQty", "BuyerSellerRatio", "BuyerSellerMedianWindow",
    "BuyerSellerMinWindow", "OrderBookStable", "BestBid", "BestAsk", "BestBidQty",
    "BestAskQty", "SpreadBps", "SpreadOk", "SectorCount", "SectorAvgChangePct",
    "SectorBreadthPct", "SectorTop2Count", "SectorTop5Count", "MarketBreadthPct",
    "IsTopGainerNow", "AlreadyAlerted", "RelativeVolume", "VolumeAcceleration",
    "TradedValueAcceleration", "RelativeStrengthVsNiftyPct", "VWAP", "OpeningRangeState",
    "IgnitionState",
]


def causal_row(**updates):
    row = {
        "When": "2026-09-14 10:00:30",
        "RecorderGeneration": "6",
        "Exchange": "NSE",
        "InstrumentToken": "123",
        "Symbol": "SHADOW",
        "Sector": "TECH",
        "LastPrice": "100",
        "PrevClose": "98",
        "Open": "99",
        "DayHigh": "101",
        "DayChangePct": "2.04",
        "UniverseSize": "2500",
        "DayChangeRank": "100",
        "DayChangeRankPct": "4",
        "PreviousDayChangeRank": "150",
        "RankImprovement": "50",
        "RankVelocityPerMinute": "5",
        "DayChangeDeltaPct": "0.7",
        "EnteredTop100": "true",
        "EnteredTop50": "false",
        "RapidRankClimber": "true",
        "OpenChangePct": "1.01",
        "PullbackFromHighPct": "0.99",
        "Volume": "200000",
        "TradedValue": "20000000",
        "BuyQty": "1000",
        "SellQty": "500",
        "BuyerSellerRatio": "2",
        "BuyerSellerMedianWindow": "1.8",
        "BuyerSellerMinWindow": "1.5",
        "OrderBookStable": "true",
        "BestBid": "99.95",
        "BestAsk": "100.05",
        "BestBidQty": "200",
        "BestAskQty": "180",
        "SpreadBps": "10",
        "SpreadOk": "true",
        "SectorCount": "20",
        "SectorAvgChangePct": "1.1",
        "SectorBreadthPct": "60",
        "SectorTop2Count": "2",
        "SectorTop5Count": "5",
        "MarketBreadthPct": "55",
        "IsTopGainerNow": "true",
        "AlreadyAlerted": "false",
        "RelativeVolume": "3.2",
        "VolumeAcceleration": "1.4",
        "TradedValueAcceleration": "1.7",
        "RelativeStrengthVsNiftyPct": "1.5",
        "VWAP": "99.5",
        "OpeningRangeState": "above_opening_high",
        "IgnitionState": "accelerating",
    }
    row.update({key: str(value) for key, value in updates.items()})
    return row


def write_feature_file(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def opening_scaffold(day: str):
    rows = []
    current = datetime.fromisoformat(f"{day} 09:15:00")
    end = datetime.fromisoformat(f"{day} 09:30:00")
    while current <= end:
        rows.append(
            causal_row(
                When=current.isoformat(sep=" "),
                Symbol=f"COVERAGE_{current.strftime('%H%M%S')}",
                DayChangePct=0,
                DayChangeRank=201,
                PreviousDayChangeRank=201,
                RankImprovement=0,
                DayChangeDeltaPct=0,
                TradedValue=0,
            )
        )
        current += timedelta(seconds=30)
    return rows


def write_coverage(source_dir: Path, day: str):
    (source_dir / "full_universe_rank_status.json").write_text(
        json.dumps(
            {
                "status": "recording_full_universe_rank",
                "date": day,
                "first_recorded_at": f"{day} 09:15:00",
                "generation_started_at": f"{day} 09:15:00",
                "full_session_eligible": True,
                "recorder_generation": 6,
                "interval_seconds": 15.0,
                "leader_pool_size": 200,
                "leader_pool_recall_pct": 100.0,
            }
        )
    )


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_sole_failure_uses_frozen_production_evaluator():
    row = causal_row()
    actual = evaluate_p5(row, set())
    assert actual.qualifies is False
    assert actual.reason == "turnover below ₹3 crore"
    assert is_sole_traded_value_failure(row, set(), actual) is True
    assert is_sole_traded_value_failure(
        causal_row(DayChangeRank=201), set()
    ) is False
    assert evaluate_p5({**row, "TradedValue": str(P5_MIN_TURNOVER)}, set()).qualifies is True


def test_shadow_observation_is_causal_allowlisted_and_cannot_promote(tmp_path):
    recorder = NGTradedValueShadowRecorder(
        tmp_path,
        clock=lambda: datetime(2026, 9, 14, 10, 0, 31, tzinfo=IST),
    )
    row = causal_row(MFE30="99", FutureHigh="999", outcome_label="winner")
    evaluation = evaluate_p5(row, set())
    assert recorder.observe_p5(row, set(), evaluation=evaluation) is True
    assert recorder.observe_p5(row, set(), evaluation=evaluation) is False
    record = read_jsonl(
        tmp_path / "observations" / "observations_2026-09-14.jsonl"
    )[0]
    assert record["shadow_only"] is True
    assert record["promotion_allowed"] is False
    assert record["production_qualification_result"] is False
    assert record["sole_failing_production_gate"] == "traded_value"
    assert record["signal_features"]["relative_volume"] == 3.2
    assert record["signal_features"]["traded_value_requirement_ratio"] == pytest.approx(2 / 3)
    serialized = json.dumps(record).lower()
    assert "mfe30" not in serialized
    assert "futurehigh" not in serialized
    assert "outcome_label" not in serialized


def test_publisher_production_decisions_are_identical_with_shadow_sink(tmp_path):
    day = "2026-09-14"
    source = tmp_path / "source" / f"live_features_{day}.csv"
    rows = opening_scaffold(day) + [
        causal_row(Symbol="SHADOW_ONLY", When=f"{day} 10:00:30", TradedValue=20_000_000),
        causal_row(Symbol="REAL_P5", When=f"{day} 10:00:45", TradedValue=30_000_000),
    ]
    write_feature_file(source, rows)
    write_coverage(source.parent, day)

    baseline = NGP5EarlyWatchPublisher(source.parent, tmp_path / "baseline")
    recorder = NGTradedValueShadowRecorder(tmp_path / "experiment_4")
    shadowed = NGP5EarlyWatchPublisher(
        source.parent,
        tmp_path / "shadowed",
        shadow_experiment_recorder=recorder,
    )
    assert baseline.process_feature_file(source) == shadowed.process_feature_file(source) == 1
    baseline_observed = baseline.state["sessions"][day]["observed"]
    shadowed_observed = shadowed.state["sessions"][day]["observed"]
    assert set(baseline_observed) == set(shadowed_observed) == {"REAL_P5"}
    assert baseline_observed["REAL_P5"]["policy_inputs"] == shadowed_observed["REAL_P5"]["policy_inputs"]
    assert set(baseline.state["pending"]) == set(shadowed.state["pending"])
    observations = read_jsonl(
        tmp_path
        / "experiment_4"
        / "observations"
        / f"observations_{day}.jsonl"
    )
    assert [item["symbol"] for item in observations] == ["SHADOW_ONLY"]


def test_shadow_recorder_failure_cannot_interrupt_production(tmp_path):
    class BrokenRecorder:
        def observe_p5(self, *_args, **_kwargs):
            raise RuntimeError("shadow disk unavailable")

    day = "2026-09-14"
    source = tmp_path / "source" / f"live_features_{day}.csv"
    write_feature_file(
        source,
        opening_scaffold(day)
        + [causal_row(Symbol="REAL_P5", When=f"{day} 10:00:45", TradedValue=30_000_000)],
    )
    write_coverage(source.parent, day)
    publisher = NGP5EarlyWatchPublisher(
        source.parent,
        tmp_path / "publisher",
        shadow_experiment_recorder=BrokenRecorder(),
    )
    assert publisher.process_feature_file(source) == 1
    assert set(publisher.state["sessions"][day]["observed"]) == {"REAL_P5"}


def test_outcomes_are_written_separately_without_mutating_observations(tmp_path):
    recorder = NGTradedValueShadowRecorder(tmp_path)
    assert recorder.observe_p5(causal_row(), set()) is True
    observation_path = tmp_path / "observations" / "observations_2026-09-14.jsonl"
    observation_before = observation_path.read_bytes()

    minute_dir = tmp_path / "minute"
    minute_dir.mkdir()
    with (minute_dir / "SHADOW.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["datetime", "open", "high", "low", "close", "volume", "symbol"],
        )
        writer.writeheader()
        writer.writerows(
            [
                {"datetime": "2026-09-14 10:00:00", "open": 99.8, "high": 120, "low": 80, "close": 100, "volume": 1, "symbol": "SHADOW"},
                {"datetime": "2026-09-14 10:01:00", "open": 100, "high": 100.6, "low": 99.6, "close": 100.2, "volume": 1, "symbol": "SHADOW"},
                {"datetime": "2026-09-14 10:03:00", "open": 100.2, "high": 102.2, "low": 98.9, "close": 102, "volume": 1, "symbol": "SHADOW"},
                {"datetime": "2026-09-14 10:10:00", "open": 102, "high": 103.5, "low": 98, "close": 103, "volume": 1, "symbol": "SHADOW"},
                {"datetime": "2026-09-14 15:29:00", "open": 103, "high": 105, "low": 97, "close": 104, "volume": 1, "symbol": "SHADOW"},
            ]
        )
    feature_path = tmp_path / "live_features_2026-09-14.csv"
    write_feature_file(
        feature_path,
        [
            causal_row(When="2026-09-14 10:00:30", TradedValue=20_000_000),
            causal_row(When="2026-09-14 10:05:00", LastPrice=102, TradedValue=31_000_000),
        ],
    )

    finalizer = NGTradedValueShadowOutcomeFinalizer(
        tmp_path,
        clock=lambda: datetime(2026, 9, 14, 16, 0, tzinfo=IST),
    )
    assert finalizer.finalize_session(
        "2026-09-14", minute_dir, live_feature_path=feature_path
    ) == 1
    assert finalizer.finalize_session(
        "2026-09-14", minute_dir, live_feature_path=feature_path
    ) == 0
    assert observation_path.read_bytes() == observation_before
    outcome = read_jsonl(tmp_path / "outcomes" / "outcomes_2026-09-14.jsonl")[0]
    assert outcome["record_kind"] == "outcome_label"
    assert outcome["experiment_id"] == EXPERIMENT_ID
    assert outcome["outcome_source"]["first_fully_future_bar"].endswith("10:01:00+05:30")
    assert outcome["excursions"]["mfe_5m_pct"] == pytest.approx(2.2)
    assert outcome["excursions"]["mae_5m_pct"] == pytest.approx(-1.1)
    assert outcome["threshold_attainment"]["plus_2_0_pct"] is True
    assert outcome["threshold_attainment"]["plus_5_0_pct"] is True
    assert outcome["threshold_attainment"]["minus_2_0_pct"] is True
    assert outcome["production_floor_cross"]["timestamp"].endswith("10:05:00+05:30")
    # Move consumed uses the full-session open-to-HOD denominator.  The
    # deliberately extreme 10:00 bar is excluded from post-observation MFE but
    # remains part of the session HOD definition.
    assert outcome["production_floor_cross"]["move_consumed_pct"] == pytest.approx(
        3 / 21 * 100
    )
    assert "excursions" not in json.loads(observation_before.decode())


def test_publisher_health_is_read_only_and_has_required_metrics(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    path = evidence / "events_2026-09-14.jsonl"
    rows = [
        {"event_kind": "p5", "delivery_state": "OBSERVED", "delivery_id": "a"},
        {"event_kind": "p5", "delivery_state": "OBSERVED", "delivery_id": "b"},
        {"event_kind": "p5", "delivery_state": "ATTEMPTED", "delivery_id": "a"},
        {"event_kind": "p5", "delivery_state": "FAILED", "delivery_id": "a", "http_status": 403},
        {"event_kind": "p5", "delivery_state": "ATTEMPTED", "delivery_id": "b"},
        {"event_kind": "p5", "delivery_state": "FAILED", "delivery_id": "b", "http_status": 429},
        {"event_kind": "p5", "delivery_state": "ATTEMPTED", "delivery_id": "b"},
        {"event_kind": "p5", "delivery_state": "FAILED", "delivery_id": "b", "http_status": 503},
        {"event_kind": "p5", "delivery_state": "DELIVERED", "delivery_id": "b"},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    evidence_before = path.read_bytes()
    decision_before = evaluate_p5(causal_row(), set())
    summary = summarize_publisher_health("ng_p5_early_watch", [evidence], "2026-09-14")
    assert summary["qualification_count"] == 2
    assert summary["delivery_attempts"] == 3
    assert summary["successful_deliveries"] == 1
    assert summary["http_403_count"] == 1
    assert summary["http_429_count"] == 1
    assert summary["http_5xx_count"] == 1
    assert summary["delivery_success_rate"] == pytest.approx(1 / 3)
    output = tmp_path / "health" / "health_2026-09-14.json"
    write_publisher_health_snapshot(
        "ng_p5_early_watch", [evidence], "2026-09-14", output
    )
    assert output.exists()
    assert path.read_bytes() == evidence_before
    assert evaluate_p5(causal_row(), set()) == decision_before

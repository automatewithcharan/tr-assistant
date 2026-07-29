import pandas as pd
from pathlib import Path
import subprocess
import sys

from scripts.audit_live_runner_capture_funnel import (
    build_capture_funnel,
    load_predictions,
    summarize_funnel,
)


def test_capture_funnel_cli_imports_from_any_working_directory(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts/audit_live_runner_capture_funnel.py"
    )
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _recall(symbol: str, **values) -> dict:
    row = {
        "Date": "2026-07-28",
        "Symbol": symbol,
        "RunnerThresholdPct": 5.0,
        "FirstCrossTime": "2026-07-28 10:30:00",
        "FullRankRecorderStart": "2026-07-28 09:15:01",
        "FullRankSessionEligible": True,
        "FullRankFeatureSeenBeforeCross": True,
        "FullRankBuyableSeenBeforeCross": True,
        "FullRankBuyableBand0_5To3Seen": True,
    }
    row.update(values)
    return row


def test_capture_funnel_identifies_each_causal_failure_stage():
    recall = pd.DataFrame(
        [
            _recall(
                "OPEN_GAP",
                FirstCrossTime="2026-07-28 09:15:00",
                FullRankFeatureSeenBeforeCross=False,
                FullRankBuyableSeenBeforeCross=False,
                FullRankBuyableBand0_5To3Seen=False,
            ),
            _recall("NOT_SEEN", FullRankFeatureSeenBeforeCross=False),
            _recall("NOT_BUYABLE", FullRankBuyableSeenBeforeCross=False),
            _recall("LATE_BAND", FullRankBuyableBand0_5To3Seen=False),
            _recall("NOT_SCORED"),
            _recall("LOW_SCORE"),
            _recall("UNFILLABLE"),
            _recall("NOT_SELECTED"),
            _recall("SELECTED"),
        ]
    )
    predictions = pd.DataFrame(
        [
            {
                "date": "2026-07-28",
                "when": pd.Timestamp("2026-07-28 09:45"),
                "symbol": symbol,
                "expected_return_pct": score,
                "selected": selected,
            }
            for symbol, score, selected in [
                ("LOW_SCORE", 0.10, False),
                ("UNFILLABLE", 0.30, False),
                ("NOT_SELECTED", 0.25, False),
                ("SELECTED", 0.40, True),
            ]
        ]
    )
    registrations = pd.DataFrame(
        [
            {
                "Date": "2026-07-28",
                "EntryAt": pd.Timestamp("2026-07-28 09:45"),
                "Symbol": symbol,
                "ExpectedReturnPct": score,
                "EntryTopLevelFillable": fillable,
            }
            for symbol, score, fillable in [
                ("UNFILLABLE", 0.30, False),
                ("NOT_SELECTED", 0.25, True),
                ("SELECTED", 0.40, True),
            ]
        ]
    )

    funnel = build_capture_funnel(
        recall, predictions, registrations, score_threshold=0.20
    ).set_index("Symbol")

    assert funnel.loc["OPEN_GAP", "FirstFailedStage"] == (
        "uncatchable_before_recorder"
    )
    assert funnel.loc["NOT_SEEN", "FirstFailedStage"] == (
        "recorder_visibility_miss"
    )
    assert funnel.loc["NOT_BUYABLE", "FirstFailedStage"] == (
        "not_contemporaneously_buyable"
    )
    assert funnel.loc["LATE_BAND", "FirstFailedStage"] == (
        "buyable_only_after_early_band"
    )
    assert funnel.loc["NOT_SCORED", "FirstFailedStage"] == (
        "frozen_model_not_scored"
    )
    assert funnel.loc["LOW_SCORE", "FirstFailedStage"] == (
        "frozen_score_below_threshold"
    )
    assert funnel.loc["UNFILLABLE", "FirstFailedStage"] == (
        "top_level_quantity_unfillable"
    )
    assert funnel.loc["NOT_SELECTED", "FirstFailedStage"] == (
        "qualified_not_selected"
    )
    assert funnel.loc["SELECTED", "FirstFailedStage"] == (
        "selected_before_runner_cross"
    )


def test_predictions_after_runner_cross_cannot_count_as_early_capture():
    recall = pd.DataFrame([_recall("LATE_MODEL")])
    predictions = pd.DataFrame(
        [
            {
                "date": "2026-07-28",
                "when": pd.Timestamp("2026-07-28 10:31"),
                "symbol": "LATE_MODEL",
                "expected_return_pct": 0.90,
                "selected": True,
            }
        ]
    )

    result = build_capture_funnel(
        recall, predictions, pd.DataFrame()
    ).iloc[0]

    assert not bool(result["FrozenModelScoredBeforeCross"])
    assert not bool(result["SelectedBeforeCross"])
    assert result["FirstFailedStage"] == "frozen_model_not_scored"


def test_broad_first_observation_lane_gets_credit_without_frozen_score():
    recall = pd.DataFrame([_recall("FAST_BURST")])
    broad = pd.DataFrame(
        [
            {
                "Date": "2026-07-28",
                "EntryAt": pd.Timestamp("2026-07-28 09:30"),
                "Symbol": "FAST_BURST",
                "EntryTopLevelFillable": True,
                "RapidRankClimber": True,
            }
        ]
    )

    result = build_capture_funnel(
        recall,
        pd.DataFrame(),
        pd.DataFrame(),
        broad,
    ).iloc[0]

    assert bool(result["BroadExactRegistrationBeforeCross"])
    assert bool(result["BroadTopLevelQuantityFillableBeforeCross"])
    assert bool(result["BroadPredeclaredPolicyCoveredBeforeCross"])
    assert "rapid_rank_climber" in result["BroadEligibleFeatureGates"]
    assert (
        "rapid_climber_diversified"
        in result["BroadEligibleIntegratedPolicies"]
    )
    assert result["FirstFailedStage"] == (
        "broad_lane_only_awaiting_selector"
    )


def test_broad_candidate_without_declared_policy_is_not_called_selectable():
    recall = pd.DataFrame([_recall("UNCOVERED")])
    broad = pd.DataFrame(
        [
            {
                "Date": "2026-07-28",
                "EntryAt": pd.Timestamp("2026-07-28 09:30"),
                "Symbol": "UNCOVERED",
                "EntryTopLevelFillable": True,
                "DayChangeRank": 500,
                "DayChangePct": 4.5,
                "EntrySpreadBps": 18.0,
            }
        ]
    )

    result = build_capture_funnel(
        recall,
        pd.DataFrame(),
        pd.DataFrame(),
        broad,
    ).iloc[0]

    assert not bool(result["BroadPredeclaredPolicyCoveredBeforeCross"])
    assert result["BroadEligibleIntegratedPolicies"] == ""
    assert result["FirstFailedStage"] == (
        "broad_lane_not_covered_by_policy_registry"
    )


def test_summary_uses_only_catchable_events_as_rate_denominator():
    funnel = build_capture_funnel(
        pd.DataFrame(
            [
                _recall(
                    "OPEN_GAP",
                    FirstCrossTime="2026-07-28 09:15:00",
                    FullRankFeatureSeenBeforeCross=False,
                    FullRankBuyableSeenBeforeCross=False,
                    FullRankBuyableBand0_5To3Seen=False,
                ),
                _recall("VISIBLE"),
                _recall("MISS", FullRankFeatureSeenBeforeCross=False),
            ]
        ),
        pd.DataFrame(),
        pd.DataFrame(),
    )

    summary = summarize_funnel(funnel).iloc[0]

    assert summary["RunnerEvents"] == 3
    assert summary["CatchableRunnerEvents"] == 2
    assert summary["RecorderSeenPctOfCatchable"] == 50.0
    assert summary["BuyableEarlyBandPctOfCatchable"] == 50.0
    assert summary["BroadPolicyCoveredPctOfCatchable"] == 0.0
    assert not bool(summary["CanPromoteStrategy"])
    assert not bool(summary["RealOrdersAllowed"])


def test_prediction_loader_normalizes_aware_timestamp_to_market_clock(
    tmp_path,
):
    pd.DataFrame(
        [
            {
                "date": "2026-07-28",
                "when": "2026-07-28T09:45:00+05:30",
                "symbol": "EARLY",
                "expected_return_pct": 0.3,
                "selected": False,
            }
        ]
    ).to_csv(tmp_path / "predictions.csv", index=False)

    loaded = load_predictions([str(tmp_path / "predictions.csv")])

    assert loaded.iloc[0]["when"] == pd.Timestamp("2026-07-28 09:45:00")
    assert loaded["when"].dt.tz is None


def test_prediction_loader_preserves_naive_market_clock(tmp_path):
    pd.DataFrame(
        [
            {
                "date": "2026-07-28",
                "when": "2026-07-28 09:45:00",
                "symbol": "EARLY",
                "expected_return_pct": 0.3,
                "selected": False,
            }
        ]
    ).to_csv(tmp_path / "predictions.csv", index=False)

    loaded = load_predictions([str(tmp_path / "predictions.csv")])

    assert loaded.iloc[0]["when"] == pd.Timestamp("2026-07-28 09:45:00")

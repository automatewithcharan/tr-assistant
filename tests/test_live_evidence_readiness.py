from pathlib import Path

import pandas as pd

from scripts.assess_live_evidence_readiness import (
    _max_drawdown_pct,
    assess,
    first_batch_daily,
    load_outcomes,
)


def test_max_drawdown_includes_loss_from_starting_capital():
    drawdown = _max_drawdown_pct(pd.Series([-1.0, 0.5]))
    assert round(drawdown, 4) == -1.0


def test_first_batch_ignores_later_hindsight_winner():
    bucket = pd.DataFrame(
        [
            {
                "Date": "2026-07-01",
                "When": pd.Timestamp("2026-07-01 09:30:00"),
                "OutcomeAvailable": True,
                "ret": -0.5,
            },
            {
                "Date": "2026-07-01",
                "When": pd.Timestamp("2026-07-01 10:30:00"),
                "OutcomeAvailable": True,
                "ret": 5.0,
            },
        ]
    )
    daily, trade_sessions, candidates = first_batch_daily(
        bucket, ["2026-07-01", "2026-07-02"], "ret"
    )
    assert daily.loc["2026-07-01"] == -0.5
    assert daily.loc["2026-07-02"] == 0.0
    assert trade_sessions == 1
    assert candidates == 1


def test_robust_forward_bucket_can_only_become_shadow_candidate():
    dates = pd.bdate_range("2026-01-01", periods=24).strftime("%Y-%m-%d").tolist()
    rows = []
    for date in dates:
        for index in range(5):
            rows.append(
                {
                    "Date": date,
                    "When": pd.Timestamp(f"{date} 09:30:00"),
                    "Symbol": f"S{index}",
                    "FeatureBucket": "missed_runner",
                    "EvaluationEligible": True,
                    "OutcomeAvailable": True,
                    "ForwardReturnPct": 0.3,
                }
            )
    report = assess(pd.DataFrame(rows), stress_cost_pct=0.1)
    row = report[report["Contract"].eq("forward_horizon")].iloc[0]
    assert bool(row["SampleGatePassed"])
    assert bool(row["PerformanceGatePassed"])
    assert bool(row["ShadowCandidate"])
    assert not bool(row["GoalEvidencePassed"])
    assert row["RequiredDailyReturnPct"] == 2.5
    assert row["ProjectedStressDailyPnl"] == 800.0
    assert not bool(row["EvidenceAccepted"])
    assert not bool(row["PaperPromotionAllowed"])
    assert not bool(row["RealOrdersAllowed"])


def test_goal_evidence_still_cannot_enable_trading():
    dates = pd.bdate_range("2026-01-01", periods=24).strftime("%Y-%m-%d").tolist()
    rows = []
    for date in dates:
        for index in range(5):
            rows.append(
                {
                    "Date": date,
                    "When": pd.Timestamp(f"{date} 09:30:{index:02d}"),
                    "Symbol": f"S{index}",
                    "FeatureBucket": "missed_runner",
                    "EvaluationEligible": True,
                    "OutcomeAvailable": True,
                    "ForwardReturnPct": 3.0,
                }
            )
    report = assess(pd.DataFrame(rows), stress_cost_pct=0.1)
    row = report[report["Contract"].eq("forward_horizon")].iloc[0]
    assert bool(row["GoalEvidencePassed"])
    assert not bool(row["EvidenceAccepted"])
    assert not bool(row["PaperPromotionAllowed"])
    assert not bool(row["RealOrdersAllowed"])


def test_missing_coverage_blocks_shadow_candidate():
    dates = pd.bdate_range("2026-01-01", periods=24).strftime("%Y-%m-%d").tolist()
    rows = []
    for date in dates:
        for index in range(5):
            rows.append(
                {
                    "Date": date,
                    "When": pd.Timestamp(f"{date} 09:30:00"),
                    "Symbol": f"S{index}",
                    "FeatureBucket": "missed_runner",
                    "EvaluationEligible": True,
                    "OutcomeAvailable": index == 0,
                    "ForwardReturnPct": 0.3,
                }
            )
    report = assess(pd.DataFrame(rows))
    row = report[report["Contract"].eq("forward_horizon")].iloc[0]
    assert row["CoveragePct"] == 20.0
    assert not bool(row["SampleGatePassed"])
    assert not bool(row["ShadowCandidate"])


def test_missing_early_batch_cannot_be_replaced_by_later_available_signal():
    bucket = pd.DataFrame(
        [
            {
                "Date": "2026-07-01",
                "When": pd.Timestamp("2026-07-01 09:30:00"),
                "EvaluationEligible": True,
                "OutcomeAvailable": False,
                "ret": float("nan"),
            },
            {
                "Date": "2026-07-01",
                "When": pd.Timestamp("2026-07-01 10:30:00"),
                "EvaluationEligible": True,
                "OutcomeAvailable": True,
                "ret": 5.0,
            },
        ]
    )
    daily, trade_sessions, candidates = first_batch_daily(
        bucket, ["2026-07-01"], "ret"
    )
    assert daily.loc["2026-07-01"] == 0.0
    assert trade_sessions == 0
    assert candidates == 0


def test_csv_false_strings_are_parsed_as_false(tmp_path: Path):
    path = tmp_path / "outcomes.csv"
    pd.DataFrame(
        [
            {
                "Date": "2026-07-01",
                "When": "2026-07-01 09:30:00",
                "Symbol": "ABC",
                "FeatureBucket": "candidate",
                "EvaluationEligible": "False",
                "OutcomeAvailable": "False",
                "ForwardReturnPct": 5.0,
            }
        ]
    ).to_csv(path, index=False)
    outcomes = load_outcomes([str(path)])
    assert not bool(outcomes.iloc[0]["EvaluationEligible"])
    assert not bool(outcomes.iloc[0]["OutcomeAvailable"])


def test_nested_wildcard_inputs_are_loaded(tmp_path: Path):
    path = tmp_path / "eod" / "2026-07-01" / "live_features_rows.csv"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "Date": "2026-07-01",
                "When": "2026-07-01 09:30:00",
                "Symbol": "ABC",
                "FeatureBucket": "missed_runner",
                "EvaluationEligible": True,
                "OutcomeAvailable": True,
                "ForwardReturnPct": 0.5,
            }
        ]
    ).to_csv(path, index=False)
    pattern = str(tmp_path / "eod" / "*" / "live_features_rows.csv")
    outcomes = load_outcomes([pattern])
    assert len(outcomes) == 1
    assert outcomes.iloc[0]["Symbol"] == "ABC"


def test_profitable_reject_bucket_cannot_become_shadow_candidate():
    dates = pd.bdate_range("2026-01-01", periods=24).strftime("%Y-%m-%d").tolist()
    rows = []
    for date in dates:
        for index in range(5):
            rows.append(
                {
                    "Date": date,
                    "When": pd.Timestamp(f"{date} 09:30:00"),
                    "Symbol": f"S{index}",
                    "FeatureBucket": "leader_ignition|ignition_watch|reject|too late",
                    "EvaluationEligible": True,
                    "OutcomeAvailable": True,
                    "ForwardReturnPct": 3.0,
                }
            )
    row = assess(pd.DataFrame(rows)).iloc[0]
    assert not bool(row["StrategyEligibleBucket"])
    assert not bool(row["SampleGatePassed"])
    assert not bool(row["ShadowCandidate"])

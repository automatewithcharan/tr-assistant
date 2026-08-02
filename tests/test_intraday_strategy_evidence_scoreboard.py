import json
from pathlib import Path

import pandas as pd

from scripts.build_intraday_strategy_evidence_scoreboard import write_scoreboard


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_scoreboard_does_not_rank_partial_top_book_as_executable(tmp_path):
    data = tmp_path / "Data"
    _json(
        data / "full_universe_rank_capital_allocation/status.json",
        {
            "status": "collecting_capital_allocation_selection_sessions",
            "capital": 500_000,
            "best_partial_diagnostic": {
                "selector": "acceleration_first",
                "contract": "t2_0_s1_0_h60",
                "slots": 1,
                "sessions": 5,
                "trades": 38,
                "avg_daily_pnl": 12_000,
                "lower_95_pct": 1.0,
                "worst_session_pct": -1.0,
            },
        },
    )

    scoreboard = write_scoreboard(data, tmp_path / "out")
    row = scoreboard[
        scoreboard["Lane"].eq("Full-universe capital allocation")
    ].iloc[0]

    assert row["StressAvgDailyPnl"] == 12_000
    assert bool(row["DiagnosticOnly"])
    assert not bool(row["CapitalAllocationComparable"])
    status = json.loads((tmp_path / "out/status.json").read_text())
    assert status["capital_allocation_comparable_lanes"] == 0
    assert not status["real_orders_allowed"]


def test_only_accepted_five_level_forward_is_capital_comparable(tmp_path):
    data = tmp_path / "Data"
    _json(
        data / "full_universe_rank_depth_execution/status.json",
        {
            "status": "accepted_depth_execution_forward",
            "execution_schema_version": 2,
            "capital": 500_000,
            "accepted": True,
            "paper_promotion_allowed": True,
            "real_orders_allowed": False,
            "forward": {
                "sessions": 20,
                "trades": 60,
                "avg_daily_pnl": 11_000,
                "lower_95_pct": 0.2,
                "positive_session_pct": 70,
                "goal_hit_pct": 55,
                "worst_session_pct": -2.0,
            },
        },
    )
    ranked_path = data / "ranked_confirmation_replay/readiness.csv"
    ranked_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [{
            "ConfigName": "misleading",
            "CompleteDays": 30,
            "ClosedTrades": 100,
            "StressAvgDailyPnl": 50_000,
            "StressLower95DailyPnl": 20_000,
            "PositiveDayPct": 90,
            "GoalHitPct": 90,
            "WorstDayPnl": -100,
            "ShadowCandidate": True,
            "PaperPromotionAllowed": True,
            "RealOrdersAllowed": False,
        }]
    ).to_csv(ranked_path, index=False)

    scoreboard = write_scoreboard(data, tmp_path / "out")
    comparable = scoreboard[
        scoreboard["CapitalAllocationComparable"].astype(bool)
    ]

    assert comparable["Lane"].tolist() == ["Five-level full-universe rank"]
    status = json.loads((tmp_path / "out/status.json").read_text())
    assert status["goal_evidence_passes"] == 1
    assert status["best_comparable_lane"] == "Five-level full-universe rank"
    assert not status["real_orders_allowed"]


def test_rank_migration_forward_is_visible_but_not_depth_comparable(tmp_path):
    data = tmp_path / "Data"
    _json(
        data / "live_rank_migration/status.json",
        {
            "status": "collecting_forward_evidence",
            "capital": 500_000,
            "accepted": False,
            "paper_promotion_allowed": False,
            "real_orders_allowed": False,
            "forward_stress": {
                "sessions": 5,
                "trades": 21,
                "avg_daily_pnl": 3_403.59,
                "lower_95_pct": -1.210176,
                "positive_session_pct": 40,
                "goal_hit_pct": 20,
                "worst_session_pct": -0.629079,
            },
        },
    )

    scoreboard = write_scoreboard(data, tmp_path / "out")
    row = scoreboard[scoreboard["Lane"].eq("Frozen rank migration")].iloc[0]

    assert row["StressAvgDailyPnl"] == 3_403.59
    assert row["StressLower95DailyPnl"] == -6_050.88
    assert bool(row["MeasuredEvidence"])
    assert bool(row["DiagnosticOnly"])
    assert not bool(row["CapitalAllocationComparable"])


def test_predeclared_capacity_policies_are_visible_but_not_comparable(tmp_path):
    data = tmp_path / "Data"
    _json(
        data / "predeclared_capacity_challenger/status.json",
        {
            "status": "collecting_predeclared_capacity_forward_sessions",
            "capital": 500_000,
            "policy_summaries": [
                {
                    "policy": "opening_ranked_t0_8_s0_5",
                    "gate": "opening_ranked",
                    "selector": "rank_first",
                    "contract": "t0_8_s0_5_h30",
                    "slots": 3,
                    "execution_complete_sessions": 4,
                    "trades": 12,
                    "avg_daily_pnl": 1_250,
                    "lower_95_pct": -0.4,
                    "positive_session_pct": 50,
                    "goal_hit_pct": 0,
                    "worst_session_pct": -1.2,
                }
            ],
            "paper_only": True,
            "alerts_allowed": False,
            "paper_promotion_allowed": False,
            "real_orders_allowed": False,
        },
    )

    scoreboard = write_scoreboard(data, tmp_path / "out")
    rows = scoreboard[
        scoreboard["Lane"].eq("Predeclared capacity challenger")
    ]
    assert len(rows) == 11
    row = rows[rows["Variant"].str.startswith("opening_ranked_t0_8_s0_5")].iloc[0]
    assert row["StressAvgDailyPnl"] == 1_250
    assert row["StressLower95DailyPnl"] == -2_000.0
    assert bool(row["MeasuredEvidence"])
    assert bool(row["DiagnosticOnly"])
    assert not bool(row["CapitalAllocationComparable"])
    assert not bool(row["PaperPromotionAllowed"])
    assert not bool(row["RealOrdersAllowed"])


def test_accepted_rank_migration_depth_forward_becomes_comparable(tmp_path):
    data = tmp_path / "Data"
    _json(
        data / "frozen_rank_migration_depth_forward/status.json",
        {
            "status": "accepted_untouched_depth_forward",
            "contract": "t1_5_s1_0_h60",
            "execution_schema_version": 2,
            "capital": 500_000,
            "accepted": True,
            "paper_promotion_allowed": True,
            "real_orders_allowed": False,
            "forward": {
                "sessions": 20,
                "trades": 60,
                "avg_daily_pnl": 10_500,
                "lower_95_pct": 0.1,
                "positive_session_pct": 70,
                "goal_hit_pct": 55,
                "worst_session_pct": -1.0,
            },
        },
    )

    scoreboard = write_scoreboard(data, tmp_path / "out")
    row = scoreboard[
        scoreboard["Lane"].eq("Five-level frozen rank migration")
    ].iloc[0]

    assert bool(row["CapitalAllocationComparable"])
    assert row["StressLower95DailyPnl"] == 500.0
    assert not bool(row["RealOrdersAllowed"])


def test_accepted_balanced_stop_portfolio_becomes_comparable(tmp_path):
    data = tmp_path / "Data"
    _json(
        data / "frozen_balanced_stop_portfolio_forward/status.json",
        {
            "status": "accepted_untouched_depth_forward",
            "contract": "t1_5_s1_0_h60",
            "execution_schema_version": 2,
            "capital": 500_000,
            "accepted": True,
            "paper_promotion_allowed": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
            "forward": {
                "sessions": 20,
                "trades": 80,
                "avg_daily_pnl": 10_750,
                "lower_95_pct": 0.15,
                "positive_session_pct": 75,
                "goal_hit_pct": 55,
                "worst_session_pct": -1.25,
            },
        },
    )

    scoreboard = write_scoreboard(data, tmp_path / "out")
    row = scoreboard[
        scoreboard["Lane"].eq("Five-level balanced stop portfolio")
    ].iloc[0]

    assert bool(row["CapitalAllocationComparable"])
    assert row["StressAvgDailyPnl"] == 10_750
    assert row["StressLower95DailyPnl"] == 750.0
    assert not bool(row["RealOrdersAllowed"])


def test_accepted_primary_post_margin_portfolio_becomes_comparable(
    tmp_path,
):
    data = tmp_path / "Data"
    _json(
        data / "frozen_balanced_stop_margin_forward/status.json",
        {
            "status": "accepted_primary_1_50x_post_margin_forward",
            "contract": "t1_5_s1_0_h60",
            "execution_schema_version": 2,
            "capital": 500_000,
            "primary_multiplier": 1.5,
            "accepted": True,
            "paper_promotion_allowed": True,
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
            "forward": {
                "sessions": 20,
                "trades": 80,
                "avg_daily_pnl": 10_250,
                "lower_95_pct": 0.12,
                "positive_session_pct": 75,
                "goal_hit_pct": 55,
                "worst_session_pct": -1.25,
            },
        },
    )

    scoreboard = write_scoreboard(data, tmp_path / "out")
    row = scoreboard[
        scoreboard["Lane"].eq(
            "Post-margin balanced stop portfolio 1.50x"
        )
    ].iloc[0]

    assert bool(row["CapitalAllocationComparable"])
    assert row["StressAvgDailyPnl"] == 10_250
    assert row["StressLower95DailyPnl"] == 600.0
    assert not bool(row["RealOrdersAllowed"])


def test_historical_rank_displacement_remains_diagnostic(tmp_path):
    data = tmp_path / "Data"
    _json(
        data / "historical_rank_displacement/status.json",
        {
            "status": "supports_frozen_forward_collection",
            "policy_version": "rank_displacement_v1_20260729",
            "policy": {"capital": 500_000},
            "displacement": {
                "sessions": 9,
                "trades": 45,
                "avg_daily_pnl": 7_287.52,
                "lower_95_pct": 0.73,
                "positive_session_pct": 100,
                "goal_hit_pct": 33.33,
                "worst_session_pct": 0.40,
            },
            "paper_promotion_allowed": False,
            "real_orders_allowed": False,
        },
    )

    scoreboard = write_scoreboard(data, tmp_path / "out")
    row = scoreboard[
        scoreboard["Lane"].eq("Historical rank displacement")
    ].iloc[0]

    assert bool(row["MeasuredEvidence"])
    assert bool(row["DiagnosticOnly"])
    assert not bool(row["Accepted"])
    assert not bool(row["PaperPromotionAllowed"])
    assert not bool(row["RealOrdersAllowed"])

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

from pathlib import Path

import pandas as pd

from scripts import dashboard


def test_money_formats_negative_rupees_conventionally():
    assert dashboard.money(-840) == "-₹840"
    assert dashboard.money(1250) == "₹1,250"


def test_ranked_confirmation_stats_reports_losses_and_pause(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "ranked_confirmation_paper"
    root.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "date": "2026-07-13",
                "symbol": "AAA",
                "closed": True,
                "net_pnl": -400.0,
                "exit_reason": "stop_hit",
            },
            {
                "date": "2026-07-13",
                "symbol": "BBB",
                "closed": "True",
                "net_pnl": -300.0,
                "exit_reason": "stop_hit",
            },
        ]
    ).to_csv(root / "trades_latest.csv", index=False)
    pd.DataFrame([{"symbol": "AAA"}, {"symbol": "BBB"}]).to_csv(
        root / "candidates_latest.csv", index=False
    )

    stats = dashboard.ranked_confirmation_stats()

    assert stats["closed"] == 2
    assert stats["net_pnl"] == -700.0
    assert stats["stops"] == 2
    assert stats["entry_paused"]
    assert stats["forward_closed"] == 2
    assert stats["forward_wins"] == 0
    assert stats["forward_net_pnl"] == -700.0


def test_ranked_confirmation_stats_accumulates_daily_ledgers_without_latest_duplicate(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "ranked_confirmation_paper"
    root.mkdir(parents=True)
    day_one = pd.DataFrame(
        [{
            "date": "2026-07-13", "symbol": "AAA", "entry_time": "09:30",
            "closed": True, "net_pnl": -400.0, "exit_reason": "stop_hit",
        }]
    )
    day_two = pd.DataFrame(
        [{
            "date": "2026-07-14", "symbol": "BBB", "entry_time": "10:00",
            "closed": True, "net_pnl": 250.0, "exit_reason": "target_hit",
        }]
    )
    day_one.to_csv(root / "trades_2026-07-13.csv", index=False)
    day_two.to_csv(root / "trades_2026-07-14.csv", index=False)
    day_two.to_csv(root / "trades_latest.csv", index=False)
    pd.DataFrame().to_csv(root / "candidates_latest.csv", index=False)

    stats = dashboard.ranked_confirmation_stats()

    assert stats["forward_days"] == 2
    assert stats["forward_closed"] == 2
    assert stats["forward_wins"] == 1
    assert stats["forward_net_pnl"] == -150.0


def test_evidence_readiness_stats_uses_best_stressed_candidate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "live_feature_recorder"
    root.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "FeatureBucket": "missed_runner",
                "Contract": "t1_0_s0_5",
                "StrategyEligibleBucket": True,
                "Sessions": 5,
                "CoveragePct": 91.3,
                "StressAvgDailyReturnPct": -0.65,
                "ProjectedStressDailyPnl": -2600.0,
                "StressReturnLower95Pct": -0.9,
                "ShadowCandidate": False,
                "GoalEvidencePassed": False,
            },
            {
                "FeatureBucket": "missed_stable_tight_sector",
                "Contract": "forward_horizon",
                "StrategyEligibleBucket": "True",
                "Sessions": 5,
                "CoveragePct": 87.5,
                "StressAvgDailyReturnPct": -0.0374,
                "ProjectedStressDailyPnl": -149.59,
                "StressReturnLower95Pct": -0.3859,
                "ShadowCandidate": False,
                "GoalEvidencePassed": False,
            },
        ]
    ).to_csv(root / "evidence_readiness.csv", index=False)
    status_root = root / "bootstrap" / "2026-07-06_2026-07-10"
    status_root.mkdir(parents=True)
    (status_root / "status.json").write_text(
        '{"end_date":"2026-07-10","combined_coverage_pct":72.7,'
        '"coverage_gate_passed":false}',
        encoding="utf-8",
    )

    stats = dashboard.evidence_readiness_stats()

    assert stats["sessions"] == 5
    assert stats["coverage"] == 72.7
    assert stats["best_bucket"] == "missed_stable_tight_sector"
    assert stats["best_stress_pnl"] == -149.59
    assert stats["shadow_candidates"] == 0
    assert stats["goal_passes"] == 0


def test_ranked_replay_readiness_reports_accumulated_rejection(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "ranked_confirmation_replay"
    root.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "ConfigName": "baseline_current",
                "CompleteDays": 5,
                "ClosedTrades": 7,
                "Wins": 0,
                "StressAvgDailyPnl": -650.0,
                "StressLower95DailyPnl": -900.0,
                "ProfitFactor": 0.0,
                "ShadowCandidate": False,
                "GoalEvidencePassed": False,
                "PaperPromotionAllowed": False,
                "RealOrdersAllowed": False,
            },
            {
                "ConfigName": "patient_strong",
                "CompleteDays": 5,
                "ClosedTrades": 2,
                "Wins": 1,
                "StressAvgDailyPnl": -20.0,
                "StressLower95DailyPnl": -200.0,
                "ProfitFactor": 0.8,
                "ShadowCandidate": "False",
                "GoalEvidencePassed": "False",
                "PaperPromotionAllowed": "False",
                "RealOrdersAllowed": "False",
            },
        ]
    ).to_csv(root / "readiness.csv", index=False)

    stats = dashboard.ranked_replay_readiness_stats()

    assert stats["complete_days"] == 5
    assert stats["best_config"] == "patient_strong"
    assert stats["stress_avg_daily_pnl"] == -20.0
    assert stats["shadow_candidates"] == 0
    assert not stats["paper_promotion_allowed"]
    assert not stats["real_orders_allowed"]

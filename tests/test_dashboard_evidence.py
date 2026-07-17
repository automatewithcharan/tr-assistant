import json
from pathlib import Path

import pandas as pd

from scripts import dashboard


def test_money_formats_negative_rupees_conventionally():
    assert dashboard.money(-840) == "-₹840"
    assert dashboard.money(1250) == "₹1,250"


def test_broad_rank_checkpoint_stats_distinguishes_diagnostic_and_full(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    first = tmp_path / "broad_rank_acceleration" / "checkpoint_1000"
    first.mkdir(parents=True)
    (first / "status.json").write_text(
        json.dumps(
            {
                "milestone": 1000,
                "diagnostic_only": True,
                "broad_coverage_gate_passed": False,
                "historical_goal_gate_passed": False,
            }
        ),
        encoding="utf-8",
    )
    diagnostic = dashboard.broad_rank_checkpoint_stats()
    assert diagnostic["latest_milestone"] == 1000
    assert diagnostic["next_milestone"] == 2000
    assert diagnostic["diagnostic_only"]
    assert not diagnostic["historical_goal_gate_passed"]

    final = tmp_path / "broad_rank_acceleration" / "checkpoint_2900"
    final.mkdir(parents=True)
    (final / "status.json").write_text(
        json.dumps(
            {
                "milestone": 2900,
                "diagnostic_only": False,
                "broad_coverage_gate_passed": True,
                "historical_goal_gate_passed": True,
            }
        ),
        encoding="utf-8",
    )
    complete = dashboard.broad_rank_checkpoint_stats()
    assert complete["latest_milestone"] == 2900
    assert complete["next_milestone"] == 2000
    assert complete["coverage_gate_passed"]
    assert complete["historical_goal_gate_passed"]
    assert not complete["paper_promotion_allowed"]
    assert not complete["real_orders_allowed"]


def test_eventual_runner_classifier_stats_reads_stressed_final_only(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "eventual_runner_current_505"
    root.mkdir()
    (root / "classifier_status.json").write_text(
        json.dumps(
            {
                "strategy": "walk_forward_eventual_10pct_runner_classifier",
                "capital_reuse": "exact_exit_bar_unleveraged",
                "final_goal_contract_passed": False,
                "selected": {
                    "selection_gate_passed": True,
                    "stress_final_avg_daily_pnl": 1250.0,
                    "stress_final_mean_lower_95_pnl": -450.0,
                    "stress_final_goal_hit_pct": 4.0,
                    "threshold": 0.8,
                    "slots": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    stats = dashboard.eventual_runner_classifier_stats()
    assert stats["available"]
    assert stats["selection_gate_passed"]
    assert stats["stress_final_avg_pnl"] == 1250.0
    assert stats["stress_final_lower95_pnl"] == -450.0
    assert not stats["final_goal_contract_passed"]
    assert not stats["paper_promotion_allowed"]
    assert not stats["real_orders_allowed"]


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


def test_event_shadow_readiness_reports_stressed_baseline(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "intraday_event_shadow_rebuilt" / "model_v1"
    root.mkdir(parents=True)
    (root / "summary.json").write_text(
        json.dumps(
            {
                "sessions": 4,
                "events": 9056,
                "status": "insufficient_forward_evidence",
                "frozen_heuristic_baseline": {
                    "trades": 4,
                    "stress_avg_daily_return_pct": 0.625,
                    "stress_lower95_daily_return_pct": -0.403125,
                    "stress_avg_daily_pnl": 2500.0,
                    "positive_session_pct": 75.0,
                    "daily_goal_hit_pct": 0.0,
                    "sample_gate_passed": False,
                    "performance_gate_passed": False,
                },
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.event_shadow_readiness_stats()

    assert stats["sessions"] == 4
    assert stats["events"] == 9056
    assert stats["stress_avg_pnl"] == 2500.0
    assert stats["stress_lower95"] == -0.403125
    assert not stats["paper_promotion_allowed"]
    assert not stats["real_orders_allowed"]


def test_historical_transfer_reports_strict_future_holdout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "intraday_event_shadow_rebuilt" / "historical_transfer"
    root.mkdir(parents=True)
    (root / "holdout_summary.json").write_text(
        json.dumps(
            {
                "sessions": 4,
                "scored_live_candidates": 9056,
                "traded_sessions": 4,
                "avg_stressed_return_pct": 0.0,
                "avg_stressed_pnl": 0.0,
                "total_stressed_pnl": 0.0,
                "lower_95_pct": -2.296733,
                "positive_session_pct": 50.0,
                "goal_hit_pct": 0.0,
                "paper_promotion_allowed": False,
                "temporal_boundary": {"historical_end": "2026-05-29", "live_end": "2026-07-16"},
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.historical_transfer_stats()

    assert stats["sessions"] == 4
    assert stats["candidates"] == 9056
    assert stats["avg_pnl"] == 0.0
    assert stats["lower95"] == -2.296733
    assert not stats["paper_promotion_allowed"]
    assert not stats["real_orders_allowed"]


def test_event_exit_contracts_keep_exploratory_and_forward_separate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "intraday_event_shadow_rebuilt" / "exit_contracts"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps({"config_count": 48, "sessions": 4, "forward_sessions": 0, "paper_candidates": 0}),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "config": "t5_s0.5_n1",
                "exploratory_avg_daily_pnl": 3767.09,
                "exploratory_lower_95_pct": -3.208355,
                "forward_avg_daily_pnl": 0.0,
                "forward_lower_95_pct": "",
                "paper_candidate": False,
            }
        ]
    ).to_csv(root / "readiness.csv", index=False)

    stats = dashboard.event_exit_contract_stats()

    assert stats["configs"] == 48
    assert stats["forward_sessions"] == 0
    assert stats["exploratory_best"] == "t5_s0.5_n1"
    assert stats["exploratory_avg_pnl"] == 3767.09
    assert not stats["paper_promotion_allowed"]
    assert not stats["real_orders_allowed"]


def test_frozen_microstructure_and_backfill_status(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    recorder = tmp_path / "live_feature_recorder"
    recorder.mkdir(parents=True)
    (recorder / "frozen_microstructure_selector_status.json").write_text(
        json.dumps(
            {
                "selector": "missed_stable_executable_known_sector",
                "contract": "forward_horizon",
                "forward_start": "2026-07-17",
                "selection": {"avg_daily_pnl": 647.72},
                "forward": {"sessions": 3, "candidates": 5},
                "forward_stress": {"avg_daily_pnl": 120.0, "lower_95_pct": -0.4},
                "minimum_forward_sessions": 20,
                "selection_rejected": True,
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )
    ignition = tmp_path / "live_microstructure_ignition"
    ignition.mkdir()
    (ignition / "status.json").write_text(
        json.dumps(
            {
                "selection_end": "2026-07-16",
                "forward_start": "2026-07-17",
                "selection_sessions": 9,
                "forward_sessions": 1,
                "frozen_configs": [],
                "selection_rejected": True,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "broad_universe_5minute_backfill_summary.json").write_text(
        json.dumps(
            {
                "universe_symbols": 2990,
                "mapped_symbols": 2955,
                "exact_range_cache_files": 505,
                "needs_fetch_symbols": 2450,
                "estimated_api_calls": 31850,
                "estimated_total_range_bytes": 6203927940,
                "start": "2024-05-30",
                "end": "2026-05-29",
            }
        ),
        encoding="utf-8",
    )

    micro = dashboard.frozen_microstructure_stats()
    ignition_status = dashboard.ignition_registry_stats()
    backfill = dashboard.broad_backfill_stats()
    assert micro["forward_sessions"] == 3
    assert micro["selection_avg_pnl"] == 647.72
    assert micro["selection_rejected"]
    assert not micro["real_orders_allowed"]
    assert ignition_status["selection_sessions"] == 9
    assert ignition_status["selection_rejected"]
    assert not ignition_status["real_orders_allowed"]
    assert backfill["cached"] == 505
    assert round(backfill["coverage_pct"], 2) == 17.09

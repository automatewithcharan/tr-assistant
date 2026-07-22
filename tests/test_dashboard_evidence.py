import json
from pathlib import Path

import pandas as pd

from scripts import dashboard


def test_money_formats_negative_rupees_conventionally():
    assert dashboard.money(-840) == "-₹840"
    assert dashboard.money(1250) == "₹1,250"


def test_leader_dashboard_reports_ready_prior_session_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "live_leader_continuation"
    root.mkdir(parents=True)
    (root / "frozen_ranker_status.json").write_text(
        json.dumps(
            {
                "status": "collecting_forward_evidence",
                "live_model": {
                    "trained_through": "2026-07-21",
                    "valid_from": "2026-07-22",
                },
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["live_paper_status"] == "artifact ready"
    assert stats["live_paper_date"] == "2026-07-22"
    assert stats["live_paper_model_through"] == "2026-07-21"
    assert stats["live_paper_predictions"] == 0


def test_leader_dashboard_reports_pullback_reclaim_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "leader_pullback_reclaim_audit"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "rejected_current_rule",
                "goal_stress": {
                    "sessions": 7,
                    "trades": 26,
                    "avg_daily_pnl": -8163.46,
                    "lower_95_pct": -3.32837,
                    "positive_session_pct": 14.29,
                    "goal_hit_pct": 0.0,
                },
                "accepted": False,
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["reclaim_status"] == "rejected_current_rule"
    assert stats["reclaim_sessions"] == 7
    assert stats["reclaim_trades"] == 26
    assert stats["reclaim_stress_avg_pnl"] == -8163.46
    assert stats["reclaim_stress_lower95_pct"] == -3.32837
    assert stats["reclaim_positive_session_pct"] == 14.29
    assert stats["reclaim_goal_hit_pct"] == 0.0
    assert not stats["reclaim_accepted"]


def test_leader_dashboard_reports_rank_migration_and_full_universe_recorder(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    migration = tmp_path / "live_rank_migration"
    migration.mkdir(parents=True)
    (migration / "status.json").write_text(
        json.dumps(
            {
                "status": "collecting_forward_evidence",
                "diagnostic_stress": {
                    "sessions": 4,
                    "trades": 9,
                    "avg_daily_pnl": 1367.57,
                    "lower_95_pct": -0.596931,
                },
                "forward_stress": {
                    "sessions": 1,
                    "trades": 2,
                    "avg_daily_pnl": 500.0,
                    "lower_95_pct": -0.2,
                    "goal_hit_pct": 0.0,
                },
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )
    recorder = tmp_path / "live_feature_recorder"
    recorder.mkdir()
    (recorder / "full_universe_rank_status.json").write_text(
        json.dumps(
            {
                "status": "recording_full_universe_rank",
                "recorded_at": "2026-07-22 12:00:00",
                "first_recorded_at": "2026-07-22 09:16:00",
                "full_session_eligible": True,
                "universe_size": 2990,
                "rows_this_snapshot": 75,
                "rapid_rank_climbers_this_snapshot": 3,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["rank_migration_status"] == "collecting_forward_evidence"
    assert stats["rank_migration_diagnostic_trades"] == 9
    assert stats["rank_migration_diagnostic_avg_pnl"] == 1367.57
    assert stats["rank_migration_forward_sessions"] == 1
    assert stats["rank_migration_forward_trades"] == 2
    assert not stats["rank_migration_accepted"]
    assert stats["full_rank_recorder_status"] == "recording_full_universe_rank"
    assert stats["full_rank_universe"] == 2990
    assert stats["full_rank_rows"] == 75
    assert stats["full_rank_rapid"] == 3
    assert stats["full_rank_first_recorded_at"] == "2026-07-22 09:16:00"
    assert stats["full_rank_session_eligible"] is True


def test_leader_dashboard_reports_full_universe_rank_strategy_gate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "full_universe_rank_strategy"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "collecting_full_session_selection_evidence",
                "eligible_full_sessions": 3,
                "selection_sessions_required": 12,
                "partial_sessions": 1,
                "executable_rows": 420,
                "forward_stress": {
                    "sessions": 0,
                    "trades": 0,
                    "avg_daily_pnl": 0.0,
                },
                "accepted": False,
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["full_rank_strategy_status"] == "collecting_full_session_selection_evidence"
    assert stats["full_rank_strategy_eligible_sessions"] == 3
    assert stats["full_rank_strategy_required_sessions"] == 12
    assert stats["full_rank_strategy_partial_sessions"] == 1
    assert stats["full_rank_strategy_rows"] == 420
    assert not stats["full_rank_strategy_accepted"]


def test_leader_dashboard_reports_exact_full_rank_paper_ledger(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "full_universe_rank_paper"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "recording_exact_ask_to_bid_paper_outcomes",
                "updated_at": "2026-07-22 12:30:00",
                "entries": 14,
                "active": 3,
                "closed": 11,
                "wins": 7,
                "net_pnl": 2345.67,
                "paper_only": True,
                "alerts_allowed": False,
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["full_rank_paper_status"] == "recording_exact_ask_to_bid_paper_outcomes"
    assert stats["full_rank_paper_entries"] == 14
    assert stats["full_rank_paper_active"] == 3
    assert stats["full_rank_paper_closed"] == 11
    assert stats["full_rank_paper_wins"] == 7
    assert stats["full_rank_paper_net_pnl"] == 2345.67


def test_leader_dashboard_reports_exact_capital_strategy_gate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "full_universe_rank_exact_strategy"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "collecting_exact_selection_sessions",
                "eligible_exact_sessions": 3,
                "selection_sessions_required": 12,
                "partial_or_incomplete_sessions": 1,
                "entry_labels": 140,
                "closed_labels": 120,
                "frozen_selection": {},
                "forward": {"sessions": 0, "trades": 0, "avg_daily_pnl": 0.0},
                "accepted": False,
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["full_rank_exact_status"] == "collecting_exact_selection_sessions"
    assert stats["full_rank_exact_sessions"] == 3
    assert stats["full_rank_exact_required"] == 12
    assert stats["full_rank_exact_partial"] == 1
    assert stats["full_rank_exact_labels"] == 140
    assert stats["full_rank_exact_closed"] == 120
    assert stats["full_rank_exact_selector"] == "not frozen"
    assert not stats["full_rank_exact_accepted"]


def test_leader_dashboard_reports_contract_matrix_and_capital_gate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    live = tmp_path / "full_universe_rank_contract_matrix"
    live.mkdir(parents=True)
    (live / "status.json").write_text(
        json.dumps(
            {
                "status": "recording_exact_exit_contract_matrix",
                "contracts": 9,
                "entry_labels": 40,
                "active_contracts": 250,
                "closed_contracts": 110,
            }
        ),
        encoding="utf-8",
    )
    judge = tmp_path / "full_universe_rank_contract_strategy"
    judge.mkdir(parents=True)
    (judge / "status.json").write_text(
        json.dumps(
            {
                "status": "collecting_contract_forward_sessions",
                "eligible_sessions": 12,
                "selection_sessions_required": 12,
                "frozen_selection": {
                    "contract": "t1_0_s0_5_h30",
                    "selector": "acceleration_first",
                },
                "forward": {"sessions": 3, "trades": 12, "avg_daily_pnl": 3200},
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["contract_matrix_status"] == "recording_exact_exit_contract_matrix"
    assert stats["contract_matrix_contracts"] == 9
    assert stats["contract_matrix_active"] == 250
    assert stats["contract_strategy_sessions"] == 12
    assert stats["contract_strategy_combination"] == "t1_0_s0_5_h30 / acceleration_first"
    assert stats["contract_strategy_forward_sessions"] == 3
    assert stats["contract_strategy_forward_avg_pnl"] == 3200
    assert not stats["contract_strategy_accepted"]


def test_leader_dashboard_reports_entry_timing_matrix_and_gate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    live = tmp_path / "full_universe_rank_entry_timing"
    live.mkdir(parents=True)
    (live / "status.json").write_text(
        json.dumps(
            {
                "status": "recording_exact_entry_timing_matrix",
                "variants": 6,
                "entry_labels": 20,
                "waiting": 40,
                "open_positions": 25,
                "fills": 50,
                "nonfills": 5,
                "closed_positions": 25,
            }
        ),
        encoding="utf-8",
    )
    judge = tmp_path / "full_universe_rank_entry_strategy"
    judge.mkdir(parents=True)
    (judge / "status.json").write_text(
        json.dumps(
            {
                "status": "collecting_entry_timing_forward_sessions",
                "eligible_sessions": 12,
                "selection_sessions_required": 12,
                "frozen_selection": {
                    "variant": "dip_0_50",
                    "selector": "rank_first",
                },
                "forward": {"sessions": 2, "trades": 8, "avg_daily_pnl": 4100},
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["entry_timing_status"] == "recording_exact_entry_timing_matrix"
    assert stats["entry_timing_variants"] == 6
    assert stats["entry_timing_fills"] == 50
    assert stats["entry_timing_nonfills"] == 5
    assert stats["entry_strategy_sessions"] == 12
    assert stats["entry_strategy_combination"] == "dip_0_50 / rank_first"
    assert stats["entry_strategy_forward_avg_pnl"] == 4100
    assert not stats["entry_strategy_accepted"]


def test_leader_dashboard_reports_feature_gate_strategy(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "full_universe_rank_feature_strategy"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "collecting_feature_gate_forward_sessions",
                "feature_gates": 9,
                "eligible_sessions": 12,
                "selection_sessions_required": 12,
                "frozen_selection": {
                    "gate": "sector_rotation",
                    "selector": "balanced_rank_acceleration",
                },
                "forward": {"sessions": 4, "trades": 16, "avg_daily_pnl": 5200},
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["feature_strategy_status"] == "collecting_feature_gate_forward_sessions"
    assert stats["feature_strategy_gates"] == 9
    assert stats["feature_strategy_sessions"] == 12
    assert stats["feature_strategy_combination"] == "sector_rotation / balanced_rank_acceleration"
    assert stats["feature_strategy_forward_sessions"] == 4
    assert stats["feature_strategy_forward_avg_pnl"] == 5200
    assert not stats["feature_strategy_accepted"]


def test_leader_dashboard_reports_capital_growth_without_claiming_validation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "intraday_capital_growth"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "awaiting_validated_forward_strategy",
                "initial_capital": 500000,
                "monthly_contribution": 200000,
                "daily_goal": 10000,
                "initial_required_daily_return_pct": 2.0,
                "first_month_required_return_at_or_below_1pct_without_trading_profit": 4,
                "validated_strategy": {},
                "capital_projection_allowed": False,
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {"scenario": "mathematical_0.00pct", "month": 12, "end_capital": 2900000},
            {"scenario": "mathematical_0.50pct", "month": 12, "end_capital": 6000000},
            {"scenario": "mathematical_1.00pct", "month": 12, "end_capital": 12000000},
        ]
    ).to_csv(root / "monthly_scenarios.csv", index=False)

    stats = dashboard.leader_discovery_stats()

    assert stats["capital_growth_status"] == "awaiting_validated_forward_strategy"
    assert stats["capital_initial"] == 500000
    assert stats["capital_monthly_addition"] == 200000
    assert stats["capital_initial_required_pct"] == 2.0
    assert stats["capital_one_pct_month"] == 4
    assert stats["capital_validated_strategy"] == "none"
    assert stats["capital_month12_no_edge"] == 2900000
    assert not stats["capital_projection_allowed"]


def test_leader_dashboard_reports_capital_allocation_without_promoting_partial(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "full_universe_rank_capital_allocation"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "collecting_capital_allocation_forward_sessions",
                "predeclared_combinations": 144,
                "eligible_sessions": 12,
                "selection_sessions_required": 12,
                "frozen_selection": {
                    "contract": "t1_0_s0_5_h30",
                    "selector": "acceleration_first",
                    "slots": 3,
                },
                "forward": {"sessions": 4, "trades": 22, "avg_daily_pnl": 6100},
                "best_partial_diagnostic": {
                    "slots": 5,
                    "avg_daily_pnl": 15000,
                    "excluded_from_evidence": True,
                },
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["capital_allocation_combinations"] == 144
    assert stats["capital_allocation_sessions"] == 12
    assert stats["capital_allocation_combination"] == "t1_0_s0_5_h30 / acceleration_first / 3 slots"
    assert stats["capital_allocation_forward_sessions"] == 4
    assert stats["capital_allocation_forward_avg_pnl"] == 6100
    assert stats["capital_allocation_partial_pnl"] == 15000
    assert not stats["capital_allocation_accepted"]


def test_leader_dashboard_reports_integrated_policy_forward_separately(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "full_universe_rank_integrated_policies"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        json.dumps(
            {
                "status": "collecting_integrated_policy_forward_sessions",
                "policies": 10,
                "eligible_sessions": 12,
                "selection_sessions_required": 12,
                "frozen_selection": {"policy": "morning_acceleration"},
                "forward": {"sessions": 3, "trades": 14, "avg_daily_pnl": 7200},
                "best_partial_diagnostic": {
                    "policy": "opening_fast_scalp",
                    "avg_daily_pnl": 18000,
                    "excluded_from_evidence": True,
                },
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["integrated_policy_count"] == 10
    assert stats["integrated_policy_sessions"] == 12
    assert stats["integrated_policy_frozen"] == "morning_acceleration"
    assert stats["integrated_policy_forward_sessions"] == 3
    assert stats["integrated_policy_forward_avg_pnl"] == 7200
    assert stats["integrated_policy_partial_name"] == "opening_fast_scalp"
    assert not stats["integrated_policy_accepted"]


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


def test_eventual_runner_dashboard_prefers_highest_broad_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    current = tmp_path / "eventual_runner_current_505"
    current.mkdir()
    (current / "classifier_status.json").write_text(
        json.dumps({"selected": {"stress_final_avg_daily_pnl": 100.0}}),
        encoding="utf-8",
    )
    for milestone, pnl in ((1000, 200.0), (2000, 300.0)):
        root = tmp_path / "broad_rank_acceleration" / f"checkpoint_{milestone}"
        root.mkdir(parents=True)
        (root / "eventual_runner_classifier_status.json").write_text(
            json.dumps({"selected": {"stress_final_avg_daily_pnl": pnl}}),
            encoding="utf-8",
        )
    stats = dashboard.eventual_runner_classifier_stats()
    assert stats["evidence_scope"] == "broad checkpoint 2000"
    assert stats["stress_final_avg_pnl"] == 300.0


def test_direct_target_dashboard_reads_clairvoyant_bound_without_promotion(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "eventual_runner_current_505"
    root.mkdir()
    (root / "direct_target_status.json").write_text(
        json.dumps(
            {
                "selected": {"stress_final_avg_daily_pnl": -200.0},
                "optimistic_opportunity_bound": [
                    {
                        "slots": 1,
                        "stress_final_avg_daily_pnl": 11_000.0,
                        "stress_final_mean_lower_95_pnl": 10_100.0,
                        "stress_final_goal_hit_pct": 60.0,
                    },
                    {
                        "slots": 2,
                        "stress_final_avg_daily_pnl": 9_000.0,
                        "stress_final_mean_lower_95_pnl": 8_000.0,
                        "stress_final_goal_hit_pct": 40.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    stats = dashboard.direct_target_classifier_stats()
    assert stats["available"]
    assert stats["stress_final_avg_pnl"] == -200.0
    assert stats["opportunity_bound_final_lower95_pnl"] == 10_100.0
    assert stats["opportunity_bound_slots"] == 1
    assert stats["signal_window"] == "pending"
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


def test_evidence_readiness_prefers_candidate_coverage(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "live_feature_recorder"
    root.mkdir(parents=True)
    pd.DataFrame().to_csv(root / "evidence_readiness.csv", index=False)
    status_root = root / "eod" / "2026-07-21"
    status_root.mkdir(parents=True)
    (status_root / "status.json").write_text(
        '{"date":"2026-07-21","combined_coverage_pct":70.0,'
        '"promotion_coverage_pct":98.5,"coverage_gate_passed":true}',
        encoding="utf-8",
    )

    stats = dashboard.evidence_readiness_stats()

    assert stats["coverage"] == 98.5
    assert stats["coverage_passed"]


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
                "quality_eligible_cache_files": 503,
                "quality_rejected_cache_files": 2,
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
    assert backfill["cached"] == 503
    assert backfill["present"] == 505
    assert backfill["rejected"] == 2
    assert round(backfill["coverage_pct"], 2) == 17.02

import json

from scripts.build_broad_rank_checkpoint_status import build_status, write_status


def _write_side_status(directory, side, *, selection=True, goal=True):
    (directory / f"{side}_status.json").write_text(
        json.dumps(
            {
                "side": side,
                "selection_gate_passed": selection,
                "final_goal_evidence_passed": goal,
                "paper_promotion_allowed": False,
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )


def _write_classifier_status(directory, *, goal):
    (directory / "eventual_runner_classifier_status.json").write_text(
        json.dumps(
            {
                "final_goal_contract_passed": goal,
                "paper_promotion_allowed": False,
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )


def _write_direct_classifier_status(directory, *, goal):
    (directory / "direct_target_classifier_status.json").write_text(
        json.dumps({"final_goal_contract_passed": goal}), encoding="utf-8"
    )


def _write_strategy_family_status(directory, *, goal):
    (directory / "strategy_family_status.json").write_text(
        json.dumps({"final_goal_contract_passed": goal}),
        encoding="utf-8",
    )


def test_partial_checkpoint_is_diagnostic_even_when_strategy_metrics_pass(tmp_path):
    _write_side_status(tmp_path, "long")
    _write_side_status(tmp_path, "short", goal=False)
    status = build_status(tmp_path, 2_000, 2_000, 2_915)
    assert not status["broad_coverage_gate_passed"]
    assert status["diagnostic_only"]
    assert not status["historical_goal_gate_passed"]
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]


def test_near_complete_checkpoint_can_report_but_not_promote_goal_evidence(tmp_path):
    _write_side_status(tmp_path, "long")
    _write_side_status(tmp_path, "short", selection=False, goal=False)
    status = build_status(tmp_path, 2_900, 2_900, 2_955)
    assert status["coverage_ratio"] >= 0.98
    assert status["broad_coverage_gate_passed"]
    assert status["historical_goal_gate_passed"]
    assert status["forward_validation_required"]
    assert not status["evidence_accepted"]
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]


def test_symbol_count_floor_blocks_small_high_ratio_universe(tmp_path):
    _write_side_status(tmp_path, "long")
    _write_side_status(tmp_path, "short")
    status = build_status(tmp_path, 1_000, 1_000, 1_000)
    assert status["coverage_ratio"] == 1.0
    assert not status["broad_coverage_gate_passed"]
    assert not status["historical_goal_gate_passed"]


def test_full_coverage_can_report_classifier_goal_without_enabling_orders(tmp_path):
    _write_side_status(tmp_path, "long", selection=False, goal=False)
    _write_side_status(tmp_path, "short", selection=False, goal=False)
    _write_classifier_status(tmp_path, goal=True)
    status = build_status(tmp_path, 2_900, 2_900, 2_915)
    assert status["eventual_runner_classifier_goal_passed"]
    assert status["historical_goal_gate_passed"]
    assert status["forward_validation_required"]
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]


def test_full_coverage_can_report_direct_target_goal_without_enabling_orders(tmp_path):
    _write_side_status(tmp_path, "long", selection=False, goal=False)
    _write_side_status(tmp_path, "short", selection=False, goal=False)
    _write_direct_classifier_status(tmp_path, goal=True)
    status = build_status(tmp_path, 2900, 2900, 2915)
    assert status["direct_target_classifier_goal_passed"]
    assert status["historical_goal_gate_passed"]
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]


def test_full_coverage_can_report_rolling_family_goal_without_enabling_orders(
    tmp_path,
):
    _write_side_status(tmp_path, "long", selection=False, goal=False)
    _write_side_status(tmp_path, "short", selection=False, goal=False)
    _write_strategy_family_status(tmp_path, goal=True)

    status = build_status(tmp_path, 2900, 2900, 2915)

    assert status["strategy_family_goal_passed"]
    assert status["historical_goal_gate_passed"]
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]


def test_status_write_is_complete_json(tmp_path):
    path = tmp_path / "nested" / "status.json"
    write_status(path, {"real_orders_allowed": False})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "real_orders_allowed": False
    }


def test_checkpoint_status_records_frozen_input_provenance(tmp_path):
    _write_side_status(tmp_path, "long", goal=False)
    _write_side_status(tmp_path, "short", goal=False)
    (tmp_path / "code_commit_at_start.txt").write_text("abc123\n", encoding="utf-8")
    (tmp_path / "started_at_utc.txt").write_text(
        "2026-07-18T06:00:00Z\n", encoding="utf-8"
    )
    (tmp_path / "quality_universe_sha256.txt").write_text(
        "quality-hash\n", encoding="utf-8"
    )
    (tmp_path / "calendar_sha256.txt").write_text(
        "calendar-hash\n", encoding="utf-8"
    )
    (tmp_path / "starting_capital.txt").write_text("500000\n", encoding="utf-8")
    (tmp_path / "daily_goal.txt").write_text("10000\n", encoding="utf-8")
    (tmp_path / "stress_cost_pct.txt").write_text("0.10\n", encoding="utf-8")
    (tmp_path / "signal_times_at_start.txt").write_text(
        "09:30,09:45,10:00\n",
        encoding="utf-8",
    )
    summary_bytes = b'{"cached": 1000}\n'
    candidate_bytes = b"date,symbol\n2026-01-01,AAA\n"
    (tmp_path / "backfill_summary_at_start.json").write_bytes(summary_bytes)
    (tmp_path / "fixed_time_candidates.csv").write_bytes(candidate_bytes)

    status = build_status(tmp_path, 1_000, 1_010, 2_915)

    assert status["provenance"] == {
        "code_commit": "abc123",
        "started_at_utc": "2026-07-18T06:00:00Z",
        "quality_universe_sha256": "quality-hash",
        "calendar_sha256": "calendar-hash",
        "backfill_summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        "fixed_time_candidates_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "starting_capital": "500000",
        "daily_goal": "10000",
        "stress_cost_pct": "0.10",
        "signal_times": "09:30,09:45,10:00",
    }

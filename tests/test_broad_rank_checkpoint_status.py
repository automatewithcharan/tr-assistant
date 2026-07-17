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


def test_status_write_is_complete_json(tmp_path):
    path = tmp_path / "nested" / "status.json"
    write_status(path, {"real_orders_allowed": False})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "real_orders_allowed": False
    }

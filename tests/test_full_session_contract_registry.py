import json

from scripts.select_full_session_contract_registry import (
    registry_rows,
    select_prefinal,
)


def _write(path, contract, prefinal_floor, final_return):
    path.write_text(
        json.dumps(
            {
                "label_mode": "expected_return",
                "return_column": f"return__{contract}",
                "exit_time_column": f"exit_time__{contract}",
                "selected": {
                    "signal_window": "through_10:30",
                    "threshold": 0.05,
                    "top_per_signal": 1,
                    "slots": 1,
                    "selection_gate_passed": prefinal_floor > 0,
                    "sample_gate_passed": True,
                    "stress_selection_block_floor_pct": prefinal_floor,
                    "stress_train_avg_daily_return_pct": prefinal_floor,
                    "stress_validation_avg_daily_return_pct": prefinal_floor,
                    "stress_final_avg_daily_return_pct": final_return,
                    "stress_final_mean_lower_95_pnl": final_return * 5000,
                    "stress_final_goal_hit_pct": 0.0,
                },
                "final_goal_contract_passed": False,
            }
        ),
        encoding="utf-8",
    )


def test_contract_registry_ignores_better_final_period(tmp_path):
    robust = tmp_path / "robust.json"
    leaked = tmp_path / "leaked.json"
    _write(robust, "h60_target_1_stop_0.5", 0.10, -1.0)
    _write(leaked, "h60_target_1.5_stop_1", -0.10, 5.0)

    registry = registry_rows([str(robust), str(leaked)])
    selected = select_prefinal(registry)

    assert selected["return_column"] == "return__h60_target_1_stop_0.5"
    assert selected["stress_final_avg_daily_return_pct"] == -1.0
    assert not selected["real_orders_allowed"]


def test_contract_registry_skips_non_expected_return_status(tmp_path):
    valid = tmp_path / "valid.json"
    ignored = tmp_path / "ignored.json"
    _write(valid, "h60_target_0.7_stop_0.5", 0.05, 0.02)
    ignored.write_text(
        json.dumps({"label_mode": "eventual_runner", "selected": {"slots": 1}}),
        encoding="utf-8",
    )

    registry = registry_rows([str(valid), str(ignored)])

    assert len(registry) == 1
    assert registry.iloc[0]["return_column"] == "return__h60_target_0.7_stop_0.5"

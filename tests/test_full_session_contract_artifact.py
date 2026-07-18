import json
from pathlib import Path

from scripts.validate_full_session_contract_artifact import is_complete_contract


def _write_artifacts(prefix: Path, contract: str) -> None:
    payload = {
        "label_mode": "expected_return",
        "feature_set": "bar_path",
        "return_column": f"return__{contract}",
        "exit_time_column": f"exit_time__{contract}",
        "exit_reason_column": f"exit__{contract}",
        "selection_uses_final_period": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    prefix.with_name(prefix.name + "_status.json").write_text(json.dumps(payload))
    for suffix in (
        "_summary.csv",
        "_selected_stress_daily.csv",
        "_selected_stress_trades.csv",
    ):
        prefix.with_name(prefix.name + suffix).write_text("header\nvalue\n")


def test_complete_contract_requires_matching_safe_artifacts(tmp_path):
    contract = "h60_target_1_stop_0.7"
    prefix = tmp_path / "expected_return"
    _write_artifacts(prefix, contract)

    assert is_complete_contract(prefix, contract)
    assert not is_complete_contract(prefix, "h60_target_1.5_stop_0.7")


def test_complete_contract_rejects_missing_or_live_enabled_output(tmp_path):
    contract = "h60_target_1_stop_0.7"
    prefix = tmp_path / "expected_return"
    _write_artifacts(prefix, contract)
    prefix.with_name(prefix.name + "_selected_stress_trades.csv").unlink()
    assert not is_complete_contract(prefix, contract)

    _write_artifacts(prefix, contract)
    status_path = prefix.with_name(prefix.name + "_status.json")
    payload = json.loads(status_path.read_text())
    payload["real_orders_allowed"] = True
    status_path.write_text(json.dumps(payload))
    assert not is_complete_contract(prefix, contract)

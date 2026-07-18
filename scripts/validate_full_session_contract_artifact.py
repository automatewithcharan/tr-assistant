#!/usr/bin/env python3
"""Validate that a full-session contract can be safely resumed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def is_complete_contract(prefix: Path, contract: str) -> bool:
    status_path = prefix.with_name(prefix.name + "_status.json")
    required = (
        status_path,
        prefix.with_name(prefix.name + "_summary.csv"),
        prefix.with_name(prefix.name + "_selected_stress_daily.csv"),
        prefix.with_name(prefix.name + "_selected_stress_trades.csv"),
    )
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    return bool(
        all(path.is_file() and path.stat().st_size > 0 for path in required)
        and payload.get("label_mode") == "expected_return"
        and payload.get("feature_set") == "bar_path"
        and payload.get("return_column") == f"return__{contract}"
        and payload.get("exit_time_column") == f"exit_time__{contract}"
        and payload.get("exit_reason_column") == f"exit__{contract}"
        and payload.get("selection_uses_final_period") is False
        and payload.get("paper_promotion_allowed") is False
        and payload.get("real_orders_allowed") is False
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--contract", required=True)
    args = parser.parse_args()
    return 0 if is_complete_contract(args.prefix, args.contract) else 1


if __name__ == "__main__":
    raise SystemExit(main())

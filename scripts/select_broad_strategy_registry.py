#!/usr/bin/env python3
"""Freeze a pre-final-only comparison across broad intraday strategy families."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _number(payload: dict, *names: str) -> float:
    for name in names:
        value = payload.get(name)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return 0.0


def normalize_status(name: str, path: str, capital: float) -> dict:
    status = json.loads(Path(path).read_text(encoding="utf-8"))
    selected = status.get("selected") or status.get("selected_metrics") or {}
    strategy = str(status.get("strategy") or status.get("evidence") or name)
    feature_set = str(status.get("feature_set") or status.get("side") or "")
    train_pnl = _number(selected, "stress_train_avg_daily_pnl")
    validation_pnl = _number(selected, "stress_validation_avg_daily_pnl")
    final_pnl = _number(selected, "stress_final_avg_daily_pnl")
    if train_pnl == 0.0:
        train_pnl = _number(selected, "stress_train_avg_daily_return_pct") * capital / 100.0
    if validation_pnl == 0.0:
        validation_pnl = _number(selected, "stress_validation_avg_daily_return_pct") * capital / 100.0
    if final_pnl == 0.0:
        final_pnl = _number(selected, "stress_final_avg_daily_return_pct") * capital / 100.0
    block_floor_pct = _number(
        selected,
        "stress_selection_block_floor_pct",
        "stress_selection_floor_pct",
        "selection_block_floor_pct",
    )
    selection_gate = bool(
        status.get("selection_gate_passed", False)
        or selected.get("selection_gate_passed", False)
        or selected.get("research_gate_passed", False)
    )
    final_gate = bool(
        status.get("final_goal_contract_passed", False)
        or status.get("final_goal_evidence_passed", False)
    )
    selection_score = min(train_pnl, validation_pnl, block_floor_pct * capital / 100.0)
    return {
        "name": name,
        "path": str(path),
        "strategy": strategy,
        "feature_set_or_side": feature_set,
        "selection_uses_final_period": bool(status.get("selection_uses_final_period", False)),
        "stress_train_avg_daily_pnl": round(train_pnl, 2),
        "stress_validation_avg_daily_pnl": round(validation_pnl, 2),
        "stress_selection_block_floor_pct": round(block_floor_pct, 6),
        "prefinal_selection_score_pnl": round(selection_score, 2),
        "selection_gate_passed": selection_gate,
        "stress_final_avg_daily_pnl": round(final_pnl, 2),
        "stress_final_mean_lower_95_pnl": round(
            _number(selected, "stress_final_mean_lower_95_pnl"), 2
        ),
        "stress_final_goal_hit_pct": round(
            _number(selected, "stress_final_goal_hit_pct"), 2
        ),
        "final_goal_contract_passed": final_gate,
        "paper_promotion_allowed": bool(status.get("paper_promotion_allowed", False)),
        "real_orders_allowed": bool(status.get("real_orders_allowed", False)),
    }


def select_registry(rows: list[dict]) -> tuple[pd.DataFrame, dict]:
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("strategy registry has no status rows")
    if frame["selection_uses_final_period"].any():
        raise ValueError("registry refuses a strategy selected using the final period")
    frame.sort_values(
        ["selection_gate_passed", "prefinal_selection_score_pnl", "name"],
        ascending=[False, False, True],
        inplace=True,
    )
    chosen = frame.iloc[0].to_dict()
    accepted = bool(
        chosen["selection_gate_passed"]
        and chosen["final_goal_contract_passed"]
        and chosen["paper_promotion_allowed"]
    )
    status = {
        "strategy": "broad_strategy_family_registry",
        "selection_order": [
            "selection_gate_passed",
            "prefinal_selection_score_pnl",
            "name",
        ],
        "selection_uses_final_period": False,
        "selected": chosen,
        "evidence_accepted": accepted,
        "paper_promotion_allowed": accepted,
        "real_orders_allowed": False,
        "rejection_reason": "no family passed pre-final and final contracts" if not accepted else "",
    }
    return frame.reset_index(drop=True), status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="append", required=True, help="NAME=PATH")
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args()
    rows = []
    for item in args.status:
        name, path = item.split("=", 1)
        rows.append(normalize_status(name.strip(), path.strip(), args.capital))
    registry, status = select_registry(rows)
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    registry.to_csv(prefix.with_name(prefix.name + "_registry.csv"), index=False)
    prefix.with_name(prefix.name + "_status.json").write_text(
        json.dumps(status, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

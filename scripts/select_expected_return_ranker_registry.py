#!/usr/bin/env python3
"""Select one expected-return ranker without consulting final-period results."""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any

import pandas as pd


def registry_rows(paths: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for path_text in sorted(set(paths)):
        path = Path(path_text)
        payload = json.loads(path.read_text(encoding="utf-8"))
        selected = payload.get("selected_metrics", {})
        if not isinstance(selected, dict) or not selected:
            continue
        stress_train = float(selected.get("stress_train_avg_daily_return_pct", 0.0))
        stress_validation = float(
            selected.get("stress_validation_avg_daily_return_pct", 0.0)
        )
        rows.append(
            {
                "path": str(path),
                "signal_time": str(payload.get("signal_time", "")),
                "contract": str(payload.get("contract", "")),
                "threshold": float(payload.get("selected_threshold", 0.0)),
                "top_k": int(payload.get("selected_top_k", 0)),
                "research_gate_passed": bool(
                    payload.get("historical_research_gate_passed", False)
                ),
                "sample_gate_passed": bool(selected.get("sample_gate_passed", False)),
                "selection_block_floor_pct": float(
                    selected.get("selection_block_floor_pct", 0.0)
                ),
                "selection_floor_pct": float(selected.get("selection_floor_pct", 0.0)),
                "stress_train_avg_daily_return_pct": stress_train,
                "stress_validation_avg_daily_return_pct": stress_validation,
                "stress_selection_floor_pct": min(stress_train, stress_validation),
                "stress_final_avg_daily_return_pct": float(
                    selected.get("stress_final_avg_daily_return_pct", 0.0)
                ),
                "stress_final_mean_lower_95_pnl": float(
                    payload.get("stress_final_metrics", {}).get(
                        "stress_final_mean_lower_95_pnl", 0.0
                    )
                ),
                "stress_final_goal_hit_pct": float(
                    payload.get("stress_final_goal_hit_pct", 0.0)
                ),
                "final_goal_contract_passed": bool(
                    payload.get("final_goal_contract_passed", False)
                ),
                "paper_promotion_allowed": False,
                "real_orders_allowed": False,
            }
        )
    if not rows:
        raise ValueError("no expected-return ranker metadata found")
    return pd.DataFrame(rows)


def select_prefinal(registry: pd.DataFrame) -> pd.Series:
    ranked = registry.sort_values(
        [
            "research_gate_passed",
            "sample_gate_passed",
            "selection_block_floor_pct",
            "stress_selection_floor_pct",
            "stress_validation_avg_daily_return_pct",
            "signal_time",
            "contract",
        ],
        ascending=[False, False, False, False, False, True, True],
        kind="mergesort",
    )
    return ranked.iloc[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="*", default=[])
    parser.add_argument("--glob", dest="glob_pattern", default="")
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args()
    paths = list(args.inputs)
    if args.glob_pattern:
        paths.extend(glob.glob(args.glob_pattern))
    registry = registry_rows(paths)
    selected = select_prefinal(registry).to_dict()
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    registry.to_csv(prefix.with_name(prefix.name + "_registry.csv"), index=False)
    status = {
        "strategy": "walk_forward_expected_return_ranker_registry",
        "selection_uses_final_period": False,
        "selection_order": [
            "research_gate_passed",
            "sample_gate_passed",
            "selection_block_floor_pct",
            "stress_selection_floor_pct",
            "stress_validation_avg_daily_return_pct",
        ],
        "candidates": registry.to_dict(orient="records"),
        "selected": selected,
        "final_goal_contract_passed": bool(
            selected.get("final_goal_contract_passed", False)
        ),
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "forward_validation_required": True,
    }
    prefix.with_name(prefix.name + "_status.json").write_text(
        json.dumps(status, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build a fail-closed status for a broad-universe rank checkpoint."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MIN_GOAL_CACHED_SYMBOLS = 2_900
MIN_GOAL_COVERAGE_RATIO = 0.98


def load_status(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid status payload: {path}")
    return payload


def load_optional_status(path: Path) -> dict[str, Any]:
    return load_status(path) if path.exists() and path.stat().st_size > 0 else {}


def build_status(
    output_dir: Path,
    milestone: int,
    cached_symbols: int,
    mapped_symbols: int,
) -> dict[str, Any]:
    long_status = load_status(output_dir / "long_status.json")
    short_status = load_status(output_dir / "short_status.json")
    classifier_status = load_optional_status(
        output_dir / "eventual_runner_classifier_status.json"
    )
    direct_classifier_status = load_optional_status(
        output_dir / "direct_target_classifier_status.json"
    )
    cached = max(0, int(cached_symbols))
    mapped = max(0, int(mapped_symbols))
    coverage_ratio = cached / mapped if mapped else 0.0
    broad_coverage_gate_passed = bool(
        cached >= MIN_GOAL_CACHED_SYMBOLS
        and coverage_ratio >= MIN_GOAL_COVERAGE_RATIO
    )
    long_goal = bool(long_status.get("final_goal_evidence_passed", False))
    short_goal = bool(short_status.get("final_goal_evidence_passed", False))
    classifier_goal = bool(
        classifier_status.get("final_goal_contract_passed", False)
    )
    direct_classifier_goal = bool(
        direct_classifier_status.get("final_goal_contract_passed", False)
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "milestone": int(milestone),
        "cached_symbols": cached,
        "mapped_symbols": mapped,
        "coverage_ratio": round(coverage_ratio, 6),
        "minimum_goal_cached_symbols": MIN_GOAL_CACHED_SYMBOLS,
        "minimum_goal_coverage_ratio": MIN_GOAL_COVERAGE_RATIO,
        "broad_coverage_gate_passed": broad_coverage_gate_passed,
        "diagnostic_only": not broad_coverage_gate_passed,
        "long_selection_gate_passed": bool(long_status.get("selection_gate_passed", False)),
        "short_selection_gate_passed": bool(short_status.get("selection_gate_passed", False)),
        "long_final_goal_evidence_passed": long_goal,
        "short_final_goal_evidence_passed": short_goal,
        "eventual_runner_classifier_goal_passed": classifier_goal,
        "direct_target_classifier_goal_passed": direct_classifier_goal,
        "historical_goal_gate_passed": bool(
            broad_coverage_gate_passed
            and (long_goal or short_goal or classifier_goal or direct_classifier_goal)
        ),
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "forward_validation_required": True,
        "universe_caveat": "current_live_universe_not_point_in_time_membership",
        "long_status": long_status,
        "short_status": short_status,
        "eventual_runner_classifier_status": classifier_status,
        "direct_target_classifier_status": direct_classifier_status,
    }


def write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--milestone", required=True, type=int)
    parser.add_argument("--cached-symbols", required=True, type=int)
    parser.add_argument("--mapped-symbols", required=True, type=int)
    args = parser.parse_args()
    payload = build_status(
        Path(args.output_dir),
        args.milestone,
        args.cached_symbols,
        args.mapped_symbols,
    )
    write_status(Path(args.status), payload)
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

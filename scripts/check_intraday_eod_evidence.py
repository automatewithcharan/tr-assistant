#!/usr/bin/env python3
"""Verify that every authoritative intraday evidence artifact refreshed after close."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date as date_type
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")


ARTIFACTS = {
    "full_rank_strategy": "results/Data/full_universe_rank_strategy/status.json",
    "exact_one_slot": "results/Data/full_universe_rank_exact_strategy/status.json",
    "exit_contract_matrix": "results/Data/full_universe_rank_contract_strategy/status.json",
    "entry_timing_matrix": "results/Data/full_universe_rank_entry_strategy/status.json",
    "feature_gate_matrix": "results/Data/full_universe_rank_feature_strategy/status.json",
    "capital_allocation_matrix": "results/Data/full_universe_rank_capital_allocation/status.json",
    "opportunity_ceiling": "results/Data/full_universe_rank_opportunity_ceiling/status.json",
    "capital_growth": "results/Data/intraday_capital_growth/status.json",
    "runner_recall": "results/Data/live_top_gainer_recorder_recall/status.json",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def assess(root: Path, session_date: str, cutoff_epoch: float) -> dict[str, Any]:
    checks = []
    base_path = (
        root
        / "results/Data/live_feature_recorder/eod"
        / session_date
        / "status.json"
    )
    base = load_json(base_path)
    base_status = str(base.get("status") or "")
    base_fresh = base_path.exists() and base_path.stat().st_mtime >= cutoff_epoch
    base_complete = base_status.startswith("complete") or base_status == "coverage_gate_failed"
    checks.append(
        {
            "name": "base_eod_labels",
            "passed": bool(base_fresh and base_complete),
            "detail": f"status={base_status or 'missing'}, fresh_after_close={base_fresh}",
        }
    )

    for name, relative in ARTIFACTS.items():
        path = root / relative
        payload = load_json(path)
        fresh = path.exists() and path.stat().st_mtime >= cutoff_epoch
        unsafe = bool(payload.get("real_orders_allowed", False))
        passed = bool(payload and fresh and not unsafe)
        checks.append(
            {
                "name": name,
                "passed": passed,
                "detail": (
                    f"status={payload.get('status', payload.get('evidence_type', 'missing'))}, "
                    f"fresh_after_close={fresh}, real_orders_allowed={unsafe}"
                ),
            }
        )

    issues = [item["detail"] for item in checks if not item["passed"]]
    session_eligible = base_status.startswith("complete")
    if issues:
        status = "incomplete_eod_pipeline"
    elif session_eligible:
        status = "complete_eligible_session"
    else:
        status = "complete_ineligible_session"
    return {
        "status": status,
        "date": session_date,
        "checked_at": datetime.now(IST).isoformat(timespec="seconds"),
        "base_eod_status": base_status or "missing",
        "session_evidence_eligible": session_eligible,
        "artifacts_refreshed": sum(1 for item in checks if item["passed"]),
        "artifacts_required": len(checks),
        "issues": issues,
        "checks": checks,
        "strategy_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }


def write_report(root: Path, report: dict[str, Any]) -> None:
    output = root / "results/Data/intraday_evidence_pipeline"
    output.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, indent=2) + "\n"
    (output / f"eod_{report['date']}.json").write_text(content, encoding="utf-8")
    (output / "eod_latest.json").write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--date")
    parser.add_argument("--allow-issues", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    session_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
    parsed_date = date_type.fromisoformat(session_date)
    cutoff = datetime.combine(parsed_date, time(15, 30), tzinfo=IST).timestamp()
    report = assess(root, session_date, cutoff)
    write_report(root, report)
    print(
        f"{report['status']}: {report['artifacts_refreshed']}/"
        f"{report['artifacts_required']} EOD artifacts refreshed"
    )
    for item in report["checks"]:
        print(f"{'OK' if item['passed'] else 'FAIL'} {item['name']}: {item['detail']}")
    return 0 if report["status"].startswith("complete_") or args.allow_issues else 1


if __name__ == "__main__":
    raise SystemExit(main())

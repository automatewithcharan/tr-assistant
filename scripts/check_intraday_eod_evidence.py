#!/usr/bin/env python3
"""Verify that every authoritative intraday evidence artifact refreshed after close."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
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
    "depth_execution_matrix": "results/Data/full_universe_rank_depth_execution/status.json",
    "rank_migration_depth_forward": "results/Data/frozen_rank_migration_depth_forward/status.json",
    "stop_avoidance_depth_forward": "results/Data/frozen_stop_avoidance_depth_forward/status.json",
    "runner_qualified_stop_depth_forward": "results/Data/frozen_runner_qualified_stop_depth_forward/status.json",
    "balanced_stop_portfolio_depth_forward": "results/Data/frozen_balanced_stop_portfolio_forward/status.json",
    "balanced_stop_margin_depth_forward": "results/Data/frozen_balanced_stop_margin_forward/status.json",
    "rank_displacement_depth_forward": "results/Data/frozen_rank_displacement_forward/status.json",
    "historical_rank_displacement": "results/Data/historical_rank_displacement/status.json",
    "fixed_rank_first_forward": "results/Data/fixed_rank_first_forward/status.json",
    "entry_timing_matrix": "results/Data/full_universe_rank_entry_timing/status.json",
    "feature_gate_matrix": "results/Data/full_universe_rank_feature_strategy/status.json",
    "capital_allocation_matrix": "results/Data/full_universe_rank_capital_allocation/status.json",
    "integrated_policy_registry": "results/Data/full_universe_rank_integrated_policies/status.json",
    "opportunity_ceiling": "results/Data/full_universe_rank_opportunity_ceiling/status.json",
    "prior_session_ranker": "results/Data/prior_session_ranker_paper/status.json",
    "frozen_prediction_matrix": "results/Data/frozen_ranker_prediction_strategy/status.json",
    "capital_growth": "results/Data/intraday_capital_growth/status.json",
    "runner_recall": "results/Data/live_top_gainer_recorder_recall/status.json",
    "runner_capture_funnel": "results/Data/live_runner_capture_funnel/status.json",
    "strategy_evidence_scoreboard": "results/Data/intraday_strategy_evidence_scoreboard/status.json",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def rank_session_coverage(path: Path, session_date: str) -> tuple[bool, bool]:
    """Return whether the date exists and is an uninterrupted full session."""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return False, False
    matches = [row for row in rows if str(row.get("date") or "") == session_date]
    if not matches:
        return False, False
    value = str(matches[-1].get("full_session_eligible") or "").strip().lower()
    return True, value in {"1", "1.0", "true", "yes", "y", "on"}


def ranked_paper_ledger_integrity(
    path: Path,
    session_date: str,
) -> dict[str, Any]:
    """Reconcile paper entries, exits, and the persisted daily summary."""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = [
                row
                for row in csv.DictReader(handle)
                if str(row.get("timestamp") or "")[:10] == session_date
            ]
    except Exception:
        rows = []

    entries = [row for row in rows if row.get("event") == "paper_entry"]
    exits = [row for row in rows if row.get("event") == "paper_exit"]
    summaries = [
        row for row in rows if row.get("event") == "paper_daily_summary"
    ]
    entry_symbols = Counter(
        str(row.get("symbol") or "").strip().upper() for row in entries
    )
    exit_symbols = Counter(
        str(row.get("symbol") or "").strip().upper() for row in exits
    )
    summary = summaries[-1] if summaries else {}
    try:
        details = json.loads(str(summary.get("details_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        details = {}
    try:
        exit_net = round(sum(float(row.get("net_pnl") or 0.0) for row in exits), 2)
    except (TypeError, ValueError):
        exit_net = float("nan")
    try:
        summary_trades = int(details.get("trades", -1))
        summary_net = round(float(details.get("net_pnl")), 2)
    except (TypeError, ValueError):
        summary_trades = -1
        summary_net = float("nan")

    passed = bool(
        summaries
        and entry_symbols == exit_symbols
        and summary_trades == len(entries)
        and abs(summary_net - exit_net) <= 0.01
    )
    return {
        "passed": passed,
        "entries": len(entries),
        "exits": len(exits),
        "summaries": len(summaries),
        "summary_trades": summary_trades,
        "exit_net_pnl": exit_net,
        "summary_net_pnl": summary_net,
        "unmatched_entries": sorted((entry_symbols - exit_symbols).elements()),
        "unmatched_exits": sorted((exit_symbols - entry_symbols).elements()),
    }


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

    rank_coverage_path = (
        root / "results/Data/full_universe_rank_strategy/session_coverage.csv"
    )
    rank_coverage_fresh = (
        rank_coverage_path.exists()
        and rank_coverage_path.stat().st_mtime >= cutoff_epoch
    )
    rank_date_found, rank_full_session = rank_session_coverage(
        rank_coverage_path, session_date
    )
    checks.append(
        {
            "name": "rank_session_coverage_manifest",
            "passed": bool(rank_coverage_fresh and rank_date_found),
            "detail": (
                f"date_found={rank_date_found}, fresh_after_close={rank_coverage_fresh}, "
                f"full_session_eligible={rank_full_session}"
            ),
        }
    )

    paper_ledger = ranked_paper_ledger_integrity(
        root / "results/Data/ranked_confirmation_paper/events.csv",
        session_date,
    )
    checks.append(
        {
            "name": "ranked_confirmation_paper_ledger",
            "passed": bool(paper_ledger["passed"]),
            "detail": (
                f"entries={paper_ledger['entries']}, exits={paper_ledger['exits']}, "
                f"summaries={paper_ledger['summaries']}, "
                f"summary_trades={paper_ledger['summary_trades']}, "
                f"exit_net_pnl={paper_ledger['exit_net_pnl']}, "
                f"summary_net_pnl={paper_ledger['summary_net_pnl']}, "
                f"unmatched_entries={paper_ledger['unmatched_entries']}, "
                f"unmatched_exits={paper_ledger['unmatched_exits']}"
            ),
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
    session_eligible = bool(base_status.startswith("complete") and rank_full_session)
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
        "rank_full_session_eligible": rank_full_session,
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

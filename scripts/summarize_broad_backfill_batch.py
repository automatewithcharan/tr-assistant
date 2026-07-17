#!/usr/bin/env python3
"""Write a durable, fail-closed summary for one broad-history batch."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def summarize(
    summary_path: Path,
    audit_path: Path,
    batch_index: int,
    batch_rc: int,
    plan_rc: int,
    sync_rc: int,
) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    audit_counts: dict[str, int] = {}
    if audit_path.exists() and audit_path.stat().st_size > 0:
        audit = pd.read_csv(audit_path)
        if "status" in audit.columns:
            audit_counts = {
                str(status): int(count)
                for status, count in audit["status"].fillna("unknown").value_counts().items()
            }
    fetch_errors = int(audit_counts.get("fetch_error", 0))
    successful = bool(
        int(batch_rc) == 0
        and int(plan_rc) == 0
        and int(sync_rc) == 0
        and fetch_errors == 0
        and bool(audit_counts)
    )
    cached = int(summary.get("exact_range_cache_files") or 0)
    mapped = int(summary.get("mapped_symbols") or 0)
    remaining = int(summary.get("needs_fetch_symbols") or 0)
    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "batch_index": int(batch_index),
        "batch_rc": int(batch_rc),
        "post_plan_rc": int(plan_rc),
        "sync_rc": int(sync_rc),
        "successful": successful,
        "cached_symbols": cached,
        "mapped_symbols": mapped,
        "remaining_symbols": remaining,
        "coverage_pct": round(cached / mapped * 100.0, 2) if mapped else 0.0,
        "audit_status_counts": audit_counts,
        "fetch_errors": fetch_errors,
        "research_only": True,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    state = "complete" if successful else "NEEDS ATTENTION"
    status["message"] = (
        f"Broad history batch {batch_index} {state} | cached {cached}/{mapped} "
        f"({status['coverage_pct']:.1f}%) | remaining {remaining} | "
        f"audit {audit_counts or 'missing'} | rc batch={batch_rc} plan={plan_rc} sync={sync_rc} | "
        "research only, no broker orders"
    )
    return status


def write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--batch-index", required=True, type=int)
    parser.add_argument("--batch-rc", required=True, type=int)
    parser.add_argument("--plan-rc", required=True, type=int)
    parser.add_argument("--sync-rc", required=True, type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    status = summarize(
        Path(args.summary),
        Path(args.audit),
        args.batch_index,
        args.batch_rc,
        args.plan_rc,
        args.sync_rc,
    )
    write_status(Path(args.output), status)
    print(json.dumps(status, indent=2))
    return 0 if status["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only reconciliation for direct-lane Discord candidate delivery."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.TradingViewCandidateDelivery import (  # noqa: E402
    format_reconciliation,
    reconcile_tradingview_candidates,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="SignalMonitor output directory")
    parser.add_argument("--date", required=True, help="Trading date (YYYY-MM-DD)")
    parser.add_argument(
        "--retry-ttl-seconds",
        type=float,
        default=float(os.environ.get("TRADINGVIEW_CANDIDATE_RETRY_TTL_SECONDS", "600") or "600"),
    )
    parser.add_argument(
        "--max-total-attempts",
        type=int,
        default=int(float(os.environ.get("TRADINGVIEW_CANDIDATE_MAX_TOTAL_ATTEMPTS", "6") or "6")),
    )
    args = parser.parse_args()
    report = reconcile_tradingview_candidates(
        args.output_dir,
        args.date,
        retry_ttl_seconds=args.retry_ttl_seconds,
        max_total_attempts=args.max_total_attempts,
    )
    print(format_reconciliation(report))
    return 1 if any(
        lane["missing"] or lane["terminal_failed"] or lane["retryable_stale"]
        for lane in report["lanes"].values()
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())

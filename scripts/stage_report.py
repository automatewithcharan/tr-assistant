#!/usr/bin/env python3
"""
Stage Validation Report — honest readiness dashboard.

Shows exactly where a strategy stands against the staged framework:
  Stage 1: 0.3 %/day net  (3 months paper)
  Stage 2: 0.5 %/day net  (6 months paper/live-sim)
  Stage 3: 1.0 %/day net  (12+ months)

Usage:
    # Evaluate against Stage 1 (default)
    python scripts/stage_report.py

    # Evaluate a specific strategy against Stage 2
    python scripts/stage_report.py --strategy gap_fade_short --stage 2

    # Pass Tier 0 flags (after manual audit)
    python scripts/stage_report.py --lookahead-clean --spread-model-ok

    # Show all strategies in the paper trading directory
    python scripts/stage_report.py --all

    # Use a specific number of days of history
    python scripts/stage_report.py --days 90
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from collections import Counter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from pkscreener.classes.StageValidator import StageValidator, STAGES


def _list_strategies(paper_dir: str) -> list[str]:
    """Return unique strategy names found in paper trading CSVs."""
    import csv
    strategies: set[str] = set()
    for fp in glob.glob(os.path.join(paper_dir, "intraday_paper_*.csv")):
        try:
            with open(fp) as f:
                for row in csv.DictReader(f):
                    s = row.get("strategy", "").strip()
                    if s:
                        strategies.add(s)
        except Exception:
            continue
    return sorted(strategies) or ["all"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage validation dashboard — honest strategy readiness report"
    )
    parser.add_argument(
        "--strategy", "-s", default="all",
        help="Strategy name to evaluate (default: all strategies combined)"
    )
    parser.add_argument(
        "--stage", "-g", type=int, default=1, choices=[1, 2, 3],
        help="Target stage to validate against (default: 1)"
    )
    parser.add_argument(
        "--days", "-d", type=int, default=60,
        help="Number of days of paper trading history to use (default: 60)"
    )
    parser.add_argument(
        "--all", "-a", action="store_true",
        help="Report on every strategy found in the paper trading directory"
    )

    # Tier 0 manual flags (set after doing the audit work)
    tier0 = parser.add_argument_group("Tier 0 audit flags (set after manual verification)")
    tier0.add_argument("--lookahead-clean",     action="store_true",
                       help="Mark lookahead_clean as VERIFIED")
    tier0.add_argument("--spread-model-ok",     action="store_true",
                       help="Mark spread_model_realistic as VERIFIED")
    tier0.add_argument("--no-survivorship",     action="store_true",
                       help="Mark no_survivorship_bias as VERIFIED")
    tier0.add_argument("--walk-forward-ok",     action="store_true",
                       help="Mark walk_forward_cross_regime as VERIFIED")

    args = parser.parse_args()

    from PKDevTools.classes import Archiver
    paper_dir = os.path.join(Archiver.get_user_data_dir(), "intraday_paper")

    # Build Tier 0 flag dict
    tier0_flags: dict[str, bool] = {}
    if args.lookahead_clean:  tier0_flags["lookahead_clean"]          = True
    if args.spread_model_ok:  tier0_flags["spread_model_realistic"]   = True
    if args.no_survivorship:  tier0_flags["no_survivorship_bias"]     = True
    if args.walk_forward_ok:  tier0_flags["walk_forward_cross_regime"] = True

    validator = StageValidator()

    # Determine which strategies to report on
    if args.all:
        strategies = _list_strategies(paper_dir)
        if not strategies:
            print("No paper trading data found. Run the paper session first:")
            print("  python scripts/run_intraday_paper_session.py")
            return
    else:
        strategies = [args.strategy]

    for strategy in strategies:
        result = validator.evaluate(
            strategy_name=strategy,
            days=args.days,
            target_stage=args.stage,
            tier0_flags=tier0_flags,
        )
        print(StageValidator.format_report(result))
        print()

    # Summary line if multiple strategies
    if len(strategies) > 1:
        print("─" * 66)
        print(f"  Evaluated {len(strategies)} strategies against Stage {args.stage}")
        print("─" * 66)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Inspect scanner lifecycle evidence and ingest trusted Pine event payloads.

This is intentionally a local CLI, not a public webhook server.  A future
authenticated receiver can call CandidateLifecycleTracker.ingest_pine_event
after validating its request signature and applying rate/body-size limits.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from pkscreener.classes.CandidateLifecycleTracker import (
    CandidateLifecycleTracker,
    format_daily_report,
)


def _payload(args: argparse.Namespace) -> dict:
    if args.payload_json:
        value = args.payload_json
    elif args.payload_file:
        value = Path(args.payload_file).read_text(encoding="utf-8")
    else:
        value = sys.stdin.read()
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Pine payload must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Candidate lifecycle observability; never places trades."
    )
    parser.add_argument("--output-dir", required=True, help="PKScreener user-data directory")
    parser.add_argument("--match-max-age-minutes", type=float, default=30.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest-pine", help="ingest one trusted Pine JSON event")
    source = ingest.add_mutually_exclusive_group()
    source.add_argument("--payload-json")
    source.add_argument("--payload-file")
    ingest.add_argument("--source", default="manual_cli")

    inspect = subparsers.add_parser("inspect", help="print one candidate and linked lifecycle")
    inspect.add_argument("candidate_id")

    report = subparsers.add_parser("report", help="print a descriptive daily report")
    report.add_argument("--date", required=True, help="IST trading date YYYY-MM-DD")
    report.add_argument("--json", action="store_true", dest="as_json")

    finalize = subparsers.add_parser("finalize-eod", help="finalize missing horizons and EOD status")
    finalize.add_argument("--date", required=True, help="IST trading date YYYY-MM-DD")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    tracker = CandidateLifecycleTracker(
        args.output_dir,
        pine_match_max_age_minutes=args.match_max_age_minutes,
    )
    if args.command == "ingest-pine":
        print(json.dumps(tracker.ingest_pine_event(_payload(args), args.source), indent=2, sort_keys=True))
    elif args.command == "inspect":
        print(json.dumps(tracker.inspect_candidate(args.candidate_id), indent=2, sort_keys=True))
    elif args.command == "report":
        result = tracker.daily_report(args.date)
        print(json.dumps(result, indent=2, sort_keys=True) if args.as_json else format_daily_report(result))
    elif args.command == "finalize-eod":
        print(json.dumps({"finalized": tracker.finalize_eod(args.date)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

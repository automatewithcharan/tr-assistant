#!/usr/bin/env python3
"""Summarize append-only Buy Now rejection traces by trading date and symbol."""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.BuyNowRejectionObservability import (
    load_events,
    summarize_by_date_symbol,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--glob",
        action="append",
        dest="patterns",
        default=None,
        help="JSONL input glob; may be repeated",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    patterns = args.patterns or [
        "results/Data/buy_now_rejection_observability/*.jsonl"
    ]
    paths = sorted(
        {
            Path(path)
            for pattern in patterns
            for path in glob.glob(pattern)
        }
    )
    payload = {
        "input_files": [str(path) for path in paths],
        "groups": summarize_by_date_symbol(load_events(paths)),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

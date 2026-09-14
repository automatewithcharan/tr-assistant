#!/usr/bin/env python3
"""Write post-session labels for NG traded-value shadow observations."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.NGTradedValueShadowExperiment import (  # noqa: E402
    NGTradedValueShadowOutcomeFinalizer,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Finalize isolated Experiment 4 outcomes from authoritative minute data."
    )
    parser.add_argument("session_date", help="IST session date, YYYY-MM-DD")
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=ROOT / "results" / "Data" / "experiment_4_traded_value_shadow",
    )
    parser.add_argument(
        "--minute-dir",
        type=Path,
        required=True,
        help="Directory containing authoritative SYMBOL.csv one-minute OHLCV",
    )
    parser.add_argument(
        "--live-feature-path",
        type=Path,
        help="Causal live_features_DATE.csv used to locate later production-floor crossing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    written = NGTradedValueShadowOutcomeFinalizer(args.experiment_dir).finalize_session(
        args.session_date,
        args.minute_dir,
        live_feature_path=args.live_feature_path,
    )
    print(f"wrote {written} Experiment 4 outcome label(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

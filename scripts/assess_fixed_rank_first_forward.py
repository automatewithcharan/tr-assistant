#!/usr/bin/env python3
"""Judge one frozen rank-first hypothesis on untouched exact sessions.

The hypothesis was chosen after observing the incomplete 2026-07-27 session.
That date is permanently excluded from forward evidence. Future sessions may
score the fixed rule but cannot change its selector, contract, or risk terms.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assess_exact_full_rank_paper import (
    passes_goal_gate,
    replay_one_slot,
)
from scripts.assess_full_rank_contract_matrix import (
    load_matrix_events,
    matrix_session_coverage,
)
from scripts.assess_full_universe_rank_strategy import raw_session_coverage
from scripts.assess_leader_pullback_reclaim import metrics


HYPOTHESIS = {
    "name": "rank_first_t1_s1_h60_one_slot",
    "selector": "rank_first",
    "contract": "t1_0_s1_0_h60",
    "capital": 500_000.0,
    "daily_goal": 10_000.0,
    "max_trades_per_day": 10,
    "selection_cutoff": "2026-07-27",
    "forward_start": "2026-07-28",
    "forward_sessions_required": 20,
    "selection_origin": (
        "chosen after observing the incomplete 2026-07-27 matrix; "
        "that session is diagnostic only"
    ),
}


def evaluate_fixed_hypothesis(
    events: pd.DataFrame,
    eligible_dates: Iterable[str],
    *,
    capital: float = HYPOTHESIS["capital"],
    daily_goal: float = HYPOTHESIS["daily_goal"],
    max_trades_per_day: int = HYPOTHESIS["max_trades_per_day"],
    forward_sessions_required: int = HYPOTHESIS[
        "forward_sessions_required"
    ],
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return immutable selection diagnostic and untouched forward results."""
    dates = sorted({str(value) for value in eligible_dates})
    cutoff = str(HYPOTHESIS["selection_cutoff"])
    contract_events = (
        events[
            events["Contract"].astype(str).eq(HYPOTHESIS["contract"])
        ].copy()
        if events is not None and not events.empty
        else pd.DataFrame()
    )
    selection_dates = [date for date in dates if date <= cutoff]
    forward_dates = [date for date in dates if date > cutoff]
    selection_daily, selection_trades = replay_one_slot(
        contract_events,
        selection_dates,
        selector=str(HYPOTHESIS["selector"]),
        capital=capital,
        max_trades_per_day=max_trades_per_day,
    )
    forward_daily, forward_trades = replay_one_slot(
        contract_events,
        forward_dates,
        selector=str(HYPOTHESIS["selector"]),
        capital=capital,
        max_trades_per_day=max_trades_per_day,
    )
    selection_result = metrics(selection_daily, daily_goal)
    forward_result = metrics(forward_daily, daily_goal)
    accepted = bool(
        len(forward_dates) >= int(forward_sessions_required)
        and passes_goal_gate(
            forward_result,
            daily_goal,
            forward_sessions_required,
        )
    )
    status_name = (
        "accepted_fixed_rank_first_forward"
        if accepted
        else (
            "fixed_rank_first_forward_rejected"
            if len(forward_dates) >= int(forward_sessions_required)
            else "collecting_fixed_rank_first_forward_sessions"
        )
    )
    status = {
        "evidence": "fixed_exact_rank_first_untouched_forward",
        "status": status_name,
        "hypothesis": dict(HYPOTHESIS),
        "hypothesis_frozen_before_forward_start": True,
        "selection_uses_observed_outcome": True,
        "selection_excluded_from_forward_evidence": True,
        "multiple_comparison_selection": True,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "max_trades_per_day": int(max_trades_per_day),
        "selection_dates": selection_dates,
        "selection_diagnostic": selection_result,
        "forward_dates": forward_dates,
        "forward_sessions": int(len(forward_dates)),
        "forward_sessions_required": int(forward_sessions_required),
        "forward": forward_result,
        "accepted": accepted,
        "paper_promotion_allowed": accepted,
        "real_orders_allowed": False,
    }
    return (
        status,
        selection_daily,
        selection_trades,
        forward_daily,
        forward_trades,
    )


def run_audit(
    *,
    entry_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    output_dir: Path,
    capital: float = HYPOTHESIS["capital"],
    daily_goal: float = HYPOTHESIS["daily_goal"],
    max_trades_per_day: int = HYPOTHESIS["max_trades_per_day"],
    forward_sessions_required: int = HYPOTHESIS[
        "forward_sessions_required"
    ],
    min_snapshots: int = 1000,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    entries, events = load_matrix_events(entry_patterns, outcome_patterns)
    raw = raw_session_coverage(raw_patterns, min_snapshots=min_snapshots)
    coverage = matrix_session_coverage(entries, events, raw)
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = (
        coverage.loc[coverage["eligible"], "date"].astype(str).tolist()
        if not coverage.empty
        else []
    )
    (
        status,
        selection_daily,
        selection_trades,
        forward_daily,
        forward_trades,
    ) = evaluate_fixed_hypothesis(
        events,
        eligible_dates,
        capital=capital,
        daily_goal=daily_goal,
        max_trades_per_day=max_trades_per_day,
        forward_sessions_required=forward_sessions_required,
    )
    status.update(
        {
            "raw_sessions": int(len(raw)),
            "eligible_exact_sessions": int(len(eligible_dates)),
            "partial_or_incomplete_sessions": int(
                len(coverage) - len(eligible_dates)
            ),
            "entry_labels": int(len(entries)),
            "closed_labels": int(len(events)),
        }
    )
    selection_daily.to_csv(
        output_dir / "selection_diagnostic_daily.csv", index=False
    )
    selection_trades.to_csv(
        output_dir / "selection_diagnostic_trades.csv", index=False
    )
    forward_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    forward_trades.to_csv(output_dir / "forward_trades.csv", index=False)
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry-glob", action="append", default=[])
    parser.add_argument("--outcome-glob", action="append", default=[])
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument(
        "--output-dir",
        default="results/Data/fixed_rank_first_forward",
    )
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()
    status = run_audit(
        entry_patterns=args.entry_glob
        or ["results/Data/full_universe_rank_paper/entries_20*.csv"],
        outcome_patterns=args.outcome_glob
        or [
            "results/Data/full_universe_rank_contract_matrix/"
            "outcomes_20*.csv"
        ],
        raw_patterns=args.raw_glob
        or ["results/Data/live_feature_recorder/live_features_20*.csv"],
        output_dir=Path(args.output_dir),
        capital=args.capital,
        daily_goal=args.daily_goal,
        max_trades_per_day=args.max_trades,
        forward_sessions_required=args.forward_sessions,
        min_snapshots=args.min_snapshots,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

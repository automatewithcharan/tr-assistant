#!/usr/bin/env python3
"""Evaluate a predeclared, capacity-aware paper challenger.

This is a forward paper lane, not a selector tuner. The policies are fixed
before the session and use the same five-level entry/exit depth evidence as
the main judges. Outcomes are reported separately so a later winner cannot
silently replace the original experiment.
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

from scripts.assess_depth_executable_rank_strategy import (
    depth_session_coverage,
    load_depth_events,
    replay_depth_slots,
)
from scripts.assess_leader_pullback_reclaim import metrics
from scripts.assess_full_universe_rank_strategy import raw_session_coverage


CAPITAL = 500_000.0
SLOTS = 5
SLOT_CAPITAL = CAPITAL / SLOTS
MAX_TRADES_PER_DAY = 10
FORWARD_START = "2026-08-03"
POLICIES = {
    "acceleration_t1_5_s1_0": {
        "selector": "acceleration_first",
        "contract": "t1_5_s1_0_h60",
        "description": "rank acceleration with a 1.0% stop",
    },
    "acceleration_t1_5_s1_5": {
        "selector": "acceleration_first",
        "contract": "t1_5_s1_5_h60",
        "description": "rank acceleration with a 1.5% stop",
    },
}


def _policy_eligible_dates(
    entries: pd.DataFrame,
    events: pd.DataFrame,
    raw: pd.DataFrame,
    contract: str,
    forward_start: str,
) -> list[str]:
    """Require complete raw coverage and a complete outcome for this contract."""
    dates = []
    for record in (raw.to_dict(orient="records") if raw is not None else []):
        date = str(record.get("date", ""))
        if date < forward_start or not bool(record.get("full_session_eligible")):
            continue
        entry_ids = set(
            entries.loc[entries["date"].eq(date), "EventId"].astype(str)
        ) if entries is not None and not entries.empty else set()
        closed_ids = set(
            events.loc[
                events["date"].eq(date)
                & events["Contract"].astype(str).eq(contract),
                "EventId",
            ].astype(str)
        ) if events is not None and not events.empty else set()
        if entry_ids and entry_ids.issubset(closed_ids):
            dates.append(date)
    return sorted(set(dates))


def run_challenger(
    *,
    entry_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    output_dir: Path,
    forward_start: str = FORWARD_START,
    daily_goal: float = 10_000.0,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    entries, events = load_depth_events(entry_patterns, outcome_patterns)
    raw = raw_session_coverage(raw_patterns, min_snapshots=1000)
    coverage = depth_session_coverage(entries, events, raw)
    coverage.to_csv(output_dir / "coverage.csv", index=False)

    policy_rows = []
    policy_daily = []
    policy_trades = []
    for name, policy in POLICIES.items():
        dates = _policy_eligible_dates(
            entries, events, raw, str(policy["contract"]), forward_start
        )
        policy_events = (
            events[events["date"].isin(dates)].copy()
            if events is not None and not events.empty
            else events
        )
        daily, trades = replay_depth_slots(
            policy_events,
            dates,
            selector=str(policy["selector"]),
            slots=SLOTS,
            capital=CAPITAL,
            max_trades_per_day=MAX_TRADES_PER_DAY,
        )
        complete_daily = (
            daily[daily["execution_complete"].astype(bool)]
            if not daily.empty and "execution_complete" in daily.columns
            else pd.DataFrame()
        )
        result = metrics(complete_daily, daily_goal)
        policy_rows.append(
            {
                "policy": name,
                **policy,
                "capital": CAPITAL,
                "slots": SLOTS,
                "slot_capital": SLOT_CAPITAL,
                "max_trades_per_day": MAX_TRADES_PER_DAY,
                "forward_start": forward_start,
                "eligible_sessions": len(dates),
                "execution_complete_sessions": int(
                    daily["execution_complete"].astype(bool).sum()
                ) if not daily.empty else 0,
                **result,
            }
        )
        if not daily.empty:
            policy_daily.append(daily.assign(policy=name))
        if not trades.empty:
            policy_trades.append(trades.assign(policy=name))

    summary = pd.DataFrame(policy_rows)
    summary.to_csv(output_dir / "policy_summary.csv", index=False)
    pd.concat(policy_daily, ignore_index=True).to_csv(
        output_dir / "daily.csv", index=False
    ) if policy_daily else pd.DataFrame().to_csv(output_dir / "daily.csv", index=False)
    pd.concat(policy_trades, ignore_index=True).to_csv(
        output_dir / "trades.csv", index=False
    ) if policy_trades else pd.DataFrame().to_csv(output_dir / "trades.csv", index=False)

    status = {
        "evidence": "predeclared_capacity_aware_paper_challenger",
        "status": "collecting_predeclared_capacity_forward_sessions",
        "forward_start": forward_start,
        "capital": CAPITAL,
        "slots": SLOTS,
        "slot_capital": SLOT_CAPITAL,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "policies": POLICIES,
        "policy_count": len(POLICIES),
        "policy_summaries": policy_rows,
        "selection_uses_forward_outcomes": False,
        "paper_only": True,
        "alerts_allowed": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
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
        default="results/Data/predeclared_capacity_challenger",
    )
    parser.add_argument("--forward-start", default=FORWARD_START)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    args = parser.parse_args()
    status = run_challenger(
        entry_patterns=args.entry_glob or [
            "results/Data/full_universe_rank_paper/entries_20*.csv"
        ],
        outcome_patterns=args.outcome_glob or [
            "results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv"
        ],
        raw_patterns=args.raw_glob or [
            "results/Data/live_feature_recorder/live_features_20*.csv"
        ],
        output_dir=Path(args.output_dir),
        forward_start=args.forward_start,
        daily_goal=args.daily_goal,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

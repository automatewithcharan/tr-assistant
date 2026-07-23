#!/usr/bin/env python3
"""Forward-frozen judge for complete full-rank intraday policy families."""

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

from pkscreener.classes.FullUniverseRankContractMatrixObserver import CONTRACTS
from scripts.assess_exact_full_rank_paper import SELECTORS, passes_goal_gate
from scripts.assess_full_rank_capital_allocation import (
    SLOT_COUNTS,
    replay_concurrent_slots,
)
from scripts.assess_full_rank_contract_matrix import (
    load_matrix_events,
    matrix_session_coverage,
)
from scripts.assess_full_rank_feature_gates import FEATURE_GATES, apply_feature_gate
from scripts.assess_full_universe_rank_strategy import raw_session_coverage
from scripts.assess_leader_pullback_reclaim import metrics


INTEGRATED_POLICIES = {
    "opening_fast_scalp": {
        "description": "opening leaders with three short-duration slots",
        "gate": "opening_ranked",
        "selector": "rank_first",
        "contract": "t0_8_s0_5_h30",
        "slots": 3,
    },
    "opening_quality_scalp": {
        "description": "opening leaders ranked by balanced microstructure quality",
        "gate": "opening_ranked",
        "selector": "balanced_rank_acceleration",
        "contract": "t1_0_s0_5_h30",
        "slots": 2,
    },
    "morning_acceleration": {
        "description": "morning rank acceleration with diversified confirmation",
        "gate": "morning_acceleration",
        "selector": "acceleration_first",
        "contract": "t1_5_s1_0_h60",
        "slots": 3,
    },
    "early_rank_acceleration": {
        "description": "strong early rank migration before a three-percent move",
        "gate": "early_rank_acceleration",
        "selector": "acceleration_first",
        "contract": "t2_0_s1_0_h60",
        "slots": 2,
    },
    "balanced_quality": {
        "description": "balanced rank, spread, pullback, and buyer-depth policy",
        "gate": "balanced_quality",
        "selector": "balanced_rank_acceleration",
        "contract": "t1_5_s1_0_h60",
        "slots": 2,
    },
    "sector_rotation": {
        "description": "confirmed sector breadth with three concurrent positions",
        "gate": "sector_rotation",
        "selector": "balanced_rank_acceleration",
        "contract": "t1_5_s1_0_h60",
        "slots": 3,
    },
    "stable_depth_scalp": {
        "description": "persistent buyer depth with a short fixed exit",
        "gate": "stable_buyer_depth",
        "selector": "balanced_rank_acceleration",
        "contract": "t1_0_s0_5_h30",
        "slots": 3,
    },
    "afternoon_fresh_acceleration": {
        "description": "later-starting fresh movers before the final hour",
        "gate": "afternoon_fresh_acceleration",
        "selector": "acceleration_first",
        "contract": "t1_0_s0_5_h30",
        "slots": 3,
    },
    "top10_continuation": {
        "description": "top-ten leaders with a wider continuation contract",
        "gate": "top_10",
        "selector": "rank_first",
        "contract": "t3_0_s1_5_h90",
        "slots": 2,
    },
    "rapid_climber_diversified": {
        "description": "broad rapid climbers spread across five capital slots",
        "gate": "rapid_rank_climber",
        "selector": "balanced_rank_acceleration",
        "contract": "t1_0_s1_0_h60",
        "slots": 5,
    },
}


def validate_policy_registry() -> None:
    for name, policy in INTEGRATED_POLICIES.items():
        if policy["gate"] not in FEATURE_GATES:
            raise ValueError(f"{name}: unknown gate {policy['gate']}")
        if policy["selector"] not in SELECTORS:
            raise ValueError(f"{name}: unknown selector {policy['selector']}")
        if policy["contract"] not in CONTRACTS:
            raise ValueError(f"{name}: unknown contract {policy['contract']}")
        if int(policy["slots"]) not in SLOT_COUNTS:
            raise ValueError(f"{name}: unsupported slot count {policy['slots']}")


def replay_policy(
    events: pd.DataFrame,
    dates: Iterable[str],
    *,
    policy_name: str,
    capital: float,
    max_trades_per_day: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    validate_policy_registry()
    if policy_name not in INTEGRATED_POLICIES:
        raise ValueError(f"unknown integrated policy: {policy_name}")
    policy = INTEGRATED_POLICIES[policy_name]
    filtered = (
        events[events["Contract"].astype(str).eq(policy["contract"])].copy()
        if events is not None and not events.empty
        else pd.DataFrame()
    )
    filtered = apply_feature_gate(filtered, str(policy["gate"]))
    daily, trades = replay_concurrent_slots(
        filtered,
        dates,
        selector=str(policy["selector"]),
        slots=int(policy["slots"]),
        capital=capital,
        max_trades_per_day=max_trades_per_day,
    )
    if not daily.empty:
        daily["policy"] = policy_name
    if not trades.empty:
        trades["policy"] = policy_name
    return daily, trades


def evaluate_policies(
    events: pd.DataFrame,
    dates: Iterable[str],
    *,
    capital: float,
    daily_goal: float,
    max_trades_per_day: int,
) -> list[dict]:
    rows = []
    for name, policy in INTEGRATED_POLICIES.items():
        daily, _ = replay_policy(
            events,
            dates,
            policy_name=name,
            capital=capital,
            max_trades_per_day=max_trades_per_day,
        )
        rows.append(
            {
                "policy": name,
                **policy,
                **metrics(daily, daily_goal),
            }
        )
    return rows


def run_audit(
    *,
    entry_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    output_dir: Path,
    capital: float = 500_000.0,
    daily_goal: float = 10_000.0,
    max_trades_per_day: int = 10,
    selection_sessions_required: int = 12,
    forward_sessions_required: int = 20,
    min_snapshots: int = 1000,
) -> dict:
    validate_policy_registry()
    output_dir.mkdir(parents=True, exist_ok=True)
    entries, all_events = load_matrix_events(entry_patterns, outcome_patterns)
    raw = raw_session_coverage(raw_patterns, min_snapshots=min_snapshots)
    coverage = matrix_session_coverage(entries, all_events, raw)
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = (
        coverage.loc[coverage["eligible"], "date"].astype(str).tolist()
        if not coverage.empty
        else []
    )
    eligible_events = (
        all_events[all_events["date"].isin(eligible_dates)].copy()
        if not all_events.empty
        else all_events
    )
    selection_dates = eligible_dates[:selection_sessions_required]
    selection_rows = evaluate_policies(
        eligible_events,
        selection_dates,
        capital=capital,
        daily_goal=daily_goal,
        max_trades_per_day=max_trades_per_day,
    )
    pd.DataFrame(selection_rows).to_csv(output_dir / "selection_summary.csv", index=False)

    partial_dates = (
        coverage.loc[~coverage["eligible"], "date"].astype(str).tolist()
        if not coverage.empty
        else []
    )
    partial_rows = evaluate_policies(
        all_events,
        partial_dates,
        capital=capital,
        daily_goal=daily_goal,
        max_trades_per_day=max_trades_per_day,
    )
    pd.DataFrame(partial_rows).to_csv(output_dir / "partial_diagnostic.csv", index=False)
    partial_best = max(
        partial_rows,
        key=lambda row: float(row.get("avg_daily_pnl", 0.0) or 0.0),
        default=None,
    )

    frozen_path = output_dir / "frozen_selection.json"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8")) if frozen_path.exists() else {}
    if not frozen and len(selection_dates) >= int(selection_sessions_required):
        passing = [
            row
            for row in selection_rows
            if passes_goal_gate(row, daily_goal, selection_sessions_required)
        ]
        winner = max(
            passing,
            key=lambda row: float(row.get("avg_daily_pnl", 0.0) or 0.0),
            default=None,
        )
        winner_name = str(winner["policy"]) if winner else ""
        frozen = {
            "selection_status": "passed" if winner else "rejected",
            "policy": winner_name,
            "policy_definition": INTEGRATED_POLICIES.get(winner_name, {}),
            "selection_start": selection_dates[0],
            "selection_end": selection_dates[-1],
            "selection_dates": selection_dates,
            "policy_registry": INTEGRATED_POLICIES,
            "result": winner or {},
            "real_orders_allowed": False,
        }
        frozen_path.write_text(json.dumps(frozen, indent=2), encoding="utf-8")

    forward_dates: list[str] = []
    forward_daily = pd.DataFrame()
    forward_trades = pd.DataFrame()
    forward_result = metrics(pd.DataFrame(), daily_goal)
    frozen_policy = str(frozen.get("policy", ""))
    if frozen_policy in INTEGRATED_POLICIES:
        selection_end = str(frozen.get("selection_end", ""))
        forward_dates = [date for date in eligible_dates if date > selection_end]
        forward_daily, forward_trades = replay_policy(
            eligible_events,
            forward_dates,
            policy_name=frozen_policy,
            capital=capital,
            max_trades_per_day=max_trades_per_day,
        )
        forward_result = metrics(forward_daily, daily_goal)
    forward_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    forward_trades.to_csv(output_dir / "forward_trades.csv", index=False)

    accepted = bool(
        frozen_policy
        and len(forward_dates) >= int(forward_sessions_required)
        and passes_goal_gate(forward_result, daily_goal, forward_sessions_required)
    )
    if len(selection_dates) < int(selection_sessions_required):
        status_name = "collecting_integrated_policy_selection_sessions"
    elif frozen.get("selection_status") != "passed":
        status_name = "integrated_policy_selection_rejected"
    elif len(forward_dates) < int(forward_sessions_required):
        status_name = "collecting_integrated_policy_forward_sessions"
    else:
        status_name = "accepted_integrated_policy_forward" if accepted else "integrated_policy_forward_rejected"
    status = {
        "evidence": "full_universe_rank_integrated_policy_registry",
        "status": status_name,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "max_trades_per_day": int(max_trades_per_day),
        "policies": len(INTEGRATED_POLICIES),
        "policy_registry": INTEGRATED_POLICIES,
        "selection_sessions_required": int(selection_sessions_required),
        "forward_sessions_required": int(forward_sessions_required),
        "raw_sessions": int(len(raw)),
        "eligible_sessions": int(len(eligible_dates)),
        "partial_or_incomplete_sessions": int(len(coverage) - len(eligible_dates)),
        "selection_dates": selection_dates,
        "frozen_selection": frozen,
        "forward_dates": forward_dates,
        "forward": forward_result,
        "best_partial_diagnostic": ({**partial_best, "excluded_from_evidence": True} if partial_best else {}),
        "accepted": accepted,
        "paper_promotion_allowed": accepted,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry-glob", action="append", default=[])
    parser.add_argument("--outcome-glob", action="append", default=[])
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument("--output-dir", default="results/Data/full_universe_rank_integrated_policies")
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--selection-sessions", type=int, default=12)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()
    status = run_audit(
        entry_patterns=args.entry_glob or ["results/Data/full_universe_rank_paper/entries_20*.csv"],
        outcome_patterns=args.outcome_glob or ["results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv"],
        raw_patterns=args.raw_glob or ["results/Data/live_feature_recorder/live_features_20*.csv"],
        output_dir=Path(args.output_dir),
        capital=args.capital,
        daily_goal=args.daily_goal,
        max_trades_per_day=args.max_trades,
        selection_sessions_required=args.selection_sessions,
        forward_sessions_required=args.forward_sessions,
        min_snapshots=args.min_snapshots,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

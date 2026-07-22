#!/usr/bin/env python3
"""Frozen one-slot judge for causal full-rank candidate feature gates."""

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
    SELECTORS,
    exact_session_coverage,
    load_exact_events,
    passes_goal_gate,
    replay_one_slot,
)
from scripts.assess_full_universe_rank_strategy import raw_session_coverage
from scripts.assess_leader_pullback_reclaim import metrics


FEATURE_GATES = {
    "all_buyable": "all executable rank labels",
    "rapid_rank_climber": "explicit rapid rank climbers only",
    "top_10": "current day-change rank 1-10",
    "top_25": "current day-change rank 1-25",
    "early_rank_acceleration": "rank improvement >=25, move acceleration >=0.15%, day move <=3%",
    "sector_rotation": "two sector peers above 2%, sector breadth >=50%, day move <=3.5%",
    "stable_buyer_depth": "stable order book and current/window buyer-seller ratio >=1.2x",
    "tight_high_turnover": "spread <=10 bps and traded value >=Rs10Cr",
    "balanced_quality": "top75, <=3% move, <=0.8% pullback, <=15bps spread and B/S >=1x",
}


def _number(frame: pd.DataFrame, column: str, default: float = 0.0) -> pd.Series:
    values = frame[column] if column in frame.columns else pd.Series(default, index=frame.index)
    return pd.to_numeric(values, errors="coerce").fillna(default)


def _truthy(frame: pd.DataFrame, column: str) -> pd.Series:
    values = frame[column] if column in frame.columns else pd.Series(False, index=frame.index)
    return values.fillna("").astype(str).str.strip().str.lower().isin(
        {"1", "1.0", "true", "yes", "y", "on"}
    )


def apply_feature_gate(frame: pd.DataFrame, gate: str) -> pd.DataFrame:
    if gate not in FEATURE_GATES:
        raise ValueError(f"unknown feature gate: {gate}")
    if frame is None or frame.empty or gate == "all_buyable":
        return frame.copy() if frame is not None else pd.DataFrame()
    rank = _number(frame, "DayChangeRank", 9999)
    move = _number(frame, "DayChangePct")
    improvement = _number(frame, "RankImprovement")
    delta = _number(frame, "DayChangeDeltaPct")
    sector_top2 = _number(frame, "SectorTop2Count")
    sector_breadth = _number(frame, "SectorBreadthPct")
    bs = _number(frame, "BuyerSellerRatio")
    bs_median = _number(frame, "BuyerSellerMedianWindow")
    spread = _number(frame, "EntrySpreadBps", 9999)
    turnover = _number(frame, "TradedValue")
    pullback = _number(frame, "PullbackFromHighPct", 9999)
    if gate == "rapid_rank_climber":
        mask = _truthy(frame, "RapidRankClimber")
    elif gate == "top_10":
        mask = rank.between(1, 10)
    elif gate == "top_25":
        mask = rank.between(1, 25)
    elif gate == "early_rank_acceleration":
        mask = improvement.ge(25) & delta.ge(0.15) & move.le(3.0)
    elif gate == "sector_rotation":
        mask = sector_top2.ge(2) & sector_breadth.ge(50.0) & move.le(3.5)
    elif gate == "stable_buyer_depth":
        mask = _truthy(frame, "OrderBookStable") & bs.ge(1.2) & bs_median.ge(1.2)
    elif gate == "tight_high_turnover":
        mask = spread.le(10.0) & turnover.ge(100_000_000.0)
    else:
        mask = (
            rank.between(1, 75)
            & move.between(0.5, 3.0)
            & pullback.le(0.8)
            & spread.le(15.0)
            & bs.ge(1.0)
        )
    return frame[mask].copy()


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
    min_snapshots: int = 300,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    entries, all_events = load_exact_events(entry_patterns, outcome_patterns)
    raw = raw_session_coverage(raw_patterns, min_snapshots=min_snapshots)
    coverage = exact_session_coverage(entries, all_events, raw)
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = coverage.loc[coverage["eligible"], "date"].astype(str).tolist() if not coverage.empty else []
    events = all_events[all_events["date"].isin(eligible_dates)].copy() if not all_events.empty else all_events
    selection_dates = eligible_dates[:selection_sessions_required]

    result_rows = []
    for gate in FEATURE_GATES:
        gated = apply_feature_gate(events, gate)
        for selector in SELECTORS:
            daily, _ = replay_one_slot(
                gated, selection_dates, selector=selector,
                capital=capital, max_trades_per_day=max_trades_per_day,
            )
            result_rows.append(
                {
                    "gate": gate,
                    "gate_description": FEATURE_GATES[gate],
                    "selector": selector,
                    "candidate_rows": int(len(gated)),
                    **metrics(daily, daily_goal),
                }
            )
    pd.DataFrame(result_rows).to_csv(output_dir / "selection_summary.csv", index=False)

    frozen_path = output_dir / "frozen_selection.json"
    frozen = {}
    if frozen_path.exists():
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    elif len(selection_dates) >= int(selection_sessions_required):
        passing = [
            row for row in result_rows
            if passes_goal_gate(row, daily_goal, selection_sessions_required)
        ]
        winner = max(passing, key=lambda row: row["avg_daily_pnl"], default=None)
        frozen = {
            "selection_status": "passed" if winner else "rejected",
            "gate": str(winner["gate"]) if winner else "",
            "selector": str(winner["selector"]) if winner else "",
            "selection_start": selection_dates[0],
            "selection_end": selection_dates[-1],
            "selection_dates": selection_dates,
            "feature_gate_definitions": FEATURE_GATES,
            "selector_definitions": SELECTORS,
            "result": winner or {},
            "real_orders_allowed": False,
        }
        frozen_path.write_text(json.dumps(frozen, indent=2), encoding="utf-8")

    forward_dates = []
    forward_daily = pd.DataFrame()
    forward_trades = pd.DataFrame()
    forward_result = metrics(pd.DataFrame(), daily_goal)
    frozen_gate = str(frozen.get("gate", ""))
    frozen_selector = str(frozen.get("selector", ""))
    if frozen_gate and frozen_selector:
        selection_end = str(frozen.get("selection_end", ""))
        forward_dates = [date for date in eligible_dates if date > selection_end]
        forward_events = apply_feature_gate(events, frozen_gate)
        forward_daily, forward_trades = replay_one_slot(
            forward_events, forward_dates, selector=frozen_selector,
            capital=capital, max_trades_per_day=max_trades_per_day,
        )
        forward_result = metrics(forward_daily, daily_goal)
    forward_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    forward_trades.to_csv(output_dir / "forward_trades.csv", index=False)
    accepted = bool(
        frozen_gate
        and len(forward_dates) >= int(forward_sessions_required)
        and passes_goal_gate(forward_result, daily_goal, forward_sessions_required)
    )
    if len(selection_dates) < int(selection_sessions_required):
        status_name = "collecting_feature_gate_selection_sessions"
    elif frozen.get("selection_status") != "passed":
        status_name = "feature_gate_selection_rejected"
    elif len(forward_dates) < int(forward_sessions_required):
        status_name = "collecting_feature_gate_forward_sessions"
    else:
        status_name = "accepted_feature_gate_forward" if accepted else "feature_gate_forward_rejected"
    status = {
        "evidence": "full_universe_rank_exact_feature_gate_one_slot",
        "status": status_name,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "max_trades_per_day": int(max_trades_per_day),
        "feature_gates": len(FEATURE_GATES),
        "selectors": len(SELECTORS),
        "predeclared_combinations": len(FEATURE_GATES) * len(SELECTORS),
        "selection_sessions_required": int(selection_sessions_required),
        "forward_sessions_required": int(forward_sessions_required),
        "raw_sessions": int(len(raw)),
        "eligible_sessions": int(len(eligible_dates)),
        "partial_or_incomplete_sessions": int(len(coverage) - len(eligible_dates)),
        "entry_labels": int(len(entries)),
        "closed_labels": int(len(all_events)),
        "selection_dates": selection_dates,
        "frozen_selection": frozen,
        "forward_dates": forward_dates,
        "forward": forward_result,
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
    parser.add_argument("--output-dir", default="results/Data/full_universe_rank_feature_strategy")
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--selection-sessions", type=int, default=12)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--min-snapshots", type=int, default=300)
    args = parser.parse_args()
    status = run_audit(
        entry_patterns=args.entry_glob or ["results/Data/full_universe_rank_paper/entries_20*.csv"],
        outcome_patterns=args.outcome_glob or ["results/Data/full_universe_rank_paper/outcomes_20*.csv"],
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

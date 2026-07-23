#!/usr/bin/env python3
"""Forward-frozen capital-allocation judge for exact full-rank outcomes.

The strict one-slot judge remains the baseline. This matrix tests whether a
fixed number of concurrent, equal-sized slots prevents one early position from
blocking later leaders while keeping total deployed capital at or below the
account budget.
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

from pkscreener.classes.FullUniverseRankContractMatrixObserver import CONTRACTS
from scripts.assess_exact_full_rank_paper import SELECTORS, passes_goal_gate
from scripts.assess_full_rank_contract_matrix import (
    load_matrix_events,
    matrix_session_coverage,
)
from scripts.assess_full_universe_rank_strategy import raw_session_coverage
from scripts.assess_leader_pullback_reclaim import metrics


SLOT_COUNTS = (1, 2, 3, 5)


def replay_concurrent_slots(
    events: pd.DataFrame,
    dates: Iterable[str],
    *,
    selector: str,
    slots: int,
    capital: float = 500_000.0,
    max_trades_per_day: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replay causal minute batches with fixed, non-leveraged capital slots."""
    if selector not in SELECTORS:
        raise ValueError(f"unknown selector: {selector}")
    if int(slots) not in SLOT_COUNTS:
        raise ValueError(f"unsupported slot count: {slots}")
    slot_capital = float(capital) / int(slots)
    config = SELECTORS[selector]
    daily_rows = []
    trade_rows = []

    for session_date in sorted({str(value) for value in dates}):
        day = (
            events[events["date"].eq(session_date)].copy()
            if events is not None and not events.empty
            else pd.DataFrame()
        )
        if not day.empty:
            day["entry_batch"] = day["entry_time"].dt.floor("min")
            day.sort_values(
                ["entry_batch", *config["columns"]],
                ascending=[True, *config["ascending"]],
                inplace=True,
            )

        active_exits: list[pd.Timestamp] = []
        traded_symbols: set[str] = set()
        day_pnl = 0.0
        day_trades = 0
        peak_concurrent = 0

        if not day.empty:
            for batch, visible in day.groupby("entry_batch", sort=True):
                active_exits = [
                    exit_at
                    for exit_at in active_exits
                    if pd.Timestamp(exit_at) > pd.Timestamp(batch)
                ]
                available = int(slots) - len(active_exits)
                if available <= 0 or day_trades >= int(max_trades_per_day):
                    continue
                visible = visible[~visible["Symbol"].isin(traded_symbols)]
                if visible.empty:
                    continue

                for _, pick in visible.iterrows():
                    symbol = str(pick["Symbol"])
                    if symbol in traded_symbols:
                        continue
                    if available <= 0 or day_trades >= int(max_trades_per_day):
                        break
                    entry_ask = float(pick["EntryAsk"])
                    exit_bid = float(pick["ExitBid"])
                    quantity = int(slot_capital // entry_ask) if entry_ask > 0 else 0
                    if quantity < 1:
                        continue
                    stress_pct = float(pick.get("StressPct", 0.0) or 0.0)
                    deployed = entry_ask * quantity
                    gross_pnl = (exit_bid - entry_ask) * quantity
                    stress_cost = deployed * stress_pct / 100.0
                    net_pnl = gross_pnl - stress_cost
                    account_return_pct = net_pnl / float(capital) * 100.0
                    position_return_pct = net_pnl / deployed * 100.0 if deployed else 0.0
                    trade_rows.append(
                        {
                            "date": session_date,
                            "contract": str(pick.get("Contract", "")),
                            "selector": selector,
                            "slots": int(slots),
                            "symbol": symbol,
                            "cohort": str(pick.get("Cohort", "")),
                            "entry_time": pick["entry_time"],
                            "exit_time": pick["exit_time"],
                            "exit_reason": str(pick.get("ExitReason", "")),
                            "entry_ask": round(entry_ask, 2),
                            "exit_bid": round(exit_bid, 2),
                            "slot_capital": round(slot_capital, 2),
                            "deployed_capital": round(deployed, 2),
                            "quantity": quantity,
                            "day_change_rank": int(pick["DayChangeRank"]),
                            "position_return_pct": round(position_return_pct, 6),
                            "account_return_pct": round(account_return_pct, 6),
                            "pnl": round(net_pnl, 2),
                        }
                    )
                    active_exits.append(pd.Timestamp(pick["exit_time"]))
                    traded_symbols.add(symbol)
                    available -= 1
                    day_trades += 1
                    day_pnl += net_pnl
                    peak_concurrent = max(peak_concurrent, len(active_exits))

        daily_rows.append(
            {
                "date": session_date,
                "contract": (
                    str(day["Contract"].iloc[0])
                    if not day.empty and "Contract" in day.columns
                    else ""
                ),
                "selector": selector,
                "slots": int(slots),
                "slot_capital": round(slot_capital, 2),
                "trades": day_trades,
                "peak_concurrent": peak_concurrent,
                "pnl": round(day_pnl, 2),
                "daily_return_pct": round(day_pnl / float(capital) * 100.0, 6),
            }
        )
    return pd.DataFrame(daily_rows), pd.DataFrame(trade_rows)


def evaluate_combinations(
    events: pd.DataFrame,
    dates: Iterable[str],
    *,
    capital: float,
    daily_goal: float,
    max_trades_per_day: int,
) -> list[dict]:
    rows = []
    for contract in CONTRACTS:
        contract_events = (
            events[events["Contract"].astype(str).eq(contract)].copy()
            if events is not None and not events.empty
            else pd.DataFrame()
        )
        for selector in SELECTORS:
            for slots in SLOT_COUNTS:
                daily, _ = replay_concurrent_slots(
                    contract_events,
                    dates,
                    selector=selector,
                    slots=slots,
                    capital=capital,
                    max_trades_per_day=max_trades_per_day,
                )
                rows.append(
                    {
                        "contract": contract,
                        "selector": selector,
                        "slots": slots,
                        "slot_capital": round(float(capital) / slots, 2),
                        "target_pct": CONTRACTS[contract]["target_pct"],
                        "stop_pct": CONTRACTS[contract]["stop_pct"],
                        "horizon_minutes": CONTRACTS[contract]["horizon_minutes"],
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
    selection_rows = evaluate_combinations(
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
    partial_rows = evaluate_combinations(
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
        frozen = {
            "selection_status": "passed" if winner else "rejected",
            "contract": str(winner["contract"]) if winner else "",
            "selector": str(winner["selector"]) if winner else "",
            "slots": int(winner["slots"]) if winner else 0,
            "selection_start": selection_dates[0],
            "selection_end": selection_dates[-1],
            "selection_dates": selection_dates,
            "slot_counts": list(SLOT_COUNTS),
            "contract_definitions": CONTRACTS,
            "selector_definitions": SELECTORS,
            "result": winner or {},
            "real_orders_allowed": False,
        }
        frozen_path.write_text(json.dumps(frozen, indent=2), encoding="utf-8")

    forward_dates: list[str] = []
    forward_daily = pd.DataFrame()
    forward_trades = pd.DataFrame()
    forward_result = metrics(pd.DataFrame(), daily_goal)
    frozen_contract = str(frozen.get("contract", ""))
    frozen_selector = str(frozen.get("selector", ""))
    frozen_slots = int(frozen.get("slots", 0) or 0)
    if frozen_contract and frozen_selector and frozen_slots in SLOT_COUNTS:
        selection_end = str(frozen.get("selection_end", ""))
        forward_dates = [date for date in eligible_dates if date > selection_end]
        forward_events = eligible_events[
            eligible_events["Contract"].astype(str).eq(frozen_contract)
        ].copy()
        forward_daily, forward_trades = replay_concurrent_slots(
            forward_events,
            forward_dates,
            selector=frozen_selector,
            slots=frozen_slots,
            capital=capital,
            max_trades_per_day=max_trades_per_day,
        )
        forward_result = metrics(forward_daily, daily_goal)
    forward_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    forward_trades.to_csv(output_dir / "forward_trades.csv", index=False)

    accepted = bool(
        frozen_contract
        and len(forward_dates) >= int(forward_sessions_required)
        and passes_goal_gate(forward_result, daily_goal, forward_sessions_required)
    )
    if len(selection_dates) < int(selection_sessions_required):
        status_name = "collecting_capital_allocation_selection_sessions"
    elif frozen.get("selection_status") != "passed":
        status_name = "capital_allocation_selection_rejected"
    elif len(forward_dates) < int(forward_sessions_required):
        status_name = "collecting_capital_allocation_forward_sessions"
    else:
        status_name = (
            "accepted_capital_allocation_forward"
            if accepted
            else "capital_allocation_forward_rejected"
        )
    status = {
        "evidence": "full_universe_rank_exact_concurrent_capital_allocation",
        "status": status_name,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "max_trades_per_day": int(max_trades_per_day),
        "contracts": len(CONTRACTS),
        "selectors": len(SELECTORS),
        "slot_counts": list(SLOT_COUNTS),
        "predeclared_combinations": len(CONTRACTS) * len(SELECTORS) * len(SLOT_COUNTS),
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
    parser.add_argument("--output-dir", default="results/Data/full_universe_rank_capital_allocation")
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

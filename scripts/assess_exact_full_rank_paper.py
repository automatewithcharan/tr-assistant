#!/usr/bin/env python3
"""Capital-constrained judge for exact full-rank ask-to-bid paper outcomes."""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assess_full_universe_rank_strategy import raw_session_coverage
from scripts.assess_leader_pullback_reclaim import metrics


SELECTORS = {
    "rank_first": {
        "description": "lowest visible day-change rank, then tightest spread",
        "columns": ["DayChangeRank", "EntrySpreadBps", "Symbol"],
        "ascending": [True, True, True],
    },
    "acceleration_first": {
        "description": "highest rank velocity and rank improvement, then rank",
        "columns": ["RankVelocityPerMinute", "RankImprovement", "DayChangeRank", "Symbol"],
        "ascending": [False, False, True, True],
    },
    "early_move_first": {
        "description": "smallest positive day move, then strongest rank",
        "columns": ["DayChangePct", "DayChangeRank", "EntrySpreadBps", "Symbol"],
        "ascending": [True, True, True, True],
    },
    "balanced_rank_acceleration": {
        "description": "fixed causal blend of rank, acceleration, spread and buyer depth",
        "columns": ["BalancedScore", "DayChangeRank", "Symbol"],
        "ascending": [False, True, True],
    },
}

NUMERIC_COLUMNS = [
    "EntryAsk", "EntryBid", "EntrySpreadBps", "Quantity", "Capital",
    "DayChangePct", "UniverseSize", "DayChangeRank", "DayChangeRankPct",
    "PreviousDayChangeRank", "RankImprovement", "RankVelocityPerMinute",
    "DayChangeDeltaPct", "TradedValue", "BuyerSellerRatio", "NetReturnPct",
    "GrossReturnPct", "NetPnl", "GrossPnl", "StressCost",
]


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {path for pattern in patterns for path in glob.glob(str(pattern), recursive=True)}
    )


def load_exact_events(
    entry_patterns: Iterable[str], outcome_patterns: Iterable[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return all entry labels and the subset with an exact observed exit."""
    entries = []
    outcomes = []
    for path in _files(entry_patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if {"EventId", "Date", "EntryAt", "Symbol"}.issubset(frame.columns):
            entries.append(frame)
    for path in _files(outcome_patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if {"EventId", "ExitAt", "ExitReason", "NetReturnPct"}.issubset(frame.columns):
            outcomes.append(frame)
    if not entries:
        return pd.DataFrame(), pd.DataFrame()
    entry_data = pd.concat(entries, ignore_index=True)
    entry_data = entry_data.drop_duplicates("EventId", keep="first").copy()
    entry_data["date"] = entry_data["Date"].astype(str)
    entry_data["entry_time"] = pd.to_datetime(entry_data["EntryAt"], errors="coerce")
    entry_data["Symbol"] = entry_data["Symbol"].astype(str).str.strip().str.upper()
    for column in NUMERIC_COLUMNS:
        if column not in entry_data.columns:
            entry_data[column] = 0.0
        entry_data[column] = pd.to_numeric(entry_data[column], errors="coerce")
    entry_data["BalancedScore"] = balanced_score(entry_data)

    if not outcomes:
        return entry_data, pd.DataFrame()
    outcome_data = pd.concat(outcomes, ignore_index=True)
    outcome_data = outcome_data.drop_duplicates("EventId", keep="last").copy()
    outcome_columns = [
        "EventId", "ExitAt", "ExitReason", "ExitBid", "HoldMinutes",
        "GrossReturnPct", "NetReturnPct", "GrossPnl", "StressCost", "NetPnl",
    ]
    for column in outcome_columns:
        if column not in outcome_data.columns:
            outcome_data[column] = pd.NA
    merged = entry_data.merge(
        outcome_data[outcome_columns], on="EventId", how="inner", suffixes=("", "_outcome")
    )
    merged["exit_time"] = pd.to_datetime(merged["ExitAt"], errors="coerce")
    for column in ("ExitBid", "HoldMinutes", "GrossReturnPct_outcome", "NetReturnPct_outcome", "GrossPnl_outcome", "StressCost_outcome", "NetPnl_outcome"):
        if column in merged.columns:
            merged[column] = pd.to_numeric(merged[column], errors="coerce")
    # Prefer the authoritative exit ledger when names overlap the entry schema.
    for column in ("GrossReturnPct", "NetReturnPct", "GrossPnl", "StressCost", "NetPnl"):
        outcome_column = f"{column}_outcome"
        if outcome_column in merged.columns:
            merged[column] = merged[outcome_column]
    merged = merged[
        merged["entry_time"].notna()
        & merged["exit_time"].notna()
        & merged["NetReturnPct"].notna()
        & (merged["exit_time"] > merged["entry_time"])
    ].copy()
    return entry_data, merged.sort_values(["date", "entry_time", "Symbol"])


def balanced_score(frame: pd.DataFrame) -> pd.Series:
    universe = pd.to_numeric(frame.get("UniverseSize", 0), errors="coerce").fillna(0).clip(lower=1)
    rank = pd.to_numeric(frame.get("DayChangeRank", 0), errors="coerce").fillna(universe)
    improvement = pd.to_numeric(frame.get("RankImprovement", 0), errors="coerce").fillna(0).clip(lower=0)
    delta = pd.to_numeric(frame.get("DayChangeDeltaPct", 0), errors="coerce").fillna(0).clip(lower=0, upper=1)
    spread = pd.to_numeric(frame.get("EntrySpreadBps", 20), errors="coerce").fillna(20).clip(lower=0, upper=20)
    buyer_ratio = pd.to_numeric(frame.get("BuyerSellerRatio", 0), errors="coerce").fillna(0).clip(lower=0, upper=5)
    rank_quality = (1.0 - rank / universe).clip(lower=0, upper=1)
    acceleration = (improvement / universe).clip(lower=0, upper=1)
    return (
        0.35 * rank_quality
        + 0.25 * acceleration
        + 0.20 * delta
        + 0.10 * (1.0 - spread / 20.0)
        + 0.10 * (buyer_ratio / 5.0)
    )


def exact_session_coverage(
    entries: pd.DataFrame, outcomes: pd.DataFrame, raw_coverage: pd.DataFrame
) -> pd.DataFrame:
    rows = []
    if raw_coverage is None or raw_coverage.empty:
        return pd.DataFrame(
            columns=["date", "raw_full_session", "entries", "outcomes", "outcome_coverage_pct", "eligible"]
        )
    outcome_ids = set(outcomes.get("EventId", pd.Series(dtype=str)).astype(str))
    for raw in raw_coverage.to_dict(orient="records"):
        date = str(raw.get("date", ""))
        day_entries = entries[entries["date"].eq(date)] if not entries.empty else pd.DataFrame()
        entry_ids = set(day_entries.get("EventId", pd.Series(dtype=str)).astype(str))
        closed = len(entry_ids & outcome_ids)
        total = len(entry_ids)
        coverage = closed / total * 100.0 if total else 100.0
        raw_full = bool(raw.get("full_session_eligible", False))
        rows.append(
            {
                "date": date,
                "raw_full_session": raw_full,
                "entries": total,
                "outcomes": closed,
                "outcome_coverage_pct": round(coverage, 2),
                "eligible": bool(raw_full and coverage >= 100.0),
            }
        )
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def replay_one_slot(
    events: pd.DataFrame,
    dates: Iterable[str],
    *,
    selector: str,
    capital: float = 500_000.0,
    max_trades_per_day: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if selector not in SELECTORS:
        raise ValueError(f"unknown selector: {selector}")
    config = SELECTORS[selector]
    daily_rows = []
    trades = []
    for date in sorted({str(value) for value in dates}):
        day = events[events["date"].eq(date)].copy() if not events.empty else pd.DataFrame()
        if not day.empty:
            day["entry_batch"] = day["entry_time"].dt.floor("min")
            day.sort_values(
                ["entry_batch", *config["columns"]],
                ascending=[True, *config["ascending"]],
                inplace=True,
            )
        active_exit = None
        traded_symbols = set()
        day_pnl = 0.0
        day_trades = 0
        if not day.empty:
            for batch, visible in day.groupby("entry_batch", sort=True):
                if active_exit is not None and pd.Timestamp(active_exit) > pd.Timestamp(batch):
                    continue
                if day_trades >= int(max_trades_per_day):
                    break
                visible = visible[~visible["Symbol"].isin(traded_symbols)]
                if visible.empty:
                    continue
                pick = visible.iloc[0]
                net_return = float(pick["NetReturnPct"])
                pnl = float(capital) * net_return / 100.0
                trades.append(
                    {
                        "date": date,
                        "selector": selector,
                        "symbol": str(pick["Symbol"]),
                        "cohort": str(pick.get("Cohort", "")),
                        "entry_time": pick["entry_time"],
                        "exit_time": pick["exit_time"],
                        "exit_reason": str(pick.get("ExitReason", "")),
                        "entry_ask": round(float(pick["EntryAsk"]), 2),
                        "exit_bid": round(float(pick["ExitBid"]), 2),
                        "day_change_rank": int(pick["DayChangeRank"]),
                        "net_return_pct": round(net_return, 6),
                        "pnl": round(pnl, 2),
                    }
                )
                active_exit = pick["exit_time"]
                traded_symbols.add(str(pick["Symbol"]))
                day_trades += 1
                day_pnl += pnl
        daily_rows.append(
            {
                "date": date,
                "selector": selector,
                "trades": day_trades,
                "pnl": round(day_pnl, 2),
                "daily_return_pct": round(day_pnl / float(capital) * 100.0, 6),
            }
        )
    return pd.DataFrame(daily_rows), pd.DataFrame(trades)


def passes_goal_gate(result: dict, daily_goal: float, minimum_sessions: int) -> bool:
    return bool(
        result.get("sessions", 0) >= int(minimum_sessions)
        and result.get("trade_sessions", 0) >= max(1, int(minimum_sessions * 0.75))
        and result.get("trades", 0) >= max(10, int(minimum_sessions * 2.5))
        and result.get("avg_daily_pnl", 0.0) >= float(daily_goal)
        and result.get("lower_95_pct") is not None
        and result.get("lower_95_pct") > 0.0
        and result.get("block_floor_pct") is not None
        and result.get("block_floor_pct") > 0.0
        and result.get("goal_hit_pct", 0.0) >= 50.0
        and result.get("worst_session_pct", -999.0) >= -2.5
    )


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
    entries, events = load_exact_events(entry_patterns, outcome_patterns)
    raw = raw_session_coverage(raw_patterns, min_snapshots=min_snapshots)
    coverage = exact_session_coverage(entries, events, raw)
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = coverage.loc[coverage["eligible"], "date"].astype(str).tolist() if not coverage.empty else []
    all_closed_labels = int(len(events))
    events = events[events["date"].isin(eligible_dates)].copy() if not events.empty else events

    frozen_path = output_dir / "frozen_selection.json"
    selection_dates = eligible_dates[:selection_sessions_required]
    selector_results = {}
    for name in SELECTORS:
        daily, trades = replay_one_slot(
            events, selection_dates, selector=name, capital=capital,
            max_trades_per_day=max_trades_per_day,
        )
        selector_results[name] = metrics(daily, daily_goal)
        daily.to_csv(output_dir / f"selection_{name}_daily.csv", index=False)
        trades.to_csv(output_dir / f"selection_{name}_trades.csv", index=False)
    pd.DataFrame(
        [{"selector": name, "description": SELECTORS[name]["description"], **result}
         for name, result in selector_results.items()]
    ).to_csv(output_dir / "selection_summary.csv", index=False)

    frozen = {}
    if frozen_path.exists():
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    elif len(selection_dates) >= int(selection_sessions_required):
        passing = [
            name for name, result in selector_results.items()
            if passes_goal_gate(result, daily_goal, selection_sessions_required)
        ]
        winner = max(
            passing,
            key=lambda name: selector_results[name]["avg_daily_pnl"],
            default="",
        )
        frozen = {
            "selection_status": "passed" if winner else "rejected",
            "selector": winner,
            "selection_start": selection_dates[0],
            "selection_end": selection_dates[-1],
            "selection_dates": selection_dates,
            "selector_definitions": SELECTORS,
            "result": selector_results.get(winner, {}),
            "real_orders_allowed": False,
        }
        frozen_path.write_text(json.dumps(frozen, indent=2), encoding="utf-8")

    forward_dates = []
    forward_daily = pd.DataFrame()
    forward_trades = pd.DataFrame()
    forward_result = metrics(pd.DataFrame(), daily_goal)
    frozen_selector = str(frozen.get("selector", ""))
    if frozen_selector:
        selection_end = str(frozen.get("selection_end", ""))
        forward_dates = [date for date in eligible_dates if date > selection_end]
        forward_daily, forward_trades = replay_one_slot(
            events, forward_dates, selector=frozen_selector, capital=capital,
            max_trades_per_day=max_trades_per_day,
        )
        forward_result = metrics(forward_daily, daily_goal)
    forward_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    forward_trades.to_csv(output_dir / "forward_trades.csv", index=False)

    accepted = bool(
        frozen_selector
        and len(forward_dates) >= int(forward_sessions_required)
        and passes_goal_gate(forward_result, daily_goal, forward_sessions_required)
    )
    if len(selection_dates) < int(selection_sessions_required):
        status_name = "collecting_exact_selection_sessions"
    elif frozen.get("selection_status") != "passed":
        status_name = "exact_selection_rejected"
    elif len(forward_dates) < int(forward_sessions_required):
        status_name = "collecting_exact_forward_sessions"
    else:
        status_name = "accepted_exact_forward" if accepted else "exact_forward_rejected"
    status = {
        "evidence": "full_universe_rank_exact_ask_to_bid_one_slot",
        "status": status_name,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "max_trades_per_day": int(max_trades_per_day),
        "selection_sessions_required": int(selection_sessions_required),
        "forward_sessions_required": int(forward_sessions_required),
        "raw_sessions": int(len(raw)),
        "eligible_exact_sessions": int(len(eligible_dates)),
        "partial_or_incomplete_sessions": int(len(coverage) - len(eligible_dates)),
        "entry_labels": int(len(entries)),
        "closed_labels": all_closed_labels,
        "eligible_exact_outcomes": int(len(events)),
        "selection_dates": selection_dates,
        "selection_results": selector_results,
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
    parser.add_argument("--output-dir", default="results/Data/full_universe_rank_exact_strategy")
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--selection-sessions", type=int, default=12)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--min-snapshots", type=int, default=300)
    args = parser.parse_args()
    root = "results/Data/full_universe_rank_paper"
    status = run_audit(
        entry_patterns=args.entry_glob or [f"{root}/entries_20*.csv"],
        outcome_patterns=args.outcome_glob or [f"{root}/outcomes_20*.csv"],
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

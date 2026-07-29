#!/usr/bin/env python3
"""Frozen one-slot judge for exact full-rank entry timing variants."""

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

from pkscreener.classes.FullUniverseRankEntryTimingObserver import ENTRY_VARIANTS
from scripts.assess_exact_full_rank_paper import (
    SELECTORS,
    load_exact_events,
    passes_goal_gate,
    replay_one_slot,
)
from scripts.assess_full_universe_rank_strategy import raw_session_coverage
from scripts.assess_leader_pullback_reclaim import metrics


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {path for pattern in patterns for path in glob.glob(str(pattern), recursive=True)}
    )


def _load_frames(patterns: Iterable[str], required: set[str]) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if required.issubset(frame.columns):
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_timing_events(
    entry_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
    nonfill_patterns: Iterable[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base_entries, _ = load_exact_events(
        entry_patterns,
        [],
        require_top_level_fillable=False,
    )
    outcomes = _load_frames(
        outcome_patterns,
        {
            "TimingId", "EventId", "Variant", "EntryAt", "ExitAt",
            "NetReturnPct", "EntryTopLevelFillable",
        },
    )
    nonfills = _load_frames(
        nonfill_patterns,
        {"TimingId", "EventId", "Variant", "ResolvedAt", "Reason"},
    )
    if not nonfills.empty:
        nonfills = nonfills.drop_duplicates("TimingId", keep="last")
    if base_entries.empty or outcomes.empty:
        return base_entries, pd.DataFrame(), nonfills
    outcomes = outcomes.drop_duplicates("TimingId", keep="last")
    outcome_columns = [
        "TimingId", "EventId", "Variant", "SignalAt", "EntryAt", "ExitAt",
        "ExitReason", "EntryAsk", "ExitBid", "EntryDelayMinutes", "HoldMinutes",
        "EntryAskQty", "RequestedQuantity", "EntryTopLevelFillable",
        "GrossReturnPct", "NetReturnPct", "GrossPnl", "StressCost", "NetPnl",
    ]
    for column in outcome_columns:
        if column not in outcomes.columns:
            outcomes[column] = pd.NA
    entry_only = base_entries.drop(
        columns=[
            "EntryAt", "EntryAsk", "ExitAt", "ExitReason", "ExitBid",
            "HoldMinutes", "GrossReturnPct", "NetReturnPct", "GrossPnl",
            "StressCost", "NetPnl", "entry_time", "exit_time",
        ],
        errors="ignore",
    )
    events = entry_only.merge(outcomes[outcome_columns], on="EventId", how="inner")
    events["entry_time"] = pd.to_datetime(events["EntryAt"], errors="coerce")
    events["exit_time"] = pd.to_datetime(events["ExitAt"], errors="coerce")
    events["EntryTopLevelFillable"] = (
        events["EntryTopLevelFillable"]
        .fillna(False)
        .astype(str)
        .str.strip()
        .str.lower()
        .isin({"1", "1.0", "true", "yes", "y", "on"})
    )
    for column in (
        "EntryAsk", "ExitBid", "EntryDelayMinutes", "HoldMinutes",
        "GrossReturnPct", "NetReturnPct", "GrossPnl", "StressCost", "NetPnl",
    ):
        events[column] = pd.to_numeric(events[column], errors="coerce")
    events = events[
        events["entry_time"].notna()
        & events["exit_time"].notna()
        & events["NetReturnPct"].notna()
        & (events["exit_time"] > events["entry_time"])
        & events["EntryTopLevelFillable"]
        & events["Variant"].astype(str).isin(ENTRY_VARIANTS)
    ].copy()
    return base_entries, events.sort_values(
        ["date", "entry_time", "Variant", "Symbol"]
    ).reset_index(drop=True), nonfills


def timing_session_coverage(
    entries: pd.DataFrame,
    events: pd.DataFrame,
    nonfills: pd.DataFrame,
    raw_coverage: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    if raw_coverage is None or raw_coverage.empty:
        return pd.DataFrame()
    resolved_ids = set(events.get("TimingId", pd.Series(dtype=str)).astype(str))
    resolved_ids.update(
        nonfills.get("TimingId", pd.Series(dtype=str)).astype(str).tolist()
        if not nonfills.empty else []
    )
    for raw in raw_coverage.to_dict(orient="records"):
        date = str(raw.get("date", ""))
        event_ids = set(
            entries.loc[entries["date"].eq(date), "EventId"].astype(str)
        ) if not entries.empty else set()
        complete_variants = 0
        coverage_map = {}
        for variant in ENTRY_VARIANTS:
            expected = {f"{event_id}|{variant}" for event_id in event_ids}
            pct = len(expected & resolved_ids) / len(expected) * 100.0 if expected else 100.0
            coverage_map[variant] = round(pct, 2)
            complete_variants += int(pct >= 100.0)
        raw_full = bool(raw.get("full_session_eligible", False))
        rows.append(
            {
                "date": date,
                "raw_full_session": raw_full,
                "entries": len(event_ids),
                "complete_variants": complete_variants,
                "required_variants": len(ENTRY_VARIANTS),
                "eligible": bool(raw_full and complete_variants == len(ENTRY_VARIANTS)),
                "variant_coverage_json": json.dumps(coverage_map, sort_keys=True),
            }
        )
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def run_audit(
    *,
    entry_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
    nonfill_patterns: Iterable[str],
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
    entries, all_events, nonfills = load_timing_events(
        entry_patterns, outcome_patterns, nonfill_patterns
    )
    raw = raw_session_coverage(raw_patterns, min_snapshots=min_snapshots)
    coverage = timing_session_coverage(entries, all_events, nonfills, raw)
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = coverage.loc[coverage["eligible"], "date"].astype(str).tolist() if not coverage.empty else []
    events = all_events[all_events["date"].isin(eligible_dates)].copy() if not all_events.empty else all_events
    selection_dates = eligible_dates[:selection_sessions_required]

    result_rows = []
    for variant in ENTRY_VARIANTS:
        variant_events = events[events["Variant"].astype(str).eq(variant)].copy() if not events.empty else events
        for selector in SELECTORS:
            daily, _ = replay_one_slot(
                variant_events, selection_dates, selector=selector,
                capital=capital, max_trades_per_day=max_trades_per_day,
            )
            result_rows.append(
                {"variant": variant, "selector": selector, **metrics(daily, daily_goal)}
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
            "variant": str(winner["variant"]) if winner else "",
            "selector": str(winner["selector"]) if winner else "",
            "selection_start": selection_dates[0],
            "selection_end": selection_dates[-1],
            "selection_dates": selection_dates,
            "variant_definitions": ENTRY_VARIANTS,
            "selector_definitions": SELECTORS,
            "result": winner or {},
            "real_orders_allowed": False,
        }
        frozen_path.write_text(json.dumps(frozen, indent=2), encoding="utf-8")

    forward_dates = []
    forward_daily = pd.DataFrame()
    forward_trades = pd.DataFrame()
    forward_result = metrics(pd.DataFrame(), daily_goal)
    frozen_variant = str(frozen.get("variant", ""))
    frozen_selector = str(frozen.get("selector", ""))
    if frozen_variant and frozen_selector:
        selection_end = str(frozen.get("selection_end", ""))
        forward_dates = [date for date in eligible_dates if date > selection_end]
        forward_events = events[events["Variant"].astype(str).eq(frozen_variant)].copy()
        forward_daily, forward_trades = replay_one_slot(
            forward_events, forward_dates, selector=frozen_selector,
            capital=capital, max_trades_per_day=max_trades_per_day,
        )
        forward_result = metrics(forward_daily, daily_goal)
    forward_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    forward_trades.to_csv(output_dir / "forward_trades.csv", index=False)
    accepted = bool(
        frozen_variant
        and len(forward_dates) >= int(forward_sessions_required)
        and passes_goal_gate(forward_result, daily_goal, forward_sessions_required)
    )
    if len(selection_dates) < int(selection_sessions_required):
        status_name = "collecting_entry_timing_selection_sessions"
    elif frozen.get("selection_status") != "passed":
        status_name = "entry_timing_selection_rejected"
    elif len(forward_dates) < int(forward_sessions_required):
        status_name = "collecting_entry_timing_forward_sessions"
    else:
        status_name = "accepted_entry_timing_forward" if accepted else "entry_timing_forward_rejected"
    status = {
        "evidence": "full_universe_rank_exact_entry_timing_one_slot",
        "status": status_name,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "max_trades_per_day": int(max_trades_per_day),
        "variants": len(ENTRY_VARIANTS),
        "selectors": len(SELECTORS),
        "predeclared_combinations": len(ENTRY_VARIANTS) * len(SELECTORS),
        "selection_sessions_required": int(selection_sessions_required),
        "forward_sessions_required": int(forward_sessions_required),
        "raw_sessions": int(len(raw)),
        "eligible_sessions": int(len(eligible_dates)),
        "partial_or_incomplete_sessions": int(len(coverage) - len(eligible_dates)),
        "entry_labels": int(len(entries)),
        "filled_closed": int(len(all_events)),
        "nonfills": int(len(nonfills)),
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
    parser.add_argument("--nonfill-glob", action="append", default=[])
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument("--output-dir", default="results/Data/full_universe_rank_entry_strategy")
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--selection-sessions", type=int, default=12)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()
    root = "results/Data/full_universe_rank_entry_timing"
    status = run_audit(
        entry_patterns=args.entry_glob or ["results/Data/full_universe_rank_paper/entries_20*.csv"],
        outcome_patterns=args.outcome_glob or [f"{root}/outcomes_20*.csv"],
        nonfill_patterns=args.nonfill_glob or [f"{root}/nonfills_20*.csv"],
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

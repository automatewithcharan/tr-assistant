#!/usr/bin/env python3
"""Five-level, slot-sized execution judge for full-rank strategies."""

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

from pkscreener.classes.FullUniverseRankContractMatrixObserver import CONTRACTS
from pkscreener.classes.OrderBookExecution import (
    EXECUTION_SCHEMA_VERSION,
    affordable_market_fill,
    market_fill,
    parse_depth_json,
)
from scripts.assess_exact_full_rank_paper import SELECTORS, balanced_score
from scripts.assess_full_rank_capital_allocation import SLOT_COUNTS
from scripts.assess_full_universe_rank_strategy import raw_session_coverage
from scripts.assess_leader_pullback_reclaim import metrics


MIN_SLOT_DEPLOYMENT_PCT = 80.0
PREDECLARED_COMBINATIONS = 144
SELECTION_SESSIONS_REQUIRED = 12
FORWARD_SESSIONS_REQUIRED = 20


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {path for pattern in patterns for path in glob.glob(str(pattern))}
    )


def _load_frames(
    patterns: Iterable[str],
    required: set[str],
) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if required.issubset(frame.columns):
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _normalize_ist_wall_clock(values: pd.Series) -> pd.Series:
    """Normalize mixed timestamp encodings without shifting naive IST values."""
    def normalize(value):
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return pd.NaT
        if parsed.tzinfo is None:
            return parsed
        return parsed.tz_convert("Asia/Kolkata").tz_localize(None)

    return pd.to_datetime(values.map(normalize), errors="coerce")


def load_depth_events(
    entry_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load only five-level execution-schema entries and contract outcomes."""
    entry_required = {
        "EventId", "Date", "EntryAt", "RecorderGeneration", "Exchange",
        "Symbol", "EntryAsk", "ExecutionSchemaVersion", "EntryDepthJson",
    }
    outcome_required = {
        "MatrixId", "EventId", "Contract", "ExitAt", "ExitReason",
        "ExecutionSchemaVersion", "ExitDepthJson",
    }
    entries = _load_frames(entry_patterns, entry_required)
    outcomes = _load_frames(outcome_patterns, outcome_required)
    if entries.empty:
        return pd.DataFrame(), pd.DataFrame()

    entries["ExecutionSchemaVersion"] = pd.to_numeric(
        entries["ExecutionSchemaVersion"], errors="coerce"
    ).fillna(0).astype(int)
    entries["RecorderGeneration"] = pd.to_numeric(
        entries["RecorderGeneration"], errors="coerce"
    ).fillna(0).astype(int)
    entries["Exchange"] = (
        entries["Exchange"].fillna("").astype(str).str.strip().str.upper()
    )
    entries = entries[
        entries["ExecutionSchemaVersion"].ge(EXECUTION_SCHEMA_VERSION)
        & entries["RecorderGeneration"].ge(6)
        & entries["Exchange"].eq("NSE")
    ].drop_duplicates("EventId", keep="first").copy()
    if entries.empty or outcomes.empty:
        return entries, pd.DataFrame()

    outcomes["ExecutionSchemaVersion"] = pd.to_numeric(
        outcomes["ExecutionSchemaVersion"], errors="coerce"
    ).fillna(0).astype(int)
    outcomes = outcomes[
        outcomes["ExecutionSchemaVersion"].ge(EXECUTION_SCHEMA_VERSION)
        & outcomes["Contract"].astype(str).isin(CONTRACTS)
    ].drop_duplicates("MatrixId", keep="last").copy()

    numeric_columns = [
        "EntryAsk", "EntrySpreadBps", "DayChangePct", "UniverseSize",
        "DayChangeRank", "PreviousDayChangeRank", "RankImprovement",
        "RankVelocityPerMinute", "DayChangeDeltaPct", "TradedValue",
        "BuyerSellerRatio", "StressPct",
    ]
    for column in numeric_columns:
        if column not in entries.columns:
            entries[column] = 0.0
        entries[column] = pd.to_numeric(entries[column], errors="coerce").fillna(0.0)
    entries["date"] = entries["Date"].astype(str)
    entries["entry_time"] = _normalize_ist_wall_clock(entries["EntryAt"])
    entries["Symbol"] = entries["Symbol"].astype(str).str.strip().str.upper()
    entries["BalancedScore"] = balanced_score(entries)

    outcome_columns = [
        "MatrixId", "EventId", "Contract", "ExitAt", "ExitReason",
        "ExitDepthJson", "ExecutionSchemaVersion",
    ]
    events = entries.merge(
        outcomes[outcome_columns],
        on="EventId",
        how="inner",
        suffixes=("", "_outcome"),
    )
    events["exit_time"] = _normalize_ist_wall_clock(events["ExitAt"])
    events = events[
        events["entry_time"].notna()
        & events["exit_time"].notna()
        & (events["exit_time"] > events["entry_time"])
    ].copy()
    return entries, events.sort_values(
        ["date", "entry_time", "Contract", "Symbol"]
    ).reset_index(drop=True)


def depth_session_coverage(
    entries: pd.DataFrame,
    events: pd.DataFrame,
    raw_coverage: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    if raw_coverage is None or raw_coverage.empty:
        return pd.DataFrame()
    for raw in raw_coverage.to_dict(orient="records"):
        date = str(raw.get("date", ""))
        entry_ids = set(
            entries.loc[entries["date"].eq(date), "EventId"].astype(str)
        ) if not entries.empty else set()
        day = events[events["date"].eq(date)] if not events.empty else pd.DataFrame()
        complete = 0
        contract_coverage = {}
        for contract in CONTRACTS:
            closed = set(
                day.loc[
                    day["Contract"].astype(str).eq(contract), "EventId"
                ].astype(str)
            ) if not day.empty else set()
            pct = (
                len(entry_ids & closed) / len(entry_ids) * 100.0
                if entry_ids
                else 100.0
            )
            contract_coverage[contract] = round(pct, 2)
            complete += int(pct >= 100.0)
        raw_full = bool(raw.get("full_session_eligible", False))
        rows.append(
            {
                "date": date,
                "raw_full_session": raw_full,
                "schema_v2_entries": len(entry_ids),
                "complete_contracts": complete,
                "required_contracts": len(CONTRACTS),
                "eligible": bool(
                    raw_full
                    and bool(entry_ids)
                    and complete == len(CONTRACTS)
                ),
                "contract_coverage_json": json.dumps(
                    contract_coverage, sort_keys=True
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def replay_depth_slots(
    events: pd.DataFrame,
    dates: Iterable[str],
    *,
    selector: str,
    slots: int,
    capital: float = 500_000.0,
    max_trades_per_day: int = 10,
    min_slot_deployment_pct: float = MIN_SLOT_DEPLOYMENT_PCT,
    allowed_slot_counts: Iterable[int] = SLOT_COUNTS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replay causal selection with actual five-level entry and exit VWAPs."""
    if selector not in SELECTORS:
        raise ValueError(f"unknown selector: {selector}")
    allowed_slots = {int(value) for value in allowed_slot_counts}
    if int(slots) not in allowed_slots:
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
        selected = 0
        executable = 0
        exit_depth_failures = 0

        if not day.empty:
            for batch, visible in day.groupby("entry_batch", sort=True):
                active_exits = [
                    value for value in active_exits
                    if pd.Timestamp(value) > pd.Timestamp(batch)
                ]
                available = int(slots) - len(active_exits)
                if available <= 0 or selected >= int(max_trades_per_day):
                    continue
                for _, pick in visible.iterrows():
                    symbol = str(pick["Symbol"])
                    if symbol in traded_symbols:
                        continue
                    if available <= 0 or selected >= int(max_trades_per_day):
                        break
                    entry_fill = affordable_market_fill(
                        parse_depth_json(pick.get("EntryDepthJson")),
                        slot_capital,
                    )
                    quantity = int(entry_fill["filled_quantity"])
                    deployed = float(entry_fill["notional"])
                    deployment_pct = (
                        deployed / slot_capital * 100.0 if slot_capital > 0 else 0.0
                    )
                    if (
                        quantity < 1
                        or deployment_pct < float(min_slot_deployment_pct)
                    ):
                        continue

                    selected += 1
                    traded_symbols.add(symbol)
                    active_exits.append(pd.Timestamp(pick["exit_time"]))
                    available -= 1
                    exit_fill = market_fill(
                        parse_depth_json(pick.get("ExitDepthJson")),
                        quantity,
                    )
                    complete = bool(exit_fill["fillable"])
                    pnl = 0.0
                    if complete:
                        entry_vwap = float(entry_fill["vwap"])
                        exit_vwap = float(exit_fill["vwap"])
                        gross_pnl = (exit_vwap - entry_vwap) * quantity
                        stress_pct = float(pick.get("StressPct", 0.10) or 0.10)
                        stress_cost = deployed * stress_pct / 100.0
                        pnl = gross_pnl - stress_cost
                        day_pnl += pnl
                        executable += 1
                    else:
                        exit_depth_failures += 1
                    trade_rows.append(
                        {
                            "date": session_date,
                            "contract": str(pick.get("Contract", "")),
                            "selector": selector,
                            "slots": int(slots),
                            "symbol": symbol,
                            "entry_time": pick["entry_time"],
                            "exit_time": pick["exit_time"],
                            "exit_reason": str(pick.get("ExitReason", "")),
                            "slot_capital": round(slot_capital, 2),
                            "deployed_capital": round(deployed, 2),
                            "deployment_pct": round(deployment_pct, 4),
                            "quantity": quantity,
                            "entry_vwap": float(entry_fill["vwap"]),
                            "exit_vwap": (
                                float(exit_fill["vwap"]) if complete else pd.NA
                            ),
                            "entry_levels_used": int(entry_fill["levels_used"]),
                            "exit_levels_used": int(exit_fill["levels_used"]),
                            "exit_filled_quantity": int(
                                exit_fill["filled_quantity"]
                            ),
                            "execution_complete": complete,
                            "pnl": round(pnl, 2) if complete else pd.NA,
                        }
                    )
        execution_complete = exit_depth_failures == 0
        daily_rows.append(
            {
                "date": session_date,
                "selector": selector,
                "slots": int(slots),
                "slot_capital": round(slot_capital, 2),
                "trades": selected,
                "executable_trades": executable,
                "exit_depth_failures": exit_depth_failures,
                "execution_complete": execution_complete,
                "pnl": round(day_pnl, 2) if execution_complete else pd.NA,
                "daily_return_pct": (
                    round(day_pnl / float(capital) * 100.0, 6)
                    if execution_complete
                    else pd.NA
                ),
            }
        )
    return pd.DataFrame(daily_rows), pd.DataFrame(trade_rows)


def execution_metrics(
    daily: pd.DataFrame,
    daily_goal: float,
    expected_sessions: int,
) -> dict:
    complete = (
        daily[daily["execution_complete"].astype(bool)].copy()
        if daily is not None and not daily.empty
        else pd.DataFrame()
    )
    result = metrics(complete, daily_goal)
    completed = int(len(complete))
    expected = int(expected_sessions)
    result.update(
        {
            "expected_sessions": expected,
            "execution_complete_sessions": completed,
            "execution_coverage_pct": round(
                completed / expected * 100.0, 2
            ) if expected else 0.0,
            "exit_depth_failures": int(
                pd.to_numeric(
                    daily.get("exit_depth_failures", 0), errors="coerce"
                ).fillna(0).sum()
            ) if daily is not None and not daily.empty else 0,
        }
    )
    return result


def passes_depth_gate(
    result: dict,
    daily_goal: float,
    minimum_sessions: int,
) -> bool:
    return bool(
        result.get("expected_sessions", 0) >= int(minimum_sessions)
        and result.get("execution_complete_sessions", 0)
        == result.get("expected_sessions", 0)
        and result.get("trade_sessions", 0)
        >= max(1, int(minimum_sessions * 0.75))
        and result.get("trades", 0) >= max(10, int(minimum_sessions * 2.5))
        and result.get("avg_daily_pnl", 0.0) >= float(daily_goal)
        and result.get("lower_95_pct") is not None
        and result.get("lower_95_pct") > 0.0
        and result.get("block_floor_pct") is not None
        and result.get("block_floor_pct") > 0.0
        and result.get("goal_hit_pct", 0.0) >= 50.0
        and result.get("worst_session_pct", -999.0) >= -2.5
    )


def evaluate_combinations(
    events: pd.DataFrame,
    dates: list[str],
    *,
    capital: float,
    daily_goal: float,
    max_trades_per_day: int,
) -> list[dict]:
    rows = []
    for contract in CONTRACTS:
        contract_events = events[
            events["Contract"].astype(str).eq(contract)
        ].copy() if not events.empty else events
        for selector in SELECTORS:
            for slots in SLOT_COUNTS:
                daily, _ = replay_depth_slots(
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
                        **execution_metrics(daily, daily_goal, len(dates)),
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
    selection_sessions_required: int = SELECTION_SESSIONS_REQUIRED,
    forward_sessions_required: int = FORWARD_SESSIONS_REQUIRED,
    min_snapshots: int = 1000,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    entries, events = load_depth_events(entry_patterns, outcome_patterns)
    raw = raw_session_coverage(raw_patterns, min_snapshots=min_snapshots)
    coverage = depth_session_coverage(entries, events, raw)
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = (
        coverage.loc[coverage["eligible"], "date"].astype(str).tolist()
        if not coverage.empty
        else []
    )
    eligible_events = events[
        events["date"].isin(eligible_dates)
    ].copy() if not events.empty else events
    selection_dates = eligible_dates[:selection_sessions_required]
    rows = evaluate_combinations(
        eligible_events,
        selection_dates,
        capital=capital,
        daily_goal=daily_goal,
        max_trades_per_day=max_trades_per_day,
    )
    pd.DataFrame(rows).to_csv(output_dir / "selection_summary.csv", index=False)

    frozen_path = output_dir / "frozen_selection.json"
    frozen = (
        json.loads(frozen_path.read_text(encoding="utf-8"))
        if frozen_path.exists()
        else {}
    )
    if not frozen and len(selection_dates) >= int(selection_sessions_required):
        passing = [
            row for row in rows
            if passes_depth_gate(
                row, daily_goal, selection_sessions_required
            )
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
            "selection_dates": selection_dates,
            "selection_start": selection_dates[0],
            "selection_end": selection_dates[-1],
            "execution_schema_version": EXECUTION_SCHEMA_VERSION,
            "minimum_slot_deployment_pct": MIN_SLOT_DEPLOYMENT_PCT,
            "result": winner or {},
            "real_orders_allowed": False,
        }
        frozen_path.write_text(json.dumps(frozen, indent=2), encoding="utf-8")

    forward_dates: list[str] = []
    forward_daily = pd.DataFrame()
    forward_trades = pd.DataFrame()
    forward_result = execution_metrics(
        pd.DataFrame(), daily_goal, 0
    )
    contract = str(frozen.get("contract", ""))
    selector = str(frozen.get("selector", ""))
    slots = int(frozen.get("slots", 0) or 0)
    if contract and selector and slots in SLOT_COUNTS:
        forward_dates = [
            date for date in eligible_dates
            if date > str(frozen.get("selection_end", ""))
        ]
        forward_daily, forward_trades = replay_depth_slots(
            eligible_events[
                eligible_events["Contract"].astype(str).eq(contract)
            ].copy(),
            forward_dates,
            selector=selector,
            slots=slots,
            capital=capital,
            max_trades_per_day=max_trades_per_day,
        )
        forward_result = execution_metrics(
            forward_daily, daily_goal, len(forward_dates)
        )
    forward_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    forward_trades.to_csv(output_dir / "forward_trades.csv", index=False)

    accepted = bool(
        contract
        and len(forward_dates) >= int(forward_sessions_required)
        and passes_depth_gate(
            forward_result, daily_goal, forward_sessions_required
        )
    )
    if len(selection_dates) < int(selection_sessions_required):
        status_name = "collecting_depth_execution_selection_sessions"
    elif frozen.get("selection_status") != "passed":
        status_name = "depth_execution_selection_rejected"
    elif len(forward_dates) < int(forward_sessions_required):
        status_name = "collecting_depth_execution_forward_sessions"
    else:
        status_name = (
            "accepted_depth_execution_forward"
            if accepted
            else "depth_execution_forward_rejected"
        )
    status = {
        "evidence": "five_level_depth_slot_sized_rank_strategy",
        "status": status_name,
        "execution_schema_version": EXECUTION_SCHEMA_VERSION,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "minimum_slot_deployment_pct": MIN_SLOT_DEPLOYMENT_PCT,
        "max_trades_per_day": int(max_trades_per_day),
        "predeclared_combinations": PREDECLARED_COMBINATIONS,
        "selection_sessions_required": int(selection_sessions_required),
        "forward_sessions_required": int(forward_sessions_required),
        "eligible_sessions": len(eligible_dates),
        "selection_dates": selection_dates,
        "frozen_selection": frozen,
        "forward_dates": forward_dates,
        "forward": forward_result,
        "accepted": accepted,
        "paper_promotion_allowed": accepted,
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
        default="results/Data/full_universe_rank_depth_execution",
    )
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--selection-sessions", type=int, default=12)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()
    status = run_audit(
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

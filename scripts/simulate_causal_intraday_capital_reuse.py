#!/usr/bin/env python3
"""Simulate unleveraged intraday capital reuse from exact replay exits."""

from __future__ import annotations

from typing import Any

import pandas as pd


def simulate_day(
    candidates: pd.DataFrame,
    capital: float,
    slots: int,
    return_column: str,
    exit_time_column: str,
    extra_cost_pct: float = 0.0,
    max_trades_per_day: int = 10,
) -> tuple[dict[str, Any], pd.DataFrame]:
    if capital <= 0:
        raise ValueError("capital must be positive")
    if slots < 1:
        raise ValueError("slots must be at least one")
    if max_trades_per_day < 1:
        raise ValueError("max_trades_per_day must be at least one")
    required = {"date", "symbol", "entry_time", exit_time_column, return_column}
    missing = required - set(candidates.columns)
    if missing:
        raise ValueError(f"candidate frame missing columns: {', '.join(sorted(missing))}")

    day = candidates.copy()
    day["entry_time"] = pd.to_datetime(day["entry_time"], errors="coerce")
    day[exit_time_column] = pd.to_datetime(day[exit_time_column], errors="coerce")
    day[return_column] = pd.to_numeric(day[return_column], errors="coerce")
    day = day.dropna(subset=["entry_time", exit_time_column, return_column])
    sort_columns = ["entry_time"]
    ascending = [True]
    if "selection_score" in day.columns:
        sort_columns.append("selection_score")
        ascending.append(False)
    sort_columns.append("symbol")
    ascending.append(True)
    day.sort_values(sort_columns, ascending=ascending, inplace=True)

    initial_capital = float(capital)
    slot_capital = initial_capital / int(slots)
    cash = initial_capital
    active: list[dict[str, Any]] = []
    traded_symbols: set[str] = set()
    records = []

    def release_before(timestamp: pd.Timestamp) -> None:
        nonlocal cash, active
        still_active = []
        for position in active:
            # Strict comparison is conservative: an exit somewhere inside the
            # same five-minute candle cannot fund another entry at that candle's open.
            if position["exit_time"] < timestamp:
                cash += position["released_cash"]
            else:
                still_active.append(position)
        active = still_active

    for row in day.to_dict(orient="records"):
        entry_time = pd.Timestamp(row["entry_time"])
        exit_time = pd.Timestamp(row[exit_time_column])
        symbol = str(row["symbol"]).strip().upper()
        release_before(entry_time)
        reason = ""
        if symbol in traded_symbols:
            reason = "same_symbol_already_traded"
        elif len(traded_symbols) >= int(max_trades_per_day):
            reason = "daily_trade_cap"
        elif len(active) >= int(slots):
            reason = "all_slots_busy"
        elif cash <= 0:
            reason = "no_cash"
        allocation = min(slot_capital, cash) if not reason else 0.0
        if allocation <= 0 and not reason:
            reason = "insufficient_cash"
        net_return = float(row[return_column]) - float(extra_cost_pct)
        executed = not reason
        pnl = allocation * net_return / 100.0 if executed else 0.0
        if executed:
            cash -= allocation
            active.append(
                {
                    "symbol": symbol,
                    "exit_time": exit_time,
                    "released_cash": allocation + pnl,
                }
            )
            traded_symbols.add(symbol)
        records.append(
            {
                "date": str(row["date"]),
                "symbol": symbol,
                "entry_time": str(entry_time),
                "exit_time": str(exit_time),
                "executed": executed,
                "skip_reason": reason,
                "allocation": round(allocation, 2),
                "net_return_pct": round(net_return, 6),
                "pnl": round(pnl, 2),
                "selection_score": float(row.get("selection_score", 0.0) or 0.0),
                "selection_label": int(row.get("selection_label", 0) or 0),
                "eventual_runner_label": int(row.get("eventual_runner_label", 0) or 0),
            }
        )

    for position in active:
        cash += position["released_cash"]
    pnl = cash - initial_capital
    summary = {
        "date": str(day.iloc[0]["date"]) if not day.empty else "",
        "candidate_trades": int(len(day)),
        "executed_trades": int(sum(bool(row["executed"]) for row in records)),
        "trades": int(sum(bool(row["executed"]) for row in records)),
        "skipped_busy": int(sum(row["skip_reason"] == "all_slots_busy" for row in records)),
        "ending_cash": round(cash, 2),
        "pnl": round(pnl, 2),
        "daily_return_pct": round(pnl / initial_capital * 100.0, 6),
        "capital_reuse": "exact_exit_bar_unleveraged",
        "slots": int(slots),
        "max_trades_per_day": int(max_trades_per_day),
        "real_orders_allowed": False,
    }
    return summary, pd.DataFrame(records)


def simulate_calendar(
    candidates: pd.DataFrame,
    dates: list[str],
    capital: float,
    slots: int,
    return_column: str,
    exit_time_column: str,
    daily_goal: float,
    extra_cost_pct: float = 0.0,
    max_trades_per_day: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    groups = {str(date): group for date, group in candidates.groupby("date", sort=False)}
    empty = candidates.iloc[0:0]
    daily_rows = []
    trade_frames = []
    for date in dates:
        summary, trades = simulate_day(
            groups.get(str(date), empty),
            capital,
            slots,
            return_column,
            exit_time_column,
            extra_cost_pct,
            max_trades_per_day,
        )
        summary["date"] = str(date)
        summary["daily_goal_hit"] = bool(summary["pnl"] >= float(daily_goal))
        daily_rows.append(summary)
        if not trades.empty:
            trade_frames.append(trades)
    return (
        pd.DataFrame(daily_rows),
        pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame(),
    )

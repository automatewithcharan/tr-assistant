#!/usr/bin/env python3
"""Project a historical daily-return stream onto a funded capital path.

This is an accounting transform, not a forecast or a strategy selector. It
compounds only the supplied net daily returns and adds external capital at the
first observed trading session of each subsequent calendar month.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def project_capital_path(
    daily: pd.DataFrame,
    starting_capital: float = 500_000.0,
    monthly_contribution: float = 200_000.0,
    daily_goal: float = 10_000.0,
    date_column: str = "date",
    return_column: str = "daily_return_pct",
) -> tuple[pd.DataFrame, dict]:
    if starting_capital <= 0:
        raise ValueError("starting capital must be positive")
    if monthly_contribution < 0:
        raise ValueError("monthly contribution cannot be negative")
    if daily_goal <= 0:
        raise ValueError("daily goal must be positive")
    missing = {date_column, return_column} - set(daily.columns)
    if missing:
        raise ValueError(f"daily input missing columns: {', '.join(sorted(missing))}")

    frame = daily[[date_column, return_column]].copy()
    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce")
    frame[return_column] = pd.to_numeric(frame[return_column], errors="coerce")
    if frame[[date_column, return_column]].isna().any().any():
        raise ValueError("daily input contains invalid dates or returns")
    if frame[date_column].duplicated().any():
        raise ValueError("daily input contains duplicate dates")
    if (frame[return_column] <= -100.0).any():
        raise ValueError("daily return cannot be -100% or lower")
    frame.sort_values(date_column, inplace=True)
    frame.reset_index(drop=True, inplace=True)
    if frame.empty:
        raise ValueError("daily input is empty")

    equity = float(starting_capital)
    contributed = float(starting_capital)
    strategy_index = 1.0
    strategy_peak = 1.0
    previous_month = None
    rows = []
    for row in frame.to_dict(orient="records"):
        date = pd.Timestamp(row[date_column])
        month = date.to_period("M")
        contribution = 0.0
        if previous_month is not None and month != previous_month:
            contribution = float(monthly_contribution)
            equity += contribution
            contributed += contribution

        start_of_day = equity
        return_pct = float(row[return_column])
        pnl = start_of_day * return_pct / 100.0
        equity += pnl
        strategy_index *= 1.0 + return_pct / 100.0
        strategy_peak = max(strategy_peak, strategy_index)
        drawdown_pct = (strategy_index / strategy_peak - 1.0) * 100.0
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "external_contribution": round(contribution, 2),
                "start_of_day_capital": round(start_of_day, 2),
                "daily_return_pct": round(return_pct, 6),
                "strategy_pnl": round(pnl, 2),
                "end_of_day_capital": round(equity, 2),
                "cumulative_external_capital": round(contributed, 2),
                "cumulative_strategy_pnl": round(equity - contributed, 2),
                "strategy_drawdown_pct": round(drawdown_pct, 6),
                "positive_day": bool(pnl > 0.0),
                "daily_goal_hit": bool(pnl >= float(daily_goal)),
                "required_return_for_goal_pct": round(
                    float(daily_goal) / start_of_day * 100.0, 6
                ),
            }
        )
        previous_month = month

    output = pd.DataFrame(rows)
    returns = output["daily_return_pct"].to_numpy(dtype=float)
    geometric_daily = (float(np.prod(1.0 + returns / 100.0)) ** (1.0 / len(returns)) - 1.0) * 100.0
    summary = {
        "sessions": int(len(output)),
        "starting_capital": round(float(starting_capital), 2),
        "monthly_contribution": round(float(monthly_contribution), 2),
        "total_external_capital": round(float(contributed), 2),
        "ending_capital": round(float(equity), 2),
        "net_strategy_pnl": round(float(equity - contributed), 2),
        "avg_daily_strategy_pnl": round(float(output["strategy_pnl"].mean()), 2),
        "median_daily_strategy_pnl": round(float(output["strategy_pnl"].median()), 2),
        "avg_daily_return_pct": round(float(output["daily_return_pct"].mean()), 6),
        "geometric_daily_return_pct": round(float(geometric_daily), 6),
        "positive_day_pct": round(float(output["positive_day"].mean() * 100.0), 2),
        "daily_goal": round(float(daily_goal), 2),
        "daily_goal_hit_pct": round(float(output["daily_goal_hit"].mean() * 100.0), 2),
        "worst_strategy_drawdown_pct": round(
            float(output["strategy_drawdown_pct"].min()), 6
        ),
        "initial_required_daily_return_pct": round(
            float(daily_goal) / float(starting_capital) * 100.0, 6
        ),
        "uses_historical_daily_returns": True,
        "is_forecast": False,
        "selection_metric": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    return output, summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--date-column", default="date")
    parser.add_argument("--return-column", default="daily_return_pct")
    parser.add_argument("--starting-capital", type=float, default=500_000.0)
    parser.add_argument("--monthly-contribution", type=float, default=200_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    args = parser.parse_args()

    daily = pd.read_csv(args.input)
    path, summary = project_capital_path(
        daily,
        starting_capital=args.starting_capital,
        monthly_contribution=args.monthly_contribution,
        daily_goal=args.daily_goal,
        date_column=args.date_column,
        return_column=args.return_column,
    )
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    path.to_csv(prefix.with_name(prefix.name + "_daily.csv"), index=False)
    prefix.with_name(prefix.name + "_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

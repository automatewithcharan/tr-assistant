#!/usr/bin/env python3
"""Backtest a flexible intraday daily-profit goal from saved alert outcomes.

The simulation is portfolio-level:
  - Start with a daily capital pool, e.g. Rs 2,00,000.
  - Process qualified signals in alert-time order.
  - Allocate a fixed ticket per signal while capital is available.
  - Do not impose a fixed stock-count cap; capital naturally limits entries.
  - Stop adding new trades once the day's realized P&L reaches the goal.

The source data has max favorable/adverse excursion, not full tick-level exit
timestamps. If target and stop both occur, this script assumes the stop happened
first. Open trades that hit neither level exit at the recorded EOD return.
"""

from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path
from typing import Iterable

import pandas as pd


DEFAULT_INPUT = "results/Data/anomaly_history.csv"
DEFAULT_OUT_PREFIX = "results/Data/daily_profit_goal"


def _to_float(value, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _exit_return_pct(row: pd.Series, target_pct: float, stop_pct: float, cost_pct: float) -> tuple[float, str]:
    mfe = _to_float(row.get("MaxFavExcursionPct"))
    mae = _to_float(row.get("MaxAdvExcursionPct"))
    eod = _to_float(row.get("EODReturnPct"))

    target_hit = mfe >= target_pct
    stop_hit = mae <= -abs(stop_pct)
    if target_hit and stop_hit:
        return -abs(stop_pct) - cost_pct, "stop_first"
    if stop_hit:
        return -abs(stop_pct) - cost_pct, "stop"
    if target_hit:
        return target_pct - cost_pct, "target"
    return eod - cost_pct, "eod"


def _load_alerts(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {
        "Date",
        "When",
        "Symbol",
        "Action",
        "QualityScore",
        "MaxFavExcursionPct",
        "MaxAdvExcursionPct",
        "EODReturnPct",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"{path} is missing columns: {', '.join(missing)}")
    df = df.copy()
    df["When"] = pd.to_datetime(df["When"], errors="coerce")
    df["Date"] = df["Date"].fillna(df["When"].dt.date.astype(str)).astype(str)
    df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper()
    df["Action"] = df["Action"].astype(str)
    df["QualityScore"] = pd.to_numeric(df["QualityScore"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["When"])
    df = df[df["Symbol"] != ""]
    return df.sort_values(["Date", "When", "QualityScore", "Symbol"], ascending=[True, True, False, True])


def _filter_alerts(df: pd.DataFrame, action_filter: str, min_quality: float, outcome_source_filter: str) -> pd.DataFrame:
    out = df[df["QualityScore"] >= float(min_quality)].copy()
    if outcome_source_filter == "post_alert_intraday":
        if "OutcomeSource" not in out.columns:
            return out.iloc[0:0].copy()
        sources = out["OutcomeSource"].astype(str)
        intraday_source = (
            sources.str.contains("5minute", case=False, na=False)
            | sources.str.contains(
                r"(?:^|[/\\])minute(?:[/\\]|$)",
                case=False,
                na=False,
                regex=True,
            )
        )
        out = out[intraday_source].copy()
    elif outcome_source_filter == "any":
        pass
    else:
        raise ValueError(f"unknown outcome source filter: {outcome_source_filter}")

    action = out["Action"].str.lower()
    if action_filter == "all":
        return out
    if action_filter == "grade_a":
        return out[action.str.contains("grade-a|runner", regex=True, na=False)].copy()
    if action_filter == "grade_ab":
        return out[action.str.contains("grade-a|grade-b|runner|anomaly", regex=True, na=False)].copy()
    if action_filter == "no_premove":
        return out[~action.str.contains("pre-move", regex=False, na=False)].copy()
    if action_filter == "premove":
        return out[action.str.contains("pre-move", regex=False, na=False)].copy()
    raise ValueError(f"unknown action filter: {action_filter}")


def simulate(
    df: pd.DataFrame,
    *,
    capital: float,
    ticket: float,
    goal_pct: float,
    target_pct: float,
    stop_pct: float,
    cost_pct: float,
    min_quality: float,
    action_filter: str,
    outcome_source_filter: str,
    rank_by: str,
    compound: bool,
) -> tuple[pd.DataFrame, dict]:
    qualified = _filter_alerts(
        df,
        action_filter=action_filter,
        min_quality=min_quality,
        outcome_source_filter=outcome_source_filter,
    )
    if qualified.empty:
        return pd.DataFrame(), {
            "action_filter": action_filter,
            "outcome_source_filter": outcome_source_filter,
            "rank_by": rank_by,
            "min_quality": min_quality,
            "capital": capital,
            "ticket": ticket,
            "goal_pct": goal_pct,
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "cost_pct": cost_pct,
            "compound": compound,
            "days": 0,
            "trade_count": 0,
            "avg_trades_per_day": 0.0,
            "goal_hit_pct": 0.0,
            "positive_day_pct": 0.0,
            "avg_daily_return_pct": 0.0,
            "median_daily_return_pct": 0.0,
            "worst_day_pct": 0.0,
            "best_day_pct": 0.0,
            "avg_losing_day_pct": 0.0,
            "total_pnl": 0.0,
            "ending_capital": capital,
        }

    current_capital = float(capital)
    daily_rows = []
    trade_rows = []
    for day, day_df in qualified.groupby("Date", sort=True):
        day_capital = current_capital
        goal_amount = day_capital * goal_pct / 100.0
        available = day_capital
        pnl = 0.0
        trades = 0
        winners = 0
        stopped_by_goal = False
        seen_symbols = set()

        if rank_by == "time":
            ordered = day_df.sort_values(["When", "QualityScore", "Symbol"], ascending=[True, False, True])
        elif rank_by == "quality":
            ordered = day_df.sort_values(["QualityScore", "When", "Symbol"], ascending=[False, True, True])
        else:
            raise ValueError(f"unknown rank_by: {rank_by}")

        for _, row in ordered.iterrows():
            if pnl >= goal_amount:
                stopped_by_goal = True
                break
            symbol = str(row["Symbol"])
            if symbol in seen_symbols:
                continue
            if available < ticket:
                break

            ret_pct, exit_reason = _exit_return_pct(row, target_pct=target_pct, stop_pct=stop_pct, cost_pct=cost_pct)
            trade_capital = min(ticket, available)
            trade_pnl = trade_capital * ret_pct / 100.0
            available -= trade_capital
            pnl += trade_pnl
            trades += 1
            winners += int(trade_pnl > 0)
            seen_symbols.add(symbol)
            trade_rows.append(
                {
                    "date": day,
                    "when": row["When"],
                    "symbol": symbol,
                    "action": row["Action"],
                    "quality": row["QualityScore"],
                    "capital": round(trade_capital, 2),
                    "return_pct": round(ret_pct, 4),
                    "pnl": round(trade_pnl, 2),
                    "exit_reason": exit_reason,
                }
            )

        day_return_pct = pnl / day_capital * 100.0 if day_capital > 0 else 0.0
        daily_rows.append(
            {
                "date": day,
                "starting_capital": round(day_capital, 2),
                "goal_amount": round(goal_amount, 2),
                "trades": trades,
                "winners": winners,
                "win_rate_pct": round(winners / trades * 100.0, 2) if trades else 0.0,
                "pnl": round(pnl, 2),
                "daily_return_pct": round(day_return_pct, 4),
                "goal_hit": pnl >= goal_amount,
                "stopped_by_goal": stopped_by_goal,
                "unused_capital": round(available, 2),
            }
        )
        if compound:
            current_capital = max(0.0, current_capital + pnl)

    daily = pd.DataFrame(daily_rows)
    trades_df = pd.DataFrame(trade_rows)
    downside = daily[daily["daily_return_pct"] < 0]["daily_return_pct"]
    summary = {
        "action_filter": action_filter,
        "min_quality": min_quality,
        "capital": capital,
        "ticket": ticket,
        "goal_pct": goal_pct,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "cost_pct": cost_pct,
        "compound": compound,
        "outcome_source_filter": outcome_source_filter,
        "rank_by": rank_by,
        "days": int(len(daily)),
        "trade_count": int(len(trades_df)),
        "avg_trades_per_day": round(float(daily["trades"].mean()), 2) if not daily.empty else 0.0,
        "goal_hit_pct": round(float(daily["goal_hit"].mean() * 100.0), 2) if not daily.empty else 0.0,
        "positive_day_pct": round(float((daily["daily_return_pct"] > 0).mean() * 100.0), 2) if not daily.empty else 0.0,
        "avg_daily_return_pct": round(float(daily["daily_return_pct"].mean()), 4) if not daily.empty else 0.0,
        "median_daily_return_pct": round(float(daily["daily_return_pct"].median()), 4) if not daily.empty else 0.0,
        "worst_day_pct": round(float(daily["daily_return_pct"].min()), 4) if not daily.empty else 0.0,
        "best_day_pct": round(float(daily["daily_return_pct"].max()), 4) if not daily.empty else 0.0,
        "avg_losing_day_pct": round(float(downside.mean()), 4) if not downside.empty else 0.0,
        "total_pnl": round(float(daily["pnl"].sum()), 2) if not daily.empty else 0.0,
        "ending_capital": round(float(daily.iloc[-1]["starting_capital"] + daily.iloc[-1]["pnl"]), 2) if compound and not daily.empty else round(capital + float(daily["pnl"].sum()), 2) if not daily.empty else capital,
    }
    return daily, summary


def _parse_csv_numbers(text: str, cast=float) -> list:
    return [cast(item.strip()) for item in str(text).split(",") if item.strip() != ""]


def _iter_configs(args) -> Iterable[dict]:
    for action_filter, outcome_source_filter, rank_by, min_quality, ticket, goal_pct, target_pct, stop_pct in product(
        args.action_filters,
        args.outcome_source_filter,
        args.rank_by,
        args.min_quality,
        args.tickets,
        args.goal_pct,
        args.target_pct,
        args.stop_pct,
    ):
        if stop_pct > target_pct * 2.5:
            continue
        yield {
            "capital": args.capital,
            "ticket": ticket,
            "goal_pct": goal_pct,
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "cost_pct": args.cost_pct,
            "min_quality": min_quality,
            "action_filter": action_filter,
            "outcome_source_filter": outcome_source_filter,
            "rank_by": rank_by,
            "compound": args.compound,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest flexible daily profit target from alert history")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--out-prefix", default=DEFAULT_OUT_PREFIX)
    parser.add_argument("--capital", type=float, default=200000.0)
    parser.add_argument("--tickets", default="20000,50000")
    parser.add_argument("--goal-pct", default="0.5,1.0,2.0,3.0")
    parser.add_argument("--target-pct", default="1.5,2.0,3.0")
    parser.add_argument("--stop-pct", default="0.5,0.8,1.0")
    parser.add_argument("--min-quality", default="0,50,60,70,75")
    parser.add_argument("--action-filters", default="all")
    parser.add_argument(
        "--outcome-source-filter",
        default="post_alert_intraday",
        help="comma-separated: post_alert_intraday,any. Default avoids full-day OHLC leakage.",
    )
    parser.add_argument("--rank-by", default="quality,time", help="comma-separated: quality,time")
    parser.add_argument("--cost-pct", type=float, default=0.15)
    parser.add_argument("--compound", action="store_true")
    args = parser.parse_args()

    args.tickets = _parse_csv_numbers(args.tickets, float)
    args.goal_pct = _parse_csv_numbers(args.goal_pct, float)
    args.target_pct = _parse_csv_numbers(args.target_pct, float)
    args.stop_pct = _parse_csv_numbers(args.stop_pct, float)
    args.min_quality = _parse_csv_numbers(args.min_quality, float)
    args.action_filters = [item.strip() for item in args.action_filters.split(",") if item.strip()]
    args.outcome_source_filter = [item.strip() for item in args.outcome_source_filter.split(",") if item.strip()]
    args.rank_by = [item.strip() for item in args.rank_by.split(",") if item.strip()]

    df = _load_alerts(args.input)
    summaries = []
    best_daily = pd.DataFrame()
    best_summary = None
    for cfg in _iter_configs(args):
        daily, summary = simulate(df, **cfg)
        summaries.append(summary)
        if best_summary is None or (
            summary["goal_hit_pct"],
            summary["avg_daily_return_pct"],
            summary["worst_day_pct"],
            summary["trade_count"],
        ) > (
            best_summary["goal_hit_pct"],
            best_summary["avg_daily_return_pct"],
            best_summary["worst_day_pct"],
            best_summary["trade_count"],
        ):
            best_summary = summary
            best_daily = daily

    summary_df = pd.DataFrame(summaries)
    summary_df.sort_values(
        ["goal_hit_pct", "avg_daily_return_pct", "worst_day_pct", "positive_day_pct"],
        ascending=[False, False, False, False],
        inplace=True,
    )

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = out_prefix.with_name(out_prefix.name + "_summary.csv")
    daily_path = out_prefix.with_name(out_prefix.name + "_best_daily.csv")
    summary_df.to_csv(summary_path, index=False)
    best_daily.to_csv(daily_path, index=False)

    cols = [
        "action_filter",
        "outcome_source_filter",
        "rank_by",
        "min_quality",
        "ticket",
        "goal_pct",
        "target_pct",
        "stop_pct",
        "days",
        "trade_count",
        "avg_trades_per_day",
        "goal_hit_pct",
        "positive_day_pct",
        "avg_daily_return_pct",
        "median_daily_return_pct",
        "worst_day_pct",
        "best_day_pct",
        "total_pnl",
    ]
    print(f"Input: {args.input} rows={len(df)} days={df['Date'].nunique()} date_range={df['Date'].min()}..{df['Date'].max()}")
    print(f"Saved: {summary_path}")
    print(f"Saved: {daily_path}")
    print("\nTop configurations:")
    print(summary_df[cols].head(25).to_string(index=False))
    if not best_daily.empty:
        print("\nBest configuration daily detail:")
        print(best_daily.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

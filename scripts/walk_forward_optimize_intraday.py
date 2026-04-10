#!/usr/bin/env python3
"""
Walk-forward optimizer for intraday backtest trades.

This script reads the CSV exported by `scripts/backtest_intraday_live.py` and
searches simple threshold rules on the net-of-cost trade returns:
  - score minimum
  - volume-ratio minimum
  - change-at-entry maximum
  - entry-time cutoff
  - top-N picks per day

It reports:
  1. Baseline performance with no extra filters
  2. Best static filters on the full sample
  3. Walk-forward out-of-sample performance using prior days to choose filters
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional

import pandas as pd


@dataclass(frozen=True)
class FilterConfig:
    score_min: int
    volume_min: float
    change_max: float
    entry_cutoff_min: int
    top_n: int


def _time_to_minute(value: str) -> int:
    text = str(value or "").strip().lstrip("~")
    if not text:
        return 24 * 60
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return hour * 60 + minute
    except Exception:
        return 24 * 60


def _minute_to_label(total_minutes: int) -> str:
    hour = total_minutes // 60
    minute = total_minutes % 60
    return f"{hour:02d}:{minute:02d}"


def _load_trades(csv_path: str) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    df = pd.read_csv(csv_path)
    if df.empty:
        raise ValueError(f"No trades found in {csv_path}")

    if "net_pnl_pct" not in df.columns:
        if "pnl_pct" not in df.columns:
            raise ValueError("CSV must contain either 'net_pnl_pct' or 'pnl_pct'")
        df["net_pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
    if "gross_pnl_pct" not in df.columns:
        df["gross_pnl_pct"] = pd.to_numeric(df["net_pnl_pct"], errors="coerce").fillna(0.0)

    for col in ["score", "volume_ratio", "change_at_entry", "net_pnl_pct", "gross_pnl_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df["date"] = df["date"].astype(str)
    df["entry_time"] = df["entry_time"].astype(str)
    df["entry_minute"] = df["entry_time"].apply(_time_to_minute)
    mins_from_open = (df["entry_minute"] - (9 * 60 + 15)).clip(lower=0)
    # Higher score/volume is better; lower change% and earlier entry are better.
    df["quality_rank"] = (
        df["score"]
        + (df["volume_ratio"].clip(upper=4.0) * 8.0)
        - (df["change_at_entry"].clip(lower=0.0) * 1.8)
        - (mins_from_open * 0.02)
    )
    return df.sort_values(["date", "quality_rank"], ascending=[True, False]).reset_index(drop=True)


def _generate_configs() -> List[FilterConfig]:
    score_grid = [0, 20, 30, 40, 50, 60]
    volume_grid = [0.5, 1.0, 1.5, 2.0, 2.5]
    change_grid = [4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 16.0]
    cutoff_grid = [
        _time_to_minute("10:30"),
        _time_to_minute("11:00"),
        _time_to_minute("12:00"),
        _time_to_minute("13:00"),
        _time_to_minute("14:00"),
        _time_to_minute("15:15"),
    ]
    top_n_grid = [1, 2, 3, 5]

    configs: List[FilterConfig] = []
    for score_min in score_grid:
        for volume_min in volume_grid:
            for change_max in change_grid:
                for entry_cutoff_min in cutoff_grid:
                    for top_n in top_n_grid:
                        configs.append(
                            FilterConfig(
                                score_min=score_min,
                                volume_min=volume_min,
                                change_max=change_max,
                                entry_cutoff_min=entry_cutoff_min,
                                top_n=top_n,
                            )
                        )
    return configs


def _apply_config(df: pd.DataFrame, cfg: FilterConfig) -> pd.DataFrame:
    if df is None or df.empty:
        return df.iloc[0:0].copy()

    filtered = df[
        (df["score"] >= cfg.score_min)
        & (df["volume_ratio"] >= cfg.volume_min)
        & (df["change_at_entry"] <= cfg.change_max)
        & (df["entry_minute"] <= cfg.entry_cutoff_min)
    ].copy()
    if filtered.empty:
        return filtered

    return filtered.groupby("date", group_keys=False).head(cfg.top_n).reset_index(drop=True)


def _summarize(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {
            "trades": 0,
            "days": 0,
            "win_rate": 0.0,
            "avg_net": 0.0,
            "median_net": 0.0,
            "avg_gross": 0.0,
            "rank_score": float("-inf"),
            "trades_per_day": 0.0,
        }

    days = int(df["date"].nunique())
    trades = int(len(df))
    avg_net = float(df["net_pnl_pct"].mean())
    median_net = float(df["net_pnl_pct"].median())
    avg_gross = float(df["gross_pnl_pct"].mean())
    win_rate = float((df["net_pnl_pct"] > 0).mean() * 100.0)
    trades_per_day = float(trades / max(days, 1))
    # Small-sample resistant rank: edge times sqrt(sample size).
    rank_score = avg_net * math.sqrt(max(trades, 1))
    return {
        "trades": trades,
        "days": days,
        "win_rate": win_rate,
        "avg_net": avg_net,
        "median_net": median_net,
        "avg_gross": avg_gross,
        "rank_score": rank_score,
        "trades_per_day": trades_per_day,
    }


def _describe_config(cfg: FilterConfig) -> str:
    return (
        f"score>={cfg.score_min}, vol>={cfg.volume_min:.2f}, "
        f"chg<={cfg.change_max:.1f}, time<={_minute_to_label(cfg.entry_cutoff_min)}, "
        f"top{cfg.top_n}/day"
    )


def _static_leaderboard(df: pd.DataFrame, configs: Iterable[FilterConfig], min_trades: int, min_days: int) -> List[dict]:
    rows: List[dict] = []
    for cfg in configs:
        chosen = _apply_config(df, cfg)
        metrics = _summarize(chosen)
        if metrics["trades"] < min_trades or metrics["days"] < min_days:
            continue
        rows.append({"config": cfg, **metrics})
    rows.sort(
        key=lambda row: (
            row["rank_score"],
            row["avg_net"],
            row["median_net"],
            row["win_rate"],
        ),
        reverse=True,
    )
    return rows


def _walk_forward(
    df: pd.DataFrame,
    configs: Iterable[FilterConfig],
    train_days: int,
    min_trades_train: int,
    min_days_train: int,
) -> tuple[pd.DataFrame, List[dict]]:
    dates = sorted(df["date"].unique().tolist())
    picks: List[pd.DataFrame] = []
    decisions: List[dict] = []

    for idx in range(train_days, len(dates)):
        train_dates = dates[idx - train_days:idx]
        test_date = dates[idx]
        train_df = df[df["date"].isin(train_dates)].copy()
        test_df = df[df["date"] == test_date].copy()
        if train_df.empty or test_df.empty:
            continue

        best_row: Optional[dict] = None
        for cfg in configs:
            selected_train = _apply_config(train_df, cfg)
            metrics = _summarize(selected_train)
            if metrics["trades"] < min_trades_train or metrics["days"] < min_days_train:
                continue
            row = {"config": cfg, **metrics}
            if best_row is None or (
                row["rank_score"],
                row["avg_net"],
                row["median_net"],
                row["win_rate"],
            ) > (
                best_row["rank_score"],
                best_row["avg_net"],
                best_row["median_net"],
                best_row["win_rate"],
            ):
                best_row = row

        if best_row is None:
            continue

        selected_test = _apply_config(test_df, best_row["config"])
        test_metrics = _summarize(selected_test)
        decisions.append(
            {
                "test_date": test_date,
                "train_window": f"{train_dates[0]}..{train_dates[-1]}",
                "config": best_row["config"],
                "train_trades": best_row["trades"],
                "train_avg_net": best_row["avg_net"],
                "test_trades": test_metrics["trades"],
                "test_avg_net": test_metrics["avg_net"],
                "test_win_rate": test_metrics["win_rate"],
            }
        )
        if not selected_test.empty:
            selected_test = selected_test.copy()
            selected_test["wf_test_date"] = test_date
            selected_test["wf_train_window"] = f"{train_dates[0]}..{train_dates[-1]}"
            selected_test["wf_config"] = _describe_config(best_row["config"])
            picks.append(selected_test)

    if not picks:
        return df.iloc[0:0].copy(), decisions
    return pd.concat(picks, ignore_index=True), decisions


def _print_summary(title: str, df: pd.DataFrame) -> None:
    metrics = _summarize(df)
    print(f"\n{title}")
    print("-" * len(title))
    print(f"Trades:        {metrics['trades']}")
    print(f"Days:          {metrics['days']}")
    print(f"Trades / day:  {metrics['trades_per_day']:.2f}")
    print(f"Win rate:      {metrics['win_rate']:.1f}%")
    print(f"Avg net:       {metrics['avg_net']:+.2f}%")
    print(f"Median net:    {metrics['median_net']:+.2f}%")
    print(f"Avg gross:     {metrics['avg_gross']:+.2f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward optimizer for intraday backtest trades")
    parser.add_argument("--csv", default=os.path.join("results", "backtest_intraday_live.csv"), help="CSV exported by backtest_intraday_live.py")
    parser.add_argument("--train-days", type=int, default=5, help="Number of prior days used to choose each test-day config")
    parser.add_argument("--min-trades-train", type=int, default=12, help="Minimum training trades for a config to be considered")
    parser.add_argument("--min-days-train", type=int, default=3, help="Minimum training days covered by a config")
    parser.add_argument("--top-k", type=int, default=15, help="How many static leaderboard rows to print")
    parser.add_argument("--export", default="", help="Optional CSV path for walk-forward selected trades")
    args = parser.parse_args()

    trades_df = _load_trades(args.csv)
    configs = _generate_configs()

    print(f"Loaded {len(trades_df)} trades from {args.csv}")
    print(f"Dates: {trades_df['date'].min()} .. {trades_df['date'].max()} ({trades_df['date'].nunique()} days)")

    _print_summary("Baseline (all simulated trades)", trades_df)

    leaderboard = _static_leaderboard(
        trades_df,
        configs=configs,
        min_trades=max(5, int(args.min_trades_train)),
        min_days=max(1, int(args.min_days_train)),
    )
    print("\nTop static filters")
    print("------------------")
    if not leaderboard:
        print("No configs met the minimum trade/day requirements.")
    else:
        print(f"{'Rank':>4s} {'Trades':>6s} {'Days':>5s} {'WR':>6s} {'Net':>8s} {'Gross':>8s}  Filter")
        for rank, row in enumerate(leaderboard[: max(1, args.top_k)], start=1):
            print(
                f"{rank:>4d} {row['trades']:>6d} {row['days']:>5d} "
                f"{row['win_rate']:>5.1f}% {row['avg_net']:>+7.2f}% {row['avg_gross']:>+7.2f}%  "
                f"{_describe_config(row['config'])}"
            )

    wf_df, decisions = _walk_forward(
        trades_df,
        configs=configs,
        train_days=max(1, int(args.train_days)),
        min_trades_train=max(3, int(args.min_trades_train)),
        min_days_train=max(1, int(args.min_days_train)),
    )
    _print_summary("Walk-forward selected trades", wf_df)

    if decisions:
        print("\nWalk-forward decisions")
        print("----------------------")
        print(f"{'Test Date':<12s} {'Train Window':<23s} {'TrainN':>7s} {'TrainNet':>9s} {'TestN':>6s} {'TestNet':>8s} {'WR':>6s}  Filter")
        for row in decisions:
            print(
                f"{row['test_date']:<12s} {row['train_window']:<23s} {row['train_trades']:>7d} "
                f"{row['train_avg_net']:>+8.2f}% {row['test_trades']:>6d} {row['test_avg_net']:>+7.2f}% "
                f"{row['test_win_rate']:>5.1f}%  {_describe_config(row['config'])}"
            )
    else:
        print("\nWalk-forward decisions")
        print("----------------------")
        print("Not enough dates/trades to form train/test windows.")

    if args.export:
        wf_df.to_csv(args.export, index=False)
        print(f"\nExported walk-forward picks to {args.export}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

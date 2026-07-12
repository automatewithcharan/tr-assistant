#!/usr/bin/env python3
"""Audit whether adding lower-ranked intraday trades improves portfolio profit.

The input must contain post-entry replay outcomes. Capital is split equally over
the configured number of slots; unfilled slots remain cash. The audit does not
pretend that capital can be reused intraday because the current replay artifact
does not contain exact exit timestamps.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


DEFAULT_INPUT = (
    "results/Data/"
    "intraday_replacement_selection_n500_mfe1_liquidity_guarded_early_failure_"
    "vwap70_exitfine_best_replayed_trades.csv"
)
DEFAULT_BASE = (
    "results/Data/"
    "intraday_event_family_ranker_walk_forward_n500_mfe1_liquidity_scored_trades.csv"
)
DEFAULT_OUTPUT = "results/Data/intraday_multi_trade_scaling_audit"


def load_ranked_candidates(path: str, min_vwap_dist_pct: float) -> pd.DataFrame:
    candidates = pd.read_csv(path)
    required = {
        "date",
        "symbol",
        "event_type",
        "trade_return_pct",
        "rank_score",
        "adjusted_score",
        "vwap_dist_pct",
    }
    missing = required - set(candidates.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")

    candidates["date"] = candidates["date"].astype(str)
    candidates["symbol"] = candidates["symbol"].astype(str).str.strip().str.upper()
    numeric = ["trade_return_pct", "rank_score", "adjusted_score", "vwap_dist_pct"]
    for column in numeric:
        candidates[column] = pd.to_numeric(candidates[column], errors="coerce")
    candidates = candidates.dropna(subset=numeric + ["symbol"])
    candidates = candidates[candidates["vwap_dist_pct"] >= float(min_vwap_dist_pct)].copy()

    # Several event families can fire for one stock. They are one executable
    # position, so retain only its highest-ranked event for the day.
    candidates.sort_values(
        ["date", "adjusted_score", "rank_score", "symbol", "event_type"],
        ascending=[True, False, False, True, True],
        inplace=True,
    )
    return candidates.drop_duplicates(["date", "symbol"], keep="first").reset_index(drop=True)


def load_dates(path: str) -> list[str]:
    dates = pd.read_csv(path, usecols=["date"])["date"].astype(str)
    return sorted(dates.unique().tolist())


def build_daily(
    candidates: pd.DataFrame,
    dates: list[str],
    slots: int,
    capital: float,
    daily_goal: float,
    extra_cost_pct: float = 0.0,
) -> pd.DataFrame:
    if slots < 1:
        raise ValueError("slots must be at least 1")
    if capital <= 0:
        raise ValueError("capital must be positive")

    slot_capital = float(capital) / int(slots)
    grouped = {date: day for date, day in candidates.groupby("date", sort=True)}
    empty_day = candidates.iloc[0:0]
    rows = []
    for date in dates:
        day = grouped.get(str(date), empty_day).head(int(slots))
        returns = (
            day["trade_return_pct"].astype(float) - float(extra_cost_pct)
            if not day.empty
            else pd.Series(dtype=float)
        )
        pnl = float((returns * slot_capital / 100.0).sum())
        rows.append(
            {
                "date": str(date),
                "target_slots": int(slots),
                "trades": int(len(day)),
                "deployed_capital": round(float(len(day)) * slot_capital, 2),
                "pnl": round(pnl, 2),
                "daily_return_pct": round(pnl / float(capital) * 100.0, 6),
                "daily_goal_hit": bool(pnl >= float(daily_goal)),
                "symbols": "|".join(day["symbol"].astype(str).tolist()),
            }
        )
    return pd.DataFrame(rows)


def summarize(daily: pd.DataFrame, slots: int, capital: float, daily_goal: float) -> dict:
    train = daily.iloc[:206]
    validation = daily.iloc[206:309]
    final = daily.tail(104)

    def mean(frame: pd.DataFrame) -> float:
        return float(frame["daily_return_pct"].mean()) if not frame.empty else 0.0

    return {
        "evidence": "historical_5minute_replay_portfolio_proxy",
        "capital_reuse": "disabled_no_exact_exit_timestamps",
        "slots": int(slots),
        "capital": float(capital),
        "capital_per_slot": round(float(capital) / int(slots), 2),
        "daily_goal": float(daily_goal),
        "days": int(len(daily)),
        "traded_days": int((daily["trades"] > 0).sum()),
        "trades": int(daily["trades"].sum()),
        "avg_daily_pnl": round(float(daily["pnl"].mean()), 2),
        "avg_daily_return_pct": round(mean(daily), 6),
        "train_avg_daily_return_pct": round(mean(train), 6),
        "validation_avg_daily_return_pct": round(mean(validation), 6),
        "final_avg_daily_return_pct": round(mean(final), 6),
        "daily_goal_hit_days": int(daily["daily_goal_hit"].sum()),
        "daily_goal_hit_pct": round(float(daily["daily_goal_hit"].mean() * 100.0), 4),
        "positive_day_pct": round(float((daily["pnl"] > 0).mean() * 100.0), 4),
        "worst_day_pnl": round(float(daily["pnl"].min()), 2),
        "best_day_pnl": round(float(daily["pnl"].max()), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT)
    parser.add_argument("--capital", type=float, default=400000.0)
    parser.add_argument("--daily-goal", type=float, default=10000.0)
    parser.add_argument("--max-slots", type=int, default=10)
    parser.add_argument("--min-vwap-dist-pct", type=float, default=0.70)
    parser.add_argument("--extra-cost-pct", type=float, default=0.0)
    args = parser.parse_args()

    candidates = load_ranked_candidates(args.input, args.min_vwap_dist_pct)
    dates = load_dates(args.base)
    summaries = []
    daily_frames = []
    for slots in range(1, int(args.max_slots) + 1):
        daily = build_daily(
            candidates,
            dates,
            slots=slots,
            capital=args.capital,
            daily_goal=args.daily_goal,
            extra_cost_pct=args.extra_cost_pct,
        )
        daily_frames.append(daily)
        summaries.append(summarize(daily, slots, args.capital, args.daily_goal))

    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = prefix.with_name(prefix.name + "_summary.csv")
    daily_path = prefix.with_name(prefix.name + "_daily.csv")
    summary = pd.DataFrame(summaries)
    summary.to_csv(summary_path, index=False)
    pd.concat(daily_frames, ignore_index=True).to_csv(daily_path, index=False)
    print(f"Saved: {summary_path}")
    print(f"Saved: {daily_path}")
    print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Simulate portfolio-level intraday goal rules from alert-lane backtest rows."""

from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path

import pandas as pd


def _exit_return(row: pd.Series, target_pct: float, stop_pct: float, eod_exit: bool) -> float:
    mfe = float(row.get("max_fav_pct", 0.0) or 0.0)
    mae = float(row.get("max_adv_pct", 0.0) or 0.0)
    eod = float(row.get("eod_return_pct", 0.0) or 0.0)

    target_hit = mfe >= target_pct
    stop_hit = mae <= -abs(stop_pct)
    if target_hit and stop_hit:
        return -abs(stop_pct)  # conservative: unknown intraday order
    if target_hit:
        return target_pct
    if stop_hit:
        return -abs(stop_pct)
    return eod if eod_exit else 0.0


def _build_signal_sets(df: pd.DataFrame) -> dict[tuple[str, str], set[str]]:
    return df.groupby(["date", "symbol"])["lane"].apply(lambda s: set(map(str, s))).to_dict()


def _eligible(df: pd.DataFrame, lane_rule: str, signal_sets: dict[tuple[str, str], set[str]]) -> pd.DataFrame:
    work = df.copy()
    if lane_rule == "premove":
        return work[work["action"].astype(str).str.contains("Pre-Move Watch", na=False)]
    if lane_rule == "grade_a":
        return work[work["action"].astype(str).str.contains("Grade-A Runner", na=False)]
    if lane_rule == "anomaly":
        return work[work["lane"] == "anomaly"]
    if lane_rule == "direct":
        return work[work["lane"] == "direct"]
    if lane_rule == "ob":
        return work[work["lane"] == "ob"]
    if lane_rule == "direct_or_anomaly":
        return work[work["lane"].isin(["direct", "anomaly"])]
    if lane_rule == "direct_plus_ob":
        mask = work.apply(
            lambda r: r["lane"] == "direct" and "ob" in signal_sets.get((r["date"], r["symbol"]), set()),
            axis=1,
        )
        return work[mask]
    if lane_rule == "anomaly_plus_ob":
        mask = work.apply(
            lambda r: r["lane"] == "anomaly" and "ob" in signal_sets.get((r["date"], r["symbol"]), set()),
            axis=1,
        )
        return work[mask]
    if lane_rule == "direct_plus_anomaly":
        mask = work.apply(
            lambda r: r["lane"] in {"direct", "anomaly"}
            and {"direct", "anomaly"}.issubset(signal_sets.get((r["date"], r["symbol"]), set())),
            axis=1,
        )
        return work[mask]
    raise ValueError(f"Unknown lane rule: {lane_rule}")


def simulate(
    df: pd.DataFrame,
    lane_rule: str,
    target_pct: float,
    stop_pct: float,
    max_trades: int,
    eod_exit: bool,
    goal_pct: float,
) -> tuple[pd.DataFrame, dict]:
    signal_sets = _build_signal_sets(df)
    eligible = _eligible(df, lane_rule, signal_sets).copy()
    if eligible.empty:
        return eligible, {
            "lane_rule": lane_rule,
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "max_trades": max_trades,
            "days": 0,
            "trade_count": 0,
            "avg_daily_pct": 0.0,
            "win_day_pct": 0.0,
            "hit_goal_day_pct": 0.0,
        }

    eligible.sort_values(["date", "when", "symbol"], inplace=True)
    selected = eligible.groupby("date", group_keys=False).head(max_trades).copy()
    selected["trade_return_pct"] = selected.apply(
        lambda r: _exit_return(r, target_pct=target_pct, stop_pct=stop_pct, eod_exit=eod_exit),
        axis=1,
    )
    daily = selected.groupby("date").agg(
        trades=("symbol", "count"),
        daily_pct=("trade_return_pct", "mean"),
        symbols=("symbol", lambda s: ",".join(map(str, s))),
    ).reset_index()
    summary = {
        "lane_rule": lane_rule,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "max_trades": max_trades,
        "eod_exit": bool(eod_exit),
        "days": int(len(daily)),
        "trade_count": int(len(selected)),
        "avg_daily_pct": round(float(daily["daily_pct"].mean()), 3) if not daily.empty else 0.0,
        "median_daily_pct": round(float(daily["daily_pct"].median()), 3) if not daily.empty else 0.0,
        "min_daily_pct": round(float(daily["daily_pct"].min()), 3) if not daily.empty else 0.0,
        "max_daily_pct": round(float(daily["daily_pct"].max()), 3) if not daily.empty else 0.0,
        "win_day_pct": round(float((daily["daily_pct"] > 0).mean() * 100.0), 2) if not daily.empty else 0.0,
        "hit_goal_day_pct": round(float((daily["daily_pct"] >= goal_pct).mean() * 100.0), 2) if not daily.empty else 0.0,
    }
    return daily, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate intraday 3% daily goal from alert backtest rows")
    parser.add_argument("--input", default="results/Data/alert_lane_backtest_all_lanes.csv")
    parser.add_argument("--out-prefix", default="results/Data/intraday_goal")
    parser.add_argument("--goal-pct", type=float, default=3.0)
    parser.add_argument("--burst-min-days", type=int, default=2)
    parser.add_argument("--burst-max-days", type=int, default=3)
    parser.add_argument("--robust-min-days", type=int, default=5)
    args = parser.parse_args()

    df = pd.read_csv(args.input, parse_dates=["when"])
    df = df[df.get("status", "ok") == "ok"].copy()
    df["date"] = df["when"].dt.date.astype(str)

    lane_rules = [
        "premove",
        "grade_a",
        "anomaly",
        "direct",
        "ob",
        "direct_or_anomaly",
        "direct_plus_ob",
        "anomaly_plus_ob",
        "direct_plus_anomaly",
    ]
    targets = [1.0, 1.5, 2.0, 3.0]
    stops = [0.8, 1.0, 1.5, 2.0]
    max_trades_values = [1, 2, 3]
    eod_values = [True]

    summaries = []
    daily_frames = []
    for lane_rule, target, stop, max_trades, eod_exit in product(
        lane_rules, targets, stops, max_trades_values, eod_values
    ):
        daily, summary = simulate(df, lane_rule, target, stop, max_trades, eod_exit, goal_pct=float(args.goal_pct))
        summaries.append(summary)
        if not daily.empty:
            daily = daily.copy()
            for key, value in summary.items():
                if key not in daily.columns:
                    daily[key] = value
            daily_frames.append(daily)

    summary_df = pd.DataFrame(summaries)
    summary_df.sort_values(
        ["avg_daily_pct", "hit_goal_day_pct", "days", "trade_count"],
        ascending=[False, False, False, False],
        inplace=True,
    )
    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = out_prefix.with_name(out_prefix.name + "_summary.csv")
    daily_path = out_prefix.with_name(out_prefix.name + "_daily.csv")
    summary_df.to_csv(summary_path, index=False)
    pd.concat(daily_frames, ignore_index=True).to_csv(daily_path, index=False)

    cols = [
        "lane_rule", "target_pct", "stop_pct", "max_trades", "days", "trade_count",
        "avg_daily_pct", "median_daily_pct", "min_daily_pct", "win_day_pct", "hit_goal_day_pct",
    ]
    print(f"Saved: {summary_path}")
    print(f"Saved: {daily_path}")
    print("\nTop simulations:")
    print(summary_df[cols].head(40).to_string(index=False))
    print(f"\nBurst rules with avg daily >= {args.goal_pct:g}% and {args.burst_min_days}-{args.burst_max_days} active days:")
    burst = summary_df[
        (summary_df["avg_daily_pct"] >= float(args.goal_pct))
        & (summary_df["days"] >= int(args.burst_min_days))
        & (summary_df["days"] <= int(args.burst_max_days))
    ]
    print("None." if burst.empty else burst[cols].to_string(index=False))

    print(f"\nRules with avg daily >= {args.goal_pct:g}% and days >= {args.robust_min_days}:")
    robust = summary_df[
        (summary_df["avg_daily_pct"] >= float(args.goal_pct))
        & (summary_df["days"] >= int(args.robust_min_days))
    ]
    print("None." if robust.empty else robust[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Historical replay of frozen same-timestamp selectors and exit contracts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import t as student_t


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.assess_event_shadow_candidate_selectors import SELECTORS, score_batch  # noqa: E402
from scripts.select_causal_intraday_exit_grid import (  # noqa: E402
    _load_candidates,
    build_contracts,
    replay_exit_grid,
    split_scored_dates,
)


DEFAULT_INPUT = "results/Data/intraday_event_candidates_n500_causal_cross_sectional_trades.csv"
DEFAULT_DATA_DIR = "results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29"
DEFAULT_OUTPUT = "results/Data/historical_event_selector_exit_registry"
TARGETS = (1.5, 3.0)
STOPS = (1.0,)
POSITION_COUNTS = (1, 2)


def selected_indices(candidates: pd.DataFrame, selector: str, positions: int) -> np.ndarray:
    eligible = candidates.copy()
    first_times = eligible.groupby("date")["signal_time"].transform("min")
    eligible = eligible[eligible["signal_time"].eq(first_times)].copy()
    eligible["event_score"] = pd.to_numeric(eligible["score"], errors="coerce").fillna(0.0)
    scored_parts = []
    for _, day in eligible.groupby("date", sort=False):
        day = day.copy()
        day["selector_score"] = score_batch(day, selector)
        scored_parts.append(day)
    if not scored_parts:
        return np.array([], dtype=int)
    eligible = pd.concat(scored_parts, axis=0)
    eligible.sort_values(
        ["date", "selector_score", "event_score", "symbol", "event_type"],
        ascending=[True, False, False, True, True],
        inplace=True,
    )
    eligible = eligible.drop_duplicates(["date", "symbol"], keep="first")
    eligible = eligible.groupby("date", group_keys=False).head(max(1, int(positions)))
    return eligible.index.to_numpy(dtype=int)


def daily_returns(
    candidates: pd.DataFrame,
    net_returns: np.ndarray,
    dates: list[str],
    indices: np.ndarray,
) -> pd.DataFrame:
    selected = candidates.loc[indices, ["date", "symbol", "event_type", "signal_time"]].copy()
    selected["net_return_pct"] = [float(net_returns[int(index)]) for index in indices]
    selected = selected[np.isfinite(selected["net_return_pct"])].copy()
    grouped = selected.groupby("date")["net_return_pct"].agg(["count", "mean"]) if not selected.empty else pd.DataFrame()
    rows = []
    for date in dates:
        if not grouped.empty and date in grouped.index:
            trades = int(grouped.loc[date, "count"])
            value = float(grouped.loc[date, "mean"])
        else:
            trades = 0
            value = 0.0
        rows.append({"date": date, "trades": trades, "daily_return_pct": value})
    return pd.DataFrame(rows)


def metrics(daily: pd.DataFrame, capital: float, goal: float) -> dict:
    values = pd.to_numeric(daily["daily_return_pct"], errors="coerce").fillna(0.0)
    count = int(len(values))
    mean = float(values.mean()) if count else 0.0
    if count >= 2:
        lower = mean - float(student_t.ppf(0.975, count - 1)) * float(values.std(ddof=1) / np.sqrt(count))
    else:
        lower = float("-inf")
    return {
        "days": count,
        "traded_days": int((daily["trades"] > 0).sum()),
        "avg_daily_return_pct": round(mean, 6),
        "avg_daily_pnl": round(capital * mean / 100.0, 2),
        "positive_day_pct": round(float((values > 0).mean() * 100.0), 4) if count else 0.0,
        "goal_hit_pct": round(float((values * capital / 100.0 >= goal).mean() * 100.0), 4) if count else 0.0,
        "worst_day_pct": round(float(values.min()), 6) if count else 0.0,
        "lower_95_pct": round(lower, 6) if np.isfinite(lower) else None,
    }


def evaluate(
    candidates: pd.DataFrame,
    gross: np.ndarray,
    contracts,
    splits: dict[str, list[str]],
    cost_pct: float,
    stress_pct: float,
    capital: float,
    goal: float,
) -> tuple[pd.DataFrame, dict[tuple, pd.DataFrame]]:
    rows = []
    daily_outputs = {}
    pre_final_dates = [*splits["train"], *splits["validation"]]
    blocks = [list(values) for values in np.array_split(pre_final_dates, 4) if len(values)]
    for selector in SELECTORS:
        for positions in POSITION_COUNTS:
            indices = selected_indices(candidates, selector, positions)
            for contract_index, contract in enumerate(contracts):
                net = gross[:, contract_index].astype(float) - float(cost_pct) - float(stress_pct)
                key = (selector, contract.target_pct, contract.stop_pct, positions)
                all_dates = [*splits["train"], *splits["validation"], *splits["final"]]
                all_daily = daily_returns(candidates, net, all_dates, indices)
                daily_outputs[key] = all_daily
                row = {
                    "config": f"{selector}_t{contract.target_pct:g}_s{contract.stop_pct:g}_n{positions}",
                    "selector": selector,
                    "target_pct": contract.target_pct,
                    "stop_pct": contract.stop_pct,
                    "positions": positions,
                    "cost_pct": float(cost_pct),
                    "extra_stress_pct": float(stress_pct),
                }
                for split_name, dates in splits.items():
                    split_daily = all_daily[all_daily["date"].isin(dates)]
                    for name, value in metrics(split_daily, capital, goal).items():
                        row[f"{split_name}_{name}"] = value
                block_values = []
                for index, dates in enumerate(blocks, start=1):
                    block = metrics(all_daily[all_daily["date"].isin(dates)], capital, goal)
                    value = float(block["avg_daily_return_pct"])
                    row[f"selection_block_{index}_avg_daily_return_pct"] = value
                    block_values.append(value)
                row["selection_block_floor_pct"] = round(min(block_values), 6)
                row["selection_block_mean_pct"] = round(float(np.mean(block_values)), 6)
                row["selection_floor_pct"] = round(
                    min(float(row["train_avg_daily_return_pct"]), float(row["validation_avg_daily_return_pct"])), 6
                )
                row["selection_mean_pct"] = round(
                    (float(row["train_avg_daily_return_pct"]) + float(row["validation_avg_daily_return_pct"])) / 2.0, 6
                )
                rows.append(row)
    summary = pd.DataFrame(rows)
    summary.sort_values(
        ["selection_block_floor_pct", "selection_block_mean_pct", "selection_floor_pct", "validation_avg_daily_return_pct", "config"],
        ascending=[False, False, False, False, True],
        inplace=True,
    )
    return summary.reset_index(drop=True), daily_outputs


def acceptance(selected: pd.Series, min_final_days: int, required_avg_pnl: float) -> dict:
    prefinal_positive = (
        float(selected["train_avg_daily_return_pct"]) > 0.0
        and float(selected["validation_avg_daily_return_pct"]) > 0.0
        and float(selected["selection_block_floor_pct"]) > 0.0
    )
    final_sample = int(selected["final_days"]) >= int(min_final_days)
    final_confidence = float(selected["final_lower_95_pct"]) > 0.0
    final_goal = float(selected["final_avg_daily_pnl"]) >= float(required_avg_pnl)
    return {
        "prefinal_consistency_gate": bool(prefinal_positive),
        "final_sample_gate": bool(final_sample),
        "final_confidence_gate": bool(final_confidence),
        "final_goal_gate": bool(final_goal),
        "accepted": bool(prefinal_positive and final_sample and final_confidence and final_goal),
        "real_orders_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT)
    parser.add_argument("--warmup-days", type=int, default=80)
    parser.add_argument("--cost-pct", type=float, default=0.15)
    parser.add_argument("--extra-stress-pct", type=float, default=0.10)
    parser.add_argument("--capital", type=float, default=400000.0)
    parser.add_argument("--daily-goal", type=float, default=10000.0)
    parser.add_argument("--min-final-days", type=int, default=100)
    parser.add_argument("--progress-every", type=int, default=50)
    args = parser.parse_args()

    candidates = _load_candidates(args.input)
    contracts = build_contracts(list(TARGETS), list(STOPS), side="long")
    gross, _, replay_stats = replay_exit_grid(candidates, args.data_dir, contracts, args.progress_every)
    _, splits = split_scored_dates(candidates, args.warmup_days)
    summary, daily_outputs = evaluate(
        candidates, gross, contracts, splits, args.cost_pct, args.extra_stress_pct, args.capital, args.daily_goal
    )
    selected = summary.iloc[0]
    key = (str(selected["selector"]), float(selected["target_pct"]), float(selected["stop_pct"]), int(selected["positions"]))
    gates = acceptance(selected, args.min_final_days, args.daily_goal)
    manifest = {
        "evidence": "historical_same_timestamp_selector_exit_registry",
        "selection_uses_final_period": False,
        "selected_configuration": str(selected["config"]),
        "selectors": list(SELECTORS),
        "targets": list(TARGETS),
        "stops": list(STOPS),
        "position_counts": list(POSITION_COUNTS),
        "split_days": {name: len(dates) for name, dates in splits.items()},
        "train_avg_daily_pnl": float(selected["train_avg_daily_pnl"]),
        "validation_avg_daily_pnl": float(selected["validation_avg_daily_pnl"]),
        "final_avg_daily_pnl_after_selection": float(selected["final_avg_daily_pnl"]),
        "final_lower_95_pct": float(selected["final_lower_95_pct"]),
        "final_goal_hit_pct": float(selected["final_goal_hit_pct"]),
        "replay_stats": replay_stats,
        **gates,
    }
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    daily_outputs[key].to_csv(prefix.with_name(prefix.name + "_selected_daily.csv"), index=False)
    prefix.with_name(prefix.name + "_selected.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    print(summary.head(24).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

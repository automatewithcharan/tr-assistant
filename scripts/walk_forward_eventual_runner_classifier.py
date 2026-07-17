#!/usr/bin/env python3
"""Walk-forward classifier for early candidates that later become 10% runners."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_rank_acceleration_registry import period_metrics, temporal_splits
from scripts.select_causal_intraday_exit_grid import load_calendar_dates
from scripts.simulate_causal_intraday_capital_reuse import simulate_calendar


FEATURES = [
    "signal_minute",
    "move_from_open_pct",
    "day_change_pct",
    "gap_pct",
    "benchmark_move_pct",
    "relative_strength_pct",
    "cum_volume_ratio",
    "volume_acceleration",
    "log_signal_turnover",
    "range_position",
    "pullback_pct",
    "vwap_distance_pct",
    "recent_return_3bar_pct",
    "move_rank",
    "volume_rank",
    "acceleration_rank",
    "relative_strength_rank",
    "range_rank",
    "turnover_rank",
    "sector_breadth_rank",
    "pullback_quality_rank",
    "vwap_quality_rank",
]


def build_runner_labels(
    candidates: pd.DataFrame,
    runner_events: pd.DataFrame,
    threshold_pct: float = 10.0,
) -> pd.DataFrame:
    events = runner_events[
        pd.to_numeric(runner_events["runner_threshold_pct"], errors="coerce").eq(
            float(threshold_pct)
        )
    ].copy()
    events["date"] = events["date"].astype(str)
    events["symbol"] = events["symbol"].astype(str).str.strip().str.upper()
    events["first_cross_minute"] = pd.to_numeric(
        events["first_cross_minute"], errors="coerce"
    )
    events = events.dropna(subset=["first_cross_minute"]).drop_duplicates(
        ["date", "symbol"]
    )
    cross_map = events.set_index(["date", "symbol"])["first_cross_minute"]
    output = candidates.copy()
    output["date"] = output["date"].astype(str)
    output["symbol"] = output["symbol"].astype(str).str.strip().str.upper()
    keys = pd.MultiIndex.from_frame(output[["date", "symbol"]])
    output["runner_first_cross_minute"] = cross_map.reindex(keys).to_numpy()
    output["eventual_runner_label"] = (
        output["runner_first_cross_minute"].notna()
        & (output["runner_first_cross_minute"] > output["signal_minute"])
    ).astype(int)
    return output


def load_labeled_candidates(
    candidate_path: str,
    event_path: str,
    return_column: str,
    exit_time_column: str,
    threshold_pct: float = 10.0,
) -> pd.DataFrame:
    candidates = pd.read_csv(candidate_path)
    required = {
        "date",
        "symbol",
        "signal_minute",
        "entry_time",
        "signal_turnover",
        return_column,
        exit_time_column,
        *[feature for feature in FEATURES if feature != "log_signal_turnover"],
    }
    missing = required - set(candidates.columns)
    if missing:
        raise ValueError(f"candidate input missing columns: {', '.join(sorted(missing))}")
    candidates["entry_time"] = pd.to_datetime(candidates["entry_time"], errors="coerce")
    candidates[exit_time_column] = pd.to_datetime(
        candidates[exit_time_column], errors="coerce"
    )
    candidates[return_column] = pd.to_numeric(candidates[return_column], errors="coerce")
    candidates["log_signal_turnover"] = np.log1p(
        pd.to_numeric(candidates["signal_turnover"], errors="coerce").clip(lower=0.0)
    )
    for feature in FEATURES:
        candidates[feature] = pd.to_numeric(candidates[feature], errors="coerce")
    candidates = candidates.dropna(
        subset=["date", "symbol", "entry_time", exit_time_column, return_column]
    ).copy()
    events = pd.read_csv(event_path)
    return build_runner_labels(candidates, events, threshold_pct)


def _model(min_samples_leaf: int = 40) -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            (
                "model",
                HistGradientBoostingClassifier(
                    loss="log_loss",
                    max_iter=80,
                    learning_rate=0.04,
                    max_leaf_nodes=12,
                    min_samples_leaf=int(min_samples_leaf),
                    l2_regularization=3.0,
                    random_state=42,
                ),
            ),
        ]
    )


def score_walk_forward(
    frame: pd.DataFrame,
    calendar_dates: list[str],
    lookback_days: int = 200,
    min_train_days: int = 80,
    min_train_rows: int = 500,
    min_positive_rows: int = 20,
    retrain_every_days: int = 10,
    min_samples_leaf: int = 40,
) -> pd.DataFrame:
    groups = {date: group.copy() for date, group in frame.groupby("date", sort=False)}
    scored = []
    model = None
    trained_index = -1
    trained_through = ""
    for index, date in enumerate(calendar_dates):
        if index < int(min_train_days):
            continue
        day = groups.get(date)
        if day is None or day.empty:
            continue
        prior_dates = calendar_dates[max(0, index - int(lookback_days)) : index]
        if model is None or index - trained_index >= int(retrain_every_days):
            parts = [groups[prior] for prior in prior_dates if prior in groups]
            if not parts:
                continue
            train = pd.concat(parts, ignore_index=True)
            positives = int(train["eventual_runner_label"].sum())
            negatives = int(len(train) - positives)
            if (
                len(train) < int(min_train_rows)
                or positives < int(min_positive_rows)
                or negatives <= 0
            ):
                continue
            day_counts = train.groupby("date")["symbol"].transform("count").clip(lower=1)
            positive_boost = min(50.0, negatives / max(1.0, positives))
            weights = (1.0 / day_counts.to_numpy(dtype=float)) * np.where(
                train["eventual_runner_label"].to_numpy(dtype=int) == 1,
                positive_boost,
                1.0,
            )
            model = _model(min_samples_leaf)
            model.fit(
                train[FEATURES],
                train["eventual_runner_label"],
                model__sample_weight=weights,
            )
            trained_index = index
            trained_through = prior_dates[-1]
        scored_day = day.copy()
        scored_day["runner_probability"] = model.predict_proba(
            scored_day[FEATURES]
        )[:, 1]
        scored_day["model_trained_through"] = trained_through
        scored.append(scored_day)
    return pd.concat(scored, ignore_index=True) if scored else pd.DataFrame()


def select_candidates(
    scored: pd.DataFrame,
    threshold: float,
    top_per_signal: int,
) -> pd.DataFrame:
    selected = scored[scored["runner_probability"] >= float(threshold)].copy()
    if selected.empty:
        return selected
    selected.sort_values(
        ["date", "signal_minute", "runner_probability", "symbol"],
        ascending=[True, True, False, True],
        inplace=True,
    )
    selected = selected.groupby(["date", "signal_minute"], group_keys=False).head(
        int(top_per_signal)
    )
    selected["selection_score"] = selected["runner_probability"]
    return selected


def evaluate_registry(
    scored: pd.DataFrame,
    calendar_dates: list[str],
    return_column: str,
    exit_time_column: str,
    capital: float = 400_000.0,
    daily_goal: float = 10_000.0,
    stress_cost_pct: float = 0.10,
    thresholds: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9),
) -> tuple[pd.DataFrame, dict[tuple[float, int, int], tuple[pd.DataFrame, pd.DataFrame]]]:
    scored_dates = [date for date in calendar_dates if date >= str(scored["date"].min())]
    splits = temporal_splits(scored_dates, warmup_days=0)
    prefinal = [*splits["train"], *splits["validation"]]
    blocks = [list(values) for values in np.array_split(prefinal, 4) if len(values)]
    rows = []
    artifacts = {}
    for threshold in thresholds:
        for top_per_signal in (1, 2):
            selected = select_candidates(scored, threshold, top_per_signal)
            for slots in (1, 2):
                key = (float(threshold), int(top_per_signal), int(slots))
                daily, trades = simulate_calendar(
                    selected,
                    scored_dates,
                    capital,
                    slots,
                    return_column,
                    exit_time_column,
                    daily_goal,
                )
                stress_daily, _ = simulate_calendar(
                    selected,
                    scored_dates,
                    capital,
                    slots,
                    return_column,
                    exit_time_column,
                    daily_goal,
                    extra_cost_pct=stress_cost_pct,
                )
                artifacts[key] = (daily, trades)
                row: dict[str, Any] = {
                    "threshold": float(threshold),
                    "top_per_signal": int(top_per_signal),
                    "slots": int(slots),
                    "capital_reuse": "exact_exit_bar_unleveraged",
                }
                for name, dates in splits.items():
                    period = daily[daily["date"].isin(dates)]
                    row.update(period_metrics(period, name, capital))
                    stress_period = stress_daily[stress_daily["date"].isin(dates)]
                    row.update(period_metrics(stress_period, f"stress_{name}", capital))
                block_values = []
                stress_block_values = []
                for index, dates in enumerate(blocks, start=1):
                    value = float(
                        daily[daily["date"].isin(dates)]["daily_return_pct"].mean()
                    )
                    stress_value = float(
                        stress_daily[stress_daily["date"].isin(dates)][
                            "daily_return_pct"
                        ].mean()
                    )
                    row[f"block_{index}_avg_daily_return_pct"] = round(value, 6)
                    row[f"stress_block_{index}_avg_daily_return_pct"] = round(
                        stress_value, 6
                    )
                    block_values.append(value)
                    stress_block_values.append(stress_value)
                executed = trades[trades["executed"].eq(True)] if not trades.empty else trades
                row["selected_runner_precision_pct"] = round(
                    float(executed["eventual_runner_label"].mean() * 100.0)
                    if not executed.empty
                    else 0.0,
                    2,
                )
                row["selection_block_floor_pct"] = round(min(block_values), 6)
                row["stress_selection_block_floor_pct"] = round(
                    min(stress_block_values), 6
                )
                row["sample_gate_passed"] = bool(
                    row["train_trade_days"] >= 60
                    and row["validation_trade_days"] >= 30
                )
                row["selection_gate_passed"] = bool(
                    row["sample_gate_passed"]
                    and row["stress_selection_block_floor_pct"] > 0.0
                    and row["stress_train_avg_daily_return_pct"] > 0.0
                    and row["stress_validation_avg_daily_return_pct"] > 0.0
                )
                row["evidence_accepted"] = False
                row["paper_promotion_allowed"] = False
                row["real_orders_allowed"] = False
                rows.append(row)
    summary = pd.DataFrame(rows)
    summary.sort_values(
        [
            "selection_gate_passed",
            "sample_gate_passed",
            "stress_selection_block_floor_pct",
            "stress_validation_avg_daily_return_pct",
        ],
        ascending=False,
        inplace=True,
    )
    return summary.reset_index(drop=True), artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--runner-events", required=True)
    parser.add_argument("--calendar-file", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--return-column", default="return__h60_target_1.5_stop_1")
    parser.add_argument("--exit-time-column", default="exit_time__h60_target_1.5_stop_1")
    parser.add_argument("--runner-threshold", type=float, default=10.0)
    parser.add_argument("--capital", type=float, default=400_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    args = parser.parse_args()
    frame = load_labeled_candidates(
        args.candidates,
        args.runner_events,
        args.return_column,
        args.exit_time_column,
        args.runner_threshold,
    )
    calendar = load_calendar_dates(args.calendar_file)
    scored = score_walk_forward(frame, calendar)
    if scored.empty:
        raise RuntimeError("eventual-runner classifier produced no scored rows")
    summary, artifacts = evaluate_registry(
        scored,
        calendar,
        args.return_column,
        args.exit_time_column,
        args.capital,
        args.daily_goal,
    )
    selected = summary.iloc[0].to_dict()
    key = (
        float(selected["threshold"]),
        int(selected["top_per_signal"]),
        int(selected["slots"]),
    )
    daily, trades = artifacts[key]
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    daily.to_csv(prefix.with_name(prefix.name + "_selected_daily.csv"), index=False)
    trades.to_csv(prefix.with_name(prefix.name + "_selected_trades.csv"), index=False)
    final_goal_contract_passed = bool(
        selected["selection_gate_passed"]
        and selected["stress_final_trade_days"] >= 30
        and selected["stress_final_mean_lower_95_pnl"] >= args.daily_goal
        and selected["stress_final_goal_hit_pct"] >= 50.0
        and selected["stress_final_worst_day_pct"] > -1.5
    )
    status = {
        "strategy": "walk_forward_eventual_10pct_runner_classifier",
        "selection_uses_final_period": False,
        "label_available_only_after_session": True,
        "capital_reuse": "exact_exit_bar_unleveraged",
        "selected": selected,
        "final_goal_contract_passed": final_goal_contract_passed,
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "universe_caveat": "current_cached_universe_survivorship_bias",
    }
    prefix.with_name(prefix.name + "_status.json").write_text(
        json.dumps(status, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

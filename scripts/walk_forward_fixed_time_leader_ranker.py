#!/usr/bin/env python3
"""Walk-forward expected-return ranker for fixed-time leader candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.select_causal_intraday_exit_grid import load_calendar_dates
from scripts.research_rank_acceleration_registry import period_metrics
from scripts.backtest_fixed_time_leader_rank import BAR_PATH_FEATURES


DEFAULT_INPUT = "results/Data/fixed_time_leader_rank_v1_candidates.csv"
DEFAULT_CALENDAR = (
    "results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29/NIFTY 50.csv"
)
DEFAULT_OUTPUT = "results/Data/fixed_time_leader_ranker_v1"
DEFAULT_CONTRACT = "h60_target_1.5_stop_0.7"

SNAPSHOT_MODEL_FEATURES = [
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
MODEL_FEATURES = [*SNAPSHOT_MODEL_FEATURES, *BAR_PATH_FEATURES]


def _model() -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            (
                "model",
                HistGradientBoostingRegressor(
                    loss="squared_error",
                    max_iter=100,
                    learning_rate=0.04,
                    max_leaf_nodes=12,
                    min_samples_leaf=40,
                    l2_regularization=2.0,
                    random_state=42,
                ),
            ),
        ]
    )


def load_candidates(
    path: str,
    signal_minute: int,
    contract: str,
    features: list[str] | tuple[str, ...] = SNAPSHOT_MODEL_FEATURES,
) -> pd.DataFrame:
    frame = pd.read_csv(path)
    return_column = f"return__{contract}"
    required = {
        "date",
        "symbol",
        "signal_minute",
        "signal_time",
        "signal_turnover",
        return_column,
        *[feature for feature in features if feature != "log_signal_turnover"],
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
    frame = frame[frame["signal_minute"].eq(int(signal_minute))].copy()
    frame["date"] = frame["date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["signal_time"] = pd.to_datetime(frame["signal_time"], errors="coerce")
    frame["log_signal_turnover"] = np.log1p(
        pd.to_numeric(frame["signal_turnover"], errors="coerce").clip(lower=0.0)
    )
    frame["trade_return_pct"] = pd.to_numeric(frame[return_column], errors="coerce")
    for column in features:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(
        subset=["date", "symbol", "signal_time", "trade_return_pct"]
    ).reset_index(drop=True)


def score_walk_forward(
    frame: pd.DataFrame,
    calendar_dates: list[str],
    lookback_days: int = 160,
    min_train_days: int = 80,
    min_train_rows: int = 500,
    retrain_every_days: int = 5,
    progress_every: int = 25,
    features: list[str] | tuple[str, ...] = SNAPSHOT_MODEL_FEATURES,
) -> pd.DataFrame:
    groups = {date: group.copy() for date, group in frame.groupby("date", sort=False)}
    date_position = {date: index for index, date in enumerate(calendar_dates)}
    scored = []
    model = None
    trained_index = -1
    for index, date in enumerate(calendar_dates):
        if index < int(min_train_days):
            continue
        day = groups.get(date)
        if day is None or day.empty:
            continue
        prior_dates = calendar_dates[max(0, index - int(lookback_days)) : index]
        retrain = model is None or index - trained_index >= int(retrain_every_days)
        if retrain:
            train_parts = [groups[prior] for prior in prior_dates if prior in groups]
            if not train_parts:
                continue
            train = pd.concat(train_parts, ignore_index=True)
            if len(train) < int(min_train_rows) or train["date"].nunique() < int(min_train_days) // 2:
                continue
            counts = train.groupby("date")["symbol"].transform("count").clip(lower=1)
            sample_weight = 1.0 / counts.to_numpy(dtype=float)
            model = _model()
            model.fit(
                train[list(features)],
                train["trade_return_pct"],
                model__sample_weight=sample_weight,
            )
            trained_index = index
        day = day.copy()
        day["expected_return_pct"] = model.predict(day[list(features)])
        day["model_trained_through"] = prior_dates[-1]
        day["calendar_index"] = date_position[date]
        scored.append(day)
        if progress_every and len(scored) % int(progress_every) == 0:
            print(
                f"scored_days={len(scored)} date={date} "
                f"trained_through={prior_dates[-1]}",
                flush=True,
            )
    return pd.concat(scored, ignore_index=True) if scored else pd.DataFrame()


def select_daily(
    scored: pd.DataFrame,
    calendar_dates: list[str],
    threshold: float,
    top_k: int,
    extra_cost_pct: float = 0.0,
) -> pd.DataFrame:
    eligible = scored[scored["expected_return_pct"] >= float(threshold)].copy()
    eligible.sort_values(
        ["date", "expected_return_pct", "symbol"],
        ascending=[True, False, True],
        inplace=True,
    )
    selected = eligible.groupby("date", sort=False).head(int(top_k))
    selected_groups = {
        date: group.copy() for date, group in selected.groupby("date", sort=False)
    }
    rows = []
    for date in calendar_dates:
        picks = selected_groups.get(date)
        if picks is None or picks.empty:
            rows.append(
                {
                    "date": date,
                    "trades": 0,
                    "daily_return_pct": 0.0,
                    "symbols": "",
                    "mean_expected_return_pct": 0.0,
                    "model_trained_through": "",
                }
            )
            continue
        rows.append(
            {
                "date": date,
                "trades": int(len(picks)),
                "daily_return_pct": round(
                    float(picks["trade_return_pct"].mean()) - float(extra_cost_pct),
                    6,
                ),
                "symbols": ",".join(picks["symbol"].astype(str).tolist()),
                "mean_expected_return_pct": round(
                    float(picks["expected_return_pct"].mean()), 6
                ),
                "model_trained_through": str(picks.iloc[0]["model_trained_through"]),
            }
        )
    return pd.DataFrame(rows)


def temporal_splits(scored_dates: list[str]) -> dict[str, list[str]]:
    if len(scored_dates) < 12:
        raise ValueError("not enough scored market sessions")
    train_end = len(scored_dates) // 2
    validation_end = train_end + (len(scored_dates) - train_end) // 2
    return {
        "train": scored_dates[:train_end],
        "validation": scored_dates[train_end:validation_end],
        "final": scored_dates[validation_end:],
    }


def _period_metrics(daily: pd.DataFrame, dates: list[str], prefix: str) -> dict[str, Any]:
    period = daily[daily["date"].isin(dates)]
    return {
        f"{prefix}_days": int(len(period)),
        f"{prefix}_trade_days": int((period["trades"] > 0).sum()),
        f"{prefix}_trades": int(period["trades"].sum()),
        f"{prefix}_avg_daily_return_pct": round(
            float(period["daily_return_pct"].mean()) if len(period) else 0.0,
            6,
        ),
        f"{prefix}_positive_day_pct": round(
            float((period["daily_return_pct"] > 0).mean() * 100.0) if len(period) else 0.0,
            4,
        ),
        f"{prefix}_worst_day_pct": round(
            float(period["daily_return_pct"].min()) if len(period) else 0.0,
            6,
        ),
        f"{prefix}_best_day_pct": round(
            float(period["daily_return_pct"].max()) if len(period) else 0.0,
            6,
        ),
    }


def summarize(
    scored: pd.DataFrame,
    scored_dates: list[str],
    thresholds: tuple[float, ...],
    top_ks: tuple[int, ...],
    stress_cost_pct: float,
) -> tuple[pd.DataFrame, dict[tuple[float, int], pd.DataFrame]]:
    splits = temporal_splits(scored_dates)
    prefinal_dates = [*splits["train"], *splits["validation"]]
    blocks = [list(values) for values in np.array_split(prefinal_dates, 4) if len(values)]
    rows = []
    daily_outputs: dict[tuple[float, int], pd.DataFrame] = {}
    for threshold in thresholds:
        for top_k in top_ks:
            daily = select_daily(scored, scored_dates, threshold, top_k)
            stress = select_daily(
                scored,
                scored_dates,
                threshold,
                top_k,
                extra_cost_pct=stress_cost_pct,
            )
            daily_outputs[(float(threshold), int(top_k))] = daily
            row: dict[str, Any] = {
                "threshold": float(threshold),
                "top_k": int(top_k),
            }
            for split_name, dates in splits.items():
                row.update(_period_metrics(daily, dates, split_name))
                row.update(_period_metrics(stress, dates, f"stress_{split_name}"))
            block_values = []
            for index, dates in enumerate(blocks, start=1):
                value = _period_metrics(daily, dates, f"block_{index}")[
                    f"block_{index}_avg_daily_return_pct"
                ]
                row[f"block_{index}_avg_daily_return_pct"] = value
                block_values.append(float(value))
            row["selection_block_floor_pct"] = round(min(block_values), 6)
            row["selection_floor_pct"] = round(
                min(
                    float(row["train_avg_daily_return_pct"]),
                    float(row["validation_avg_daily_return_pct"]),
                ),
                6,
            )
            row["sample_gate_passed"] = bool(
                row["train_trade_days"] >= 60
                and row["validation_trade_days"] >= 30
            )
            row["research_gate_passed"] = bool(
                row["sample_gate_passed"]
                and row["selection_block_floor_pct"] > 0.0
                and row["selection_floor_pct"] > 0.0
                and row["stress_validation_avg_daily_return_pct"] > 0.0
            )
            row["evidence_accepted"] = False
            row["paper_promotion_allowed"] = False
            row["real_orders_allowed"] = False
            rows.append(row)
    summary = pd.DataFrame(rows)
    summary.sort_values(
        [
            "research_gate_passed",
            "sample_gate_passed",
            "selection_block_floor_pct",
            "selection_floor_pct",
            "validation_avg_daily_return_pct",
            "threshold",
        ],
        ascending=[False, False, False, False, False, False],
        inplace=True,
    )
    return summary.reset_index(drop=True), daily_outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--calendar-file", default=DEFAULT_CALENDAR)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-time", default="10:30")
    parser.add_argument("--contract", default=DEFAULT_CONTRACT)
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--min-train-days", type=int, default=80)
    parser.add_argument("--min-train-rows", type=int, default=500)
    parser.add_argument("--retrain-every-days", type=int, default=5)
    parser.add_argument("--thresholds", default="-0.20,-0.10,0.0,0.05,0.10,0.20,0.30")
    parser.add_argument("--top-k", default="1,3")
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument(
        "--feature-set", choices=("snapshot", "bar_path"), default="snapshot"
    )
    args = parser.parse_args()

    hour, minute = [int(value) for value in args.signal_time.split(":", 1)]
    signal_minute = hour * 60 + minute
    model_features = (
        MODEL_FEATURES if args.feature_set == "bar_path" else SNAPSHOT_MODEL_FEATURES
    )
    candidates = load_candidates(
        args.input,
        signal_minute,
        args.contract,
        features=model_features,
    )
    calendar = load_calendar_dates(args.calendar_file)
    scored = score_walk_forward(
        candidates,
        calendar,
        lookback_days=args.lookback_days,
        min_train_days=args.min_train_days,
        min_train_rows=args.min_train_rows,
        retrain_every_days=args.retrain_every_days,
        progress_every=args.progress_every,
        features=model_features,
    )
    if scored.empty:
        raise RuntimeError("walk-forward fixed-time ranker produced no predictions")
    first_scored_index = int(scored["calendar_index"].min())
    scored_dates = calendar[first_scored_index:]
    thresholds = tuple(
        float(value.strip()) for value in args.thresholds.split(",") if value.strip()
    )
    top_ks = tuple(int(value.strip()) for value in args.top_k.split(",") if value.strip())
    summary, daily_outputs = summarize(
        scored,
        scored_dates,
        thresholds,
        top_ks,
        args.stress_cost_pct,
    )
    selected = summary.iloc[0].to_dict()
    selected_key = (float(selected["threshold"]), int(selected["top_k"]))
    selected_daily = daily_outputs[selected_key]
    selected_stress_daily = select_daily(
        scored,
        scored_dates,
        selected_key[0],
        selected_key[1],
        extra_cost_pct=args.stress_cost_pct,
    )
    final_dates = temporal_splits(scored_dates)["final"]
    stress_final = selected_stress_daily[
        selected_stress_daily["date"].isin(final_dates)
    ].copy()
    stress_final_metrics = period_metrics(stress_final, "stress_final", args.capital)
    stress_final_pnl = stress_final["daily_return_pct"] * float(args.capital) / 100.0
    stress_final_goal_hit_pct = round(
        float((stress_final_pnl >= float(args.daily_goal)).mean() * 100.0)
        if len(stress_final_pnl)
        else 0.0,
        2,
    )
    final_goal_contract_passed = bool(
        selected["research_gate_passed"]
        and stress_final_metrics["stress_final_trade_days"] >= 30
        and stress_final_metrics["stress_final_mean_lower_95_pnl"] >= args.daily_goal
        and stress_final_goal_hit_pct >= 50.0
        and stress_final_metrics["stress_final_worst_day_pct"] > -1.5
    )
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    scored.to_csv(prefix.with_name(prefix.name + "_scored_candidates.csv"), index=False)
    selected_daily.to_csv(
        prefix.with_name(prefix.name + "_selected_daily.csv"), index=False
    )
    selected_stress_daily.to_csv(
        prefix.with_name(prefix.name + "_selected_stress_daily.csv"), index=False
    )
    metadata = {
        "model": "hist_gradient_boosting_expected_return_equal_day_weighted",
        "features": list(model_features),
        "feature_set": args.feature_set,
        "signal_time": args.signal_time,
        "contract": args.contract,
        "candidate_availability": "fixed_completed_bar_cross_section_then_next_bar_open",
        "feature_availability": "completed_signal_bar_and_prior_sessions_only",
        "model_training": "prior_160_market_sessions_only_retrained_every_5_sessions",
        "selection_uses_final_period": False,
        "selected_threshold": selected_key[0],
        "selected_top_k": selected_key[1],
        "starting_capital": float(args.capital),
        "daily_goal": float(args.daily_goal),
        "stress_extra_cost_pct": float(args.stress_cost_pct),
        "historical_research_gate_passed": bool(selected["research_gate_passed"]),
        "stress_final_metrics": stress_final_metrics,
        "stress_final_goal_hit_pct": stress_final_goal_hit_pct,
        "final_goal_contract_passed": final_goal_contract_passed,
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "universe_caveat": "current_cached_universe_survivorship_bias",
        "selected_metrics": selected,
    }
    prefix.with_name(prefix.name + "_selected.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    preview = [
        "threshold",
        "top_k",
        "train_trades",
        "train_avg_daily_return_pct",
        "validation_trades",
        "validation_avg_daily_return_pct",
        "final_trades",
        "final_avg_daily_return_pct",
        "stress_validation_avg_daily_return_pct",
        "selection_block_floor_pct",
        "research_gate_passed",
    ]
    print(
        f"candidates={len(candidates)} scored={len(scored)} "
        f"days={len(scored_dates)} selected={selected_key}"
    )
    print(summary[preview].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

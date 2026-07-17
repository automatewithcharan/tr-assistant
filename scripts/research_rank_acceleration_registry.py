#!/usr/bin/env python3
"""Audit emerging cross-sectional leaders without final-period selection leakage."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from scripts.select_causal_intraday_exit_grid import (
    build_contracts,
    first_touch_grid,
    load_calendar_dates,
)


DEFAULT_INPUT = "results/Data/fixed_time_leader_rank_v1_candidates.csv"
DEFAULT_CALENDAR = (
    "results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29/NIFTY 50.csv"
)
DEFAULT_OUTPUT = "results/Data/rank_acceleration_registry_v1"


@dataclass(frozen=True)
class RankAccelerationRule:
    name: str
    min_rank_delta: float
    min_current_rank: float
    max_move_pct: float


RULES = (
    RankAccelerationRule("emerging_early", 0.15, 0.70, 1.50),
    RankAccelerationRule("emerging_confirmed", 0.20, 0.75, 3.00),
    RankAccelerationRule("emerging_fast", 0.30, 0.70, 3.00),
)


def load_candidates(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "date",
        "symbol",
        "signal_minute",
        "move_from_open_pct",
        "day_change_pct",
        "move_rank",
        "volume_rank",
        "acceleration_rank",
        "relative_strength_rank",
        "range_rank",
        "turnover_rank",
        "pullback_quality_rank",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"candidate input missing columns: {', '.join(sorted(missing))}")
    frame["date"] = frame["date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    numeric = required - {"date", "symbol"}
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["date", "symbol", "signal_minute", "move_rank"]).copy()


def add_rank_acceleration(frame: pd.DataFrame) -> pd.DataFrame:
    """Add only prior-scan deltas; no outcome or later bar participates."""
    ordered = frame.sort_values(["date", "symbol", "signal_minute"]).copy()
    grouped = ordered.groupby(["date", "symbol"], sort=False)
    ordered["previous_signal_minute"] = grouped["signal_minute"].shift(1)
    ordered["previous_move_rank"] = grouped["move_rank"].shift(1)
    ordered["previous_move_pct"] = grouped["move_from_open_pct"].shift(1)
    ordered["rank_delta"] = ordered["move_rank"] - ordered["previous_move_rank"]
    ordered["move_delta_pct"] = (
        ordered["move_from_open_pct"] - ordered["previous_move_pct"]
    )
    ordered["scan_gap_minutes"] = (
        ordered["signal_minute"] - ordered["previous_signal_minute"]
    )
    return ordered


def rule_pool(frame: pd.DataFrame, rule: RankAccelerationRule) -> pd.DataFrame:
    mask = (
        frame["scan_gap_minutes"].between(1.0, 30.0)
        & frame["signal_minute"].between(9 * 60 + 30, 10 * 60 + 30)
        & frame["move_from_open_pct"].between(0.5, rule.max_move_pct)
        & frame["day_change_pct"].between(0.5, 4.0)
        & (frame["rank_delta"] >= rule.min_rank_delta)
        & (frame["move_rank"] >= rule.min_current_rank)
        & (frame["move_delta_pct"] > 0.0)
        & (frame["acceleration_rank"] >= 0.60)
        & (frame["relative_strength_rank"] >= 0.60)
    )
    pool = frame[mask].copy()
    if pool.empty:
        return pool
    pool["emergence_score"] = 100.0 * (
        0.40 * pool["rank_delta"].clip(lower=0.0, upper=1.0)
        + 0.18 * pool["acceleration_rank"]
        + 0.16 * pool["relative_strength_rank"]
        + 0.10 * pool["volume_rank"]
        + 0.06 * pool["range_rank"]
        + 0.05 * pool["turnover_rank"]
        + 0.05 * pool["pullback_quality_rank"]
    )
    earliest = pool.groupby("date")["signal_minute"].transform("min")
    pool = pool[pool["signal_minute"].eq(earliest)].copy()
    pool.sort_values(
        ["date", "emergence_score", "symbol"],
        ascending=[True, False, True],
        inplace=True,
    )
    pool["selection_rank"] = pool.groupby("date", sort=False).cumcount() + 1
    pool["rule"] = rule.name
    return pool


def temporal_splits(calendar_dates: list[str], warmup_days: int) -> dict[str, list[str]]:
    scored = sorted(set(calendar_dates))[max(0, int(warmup_days)) :]
    if len(scored) < 12:
        raise ValueError("not enough scored sessions")
    train_end = len(scored) // 2
    validation_end = train_end + (len(scored) - train_end) // 2
    return {
        "train": scored[:train_end],
        "validation": scored[train_end:validation_end],
        "final": scored[validation_end:],
    }


def daily_returns(
    pool: pd.DataFrame,
    dates: list[str],
    return_column: str,
    top_k: int,
    extra_cost_pct: float = 0.0,
) -> pd.DataFrame:
    selected = pool[pool["selection_rank"] <= int(top_k)].copy()
    selected[return_column] = pd.to_numeric(selected[return_column], errors="coerce")
    selected = selected.dropna(subset=[return_column])
    observed = selected.groupby("date")[return_column].mean()
    rows = []
    for date in dates:
        trades = int((selected["date"] == date).sum())
        value = float(observed.get(date, 0.0))
        if trades and extra_cost_pct:
            value -= float(extra_cost_pct)
        rows.append({"date": date, "trades": trades, "daily_return_pct": value})
    return pd.DataFrame(rows)


def period_metrics(daily: pd.DataFrame, prefix: str, capital: float) -> dict[str, Any]:
    values = pd.to_numeric(daily["daily_return_pct"], errors="coerce").fillna(0.0)
    return {
        f"{prefix}_days": int(len(daily)),
        f"{prefix}_trade_days": int((daily["trades"] > 0).sum()),
        f"{prefix}_trades": int(daily["trades"].sum()),
        f"{prefix}_avg_daily_return_pct": round(float(values.mean()), 6),
        f"{prefix}_avg_daily_pnl": round(float(values.mean()) * capital / 100.0, 2),
        f"{prefix}_positive_day_pct": round(float((values > 0.0).mean() * 100.0), 2),
        f"{prefix}_goal_hit_pct": round(float((values >= 2.5).mean() * 100.0), 2),
        f"{prefix}_worst_day_pct": round(float(values.min()), 6),
    }


def evaluate_registry(
    frame: pd.DataFrame,
    calendar_dates: list[str],
    warmup_days: int = 76,
    capital: float = 400_000.0,
    stress_cost_pct: float = 0.10,
    return_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    accelerated = add_rank_acceleration(frame)
    splits = temporal_splits(calendar_dates, warmup_days)
    prefinal = [*splits["train"], *splits["validation"]]
    blocks = [list(values) for values in np.array_split(prefinal, 4) if len(values)]
    return_columns = return_columns or sorted(
        column for column in accelerated.columns if column.startswith("return__")
    )
    rows = []
    pools: dict[str, pd.DataFrame] = {}
    for rule in RULES:
        pool = rule_pool(accelerated, rule)
        pools[rule.name] = pool
        for return_column in return_columns:
            contract = return_column.removeprefix("return__")
            for top_k in (1, 2):
                row: dict[str, Any] = {
                    "rule": rule.name,
                    "contract": contract,
                    "top_k": top_k,
                }
                for name, dates in splits.items():
                    daily = daily_returns(pool, dates, return_column, top_k)
                    row.update(period_metrics(daily, name, capital))
                    stress = daily_returns(
                        pool, dates, return_column, top_k, extra_cost_pct=stress_cost_pct
                    )
                    row.update(period_metrics(stress, f"stress_{name}", capital))
                block_values = []
                for index, dates in enumerate(blocks, start=1):
                    block = daily_returns(pool, dates, return_column, top_k)
                    value = float(block["daily_return_pct"].mean())
                    row[f"block_{index}_avg_daily_return_pct"] = round(value, 6)
                    block_values.append(value)
                row["selection_block_floor_pct"] = round(min(block_values), 6)
                row["selection_floor_pct"] = round(
                    min(
                        row["train_avg_daily_return_pct"],
                        row["validation_avg_daily_return_pct"],
                    ),
                    6,
                )
                row["sample_gate_passed"] = bool(
                    row["train_trade_days"] >= 60
                    and row["validation_trade_days"] >= 30
                )
                row["selection_gate_passed"] = bool(
                    row["sample_gate_passed"]
                    and row["selection_block_floor_pct"] > 0.0
                    and row["selection_floor_pct"] > 0.0
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
            "selection_block_floor_pct",
            "selection_floor_pct",
            "stress_validation_avg_daily_return_pct",
        ],
        ascending=False,
        inplace=True,
    )
    return summary.reset_index(drop=True), pools


def replay_short_returns(
    frame: pd.DataFrame,
    data_dir: str,
    horizon_minutes: int,
    targets: list[float],
    stops: list[float],
    slippage_bps: float = 5.0,
    cost_pct: float = 0.15,
) -> tuple[pd.DataFrame, list[str], dict[str, int]]:
    """Replay exact short paths only for candidates used by any causal rule."""
    accelerated = add_rank_acceleration(frame)
    used_indices: set[int] = set()
    for rule in RULES:
        used_indices.update(int(index) for index in rule_pool(accelerated, rule).index)
    contracts = build_contracts(targets, stops, side="short")
    return_columns = [
        f"return__h{int(horizon_minutes)}_{contract.key}" for contract in contracts
    ]
    output = frame.copy()
    for column in return_columns:
        output[column] = np.nan
    stats = {
        "candidate_rows": len(used_indices),
        "matched_rows": 0,
        "missing_rows": 0,
        "symbol_files": 0,
    }
    if not used_indices:
        return output, return_columns, stats
    candidates = output.loc[sorted(used_indices)].copy()
    root = Path(data_dir)
    for symbol, symbol_rows in candidates.groupby("symbol", sort=True):
        path = root / f"{symbol}.csv"
        if not path.exists():
            stats["missing_rows"] += len(symbol_rows)
            continue
        try:
            bars = pd.read_csv(path, usecols=["datetime", "high", "low", "close"])
        except Exception:
            stats["missing_rows"] += len(symbol_rows)
            continue
        bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
        for column in ("high", "low", "close"):
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
        bars = bars.dropna().sort_values("datetime")
        if bars.empty:
            stats["missing_rows"] += len(symbol_rows)
            continue
        stats["symbol_files"] += 1
        bars["date"] = bars["datetime"].dt.date.astype(str)
        days = {date: day.reset_index(drop=True) for date, day in bars.groupby("date")}
        for index, row in symbol_rows.iterrows():
            day = days.get(str(row["date"]))
            entry_time = pd.Timestamp(row["entry_time"])
            if day is None or pd.isna(entry_time):
                stats["missing_rows"] += 1
                continue
            future = day[
                (day["datetime"] >= entry_time)
                & (day["datetime"] < entry_time + pd.Timedelta(minutes=int(horizon_minutes)))
            ]
            if future.empty or pd.Timestamp(future.iloc[0]["datetime"]) != entry_time:
                stats["missing_rows"] += 1
                continue
            raw_entry = float(row["raw_entry_price"])
            entry = raw_entry * (1.0 - float(slippage_bps) / 10_000.0)
            gross, _ = first_touch_grid(
                future["high"].to_numpy(dtype=float),
                future["low"].to_numpy(dtype=float),
                float(future.iloc[-1]["close"]),
                entry,
                contracts,
            )
            for contract_index, column in enumerate(return_columns):
                output.at[index, column] = float(gross[contract_index]) - float(cost_pct)
            stats["matched_rows"] += 1
    return output, return_columns, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--calendar-file", default=DEFAULT_CALENDAR)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT)
    parser.add_argument("--warmup-days", type=int, default=76)
    parser.add_argument("--capital", type=float, default=400_000.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    parser.add_argument("--side", choices=["long", "short"], default="long")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--horizon-minutes", type=int, default=60)
    parser.add_argument("--target-pcts", default="0.5,0.7,1.0,1.5")
    parser.add_argument("--stop-pcts", default="0.5,0.7,1.0,1.5")
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--cost-pct", type=float, default=0.15)
    args = parser.parse_args()

    frame = load_candidates(args.input)
    calendar = load_calendar_dates(args.calendar_file)
    replay_stats = None
    return_columns = None
    if args.side == "short":
        if not args.data_dir:
            raise ValueError("--data-dir is required for exact short replay")
        targets = [float(value) for value in args.target_pcts.split(",") if value.strip()]
        stops = [float(value) for value in args.stop_pcts.split(",") if value.strip()]
        frame, return_columns, replay_stats = replay_short_returns(
            frame,
            args.data_dir,
            args.horizon_minutes,
            targets,
            stops,
            slippage_bps=args.slippage_bps,
            cost_pct=args.cost_pct,
        )
    summary, pools = evaluate_registry(
        frame,
        calendar,
        warmup_days=args.warmup_days,
        capital=args.capital,
        stress_cost_pct=args.stress_cost_pct,
        return_columns=return_columns,
    )
    if summary.empty:
        raise RuntimeError("rank-acceleration registry produced no configurations")
    selected = summary.iloc[0].to_dict()
    selected_rule = str(selected["rule"])
    selected_contract = str(selected["contract"])
    selected_top_k = int(selected["top_k"])
    splits = temporal_splits(calendar, args.warmup_days)
    final_daily = daily_returns(
        pools[selected_rule],
        splits["final"],
        f"return__{selected_contract}",
        selected_top_k,
    )

    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    final_daily.to_csv(prefix.with_name(prefix.name + "_selected_final_daily.csv"), index=False)
    pools[selected_rule].to_csv(
        prefix.with_name(prefix.name + "_selected_candidates.csv"), index=False
    )
    status = {
        "evidence": "cross_sectional_rank_acceleration_registry",
        "side": args.side,
        "rules": [asdict(rule) for rule in RULES],
        "selection_uses_final_period": False,
        "short_replay_stats": replay_stats,
        "selected_rule": selected_rule,
        "selected_contract": selected_contract,
        "selected_top_k": selected_top_k,
        "selection_gate_passed": bool(selected["selection_gate_passed"]),
        "selected_metrics": selected,
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

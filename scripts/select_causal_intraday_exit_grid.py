#!/usr/bin/env python3
"""Select an intraday event exit contract without using the final audit block.

The event ranker can only learn a useful target when the simulated trade
contract itself has reasonable expectancy.  This evaluator replays the same
causal event candidates through a target/stop grid, selects the contract from
train and validation periods, and writes outcome rows for a later walk-forward
ranker.  The final period is reported only after selection.

Five-minute candles cannot reveal which side traded first inside one candle.
When target and stop are both touched in that candle, the stop wins.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_INPUT = "results/Data/intraday_event_candidates_n500_causal_cross_sectional_trades.csv"
DEFAULT_DATA_DIR = "results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29"
DEFAULT_OUTPUT_PREFIX = "results/Data/causal_intraday_exit_grid_v1"

EXIT_MISSING = np.uint8(0)
EXIT_TARGET = np.uint8(1)
EXIT_STOP = np.uint8(2)
EXIT_EOD = np.uint8(3)


@dataclass(frozen=True)
class ExitContract:
    target_pct: float
    stop_pct: float
    side: str = "long"

    @property
    def key(self) -> str:
        return f"{self.side}_target_{self.target_pct:g}_stop_{self.stop_pct:g}"


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in str(value or "").split(",") if item.strip()]


def build_contracts(targets: list[float], stops: list[float], side: str = "long") -> list[ExitContract]:
    if side not in {"long", "short"}:
        raise ValueError(f"unsupported side: {side}")
    return [ExitContract(float(target), float(stop), side) for target, stop in product(targets, stops)]


def first_touch_grid(
    highs: np.ndarray,
    lows: np.ndarray,
    eod_close: float,
    entry_price: float,
    contracts: list[ExitContract],
) -> tuple[np.ndarray, np.ndarray]:
    """Return gross returns and compact exit codes for one candidate.

    Candle ordering is conservative: a stop wins if target and stop first
    appear at the same bar index.
    """
    count = len(contracts)
    returns = np.full(count, np.nan, dtype=np.float32)
    codes = np.full(count, EXIT_MISSING, dtype=np.uint8)
    if entry_price <= 0 or len(highs) == 0 or len(lows) == 0:
        return returns, codes

    target_first: dict[float, int] = {}
    stop_first: dict[float, int] = {}
    missing_index = len(highs) + 1
    side = contracts[0].side if contracts else "long"
    if any(contract.side != side for contract in contracts):
        raise ValueError("all contracts in one grid must use the same side")
    for target in sorted({contract.target_pct for contract in contracts}):
        if side == "long":
            hits = np.flatnonzero(highs >= entry_price * (1.0 + target / 100.0))
        else:
            hits = np.flatnonzero(lows <= entry_price * (1.0 - target / 100.0))
        target_first[target] = int(hits[0]) if len(hits) else missing_index
    for stop in sorted({contract.stop_pct for contract in contracts}):
        if side == "long":
            hits = np.flatnonzero(lows <= entry_price * (1.0 - stop / 100.0))
        else:
            hits = np.flatnonzero(highs >= entry_price * (1.0 + stop / 100.0))
        stop_first[stop] = int(hits[0]) if len(hits) else missing_index

    direction = 1.0 if side == "long" else -1.0
    eod_return = direction * (float(eod_close) - entry_price) / entry_price * 100.0
    for index, contract in enumerate(contracts):
        target_index = target_first[contract.target_pct]
        stop_index = stop_first[contract.stop_pct]
        if stop_index <= target_index and stop_index < missing_index:
            returns[index] = -abs(contract.stop_pct)
            codes[index] = EXIT_STOP
        elif target_index < stop_index and target_index < missing_index:
            returns[index] = contract.target_pct
            codes[index] = EXIT_TARGET
        else:
            returns[index] = eod_return
            codes[index] = EXIT_EOD
    return returns, codes


def _load_candidates(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"date", "symbol", "signal_time", "entry_time", "entry_price", "score"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
    frame = frame.copy()
    frame["date"] = frame["date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["signal_time"] = pd.to_datetime(frame["signal_time"], errors="coerce")
    frame["entry_time"] = pd.to_datetime(frame["entry_time"], errors="coerce")
    frame["entry_price"] = pd.to_numeric(frame["entry_price"], errors="coerce")
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce").fillna(0.0)
    return frame.dropna(subset=["signal_time", "entry_time", "entry_price"]).reset_index(drop=True)


def replay_exit_grid(
    candidates: pd.DataFrame,
    data_dir: str,
    contracts: list[ExitContract],
    progress_every: int = 50,
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """Replay every candidate while loading each symbol candle file once."""
    returns = np.full((len(candidates), len(contracts)), np.nan, dtype=np.float32)
    codes = np.full((len(candidates), len(contracts)), EXIT_MISSING, dtype=np.uint8)
    stats = {"candidate_rows": int(len(candidates)), "matched_rows": 0, "missing_rows": 0, "symbol_files": 0}
    root = Path(data_dir)
    grouped = list(candidates.groupby("symbol", sort=True))

    for symbol_index, (symbol, symbol_candidates) in enumerate(grouped, start=1):
        if progress_every and (symbol_index == 1 or symbol_index % int(progress_every) == 0):
            print(
                f"[{symbol_index}/{len(grouped)}] {symbol} matched={stats['matched_rows']} missing={stats['missing_rows']}",
                flush=True,
            )
        path = root / f"{symbol}.csv"
        if not path.exists():
            stats["missing_rows"] += int(len(symbol_candidates))
            continue
        try:
            bars = pd.read_csv(path, usecols=["datetime", "high", "low", "close"])
        except Exception:
            stats["missing_rows"] += int(len(symbol_candidates))
            continue
        bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
        for column in ["high", "low", "close"]:
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
        bars = bars.dropna().sort_values("datetime").copy()
        if bars.empty:
            stats["missing_rows"] += int(len(symbol_candidates))
            continue
        stats["symbol_files"] += 1
        bars["date"] = bars["datetime"].dt.date.astype(str)
        day_map = {
            str(date): day.reset_index(drop=True)
            for date, day in bars.groupby("date", sort=False)
        }

        for row in symbol_candidates.itertuples():
            day = day_map.get(str(row.date))
            if day is None or day.empty:
                stats["missing_rows"] += 1
                continue
            timestamps = day["datetime"].to_numpy(dtype="datetime64[ns]")
            entry_time = np.datetime64(pd.Timestamp(row.entry_time).to_datetime64())
            entry_index = int(np.searchsorted(timestamps, entry_time, side="left"))
            if entry_index >= len(day):
                stats["missing_rows"] += 1
                continue
            gross, exit_codes = first_touch_grid(
                day["high"].to_numpy(dtype=float)[entry_index:],
                day["low"].to_numpy(dtype=float)[entry_index:],
                float(day.iloc[-1]["close"]),
                float(row.entry_price),
                contracts,
            )
            returns[int(row.Index), :] = gross
            codes[int(row.Index), :] = exit_codes
            stats["matched_rows"] += 1
    return returns, codes, stats


def load_calendar_dates(path: str) -> list[str]:
    """Load a complete market-session calendar from a candle or date CSV."""
    if not path:
        return []
    frame = pd.read_csv(path)
    if "datetime" in frame.columns:
        values = pd.to_datetime(frame["datetime"], errors="coerce")
    elif "date" in frame.columns:
        values = pd.to_datetime(frame["date"], errors="coerce")
    else:
        raise ValueError(f"{path} missing datetime/date calendar column")
    return sorted(values.dropna().dt.date.astype(str).unique().tolist())


def split_scored_dates(
    candidates: pd.DataFrame,
    warmup_days: int,
    calendar_dates: list[str] | None = None,
) -> tuple[list[str], dict[str, list[str]]]:
    candidate_dates = sorted(candidates["date"].unique().tolist())
    all_dates = sorted(set(calendar_dates or candidate_dates))
    if candidate_dates:
        all_dates = [
            date for date in all_dates
            if candidate_dates[0] <= date <= candidate_dates[-1]
        ]
    scored_dates = all_dates[max(0, int(warmup_days)) :]
    if len(scored_dates) < 6:
        raise ValueError("not enough scored dates for temporal train/validation/final splits")
    train_end = len(scored_dates) // 2
    validation_end = train_end + (len(scored_dates) - train_end) // 2
    # Match the existing 413-day evidence split exactly: 206 / 103 / 104.
    if len(scored_dates) == 413:
        train_end = 206
        validation_end = 309
    splits = {
        "train": scored_dates[:train_end],
        "validation": scored_dates[train_end:validation_end],
        "final": scored_dates[validation_end:],
    }
    return scored_dates, splits


def _top_candidate_indices(
    candidates: pd.DataFrame,
    dates: list[str],
    top_per_day: int,
    selection_mode: str = "first_batch",
) -> np.ndarray:
    eligible = candidates[candidates["date"].isin(dates)].copy()
    if selection_mode == "first_batch":
        first_times = eligible.groupby("date")["signal_time"].transform("min")
        eligible = eligible[eligible["signal_time"].eq(first_times)].copy()
        eligible.sort_values(
            ["date", "score", "symbol"],
            ascending=[True, False, True],
            inplace=True,
        )
    elif selection_mode == "day_best_research":
        eligible.sort_values(
            ["date", "score", "signal_time", "symbol"],
            ascending=[True, False, True, True],
            inplace=True,
        )
    else:
        raise ValueError(f"unsupported selection mode: {selection_mode}")
    eligible = eligible.drop_duplicates(["date", "symbol"], keep="first")
    eligible = eligible.groupby("date", group_keys=False).head(max(1, int(top_per_day)))
    return eligible.index.to_numpy(dtype=int)


def _split_metrics(
    candidates: pd.DataFrame,
    net_returns: np.ndarray,
    dates: list[str],
    selected_indices: np.ndarray,
) -> dict[str, float | int]:
    date_index = {date: idx for idx, date in enumerate(dates)}
    daily_values: list[list[float]] = [[] for _ in dates]
    for candidate_index in selected_indices:
        row = candidates.iloc[int(candidate_index)]
        day_index = date_index.get(str(row["date"]))
        value = float(net_returns[int(candidate_index)])
        if day_index is not None and np.isfinite(value):
            daily_values[day_index].append(value)
    daily = np.array([float(np.mean(values)) if values else 0.0 for values in daily_values], dtype=float)
    traded = np.array([bool(values) for values in daily_values])
    return {
        "days": int(len(dates)),
        "traded_days": int(traded.sum()),
        "avg_daily_return_pct": round(float(daily.mean()) if len(daily) else 0.0, 6),
        "positive_day_pct": round(float((daily > 0).mean() * 100.0) if len(daily) else 0.0, 4),
        "worst_day_pct": round(float(daily.min()) if len(daily) else 0.0, 6),
        "best_day_pct": round(float(daily.max()) if len(daily) else 0.0, 6),
    }


def summarize_contracts(
    candidates: pd.DataFrame,
    gross_returns: np.ndarray,
    contracts: list[ExitContract],
    splits: dict[str, list[str]],
    cost_pct: float,
    top_per_day: int,
    selection_mode: str = "first_batch",
) -> pd.DataFrame:
    selected_by_split = {
        name: _top_candidate_indices(candidates, dates, top_per_day, selection_mode)
        for name, dates in splits.items()
    }
    pre_final_dates = [*splits["train"], *splits["validation"]]
    pre_final_blocks = [list(values) for values in np.array_split(pre_final_dates, 4) if len(values)]
    selected_by_block = [
        _top_candidate_indices(candidates, dates, top_per_day, selection_mode)
        for dates in pre_final_blocks
    ]
    rows = []
    for contract_index, contract in enumerate(contracts):
        net = gross_returns[:, contract_index].astype(float) - float(cost_pct)
        row: dict[str, float | int | str | bool] = {
            "contract": contract.key,
            "target_pct": contract.target_pct,
            "stop_pct": contract.stop_pct,
            "side": contract.side,
            "cost_pct": float(cost_pct),
            "top_per_day": int(top_per_day),
            "selection_mode": selection_mode,
        }
        for split_name, dates in splits.items():
            metrics = _split_metrics(candidates, net, dates, selected_by_split[split_name])
            for metric, value in metrics.items():
                row[f"{split_name}_{metric}"] = value
        block_returns = []
        for block_index, (block_dates, block_indices) in enumerate(
            zip(pre_final_blocks, selected_by_block),
            start=1,
        ):
            block_metrics = _split_metrics(candidates, net, block_dates, block_indices)
            block_return = float(block_metrics["avg_daily_return_pct"])
            row[f"selection_block_{block_index}_avg_daily_return_pct"] = block_return
            block_returns.append(block_return)
        train_avg = float(row["train_avg_daily_return_pct"])
        validation_avg = float(row["validation_avg_daily_return_pct"])
        row["selection_floor_pct"] = round(min(train_avg, validation_avg), 6)
        row["selection_mean_pct"] = round((train_avg + validation_avg) / 2.0, 6)
        row["selection_block_floor_pct"] = round(min(block_returns), 6)
        row["selection_block_mean_pct"] = round(float(np.mean(block_returns)), 6)
        rows.append(row)
    summary = pd.DataFrame(rows)
    summary.sort_values(
        [
            "selection_block_floor_pct",
            "selection_block_mean_pct",
            "selection_floor_pct",
            "validation_avg_daily_return_pct",
            "target_pct",
            "stop_pct",
        ],
        ascending=[False, False, False, False, True, True],
        inplace=True,
    )
    return summary.reset_index(drop=True)


def selected_outcome_rows(
    candidates: pd.DataFrame,
    gross_returns: np.ndarray,
    codes: np.ndarray,
    contracts: list[ExitContract],
    selected_contract: ExitContract,
    cost_pct: float,
) -> pd.DataFrame:
    contract_index = contracts.index(selected_contract)
    out = candidates.copy()
    gross = gross_returns[:, contract_index].astype(float)
    code = codes[:, contract_index]
    reason_map = {
        int(EXIT_MISSING): "missing_bars",
        int(EXIT_TARGET): "target",
        int(EXIT_STOP): "stop_first_or_stop",
        int(EXIT_EOD): "eod",
    }
    out["gross_return_pct"] = np.round(gross, 6)
    out["trade_return_pct"] = np.round(gross - float(cost_pct), 6)
    out["exit_reason"] = [reason_map[int(value)] for value in code]
    out["selected_target_pct"] = selected_contract.target_pct
    out["selected_stop_pct"] = selected_contract.stop_pct
    out["selected_side"] = selected_contract.side
    out["embedded_cost_pct"] = float(cost_pct)
    return out[np.isfinite(out["trade_return_pct"])].reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT_PREFIX)
    parser.add_argument("--target-pcts", default="0.5,0.7,1.0,1.5,2.0,3.0")
    parser.add_argument("--stop-pcts", default="0.4,0.5,0.7,1.0,1.5,2.5")
    parser.add_argument("--cost-pct", type=float, default=0.15)
    parser.add_argument("--side", choices=["long", "short"], default="long")
    parser.add_argument("--warmup-days", type=int, default=80)
    parser.add_argument("--top-per-day", type=int, default=1)
    parser.add_argument(
        "--selection-mode",
        choices=["first_batch", "day_best_research"],
        default="first_batch",
    )
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument(
        "--calendar-file",
        default="",
        help="Optional benchmark candle/date CSV; includes zero-trade market days in metrics.",
    )
    args = parser.parse_args()

    candidates = _load_candidates(args.input)
    contracts = build_contracts(parse_float_list(args.target_pcts), parse_float_list(args.stop_pcts), args.side)
    if not contracts:
        raise ValueError("target/stop grid is empty")
    gross_returns, codes, replay_stats = replay_exit_grid(
        candidates,
        args.data_dir,
        contracts,
        progress_every=args.progress_every,
    )
    calendar_dates = load_calendar_dates(args.calendar_file)
    scored_dates, splits = split_scored_dates(
        candidates,
        args.warmup_days,
        calendar_dates=calendar_dates,
    )
    summary = summarize_contracts(
        candidates,
        gross_returns,
        contracts,
        splits,
        cost_pct=args.cost_pct,
        top_per_day=args.top_per_day,
        selection_mode=args.selection_mode,
    )
    selected_row = summary.iloc[0].to_dict()
    selected_contract = ExitContract(
        target_pct=float(selected_row["target_pct"]),
        stop_pct=float(selected_row["stop_pct"]),
        side=str(selected_row["side"]),
    )
    outcomes = selected_outcome_rows(
        candidates,
        gross_returns,
        codes,
        contracts,
        selected_contract,
        cost_pct=args.cost_pct,
    )

    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = prefix.with_name(prefix.name + "_summary.csv")
    outcomes_path = prefix.with_name(prefix.name + "_selected_outcomes.csv")
    metadata_path = prefix.with_name(prefix.name + "_selected.json")
    summary.to_csv(summary_path, index=False)
    outcomes.to_csv(outcomes_path, index=False)
    metadata = {
        "selection_uses_final_period": False,
        "selected_target_pct": selected_contract.target_pct,
        "selected_stop_pct": selected_contract.stop_pct,
        "selected_side": selected_contract.side,
        "cost_pct": float(args.cost_pct),
        "warmup_days": int(args.warmup_days),
        "scored_days": len(scored_dates),
        "split_days": {name: len(dates) for name, dates in splits.items()},
        "calendar_file": str(args.calendar_file or ""),
        "calendar_sessions_included": bool(calendar_dates),
        "selection_mode": args.selection_mode,
        "train_avg_daily_return_pct": selected_row["train_avg_daily_return_pct"],
        "validation_avg_daily_return_pct": selected_row["validation_avg_daily_return_pct"],
        "final_avg_daily_return_pct_after_selection": selected_row["final_avg_daily_return_pct"],
        "selection_floor_pct": selected_row["selection_floor_pct"],
        "selection_block_floor_pct": selected_row["selection_block_floor_pct"],
        "selection_block_mean_pct": selected_row["selection_block_mean_pct"],
        "candidate_strategy_accepted": False,
        "real_orders_allowed": False,
        "reason": "exit contract selection is research input, not a validated entry strategy",
        "feature_availability": "signal_close_or_earlier",
        "leakage_audit_passed": args.selection_mode == "first_batch",
        "replay_stats": replay_stats,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(f"candidates={len(candidates)} contracts={len(contracts)} matched={replay_stats['matched_rows']}")
    print(f"selected={selected_contract.key} using train+validation only")
    print(summary.head(12).to_string(index=False))
    print(f"saved={summary_path}")
    print(f"saved={outcomes_path}")
    print(f"saved={metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

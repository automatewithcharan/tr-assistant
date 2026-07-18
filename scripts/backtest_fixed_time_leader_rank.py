#!/usr/bin/env python3
"""Backtest causal fixed-time cross-sectional intraday leader selection.

Each signal batch is formed from a completed five-minute bar. Per-symbol
volume surprise uses only prior sessions, cross-sectional ranks use only the
same completed signal batch, and simulated entry is the following bar open.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.MarketContext import get_sector
from scripts.select_causal_intraday_exit_grid import (
    EXIT_EOD,
    EXIT_STOP,
    EXIT_TARGET,
    ExitContract,
    first_touch_grid_with_indices,
    load_calendar_dates,
)


DEFAULT_DATA_DIR = "results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29"
DEFAULT_CALENDAR = f"{DEFAULT_DATA_DIR}/NIFTY 50.csv"
DEFAULT_OUTPUT = "results/Data/fixed_time_leader_rank_v1"

_WORKER_BENCHMARK: dict[pd.Timestamp, float] = {}
_WORKER_CONFIG = None
_WORKER_CONTRACTS = None


@dataclass(frozen=True)
class FixedLeaderConfig:
    signal_minutes: tuple[int, ...]
    horizons: tuple[int, ...]
    min_history_sessions: int = 10
    volume_baseline_sessions: int = 20
    min_price: float = 50.0
    min_signal_turnover: float = 30_000_000.0
    turnover_mode: str = "absolute"
    min_signal_turnover_floor: float = 5_000_000.0
    min_move_from_open_pct: float = 0.30
    max_move_from_open_pct: float = 3.50
    min_day_change_pct: float = 0.30
    max_day_change_pct: float = 5.00
    max_abs_gap_pct: float = 3.00
    min_cum_volume_ratio: float = 1.00
    min_volume_acceleration: float = 0.50
    min_relative_strength_pct: float = 0.20
    min_range_position: float = 0.65
    max_pullback_pct: float = 0.80
    min_vwap_distance_pct: float = 0.00
    max_vwap_distance_pct: float = 1.50
    entry_slippage_bps: float = 5.0
    additional_cost_pct: float = 0.15


def projected_daily_turnover(signal_turnover: float, signal_minute: int) -> float:
    """Project causal cumulative turnover over the 375-minute NSE session."""
    completed_minutes = max(5, int(signal_minute) - (9 * 60 + 15) + 5)
    return float(signal_turnover) * (375.0 / min(375, completed_minutes))


def turnover_gate_values(
    signal_turnover: float,
    signal_minute: int,
    config: FixedLeaderConfig,
) -> tuple[bool, float]:
    projected = projected_daily_turnover(signal_turnover, signal_minute)
    if config.turnover_mode == "absolute":
        return signal_turnover >= config.min_signal_turnover, projected
    if config.turnover_mode == "projected_daily":
        return (
            signal_turnover >= config.min_signal_turnover_floor
            and projected >= config.min_signal_turnover
        ), projected
    raise ValueError(f"unsupported turnover mode: {config.turnover_mode}")


BAR_PATH_FEATURES = [
    "return_1bar_pct",
    "return_2bar_pct",
    "return_6bar_pct",
    "realized_volatility_6bar_pct",
    "directional_efficiency_6bar",
    "signal_close_location",
    "signal_body_pct",
    "signal_upper_wick_pct",
    "green_bar_fraction_6",
    "volume_persistence_3bar",
    "recent_volume_share",
    "vwap_slope_3bar_pct",
    "breakout_prior_6bar_pct",
    "opening_range_breakout_pct",
]


def causal_bar_path_features(
    day: pd.DataFrame,
    signal_index: int,
    cumulative_volume: np.ndarray,
    vwap: np.ndarray,
) -> dict[str, float]:
    """Summarize the completed path through the signal bar only."""
    closes = day["close"].to_numpy(dtype=float)[: signal_index + 1]
    opens = day["open"].to_numpy(dtype=float)[: signal_index + 1]
    highs = day["high"].to_numpy(dtype=float)[: signal_index + 1]
    lows = day["low"].to_numpy(dtype=float)[: signal_index + 1]
    volumes = day["volume"].to_numpy(dtype=float)[: signal_index + 1]

    def trailing_return(bars: int) -> float:
        prior_index = len(closes) - 1 - bars
        if prior_index < 0 or closes[prior_index] <= 0:
            return 0.0
        return (closes[-1] / closes[prior_index] - 1.0) * 100.0

    returns = np.diff(closes[-7:]) / closes[-7:-1] * 100.0 if len(closes) >= 2 else np.array([])
    net_move = abs(float(closes[-1] - closes[max(0, len(closes) - 7)]))
    path_distance = float(np.abs(np.diff(closes[-7:])).sum()) if len(closes) >= 2 else 0.0
    candle_range = float(highs[-1] - lows[-1])
    close_location = (closes[-1] - lows[-1]) / candle_range if candle_range > 0 else 0.5
    body_pct = (closes[-1] / opens[-1] - 1.0) * 100.0 if opens[-1] > 0 else 0.0
    upper_wick_pct = (
        (highs[-1] - max(opens[-1], closes[-1])) / closes[-1] * 100.0
        if closes[-1] > 0
        else 0.0
    )
    recent_start = max(0, len(closes) - 6)
    green_fraction = float(np.mean(closes[recent_start:] > opens[recent_start:]))
    recent_three = volumes[max(0, len(volumes) - 3) :]
    preceding_three = volumes[max(0, len(volumes) - 6) : max(0, len(volumes) - 3)]
    prior_volume = float(preceding_three.mean()) if len(preceding_three) else 0.0
    volume_persistence = float(recent_three.mean()) / prior_volume if prior_volume > 0 else 0.0
    cumulative_at_signal = float(cumulative_volume[signal_index])
    recent_volume_share = float(recent_three.sum()) / cumulative_at_signal if cumulative_at_signal > 0 else 0.0
    vwap_prior_index = signal_index - 3
    vwap_slope = (
        (float(vwap[signal_index]) / float(vwap[vwap_prior_index]) - 1.0) * 100.0
        if vwap_prior_index >= 0 and float(vwap[vwap_prior_index]) > 0
        else 0.0
    )
    prior_highs = highs[max(0, len(highs) - 7) : -1]
    prior_high = float(prior_highs.max()) if len(prior_highs) else float(highs[-1])
    breakout = (closes[-1] / prior_high - 1.0) * 100.0 if prior_high > 0 else 0.0
    opening_range_high = float(highs[: min(3, len(highs))].max())
    opening_breakout = (
        (closes[-1] / opening_range_high - 1.0) * 100.0
        if opening_range_high > 0
        else 0.0
    )
    return {
        "return_1bar_pct": trailing_return(1),
        "return_2bar_pct": trailing_return(2),
        "return_6bar_pct": trailing_return(6),
        "realized_volatility_6bar_pct": float(np.std(returns)) if len(returns) else 0.0,
        "directional_efficiency_6bar": net_move / path_distance if path_distance > 0 else 0.0,
        "signal_close_location": float(close_location),
        "signal_body_pct": float(body_pct),
        "signal_upper_wick_pct": float(upper_wick_pct),
        "green_bar_fraction_6": green_fraction,
        "volume_persistence_3bar": float(volume_persistence),
        "recent_volume_share": float(recent_volume_share),
        "vwap_slope_3bar_pct": float(vwap_slope),
        "breakout_prior_6bar_pct": float(breakout),
        "opening_range_breakout_pct": float(opening_breakout),
    }


@dataclass(frozen=True)
class LeaderContract:
    horizon_minutes: int
    target_pct: float
    stop_pct: float

    @property
    def key(self) -> str:
        return (
            f"h{self.horizon_minutes}_target_{self.target_pct:g}"
            f"_stop_{self.stop_pct:g}"
        )

    @property
    def return_column(self) -> str:
        return f"return__{self.key}"

    @property
    def exit_column(self) -> str:
        return f"exit__{self.key}"

    @property
    def exit_bar_column(self) -> str:
        return f"exit_bar__{self.key}"

    @property
    def exit_time_column(self) -> str:
        return f"exit_time__{self.key}"


def parse_clock_list(value: str) -> tuple[int, ...]:
    output = []
    for item in str(value or "").split(","):
        text = item.strip()
        if not text:
            continue
        hour, minute = [int(part) for part in text.split(":", 1)]
        output.append(hour * 60 + minute)
    return tuple(sorted(set(output)))


def parse_int_list(value: str) -> tuple[int, ...]:
    return tuple(sorted({int(item.strip()) for item in str(value or "").split(",") if item.strip()}))


def parse_float_list(value: str) -> tuple[float, ...]:
    return tuple(sorted({float(item.strip()) for item in str(value or "").split(",") if item.strip()}))


def build_contracts(
    horizons: tuple[int, ...],
    targets: tuple[float, ...],
    stops: tuple[float, ...],
) -> list[LeaderContract]:
    return [
        LeaderContract(horizon, target, stop)
        for horizon in horizons
        for target in targets
        for stop in stops
    ]


def _load_benchmark(path: str) -> dict[pd.Timestamp, float]:
    frame = pd.read_csv(path, parse_dates=["datetime"]).sort_values("datetime")
    output: dict[pd.Timestamp, float] = {}
    frame["date"] = frame["datetime"].dt.date.astype(str)
    for _, day in frame.groupby("date", sort=True):
        day_open = float(day.iloc[0]["open"])
        if day_open <= 0:
            continue
        moves = (pd.to_numeric(day["close"], errors="coerce") / day_open - 1.0) * 100.0
        for timestamp, move in zip(day["datetime"], moves):
            if pd.notna(move):
                output[pd.Timestamp(timestamp)] = float(move)
    return output


def _exit_reason(code: int) -> str:
    if int(code) == int(EXIT_TARGET):
        return "target"
    if int(code) == int(EXIT_STOP):
        return "stop"
    if int(code) == int(EXIT_EOD):
        return "horizon"
    return "missing"


def _simulate_contracts(
    day: pd.DataFrame,
    entry_index: int,
    raw_entry_price: float,
    contracts: list[LeaderContract],
    config: FixedLeaderConfig,
) -> dict[str, Any] | None:
    adjusted_entry = raw_entry_price * (1.0 + config.entry_slippage_bps / 10_000.0)
    output: dict[str, Any] = {
        "raw_entry_price": round(raw_entry_price, 6),
        "entry_price_after_slippage": round(adjusted_entry, 6),
    }
    grouped: dict[int, list[LeaderContract]] = {}
    for contract in contracts:
        grouped.setdefault(contract.horizon_minutes, []).append(contract)
    for horizon, horizon_contracts in grouped.items():
        bars = max(1, int(horizon) // 5)
        final_index = entry_index + bars - 1
        if final_index >= len(day):
            return None
        grid = [
            ExitContract(contract.target_pct, contract.stop_pct, "long")
            for contract in horizon_contracts
        ]
        gross, codes, exit_indices = first_touch_grid_with_indices(
            day.loc[entry_index:final_index, "high"].to_numpy(dtype=float),
            day.loc[entry_index:final_index, "low"].to_numpy(dtype=float),
            float(day.loc[final_index, "close"]),
            adjusted_entry,
            grid,
        )
        for index, contract in enumerate(horizon_contracts):
            if not np.isfinite(gross[index]):
                return None
            output[contract.return_column] = round(
                float(gross[index]) - float(config.additional_cost_pct),
                6,
            )
            output[contract.exit_column] = _exit_reason(int(codes[index]))
            exit_index = int(exit_indices[index])
            output[contract.exit_bar_column] = exit_index
            output[contract.exit_time_column] = (
                str(pd.Timestamp(day.loc[entry_index + exit_index, "datetime"]))
                if exit_index >= 0
                else ""
            )
    return output


def _process_symbol_file(
    path_text: str,
    benchmark: dict[pd.Timestamp, float],
    config: FixedLeaderConfig,
    contracts: list[LeaderContract],
) -> list[dict]:
    path = Path(path_text)
    symbol = path.stem.upper()
    if symbol == "NIFTY 50":
        return []
    try:
        frame = pd.read_csv(path, parse_dates=["datetime"]).sort_values("datetime")
    except Exception:
        return []
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return []
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["date"] = frame["datetime"].dt.date.astype(str)
    frame["minute"] = frame["datetime"].dt.hour * 60 + frame["datetime"].dt.minute

    sector = get_sector(symbol)
    volume_history: dict[int, list[float]] = {minute: [] for minute in config.signal_minutes}
    previous_close = 0.0
    rows: list[dict] = []
    for date, raw_day in frame.groupby("date", sort=True):
        day = raw_day.sort_values("datetime").reset_index(drop=True).copy()
        if day.empty:
            continue
        day_open = float(day.loc[0, "open"])
        if day_open <= 0:
            previous_close = float(day.iloc[-1]["close"])
            continue
        volumes = day["volume"].to_numpy(dtype=float)
        closes = day["close"].to_numpy(dtype=float)
        highs = day["high"].to_numpy(dtype=float)
        lows = day["low"].to_numpy(dtype=float)
        cumulative_volume = np.cumsum(volumes)
        cumulative_turnover = np.cumsum(volumes * closes)
        typical = (
            day["high"].to_numpy(dtype=float)
            + day["low"].to_numpy(dtype=float)
            + closes
        ) / 3.0
        cumulative_value = np.cumsum(typical * volumes)
        vwap = np.divide(
            cumulative_value,
            cumulative_volume,
            out=np.zeros(len(day), dtype=float),
            where=cumulative_volume > 0,
        )
        high_so_far = np.maximum.accumulate(highs)
        low_so_far = np.minimum.accumulate(lows)

        for signal_minute in config.signal_minutes:
            matches = np.flatnonzero(day["minute"].to_numpy(dtype=int) == signal_minute)
            if not len(matches):
                continue
            signal_index = int(matches[0])
            current_cumulative_volume = float(cumulative_volume[signal_index])
            prior_volumes = volume_history[signal_minute]
            history_count = len(prior_volumes)
            baseline = (
                float(np.median(prior_volumes[-config.volume_baseline_sessions :]))
                if prior_volumes
                else 0.0
            )
            cumulative_volume_ratio = (
                current_cumulative_volume / baseline if baseline > 0 else 0.0
            )

            # Append only after computing today's feature, so the current
            # session can never leak into its own prior-session baseline.
            prior_volumes.append(current_cumulative_volume)
            if len(prior_volumes) > config.volume_baseline_sessions:
                del prior_volumes[:-config.volume_baseline_sessions]

            entry_index = signal_index + 1
            if previous_close <= 0 or entry_index >= len(day):
                continue
            timestamp = pd.Timestamp(day.loc[signal_index, "datetime"])
            if timestamp not in benchmark:
                continue

            signal_price = float(closes[signal_index])
            raw_entry_price = float(day.loc[entry_index, "open"])
            if min(signal_price, raw_entry_price) <= 0:
                continue
            move_from_open = (signal_price / day_open - 1.0) * 100.0
            day_change = (signal_price / previous_close - 1.0) * 100.0
            gap_pct = (day_open / previous_close - 1.0) * 100.0
            benchmark_move = float(benchmark[timestamp])
            relative_strength = move_from_open - benchmark_move
            intraday_range = float(high_so_far[signal_index] - low_so_far[signal_index])
            range_position = (
                (signal_price - float(low_so_far[signal_index])) / intraday_range
                if intraday_range > 0
                else 0.0
            )
            pullback = (
                (float(high_so_far[signal_index]) - signal_price) / signal_price * 100.0
            )
            vwap_value = float(vwap[signal_index])
            vwap_distance = (
                (signal_price / vwap_value - 1.0) * 100.0 if vwap_value > 0 else 0.0
            )
            previous_three = volumes[max(0, signal_index - 3) : signal_index]
            previous_three_average = float(previous_three.mean()) if len(previous_three) else 0.0
            volume_acceleration = (
                float(volumes[signal_index]) / previous_three_average
                if previous_three_average > 0
                else 0.0
            )
            recent_return = (
                (signal_price / float(closes[signal_index - 3]) - 1.0) * 100.0
                if signal_index >= 3 and closes[signal_index - 3] > 0
                else 0.0
            )
            signal_turnover = float(cumulative_turnover[signal_index])
            turnover_passed, projected_turnover = turnover_gate_values(
                signal_turnover, signal_minute, config
            )

            qualifies = (
                history_count >= config.min_history_sessions
                and signal_price >= config.min_price
                and turnover_passed
                and config.min_move_from_open_pct <= move_from_open <= config.max_move_from_open_pct
                and config.min_day_change_pct <= day_change <= config.max_day_change_pct
                and abs(gap_pct) <= config.max_abs_gap_pct
                and cumulative_volume_ratio >= config.min_cum_volume_ratio
                and volume_acceleration >= config.min_volume_acceleration
                and relative_strength >= config.min_relative_strength_pct
                and range_position >= config.min_range_position
                and pullback <= config.max_pullback_pct
                and config.min_vwap_distance_pct <= vwap_distance <= config.max_vwap_distance_pct
            )
            if not qualifies:
                continue
            outcomes = _simulate_contracts(
                day,
                entry_index,
                raw_entry_price,
                contracts,
                config,
            )
            if outcomes is None:
                continue
            path_features = causal_bar_path_features(
                day,
                signal_index,
                cumulative_volume,
                vwap,
            )
            rows.append(
                {
                    "date": str(date),
                    "symbol": symbol,
                    "sector": sector,
                    "signal_minute": int(signal_minute),
                    "signal_time": timestamp,
                    "entry_time": pd.Timestamp(day.loc[entry_index, "datetime"]),
                    "signal_price": round(signal_price, 6),
                    "move_from_open_pct": round(move_from_open, 6),
                    "day_change_pct": round(day_change, 6),
                    "gap_pct": round(gap_pct, 6),
                    "benchmark_move_pct": round(benchmark_move, 6),
                    "relative_strength_pct": round(relative_strength, 6),
                    "cum_volume_ratio": round(cumulative_volume_ratio, 6),
                    "volume_acceleration": round(volume_acceleration, 6),
                    "history_sessions": int(history_count),
                    "signal_turnover": round(signal_turnover, 2),
                    "projected_daily_turnover": round(projected_turnover, 2),
                    "turnover_mode": config.turnover_mode,
                    "range_position": round(range_position, 6),
                    "pullback_pct": round(pullback, 6),
                    "vwap_distance_pct": round(vwap_distance, 6),
                    "recent_return_3bar_pct": round(recent_return, 6),
                    **{
                        name: round(float(value), 6)
                        for name, value in path_features.items()
                    },
                    "candidate_availability": "fixed_completed_bar_cross_section_then_next_bar_open",
                    **outcomes,
                }
            )
        previous_close = float(day.iloc[-1]["close"])
    return rows


def _init_worker(
    benchmark: dict[pd.Timestamp, float],
    config: FixedLeaderConfig,
    contracts: list[LeaderContract],
) -> None:
    global _WORKER_BENCHMARK, _WORKER_CONFIG, _WORKER_CONTRACTS
    _WORKER_BENCHMARK = benchmark
    _WORKER_CONFIG = config
    _WORKER_CONTRACTS = contracts


def _worker(path_text: str) -> list[dict]:
    return _process_symbol_file(
        path_text,
        _WORKER_BENCHMARK,
        _WORKER_CONFIG,
        _WORKER_CONTRACTS,
    )


def candidate_files(data_dir: str, universe_file: str = "") -> list[Path]:
    files = [
        path
        for path in sorted(Path(data_dir).glob("*.csv"))
        if path.stem.upper() != "NIFTY 50"
    ]
    if not universe_file:
        return files
    universe = pd.read_csv(universe_file)
    if universe.empty:
        raise ValueError(f"quality universe is empty: {universe_file}")
    symbol_column = next(
        (name for name in ("symbol", "Symbol", "SYMBOL", "Stock") if name in universe.columns),
        universe.columns[0],
    )
    eligible = set(universe[symbol_column].astype(str).str.strip().str.upper())
    return [path for path in files if path.stem.strip().upper() in eligible]


def build_candidates(
    data_dir: str,
    calendar_file: str,
    config: FixedLeaderConfig,
    contracts: list[LeaderContract],
    workers: int,
    progress_every: int,
    limit_symbols: int = 0,
    universe_file: str = "",
) -> pd.DataFrame:
    benchmark = _load_benchmark(calendar_file)
    files = candidate_files(data_dir, universe_file)
    if limit_symbols:
        files = files[: int(limit_symbols)]
    rows: list[dict] = []
    executor = None
    if int(workers) <= 1:
        results = (
            _process_symbol_file(str(path), benchmark, config, contracts)
            for path in files
        )
    else:
        executor = ProcessPoolExecutor(
            max_workers=max(1, int(workers)),
            initializer=_init_worker,
            initargs=(benchmark, config, contracts),
        )
        results = executor.map(_worker, [str(path) for path in files], chunksize=1)
    try:
        for index, result in enumerate(results, start=1):
            rows.extend(result)
            if progress_every and (index == 1 or index % int(progress_every) == 0):
                print(f"[{index}/{len(files)}] raw_candidates={len(rows)}", flush=True)
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
    return rank_cross_section(pd.DataFrame(rows))


def rank_cross_section(candidates: pd.DataFrame) -> pd.DataFrame:
    """Rank a same-time candidate batch using signal-time-only features."""
    if candidates is None or candidates.empty:
        return pd.DataFrame() if candidates is None else candidates.copy()
    frame = candidates.copy()
    rank_specs = {
        "move_rank": ("move_from_open_pct", True),
        "volume_rank": ("cum_volume_ratio", True),
        "acceleration_rank": ("volume_acceleration", True),
        "relative_strength_rank": ("relative_strength_pct", True),
        "range_rank": ("range_position", True),
        "turnover_rank": ("signal_turnover", True),
        "pullback_quality_rank": ("pullback_pct", False),
        "vwap_quality_rank": ("vwap_distance_pct", False),
    }
    for output, (source, high_is_good) in rank_specs.items():
        values = frame[source] if high_is_good else -frame[source]
        frame[output] = values.groupby([frame["date"], frame["signal_minute"]]).rank(
            method="average",
            pct=True,
        )
    frame["sector_candidate_count"] = frame.groupby(
        ["date", "signal_minute", "sector"], sort=False
    )["symbol"].transform("count")
    unknown_sector = frame["sector"].astype(str).str.upper().isin({"UNKNOWN", "OTHER", ""})
    frame.loc[unknown_sector, "sector_candidate_count"] = 1
    frame["sector_breadth_rank"] = frame["sector_candidate_count"].groupby(
        [frame["date"], frame["signal_minute"]]
    ).rank(method="average", pct=True)
    frame["rank_score"] = 100.0 * (
        0.25 * frame["move_rank"]
        + 0.25 * frame["volume_rank"]
        + 0.15 * frame["acceleration_rank"]
        + 0.15 * frame["relative_strength_rank"]
        + 0.07 * frame["range_rank"]
        + 0.05 * frame["turnover_rank"]
        + 0.04 * frame["sector_breadth_rank"]
        + 0.02 * frame["pullback_quality_rank"]
        + 0.02 * frame["vwap_quality_rank"]
    )
    frame.sort_values(
        ["date", "signal_minute", "rank_score", "symbol"],
        ascending=[True, True, False, True],
        inplace=True,
    )
    frame["rank_within_batch"] = frame.groupby(
        ["date", "signal_minute"], sort=False
    ).cumcount() + 1
    return frame.reset_index(drop=True)


def _portfolio_metrics(
    frame: pd.DataFrame,
    dates: list[str],
    return_column: str,
    top_k: int,
) -> dict[str, float | int]:
    selected = frame[frame["rank_within_batch"] <= int(top_k)]
    daily_observed = selected.groupby("date")[return_column].mean()
    daily = daily_observed.reindex(dates, fill_value=0.0)
    return {
        "days": len(dates),
        "trade_days": int(daily_observed.index.isin(dates).sum()),
        "trades": int(selected["date"].isin(dates).sum()),
        "avg_daily_return_pct": round(float(daily.mean()), 6),
        "avg_trade_return_pct": round(
            float(selected.loc[selected["date"].isin(dates), return_column].mean())
            if selected["date"].isin(dates).any()
            else 0.0,
            6,
        ),
        "positive_day_pct": round(float((daily > 0).mean() * 100.0), 4),
        "worst_day_pct": round(float(daily.min()), 6),
        "best_day_pct": round(float(daily.max()), 6),
    }


def summarize(
    candidates: pd.DataFrame,
    contracts: list[LeaderContract],
    top_ks: tuple[int, ...],
    calendar_dates: list[str],
    warmup_days: int,
) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        raise ValueError("cannot summarize empty fixed-time leader candidates")
    scored_dates = sorted(set(calendar_dates))[max(0, int(warmup_days)) :]
    if len(scored_dates) < 6:
        raise ValueError("not enough market sessions after warmup for temporal splits")
    train_end = len(scored_dates) // 2
    validation_end = train_end + (len(scored_dates) - train_end) // 2
    splits = {
        "train": scored_dates[:train_end],
        "validation": scored_dates[train_end:validation_end],
        "final": scored_dates[validation_end:],
    }
    prefinal_dates = [*splits["train"], *splits["validation"]]
    blocks = [list(values) for values in np.array_split(prefinal_dates, 4) if len(values)]
    rows = []
    for signal_minute in sorted(candidates["signal_minute"].unique()):
        signal_frame = candidates[candidates["signal_minute"] == signal_minute]
        for contract in contracts:
            for top_k in top_ks:
                row: dict[str, Any] = {
                    "signal_minute": int(signal_minute),
                    "signal_time": f"{int(signal_minute) // 60:02d}:{int(signal_minute) % 60:02d}",
                    "contract": contract.key,
                    "horizon_minutes": contract.horizon_minutes,
                    "target_pct": contract.target_pct,
                    "stop_pct": contract.stop_pct,
                    "top_k": int(top_k),
                }
                for split_name, dates in splits.items():
                    metrics = _portfolio_metrics(
                        signal_frame,
                        dates,
                        contract.return_column,
                        top_k,
                    )
                    for metric, value in metrics.items():
                        row[f"{split_name}_{metric}"] = value
                block_values = []
                for index, dates in enumerate(blocks, start=1):
                    value = _portfolio_metrics(
                        signal_frame,
                        dates,
                        contract.return_column,
                        top_k,
                    )["avg_daily_return_pct"]
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
                row["research_gate_passed"] = bool(
                    row["train_trade_days"] >= 60
                    and row["validation_trade_days"] >= 30
                    and row["selection_block_floor_pct"] > 0.0
                    and row["selection_floor_pct"] > 0.0
                )
                row["evidence_accepted"] = False
                row["real_orders_allowed"] = False
                row["feature_availability"] = "completed_signal_bar_and_prior_sessions_only"
                row["universe_caveat"] = "current_cached_universe_survivorship_bias"
                rows.append(row)
    summary = pd.DataFrame(rows)
    summary.sort_values(
        [
            "research_gate_passed",
            "selection_block_floor_pct",
            "selection_floor_pct",
            "validation_avg_daily_return_pct",
        ],
        ascending=[False, False, False, False],
        inplace=True,
    )
    return summary.reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--calendar-file", default=DEFAULT_CALENDAR)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-times", default="09:30,09:45,10:00,10:30,11:00")
    parser.add_argument("--horizons", default="30,60,120")
    parser.add_argument("--targets", default="0.5,0.7,1.0,1.5")
    parser.add_argument("--stops", default="0.5,0.7,1.0")
    parser.add_argument("--top-k", default="1,3")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--universe-file", default="")
    parser.add_argument("--warmup-days", type=int, default=80)
    parser.add_argument("--min-history-sessions", type=int, default=10)
    parser.add_argument("--volume-baseline-sessions", type=int, default=20)
    parser.add_argument("--min-signal-turnover", type=float, default=30_000_000.0)
    parser.add_argument(
        "--turnover-mode",
        choices=("absolute", "projected_daily"),
        default="absolute",
    )
    parser.add_argument("--min-signal-turnover-floor", type=float, default=5_000_000.0)
    parser.add_argument("--min-move-from-open-pct", type=float, default=0.30)
    parser.add_argument("--max-move-from-open-pct", type=float, default=3.50)
    parser.add_argument("--min-day-change-pct", type=float, default=0.30)
    parser.add_argument("--max-day-change-pct", type=float, default=5.00)
    parser.add_argument("--min-cum-volume-ratio", type=float, default=1.00)
    parser.add_argument("--entry-slippage-bps", type=float, default=5.0)
    parser.add_argument("--additional-cost-pct", type=float, default=0.15)
    args = parser.parse_args()

    signal_minutes = parse_clock_list(args.signal_times)
    horizons = parse_int_list(args.horizons)
    targets = parse_float_list(args.targets)
    stops = parse_float_list(args.stops)
    top_ks = parse_int_list(args.top_k)
    if not signal_minutes or not horizons or not targets or not stops or not top_ks:
        raise ValueError("signal times, horizons, targets, stops, and top-k cannot be empty")
    config = FixedLeaderConfig(
        signal_minutes=signal_minutes,
        horizons=horizons,
        min_history_sessions=args.min_history_sessions,
        volume_baseline_sessions=args.volume_baseline_sessions,
        min_signal_turnover=args.min_signal_turnover,
        turnover_mode=args.turnover_mode,
        min_signal_turnover_floor=args.min_signal_turnover_floor,
        min_move_from_open_pct=args.min_move_from_open_pct,
        max_move_from_open_pct=args.max_move_from_open_pct,
        min_day_change_pct=args.min_day_change_pct,
        max_day_change_pct=args.max_day_change_pct,
        min_cum_volume_ratio=args.min_cum_volume_ratio,
        entry_slippage_bps=args.entry_slippage_bps,
        additional_cost_pct=args.additional_cost_pct,
    )
    contracts = build_contracts(horizons, targets, stops)
    candidates = build_candidates(
        args.data_dir,
        args.calendar_file,
        config,
        contracts,
        args.workers,
        args.progress_every,
        args.limit_symbols,
        args.universe_file,
    )
    if candidates.empty:
        raise RuntimeError("no fixed-time leader candidates")
    summary = summarize(
        candidates,
        contracts,
        top_ks,
        load_calendar_dates(args.calendar_file),
        args.warmup_days,
    )
    summary["entry_slippage_bps"] = float(config.entry_slippage_bps)
    summary["additional_cost_pct"] = float(config.additional_cost_pct)
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(prefix.with_name(prefix.name + "_candidates.csv"), index=False)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    metadata = {
        "strategy": "fixed_time_cross_sectional_leader_rank_v1",
        "signal_times": [
            f"{minute // 60:02d}:{minute % 60:02d}" for minute in signal_minutes
        ],
        "horizons": list(horizons),
        "targets": list(targets),
        "stops": list(stops),
        "top_k": list(top_ks),
        "feature_availability": "completed_signal_bar_and_prior_sessions_only",
        "bar_path_features": list(BAR_PATH_FEATURES),
        "bar_path_feature_availability": "completed_bars_through_signal_bar_only",
        "candidate_availability": "fixed_completed_bar_cross_section_then_next_bar_open",
        "prior_volume_baseline_sessions": config.volume_baseline_sessions,
        "entry_slippage_bps": config.entry_slippage_bps,
        "additional_cost_pct": config.additional_cost_pct,
        "turnover_mode": config.turnover_mode,
        "minimum_projected_daily_turnover": config.min_signal_turnover,
        "minimum_causal_turnover_floor": config.min_signal_turnover_floor,
        "selection_uses_final_period": False,
        "historical_research_gate_count": int(summary["research_gate_passed"].sum()),
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "universe_caveat": "current_cached_universe_survivorship_bias",
        "quality_universe_file": args.universe_file,
    }
    prefix.with_name(prefix.name + "_metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    print(
        f"candidates={len(candidates)} days={candidates['date'].nunique()} "
        f"symbols={candidates['symbol'].nunique()} configs={len(summary)}"
    )
    preview = [
        "signal_time",
        "contract",
        "top_k",
        "train_avg_daily_return_pct",
        "validation_avg_daily_return_pct",
        "final_avg_daily_return_pct",
        "selection_block_floor_pct",
        "research_gate_passed",
    ]
    print(summary[preview].head(20).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

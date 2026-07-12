"""Shared, causal feature and event detection for the intraday event lane."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd


def _parse_time(value: str) -> pd.Timestamp:
    return pd.Timestamp(f"2000-01-01 {value}")


def _spaced_indices(mask: pd.Series, max_count: int, min_gap_bars: int) -> list[int]:
    selected: list[int] = []
    last_idx: int | None = None
    for idx in (int(value) for value in mask[mask].index.tolist()):
        if last_idx is not None and idx - last_idx < int(min_gap_bars):
            continue
        selected.append(idx)
        last_idx = idx
        if len(selected) >= int(max_count):
            break
    return selected


def prepare_intraday_day(
    day: pd.DataFrame,
    benchmark: Mapping[pd.Timestamp, Mapping[str, float]] | None = None,
) -> pd.DataFrame:
    """Calculate signal-time features without reading future bars."""
    required = {"datetime", "open", "high", "low", "close", "volume"}
    missing = required - set(day.columns)
    if missing:
        raise ValueError(f"intraday bars missing columns: {', '.join(sorted(missing))}")
    if day.empty:
        return day.copy()

    prepared = day.sort_values("datetime").reset_index(drop=True).copy()
    for column in ["open", "high", "low", "close", "volume"]:
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce").fillna(0.0).astype(float)
    if "time_key" not in prepared.columns:
        prepared["time_key"] = pd.to_datetime(
            "2000-01-01 " + pd.to_datetime(prepared["datetime"]).dt.strftime("%H:%M:%S")
        )

    day_open = float(prepared.iloc[0]["open"])
    if day_open <= 0:
        raise ValueError("first intraday bar must have a positive open")
    volume = prepared["volume"]
    prepared["move_from_open_pct"] = (prepared["close"] - day_open) / day_open * 100.0
    prepared["vol_avg_so_far"] = volume.expanding().mean().shift(1)
    prepared["vol_ratio"] = volume / prepared["vol_avg_so_far"].replace(0, np.nan)
    prepared["vol_ratio_5"] = volume / volume.rolling(5, min_periods=1).mean().shift(1).replace(0, np.nan)
    prepared["prior_high"] = prepared["high"].cummax().shift(1)
    prepared["high_so_far"] = prepared["high"].cummax()
    prepared["low_so_far"] = prepared["low"].cummin()
    typical = (prepared["high"] + prepared["low"] + prepared["close"]) / 3.0
    prepared["vwap"] = (typical * volume).cumsum() / volume.cumsum().replace(0, np.nan)
    prepared["vwap_dist_pct"] = (prepared["close"] - prepared["vwap"]) / prepared["vwap"].replace(0, np.nan) * 100.0
    prepared["breakout_over_prior_high_pct"] = (
        (prepared["close"] - prepared["prior_high"]) / prepared["prior_high"].replace(0, np.nan) * 100.0
    )
    prepared["candle_body_pct"] = (prepared["close"] - prepared["open"]) / prepared["open"].replace(0, np.nan) * 100.0
    day_range = (prepared["high_so_far"] - prepared["low_so_far"]).replace(0, np.nan)
    prepared["range_pct_so_far"] = day_range / day_open * 100.0
    prepared["range_position"] = ((prepared["close"] - prepared["low_so_far"]) / day_range).clip(0.0, 1.0)
    prepared["recent_return_3bar_pct"] = prepared["close"].pct_change(3).fillna(0.0) * 100.0

    or15_window = prepared[
        (prepared["time_key"] >= _parse_time("09:15:00"))
        & (prepared["time_key"] < _parse_time("09:30:00"))
    ]
    or15_high = float(or15_window["high"].max()) if not or15_window.empty else 0.0
    or15_low = float(or15_window["low"].min()) if not or15_window.empty else 0.0
    prepared["or15_complete"] = (prepared["time_key"] >= _parse_time("09:30:00")).astype(int)
    prepared["or15_high"] = np.where(prepared["or15_complete"] == 1, or15_high, 0.0)
    prepared["or15_low"] = np.where(prepared["or15_complete"] == 1, or15_low, 0.0)
    prepared["or15_breakout_pct"] = np.where(
        (prepared["or15_complete"] == 1) & (or15_high > 0),
        (prepared["close"] - or15_high) / or15_high * 100.0,
        0.0,
    )

    benchmark = benchmark or {}
    benchmark_return = []
    benchmark_recent = []
    for timestamp in prepared["datetime"]:
        item = benchmark.get(pd.Timestamp(timestamp), {})
        benchmark_return.append(float(item.get("benchmark_return_from_open_pct", 0.0)))
        benchmark_recent.append(float(item.get("benchmark_recent_return_3bar_pct", 0.0)))
    prepared["benchmark_return_from_open_pct"] = benchmark_return
    prepared["benchmark_recent_return_3bar_pct"] = benchmark_recent
    prepared["relative_strength_open_pct"] = prepared["move_from_open_pct"] - prepared["benchmark_return_from_open_pct"]
    prepared["relative_strength_3bar_pct"] = prepared["recent_return_3bar_pct"] - prepared["benchmark_recent_return_3bar_pct"]
    return prepared


def detect_intraday_events(day: pd.DataFrame, config: Any) -> list[tuple[str, int, int]]:
    """Return ``(event_type, signal_bar_index, stage)`` for causal events."""
    start = _parse_time(config.start_time)
    latest = _parse_time(config.latest_signal_time)
    base = (day["time_key"] >= start) & (day["time_key"] <= latest)
    common = (
        base
        & day["move_from_open_pct"].between(config.min_move_pct, config.max_move_pct)
        & (day["vol_ratio"].fillna(0.0) >= config.min_vol_ratio)
        & (day["range_pct_so_far"].fillna(99.0) <= config.max_range_used_pct)
        & (day["relative_strength_open_pct"] >= config.min_relative_strength_pct)
    )
    prev_close = day["close"].shift(1)
    prev_vwap = day["vwap"].shift(1)
    prev_above_or15 = day["close"].shift(1) > day["or15_high"]
    prev_above_prior = day["close"].shift(1) > day["prior_high"].shift(1)
    masks = {
        "or15_breakout": (
            common
            & (day["or15_complete"] == 1)
            & (day["or15_high"] > 0.0)
            & (day["close"] > day["or15_high"])
            & (~prev_above_or15.fillna(False))
            & day["or15_breakout_pct"].between(config.min_breakout_pct, config.max_breakout_pct)
            & (day["candle_body_pct"] >= config.min_candle_body_pct)
        ),
        "vwap_reclaim": (
            common
            & (day["vwap"] > 0.0)
            & (prev_close <= prev_vwap)
            & (day["close"] > day["vwap"])
            & day["vwap_dist_pct"].between(config.min_vwap_dist_pct, config.max_vwap_dist_pct)
            & (day["candle_body_pct"] >= config.min_candle_body_pct)
        ),
        "prior_high_continuation": (
            common
            & (day["prior_high"] > 0.0)
            & (day["close"] > day["prior_high"])
            & (~prev_above_prior.fillna(False))
            & day["breakout_over_prior_high_pct"].between(config.min_breakout_pct, config.max_breakout_pct)
            & (day["candle_body_pct"] >= config.min_candle_body_pct)
            & (day["vol_ratio_5"].fillna(0.0) >= config.min_vol_ratio_5)
        ),
        "or15_retest_hold": (
            common
            & (day["or15_complete"] == 1)
            & (day["or15_high"] > 0.0)
            & (day["high_so_far"].shift(1) > day["or15_high"])
            & (day["low"] <= day["or15_high"] * (1.0 + config.retest_tolerance_pct / 100.0))
            & (day["close"] > day["or15_high"])
            & (day["close"] > day["open"])
        ),
        "vwap_pullback_hold": (
            common
            & (day["vwap"] > 0.0)
            & (day["high_so_far"].shift(1) > day["vwap"])
            & (day["low"] <= day["vwap"] * (1.0 + config.retest_tolerance_pct / 100.0))
            & (day["close"] > day["vwap"])
            & (day["close"] > day["open"])
            & (day["vwap_dist_pct"] <= config.max_vwap_dist_pct)
        ),
    }
    events: list[tuple[str, int, int]] = []
    for name, mask in masks.items():
        indices = _spaced_indices(
            mask,
            max_count=max(1, int(config.max_events_per_type_per_day)),
            min_gap_bars=max(1, int(config.min_event_gap_bars)),
        )
        events.extend((name, idx, stage) for stage, idx in enumerate(indices, start=1))
    return events

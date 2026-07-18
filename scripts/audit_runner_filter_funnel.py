#!/usr/bin/env python3
"""Diagnose which causal candidate gates hide eventual intraday runners.

Runner labels use future session highs and are diagnostic only. Every feature
and gate is reconstructed from a completed signal bar, prior sessions, and the
contemporaneous benchmark. The output cannot promote paper or real orders.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_fixed_time_leader_rank import (
    FixedLeaderConfig,
    _load_benchmark,
    parse_clock_list,
)


GATE_ORDER = (
    "history_gate",
    "price_gate",
    "turnover_gate",
    "move_gate",
    "day_change_gate",
    "gap_gate",
    "volume_ratio_gate",
    "volume_acceleration_gate",
    "relative_strength_gate",
    "range_position_gate",
    "pullback_gate",
    "vwap_gate",
)

_EVENTS_BY_SYMBOL: dict[str, dict[str, list[dict[str, Any]]]] = {}
_BENCHMARK: dict[pd.Timestamp, float] = {}
_CONFIG: FixedLeaderConfig | None = None


def evaluate_gates(features: dict[str, float], config: FixedLeaderConfig) -> dict[str, bool]:
    return {
        "history_gate": features["history_sessions"] >= config.min_history_sessions,
        "price_gate": features["signal_price"] >= config.min_price,
        "turnover_gate": features["signal_turnover"] >= config.min_signal_turnover,
        "move_gate": config.min_move_from_open_pct
        <= features["move_from_open_pct"]
        <= config.max_move_from_open_pct,
        "day_change_gate": config.min_day_change_pct
        <= features["day_change_pct"]
        <= config.max_day_change_pct,
        "gap_gate": abs(features["gap_pct"]) <= config.max_abs_gap_pct,
        "volume_ratio_gate": features["cum_volume_ratio"] >= config.min_cum_volume_ratio,
        "volume_acceleration_gate": features["volume_acceleration"]
        >= config.min_volume_acceleration,
        "relative_strength_gate": features["relative_strength_pct"]
        >= config.min_relative_strength_pct,
        "range_position_gate": features["range_position"] >= config.min_range_position,
        "pullback_gate": features["pullback_pct"] <= config.max_pullback_pct,
        "vwap_gate": config.min_vwap_distance_pct
        <= features["vwap_distance_pct"]
        <= config.max_vwap_distance_pct,
    }


def _init_worker(
    events_by_symbol: dict[str, dict[str, list[dict[str, Any]]]],
    benchmark: dict[pd.Timestamp, float],
    config: FixedLeaderConfig,
) -> None:
    global _EVENTS_BY_SYMBOL, _BENCHMARK, _CONFIG
    _EVENTS_BY_SYMBOL = events_by_symbol
    _BENCHMARK = benchmark
    _CONFIG = config


def _process_symbol(path_text: str) -> list[dict[str, Any]]:
    path = Path(path_text)
    symbol = path.stem.upper()
    symbol_events = _EVENTS_BY_SYMBOL.get(symbol, {})
    if not symbol_events or _CONFIG is None:
        return []
    try:
        frame = pd.read_csv(path, parse_dates=["datetime"]).sort_values("datetime")
    except Exception:
        return []
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return []
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["date"] = frame["datetime"].dt.date.astype(str)
    frame["minute"] = frame["datetime"].dt.hour * 60 + frame["datetime"].dt.minute

    config = _CONFIG
    volume_history: dict[int, list[float]] = {
        minute: [] for minute in config.signal_minutes
    }
    previous_close = 0.0
    output: list[dict[str, Any]] = []
    for date, raw_day in frame.groupby("date", sort=True):
        day = raw_day.sort_values("datetime").reset_index(drop=True)
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
        typical = (highs + lows + closes) / 3.0
        cumulative_value = np.cumsum(typical * volumes)
        vwap = np.divide(
            cumulative_value,
            cumulative_volume,
            out=np.zeros(len(day), dtype=float),
            where=cumulative_volume > 0,
        )
        high_so_far = np.maximum.accumulate(highs)
        low_so_far = np.minimum.accumulate(lows)
        events = symbol_events.get(str(date), [])

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
            cum_volume_ratio = (
                current_cumulative_volume / baseline if baseline > 0 else 0.0
            )
            prior_volumes.append(current_cumulative_volume)
            if len(prior_volumes) > config.volume_baseline_sessions:
                del prior_volumes[:-config.volume_baseline_sessions]

            if not events or previous_close <= 0:
                continue
            timestamp = pd.Timestamp(day.loc[signal_index, "datetime"])
            benchmark_move = _BENCHMARK.get(timestamp)
            if benchmark_move is None:
                continue
            signal_price = float(closes[signal_index])
            if signal_price <= 0:
                continue
            move_from_open = (signal_price / day_open - 1.0) * 100.0
            day_change = (signal_price / previous_close - 1.0) * 100.0
            gap_pct = (day_open / previous_close - 1.0) * 100.0
            relative_strength = move_from_open - float(benchmark_move)
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
            prior_three = volumes[max(0, signal_index - 3) : signal_index]
            prior_three_average = float(prior_three.mean()) if len(prior_three) else 0.0
            volume_acceleration = (
                float(volumes[signal_index]) / prior_three_average
                if prior_three_average > 0
                else 0.0
            )
            features = {
                "history_sessions": float(history_count),
                "signal_price": signal_price,
                "signal_turnover": float(cumulative_turnover[signal_index]),
                "move_from_open_pct": move_from_open,
                "day_change_pct": day_change,
                "gap_pct": gap_pct,
                "cum_volume_ratio": cum_volume_ratio,
                "volume_acceleration": volume_acceleration,
                "relative_strength_pct": relative_strength,
                "range_position": range_position,
                "pullback_pct": pullback,
                "vwap_distance_pct": vwap_distance,
            }
            gates = evaluate_gates(features, config)
            for event in events:
                cross_minute = int(event["first_cross_minute"])
                if signal_minute >= cross_minute:
                    continue
                output.append(
                    {
                        "event_id": event["event_id"],
                        "date": str(date),
                        "symbol": symbol,
                        "runner_threshold_pct": float(event["runner_threshold_pct"]),
                        "first_cross_minute": cross_minute,
                        "signal_minute": int(signal_minute),
                        **{key: round(value, 6) for key, value in features.items()},
                        **gates,
                        "all_gates_passed": all(gates.values()),
                        "diagnostic_only": True,
                        "paper_promotion_allowed": False,
                        "real_orders_allowed": False,
                    }
                )
        previous_close = float(day.iloc[-1]["close"])
    return output


def summarize_filter_funnel(
    events: pd.DataFrame,
    states: pd.DataFrame,
    cutoffs: tuple[int, ...],
    first_scan_minute: int = 570,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    event_frame = events.copy()
    event_frame["event_id"] = event_frame["event_id"].astype(str)
    state_frame = states.copy()
    if not state_frame.empty:
        state_frame["event_id"] = state_frame["event_id"].astype(str)
    summary_rows = []
    blocker_rows = []
    for threshold in sorted(event_frame["runner_threshold_pct"].unique()):
        eligible = event_frame[
            event_frame["runner_threshold_pct"].eq(threshold)
            & (event_frame["first_cross_minute"] > int(first_scan_minute))
        ]
        denominator = int(eligible["event_id"].nunique())
        eligible_ids = set(eligible["event_id"])
        threshold_states = state_frame[
            state_frame["event_id"].isin(eligible_ids)
        ] if not state_frame.empty else state_frame
        for cutoff in cutoffs:
            options = threshold_states[
                threshold_states["signal_minute"] <= int(cutoff)
            ] if not threshold_states.empty else threshold_states
            available_ids = set(options["event_id"]) if not options.empty else set()
            summary_rows.append(
                {
                    "runner_threshold_pct": float(threshold),
                    "cutoff_minute": int(cutoff),
                    "gate_type": "availability",
                    "gate": "causal_state_available",
                    "eligible_events": denominator,
                    "recovered_events": len(available_ids),
                    "recall_pct": round(len(available_ids) / denominator * 100.0, 2)
                    if denominator
                    else 0.0,
                }
            )
            cumulative: list[str] = []
            best_stage: dict[str, int] = {event_id: 0 for event_id in available_ids}
            for stage, gate in enumerate(GATE_ORDER, start=1):
                cumulative.append(gate)
                if options.empty:
                    individual_ids: set[str] = set()
                    cumulative_ids: set[str] = set()
                else:
                    individual_ids = set(options.loc[options[gate].eq(True), "event_id"])
                    mask = options[cumulative].all(axis=1)
                    cumulative_ids = set(options.loc[mask, "event_id"])
                for event_id in cumulative_ids:
                    best_stage[event_id] = stage
                for gate_type, recovered in (
                    ("individual", individual_ids),
                    ("cumulative", cumulative_ids),
                ):
                    summary_rows.append(
                        {
                            "runner_threshold_pct": float(threshold),
                            "cutoff_minute": int(cutoff),
                            "gate_type": gate_type,
                            "gate": gate,
                            "eligible_events": denominator,
                            "recovered_events": len(recovered),
                            "recall_pct": round(len(recovered) / denominator * 100.0, 2)
                            if denominator
                            else 0.0,
                        }
                    )
            blockers: dict[str, int] = {"no_state": denominator - len(available_ids)}
            for event_id, stage in best_stage.items():
                blocker = GATE_ORDER[stage] if stage < len(GATE_ORDER) else "all_gates_passed"
                blockers[blocker] = blockers.get(blocker, 0) + 1
            for blocker, count in blockers.items():
                blocker_rows.append(
                    {
                        "runner_threshold_pct": float(threshold),
                        "cutoff_minute": int(cutoff),
                        "best_path_blocker": blocker,
                        "events": int(count),
                        "events_pct": round(count / denominator * 100.0, 2)
                        if denominator
                        else 0.0,
                    }
                )
    summary = pd.DataFrame(summary_rows)
    summary["diagnostic_only"] = True
    summary["paper_promotion_allowed"] = False
    summary["real_orders_allowed"] = False
    blockers = pd.DataFrame(blocker_rows)
    return summary, blockers


def load_events(path: str) -> pd.DataFrame:
    events = pd.read_csv(path)
    required = {"date", "symbol", "runner_threshold_pct", "first_cross_minute"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"runner events missing columns: {', '.join(sorted(missing))}")
    events["date"] = events["date"].astype(str)
    events["symbol"] = events["symbol"].astype(str).str.strip().str.upper()
    events["runner_threshold_pct"] = pd.to_numeric(
        events["runner_threshold_pct"], errors="coerce"
    )
    events["first_cross_minute"] = pd.to_numeric(
        events["first_cross_minute"], errors="coerce"
    )
    events = events.dropna(
        subset=["runner_threshold_pct", "first_cross_minute"]
    ).drop_duplicates(["date", "symbol", "runner_threshold_pct"])
    events["event_id"] = (
        events["date"]
        + "|"
        + events["symbol"]
        + "|"
        + events["runner_threshold_pct"].astype(str)
    )
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--runner-events", required=True)
    parser.add_argument("--calendar-file", required=True)
    parser.add_argument("--universe-file", default="")
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--signal-times", default="09:30,09:45,10:00,10:30")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    events = load_events(args.runner_events)
    events_by_symbol: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for (symbol, date), group in events.groupby(["symbol", "date"], sort=False):
        events_by_symbol.setdefault(str(symbol), {})[str(date)] = group.to_dict(
            orient="records"
        )
    config = FixedLeaderConfig(
        signal_minutes=parse_clock_list(args.signal_times), horizons=(60,)
    )
    benchmark = _load_benchmark(args.calendar_file)
    files = [
        path
        for path in sorted(Path(args.data_dir).glob("*.csv"))
        if path.stem.upper() in events_by_symbol
    ]
    if args.universe_file:
        universe = pd.read_csv(args.universe_file)
        column = "symbol" if "symbol" in universe.columns else universe.columns[0]
        allowed = set(universe[column].astype(str).str.strip().str.upper())
        files = [path for path in files if path.stem.upper() in allowed]
        events = events[events["symbol"].isin(allowed)].copy()

    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(
        max_workers=max(1, int(args.workers)),
        initializer=_init_worker,
        initargs=(events_by_symbol, benchmark, config),
    ) as executor:
        for index, result in enumerate(
            executor.map(_process_symbol, [str(path) for path in files], chunksize=1),
            start=1,
        ):
            rows.extend(result)
            if index == 1 or index % 100 == 0:
                print(f"[{index}/{len(files)}] runner_states={len(rows)}", flush=True)
    states = pd.DataFrame(rows)
    cutoffs = config.signal_minutes
    summary, blockers = summarize_filter_funnel(events, states, cutoffs)
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    states.to_csv(prefix.with_name(prefix.name + "_states.csv"), index=False)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    blockers.to_csv(prefix.with_name(prefix.name + "_blockers.csv"), index=False)
    status = {
        "runner_events": int(events["event_id"].nunique()),
        "runner_states": int(len(states)),
        "symbols": int(events["symbol"].nunique()),
        "signal_minutes": list(config.signal_minutes),
        "gate_order": list(GATE_ORDER),
        "feature_availability": "completed_signal_bar_and_prior_sessions_only",
        "labels_use_future_session_highs": True,
        "diagnostic_only": True,
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    prefix.with_name(prefix.name + "_status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

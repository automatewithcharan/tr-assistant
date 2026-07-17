#!/usr/bin/env python3
"""Measure early-candidate recall against eventual intraday runners."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_DATA_DIR = "results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29"
DEFAULT_CANDIDATES = "results/Data/fixed_time_leader_rank_v1_candidates.csv"
DEFAULT_OUTPUT = "results/Data/top_gainer_early_recall_v1"


def parse_float_list(value: str) -> tuple[float, ...]:
    return tuple(sorted({float(item.strip()) for item in value.split(",") if item.strip()}))


def parse_clock_list(value: str) -> tuple[int, ...]:
    minutes = []
    for item in value.split(","):
        hour, minute = (int(part) for part in item.strip().split(":", 1))
        minutes.append(hour * 60 + minute)
    return tuple(sorted(set(minutes)))


def _runner_events_for_file(args: tuple[str, tuple[float, ...], float, float]) -> list[dict[str, Any]]:
    path_text, thresholds, min_price, min_daily_turnover = args
    path = Path(path_text)
    if path.stem.upper() == "NIFTY 50":
        return []
    try:
        frame = pd.read_csv(path, usecols=["datetime", "open", "high", "close", "volume"])
    except Exception:
        return []
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    for column in ("open", "high", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["datetime", "open", "high", "close", "volume"])
    if frame.empty:
        return []
    frame["date"] = frame["datetime"].dt.date.astype(str)
    frame["minute"] = frame["datetime"].dt.hour * 60 + frame["datetime"].dt.minute
    events = []
    for date, day in frame.groupby("date", sort=True):
        day = day.sort_values("datetime").reset_index(drop=True)
        day_open = float(day.iloc[0]["open"])
        if day_open < float(min_price):
            continue
        turnover = float((day["close"] * day["volume"]).sum())
        if turnover < float(min_daily_turnover):
            continue
        highs = day["high"].to_numpy(dtype=float)
        peak_pct = (float(np.nanmax(highs)) / day_open - 1.0) * 100.0
        for threshold in thresholds:
            if peak_pct < threshold:
                continue
            crossed = np.flatnonzero(highs >= day_open * (1.0 + threshold / 100.0))
            if not len(crossed):
                continue
            first = day.iloc[int(crossed[0])]
            events.append(
                {
                    "date": str(date),
                    "symbol": path.stem.upper(),
                    "runner_threshold_pct": float(threshold),
                    "first_cross_minute": int(first["minute"]),
                    "first_cross_time": str(first["datetime"]),
                    "peak_from_open_pct": round(peak_pct, 6),
                    "daily_turnover": round(turnover, 2),
                }
            )
    return events


def load_runner_events(
    data_dir: str,
    thresholds: tuple[float, ...],
    workers: int = 8,
    min_price: float = 50.0,
    min_daily_turnover: float = 30_000_000.0,
) -> pd.DataFrame:
    files = sorted(Path(data_dir).glob("*.csv"))
    inputs = [(str(path), thresholds, min_price, min_daily_turnover) for path in files]
    rows: list[dict[str, Any]] = []
    if int(workers) <= 1:
        results = map(_runner_events_for_file, inputs)
    else:
        with ProcessPoolExecutor(max_workers=int(workers)) as executor:
            results = executor.map(_runner_events_for_file, inputs, chunksize=4)
            for result in results:
                rows.extend(result)
        results = None
    if results is not None:
        for result in results:
            rows.extend(result)
    return pd.DataFrame(rows)


def load_candidates(path: str, target_exit_column: str) -> pd.DataFrame:
    use = [
        "date",
        "symbol",
        "signal_minute",
        "move_from_open_pct",
        target_exit_column,
    ]
    frame = pd.read_csv(path, usecols=use)
    frame["date"] = frame["date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["signal_minute"] = pd.to_numeric(frame["signal_minute"], errors="coerce")
    frame["move_from_open_pct"] = pd.to_numeric(frame["move_from_open_pct"], errors="coerce")
    return frame.dropna(subset=["date", "symbol", "signal_minute"]).copy()


def evaluate_recall(
    events: pd.DataFrame,
    candidates: pd.DataFrame,
    cutoffs: tuple[int, ...],
    target_exit_column: str,
    first_scan_minute: int = 9 * 60 + 30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if events.empty:
        raise ValueError("runner event set is empty")
    first_candidate = (
        candidates.sort_values(["date", "symbol", "signal_minute"])
        .groupby(["date", "symbol"], sort=False)
    )
    candidate_map = {
        key: group.to_dict(orient="records") for key, group in first_candidate
    }
    detail_rows = []
    summary_rows = []
    for threshold in sorted(events["runner_threshold_pct"].unique()):
        threshold_events = events[events["runner_threshold_pct"].eq(threshold)]
        for cutoff in cutoffs:
            detected = 0
            detected_under_three = 0
            target_hits = 0
            lead_minutes = []
            catchable = int((threshold_events["first_cross_minute"] > first_scan_minute).sum())
            already_crossed = int((threshold_events["first_cross_minute"] <= first_scan_minute).sum())
            for event in threshold_events.to_dict(orient="records"):
                key = (str(event["date"]), str(event["symbol"]))
                options = [
                    row
                    for row in candidate_map.get(key, [])
                    if int(row["signal_minute"]) <= int(cutoff)
                    and int(row["signal_minute"]) < int(event["first_cross_minute"])
                ]
                pick = options[0] if options else None
                if pick is not None:
                    detected += 1
                    move = float(pick["move_from_open_pct"])
                    detected_under_three += int(move <= 3.0)
                    target_hits += int(str(pick[target_exit_column]).strip().lower() == "target")
                    lead = int(event["first_cross_minute"]) - int(pick["signal_minute"])
                    lead_minutes.append(lead)
                detail_rows.append(
                    {
                        **event,
                        "cutoff_minute": int(cutoff),
                        "catchable_after_first_scan": int(event["first_cross_minute"]) > first_scan_minute,
                        "detected_before_cross": pick is not None,
                        "detection_minute": int(pick["signal_minute"]) if pick else 0,
                        "move_at_detection_pct": float(pick["move_from_open_pct"]) if pick else np.nan,
                        "lead_minutes": (
                            int(event["first_cross_minute"]) - int(pick["signal_minute"])
                            if pick
                            else np.nan
                        ),
                        "post_detection_target_hit": (
                            str(pick[target_exit_column]).strip().lower() == "target" if pick else False
                        ),
                    }
                )
            total = int(len(threshold_events))
            summary_rows.append(
                {
                    "runner_threshold_pct": float(threshold),
                    "cutoff_minute": int(cutoff),
                    "total_runner_events": total,
                    "already_crossed_by_first_scan": already_crossed,
                    "catchable_after_first_scan": catchable,
                    "detected_before_cross": detected,
                    "recall_all_pct": round(detected / total * 100.0, 2) if total else 0.0,
                    "recall_catchable_pct": round(detected / catchable * 100.0, 2) if catchable else 0.0,
                    "detected_under_3pct": detected_under_three,
                    "median_lead_minutes": round(float(np.median(lead_minutes)), 2) if lead_minutes else 0.0,
                    "post_detection_target_hits": target_hits,
                    "post_detection_target_hit_pct": round(target_hits / detected * 100.0, 2) if detected else 0.0,
                    "diagnostic_only": True,
                    "paper_promotion_allowed": False,
                    "real_orders_allowed": False,
                }
            )
    return pd.DataFrame(summary_rows), pd.DataFrame(detail_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT)
    parser.add_argument("--runner-thresholds", default="5,10")
    parser.add_argument("--cutoffs", default="09:30,09:45,10:00,10:30")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--min-price", type=float, default=50.0)
    parser.add_argument("--min-daily-turnover", type=float, default=30_000_000.0)
    parser.add_argument("--target-exit-column", default="exit__h60_target_1.5_stop_1")
    args = parser.parse_args()
    thresholds = parse_float_list(args.runner_thresholds)
    cutoffs = parse_clock_list(args.cutoffs)
    events = load_runner_events(
        args.data_dir,
        thresholds,
        workers=args.workers,
        min_price=args.min_price,
        min_daily_turnover=args.min_daily_turnover,
    )
    candidates = load_candidates(args.candidates, args.target_exit_column)
    if not candidates.empty:
        events = events[events["date"] >= str(candidates["date"].min())].copy()
    summary, details = evaluate_recall(events, candidates, cutoffs, args.target_exit_column)
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    details.to_csv(prefix.with_name(prefix.name + "_events.csv"), index=False)
    status = {
        "runner_events": int(len(events)),
        "symbols": int(events["symbol"].nunique()) if not events.empty else 0,
        "dates": int(events["date"].nunique()) if not events.empty else 0,
        "runner_thresholds": list(thresholds),
        "cutoffs": list(cutoffs),
        "target_exit_column": args.target_exit_column,
        "diagnostic_only": True,
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "universe_caveat": "current_cached_universe_survivorship_bias",
    }
    prefix.with_name(prefix.name + "_status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

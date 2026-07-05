#!/usr/bin/env python3
"""Evaluate forward outcomes for live feature recorder rows.

This is the post-market judge for the live-only evidence stream. It does not
create alerts or orders. It labels each recorded live feature row with what
happened after that moment using local 5-minute candles.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


DEFAULT_INPUT = "results/Data/live_feature_recorder/live_features_*.csv"
DEFAULT_DATA_DIR = "results/Data/kite_history_cache/5minute"
DEFAULT_OUT_PREFIX = "results/Data/live_feature_recorder/live_feature_outcomes"
TARGETS = (0.5, 0.7, 1.0, 1.5, 3.0)
STOPS = (0.5, 1.0, 2.5)


def _metric_suffix(value: float) -> str:
    return str(float(value)).replace(".", "_").replace("-", "m")


def _input_files(pattern: str) -> list[Path]:
    path = Path(pattern)
    if any(char in str(pattern) for char in "*?[]"):
        return sorted(path.parent.glob(path.name))
    return [path] if path.exists() else []


def load_live_features(pattern: str) -> pd.DataFrame:
    frames = []
    for path in _input_files(pattern):
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if df.empty:
            continue
        df["SourceFile"] = str(path)
        frames.append(df)
    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    required = {"When", "Symbol", "LastPrice"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"live feature input missing columns: {', '.join(sorted(missing))}")
    out["When"] = pd.to_datetime(out["When"], errors="coerce")
    out["Date"] = out["When"].dt.date.astype(str)
    out["Symbol"] = out["Symbol"].astype(str).str.strip().str.upper()
    out["LastPrice"] = pd.to_numeric(out["LastPrice"], errors="coerce")
    return out.dropna(subset=["When", "LastPrice"]).reset_index(drop=True)


def load_symbol_days(data_dir: str, symbols: Iterable[str]) -> dict[tuple[str, str], pd.DataFrame]:
    out: dict[tuple[str, str], pd.DataFrame] = {}
    data_root = Path(data_dir)
    for symbol in sorted({str(sym).strip().upper() for sym in symbols if str(sym).strip()}):
        path = data_root / f"{symbol}.csv"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if "datetime" not in df.columns:
            for candidate in ("date", "timestamp", "time"):
                if candidate in df.columns:
                    df["datetime"] = df[candidate]
                    break
        if not {"datetime", "high", "low", "close"}.issubset(df.columns):
            continue
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime").copy()
        for col in ("high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["high", "low", "close"])
        if df.empty:
            continue
        df["Date"] = df["datetime"].dt.date.astype(str)
        for date, day in df.groupby("Date", sort=False):
            out[(symbol, str(date))] = day.reset_index(drop=True)
    return out


def simulate_target_stop(
    future: pd.DataFrame,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
) -> tuple[str, float, str]:
    if future.empty or entry_price <= 0:
        return "no_future", 0.0, ""
    target = entry_price * (1.0 + target_pct / 100.0)
    stop = entry_price * (1.0 - stop_pct / 100.0)
    highs = future["high"].to_numpy(dtype=float)
    lows = future["low"].to_numpy(dtype=float)
    times = future["datetime"].astype(str).to_numpy()
    target_hits = np.flatnonzero(highs >= target)
    stop_hits = np.flatnonzero(lows <= stop)
    first_target = int(target_hits[0]) if len(target_hits) else None
    first_stop = int(stop_hits[0]) if len(stop_hits) else None
    if first_target is not None and first_stop is not None:
        if first_target == first_stop:
            return "stop_first_same_bar", -abs(stop_pct), str(times[first_stop])
        if first_stop < first_target:
            return "stop", -abs(stop_pct), str(times[first_stop])
        return "target", float(target_pct), str(times[first_target])
    if first_stop is not None:
        return "stop", -abs(stop_pct), str(times[first_stop])
    if first_target is not None:
        return "target", float(target_pct), str(times[first_target])
    eod_close = float(future.iloc[-1]["close"])
    eod_return = (eod_close - entry_price) / entry_price * 100.0
    return "eod", eod_return, str(future.iloc[-1]["datetime"])


def feature_bucket(row: pd.Series) -> str:
    missed = bool(row.get("MissedRunnerCandidate", False))
    stable = bool(row.get("OrderBookStable", False))
    spread_ok = bool(row.get("SpreadOk", False))
    alerted = bool(row.get("AlreadyAlertedToday", False))
    sector_top2 = float(row.get("SectorTop2Count", 0) or 0) >= 2
    if missed and stable and spread_ok and sector_top2:
        return "missed_stable_tight_sector"
    if missed and stable:
        return "missed_stable_depth"
    if missed:
        return "missed_runner"
    if alerted:
        return "alerted_today"
    if stable:
        return "stable_order_book"
    return "other"


def evaluate_rows(features: pd.DataFrame, symbol_days: dict[tuple[str, str], pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for _, row in features.iterrows():
        symbol = str(row["Symbol"]).strip().upper()
        date = str(row["Date"])
        entry_time = pd.Timestamp(row["When"])
        entry_price = float(row["LastPrice"])
        day = symbol_days.get((symbol, date))
        future = pd.DataFrame()
        if day is not None and not day.empty:
            future = day[day["datetime"] >= entry_time].reset_index(drop=True)

        out = row.to_dict()
        out["FeatureBucket"] = feature_bucket(row)
        if future.empty or entry_price <= 0:
            out["BarsAfterEntry"] = 0
            out["MaxFavorablePct"] = 0.0
            out["MaxAdversePct"] = 0.0
            out["EodReturnPct"] = 0.0
        else:
            max_high = float(future["high"].max())
            min_low = float(future["low"].min())
            eod_close = float(future.iloc[-1]["close"])
            out["BarsAfterEntry"] = int(len(future))
            out["MaxFavorablePct"] = round((max_high - entry_price) / entry_price * 100.0, 4)
            out["MaxAdversePct"] = round((min_low - entry_price) / entry_price * 100.0, 4)
            out["EodReturnPct"] = round((eod_close - entry_price) / entry_price * 100.0, 4)

        for target in TARGETS:
            for stop in STOPS:
                suffix = f"t{_metric_suffix(target)}_s{_metric_suffix(stop)}"
                outcome, ret, exit_time = simulate_target_stop(future, entry_price, target, stop)
                out[f"{suffix}_outcome"] = outcome
                out[f"{suffix}_return_pct"] = round(ret, 4)
                out[f"{suffix}_exit_time"] = exit_time
        rows.append(out)
    return pd.DataFrame(rows)


def summarize(outcomes: pd.DataFrame) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame()
    target_cols = [
        col
        for col in outcomes.columns
        if col.startswith("t") and col.endswith("_outcome")
    ]
    grouped = []
    for bucket, group in outcomes.groupby("FeatureBucket", dropna=False):
        row = {
            "FeatureBucket": bucket,
            "Rows": int(len(group)),
            "Symbols": int(group["Symbol"].nunique()) if "Symbol" in group.columns else 0,
            "AvgMfePct": round(float(group["MaxFavorablePct"].mean()), 4),
            "MedianMfePct": round(float(group["MaxFavorablePct"].median()), 4),
            "AvgMaePct": round(float(group["MaxAdversePct"].mean()), 4),
            "AvgEodReturnPct": round(float(group["EodReturnPct"].mean()), 4),
        }
        for col in target_cols:
            hit_rate = (group[col].astype(str) == "target").mean() * 100.0
            row[f"{col.replace('_outcome', '')}_hit_rate_pct"] = round(float(hit_rate), 2)
        grouped.append(row)
    return pd.DataFrame(grouped).sort_values(["Rows", "FeatureBucket"], ascending=[False, True])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-prefix", default=DEFAULT_OUT_PREFIX)
    args = parser.parse_args()

    features = load_live_features(args.input)
    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    outcomes_path = Path(f"{out_prefix}_rows.csv")
    summary_path = Path(f"{out_prefix}_summary.csv")

    if features.empty:
        pd.DataFrame().to_csv(outcomes_path, index=False)
        pd.DataFrame().to_csv(summary_path, index=False)
        print("No live feature rows found.")
        print(f"Saved: {outcomes_path}")
        print(f"Saved: {summary_path}")
        return 0

    symbol_days = load_symbol_days(args.data_dir, features["Symbol"].unique())
    outcomes = evaluate_rows(features, symbol_days)
    summary = summarize(outcomes)
    outcomes.to_csv(outcomes_path, index=False)
    summary.to_csv(summary_path, index=False)
    print(f"Saved: {outcomes_path}")
    print(f"Saved: {summary_path}")
    print(f"Rows: {len(outcomes)} symbols={outcomes['Symbol'].nunique()} days={outcomes['Date'].nunique()}")
    if not summary.empty:
        print(summary.head(20).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

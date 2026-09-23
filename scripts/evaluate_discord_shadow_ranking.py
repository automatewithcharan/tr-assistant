#!/usr/bin/env python3
"""Read-only outcome evaluator for Discord Phase 2 shadow priorities.

The evaluator never rewrites ranking evidence and never feeds outcomes into the
live scorer. It joins later bars/Pine BUY logs only in memory, after priority is
already frozen.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from pkscreener.classes.DiscordShadowRanking import load_shadow_priority_records


HORIZONS = (5, 15, 30, 60)
THRESHOLDS = (0.5, 1.0, 2.0)


def _ist_naive(value: Any) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    timestamp = pd.Timestamp(parsed)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("Asia/Kolkata").tz_localize(None)
    return timestamp


def _flatten(records: Iterable[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for record in records:
        features = dict(record.get("features") or {})
        latencies = dict(record.get("stage_latencies_seconds") or {})
        drifts = dict(record.get("stage_price_drifts_pct") or {})
        rows.append({
            **{key: value for key, value in record.items() if not isinstance(value, (dict, list))},
            **features,
            **latencies,
            **drifts,
            "warnings_text": ",".join(record.get("warnings") or []),
        })
    return pd.DataFrame(rows)


def _read_bars(pattern: str | None) -> pd.DataFrame:
    if not pattern:
        return pd.DataFrame()
    frames = []
    for path in sorted(glob.glob(pattern)):
        frame = pd.read_csv(path)
        lower = {str(column).lower(): column for column in frame.columns}
        time_column = next((lower[key] for key in ("datetime", "timestamp", "time") if key in lower), None)
        symbol_column = next((lower[key] for key in ("symbol", "stock", "ticker") if key in lower), None)
        if time_column is None or symbol_column is None:
            continue
        frame = frame.rename(columns={time_column: "bar_time", symbol_column: "symbol"})
        frame["bar_time"] = frame["bar_time"].map(_ist_naive)
        frame["symbol"] = frame["symbol"].astype(str).str.upper().str.replace("NSE:", "", regex=False)
        for column in ("high", "low", "close"):
            source = lower.get(column)
            if source and source != column:
                frame = frame.rename(columns={source: column})
        if {"high", "low", "close"}.issubset(frame.columns):
            frames.append(frame[["symbol", "bar_time", "high", "low", "close"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _normalize_pine_buys(paths: Iterable[str]) -> pd.DataFrame:
    rows = []
    price_re = re.compile(r"EntryRef\s+([0-9.]+)", re.I)
    for path in paths:
        frame = pd.read_csv(path)
        for raw in frame.to_dict(orient="records"):
            description = str(raw.get("Description") or raw.get("description") or raw.get("message") or "")
            if "NG SCALP BUY" not in description.upper():
                continue
            symbol = str(raw.get("Ticker Name") or raw.get("symbol") or raw.get("ticker") or "").upper()
            symbol = symbol.replace("NSE:", "").split(",", 1)[0].strip()
            timestamp = _ist_naive(raw.get("Time") or raw.get("timestamp") or raw.get("time"))
            match = price_re.search(description)
            price = float(match.group(1)) if match else pd.to_numeric(raw.get("price"), errors="coerce")
            if symbol and not pd.isna(timestamp):
                rows.append({"symbol": symbol, "pine_buy_timestamp": timestamp, "pine_buy_price": price})
    return pd.DataFrame(rows)


def attach_future_outcomes(
    records: Iterable[dict[str, Any]],
    bars: pd.DataFrame,
    pine_buys: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Attach strictly post-attempt outcomes to already-frozen records."""
    frame = _flatten(records)
    if frame.empty:
        return frame
    frame["discord_attempt_at"] = frame["discord_attempt_at"].map(_ist_naive)
    frame["discord_attempt_price"] = pd.to_numeric(frame["discord_attempt_price"], errors="coerce")
    normalized_bars = bars.copy()
    if not normalized_bars.empty:
        normalized_bars["bar_time"] = normalized_bars["bar_time"].map(_ist_naive)
        normalized_bars["symbol"] = normalized_bars["symbol"].astype(str).str.upper().str.replace("NSE:", "", regex=False)
    outcome_rows = []
    for record in frame.to_dict(orient="records"):
        symbol = str(record.get("symbol") or "").replace("NSE:", "").upper()
        start = record.get("discord_attempt_at")
        entry = record.get("discord_attempt_price")
        result: dict[str, Any] = {}
        symbol_bars = normalized_bars[normalized_bars["symbol"] == symbol].sort_values("bar_time") if not normalized_bars.empty else pd.DataFrame()
        future = symbol_bars[symbol_bars["bar_time"] > start] if not symbol_bars.empty and not pd.isna(start) else pd.DataFrame()
        if entry and not pd.isna(entry) and entry > 0:
            for minutes in HORIZONS:
                window = future[future["bar_time"] <= start + pd.Timedelta(minutes=minutes)]
                result[f"mfe_{minutes}m_pct"] = round((window["high"].max() / entry - 1) * 100, 6) if not window.empty else None
                result[f"mae_{minutes}m_pct"] = round((window["low"].min() / entry - 1) * 100, 6) if not window.empty else None
            same_day = future[future["bar_time"].dt.date == start.date()] if not future.empty else future
            result["mfe_eod_pct"] = round((same_day["high"].max() / entry - 1) * 100, 6) if not same_day.empty else None
            result["mae_eod_pct"] = round((same_day["low"].min() / entry - 1) * 100, 6) if not same_day.empty else None
            favorable = same_day[same_day["high"] >= entry * 1.005] if not same_day.empty else same_day
            result["minutes_to_0_5_pct"] = round((favorable.iloc[0]["bar_time"] - start).total_seconds() / 60, 3) if not favorable.empty else None
        outcome_rows.append(result)
    outcomes = pd.concat([frame.reset_index(drop=True), pd.DataFrame(outcome_rows)], axis=1)

    outcomes["pine_buy_generated"] = False
    outcomes["pine_buy_timestamp"] = pd.NaT
    outcomes["pine_buy_price"] = float("nan")
    if pine_buys is not None and not pine_buys.empty:
        pine_buys = pine_buys.copy()
        pine_buys["pine_buy_timestamp"] = pine_buys["pine_buy_timestamp"].map(_ist_naive)
        for index, record in outcomes.iterrows():
            symbol = str(record["symbol"]).replace("NSE:", "").upper()
            matches = pine_buys[
                (pine_buys["symbol"] == symbol)
                & (pine_buys["pine_buy_timestamp"] >= record["discord_attempt_at"])
                & (pine_buys["pine_buy_timestamp"].dt.date == record["discord_attempt_at"].date())
            ].sort_values("pine_buy_timestamp")
            if matches.empty:
                continue
            buy = matches.iloc[0]
            outcomes.at[index, "pine_buy_generated"] = True
            outcomes.at[index, "pine_buy_timestamp"] = buy["pine_buy_timestamp"]
            outcomes.at[index, "pine_buy_price"] = buy["pine_buy_price"]
            outcomes.at[index, "discord_to_pine_seconds"] = (
                buy["pine_buy_timestamp"] - record["discord_attempt_at"]
            ).total_seconds()
            if record["discord_attempt_price"] > 0 and not pd.isna(buy["pine_buy_price"]):
                outcomes.at[index, "discord_to_pine_price_drift_pct"] = (
                    buy["pine_buy_price"] / record["discord_attempt_price"] - 1
                ) * 100
    return outcomes


def summarize_group(frame: pd.DataFrame, group: str) -> pd.DataFrame:
    rows = []
    if frame.empty or group not in frame:
        return pd.DataFrame()
    def numeric(part: pd.DataFrame, column: str) -> pd.Series:
        if column not in part:
            return pd.Series(index=part.index, dtype="float64")
        return pd.to_numeric(part[column], errors="coerce")
    for value, part in frame.groupby(group, dropna=False):
        row: dict[str, Any] = {group: value, "candidate_count": len(part)}
        for minutes in HORIZONS:
            mfe = numeric(part, f"mfe_{minutes}m_pct")
            mae = numeric(part, f"mae_{minutes}m_pct")
            row[f"median_mfe_{minutes}m_pct"] = mfe.median()
            row[f"median_mae_{minutes}m_pct"] = mae.median()
            row[f"mfe_mae_ratio_{minutes}m"] = mfe.median() / abs(mae.median()) if mae.notna().any() and mae.median() != 0 else None
            for threshold in THRESHOLDS:
                valid = mfe.dropna()
                row[f"prob_{threshold:g}pct_{minutes}m"] = (valid >= threshold).mean() if not valid.empty else None
        row["median_mfe_eod_pct"] = numeric(part, "mfe_eod_pct").median()
        row["median_mae_eod_pct"] = numeric(part, "mae_eod_pct").median()
        row["median_minutes_to_0_5_pct"] = numeric(part, "minutes_to_0_5_pct").median()
        row["pine_buy_conversion_rate"] = part["pine_buy_generated"].astype(bool).mean()
        row["median_discord_to_pine_seconds"] = numeric(part, "discord_to_pine_seconds").median()
        row["median_discord_to_pine_drift_pct"] = numeric(part, "discord_to_pine_price_drift_pct").median()
        rows.append(row)
    return pd.DataFrame(rows)


def add_segments(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if result.empty:
        return result
    result["time_of_day"] = pd.to_datetime(result["discord_attempt_at"], errors="coerce").dt.hour.map(
        lambda hour: None if pd.isna(hour) else "open" if hour < 10 else "midday" if hour < 13 else "afternoon"
    )
    for source, target, bins, labels in (
        ("traded_value", "liquidity_bucket", [-float("inf"), 10_000_000, 50_000_000, float("inf")], ["low", "medium", "strong"]),
        ("rvol", "rvol_bucket", [-float("inf"), 1.5, 2.5, float("inf")], ["<1.5x", "1.5-2.5x", ">=2.5x"]),
        ("nifty_relative_strength_pct", "nifty_rs_bucket", [-float("inf"), -0.5, 0.5, float("inf")], ["weak", "neutral", "strong"]),
        ("sector_strength_pct", "sector_rs_bucket", [-float("inf"), 0, 1, float("inf")], ["negative", "0-1%", ">=1%"]),
        ("vwap_distance_pct", "vwap_extension_bucket", [-float("inf"), 0, 1, 2, float("inf")], ["below", "0-1%", "1-2%", ">2%"]),
    ):
        if source in result:
            result[target] = pd.cut(pd.to_numeric(result[source], errors="coerce"), bins=bins, labels=labels, right=False)
    return result


def evaluate(records_path: str, bars_pattern: str | None = None, pine_paths: Iterable[str] = ()) -> dict[str, pd.DataFrame]:
    records = load_shadow_priority_records(records_path)
    outcomes = add_segments(attach_future_outcomes(records, _read_bars(bars_pattern), _normalize_pine_buys(pine_paths)))
    reports = {"outcomes": outcomes, "priority": summarize_group(outcomes, "priority")}
    for segment in (
        "lane", "multi_lane", "time_of_day", "liquidity_bucket", "rvol_bucket",
        "nifty_rs_bucket", "sector_rs_bucket", "vwap_extension_bucket",
    ):
        reports[segment] = summarize_group(outcomes, segment)
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True, help="Phase 2 JSONL file or directory")
    parser.add_argument("--bars", help="Glob for later intraday bar CSVs")
    parser.add_argument("--pine-alerts", action="append", default=[], help="TradingView alert CSV (repeatable)")
    parser.add_argument("--output", help="Optional new report directory; source evidence is never modified")
    args = parser.parse_args()
    reports = evaluate(args.records, args.bars, args.pine_alerts)
    if args.output:
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        for name, frame in reports.items():
            frame.to_csv(output / f"discord_shadow_{name}.csv", index=False)
    print(json.dumps({name: len(frame) for name, frame in reports.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

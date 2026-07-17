#!/usr/bin/env python3
"""Rebuild corrupted UTC-naive event-shadow evidence without overwriting source."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.IntradayEventEngine import (
    add_cross_sectional_event_features,
    detect_intraday_events,
    live_event_feature_row,
    prepare_intraday_day,
    score_intraday_event,
)
from pkscreener.classes.IntradayEventShadowOutcomes import (
    evaluate_event_shadow_day,
    evaluate_event_shadow_fade_day,
)
from pkscreener.classes.IntradayEventShadowRecorder import EventShadowConfig


MARKET_OPEN = 9 * 60 + 15
LAST_RELIABLE_BAR = 15 * 60 + 20


def normalize_archived_bars(frame: pd.DataFrame, source_timezone: str = "auto") -> tuple[pd.DataFrame, str]:
    data = frame.copy()
    data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
    data = data.dropna(subset=["datetime"]).copy()
    if data.empty:
        return data, "none"
    if data["datetime"].dt.tz is not None:
        data["datetime"] = data["datetime"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        repair = "timezone_aware_to_ist"
    else:
        timezone = str(source_timezone or "auto").strip().lower()
        if timezone == "auto":
            minute_90 = float(
                (data["datetime"].dt.hour * 60 + data["datetime"].dt.minute).quantile(0.90)
            )
            timezone = "utc" if minute_90 < 12 * 60 else "ist"
        if timezone == "utc":
            data["datetime"] = data["datetime"] + pd.Timedelta(hours=5, minutes=30)
            repair = "utc_naive_plus_0530"
        elif timezone == "ist":
            repair = "ist_naive_unchanged"
        else:
            raise ValueError(f"unsupported source timezone: {source_timezone}")

    minute = data["datetime"].dt.hour * 60 + data["datetime"].dt.minute
    data = data[minute.between(MARKET_OPEN, LAST_RELIABLE_BAR)].copy()
    data["symbol"] = data["symbol"].astype(str).str.strip().str.upper()
    data = data.drop_duplicates(["symbol", "datetime"], keep="last")
    return data.sort_values(["datetime", "symbol"]).reset_index(drop=True), repair


def _benchmark_map(bars: pd.DataFrame, symbol: str) -> dict:
    benchmark = bars[bars["symbol"].eq(symbol)].sort_values("datetime").copy()
    if benchmark.empty:
        return {}
    day_open = float(benchmark.iloc[0]["open"])
    benchmark["benchmark_return_from_open_pct"] = (
        benchmark["close"].astype(float) - day_open
    ) / day_open * 100.0
    benchmark["benchmark_recent_return_3bar_pct"] = (
        benchmark["close"].astype(float).pct_change(3).fillna(0.0) * 100.0
    )
    return {
        pd.Timestamp(row.datetime): {
            "benchmark_return_from_open_pct": float(row.benchmark_return_from_open_pct),
            "benchmark_recent_return_3bar_pct": float(row.benchmark_recent_return_3bar_pct),
        }
        for row in benchmark.itertuples(index=False)
    }


def rebuild_events(bars: pd.DataFrame, config: EventShadowConfig | None = None) -> pd.DataFrame:
    config = config or EventShadowConfig()
    benchmark_symbol = config.benchmark_symbol.strip().upper()
    benchmark = _benchmark_map(bars, benchmark_symbol)
    if not benchmark:
        return pd.DataFrame()
    raw_events = []
    config_namespace = SimpleNamespace(**asdict(config))
    for symbol, symbol_bars in bars.groupby("symbol", sort=True):
        if symbol == benchmark_symbol or len(symbol_bars) < 4:
            continue
        prepared = prepare_intraday_day(
            symbol_bars[["datetime", "open", "high", "low", "close", "volume"]],
            benchmark,
        )
        for event_type, index, stage in detect_intraday_events(prepared, config_namespace):
            signal = prepared.iloc[index]
            features = live_event_feature_row(prepared, index, event_type)
            raw_events.append(
                {
                    "date": str(pd.Timestamp(signal["datetime"]).date()),
                    "symbol": symbol,
                    "event_type": event_type,
                    "event_stage": int(stage),
                    "signal_time": pd.Timestamp(signal["datetime"]).isoformat(),
                    "price": round(float(signal["close"]), 4),
                    "move_from_open_pct": round(float(signal["move_from_open_pct"]), 6),
                    "vol_ratio": round(float(signal["vol_ratio"]), 6),
                    "vol_ratio_5": round(float(signal["vol_ratio_5"]), 6),
                    "vwap_dist_pct": round(float(signal["vwap_dist_pct"]), 6),
                    "relative_strength_open_pct": round(float(signal["relative_strength_open_pct"]), 6),
                    "range_pct_so_far": round(float(signal["range_pct_so_far"]), 6),
                    "event_score": round(score_intraday_event(event_type, signal), 6),
                    "benchmark_available": True,
                    "paper_only": True,
                    "rebuild_source": "archived_live_tick_bars",
                    **features,
                }
            )
    if not raw_events:
        return pd.DataFrame()
    enriched = []
    event_frame = pd.DataFrame(raw_events)
    for _, same_time in event_frame.groupby("signal_time", sort=True):
        enriched.extend(add_cross_sectional_event_features(same_time.to_dict(orient="records")))
    return pd.DataFrame(enriched).sort_values(
        ["signal_time", "event_score", "symbol"], ascending=[True, False, True]
    ).reset_index(drop=True)


def rebuild_day(
    source_root: Path,
    output_root: Path,
    date: str,
    source_timezone: str = "auto",
) -> dict:
    source = source_root / "intraday_event_shadow" / f"bars_{date}.csv"
    if not source.exists():
        return {"date": date, "status": "missing_source", "real_orders_allowed": False}
    raw = pd.read_csv(source)
    bars, repair = normalize_archived_bars(raw, source_timezone=source_timezone)
    target = output_root / "intraday_event_shadow"
    target.mkdir(parents=True, exist_ok=True)
    bars.to_csv(target / f"bars_{date}.csv", index=False)
    events = rebuild_events(bars)
    events.to_csv(target / f"events_{date}.csv", index=False)
    long_outcomes, _ = evaluate_event_shadow_day(
        output_root, date, required_last_bar="15:20:00"
    )
    short_outcomes, _ = evaluate_event_shadow_fade_day(
        output_root, date, required_last_bar="15:20:00"
    )
    return {
        "date": date,
        "status": "complete" if not events.empty else "no_events",
        "timestamp_repair": repair,
        "bars": int(len(bars)),
        "symbols": int(bars["symbol"].nunique()) if not bars.empty else 0,
        "events": int(len(events)),
        "complete_long_outcomes": int(long_outcomes.get("complete_session", pd.Series(dtype=bool)).sum()),
        "complete_short_outcomes": int(short_outcomes.get("complete_session", pd.Series(dtype=bool)).sum()),
        "paper_only": True,
        "real_orders_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default="results/Data")
    parser.add_argument("--output-root", default="results/Data/intraday_event_shadow_rebuilt")
    parser.add_argument("--dates", required=True, help="comma-separated YYYY-MM-DD values")
    parser.add_argument("--source-timezone", choices=["auto", "utc", "ist"], default="auto")
    args = parser.parse_args()
    source_root = Path(args.source_root)
    output_root = Path(args.output_root)
    dates = [item.strip() for item in args.dates.split(",") if item.strip()]
    rows = [
        rebuild_day(source_root, output_root, date, source_timezone=args.source_timezone)
        for date in dates
    ]
    status = {
        "evidence_type": "repaired_live_tick_event_shadow",
        "source_preserved": True,
        "days": rows,
        "paper_only": True,
        "real_orders_allowed": False,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "rebuild_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))
    return 0 if any(row["status"] == "complete" for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Analyze PKScreener buy alerts across multiple holding horizons.

This script uses the saved signal-performance ledger as the source of actual
alerts, then extends those alerts into multi-day holding periods using cached
daily candles. The goal is to classify alerts into the horizon where they have
the best expectancy rather than assuming every alert is purely intraday.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "results" / "Data"
DAY_CACHE_DIR = DATA_DIR / "kite_history_cache" / "day"
DEFAULT_LEDGER = DATA_DIR / "signalperf_allstocks_ledger_latest.csv"
DEFAULT_DISCORD_EXPORT = BASE_DIR / "Screener Alerts - Text Channels - general [1488777695750914060].txt"


BUY_ACTIONS = {"Strong Buy Now", "Buy Now", "Momentum Buy"}
HORIZON_LABELS = {
    "15m": "Intraday 15m",
    "60m": "Intraday 60m",
    "EOD": "Intraday EOD",
    "1D": "Next day",
    "2D": "2-day swing",
    "5D": "5-day swing",
    "10D": "10-day swing",
    "20D": "20-day position",
    "60D": "60-day investment watch",
}

BOT_BLOCK_RE = re.compile(r"^\[(\d{2}-\d{2}-\d{4} \d{2}:\d{2})\] Spidey Bot\s*$", re.M)

# Single rich block alerts.
RICH_ALERT_RE = re.compile(
    r"\*\*(?:🟢 BUY|🔥 STRONG BUY|🔍 MISSED EARLIER, NOW STRONG)\s+—\s+`(?P<sym>[^`]+)`.*?"
    r"Price now:\s*`₹(?P<price>[\d.]+)`.*?"
    r"EXIT if price drops to\s*`₹(?P<stop>[\d.]+)`.*?"
    r"SELL(?: at| near)\s*`₹(?P<t1>[\d.]+)`(?:.*?or\s*`₹(?P<t2>[\d.]+)`)?",
    re.S,
)

DEMAND_ALERT_RE = re.compile(
    r"\*\*🔮\s*(?P<sym>[A-Z0-9&\-]+).*?"
    r"Buy\s+\*\*\d+\s+shares\*\*\s+at\s+~`₹(?P<price>[\d.]+)`.*?"
    r"EXIT if price drops to\s*`₹(?P<stop>[\d.]+)`.*?"
    r"SELL near\s*`₹(?P<t2>[\d.]+)`",
    re.S,
)

MOMENTUM_ALERT_RE = re.compile(
    r"\*\*🔵 MOMENTUM BUY \[[A-Z]\]\s+—\s+`(?P<sym>[^`]+)`\*\*.*?"
    r"Price\s*`₹(?P<price>[\d.]+)`.*?"
    r"Stop Loss\s*`₹(?P<stop>[\d.]+)`\s*\|\s*🟡 Target 1\s*`₹(?P<t1>[\d.]+)`\s*\|\s*🟢 Target 2\s*`₹(?P<t2>[\d.]+)`",
    re.S,
)

PICKS_BLOCK_RE = re.compile(
    r"\*\*BUY `(?P<sym>[^`]+)`\*\*.*?"
    r"Price `₹(?P<price>[\d.]+)`.*?"
    r"Stop Loss `(?P<stop>[\d.]+)` \| Target 1 `(?P<t1>[\d.]+)` \| Target 2 `(?P<t2>[\d.]+)`",
    re.S,
)

OPENING_SCAN_RE = re.compile(
    r"\*\*\d+\.\s+[⭐🔥✅📝]+\s+`(?P<sym>[^`]+)`\*\*.*?"
    r"`₹(?P<price>[\d.]+)`.*?"
    r"SL `₹(?P<stop>[\d.]+)` → T1 `₹(?P<t1>[\d.]+)` → T2 `₹(?P<t2>[\d.]+)`",
    re.S,
)


def _safe_float(value, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in [None, "", "None"]:
            return default
        numeric = float(value)
        if numeric != numeric:  # NaN
            return default
        return numeric
    except Exception:
        return default


def load_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Ledger not found: {path}")
    df = pd.read_csv(path)
    if df.empty:
        return df
    if "AlertId" in df.columns:
        df = df.drop_duplicates(subset=["AlertId"], keep="last")
    df["AlertTime"] = pd.to_datetime(df["AlertTime"], errors="coerce")
    df["AlertDate"] = df["AlertTime"].dt.date
    df["EntryPrice"] = pd.to_numeric(df["EntryPrice"], errors="coerce")
    df["SignalStrengthPct"] = pd.to_numeric(df.get("SignalStrengthPct"), errors="coerce")
    return df


def iter_bot_blocks(text: str) -> Iterable[Tuple[datetime, str]]:
    matches = list(BOT_BLOCK_RE.finditer(text))
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        when = datetime.strptime(match.group(1), "%d-%m-%Y %H:%M")
        yield when, text[start:end].strip()


def parse_discord_export(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()

    rows: List[Dict] = []
    text = path.read_text(errors="ignore")
    for when, body in iter_bot_blocks(text):
        matched_any = False
        for regex, action in [
            (RICH_ALERT_RE, "Buy Now"),
            (DEMAND_ALERT_RE, "Momentum Buy"),
            (MOMENTUM_ALERT_RE, "Momentum Buy"),
            (PICKS_BLOCK_RE, "Buy Now"),
            (OPENING_SCAN_RE, "Buy Now"),
        ]:
            for match in regex.finditer(body):
                data = match.groupdict()
                price = _safe_float(data.get("price"))
                stop = _safe_float(data.get("stop"), 0.0) or 0.0
                t1 = _safe_float(data.get("t1"), 0.0) or 0.0
                t2 = _safe_float(data.get("t2"), 0.0) or 0.0
                if price is None or price <= 0:
                    continue
                rows.append(
                    {
                        "AlertId": f"discord|{when.strftime('%Y-%m-%d %H:%M')}|{data['sym']}|{action}",
                        "AlertTime": when.strftime("%Y-%m-%d %H:%M:%S"),
                        "Universe": "discord_export",
                        "Symbol": str(data["sym"]).strip().upper(),
                        "SignalAction": action,
                        "Direction": 1,
                        "EntryPrice": price,
                        "TriggerPrice": price,
                        "StopLoss": stop,
                        "Target1": t1,
                        "Target2": t2,
                        "SuggestedQty": pd.NA,
                        "SignalStrengthPct": pd.NA,
                        "Checks": "",
                        "Why": body.splitlines()[0][:200] if body else "",
                    }
                )
                matched_any = True
        if not matched_any:
            continue

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["AlertTime"] = pd.to_datetime(df["AlertTime"], errors="coerce")
    df["AlertDate"] = df["AlertTime"].dt.date
    df["EntryPrice"] = pd.to_numeric(df["EntryPrice"], errors="coerce")
    return df.drop_duplicates(subset=["AlertId"], keep="first")


def filter_buy_alerts(df: pd.DataFrame) -> pd.DataFrame:
    buy_df = df[df["SignalAction"].astype(str).isin(BUY_ACTIONS)].copy()
    buy_df = buy_df[buy_df["EntryPrice"].fillna(0) > 0].copy()
    buy_df = buy_df[buy_df["AlertDate"].notna()].copy()
    return buy_df


def load_daily_history(symbol: str, cache: Dict[str, Optional[pd.DataFrame]]) -> Optional[pd.DataFrame]:
    if symbol in cache:
        return cache[symbol]
    path = DAY_CACHE_DIR / f"{symbol}.csv"
    if not path.exists():
        cache[symbol] = None
        return None
    try:
        df = pd.read_csv(path)
        if df.empty or "datetime" not in df.columns:
            cache[symbol] = None
            return None
        df["date"] = pd.to_datetime(df["datetime"], errors="coerce").dt.date
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "close"])
        cache[symbol] = df.sort_values("date").reset_index(drop=True)
        return cache[symbol]
    except Exception:
        cache[symbol] = None
        return None


def future_close_return(
    day_df: pd.DataFrame,
    alert_date,
    entry_price: float,
    future_days: int,
) -> Optional[float]:
    if day_df is None or day_df.empty or entry_price <= 0:
        return None
    future_df = day_df[day_df["date"] > alert_date]
    if future_df.empty or len(future_df) < future_days:
        return None
    exit_row = future_df.iloc[future_days - 1]
    exit_price = _safe_float(exit_row.get("close"))
    if exit_price is None or exit_price <= 0:
        return None
    return round(((exit_price - entry_price) / entry_price) * 100, 2)


def horizon_drawdown(
    day_df: pd.DataFrame,
    alert_date,
    entry_price: float,
    future_days: int,
) -> Tuple[Optional[float], Optional[float]]:
    if day_df is None or day_df.empty or entry_price <= 0:
        return None, None
    window_df = day_df[day_df["date"] > alert_date].head(future_days)
    if window_df.empty:
        return None, None
    min_low = pd.to_numeric(window_df.get("low"), errors="coerce").dropna()
    max_high = pd.to_numeric(window_df.get("high"), errors="coerce").dropna()
    drawdown = None
    runup = None
    if not min_low.empty:
        drawdown = round(((min_low.min() - entry_price) / entry_price) * 100, 2)
    if not max_high.empty:
        runup = round(((max_high.max() - entry_price) / entry_price) * 100, 2)
    return drawdown, runup


def extend_horizons(df: pd.DataFrame, max_horizon_days: Iterable[int]) -> pd.DataFrame:
    cache: Dict[str, Optional[pd.DataFrame]] = {}
    rows: List[Dict] = []

    for _, row in df.iterrows():
        symbol = str(row.get("Symbol", "")).strip().upper()
        alert_date = row.get("AlertDate")
        entry_price = _safe_float(row.get("EntryPrice"))
        if not symbol or alert_date is None or entry_price is None or entry_price <= 0:
            continue

        day_df = load_daily_history(symbol, cache)
        enriched = row.to_dict()
        for horizon in max_horizon_days:
            ret = future_close_return(day_df, alert_date, entry_price, horizon)
            dd, ru = horizon_drawdown(day_df, alert_date, entry_price, horizon)
            enriched[f"Return{horizon}D"] = ret
            enriched[f"Drawdown{horizon}D"] = dd
            enriched[f"Runup{horizon}D"] = ru
        rows.append(enriched)

    return pd.DataFrame(rows)


def summarize_horizon(df: pd.DataFrame, column: str, dd_column: Optional[str] = None) -> Dict[str, float]:
    series = pd.to_numeric(df[column], errors="coerce").dropna()
    if series.empty:
        return {
            "Samples": 0,
            "WinRatePct": 0.0,
            "AvgReturnPct": 0.0,
            "MedianReturnPct": 0.0,
            "P25ReturnPct": 0.0,
            "P75ReturnPct": 0.0,
            "AvgDrawdownPct": 0.0,
        }
    out = {
        "Samples": int(len(series)),
        "WinRatePct": round(float((series > 0).mean() * 100), 2),
        "AvgReturnPct": round(float(series.mean()), 3),
        "MedianReturnPct": round(float(series.median()), 3),
        "P25ReturnPct": round(float(series.quantile(0.25)), 3),
        "P75ReturnPct": round(float(series.quantile(0.75)), 3),
        "AvgDrawdownPct": 0.0,
    }
    if dd_column and dd_column in df.columns:
        dd = pd.to_numeric(df[dd_column], errors="coerce").dropna()
        if not dd.empty:
            out["AvgDrawdownPct"] = round(float(dd.mean()), 3)
    return out


def classify_best_horizon(row: pd.Series) -> str:
    horizon_candidates = [
        ("15m", _safe_float(row.get("Return15mPct"))),
        ("60m", _safe_float(row.get("Return60mPct"))),
        ("EOD", _safe_float(row.get("ReturnEODPct"))),
        ("2D", _safe_float(row.get("Return2D"))),
        ("5D", _safe_float(row.get("Return5D"))),
        ("10D", _safe_float(row.get("Return10D"))),
        ("20D", _safe_float(row.get("Return20D"))),
        ("60D", _safe_float(row.get("Return60D"))),
    ]
    horizon_candidates = [(k, v) for k, v in horizon_candidates if v is not None]
    if not horizon_candidates:
        return "Unknown"
    best_key, best_ret = max(horizon_candidates, key=lambda item: item[1])
    if best_ret <= 0:
        return "Avoid"
    return HORIZON_LABELS.get(best_key, best_key)


def build_summary_tables(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    horizon_map = [
        ("15m", "Return15mPct", None),
        ("60m", "Return60mPct", None),
        ("EOD", "ReturnEODPct", None),
        ("1D", "ReturnNextDayPct", None),
        ("2D", "Return2D", "Drawdown2D"),
        ("5D", "Return5D", "Drawdown5D"),
        ("10D", "Return10D", "Drawdown10D"),
        ("20D", "Return20D", "Drawdown20D"),
        ("60D", "Return60D", "Drawdown60D"),
    ]
    for label, column, dd_column in horizon_map:
        summary = summarize_horizon(df, column, dd_column)
        summary["Horizon"] = HORIZON_LABELS[label]
        rows.append(summary)

    horizon_df = pd.DataFrame(rows)[
        [
            "Horizon",
            "Samples",
            "WinRatePct",
            "AvgReturnPct",
            "MedianReturnPct",
            "P25ReturnPct",
            "P75ReturnPct",
            "AvgDrawdownPct",
        ]
    ]

    classified = df.copy()
    classified["BestHorizon"] = classified.apply(classify_best_horizon, axis=1)
    class_summary = (
        classified.groupby("BestHorizon")
        .agg(
            Alerts=("Symbol", "count"),
            Symbols=("Symbol", "nunique"),
            AvgConfidence=("SignalStrengthPct", "mean"),
        )
        .reset_index()
        .sort_values(["Alerts", "Symbols"], ascending=[False, False])
    )
    class_summary["AvgConfidence"] = class_summary["AvgConfidence"].round(2)
    return horizon_df, class_summary


def estimate_capital_table(horizon_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    targets = [50000, 100000, 200000]
    for _, row in horizon_df.iterrows():
        avg_return = _safe_float(row.get("AvgReturnPct"), 0.0) or 0.0
        if avg_return <= 0:
            continue
        for target in targets:
            capital = target / (avg_return / 100.0)
            rows.append(
                {
                    "Horizon": row["Horizon"],
                    "TargetMonthlyIncome": target,
                    "CapitalNeeded": round(capital, 2),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-horizon alert backtest using the saved signal ledger.")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Path to signalperf ledger CSV")
    parser.add_argument("--discord-export", default=str(DEFAULT_DISCORD_EXPORT), help="Optional Discord export text file")
    parser.add_argument("--universe", default="allstocks", help="Used only for output tags")
    parser.add_argument("--export-prefix", default=str(DATA_DIR / "multi_horizon_alerts"), help="Output path prefix")
    args = parser.parse_args()

    ledger_path = Path(args.ledger)
    raw_df = load_ledger(ledger_path)
    buy_df = filter_buy_alerts(raw_df)
    discord_path = Path(args.discord_export)
    discord_df = parse_discord_export(discord_path)
    if not discord_df.empty:
        discord_buy_df = filter_buy_alerts(discord_df)
        buy_df = pd.concat([buy_df, discord_buy_df], ignore_index=True, sort=False)
        if "AlertId" in buy_df.columns:
            buy_df = buy_df.drop_duplicates(subset=["AlertId"], keep="first")

    enriched_df = extend_horizons(buy_df, max_horizon_days=[2, 5, 10, 20, 60])
    if enriched_df.empty:
        raise SystemExit("No buy alerts with usable data found.")

    horizon_df, class_df = build_summary_tables(enriched_df)
    capital_df = estimate_capital_table(horizon_df)

    prefix = Path(args.export_prefix)
    enriched_path = prefix.with_name(prefix.name + "_enriched.csv")
    horizon_path = prefix.with_name(prefix.name + "_summary.csv")
    class_path = prefix.with_name(prefix.name + "_classification.csv")
    capital_path = prefix.with_name(prefix.name + "_capital.csv")

    enriched_df.to_csv(enriched_path, index=False)
    horizon_df.to_csv(horizon_path, index=False)
    class_df.to_csv(class_path, index=False)
    capital_df.to_csv(capital_path, index=False)

    print(f"Buy alerts analyzed: {len(enriched_df)}")
    print(f"Unique symbols: {enriched_df['Symbol'].nunique()}")
    print("\nHorizon summary:")
    print(horizon_df.to_string(index=False))
    print("\nBest-horizon classification:")
    print(class_df.to_string(index=False))
    if not capital_df.empty:
        print("\nNaive capital table (single-trade compounding assumption, for scale only):")
        print(capital_df.to_string(index=False))
    print(f"\nSaved: {enriched_path}")
    print(f"Saved: {horizon_path}")
    print(f"Saved: {class_path}")
    print(f"Saved: {capital_path}")


if __name__ == "__main__":
    main()

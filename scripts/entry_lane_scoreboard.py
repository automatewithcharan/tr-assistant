#!/usr/bin/env python3
"""
Build a lane-wise scoreboard for Discord buy alerts.

The goal is to answer:
- Are we early enough?
- Which lane is making money?
- What should have been auto-buys vs alert-only?

This script parses the Discord export, classifies each actionable alert into
entry lanes (`early`, `continuation`, `stretched`), then extends the alert
through cached daily history to measure 2-day and 5-day outcomes.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "results" / "Data"
DAY_CACHE_DIR = DATA_DIR / "kite_history_cache" / "day"
DEFAULT_DISCORD_EXPORT = BASE_DIR / "Screener Alerts - Text Channels - general [1488777695750914060].txt"


BOT_BLOCK_RE = re.compile(r"^\[(\d{2}-\d{2}-\d{4} \d{2}:\d{2})\] Spidey Bot\s*$", re.M)

ALERT_RE = re.compile(
    r"\*\*(?:🟢 BUY|🔥 STRONG BUY|🌱 EARLY ENTRY|🔍 MISSED EARLIER, NOW STRONG)\s+—\s+`(?P<sym>[^`]+)`.*?"
    r"Price now:\s*`₹(?P<price>[\d.]+)`\s*\((?P<move>[+\-]?[\d.]+)%.*?\).*?"
    r"Volume:\s*`(?P<vol>[\d.]+)x`.*?\|\s*`(?P<conf>[\d.]+)%` indicators agree.*?"
    r"EXIT if price drops to\s*`₹(?P<stop>[\d.]+)`.*?"
    r"SELL at\s*`₹(?P<t1>[\d.]+)`.*?or\s*`₹(?P<t2>[\d.]+)`",
    re.S,
)

BUY_ABOVE_RE = re.compile(r"Buy Above\s*`₹(?P<buy_above>[\d.]+)`")


def _safe_float(value, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, "", "None"):
            return default
        num = float(value)
        if num != num:
            return default
        return num
    except Exception:
        return default


def iter_bot_blocks(text: str) -> Iterable[Tuple[datetime, str]]:
    matches = list(BOT_BLOCK_RE.finditer(text))
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        when = datetime.strptime(match.group(1), "%d-%m-%Y %H:%M")
        yield when, text[start:end].strip()


def parse_discord_buy_alerts(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Discord export not found: {path}")

    rows: List[Dict] = []
    text = path.read_text(errors="ignore")
    for when, body in iter_bot_blocks(text):
        for match in ALERT_RE.finditer(body):
            gd = match.groupdict()
            buy_above_match = BUY_ABOVE_RE.search(body[match.start(): match.end() + 200])
            price = _safe_float(gd.get("price"))
            stop = _safe_float(gd.get("stop"))
            t1 = _safe_float(gd.get("t1"))
            t2 = _safe_float(gd.get("t2"))
            move = _safe_float(gd.get("move"), 0.0) or 0.0
            vol = _safe_float(gd.get("vol"), 0.0) or 0.0
            conf = _safe_float(gd.get("conf"), 0.0) or 0.0
            buy_above = _safe_float(buy_above_match.group("buy_above") if buy_above_match else price, price) or price
            if not price or not stop or not t1 or not t2:
                continue
            rows.append(
                {
                    "alert_time": when,
                    "alert_date": when.date(),
                    "symbol": str(gd["sym"]).strip().upper(),
                    "entry_price": price,
                    "buy_above": buy_above,
                    "stop_loss": stop,
                    "target1": t1,
                    "target2": t2,
                    "day_move_pct": move,
                    "volume_ratio": vol,
                    "confidence": conf,
                    "body": body[:500],
                }
            )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.drop_duplicates(subset=["alert_time", "symbol"], keep="first").sort_values("alert_time")


def _compute_rr2(entry_price: float, stop_loss: float, target2: float) -> float:
    if entry_price <= 0 or stop_loss <= 0 or target2 <= 0 or entry_price <= stop_loss:
        return 0.0
    return round((target2 - entry_price) / max(entry_price - stop_loss, 0.01), 2)


def _entry_gap_pct(entry_price: float, buy_above: float) -> float:
    if entry_price <= 0 or buy_above <= 0:
        return 0.0
    return round(((buy_above - entry_price) / entry_price) * 100, 2)


def classify_lane(row: pd.Series) -> Tuple[str, str]:
    rr2 = _compute_rr2(row.entry_price, row.stop_loss, row.target2)
    gap_pct = _entry_gap_pct(row.entry_price, row.buy_above)
    move = float(row.day_move_pct)
    vol = float(row.volume_ratio)
    conf = float(row.confidence)
    if rr2 < 1.4 or conf < 20 or vol < 1.4:
        return "alert_only", "below minimum quality for execution"
    if move <= 2.6 and gap_pct <= 0.8 and vol >= 1.8 and rr2 >= 1.5:
        return "early", "cleaner early breakout window"
    if move <= 6.2 and gap_pct <= 1.0 and vol >= 2.0 and rr2 >= 1.8:
        return "continuation", "late but still actionable continuation"
    return "stretched", "already stretched / chasey entry"


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
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
        cache[symbol] = df
        return df
    except Exception:
        cache[symbol] = None
        return None


def future_close_return(day_df: pd.DataFrame, alert_date, entry_price: float, future_days: int) -> Optional[float]:
    if day_df is None or day_df.empty or entry_price <= 0:
        return None
    future_df = day_df[day_df["date"] > alert_date]
    if len(future_df) < future_days:
        return None
    exit_price = _safe_float(future_df.iloc[future_days - 1].get("close"))
    if exit_price is None or exit_price <= 0:
        return None
    return round(((exit_price - entry_price) / entry_price) * 100, 2)


def horizon_drawdown(day_df: pd.DataFrame, alert_date, entry_price: float, future_days: int) -> Tuple[Optional[float], Optional[float]]:
    if day_df is None or day_df.empty or entry_price <= 0:
        return None, None
    window_df = day_df[day_df["date"] > alert_date].head(future_days)
    if window_df.empty:
        return None, None
    lows = pd.to_numeric(window_df.get("low"), errors="coerce").dropna()
    highs = pd.to_numeric(window_df.get("high"), errors="coerce").dropna()
    drawdown = round(((lows.min() - entry_price) / entry_price) * 100, 2) if not lows.empty else None
    runup = round(((highs.max() - entry_price) / entry_price) * 100, 2) if not highs.empty else None
    return drawdown, runup


def extend_alerts(df: pd.DataFrame) -> pd.DataFrame:
    cache: Dict[str, Optional[pd.DataFrame]] = {}
    rows: List[Dict] = []
    for _, row in df.iterrows():
        hist = load_daily_history(row.symbol, cache)
        enriched = row.to_dict()
        for horizon in (2, 5):
            ret = future_close_return(hist, row.alert_date, row.entry_price, horizon)
            dd, ru = horizon_drawdown(hist, row.alert_date, row.entry_price, horizon)
            enriched[f"return_{horizon}d_pct"] = ret
            enriched[f"drawdown_{horizon}d_pct"] = dd
            enriched[f"runup_{horizon}d_pct"] = ru
        rows.append(enriched)
    return pd.DataFrame(rows)


def summarize_by_lane(df: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict] = []
    for lane, lane_df in df.groupby("entry_lane", dropna=False):
        lane_df = lane_df.copy()
        r2 = pd.to_numeric(lane_df["return_2d_pct"], errors="coerce").dropna()
        r5 = pd.to_numeric(lane_df["return_5d_pct"], errors="coerce").dropna()
        rows.append(
            {
                "EntryLane": lane,
                "Alerts": int(len(lane_df)),
                "Symbols": int(lane_df["symbol"].nunique()),
                "AvgDayMovePct": round(pd.to_numeric(lane_df["day_move_pct"], errors="coerce").mean(), 3),
                "AvgVolumeRatio": round(pd.to_numeric(lane_df["volume_ratio"], errors="coerce").mean(), 3),
                "AvgConfidence": round(pd.to_numeric(lane_df["confidence"], errors="coerce").mean(), 3),
                "TwoDaySamples": int(r2.shape[0]),
                "TwoDayWinRatePct": round((r2.gt(0).mean() * 100) if not r2.empty else 0.0, 2),
                "TwoDayAvgReturnPct": round(r2.mean(), 3) if not r2.empty else 0.0,
                "FiveDaySamples": int(r5.shape[0]),
                "FiveDayWinRatePct": round((r5.gt(0).mean() * 100) if not r5.empty else 0.0, 2),
                "FiveDayAvgReturnPct": round(r5.mean(), 3) if not r5.empty else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("EntryLane")


def summarize_lane_horizon(df: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict] = []
    for lane, lane_df in df.groupby("entry_lane", dropna=False):
        for label, col in (("2-day swing", "return_2d_pct"), ("5-day swing", "return_5d_pct")):
            series = pd.to_numeric(lane_df[col], errors="coerce").dropna()
            if series.empty:
                continue
            rows.append(
                {
                    "EntryLane": lane,
                    "Horizon": label,
                    "Samples": int(series.shape[0]),
                    "WinRatePct": round(series.gt(0).mean() * 100, 2),
                    "AvgReturnPct": round(series.mean(), 3),
                    "MedianReturnPct": round(series.median(), 3),
                    "P25ReturnPct": round(series.quantile(0.25), 3),
                    "P75ReturnPct": round(series.quantile(0.75), 3),
                }
            )
    return pd.DataFrame(rows)


def top_examples(df: pd.DataFrame, lane: str, horizon_col: str, n: int = 10, ascending: bool = False) -> pd.DataFrame:
    lane_df = df[df["entry_lane"] == lane].copy()
    if lane_df.empty or horizon_col not in lane_df.columns:
        return pd.DataFrame()
    lane_df[horizon_col] = pd.to_numeric(lane_df[horizon_col], errors="coerce")
    lane_df = lane_df.dropna(subset=[horizon_col])
    cols = ["alert_time", "symbol", "day_move_pct", "volume_ratio", "confidence", "entry_lane", "entry_reason", horizon_col]
    return lane_df.sort_values(horizon_col, ascending=ascending)[cols].head(n)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a lane-wise scoreboard for Discord buy alerts.")
    parser.add_argument("--discord-export", type=Path, default=DEFAULT_DISCORD_EXPORT)
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()

    alerts = parse_discord_buy_alerts(args.discord_export)
    if alerts.empty:
        raise SystemExit("No parseable buy alerts found in Discord export.")

    alerts[["entry_lane", "entry_reason"]] = alerts.apply(
        lambda row: pd.Series(classify_lane(row)),
        axis=1,
    )
    enriched = extend_alerts(alerts)
    summary = summarize_by_lane(enriched)
    lane_horizon = summarize_lane_horizon(enriched)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    enriched_path = output_dir / "entry_lane_scoreboard_enriched.csv"
    summary_path = output_dir / "entry_lane_scoreboard_summary.csv"
    lane_horizon_path = output_dir / "entry_lane_scoreboard_lane_horizon.csv"
    winners_path = output_dir / "entry_lane_scoreboard_best_continuations.csv"
    misses_path = output_dir / "entry_lane_scoreboard_stretched_winners.csv"

    enriched.to_csv(enriched_path, index=False)
    summary.to_csv(summary_path, index=False)
    lane_horizon.to_csv(lane_horizon_path, index=False)
    top_examples(enriched, "continuation", "return_5d_pct", n=20, ascending=False).to_csv(winners_path, index=False)
    top_examples(enriched, "stretched", "return_5d_pct", n=20, ascending=False).to_csv(misses_path, index=False)

    print("Saved:")
    print(f"  {summary_path}")
    print(f"  {lane_horizon_path}")
    print(f"  {enriched_path}")
    print(f"  {winners_path}")
    print(f"  {misses_path}")
    print()
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()

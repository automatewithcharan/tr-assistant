#!/usr/bin/env python3
"""
Fresh Kite minute replay for the early-engine auto-buy lane.

This script deliberately does not read saved signal-monitor snapshots.  It
fetches official Kite daily/minute candles, replays each minute in timestamp
order, and only uses data that would have been known at that minute.

It is not a profit guarantee.  The point is to make the assumptions visible
and to stop trusting stale local boards when tuning live auto-buy rules.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider


RESULTS_DIR = BASE_DIR / "results" / "Data"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
FRESH_CACHE_DIR = RESULTS_DIR / "fresh_early_engine_cache"
FRESH_CACHE_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE_FILES = {
    "nifty500": BASE_DIR / "results" / "Indices" / "ind_nifty500list.csv",
    "all": BASE_DIR / "results" / "Indices" / "NSE_BSE_COMBINED.csv",
    "nse": BASE_DIR / "results" / "Indices" / "EQUITY_L.csv",
}

MARKET_OPEN = dtime(9, 15)
ENTRY_START = dtime(9, 30)
ENTRY_END = dtime(11, 30)
EOD_EXIT = dtime(15, 15)
API_SLEEP = 0.36
ROUND_TRIP_COST = 0.0025


@dataclass(frozen=True)
class StrategyConfig:
    min_price: float = 20.0
    min_change: float = 1.0
    max_change: float = 4.5
    min_turnover_cr: float = 5.0
    min_expected_vol_ratio: float = 3.0
    min_rr: float = 1.5
    near_high_pct: float = 0.7
    min_avg_turnover_cr: float = 2.0
    max_gap_pct: float = 0.5
    min_orb_range_pct: float = 0.2
    max_orb_range_pct: float = 2.5
    max_risk_pct: float = 2.2
    min_risk_pct: float = 0.8
    target_r_multiple: float = 1.8
    capital: float = 100_000.0
    per_trade_cap: float = 25_000.0
    max_open_positions: int = 4
    max_trades_per_day: int = 6


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fresh-data early-engine minute replay")
    parser.add_argument("--universe", choices=sorted(UNIVERSE_FILES), default="nifty500")
    parser.add_argument("--days", type=int, default=5, help="Recent calendar days to test")
    parser.add_argument("--from-date", type=str, default=None)
    parser.add_argument("--to-date", type=str, default=None)
    parser.add_argument("--max-symbols", type=int, default=250)
    parser.add_argument("--top-liquidity", type=int, default=250)
    parser.add_argument(
        "--per-day-candidates",
        type=int,
        default=0,
        help="Keep the top N daily/liquidity candidates per trading day before minute replay.",
    )
    parser.add_argument("--limit-minute-fetches", type=int, default=600)
    parser.add_argument("--out-prefix", type=str, default="fresh_early_engine")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def load_universe(name: str, max_symbols: int) -> list[str]:
    path = UNIVERSE_FILES[name]
    df = pd.read_csv(path)
    if "Symbol" in df.columns:
        symbols = df["Symbol"]
    elif "SYMBOL" in df.columns:
        symbols = df["SYMBOL"]
    else:
        symbols = df.iloc[:, 0]
    clean = []
    seen = set()
    for value in symbols:
        symbol = str(value).strip().upper()
        if not symbol or symbol in seen or symbol == "SYMBOL":
            continue
        seen.add(symbol)
        clean.append(symbol)
        if max_symbols and len(clean) >= max_symbols:
            break
    return clean


def date_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.to_date:
        to_day = datetime.strptime(args.to_date, "%Y-%m-%d").date()
    else:
        to_day = date.today()
    if args.from_date:
        from_day = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    else:
        from_day = to_day - timedelta(days=max(args.days, 1) + 2)
    return datetime.combine(from_day, MARKET_OPEN), datetime.combine(to_day, dtime(15, 30))


def fetch_daily(provider: KiteHistoricalDataProvider, symbol: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
    cache_path = FRESH_CACHE_DIR / f"{symbol}_day_{start.date()}_{end.date()}.csv"
    if cache_path.exists():
        try:
            cached = pd.read_csv(cache_path, parse_dates=["dt"])
            cached["day"] = pd.to_datetime(cached["day"]).dt.date
            return cached
        except Exception:
            pass
    lookback_start = start - timedelta(days=45)
    df = provider.get_history_range(symbol, "day", lookback_start, end)
    time.sleep(API_SLEEP)
    if df is None or df.empty:
        return None
    df = df.reset_index().rename(columns={"datetime": "dt"})
    df["day"] = pd.to_datetime(df["dt"]).dt.date
    out = df.sort_values("dt").reset_index(drop=True)
    out.to_csv(cache_path, index=False)
    return out


def fetch_minutes(provider: KiteHistoricalDataProvider, symbol: str, day: date) -> Optional[pd.DataFrame]:
    cache_path = FRESH_CACHE_DIR / f"{symbol}_{day}_minute.json"
    if cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text())
            out = pd.DataFrame(raw)
            out["dt"] = pd.to_datetime(out["dt"])
            out["t"] = out["dt"].dt.time
            return out
        except Exception:
            pass
    start = datetime.combine(day, MARKET_OPEN)
    end = datetime.combine(day, dtime(15, 30))
    df = provider.get_history_range(symbol, "minute", start, end)
    time.sleep(API_SLEEP)
    if df is None or df.empty or len(df) < 20:
        return None
    out = df.reset_index().rename(columns={"datetime": "dt"})
    out["dt"] = pd.to_datetime(out["dt"])
    out["t"] = out["dt"].dt.time
    out = out.sort_values("dt").reset_index(drop=True)
    serializable = out.copy()
    serializable["dt"] = serializable["dt"].astype(str)
    serializable["t"] = serializable["t"].astype(str)
    cache_path.write_text(serializable.to_json(orient="records"))
    return out


def enrich_daily(daily: pd.DataFrame) -> pd.DataFrame:
    df = daily.copy()
    df["prev_close"] = df["close"].shift(1)
    df["avg_volume_20"] = df["volume"].rolling(20).mean().shift(1)
    df["avg_turnover_cr_20"] = ((df["close"] * df["volume"]) / 1e7).rolling(20).mean().shift(1)
    return df


def liquid_symbol_days(
    daily_by_symbol: dict[str, pd.DataFrame],
    start: datetime,
    end: datetime,
    cfg: StrategyConfig,
    top_liquidity: int,
    per_day_candidates: int = 0,
) -> list[dict]:
    rows: list[dict] = []
    for symbol, daily in daily_by_symbol.items():
        if daily is None or daily.empty:
            continue
        df = enrich_daily(daily)
        for _, row in df.iterrows():
            day = row["day"]
            if not (start.date() <= day <= end.date()):
                continue
            prev_close = float(row.get("prev_close") or 0)
            avg_vol = float(row.get("avg_volume_20") or 0)
            avg_turnover = float(row.get("avg_turnover_cr_20") or 0)
            if prev_close <= 0 or avg_vol <= 0 or avg_turnover < cfg.min_avg_turnover_cr:
                continue
            high_change = (float(row["high"]) - prev_close) / prev_close * 100
            open_gap = (float(row["open"]) - prev_close) / prev_close * 100
            if high_change < cfg.min_change or abs(open_gap) > 7.5:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "day": day,
                    "prev_close": prev_close,
                    "open_gap_pct": open_gap,
                    "avg_volume_20": avg_vol,
                    "avg_turnover_cr_20": avg_turnover,
                    "daily_high_change_pct": high_change,
                }
            )
    if per_day_candidates > 0:
        by_day: dict[date, list[dict]] = {}
        for row in rows:
            by_day.setdefault(row["day"], []).append(row)
        selected: list[dict] = []
        for day in sorted(by_day):
            day_rows = sorted(
                by_day[day],
                key=lambda item: (
                    item["daily_high_change_pct"],
                    item["avg_turnover_cr_20"],
                ),
                reverse=True,
            )
            selected.extend(day_rows[:per_day_candidates])
        rows = selected

    rows.sort(key=lambda item: (item["day"], -item["daily_high_change_pct"], -item["avg_turnover_cr_20"]))
    if top_liquidity:
        return rows[:top_liquidity]
    return rows


def elapsed_fraction(ts: dtime) -> float:
    minutes = (ts.hour * 60 + ts.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute) + 1
    return min(max(minutes / 375.0, 0.025), 1.0)


def first_signal(symbol: str, day_info: dict, minute_df: pd.DataFrame, cfg: StrategyConfig) -> Optional[dict]:
    prev_close = float(day_info["prev_close"])
    avg_vol = float(day_info["avg_volume_20"])
    open_price = float(minute_df.iloc[0]["open"])
    gap_pct = (open_price - prev_close) / prev_close * 100
    if gap_pct > cfg.max_gap_pct:
        return None
    opening = minute_df.head(15)
    if len(opening) < 15:
        return None
    orb_high = float(opening["high"].max())
    orb_low = float(opening["low"].min())
    orb_range_pct = (orb_high - orb_low) / max(open_price, 0.01) * 100
    if orb_range_pct < cfg.min_orb_range_pct or orb_range_pct > cfg.max_orb_range_pct:
        return None

    cum_vol = 0.0
    cum_pv = 0.0
    day_high = -math.inf
    day_low = math.inf

    for idx, row in minute_df.iterrows():
        t = row["t"]
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        volume = float(row["volume"])
        typical = (high + low + close) / 3.0

        cum_vol += max(volume, 0.0)
        cum_pv += typical * max(volume, 0.0)
        day_high = max(day_high, high)
        day_low = min(day_low, low)

        if t < ENTRY_START or t > ENTRY_END:
            continue
        if idx < 15:
            continue
        if close < cfg.min_price or prev_close <= 0 or cum_vol <= 0:
            continue
        if close <= orb_high:
            continue
        if close <= float(row["open"]):
            continue

        change_pct = (close - prev_close) / prev_close * 100
        if change_pct < cfg.min_change or change_pct > cfg.max_change:
            continue

        vwap = cum_pv / cum_vol
        if close < vwap:
            continue
        if close < day_high * (1 - cfg.near_high_pct / 100.0):
            continue

        expected_vol_ratio = cum_vol / max(avg_vol * elapsed_fraction(t), 1.0)
        turnover_cr = cum_vol * close / 1e7
        if expected_vol_ratio < cfg.min_expected_vol_ratio or turnover_cr < cfg.min_turnover_cr:
            continue

        structural_stop = max(day_low, min(vwap * 0.995, close * (1 - cfg.min_risk_pct / 100.0)))
        risk = close - structural_stop
        min_risk = close * cfg.min_risk_pct / 100.0
        max_risk = close * cfg.max_risk_pct / 100.0
        if risk < min_risk:
            risk = min_risk
        if risk > max_risk:
            continue
        stop = close - risk
        target = close + risk * cfg.target_r_multiple
        rr = (target - close) / max(close - stop, 0.01)
        if rr < cfg.min_rr:
            continue

        return {
            "symbol": symbol,
            "day": day_info["day"].isoformat(),
            "entry_idx": int(idx),
            "entry_time": row["dt"],
            "entry": close,
            "stop": stop,
            "target": target,
            "risk_pct": (risk / close) * 100,
            "rr": rr,
            "change_pct": change_pct,
            "gap_pct": gap_pct,
            "vwap": vwap,
            "turnover_cr": turnover_cr,
            "expected_vol_ratio": expected_vol_ratio,
            "day_high_at_entry": day_high,
            "day_low_at_entry": day_low,
            "orb_high": orb_high,
            "orb_low": orb_low,
            "orb_range_pct": orb_range_pct,
        }
    return None


def simulate_exit(signal: dict, minute_df: pd.DataFrame, cfg: StrategyConfig) -> dict:
    entry = float(signal["entry"])
    stop = float(signal["stop"])
    target = float(signal["target"])
    qty = max(int(cfg.per_trade_cap // entry), 1)
    exit_price = float(minute_df.iloc[-1]["close"])
    exit_time = minute_df.iloc[-1]["dt"]
    exit_reason = "EOD"

    for _, row in minute_df.iloc[int(signal["entry_idx"]) + 1 :].iterrows():
        t = row["t"]
        if float(row["low"]) <= stop:
            exit_price = stop
            exit_time = row["dt"]
            exit_reason = "SL"
            break
        if float(row["high"]) >= target:
            exit_price = target
            exit_time = row["dt"]
            exit_reason = "TARGET"
            break
        if t >= EOD_EXIT:
            exit_price = float(row["close"])
            exit_time = row["dt"]
            exit_reason = "EOD"
            break

    gross = (exit_price - entry) * qty
    costs = (entry + exit_price) * qty * ROUND_TRIP_COST / 2.0
    pnl = gross - costs
    out = dict(signal)
    out.update(
        {
            "qty": qty,
            "exit_time": exit_time,
            "exit": exit_price,
            "exit_reason": exit_reason,
            "gross_pnl": gross,
            "costs": costs,
            "net_pnl": pnl,
            "return_on_trade_pct": (exit_price - entry) / entry * 100,
        }
    )
    return out


def portfolio_replay(raw_trades: Iterable[dict], cfg: StrategyConfig) -> pd.DataFrame:
    trades = sorted(raw_trades, key=lambda item: pd.Timestamp(item["entry_time"]))
    accepted: list[dict] = []
    open_positions: list[dict] = []
    day_counts: dict[str, int] = {}

    for trade in trades:
        entry_time = pd.Timestamp(trade["entry_time"])
        day = str(trade["day"])
        open_positions = [pos for pos in open_positions if pd.Timestamp(pos["exit_time"]) > entry_time]
        if len(open_positions) >= cfg.max_open_positions:
            continue
        if day_counts.get(day, 0) >= cfg.max_trades_per_day:
            continue
        deployed = sum(float(pos["entry"]) * int(pos["qty"]) for pos in open_positions)
        needed = float(trade["entry"]) * int(trade["qty"])
        if deployed + needed > cfg.capital:
            continue
        day_counts[day] = day_counts.get(day, 0) + 1
        accepted.append(trade)
        open_positions.append(trade)

    return pd.DataFrame(accepted)


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(
            [{"trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "net_pnl": 0.0, "avg_pnl": 0.0}]
        )
    wins = trades[trades["net_pnl"] > 0]
    losses = trades[trades["net_pnl"] <= 0]
    return pd.DataFrame(
        [
            {
                "trades": int(len(trades)),
                "wins": int(len(wins)),
                "losses": int(len(losses)),
                "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
                "net_pnl": round(float(trades["net_pnl"].sum()), 2),
                "avg_pnl": round(float(trades["net_pnl"].mean()), 2),
                "median_pnl": round(float(trades["net_pnl"].median()), 2),
                "target_exits": int((trades["exit_reason"] == "TARGET").sum()),
                "sl_exits": int((trades["exit_reason"] == "SL").sum()),
                "eod_exits": int((trades["exit_reason"] == "EOD").sum()),
            }
        ]
    )


def main() -> int:
    args = parse_args()
    cfg = StrategyConfig()
    start, end = date_window(args)
    provider = KiteHistoricalDataProvider()
    symbols = load_universe(args.universe, args.max_symbols)

    if not args.quiet:
        print("Fresh early-engine replay")
        print(f"  universe={args.universe} symbols={len(symbols)} window={start.date()}..{end.date()}")
        print("  assumptions: Kite fresh daily/minute candles, next-minute exits, SL checked before target inside same candle")

    daily_by_symbol: dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols, 1):
        daily = fetch_daily(provider, symbol, start, end)
        if daily is not None and not daily.empty:
            daily_by_symbol[symbol] = daily
        if not args.quiet and (i == 1 or i % 25 == 0):
            print(f"  daily fetched {i}/{len(symbols)} usable={len(daily_by_symbol)}")

    symbol_days = liquid_symbol_days(
        daily_by_symbol,
        start,
        end,
        cfg,
        args.top_liquidity,
        per_day_candidates=args.per_day_candidates,
    )
    if args.limit_minute_fetches:
        symbol_days = symbol_days[: args.limit_minute_fetches]
    if not args.quiet:
        print(f"  minute candidates={len(symbol_days)} after daily/liquidity prefilter")

    raw_trades: list[dict] = []
    checked = 0
    for item in symbol_days:
        checked += 1
        minute_df = fetch_minutes(provider, item["symbol"], item["day"])
        if minute_df is None or minute_df.empty:
            continue
        signal = first_signal(item["symbol"], item, minute_df, cfg)
        if signal:
            raw_trades.append(simulate_exit(signal, minute_df, cfg))
        if not args.quiet and (checked == 1 or checked % 25 == 0):
            print(f"  minute replay {checked}/{len(symbol_days)} raw_signals={len(raw_trades)}")

    raw_df = pd.DataFrame(raw_trades)
    trades_df = portfolio_replay(raw_trades, cfg)
    summary_df = summarize(trades_df)
    by_day_df = (
        trades_df.groupby("day", as_index=False)
        .agg(trades=("symbol", "count"), net_pnl=("net_pnl", "sum"), wins=("net_pnl", lambda s: int((s > 0).sum())))
        if not trades_df.empty
        else pd.DataFrame(columns=["day", "trades", "net_pnl", "wins"])
    )
    by_month_df = pd.DataFrame(columns=["month", "trades", "net_pnl", "wins", "win_rate_pct"])
    if not trades_df.empty:
        tmp = trades_df.copy()
        tmp["month"] = pd.to_datetime(tmp["day"]).dt.strftime("%Y-%m")
        by_month_df = tmp.groupby("month", as_index=False).agg(
            trades=("symbol", "count"),
            net_pnl=("net_pnl", "sum"),
            wins=("net_pnl", lambda s: int((s > 0).sum())),
        )
        by_month_df["win_rate_pct"] = (by_month_df["wins"] / by_month_df["trades"] * 100).round(1)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = RESULTS_DIR / f"{args.out_prefix}_raw_{stamp}.csv"
    trades_path = RESULTS_DIR / f"{args.out_prefix}_trades_{stamp}.csv"
    summary_path = RESULTS_DIR / f"{args.out_prefix}_summary_{stamp}.csv"
    by_day_path = RESULTS_DIR / f"{args.out_prefix}_by_day_{stamp}.csv"
    by_month_path = RESULTS_DIR / f"{args.out_prefix}_by_month_{stamp}.csv"
    raw_df.to_csv(raw_path, index=False)
    trades_df.to_csv(trades_path, index=False)
    summary_df.to_csv(summary_path, index=False)
    by_day_df.to_csv(by_day_path, index=False)
    by_month_df.to_csv(by_month_path, index=False)

    print("\nSummary")
    print(summary_df.to_string(index=False))
    if not by_day_df.empty:
        print("\nBy day")
        print(by_day_df.to_string(index=False))
    if not by_month_df.empty:
        print("\nBy month")
        print(by_month_df.to_string(index=False))
    print("\nFiles")
    print(f"  raw:     {raw_path}")
    print(f"  trades:  {trades_path}")
    print(f"  summary: {summary_path}")
    print(f"  by_day:  {by_day_path}")
    print(f"  month:   {by_month_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

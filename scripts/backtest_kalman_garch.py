#!/usr/bin/env python3
"""
Backtest: Kalman+GARCH Signal Quality Filter vs Old System

Replays the last 60 trading days of cached daily data for all stocks.
For each day, simulates:
  OLD: Buy any stock where basic signal fires (vol>=1x, change>0)
  NEW: Buy only when Kalman quality>=40, trend!="down", no noise spike, GARCH vol regime OK

Measures:
  - Win rate (entry → next-day close)
  - Win rate (entry → 3-day close)
  - Average return
  - P&L on ₹1L capital with 20% per-trade allocation
"""

import csv
import os
import sys
import math
from collections import defaultdict
from typing import Dict, List, Tuple

import numpy as np

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkscreener.classes.AdvancedFilters import SignalQualityFilter, GARCHVolatility

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "results", "Data", "kite_history_cache", "day")

# Capital config
CAPITAL = 100000  # ₹1L
ALLOCATION_PCT = 20  # 20% per trade
MAX_POSITIONS = 5
POSITION_SIZE = CAPITAL * ALLOCATION_PCT / 100

# Minimum bars of lookback for Kalman/GARCH
MIN_LOOKBACK = 60


def load_all_stocks() -> Dict[str, List[dict]]:
    stocks = {}
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".csv"):
            continue
        symbol = fname.replace(".csv", "")
        candles = []
        try:
            with open(os.path.join(DATA_DIR, fname)) as f:
                for row in csv.DictReader(f):
                    candles.append({
                        "date": row["datetime"][:10],
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(float(row["volume"])),
                    })
        except Exception:
            continue
        if len(candles) >= MIN_LOOKBACK + 5:
            stocks[symbol] = candles
    return stocks


def compute_rsi(closes: List[float], period=14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def compute_vol_ratio(volumes: List[int], i: int, period=20) -> float:
    if i < period + 1:
        return 1.0
    avg = sum(volumes[i - period:i]) / period
    return volumes[i] / avg if avg > 0 else 1.0


def compute_atr(candles: List[dict], i: int, period=14) -> float:
    if i < period:
        return candles[i]["close"] * 0.02
    trs = []
    for j in range(i - period + 1, i + 1):
        h = candles[j]["high"]
        l = candles[j]["low"]
        pc = candles[j - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else candles[i]["close"] * 0.02


def old_signal_fires(candles: List[dict], i: int) -> bool:
    """Old system: basic signal — volume above avg, positive change, RSI not extreme."""
    closes = [c["close"] for c in candles[:i + 1]]
    volumes = [c["volume"] for c in candles[:i + 1]]

    change_pct = (candles[i]["close"] - candles[i - 1]["close"]) / candles[i - 1]["close"] * 100
    vol_ratio = compute_vol_ratio(volumes, i)
    rsi = compute_rsi(closes[-30:])

    return (
        change_pct > 0
        and vol_ratio >= 1.0
        and 30 < rsi < 75
        and candles[i]["volume"] > 50000
    )


def new_signal_fires(
    candles: List[dict],
    i: int,
    sqf: SignalQualityFilter,
    garch: GARCHVolatility,
) -> Tuple[bool, dict]:
    """New system: old gates + Kalman quality + GARCH regime."""
    closes = [c["close"] for c in candles[:i + 1]]
    volumes = [c["volume"] for c in candles[:i + 1]]

    change_pct = (candles[i]["close"] - candles[i - 1]["close"]) / candles[i - 1]["close"] * 100
    vol_ratio = compute_vol_ratio(volumes, i)
    rsi = compute_rsi(closes[-30:])

    # Basic gates (same as old but tighter volume)
    if not (change_pct > 0 and vol_ratio >= 1.3 and 30 < rsi < 70 and candles[i]["volume"] > 50000):
        return False, {}

    # Kalman + GARCH evaluation
    close_arr = np.array(closes[-MIN_LOOKBACK:], dtype=float)
    sq = sqf.evaluate(close_arr)

    kalman_quality = sq.get("quality_score", 50)
    kalman_trend = sq.get("kalman_trend", "flat")
    vol_regime = sq.get("vol_regime", "normal")
    noise_spike = sq.get("noise_spike", False)

    # Quality gates
    if kalman_quality < 40:
        return False, sq
    if kalman_trend == "down":
        return False, sq
    if noise_spike and kalman_quality < 50:
        return False, sq
    if vol_regime == "extreme":
        return False, sq

    # GARCH adaptive stop
    returns = np.diff(close_arr) / close_arr[:-1]
    atr_mult = garch.adaptive_atr_multiplier(returns, base_multiplier=2.0)

    sq["vol_ratio"] = vol_ratio
    sq["change_pct"] = change_pct
    sq["atr_mult"] = atr_mult
    return True, sq


def simulate_trade(candles: List[dict], entry_idx: int, hold_days: int, atr_mult: float = 2.0) -> dict:
    """Simulate a trade: entry at close[entry_idx], exit at close[entry_idx+hold_days]."""
    entry = candles[entry_idx]["close"]
    atr = compute_atr(candles, entry_idx)
    stop_loss = entry - atr * atr_mult * 0.75

    exit_idx = min(entry_idx + hold_days, len(candles) - 1)
    if exit_idx <= entry_idx:
        return {"return_pct": 0.0, "sl_hit": False, "exit_price": entry}

    # Check if SL hit during hold
    sl_hit = False
    exit_price = candles[exit_idx]["close"]
    for j in range(entry_idx + 1, exit_idx + 1):
        if candles[j]["low"] <= stop_loss:
            sl_hit = True
            exit_price = stop_loss
            break

    return_pct = (exit_price - entry) / entry * 100
    return {
        "return_pct": return_pct,
        "sl_hit": sl_hit,
        "exit_price": exit_price,
        "entry_price": entry,
    }


def run_backtest():
    print("Loading stock data...")
    stocks = load_all_stocks()
    print(f"Loaded {len(stocks)} stocks with >={MIN_LOOKBACK + 5} candles\n")

    sqf = SignalQualityFilter()
    garch_inst = GARCHVolatility()

    # Get common trading dates (last 60 days)
    date_stocks = defaultdict(int)
    for sym, candles in stocks.items():
        for c in candles:
            date_stocks[c["date"]] += 1
    all_dates = sorted([d for d, cnt in date_stocks.items() if cnt >= 100])
    test_dates = all_dates[-60:]

    print(f"Test period: {test_dates[0]} to {test_dates[-1]} ({len(test_dates)} trading days)\n")

    # Results collectors
    for hold_days in [1, 3]:
        old_trades = []
        new_trades = []

        for date in test_dates:
            old_signals_today = []
            new_signals_today = []

            for sym, candles in stocks.items():
                date_map = {c["date"]: idx for idx, c in enumerate(candles)}
                if date not in date_map:
                    continue
                i = date_map[date]
                if i < MIN_LOOKBACK or i + hold_days >= len(candles):
                    continue

                # Old system
                if old_signal_fires(candles, i):
                    vol_ratio = compute_vol_ratio(
                        [c["volume"] for c in candles[:i + 1]], i
                    )
                    old_signals_today.append((vol_ratio, sym, i, 2.0))

                # New system
                fires, sq = new_signal_fires(candles, i, sqf, garch_inst)
                if fires:
                    vol_ratio = sq.get("vol_ratio", 1.0)
                    atr_mult = sq.get("atr_mult", 2.0)
                    new_signals_today.append((vol_ratio, sym, i, atr_mult))

            # Take top MAX_POSITIONS by volume ratio
            old_signals_today.sort(reverse=True)
            new_signals_today.sort(reverse=True)

            for _, sym, i, atr_mult in old_signals_today[:MAX_POSITIONS]:
                result = simulate_trade(stocks[sym], i, hold_days, atr_mult=2.0)
                result["symbol"] = sym
                result["date"] = date
                old_trades.append(result)

            for _, sym, i, atr_mult in new_signals_today[:MAX_POSITIONS]:
                result = simulate_trade(stocks[sym], i, hold_days, atr_mult=atr_mult)
                result["symbol"] = sym
                result["date"] = date
                new_trades.append(result)

        # Print results
        print(f"\n{'=' * 70}")
        print(f"HOLD {hold_days}D BACKTEST — {test_dates[0]} to {test_dates[-1]}")
        print(f"{'=' * 70}")

        for label, trades in [("OLD SYSTEM", old_trades), ("NEW (Kalman+GARCH)", new_trades)]:
            if not trades:
                print(f"\n  {label}: No trades")
                continue

            returns = [t["return_pct"] for t in trades]
            winners = [r for r in returns if r > 0]
            losers = [r for r in returns if r <= 0]
            win_rate = len(winners) / len(returns) * 100 if returns else 0

            gross_profit = sum(winners) if winners else 0
            gross_loss = abs(sum(losers)) if losers else 0
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

            # P&L simulation
            total_pnl = sum(POSITION_SIZE * r / 100 for r in returns)
            trades_per_day = len(trades) / len(test_dates)
            daily_pnl = total_pnl / len(test_dates)
            monthly_pnl = daily_pnl * 22  # ~22 trading days

            sl_hits = sum(1 for t in trades if t.get("sl_hit"))

            print(f"\n  {label}:")
            print(f"    Total Trades:    {len(trades)} ({trades_per_day:.1f}/day)")
            print(f"    Win Rate:        {win_rate:.1f}% ({len(winners)}/{len(trades)})")
            print(f"    Avg Return:      {np.mean(returns):+.2f}%")
            print(f"    Avg Winner:      {np.mean(winners):+.2f}%" if winners else "    Avg Winner:      N/A")
            print(f"    Avg Loser:       {np.mean(losers):+.2f}%" if losers else "    Avg Loser:       N/A")
            print(f"    Best Trade:      {max(returns):+.2f}%")
            print(f"    Worst Trade:     {min(returns):+.2f}%")
            print(f"    Profit Factor:   {profit_factor:.2f}")
            print(f"    SL Hits:         {sl_hits}/{len(trades)} ({sl_hits / len(trades) * 100:.0f}%)")
            print(f"    ── Capital: ₹{CAPITAL:,.0f} @ {ALLOCATION_PCT}% per trade ──")
            print(f"    Total P&L:       ₹{total_pnl:+,.0f}")
            print(f"    Daily Avg P&L:   ₹{daily_pnl:+,.0f}")
            print(f"    Monthly Est:     ₹{monthly_pnl:+,.0f}")

        # Head-to-head comparison
        if old_trades and new_trades:
            old_returns = [t["return_pct"] for t in old_trades]
            new_returns = [t["return_pct"] for t in new_trades]
            old_wr = sum(1 for r in old_returns if r > 0) / len(old_returns) * 100
            new_wr = sum(1 for r in new_returns if r > 0) / len(new_returns) * 100
            old_daily = sum(POSITION_SIZE * r / 100 for r in old_returns) / len(test_dates)
            new_daily = sum(POSITION_SIZE * r / 100 for r in new_returns) / len(test_dates)

            print(f"\n  HEAD-TO-HEAD:")
            print(f"    Win Rate:   OLD {old_wr:.1f}%  vs  NEW {new_wr:.1f}%  → {'NEW' if new_wr > old_wr else 'OLD'} wins by {abs(new_wr - old_wr):.1f}pp")
            print(f"    Daily P&L:  OLD ₹{old_daily:+,.0f}  vs  NEW ₹{new_daily:+,.0f}  → {'NEW' if new_daily > old_daily else 'OLD'} earns ₹{abs(new_daily - old_daily):,.0f} more/day")
            print(f"    Monthly:    OLD ₹{old_daily * 22:+,.0f}  vs  NEW ₹{new_daily * 22:+,.0f}")


if __name__ == "__main__":
    run_backtest()

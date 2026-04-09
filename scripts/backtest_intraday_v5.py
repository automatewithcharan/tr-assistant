#!/usr/bin/env python3
"""
Intraday Strategy Backtest V5: Find the sweet spot for 10-20% monthly returns.

Tests MULTIPLE entry/exit strategies on 1 year of daily OHLCV data:

Strategy variants tested:
  A) CURRENT: Buy 3%+ gap at open, no SL, sell at close  → baseline
  B) GAP + STRICT SL: Same entry but 1.5% max loss SL    → risk control
  C) GAP + TRAILING STOP: SL + move SL up as stock runs   → lock profits
  D) VOLUME MONSTER: Only vol >= 3x + trailing stop        → quality filter
  E) MARKET FILTER: Skip if "Nifty proxy" (avg broad mkt) is bearish

For each, we compute:
  - Win rate (closed above entry)
  - Avg profit per trade
  - Max drawdown per trade
  - Effective monthly return given 2-4 trades/day
  - Expectancy = (win% × avg_win) - (loss% × avg_loss)

Using daily OHLCV (open, high, low, close):
  - Entry = open price (simulates market buy at open)
  - Intraday high = best case profit (we model taking partial at +2%, +4%)
  - Intraday low = worst case drawdown (did SL trigger?)
  - Close = EOD exit price

Key insight: With daily candles, we DON'T know if high or low came first.
Conservative assumption: if low < SL, SL triggered (assume worst case).
"""
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import warnings
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "results", "Data", "kite_history_cache", "day")


# ──────────────────────────────────────────────────────────────────────
# Indicators
# ──────────────────────────────────────────────────────────────────────

def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average True Range — measures typical daily price range."""
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


def compute_cci(highs: list, lows: list, closes: list, period: int = 20) -> float:
    if len(closes) < period:
        return 0.0
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(-period, 0)]
    tp_mean = sum(tp) / period
    mean_dev = sum(abs(t - tp_mean) for t in tp) / period
    if mean_dev == 0:
        return 0.0
    return (tp[-1] - tp_mean) / (0.015 * mean_dev)


def compute_mfi(highs: list, lows: list, closes: list, volumes: list,
                period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(-period - 1, 0)]
    pos_flow = neg_flow = 0.0
    for i in range(1, period + 1):
        mf = tp[i] * volumes[-(period + 1 - i)]
        if tp[i] > tp[i - 1]:
            pos_flow += mf
        else:
            neg_flow += mf
    if neg_flow == 0:
        return 100.0
    return 100 - (100 / (1 + pos_flow / neg_flow))


def compute_vol_ratio(volumes: list, period: int = 20) -> float:
    if len(volumes) < period + 1:
        return 1.0
    avg = sum(volumes[-period - 1:-1]) / period
    if avg == 0:
        return 1.0
    return volumes[-1] / avg


# ──────────────────────────────────────────────────────────────────────
# Load all stock data
# ──────────────────────────────────────────────────────────────────────

@dataclass
class DayCandle:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


def load_all_stocks() -> Dict[str, List[DayCandle]]:
    """Load all daily OHLCV data from cache."""
    stocks = {}
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".csv"):
            continue
        symbol = fname.replace(".csv", "")
        path = os.path.join(DATA_DIR, fname)
        candles = []
        try:
            with open(path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    candles.append(DayCandle(
                        date=row["datetime"][:10],
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row["volume"])),
                    ))
        except Exception:
            continue
        if len(candles) >= 30:
            stocks[symbol] = candles
    return stocks


# ──────────────────────────────────────────────────────────────────────
# Strategy simulation
# ──────────────────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    symbol: str
    date: str
    entry: float
    exit_price: float
    pnl_pct: float
    max_drawdown_pct: float
    high_from_entry_pct: float
    strategy: str
    gap_pct: float
    vol_ratio: float
    rsi: float


def simulate_strategies(stocks: Dict[str, List[DayCandle]]) -> Dict[str, List[TradeResult]]:
    """
    For each trading day, find all 3%+ gap-ups and simulate different exit strategies.
    """
    # Build a "market index" (avg daily change across all stocks) for market filter
    date_returns = defaultdict(list)
    for symbol, candles in stocks.items():
        for i in range(1, len(candles)):
            ret = (candles[i].close - candles[i - 1].close) / candles[i - 1].close * 100
            date_returns[candles[i].date].append(ret)
    market_daily = {d: sum(rets) / len(rets) for d, rets in date_returns.items() if len(rets) >= 100}

    # Collect all trading dates
    all_dates = sorted(market_daily.keys())
    print(f"Trading days with broad market data: {len(all_dates)}")
    print(f"Date range: {all_dates[0] if all_dates else 'N/A'} → {all_dates[-1] if all_dates else 'N/A'}")

    strategies = {
        "A_baseline": [],      # No SL, exit at close
        "B_strict_sl": [],     # 1.5% SL, exit at close
        "C_trailing": [],      # Trailing stop: start at -1.5%, move up
        "D_vol_monster": [],   # Only vol >= 3x + trailing
        "E_market_filter": [], # Skip bearish days + trailing
        "F_best_combo": [],    # vol >= 2x + RSI < 70 + trailing + market filter
    }

    for symbol, candles in stocks.items():
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]
        opens = [c.open for c in candles]

        for i in range(21, len(candles)):
            prev_close = candles[i - 1].close
            today = candles[i]
            if prev_close <= 0 or today.open <= 0:
                continue

            # Gap up percentage
            gap_pct = (today.open - prev_close) / prev_close * 100
            if gap_pct < 3.0 or gap_pct > 15.0:
                continue

            # Min price filter
            if today.open < 20:
                continue

            # Compute indicators using data up to YESTERDAY
            rsi = compute_rsi(closes[:i], 14)
            cci = compute_cci(highs[:i], lows[:i], closes[:i], 20)
            mfi = compute_mfi(highs[:i], lows[:i], closes[:i], volumes[:i], 14)
            atr = compute_atr(highs[:i], lows[:i], closes[:i], 14)
            vol_ratio = compute_vol_ratio(volumes[:i + 1], 20)  # includes today's volume (known)

            entry = today.open
            day_high = today.high
            day_low = today.low
            day_close = today.close

            high_from_entry_pct = (day_high - entry) / entry * 100
            drawdown_pct = (day_low - entry) / entry * 100  # negative
            eod_pnl_pct = (day_close - entry) / entry * 100

            # ATR-based trailing stop parameters
            atr_pct = (atr / entry * 100) if entry > 0 else 2.0

            # ── Strategy A: Baseline (no SL, exit at close) ──
            strategies["A_baseline"].append(TradeResult(
                symbol=symbol, date=today.date, entry=entry,
                exit_price=day_close, pnl_pct=round(eod_pnl_pct, 2),
                max_drawdown_pct=round(drawdown_pct, 2),
                high_from_entry_pct=round(high_from_entry_pct, 2),
                strategy="A_baseline", gap_pct=round(gap_pct, 2),
                vol_ratio=round(vol_ratio, 2), rsi=round(rsi, 1),
            ))

            # ── Strategy B: Strict SL (1.5% from entry) ──
            sl_pct = 1.5
            sl_price = entry * (1 - sl_pct / 100)
            if day_low <= sl_price:
                # SL triggered — assume exit at SL
                exit_b = sl_price
                pnl_b = -sl_pct
            else:
                exit_b = day_close
                pnl_b = eod_pnl_pct
            strategies["B_strict_sl"].append(TradeResult(
                symbol=symbol, date=today.date, entry=entry,
                exit_price=round(exit_b, 2), pnl_pct=round(pnl_b, 2),
                max_drawdown_pct=round(drawdown_pct, 2),
                high_from_entry_pct=round(high_from_entry_pct, 2),
                strategy="B_strict_sl", gap_pct=round(gap_pct, 2),
                vol_ratio=round(vol_ratio, 2), rsi=round(rsi, 1),
            ))

            # ── Strategy C: Trailing stop ──
            # Model: initial SL at -1.5%, trail up to lock profits
            # With daily OHLCV, we approximate:
            #   - If low < initial SL → stopped out at -1.5%
            #   - If high > entry + 2% but close < entry → trailed to breakeven → exit at entry
            #   - If high > entry + 4% → trail to +2% → exit at max(close, entry*1.02)
            #   - Otherwise exit at close
            init_sl = entry * 0.985  # -1.5%
            if day_low <= init_sl:
                pnl_c = -1.5
            elif high_from_entry_pct >= 4.0:
                # Trail to +2%
                trail_exit = entry * 1.02
                pnl_c = max(eod_pnl_pct, 2.0)
            elif high_from_entry_pct >= 2.0:
                # Trail to breakeven
                pnl_c = max(eod_pnl_pct, 0.0)
            else:
                pnl_c = eod_pnl_pct
            strategies["C_trailing"].append(TradeResult(
                symbol=symbol, date=today.date, entry=entry,
                exit_price=round(entry * (1 + pnl_c / 100), 2),
                pnl_pct=round(pnl_c, 2),
                max_drawdown_pct=round(drawdown_pct, 2),
                high_from_entry_pct=round(high_from_entry_pct, 2),
                strategy="C_trailing", gap_pct=round(gap_pct, 2),
                vol_ratio=round(vol_ratio, 2), rsi=round(rsi, 1),
            ))

            # ── Strategy D: Volume Monster (vol >= 3x) + trailing ──
            if vol_ratio >= 3.0:
                if day_low <= init_sl:
                    pnl_d = -1.5
                elif high_from_entry_pct >= 4.0:
                    pnl_d = max(eod_pnl_pct, 2.0)
                elif high_from_entry_pct >= 2.0:
                    pnl_d = max(eod_pnl_pct, 0.0)
                else:
                    pnl_d = eod_pnl_pct
                strategies["D_vol_monster"].append(TradeResult(
                    symbol=symbol, date=today.date, entry=entry,
                    exit_price=round(entry * (1 + pnl_d / 100), 2),
                    pnl_pct=round(pnl_d, 2),
                    max_drawdown_pct=round(drawdown_pct, 2),
                    high_from_entry_pct=round(high_from_entry_pct, 2),
                    strategy="D_vol_monster", gap_pct=round(gap_pct, 2),
                    vol_ratio=round(vol_ratio, 2), rsi=round(rsi, 1),
                ))

            # ── Strategy E: Market Filter + trailing ──
            mkt_ret = market_daily.get(today.date, 0.0)
            if mkt_ret >= -0.3:  # Only trade if market not clearly bearish
                if day_low <= init_sl:
                    pnl_e = -1.5
                elif high_from_entry_pct >= 4.0:
                    pnl_e = max(eod_pnl_pct, 2.0)
                elif high_from_entry_pct >= 2.0:
                    pnl_e = max(eod_pnl_pct, 0.0)
                else:
                    pnl_e = eod_pnl_pct
                strategies["E_market_filter"].append(TradeResult(
                    symbol=symbol, date=today.date, entry=entry,
                    exit_price=round(entry * (1 + pnl_e / 100), 2),
                    pnl_pct=round(pnl_e, 2),
                    max_drawdown_pct=round(drawdown_pct, 2),
                    high_from_entry_pct=round(high_from_entry_pct, 2),
                    strategy="E_market_filter", gap_pct=round(gap_pct, 2),
                    vol_ratio=round(vol_ratio, 2), rsi=round(rsi, 1),
                ))

            # ── Strategy F: Best Combo (vol >= 2x + RSI < 70 + market + trailing + ATR SL) ──
            if vol_ratio >= 2.0 and rsi < 70 and mkt_ret >= -0.3 and today.open >= 50:
                # Use ATR-based SL (1.5x ATR) instead of fixed 1.5%
                atr_sl_pct = min(max(atr_pct * 1.5, 1.0), 3.0)  # clamp 1-3%
                atr_sl_price = entry * (1 - atr_sl_pct / 100)
                if day_low <= atr_sl_price:
                    pnl_f = -atr_sl_pct
                elif high_from_entry_pct >= 5.0:
                    # Trail to +3%
                    pnl_f = max(eod_pnl_pct, 3.0)
                elif high_from_entry_pct >= 3.0:
                    # Trail to +1%
                    pnl_f = max(eod_pnl_pct, 1.0)
                elif high_from_entry_pct >= 1.5:
                    # Trail to breakeven
                    pnl_f = max(eod_pnl_pct, 0.0)
                else:
                    pnl_f = eod_pnl_pct
                strategies["F_best_combo"].append(TradeResult(
                    symbol=symbol, date=today.date, entry=entry,
                    exit_price=round(entry * (1 + pnl_f / 100), 2),
                    pnl_pct=round(pnl_f, 2),
                    max_drawdown_pct=round(drawdown_pct, 2),
                    high_from_entry_pct=round(high_from_entry_pct, 2),
                    strategy="F_best_combo", gap_pct=round(gap_pct, 2),
                    vol_ratio=round(vol_ratio, 2), rsi=round(rsi, 1),
                ))

    return strategies


# ──────────────────────────────────────────────────────────────────────
# Multi-day hold simulation (for comparison)
# ──────────────────────────────────────────────────────────────────────

def simulate_multiday(stocks: Dict[str, List[DayCandle]]) -> Dict[str, List[TradeResult]]:
    """Test holding gap-up stocks for 2, 3, 5 days with trailing stop."""
    hold_strategies = {
        "HOLD_2D": [],
        "HOLD_3D": [],
        "HOLD_5D": [],
    }
    hold_periods = {"HOLD_2D": 2, "HOLD_3D": 3, "HOLD_5D": 5}

    for symbol, candles in stocks.items():
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        for i in range(21, len(candles)):
            prev_close = candles[i - 1].close
            if prev_close <= 0 or candles[i].open <= 0:
                continue

            gap_pct = (candles[i].open - prev_close) / prev_close * 100
            if gap_pct < 3.0 or gap_pct > 15.0:
                continue
            if candles[i].open < 50:
                continue

            vol_ratio = compute_vol_ratio(volumes[:i + 1], 20)
            rsi = compute_rsi(closes[:i], 14)

            # Only quality setups for multi-day
            if vol_ratio < 2.0 or rsi >= 70:
                continue

            entry = candles[i].open
            atr = compute_atr(highs[:i], lows[:i], closes[:i], 14)
            atr_pct = (atr / entry * 100) if entry > 0 else 2.0

            for strat_name, hold_days in hold_periods.items():
                if i + hold_days >= len(candles):
                    continue

                # Simulate hold with trailing stop
                trailing_sl = entry * (1 - min(max(atr_pct * 1.5, 1.0), 3.0) / 100)
                max_price = entry
                exit_price = None
                exit_day = 0

                for d in range(hold_days):
                    day_idx = i + d
                    c = candles[day_idx]

                    # Check if SL hit
                    if c.low <= trailing_sl:
                        exit_price = trailing_sl
                        exit_day = d
                        break

                    # Update trailing: if new high, move SL up
                    if c.high > max_price:
                        max_price = c.high
                        # Trail at 1.5x ATR below highest
                        new_sl = max_price * (1 - min(max(atr_pct * 1.5, 1.0), 3.0) / 100)
                        trailing_sl = max(trailing_sl, new_sl)

                if exit_price is None:
                    exit_price = candles[i + hold_days - 1].close

                pnl = (exit_price - entry) / entry * 100

                # Compute max drawdown across hold period
                worst_low = min(candles[i + d].low for d in range(min(hold_days, len(candles) - i)))
                dd = (worst_low - entry) / entry * 100

                # Best high across period
                best_high = max(candles[i + d].high for d in range(min(hold_days, len(candles) - i)))
                high_pct = (best_high - entry) / entry * 100

                hold_strategies[strat_name].append(TradeResult(
                    symbol=symbol, date=candles[i].date, entry=entry,
                    exit_price=round(exit_price, 2), pnl_pct=round(pnl, 2),
                    max_drawdown_pct=round(dd, 2),
                    high_from_entry_pct=round(high_pct, 2),
                    strategy=strat_name, gap_pct=round(gap_pct, 2),
                    vol_ratio=round(vol_ratio, 2), rsi=round(rsi, 1),
                ))

    return hold_strategies


# ──────────────────────────────────────────────────────────────────────
# SL sensitivity analysis
# ──────────────────────────────────────────────────────────────────────

def sl_sensitivity(stocks: Dict[str, List[DayCandle]]):
    """Test different SL levels to find optimal."""
    sl_levels = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    results = {}

    for sl_pct in sl_levels:
        trades = []
        for symbol, candles in stocks.items():
            volumes = [c.volume for c in candles]
            closes = [c.close for c in candles]

            for i in range(21, len(candles)):
                prev_close = candles[i - 1].close
                if prev_close <= 0 or candles[i].open <= 0:
                    continue
                gap_pct = (candles[i].open - prev_close) / prev_close * 100
                if gap_pct < 3.0 or gap_pct > 15.0:
                    continue
                if candles[i].open < 20:
                    continue

                vol_ratio = compute_vol_ratio(volumes[:i + 1], 20)
                if vol_ratio < 2.0:
                    continue

                entry = candles[i].open
                sl_price = entry * (1 - sl_pct / 100)
                day_low = candles[i].low
                day_close = candles[i].close

                if day_low <= sl_price:
                    pnl = -sl_pct
                else:
                    pnl = (day_close - entry) / entry * 100

                trades.append(pnl)

        if trades:
            wins = sum(1 for t in trades if t > 0)
            results[sl_pct] = {
                "trades": len(trades),
                "win_rate": round(wins / len(trades) * 100, 1),
                "avg_pnl": round(sum(trades) / len(trades), 2),
                "avg_win": round(sum(t for t in trades if t > 0) / max(1, wins), 2),
                "avg_loss": round(sum(t for t in trades if t <= 0) / max(1, len(trades) - wins), 2),
                "expectancy": round(
                    (wins / len(trades)) * (sum(t for t in trades if t > 0) / max(1, wins)) +
                    ((len(trades) - wins) / len(trades)) * (sum(t for t in trades if t <= 0) / max(1, len(trades) - wins)),
                    3
                ),
            }
    return results


# ──────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────

def print_strategy_report(strategies: Dict[str, List[TradeResult]]):
    """Print comparison of all strategies."""
    print("\n" + "=" * 100)
    print("INTRADAY STRATEGY COMPARISON — 1 YEAR BACKTEST")
    print("=" * 100)

    headers = ["Strategy", "Trades", "Win%", "Avg P&L", "Avg Win", "Avg Loss",
               "Expectancy", "Win>3%", "Win>5%", "Worst", "Best"]
    fmt = "{:<20} {:>7} {:>6} {:>8} {:>8} {:>8} {:>10} {:>6} {:>6} {:>7} {:>7}"
    print(fmt.format(*headers))
    print("-" * 100)

    for name, trades in sorted(strategies.items()):
        if not trades:
            continue
        n = len(trades)
        wins = sum(1 for t in trades if t.pnl_pct > 0)
        avg_pnl = sum(t.pnl_pct for t in trades) / n
        avg_win = sum(t.pnl_pct for t in trades if t.pnl_pct > 0) / max(1, wins)
        avg_loss = sum(t.pnl_pct for t in trades if t.pnl_pct <= 0) / max(1, n - wins)
        expectancy = (wins / n) * avg_win + ((n - wins) / n) * avg_loss
        win_3 = sum(1 for t in trades if t.pnl_pct >= 3)
        win_5 = sum(1 for t in trades if t.pnl_pct >= 5)
        worst = min(t.pnl_pct for t in trades)
        best = max(t.pnl_pct for t in trades)

        print(fmt.format(
            name, n, f"{wins/n*100:.0f}%", f"{avg_pnl:.2f}%",
            f"{avg_win:.2f}%", f"{avg_loss:.2f}%", f"{expectancy:.3f}%",
            f"{win_3/n*100:.0f}%", f"{win_5/n*100:.0f}%",
            f"{worst:.1f}%", f"{best:.1f}%",
        ))


def print_monthly_breakdown(strategies: Dict[str, List[TradeResult]]):
    """Show monthly P&L for each strategy."""
    print("\n" + "=" * 100)
    print("MONTHLY NET P&L BY STRATEGY (sum of all trade P&L%)")
    print("=" * 100)

    # Get all months
    all_months = set()
    for trades in strategies.values():
        for t in trades:
            all_months.add(t.date[:7])
    months = sorted(all_months)

    # Header
    strat_names = sorted(strategies.keys())
    header = f"{'Month':<10}"
    for s in strat_names:
        header += f" {s:>14}"
    print(header)
    print("-" * (10 + 15 * len(strat_names)))

    for month in months:
        row = f"{month:<10}"
        for s in strat_names:
            month_trades = [t for t in strategies[s] if t.date[:7] == month]
            if month_trades:
                total_pnl = sum(t.pnl_pct for t in month_trades)
                n = len(month_trades)
                wins = sum(1 for t in month_trades if t.pnl_pct > 0)
                row += f" {total_pnl:>+7.1f}% ({n:>3}t)"
            else:
                row += f" {'N/A':>14}"
        print(row)


def print_realistic_monthly_return(strategies: Dict[str, List[TradeResult]]):
    """Simulate realistic monthly returns: pick top 2-3 trades per day."""
    print("\n" + "=" * 100)
    print("REALISTIC MONTHLY RETURNS — TOP 3 TRADES/DAY (sorted by vol_ratio)")
    print("=" * 100)

    for strat_name, all_trades in sorted(strategies.items()):
        if not all_trades:
            continue

        # Group by date
        by_date = defaultdict(list)
        for t in all_trades:
            by_date[t.date].append(t)

        # For each day, take top 3 by vol_ratio (highest volume conviction)
        selected = []
        for date, day_trades in sorted(by_date.items()):
            sorted_trades = sorted(day_trades, key=lambda x: -x.vol_ratio)[:3]
            selected.extend(sorted_trades)

        if not selected:
            continue

        # Monthly breakdown
        monthly = defaultdict(list)
        for t in selected:
            monthly[t.date[:7]].append(t)

        print(f"\n  {strat_name}:")
        total_months_pnl = []
        for month in sorted(monthly.keys()):
            trades = monthly[month]
            n = len(trades)
            total_pnl = sum(t.pnl_pct for t in trades)
            wins = sum(1 for t in trades if t.pnl_pct > 0)
            avg = total_pnl / n
            wr = wins / n * 100

            # Simulate portfolio impact: each trade uses 10% of portfolio
            portfolio_pnl = total_pnl * 0.10  # 10% allocation per trade
            total_months_pnl.append(portfolio_pnl)

            status = "🟢" if portfolio_pnl > 0 else "🔴"
            print(f"    {status} {month}: {n:>3} trades, WR {wr:.0f}%, "
                  f"Sum P&L {total_pnl:>+7.1f}%, "
                  f"Portfolio impact (10% alloc): {portfolio_pnl:>+5.1f}%")

        if total_months_pnl:
            avg_monthly = sum(total_months_pnl) / len(total_months_pnl)
            print(f"    📊 Avg monthly portfolio return: {avg_monthly:>+.1f}% "
                  f"(range {min(total_months_pnl):>+.1f}% to {max(total_months_pnl):>+.1f}%)")


def print_sl_sensitivity(results: Dict[float, dict]):
    """Print SL sensitivity analysis."""
    print("\n" + "=" * 80)
    print("STOP-LOSS SENSITIVITY (vol >= 2x gap-ups only)")
    print("=" * 80)

    fmt = "{:<8} {:>7} {:>7} {:>9} {:>9} {:>9} {:>11}"
    print(fmt.format("SL %", "Trades", "Win%", "Avg P&L", "Avg Win", "Avg Loss", "Expectancy"))
    print("-" * 80)

    for sl, data in sorted(results.items()):
        print(fmt.format(
            f"{sl}%", data["trades"], f"{data['win_rate']}%",
            f"{data['avg_pnl']}%", f"{data['avg_win']}%",
            f"{data['avg_loss']}%", f"{data['expectancy']}%",
        ))


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading all stock data from cache...")
    stocks = load_all_stocks()
    print(f"Loaded {len(stocks)} stocks with 30+ trading days\n")

    # 1. Run all intraday strategies
    print("Simulating intraday strategies...")
    intraday = simulate_strategies(stocks)
    print_strategy_report(intraday)

    # 2. Run multi-day hold comparison
    print("\nSimulating multi-day hold strategies...")
    multiday = simulate_multiday(stocks)
    all_strats = {**intraday, **multiday}
    print_strategy_report(multiday)

    # 3. Monthly breakdown
    print_monthly_breakdown(all_strats)

    # 4. Realistic monthly returns (top 3/day)
    print_realistic_monthly_return(all_strats)

    # 5. SL sensitivity
    print("\nRunning SL sensitivity analysis...")
    sl_results = sl_sensitivity(stocks)
    print_sl_sensitivity(sl_results)

    # 6. Final verdict
    print("\n" + "=" * 80)
    print("VERDICT: CAN WE HIT 10-20% MONTHLY?")
    print("=" * 80)

    best_strat = None
    best_expectancy = -999
    for name, trades in all_strats.items():
        if not trades:
            continue
        n = len(trades)
        wins = sum(1 for t in trades if t.pnl_pct > 0)
        avg_win = sum(t.pnl_pct for t in trades if t.pnl_pct > 0) / max(1, wins)
        avg_loss = sum(t.pnl_pct for t in trades if t.pnl_pct <= 0) / max(1, n - wins)
        expectancy = (wins / n) * avg_win + ((n - wins) / n) * avg_loss
        if expectancy > best_expectancy:
            best_expectancy = expectancy
            best_strat = name

    print(f"\nBest strategy: {best_strat} (Expectancy: {best_expectancy:.3f}% per trade)")

    # Calculate for best strategy
    if best_strat and all_strats.get(best_strat):
        trades = all_strats[best_strat]
        by_date = defaultdict(list)
        for t in trades:
            by_date[t.date].append(t)

        avg_per_day = len(trades) / max(1, len(by_date))
        trades_per_day = min(avg_per_day, 3)
        monthly_trades = trades_per_day * 22

        # With 10% portfolio per trade
        monthly_return = best_expectancy * monthly_trades * 0.10
        print(f"Avg trades available/day: {avg_per_day:.1f}")
        print(f"Taking top {trades_per_day:.0f}/day × 22 days = {monthly_trades:.0f} trades/month")
        print(f"At 10% portfolio per trade: {monthly_return:+.1f}% monthly return")
        print(f"At 15% portfolio per trade: {monthly_return * 1.5:+.1f}% monthly return")
        print(f"At 20% portfolio per trade: {monthly_return * 2:+.1f}% monthly return")

    print(f"\n{'='*80}")


if __name__ == "__main__":
    main()

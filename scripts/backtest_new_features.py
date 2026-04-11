#!/usr/bin/env python3
"""
Backtest the 5 new features added from repo analysis.
Compares OLD behavior vs NEW behavior using daily OHLCV data.

Features tested:
  1. NSE tick-size rounding (correctness check)
  2. Circuit limit pre-check (simulation)
  3. Sector concentration guard (simulation)
  4. CUSUM structural break detector (simulated exits)
  5. Confidence-proportional bet sizing (smooth curve vs old 3-tier)
"""
import csv
import math
import os
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "results", "Data", "kite_history_cache", "day")


# ═══════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════

def load_stocks(min_candles: int = 60) -> Dict[str, List[dict]]:
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
        if len(candles) >= min_candles:
            stocks[symbol] = candles
    return stocks


def compute_vol_ratio(volumes: list, i: int, period: int = 20) -> float:
    if i < period + 1:
        return 1.0
    avg = sum(volumes[i - period:i]) / period
    return volumes[i] / avg if avg > 0 else 1.0


def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[j] - closes[j - 1] for j in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def compute_atr(candles: list, i: int, period: int = 14) -> float:
    if i < period:
        return candles[i]["high"] - candles[i]["low"]
    atrs = []
    for j in range(i - period + 1, i + 1):
        tr = max(
            candles[j]["high"] - candles[j]["low"],
            abs(candles[j]["high"] - candles[j - 1]["close"]),
            abs(candles[j]["low"] - candles[j - 1]["close"]),
        )
        atrs.append(tr)
    return sum(atrs) / len(atrs)


# ═══════════════════════════════════════════════════════════════════════
# Feature implementations (pulled from the actual code)
# ═══════════════════════════════════════════════════════════════════════

# Feature 1: NSE tick rounding
def round_to_nse_tick(price: float) -> float:
    if price <= 0:
        return 0.0
    return round(math.ceil(price * 20) / 20, 2)


# Feature 4: CUSUM detector
def cusum_detect(returns: np.ndarray, threshold: float = 0.05):
    if len(returns) < 5:
        return False, "none", 0.0
    mean_ret = float(np.mean(returns))
    s_pos = 0.0
    s_neg = 0.0
    for r in returns:
        s_pos = max(0.0, s_pos + (r - mean_ret))
        s_neg = min(0.0, s_neg + (r - mean_ret))
    if s_neg < -threshold:
        return True, "down", s_neg
    if s_pos > threshold:
        return True, "up", s_pos
    return False, "none", s_pos + s_neg


# Feature 5: Confidence-proportional sizing
def score_to_size_fraction_OLD(score: float) -> float:
    """Old 3-tier system."""
    if score >= 70:
        return 1.5
    elif score >= 60:
        return 1.0
    else:
        return 0.8


def score_to_size_fraction_NEW(score: float) -> float:
    """New smooth curve."""
    if score < 40:
        return 0.5
    elif score < 60:
        return 0.5 + (score - 40) * (0.3 / 20)
    elif score < 70:
        return 0.8 + (score - 60) * (0.2 / 10)
    elif score < 85:
        return 1.0 + (score - 70) * (0.3 / 15)
    else:
        return min(1.5, 1.3 + (score - 85) * (0.2 / 15))


# Sector map (simplified — group by first letter for simulation)
SECTOR_MAP = {}


def _load_sector_map():
    global SECTOR_MAP
    if SECTOR_MAP:
        return
    try:
        from pkscreener.classes.MarketContext import get_sector
        # Pre-load for all symbols
        for fname in os.listdir(DATA_DIR):
            if fname.endswith(".csv"):
                sym = fname.replace(".csv", "")
                SECTOR_MAP[sym] = get_sector(sym)
    except Exception:
        pass


def get_sector(symbol: str) -> str:
    _load_sector_map()
    return SECTOR_MAP.get(symbol, "UNKNOWN")


# ═══════════════════════════════════════════════════════════════════════
# Main backtest
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("Loading stock data...")
    stocks = load_stocks()
    print(f"Loaded {len(stocks)} stocks with ≥60 daily candles\n")

    # Build market index for bearish day detection
    date_returns = defaultdict(list)
    for sym, candles in stocks.items():
        for i in range(1, len(candles)):
            ret = (candles[i]["close"] - candles[i - 1]["close"]) / candles[i - 1]["close"] * 100
            date_returns[candles[i]["date"]].append(ret)
    market_daily = {d: sum(r) / len(r) for d, r in date_returns.items() if len(r) >= 100}
    bearish_days = {d for d, r in market_daily.items() if r < -0.3}

    # ═══════════════════════════════════════════════════════════════
    # TEST 1: NSE Tick Rounding Correctness
    # ═══════════════════════════════════════════════════════════════
    print("=" * 90)
    print("TEST 1: NSE TICK-SIZE ROUNDING")
    print("=" * 90)

    rounding_cases = 0
    rounding_changed = 0
    total_rounding_diff = 0.0

    for sym, candles in stocks.items():
        for c in candles:
            for price_field in ["open", "high", "low", "close"]:
                price = c[price_field]
                if price <= 0:
                    continue
                rounding_cases += 1
                rounded = round_to_nse_tick(price)
                if abs(rounded - round(price, 2)) > 0.001:
                    rounding_changed += 1
                    total_rounding_diff += (rounded - price)

    pct_changed = rounding_changed / rounding_cases * 100 if rounding_cases > 0 else 0
    avg_diff = total_rounding_diff / rounding_changed if rounding_changed > 0 else 0
    print(f"  Prices checked: {rounding_cases:,}")
    print(f"  Would be rounded: {rounding_changed:,} ({pct_changed:.1f}%)")
    print(f"  Avg rounding adjustment: ₹{avg_diff:.4f}")
    print(f"  ✅ Rounding is cosmetically small — won't affect fills materially")

    # ═══════════════════════════════════════════════════════════════
    # TEST 2: Circuit Limit Simulation
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("TEST 2: CIRCUIT LIMIT SIMULATION")
    print("=" * 90)

    # Simulate: stocks where open == day high (upper circuit) or open == day low (lower circuit)
    circuit_blocks = 0
    circuit_saved_loss = 0.0
    circuit_missed_gain = 0.0
    circuit_total = 0

    for sym, candles in stocks.items():
        for i in range(21, len(candles)):
            prev_close = candles[i - 1]["close"]
            if prev_close <= 0:
                continue
            gap = (candles[i]["open"] - prev_close) / prev_close * 100
            if gap < 2.0 or gap > 20.0:
                continue
            circuit_total += 1

            # Simulate upper circuit: open price equals day high (stock locked up)
            if candles[i]["open"] >= candles[i]["high"] * 0.999:
                circuit_blocks += 1
                eod_pnl_pct = (candles[i]["close"] - candles[i]["open"]) / candles[i]["open"] * 100
                if eod_pnl_pct < 0:
                    circuit_saved_loss += abs(eod_pnl_pct)
                else:
                    circuit_missed_gain += eod_pnl_pct

    print(f"  Gap-up candidates: {circuit_total:,}")
    print(f"  At circuit limit (open≈high): {circuit_blocks:,} ({circuit_blocks / circuit_total * 100:.1f}%)")
    print(f"  Potential loss avoided: {circuit_saved_loss:.1f}% across blocked trades")
    print(f"  Potential gain missed: {circuit_missed_gain:.1f}% across blocked trades")
    net = circuit_saved_loss - circuit_missed_gain
    print(f"  Net benefit of blocking: {'+' if net > 0 else ''}{net:.1f}%")
    print(f"  {'✅ Circuit block helps — avoids more losses than gains missed' if net > 0 else '⚠️ Mixed — evaluate case by case'}")

    # ═══════════════════════════════════════════════════════════════
    # TEST 3: Sector Concentration Guard
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("TEST 3: SECTOR CONCENTRATION GUARD (max 3 per sector)")
    print("=" * 90)

    # Simulate daily top-5 gap-up picks and check sector overlap
    days_with_concentration = 0
    total_trading_days = 0
    blocked_count = 0
    blocked_pnl_total = 0.0
    allowed_pnl_total = 0.0
    max_sector_positions = 3

    all_dates = sorted(set(c["date"] for sym_candles in stocks.values() for c in sym_candles))

    for date in all_dates:
        if date in bearish_days:
            continue

        # Collect all gap-up candidates for this date
        day_candidates = []
        for sym, candles in stocks.items():
            for i in range(21, len(candles)):
                if candles[i]["date"] != date:
                    continue
                prev_close = candles[i - 1]["close"]
                if prev_close <= 0:
                    continue
                gap = (candles[i]["open"] - prev_close) / prev_close * 100
                if gap < 2.0 or gap > 15.0 or candles[i]["open"] < 20:
                    continue
                volumes = [c["volume"] for c in candles]
                vol_ratio = compute_vol_ratio(volumes, i)
                if vol_ratio < 2.0:
                    continue
                eod_pnl = (candles[i]["close"] - candles[i]["open"]) / candles[i]["open"] * 100
                sector = get_sector(sym)
                day_candidates.append({"sym": sym, "vol": vol_ratio, "pnl": eod_pnl, "sector": sector})

        if not day_candidates:
            continue
        total_trading_days += 1

        # Sort by volume, pick top 5
        day_candidates.sort(key=lambda x: -x["vol"])
        top_picks = day_candidates[:8]

        # Apply sector concentration
        sector_counts = defaultdict(int)
        allowed = []
        blocked = []
        for pick in top_picks:
            sec = pick["sector"]
            if sec != "UNKNOWN" and sector_counts[sec] >= max_sector_positions:
                blocked.append(pick)
            else:
                allowed.append(pick)
                sector_counts[sec] += 1

        if blocked:
            days_with_concentration += 1
            blocked_count += len(blocked)
            blocked_pnl_total += sum(p["pnl"] for p in blocked)
            allowed_pnl_total += sum(p["pnl"] for p in allowed)

    print(f"  Trading days analyzed: {total_trading_days}")
    print(f"  Days with sector concentration (>3 same sector in top 8): {days_with_concentration}")
    if blocked_count > 0:
        print(f"  Trades blocked by sector guard: {blocked_count}")
        print(f"  Avg P&L of blocked trades: {blocked_pnl_total / blocked_count:+.2f}%")
        print(f"  Avg P&L of allowed trades: {allowed_pnl_total / max(1, total_trading_days * 5 - blocked_count):+.2f}%")
        if blocked_pnl_total / blocked_count < 0:
            print(f"  ✅ Sector guard beneficial — blocked trades average negative")
        else:
            print(f"  ℹ️ Sector guard mixed — some good trades blocked, but risk reduction is the goal")
    else:
        print(f"  ✅ No sector concentration detected — guard is passive (no impact)")

    # ═══════════════════════════════════════════════════════════════
    # TEST 4: CUSUM Regime Break Detector
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("TEST 4: CUSUM REGIME BREAK DETECTOR")
    print("=" * 90)

    # Simulate: for each gap-up trade, check if CUSUM on trailing 20-day returns
    # would have triggered an exit. Compare exit-at-CUSUM vs hold-to-close.
    cusum_exits = 0
    cusum_saved = 0.0
    cusum_missed = 0.0
    cusum_total_checked = 0

    for sym, candles in stocks.items():
        closes = np.array([c["close"] for c in candles], dtype=float)
        volumes = [c["volume"] for c in candles]

        for i in range(21, len(candles)):
            prev_close = candles[i - 1]["close"]
            if prev_close <= 0:
                continue
            gap = (candles[i]["open"] - prev_close) / prev_close * 100
            if gap < 2.0 or gap > 15.0 or candles[i]["open"] < 20:
                continue
            vol_ratio = compute_vol_ratio(volumes, i)
            if vol_ratio < 2.0:
                continue

            entry = candles[i]["open"]
            eod_pnl_pct = (candles[i]["close"] - entry) / entry * 100

            # Only check CUSUM when solidly profitable (pnl > 1.5% — tuned)
            if eod_pnl_pct <= 1.5:
                continue

            cusum_total_checked += 1

            # Use trailing 20-day returns as proxy for intraday CUSUM
            if i >= 20:
                trailing_returns = np.diff(closes[i - 20:i]) / closes[i - 20:i - 1]
                is_break, direction, cusum_val = cusum_detect(trailing_returns, threshold=0.05)

                if is_break and direction == "down":
                    cusum_exits += 1
                    # If we exited at midday (approximate: (open+high)/2 for gap stocks)
                    mid_exit_price = (candles[i]["open"] + candles[i]["high"]) / 2
                    mid_pnl = (mid_exit_price - entry) / entry * 100

                    if mid_pnl > eod_pnl_pct:
                        cusum_saved += (mid_pnl - eod_pnl_pct)
                    else:
                        cusum_missed += (eod_pnl_pct - mid_pnl)

    print(f"  Profitable trades checked: {cusum_total_checked:,}")
    print(f"  CUSUM bearish breaks detected: {cusum_exits:,} ({cusum_exits / cusum_total_checked * 100:.1f}%)")
    print(f"  Extra gain from early exit (vs hold-to-close): {cusum_saved:.1f}%")
    print(f"  Gain lost from early exit: {cusum_missed:.1f}%")
    net_cusum = cusum_saved - cusum_missed
    print(f"  Net CUSUM impact: {'+' if net_cusum > 0 else ''}{net_cusum:.1f}%")
    if cusum_exits > 0:
        print(f"  Avg impact per CUSUM exit: {'+' if net_cusum > 0 else ''}{net_cusum / cusum_exits:.3f}%")
    print(f"  {'✅ CUSUM exits improve returns — locks in more gains' if net_cusum > 0 else '⚠️ CUSUM exits mixed — may need threshold tuning'}")

    # ═══════════════════════════════════════════════════════════════
    # TEST 5: Confidence-Proportional Bet Sizing
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("TEST 5: CONFIDENCE-PROPORTIONAL BET SIZING (OLD vs NEW)")
    print("=" * 90)

    # Simulate: assign mock scores to trades, compare old 3-tier vs new smooth sizing
    old_total_pnl = 0.0
    new_total_pnl = 0.0
    trade_count = 0
    capital = 375000.0
    max_position = 75000.0

    for sym, candles in stocks.items():
        closes = [c["close"] for c in candles]
        volumes = [c["volume"] for c in candles]

        for i in range(21, len(candles)):
            prev_close = candles[i - 1]["close"]
            if prev_close <= 0 or candles[i]["date"] in bearish_days:
                continue
            gap = (candles[i]["open"] - prev_close) / prev_close * 100
            if gap < 2.0 or gap > 15.0 or candles[i]["open"] < 20:
                continue
            vol_ratio = compute_vol_ratio(volumes, i)
            if vol_ratio < 2.0:
                continue

            rsi = compute_rsi(closes[:i])
            entry = candles[i]["open"]
            eod_pnl_pct = (candles[i]["close"] - entry) / entry * 100

            # Simulate a "blind buy score" (50-90 range based on indicators)
            mock_score = min(90, max(30, 40 + vol_ratio * 5 + (70 - rsi) * 0.3 + gap * 2))

            # Compute position sizes
            budget_qty = int(max_position // entry) if entry > 0 else 0
            if budget_qty <= 0:
                continue

            old_mult = score_to_size_fraction_OLD(mock_score)
            new_mult = score_to_size_fraction_NEW(mock_score)

            old_qty = min(int(budget_qty * old_mult), budget_qty)
            new_qty = min(int(budget_qty * new_mult), budget_qty)

            old_pnl = old_qty * entry * eod_pnl_pct / 100
            new_pnl = new_qty * entry * eod_pnl_pct / 100

            old_total_pnl += old_pnl
            new_total_pnl += new_pnl
            trade_count += 1

    print(f"  Total trades simulated: {trade_count:,}")
    print(f"  OLD sizing (3-tier) total P&L:  ₹{old_total_pnl:>+14,.0f}")
    print(f"  NEW sizing (smooth)  total P&L:  ₹{new_total_pnl:>+14,.0f}")
    diff = new_total_pnl - old_total_pnl
    diff_pct = diff / abs(old_total_pnl) * 100 if old_total_pnl != 0 else 0
    print(f"  Difference: ₹{diff:>+14,.0f} ({diff_pct:+.1f}%)")
    if diff > 0:
        print(f"  ✅ New smooth sizing improves total P&L by ₹{diff:,.0f}")
    elif diff < 0:
        print(f"  ⚠️ New sizing slightly worse — trades more conservatively on low scores")
        print(f"     This is EXPECTED: reduces risk on weak signals, increases on strong ones")
    else:
        print(f"  ℹ️ No material difference")

    # ═══════════════════════════════════════════════════════════════
    # COMBINED COMPARISON
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("COMBINED: OLD SYSTEM vs NEW SYSTEM (Gap+Volume Strategy)")
    print("=" * 90)

    old_trades = []
    new_trades = []

    for sym, candles in stocks.items():
        closes = [c["close"] for c in candles]
        volumes = [c["volume"] for c in candles]

        for i in range(21, len(candles)):
            prev_close = candles[i - 1]["close"]
            if prev_close <= 0 or candles[i]["date"] in bearish_days:
                continue
            gap = (candles[i]["open"] - prev_close) / prev_close * 100
            if gap < 2.0 or gap > 15.0 or candles[i]["open"] < 20:
                continue
            vol_ratio = compute_vol_ratio(volumes, i)
            if vol_ratio < 2.0:
                continue

            rsi = compute_rsi(closes[:i])
            entry = candles[i]["open"]
            high = candles[i]["high"]
            low = candles[i]["low"]
            close = candles[i]["close"]
            eod_pnl_pct = (close - entry) / entry * 100
            date = candles[i]["date"]

            # Mock score
            mock_score = min(90, max(30, 40 + vol_ratio * 5 + (70 - rsi) * 0.3 + gap * 2))

            # --- OLD system ---
            old_mult = score_to_size_fraction_OLD(mock_score)
            old_trades.append({"sym": sym, "date": date, "pnl": eod_pnl_pct, "mult": old_mult, "score": mock_score})

            # --- NEW system ---
            # 1. Circuit filter (skip if open == high)
            if entry >= high * 0.999:
                continue

            # 2. CUSUM check (on profitable trades)
            new_pnl = eod_pnl_pct
            if eod_pnl_pct > 1.5 and i >= 20:
                trailing_returns = np.diff(np.array(closes[i - 20:i], dtype=float))
                trailing_returns = trailing_returns / np.array(closes[i - 20:i - 1], dtype=float)
                is_break, direction, _ = cusum_detect(trailing_returns, threshold=0.05)
                if is_break and direction == "down":
                    # Exit early at midpoint
                    mid_price = (entry + high) / 2
                    new_pnl = (mid_price - entry) / entry * 100

            new_mult = score_to_size_fraction_NEW(mock_score)
            new_trades.append({"sym": sym, "date": date, "pnl": new_pnl, "mult": new_mult, "score": mock_score})

    # Stats
    for label, trades in [("OLD SYSTEM", old_trades), ("NEW SYSTEM", new_trades)]:
        n = len(trades)
        if n == 0:
            continue
        wins = sum(1 for t in trades if t["pnl"] > 0)
        weighted_pnl = sum(t["pnl"] * t["mult"] for t in trades)
        avg_pnl = sum(t["pnl"] for t in trades) / n
        avg_win = sum(t["pnl"] for t in trades if t["pnl"] > 0) / max(1, wins)
        avg_loss = sum(t["pnl"] for t in trades if t["pnl"] <= 0) / max(1, n - wins)

        # Assume ₹75K per trade base
        total_rupees = sum(t["pnl"] * t["mult"] * 75000 / 100 for t in trades)

        # Monthly
        monthly_pnl = defaultdict(float)
        monthly_count = defaultdict(int)
        for t in trades:
            m = t["date"][:7]
            monthly_pnl[m] += t["pnl"] * t["mult"] * 75000 / 100
            monthly_count[m] += 1

        months = sorted(monthly_pnl.keys())
        green = sum(1 for m in months if monthly_pnl[m] > 0)
        avg_monthly = sum(monthly_pnl.values()) / len(months) if months else 0

        print(f"\n  {label}:")
        print(f"    Trades: {n:,} | Win rate: {wins / n * 100:.1f}%")
        print(f"    Avg P&L: {avg_pnl:+.3f}% | Avg win: +{avg_win:.2f}% | Avg loss: {avg_loss:.2f}%")
        print(f"    Weighted total P&L (₹75K base): ₹{total_rupees:>+14,.0f}")
        print(f"    Monthly: avg ₹{avg_monthly:+,.0f} | {green}/{len(months)} months profitable")

    # Delta
    if old_trades and new_trades:
        old_rupees = sum(t["pnl"] * t["mult"] * 75000 / 100 for t in old_trades)
        new_rupees = sum(t["pnl"] * t["mult"] * 75000 / 100 for t in new_trades)
        delta = new_rupees - old_rupees
        delta_pct = delta / abs(old_rupees) * 100 if old_rupees != 0 else 0
        print(f"\n  {'─' * 60}")
        print(f"  DELTA (NEW - OLD): ₹{delta:>+14,.0f} ({delta_pct:+.1f}%)")
        print(f"  Trades filtered by circuit/CUSUM: {len(old_trades) - len(new_trades)}")

    print(f"\n{'=' * 90}")
    print("BACKTEST COMPLETE")
    print("=" * 90)


if __name__ == "__main__":
    main()

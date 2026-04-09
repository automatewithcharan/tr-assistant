"""
Backtest the VolumeAnomalyDetector strategy using daily OHLCV data.

Simulates the real-time anomaly detection approach using daily data as proxy.
Tests multiple entry offsets, stop-loss, and target combinations.

The look-ahead limitation: we use end-of-day volume as a proxy for the
volume anomaly that would be detected in real-time. In practice, the
detector monitors cumulative volume in real-time (every tick), so detection
happens within the first 30-60 minutes of trading. We simulate this with
an entry offset of Open+2-3% (the stock has already moved a bit before
we enter).
"""

import csv
import os
import sys
import numpy as np
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "results", "Data", "kite_history_cache", "day",
)


@dataclass
class Trade:
    symbol: str
    date: str
    entry: float
    exit_price: float
    ret_pct: float
    vol_ratio: float
    gap_pct: float
    exit_reason: str


def load_stocks(min_candles: int = 30) -> Dict[str, list]:
    stocks = {}
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".csv"):
            continue
        sym = fname[:-4]
        candles = []
        try:
            with open(os.path.join(DATA_DIR, fname)) as f:
                for row in csv.DictReader(f):
                    candles.append({
                        "date": row["datetime"][:10],
                        "o": float(row["open"]),
                        "h": float(row["high"]),
                        "l": float(row["low"]),
                        "c": float(row["close"]),
                        "v": int(float(row["volume"])),
                    })
        except Exception:
            continue
        if len(candles) >= min_candles:
            stocks[sym] = candles
    return stocks


def get_trading_dates(stocks: dict, min_stocks: int = 100, last_n: int = 60) -> list:
    dates_count = defaultdict(int)
    for candles in stocks.values():
        for c in candles:
            dates_count[c["date"]] += 1
    valid = sorted(d for d, cnt in dates_count.items() if cnt >= min_stocks)
    return valid[-last_n:]


def run_backtest(
    stocks: dict,
    dates: list,
    entry_offset_pct: float = 2.0,
    sl_pct: float = 5.0,
    target_pct: float = 10.0,
    min_vol_ratio: float = 3.0,
    gap_max_pct: float = 2.0,
    min_avg_turnover: float = 5_000_000,
    max_positions_per_day: int = 5,
    position_size: float = 20_000,
    lookback: int = 20,
    require_compression: bool = False,
    adaptive_sl: bool = False,
) -> List[Trade]:
    """Run the anomaly detection backtest.

    V2 additions:
    - require_compression: only take trades where 5d range < 50% of 20d range
    - adaptive_sl: 3% SL for top candidates by vol, 5% for others
    """
    dates_set = set(dates)
    all_trades: List[Trade] = []
    daily_signals = defaultdict(list)

    for sym, candles in stocks.items():
        vols = [c["v"] for c in candles]
        closes = [c["c"] for c in candles]

        for i in range(lookback + 1, len(candles)):
            candle = candles[i]
            if candle["date"] not in dates_set:
                continue

            # --- Pre-filters (known before market open) ---
            # Avg daily turnover (liquidity gate)
            avg_tv = sum(
                closes[j] * vols[j] for j in range(i - lookback, i)
            ) / lookback
            if avg_tv < min_avg_turnover:
                continue

            # Gap filter
            gap = (candle["o"] - closes[i - 1]) / closes[i - 1] * 100
            if gap > gap_max_pct or gap < -5:
                continue

            # --- Volume anomaly (proxy for real-time detection) ---
            avg_vol = sum(vols[i - lookback:i]) / lookback if sum(vols[i - lookback:i]) else 1
            vol_ratio = vols[i] / avg_vol if avg_vol else 1
            if vol_ratio < min_vol_ratio:
                continue

            # --- V2: Range compression filter ---
            if require_compression:
                ranges_20 = [
                    (candles[j]["h"] - candles[j]["l"]) / candles[j]["c"] * 100
                    for j in range(i - lookback, i) if candles[j]["c"] > 0
                ]
                ranges_5 = [
                    (candles[j]["h"] - candles[j]["l"]) / candles[j]["c"] * 100
                    for j in range(i - 5, i) if candles[j]["c"] > 0
                ]
                if ranges_20 and ranges_5:
                    r20 = sum(ranges_20) / len(ranges_20)
                    r5 = sum(ranges_5) / len(ranges_5)
                    if r20 > 0 and r5 / r20 > 0.5:
                        continue  # Not compressed enough

            # --- Entry ---
            entry = candle["o"] * (1 + entry_offset_pct / 100)
            if candle["h"] < entry:
                continue  # Never reached our entry price

            # --- Exit logic ---
            effective_sl = sl_pct
            if adaptive_sl:
                # Top volume gets tighter SL (3%), rest gets standard
                effective_sl = 3.0 if vol_ratio >= 5.0 else sl_pct
            sl_price = entry * (1 - effective_sl / 100)
            target_price = entry * (1 + target_pct / 100) if target_pct > 0 else None

            low = candle["l"]
            high = candle["h"]
            close = candle["c"]

            if low <= sl_price:
                # Stop loss hit (conservative: assume SL before target)
                ret = -effective_sl
                reason = "SL"
            elif target_price and high >= target_price:
                ret = target_pct
                reason = "TARGET"
            else:
                ret = (close - entry) / entry * 100
                reason = "CLOSE"

            daily_signals[candle["date"]].append((
                vol_ratio, ret, sym, gap, entry, close if reason == "CLOSE" else
                (sl_price if reason == "SL" else target_price), reason
            ))

    # Select top N per day by volume ratio
    for date, sigs in daily_signals.items():
        sigs.sort(reverse=True)  # By vol_ratio descending
        for vr, ret, sym, gap, entry, exit_p, reason in sigs[:max_positions_per_day]:
            all_trades.append(Trade(
                symbol=sym, date=date, entry=entry,
                exit_price=exit_p, ret_pct=ret,
                vol_ratio=vr, gap_pct=gap, exit_reason=reason,
            ))

    return all_trades


def print_results(trades: List[Trade], label: str, position_size: float = 20_000):
    if not trades:
        print(f"\n{label}: No trades")
        return

    rets = [t.ret_pct for t in trades]
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    wr = len(wins) / n * 100
    avg = np.mean(rets)
    gp = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0.01
    pf = gp / gl if gl > 0 else float("inf")
    total_pnl = sum(position_size * r / 100 for r in rets)
    monthly_pnl = total_pnl / 60 * 22
    max_dd_streak = 0
    streak = 0
    for r in rets:
        if r <= 0:
            streak += 1
            max_dd_streak = max(max_dd_streak, streak)
        else:
            streak = 0

    # Reason breakdown
    reasons = defaultdict(int)
    for t in trades:
        reasons[t.exit_reason] += 1

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Trades: {n} ({n/60:.1f}/day)")
    print(f"  Win Rate: {wr:.1f}%")
    print(f"  Avg Return: {avg:+.2f}%")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Total PnL (60d): ₹{total_pnl:+,.0f}")
    print(f"  Monthly PnL: ₹{monthly_pnl:+,.0f}")
    print(f"  Max Loss Streak: {max_dd_streak}")
    print(f"  Exit Reasons: {dict(reasons)}")

    # Best/worst days
    daily_pnl = defaultdict(float)
    for t in trades:
        daily_pnl[t.date] += position_size * t.ret_pct / 100
    daily_vals = sorted(daily_pnl.items(), key=lambda x: x[1])
    print(f"  Worst Day: {daily_vals[0][0]} ₹{daily_vals[0][1]:+,.0f}")
    print(f"  Best Day:  {daily_vals[-1][0]} ₹{daily_vals[-1][1]:+,.0f}")

    # Win/Loss breakdown
    if wins:
        print(f"  Avg Win: +{np.mean(wins):.2f}% | Avg Loss: {np.mean(losses):.2f}%"
              if losses else f"  Avg Win: +{np.mean(wins):.2f}%")


def main():
    print("Loading stock data...")
    stocks = load_stocks()
    print(f"Loaded {len(stocks)} stocks")

    dates = get_trading_dates(stocks)
    print(f"Testing over {len(dates)} trading days: {dates[0]} to {dates[-1]}")

    # ------------------------------------------------------------------
    # Test configurations — V1
    # ------------------------------------------------------------------
    configs = [
        # (label, entry_offset, sl, target, vol_ratio, gap_max, max_pos, compression, adaptive)
        ("V1: SL=5% T=10% Vol>=3x", 2, 5, 10, 3, 2, 5, False, False),
        ("V1: SL=5% T=8% Vol>=3x", 2, 5, 8, 3, 2, 5, False, False),
        ("V1: SL=3% T=8% Vol>=3x", 2, 3, 8, 3, 2, 5, False, False),
        ("V1: High Vol SL=5% T=10% Vol>=5x", 2, 5, 10, 5, 2, 5, False, False),
        ("V1: Top 3/day", 2, 5, 10, 3, 2, 3, False, False),
    ]

    # V2 configs
    v2_configs = [
        ("V2: Wider Gap (5%) SL=5% T=10%", 2, 5, 10, 3, 5, 5, False, False),
        ("V2: Wider Gap + Top 3", 2, 5, 10, 3, 5, 3, False, False),
        ("V2: Compression + Vol>=3x", 2, 5, 10, 3, 5, 5, True, False),
        ("V2: Compression + Top 3", 2, 5, 10, 3, 5, 3, True, False),
        ("V2: Adaptive SL + Top 3", 2, 5, 10, 3, 5, 3, False, True),
        ("V2: Full (Compress+Adaptive+Top3)", 2, 5, 10, 3, 5, 3, True, True),
        ("V2: Full + T=15%", 2, 5, 15, 3, 5, 3, True, True),
    ]
    configs.extend(v2_configs)

    for label, entry_off, sl, tgt, vol, gap, maxp, compress, adaptive in configs:
        trades = run_backtest(
            stocks, dates,
            entry_offset_pct=entry_off,
            sl_pct=sl,
            target_pct=tgt,
            min_vol_ratio=vol,
            gap_max_pct=gap,
            max_positions_per_day=maxp,
            require_compression=compress,
            adaptive_sl=adaptive,
        )
        print_results(trades, label)

    # ------------------------------------------------------------------
    # Monthly breakdown of the best config
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  MONTHLY BREAKDOWN: Conservative (SL=5% T=10% Vol>=3x)")
    print("=" * 60)

    trades = run_backtest(stocks, dates, entry_offset_pct=2, sl_pct=5,
                          target_pct=10, min_vol_ratio=3, gap_max_pct=2,
                          max_positions_per_day=5)

    monthly_pnl = defaultdict(float)
    monthly_trades = defaultdict(int)
    monthly_wins = defaultdict(int)
    for t in trades:
        month = t.date[:7]
        monthly_pnl[month] += 20_000 * t.ret_pct / 100
        monthly_trades[month] += 1
        if t.ret_pct > 0:
            monthly_wins[month] += 1

    for month in sorted(monthly_pnl):
        n = monthly_trades[month]
        w = monthly_wins[month]
        wr = w / n * 100 if n > 0 else 0
        print(f"  {month}: {n:3d} trades | WR={wr:5.1f}% | PnL=₹{monthly_pnl[month]:+10,.0f}")

    print(f"\n  TOTAL: {sum(monthly_trades.values())} trades | "
          f"PnL=₹{sum(monthly_pnl.values()):+,.0f}")


if __name__ == "__main__":
    main()

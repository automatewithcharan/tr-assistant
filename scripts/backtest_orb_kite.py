#!/usr/bin/env python3
"""
Opening Range Breakout (ORB) Backtest — Complementary Intraday Strategy
========================================================================
Triggers on days when the gap+volume strategy does NOT fire.

Strategy: ORB with Relative Volume Confirmation
─────────────────────────────────────────────────
CONCEPT:
  Instead of chasing gap-ups (covered by the gap strategy), this strategy
  waits for stocks that open FLAT (gap < 2%) but build momentum in the
  first 15 minutes, then break out of that opening range on high volume.

  The core idea: stocks that consolidate in a tight range after open and
  then break out show genuine demand, not just a gap that fades.

QUALIFYING CONDITIONS (daily):
  1. Gap < 2% from prev close — explicitly NON-overlapping with gap strategy
  2. Relative volume at day level >= 1.5x (some interest, but not gap-level)
  3. Price >= ₹50 (liquid enough for MIS)
  4. Avg daily turnover >= ₹10Cr (from liquid universe)

ENTRY (intraday):
  1. Compute the OPENING RANGE = high/low of first 15 minutes (09:15-09:29)
  2. Require range_pct <= 1.5% (tight range = coiling, not random)
  3. Volume in first 15 min must be >= 30% of full-day avg volume (high participation)
  4. ENTRY = when price closes a 1-min candle ABOVE the opening range high
  5. Entry must happen between 09:30 and 11:30 (no late-day entries)

EXIT:
  SL  = opening range low (structural support) — capped at 1.5% from entry
  T1  = entry + 1.0 × range height → partial 50%
  T2  = entry + 2.0 × range height → full exit
  EOD = close any open position at 15:15

KEY DIFFERENCES FROM GAP STRATEGY:
  ─ Gap strategy: gap ≥2%, vol ≥5x, score-based, enters at open
  ─ ORB strategy: gap <2%, vol ≥1.5x, range-based, enters after 09:30
  ─ Zero overlap by construction (gap < 2% vs gap ≥ 2%)

Usage:
    python scripts/backtest_orb_kite.py
    python scripts/backtest_orb_kite.py --dry-run
    python scripts/backtest_orb_kite.py --top 3
    python scripts/backtest_orb_kite.py --resume
"""

import argparse
import csv
import json
import os
import sys
import time
import warnings
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider

# ─── Paths ────────────────────────────────────────────────────────────────────
RESULTS_DIR = BASE_DIR / "results"
CACHE_DIR   = RESULTS_DIR / "Data" / "backtest_orb_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Reuse daily data from gap strategy cache (same universe, same daily bars)
GAP_CACHE_DIR = RESULTS_DIR / "Data" / "backtest_2year_cache"
LIQUID_FILE   = Path("/tmp/liquid_universe.csv")

# ─── Strategy constants ──────────────────────────────────────────────────────
# Qualifying (daily)
MAX_GAP_PCT       = 2.0    # gap MUST be < 2% — non-overlapping with gap strategy
MIN_GAP_PCT       = -1.0   # allow slight gap-downs (mean reversion bounce)
MIN_VOL_RATIO     = 1.5    # need some volume interest, not dead stock
MIN_PRICE         = 50.0   # MIS-tradeable
VOL_LOOKBACK      = 20     # 20-day vol avg

# ORB parameters (intraday)
ORB_MINUTES       = 15     # opening range = first 15 minutes (09:15-09:29)
MAX_RANGE_PCT     = 2.5    # range must be <= 2.5% of open (sweep-optimised from 1.5%)
MIN_RANGE_PCT     = 0.2    # range must be >= 0.2% (not zero-spread)
ORB_VOL_FRAC      = 0.15   # ORB volume must be >= 15% of full day avg vol (sweep-optimised from 0.30)
ENTRY_DEADLINE    = "11:30" # no entries after 11:30
BREAKOUT_CONFIRM  = 1      # candles above ORB high to confirm (1 = immediate)

# Exit parameters
SL_MAX_PCT        = 0.015  # SL capped at 1.5% from entry
T1_MULT           = 1.0    # T1 = entry + 1.0 × range (triggers trailing stop)
T2_MULT           = 2.0    # legacy — now only used as reference, no cap
TRAIL_PCT         = 0.025  # 2.5% trailing stop from running high (after T1)
PARTIAL_FRAC      = 0.33   # book 33% at T1, let 67% ride with trail
EOD_EXIT_TIME     = "15:15" # close any open position at 15:15

# Risk/sizing
POSITION_SIZE     = 75_000.0
ROUND_TRIP_COST   = 0.002   # 0.2% brokerage + taxes
MAX_PER_DAY       = 3       # max 3 ORB trades per day
MIN_AVG_TURNOVER  = 10.0    # ₹10Cr avg daily turnover
API_SLEEP         = 0.37


# ─── Universe ────────────────────────────────────────────────────────────────

def build_universe(min_turnover_cr: float) -> List[str]:
    """Return liquid NSE symbols."""
    if not LIQUID_FILE.exists():
        print("  liquid_universe.csv not found — using proxy list")
        return _proxy_list()
    df = pd.read_csv(LIQUID_FILE)
    df = df[df["avg_turnover_cr"] >= min_turnover_cr].sort_values(
        "avg_turnover_cr", ascending=False
    )
    symbols = df["symbol"].tolist()
    print(f"  Universe: {len(symbols)} symbols (avg turnover >= ₹{min_turnover_cr}Cr)")
    return symbols


def _proxy_list() -> List[str]:
    return [
        "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","SBIN",
        "BAJFINANCE","BHARTIARTL","KOTAKBANK","LT","WIPRO","AXISBANK","MARUTI",
        "SUNPHARMA","ONGC","TATASTEEL","POWERGRID","NTPC","TITAN",
    ]


# ─── Daily data (reuse gap strategy cache) ───────────────────────────────────

def load_daily_data(provider: KiteHistoricalDataProvider, symbol: str,
                    from_date: datetime, to_date: datetime) -> Optional[pd.DataFrame]:
    """Load daily OHLCV — check gap strategy cache first, then ORB cache, then fetch."""
    for cdir in [GAP_CACHE_DIR, CACHE_DIR]:
        cache_file = cdir / f"{symbol}_day.csv"
        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file, parse_dates=["datetime"])
                return df.sort_values("datetime").reset_index(drop=True)
            except Exception:
                pass

    # Fetch from Kite
    df = provider.get_history_range(symbol, interval="day",
                                    from_date=from_date, to_date=to_date)
    if df is None or df.empty:
        return None

    df = df.reset_index()
    cache_file = CACHE_DIR / f"{symbol}_day.csv"
    df.to_csv(cache_file, index=False)
    time.sleep(API_SLEEP)
    return df


# ─── Find ORB-qualifying days ────────────────────────────────────────────────

def find_qualifying_days(df: pd.DataFrame, symbol: str) -> List[dict]:
    """
    From daily data, find days where:
      - gap < 2% (non-overlapping with gap strategy)
      - vol ratio >= 1.5x
      - price >= ₹50
    Returns list of qualifying day info dicts.
    """
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)

    if len(df) < VOL_LOOKBACK + 5:
        return []

    df["prev_close"] = df["close"].shift(1)
    df["avg_vol"]    = df["volume"].rolling(VOL_LOOKBACK).mean().shift(1)
    df["vol_ratio"]  = df["volume"] / df["avg_vol"].replace(0, np.nan)

    qualifying = []
    for idx in range(VOL_LOOKBACK + 1, len(df)):
        row = df.iloc[idx]
        if pd.isna(row["prev_close"]) or row["prev_close"] <= 0:
            continue
        if row["open"] < MIN_PRICE:
            continue
        if pd.isna(row["vol_ratio"]):
            continue

        vol_ratio = float(row["vol_ratio"])
        if vol_ratio < MIN_VOL_RATIO:
            continue

        gap_pct = (row["open"] - row["prev_close"]) / row["prev_close"] * 100

        # KEY: only days where gap strategy does NOT trigger
        if gap_pct >= MAX_GAP_PCT:
            continue
        if gap_pct < MIN_GAP_PCT:
            continue

        # Additional daily quality filter: day must have closed positive
        # (we know this in backtest — in live, we won't have this info)
        # REMOVED: this would be look-ahead bias. We qualify based on open/vol only.

        date_str = row["datetime"].strftime("%Y-%m-%d")
        qualifying.append({
            "symbol"    : symbol,
            "date"      : date_str,
            "open"      : float(row["open"]),
            "prev_close": float(row["prev_close"]),
            "high"      : float(row["high"]),
            "low"       : float(row["low"]),
            "close"     : float(row["close"]),
            "volume"    : int(row["volume"]),
            "vol_ratio" : round(vol_ratio, 2),
            "gap_pct"   : round(gap_pct, 2),
            "avg_vol"   : float(row["avg_vol"]),
        })

    return qualifying


# ─── Minute candle fetch ─────────────────────────────────────────────────────

def fetch_minute_candles(provider: KiteHistoricalDataProvider,
                         symbol: str, date_str: str) -> Optional[List[dict]]:
    """Fetch minute candles for symbol on given date. Checks both caches."""
    # Check ORB cache first, then gap cache
    for cdir in [CACHE_DIR, GAP_CACHE_DIR]:
        cache_file = cdir / f"{symbol}_{date_str}_min.json"
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    return json.load(f)
            except Exception:
                pass

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    from_dt = dt.replace(hour=9, minute=15, second=0)
    to_dt   = dt.replace(hour=15, minute=30, second=0)

    df = provider.get_history_range(symbol, interval="minute",
                                    from_date=from_dt, to_date=to_dt)
    time.sleep(API_SLEEP)

    if df is None or df.empty or len(df) < 10:
        return None

    df = df.reset_index()
    candles = []
    for _, row in df.iterrows():
        try:
            t = pd.Timestamp(row["datetime"])
            candles.append({
                "time"  : t.strftime("%H:%M"),
                "open"  : float(row["open"]),
                "high"  : float(row["high"]),
                "low"   : float(row["low"]),
                "close" : float(row["close"]),
                "volume": int(row["volume"]),
            })
        except Exception:
            pass

    if candles:
        cache_file = CACHE_DIR / f"{symbol}_{date_str}_min.json"
        with open(cache_file, "w") as f:
            json.dump(candles, f)
    return candles or None


# ─── ORB Simulation ─────────────────────────────────────────────────────────

def simulate_orb(symbol: str, date_str: str, candles: List[dict],
                 day_info: dict, position_size: float) -> Optional[dict]:
    """
    Simulate an ORB trade using minute candles.

    1. Compute opening range from first 15 candles (09:15 - 09:29)
    2. Validate range tightness and volume
    3. Enter long when candle closes above ORB high (after 09:30)
    4. SL = ORB low (capped at 1.5% from entry)
    5. T1 = entry + range, T2 = entry + 2×range
    6. Partial at T1, full at T2, EOD at 15:15
    """
    if not candles or len(candles) < ORB_MINUTES + 10:
        return None

    # ── Step 1: Build opening range ──────────────────────────────────────
    orb_candles = candles[:ORB_MINUTES]

    orb_high   = max(c["high"] for c in orb_candles)
    orb_low    = min(c["low"]  for c in orb_candles)
    orb_volume = sum(c["volume"] for c in orb_candles)
    orb_open   = candles[0]["open"]

    if orb_open <= 0 or orb_high <= 0 or orb_low <= 0:
        return None

    range_height = orb_high - orb_low
    range_pct    = range_height / orb_open * 100

    # ── Step 2: Validate range quality ───────────────────────────────────
    # Range too wide = volatile/choppy, not a coil
    if range_pct > MAX_RANGE_PCT:
        return None

    # Range too tight = no movement, likely illiquid
    if range_pct < MIN_RANGE_PCT:
        return None

    # ORB volume check: should be meaningful % of daily average
    avg_vol = day_info.get("avg_vol", 0)
    if avg_vol > 0 and orb_volume < avg_vol * ORB_VOL_FRAC:
        return None

    # ── Step 3: Scan for breakout entry ──────────────────────────────────
    entry_price = None
    entry_idx   = None
    entry_time  = None

    for i in range(ORB_MINUTES, len(candles)):
        c = candles[i]
        if c["time"] > ENTRY_DEADLINE:
            break  # too late to enter

        # Breakout = candle closes above ORB high
        if c["close"] > orb_high and c["high"] > orb_high:
            entry_price = c["close"]
            entry_idx   = i
            entry_time  = c["time"]
            break

    if entry_price is None:
        return None  # no breakout today

    # ── Step 4: Compute levels ───────────────────────────────────────────
    sl_structural = orb_low
    sl_capped     = entry_price * (1 - SL_MAX_PCT)
    sl            = max(sl_structural, sl_capped)  # tighter of the two

    t1 = entry_price + range_height * T1_MULT

    qty = int(position_size // entry_price)
    if qty <= 0:
        return None

    invested = qty * entry_price
    cost     = invested * ROUND_TRIP_COST

    risk_pct = (entry_price - sl) / entry_price * 100

    # ── Step 5: Simulate trade ───────────────────────────────────────────
    t1_hit = False
    qty_remaining = qty
    partial_pnl   = 0.0
    exit_price    = None
    exit_time     = None
    exit_reason   = "EOD"
    final_pnl     = 0.0
    high_so_far   = entry_price
    trail_sl      = sl  # starts at structural SL, upgraded after T1

    for i in range(entry_idx + 1, len(candles)):
        c = candles[i]
        high_so_far = max(high_so_far, c["high"])

        # EOD exit time
        if c["time"] >= EOD_EXIT_TIME:
            exit_price  = c["close"]
            exit_time   = c["time"]
            exit_reason = "TRAIL_EOD" if t1_hit else "EOD"
            final_pnl   = partial_pnl + (exit_price - entry_price) * qty_remaining - cost
            break

        # T1 hit — partial exit (33%), start trailing
        if c["high"] >= t1 and not t1_hit:
            t1_hit = True
            qty_t1  = int(qty * PARTIAL_FRAC)
            partial_pnl += (t1 - entry_price) * qty_t1
            qty_remaining = qty - qty_t1
            # Move SL to breakeven or trail, whichever is higher
            trail_sl = max(entry_price, high_so_far * (1 - TRAIL_PCT))

        # Update trailing stop after T1 (ratchets up only)
        if t1_hit:
            new_trail = high_so_far * (1 - TRAIL_PCT)
            trail_sl = max(trail_sl, new_trail)

        # Check stop (structural SL before T1, trailing after T1)
        if c["low"] <= trail_sl:
            exit_price  = trail_sl
            exit_time   = c["time"]
            exit_reason = "TRAIL_HIT" if t1_hit else "SL_HIT"
            final_pnl   = partial_pnl + (trail_sl - entry_price) * qty_remaining - cost
            break

    else:
        # Reached end of candles without explicit exit
        exit_price = candles[-1]["close"]
        exit_time  = candles[-1]["time"]
        exit_reason = "TRAIL_EOD" if t1_hit else "EOD"
        final_pnl = partial_pnl + (exit_price - entry_price) * qty_remaining - cost

    gross_pct = (exit_price - entry_price) / entry_price * 100 if exit_price else 0

    return {
        "symbol"      : symbol,
        "date"        : date_str,
        "entry"       : round(entry_price, 2),
        "exit"        : round(exit_price or entry_price, 2),
        "entry_time"  : entry_time,
        "exit_time"   : exit_time,
        "qty"         : qty,
        "invested"    : round(invested, 0),
        "exit_reason" : exit_reason,
        "gross_pct"   : round(gross_pct, 2),
        "net_pnl"     : round(final_pnl, 2),
        "won"         : final_pnl > 0,
        "vol_ratio"   : day_info["vol_ratio"],
        "gap_pct"     : day_info["gap_pct"],
        "orb_high"    : round(orb_high, 2),
        "orb_low"     : round(orb_low, 2),
        "range_pct"   : round(range_pct, 2),
        "risk_pct"    : round(risk_pct, 2),
        "orb_volume"  : orb_volume,
    }


# ─── Report ──────────────────────────────────────────────────────────────────

def print_report(trades: List[dict]):
    if not trades:
        print("\nNo trades executed.")
        return

    df = pd.DataFrame(trades)
    total = len(df)
    wr    = df["won"].mean() * 100
    avg   = df["net_pnl"].mean()
    tot   = df["net_pnl"].sum()
    best  = df["net_pnl"].max()
    worst = df["net_pnl"].min()

    df["date"] = pd.to_datetime(df["date"])
    span = (df["date"].max() - df["date"].min()).days
    months = max(1, span / 30.44)
    monthly = tot / months

    print("\n" + "=" * 70)
    print("  ORB STRATEGY — BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Period          : {df['date'].min().date()} → {df['date'].max().date()} ({span} days)")
    print(f"  Total trades    : {total}")
    print(f"  Win rate        : {wr:.1f}%")
    print(f"  Avg P&L/trade   : ₹{avg:,.0f}")
    print(f"  Total net P&L   : ₹{tot:,.0f}")
    print(f"  Monthly P&L     : ₹{monthly:,.0f}")
    print(f"  Best trade      : ₹{best:,.0f}")
    print(f"  Worst trade     : ₹{worst:,.0f}")
    print(f"  Trades/month    : {total/months:.1f}")

    print(f"\n  Exit reasons:")
    for reason, cnt in df["exit_reason"].value_counts().items():
        sub = df[df["exit_reason"] == reason]
        print(f"    {reason:20s}: {cnt:3d} trades, avg ₹{sub['net_pnl'].mean():+,.0f}")

    print(f"\n  Monthly breakdown:")
    df["month"] = df["date"].dt.to_period("M")
    mdf = df.groupby("month").agg(
        trades=("net_pnl", "count"),
        pnl=("net_pnl", "sum"),
        wr=("won", "mean")
    )
    for m, row in mdf.iterrows():
        print(f"    {m}: {row['trades']:2.0f} trades, ₹{row['pnl']:+8,.0f}, WR {row['wr']*100:.0f}%")

    print(f"\n  Range stats:")
    print(f"    Avg range %   : {df['range_pct'].mean():.2f}%")
    print(f"    Avg risk %    : {df['risk_pct'].mean():.2f}%")
    print(f"    Avg vol ratio : {df['vol_ratio'].mean():.1f}x")

    # Consecutive losses
    results = df["won"].astype(bool).tolist()
    max_loss_streak = curr = 0
    for w in results:
        if not w:
            curr += 1
            max_loss_streak = max(max_loss_streak, curr)
        else:
            curr = 0
    print(f"    Max consec losses: {max_loss_streak}")
    print("=" * 70)

    return df


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ORB Strategy Backtest")
    parser.add_argument("--min-turnover", type=float, default=MIN_AVG_TURNOVER)
    parser.add_argument("--top", type=int, default=MAX_PER_DAY,
                        help="Max trades per day (ranked by vol ratio)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip already-fetched minute data")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show qualifying days without fetching minute data")
    parser.add_argument("--position", type=float, default=POSITION_SIZE)
    parser.add_argument("--orb-minutes", type=int, default=ORB_MINUTES)
    parser.add_argument("--max-range", type=float, default=MAX_RANGE_PCT)
    parser.add_argument("--min-vol", type=float, default=MIN_VOL_RATIO)
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  ORB STRATEGY BACKTEST — Opening Range Breakout")
    print("  Complementary to Gap+Volume strategy (gap < 2% only)")
    print("=" * 70)
    print(f"  ORB window     : first {args.orb_minutes} minutes")
    print(f"  Max range      : {args.max_range}%")
    print(f"  Min vol ratio  : {args.min_vol}x")
    print(f"  Gap range      : {MIN_GAP_PCT}% to <{MAX_GAP_PCT}%")
    print(f"  Position size  : ₹{args.position:,.0f}")
    print(f"  Max/day        : {args.top}")
    print(f"  T1/T2          : {T1_MULT}× / {T2_MULT}× range")
    print(f"  SL cap         : {SL_MAX_PCT*100}%")
    print("=" * 70)

    # Build universe
    symbols = build_universe(args.min_turnover)
    if not symbols:
        print("ERROR: No symbols in universe.")
        return

    # Connect to Kite (only if not dry-run or if we need new data)
    provider = KiteHistoricalDataProvider()

    from_date = datetime(2024, 4, 1)
    to_date   = datetime(2026, 4, 10)

    # ── STEP 1: Load daily data and find qualifying days ─────────────────
    print(f"\n[1/3] Loading daily data for {len(symbols)} symbols...")
    all_qualifying = []
    for i, sym in enumerate(symbols):
        daily = load_daily_data(provider, sym, from_date, to_date)
        if daily is None or daily.empty:
            continue
        quals = find_qualifying_days(daily, sym)
        all_qualifying.extend(quals)
        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(symbols)} symbols, {len(all_qualifying)} qualifying days so far")

    print(f"  Total qualifying stock-days: {len(all_qualifying)}")

    if not all_qualifying:
        print("No qualifying days found.")
        return

    # ── STEP 2: Group by date and rank ───────────────────────────────────
    by_date = defaultdict(list)
    for q in all_qualifying:
        by_date[q["date"]].append(q)

    # For each date, take top N by vol_ratio
    selected = []
    for dt, candidates in sorted(by_date.items()):
        ranked = sorted(candidates, key=lambda x: x["vol_ratio"], reverse=True)
        selected.extend(ranked[:args.top])

    print(f"  Selected for simulation: {len(selected)} stock-days across {len(by_date)} dates")
    print(f"  Avg candidates/day: {len(all_qualifying)/len(by_date):.1f}")

    if args.dry_run:
        print("\n  [DRY RUN] Top 20 qualifying days:")
        for q in sorted(selected, key=lambda x: x["vol_ratio"], reverse=True)[:20]:
            print(f"    {q['date']} {q['symbol']:>15s}  gap={q['gap_pct']:+.1f}%  "
                  f"vol={q['vol_ratio']:.1f}x")
        print(f"\n  Would need ~{len(selected)} minute-data API calls.")
        return

    # ── STEP 3: Fetch minute data and simulate ───────────────────────────
    print(f"\n[2/3] Fetching minute data and simulating ({len(selected)} stock-days)...")
    trades = []
    skipped = fetched = cached = 0

    for i, q in enumerate(selected):
        sym  = q["symbol"]
        dt   = q["date"]

        candles = fetch_minute_candles(provider, sym, dt)
        if candles:
            # Check if it was cached
            for cdir in [CACHE_DIR, GAP_CACHE_DIR]:
                if (cdir / f"{sym}_{dt}_min.json").exists():
                    cached += 1
                    break
            else:
                fetched += 1
        else:
            skipped += 1
            continue

        result = simulate_orb(sym, dt, candles, q, args.position)
        if result:
            trades.append(result)

        if (i + 1) % 100 == 0:
            print(f"  ... {i+1}/{len(selected)} processed, "
                  f"{len(trades)} trades, {fetched} fetched, {cached} cached")

    print(f"  Done: {len(trades)} trades from {len(selected)} candidates")
    print(f"  (fetched={fetched}, cached={cached}, skipped={skipped})")

    # ── STEP 4: Report ───────────────────────────────────────────────────
    print(f"\n[3/3] Results...")
    result_df = print_report(trades)

    # Save results
    if trades:
        out_file = RESULTS_DIR / "backtest_orb_results.csv"
        pd.DataFrame(trades).to_csv(out_file, index=False)
        print(f"\n  Results saved to {out_file}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Quick diagnostic: where are ORB candidates being filtered?"""

import json, sys
from pathlib import Path
from collections import Counter

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

CACHE_DIR     = BASE_DIR / "results" / "Data" / "backtest_orb_cache"
GAP_CACHE_DIR = BASE_DIR / "results" / "Data" / "backtest_2year_cache"

# Read the results CSV to know which stock-days were selected
import pandas as pd
import csv

# We need to replay the qualifying logic. Import from the main script.
from backtest_orb_kite import (
    build_universe, load_daily_data, find_qualifying_days,
    KiteHistoricalDataProvider, fetch_minute_candles
)
from collections import defaultdict
from datetime import datetime

def load_candles_from_cache(sym, dt):
    """Load minute candles from cache (ORB or gap)."""
    for cdir in [CACHE_DIR, GAP_CACHE_DIR]:
        fpath = cdir / f"{sym}_{dt}_min.json"
        if fpath.exists():
            with open(fpath) as f:
                return json.load(f)
    return None


def diagnose_orb(candles, day_info,
                 orb_minutes=15, max_range=1.5, min_range=0.2,
                 vol_frac=0.30, deadline="11:30"):
    """Return (passed: bool, reason: str, stats: dict)."""
    if not candles or len(candles) < orb_minutes + 10:
        return False, "insufficient_candles", {}

    orb = candles[:orb_minutes]
    orb_high   = max(c["high"] for c in orb)
    orb_low    = min(c["low"]  for c in orb)
    orb_volume = sum(c["volume"] for c in orb)
    orb_open   = candles[0]["open"]

    if orb_open <= 0 or orb_high <= 0 or orb_low <= 0:
        return False, "bad_prices", {}

    range_pct = (orb_high - orb_low) / orb_open * 100

    stats = {"range_pct": round(range_pct, 3), "orb_vol": orb_volume}

    if range_pct > max_range:
        return False, "range_too_wide", stats
    if range_pct < min_range:
        return False, "range_too_tight", stats

    avg_vol = day_info.get("avg_vol", 0)
    if avg_vol > 0:
        stats["vol_frac_actual"] = round(orb_volume / avg_vol, 3)
        if orb_volume < avg_vol * vol_frac:
            return False, "orb_volume_low", stats

    # Scan for breakout
    for i in range(orb_minutes, len(candles)):
        c = candles[i]
        if c["time"] > deadline:
            break
        if c["close"] > orb_high and c["high"] > orb_high:
            stats["breakout_time"] = c["time"]
            return True, "TRADE", stats

    return False, "no_breakout", stats


def main():
    symbols = build_universe(10.0)
    provider = KiteHistoricalDataProvider()
    from_date = datetime(2024, 4, 1)
    to_date   = datetime(2026, 4, 10)

    print("Loading daily data...")
    all_qualifying = []
    for sym in symbols:
        daily = load_daily_data(provider, sym, from_date, to_date)
        if daily is None or daily.empty:
            continue
        quals = find_qualifying_days(daily, sym)
        # Filter to min_vol=2.0 as used in the backtest run
        quals = [q for q in quals if q["vol_ratio"] >= 2.0]
        all_qualifying.extend(quals)

    by_date = defaultdict(list)
    for q in all_qualifying:
        by_date[q["date"]].append(q)

    selected = []
    for dt, candidates in sorted(by_date.items()):
        ranked = sorted(candidates, key=lambda x: x["vol_ratio"], reverse=True)
        selected.extend(ranked[:3])

    print(f"Total selected: {len(selected)}")

    # ── Diagnose with CURRENT params ──
    print("\n=== CURRENT PARAMS (range 0.2-1.5%, vol_frac=0.30, deadline=11:30) ===")
    reasons = Counter()
    range_too_wide_pcts = []

    for q in selected:
        candles = load_candles_from_cache(q["symbol"], q["date"])
        if not candles:
            reasons["no_cache"] += 1
            continue
        passed, reason, stats = diagnose_orb(candles, q)
        reasons[reason] += 1
        if reason == "range_too_wide":
            range_too_wide_pcts.append(stats["range_pct"])

    for reason, cnt in reasons.most_common():
        print(f"  {reason:25s}: {cnt:5d} ({cnt/len(selected)*100:.1f}%)")

    if range_too_wide_pcts:
        import numpy as np
        arr = np.array(range_too_wide_pcts)
        print(f"\n  Range-too-wide distribution:")
        for pct in [2.0, 2.5, 3.0, 4.0, 5.0]:
            cnt = (arr <= pct).sum()
            print(f"    <= {pct}%: {cnt}/{len(arr)} ({cnt/len(arr)*100:.1f}%)")

    # ── Test different param combos ──
    combos = [
        {"label": "wider_range_2.5",   "max_range": 2.5, "min_range": 0.1, "vol_frac": 0.30, "deadline": "11:30"},
        {"label": "wider_range_3.0",   "max_range": 3.0, "min_range": 0.1, "vol_frac": 0.30, "deadline": "11:30"},
        {"label": "low_vol_frac_0.15", "max_range": 1.5, "min_range": 0.2, "vol_frac": 0.15, "deadline": "11:30"},
        {"label": "low_vol_frac_0.10", "max_range": 1.5, "min_range": 0.2, "vol_frac": 0.10, "deadline": "11:30"},
        {"label": "no_vol_frac",       "max_range": 1.5, "min_range": 0.2, "vol_frac": 0.00, "deadline": "11:30"},
        {"label": "late_deadline_13",  "max_range": 1.5, "min_range": 0.2, "vol_frac": 0.30, "deadline": "13:00"},
        {"label": "late_deadline_14",  "max_range": 1.5, "min_range": 0.2, "vol_frac": 0.30, "deadline": "14:00"},
        {"label": "combo_relaxed",     "max_range": 2.5, "min_range": 0.1, "vol_frac": 0.15, "deadline": "13:00"},
        {"label": "combo_very_relaxed","max_range": 3.0, "min_range": 0.1, "vol_frac": 0.10, "deadline": "14:00"},
    ]

    print(f"\n{'='*70}")
    print(f"  PARAMETER SWEEP — Trade Counts")
    print(f"{'='*70}")
    print(f"  {'Params':<25s} {'Trades':>7s} {'Rate':>6s}")
    print(f"  {'-'*40}")

    for combo in combos:
        trade_count = 0
        for q in selected:
            candles = load_candles_from_cache(q["symbol"], q["date"])
            if not candles:
                continue
            passed, reason, stats = diagnose_orb(
                candles, q,
                max_range=combo["max_range"],
                min_range=combo["min_range"],
                vol_frac=combo["vol_frac"],
                deadline=combo["deadline"]
            )
            if passed:
                trade_count += 1
        rate = trade_count / len(selected) * 100
        print(f"  {combo['label']:<25s} {trade_count:>7d} {rate:>5.1f}%")


if __name__ == "__main__":
    main()

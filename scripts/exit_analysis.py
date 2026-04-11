#!/usr/bin/env python3
"""
Exit Optimization Analysis — Gap + ORB Strategies
===================================================
For each historical trade, replay minute candles to measure:
1. How much further price went AFTER fixed T2 exit
2. Maximum favourable excursion (MFE) vs actual P&L captured
3. Maximum adverse excursion (MAE) for trailing stop design
4. Time spent above T1 vs actual hold time
5. EOD close vs peak — quantify EOD exits leaving money

This tells us exactly where fixed exits leave money on the table.
"""

import json, sys, csv
from pathlib import Path
from collections import defaultdict
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

GAP_CACHE  = BASE_DIR / "results" / "Data" / "backtest_2year_cache"
ORB_CACHE  = BASE_DIR / "results" / "Data" / "backtest_orb_cache"
GAP_RESULTS = BASE_DIR / "results" / "backtest_2year_optimized.csv"
ORB_RESULTS = BASE_DIR / "results" / "backtest_orb_results.csv"


def load_candles(sym, dt, strategy):
    """Load minute candles from cache."""
    caches = [ORB_CACHE, GAP_CACHE] if strategy == "ORB" else [GAP_CACHE, ORB_CACHE]
    for cdir in caches:
        fpath = cdir / f"{sym}_{dt}_min.json"
        if fpath.exists():
            with open(fpath) as f:
                return json.load(f)
    return None


def analyze_gap_trades():
    """Deep analysis of gap strategy exit quality."""
    df = pd.read_csv(GAP_RESULTS)
    print(f"\n{'='*70}")
    print(f"  GAP STRATEGY — EXIT ANALYSIS ({len(df)} trades)")
    print(f"{'='*70}")

    # Current exit breakdown
    print(f"\n  Current exits:")
    for reason, g in df.groupby("exit_reason"):
        print(f"    {reason:18s}: {len(g):3d} trades, avg ₹{g['net_pnl'].mean():+,.0f}")

    # For each trade, replay candles to find MFE/MAE
    mfe_list = []  # max favourable excursion %
    mae_list = []  # max adverse excursion %
    mfe_after_t2 = []  # how much higher after T2 exit
    eod_vs_peak = []  # for EOD exits: close vs peak
    t2_exits = []
    eod_exits = []
    sl_exits = []

    for _, row in df.iterrows():
        candles = load_candles(row["symbol"], row["date"], "GAP")
        if not candles:
            continue

        entry = candles[0]["open"]
        if entry <= 0:
            continue

        t1 = entry * 1.015  # 1.5%
        t2 = entry * 1.03   # 3%
        sl = entry * 0.99   # 1%

        # Track price trajectory
        highs = [c["high"] for c in candles[1:]]
        lows  = [c["low"]  for c in candles[1:]]
        closes = [c["close"] for c in candles[1:]]

        if not highs:
            continue

        peak = max(highs)
        valley = min(lows)
        eod_close = closes[-1] if closes else entry

        mfe_pct = (peak - entry) / entry * 100
        mae_pct = (entry - valley) / entry * 100
        mfe_list.append(mfe_pct)
        mae_list.append(mae_pct)

        exit_reason = row["exit_reason"]

        if exit_reason == "T2_HIT":
            # How much further did it go after T2?
            t2_idx = None
            for i, h in enumerate(highs):
                if h >= t2:
                    t2_idx = i
                    break
            if t2_idx is not None:
                remaining_highs = highs[t2_idx:]
                if remaining_highs:
                    post_t2_peak = max(remaining_highs)
                    extra_pct = (post_t2_peak - t2) / t2 * 100
                    mfe_after_t2.append(extra_pct)
                    t2_exits.append({
                        "symbol": row["symbol"],
                        "date": row["date"],
                        "entry": entry,
                        "t2": t2,
                        "peak": peak,
                        "post_t2_peak": post_t2_peak,
                        "mfe_pct": mfe_pct,
                        "extra_pct": extra_pct,
                        "eod_close": eod_close,
                        "eod_vs_entry_pct": (eod_close - entry) / entry * 100,
                    })

        elif exit_reason in ("EOD", "T1_PARTIAL_EOD"):
            gap_from_peak = (peak - eod_close) / peak * 100
            eod_vs_peak.append({
                "symbol": row["symbol"],
                "date": row["date"],
                "entry": entry,
                "peak": peak,
                "eod_close": eod_close,
                "mfe_pct": mfe_pct,
                "eod_gap_from_peak_pct": gap_from_peak,
                "eod_vs_entry_pct": (eod_close - entry) / entry * 100,
            })
            eod_exits.append({
                "mfe_pct": mfe_pct,
                "captured_pct": (eod_close - entry) / entry * 100,
            })

        elif exit_reason == "SL_HIT":
            # Was there a recovery after SL? (for trailing stop analysis)
            sl_exits.append({
                "symbol": row["symbol"],
                "date": row["date"],
                "mae_pct": mae_pct,
                "mfe_pct": mfe_pct,
                "peak_before_sl": peak,
            })

    # Results
    mfe_arr = np.array(mfe_list)
    mae_arr = np.array(mae_list)

    print(f"\n  MFE (Max Favourable Excursion) from entry:")
    for pct in [1, 2, 3, 4, 5, 7, 10]:
        cnt = (mfe_arr >= pct).sum()
        print(f"    >= {pct:2d}%: {cnt:3d}/{len(mfe_arr)} ({cnt/len(mfe_arr)*100:.0f}%)")
    print(f"    Avg MFE: {mfe_arr.mean():.2f}%")
    print(f"    Median MFE: {np.median(mfe_arr):.2f}%")

    print(f"\n  MAE (Max Adverse Excursion) from entry:")
    for pct in [0.5, 1.0, 1.5, 2.0, 3.0]:
        cnt = (mae_arr >= pct).sum()
        print(f"    >= {pct:.1f}%: {cnt:3d}/{len(mae_arr)} ({cnt/len(mae_arr)*100:.0f}%)")

    if mfe_after_t2:
        arr = np.array(mfe_after_t2)
        print(f"\n  T2 exits — additional upside LEFT ON TABLE after T2 (3%):")
        print(f"    n = {len(arr)}")
        print(f"    Avg extra: {arr.mean():.2f}%")
        print(f"    Median extra: {np.median(arr):.2f}%")
        for pct in [0.5, 1, 2, 3, 5]:
            cnt = (arr >= pct).sum()
            print(f"    >= {pct}% more: {cnt}/{len(arr)} ({cnt/len(arr)*100:.0f}%)")

        print(f"\n  Top 10 T2 exits with most upside left:")
        t2_sorted = sorted(t2_exits, key=lambda x: x["extra_pct"], reverse=True)[:10]
        for t in t2_sorted:
            print(f"    {t['date']} {t['symbol']:>12s}: "
                  f"entry={t['entry']:.1f} T2={t['t2']:.1f} "
                  f"post_peak={t['post_t2_peak']:.1f} extra={t['extra_pct']:+.1f}% "
                  f"EOD_close={t['eod_close']:.1f} ({t['eod_vs_entry_pct']:+.1f}%)")

    if eod_exits:
        print(f"\n  EOD exits — captured vs available:")
        eod_df = pd.DataFrame(eod_exits)
        print(f"    n = {len(eod_df)}")
        print(f"    Avg MFE available: {eod_df['mfe_pct'].mean():.2f}%")
        print(f"    Avg actually captured: {eod_df['captured_pct'].mean():.2f}%")
        print(f"    Avg left on table (MFE - captured): {(eod_df['mfe_pct'] - eod_df['captured_pct']).mean():.2f}%")

    return mfe_arr, mae_arr


def analyze_orb_trades():
    """Deep analysis of ORB strategy exit quality."""
    df = pd.read_csv(ORB_RESULTS)
    print(f"\n{'='*70}")
    print(f"  ORB STRATEGY — EXIT ANALYSIS ({len(df)} trades)")
    print(f"{'='*70}")

    print(f"\n  Current exits:")
    for reason, g in df.groupby("exit_reason"):
        print(f"    {reason:18s}: {len(g):3d} trades, avg ₹{g['net_pnl'].mean():+,.0f}")

    mfe_list = []
    mae_list = []
    mfe_after_t2 = []
    eod_exits = []
    t2_exits = []
    trail_analysis = []  # for trailing stop design

    for _, row in df.iterrows():
        candles = load_candles(row["symbol"], row["date"], "ORB")
        if not candles:
            continue

        entry = row["entry"]
        orb_high = row["orb_high"]
        orb_low  = row["orb_low"]
        range_height = orb_high - orb_low
        entry_time = row["entry_time"]

        if entry <= 0 or range_height <= 0:
            continue

        t1 = entry + range_height * 1.0
        t2 = entry + range_height * 2.0
        sl = max(orb_low, entry * (1 - 0.015))

        # Find entry candle index
        entry_idx = None
        for i, c in enumerate(candles):
            if c["time"] >= entry_time:
                entry_idx = i
                break
        if entry_idx is None:
            continue

        post_entry = candles[entry_idx + 1:]
        if not post_entry:
            continue

        # Cut at 15:15
        eod_candles = [c for c in post_entry if c["time"] <= "15:15"]
        if not eod_candles:
            continue

        highs  = [c["high"] for c in eod_candles]
        lows   = [c["low"]  for c in eod_candles]
        closes = [c["close"] for c in eod_candles]

        peak = max(highs)
        valley = min(lows)
        eod_close = closes[-1]

        mfe_pct = (peak - entry) / entry * 100
        mae_pct = (entry - valley) / entry * 100
        mfe_list.append(mfe_pct)
        mae_list.append(mae_pct)

        exit_reason = row["exit_reason"]

        # For trailing stop design: record the price path
        running_high = entry
        drawdowns_from_high = []
        for c in eod_candles:
            running_high = max(running_high, c["high"])
            if running_high > entry:
                dd = (running_high - c["low"]) / running_high * 100
                drawdowns_from_high.append(dd)

        if drawdowns_from_high:
            trail_analysis.append({
                "symbol": row["symbol"],
                "date": row["date"],
                "mfe_pct": mfe_pct,
                "max_dd_from_high": max(drawdowns_from_high),
                "exit_reason": exit_reason,
                "won": row["won"],
                "net_pnl": row["net_pnl"],
            })

        if exit_reason == "T2_HIT":
            t2_idx = None
            for i, h in enumerate(highs):
                if h >= t2:
                    t2_idx = i
                    break
            if t2_idx is not None:
                remaining_highs = highs[t2_idx:]
                post_t2_peak = max(remaining_highs) if remaining_highs else t2
                extra_pct = (post_t2_peak - t2) / t2 * 100
                mfe_after_t2.append(extra_pct)
                t2_exits.append({
                    "symbol": row["symbol"], "date": row["date"],
                    "entry": entry, "t2": t2, "peak": peak,
                    "post_t2_peak": post_t2_peak,
                    "extra_pct": extra_pct,
                    "eod_close": eod_close,
                    "eod_vs_entry_pct": (eod_close - entry) / entry * 100,
                    "range_pct": row["range_pct"],
                })

        elif exit_reason in ("EOD", "T1_PARTIAL_EOD"):
            eod_exits.append({
                "symbol": row["symbol"], "date": row["date"],
                "mfe_pct": mfe_pct,
                "captured_pct": (eod_close - entry) / entry * 100,
                "range_pct": row["range_pct"],
            })

    mfe_arr = np.array(mfe_list)
    mae_arr = np.array(mae_list)

    print(f"\n  MFE (Max Favourable Excursion) from entry:")
    for pct in [1, 2, 3, 4, 5, 7]:
        cnt = (mfe_arr >= pct).sum()
        print(f"    >= {pct:2d}%: {cnt:3d}/{len(mfe_arr)} ({cnt/len(mfe_arr)*100:.0f}%)")
    print(f"    Avg MFE: {mfe_arr.mean():.2f}%")
    print(f"    Median MFE: {np.median(mfe_arr):.2f}%")

    print(f"\n  MAE (Max Adverse Excursion) from entry:")
    for pct in [0.5, 1.0, 1.5, 2.0, 3.0]:
        cnt = (mae_arr >= pct).sum()
        print(f"    >= {pct:.1f}%: {cnt:3d}/{len(mae_arr)} ({cnt/len(mae_arr)*100:.0f}%)")

    if mfe_after_t2:
        arr = np.array(mfe_after_t2)
        print(f"\n  T2 exits — additional upside LEFT ON TABLE after T2 (2× range):")
        print(f"    n = {len(arr)}")
        print(f"    Avg extra: {arr.mean():.2f}%")
        print(f"    Median extra: {np.median(arr):.2f}%")
        for pct in [0.5, 1, 2, 3]:
            cnt = (arr >= pct).sum()
            print(f"    >= {pct}% more: {cnt}/{len(arr)} ({cnt/len(arr)*100:.0f}%)")

    if eod_exits:
        eod_df = pd.DataFrame(eod_exits)
        print(f"\n  EOD exits — captured vs available:")
        print(f"    n = {len(eod_df)}")
        print(f"    Avg MFE available: {eod_df['mfe_pct'].mean():.2f}%")
        print(f"    Avg actually captured: {eod_df['captured_pct'].mean():.2f}%")
        print(f"    Avg left on table: {(eod_df['mfe_pct'] - eod_df['captured_pct']).mean():.2f}%")

    # Trailing stop analysis
    if trail_analysis:
        ta_df = pd.DataFrame(trail_analysis)
        winners = ta_df[ta_df["won"] == True]
        losers  = ta_df[ta_df["won"] == False]
        print(f"\n  Trailing stop design (drawdown from running high):")
        print(f"    Winners max DD from high: avg {winners['max_dd_from_high'].mean():.2f}%, "
              f"median {winners['max_dd_from_high'].median():.2f}%, "
              f"p75 {winners['max_dd_from_high'].quantile(0.75):.2f}%")
        print(f"    Losers max DD from high:  avg {losers['max_dd_from_high'].mean():.2f}%, "
              f"median {losers['max_dd_from_high'].median():.2f}%, "
              f"p75 {losers['max_dd_from_high'].quantile(0.75):.2f}%")

        # Optimal trailing stop: what % drawdown captures most winners?
        print(f"\n    If trailing stop = X% from high, how many winners survive?")
        for trail_pct in [0.3, 0.5, 0.7, 1.0, 1.2, 1.5, 2.0]:
            w_survive = (winners["max_dd_from_high"] <= trail_pct).sum()
            l_stopped = (losers["max_dd_from_high"] > trail_pct).sum()
            print(f"      {trail_pct:.1f}%: {w_survive}/{len(winners)} winners survive, "
                  f"{l_stopped}/{len(losers)} losers stopped out")

    return mfe_arr, mae_arr


if __name__ == "__main__":
    gap_mfe, gap_mae = analyze_gap_trades()
    orb_mfe, orb_mae = analyze_orb_trades()

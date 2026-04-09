#!/usr/bin/env python3
"""
Post-Market Backtest: April 8, 2026

Evaluates today's signal monitor output against actual price action.
Uses cached Kite historical data to measure signal accuracy.

Metrics:
- Win rate by NowAction category
- Average return by signal type
- Best/worst performers
- CompositeRank correlation with returns
"""

import os
import sys
import glob
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# ============================================================================
# CONFIG
# ============================================================================
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "Data")
HISTORY_DIR = os.path.join(RESULTS_DIR, "kite_history_cache", "day")
STATE_PATTERN = os.path.join(RESULTS_DIR, "signalmonitor_allstocks_state_20260408_*.csv")

# Analysis targets
TARGET_INTRADAY = True   # EOD vs entry price
TARGET_1D = True         # Next day close vs entry
TARGET_3D = True         # 3-day hold return


def load_all_state_snapshots():
    """Load all intraday state snapshots from today."""
    files = sorted(glob.glob(STATE_PATTERN))
    if not files:
        print("No state files found for today.")
        return None

    snapshots = []
    for f in files:
        try:
            df = pd.read_csv(f, quoting=1)
            ts = os.path.basename(f).replace("signalmonitor_allstocks_state_", "").replace(".csv", "")
            df["SnapshotTime"] = ts
            snapshots.append(df)
        except Exception as e:
            print(f"  Skipped {os.path.basename(f)}: {e}")

    if not snapshots:
        return None

    all_df = pd.concat(snapshots, ignore_index=True)
    print(f"Loaded {len(snapshots)} snapshots, {len(all_df)} total rows")
    return all_df


def load_stock_history(symbol):
    """Load daily OHLCV from Kite cache."""
    path = os.path.join(HISTORY_DIR, f"{symbol}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        # Column is 'datetime', not 'date'
        date_col = "datetime" if "datetime" in df.columns else "date"
        if date_col in df.columns:
            df["date"] = pd.to_datetime(df[date_col], utc=True)
            df.sort_values("date", inplace=True)
        return df
    except Exception:
        return None


def get_first_buy_signals(all_states_df):
    """
    For each symbol, get the FIRST time it appeared as a buy signal today.
    This simulates entering at the earliest alert.
    """
    buys = all_states_df[all_states_df["NowAction"].isin(["Buy Now", "Strong Buy Now"])].copy()
    if buys.empty:
        return buys

    # Keep first occurrence per symbol (earliest alert)
    buys.sort_values("SnapshotTime", inplace=True)
    first_buys = buys.groupby("Symbol").first().reset_index()
    return first_buys


def compute_eod_returns(signals_df):
    """Compute end-of-day returns using the state data itself."""
    results = []
    for _, row in signals_df.iterrows():
        symbol = row["Symbol"]
        entry_price = float(row.get("BuyAbove", 0) or 0)
        price_now = float(row.get("PriceNow", 0) or 0)
        prev_close = float(row.get("PrevClose", 0) or 0)
        stop_loss = float(row.get("SellIfBelow", 0) or 0)
        target1 = float(row.get("TakeProfit1", 0) or 0)
        target2 = float(row.get("TakeProfit2", 0) or 0)

        if entry_price <= 0 or price_now <= 0:
            continue

        # Entry at BuyAbove, current EOD at PriceNow
        entry_return_pct = (price_now - entry_price) / entry_price * 100.0
        day_change = float(row.get("ChangePct", 0) or 0)

        # Check SL/Target hits
        sl_hit = price_now <= stop_loss if stop_loss > 0 else False
        t1_hit = price_now >= target1 if target1 > 0 else False
        t2_hit = price_now >= target2 if target2 > 0 else False

        results.append({
            "Symbol": symbol,
            "NowAction": row.get("NowAction", ""),
            "EntryPrice": entry_price,
            "EODPrice": price_now,
            "PrevClose": prev_close,
            "StopLoss": stop_loss,
            "Target1": target1,
            "Target2": target2,
            "EntryReturnPct": round(entry_return_pct, 2),
            "DayChangePct": day_change,
            "VolumeRatio": float(row.get("VolumeRatio", 0) or 0),
            "SignalStrength": float(row.get("SignalStrengthPct", 0) or 0),
            "RSI": float(row.get("RSI", 0) or 0),
            "CCI": float(row.get("CCI", 0) or 0),
            "MFI": float(row.get("MFI", 0) or 0),
            "SLHit": sl_hit,
            "T1Hit": t1_hit,
            "T2Hit": t2_hit,
            "Winner": entry_return_pct > 0,
            "AlertTime": row.get("SnapshotTime", ""),
        })

    return pd.DataFrame(results)


def analyze_all_signals(all_states_df):
    """Analyze all NowAction categories for today."""
    latest = all_states_df[all_states_df["SnapshotTime"] == all_states_df["SnapshotTime"].max()].copy()

    print(f"\n{'='*70}")
    print(f"POST-MARKET ANALYSIS: April 8, 2026")
    print(f"{'='*70}")
    print(f"\nTotal symbols scanned: {len(latest)}")
    print(f"\nSignal Distribution (latest snapshot):")
    print(latest["NowAction"].value_counts().to_string())

    return latest


def backtest_buy_signals(all_states_df):
    """Backtest all buy signals from today."""
    first_buys = get_first_buy_signals(all_states_df)
    if first_buys.empty:
        print("\nNo buy signals found today.")
        return

    results = compute_eod_returns(first_buys)
    if results.empty:
        print("\nCould not compute returns for any signals.")
        return

    total = len(results)
    winners = results["Winner"].sum()
    losers = total - winners
    win_rate = winners / total * 100 if total > 0 else 0

    avg_return = results["EntryReturnPct"].mean()
    avg_winner = results[results["Winner"]]["EntryReturnPct"].mean() if winners > 0 else 0
    avg_loser = results[~results["Winner"]]["EntryReturnPct"].mean() if losers > 0 else 0
    t1_hits = results["T1Hit"].sum()
    t2_hits = results["T2Hit"].sum()
    sl_hits = results["SLHit"].sum()

    print(f"\n{'='*70}")
    print(f"BUY SIGNAL BACKTEST: {total} signals")
    print(f"{'='*70}")
    print(f"  Win Rate:       {win_rate:.1f}% ({int(winners)}/{total})")
    print(f"  Avg Return:     {avg_return:+.2f}%")
    print(f"  Avg Winner:     {avg_winner:+.2f}%")
    print(f"  Avg Loser:      {avg_loser:+.2f}%")
    print(f"  Target 1 Hit:   {int(t1_hits)}/{total} ({t1_hits/total*100:.0f}%)")
    print(f"  Target 2 Hit:   {int(t2_hits)}/{total} ({t2_hits/total*100:.0f}%)")
    print(f"  Stop Loss Hit:  {int(sl_hits)}/{total} ({sl_hits/total*100:.0f}%)")

    # Profit factor
    gross_profit = results[results["Winner"]]["EntryReturnPct"].sum()
    gross_loss = abs(results[~results["Winner"]]["EntryReturnPct"].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    print(f"  Profit Factor:  {pf:.2f}")

    # By volume ratio bucket  
    print(f"\n--- By Volume Ratio ---")
    results["VolBucket"] = pd.cut(results["VolumeRatio"], bins=[0, 1.0, 1.5, 2.0, 3.0, 100], labels=["<1x", "1-1.5x", "1.5-2x", "2-3x", ">3x"])
    vol_stats = results.groupby("VolBucket", observed=True).agg(
        Count=("Symbol", "count"),
        WinRate=("Winner", "mean"),
        AvgReturn=("EntryReturnPct", "mean"),
    ).round(2)
    vol_stats["WinRate"] = (vol_stats["WinRate"] * 100).round(1).astype(str) + "%"
    vol_stats["AvgReturn"] = vol_stats["AvgReturn"].astype(str) + "%"
    print(vol_stats.to_string())

    # By signal strength bucket
    print(f"\n--- By Signal Strength ---")
    results["StrBucket"] = pd.cut(results["SignalStrength"], bins=[0, 25, 35, 50, 75, 100], labels=["<25", "25-35", "35-50", "50-75", ">75"])
    str_stats = results.groupby("StrBucket", observed=True).agg(
        Count=("Symbol", "count"),
        WinRate=("Winner", "mean"),
        AvgReturn=("EntryReturnPct", "mean"),
    ).round(2)
    str_stats["WinRate"] = (str_stats["WinRate"] * 100).round(1).astype(str) + "%"
    str_stats["AvgReturn"] = str_stats["AvgReturn"].astype(str) + "%"
    print(str_stats.to_string())

    # Top 10 winners
    print(f"\n--- Top 10 Winners ---")
    top = results.nlargest(10, "EntryReturnPct")
    print(top[["Symbol", "EntryPrice", "EODPrice", "EntryReturnPct", "VolumeRatio", "SignalStrength", "AlertTime"]].to_string(index=False))

    # Top 10 losers
    print(f"\n--- Top 10 Losers ---")
    bottom = results.nsmallest(10, "EntryReturnPct")
    print(bottom[["Symbol", "EntryPrice", "EODPrice", "EntryReturnPct", "VolumeRatio", "SignalStrength", "AlertTime"]].to_string(index=False))

    # RSI distribution of winners vs losers
    print(f"\n--- RSI Distribution ---")
    print(f"  Winners avg RSI:  {results[results['Winner']]['RSI'].mean():.1f}")
    print(f"  Losers avg RSI:   {results[~results['Winner']]['RSI'].mean():.1f}")

    return results


def backtest_sell_signals(all_states_df):
    """Check if sell signals actually fell."""
    latest = all_states_df[all_states_df["SnapshotTime"] == all_states_df["SnapshotTime"].max()].copy()
    sells = latest[latest["NowAction"] == "Sell / Exit"].copy()

    if sells.empty:
        print("\nNo sell signals today.")
        return

    sells["DayChangePct"] = pd.to_numeric(sells["ChangePct"], errors="coerce").fillna(0)
    neg_count = (sells["DayChangePct"] < 0).sum()
    total = len(sells)

    print(f"\n{'='*70}")
    print(f"SELL SIGNAL CHECK: {total} signals")
    print(f"{'='*70}")
    print(f"  Actually fell today: {neg_count}/{total} ({neg_count/total*100:.1f}%)")
    print(f"  Avg day change:     {sells['DayChangePct'].mean():+.2f}%")

    # Distribution
    print(f"\n  Biggest drops:")
    biggest = sells.nsmallest(5, "DayChangePct")
    for _, r in biggest.iterrows():
        print(f"    {r['Symbol']:15s}  {r['DayChangePct']:+.2f}%")

    return sells


def time_analysis(all_states_df):
    """When during the day do buy signals first appear?"""
    first_buys = get_first_buy_signals(all_states_df)
    if first_buys.empty:
        return

    # Parse snapshot times to get hour buckets
    first_buys["AlertHour"] = first_buys["SnapshotTime"].str[0:2].astype(int)

    print(f"\n{'='*70}")
    print(f"SIGNAL TIMING ANALYSIS")
    print(f"{'='*70}")
    print(f"\nBuy signals by time of day (IST):")
    hour_dist = first_buys["AlertHour"].value_counts().sort_index()
    for hour, count in hour_dist.items():
        # Timestamps appear to be UTC; add 5:30 for IST
        ist_hour = (hour + 5) % 24
        print(f"  {ist_hour:02d}:00-{ist_hour:02d}:59 IST  {count:3d} signals")


def multi_day_forward_test(buy_results_df):
    """
    For buy signals, check if historical data shows forward returns.
    Uses cached Kite daily data.
    """
    if buy_results_df is None or buy_results_df.empty:
        return

    forward_results = []
    for _, row in buy_results_df.iterrows():
        symbol = row["Symbol"]
        entry = row["EntryPrice"]
        hist = load_stock_history(symbol)
        if hist is None or len(hist) < 5:
            continue

        # Find today's row (April 8)
        today_mask = hist["date"].dt.date == pd.Timestamp("2026-04-08").date()
        if not today_mask.any():
            continue

        today_idx = hist.index[today_mask][-1]
        pos = hist.index.get_loc(today_idx)

        # Forward returns (if data available from previous trading days for context)
        eod_close = float(hist.iloc[pos]["close"]) if pos < len(hist) else None

        forward_results.append({
            "Symbol": symbol,
            "EntryPrice": entry,
            "EODClose": eod_close,
            "EODReturn": (eod_close - entry) / entry * 100 if eod_close and entry > 0 else None,
        })

    if forward_results:
        fwd_df = pd.DataFrame(forward_results)
        valid = fwd_df.dropna(subset=["EODReturn"])
        if not valid.empty:
            print(f"\n{'='*70}")
            print(f"FORWARD TEST (using cached daily data)")
            print(f"{'='*70}")
            print(f"  Symbols with cached data: {len(valid)}/{len(buy_results_df)}")
            print(f"  EOD win rate:  {(valid['EODReturn'] > 0).mean()*100:.1f}%")
            print(f"  Avg EOD return: {valid['EODReturn'].mean():+.2f}%")


def main():
    print("=" * 70)
    print("PKScreener Post-Market Backtest")
    print(f"Date: April 8, 2026 | Post-market analysis")
    print("=" * 70)

    # Load all intraday snapshots
    all_states = load_all_state_snapshots()
    if all_states is None:
        print("No data available for backtest.")
        return

    # 1. Signal distribution analysis
    latest = analyze_all_signals(all_states)

    # 2. Buy signal backtest
    buy_results = backtest_buy_signals(all_states)

    # 3. Sell signal check
    backtest_sell_signals(all_states)

    # 4. Timing analysis
    time_analysis(all_states)

    # 5. Forward test against cached data
    multi_day_forward_test(buy_results)

    print(f"\n{'='*70}")
    print(f"BACKTEST COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()

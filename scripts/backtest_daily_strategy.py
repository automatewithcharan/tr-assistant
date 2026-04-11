#!/usr/bin/env python3
"""
Offline Daily-OHLCV Backtest — "Consistent Daily Income Mode"
=============================================================
Tests whether the current AutoPilot strategy can generate Rs.2 lakh/month.
Fully vectorized using pandas/numpy — runs in ~30 seconds for 13 months.

Strategy rules simulated:
  - Score >= 65, Volume Ratio >= 2.5x, Distribution block
  - Entry = open price (stock gapped up >= 2% from prev_close)
  - SL = entry*0.95, T1 = entry*1.05 (50% exit), T2 = entry*1.10 (full exit)
  - Max 5 trades/day by volume ratio rank
  - Position: Rs.75,000 per trade (5x MIS leverage)
  - Costs: 0.2% round-trip

Usage:
    python scripts/backtest_daily_strategy.py
    python scripts/backtest_daily_strategy.py --start 2025-06-01
    python scripts/backtest_daily_strategy.py --min-score 55 --min-vol 2.0
"""

import argparse, glob, os, sys, warnings
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "results" / "Data" / "kite_history_cache" / "day"

MIN_SCORE = 65.0
MIN_VOL_RATIO = 2.5
ENTRY_MIN_PCT = 2.0
ENTRY_MAX_PCT = 15.0
T1_PCT = 0.05
T2_PCT = 0.10
SL_PCT = 0.05
PARTIAL_FRAC = 0.50
ROUND_TRIP_COST = 0.002
MAX_PER_DAY = 5
POSITION_SIZE = 75_000
MIN_PRICE = 10.0
MIN_VOL_DAILY = 100_000
VOL_LB = 20


def load_master(data_dir, start, end):
    files = glob.glob(str(data_dir / "*.csv"))
    print(f"Loading {len(files)} OHLCV files (vectorized)...")
    parts = []
    for f in files:
        sym = Path(f).stem
        try:
            df = pd.read_csv(f, parse_dates=["datetime"])
            df["date"] = df["datetime"].dt.date.astype(str)
            df = df.sort_values("date").reset_index(drop=True)
            if len(df) < VOL_LB + 10 or df["close"].iloc[-1] < MIN_PRICE:
                continue

            # Indicators
            delta = df["close"].diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            df["rsi"] = 100 - 100 / (1 + rs)

            tp = (df["high"] + df["low"] + df["close"]) / 3
            tp_mean = tp.rolling(20).mean()
            md = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
            df["cci"] = (tp - tp_mean) / (0.015 * md.replace(0, np.nan))

            mf = tp * df["volume"]
            dir_ = np.sign(tp.diff())
            pos = (mf * (dir_ > 0)).rolling(14).sum()
            neg = (mf * (dir_ <= 0)).rolling(14).sum()
            mfi_r = pos / neg.replace(0, np.nan)
            df["mfi"] = 100 - 100 / (1 + mfi_r)

            df["avg_vol"] = df["volume"].rolling(VOL_LB).mean().shift(1)
            df["vol_ratio"] = df["volume"] / df["avg_vol"].replace(0, np.nan)
            df["prev_close"] = df["close"].shift(1)
            df["gap_pct"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100
            df["symbol"] = sym

            cols = ["date","symbol","open","high","low","close","volume",
                    "prev_close","gap_pct","vol_ratio","rsi","cci","mfi"]
            parts.append(df[cols])
        except Exception:
            pass

    print(f"  Processed {len(parts)} symbols.")
    master = pd.concat(parts, ignore_index=True)
    master = master[(master["date"] >= start) & (master["date"] <= end)]
    return master


def score_df(d):
    sc = pd.Series(0.0, index=d.index)
    hv = d["vol_ratio"] >= 2.0
    cci_n = (d["cci"] + 200).clip(0, 800) / 4.0
    osc_med = pd.concat([d["rsi"], cci_n, d["mfi"]], axis=1).median(axis=1)

    sc += np.where(osc_med < 25, 25,
          np.where(osc_med < 35, 18,
          np.where(osc_med < 50, 10, 0)))
    sc += np.where(osc_med > 80,
                   np.where((d["gap_pct"] >= 5) | hv, -5, -12),
          np.where((osc_med > 65) & ~((d["gap_pct"] >= 5) | hv), -5, 0))
    sc += np.where(d["vol_ratio"] >= 2.5, 20,
          np.where(d["vol_ratio"] >= 1.5, 15,
          np.where(d["vol_ratio"] >= 1.0, 10,
          np.where(d["vol_ratio"] >= 0.7, 5, 0))))
    sc += np.where(d["gap_pct"] >= 7, 15,
          np.where(d["gap_pct"] >= 4, 10, 0))
    chg_cap = np.where(d["gap_pct"] >= 4, 10, 15)
    sc += np.where(d["gap_pct"] >= 5, chg_cap,
          np.where(d["gap_pct"] >= 2.5, np.minimum(12, chg_cap),
          np.where(d["gap_pct"] >= 1, 10,
          np.where(d["gap_pct"] > 0, 6,
          np.where(d["gap_pct"] > -1, 2, -5)))))
    return sc.clip(0, 100)


def run(start, end, min_score, min_vol, top_n, position_size, verbose):
    master = load_master(DATA_DIR, start, end)

    print(f"Applying filters (score>={min_score}, vol>={min_vol}x)...")
    d = master[
        (master["open"] >= MIN_PRICE) &
        (master["volume"] >= MIN_VOL_DAILY) &
        (master["gap_pct"] >= ENTRY_MIN_PCT) &
        (master["gap_pct"] < ENTRY_MAX_PCT) &
        (master["vol_ratio"] >= min_vol)
    ].dropna(subset=["rsi","cci","mfi","vol_ratio","gap_pct"]).copy()

    d["score"] = score_df(d)
    d = d[d["score"] >= min_score]

    # Distribution block
    distrib = (d["vol_ratio"] >= 3.0) & (d["gap_pct"] > 0) & \
              ((d["vol_ratio"] / d["gap_pct"]) > 1.5)
    d = d[~distrib]

    print(f"  Candidates passing filters: {len(d):,} across {d['date'].nunique()} trading days")

    # Top N per day
    d = d.sort_values(["date","vol_ratio"], ascending=[True, False])
    d = d.groupby("date").head(top_n).reset_index(drop=True)
    print(f"  After top-{top_n}/day selection: {len(d):,} trades to simulate")

    # Simulate
    entry = d["open"].values
    high  = d["high"].values
    low   = d["low"].values
    close = d["close"].values
    sl = entry * (1 - SL_PCT)
    t1 = entry * (1 + T1_PCT)
    t2 = entry * (1 + T2_PCT)
    qty = (position_size // entry).astype(int)
    inv = qty * entry
    cost = inv * ROUND_TRIP_COST

    sl_hit = low <= sl
    t2_hit = (high >= t2) & ~sl_hit
    t1_hit = (high >= t1) & ~sl_hit & ~t2_hit

    qty1 = (qty * PARTIAL_FRAC).astype(int)
    qty2 = qty - qty1

    pnl = np.where(sl_hit, (sl - entry)*qty - cost,
          np.where(t2_hit, (t2 - entry)*qty - cost,
          np.where(t1_hit, (t1 - entry)*qty1 + (close - entry)*qty2 - cost,
                            (close - entry)*qty - cost)))

    exit_reason = np.where(sl_hit, "SL_HIT",
                  np.where(t2_hit, "T2_HIT",
                  np.where(t1_hit, "T1_PARTIAL_EOD", "EOD")))

    d = d.copy()
    d["qty"] = qty
    d["invested"] = inv.round(0)
    d["exit_reason"] = exit_reason
    d["net_pnl_inr"] = pnl.round(2)
    d["won"] = pnl > 0
    trades = d[d["qty"] > 0].copy()

    if verbose:
        for _, t in trades.iterrows():
            sign = "OK " if t["won"] else "BAD"
            print(f"  {sign} {t['date']} {t['symbol']:12} "
                  f"pnl=Rs{t['net_pnl_inr']:+.0f} reason={t['exit_reason']} "
                  f"vol={t['vol_ratio']:.1f}x sc={t['score']:.0f}")

    return trades


def print_report(trades, position_size):
    if trades.empty:
        print("\nNo trades. Try --min-score 55 --min-vol 2.0")
        return

    tr = trades
    total = len(tr)
    wr = tr["won"].mean() * 100
    avg = tr["net_pnl_inr"].mean()
    tot = tr["net_pnl_inr"].sum()
    best = tr["net_pnl_inr"].max()
    worst = tr["net_pnl_inr"].min()

    print("\n" + "=" * 70)
    print("  BACKTEST RESULTS  -- Consistent Daily Income Mode")
    print("=" * 70)
    print(f"  Trades executed      : {total:,}")
    print(f"  Win Rate             : {wr:.1f}%")
    print(f"  Avg P&L / trade      : Rs{avg:+,.0f}")
    print(f"  Total Net P&L        : Rs{tot:+,.0f}")
    print(f"  Best trade           : Rs{best:+,.0f}")
    print(f"  Worst trade          : Rs{worst:+,.0f}")
    print(f"  Avg volume ratio     : {tr['vol_ratio'].mean():.2f}x")
    print(f"  Avg score            : {tr['score'].mean():.1f}")
    print()

    print("  Exit Reasons:")
    for reason, g in tr.groupby("exit_reason"):
        w = g["won"].mean()*100
        a = g["net_pnl_inr"].mean()
        s = g["net_pnl_inr"].sum()
        print(f"    {reason:22} {len(g):4} trades  WR={w:3.0f}%  avg=Rs{a:+8,.0f}  total=Rs{s:+12,.0f}")

    print()
    print(f"  {'Month':<10} {'Trades':>7} {'WinRate':>8} {'AvgRs':>10} {'MonthlyRs':>14} {'2L?':>6}")
    print("  " + "-" * 63)

    tr["month"] = tr["date"].str[:7]
    target = 200_000
    mhit = 0
    mtot = 0
    for month, g in tr.groupby("month"):
        wr_m = g["won"].mean()*100
        avg_m = g["net_pnl_inr"].mean()
        tot_m = g["net_pnl_inr"].sum()
        chk = "YES" if tot_m >= target else "NO "
        print(f"  {month:<10} {len(g):>7}  {wr_m:>7.0f}%  Rs{avg_m:>7,.0f}  Rs{tot_m:>12,.0f}  {chk}")
        mtot += 1
        if tot_m >= target: mhit += 1

    print("  " + "-" * 63)
    avg_mo = tot / mtot if mtot > 0 else 0
    print(f"\n  Avg monthly P&L      : Rs{avg_mo:+,.0f}")
    if mtot > 0:
        print(f"  Months hitting Rs2L  : {mhit}/{mtot} ({mhit/mtot*100:.0f}%)")

    print()
    if avg_mo >= target:
        print("  VERDICT: Strategy EXCEEDS Rs2L/month target!")
    elif avg_mo > 0:
        mult = target / avg_mo
        req_pos = position_size * mult
        req_cap = req_pos * MAX_PER_DAY / 5
        print(f"  VERDICT: Strategy achieves Rs{avg_mo:,.0f}/month average.")
        print(f"  To consistently hit Rs2L/month:")
        print(f"    Position size needed : Rs{req_pos:,.0f} per trade  (vs current Rs{position_size:,.0f})")
        print(f"    Base capital needed  : Rs{req_cap:,.0f}  (with 5x MIS leverage)")
    else:
        print("  VERDICT: Strategy is unprofitable on average — review filters.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start",     default="2025-03-01")
    p.add_argument("--end",       default="2026-04-10")
    p.add_argument("--top",       type=int,   default=MAX_PER_DAY)
    p.add_argument("--min-score", type=float, default=MIN_SCORE)
    p.add_argument("--min-vol",   type=float, default=MIN_VOL_RATIO)
    p.add_argument("--position",  type=float, default=POSITION_SIZE)
    p.add_argument("--verbose",   action="store_true")
    p.add_argument("--export",    default="results/backtest_daily_strategy.csv")
    args = p.parse_args()

    trades = run(args.start, args.end, args.min_score, args.min_vol,
                 args.top, args.position, args.verbose)
    print_report(trades, args.position)

    if not trades.empty and args.export:
        trades.to_csv(args.export, index=False)
        print(f"\n  Trades exported to: {args.export}")

if __name__ == "__main__":
    main()

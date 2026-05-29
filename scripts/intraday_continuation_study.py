#!/usr/bin/env python3
"""Does 'already up X% mid-day' predict it keeps rising? The core of "can we catch them".

For every 5-min bar (mid-day, 09:45-14:30) across 179 liquid names / 2 years, we
bucket by how much the stock is already up from the open, then measure what
happens NEXT:
  - average return over the next 30 min,
  - probability it's higher after 30 min,
  - probability it reaches +3% more (your goal) within 60 min BEFORE dropping 1.5%.

If 'up a lot' predicted 'up more', the high buckets would show big positive
forward returns. If it's a coin flip (or worse), 'easily catch' is a mirage.
This is pure measurement of what happens after the condition — no strategy, no
tuning, no look-ahead concern.
"""
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "results" / "Data" / "kite_history_backfill" / "5minute" / "2024-05-13_2026-05-12"

FWD_30 = 6     # 6 x 5-min bars = 30 min
FWD_60 = 12    # 60 min
TGT = 0.03     # +3% more
STP = 0.015    # -1.5%
BUCKETS = [(1, 3), (3, 5), (5, 10), (10, 20), (20, 100)]


def per_symbol(df):
    dt = pd.to_datetime(df["datetime"])
    df = df.assign(date=dt.dt.date.astype(str), t=dt.dt.strftime("%H:%M"))
    recs = []
    for _, g in df.groupby("date"):
        g = g.reset_index(drop=True)
        n = len(g)
        if n < FWD_60 + 2:
            continue
        op = float(g.iloc[0]["open"])
        if op <= 0:
            continue
        close = g["close"].to_numpy(float)
        high = g["high"].to_numpy(float)
        low = g["low"].to_numpy(float)
        gain = (close / op - 1) * 100
        nan = np.full(n, np.nan)

        def shift_up(a, k):
            return np.concatenate([a[k:], np.full(k, np.nan)])

        fwd30 = (shift_up(close, FWD_30) / close - 1) * 100
        fut_high = np.nanmax(np.vstack([shift_up(high, k) for k in range(1, FWD_60 + 1)]), axis=0)
        fut_low = np.nanmin(np.vstack([shift_up(low, k) for k in range(1, FWD_60 + 1)]), axis=0)
        reach3 = (fut_high >= close * (1 + TGT)).astype(float)
        drop15 = (fut_low <= close * (1 - STP)).astype(float)
        t = g["t"].to_numpy()
        midday = (t >= "09:45") & (t <= "14:30")
        for i in range(n):
            if midday[i] and gain[i] >= 1 and not np.isnan(fwd30[i]):
                recs.append((gain[i], fwd30[i], reach3[i], drop15[i]))
    return recs


def main():
    files = sorted(glob.glob(str(DATA_DIR / "*.csv")))
    print(f"Universe: {len(files)} liquid symbols, 5-min bars, 2 years")
    print("Question: when a stock is already up X% mid-day, what happens next?\n")
    recs = []
    for k, f in enumerate(files):
        try:
            recs += per_symbol(pd.read_csv(f))
        except Exception:
            continue
        if (k + 1) % 40 == 0:
            print(f"  ...{k+1}/{len(files)} symbols")
    arr = np.array(recs)
    gain, fwd30, reach3, drop15 = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    print(f"\nMeasured {len(arr):,} mid-day moments where a stock was already up >=1%.\n")
    print(f"{'Already up':>12} | {'cases':>9} | {'avg next-30min':>14} | {'% higher in 30m':>15} "
          f"| {'reaches +3% more':>16} | {'drops -1.5% first(ish)':>22}")
    print("-" * 100)
    for lo, hi in BUCKETS:
        m = (gain >= lo) & (gain < hi)
        if m.sum() == 0:
            continue
        print(f"{f'+{lo}-{hi}%':>12} | {m.sum():>9,} | {fwd30[m].mean():>13.3f}% "
              f"| {(fwd30[m] > 0).mean()*100:>14.1f}% | {reach3[m].mean()*100:>15.1f}% "
              f"| {drop15[m].mean()*100:>21.1f}%")
    print("\nIf 'up more' were catchable, the high buckets would show big positive "
          "next-30min returns and >>50% higher. Read the table.")


if __name__ == "__main__":
    main()

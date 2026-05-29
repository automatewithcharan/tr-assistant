#!/usr/bin/env python3
"""Honest intraday backtest — momentum (chase) vs fade (mean-reversion), A/B.

Tests one fixed trigger on 179 LIQUID names (5-min bars, 2 years) and scores
BOTH directions from the SAME signals in a single pass, so the only difference
is long vs short. The point is not to find a winner by tuning — it's to measure,
without the usual backtest lies, whether either side actually pays.

  - MOMENTUM (long): buy the +2% volume spike, ride for +3%.
  - FADE (short):    short the +2% volume spike, expect reversion for +3% down.

Anti-bias measures:
  1. NO LOOK-AHEAD — signal on a bar's close, fill the NEXT bar's open.
  2. STOP-BEFORE-TARGET — if a bar spans both, count the stop (worst case).
  3. GAP-AWARE FILLS — stop that gaps through fills at the bar open.
  4. REAL COSTS + SLIPPAGE — subtracted per trade, reported at 3 levels.
  5. ONE trade per stock per day.
  6. IN-SAMPLE vs OUT-OF-SAMPLE halves reported.
  7. Parameters fixed a-priori (NOT tuned to this data).
"""
import glob
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "results" / "Data" / "kite_history_backfill" / "5minute" / "2024-05-13_2026-05-12"

UP_TRIGGER = 0.02
VOL_SURGE = 2.0
TARGET = 0.03
STOP = 0.015
ENTRY_START = "09:30"
ENTRY_END = "11:00"
EOD = "15:15"
COST_LEVELS = [0.0015, 0.0025, 0.0035]
SPLIT_DATE = "2025-05-13"


def backtest_symbol(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    dt = pd.to_datetime(df["datetime"])
    df["date"] = dt.dt.date.astype(str)
    df["t"] = dt.dt.strftime("%H:%M")
    out = []
    for day, g in df.groupby("date"):
        g = g.reset_index(drop=True)
        if len(g) < 5:
            continue
        open_px = float(g.iloc[0]["open"])
        if open_px <= 0:
            continue
        entry_idx = None
        for i in range(len(g)):
            row = g.iloc[i]
            if row["t"] < ENTRY_START:
                continue
            if row["t"] > ENTRY_END:
                break
            avg_vol = g.iloc[: i + 1]["volume"].mean()
            if (float(row["close"]) >= open_px * (1 + UP_TRIGGER)
                    and avg_vol > 0 and float(row["volume"]) >= VOL_SURGE * avg_vol):
                entry_idx = i + 1
                break
        if entry_idx is None or entry_idx >= len(g):
            continue
        entry_px = float(g.iloc[entry_idx]["open"])
        if entry_px <= 0:
            continue
        long_tgt, long_stp = entry_px * (1 + TARGET), entry_px * (1 - STOP)
        short_tgt, short_stp = entry_px * (1 - TARGET), entry_px * (1 + STOP)
        l_exit = l_reason = s_exit = s_reason = None
        for j in range(entry_idx, len(g)):
            b = g.iloc[j]
            if b["t"] >= EOD:
                op = float(b["open"])
                if l_exit is None:
                    l_exit, l_reason = op, "eod"
                if s_exit is None:
                    s_exit, s_reason = op, "eod"
                break
            lo, hi, op = float(b["low"]), float(b["high"]), float(b["open"])
            if l_exit is None:  # long: downside stop first
                if lo <= long_stp:
                    l_exit, l_reason = min(long_stp, op), "stop"
                elif hi >= long_tgt:
                    l_exit, l_reason = long_tgt, "target"
            if s_exit is None:  # short: upside stop first
                if hi >= short_stp:
                    s_exit, s_reason = max(short_stp, op), "stop"
                elif lo <= short_tgt:
                    s_exit, s_reason = short_tgt, "target"
            if l_exit is not None and s_exit is not None:
                break
        last_close = float(g.iloc[-1]["close"])
        if l_exit is None:
            l_exit, l_reason = last_close, "eod"
        if s_exit is None:
            s_exit, s_reason = last_close, "eod"
        out.append({
            "date": day, "symbol": g.iloc[0]["symbol"],
            "long_gross": (l_exit / entry_px - 1) * 100, "long_reason": l_reason,
            "short_gross": (entry_px / s_exit - 1) * 100, "short_reason": s_reason,
        })
    return out


def report_side(df, gross_col, reason_col, label):
    print(f"\n========== {label} ==========")
    tgt = (df[reason_col] == "target").mean() * 100
    stp = (df[reason_col] == "stop").mean() * 100
    eod = (df[reason_col] == "eod").mean() * 100
    print(f"Exit mix: target {tgt:.1f}% | stop {stp:.1f}% | EOD {eod:.1f}%")
    for tag, sub in [("ALL", df),
                     ("IN-SAMPLE  yr1", df[df["date"] < SPLIT_DATE]),
                     ("OUT-SAMPLE yr2", df[df["date"] >= SPLIT_DATE])]:
        print(f"  {tag} (n={len(sub)}):")
        for cost in COST_LEVELS:
            net = sub[gross_col] - cost * 100
            wr = (net > 0).mean() * 100
            daily = net.groupby(sub["date"]).mean()
            pos = (daily > 0).mean() * 100
            print(f"     cost {cost*100:.2f}%: avg/trade {net.mean():+.3f}% | win {wr:.1f}% "
                  f"| avg/day {daily.mean():+.3f}% | +days {pos:.0f}% | total {net.sum():+.0f}%")


def main():
    files = sorted(glob.glob(str(DATA_DIR / "*.csv")))
    print(f"Universe: {len(files)} liquid symbols | 5-min bars | trigger: up >= "
          f"{UP_TRIGGER:.0%} on {VOL_SURGE}x vol ({ENTRY_START}-{ENTRY_END}), fill next bar; "
          f"target +{TARGET:.0%}, stop -{STOP:.1%}, else EOD")
    rows = []
    for n, f in enumerate(files):
        try:
            rows += backtest_symbol(pd.read_csv(f))
        except Exception:
            continue
        if (n + 1) % 40 == 0:
            print(f"  ...processed {n+1}/{len(files)} symbols")
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    if df.empty:
        print("No trades."); return
    nd = df["date"].nunique()
    print(f"\nSame {len(df)} signals scored both ways | {nd} days | ~{len(df)/nd:.1f} signals/day")
    report_side(df, "long_gross", "long_reason", "MOMENTUM — buy the spike (chase)")
    report_side(df, "short_gross", "short_reason", "FADE — short the spike (mean-reversion)")


if __name__ == "__main__":
    main()

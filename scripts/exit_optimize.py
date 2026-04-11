#!/usr/bin/env python3
"""
Exit Optimization Backtest — Trailing Stop + Adaptive Exits
============================================================
Replays all trades with DIFFERENT exit logic and compares P&L.

For EACH trade, we have the minute candles cached. We simulate:
  A) CURRENT: Fixed T1 (partial) + fixed T2 (full) + SL + EOD
  B) TRAIL_BASIC: After T1, trail at entry (breakeven trail)
  C) TRAIL_ATR: After T1, trail at entry + dynamic based on range
  D) TRAIL_PCT: After T1, trail at X% below running high
  E) RATCHET: Move SL up in steps (entry→T1→trail) 
  F) NO_T2_TRAIL: Remove T2 cap, trail from running high after T1

Strategy-specific:
  GAP: The MFE is huge (avg 7%), fixed T2=3% is leaving 4% on table. 
       Best approach: remove T2 cap, trail from high after T1.
  ORB: Moderate MFE (avg 3.4%), EOD exits dominate.
       Best approach: tighter trail after T1, protect profits earlier.
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


def load_candles(sym, dt, strategy):
    caches = [ORB_CACHE, GAP_CACHE] if strategy == "ORB" else [GAP_CACHE, ORB_CACHE]
    for cdir in caches:
        fpath = cdir / f"{sym}_{dt}_min.json"
        if fpath.exists():
            with open(fpath) as f:
                return json.load(f)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  GAP STRATEGY EXIT VARIANTS
# ═══════════════════════════════════════════════════════════════════════════════

def gap_current(candles, entry, qty, cost, confirm_candles=5, confirm_dip=0.005):
    """Current fixed exits: T1=1.5%, T2=3%, SL=1%."""
    sl = entry * 0.99
    t1 = entry * 1.015
    t2 = entry * 1.03

    # 5-min confirmation
    first_n_low = min(c["low"] for c in candles[:confirm_candles])
    if first_n_low < entry * (1 - confirm_dip):
        return None

    high_so_far = entry
    t1_hit = False
    qty_rem = qty
    partial_pnl = 0.0

    for c in candles[1:]:
        high_so_far = max(high_so_far, c["high"])

        if c["high"] >= t2:
            return partial_pnl + (t2 - entry) * qty_rem - cost, "T2_HIT"

        if c["high"] >= t1 and not t1_hit:
            t1_hit = True
            qty_t1 = int(qty * 0.50)
            partial_pnl += (t1 - entry) * qty_t1
            qty_rem = qty - qty_t1

        if c["low"] <= sl:
            return partial_pnl + (sl - entry) * qty_rem - cost, "SL_HIT"

        drop = (high_so_far - c["close"]) / high_so_far * 100 if high_so_far > 0 else 0
        if drop >= 3.0 and c["close"] < entry:
            return partial_pnl + (c["close"] - entry) * qty_rem - cost, "FADE_EXIT"

    last = candles[-1]["close"]
    reason = "T1_PARTIAL_EOD" if t1_hit else "EOD"
    return partial_pnl + (last - entry) * qty_rem - cost, reason


def gap_trail_after_t1(candles, entry, qty, cost, trail_pct,
                       confirm_candles=5, confirm_dip=0.005):
    """
    After T1 hit: remove T2 cap, trail at trail_pct% below running high.
    Before T1: standard SL.
    """
    sl = entry * 0.99
    t1 = entry * 1.015

    first_n_low = min(c["low"] for c in candles[:confirm_candles])
    if first_n_low < entry * (1 - confirm_dip):
        return None

    high_so_far = entry
    t1_hit = False
    qty_rem = qty
    partial_pnl = 0.0
    trail_sl = sl  # starts at fixed SL

    for c in candles[1:]:
        high_so_far = max(high_so_far, c["high"])

        # T1 hit: partial exit, start trailing
        if c["high"] >= t1 and not t1_hit:
            t1_hit = True
            qty_t1 = int(qty * 0.50)
            partial_pnl += (t1 - entry) * qty_t1
            qty_rem = qty - qty_t1
            # Move SL to breakeven or trail, whichever is higher
            trail_sl = max(entry, high_so_far * (1 - trail_pct / 100))

        # Update trail after T1
        if t1_hit:
            new_trail = high_so_far * (1 - trail_pct / 100)
            trail_sl = max(trail_sl, new_trail)

        # Check SL / trail
        check_sl = trail_sl if t1_hit else sl
        if c["low"] <= check_sl:
            exit_px = check_sl
            reason = "TRAIL_HIT" if t1_hit else "SL_HIT"
            return partial_pnl + (exit_px - entry) * qty_rem - cost, reason

    last = candles[-1]["close"]
    reason = "TRAIL_EOD" if t1_hit else "EOD"
    return partial_pnl + (last - entry) * qty_rem - cost, reason


def gap_ratchet(candles, entry, qty, cost,
                confirm_candles=5, confirm_dip=0.005):
    """
    Ratchet SL up in stages:
      Stage 0: SL = entry * 0.99 (1%)
      Stage 1 (after +1.5%): SL → entry (breakeven)
      Stage 2 (after +3%): SL → entry * 1.015 (lock T1)
      Stage 3 (after +5%): SL → entry * 1.03 (lock T2) 
      No cap — let it run with trail of 2% from running high.
    """
    sl = entry * 0.99
    t1 = entry * 1.015

    first_n_low = min(c["low"] for c in candles[:confirm_candles])
    if first_n_low < entry * (1 - confirm_dip):
        return None

    high_so_far = entry
    t1_hit = False
    stage = 0
    qty_rem = qty
    partial_pnl = 0.0
    trail_pct = 2.0  # 2% trail after stage 2

    for c in candles[1:]:
        high_so_far = max(high_so_far, c["high"])
        move_pct = (high_so_far - entry) / entry * 100

        # Ratchet stages
        if move_pct >= 5.0 and stage < 3:
            stage = 3
            sl = max(sl, entry * 1.03)
        elif move_pct >= 3.0 and stage < 2:
            stage = 2
            sl = max(sl, entry * 1.015)
        elif move_pct >= 1.5 and stage < 1:
            stage = 1
            sl = max(sl, entry)

        # T1 partial
        if c["high"] >= t1 and not t1_hit:
            t1_hit = True
            qty_t1 = int(qty * 0.50)
            partial_pnl += (t1 - entry) * qty_t1
            qty_rem = qty - qty_t1

        # Trailing after stage 2
        if stage >= 2:
            trail = high_so_far * (1 - trail_pct / 100)
            sl = max(sl, trail)

        if c["low"] <= sl:
            reason = "RATCHET_HIT" if stage > 0 else "SL_HIT"
            return partial_pnl + (sl - entry) * qty_rem - cost, reason

    last = candles[-1]["close"]
    return partial_pnl + (last - entry) * qty_rem - cost, "EOD"


# ═══════════════════════════════════════════════════════════════════════════════
#  ORB STRATEGY EXIT VARIANTS
# ═══════════════════════════════════════════════════════════════════════════════

def orb_current(candles, entry, entry_idx, orb_low, range_height, qty, cost):
    """Current fixed exits: T1=1×range, T2=2×range, SL=ORB low (capped 1.5%)."""
    sl = max(orb_low, entry * (1 - 0.015))
    t1 = entry + range_height * 1.0
    t2 = entry + range_height * 2.0

    t1_hit = False
    qty_rem = qty
    partial_pnl = 0.0

    for i in range(entry_idx + 1, len(candles)):
        c = candles[i]
        if c["time"] >= "15:15":
            last = c["close"]
            reason = "T1_PARTIAL_EOD" if t1_hit else "EOD"
            return partial_pnl + (last - entry) * qty_rem - cost, reason

        if c["low"] <= sl:
            return partial_pnl + (sl - entry) * qty_rem - cost, "SL_HIT"
        if c["high"] >= t2:
            return partial_pnl + (t2 - entry) * qty_rem - cost, "T2_HIT"
        if c["high"] >= t1 and not t1_hit:
            t1_hit = True
            qty_t1 = int(qty * 0.50)
            partial_pnl += (t1 - entry) * qty_t1
            qty_rem = qty - qty_t1

    last = candles[-1]["close"]
    return partial_pnl + (last - entry) * qty_rem - cost, "EOD"


def orb_trail_after_t1(candles, entry, entry_idx, orb_low, range_height, qty, cost, trail_pct):
    """After T1: trail at trail_pct% below running high. No T2 cap."""
    sl = max(orb_low, entry * (1 - 0.015))
    t1 = entry + range_height * 1.0

    high_so_far = entry
    t1_hit = False
    qty_rem = qty
    partial_pnl = 0.0
    trail_sl = sl

    for i in range(entry_idx + 1, len(candles)):
        c = candles[i]
        high_so_far = max(high_so_far, c["high"])

        if c["time"] >= "15:15":
            last = c["close"]
            reason = "TRAIL_EOD" if t1_hit else "EOD"
            return partial_pnl + (last - entry) * qty_rem - cost, reason

        if c["high"] >= t1 and not t1_hit:
            t1_hit = True
            qty_t1 = int(qty * 0.50)
            partial_pnl += (t1 - entry) * qty_t1
            qty_rem = qty - qty_t1
            trail_sl = max(entry, high_so_far * (1 - trail_pct / 100))

        if t1_hit:
            new_trail = high_so_far * (1 - trail_pct / 100)
            trail_sl = max(trail_sl, new_trail)

        check_sl = trail_sl if t1_hit else sl
        if c["low"] <= check_sl:
            reason = "TRAIL_HIT" if t1_hit else "SL_HIT"
            return partial_pnl + (check_sl - entry) * qty_rem - cost, reason

    last = candles[-1]["close"]
    return partial_pnl + (last - entry) * qty_rem - cost, "EOD"


def orb_ratchet(candles, entry, entry_idx, orb_low, range_height, qty, cost):
    """
    ORB ratchet:
      Stage 0: SL = ORB low (structural)
      Stage 1 (after T1 = 1×range): SL → entry (breakeven), partial 50%
      Stage 2 (after 1.5×range): SL → entry + 0.5×range, trail 1% from high
    """
    sl = max(orb_low, entry * (1 - 0.015))
    t1 = entry + range_height * 1.0

    high_so_far = entry
    t1_hit = False
    stage = 0
    qty_rem = qty
    partial_pnl = 0.0

    for i in range(entry_idx + 1, len(candles)):
        c = candles[i]
        high_so_far = max(high_so_far, c["high"])

        if c["time"] >= "15:15":
            last = c["close"]
            return partial_pnl + (last - entry) * qty_rem - cost, "EOD"

        move = high_so_far - entry

        # Ratchet
        if move >= range_height * 1.5 and stage < 2:
            stage = 2
            sl = max(sl, entry + range_height * 0.5)
        elif move >= range_height and stage < 1:
            stage = 1
            sl = max(sl, entry)

        # T1 partial
        if c["high"] >= t1 and not t1_hit:
            t1_hit = True
            qty_t1 = int(qty * 0.50)
            partial_pnl += (t1 - entry) * qty_t1
            qty_rem = qty - qty_t1

        # Trail after stage 2
        if stage >= 2:
            trail = high_so_far * (1 - 1.0 / 100)  # 1% trail
            sl = max(sl, trail)

        if c["low"] <= sl:
            reason = "RATCHET_HIT" if stage > 0 else "SL_HIT"
            return partial_pnl + (sl - entry) * qty_rem - cost, reason

    last = candles[-1]["close"]
    return partial_pnl + (last - entry) * qty_rem - cost, "EOD"


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN COMPARISON
# ═══════════════════════════════════════════════════════════════════════════════

def run_gap_comparison():
    """Compare all gap exit variants."""
    df = pd.read_csv(BASE_DIR / "results" / "backtest_2year_optimized.csv")
    print(f"\n{'='*80}")
    print(f"  GAP STRATEGY — EXIT VARIANT COMPARISON ({len(df)} trades)")
    print(f"{'='*80}")

    variants = {
        "CURRENT (T1=1.5%, T2=3%)": [],
        "TRAIL 1.5% after T1": [],
        "TRAIL 2.0% after T1": [],
        "TRAIL 2.5% after T1": [],
        "TRAIL 3.0% after T1": [],
        "RATCHET (staged SL + 2% trail)": [],
    }

    for _, row in df.iterrows():
        candles = load_candles(row["symbol"], row["date"], "GAP")
        if not candles or len(candles) < 10:
            continue

        entry = candles[0]["open"]
        if entry <= 0:
            continue

        qty = int(75000 // entry)
        if qty <= 0:
            continue
        cost = qty * entry * 0.002

        r = gap_current(candles, entry, qty, cost)
        if r is None:
            continue
        variants["CURRENT (T1=1.5%, T2=3%)"].append(r)

        for trail_pct, label in [(1.5, "TRAIL 1.5% after T1"),
                                  (2.0, "TRAIL 2.0% after T1"),
                                  (2.5, "TRAIL 2.5% after T1"),
                                  (3.0, "TRAIL 3.0% after T1")]:
            r = gap_trail_after_t1(candles, entry, qty, cost, trail_pct)
            if r is not None:
                variants[label].append(r)

        r = gap_ratchet(candles, entry, qty, cost)
        if r is not None:
            variants["RATCHET (staged SL + 2% trail)"].append(r)

    print(f"\n  {'Exit variant':<35s} {'#':>4s} {'WR':>6s} {'Avg₹':>8s} {'Tot₹':>10s} {'Mo₹':>8s}  Exit breakdown")
    print(f"  {'-'*100}")

    for label, results in variants.items():
        if not results:
            continue
        pnls = [r[0] for r in results]
        reasons = [r[1] for r in results]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n * 100
        avg = np.mean(pnls)
        tot = np.sum(pnls)
        months = 23.0
        monthly = tot / months

        reason_counts = defaultdict(int)
        for r in reasons:
            reason_counts[r] += 1
        exits_str = " ".join(f"{k}:{v}" for k, v in sorted(reason_counts.items(), key=lambda x: -x[1]))

        print(f"  {label:<35s} {n:>4d} {wr:>5.1f}% ₹{avg:>+7,.0f} ₹{tot:>+9,.0f} ₹{monthly:>+7,.0f}  {exits_str}")


def run_orb_comparison():
    """Compare all ORB exit variants."""
    df = pd.read_csv(BASE_DIR / "results" / "backtest_orb_results.csv")
    print(f"\n{'='*80}")
    print(f"  ORB STRATEGY — EXIT VARIANT COMPARISON ({len(df)} trades)")
    print(f"{'='*80}")

    variants = {
        "CURRENT (T1=1×R, T2=2×R)": [],
        "TRAIL 0.7% after T1": [],
        "TRAIL 1.0% after T1": [],
        "TRAIL 1.2% after T1": [],
        "TRAIL 1.5% after T1": [],
        "TRAIL 2.0% after T1": [],
        "RATCHET (staged + 1% trail)": [],
    }

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

        # Find entry index
        entry_idx = None
        for i, c in enumerate(candles):
            if c["time"] >= entry_time:
                entry_idx = i
                break
        if entry_idx is None:
            continue

        qty = int(75000 // entry)
        if qty <= 0:
            continue
        cost = qty * entry * 0.002

        r = orb_current(candles, entry, entry_idx, orb_low, range_height, qty, cost)
        if r is not None:
            variants["CURRENT (T1=1×R, T2=2×R)"].append(r)

        for trail_pct, label in [(0.7, "TRAIL 0.7% after T1"),
                                  (1.0, "TRAIL 1.0% after T1"),
                                  (1.2, "TRAIL 1.2% after T1"),
                                  (1.5, "TRAIL 1.5% after T1"),
                                  (2.0, "TRAIL 2.0% after T1")]:
            r = orb_trail_after_t1(candles, entry, entry_idx, orb_low, range_height, qty, cost, trail_pct)
            if r is not None:
                variants[label].append(r)

        r = orb_ratchet(candles, entry, entry_idx, orb_low, range_height, qty, cost)
        if r is not None:
            variants["RATCHET (staged + 1% trail)"].append(r)

    print(f"\n  {'Exit variant':<35s} {'#':>4s} {'WR':>6s} {'Avg₹':>8s} {'Tot₹':>10s} {'Mo₹':>8s}  Exit breakdown")
    print(f"  {'-'*100}")

    for label, results in variants.items():
        if not results:
            continue
        pnls = [r[0] for r in results]
        reasons = [r[1] for r in results]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n * 100
        avg = np.mean(pnls)
        tot = np.sum(pnls)
        months = 23.0
        monthly = tot / months

        reason_counts = defaultdict(int)
        for r in reasons:
            reason_counts[r] += 1
        exits_str = " ".join(f"{k}:{v}" for k, v in sorted(reason_counts.items(), key=lambda x: -x[1]))

        print(f"  {label:<35s} {n:>4d} {wr:>5.1f}% ₹{avg:>+7,.0f} ₹{tot:>+9,.0f} ₹{monthly:>+7,.0f}  {exits_str}")


if __name__ == "__main__":
    run_gap_comparison()
    run_orb_comparison()

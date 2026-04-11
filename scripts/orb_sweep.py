#!/usr/bin/env python3
"""
Parameter sweep for ORB strategy.
Tests different param combos with FULL P&L simulation using cached minute data.
"""

import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

# Import and override globals before importing simulate_orb
import scripts.backtest_orb_kite as orb

def run_sweep(selected, combos):
    """Run full simulation for each param combo."""
    import json

    CACHE_DIR = orb.CACHE_DIR
    GAP_CACHE_DIR = orb.GAP_CACHE_DIR

    def load_candles(sym, dt):
        for cdir in [CACHE_DIR, GAP_CACHE_DIR]:
            fpath = cdir / f"{sym}_{dt}_min.json"
            if fpath.exists():
                with open(fpath) as f:
                    return json.load(f)
        return None

    results = []
    for combo in combos:
        label = combo["label"]

        # Override globals
        orb.MAX_RANGE_PCT  = combo.get("max_range", 1.5)
        orb.MIN_RANGE_PCT  = combo.get("min_range", 0.2)
        orb.ORB_VOL_FRAC   = combo.get("vol_frac", 0.30)
        orb.ENTRY_DEADLINE = combo.get("deadline", "11:30")
        orb.SL_MAX_PCT     = combo.get("sl_max", 0.015)
        orb.T1_MULT        = combo.get("t1_mult", 1.0)
        orb.T2_MULT        = combo.get("t2_mult", 2.0)

        trades = []
        for q in selected:
            candles = load_candles(q["symbol"], q["date"])
            if not candles:
                continue
            result = orb.simulate_orb(q["symbol"], q["date"], candles, q, 75000.0)
            if result:
                trades.append(result)

        if not trades:
            results.append({"label": label, "n": 0})
            continue

        import pandas as pd
        df = pd.DataFrame(trades)
        total = len(df)
        wins  = df["won"].sum()
        wr    = wins / total * 100
        avg   = df["net_pnl"].mean()
        tot   = df["net_pnl"].sum()
        span  = (pd.to_datetime(df["date"]).max() - pd.to_datetime(df["date"]).min()).days
        months = max(1, span / 30.44)
        monthly = tot / months
        tpm = total / months

        # Consecutive losses
        streaks = []
        curr = 0
        for w in df["won"].tolist():
            if not w:
                curr += 1
            else:
                if curr > 0:
                    streaks.append(curr)
                curr = 0
        if curr > 0:
            streaks.append(curr)
        max_loss = max(streaks) if streaks else 0

        # Exit reason counts
        exit_counts = df["exit_reason"].value_counts().to_dict()

        results.append({
            "label": label,
            "n": total,
            "wr": wr,
            "avg_pnl": avg,
            "tot_pnl": tot,
            "monthly": monthly,
            "tpm": tpm,
            "max_loss_streak": max_loss,
            "exits": exit_counts,
            "avg_range": df["range_pct"].mean(),
            "avg_risk": df["risk_pct"].mean(),
        })

    return results


def main():
    # Load data (same as main script)
    symbols = orb.build_universe(10.0)
    provider = orb.KiteHistoricalDataProvider()
    from_date = datetime(2024, 4, 1)
    to_date   = datetime(2026, 4, 10)

    print("Loading daily data...")
    all_qualifying = []
    for sym in symbols:
        daily = orb.load_daily_data(provider, sym, from_date, to_date)
        if daily is None or daily.empty:
            continue
        quals = orb.find_qualifying_days(daily, sym)
        quals = [q for q in quals if q["vol_ratio"] >= 2.0]
        all_qualifying.extend(quals)

    by_date = defaultdict(list)
    for q in all_qualifying:
        by_date[q["date"]].append(q)
    selected = []
    for dt, candidates in sorted(by_date.items()):
        ranked = sorted(candidates, key=lambda x: x["vol_ratio"], reverse=True)
        selected.extend(ranked[:3])
    print(f"Selected: {len(selected)} stock-days")

    # Define parameter combos
    combos = [
        # Baseline
        {"label": "BASELINE (1.5/0.30)",
         "max_range": 1.5, "vol_frac": 0.30},

        # Widen range only
        {"label": "range=2.0 vf=0.30",
         "max_range": 2.0, "vol_frac": 0.30},
        {"label": "range=2.5 vf=0.30",
         "max_range": 2.5, "vol_frac": 0.30},
        {"label": "range=3.0 vf=0.30",
         "max_range": 3.0, "vol_frac": 0.30},

        # Lower vol fraction only
        {"label": "range=1.5 vf=0.15",
         "max_range": 1.5, "vol_frac": 0.15},
        {"label": "range=1.5 vf=0.10",
         "max_range": 1.5, "vol_frac": 0.10},
        {"label": "range=1.5 vf=0.00",
         "max_range": 1.5, "vol_frac": 0.00},

        # Combos
        {"label": "range=2.0 vf=0.15",
         "max_range": 2.0, "vol_frac": 0.15},
        {"label": "range=2.0 vf=0.10",
         "max_range": 2.0, "vol_frac": 0.10},
        {"label": "range=2.5 vf=0.15",
         "max_range": 2.5, "vol_frac": 0.15},
        {"label": "range=2.5 vf=0.10",
         "max_range": 2.5, "vol_frac": 0.10},
        {"label": "range=3.0 vf=0.10",
         "max_range": 3.0, "vol_frac": 0.10},

        # Wider SL cap for wider ranges
        {"label": "range=2.5 vf=0.15 sl=2.5%",
         "max_range": 2.5, "vol_frac": 0.15, "sl_max": 0.025},
        {"label": "range=3.0 vf=0.10 sl=3.0%",
         "max_range": 3.0, "vol_frac": 0.10, "sl_max": 0.030},

        # Different target multipliers
        {"label": "range=2.5 vf=0.15 t2=3x",
         "max_range": 2.5, "vol_frac": 0.15, "t2_mult": 3.0},
        {"label": "range=2.0 vf=0.15 t2=3x",
         "max_range": 2.0, "vol_frac": 0.15, "t2_mult": 3.0},
    ]

    print(f"\nRunning {len(combos)} parameter combos...\n")
    results = run_sweep(selected, combos)

    # Print results table
    print(f"\n{'='*110}")
    print(f"  ORB PARAMETER SWEEP RESULTS")
    print(f"{'='*110}")
    print(f"  {'Params':<30s} {'#':>4s} {'WR%':>6s} {'Avg₹':>7s} {'Tot₹':>8s} {'Mo₹':>7s} {'T/m':>5s} {'MaxL':>5s} {'AvgR%':>6s} {'Exit breakdown'}")
    print(f"  {'-'*108}")

    for r in results:
        if r["n"] == 0:
            print(f"  {r['label']:<30s} {0:>4d}")
            continue
        exits_str = " ".join(f"{k}:{v}" for k, v in r["exits"].items())
        print(f"  {r['label']:<30s} {r['n']:>4d} {r['wr']:>5.1f}% "
              f"₹{r['avg_pnl']:>+6.0f} ₹{r['tot_pnl']:>+7.0f} ₹{r['monthly']:>+6.0f} "
              f"{r['tpm']:>4.1f} {r['max_loss_streak']:>5d} {r['avg_range']:>5.2f}% {exits_str}")

    print(f"{'='*110}")


if __name__ == "__main__":
    main()

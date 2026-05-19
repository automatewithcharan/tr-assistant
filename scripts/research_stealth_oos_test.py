"""Out-of-sample test of stealth_accumulation on 2020-2024 data.

Runs the EXACT best config from in-sample (2024-2026) on completely fresh
historical data the strategy was never developed on. This is the strongest
non-real-time test of edge persistence.
"""

from __future__ import annotations
import sys, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import scripts.research_stealth_accumulation as mod
from scripts.research_stealth_accumulation import (
    add_features, generate_signals, run_config, summarize, StealthConfig
)

# Override the cache dir to point to the OOS data
OOS_DIR = REPO / "results" / "Data" / "kite_history_backfill" / "day" / "2020-05-13_2024-05-12"
IS_DIR  = REPO / "results" / "Data" / "kite_history_backfill" / "day" / "2024-05-13_2026-05-12"


def load_window(cache_dir: Path, universe_csv: Path):
    """Same loader, but point at a specific dir."""
    mod.DAY_CACHE = cache_dir
    df, nifty = mod.load_features(universe_csv)
    return df, nifty


def run_window(label: str, cache_dir: Path, universe_csv: Path, cfg: StealthConfig):
    print(f"\n=== {label}: data dir = {cache_dir.name} ===")
    df, nifty = load_window(cache_dir, universe_csv)
    print(f"  Loaded: {df['symbol'].nunique()} symbols, date range {df['date'].min().date()} → {df['date'].max().date()}")
    feats = add_features(df, nifty, cons_window=60)
    n_days = feats["date"].nunique()
    print(f"  Trading days: {n_days}")
    trades = run_config(feats, cfg)
    s = summarize(trades, n_days)
    print(f"\n  Results:")
    if s.get("n", 0) == 0:
        print(f"    NO TRADES")
        return s, trades
    print(f"    Trades:             {s['n']}")
    print(f"    Win rate:           {s['win_rate_pct']}%")
    print(f"    Avg net/trade:      {s['avg_net_per_trade_pct']:+.3f}%")
    print(f"    Avg hold:           {s['avg_hold_days']:.1f} days")
    print(f"    Profit factor:      {s['profit_factor']:.2f}")
    print(f"    Total cumulative:   {s['total_net_pct']:.2f}%")
    print(f"    Avg daily:          {s['avg_daily_net_pct']:+.4f}%")
    if not trades.empty:
        print(f"\n  By exit reason:")
        print(trades.groupby("exit_reason")["trade_return_pct"].agg(["count", "mean"]).to_string())
    return s, trades


def main():
    universe = REPO / "results" / "Data" / "stealth_universe.csv"

    # Frozen best config from in-sample 2024-2026 research
    BEST = StealthConfig(
        target_pct=20.0,
        stop_pct=5.0,
        max_hold_days=45,
        params={
            "max_range_pct": 10,
            "min_rs_pct": 6,
            "min_accum_ratio": 1.0,
            "max_ret_10d_pct": 8,
            "min_value_cr": 1.0,
            "min_price": 40,
        },
    )
    print(f"Frozen config: {BEST.label()}")

    # 1) Re-confirm in-sample result with this exact config
    is_summary, is_trades = run_window("IN-SAMPLE 2024-2026 (developed on)", IS_DIR, universe, BEST)

    # 2) Out-of-sample test on 2020-2024
    oos_summary, oos_trades = run_window("OUT-OF-SAMPLE 2020-2024 (never seen)", OOS_DIR, universe, BEST)

    # 3) Verdict
    print(f"\n{'='*60}")
    print(f"VERDICT")
    print(f"{'='*60}")
    is_daily = is_summary.get("avg_daily_net_pct", 0)
    oos_daily = oos_summary.get("avg_daily_net_pct", 0)
    is_n = is_summary.get("n", 0)
    oos_n = oos_summary.get("n", 0)
    print(f"  In-sample (2024-2026):     {is_daily:+.4f}%/day  ({is_n} trades)")
    print(f"  Out-of-sample (2020-2024): {oos_daily:+.4f}%/day  ({oos_n} trades)")
    if oos_daily > 0 and is_daily > 0:
        ratio = oos_daily / is_daily
        print(f"  OOS / IS ratio:            {ratio:.2f}x")
        if ratio >= 0.7:
            print(f"  → EDGE LIKELY REAL — OOS holds {ratio*100:.0f}% of IS performance")
        elif ratio >= 0.3:
            print(f"  → EDGE WEAKLY CONFIRMED — OOS shows {ratio*100:.0f}% of IS, partial validation")
        else:
            print(f"  → POSSIBLE OVERFIT — OOS shows {ratio*100:.0f}% of IS")
    elif oos_daily <= 0:
        print(f"  → LIKELY OVERFIT — strategy negative on never-seen data")
    elif is_daily <= 0:
        print(f"  → IS itself negative (something wrong with re-run)")

    # Save merged trades
    out = REPO / "results" / "Data" / "stealth_oos_trades.csv"
    combined = pd.concat([
        is_trades.assign(window="in_sample"),
        oos_trades.assign(window="out_of_sample"),
    ], ignore_index=True)
    combined.to_csv(out, index=False)
    print(f"\n  Wrote: {out}")


if __name__ == "__main__":
    main()

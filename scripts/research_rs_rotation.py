"""Relative-strength leader rotation — buy top-RS stocks, rebalance monthly.

Signal philosophy:
  Cross-sectional momentum (Jegadeesh-Titman 1993): stocks that outperformed
  the universe over the past 6 months tend to keep outperforming the next
  1-3 months. Buy the top N by 6-month RS, hold 1 month, rebalance.

Implementation:
  Each month-end:
    1. Rank all liquid stocks by 6-month return
    2. Skip top 5% (avoid blow-off-top reversals)
    3. Buy ranks 6-25 (top quintile minus tail) with equal weight
    4. Hold 21 trading days
    5. At end, repeat with fresh ranks
"""

from __future__ import annotations
import argparse, sys, warnings
warnings.filterwarnings("ignore")
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DAY_CACHE = REPO / "results" / "Data" / "kite_history_backfill" / "day" / "2024-05-13_2026-05-12"
RESULTS = REPO / "results" / "Data"


def fee() -> float:
    return 0.30


def load_features(universe_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    syms = pd.read_csv(universe_path)["symbol"].str.upper().tolist()
    parts = []
    for s in syms:
        fp = DAY_CACHE / f"{s}.csv"
        if not fp.exists():
            continue
        d = pd.read_csv(fp)
        d["datetime"] = pd.to_datetime(d["datetime"], utc=True).dt.tz_convert(None)
        d["date"] = d["datetime"].dt.normalize()
        d["symbol"] = s
        parts.append(d[["date", "symbol", "open", "high", "low", "close", "volume"]])
    df = pd.concat(parts, ignore_index=True)

    nfp = DAY_CACHE / "NIFTY 50.csv"
    if nfp.exists():
        n = pd.read_csv(nfp)
        n["date"] = pd.to_datetime(n["datetime"], utc=True).dt.tz_convert(None).dt.normalize()
        nifty = n[["date", "close"]].rename(columns={"close": "nifty_close"}).sort_values("date")
    else:
        nifty = pd.DataFrame()
    return df, nifty


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    work = df.sort_values(["symbol", "date"]).reset_index(drop=True).copy()

    def per_sym(g):
        g = g.sort_values("date").copy()
        g["ret_6m"] = g["close"].pct_change(120) * 100  # 6-month return (120 trading days)
        g["ret_3m"] = g["close"].pct_change(60) * 100
        g["ret_1m"] = g["close"].pct_change(21) * 100
        g["day_value_cr"] = g["close"] * g["volume"] / 1e7
        g["avg_value20_cr"] = g["day_value_cr"].shift(1).rolling(20).mean()
        return g

    _sym = work["symbol"].copy()
    work = work.groupby("symbol", sort=False, group_keys=False).apply(per_sym)
    if "symbol" not in work.columns:
        work.insert(0, "symbol", _sym.reindex(work.index))
    return work.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)


def run_rotation(features: pd.DataFrame, top_pct_skip: float, top_n: int,
                 hold_days: int, min_value_cr: float, ret_lookback: str) -> tuple[pd.DataFrame, float]:
    """Walk forward through trading days; at every `hold_days` interval, rebalance."""
    features = features.copy()
    features["date"] = pd.to_datetime(features["date"])
    all_dates = sorted(features["date"].unique())
    if len(all_dates) < 130:
        return pd.DataFrame(), 0.0

    cost = fee()
    trades = []
    rebalance_idx = 120  # need 6 months of history first
    while rebalance_idx + hold_days < len(all_dates):
        rebal_date = all_dates[rebalance_idx]
        entry_idx = rebalance_idx + 1
        if entry_idx >= len(all_dates):
            break
        entry_date = all_dates[entry_idx]
        exit_idx = min(rebalance_idx + hold_days, len(all_dates) - 1)
        exit_date = all_dates[exit_idx]

        # Snapshot at rebalance_date
        snap = features[features["date"] == rebal_date]
        snap = snap[(snap["avg_value20_cr"] >= min_value_cr) & snap[ret_lookback].notna()]
        if len(snap) < top_n + 5:
            rebalance_idx += hold_days
            continue
        # Rank: skip top N% blow-offs, take next top_n
        snap = snap.sort_values(ret_lookback, ascending=False).reset_index(drop=True)
        skip_count = max(1, int(len(snap) * top_pct_skip / 100))
        winners = snap.iloc[skip_count: skip_count + top_n]["symbol"].tolist()

        # For each winner: buy at entry_date open, sell at exit_date close
        for sym in winners:
            entry_row = features[(features["symbol"] == sym) & (features["date"] == entry_date)]
            exit_row = features[(features["symbol"] == sym) & (features["date"] == exit_date)]
            if entry_row.empty or exit_row.empty:
                continue
            entry_px = float(entry_row["open"].iloc[0])
            exit_px = float(exit_row["close"].iloc[0])
            if entry_px <= 0 or np.isnan(entry_px):
                continue
            gross = (exit_px / entry_px - 1) * 100
            net = gross - cost
            trades.append({
                "rebal_date": rebal_date, "entry_date": entry_date, "exit_date": exit_date,
                "symbol": sym, "entry_price": round(entry_px, 4), "exit_price": round(exit_px, 4),
                "trade_return_pct": round(net, 4), "hold_days": exit_idx - entry_idx,
            })
        rebalance_idx += hold_days

    return pd.DataFrame(trades), len(all_dates)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=str(RESULTS / "stealth_universe.csv"))
    ap.add_argument("--out-prefix", default=str(RESULTS / "rs_rotation"))
    args = ap.parse_args()

    print(f"[i] Loading {args.universe}")
    df, _ = load_features(Path(args.universe))
    feats = add_features(df)
    n_days = feats["date"].nunique()
    print(f"[i] {df['symbol'].nunique()} symbols, {n_days} trading days")

    grid = {
        "top_pct_skip":  [0, 5, 10],     # skip top N% as blow-off
        "top_n":         [10, 20, 30],   # how many to hold
        "hold_days":     [10, 21, 42],   # rebalance interval (2w / 1m / 2m)
        "min_value_cr":  [10, 25],
        "ret_lookback":  ["ret_6m", "ret_3m", "ret_1m"],
    }
    keys = list(grid.keys())
    combos = list(product(*grid.values()))
    print(f"[i] Grid: {len(combos)} configs")

    results = []
    for idx, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        trades, total_days = run_rotation(feats, **params)
        if trades.empty:
            results.append({"n": 0, **{f"cfg_{k}": v for k, v in params.items()}})
            continue
        n = len(trades)
        wins = trades[trades["trade_return_pct"] > 0]
        losses = trades[trades["trade_return_pct"] <= 0]
        gp = wins["trade_return_pct"].sum()
        gl = -losses["trade_return_pct"].sum()
        # For rotation, capital is split across positions, so daily return per portfolio = sum/n_days/top_n
        daily_per_unit_capital = trades["trade_return_pct"].sum() / total_days / params["top_n"]
        results.append({
            "n": n, "n_unique_rebalances": trades["rebal_date"].nunique(),
            "win_rate_pct": round(len(wins) / n * 100, 2),
            "avg_net_per_trade_pct": round(trades["trade_return_pct"].mean(), 4),
            "profit_factor": round(gp / gl if gl > 0 else np.inf, 3),
            "total_basket_return_pct": round(trades["trade_return_pct"].sum() / params["top_n"], 2),
            "avg_daily_basket_pct": round(daily_per_unit_capital, 4),
            "label": f"rs_{params['ret_lookback']}|skip={params['top_pct_skip']}%|topN={params['top_n']}|hold={params['hold_days']}d|val>={params['min_value_cr']}cr",
            **{f"cfg_{k}": v for k, v in params.items()},
        })

    res = pd.DataFrame(results)
    res = res[res["n"] > 0].sort_values("avg_daily_basket_pct", ascending=False)
    out_csv = Path(f"{args.out_prefix}_ranking.csv")
    res.to_csv(out_csv, index=False)
    print(f"\n[+] Wrote: {out_csv}")

    print(f"\n=== TOP 15 ===")
    cols = ["n", "n_unique_rebalances", "win_rate_pct", "avg_net_per_trade_pct", "profit_factor", "total_basket_return_pct", "avg_daily_basket_pct", "label"]
    print(res[cols].head(15).to_string(index=False))

    print(f"\nConfigs with >0 daily:   {(res['avg_daily_basket_pct'] > 0).sum()} / {len(res)}")
    print(f"Configs >= 0.1%/day:     {(res['avg_daily_basket_pct'] >= 0.1).sum()}")
    print(f"Configs >= 0.3%/day:     {(res['avg_daily_basket_pct'] >= 0.3).sum()}  (Stage 1 gate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

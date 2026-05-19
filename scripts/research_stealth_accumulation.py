"""Stealth accumulation scanner — find stocks consolidating quietly before a breakout.

Signal philosophy:
  Catch stocks in late Stage 1 / early Stage 2 (Weinstein) — stocks that are
  consolidating in a tight range with accumulation patterns, BEFORE the
  multi-week trend becomes obvious to everyone.

Signal components:
  1. Tight consolidation:  60-day price range ≤ X% (Bollinger / range tightness)
  2. Relative strength:    stock 60-day return ≥ Nifty 60-day return + threshold
  3. Quiet accumulation:   recent 20d up-day volume / down-day volume ≥ threshold
  4. Not already in mainstream momentum: 10-day return ≤ ceiling (avoid chasing)
  5. Above 200-day EMA:    only stocks in long-term uptrend
  6. Liquidity:            avg 60-day turnover ≥ ₹1cr/day

Entry: next day's open after signal.
Exit:  +target / -stop / max_hold (whichever fires first)
Cost:  Indian delivery fees (broker + STT + GST + stamp).

All features use shift(1).rolling() — no lookahead.
"""

from __future__ import annotations
import argparse, os, sys, json, warnings
warnings.filterwarnings("ignore", category=FutureWarning)
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DAY_CACHE = REPO / "results" / "Data" / "kite_history_backfill" / "day" / "2024-05-13_2026-05-12"
RESULTS = REPO / "results" / "Data"


def indian_delivery_fee_pct(avg_value_cr: float = 10.0) -> float:
    """Round-trip delivery costs: brokerage + STT + GST + stamp + small slippage."""
    return 0.30  # 0.15% one-way × 2 (conservative)


@dataclass(frozen=True)
class StealthConfig:
    family: str = "stealth_accumulation"
    target_pct: float = 12.0
    stop_pct: float = 5.0
    max_hold_days: int = 30
    params: dict = field(default_factory=dict)

    def label(self) -> str:
        p = self.params
        return (
            f"stealth|cons={p.get('consolidation_window')}d|"
            f"tight<={p.get('max_range_pct')}%|"
            f"rs>={p.get('min_rs_pct')}%|"
            f"accum>={p.get('min_accum_ratio')}|"
            f"ret10<={p.get('max_ret_10d_pct')}%|"
            f"t={self.target_pct}|s={self.stop_pct}|hold={self.max_hold_days}"
        )


def load_features(universe_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load daily OHLC for universe + NIFTY 50, returning concatenated features."""
    syms = pd.read_csv(universe_path)["symbol"].str.upper().tolist()
    parts = []
    for s in syms:
        fp = DAY_CACHE / f"{s}.csv"
        if not fp.exists():
            continue
        d = pd.read_csv(fp)
        d["datetime"] = pd.to_datetime(d["datetime"])
        d["date"] = d["datetime"].dt.date
        d["symbol"] = s
        parts.append(d[["date", "symbol", "open", "high", "low", "close", "volume"]])
    if not parts:
        raise RuntimeError("No symbol data loaded")
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])

    # Nifty
    nfp = DAY_CACHE / "NIFTY 50.csv"
    if nfp.exists():
        n = pd.read_csv(nfp)
        n["date"] = pd.to_datetime(n["datetime"], utc=True).dt.tz_convert(None).dt.normalize()
        nifty = n[["date", "close"]].rename(columns={"close": "nifty_close"}).sort_values("date")
    else:
        nifty = pd.DataFrame(columns=["date", "nifty_close"])
    return df, nifty


def add_features(df: pd.DataFrame, nifty: pd.DataFrame, cons_window: int = 60) -> pd.DataFrame:
    """Add per-symbol features. All rolling features use shift(1) — no lookahead."""
    work = df.sort_values(["symbol", "date"]).reset_index(drop=True).copy()

    def per_sym(g):
        g = g.sort_values("date").copy()
        # Price range over consolidation window (uses prior bars only)
        g["range_high"] = g["close"].shift(1).rolling(cons_window).max()
        g["range_low"]  = g["close"].shift(1).rolling(cons_window).min()
        g["range_mid"]  = (g["range_high"] + g["range_low"]) / 2.0
        g["range_pct"]  = (g["range_high"] - g["range_low"]) / g["range_mid"] * 100.0
        # Returns observable at close of T
        g["ret_10d"]  = g["close"].pct_change(10) * 100.0
        g["ret_60d"]  = g["close"].pct_change(cons_window) * 100.0
        # Trend filter
        g["ema_200"]  = g["close"].ewm(span=200, adjust=False).mean()
        g["above_ema200"] = (g["close"] > g["ema_200"]).astype(int)
        # Volume accumulation: ratio of up-day to down-day volume over last 20 bars (prior, no T)
        prev_close = g["close"].shift(1)
        up = (g["close"] > prev_close).shift(1).fillna(False).astype(int)
        down = (g["close"] < prev_close).shift(1).fillna(False).astype(int)
        vol_prev = g["volume"].shift(1)
        up_vol = (vol_prev * up).rolling(20).sum()
        down_vol = (vol_prev * down).rolling(20).sum()
        g["accum_ratio"] = up_vol / down_vol.replace(0, np.nan)
        # Liquidity
        g["day_value_cr"] = g["close"] * g["volume"] / 1e7
        g["avg_value60_cr"] = g["day_value_cr"].shift(1).rolling(60).mean()
        # Breakout trigger: today's close breaks above the consolidation range
        g["breaks_range_high"] = (g["close"] > g["range_high"]).astype(int)
        return g

    import warnings
    _sym = work["symbol"].copy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        work = work.groupby("symbol", sort=False, group_keys=False).apply(per_sym)
    if "symbol" not in work.columns:
        work.insert(0, "symbol", _sym.reindex(work.index))

    # Nifty features
    if not nifty.empty:
        n = nifty.sort_values("date").copy()
        n["nifty_ret_60d"] = n["nifty_close"].pct_change(cons_window) * 100.0
        work = work.merge(n[["date", "nifty_close", "nifty_ret_60d"]], on="date", how="left")
        work["rs_pct"] = work["ret_60d"] - work["nifty_ret_60d"]
    else:
        work["rs_pct"] = work["ret_60d"]

    return work.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)


def generate_signals(features: pd.DataFrame, cfg: StealthConfig) -> pd.Series:
    p = cfg.params
    cond = (
        (features["avg_value60_cr"].fillna(0) >= float(p.get("min_value_cr", 1.0)))
        & (features["close"] >= float(p.get("min_price", 40)))
        & (features["range_pct"] <= float(p.get("max_range_pct", 15.0)))
        & (features["rs_pct"] >= float(p.get("min_rs_pct", 0.0)))
        & (features["accum_ratio"] >= float(p.get("min_accum_ratio", 1.0)))
        & (features["ret_10d"] <= float(p.get("max_ret_10d_pct", 5.0)))
        & (features["above_ema200"] == 1)
        & (features["breaks_range_high"] == 1)  # trigger day
    )
    return cond.fillna(False)


def simulate_trades_for_symbol(sdf: pd.DataFrame, signal_mask: pd.Series, cfg: StealthConfig) -> list[dict]:
    sdf = sdf.sort_values("date")
    sig = signal_mask.reindex(sdf.index).fillna(False).to_numpy(dtype=bool)
    sdf = sdf.reset_index(drop=True)
    signal_mask = pd.Series(sig, index=sdf.index)
    symbol = str(sdf["symbol"].iloc[0]) if not sdf.empty else ""
    avg_val = float(sdf["avg_value60_cr"].median() or 0.0)
    cost = indian_delivery_fee_pct(avg_val)
    trades = []
    skip_until = -1
    for i in range(len(sdf) - 1):
        if i <= skip_until or not signal_mask.iloc[i]:
            continue
        entry_row = sdf.iloc[i + 1]
        entry_px = float(entry_row["open"])
        if entry_px <= 0 or np.isnan(entry_px):
            continue
        target = entry_px * (1.0 + cfg.target_pct / 100.0)
        stop = entry_px * (1.0 - cfg.stop_pct / 100.0)
        exit_px = np.nan; reason = "max_hold"
        end_idx = min(i + 1 + cfg.max_hold_days, len(sdf) - 1)
        actual_exit = end_idx
        for j in range(i + 1, end_idx + 1):
            bar = sdf.iloc[j]
            lo, hi = float(bar["low"]), float(bar["high"])
            if lo <= stop:
                exit_px, reason, actual_exit = stop, "stop", j; break
            if hi >= target:
                exit_px, reason, actual_exit = target, "target", j; break
        if np.isnan(exit_px):
            exit_px = float(sdf.iloc[actual_exit]["close"])
        gross = (exit_px / entry_px - 1.0) * 100.0
        net = gross - cost
        trades.append({
            "signal_date": sdf.iloc[i]["date"],
            "entry_date": entry_row["date"],
            "exit_date": sdf.iloc[actual_exit]["date"],
            "symbol": symbol,
            "entry_price": round(entry_px, 4),
            "exit_price": round(exit_px, 4),
            "gross_return_pct": round(gross, 4),
            "cost_pct": round(cost, 4),
            "trade_return_pct": round(net, 4),
            "exit_reason": reason,
            "hold_days": actual_exit - (i + 1),
        })
        skip_until = actual_exit
    return trades


def run_config(features: pd.DataFrame, cfg: StealthConfig) -> pd.DataFrame:
    sig = generate_signals(features, cfg)
    all_trades = []
    for sym, sdf in features.groupby("symbol", sort=False):
        m = sig.reindex(sdf.index).fillna(False)
        if not m.any():
            continue
        all_trades.extend(simulate_trades_for_symbol(sdf, m, cfg))
    return pd.DataFrame(all_trades) if all_trades else pd.DataFrame()


def summarize(trades: pd.DataFrame, num_trading_days: int) -> dict:
    if trades.empty:
        return {"n": 0}
    n = len(trades)
    wins = trades[trades["trade_return_pct"] > 0]
    win_rate = len(wins) / n * 100
    avg_net = trades["trade_return_pct"].mean()
    avg_hold = trades["hold_days"].mean()
    gross_profit = wins["trade_return_pct"].sum()
    gross_loss = -trades[trades["trade_return_pct"] <= 0]["trade_return_pct"].sum()
    pf = gross_profit / gross_loss if gross_loss > 0 else np.inf
    # Avg daily return = sum(net) / trading_days  (treating capital as 1 position rotated)
    total_net = trades["trade_return_pct"].sum()
    avg_daily = total_net / num_trading_days
    by_reason = trades.groupby("exit_reason")["trade_return_pct"].agg(["count", "mean"]).to_dict("index")
    return {
        "n": n, "win_rate_pct": round(win_rate, 2), "avg_net_per_trade_pct": round(avg_net, 4),
        "avg_hold_days": round(avg_hold, 2), "profit_factor": round(pf, 3),
        "total_net_pct": round(total_net, 2), "avg_daily_net_pct": round(avg_daily, 4),
        "by_reason": by_reason,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=str(RESULTS / "intraday_research_universe_FINAL.csv"))
    ap.add_argument("--start", default="2024-05-13")
    ap.add_argument("--end", default="2026-05-12")
    ap.add_argument("--out-prefix", default=str(RESULTS / "stealth_accum"))
    args = ap.parse_args()

    print(f"[i] Loading universe: {args.universe}")
    df, nifty = load_features(Path(args.universe))
    df = df[(df["date"] >= args.start) & (df["date"] <= args.end)].copy()
    print(f"[i] Loaded {df['symbol'].nunique()} symbols, {len(df)} rows")

    print(f"[i] Computing features...")
    feats = add_features(df, nifty, cons_window=60)
    n_days = feats["date"].nunique()
    print(f"[i] {n_days} trading days")

    # Grid search
    grid = {
        "max_range_pct":   [10, 15, 20],
        "min_rs_pct":      [0, 3, 6],
        "min_accum_ratio": [1.0, 1.3, 1.6],
        "max_ret_10d_pct": [3, 5, 8],
        "target_pct":      [8, 12, 20],
        "stop_pct":        [3, 5],
        "max_hold_days":   [20, 30, 45],
    }
    keys = list(grid.keys())
    combos = list(product(*grid.values()))
    print(f"[i] Grid: {len(combos)} configs")

    results = []
    all_trades = {}
    for idx, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        cfg = StealthConfig(
            target_pct=params["target_pct"],
            stop_pct=params["stop_pct"],
            max_hold_days=params["max_hold_days"],
            params={k: v for k, v in params.items() if k not in ("target_pct", "stop_pct", "max_hold_days")},
        )
        trades = run_config(feats, cfg)
        s = summarize(trades, n_days)
        s["label"] = cfg.label()
        s.update({f"cfg_{k}": v for k, v in params.items()})
        results.append(s)
        if not trades.empty and s.get("avg_daily_net_pct", -99) > 0.05:
            all_trades[cfg.label()] = trades
        if idx % 50 == 0:
            print(f"  [{idx}/{len(combos)}] best so far: {max(r.get('avg_daily_net_pct', -99) for r in results):.4f}%/day")

    res = pd.DataFrame(results).sort_values("avg_daily_net_pct", ascending=False)
    out_csv = Path(f"{args.out_prefix}_ranking.csv")
    res.drop(columns=["by_reason"], errors="ignore").to_csv(out_csv, index=False)
    print(f"\n[+] Wrote ranking: {out_csv}")

    print(f"\n=== TOP 15 configs by avg_daily_net_pct ===")
    cols = ["n", "win_rate_pct", "avg_net_per_trade_pct", "avg_hold_days", "profit_factor", "avg_daily_net_pct", "label"]
    print(res[cols].head(15).to_string(index=False))

    print(f"\n=== Distribution ===")
    print(res["avg_daily_net_pct"].describe())
    print(f"\nConfigs with >0 daily:   {(res['avg_daily_net_pct'] > 0).sum()} / {len(res)}")
    print(f"Configs >= 0.1%/day:     {(res['avg_daily_net_pct'] >= 0.1).sum()}")
    print(f"Configs >= 0.3%/day:     {(res['avg_daily_net_pct'] >= 0.3).sum()}  (Stage 1 gate)")
    print(f"Configs >= 0.5%/day:     {(res['avg_daily_net_pct'] >= 0.5).sum()}")

    # Save best config trades
    if all_trades:
        best_label = res.iloc[0]["label"]
        if best_label in all_trades:
            tp = Path(f"{args.out_prefix}_best_trades.csv")
            all_trades[best_label].to_csv(tp, index=False)
            print(f"\n[+] Wrote best-config trades: {tp}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Pullback-to-20-EMA scanner — re-enter trends after a dip.

Signal philosophy:
  Catch stocks in established uptrends that have pulled back to their 20-EMA
  and are now bouncing. This is the canonical "buy the dip" trade in a healthy
  trend — opposite timing from the stealth-breakout strategy.

Signal components:
  1. Healthy uptrend stack:  close > ema_50 > ema_200 (proper alignment)
  2. 200-EMA rising:         long-term trend up
  3. Pullback to 20-EMA:     low touched or came within X% of ema_20
  4. RSI oversold during pullback: 14-day RSI dipped below threshold (e.g., 45)
  5. Bounce confirmation:    today closes higher than yesterday AND above ema_20
  6. Liquidity & price gates

Entry: next day's open after the bounce confirmation day.
Exit:  +target / -stop / max_hold

All features use shift(1).rolling() — strict no-lookahead.
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
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DAY_CACHE = REPO / "results" / "Data" / "kite_history_backfill" / "day" / "2024-05-13_2026-05-12"
RESULTS = REPO / "results" / "Data"


def indian_delivery_fee_pct(avg_value_cr: float = 10.0) -> float:
    return 0.30  # ~0.15% one-way × 2


@dataclass(frozen=True)
class PullbackConfig:
    family: str = "pullback_ema20"
    target_pct: float = 6.0
    stop_pct: float = 4.0
    max_hold_days: int = 15
    params: dict = field(default_factory=dict)

    def label(self) -> str:
        p = self.params
        return (
            f"pullback|prox<={p.get('max_dist_pct')}%|"
            f"rsi<={p.get('max_rsi_low')}|"
            f"slope_ema50>={p.get('min_ema50_slope_pct')}%|"
            f"t={self.target_pct}|s={self.stop_pct}|hold={self.max_hold_days}"
        )


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
    if not parts:
        raise RuntimeError("No symbol data loaded")
    df = pd.concat(parts, ignore_index=True)

    nfp = DAY_CACHE / "NIFTY 50.csv"
    if nfp.exists():
        n = pd.read_csv(nfp)
        n["date"] = pd.to_datetime(n["datetime"], utc=True).dt.tz_convert(None).dt.normalize()
        nifty = n[["date", "close"]].rename(columns={"close": "nifty_close"}).sort_values("date")
    else:
        nifty = pd.DataFrame(columns=["date", "nifty_close"])
    return df, nifty


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def add_features(df: pd.DataFrame, nifty: pd.DataFrame) -> pd.DataFrame:
    work = df.sort_values(["symbol", "date"]).reset_index(drop=True).copy()

    def per_sym(g):
        g = g.sort_values("date").copy()
        # EMAs (current bar's close is observable at close → safe)
        g["ema_20"] = g["close"].ewm(span=20, adjust=False).mean()
        g["ema_50"] = g["close"].ewm(span=50, adjust=False).mean()
        g["ema_200"] = g["close"].ewm(span=200, adjust=False).mean()
        # Slope of EMA-50 over last 10 bars (using prior values, no lookahead)
        g["ema_50_slope_pct"] = (g["ema_50"] - g["ema_50"].shift(10)) / g["ema_50"].shift(10) * 100.0
        # Trend stack
        g["trend_up"] = ((g["close"] > g["ema_50"]) & (g["ema_50"] > g["ema_200"])).astype(int)
        # Distance to 20-EMA at today's low (positive = low went below EMA, negative = stayed above)
        g["low_to_ema20_pct"] = (g["ema_20"] - g["low"]) / g["ema_20"] * 100.0
        # 14-day RSI; we want the MINIMUM RSI in last 5 bars (was oversold recently)
        g["rsi_14"] = _rsi(g["close"], 14)
        g["rsi_min_5d"] = g["rsi_14"].rolling(5).min()
        # Bounce confirmation: today close > yesterday's close AND today close > ema_20
        g["bounce_today"] = ((g["close"] > g["close"].shift(1)) & (g["close"] > g["ema_20"])).astype(int)
        # Liquidity
        g["day_value_cr"] = g["close"] * g["volume"] / 1e7
        g["avg_value20_cr"] = g["day_value_cr"].shift(1).rolling(20).mean()
        return g

    _sym = work["symbol"].copy()
    work = work.groupby("symbol", sort=False, group_keys=False).apply(per_sym)
    if "symbol" not in work.columns:
        work.insert(0, "symbol", _sym.reindex(work.index))

    if not nifty.empty:
        n = nifty.sort_values("date").copy()
        n["nifty_ema_50"] = n["nifty_close"].ewm(span=50, adjust=False).mean()
        n["nifty_regime_bull"] = (n["nifty_close"] > n["nifty_ema_50"]).astype(int)
        work = work.merge(n[["date", "nifty_close", "nifty_regime_bull"]], on="date", how="left")
    else:
        work["nifty_regime_bull"] = 1

    return work.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)


def generate_signals(features: pd.DataFrame, cfg: PullbackConfig) -> pd.Series:
    p = cfg.params
    cond = (
        (features["avg_value20_cr"].fillna(0) >= float(p.get("min_value_cr", 5.0)))
        & (features["close"] >= float(p.get("min_price", 40)))
        & (features["trend_up"] == 1)
        & (features["ema_50_slope_pct"] >= float(p.get("min_ema50_slope_pct", 1.0)))
        & (features["low_to_ema20_pct"] >= -float(p.get("max_dist_pct", 2.0)))  # low came near or below EMA-20
        & (features["rsi_min_5d"] <= float(p.get("max_rsi_low", 45)))  # was oversold recently
        & (features["bounce_today"] == 1)  # today shows reversal
    )
    if p.get("nifty_regime") == "bull":
        cond = cond & (features["nifty_regime_bull"] == 1)
    return cond.fillna(False)


def simulate_trades_for_symbol(sdf: pd.DataFrame, signal_mask: pd.Series, cfg: PullbackConfig) -> list[dict]:
    sdf = sdf.sort_values("date")
    sig = signal_mask.reindex(sdf.index).fillna(False).to_numpy(dtype=bool)
    sdf = sdf.reset_index(drop=True)
    signal_mask = pd.Series(sig, index=sdf.index)
    symbol = str(sdf["symbol"].iloc[0]) if not sdf.empty else ""
    cost = indian_delivery_fee_pct()
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


def run_config(features: pd.DataFrame, cfg: PullbackConfig) -> pd.DataFrame:
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
    losses = trades[trades["trade_return_pct"] <= 0]
    win_rate = len(wins) / n * 100
    avg_net = trades["trade_return_pct"].mean()
    avg_hold = trades["hold_days"].mean()
    gp = wins["trade_return_pct"].sum()
    gl = -losses["trade_return_pct"].sum()
    pf = gp / gl if gl > 0 else np.inf
    total = trades["trade_return_pct"].sum()
    return {
        "n": n, "win_rate_pct": round(win_rate, 2),
        "avg_net_per_trade_pct": round(avg_net, 4), "avg_hold_days": round(avg_hold, 2),
        "profit_factor": round(pf, 3), "total_net_pct": round(total, 2),
        "avg_daily_net_pct": round(total / num_trading_days, 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=str(RESULTS / "stealth_universe.csv"))
    ap.add_argument("--start", default="2024-05-13")
    ap.add_argument("--end", default="2026-05-12")
    ap.add_argument("--out-prefix", default=str(RESULTS / "pullback_ema20"))
    args = ap.parse_args()

    print(f"[i] Loading universe: {args.universe}")
    df, nifty = load_features(Path(args.universe))
    df = df[(df["date"] >= args.start) & (df["date"] <= args.end)].copy()
    print(f"[i] Loaded {df['symbol'].nunique()} symbols, {len(df)} rows")

    print(f"[i] Computing features...")
    feats = add_features(df, nifty)
    n_days = feats["date"].nunique()
    print(f"[i] {n_days} trading days")

    grid = {
        "max_dist_pct":         [1.0, 2.0, 3.0],    # how close low must come to ema_20
        "max_rsi_low":          [40, 45, 50],
        "min_ema50_slope_pct":  [0.5, 2.0],
        "min_value_cr":         [5],
        "nifty_regime":         [None, "bull"],
        "target_pct":           [6, 10],
        "stop_pct":             [3, 5],
        "max_hold_days":        [10, 25],
    }
    keys = list(grid.keys())
    combos = list(product(*grid.values()))
    print(f"[i] Grid: {len(combos)} configs")

    results = []
    for idx, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        cfg = PullbackConfig(
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
        if idx % 200 == 0:
            best = max((r.get("avg_daily_net_pct", -99) for r in results if r.get("n", 0) > 0), default=-99)
            print(f"  [{idx}/{len(combos)}] best so far: {best:.4f}%/day")

    res = pd.DataFrame(results)
    res = res[res["n"] > 0].sort_values("avg_daily_net_pct", ascending=False)
    out_csv = Path(f"{args.out_prefix}_ranking.csv")
    res.to_csv(out_csv, index=False)
    print(f"\n[+] Wrote: {out_csv}")

    print(f"\n=== TOP 15 ===")
    cols = ["n", "win_rate_pct", "avg_net_per_trade_pct", "avg_hold_days", "profit_factor", "avg_daily_net_pct", "label"]
    print(res[cols].head(15).to_string(index=False))

    print(f"\n=== Distribution ===")
    print(res["avg_daily_net_pct"].describe())
    print(f"\nConfigs with >0 daily:   {(res['avg_daily_net_pct'] > 0).sum()} / {len(res)}")
    print(f"Configs >= 0.1%/day:     {(res['avg_daily_net_pct'] >= 0.1).sum()}")
    print(f"Configs >= 0.3%/day:     {(res['avg_daily_net_pct'] >= 0.3).sum()}  (Stage 1 gate)")
    print(f"Configs >= 0.5%/day:     {(res['avg_daily_net_pct'] >= 0.5).sum()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

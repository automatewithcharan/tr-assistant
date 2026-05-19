"""Oversold quality-bounce scanner — catch large-caps that drop sharply then bounce.

Signal philosophy:
  Quality large-cap stocks occasionally drop 5-10% in 2-3 days on no fundamental
  news (sector rotation, panic, technical breakdown). They tend to recover at
  least half the drop within 5-15 days. This is CONTRARIAN — opposite direction
  from stealth (which buys strength). Adds true diversification.

Signal components:
  1. Quality filter:        Must be in top liquidity tier (Nifty 100 / ₹50cr+/day)
  2. Long-term uptrend:     close > ema_200 (preserves "quality" assumption)
  3. Sharp drop:            5-day return ≤ -5% (or -6%, -8%)
  4. Drop is recent:        biggest drop in the 5-day window happened in last 3 days
  5. NOT a structural break: close still above ema_50 OR within X% of it
  6. Bounce confirmation:   today's close > yesterday's close AND today is green
  7. Not against trend:     ema_50 still rising or flat (no crashing knife)
  8. Liquidity gate

Entry: next day's open after bounce day.
Exit:  +target / -stop / max_hold (typically shorter than stealth, ~7-15 days)
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


def fee() -> float:
    return 0.30  # ~0.15% one-way × 2 delivery


@dataclass(frozen=True)
class OversoldConfig:
    family: str = "oversold_bounce"
    target_pct: float = 5.0
    stop_pct: float = 3.0
    max_hold_days: int = 10
    params: dict = field(default_factory=dict)

    def label(self) -> str:
        p = self.params
        return (
            f"oversold|drop_5d<={p.get('max_5d_drop')}%|"
            f"abovema50_within<={p.get('max_ema50_dist')}%|"
            f"min_value={p.get('min_value_cr')}cr|"
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
    df = pd.concat(parts, ignore_index=True)

    nfp = DAY_CACHE / "NIFTY 50.csv"
    if nfp.exists():
        n = pd.read_csv(nfp)
        n["date"] = pd.to_datetime(n["datetime"], utc=True).dt.tz_convert(None).dt.normalize()
        nifty = n[["date", "close"]].rename(columns={"close": "nifty_close"}).sort_values("date")
    else:
        nifty = pd.DataFrame(columns=["date", "nifty_close"])
    return df, nifty


def add_features(df: pd.DataFrame, nifty: pd.DataFrame) -> pd.DataFrame:
    work = df.sort_values(["symbol", "date"]).reset_index(drop=True).copy()

    def per_sym(g):
        g = g.sort_values("date").copy()
        g["ema_50"] = g["close"].ewm(span=50, adjust=False).mean()
        g["ema_200"] = g["close"].ewm(span=200, adjust=False).mean()
        # 5-day return
        g["ret_5d"] = g["close"].pct_change(5) * 100.0
        # 1-day return (for bounce day check)
        g["ret_1d"] = g["close"].pct_change(1) * 100.0
        # Distance from ema_50 (negative = below)
        g["ema50_dist_pct"] = (g["close"] - g["ema_50"]) / g["ema_50"] * 100.0
        # ema_50 slope (positive = rising)
        g["ema_50_slope_pct"] = (g["ema_50"] - g["ema_50"].shift(10)) / g["ema_50"].shift(10) * 100.0
        # Bounce: today green AND close > yesterday's close
        g["bounce"] = ((g["close"] > g["close"].shift(1)) & (g["close"] > g["open"])).astype(int)
        # Quality / trend: above 200-EMA
        g["above_ema200"] = (g["close"] > g["ema_200"]).astype(int)
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


def generate_signals(features: pd.DataFrame, cfg: OversoldConfig) -> pd.Series:
    p = cfg.params
    cond = (
        (features["avg_value20_cr"].fillna(0) >= float(p.get("min_value_cr", 25.0)))
        & (features["close"] >= float(p.get("min_price", 100)))
        & (features["above_ema200"] == 1)  # quality stock in long-term uptrend
        & (features["ret_5d"] <= -float(p.get("max_5d_drop", 5.0)))  # sharp drop
        & (features["ema50_dist_pct"] >= -float(p.get("max_ema50_dist", 5.0)))  # still near 50-EMA
        & (features["ema_50_slope_pct"] >= -float(p.get("max_ema50_decline", 1.0)))  # not crashing
        & (features["bounce"] == 1)  # today's bounce confirmation
    )
    if p.get("nifty_regime") == "bull":
        cond = cond & (features["nifty_regime_bull"] == 1)
    return cond.fillna(False)


def simulate_trades(sdf: pd.DataFrame, sig: pd.Series, cfg: OversoldConfig) -> list[dict]:
    sdf = sdf.sort_values("date")
    s = sig.reindex(sdf.index).fillna(False).to_numpy(dtype=bool)
    sdf = sdf.reset_index(drop=True)
    signal_mask = pd.Series(s, index=sdf.index)
    symbol = str(sdf["symbol"].iloc[0]) if not sdf.empty else ""
    cost = fee()
    trades, skip = [], -1
    for i in range(len(sdf) - 1):
        if i <= skip or not signal_mask.iloc[i]:
            continue
        entry_row = sdf.iloc[i + 1]
        entry_px = float(entry_row["open"])
        if entry_px <= 0 or np.isnan(entry_px):
            continue
        target = entry_px * (1 + cfg.target_pct / 100)
        stop = entry_px * (1 - cfg.stop_pct / 100)
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
        gross = (exit_px / entry_px - 1) * 100
        net = gross - cost
        trades.append({
            "signal_date": sdf.iloc[i]["date"], "entry_date": entry_row["date"],
            "exit_date": sdf.iloc[actual_exit]["date"], "symbol": symbol,
            "entry_price": round(entry_px, 4), "exit_price": round(exit_px, 4),
            "trade_return_pct": round(net, 4), "exit_reason": reason,
            "hold_days": actual_exit - (i + 1),
        })
        skip = actual_exit
    return trades


def run_config(features: pd.DataFrame, cfg: OversoldConfig) -> pd.DataFrame:
    sig = generate_signals(features, cfg)
    out = []
    for sym, sdf in features.groupby("symbol", sort=False):
        m = sig.reindex(sdf.index).fillna(False)
        if not m.any():
            continue
        out.extend(simulate_trades(sdf, m, cfg))
    return pd.DataFrame(out) if out else pd.DataFrame()


def summarize(trades: pd.DataFrame, n_days: int) -> dict:
    if trades.empty:
        return {"n": 0}
    n = len(trades)
    wins = trades[trades["trade_return_pct"] > 0]
    losses = trades[trades["trade_return_pct"] <= 0]
    gp = wins["trade_return_pct"].sum()
    gl = -losses["trade_return_pct"].sum()
    return {
        "n": n,
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "avg_net_per_trade_pct": round(trades["trade_return_pct"].mean(), 4),
        "avg_hold_days": round(trades["hold_days"].mean(), 2),
        "profit_factor": round(gp / gl if gl > 0 else np.inf, 3),
        "avg_daily_net_pct": round(trades["trade_return_pct"].sum() / n_days, 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=str(RESULTS / "stealth_universe.csv"))
    ap.add_argument("--out-prefix", default=str(RESULTS / "oversold_bounce"))
    args = ap.parse_args()

    print(f"[i] Loading {args.universe}")
    df, nifty = load_features(Path(args.universe))
    feats = add_features(df, nifty)
    n_days = feats["date"].nunique()
    print(f"[i] {df['symbol'].nunique()} symbols, {n_days} trading days")

    grid = {
        "max_5d_drop":       [5, 7, 10],
        "max_ema50_dist":    [3, 5, 8],
        "max_ema50_decline": [0.5, 1.5],
        "min_value_cr":      [25, 50],
        "min_price":         [100],
        "nifty_regime":      [None, "bull"],
        "target_pct":        [3, 5, 8],
        "stop_pct":          [2, 3, 4],
        "max_hold_days":     [7, 10, 15],
    }
    keys = list(grid.keys())
    combos = list(product(*grid.values()))
    print(f"[i] Grid: {len(combos)} configs")

    results = []
    for idx, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        cfg = OversoldConfig(
            target_pct=params["target_pct"], stop_pct=params["stop_pct"],
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
            print(f"  [{idx}/{len(combos)}] best: {best:+.4f}%/day")

    res = pd.DataFrame(results)
    res = res[res["n"] > 0].sort_values("avg_daily_net_pct", ascending=False)
    out_csv = Path(f"{args.out_prefix}_ranking.csv")
    res.to_csv(out_csv, index=False)
    print(f"\n[+] Wrote: {out_csv}")

    print(f"\n=== TOP 15 ===")
    cols = ["n", "win_rate_pct", "avg_net_per_trade_pct", "avg_hold_days", "profit_factor", "avg_daily_net_pct", "label"]
    print(res[cols].head(15).to_string(index=False))

    print(f"\nConfigs with >0 daily:   {(res['avg_daily_net_pct'] > 0).sum()} / {len(res)}")
    print(f"Configs >= 0.1%/day:     {(res['avg_daily_net_pct'] >= 0.1).sum()}")
    print(f"Configs >= 0.3%/day:     {(res['avg_daily_net_pct'] >= 0.3).sum()}  (Stage 1 gate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

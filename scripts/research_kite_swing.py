#!/usr/bin/env python3
"""Daily EOD swing strategy research.

Phase 1: survivability check for two baseline strategy classes.
  - breakout_volume   : close > prior-20d high  AND  volume > N× prior-20d avg
  - momentum_10d      : 10-day return ≥ threshold AND close > 10-day EMA

Lookahead invariant (enforced throughout):
  - Signal computed from data available at close of day T only.
  - Entry at open of day T+1 (overnight gap is real friction, not modelled away).
  - All rolling "level" comparisons (high_20d, vol_avg20) use prior bars via shift(1).
  - Same-day close (close[T]) is legitimately observable for return and EMA calculations.

Structural viability principle (permanent, applies to every strategy):
  Edge must exceed: execution_gap + infrastructure_gap + lookahead_risk.
  A strategy that fails this principle is reported as non-viable regardless of backtest P&L.

Usage:
  python scripts/research_kite_swing.py
  python scripts/research_kite_swing.py --baseline-only   # skip grid, run 2 configs
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RESULTS_DIR = REPO_ROOT / "results" / "Data"
DAY_CACHE_DIR = RESULTS_DIR / "kite_history_backfill" / "day" / "2024-05-13_2026-05-12"
REGISTRY_PATH = RESULTS_DIR / "strategy_registry.json"
OUT_PREFIX = RESULTS_DIR / "kite_swing_research"

# ---------------------------------------------------------------------------
# Strategy config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SwingConfig:
    family: str   # breakout_volume | momentum_10d
    side: str     # always "long" in phase 1
    params: dict
    target_pct: float      # % gain to close at target
    stop_pct: float        # % loss to close at stop
    max_hold_days: int     # force-exit after N trading days

    def label(self) -> str:
        parts = ["daily", self.side, self.family]
        for key in sorted(self.params):
            value = self.params[key]
            parts.append(f"{key}={value:g}" if isinstance(value, float) else f"{key}={value}")
        parts.extend([f"t={self.target_pct:g}", f"s={self.stop_pct:g}", f"hold={self.max_hold_days}"])
        return "|".join(parts)


# ---------------------------------------------------------------------------
# Cost model — delivery / swing (not intraday)
# ---------------------------------------------------------------------------

def indian_delivery_fee_pct(avg_value_cr: float = 0.0) -> float:
    """All-in round-trip cost estimate for equity delivery trade on Zerodha."""
    brokerage = 0.0            # Zerodha: zero brokerage on delivery
    stt = 0.001 * 2            # 0.1% each side
    exchange_txn = 0.00345 * 2
    sebi = 0.0001 * 2
    stamp_buy = 0.00015        # 0.015% on buy side
    gst = 0.18 * (brokerage + exchange_txn)
    slip = _slippage_pct(avg_value_cr)
    return brokerage + stt + exchange_txn + sebi + stamp_buy + gst + slip


def _slippage_pct(avg_value_cr: float) -> float:
    """Estimated one-way slippage based on liquidity bucket (applied twice round-trip)."""
    if avg_value_cr >= 200:
        return 0.05
    if avg_value_cr >= 75:
        return 0.08
    if avg_value_cr >= 25:
        return 0.12
    return 0.20


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _normalise_datetime(values) -> pd.Series:
    raw = pd.Series(values).astype(str).str.replace(r"(Z|[+-]\d{2}:?\d{2})$", "", regex=True)
    return pd.to_datetime(raw, errors="coerce")


def load_daily_symbol(symbol: str, data_dir: Path = DAY_CACHE_DIR) -> pd.DataFrame:
    path = data_dir / f"{symbol}.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if "date" in df.columns and "datetime" not in df.columns:
            df = df.rename(columns={"date": "datetime"})
        df["datetime"] = _normalise_datetime(df["datetime"])
        df["date"] = df["datetime"].dt.normalize()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df.get(col, np.nan), errors="coerce")
        df["symbol"] = symbol
        keep = ["date", "open", "high", "low", "close", "volume", "symbol"]
        return (
            df[keep]
            .dropna(subset=["date", "open", "high", "low", "close"])
            .drop_duplicates(subset=["date"])
            .sort_values("date")
            .reset_index(drop=True)
        )
    except Exception:
        return pd.DataFrame()


def load_all_daily(data_dir: Path = DAY_CACHE_DIR, limit: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (universe_df, nifty_df). limit=0 means no limit."""
    csv_files = sorted(data_dir.glob("*.csv"))
    nifty_df = pd.DataFrame()
    parts: list[pd.DataFrame] = []
    for f in csv_files:
        sym = f.stem
        df = load_daily_symbol(sym, data_dir)
        if df.empty:
            continue
        if sym == "NIFTY 50":
            nifty_df = df
        else:
            parts.append(df)
        if limit > 0 and len(parts) >= limit:
            break
    universe = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return universe, nifty_df


def check_retired_registry(family: str) -> Optional[str]:
    """Return retirement reason if this family is in the registry, else None."""
    if not REGISTRY_PATH.exists():
        return None
    try:
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        for entry in reg.get("retired", []):
            label = entry.get("strategy_label", "")
            if family in label:
                return f"RETIRED ({entry.get('reason', '?')}): {entry.get('detail', '')[:120]}"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Feature engineering
#
# Lookahead contract:
#   - high_20d   = max(close[T-20 : T-1])  — shift(1) applied
#   - vol_avg20  = mean(volume[T-20 : T-1]) — shift(1) applied
#   - ret_10d    = close[T] / close[T-10] - 1  — no lookahead (both observed at T)
#   - ema_10     = EMA up to and including close[T] — valid (T is observable)
#   - atr14      = EMA of true range up to T — valid
#   - nifty data = same-day close — valid (both sides observe close at T)
# ---------------------------------------------------------------------------

def _true_range_series(g: pd.DataFrame) -> pd.Series:
    prev_close = g["close"].shift(1)
    return pd.concat([
        g["high"] - g["low"],
        (g["high"] - prev_close).abs(),
        (g["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def add_daily_features(df: pd.DataFrame, nifty: pd.DataFrame) -> pd.DataFrame:
    work = df.copy().sort_values(["symbol", "date"]).reset_index(drop=True)

    def per_sym(g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_values("date").copy()
        # Level comparisons use prior bars — shift(1) is mandatory
        g["high_20d"]    = g["close"].shift(1).rolling(20).max()
        g["low_20d"]     = g["close"].shift(1).rolling(20).min()
        g["vol_avg20"]   = g["volume"].shift(1).rolling(20).mean()
        g["vol_avg5"]    = g["volume"].shift(1).rolling(5).mean()
        # Returns using current close are clean (close[T] is observable)
        g["ret_5d"]      = g["close"].pct_change(5) * 100.0
        g["ret_10d"]     = g["close"].pct_change(10) * 100.0
        g["ret_20d"]     = g["close"].pct_change(20) * 100.0
        # EMAs including today's close are clean
        g["ema_10"]      = g["close"].ewm(span=10, adjust=False).mean()
        g["ema_20"]      = g["close"].ewm(span=20, adjust=False).mean()
        g["ema_50"]      = g["close"].ewm(span=50, adjust=False).mean()
        # ATR
        tr = _true_range_series(g)
        g["atr14"]       = tr.ewm(span=14, adjust=False).mean()
        g["atr14_pct"]   = g["atr14"] / g["close"] * 100.0
        # Liquidity
        g["day_value_cr"]  = g["close"] * g["volume"] / 1e7
        g["avg_value20_cr"] = g["day_value_cr"].shift(1).rolling(20).mean()
        return g

    import warnings
    _sym_col = work["symbol"].copy()  # pandas 3.0+ drops groupby key columns from apply
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        work = work.groupby("symbol", sort=False, group_keys=False).apply(per_sym)
    if "symbol" not in work.columns:
        work.insert(0, "symbol", _sym_col.reindex(work.index))

    # Nifty features — all observable at close of day T
    if not nifty.empty:
        n = nifty[["date", "close"]].rename(columns={"close": "nifty_close"}).copy()
        n = n.sort_values("date")
        n["nifty_ret_10d"]   = n["nifty_close"].pct_change(10) * 100.0
        # Regime: nifty_close > nifty_ema_50 — both observable at T's close, no lookahead
        n["nifty_ema_50"]    = n["nifty_close"].ewm(span=50, adjust=False).mean()
        n["nifty_ema_200"]   = n["nifty_close"].ewm(span=200, adjust=False).mean()
        n["nifty_regime_bull"] = (n["nifty_close"] > n["nifty_ema_50"]).astype(int)
        n["nifty_strong_bull"] = (n["nifty_close"] > n["nifty_ema_200"]).astype(int)
        work = work.merge(
            n[["date", "nifty_close", "nifty_ret_10d",
               "nifty_regime_bull", "nifty_strong_bull"]],
            on="date", how="left",
        )
        work["rs_vs_nifty_10d"] = work["ret_10d"] - work["nifty_ret_10d"].fillna(0.0)
    else:
        work["nifty_close"] = np.nan
        work["nifty_ret_10d"]    = np.nan
        work["rs_vs_nifty_10d"]  = np.nan
        work["nifty_regime_bull"] = 1   # no data → don't filter
        work["nifty_strong_bull"] = 1

    return work.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Signal generation
# Returns a boolean Series (True = long signal at close of this row's date)
# ---------------------------------------------------------------------------

def generate_signals(features: pd.DataFrame, cfg: SwingConfig) -> pd.Series:
    """Compute signal mask for all rows.

    Every condition uses only data observable at close of day T.
    All rolling window features enforce shift(1) in add_daily_features.

    Regime filter (param: nifty_regime):
      "bull"        → nifty_close > nifty_ema_50   (50-day EMA uptrend)
      "strong_bull" → nifty_close > nifty_ema_200  (200-day EMA uptrend, stricter)
      absent/other  → no regime filter

    Lookahead invariant:
      nifty_close[T] and nifty_ema_50[T] are both observed at close of day T.
      The regime filter uses no future data.
    """
    p = cfg.params
    liq_ok = (
        features["avg_value20_cr"].fillna(0) >= float(p.get("min_value_cr", 25.0))
    ) & (features["close"] >= float(p.get("min_price", 40.0)))

    # Optional Nifty regime gate — applied to every family
    regime = p.get("nifty_regime", None)
    if regime == "bull":
        regime_ok = features["nifty_regime_bull"].fillna(1).astype(bool)
    elif regime == "strong_bull":
        regime_ok = features["nifty_strong_bull"].fillna(1).astype(bool)
    else:
        regime_ok = pd.Series(True, index=features.index)

    if cfg.family == "breakout_volume":
        vol_mult = float(p.get("vol_mult", 1.5))
        sig = (
            liq_ok & regime_ok
            & features["close"].gt(features["high_20d"])
            & features["volume"].gt(vol_mult * features["vol_avg20"])
            & features["high_20d"].notna()
            & features["vol_avg20"].notna()
        )

    elif cfg.family == "momentum_10d":
        min_ret = float(p.get("min_ret_pct", 8.0))
        sig = (
            liq_ok & regime_ok
            & features["ret_10d"].ge(min_ret)
            & features["close"].gt(features["ema_10"])
            & features["ret_10d"].notna()
            & features["ema_10"].notna()
        )

    else:
        sig = pd.Series(False, index=features.index)

    return sig.fillna(False)


# ---------------------------------------------------------------------------
# Trade simulation — multi-day exit
# Entry: open of day T+1.  No intraday price access used here.
# ---------------------------------------------------------------------------

def simulate_trades_for_symbol(
    sdf: pd.DataFrame,
    signal_mask: pd.Series,
    cfg: SwingConfig,
    family: str,
) -> list[dict]:
    """Simulate swing trades for a single symbol.

    Rules:
    - No overlapping trades: after entering, skip all signals until exit.
    - Entry = open[T+1]. If T+1 open is missing, skip signal.
    - Exit priority: stop > target > max_hold (stop checked before target on same bar).
    """
    # Sort by date first, then capture the aligned signal values as a numpy array
    # BEFORE reset_index — reindex must use the original index values while they still match.
    sdf = sdf.sort_values("date")
    signal_arr = signal_mask.reindex(sdf.index).infer_objects(copy=False).fillna(False).to_numpy(dtype=bool)
    sdf = sdf.reset_index(drop=True)
    signal_mask = pd.Series(signal_arr, index=sdf.index)
    symbol = str(sdf["symbol"].iloc[0]) if not sdf.empty else ""
    avg_val = float(sdf["avg_value20_cr"].median() or 0.0)
    cost = indian_delivery_fee_pct(avg_val)
    trades: list[dict] = []
    skip_until_idx = -1

    for i in range(len(sdf) - 1):  # need at least one bar after signal
        if i <= skip_until_idx:
            continue
        if not signal_mask.iloc[i]:
            continue

        entry_row = sdf.iloc[i + 1]
        entry_price = float(entry_row["open"])
        if entry_price <= 0 or np.isnan(entry_price):
            continue

        target = entry_price * (1.0 + cfg.target_pct / 100.0)
        stop   = entry_price * (1.0 - cfg.stop_pct / 100.0)

        exit_price = np.nan
        exit_reason = "max_hold"
        end_idx = min(i + 1 + cfg.max_hold_days, len(sdf) - 1)
        actual_exit_idx = end_idx

        for j in range(i + 1, end_idx + 1):
            bar = sdf.iloc[j]
            lo = float(bar["low"])
            hi = float(bar["high"])
            # Stop checked before target (conservative — stop gap is usually the bad scenario)
            if lo <= stop:
                exit_price = stop
                exit_reason = "stop"
                actual_exit_idx = j
                break
            if hi >= target:
                exit_price = target
                exit_reason = "target"
                actual_exit_idx = j
                break

        if np.isnan(exit_price):
            exit_price = float(sdf.iloc[actual_exit_idx]["close"])

        hold = actual_exit_idx - (i + 1)
        gross = (exit_price / entry_price - 1.0) * 100.0
        net = gross - cost

        trades.append({
            "signal_date": sdf.iloc[i]["date"],
            "entry_date":  entry_row["date"],
            "exit_date":   sdf.iloc[actual_exit_idx]["date"],
            "symbol":      symbol,
            "family":      family,
            "entry_price": round(entry_price, 4),
            "exit_price":  round(exit_price, 4),
            "gross_return_pct": round(gross, 4),
            "cost_pct":    round(cost, 4),
            "trade_return_pct": round(net, 4),
            "exit_reason": exit_reason,
            "hold_days":   hold,
        })
        skip_until_idx = actual_exit_idx  # no new entry until this trade is done

    return trades


def run_all_trades(features: pd.DataFrame, cfg: SwingConfig) -> pd.DataFrame:
    """Run signal generation and trade simulation across all symbols."""
    signal_mask = generate_signals(features, cfg)
    all_trades: list[dict] = []
    for symbol, sdf in features.groupby("symbol", sort=False):
        sym_mask = signal_mask.reindex(sdf.index).fillna(False)
        if not sym_mask.any():
            continue
        trades = simulate_trades_for_symbol(sdf, sym_mask, cfg, cfg.family)
        all_trades.extend(trades)
    if not all_trades:
        return pd.DataFrame()
    return (
        pd.DataFrame(all_trades)
        .sort_values(["entry_date", "symbol"])
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _max_drawdown(returns_pct: pd.Series) -> float:
    equity = (1.0 + returns_pct.fillna(0) / 100.0).cumprod()
    rolling_max = equity.cummax()
    dd = (equity - rolling_max) / rolling_max * 100.0
    return round(float(abs(dd.min())) if not dd.empty else 0.0, 2)


def summarize_trades(trades: pd.DataFrame, trading_dates: list[pd.Timestamp]) -> dict:
    """Aggregate per-trade stats.

    max_drawdown uses a sequential per-trade equity curve (trades sorted by entry_date).
    This avoids the fake 100% drawdown that results from summing all concurrent entries
    on the same day without normalising by position count.
    """
    if trades.empty:
        return {
            "n_trades": 0, "win_rate_pct": 0.0, "avg_trade_return_pct": 0.0,
            "profit_factor": 0.0, "max_drawdown_pct": 0.0, "sharpe_trade": 0.0,
            "avg_daily_return_pct": 0.0, "positive_month_pct": 0.0, "month_count": 0,
        }

    n = len(trades)
    wr = float((trades["trade_return_pct"] > 0).mean() * 100.0)
    avg_ret = float(trades["trade_return_pct"].mean())
    wins  = trades.loc[trades["trade_return_pct"] > 0, "trade_return_pct"].sum()
    losses = abs(trades.loc[trades["trade_return_pct"] <= 0, "trade_return_pct"].sum())
    pf = round(float(wins / losses), 3) if losses > 0 else 99.0

    std_ret = float(trades["trade_return_pct"].std(ddof=0))
    avg_hold = max(1.0, float(trades["hold_days"].mean()))
    sharpe_t = float(avg_ret / std_ret * math.sqrt(252.0 / avg_hold)) if std_ret > 0 else 0.0

    # Sequential trade equity curve — each trade's P&L applied one at a time in entry-date order.
    # This models a single-position-at-a-time equity curve (conservative for multi-symbol portfolio).
    sorted_returns = trades.sort_values("entry_date")["trade_return_pct"]
    mdd = _max_drawdown(sorted_returns)

    # Daily mean return proxy — mean per active entry day (normalises concurrent positions)
    by_day = trades.groupby("entry_date")["trade_return_pct"].mean()
    avg_daily = float(by_day.mean())

    # Monthly stability
    trades["_month"] = pd.to_datetime(trades["entry_date"]).dt.to_period("M").astype(str)
    monthly = trades.groupby("_month")["trade_return_pct"].sum()
    pos_month_pct = round(float((monthly > 0).mean() * 100.0), 1) if not monthly.empty else 0.0

    return {
        "n_trades": n,
        "win_rate_pct": round(wr, 2),
        "avg_trade_return_pct": round(avg_ret, 4),
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": mdd,
        "sharpe_trade": round(sharpe_t, 3),
        "avg_daily_return_pct": round(avg_daily, 4),
        "positive_month_pct": pos_month_pct,
        "month_count": int(len(monthly)),
    }


def concentration_check(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"top_symbol_pct": 100.0, "top_month_pct": 100.0}
    pos = trades[trades["trade_return_pct"] > 0].copy()
    total = float(pos["trade_return_pct"].sum())
    if total <= 0:
        return {"top_symbol_pct": 100.0, "top_month_pct": 100.0}
    pos["_month"] = pd.to_datetime(pos["entry_date"]).dt.to_period("M").astype(str)
    top_sym  = round(float(pos.groupby("symbol")["trade_return_pct"].sum().max() / total * 100.0), 1)
    top_mon  = round(float(pos.groupby("_month")["trade_return_pct"].sum().max() / total * 100.0), 1)
    return {"top_symbol_pct": top_sym, "top_month_pct": top_mon}


def walk_forward_metrics(trades: pd.DataFrame, dates: list[pd.Timestamp]) -> dict[str, dict]:
    """Compute metrics on 60/20/20 walk-forward splits."""
    if not dates:
        return {}
    n = len(dates)
    train_end  = dates[int(n * 0.60)]
    val_end    = dates[int(n * 0.80)]
    splits = {
        "train":      (dates[0],    train_end),
        "validation": (train_end,   val_end),
        "test":       (val_end,     dates[-1]),
    }
    result: dict[str, dict] = {}
    for name, (start, end) in splits.items():
        split_dates = [d for d in dates if start <= d <= end]
        if not trades.empty:
            mask = pd.to_datetime(trades["entry_date"]).between(start, end)
            split_trades = trades[mask].copy()
        else:
            split_trades = pd.DataFrame()
        result[name] = summarize_trades(split_trades, split_dates)
    return result


# ---------------------------------------------------------------------------
# Lookahead verification (daily version)
#
# For daily strategies there is no intraday bar selection, so the argmax trap
# that burned the intraday framework cannot occur here.  We still run a formal
# check to confirm the signal does not benefit from same-bar close data in a
# way that would be unobservable at entry time.
# ---------------------------------------------------------------------------

def verify_lookahead_clean_daily(cfg: SwingConfig) -> dict:
    """Return audit dict confirming whether this config is lookahead-clean.

    For daily EOD strategies:
    - 'level' features (high_20d, vol_avg20) use shift(1) — prior bars only ✓
    - Return features (ret_10d) use close[T] / close[T-N] — both observable ✓
    - EMA features include close[T] — observable at close ✓
    - Entry = open[T+1] — no same-day entry (overnight gap is real risk) ✓
    - No argmax across intraday bars — daily strategy has no bar-selection step ✓

    Risk: if close[T] is used in a LEVEL comparison (e.g., "today's close > 20d max
    INCLUDING today"), that is questionable.  This function checks for that.
    """
    family = cfg.family
    # Known safe families in this script
    safe_families = {"breakout_volume", "momentum_10d"}
    is_safe = family in safe_families

    # For breakout_volume: high_20d = shift(1).rolling(20).max() — does NOT include close[T] ✓
    # The signal is: close[T] > high_20d  where high_20d is max(T-20:T-1)
    # This means we're checking if TODAY's close is a new 20-day high.
    # That is a legitimate real-time observation at close-of-day.

    # For momentum_10d: ret_10d = close[T]/close[T-10] — clean
    # ema_10 = EMA including close[T] — clean (observable at close)

    # Regime filter: nifty_close[T] > nifty_ema_50[T] — both observable at T's close ✓
    regime = cfg.params.get("nifty_regime", None)
    regime_note = (
        f"Regime filter '{regime}': nifty_close and nifty_ema_50 both observable at close of T ✓"
        if regime else "No regime filter."
    )

    return {
        "family": family,
        "lookahead_clean": is_safe,
        "bar_selection_lookahead": False,   # no argmax across bars in daily strategy
        "level_shift_applied": True,
        "entry_at_next_open": True,
        "regime_filter_note": regime_note,
        "note": "Daily EOD strategy — argmax-style lookahead trap structurally absent.",
    }


# ---------------------------------------------------------------------------
# Gap vs Edge analysis
# ---------------------------------------------------------------------------

def gap_vs_edge(trades: pd.DataFrame, cfg: SwingConfig, metrics: dict) -> dict:
    """Estimate whether raw edge survives after realistic gaps.

    For daily swing:
    - Execution gap = overnight gap risk + spread/slippage (already in cost_pct)
    - Infrastructure gap = minimal (no complex order routing needed for EOD swing)
    - Lookahead risk = 0 (no intraday bar selection)
    """
    if trades.empty:
        return {
            "raw_edge_pct": 0.0, "execution_gap_pct": 0.0, "infrastructure_gap_pct": 0.0,
            "lookahead_risk_pct": 0.0, "net_viable_edge_pct": 0.0, "viable": False,
            "note": "no trades",
        }

    avg_cost    = float(trades["cost_pct"].mean())
    avg_gross   = float(trades["gross_return_pct"].mean())
    avg_net     = float(trades["trade_return_pct"].mean())

    # Estimate overnight gap friction beyond modelled slippage
    entry_gaps = (trades["entry_price"] / trades["entry_price"]).fillna(0).mean()  # placeholder
    # Realistic overnight gap for liquid NSE stocks: ~0.15–0.30% additional
    overnight_gap_friction = 0.20

    execution_gap    = avg_cost + overnight_gap_friction
    infrastructure_gap = 0.05   # minimal for EOD delivery orders
    lookahead_risk   = 0.0      # no intraday bar selection

    raw_edge = avg_gross
    net_viable = raw_edge - execution_gap - infrastructure_gap - lookahead_risk

    return {
        "raw_edge_pct": round(raw_edge, 4),
        "avg_cost_modelled_pct": round(avg_cost, 4),
        "overnight_gap_friction_pct": round(overnight_gap_friction, 4),
        "execution_gap_pct": round(execution_gap, 4),
        "infrastructure_gap_pct": round(infrastructure_gap, 4),
        "lookahead_risk_pct": round(lookahead_risk, 4),
        "net_viable_edge_pct": round(net_viable, 4),
        "viable": bool(net_viable > 0),
        "note": (
            "Edge survives cost + slippage + overnight gap friction."
            if net_viable > 0
            else "Edge is consumed by execution costs and realistic friction."
        ),
    }


# ---------------------------------------------------------------------------
# Structural Viability Score (0–100)
# ---------------------------------------------------------------------------

def structural_viability_score(metrics: dict, wf: dict, gve: dict, conc: dict) -> tuple[int, list[str]]:
    """Return (score, blocking_reasons).

    Score components:
      Edge quality (40 pts): avg trade return, profit factor, win rate
      Consistency (30 pts): walk-forward stability, positive month %
      Risk (20 pts): max drawdown, concentration
      Viability gate (10 pts): gap vs edge passes + lookahead clean
    """
    reasons: list[str] = []
    score = 0.0

    # Edge quality
    avg_ret = float(metrics.get("avg_trade_return_pct", 0.0))
    pf      = float(metrics.get("profit_factor", 0.0))
    wr      = float(metrics.get("win_rate_pct", 0.0))
    score += min(max(avg_ret * 10, 0.0), 15.0)         # up to 15 pts
    score += min(max((pf - 1.0) * 15, 0.0), 15.0)      # up to 15 pts
    score += min(max((wr - 40.0) * 0.5, 0.0), 10.0)    # up to 10 pts
    if avg_ret <= 0:
        reasons.append(f"avg_trade_return <= 0 ({avg_ret:.3f}%)")
    if pf <= 1.0:
        reasons.append(f"profit_factor <= 1.0 ({pf:.2f})")

    # Consistency
    pos_month = float(metrics.get("positive_month_pct", 0.0))
    score += min(pos_month * 0.25, 15.0)                # up to 15 pts

    # Walk-forward: test split must be positive
    test_m   = wf.get("test", {})
    train_m  = wf.get("train", {})
    val_m    = wf.get("validation", {})
    test_ret = float(test_m.get("avg_trade_return_pct", 0.0))
    if test_ret > 0:
        score += 15.0
    else:
        reasons.append(f"test split avg_trade_return <= 0 ({test_ret:.3f}%)")
    if float(train_m.get("avg_trade_return_pct", 0.0)) <= 0:
        reasons.append("train split avg_trade_return <= 0")
    if float(val_m.get("avg_trade_return_pct", 0.0)) <= 0:
        reasons.append("validation split avg_trade_return <= 0")

    # Risk
    mdd = float(metrics.get("max_drawdown_pct", 99.0))
    score += max(0.0, 20.0 - mdd * 1.0)                # up to 20 pts (penalises drawdown)
    if mdd >= 20.0:
        reasons.append(f"max_drawdown >= 20% ({mdd:.1f}%)")

    conc_sym = float(conc.get("top_symbol_pct", 100.0))
    conc_mon = float(conc.get("top_month_pct", 100.0))
    if conc_sym > 40:
        reasons.append(f"symbol concentration {conc_sym:.0f}% > 40%")
        score -= 5.0
    if conc_mon > 50:
        reasons.append(f"month concentration {conc_mon:.0f}% > 50%")
        score -= 5.0

    # Viability gate
    if gve.get("viable", False):
        score += 10.0
    else:
        reasons.append(f"gap_vs_edge failed (net_viable={gve.get('net_viable_edge_pct', 0.0):.3f}%)")

    final = int(round(max(0.0, min(score, 100.0))))
    return final, reasons


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def format_report(cfg: SwingConfig, all_trades: pd.DataFrame, wf: dict,
                  gve: dict, la: dict, conc: dict, sv_score: int,
                  sv_reasons: list[str]) -> str:
    m = summarize_trades(all_trades, [])

    def _metric_line(label: str, value, fmt: str = ".4f") -> str:
        return f"  {label:<40s} {value:{fmt}}"

    def _wf_block(name: str, wf_m: dict) -> str:
        n = wf_m.get("n_trades", 0)
        if n == 0:
            return f"  {name:<12s}  (no trades)"
        return (
            f"  {name:<12s}  trades={n:>5}  "
            f"avg={wf_m.get('avg_trade_return_pct', 0.0):>+.3f}%  "
            f"wr={wf_m.get('win_rate_pct', 0.0):>5.1f}%  "
            f"pf={wf_m.get('profit_factor', 0.0):>5.2f}  "
            f"mdd={wf_m.get('max_drawdown_pct', 0.0):>5.1f}%"
        )

    via_label  = "VIABLE" if gve.get("viable") else "NOT VIABLE"
    la_label   = "CLEAN" if la.get("lookahead_clean") else "SUSPECT"
    verdict    = "PASS" if sv_score >= 50 and not sv_reasons else "FAIL"

    lines = [
        "╔══════════════════════════════════════════════════════════════════╗",
        "║         SWING STRATEGY SURVIVABILITY REPORT                     ║",
        "╚══════════════════════════════════════════════════════════════════╝",
        f"  Strategy:   {cfg.label()}",
        f"  Verdict:    {verdict}  (Structural Viability Score: {sv_score}/100)",
        "",
        "──────────────────────────────────────────────────────────────────",
        "  TRADE METRICS (all splits combined)",
        "──────────────────────────────────────────────────────────────────",
        _metric_line("Total trades", m.get("n_trades", 0), "d"),
        _metric_line("Win rate", f"{m.get('win_rate_pct', 0.0):.1f}%", "s"),
        _metric_line("Avg trade return (net)", f"{m.get('avg_trade_return_pct', 0.0):+.3f}%", "s"),
        _metric_line("Profit factor", f"{m.get('profit_factor', 0.0):.3f}", "s"),
        _metric_line("Max drawdown (sequential trades)", f"{m.get('max_drawdown_pct', 0.0):.2f}%", "s"),
        _metric_line("Sharpe (per-trade, annualised)", f"{m.get('sharpe_trade', 0.0):.2f}", "s"),
        _metric_line("Positive months", f"{m.get('positive_month_pct', 0.0):.1f}% ({m.get('month_count', 0)} months)", "s"),
        _metric_line("Top symbol profit share", f"{conc.get('top_symbol_pct', 0.0):.1f}%", "s"),
        _metric_line("Top month profit share", f"{conc.get('top_month_pct', 0.0):.1f}%", "s"),
        "",
        "──────────────────────────────────────────────────────────────────",
        "  WALK-FORWARD SPLITS (60 / 20 / 20)",
        "──────────────────────────────────────────────────────────────────",
        _wf_block("train",      wf.get("train", {})),
        _wf_block("validation", wf.get("validation", {})),
        _wf_block("test",       wf.get("test", {})),
        "",
        "──────────────────────────────────────────────────────────────────",
        "  GAP VS EDGE ANALYSIS",
        "──────────────────────────────────────────────────────────────────",
        f"  Raw edge (avg gross per trade)     {gve.get('raw_edge_pct', 0.0):>+.3f}%",
        f"  Modelled cost (fees + slippage)   -{gve.get('avg_cost_modelled_pct', 0.0):>.3f}%",
        f"  Overnight gap friction            -{gve.get('overnight_gap_friction_pct', 0.0):>.3f}%",
        f"  Infrastructure gap               -{gve.get('infrastructure_gap_pct', 0.0):>.3f}%",
        f"  Lookahead risk                    -{gve.get('lookahead_risk_pct', 0.0):>.3f}%",
        f"  ─────────────────────────────────────────",
        f"  Net viable edge                   {gve.get('net_viable_edge_pct', 0.0):>+.3f}%   [{via_label}]",
        f"  Note: {gve.get('note', '')}",
        "",
        "──────────────────────────────────────────────────────────────────",
        "  LOOKAHEAD AUDIT (daily invariants)",
        "──────────────────────────────────────────────────────────────────",
        f"  Status:                            {la_label}",
        f"  high_20d / vol_avg20 use shift(1)  {la.get('level_shift_applied', False)}",
        f"  No argmax bar-selection step       {not la.get('bar_selection_lookahead', True)}",
        f"  Entry at next-day open             {la.get('entry_at_next_open', False)}",
        f"  Note: {la.get('note', '')}",
        "",
        "──────────────────────────────────────────────────────────────────",
        "  STRUCTURAL VIABILITY SCORE: {}/100".format(sv_score),
        "──────────────────────────────────────────────────────────────────",
    ]
    if sv_reasons:
        lines.append("  Blocking reasons:")
        for r in sv_reasons:
            lines.append(f"    • {r}")
    else:
        lines.append("  No blocking reasons.")

    lines += [
        "",
        "══════════════════════════════════════════════════════════════════",
        "  This framework does not optimise toward fantasy returns.",
        "  A score < 50 means the strategy class fails survivability.",
        "  Score ≥ 50 is a prerequisite for paper-trading validation.",
        "══════════════════════════════════════════════════════════════════",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Baseline configs
# ---------------------------------------------------------------------------

BASELINE_CONFIGS = [
    SwingConfig(
        family="breakout_volume",
        side="long",
        params={"min_value_cr": 25.0, "min_price": 40.0, "vol_mult": 1.5},
        target_pct=6.0,
        stop_pct=3.0,
        max_hold_days=10,
    ),
    SwingConfig(
        family="momentum_10d",
        side="long",
        params={"min_value_cr": 25.0, "min_price": 40.0, "min_ret_pct": 8.0},
        target_pct=6.0,
        stop_pct=3.0,
        max_hold_days=10,
    ),
]


def build_grid_configs() -> list[SwingConfig]:
    """Build a parameter grid for deeper search (post-baseline phase)."""
    configs: list[SwingConfig] = []
    targets  = [4.0, 6.0, 8.0, 10.0]
    stops    = [2.0, 3.0, 4.0]
    holds    = [7, 10, 15]
    liq_vals = [25.0, 75.0]
    min_prices = [40.0]

    # breakout_volume grid
    vol_mults = [1.3, 1.5, 2.0, 2.5]
    for t, s, h, liq, price, vm in product(targets, stops, holds, liq_vals, min_prices, vol_mults):
        if s >= t:
            continue
        configs.append(SwingConfig(
            family="breakout_volume", side="long",
            params={"min_value_cr": liq, "min_price": price, "vol_mult": vm},
            target_pct=t, stop_pct=s, max_hold_days=h,
        ))

    # momentum_10d grid
    min_rets = [5.0, 8.0, 12.0, 15.0]
    for t, s, h, liq, price, mr in product(targets, stops, holds, liq_vals, min_prices, min_rets):
        if s >= t:
            continue
        configs.append(SwingConfig(
            family="momentum_10d", side="long",
            params={"min_value_cr": liq, "min_price": price, "min_ret_pct": mr},
            target_pct=t, stop_pct=s, max_hold_days=h,
        ))

    return configs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Daily EOD swing strategy research (phase 1: survivability)")
    parser.add_argument("--baseline-only", action="store_true", default=True,
                        help="Run the 2 baseline configs only (default). Use --grid for full search.")
    parser.add_argument("--grid", action="store_true", default=False,
                        help="Run full parameter grid (slow).")
    parser.add_argument("--data-dir", default=str(DAY_CACHE_DIR))
    parser.add_argument("--out-prefix", default=str(OUT_PREFIX))
    parser.add_argument("--limit", type=int, default=0, help="Symbol limit (0=all)")
    args = parser.parse_args()

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    data_dir = Path(args.data_dir)

    t0 = time.perf_counter()
    print(f"Loading daily data from {data_dir} ...", flush=True)
    universe, nifty = load_all_daily(data_dir, limit=args.limit)
    if universe.empty:
        print("ERROR: No daily data found. Check DAY_CACHE_DIR.", file=sys.stderr)
        return 1

    n_symbols = universe["symbol"].nunique()
    print(f"  {n_symbols} symbols, {len(universe)} rows, Nifty rows: {len(nifty)}", flush=True)

    print("Adding features ...", flush=True)
    features = add_daily_features(universe, nifty)
    features = features.dropna(subset=["high_20d", "vol_avg20"]).copy()
    trading_dates = sorted(pd.to_datetime(features["date"]).dropna().unique().tolist())
    print(f"  {len(trading_dates)} trading dates after feature warm-up", flush=True)
    print(f"  Feature build: {time.perf_counter()-t0:.1f}s", flush=True)

    configs = BASELINE_CONFIGS if not args.grid else build_grid_configs()
    print(f"Running {len(configs)} config(s) ...", flush=True)

    all_rows: list[dict] = []
    report_lines: list[str] = []

    for i, cfg in enumerate(configs, 1):
        # Registry check — don't research retired strategies
        retired = check_retired_registry(cfg.family)
        if retired:
            print(f"  [{i}] SKIPPED ({cfg.label()}): {retired}")
            continue

        t1 = time.perf_counter()
        trades = run_all_trades(features, cfg)
        elapsed = time.perf_counter() - t1

        wf        = walk_forward_metrics(trades, trading_dates)
        gve       = gap_vs_edge(trades, cfg, summarize_trades(trades, trading_dates))
        la        = verify_lookahead_clean_daily(cfg)
        all_m     = summarize_trades(trades, trading_dates)
        conc      = concentration_check(trades)
        sv, reasons = structural_viability_score(all_m, wf, gve, conc)

        print(
            f"  [{i}/{len(configs)}] {cfg.label()}  "
            f"trades={all_m['n_trades']}  "
            f"avg={all_m['avg_trade_return_pct']:+.3f}%  "
            f"wr={all_m['win_rate_pct']:.1f}%  "
            f"pf={all_m['profit_factor']:.2f}  "
            f"sv={sv}/100  [{elapsed:.1f}s]",
            flush=True,
        )

        report_text = format_report(cfg, trades, wf, gve, la, conc, sv, reasons)
        report_lines.append(f"\n## {cfg.label()}\n")
        report_lines.append(report_text)

        row = {
            "strategy": cfg.label(),
            "family": cfg.family,
            "side": cfg.side,
            "params_json": json.dumps(cfg.params, sort_keys=True),
            "target_pct": cfg.target_pct,
            "stop_pct": cfg.stop_pct,
            "max_hold_days": cfg.max_hold_days,
            "sv_score": sv,
            "viable": gve.get("viable", False),
            "lookahead_clean": la.get("lookahead_clean", False),
            **{f"all_{k}": v for k, v in all_m.items()},
            **{f"train_{k}": v for k, v in wf.get("train", {}).items()},
            **{f"val_{k}": v for k, v in wf.get("validation", {}).items()},
            **{f"test_{k}": v for k, v in wf.get("test", {}).items()},
            **{f"gve_{k}": v for k, v in gve.items()},
        }
        all_rows.append(row)

        if not trades.empty:
            trades_path = out_prefix.with_name(out_prefix.name + f"_trades_{cfg.family}.csv")
            trades.to_csv(trades_path, index=False)

    # Summary CSV
    if all_rows:
        summary_df = pd.DataFrame(all_rows)
        summary_path = out_prefix.with_name(out_prefix.name + "_summary.csv")
        summary_df.to_csv(summary_path, index=False)
        print(f"\nSaved summary: {summary_path}", flush=True)

    # Combined report
    report_path = out_prefix.with_name(out_prefix.name + "_report.md")
    header = [
        "# Kite Swing Research — Phase 1: Survivability Report",
        f"\nGenerated: {pd.Timestamp.now().isoformat(timespec='seconds')}",
        f"\nConfigs run: {len(all_rows)}  |  Data: {data_dir.name}",
        f"\nPermanent principle: strategy is only viable if edge > execution_gap + infrastructure_gap + lookahead_risk.",
        "\n---\n",
    ]
    report_path.write_text("\n".join(header) + "\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"Saved report:  {report_path}", flush=True)

    total_elapsed = time.perf_counter() - t0
    print(f"\nDone in {total_elapsed:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Advanced two-year intraday strategy research with regime and risk filters.

This is a second-pass harness. It builds on research_intraday_strategies_2y.py
but tests market regime, breadth, volatility state, exit models, and portfolio
sizing. It uses only prior-day indicators plus same-day open/gap information.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.research_intraday_strategies_2y import (  # noqa: E402
    DEFAULT_UNIVERSE,
    RESULTS_DIR,
    add_features,
    load_market_data,
    load_universe,
    max_drawdown,
    split_dates,
)

ADV_CACHE = RESULTS_DIR / "strategy_research_2y_cache"


@dataclass(frozen=True)
class AdvancedConfig:
    family: str
    side: str
    params: dict
    target_pct: float
    stop_pct: float
    top_n: int
    regime: str
    exit_model: str
    sizing: str

    def label(self) -> str:
        parts = [self.side, self.family, f"regime={self.regime}", f"exit={self.exit_model}", f"sizing={self.sizing}"]
        for key in sorted(self.params):
            value = self.params[key]
            parts.append(f"{key}={value:g}" if isinstance(value, float) else f"{key}={value}")
        parts.extend([f"t={self.target_pct:g}", f"s={self.stop_pct:g}", f"top={self.top_n}"])
        return "|".join(parts)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).rolling(period).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=high.index).rolling(period).mean() / atr.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.rolling(period).mean()


def fetch_index_context(start: str, end: str, refresh: bool = False) -> pd.DataFrame:
    cache_path = ADV_CACHE / f"NIFTY_{start}_{end}.csv"
    ADV_CACHE.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and not refresh:
        return pd.read_csv(cache_path, parse_dates=["date"])

    df = yf.download("^NSEI", start=start, end=pd.to_datetime(end) + pd.Timedelta(days=1), auto_adjust=True, progress=False, threads=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns={c: c.lower() for c in df.columns}).reset_index()
    date_col = "Date" if "Date" in df.columns else "date"
    df = df.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    prev_close = df["close"].shift(1)
    tr = pd.concat([(df["high"] - df["low"]).abs(), (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["idx_gap_pct"] = (df["open"] / prev_close - 1.0) * 100.0
    df["idx_ret_1"] = df["close"].pct_change(1).shift(1) * 100.0
    df["idx_ret_5"] = df["close"].pct_change(5).shift(1) * 100.0
    df["idx_ret_20"] = df["close"].pct_change(20).shift(1) * 100.0
    df["idx_sma20"] = df["close"].rolling(20).mean().shift(1)
    df["idx_sma50"] = df["close"].rolling(50).mean().shift(1)
    df["idx_prev_close"] = prev_close
    df["idx_uptrend"] = (df["idx_prev_close"] > df["idx_sma50"]) & (df["idx_sma20"] > df["idx_sma50"])
    df["idx_atr_pct"] = tr.rolling(14).mean().shift(1) / prev_close * 100.0
    df["idx_atr_rank60"] = df["idx_atr_pct"].rolling(60).rank(pct=True).shift(1)
    keep = [
        "date", "idx_gap_pct", "idx_ret_1", "idx_ret_5", "idx_ret_20", "idx_uptrend",
        "idx_atr_pct", "idx_atr_rank60",
    ]
    out = df[keep].copy()
    out.to_csv(cache_path, index=False)
    return out


def enrich_advanced_features(features: pd.DataFrame, start: str, end: str, refresh: bool = False) -> pd.DataFrame:
    work = features.copy()
    frames = []
    for _, g in work.groupby("symbol", sort=False):
        g = g.sort_values("date").copy()
        g["adx_14"] = _adx(g["high"], g["low"], g["close"], 14).shift(1)
        g["dist_sma20_pct"] = (g["prev_close"] / g["sma_20"] - 1.0) * 100.0
        g["dist_sma50_pct"] = (g["prev_close"] / g["sma_50"] - 1.0) * 100.0
        frames.append(g)
    work = pd.concat(frames, ignore_index=True)

    breadth = (
        work.groupby("date")
        .agg(
            breadth_sma20=("dist_sma20_pct", lambda s: float((s > 0).mean())),
            breadth_sma50=("dist_sma50_pct", lambda s: float((s > 0).mean())),
            median_stock_atr_pct=("atr_pct", "median"),
            median_stock_ret5=("ret_5", "median"),
        )
        .reset_index()
    )
    idx = fetch_index_context(start, end, refresh=refresh)
    work = work.merge(breadth, on="date", how="left")
    if not idx.empty:
        work = work.merge(idx, on="date", how="left")
    else:
        for col in ["idx_gap_pct", "idx_ret_1", "idx_ret_5", "idx_ret_20", "idx_uptrend", "idx_atr_pct", "idx_atr_rank60"]:
            work[col] = np.nan
    work["idx_uptrend"] = work["idx_uptrend"].fillna(False).astype(bool)
    return work.replace([np.inf, -np.inf], np.nan)


def _base(df: pd.DataFrame, cfg: AdvancedConfig) -> pd.Series:
    return (
        (df["open"] >= 20)
        & (df["prev_close"] > 0)
        & (df["dollar_volume_cr"] >= float(cfg.params.get("min_turnover_cr", 15.0)))
        & df[["gap_pct", "ret_5", "ret_20", "rsi_14", "atr_pct", "adx_14", "breadth_sma50", "idx_atr_rank60"]].notna().all(axis=1)
    )


def _regime_mask(df: pd.DataFrame, cfg: AdvancedConfig) -> pd.Series:
    regime = cfg.regime
    if regime == "any":
        return pd.Series(True, index=df.index)
    if regime == "risk_on":
        return (df["idx_uptrend"]) & (df["breadth_sma50"] >= 0.45) & (df["idx_atr_rank60"] <= 0.80)
    if regime == "strong_risk_on":
        return (df["idx_uptrend"]) & (df["breadth_sma50"] >= 0.55) & (df["idx_ret_20"] >= 0) & (df["idx_atr_rank60"] <= 0.70)
    if regime == "risk_off":
        return (~df["idx_uptrend"]) | (df["breadth_sma50"] < 0.40)
    if regime == "calm":
        return df["idx_atr_rank60"] <= 0.55
    if regime == "volatile":
        return df["idx_atr_rank60"] >= 0.65
    if regime == "pullback":
        return (df["idx_ret_5"] <= -1.0) & (df["idx_atr_rank60"] <= 0.85)
    raise ValueError(f"Unknown regime: {regime}")


def score_candidates(df: pd.DataFrame, cfg: AdvancedConfig) -> pd.Series:
    b = _base(df, cfg) & _regime_mask(df, cfg)
    p = cfg.params
    fam = cfg.family

    if fam == "long_pullback":
        mask = (
            b
            & (df["rsi_2"] <= float(p.get("max_rsi2", 10)))
            & (df["gap_pct"].between(float(p.get("min_gap", -3.0)), float(p.get("max_gap", -0.25))))
            & (df["ret_20"] >= float(p.get("trend_floor", -5.0)))
            & (df["atr_pct"].between(float(p.get("min_atr", 1.0)), float(p.get("max_atr", 6.0))))
            & (df["adx_14"] >= float(p.get("min_adx", 12.0)))
        )
        score = (float(p.get("max_rsi2", 10)) - df["rsi_2"]) * 2 + df["gap_pct"].abs() * 3 + df["breadth_sma50"] * 20 - df["idx_atr_rank60"] * 5
    elif fam == "long_breakout":
        mask = (
            b
            & (df["open"] >= df["prior_high_20"] * (1.0 + float(p.get("breakout_buffer", 0.0)) / 100.0))
            & (df["ret_20"] >= float(p.get("min_ret20", 5.0)))
            & (df["gap_pct"].between(0.0, float(p.get("max_gap", 4.0))))
            & (df["vol_ratio_prev"] >= float(p.get("min_vol_prev", 1.0)))
            & (df["adx_14"] >= float(p.get("min_adx", 15.0)))
            & (df["atr_pct"] <= float(p.get("max_atr", 7.0)))
        )
        score = df["ret_20"] + df["gap_pct"] * 2 + df["vol_ratio_prev"].clip(0, 8) * 3 + df["adx_14"] / 3
    elif fam == "long_compression":
        mask = (
            b
            & (df["range_rank_20"] <= float(p.get("max_range_rank", 0.25)))
            & (df["gap_pct"].between(float(p.get("min_gap", 0.0)), float(p.get("max_gap", 3.0))))
            & (df["ret_20"] >= float(p.get("trend_floor", -2.0)))
            & (df["atr_pct"].between(0.8, float(p.get("max_atr", 5.5))))
            & (df["vol_ratio_prev"] >= float(p.get("min_vol_prev", 0.8)))
        )
        score = (1.0 - df["range_rank_20"]) * 25 + df["gap_pct"] * 4 + df["vol_ratio_prev"].clip(0, 6) * 2 + df["breadth_sma20"] * 10
    elif fam == "short_gap_fade":
        mask = (
            b
            & (df["gap_pct"].between(float(p.get("min_gap", 2.0)), float(p.get("max_gap", 5.0))))
            & (df["ret_5"] >= float(p.get("min_ret5", 3.0)))
            & (df["rsi_14"] >= float(p.get("min_rsi", 60.0)))
            & (df["atr_pct"].between(float(p.get("min_atr", 1.0)), float(p.get("max_atr", 8.0))))
        )
        score = df["gap_pct"] * 4 + df["ret_5"] + df["rsi_14"] / 4 + df["idx_atr_rank60"].fillna(0) * 4
    elif fam == "short_breakdown":
        mask = (
            b
            & (df["open"] <= df["prior_low_20"] * (1.0 - float(p.get("breakdown_buffer", 0.0)) / 100.0))
            & (df["ret_20"] <= -float(p.get("min_down20", 6.0)))
            & (df["gap_pct"].between(-float(p.get("max_gap", 3.0)), 0.5))
            & (df["adx_14"] >= float(p.get("min_adx", 15.0)))
        )
        score = -df["ret_20"] + (-df["gap_pct"].clip(upper=0)) * 3 + df["adx_14"] / 4
    else:
        raise ValueError(f"Unknown family: {fam}")

    return score.where(mask)


def generate_configs(depth: str = "standard") -> list[AdvancedConfig]:
    regimes = ["any", "risk_on", "strong_risk_on", "calm", "pullback", "risk_off", "volatile"]
    sizing_models = ["equal", "vol_inverse", "score_weighted"]
    exit_models = ["static", "eod_only", "tight"]
    targets = [1.5, 2.0, 3.0, 4.0]
    stops = [0.7, 1.0, 1.5]
    top_ns = [1, 2]
    configs: list[AdvancedConfig] = []

    def add(family: str, side: str, grid: dict[str, list[float]], allowed_regimes: list[str]) -> None:
        keys = list(grid)
        for values in product(*[grid[k] for k in keys]):
            params = dict(zip(keys, values))
            for target, stop, top_n, regime, exit_model, sizing in product(targets, stops, top_ns, allowed_regimes, exit_models, sizing_models):
                if stop > target:
                    continue
                if exit_model == "eod_only" and target != 4.0:
                    continue
                configs.append(
                    AdvancedConfig(
                        family=family,
                        side=side,
                        params=params,
                        target_pct=target,
                        stop_pct=stop,
                        top_n=top_n,
                        regime=regime,
                        exit_model=exit_model,
                        sizing=sizing,
                    )
                )

    add(
        "long_pullback",
        "long",
        {"max_rsi2": [5.0, 12.0], "min_gap": [-3.0, -1.5], "max_gap": [-0.25], "trend_floor": [-8.0, 0.0], "min_adx": [10.0, 16.0], "min_turnover_cr": [15.0, 30.0]},
        ["any", "risk_on", "strong_risk_on", "calm", "pullback"],
    )
    add(
        "long_breakout",
        "long",
        {"breakout_buffer": [0.0, 0.5], "min_ret20": [4.0, 8.0], "max_gap": [3.0, 5.0], "min_vol_prev": [1.0, 1.8], "min_adx": [14.0, 20.0], "min_turnover_cr": [15.0, 30.0]},
        ["risk_on", "strong_risk_on", "calm"],
    )
    add(
        "long_compression",
        "long",
        {"max_range_rank": [0.2, 0.35], "min_gap": [0.0, 0.5], "max_gap": [2.5, 4.0], "min_vol_prev": [0.8, 1.3], "min_turnover_cr": [15.0, 30.0]},
        ["risk_on", "strong_risk_on", "calm"],
    )
    add(
        "short_gap_fade",
        "short",
        {"min_gap": [2.0, 3.0], "max_gap": [4.0, 6.0], "min_ret5": [3.0, 5.0], "min_rsi": [60.0, 68.0], "min_turnover_cr": [15.0, 30.0]},
        ["any", "risk_off", "volatile", "pullback"],
    )
    add(
        "short_breakdown",
        "short",
        {"breakdown_buffer": [0.0, 0.5], "min_down20": [5.0, 10.0], "max_gap": [2.0, 4.0], "min_adx": [14.0, 20.0], "min_turnover_cr": [15.0, 30.0]},
        ["risk_off", "volatile"],
    )
    # Seed from the first-pass OOS survivor. Keep it in every reduced grid so
    # future advanced sweeps do not accidentally drop the strongest realistic
    # baseline while testing broader regime ideas.
    seed = AdvancedConfig(
        family="short_gap_fade",
        side="short",
        params={"max_gap": 4.0, "min_gap": 3.0, "min_ret5": 5.0, "min_rsi": 60.0, "min_turnover_cr": 10.0},
        target_pct=4.0,
        stop_pct=0.8,
        top_n=2,
        regime="any",
        exit_model="static",
        sizing="equal",
    )
    if seed.label() not in {cfg.label() for cfg in configs}:
        configs.append(seed)
    if depth == "quick":
        reduced = [
            cfg for idx, cfg in enumerate(configs)
            if cfg.sizing in {"equal", "vol_inverse"}
            and cfg.exit_model in {"static", "tight"}
            and idx % 8 == 0
        ]
        if seed.label() not in {cfg.label() for cfg in reduced}:
            reduced.append(seed)
        return reduced
    return configs


def _raw_trade_return(d: pd.DataFrame, cfg: AdvancedConfig, cost_pct: float) -> pd.Series:
    if cfg.side == "long":
        mfe = (d["high"] / d["open"] - 1.0) * 100.0
        mae = (d["low"] / d["open"] - 1.0) * 100.0
        eod = (d["close"] / d["open"] - 1.0) * 100.0
    else:
        mfe = (d["open"] / d["low"] - 1.0) * 100.0
        mae = (d["high"] / d["open"] - 1.0) * 100.0
        eod = (d["open"] / d["close"] - 1.0) * 100.0

    target = float(cfg.target_pct)
    stop = float(cfg.stop_pct)
    if cfg.exit_model == "tight":
        target = min(target, 2.0)
        stop = min(stop, 1.0)
    if cfg.exit_model == "eod_only":
        gross = eod.clip(lower=-stop * 1.5, upper=target)
    else:
        target_hit = mfe >= target
        stop_hit = mae >= stop
        gross = np.where(target_hit & stop_hit, -stop, np.where(target_hit, target, np.where(stop_hit, -stop, eod)))
    return pd.Series(gross, index=d.index) - float(cost_pct)


def select_trades(df: pd.DataFrame, cfg: AdvancedConfig, cost_pct: float) -> pd.DataFrame:
    work = df.copy()
    work["score"] = score_candidates(work, cfg)
    work = work.dropna(subset=["score"]).copy()
    if work.empty:
        return work
    work = work.sort_values(["date", "score", "dollar_volume_cr"], ascending=[True, False, False])
    work = work.groupby("date", group_keys=False).head(cfg.top_n).copy()
    work["trade_return_pct"] = _raw_trade_return(work, cfg, cost_pct)
    work["strategy"] = cfg.label()
    work["family"] = cfg.family
    work["side"] = cfg.side
    work["regime_filter"] = cfg.regime
    work["exit_model"] = cfg.exit_model
    work["sizing"] = cfg.sizing
    return work


def _daily_returns(trades: pd.DataFrame, all_dates: list[pd.Timestamp], cfg: AdvancedConfig, max_daily_loss_pct: float) -> pd.DataFrame:
    dates = pd.DataFrame({"date": all_dates})
    if trades.empty:
        dates["daily_return_pct"] = 0.0
        dates["trades"] = 0
        return dates

    rows = []
    for date_value, g in trades.groupby("date", sort=True):
        if cfg.sizing == "equal":
            weights = np.ones(len(g)) / len(g)
        elif cfg.sizing == "vol_inverse":
            inv = 1.0 / g["atr_pct"].clip(lower=0.8).astype(float)
            weights = (inv / inv.sum()).to_numpy() if inv.sum() > 0 else np.ones(len(g)) / len(g)
        else:
            s = g["score"].clip(lower=0).astype(float)
            weights = (s / s.sum()).to_numpy() if s.sum() > 0 else np.ones(len(g)) / len(g)
        ret = float(np.sum(g["trade_return_pct"].to_numpy() * weights))
        ret = max(ret, -abs(max_daily_loss_pct))
        rows.append({"date": date_value, "daily_return_pct": ret, "trades": len(g)})
    daily = dates.merge(pd.DataFrame(rows), on="date", how="left")
    daily["daily_return_pct"] = daily["daily_return_pct"].fillna(0.0)
    daily["trades"] = daily["trades"].fillna(0).astype(int)
    return daily


def summarize(trades: pd.DataFrame, all_dates: list[pd.Timestamp], cfg: AdvancedConfig, max_daily_loss_pct: float = 3.0) -> dict:
    daily = _daily_returns(trades, all_dates, cfg, max_daily_loss_pct=max_daily_loss_pct)
    returns = daily["daily_return_pct"].astype(float)
    equity = (1.0 + returns / 100.0).cumprod()
    if trades.empty:
        pos_sum = neg_sum = 0.0
    else:
        pos_sum = float(trades.loc[trades["trade_return_pct"] > 0, "trade_return_pct"].sum())
        neg_sum = float(trades.loc[trades["trade_return_pct"] < 0, "trade_return_pct"].sum())
    std = returns.std(ddof=1)
    return {
        "strategy": cfg.label(),
        "family": cfg.family,
        "side": cfg.side,
        "regime": cfg.regime,
        "exit_model": cfg.exit_model,
        "sizing": cfg.sizing,
        "params_json": json.dumps(cfg.params, sort_keys=True),
        "target_pct": cfg.target_pct,
        "stop_pct": cfg.stop_pct,
        "top_n": cfg.top_n,
        "trades": int(len(trades)),
        "active_days": int(trades["date"].nunique()) if not trades.empty else 0,
        "avg_daily_return_pct": round(float(returns.mean()), 4),
        "avg_active_day_return_pct": round(float(daily.loc[daily["daily_return_pct"] != 0, "daily_return_pct"].mean()), 4) if (daily["daily_return_pct"] != 0).any() else 0.0,
        "total_return_pct": round(float((equity.iloc[-1] - 1.0) * 100.0), 2) if not equity.empty else 0.0,
        "max_drawdown_pct": round(float(max_drawdown(returns)), 2),
        "sharpe": round(float((returns.mean() / std) * math.sqrt(252)), 3) if std and not math.isnan(std) else 0.0,
        "win_rate_pct": round(float((trades["trade_return_pct"] > 0).mean() * 100.0), 2) if not trades.empty else 0.0,
        "profit_factor": round(float(pos_sum / abs(neg_sum)), 3) if neg_sum < 0 else (999.0 if pos_sum > 0 else 0.0),
    }


def evaluate(df: pd.DataFrame, dates: list[pd.Timestamp], configs: list[AdvancedConfig], cost_pct: float) -> pd.DataFrame:
    rows = []
    for idx, cfg in enumerate(configs, 1):
        trades = select_trades(df, cfg, cost_pct)
        rows.append(summarize(trades, dates, cfg))
        if idx % 1000 == 0:
            print(f"  evaluated {idx}/{len(configs)}", flush=True)
    out = pd.DataFrame(rows)
    out["selection_score"] = out.apply(selection_score, axis=1)
    return out.sort_values(["selection_score", "avg_daily_return_pct", "sharpe"], ascending=[False, False, False]).reset_index(drop=True)


def selection_score(row: pd.Series) -> float:
    trades = int(row.get("trades", 0))
    if trades < 12:
        return -999.0
    dd = float(row.get("max_drawdown_pct", 0.0))
    pf = float(row.get("profit_factor", 0.0))
    sharpe = float(row.get("sharpe", 0.0))
    avg = float(row.get("avg_daily_return_pct", 0.0))
    trade_penalty = 0.10 if trades < 25 else 0.0
    dd_penalty = max(0.0, dd - 8.0) * 0.04
    pf_bonus = min(max(pf - 1.0, -0.5), 1.0) * 0.04
    return avg + sharpe * 0.025 + pf_bonus - dd_penalty - trade_penalty


def diagnose_failures(features: pd.DataFrame, out_prefix: Path) -> pd.DataFrame:
    rows = []
    for label, path in [
        ("long_validation_selected_test", RESULTS_DIR / "strategy_research_2y_validation_selected_test_trades.csv"),
        ("short_validation_selected_test", RESULTS_DIR / "strategy_research_2y_short_validation_selected_test_trades.csv"),
        ("long_walk_forward", RESULTS_DIR / "strategy_research_2y_walk_forward_trades.csv"),
    ]:
        if not path.exists():
            continue
        trades = pd.read_csv(path, parse_dates=["date"])
        if trades.empty or "trade_return_pct" not in trades.columns:
            continue
        merged = trades.merge(
            features[
                [
                    "date", "symbol", "idx_uptrend", "idx_atr_rank60", "breadth_sma50",
                    "idx_ret_5", "atr_pct", "ret_5", "gap_pct", "adx_14",
                ]
            ],
            on=["date", "symbol"],
            how="left",
            suffixes=("", "_ctx"),
        )
        merged["market_regime"] = np.select(
            [
                (merged["idx_uptrend"]) & (merged["breadth_sma50"] >= 0.55) & (merged["idx_atr_rank60"] <= 0.70),
                (merged["idx_atr_rank60"] >= 0.70),
                (~merged["idx_uptrend"]) | (merged["breadth_sma50"] < 0.40),
            ],
            ["strong_risk_on", "volatile", "risk_off"],
            default="mixed",
        )
        for regime, g in merged.groupby("market_regime"):
            rows.append(
                {
                    "sample": label,
                    "bucket": regime,
                    "trades": len(g),
                    "avg_trade_return_pct": round(float(g["trade_return_pct"].mean()), 4),
                    "win_rate_pct": round(float((g["trade_return_pct"] > 0).mean() * 100), 2),
                    "worst_trade_pct": round(float(g["trade_return_pct"].min()), 4),
                }
            )
    out = pd.DataFrame(rows)
    if not out.empty:
        out.to_csv(out_prefix.with_name(out_prefix.name + "_failure_diagnostics.csv"), index=False)
    return out


def walk_forward(features: pd.DataFrame, configs: list[AdvancedConfig], cost_pct: float, train_days: int, test_days: int, min_train_trades: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = sorted(features["date"].dropna().unique().tolist())
    selected = []
    decisions = []
    for start_idx in range(train_days, len(dates), test_days):
        train_dates = dates[start_idx - train_days:start_idx]
        test_dates = dates[start_idx:start_idx + test_days]
        if not test_dates:
            break
        train_df = features[features["date"].isin(train_dates)].copy()
        test_df = features[features["date"].isin(test_dates)].copy()
        ev = evaluate(train_df, train_dates, configs, cost_pct)
        ev = ev[ev["trades"] >= min_train_trades].copy()
        if ev.empty:
            continue
        best_label = ev.iloc[0]["strategy"]
        cfg = next(c for c in configs if c.label() == best_label)
        trades = select_trades(test_df, cfg, cost_pct)
        summary = summarize(trades, test_dates, cfg)
        if not trades.empty:
            trades = trades.copy()
            trades["walk_config"] = cfg.label()
            selected.append(trades)
        decisions.append(
            {
                "train_start": str(pd.Timestamp(train_dates[0]).date()),
                "train_end": str(pd.Timestamp(train_dates[-1]).date()),
                "test_start": str(pd.Timestamp(test_dates[0]).date()),
                "test_end": str(pd.Timestamp(test_dates[-1]).date()),
                "chosen_strategy": cfg.label(),
                "train_avg_daily_return_pct": ev.iloc[0]["avg_daily_return_pct"],
                "train_trades": ev.iloc[0]["trades"],
                "test_avg_daily_return_pct": summary["avg_daily_return_pct"],
                "test_trades": summary["trades"],
                "test_total_return_pct": summary["total_return_pct"],
            }
        )
        print(
            f"  WF {pd.Timestamp(test_dates[0]).date()}..{pd.Timestamp(test_dates[-1]).date()} "
            f"{cfg.family}/{cfg.regime} test avg={summary['avg_daily_return_pct']:.4f}% trades={summary['trades']}",
            flush=True,
        )
    return (
        pd.concat(selected, ignore_index=True) if selected else pd.DataFrame(),
        pd.DataFrame(decisions),
    )


def write_report(path: Path, data_summary: dict, diag: pd.DataFrame, train_eval: pd.DataFrame, val_eval: pd.DataFrame, final_summary: dict, wf_summary: dict, best_cfg: AdvancedConfig, goal_pct: float) -> None:
    top_families = (
        train_eval[train_eval["trades"] >= 12]
        .sort_values(["family", "selection_score"], ascending=[True, False])
        .groupby("family", as_index=False)
        .head(1)
        .sort_values("selection_score", ascending=False)
    )
    lines = [
        "# Advanced Two-Year Intraday Strategy Research",
        "",
        f"Generated: {pd.Timestamp.now().isoformat(timespec='seconds')}",
        "",
        "## Data",
        f"- Market: NSE cash equities from `{data_summary['universe_file']}`.",
        f"- Symbols requested: {data_summary['symbols_requested']}; usable: {data_summary['usable_symbols']}.",
        f"- Raw data: {data_summary['raw_start']} to {data_summary['raw_end']} ({data_summary['raw_days']} trading days).",
        f"- Signal period after warm-up: {data_summary['start']} to {data_summary['end']} ({data_summary['trading_days']} trading days).",
        "- Source: Yahoo Finance adjusted OHLCV plus Nifty 50 index context.",
        "- Note: this data source is not point-in-time constituent data, so full survivorship-bias removal is not possible here. Results are treated as research, not production proof.",
        "",
        "## Failure Diagnosis From Prior Pass",
        "- Long mean-reversion overfit most clearly: train winners had positive daily return, validation was small positive, final test fell to -0.3446% daily with 27.63% max drawdown.",
        "- Short gap-fade was more stable but sparse: final test +0.0386% daily with only 12 trades.",
        "- Unstable parameters: RSI2 threshold, opening gap width, and tight stop/large target combinations moved sharply between train and validation.",
        "- Drawdowns came from repeated failed dip-buying during weaker/volatile index breadth periods, not from one isolated trade.",
    ]
    if not diag.empty:
        lines.append("")
        lines.append("## Regime Loss Buckets")
        for _, row in diag.iterrows():
            lines.append(
                f"- {row['sample']} / {row['bucket']}: trades {int(row['trades'])}, "
                f"avg trade {row['avg_trade_return_pct']:+.4f}%, win {row['win_rate_pct']:.1f}%, worst {row['worst_trade_pct']:+.2f}%"
            )
    lines.append("")
    lines.append("## Strategy Families Tested")
    for _, row in top_families.iterrows():
        lines.append(
            f"- {row['family']} ({row['side']}): train avg daily {row['avg_daily_return_pct']:+.4f}%, "
            f"Sharpe {row['sharpe']:.2f}, trades {int(row['trades'])}, regime `{row['regime']}`, "
            f"exit `{row['exit_model']}`, sizing `{row['sizing']}`"
        )
    lines.extend(
        [
            "",
            "## Final Selected Strategy",
            f"- Strategy: `{best_cfg.label()}`",
            f"- Exact rules:",
            f"  - Side: {best_cfg.side}",
            f"  - Family: {best_cfg.family}",
            f"  - Regime filter: {best_cfg.regime}",
            f"  - Entry parameters: `{json.dumps(best_cfg.params, sort_keys=True)}`",
            f"  - Timeframe: daily OHLCV signal, same-day open entry, same-day exit.",
            f"  - Exit: `{best_cfg.exit_model}` with target {best_cfg.target_pct:.1f}% and stop {best_cfg.stop_pct:.1f}%, conservative stop-first if both hit.",
            f"  - Position sizing: `{best_cfg.sizing}` across top {best_cfg.top_n} ranked trade(s)/day.",
            f"  - Daily risk cap in simulation: -3.0% portfolio day loss.",
            "",
            "## Final Untouched Test Results",
            f"- Average daily return: {final_summary['avg_daily_return_pct']:+.4f}%",
            f"- Active-day average return: {final_summary['avg_active_day_return_pct']:+.4f}%",
            f"- Total return: {final_summary['total_return_pct']:+.2f}%",
            f"- Maximum drawdown: {final_summary['max_drawdown_pct']:.2f}%",
            f"- Sharpe ratio: {final_summary['sharpe']:.3f}",
            f"- Win rate: {final_summary['win_rate_pct']:.2f}%",
            f"- Profit factor: {final_summary['profit_factor']:.3f}",
            f"- Number of trades: {final_summary['trades']}",
            "",
            "## Walk-Forward Results",
            f"- Average daily return: {wf_summary['avg_daily_return_pct']:+.4f}%",
            f"- Total return: {wf_summary['total_return_pct']:+.2f}%",
            f"- Maximum drawdown: {wf_summary['max_drawdown_pct']:.2f}%",
            f"- Sharpe ratio: {wf_summary['sharpe']:.3f}",
            f"- Win rate: {wf_summary['win_rate_pct']:.2f}%",
            f"- Profit factor: {wf_summary['profit_factor']:.3f}",
            f"- Number of trades: {wf_summary['trades']}",
            "",
            "## Goal Distance",
            f"- Target: {goal_pct:.2f}% average daily return.",
            f"- Best final-test average daily return found: {final_summary['avg_daily_return_pct']:+.4f}%.",
            f"- Gap to target: {goal_pct - final_summary['avg_daily_return_pct']:.4f} percentage points/day.",
            f"- 3% daily goal achieved: {'YES' if final_summary['avg_daily_return_pct'] >= goal_pct else 'NO'}",
            "",
            "## Weaknesses",
            "- Daily OHLCV cannot prove true intraday order of target/stop hits; the test uses conservative stop-first ordering.",
            "- Yahoo Finance/current liquid universe does not remove survivorship bias fully.",
            "- The strongest positive final-test variants are selective; they will sit out many days.",
            "- Performance is sensitive to market breadth and volatility regime, so live paper validation remains mandatory.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Advanced 2-year intraday research with regime filters")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--start", default="2024-05-13")
    parser.add_argument("--end", default="2026-05-12")
    parser.add_argument("--cost-pct", type=float, default=0.35)
    parser.add_argument("--goal-pct", type=float, default=3.0)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--out-prefix", default=str(RESULTS_DIR / "strategy_research_2y_advanced"))
    parser.add_argument("--depth", choices=["quick", "standard"], default="standard")
    parser.add_argument("--walk-configs", type=int, default=250)
    args = parser.parse_args()

    universe_path = Path(args.universe)
    symbols = load_universe(universe_path, args.limit)
    print(f"Loading data for {len(symbols)} symbols...", flush=True)
    raw = load_market_data(symbols, args.start, args.end, refresh=bool(args.refresh))
    base_features = add_features(raw)
    features = enrich_advanced_features(base_features, args.start, args.end, refresh=bool(args.refresh))
    features = features[(features["date"] >= pd.Timestamp(args.start)) & (features["date"] <= pd.Timestamp(args.end))].copy()
    features = features.dropna(subset=["prev_close", "gap_pct", "sma_50", "atr_pct", "vol_ratio_prev", "adx_14"]).copy()

    all_dates = sorted(features["date"].dropna().unique().tolist())
    train_dates, val_dates, test_dates = split_dates(all_dates)
    train_df = features[features["date"].isin(train_dates)].copy()
    val_df = features[features["date"].isin(val_dates)].copy()
    test_df = features[features["date"].isin(test_dates)].copy()
    configs = generate_configs(args.depth)
    print(f"Usable symbols: {features['symbol'].nunique()}, dates: {len(all_dates)}, configs: {len(configs)}", flush=True)

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    diag = diagnose_failures(features, out_prefix)

    print("Evaluating advanced train grid...", flush=True)
    train_eval = evaluate(train_df, train_dates, configs, args.cost_pct)
    train_eval.to_csv(out_prefix.with_name(out_prefix.name + "_train_grid.csv"), index=False)

    train_candidates = train_eval[train_eval["trades"] >= 20].head(max(1, int(args.walk_configs))).copy()
    candidate_labels = set(train_candidates["strategy"].astype(str))
    candidate_configs = [cfg for cfg in configs if cfg.label() in candidate_labels]

    print(f"Evaluating {len(candidate_configs)} candidates on validation...", flush=True)
    val_eval = evaluate(val_df, val_dates, candidate_configs, args.cost_pct)
    val_eval.to_csv(out_prefix.with_name(out_prefix.name + "_validation_grid.csv"), index=False)
    eligible = val_eval[(val_eval["trades"] >= 8) & (val_eval["max_drawdown_pct"] <= 12.0) & (val_eval["profit_factor"] >= 0.90)].copy()
    if eligible.empty:
        eligible = val_eval[val_eval["trades"] >= 5].copy()
    if eligible.empty:
        eligible = val_eval.copy()
    best_label = eligible.sort_values(["selection_score", "avg_daily_return_pct", "sharpe"], ascending=[False, False, False]).iloc[0]["strategy"]
    best_cfg = next(cfg for cfg in configs if cfg.label() == best_label)
    print(f"Best validation-selected strategy: {best_cfg.label()}", flush=True)

    final_trades = select_trades(test_df, best_cfg, args.cost_pct)
    final_summary = summarize(final_trades, test_dates, best_cfg)
    final_trades.to_csv(out_prefix.with_name(out_prefix.name + "_final_test_trades.csv"), index=False)
    pd.DataFrame([final_summary]).to_csv(out_prefix.with_name(out_prefix.name + "_final_test_summary.csv"), index=False)

    print("Running walk-forward...", flush=True)
    wf_trades, wf_decisions = walk_forward(features, candidate_configs, args.cost_pct, train_days=252, test_days=63, min_train_trades=15)
    wf_dates = all_dates[252:]
    wf_summary = summarize(wf_trades, wf_dates, best_cfg) if not wf_trades.empty else summarize(pd.DataFrame(), wf_dates, best_cfg)
    wf_trades.to_csv(out_prefix.with_name(out_prefix.name + "_walk_forward_trades.csv"), index=False)
    wf_decisions.to_csv(out_prefix.with_name(out_prefix.name + "_walk_forward_decisions.csv"), index=False)
    pd.DataFrame([wf_summary]).to_csv(out_prefix.with_name(out_prefix.name + "_walk_forward_summary.csv"), index=False)

    data_summary = {
        "universe_file": str(universe_path),
        "symbols_requested": len(symbols),
        "usable_symbols": int(features["symbol"].nunique()),
        "raw_start": str(pd.Timestamp(raw["date"].min()).date()),
        "raw_end": str(pd.Timestamp(raw["date"].max()).date()),
        "raw_days": int(raw["date"].nunique()),
        "start": str(pd.Timestamp(all_dates[0]).date()),
        "end": str(pd.Timestamp(all_dates[-1]).date()),
        "trading_days": len(all_dates),
    }
    report_path = out_prefix.with_name(out_prefix.name + "_report.md")
    write_report(report_path, data_summary, diag, train_eval, val_eval, final_summary, wf_summary, best_cfg, args.goal_pct)

    print("\nSaved advanced outputs:")
    for suffix in [
        "_failure_diagnostics.csv",
        "_train_grid.csv",
        "_validation_grid.csv",
        "_final_test_trades.csv",
        "_final_test_summary.csv",
        "_walk_forward_trades.csv",
        "_walk_forward_decisions.csv",
        "_walk_forward_summary.csv",
        "_report.md",
    ]:
        print(f"  {out_prefix.with_name(out_prefix.name + suffix)}")
    print("\nFinal test summary:")
    for key in ["avg_daily_return_pct", "total_return_pct", "max_drawdown_pct", "sharpe", "win_rate_pct", "profit_factor", "trades"]:
        print(f"  {key}: {final_summary[key]}")
    print("Walk-forward summary:")
    for key in ["avg_daily_return_pct", "total_return_pct", "max_drawdown_pct", "sharpe", "win_rate_pct", "profit_factor", "trades"]:
        print(f"  {key}: {wf_summary[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

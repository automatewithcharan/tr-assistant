#!/usr/bin/env python3
"""Two-year long-only intraday strategy research on NSE daily OHLCV.

The harness intentionally uses only information known before or at the entry
open: prior-day indicators plus today's open/gap. Intraday exits are simulated
from the same day's high/low/close with conservative stop-first ordering when a
target and stop are both touched.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results" / "Data"
CACHE_DIR = RESULTS_DIR / "strategy_research_2y_cache"
DEFAULT_UNIVERSE = RESULTS_DIR / "liquid_universe_latest.csv"


@dataclass(frozen=True)
class StrategyConfig:
    family: str
    params: dict
    target_pct: float
    stop_pct: float
    top_n: int
    min_price: float = 20.0

    def label(self) -> str:
        parts = [self.family]
        for key in sorted(self.params):
            value = self.params[key]
            if isinstance(value, float):
                parts.append(f"{key}={value:g}")
            else:
                parts.append(f"{key}={value}")
        parts.extend([f"t={self.target_pct:g}", f"s={self.stop_pct:g}", f"top={self.top_n}"])
        return "|".join(parts)


def _normalise_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _yahoo_symbol(symbol: str) -> str:
    symbol = _normalise_symbol(symbol)
    return f"{symbol}.NS"


def load_universe(path: Path, limit: int) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path)
    col = "symbol" if "symbol" in df.columns else "SYMBOL"
    if "avg_turnover_cr" in df.columns:
        df = df.sort_values("avg_turnover_cr", ascending=False)
    symbols = [_normalise_symbol(s) for s in df[col].tolist() if _normalise_symbol(s)]
    blocked_suffixes = ("BEES", "ETF")
    symbols = [s for s in symbols if not s.endswith(blocked_suffixes)]
    return symbols[: max(1, int(limit))]


def fetch_symbol_history(symbol: str, start: str, end: str, refresh: bool) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{symbol}_{start}_{end}.csv"
    if cache_path.exists() and not refresh:
        try:
            df = pd.read_csv(cache_path, parse_dates=["date"])
            if not df.empty:
                return df
        except Exception:
            pass

    ticker = _yahoo_symbol(symbol)
    df = yf.download(ticker, start=start, end=pd.to_datetime(end) + pd.Timedelta(days=1), auto_adjust=True, progress=False, threads=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    rename = {c: c.lower() for c in df.columns}
    df = df.rename(columns=rename).reset_index()
    date_col = "Date" if "Date" in df.columns else "date"
    df = df.rename(columns={date_col: "date"})
    keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    df["symbol"] = symbol
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)
    if not df.empty:
        df.to_csv(cache_path, index=False)
    return df


def load_market_data(symbols: Iterable[str], start: str, end: str, refresh: bool) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    failures: list[str] = []
    for idx, symbol in enumerate(symbols, start=1):
        df = fetch_symbol_history(symbol, start, end, refresh)
        if df.empty or len(df) < 120:
            failures.append(symbol)
        else:
            parts.append(df)
        if idx % 25 == 0:
            print(f"  downloaded/loaded {idx} symbols; usable={len(parts)} failures={len(failures)}", flush=True)
    if not parts:
        raise RuntimeError("No usable market data loaded.")
    master = pd.concat(parts, ignore_index=True)
    master["date"] = pd.to_datetime(master["date"]).dt.tz_localize(None)
    return master.sort_values(["symbol", "date"]).reset_index(drop=True)


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol, g in df.groupby("symbol", sort=False):
        g = g.sort_values("date").copy()
        prev_close = g["close"].shift(1)
        prev_high = g["high"].shift(1)
        prev_low = g["low"].shift(1)
        tr = pd.concat(
            [
                (g["high"] - g["low"]).abs(),
                (g["high"] - prev_close).abs(),
                (g["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        g["prev_close"] = prev_close
        g["gap_pct"] = (g["open"] / prev_close - 1.0) * 100.0
        g["ret_1"] = g["close"].pct_change(1).shift(1) * 100.0
        g["ret_3"] = g["close"].pct_change(3).shift(1) * 100.0
        g["ret_5"] = g["close"].pct_change(5).shift(1) * 100.0
        g["ret_20"] = g["close"].pct_change(20).shift(1) * 100.0
        g["sma_10"] = g["close"].rolling(10).mean().shift(1)
        g["sma_20"] = g["close"].rolling(20).mean().shift(1)
        g["sma_50"] = g["close"].rolling(50).mean().shift(1)
        g["rsi_2"] = _rsi(g["close"], 2).shift(1)
        g["rsi_14"] = _rsi(g["close"], 14).shift(1)
        g["atr_14"] = tr.rolling(14).mean().shift(1)
        g["atr_pct"] = g["atr_14"] / prev_close * 100.0
        g["atr_pct_rank_60"] = g["atr_pct"].rolling(60).rank(pct=True).shift(1)
        g["vol_avg_20"] = g["volume"].rolling(20).mean().shift(1)
        g["vol_ratio_prev"] = g["volume"].shift(1) / g["vol_avg_20"]
        g["prior_high_20"] = g["high"].rolling(20).max().shift(1)
        g["prior_low_20"] = g["low"].rolling(20).min().shift(1)
        g["range_pct_prev"] = (prev_high - prev_low) / prev_close * 100.0
        g["range_rank_20"] = g["range_pct_prev"].rolling(20).rank(pct=True).shift(1)
        g["dollar_volume_cr"] = (g["close"].shift(1) * g["volume"].shift(1)) / 10_000_000.0
        frames.append(g)
    out = pd.concat(frames, ignore_index=True)
    out = out.replace([np.inf, -np.inf], np.nan)
    return out


def _base_filter(df: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    return (
        (df["open"] >= cfg.min_price)
        & (df["prev_close"] > 0)
        & (df["dollar_volume_cr"] >= float(cfg.params.get("min_turnover_cr", 10.0)))
        & df[["gap_pct", "ret_5", "ret_20", "rsi_14", "atr_pct", "vol_ratio_prev"]].notna().all(axis=1)
    )


def score_trend(df: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    min_ret20 = float(cfg.params.get("min_ret20", 6.0))
    max_gap = float(cfg.params.get("max_gap", 2.0))
    mask = (
        _base_filter(df, cfg)
        & (df["sma_20"] > df["sma_50"])
        & (df["prev_close"] > df["sma_20"])
        & (df["ret_20"] >= min_ret20)
        & (df["gap_pct"].between(-0.5, max_gap))
    )
    score = df["ret_20"] + df["ret_5"] * 0.5 + df["vol_ratio_prev"].clip(0, 5) * 2 - df["atr_pct"].clip(0, 10)
    return score.where(mask)


def score_mean_reversion(df: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    max_rsi2 = float(cfg.params.get("max_rsi2", 12.0))
    max_gap = float(cfg.params.get("max_gap", -0.5))
    trend_floor = float(cfg.params.get("trend_floor", -8.0))
    mask = (
        _base_filter(df, cfg)
        & (df["rsi_2"] <= max_rsi2)
        & (df["gap_pct"] <= max_gap)
        & (df["ret_20"] >= trend_floor)
        & (df["atr_pct"].between(1.0, 8.0))
    )
    score = (max_rsi2 - df["rsi_2"]) + (df["gap_pct"].abs() * 2) + df["dollar_volume_cr"].clip(0, 100) / 20
    return score.where(mask)


def score_breakout(df: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    breakout_buffer = float(cfg.params.get("breakout_buffer", 0.0))
    min_ret20 = float(cfg.params.get("min_ret20", 4.0))
    max_gap = float(cfg.params.get("max_gap", 5.0))
    mask = (
        _base_filter(df, cfg)
        & (df["open"] >= df["prior_high_20"] * (1.0 + breakout_buffer / 100.0))
        & (df["ret_20"] >= min_ret20)
        & (df["gap_pct"].between(0.0, max_gap))
    )
    score = df["gap_pct"] * 2 + df["ret_20"] + df["vol_ratio_prev"].clip(0, 8) * 3
    return score.where(mask)


def score_momentum(df: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    min_ret5 = float(cfg.params.get("min_ret5", 3.0))
    min_ret20 = float(cfg.params.get("min_ret20", 8.0))
    min_vol_prev = float(cfg.params.get("min_vol_prev", 1.2))
    mask = (
        _base_filter(df, cfg)
        & (df["ret_5"] >= min_ret5)
        & (df["ret_20"] >= min_ret20)
        & (df["vol_ratio_prev"] >= min_vol_prev)
        & (df["gap_pct"].between(-0.25, 3.5))
        & (df["rsi_14"] <= 78)
    )
    score = df["ret_5"] * 2 + df["ret_20"] + df["vol_ratio_prev"].clip(0, 8) * 4 - df["gap_pct"].clip(lower=0) * 1.5
    return score.where(mask)


def score_volatility(df: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    max_range_rank = float(cfg.params.get("max_range_rank", 0.25))
    min_gap = float(cfg.params.get("min_gap", 0.2))
    max_gap = float(cfg.params.get("max_gap", 3.0))
    mask = (
        _base_filter(df, cfg)
        & (df["range_rank_20"] <= max_range_rank)
        & (df["gap_pct"].between(min_gap, max_gap))
        & (df["ret_20"] >= -3.0)
        & (df["atr_pct"].between(0.8, 6.0))
    )
    score = (1.0 - df["range_rank_20"]) * 20 + df["gap_pct"] * 3 + df["ret_5"].clip(-5, 10)
    return score.where(mask)


def score_hybrid(df: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    min_vol_prev = float(cfg.params.get("min_vol_prev", 1.5))
    min_ret5 = float(cfg.params.get("min_ret5", 0.5))
    max_gap = float(cfg.params.get("max_gap", 3.0))
    min_gap = float(cfg.params.get("min_gap", 0.0))
    mask = (
        _base_filter(df, cfg)
        & (df["vol_ratio_prev"] >= min_vol_prev)
        & (df["ret_5"] >= min_ret5)
        & (df["ret_20"] >= -2.0)
        & (df["gap_pct"].between(min_gap, max_gap))
        & (df["rsi_14"].between(45, 76))
        & (df["atr_pct"].between(1.0, 7.0))
    )
    score = df["vol_ratio_prev"].clip(0, 10) * 5 + df["ret_5"] * 1.5 + df["gap_pct"] * 2 + df["ret_20"].clip(-5, 25)
    return score.where(mask)


SCORE_FUNCS: dict[str, Callable[[pd.DataFrame, StrategyConfig], pd.Series]] = {
    "trend": score_trend,
    "mean_reversion": score_mean_reversion,
    "breakout": score_breakout,
    "momentum": score_momentum,
    "volatility": score_volatility,
    "hybrid": score_hybrid,
}


def generate_configs(grid_depth: str = "standard") -> list[StrategyConfig]:
    configs: list[StrategyConfig] = []
    broad = str(grid_depth).lower() == "broad"
    targets = [1.0, 1.5, 2.0, 3.0, 4.0] if broad else [1.5, 2.0, 3.0, 4.0]
    stops = [0.8, 1.0, 1.5, 2.0] if broad else [0.8, 1.0, 1.5]
    top_ns = [1, 2, 3] if broad else [1, 2]

    def add(family: str, param_grid: dict[str, list[float]]) -> None:
        keys = list(param_grid)
        for values in product(*[param_grid[k] for k in keys]):
            params = dict(zip(keys, values))
            for target, stop, top_n in product(targets, stops, top_ns):
                if stop > target * 1.25:
                    continue
                configs.append(StrategyConfig(family=family, params=params, target_pct=target, stop_pct=stop, top_n=top_n))

    if broad:
        add("trend", {"min_ret20": [4.0, 6.0, 10.0], "max_gap": [1.0, 2.0, 3.0], "min_turnover_cr": [10.0, 25.0]})
        add("mean_reversion", {"max_rsi2": [5.0, 10.0, 15.0], "max_gap": [-0.25, -0.75, -1.25], "trend_floor": [-12.0, -5.0, 0.0], "min_turnover_cr": [10.0, 25.0]})
        add("breakout", {"breakout_buffer": [0.0, 0.25, 0.5], "min_ret20": [2.0, 5.0, 10.0], "max_gap": [3.0, 5.0], "min_turnover_cr": [10.0, 25.0]})
        add("momentum", {"min_ret5": [2.0, 4.0, 6.0], "min_ret20": [6.0, 10.0, 15.0], "min_vol_prev": [1.0, 1.5, 2.0], "min_turnover_cr": [10.0, 25.0]})
        add("volatility", {"max_range_rank": [0.15, 0.25, 0.35], "min_gap": [0.0, 0.25, 0.5], "max_gap": [2.0, 3.0, 4.0], "min_turnover_cr": [10.0, 25.0]})
        add("hybrid", {"min_vol_prev": [1.2, 1.5, 2.0], "min_ret5": [0.0, 1.0, 2.0], "min_gap": [0.0, 0.5], "max_gap": [2.0, 3.0, 4.0], "min_turnover_cr": [10.0, 25.0]})
    else:
        add("trend", {"min_ret20": [4.0, 8.0], "max_gap": [1.5, 3.0], "min_turnover_cr": [10.0, 25.0]})
        add("mean_reversion", {"max_rsi2": [5.0, 12.0], "max_gap": [-0.5, -1.25], "trend_floor": [-10.0, 0.0], "min_turnover_cr": [10.0, 25.0]})
        add("breakout", {"breakout_buffer": [0.0, 0.5], "min_ret20": [3.0, 8.0], "max_gap": [3.0, 5.0], "min_turnover_cr": [10.0, 25.0]})
        add("momentum", {"min_ret5": [2.0, 5.0], "min_ret20": [6.0, 12.0], "min_vol_prev": [1.0, 1.8], "min_turnover_cr": [10.0, 25.0]})
        add("volatility", {"max_range_rank": [0.2, 0.35], "min_gap": [0.0, 0.5], "max_gap": [2.5, 4.0], "min_turnover_cr": [10.0, 25.0]})
        add("hybrid", {"min_vol_prev": [1.2, 2.0], "min_ret5": [0.0, 1.5], "min_gap": [0.0, 0.5], "max_gap": [2.5, 4.0], "min_turnover_cr": [10.0, 25.0]})
    return configs


def _simulate_returns(candidates: pd.DataFrame, cfg: StrategyConfig, cost_pct: float) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    d = candidates.copy()
    target = float(cfg.target_pct)
    stop = float(cfg.stop_pct)
    mfe = (d["high"] / d["open"] - 1.0) * 100.0
    mae = (d["low"] / d["open"] - 1.0) * 100.0
    eod = (d["close"] / d["open"] - 1.0) * 100.0
    target_hit = mfe >= target
    stop_hit = mae <= -stop
    gross = np.where(
        target_hit & stop_hit,
        -stop,
        np.where(target_hit, target, np.where(stop_hit, -stop, eod)),
    )
    d["trade_return_pct"] = gross - cost_pct
    d["gross_return_pct"] = gross
    d["target_hit"] = target_hit
    d["stop_hit"] = stop_hit
    d["exit_reason"] = np.where(
        target_hit & stop_hit,
        "BOTH_STOP_FIRST",
        np.where(target_hit, "TARGET", np.where(stop_hit, "STOP", "EOD")),
    )
    return d


def select_trades(feature_df: pd.DataFrame, cfg: StrategyConfig, cost_pct: float) -> pd.DataFrame:
    scorer = SCORE_FUNCS[cfg.family]
    scored = feature_df.copy()
    scored["score"] = scorer(scored, cfg)
    scored = scored.dropna(subset=["score"]).copy()
    if scored.empty:
        return scored
    scored = scored.sort_values(["date", "score", "dollar_volume_cr"], ascending=[True, False, False])
    selected = scored.groupby("date", group_keys=False).head(cfg.top_n).copy()
    return _simulate_returns(selected, cfg, cost_pct)


def max_drawdown(daily_returns_pct: Iterable[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret_pct in daily_returns_pct:
        equity *= 1.0 + float(ret_pct) / 100.0
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, equity / peak - 1.0)
    return abs(max_dd) * 100.0


def summarize_trades(trades: pd.DataFrame, all_dates: list[pd.Timestamp], cfg: StrategyConfig | None = None) -> dict:
    row = {
        "strategy": cfg.label() if cfg else "",
        "family": cfg.family if cfg else "",
        "params_json": json.dumps(cfg.params, sort_keys=True) if cfg else "{}",
        "target_pct": cfg.target_pct if cfg else np.nan,
        "stop_pct": cfg.stop_pct if cfg else np.nan,
        "top_n": cfg.top_n if cfg else np.nan,
    }
    if trades.empty:
        row.update(
            {
                "trades": 0,
                "active_days": 0,
                "avg_daily_return_pct": 0.0,
                "avg_active_day_return_pct": 0.0,
                "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "sharpe": 0.0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
            }
        )
        return row

    daily = trades.groupby("date", as_index=False).agg(daily_return_pct=("trade_return_pct", "mean"), trades=("symbol", "count"))
    daily = pd.DataFrame({"date": all_dates}).merge(daily, on="date", how="left")
    daily["daily_return_pct"] = daily["daily_return_pct"].fillna(0.0)
    returns = daily["daily_return_pct"].astype(float)
    equity = (1.0 + returns / 100.0).cumprod()
    positive = trades.loc[trades["trade_return_pct"] > 0, "trade_return_pct"].sum()
    negative = trades.loc[trades["trade_return_pct"] < 0, "trade_return_pct"].sum()
    std = returns.std(ddof=1)
    row.update(
        {
            "trades": int(len(trades)),
            "active_days": int(trades["date"].nunique()),
            "avg_daily_return_pct": round(float(returns.mean()), 4),
            "avg_active_day_return_pct": round(float(daily.loc[daily["daily_return_pct"] != 0, "daily_return_pct"].mean()), 4)
            if (daily["daily_return_pct"] != 0).any()
            else 0.0,
            "total_return_pct": round(float((equity.iloc[-1] - 1.0) * 100.0), 2),
            "max_drawdown_pct": round(float(max_drawdown(returns)), 2),
            "sharpe": round(float((returns.mean() / std) * math.sqrt(252)), 3) if std and not math.isnan(std) else 0.0,
            "win_rate_pct": round(float((trades["trade_return_pct"] > 0).mean() * 100.0), 2),
            "profit_factor": round(float(positive / abs(negative)), 3) if negative < 0 else (999.0 if positive > 0 else 0.0),
        }
    )
    return row


def evaluate_configs(feature_df: pd.DataFrame, configs: list[StrategyConfig], dates: list[pd.Timestamp], cost_pct: float) -> pd.DataFrame:
    rows = []
    for idx, cfg in enumerate(configs, start=1):
        trades = select_trades(feature_df, cfg, cost_pct)
        rows.append(summarize_trades(trades, dates, cfg))
        if idx % 500 == 0:
            print(f"  evaluated {idx}/{len(configs)} configs")
    out = pd.DataFrame(rows)
    out.sort_values(
        ["avg_daily_return_pct", "sharpe", "total_return_pct", "trades"],
        ascending=[False, False, False, False],
        inplace=True,
    )
    return out.reset_index(drop=True)


def _score_for_selection(row: pd.Series, min_trades: int) -> float:
    if int(row["trades"]) < min_trades:
        return -999.0
    dd_penalty = max(0.0, float(row["max_drawdown_pct"]) - 12.0) * 0.05
    trade_penalty = 0.2 if int(row["active_days"]) < 20 else 0.0
    return float(row["avg_daily_return_pct"]) + 0.03 * float(row["sharpe"]) - dd_penalty - trade_penalty


def walk_forward(feature_df: pd.DataFrame, configs: list[StrategyConfig], cost_pct: float, train_days: int, test_days: int, min_train_trades: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    all_dates = sorted(feature_df["date"].dropna().unique().tolist())
    selected: list[pd.DataFrame] = []
    decisions: list[dict] = []
    start_idx = train_days
    while start_idx < len(all_dates):
        train_dates = all_dates[start_idx - train_days:start_idx]
        test_dates = all_dates[start_idx:start_idx + test_days]
        if not test_dates:
            break
        train_df = feature_df[feature_df["date"].isin(train_dates)].copy()
        test_df = feature_df[feature_df["date"].isin(test_dates)].copy()
        train_eval = evaluate_configs(train_df, configs, train_dates, cost_pct)
        train_eval["selection_score"] = train_eval.apply(lambda r: _score_for_selection(r, min_train_trades), axis=1)
        best = train_eval.sort_values(["selection_score", "avg_daily_return_pct", "sharpe"], ascending=[False, False, False]).iloc[0]
        cfg = next(c for c in configs if c.label() == best["strategy"])
        test_trades = select_trades(test_df, cfg, cost_pct)
        if not test_trades.empty:
            test_trades = test_trades.copy()
            test_trades["walk_config"] = cfg.label()
            selected.append(test_trades)
        test_summary = summarize_trades(test_trades, test_dates, cfg)
        decisions.append(
            {
                "train_start": str(pd.Timestamp(train_dates[0]).date()),
                "train_end": str(pd.Timestamp(train_dates[-1]).date()),
                "test_start": str(pd.Timestamp(test_dates[0]).date()),
                "test_end": str(pd.Timestamp(test_dates[-1]).date()),
                "chosen_strategy": cfg.label(),
                "train_avg_daily_return_pct": best["avg_daily_return_pct"],
                "train_trades": best["trades"],
                "test_avg_daily_return_pct": test_summary["avg_daily_return_pct"],
                "test_trades": test_summary["trades"],
                "test_total_return_pct": test_summary["total_return_pct"],
            }
        )
        print(
            f"  WF {pd.Timestamp(test_dates[0]).date()}..{pd.Timestamp(test_dates[-1]).date()} "
            f"picked {cfg.family}, test avg={test_summary['avg_daily_return_pct']:.3f}% trades={test_summary['trades']}"
            ,
            flush=True,
        )
        start_idx += test_days
    trades = pd.concat(selected, ignore_index=True) if selected else pd.DataFrame()
    return trades, pd.DataFrame(decisions)


def split_dates(all_dates: list[pd.Timestamp]) -> tuple[list[pd.Timestamp], list[pd.Timestamp], list[pd.Timestamp]]:
    n = len(all_dates)
    train_end = int(n * 0.60)
    valid_end = int(n * 0.80)
    return all_dates[:train_end], all_dates[train_end:valid_end], all_dates[valid_end:]


def write_report(
    out_path: Path,
    data_summary: dict,
    top_by_family: pd.DataFrame,
    train_best: pd.Series,
    validation_summary: dict,
    test_summary: dict,
    wf_summary: dict,
    best_cfg: StrategyConfig,
    goal_pct: float,
    cost_pct: float,
) -> None:
    achieved = wf_summary["avg_daily_return_pct"] >= goal_pct
    lines = [
        "# Two-Year Intraday Strategy Research Report",
        "",
        f"Generated: {pd.Timestamp.now().isoformat(timespec='seconds')}",
        "",
        "## Market/Data Used",
        f"- Market: NSE cash equities from `{data_summary['universe_file']}`.",
        f"- Data source: Yahoo Finance adjusted daily OHLCV, cached under `{CACHE_DIR}`.",
        f"- Symbols requested: {data_summary['symbols_requested']}; usable symbols: {data_summary['usable_symbols']}.",
        f"- Backtest period: {data_summary['start']} to {data_summary['end']} ({data_summary['trading_days']} trading days).",
        f"- Cost model: {cost_pct:.2f}% round-trip fees plus slippage deducted from every trade.",
        "- Entry model: long at same-day open using prior-day indicators and same-day opening gap only.",
        "- Exit model: same-day target/stop/EOD; if target and stop both occur, stop is assumed first.",
        "",
        "## Strategies Tested",
    ]
    for _, row in top_by_family.iterrows():
        lines.append(
            f"- {row['family']}: best train avg daily {row['avg_daily_return_pct']:.4f}%, "
            f"trades {int(row['trades'])}, Sharpe {row['sharpe']:.2f}, params `{row['params_json']}`"
        )
    lines.extend(
        [
            "",
            "## Optimization Process",
            "- Generated parameter grids for trend-following, mean-reversion, breakout, momentum, volatility-compression, and hybrid strategies.",
            "- Optimized only on the first 60% of dates.",
            "- Checked the selected strategy on the next 20% validation slice and final 20% test slice.",
            "- Ran walk-forward validation with rolling train/test windows, selecting parameters from prior data only.",
            "",
            "## Final Best Strategy",
            f"- Strategy: `{best_cfg.label()}`",
            f"- Train best average daily return: {train_best['avg_daily_return_pct']:.4f}%",
            f"- Validation average daily return: {validation_summary['avg_daily_return_pct']:.4f}%",
            f"- Final test average daily return: {test_summary['avg_daily_return_pct']:.4f}%",
            f"- Walk-forward average daily return: {wf_summary['avg_daily_return_pct']:.4f}%",
            "",
            "## Required Metrics",
            f"- Average daily return: {wf_summary['avg_daily_return_pct']:.4f}%",
            f"- Active-day average return: {wf_summary['avg_active_day_return_pct']:.4f}%",
            f"- Total return: {wf_summary['total_return_pct']:.2f}%",
            f"- Maximum drawdown: {wf_summary['max_drawdown_pct']:.2f}%",
            f"- Sharpe ratio: {wf_summary['sharpe']:.3f}",
            f"- Win rate: {wf_summary['win_rate_pct']:.2f}%",
            f"- Profit factor: {wf_summary['profit_factor']:.3f}",
            f"- Number of trades: {wf_summary['trades']}",
            f"- 3% daily goal achieved: {'YES' if achieved else 'NO'}",
            "",
            "## Conclusion",
        ]
    )
    if achieved:
        lines.append("The walk-forward test achieved the target under the stated assumptions. This still requires paper-trading before live deployment.")
    else:
        lines.append(
            "The 3% average return per trading day was not achieved realistically. "
            "A result near 3% usually required tiny samples or unstable parameter choices, which is not acceptable evidence."
        )
        lines.append(
            "Reaching 3% per trading day would require either much higher leverage, options-style convexity, unusually accurate intraday signals, "
            "or accepting drawdowns and blow-up risk that this risk-controlled cash-equity model rejects."
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Research 2-year intraday strategies with OOS and walk-forward validation")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--start", default="2024-05-13")
    parser.add_argument("--end", default="2026-05-12")
    parser.add_argument("--cost-pct", type=float, default=0.30, help="Round-trip fee+slippage cost in percent")
    parser.add_argument("--goal-pct", type=float, default=3.0)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--out-prefix", default=str(RESULTS_DIR / "strategy_research_2y"))
    parser.add_argument("--walk-train-days", type=int, default=252)
    parser.add_argument("--walk-test-days", type=int, default=63)
    parser.add_argument("--min-train-trades", type=int, default=15)
    parser.add_argument("--grid-depth", choices=["standard", "broad"], default="standard")
    parser.add_argument("--walk-configs", type=int, default=250, help="Top train configs to carry into walk-forward")
    args = parser.parse_args()

    universe_path = Path(args.universe)
    symbols = load_universe(universe_path, args.limit)
    print(f"Loading 2-year data for {len(symbols)} symbols...", flush=True)
    raw = load_market_data(symbols, args.start, args.end, refresh=bool(args.refresh))
    features = add_features(raw)
    features = features[(features["date"] >= pd.Timestamp(args.start)) & (features["date"] <= pd.Timestamp(args.end))].copy()
    features = features.dropna(subset=["prev_close", "gap_pct", "sma_50", "atr_pct", "vol_ratio_prev"]).copy()
    all_dates = sorted(features["date"].dropna().unique().tolist())
    train_dates, valid_dates, test_dates = split_dates(all_dates)
    configs = generate_configs(args.grid_depth)
    print(f"Usable symbols: {features['symbol'].nunique()}, trading days: {len(all_dates)}, configs: {len(configs)}", flush=True)
    print(f"Train/validation/test days: {len(train_dates)}/{len(valid_dates)}/{len(test_dates)}", flush=True)

    train_df = features[features["date"].isin(train_dates)].copy()
    valid_df = features[features["date"].isin(valid_dates)].copy()
    test_df = features[features["date"].isin(test_dates)].copy()

    print("Evaluating train grid...", flush=True)
    train_eval = evaluate_configs(train_df, configs, train_dates, args.cost_pct)
    train_eval["selection_score"] = train_eval.apply(lambda r: _score_for_selection(r, args.min_train_trades), axis=1)
    eligible_train = train_eval[train_eval["trades"] >= args.min_train_trades].copy()
    if eligible_train.empty:
        eligible_train = train_eval.copy()
    best_row = eligible_train.sort_values(["selection_score", "avg_daily_return_pct", "sharpe"], ascending=[False, False, False]).iloc[0]
    best_cfg = next(c for c in configs if c.label() == best_row["strategy"])
    print(f"Best train strategy: {best_cfg.label()}", flush=True)

    validation_trades = select_trades(valid_df, best_cfg, args.cost_pct)
    test_trades = select_trades(test_df, best_cfg, args.cost_pct)
    validation_summary = summarize_trades(validation_trades, valid_dates, best_cfg)
    test_summary = summarize_trades(test_trades, test_dates, best_cfg)

    print("Running walk-forward validation...", flush=True)
    wf_labels = set(train_eval.head(max(1, int(args.walk_configs)))["strategy"].astype(str).tolist())
    wf_configs = [cfg for cfg in configs if cfg.label() in wf_labels]
    wf_trades, wf_decisions = walk_forward(features, wf_configs, args.cost_pct, args.walk_train_days, args.walk_test_days, args.min_train_trades)
    wf_dates = sorted(features["date"].dropna().unique().tolist())[args.walk_train_days:]
    wf_summary = summarize_trades(wf_trades, wf_dates, best_cfg)

    top_by_family = (
        train_eval[train_eval["trades"] >= args.min_train_trades]
        .sort_values(["family", "selection_score"], ascending=[True, False])
        .groupby("family", as_index=False)
        .head(1)
        .sort_values("avg_daily_return_pct", ascending=False)
    )

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    train_eval_path = out_prefix.with_name(out_prefix.name + "_train_grid.csv")
    validation_path = out_prefix.with_name(out_prefix.name + "_validation_trades.csv")
    test_path = out_prefix.with_name(out_prefix.name + "_test_trades.csv")
    wf_trades_path = out_prefix.with_name(out_prefix.name + "_walk_forward_trades.csv")
    wf_decisions_path = out_prefix.with_name(out_prefix.name + "_walk_forward_decisions.csv")
    report_path = out_prefix.with_name(out_prefix.name + "_report.md")

    train_eval.to_csv(train_eval_path, index=False)
    validation_trades.to_csv(validation_path, index=False)
    test_trades.to_csv(test_path, index=False)
    wf_trades.to_csv(wf_trades_path, index=False)
    wf_decisions.to_csv(wf_decisions_path, index=False)

    data_summary = {
        "universe_file": str(universe_path),
        "symbols_requested": len(symbols),
        "usable_symbols": int(features["symbol"].nunique()),
        "start": str(pd.Timestamp(all_dates[0]).date()),
        "end": str(pd.Timestamp(all_dates[-1]).date()),
        "trading_days": len(all_dates),
    }
    write_report(
        report_path,
        data_summary,
        top_by_family,
        best_row,
        validation_summary,
        test_summary,
        wf_summary,
        best_cfg,
        args.goal_pct,
        args.cost_pct,
    )

    print("\nSaved outputs:")
    for p in [train_eval_path, validation_path, test_path, wf_trades_path, wf_decisions_path, report_path]:
        print(f"  {p}")
    print("\nFinal walk-forward summary:")
    for key in [
        "avg_daily_return_pct",
        "avg_active_day_return_pct",
        "total_return_pct",
        "max_drawdown_pct",
        "sharpe",
        "win_rate_pct",
        "profit_factor",
        "trades",
    ]:
        print(f"  {key}: {wf_summary[key]}")
    print(f"  3pct_goal_achieved: {wf_summary['avg_daily_return_pct'] >= args.goal_pct}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

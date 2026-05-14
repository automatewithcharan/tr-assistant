#!/usr/bin/env python3
"""Robust Kite intraday strategy research over audited intraday caches."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.kite_intraday_backfill import INTRADAY_ROOT, bars_per_day, cache_path, interval_minutes  # noqa: E402
from scripts.research_intraday_strategies_2y import RESULTS_DIR, load_universe, max_drawdown, split_dates  # noqa: E402

DEFAULT_UNIVERSE = RESULTS_DIR / "kite_backfill_2y_eligible_kite_complete_universe.csv"
DEFAULT_GATE = RESULTS_DIR / "kite_intraday_2y_quality_gate.json"
SESSION_CLOSE = pd.Timestamp("15:30").time()


@dataclass(frozen=True)
class IntradayConfig:
    family: str
    side: str
    interval: str
    params: dict
    target_pct: float
    stop_pct: float
    max_trades_per_day: int
    max_daily_loss_pct: float
    sizing: str

    def label(self) -> str:
        parts = [self.interval, self.side, self.family, f"sizing={self.sizing}"]
        for key in sorted(self.params):
            value = self.params[key]
            parts.append(f"{key}={value:g}" if isinstance(value, float) else f"{key}={value}")
        parts.extend([f"t={self.target_pct:g}", f"s={self.stop_pct:g}", f"maxd={self.max_trades_per_day}", f"dl={self.max_daily_loss_pct:g}"])
        return "|".join(parts)


@dataclass(frozen=True)
class IntradayDay:
    date: pd.Timestamp
    symbol: str
    orig_index: np.ndarray
    datetime: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    bar_index: np.ndarray
    vwap: np.ndarray
    day_open: float
    prev_close: float
    gap_pct: float
    avg_value20_cr: float
    ret_from_open_pct: np.ndarray
    rel_strength_pct: np.ndarray
    nifty_ret_from_open_pct: np.ndarray
    roll_low3: np.ndarray
    roll_high3: np.ndarray
    rolling_cache: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]


class StageTimer:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def mark(self, stage: str, started: float) -> None:
        self.rows.append({"stage": stage, "seconds": round(time.perf_counter() - started, 3)})

    def write(self, path: Path) -> None:
        pd.DataFrame(self.rows).to_csv(path, index=False)


def _normalise_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _to_datetime(values) -> pd.Series:
    raw = pd.Series(values).astype(str).str.replace(r"(Z|[+-]\d{2}:?\d{2})$", "", regex=True)
    return pd.to_datetime(raw, errors="coerce")


def read_intraday(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    path = cache_path(symbol, interval, start, end)
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if "date" in df.columns and "datetime" not in df.columns:
            df = df.rename(columns={"date": "datetime"})
        df["datetime"] = _to_datetime(df["datetime"])
        keep = ["datetime", "open", "high", "low", "close", "volume", "symbol"]
        if not set(keep).issubset(df.columns):
            return pd.DataFrame()
        df = df[keep].dropna(subset=["datetime", "open", "high", "low", "close"]).copy()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["symbol"] = _normalise_symbol(symbol)
        return df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def require_intraday_gate(gate_file: Path, intervals: list[str], min_complete_pct: float) -> dict:
    if not gate_file.exists():
        raise RuntimeError(f"Intraday quality gate missing: {gate_file}. Run scripts/kite_intraday_backfill.py first.")
    gate = json.loads(gate_file.read_text(encoding="utf-8"))
    missing = []
    for interval in intervals:
        row = gate.get("intervals", {}).get(interval, {})
        if not row.get("gate_pass") or float(row.get("complete_pct", 0.0)) < min_complete_pct or not row.get("nifty_complete"):
            missing.append(f"{interval}: gate_pass={row.get('gate_pass')} complete_pct={row.get('complete_pct')} nifty={row.get('nifty_complete')}")
    if missing:
        raise RuntimeError("Intraday quality gate failed: " + "; ".join(missing))
    return gate


def indian_intraday_fee_pct(brokerage_pct_per_side: float = 0.03) -> float:
    brokerage = min(float(brokerage_pct_per_side), 0.03) * 2
    stt_sell = 0.025
    exchange_txn = 0.00345 * 2
    sebi = 0.0001 * 2
    stamp_buy = 0.003
    gst = 0.18 * (brokerage + exchange_txn)
    return brokerage + stt_sell + exchange_txn + sebi + stamp_buy + gst


def add_intraday_features(df: pd.DataFrame, nifty: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["date"] = work["datetime"].dt.normalize()
    work["time"] = work["datetime"].dt.time
    work = work.sort_values(["symbol", "datetime"]).reset_index(drop=True)
    if work.empty:
        return work
    intraday_group = work.groupby(["symbol", "date"], sort=False)
    work["bar_index"] = intraday_group.cumcount()
    typical = (work["high"] + work["low"] + work["close"]) / 3.0
    value = typical * work["volume"]
    cum_value = value.groupby([work["symbol"], work["date"]], sort=False).cumsum()
    cum_vol = work["volume"].replace(0, np.nan).groupby([work["symbol"], work["date"]], sort=False).cumsum()
    work["vwap"] = cum_value / cum_vol
    work["day_open"] = intraday_group["open"].transform("first")
    work["ret_from_open_pct"] = (work["close"] / work["day_open"] - 1.0) * 100.0
    work["range_from_open_pct"] = (intraday_group["high"].cummax() / intraday_group["low"].cummin() - 1.0) * 100.0
    work["cum_volume"] = intraday_group["volume"].cumsum()
    work["intraday_value_cr"] = (work["close"] * work["volume"]).groupby([work["symbol"], work["date"]], sort=False).cumsum() / 1e7

    daily = (
        work.groupby(["symbol", "date"], sort=False)
        .agg(day_open=("open", "first"), day_close=("close", "last"), day_value_cr=("intraday_value_cr", "last"))
        .reset_index()
        .sort_values(["symbol", "date"])
    )
    daily["prev_close"] = daily.groupby("symbol")["day_close"].shift(1)
    daily["gap_pct"] = (daily["day_open"] / daily["prev_close"] - 1.0) * 100.0
    daily["avg_value20_cr"] = daily.groupby("symbol")["day_value_cr"].transform(lambda s: s.shift(1).rolling(20).median())
    work = work.merge(daily[["symbol", "date", "prev_close", "gap_pct", "avg_value20_cr"]], on=["symbol", "date"], how="left")

    if not nifty.empty:
        n = nifty.copy()
        n["date"] = n["datetime"].dt.normalize()
        n = n.sort_values(["date", "datetime"]).copy()
        n["nifty_open"] = n.groupby("date", sort=False)["open"].transform("first")
        n["nifty_ret_from_open_pct"] = (n["close"] / n["nifty_open"] - 1.0) * 100.0
        n = n[["datetime", "nifty_ret_from_open_pct", "close"]].rename(columns={"close": "nifty_close"})
        work = work.merge(n, on="datetime", how="left")
    else:
        work["nifty_ret_from_open_pct"] = np.nan
        work["nifty_close"] = np.nan
    work["rel_strength_pct"] = work["ret_from_open_pct"] - work["nifty_ret_from_open_pct"].fillna(0.0)
    return work.replace([np.inf, -np.inf], np.nan)


def _rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(values).rolling(window).min().to_numpy()


def _rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(values).rolling(window).max().to_numpy()


def prepare_day_cache(features: pd.DataFrame) -> list[IntradayDay]:
    cols = [
        "datetime", "open", "high", "low", "close", "volume", "bar_index", "vwap",
        "ret_from_open_pct", "rel_strength_pct", "nifty_ret_from_open_pct",
    ]
    days: list[IntradayDay] = []
    for (date, symbol), day in features.groupby(["date", "symbol"], sort=False):
        g = day.sort_values("datetime")
        data = {col: g[col].to_numpy() for col in cols}
        high = data["high"].astype(float, copy=False)
        low = data["low"].astype(float, copy=False)
        rolling_cache: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for window in {3, 4, 9, 12}:
            roll_high = _rolling_max(high, window)
            roll_low = _rolling_min(low, window)
            range_pct = (roll_high / roll_low - 1.0) * 100.0
            rolling_cache[window] = (roll_high, roll_low, range_pct)
        days.append(
            IntradayDay(
                date=pd.Timestamp(date),
                symbol=str(symbol),
                orig_index=g.index.to_numpy(),
                datetime=data["datetime"],
                open=data["open"].astype(float, copy=False),
                high=high,
                low=low,
                close=data["close"].astype(float, copy=False),
                volume=data["volume"].astype(float, copy=False),
                bar_index=data["bar_index"].astype(int, copy=False),
                vwap=data["vwap"].astype(float, copy=False),
                day_open=float(g["day_open"].iloc[0]),
                prev_close=float(g["prev_close"].iloc[0]) if not pd.isna(g["prev_close"].iloc[0]) else np.nan,
                gap_pct=float(g["gap_pct"].iloc[0]) if not pd.isna(g["gap_pct"].iloc[0]) else 0.0,
                avg_value20_cr=float(g["avg_value20_cr"].iloc[0]) if not pd.isna(g["avg_value20_cr"].iloc[0]) else 0.0,
                ret_from_open_pct=data["ret_from_open_pct"].astype(float, copy=False),
                rel_strength_pct=data["rel_strength_pct"].astype(float, copy=False),
                nifty_ret_from_open_pct=data["nifty_ret_from_open_pct"].astype(float, copy=False),
                roll_low3=rolling_cache[3][1],
                roll_high3=rolling_cache[3][0],
                rolling_cache=rolling_cache,
            )
        )
    return days


def simulate_exit(day: pd.DataFrame, entry_pos: int, side: str, entry_price: float, target_pct: float, stop_pct: float) -> tuple[pd.Timestamp, float, str]:
    after = day.iloc[entry_pos:].copy()
    if side == "long":
        target = entry_price * (1.0 + target_pct / 100.0)
        stop = entry_price * (1.0 - stop_pct / 100.0)
        for _, bar in after.iterrows():
            hit_stop = float(bar["low"]) <= stop
            hit_target = float(bar["high"]) >= target
            if hit_stop:
                return bar["datetime"], stop, "stop"
            if hit_target:
                return bar["datetime"], target, "target"
    else:
        target = entry_price * (1.0 - target_pct / 100.0)
        stop = entry_price * (1.0 + stop_pct / 100.0)
        for _, bar in after.iterrows():
            hit_stop = float(bar["high"]) >= stop
            hit_target = float(bar["low"]) <= target
            if hit_stop:
                return bar["datetime"], stop, "stop"
            if hit_target:
                return bar["datetime"], target, "target"
    last = after.iloc[-1]
    return last["datetime"], float(last["close"]), "eod"


def liquidity_slippage_pct(avg_value_cr: float) -> float:
    if avg_value_cr >= 200:
        return 0.04
    if avg_value_cr >= 75:
        return 0.07
    if avg_value_cr >= 25:
        return 0.11
    return 0.18


def simulate_exit_fast(day: IntradayDay, entry_pos: int, side: str, entry_price: float, target_pct: float, stop_pct: float) -> tuple[pd.Timestamp, float, str]:
    if side == "long":
        target = entry_price * (1.0 + target_pct / 100.0)
        stop = entry_price * (1.0 - stop_pct / 100.0)
        stop_hits = np.flatnonzero(day.low[entry_pos:] <= stop)
        target_hits = np.flatnonzero(day.high[entry_pos:] >= target)
    else:
        target = entry_price * (1.0 - target_pct / 100.0)
        stop = entry_price * (1.0 + stop_pct / 100.0)
        stop_hits = np.flatnonzero(day.high[entry_pos:] >= stop)
        target_hits = np.flatnonzero(day.low[entry_pos:] <= target)
    first_stop = int(stop_hits[0]) if len(stop_hits) else None
    first_target = int(target_hits[0]) if len(target_hits) else None
    if first_stop is not None and (first_target is None or first_stop <= first_target):
        pos = entry_pos + first_stop
        return pd.Timestamp(day.datetime[pos]), stop, "stop"
    if first_target is not None:
        pos = entry_pos + first_target
        return pd.Timestamp(day.datetime[pos]), target, "target"
    return pd.Timestamp(day.datetime[-1]), float(day.close[-1]), "eod"


def detect_lookahead_trap(
    family: str,
    side: str,
    candidates: np.ndarray,
    score: np.ndarray,
    datetimes: np.ndarray | None = None,
) -> dict:
    """Return a diagnostic dict revealing whether argmax-based selection would differ
    from chronological (first-qualifying-bar) selection.

    A non-zero divergence rate is the signature of a lookahead trap:
      - argmax picks the highest-scoring bar across the window
      - if score is correlated with future price accumulation (e.g. ret_from_open_pct),
        argmax will reliably select the intraday extreme — not the first live entry point

    This function is used in evaluate_config_fast() to tag strategies for auditing.
    It is also called directly from regression tests.
    """
    qualifying = np.flatnonzero(candidates)
    if len(qualifying) == 0:
        return {"family": family, "side": side, "n_qualifying": 0, "argmax_is_first": True, "score_time_corr": 0.0}

    first_idx = int(qualifying[0])
    argmax_idx = int(qualifying[np.argmax(score[qualifying])])
    argmax_is_first = first_idx == argmax_idx

    # Rank correlation between bar position (proxy for time) and score — detects if
    # later qualifying bars systematically score higher (lookahead signature)
    score_time_corr = 0.0
    if len(qualifying) > 2:
        positions = qualifying.astype(float)
        scores = score[qualifying].astype(float)
        # Spearman rank correlation (manual — avoids scipy dependency)
        pos_ranks = np.argsort(np.argsort(positions)).astype(float)
        score_ranks = np.argsort(np.argsort(scores)).astype(float)
        n = float(len(qualifying))
        cov = float(np.mean((pos_ranks - pos_ranks.mean()) * (score_ranks - score_ranks.mean())))
        std_p = float(np.std(pos_ranks))
        std_s = float(np.std(score_ranks))
        score_time_corr = cov / (std_p * std_s) if std_p > 0 and std_s > 0 else 0.0

    return {
        "family": family,
        "side": side,
        "n_qualifying": int(len(qualifying)),
        "argmax_is_first": bool(argmax_is_first),
        "first_bar_pos": first_idx,
        "argmax_bar_pos": argmax_idx,
        "score_time_corr": round(score_time_corr, 4),
    }


def best_signal_fast(day: IntradayDay, cfg: IntradayConfig) -> tuple[int, float] | None:
    p = cfg.params
    n = len(day.close)
    if n < max(10, bars_per_day(cfg.interval) * 0.6):
        return None
    min_price = float(p.get("min_price", 40.0))
    min_value = float(p.get("min_value_cr", 25.0))
    if day.day_open < min_price or day.avg_value20_cr < min_value:
        return None

    open_bars = max(1, int(float(p.get("opening_minutes", 30)) / interval_minutes(cfg.interval)))
    open_end = min(open_bars, n)
    or_high = float(np.nanmax(day.high[:open_end]))
    or_low = float(np.nanmin(day.low[:open_end]))
    prev_close = day.prev_close
    gap = day.gap_pct
    post_open = day.bar_index >= open_bars
    candidates = np.zeros(n, dtype=bool)
    score = np.zeros(n, dtype=float)

    if cfg.family == "opening_range_breakout":
        candidates = post_open & (day.close > or_high * (1 + float(p.get("buffer_pct", 0.05)) / 100.0)) & (day.rel_strength_pct >= float(p.get("min_rs", 0.0)))
        score = (day.close / or_high - 1.0) * 100.0 + day.rel_strength_pct
    elif cfg.family == "opening_range_breakdown":
        candidates = post_open & (day.close < or_low * (1 - float(p.get("buffer_pct", 0.05)) / 100.0)) & (day.rel_strength_pct <= -float(p.get("min_rs", 0.0)))
        score = (or_low / day.close - 1.0) * 100.0 - day.rel_strength_pct
    elif cfg.family == "vwap_mean_reversion":
        dev = float(p.get("dev_pct", 0.6))
        if cfg.side == "long":
            candidates = post_open & (day.close < day.vwap * (1 - dev / 100.0)) & (day.ret_from_open_pct < -dev)
            score = (day.vwap / day.close - 1.0) * 100.0
        else:
            candidates = post_open & (day.close > day.vwap * (1 + dev / 100.0)) & (day.ret_from_open_pct > dev)
            score = (day.close / day.vwap - 1.0) * 100.0
    elif cfg.family == "vwap_trend_continuation":
        dev = float(p.get("dev_pct", 0.15))
        if cfg.side == "long":
            candidates = post_open & (day.close > day.vwap * (1 + dev / 100.0)) & (day.close > or_high) & (day.nifty_ret_from_open_pct >= float(p.get("min_nifty", -0.2)))
            score = day.rel_strength_pct + (day.close / day.vwap - 1.0) * 100.0
        else:
            candidates = post_open & (day.close < day.vwap * (1 - dev / 100.0)) & (day.close < or_low) & (day.nifty_ret_from_open_pct <= -float(p.get("min_nifty", -0.2)))
            score = -day.rel_strength_pct + (day.vwap / day.close - 1.0) * 100.0
    elif cfg.family == "gap_fade":
        min_gap = float(p.get("min_gap_pct", 1.0))
        if cfg.side == "long":
            candidates = post_open & (gap <= -min_gap) & (day.close > or_low) & (day.close < prev_close)
            score = abs(gap) + (day.close / or_low - 1.0) * 100.0
        else:
            candidates = post_open & (gap >= min_gap) & (day.close < or_high) & (day.close > prev_close)
            score = abs(gap) + (or_high / day.close - 1.0) * 100.0
    elif cfg.family == "gap_continuation":
        min_gap = float(p.get("min_gap_pct", 0.75))
        if cfg.side == "long":
            candidates = post_open & (gap >= min_gap) & (day.close > or_high) & (day.nifty_ret_from_open_pct >= float(p.get("min_nifty", -0.1)))
            score = gap + day.rel_strength_pct
        else:
            candidates = post_open & (gap <= -min_gap) & (day.close < or_low) & (day.nifty_ret_from_open_pct <= -float(p.get("min_nifty", -0.1)))
            score = abs(gap) - day.rel_strength_pct
    elif cfg.family == "relative_strength":
        threshold = float(p.get("rs_threshold", 0.8))
        if cfg.side == "long":
            candidates = post_open & (day.rel_strength_pct >= threshold) & (day.close > day.vwap)
            score = day.rel_strength_pct
        else:
            candidates = post_open & (day.rel_strength_pct <= -threshold) & (day.close < day.vwap)
            score = -day.rel_strength_pct
    elif cfg.family == "first_hour_momentum":
        bars = max(open_bars, int(60 / interval_minutes(cfg.interval)))
        candidates = day.bar_index == bars
        if cfg.side == "long":
            candidates = candidates & (day.ret_from_open_pct >= float(p.get("ret_threshold", 1.0))) & (day.close > day.vwap)
            score = day.ret_from_open_pct + day.rel_strength_pct
        else:
            candidates = candidates & (day.ret_from_open_pct <= -float(p.get("ret_threshold", 1.0))) & (day.close < day.vwap)
            score = -day.ret_from_open_pct - day.rel_strength_pct
    elif cfg.family == "late_day_reversal":
        late_bar = max(1, int(float(p.get("late_minutes", 300)) / interval_minutes(cfg.interval)))
        candidates = day.bar_index >= late_bar
        move = float(p.get("move_pct", 1.6))
        if cfg.side == "long":
            candidates = candidates & (day.ret_from_open_pct <= -move) & (day.close > day.roll_low3)
            score = -day.ret_from_open_pct
        else:
            candidates = candidates & (day.ret_from_open_pct >= move) & (day.close < day.roll_high3)
            score = day.ret_from_open_pct
    elif cfg.family == "volatility_contraction_breakout":
        lookback = max(3, int(float(p.get("lookback_minutes", 60)) / interval_minutes(cfg.interval)))
        roll_high, roll_low, range_pct = day.rolling_cache.get(lookback, (None, None, None))
        if roll_high is None:
            roll_high = _rolling_max(day.high, lookback)
            roll_low = _rolling_min(day.low, lookback)
            range_pct = (roll_high / roll_low - 1.0) * 100.0
        tight = range_pct <= float(p.get("max_range_pct", 0.9))
        inv_range = np.divide(1.0, range_pct, out=np.zeros_like(range_pct, dtype=float), where=(range_pct != 0) & ~np.isnan(range_pct))
        if cfg.side == "long":
            candidates = post_open & tight & (day.close >= roll_high) & (day.close > day.vwap)
            score = inv_range + day.rel_strength_pct
        else:
            candidates = post_open & tight & (day.close <= roll_low) & (day.close < day.vwap)
            score = inv_range - day.rel_strength_pct
    else:
        raise ValueError(f"Unknown family: {cfg.family}")

    candidates = candidates & ~np.isnan(score)
    if not np.any(candidates):
        return None
    masked_score = np.where(candidates, score, -np.inf)
    max_score = float(np.nanmax(masked_score))
    tied = candidates & (masked_score == max_score)
    if int(tied.sum()) > 1:
        # Rare path: delegate tied ordering back to the legacy pandas implementation.
        legacy_day = pd.DataFrame(
            {
                "datetime": day.datetime,
                "open": day.open,
                "high": day.high,
                "low": day.low,
                "close": day.close,
                "volume": day.volume,
                "bar_index": day.bar_index,
                "vwap": day.vwap,
                "day_open": day.day_open,
                "prev_close": day.prev_close,
                "gap_pct": day.gap_pct,
                "avg_value20_cr": day.avg_value20_cr,
                "ret_from_open_pct": day.ret_from_open_pct,
                "rel_strength_pct": day.rel_strength_pct,
                "nifty_ret_from_open_pct": day.nifty_ret_from_open_pct,
            },
            index=day.orig_index,
        )
        legacy_candidates = signal_candidates(legacy_day, cfg)
        if legacy_candidates.empty:
            return None
        chosen_index = int(legacy_candidates.index[0])
        pos = int(np.flatnonzero(day.orig_index == chosen_index)[0])
    else:
        # First chronological qualifying bar — no future data used.
        # argmax(score) was here before; for late_day_reversal score=ret_from_open_pct
        # which picks the intraday peak using up to 90 min of future price data.
        pos = int(np.flatnonzero(candidates)[0])
    return pos, float(masked_score[pos])


def signal_candidates(day: pd.DataFrame, cfg: IntradayConfig) -> pd.DataFrame:
    p = cfg.params
    g = day.sort_values("datetime").copy()
    if len(g) < max(10, bars_per_day(cfg.interval) * 0.6):
        return pd.DataFrame()
    min_price = float(p.get("min_price", 40.0))
    min_value = float(p.get("min_value_cr", 25.0))
    if float(g["day_open"].iloc[0]) < min_price or float(g["avg_value20_cr"].iloc[0] or 0.0) < min_value:
        return pd.DataFrame()

    open_bars = max(1, int(float(p.get("opening_minutes", 30)) / interval_minutes(cfg.interval)))
    open_slice = g.iloc[: min(open_bars, len(g))]
    or_high = float(open_slice["high"].max())
    or_low = float(open_slice["low"].min())
    day_open = float(g["day_open"].iloc[0])
    prev_close = float(g["prev_close"].iloc[0]) if not pd.isna(g["prev_close"].iloc[0]) else np.nan
    gap = float(g["gap_pct"].iloc[0]) if not pd.isna(g["gap_pct"].iloc[0]) else 0.0
    candidates = pd.Series(False, index=g.index)
    score = pd.Series(0.0, index=g.index)

    post_open = g["bar_index"] >= open_bars
    if cfg.family == "opening_range_breakout":
        candidates = post_open & (g["close"] > or_high * (1 + float(p.get("buffer_pct", 0.05)) / 100.0)) & (g["rel_strength_pct"] >= float(p.get("min_rs", 0.0)))
        score = (g["close"] / or_high - 1.0) * 100.0 + g["rel_strength_pct"]
    elif cfg.family == "opening_range_breakdown":
        candidates = post_open & (g["close"] < or_low * (1 - float(p.get("buffer_pct", 0.05)) / 100.0)) & (g["rel_strength_pct"] <= -float(p.get("min_rs", 0.0)))
        score = (or_low / g["close"] - 1.0) * 100.0 - g["rel_strength_pct"]
    elif cfg.family == "vwap_mean_reversion":
        dev = float(p.get("dev_pct", 0.6))
        if cfg.side == "long":
            candidates = post_open & (g["close"] < g["vwap"] * (1 - dev / 100.0)) & (g["ret_from_open_pct"] < -dev)
            score = (g["vwap"] / g["close"] - 1.0) * 100.0
        else:
            candidates = post_open & (g["close"] > g["vwap"] * (1 + dev / 100.0)) & (g["ret_from_open_pct"] > dev)
            score = (g["close"] / g["vwap"] - 1.0) * 100.0
    elif cfg.family == "vwap_trend_continuation":
        dev = float(p.get("dev_pct", 0.15))
        if cfg.side == "long":
            candidates = post_open & (g["close"] > g["vwap"] * (1 + dev / 100.0)) & (g["close"] > or_high) & (g["nifty_ret_from_open_pct"] >= float(p.get("min_nifty", -0.2)))
            score = g["rel_strength_pct"] + (g["close"] / g["vwap"] - 1.0) * 100.0
        else:
            candidates = post_open & (g["close"] < g["vwap"] * (1 - dev / 100.0)) & (g["close"] < or_low) & (g["nifty_ret_from_open_pct"] <= -float(p.get("min_nifty", -0.2)))
            score = -g["rel_strength_pct"] + (g["vwap"] / g["close"] - 1.0) * 100.0
    elif cfg.family == "gap_fade":
        min_gap = float(p.get("min_gap_pct", 1.0))
        if cfg.side == "long":
            candidates = post_open & (gap <= -min_gap) & (g["close"] > or_low) & (g["close"] < prev_close)
            score = abs(gap) + (g["close"] / or_low - 1.0) * 100.0
        else:
            candidates = post_open & (gap >= min_gap) & (g["close"] < or_high) & (g["close"] > prev_close)
            score = abs(gap) + (or_high / g["close"] - 1.0) * 100.0
    elif cfg.family == "gap_continuation":
        min_gap = float(p.get("min_gap_pct", 0.75))
        if cfg.side == "long":
            candidates = post_open & (gap >= min_gap) & (g["close"] > or_high) & (g["nifty_ret_from_open_pct"] >= float(p.get("min_nifty", -0.1)))
            score = gap + g["rel_strength_pct"]
        else:
            candidates = post_open & (gap <= -min_gap) & (g["close"] < or_low) & (g["nifty_ret_from_open_pct"] <= -float(p.get("min_nifty", -0.1)))
            score = abs(gap) - g["rel_strength_pct"]
    elif cfg.family == "relative_strength":
        threshold = float(p.get("rs_threshold", 0.8))
        if cfg.side == "long":
            candidates = post_open & (g["rel_strength_pct"] >= threshold) & (g["close"] > g["vwap"])
            score = g["rel_strength_pct"]
        else:
            candidates = post_open & (g["rel_strength_pct"] <= -threshold) & (g["close"] < g["vwap"])
            score = -g["rel_strength_pct"]
    elif cfg.family == "first_hour_momentum":
        bars = max(open_bars, int(60 / interval_minutes(cfg.interval)))
        candidates = g["bar_index"].eq(bars)
        if cfg.side == "long":
            candidates = candidates & (g["ret_from_open_pct"] >= float(p.get("ret_threshold", 1.0))) & (g["close"] > g["vwap"])
            score = g["ret_from_open_pct"] + g["rel_strength_pct"]
        else:
            candidates = candidates & (g["ret_from_open_pct"] <= -float(p.get("ret_threshold", 1.0))) & (g["close"] < g["vwap"])
            score = -g["ret_from_open_pct"] - g["rel_strength_pct"]
    elif cfg.family == "late_day_reversal":
        late_bar = max(1, int(float(p.get("late_minutes", 300)) / interval_minutes(cfg.interval)))
        candidates = g["bar_index"] >= late_bar
        move = float(p.get("move_pct", 1.6))
        if cfg.side == "long":
            candidates = candidates & (g["ret_from_open_pct"] <= -move) & (g["close"] > g["low"].rolling(3).min())
            score = -g["ret_from_open_pct"]
        else:
            candidates = candidates & (g["ret_from_open_pct"] >= move) & (g["close"] < g["high"].rolling(3).max())
            score = g["ret_from_open_pct"]
    elif cfg.family == "volatility_contraction_breakout":
        lookback = max(3, int(float(p.get("lookback_minutes", 60)) / interval_minutes(cfg.interval)))
        range_pct = (g["high"].rolling(lookback).max() / g["low"].rolling(lookback).min() - 1.0) * 100.0
        tight = range_pct <= float(p.get("max_range_pct", 0.9))
        if cfg.side == "long":
            candidates = post_open & tight & (g["close"] >= g["high"].rolling(lookback).max()) & (g["close"] > g["vwap"])
            score = (1.0 / range_pct.replace(0, np.nan)).fillna(0) + g["rel_strength_pct"]
        else:
            candidates = post_open & tight & (g["close"] <= g["low"].rolling(lookback).min()) & (g["close"] < g["vwap"])
            score = (1.0 / range_pct.replace(0, np.nan)).fillna(0) - g["rel_strength_pct"]
    else:
        raise ValueError(f"Unknown family: {cfg.family}")

    out = g[candidates.fillna(False)].copy()
    if out.empty:
        return out
    out["signal_score"] = score.loc[out.index].astype(float)
    # Chronological order: take the first qualifying bar in time, not the highest-scoring.
    # Sorting by score picks the intraday extreme (peak for late_day_reversal short),
    # which requires future price data and is not executable in live trading.
    return out.sort_values("datetime")


def select_trades(features: pd.DataFrame, cfg: IntradayConfig, base_fee_pct: float, fee_mult: float = 1.0) -> pd.DataFrame:
    trades = []
    daily_pnl: dict[pd.Timestamp, float] = {}
    for (date, symbol), day in features.groupby(["date", "symbol"], sort=False):
        if daily_pnl.get(date, 0.0) <= -abs(cfg.max_daily_loss_pct):
            continue
        candidates = signal_candidates(day, cfg)
        if candidates.empty:
            continue
        signal = candidates.iloc[0]
        signal_pos = int(day.index.get_loc(signal.name))
        entry_pos = signal_pos + 1
        if entry_pos >= len(day):
            continue
        already = sum(1 for t in trades if t["date"] == date)
        if already >= cfg.max_trades_per_day:
            continue
        entry_bar = day.iloc[entry_pos]
        entry_price = float(entry_bar["open"])
        exit_time, exit_price, exit_reason = simulate_exit(day.reset_index(drop=True), entry_pos, cfg.side, entry_price, cfg.target_pct, cfg.stop_pct)
        gross = (exit_price / entry_price - 1.0) * 100.0 if cfg.side == "long" else (entry_price / exit_price - 1.0) * 100.0
        avg_value = float(signal.get("avg_value20_cr", 0.0) or 0.0)
        costs = base_fee_pct * fee_mult + liquidity_slippage_pct(avg_value)
        net = gross - costs
        daily_pnl[date] = daily_pnl.get(date, 0.0) + net
        trades.append(
            {
                "date": date,
                "symbol": symbol,
                "interval": cfg.interval,
                "family": cfg.family,
                "side": cfg.side,
                "signal_time": signal["datetime"],
                "entry_time": entry_bar["datetime"],
                "exit_time": exit_time,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "gross_return_pct": round(gross, 4),
                "cost_pct": round(costs, 4),
                "trade_return_pct": round(net, 4),
                "exit_reason": exit_reason,
                "score": round(float(signal["signal_score"]), 4),
            }
        )
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame(trades).sort_values(["date", "signal_time"]).reset_index(drop=True)


def select_trades_fast(day_cache: list[IntradayDay], cfg: IntradayConfig, base_fee_pct: float, fee_mult: float = 1.0, allowed_dates: set[pd.Timestamp] | None = None) -> pd.DataFrame:
    trades = []
    daily_pnl: dict[pd.Timestamp, float] = {}
    daily_counts: dict[pd.Timestamp, int] = {}
    for day in day_cache:
        if allowed_dates is not None and day.date not in allowed_dates:
            continue
        if daily_pnl.get(day.date, 0.0) <= -abs(cfg.max_daily_loss_pct):
            continue
        signal = best_signal_fast(day, cfg)
        if signal is None:
            continue
        signal_pos, score = signal
        entry_pos = signal_pos + 1
        if entry_pos >= len(day.open):
            continue
        if daily_counts.get(day.date, 0) >= cfg.max_trades_per_day:
            continue
        entry_price = float(day.open[entry_pos])
        exit_time, exit_price, exit_reason = simulate_exit_fast(day, entry_pos, cfg.side, entry_price, cfg.target_pct, cfg.stop_pct)
        gross = (exit_price / entry_price - 1.0) * 100.0 if cfg.side == "long" else (entry_price / exit_price - 1.0) * 100.0
        costs = base_fee_pct * fee_mult + liquidity_slippage_pct(day.avg_value20_cr)
        net = gross - costs
        daily_pnl[day.date] = daily_pnl.get(day.date, 0.0) + net
        daily_counts[day.date] = daily_counts.get(day.date, 0) + 1
        trades.append(
            {
                "date": day.date,
                "symbol": day.symbol,
                "interval": cfg.interval,
                "family": cfg.family,
                "side": cfg.side,
                "signal_time": pd.Timestamp(day.datetime[signal_pos]),
                "entry_time": pd.Timestamp(day.datetime[entry_pos]),
                "exit_time": exit_time,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "gross_return_pct": round(gross, 4),
                "cost_pct": round(costs, 4),
                "trade_return_pct": round(net, 4),
                "exit_reason": exit_reason,
                "score": round(float(score), 4),
            }
        )
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame(trades).sort_values(["date", "signal_time"]).reset_index(drop=True)


def signal_key(cfg: IntradayConfig) -> tuple:
    return (cfg.interval, cfg.family, cfg.side, tuple(sorted(cfg.params.items())))


def precompute_signals(day_cache: list[IntradayDay], cfg: IntradayConfig) -> list[tuple[IntradayDay, int, float]]:
    rows = []
    for day in day_cache:
        signal = best_signal_fast(day, cfg)
        if signal is not None:
            signal_pos, score = signal
            rows.append((day, signal_pos, score))
    return rows


def measure_lookahead_divergence(day_cache: list[IntradayDay], cfg: IntradayConfig) -> float:
    """Return the fraction of signal days where argmax(score) would pick a different bar
    than first-chronological-qualifying-bar.  A high value (>0.5) is the signature that
    the score function is correlated with future price accumulation (lookahead trap).

    0.0 = score-order and time-order always agree (safe)
    1.0 = score-order always picks a later bar than time-order (strong lookahead trap)
    """
    diverge = 0
    total = 0
    for day in day_cache:
        n = len(day.close)
        if n < max(10, bars_per_day(cfg.interval) * 0.6):
            continue
        if day.day_open < float(cfg.params.get("min_price", 40.0)) or day.avg_value20_cr < float(cfg.params.get("min_value_cr", 25.0)):
            continue
        signal = best_signal_fast(day, cfg)
        if signal is None:
            continue
        chronological_pos = signal[0]  # already chronological from best_signal_fast

        # Reconstruct the score/candidates the same way best_signal_fast does,
        # then find what argmax would have picked.
        p = cfg.params
        open_bars = max(1, int(float(p.get("opening_minutes", 30)) / interval_minutes(cfg.interval)))
        post_open = np.zeros(n, dtype=bool)
        if open_bars < n:
            post_open[open_bars:] = True

        # Only bother for families where score is monotonically increasing (high divergence risk)
        if cfg.family == "late_day_reversal":
            late_bar = max(1, int(float(p.get("late_minutes", 300)) / interval_minutes(cfg.interval)))
            candidates = day.bar_index >= late_bar
            move = float(p.get("move_pct", 1.6))
            if cfg.side == "long":
                candidates = candidates & (day.ret_from_open_pct <= -move) & (day.close > day.roll_low3)
                score = -day.ret_from_open_pct
            else:
                candidates = candidates & (day.ret_from_open_pct >= move) & (day.close < day.roll_high3)
                score = day.ret_from_open_pct
        else:
            # For other families use bar_index as a proxy for time to detect ordering mismatch
            score = day.bar_index.astype(float)
            candidates = np.ones(n, dtype=bool)
            signal2 = best_signal_fast(day, cfg)
            if signal2 is None:
                continue
            # Chronological = first qualifying = signal2[0]; argmax(bar_index) = same (monotonic)
            # So only late_day_reversal is the problematic family; skip others to avoid noise.
            continue

        candidates = candidates & ~np.isnan(score)
        if not np.any(candidates):
            continue
        qualifying = np.flatnonzero(candidates)
        argmax_pos = int(qualifying[np.argmax(score[qualifying])])
        total += 1
        if argmax_pos != chronological_pos:
            diverge += 1

    return round(diverge / total, 4) if total > 0 else 0.0


def select_trades_from_signals(signal_rows: list[tuple[IntradayDay, int, float]], cfg: IntradayConfig, base_fee_pct: float, fee_mult: float = 1.0, allowed_dates: set[pd.Timestamp] | None = None) -> pd.DataFrame:
    trades = []
    daily_pnl: dict[pd.Timestamp, float] = {}
    daily_counts: dict[pd.Timestamp, int] = {}
    for day, signal_pos, score in signal_rows:
        if allowed_dates is not None and day.date not in allowed_dates:
            continue
        if daily_pnl.get(day.date, 0.0) <= -abs(cfg.max_daily_loss_pct):
            continue
        entry_pos = signal_pos + 1
        if entry_pos >= len(day.open):
            continue
        if daily_counts.get(day.date, 0) >= cfg.max_trades_per_day:
            continue
        entry_price = float(day.open[entry_pos])
        exit_time, exit_price, exit_reason = simulate_exit_fast(day, entry_pos, cfg.side, entry_price, cfg.target_pct, cfg.stop_pct)
        gross = (exit_price / entry_price - 1.0) * 100.0 if cfg.side == "long" else (entry_price / exit_price - 1.0) * 100.0
        costs = base_fee_pct * fee_mult + liquidity_slippage_pct(day.avg_value20_cr)
        net = gross - costs
        daily_pnl[day.date] = daily_pnl.get(day.date, 0.0) + net
        daily_counts[day.date] = daily_counts.get(day.date, 0) + 1
        trades.append(
            {
                "date": day.date,
                "symbol": day.symbol,
                "interval": cfg.interval,
                "family": cfg.family,
                "side": cfg.side,
                "signal_time": pd.Timestamp(day.datetime[signal_pos]),
                "entry_time": pd.Timestamp(day.datetime[entry_pos]),
                "exit_time": exit_time,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "gross_return_pct": round(gross, 4),
                "cost_pct": round(costs, 4),
                "trade_return_pct": round(net, 4),
                "exit_reason": exit_reason,
                "score": round(float(score), 4),
            }
        )
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame(trades).sort_values(["date", "signal_time"]).reset_index(drop=True)


def summarize(trades: pd.DataFrame, dates: list[pd.Timestamp]) -> dict:
    if not dates:
        dates = sorted(pd.to_datetime(trades["date"]).dropna().unique().tolist()) if not trades.empty else []
    if trades.empty:
        return {"trades": 0, "avg_daily_return_pct": 0.0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0}
    by_day = trades.groupby("date")["trade_return_pct"].sum().reindex(dates, fill_value=0.0)
    equity = (1.0 + by_day / 100.0).cumprod()
    total = (float(equity.iloc[-1]) - 1.0) * 100.0 if not equity.empty else 0.0
    avg = float(by_day.mean())
    sharpe = float(by_day.mean() / by_day.std(ddof=0) * math.sqrt(252)) if by_day.std(ddof=0) > 0 else 0.0
    wins = trades[trades["trade_return_pct"] > 0]["trade_return_pct"].sum()
    losses = abs(trades[trades["trade_return_pct"] <= 0]["trade_return_pct"].sum())
    return {
        "trades": int(len(trades)),
        "avg_daily_return_pct": round(avg, 4),
        "total_return_pct": round(total, 2),
        "max_drawdown_pct": max_drawdown(by_day),
        "sharpe": round(sharpe, 3),
        "win_rate_pct": round(float((trades["trade_return_pct"] > 0).mean() * 100.0), 2),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else 99.0,
    }


def concentration(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"top_stock_profit_share_pct": 100.0, "top_day_profit_share_pct": 100.0, "top_week_profit_share_pct": 100.0, "top_month_profit_share_pct": 100.0}
    pos = trades[trades["trade_return_pct"] > 0].copy()
    total = float(pos["trade_return_pct"].sum())
    if total <= 0:
        return {"top_stock_profit_share_pct": 100.0, "top_day_profit_share_pct": 100.0, "top_week_profit_share_pct": 100.0, "top_month_profit_share_pct": 100.0}
    pos["week"] = pd.to_datetime(pos["date"]).dt.to_period("W").astype(str)
    pos["month"] = pd.to_datetime(pos["date"]).dt.to_period("M").astype(str)
    return {
        "top_stock_profit_share_pct": round(float(pos.groupby("symbol")["trade_return_pct"].sum().max() / total * 100.0), 2),
        "top_day_profit_share_pct": round(float(pos.groupby("date")["trade_return_pct"].sum().max() / total * 100.0), 2),
        "top_week_profit_share_pct": round(float(pos.groupby("week")["trade_return_pct"].sum().max() / total * 100.0), 2),
        "top_month_profit_share_pct": round(float(pos.groupby("month")["trade_return_pct"].sum().max() / total * 100.0), 2),
    }


def month_stability(trades: pd.DataFrame) -> tuple[float, int]:
    if trades.empty:
        return 0.0, 0
    tmp = trades.copy()
    tmp["month"] = pd.to_datetime(tmp["date"]).dt.to_period("M").astype(str)
    monthly = tmp.groupby("month")["trade_return_pct"].sum()
    if monthly.empty:
        return 0.0, 0
    return round(float((monthly > 0).mean() * 100.0), 2), int(len(monthly))


def rejection_reasons(row: pd.Series, min_trades: int) -> list[str]:
    reasons = []
    if int(row.get("trades", 0)) < min_trades:
        reasons.append(f"trades<{min_trades}")
    for split in ["train", "validation", "final", "walk"]:
        if float(row.get(f"{split}_avg_daily_return_pct", 0.0)) <= 0:
            reasons.append(f"{split}<=0")
    if float(row.get("max_drawdown_pct", 999.0)) >= 10:
        reasons.append("maxDD>=10")
    if float(row.get("profit_factor", 0.0)) <= 1.3:
        reasons.append("PF<=1.3")
    if float(row.get("sharpe", 0.0)) <= 1.2:
        reasons.append("Sharpe<=1.2")
    if float(row.get("positive_month_pct", 0.0)) < 55:
        reasons.append("unstable months")
    if float(row.get("high_cost_avg_daily_return_pct", -999.0)) <= 0:
        reasons.append("higher cost failed")
    for col, limit in [("top_stock_profit_share_pct", 25), ("top_day_profit_share_pct", 15), ("top_week_profit_share_pct", 25), ("top_month_profit_share_pct", 35)]:
        if float(row.get(col, 100.0)) > limit:
            reasons.append(col.replace("_profit_share_pct", " concentration"))
    return reasons


def robustness_score(row: pd.Series) -> float:
    score = 0.0
    score += min(max(float(row.get("final_avg_daily_return_pct", 0.0)), 0.0) * 120, 24)
    score += min(max(float(row.get("walk_avg_daily_return_pct", 0.0)), 0.0) * 100, 20)
    score += min(max(float(row.get("profit_factor", 0.0)) - 1.0, 0.0) * 25, 20)
    score += min(max(float(row.get("sharpe", 0.0)), 0.0) * 7, 16)
    score += max(0.0, 12.0 - float(row.get("max_drawdown_pct", 99.0)))
    score += min(int(row.get("trades", 0)) / 300.0, 1.0) * 10
    score += min(float(row.get("positive_month_pct", 0.0)) / 100.0, 1.0) * 8
    score -= max(0.0, float(row.get("top_month_profit_share_pct", 100.0)) - 30.0) * 0.25
    return round(score, 3)


def generate_configs(intervals: list[str]) -> list[IntradayConfig]:
    configs = []
    base = {"min_price": [40.0], "min_value_cr": [25.0, 75.0]}
    targets = [0.35, 0.55, 0.8, 1.1]
    stops = [0.25, 0.4, 0.65]
    max_trades = [3, 5]
    for interval in intervals:
        grids = [
            ("opening_range_breakout", "long", {"opening_minutes": [15, 30, 45], "buffer_pct": [0.03, 0.08], "min_rs": [0.0, 0.4]}),
            ("opening_range_breakdown", "short", {"opening_minutes": [15, 30, 45], "buffer_pct": [0.03, 0.08], "min_rs": [0.0, 0.4]}),
            ("vwap_mean_reversion", "long", {"opening_minutes": [15, 30], "dev_pct": [0.45, 0.75, 1.1]}),
            ("vwap_mean_reversion", "short", {"opening_minutes": [15, 30], "dev_pct": [0.45, 0.75, 1.1]}),
            ("vwap_trend_continuation", "long", {"opening_minutes": [15, 30], "dev_pct": [0.1, 0.2], "min_nifty": [-0.1, 0.1]}),
            ("vwap_trend_continuation", "short", {"opening_minutes": [15, 30], "dev_pct": [0.1, 0.2], "min_nifty": [-0.1, 0.1]}),
            ("gap_fade", "long", {"opening_minutes": [15, 30], "min_gap_pct": [0.8, 1.3]}),
            ("gap_fade", "short", {"opening_minutes": [15, 30], "min_gap_pct": [0.8, 1.3]}),
            ("gap_continuation", "long", {"opening_minutes": [15, 30], "min_gap_pct": [0.6, 1.0], "min_nifty": [-0.1, 0.1]}),
            ("gap_continuation", "short", {"opening_minutes": [15, 30], "min_gap_pct": [0.6, 1.0], "min_nifty": [-0.1, 0.1]}),
            ("relative_strength", "long", {"opening_minutes": [15, 30], "rs_threshold": [0.5, 0.9, 1.3]}),
            ("relative_strength", "short", {"opening_minutes": [15, 30], "rs_threshold": [0.5, 0.9, 1.3]}),
            ("first_hour_momentum", "long", {"opening_minutes": [60], "ret_threshold": [0.8, 1.2]}),
            ("first_hour_momentum", "short", {"opening_minutes": [60], "ret_threshold": [0.8, 1.2]}),
            ("late_day_reversal", "long", {"opening_minutes": [30], "late_minutes": [270, 300], "move_pct": [1.2, 1.8]}),
            ("late_day_reversal", "short", {"opening_minutes": [30], "late_minutes": [270, 300], "move_pct": [1.2, 1.8]}),
            ("volatility_contraction_breakout", "long", {"opening_minutes": [30], "lookback_minutes": [45, 60], "max_range_pct": [0.55, 0.85]}),
            ("volatility_contraction_breakout", "short", {"opening_minutes": [30], "lookback_minutes": [45, 60], "max_range_pct": [0.55, 0.85]}),
        ]
        for family, side, grid in grids:
            merged = {**base, **grid}
            keys = list(merged)
            for values in product(*[merged[k] for k in keys]):
                params = dict(zip(keys, values))
                for target, stop, maxd in product(targets, stops, max_trades):
                    if stop >= target:
                        continue
                    configs.append(IntradayConfig(family, side, interval, params, target, stop, maxd, 2.5, "equal"))
    return configs


def evaluate_config(cfg: IntradayConfig, features_by_interval: dict[str, pd.DataFrame], dates: list[pd.Timestamp], base_fee: float) -> tuple[dict, pd.DataFrame]:
    features = features_by_interval[cfg.interval]
    train_dates, val_dates, final_dates = split_dates(dates)
    split_summaries = {}
    split_trades = {}
    for name, split in [("train", train_dates), ("validation", val_dates), ("final", final_dates)]:
        trades = select_trades(features[features["date"].isin(split)].copy(), cfg, base_fee)
        split_trades[name] = trades
        split_summaries[name] = summarize(trades, split)
    walk_dates = dates[252:] if len(dates) > 252 else final_dates
    walk_trades = select_trades(features[features["date"].isin(walk_dates)].copy(), cfg, base_fee)
    walk_summary = summarize(walk_trades, walk_dates)
    all_trades = pd.concat([split_trades["train"], split_trades["validation"], split_trades["final"]], ignore_index=True)
    high_cost = summarize(select_trades(features[features["date"].isin(final_dates)].copy(), cfg, base_fee, fee_mult=1.5), final_dates)
    stable_month_pct, month_count = month_stability(all_trades)
    conc = concentration(all_trades)
    row = {
        "strategy": cfg.label(),
        "family": cfg.family,
        "side": cfg.side,
        "interval": cfg.interval,
        "params_json": json.dumps(cfg.params, sort_keys=True),
        "target_pct": cfg.target_pct,
        "stop_pct": cfg.stop_pct,
        "trades": int(len(all_trades)),
        "train_avg_daily_return_pct": split_summaries["train"]["avg_daily_return_pct"],
        "validation_avg_daily_return_pct": split_summaries["validation"]["avg_daily_return_pct"],
        "final_avg_daily_return_pct": split_summaries["final"]["avg_daily_return_pct"],
        "walk_avg_daily_return_pct": walk_summary["avg_daily_return_pct"],
        "total_return_pct": split_summaries["final"]["total_return_pct"],
        "max_drawdown_pct": max(split_summaries["train"]["max_drawdown_pct"], split_summaries["validation"]["max_drawdown_pct"], split_summaries["final"]["max_drawdown_pct"]),
        "profit_factor": split_summaries["final"]["profit_factor"],
        "sharpe": split_summaries["final"]["sharpe"],
        "win_rate_pct": split_summaries["final"]["win_rate_pct"],
        "positive_month_pct": stable_month_pct,
        "month_count": month_count,
        "high_cost_avg_daily_return_pct": high_cost["avg_daily_return_pct"],
        **conc,
    }
    return row, all_trades.assign(strategy_label=cfg.label()) if not all_trades.empty else all_trades


def evaluate_config_fast(
    cfg: IntradayConfig,
    day_cache_by_interval: dict[str, list[IntradayDay]],
    split_date_sets: dict[str, set[pd.Timestamp]],
    split_dates_lists: dict[str, list[pd.Timestamp]],
    dates: list[pd.Timestamp],
    base_fee: float,
    signal_cache: dict[tuple, list[tuple[IntradayDay, int, float]]] | None = None,
) -> tuple[dict, pd.DataFrame]:
    cache = day_cache_by_interval[cfg.interval]
    signal_rows = signal_cache.get(signal_key(cfg)) if signal_cache is not None else None
    if signal_rows is None:
        signal_rows = precompute_signals(cache, cfg)
    split_summaries = {}
    split_trades = {}
    for name in ["train", "validation", "final"]:
        trades = select_trades_from_signals(signal_rows, cfg, base_fee, allowed_dates=split_date_sets[name])
        split_trades[name] = trades
        split_summaries[name] = summarize(trades, split_dates_lists[name])
    walk_trades = select_trades_from_signals(signal_rows, cfg, base_fee, allowed_dates=split_date_sets["walk"])
    walk_summary = summarize(walk_trades, split_dates_lists["walk"])
    all_trades = pd.concat([split_trades["train"], split_trades["validation"], split_trades["final"]], ignore_index=True)
    high_cost = summarize(select_trades_from_signals(signal_rows, cfg, base_fee, fee_mult=1.5, allowed_dates=split_date_sets["final"]), split_dates_lists["final"])
    stable_month_pct, month_count = month_stability(all_trades)
    conc = concentration(all_trades)
    lookahead_div = measure_lookahead_divergence(cache, cfg)
    row = {
        "strategy": cfg.label(),
        "family": cfg.family,
        "side": cfg.side,
        "interval": cfg.interval,
        "params_json": json.dumps(cfg.params, sort_keys=True),
        "target_pct": cfg.target_pct,
        "stop_pct": cfg.stop_pct,
        "trades": int(len(all_trades)),
        "train_avg_daily_return_pct": split_summaries["train"]["avg_daily_return_pct"],
        "validation_avg_daily_return_pct": split_summaries["validation"]["avg_daily_return_pct"],
        "final_avg_daily_return_pct": split_summaries["final"]["avg_daily_return_pct"],
        "walk_avg_daily_return_pct": walk_summary["avg_daily_return_pct"],
        "total_return_pct": split_summaries["final"]["total_return_pct"],
        "max_drawdown_pct": max(split_summaries["train"]["max_drawdown_pct"], split_summaries["validation"]["max_drawdown_pct"], split_summaries["final"]["max_drawdown_pct"]),
        "profit_factor": split_summaries["final"]["profit_factor"],
        "sharpe": split_summaries["final"]["sharpe"],
        "win_rate_pct": split_summaries["final"]["win_rate_pct"],
        "positive_month_pct": stable_month_pct,
        "month_count": month_count,
        "high_cost_avg_daily_return_pct": high_cost["avg_daily_return_pct"],
        "lookahead_divergence_pct": lookahead_div,  # >0.5 = likely lookahead trap
        **conc,
    }
    return row, all_trades.assign(strategy_label=cfg.label()) if not all_trades.empty else all_trades


def build_reports(out_prefix: Path, ranking: pd.DataFrame, trades: pd.DataFrame, base_fee: float, data_audit: pd.DataFrame) -> None:
    accepted = ranking[ranking["accepted"]] if not ranking.empty else pd.DataFrame()
    rejected = ranking[~ranking["accepted"]] if not ranking.empty else pd.DataFrame()
    ranking.to_csv(out_prefix.with_name(out_prefix.name + "_strategy_ranking.csv"), index=False)
    accepted.to_csv(out_prefix.with_name(out_prefix.name + "_accepted_strategies.csv"), index=False)
    rejected.to_csv(out_prefix.with_name(out_prefix.name + "_rejected_strategies.csv"), index=False)
    if not trades.empty:
        trades.to_csv(out_prefix.with_name(out_prefix.name + "_candidate_trades.csv"), index=False)
        regime = trades.copy()
        regime["month"] = pd.to_datetime(regime["date"]).dt.to_period("M").astype(str)
        regime.groupby(["strategy_label", "month"])["trade_return_pct"].agg(["count", "sum", "mean"]).reset_index().to_csv(
            out_prefix.with_name(out_prefix.name + "_regime_month_performance.csv"), index=False
        )
    cost_cols = ["strategy", "final_avg_daily_return_pct", "high_cost_avg_daily_return_pct", "profit_factor", "sharpe"]
    ranking[cost_cols].head(50).to_csv(out_prefix.with_name(out_prefix.name + "_cost_sensitivity.csv"), index=False)
    data_audit.to_csv(out_prefix.with_name(out_prefix.name + "_data_audit.csv"), index=False)

    best = ranking.iloc[0] if not ranking.empty else pd.Series(dtype=object)
    lines = [
        "# Kite Intraday Research Report",
        "",
        f"Generated: {pd.Timestamp.now().isoformat(timespec='seconds')}",
        "",
        "## Data",
        f"- Intervals loaded: {sorted(data_audit['interval'].unique().tolist()) if not data_audit.empty else []}",
        f"- Symbols by interval: {data_audit.groupby('interval')['symbol'].nunique().to_dict() if not data_audit.empty else {}}",
        f"- Base estimated statutory/brokerage cost: {base_fee:.4f}% round trip before liquidity slippage.",
        "",
        "## Best Candidate",
    ]
    if best.empty:
        lines.append("No strategy produced enough trades to rank.")
    else:
        lines.extend(
            [
                f"- Strategy: `{best['strategy']}`",
                f"- Robustness score: {best['robustness_score']}",
                f"- Trades: {int(best['trades'])}",
                f"- Train avg daily: {best['train_avg_daily_return_pct']:+.4f}%",
                f"- Validation avg daily: {best['validation_avg_daily_return_pct']:+.4f}%",
                f"- Final avg daily: {best['final_avg_daily_return_pct']:+.4f}%",
                f"- Walk-forward avg daily: {best['walk_avg_daily_return_pct']:+.4f}%",
                f"- Max drawdown: {best['max_drawdown_pct']:.2f}%",
                f"- Profit factor: {best['profit_factor']:.3f}",
                f"- Sharpe: {best['sharpe']:.3f}",
                f"- Rejection reason: {best['rejection_reason'] or 'ACCEPTED'}",
            ]
        )
    lines.extend(
        [
            "",
            "## Paper-Trading Recommendation",
            "Worth paper trading: " + ("YES" if not accepted.empty else "NO"),
            "" if not accepted.empty else "No intraday candidate met the robustness acceptance criteria.",
            "",
            "## 3% Daily Benchmark",
            "- 3% average daily return remains a benchmark only, not an optimization target.",
            f"- Best final-test daily average found: {float(best.get('final_avg_daily_return_pct', 0.0)):+.4f}%" if not best.empty else "- No best strategy.",
        ]
    )
    out_prefix.with_name(out_prefix.name + "_final_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_checkpoint(path: Path, pre: list[tuple[float, float, float, IntradayConfig]], screened: int) -> None:
    rows = [
        {
            "avg_daily_return_pct": avg,
            "profit_factor": pf,
            "sharpe": sharpe,
            "strategy": cfg.label(),
            "config_json": json.dumps(
                {
                    "family": cfg.family,
                    "side": cfg.side,
                    "interval": cfg.interval,
                    "params": cfg.params,
                    "target_pct": cfg.target_pct,
                    "stop_pct": cfg.stop_pct,
                    "max_trades_per_day": cfg.max_trades_per_day,
                    "max_daily_loss_pct": cfg.max_daily_loss_pct,
                    "sizing": cfg.sizing,
                },
                sort_keys=True,
            ),
        }
        for avg, pf, sharpe, cfg in pre
    ]
    payload = {"screened": int(screened), "saved_at": pd.Timestamp.now().isoformat(timespec="seconds")}
    path.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(path, index=False)


def load_checkpoint(path: Path) -> tuple[list[tuple[float, float, float, IntradayConfig]], int]:
    meta_path = path.with_suffix(".json")
    if not path.exists() or not meta_path.exists():
        return [], 0
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    df = pd.read_csv(path)
    pre = []
    for _, row in df.iterrows():
        cfg_payload = json.loads(row["config_json"])
        cfg = IntradayConfig(
            cfg_payload["family"],
            cfg_payload["side"],
            cfg_payload["interval"],
            cfg_payload["params"],
            float(cfg_payload["target_pct"]),
            float(cfg_payload["stop_pct"]),
            int(cfg_payload["max_trades_per_day"]),
            float(cfg_payload["max_daily_loss_pct"]),
            cfg_payload["sizing"],
        )
        pre.append((float(row["avg_daily_return_pct"]), float(row["profit_factor"]), float(row["sharpe"]), cfg))
    return pre, int(meta.get("screened", 0))


def main() -> int:
    parser = argparse.ArgumentParser(description="Research robust Kite intraday strategies")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--start", default="2024-05-13")
    parser.add_argument("--end", default="2026-05-12")
    parser.add_argument("--intervals", nargs="+", default=["5minute", "15minute"])
    parser.add_argument("--limit", type=int, default=178)
    parser.add_argument("--require-quality", action="store_true")
    parser.add_argument("--gate-file", default=str(DEFAULT_GATE))
    parser.add_argument("--min-complete-pct", type=float, default=95.0)
    parser.add_argument("--min-trades", type=int, default=300)
    parser.add_argument("--candidate-count", type=int, default=120)
    parser.add_argument("--out-prefix", default=str(RESULTS_DIR / "kite_intraday_research"))
    parser.add_argument("--resume", action="store_true", help="Resume screening from the checkpoint for this out-prefix.")
    parser.add_argument("--checkpoint-every", type=int, default=250)
    parser.add_argument("--max-runtime-minutes", type=float, default=0.0, help="Stop after this many minutes after saving a checkpoint; 0 disables.")
    parser.add_argument("--verify-sample", type=int, default=0, help="Compare legacy and optimized trade selection on this many symbols, then exit.")
    args = parser.parse_args()

    run_started = time.perf_counter()
    timer = StageTimer()
    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    if args.require_quality:
        try:
            require_intraday_gate(Path(args.gate_file), args.intervals, args.min_complete_pct)
        except RuntimeError as exc:
            print(str(exc), flush=True)
            return 1

    symbols = load_universe(Path(args.universe), args.limit)
    base_fee = indian_intraday_fee_pct()
    data_audit_rows = []
    features_by_interval = {}
    day_cache_by_interval: dict[str, list[IntradayDay]] = {}
    common_dates: set[pd.Timestamp] = set()
    for interval in args.intervals:
        stage_start = time.perf_counter()
        nifty = read_intraday("NIFTY 50", interval, args.start, args.end)
        parts = []
        for idx, symbol in enumerate(symbols, 1):
            df = read_intraday(symbol, interval, args.start, args.end)
            if not df.empty:
                parts.append(df)
                data_audit_rows.append({"interval": interval, "symbol": symbol, "rows": len(df), "source": "kite_intraday_cache"})
            if idx % 25 == 0:
                print(f"  loaded {interval} {idx}/{len(symbols)} symbols; usable={len(parts)}", flush=True)
        if not parts:
            continue
        timer.mark(f"load_{interval}", stage_start)
        stage_start = time.perf_counter()
        raw = pd.concat(parts, ignore_index=True)
        features = add_intraday_features(raw, nifty)
        features = features.dropna(subset=["prev_close", "gap_pct", "avg_value20_cr", "vwap"]).copy()
        timer.mark(f"features_{interval}", stage_start)
        stage_start = time.perf_counter()
        features_by_interval[interval] = features
        day_cache_by_interval[interval] = prepare_day_cache(features)
        timer.mark(f"day_cache_{interval}", stage_start)
        dates = set(pd.to_datetime(features["date"]).dropna().unique().tolist())
        common_dates = dates if not common_dates else common_dates & dates

    data_audit = pd.DataFrame(data_audit_rows)
    if not features_by_interval:
        raise RuntimeError("No intraday Kite cache was loaded. Run kite_intraday_backfill.py first.")
    dates = sorted(common_dates) if common_dates else sorted(set().union(*[set(v["date"].unique()) for v in features_by_interval.values()]))
    configs = generate_configs(list(features_by_interval))
    train_dates, val_dates, final_dates = split_dates(dates)
    walk_dates = dates[252:] if len(dates) > 252 else final_dates
    split_dates_lists = {"train": train_dates, "validation": val_dates, "final": final_dates, "walk": walk_dates}
    split_date_sets = {name: set(values) for name, values in split_dates_lists.items()}
    if args.verify_sample > 0:
        sample_symbols = set(symbols[: int(args.verify_sample)])
        verification_rows = []
        for interval, features in features_by_interval.items():
            sample_features = features[features["symbol"].isin(sample_symbols)].copy()
            sample_cache = prepare_day_cache(sample_features)
            sample_configs = generate_configs([interval])[:20]
            for cfg in sample_configs:
                legacy = select_trades(sample_features, cfg, base_fee)
                optimized = select_trades_fast(sample_cache, cfg, base_fee)
                cols = ["date", "symbol", "signal_time", "entry_time", "exit_time", "trade_return_pct", "exit_reason", "score"]
                same = len(legacy) == len(optimized)
                if same and len(legacy):
                    same = legacy[cols].reset_index(drop=True).equals(optimized[cols].reset_index(drop=True))
                verification_rows.append({"interval": interval, "strategy": cfg.label(), "legacy_trades": len(legacy), "optimized_trades": len(optimized), "identical": bool(same)})
        verify_df = pd.DataFrame(verification_rows)
        verify_df.to_csv(out_prefix.with_name(out_prefix.name + "_verification.csv"), index=False)
        print(verify_df.to_string(index=False))
        print(f"Verification identical: {bool(verify_df['identical'].all())}")
        return 0 if bool(verify_df["identical"].all()) else 1

    checkpoint_path = out_prefix.with_name(out_prefix.name + "_screen_checkpoint.csv")
    pre = []
    resume_from = 0
    if args.resume:
        pre, resume_from = load_checkpoint(checkpoint_path)
        if resume_from:
            print(f"Resuming screening from config {resume_from}/{len(configs)} with {len(pre)} retained candidates", flush=True)
    signal_cache: dict[tuple, list[tuple[IntradayDay, int, float]]] = {}
    screen_started = time.perf_counter()
    for idx, cfg in enumerate(configs, 1):
        if idx <= resume_from:
            continue
        if args.max_runtime_minutes > 0 and (time.perf_counter() - run_started) / 60.0 >= float(args.max_runtime_minutes):
            save_checkpoint(checkpoint_path, pre, idx - 1)
            print(f"Max runtime reached; checkpoint saved at config {idx - 1}/{len(configs)}", flush=True)
            timer.write(out_prefix.with_name(out_prefix.name + "_stage_timings.csv"))
            return 3
        key = signal_key(cfg)
        if key not in signal_cache:
            signal_cache[key] = precompute_signals(day_cache_by_interval[cfg.interval], cfg)
        trades = select_trades_from_signals(signal_cache[key], cfg, base_fee)
        if len(trades) >= max(50, int(args.min_trades * 0.25)):
            sm = summarize(trades, dates)
            pre.append((sm["avg_daily_return_pct"], sm["profit_factor"], sm["sharpe"], cfg))
        if idx % max(1, int(args.checkpoint_every)) == 0:
            save_checkpoint(checkpoint_path, pre, idx)
        if idx % 100 == 0:
            elapsed = time.perf_counter() - screen_started
            rate = (idx - resume_from) / max(elapsed, 0.001)
            remaining = (len(configs) - idx) / max(rate, 0.001)
            print(f"  screened {idx}/{len(configs)} intraday configs; retained={len(pre)}; signal_defs={len(signal_cache)}; eta={remaining/60:.1f}m", flush=True)
    timer.mark("screen_configs", screen_started)
    selected = [x[3] for x in sorted(pre, key=lambda r: (r[0], r[1], r[2]), reverse=True)[: max(1, args.candidate_count)]]
    rows = []
    trade_frames = []
    eval_started = time.perf_counter()
    for idx, cfg in enumerate(selected, 1):
        row, trades = evaluate_config_fast(cfg, day_cache_by_interval, split_date_sets, split_dates_lists, dates, base_fee, signal_cache=signal_cache)
        rows.append(row)
        if not trades.empty:
            trade_frames.append(trades)
        if idx % 10 == 0:
            print(f"  evaluated {idx}/{len(selected)} retained candidates", flush=True)
    timer.mark("evaluate_candidates", eval_started)
    ranking = pd.DataFrame(rows)
    if not ranking.empty:
        ranking["rejection_reason"] = ranking.apply(lambda r: "; ".join(rejection_reasons(r, args.min_trades)), axis=1)
        ranking["accepted"] = ranking["rejection_reason"].eq("")
        ranking["robustness_score"] = ranking.apply(robustness_score, axis=1)
        ranking.sort_values(["accepted", "robustness_score", "final_avg_daily_return_pct"], ascending=[False, False, False], inplace=True)
    all_trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    build_reports(out_prefix, ranking, all_trades, base_fee, data_audit)
    timer.write(out_prefix.with_name(out_prefix.name + "_stage_timings.csv"))
    print(f"Saved: {out_prefix.with_name(out_prefix.name + '_final_report.md')}")
    print(f"Accepted strategies: {int(ranking['accepted'].sum()) if not ranking.empty else 0}")
    if not ranking.empty:
        print(ranking.head(10)[["strategy", "robustness_score", "trades", "final_avg_daily_return_pct", "profit_factor", "sharpe", "rejection_reason"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

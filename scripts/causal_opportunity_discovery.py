#!/usr/bin/env python3
"""Causal, research-only discovery of NSE 1-minute intraday opportunities.

This harness does not modify or tune PSAR, NGScalp, Discord selection, or live
TradingView behavior. It uses the existing ignored Kite minute cache and keeps
the 2026 holdout unread until an explicit one-shot unlock.

Execution contract:
- Features use completed bars at or before signal time T.
- Entry reference is the next 1-minute bar open (T+1), never signal close.
- Same-bar stop+target ambiguity is conservatively stop-first.
- Discovery reads train+validation only.
- Validation thresholds come from TRAIN score quantiles, not validation ranks.
- Holdout requires a frozen manifest and explicit --unlock-holdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = ROOT / "results" / "Data" / "kite_history_backfill" / "minute"
DEFAULT_OUT_DIR = ROOT / "results" / "Data" / "causal_opportunity_v1"

SESSION_START = pd.Timestamp("2000-01-01 09:15:00").time()
SIGNAL_START = pd.Timestamp("2000-01-01 09:20:00").time()
SIGNAL_END = pd.Timestamp("2000-01-01 14:50:00").time()
SESSION_END = pd.Timestamp("2000-01-01 15:30:00").time()

FEATURE_VERSION = 1
LABEL_VERSION = 1

EVENT_COLUMNS = [
    "event_or5_breakout",
    "event_or10_breakout",
    "event_or15_breakout",
    "event_or15_retest",
    "event_prior20_breakout",
    "event_vwap_reclaim",
    "event_vwap_pullback",
    "event_compression_expansion",
    "event_failed_breakdown_reclaim",
    "event_baseline_grid",
]

BASE_MODEL_FEATURES = [
    "minute", "bar_index",
    "ret_1_atr", "ret_3_atr", "ret_5_atr", "ret_10_atr", "ret_20_atr",
    "vwap_dist_atr", "vwap_slope_5_atr",
    "dist_session_high_atr", "dist_session_low_atr", "pullback_from_high_atr",
    "signal_range_atr", "close_position", "body_to_range",
    "upper_wick_atr", "lower_wick_atr",
    "vol_ratio_5", "vol_ratio_20", "volume_accel", "tod_rvol",
    "turnover_lakh", "median_turnover_20_lakh",
    "compression_5_atr", "compression_10_atr",
    "breakout_20_atr", "breakout_count_20",
    "or5_dist_atr", "or10_dist_atr", "or15_dist_atr",
    *EVENT_COLUMNS,
]

CROSS_SECTIONAL_FEATURES = [
    "cs_candidate_count",
    "cs_ret5_rank",
    "cs_tod_rvol_rank",
    "cs_vol_ratio_rank",
    "cs_vwap_dist_rank",
    "cs_turnover_rank",
]
MODEL_FEATURES = BASE_MODEL_FEATURES + CROSS_SECTIONAL_FEATURES

MONOTONIC_FEATURES = [
    "ret_5_atr", "ret_10_atr", "vwap_dist_atr", "vwap_slope_5_atr",
    "pullback_from_high_atr", "signal_range_atr", "close_position",
    "vol_ratio_5", "vol_ratio_20", "volume_accel", "tod_rvol",
    "turnover_lakh", "median_turnover_20_lakh", "compression_5_atr",
    "breakout_20_atr", "breakout_count_20",
    "cs_ret5_rank", "cs_tod_rvol_rank", "cs_vol_ratio_rank",
]


@dataclass(frozen=True)
class FrozenSplit:
    train_start: str = "2026-07-06"
    train_end: str = "2026-08-25"
    validation_start: str = "2026-08-26"
    validation_end: str = "2026-09-10"
    holdout_start: str = "2026-09-11"
    holdout_end: str = "2026-09-30"

    def bucket(self, value) -> str:
        date = pd.Timestamp(value).normalize()
        if pd.Timestamp(self.train_start) <= date <= pd.Timestamp(self.train_end):
            return "train"
        if pd.Timestamp(self.validation_start) <= date <= pd.Timestamp(self.validation_end):
            return "validation"
        if pd.Timestamp(self.holdout_start) <= date <= pd.Timestamp(self.holdout_end):
            return "holdout"
        return "outside"

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class ResearchConfig:
    min_price: float = 20.0
    min_median_turnover_lakh: float = 2.5
    baseline_grid_minutes: int = 5
    max_forward_bars: int = 30
    risk_atr_multiple: float = 1.0
    roundtrip_cost_pct: float = 0.20
    stress_cost_pct: float = 0.30
    max_per_timestamp: int = 5
    max_trades_per_day: int = 20
    symbol_cooldown_minutes: int = 30
    min_validation_trades: int = 30
    top_percentiles: tuple[int, ...] = (1, 2, 5, 10, 20)

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def _naive(values) -> pd.Series:
    raw = pd.Series(values).astype(str).str.replace(r"(Z|[+-]\d{2}:?\d{2})$", "", regex=True)
    return pd.to_datetime(raw, errors="coerce")


def _div(a: pd.Series, b: pd.Series) -> pd.Series:
    return a / b.replace(0, np.nan)


def _atr(frame: pd.DataFrame, window: int = 14) -> pd.Series:
    prev = frame["close"].shift(1)
    tr = pd.concat([
        (frame["high"] - frame["low"]).abs(),
        (frame["high"] - prev).abs(),
        (frame["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window, min_periods=window).mean()


def _opening_level(day: pd.DataFrame, end: str, column: str, fn: str) -> float:
    end_time = pd.Timestamp(f"2000-01-01 {end}").time()
    s = day[(day["time"] >= SESSION_START) & (day["time"] < end_time)][column]
    if s.empty:
        return np.nan
    return float(s.max() if fn == "max" else s.min())


def _tod_rvol_causal(frame: pd.DataFrame) -> pd.Series:
    """Current volume / expanding median of the SAME minute on PRIOR days."""
    out = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, indices in frame.groupby("minute", sort=False).groups.items():
        idx = list(indices)
        v = frame.loc[idx, "volume"].astype(float)
        prior = v.shift(1).expanding(min_periods=5).median().replace(0, np.nan)
        out.loc[idx] = v.to_numpy() / prior.to_numpy()
    return out


def prepare_symbol_features(raw: pd.DataFrame, symbol: str, grid_minutes: int = 5) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    work = raw.copy()
    if "date" in work.columns and "datetime" not in work.columns:
        work = work.rename(columns={"date": "datetime"})
    need = {"datetime", "open", "high", "low", "close", "volume"}
    if need - set(work.columns):
        raise ValueError(f"{symbol}: missing {sorted(need - set(work.columns))}")
    work["datetime"] = _naive(work["datetime"])
    for col in ["open", "high", "low", "close", "volume"]:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["datetime", "open", "high", "low", "close"])
    work = work.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    work["symbol"] = symbol.upper()
    work["session_date"] = work["datetime"].dt.normalize()
    work["time"] = work["datetime"].dt.time
    work["minute"] = work["datetime"].dt.hour * 60 + work["datetime"].dt.minute
    work = work[(work["time"] >= SESSION_START) & (work["time"] <= SESSION_END)].copy()

    parts = []
    for _, day in work.groupby("session_date", sort=True):
        g = day.sort_values("datetime").reset_index(drop=True).copy()
        g["bar_index"] = np.arange(len(g))
        atr = _atr(g)
        g["atr14"] = atr
        typical = (g["high"] + g["low"] + g["close"]) / 3.0
        g["vwap"] = (typical * g["volume"]).cumsum() / g["volume"].cumsum().replace(0, np.nan)
        g["vwap_dist_atr"] = _div(g["close"] - g["vwap"], atr)
        g["vwap_slope_5_atr"] = _div(g["vwap"] - g["vwap"].shift(5), atr)

        prior_hi = g["high"].cummax().shift(1)
        prior_lo = g["low"].cummin().shift(1)
        g["dist_session_high_atr"] = _div(g["close"] - prior_hi, atr)
        g["dist_session_low_atr"] = _div(g["close"] - prior_lo, atr)
        g["pullback_from_high_atr"] = _div(prior_hi - g["close"], atr)
        for n in [1, 3, 5, 10, 20]:
            g[f"ret_{n}_atr"] = _div(g["close"] - g["close"].shift(n), atr)

        rng = (g["high"] - g["low"]).clip(lower=0)
        g["signal_range_atr"] = _div(rng, atr)
        g["close_position"] = _div(g["close"] - g["low"], rng).clip(0, 1)
        g["body_to_range"] = _div(g["close"] - g["open"], rng).clip(-1, 1)
        g["upper_wick_atr"] = _div(g["high"] - g[["open", "close"]].max(axis=1), atr)
        g["lower_wick_atr"] = _div(g[["open", "close"]].min(axis=1) - g["low"], atr)

        prev5v = g["volume"].shift(1).rolling(5, min_periods=3).mean()
        prev20v = g["volume"].shift(1).rolling(20, min_periods=10).median()
        g["vol_ratio_5"] = _div(g["volume"], prev5v)
        g["vol_ratio_20"] = _div(g["volume"], prev20v)
        g["volume_accel"] = _div(
            g["volume"].shift(1).rolling(3, min_periods=2).mean(),
            g["volume"].shift(4).rolling(5, min_periods=3).mean(),
        )
        turnover = g["close"] * g["volume"] / 100_000.0
        g["turnover_lakh"] = turnover
        g["median_turnover_20_lakh"] = turnover.shift(1).rolling(20, min_periods=10).median()

        p20h = g["high"].shift(1).rolling(20, min_periods=10).max()
        p20l = g["low"].shift(1).rolling(20, min_periods=10).min()
        g["breakout_20_atr"] = _div(g["close"] - p20h, atr)
        raw_break = (g["close"] > p20h) & (g["close"].shift(1) <= p20h.shift(1))
        g["breakout_count_20"] = raw_break.shift(1).rolling(20, min_periods=1).sum()

        g["compression_5_atr"] = _div(
            g["high"].rolling(5, min_periods=5).max() - g["low"].rolling(5, min_periods=5).min(), atr
        )
        g["compression_10_atr"] = _div(
            g["high"].rolling(10, min_periods=10).max() - g["low"].rolling(10, min_periods=10).min(), atr
        )

        o5h = _opening_level(g, "09:20:00", "high", "max")
        o10h = _opening_level(g, "09:25:00", "high", "max")
        o15h = _opening_level(g, "09:30:00", "high", "max")
        for name, level, ready in [("or5", o5h, "09:20:00"), ("or10", o10h, "09:25:00"), ("or15", o15h, "09:30:00")]:
            ready_time = pd.Timestamp(f"2000-01-01 {ready}").time()
            g[f"{name}_dist_atr"] = np.where(g["time"] >= ready_time, (g["close"] - level) / atr, np.nan)

        prev_close, prev_vwap = g["close"].shift(1), g["vwap"].shift(1)

        def breakout_event(level: float, ready: str) -> pd.Series:
            t = pd.Timestamp(f"2000-01-01 {ready}").time()
            if not np.isfinite(level):
                return pd.Series(False, index=g.index)
            return (g["time"] >= t) & (g["close"] > level) & (prev_close <= level)

        g["event_or5_breakout"] = breakout_event(o5h, "09:20:00").astype(int)
        g["event_or10_breakout"] = breakout_event(o10h, "09:25:00").astype(int)
        g["event_or15_breakout"] = breakout_event(o15h, "09:30:00").astype(int)
        broke15 = (g["high"].shift(1).cummax() > o15h) if np.isfinite(o15h) else pd.Series(False, index=g.index)
        g["event_or15_retest"] = (
            (g["time"] >= pd.Timestamp("2000-01-01 09:30:00").time())
            & broke15.fillna(False) & (g["low"] <= o15h + atr * 0.10)
            & (g["close"] > o15h) & (g["close"] > g["open"])
        ).fillna(False).astype(int)
        g["event_prior20_breakout"] = raw_break.fillna(False).astype(int)
        g["event_vwap_reclaim"] = (
            (prev_close <= prev_vwap) & (g["close"] > g["vwap"]) & (g["close"] > g["open"])
        ).fillna(False).astype(int)
        prior_above = g["high"].shift(1).rolling(5, min_periods=1).max() > g["vwap"].shift(1)
        g["event_vwap_pullback"] = (
            prior_above.fillna(False) & (g["low"] <= g["vwap"] + atr * 0.10)
            & (g["close"] > g["vwap"]) & (g["close"] > g["open"])
        ).fillna(False).astype(int)
        p10h = g["high"].shift(1).rolling(10, min_periods=5).max()
        g["event_compression_expansion"] = (
            (g["compression_5_atr"].shift(1) <= 1.50) & (g["signal_range_atr"] >= 0.80)
            & (g["close_position"] >= 0.70) & (g["close"] > p10h)
        ).fillna(False).astype(int)
        g["event_failed_breakdown_reclaim"] = (
            (g["low"] < p20l) & (g["close"] > p20l) & (g["close_position"] >= 0.60)
        ).fillna(False).astype(int)
        grid = max(1, int(grid_minutes))
        g["event_baseline_grid"] = (
            ((g["minute"] - (9 * 60 + 20)) % grid == 0)
            & (g["time"] >= SIGNAL_START) & (g["time"] <= SIGNAL_END)
        ).astype(int)
        parts.append(g)

    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True).sort_values("datetime").reset_index(drop=True)
    out["tod_rvol"] = _tod_rvol_causal(out)
    return out.replace([np.inf, -np.inf], np.nan)


def candidate_mask(frame: pd.DataFrame, cfg: ResearchConfig) -> pd.Series:
    event = frame[EVENT_COLUMNS].fillna(0).astype(int).sum(axis=1) > 0
    time_ok = (frame["time"] >= SIGNAL_START) & (frame["time"] <= SIGNAL_END)
    same_day_next = frame["session_date"].shift(-1).eq(frame["session_date"])
    return (
        event & time_ok & same_day_next
        & (frame["close"] >= cfg.min_price)
        & (frame["median_turnover_20_lakh"].fillna(0) >= cfg.min_median_turnover_lakh)
        & (frame["atr14"].fillna(0) > 0)
    )


def _path(future: pd.DataFrame, entry: float, r: float, target_r: float) -> tuple[str, int | None]:
    target, stop = entry + r * target_r, entry - r
    for offset, (_, bar) in enumerate(future.iterrows(), start=1):
        sh, th = float(bar["low"]) <= stop, float(bar["high"]) >= target
        if sh and th:
            return "ambiguous_stop_first", offset
        if sh:
            return "stop", offset
        if th:
            return "target", offset
    return "neither", None


def _label_entry(work: pd.DataFrame, signal_idx: int, entry_idx: int, cfg: ResearchConfig) -> dict:
    signal = work.iloc[signal_idx]
    entry_bar = work.iloc[entry_idx]
    if entry_bar["session_date"] != signal["session_date"]:
        return {}
    entry = float(entry_bar["open"])
    r = float(signal["atr14"]) * cfg.risk_atr_multiple
    if not np.isfinite(entry) or not np.isfinite(r) or entry <= 0 or r <= 0:
        return {}
    future = work[(work.index >= entry_idx) & (work["session_date"] == signal["session_date"])].head(cfg.max_forward_bars)
    if future.empty:
        return {}
    out = {"entry_time": entry_bar["datetime"], "entry_price": entry, "risk_unit": r, "risk_pct": r / entry * 100}
    for h in [5, 15, 30]:
        p = future.head(h)
        out[f"mfe_{h}m_r"] = (float(p["high"].max()) - entry) / r
        out[f"mae_{h}m_r"] = (float(p["low"].min()) - entry) / r
        out[f"close_{h}m_r"] = (float(p.iloc[-1]["close"]) - entry) / r
    cost_r = (cfg.roundtrip_cost_pct / 100.0 * entry) / r
    stress_r = (cfg.stress_cost_pct / 100.0 * entry) / r
    out["cost_r"], out["stress_cost_r"] = cost_r, stress_r
    for target_r in [0.5, 1.0, 1.5, 2.0, 3.0]:
        result, bars = _path(future, entry, r, target_r)
        key = str(target_r).replace(".", "p")
        out[f"target_{key}r_before_stop"] = int(result == "target")
        out[f"path_{key}r_result"] = result
        out[f"bars_to_{key}r_or_stop"] = bars if bars is not None else np.nan
        gross = target_r if result == "target" else (-1.0 if result != "neither" else out["close_30m_r"])
        out[f"net_{key}r_strategy_r"] = gross - cost_r
        out[f"stress_net_{key}r_strategy_r"] = gross - stress_r
    return out


def label_candidates(features: pd.DataFrame, cfg: ResearchConfig) -> pd.DataFrame:
    work = features.sort_values("datetime").reset_index(drop=True).copy()
    rows = []
    for idx in np.flatnonzero(candidate_mask(work, cfg).to_numpy()):
        base = _label_entry(work, idx, idx + 1, cfg)
        if not base:
            continue
        signal = work.iloc[idx]
        row = {
            "symbol": signal["symbol"], "session_date": signal["session_date"],
            "signal_time": signal["datetime"], "feature_version": FEATURE_VERSION,
            "label_version": LABEL_VERSION, **base,
        }
        for feature in BASE_MODEL_FEATURES:
            row[feature] = signal.get(feature, np.nan)
        delayed = _label_entry(work, idx, idx + 2, cfg) if idx + 2 < len(work) else {}
        row["delay1_net_1p5r_strategy_r"] = delayed.get("net_1p5r_strategy_r", np.nan)
        row["delay1_stress_net_1p5r_strategy_r"] = delayed.get("stress_net_1p5r_strategy_r", np.nan)
        rows.append(row)
    return pd.DataFrame(rows)


def add_cross_sectional_features(candidates: pd.DataFrame) -> pd.DataFrame:
    out = candidates.copy()
    grouped = out.groupby("signal_time", sort=False)
    out["cs_candidate_count"] = grouped["symbol"].transform("count").astype(float)
    for source, target in [
        ("ret_5_atr", "cs_ret5_rank"), ("tod_rvol", "cs_tod_rvol_rank"),
        ("vol_ratio_5", "cs_vol_ratio_rank"), ("vwap_dist_atr", "cs_vwap_dist_rank"),
        ("turnover_lakh", "cs_turnover_rank"),
    ]:
        out[target] = grouped[source].rank(pct=True, method="average")
    return out


def discover_files(root: Path) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = {}
    for path in root.rglob("*.csv"):
        if not path.is_file():
            continue
        symbol = path.stem.upper().strip()
        if symbol in {"NIFTY", "NIFTY 50", "^NSEI"} or not symbol:
            continue
        grouped.setdefault(symbol, []).append(path)
    return grouped


def load_symbol(symbol: str, paths: Sequence[Path], start: str, end: str) -> pd.DataFrame:
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    parts = []
    for path in paths:
        try:
            f = pd.read_csv(path)
        except Exception:
            continue
        if "date" in f.columns and "datetime" not in f.columns:
            f = f.rename(columns={"date": "datetime"})
        if "datetime" not in f.columns:
            continue
        f["datetime"] = _naive(f["datetime"])
        f = f[(f["datetime"] >= start_ts) & (f["datetime"] < end_ts)]
        if not f.empty:
            parts.append(f)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out["symbol"] = symbol
    return out.dropna(subset=["datetime"]).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)


def add_split(frame: pd.DataFrame, split: FrozenSplit) -> pd.DataFrame:
    out = frame.copy()
    out["split"] = out["session_date"].map(split.bucket)
    return out[out["split"].isin(["train", "validation", "holdout"])].copy()


def monotonicity(train: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in MONOTONIC_FEATURES:
        d = train[[feature, "target_1p5r_before_stop", "net_1p5r_strategy_r"]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(d) < 100:
            continue
        try:
            d["bucket"] = pd.qcut(d[feature], 10, duplicates="drop")
        except ValueError:
            continue
        for n, (bucket, g) in enumerate(d.groupby("bucket", observed=True), start=1):
            rows.append({
                "feature": feature, "decile": n, "range": str(bucket), "n": len(g),
                "feature_median": g[feature].median(),
                "favorable_rate": g["target_1p5r_before_stop"].mean(),
                "net_expectancy_r": g["net_1p5r_strategy_r"].mean(),
            })
    return pd.DataFrame(rows)


def ranker() -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(C=0.25, penalty="l2", solver="lbfgs", max_iter=1000,
                                     class_weight="balanced", random_state=42)),
    ])


def score(model: Pipeline, frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["prediction"] = model.predict_proba(out[MODEL_FEATURES])[:, 1]
    return out


def train_thresholds(train_scored: pd.DataFrame, percentiles: Iterable[int]) -> dict[int, float]:
    return {int(p): float(train_scored["prediction"].quantile(1.0 - int(p) / 100.0)) for p in percentiles}


def execution_policy(frame: pd.DataFrame, threshold: float, cfg: ResearchConfig) -> pd.DataFrame:
    """Causal selection: fixed train-derived score threshold + time-local ranking."""
    eligible = frame[frame["prediction"] >= threshold].sort_values(["signal_time", "prediction", "symbol"], ascending=[True, False, True])
    if eligible.empty:
        return eligible
    picked = []
    day_counts: dict[pd.Timestamp, int] = {}
    last_symbol: dict[tuple[pd.Timestamp, str], pd.Timestamp] = {}
    for ts, batch in eligible.groupby("signal_time", sort=True):
        date = pd.Timestamp(ts).normalize()
        remaining = cfg.max_trades_per_day - day_counts.get(date, 0)
        if remaining <= 0:
            continue
        accepted = 0
        for _, row in batch.sort_values("prediction", ascending=False).iterrows():
            if accepted >= cfg.max_per_timestamp or remaining <= 0:
                break
            key = (date, str(row["symbol"]))
            previous = last_symbol.get(key)
            if previous is not None and pd.Timestamp(ts) - previous < pd.Timedelta(minutes=cfg.symbol_cooldown_minutes):
                continue
            picked.append(row)
            last_symbol[key] = pd.Timestamp(ts)
            day_counts[date] = day_counts.get(date, 0) + 1
            accepted += 1
            remaining -= 1
    return pd.DataFrame(picked).reset_index(drop=True) if picked else eligible.iloc[0:0].copy()


def bucket_table(validation: pd.DataFrame, thresholds: dict[int, float], cfg: ResearchConfig) -> pd.DataFrame:
    rows = []
    for pct, threshold in sorted(thresholds.items()):
        picks = execution_policy(validation, threshold, cfg)
        if picks.empty:
            continue
        daily = picks.groupby("session_date")["net_1p5r_strategy_r"].sum()
        rows.append({
            "train_top_percent": pct, "score_threshold": threshold,
            "trades": len(picks), "sessions": picks["session_date"].nunique(),
            "trades_per_day": len(picks) / max(1, validation["session_date"].nunique()),
            "target_1p5r_before_stop_pct": picks["target_1p5r_before_stop"].mean() * 100,
            "net_expectancy_r": picks["net_1p5r_strategy_r"].mean(),
            "stress_net_expectancy_r": picks["stress_net_1p5r_strategy_r"].mean(),
            "delay1_net_expectancy_r": picks["delay1_net_1p5r_strategy_r"].mean(),
            "median_mfe_30m_r": picks["mfe_30m_r"].median(),
            "median_mae_30m_r": picks["mae_30m_r"].median(),
            "positive_day_pct": (daily > 0).mean() * 100,
            "worst_day_r": daily.min(), "best_day_r": daily.max(),
        })
    return pd.DataFrame(rows)


def choose_candidate(table: pd.DataFrame, cfg: ResearchConfig) -> int | None:
    if table.empty:
        return None
    ok = table[(table["trades"] >= cfg.min_validation_trades)
               & (table["net_expectancy_r"] > 0)
               & (table["stress_net_expectancy_r"] > 0)
               & (table["delay1_net_expectancy_r"] > 0)].copy()
    if ok.empty:
        return None
    return int(ok.sort_values("train_top_percent", ascending=False).iloc[0]["train_top_percent"])


def remove_best(frame: pd.DataFrame, k: int) -> float:
    s = frame["net_1p5r_strategy_r"].sort_values(ascending=False)
    return float(s.iloc[k:].mean()) if len(s) > k else np.nan


def robustness(scored: pd.DataFrame, threshold: float, cfg: ResearchConfig) -> dict:
    picks = execution_policy(scored, threshold, cfg)
    if picks.empty:
        return {"trades": 0}
    by_symbol = picks.groupby("symbol")["net_1p5r_strategy_r"].agg(["count", "sum"])
    positive_sum = max(float(picks["net_1p5r_strategy_r"].clip(lower=0).sum()), 1e-9)
    daily = picks.groupby("session_date")["net_1p5r_strategy_r"].sum()
    return {
        "trades": int(len(picks)), "symbols": int(picks["symbol"].nunique()),
        "sessions": int(picks["session_date"].nunique()),
        "net_expectancy_r": float(picks["net_1p5r_strategy_r"].mean()),
        "stress_net_expectancy_r": float(picks["stress_net_1p5r_strategy_r"].mean()),
        "delay1_net_expectancy_r": float(picks["delay1_net_1p5r_strategy_r"].mean()),
        "remove_best_1_r": remove_best(picks, 1), "remove_best_3_r": remove_best(picks, 3),
        "remove_best_5_r": remove_best(picks, 5),
        "largest_symbol_trade_share_pct": float(by_symbol["count"].max() / len(picks) * 100),
        "largest_symbol_positive_pnl_share_pct": float(max(by_symbol["sum"].max(), 0) / positive_sum * 100),
        "positive_session_pct": float((daily > 0).mean() * 100),
        "worst_session_r": float(daily.min()), "best_session_r": float(daily.max()),
    }


def coefficient_table(model: Pipeline) -> pd.DataFrame:
    coefs = np.asarray(model.named_steps["model"].coef_[0])
    return pd.DataFrame({"feature": MODEL_FEATURES, "coefficient": coefs}).sort_values("coefficient", key=lambda s: s.abs(), ascending=False)


def make_manifest(split: FrozenSplit, cfg: ResearchConfig, selected: int, threshold: float, validation_row: dict) -> dict:
    data = {
        "research_version": 1, "status": "frozen_after_validation",
        "split": asdict(split), "split_fingerprint": split.fingerprint(),
        "config": asdict(cfg), "config_fingerprint": cfg.fingerprint(),
        "features": MODEL_FEATURES, "label": "target_1p5r_before_stop",
        "entry_contract": "completed signal bar -> next 1m open",
        "path_contract": "1 ATR risk unit; 1.5 ATR target; 1 ATR stop; stop-first same-bar ambiguity; 30m horizon",
        "selected_train_top_percent": selected, "score_threshold": threshold,
        "validation": validation_row, "model_file": "ranker_logistic.joblib", "holdout_used": False,
    }
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    data["manifest_fingerprint"] = hashlib.sha256(raw.encode()).hexdigest()
    return data


def write_report(path: Path, inventory: dict, table: pd.DataFrame, selected: int | None, robust: dict) -> None:
    lines = [
        "# Causal Intraday Opportunity Discovery v1", "",
        "## Boundary", "- Train: 2026-07-06..2026-08-25.",
        "- Validation: 2026-08-26..2026-09-10.",
        "- Holdout: 2026-09-11..2026-09-30 (not read in discovery mode).",
        "- Signal features are completed-bar causal; entry reference is next-bar open.",
        "- Score thresholds are derived from TRAIN predictions and applied unchanged to validation.",
        "", "## Inventory", "```json", json.dumps(inventory, indent=2), "```", "",
        "## Validation", table.to_markdown(index=False) if not table.empty else "No validation buckets.", "",
        "## Decision",
        (f"- Freeze train-top-{selected}% score threshold; holdout remains locked." if selected is not None
         else "- No candidate survived net, stressed-cost, and one-extra-bar-delay validation. Do not unlock holdout."),
        "", "## Robustness", "```json", json.dumps(robust, indent=2, default=str), "```", "",
        "This is research evidence, not a deployable edge. Pine comes only after a one-shot holdout pass and forward paper validation.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_candidates(grouped: dict[str, list[Path]], symbols: Sequence[str], start: str, end: str, cfg: ResearchConfig) -> tuple[pd.DataFrame, int]:
    parts, usable = [], 0
    for pos, symbol in enumerate(symbols, start=1):
        raw = load_symbol(symbol, grouped[symbol], start, end)
        if raw.empty:
            continue
        features = prepare_symbol_features(raw, symbol, cfg.baseline_grid_minutes)
        labeled = label_candidates(features, cfg)
        if not labeled.empty:
            parts.append(labeled)
            usable += 1
        if pos % 100 == 0:
            print(f"processed {pos}/{len(symbols)} symbols; usable={usable}", flush=True)
    if not parts:
        return pd.DataFrame(), usable
    return add_cross_sectional_features(pd.concat(parts, ignore_index=True)), usable


def run_discovery(args) -> int:
    split, cfg = FrozenSplit(), ResearchConfig(
        min_price=args.min_price, min_median_turnover_lakh=args.min_median_turnover_lakh,
        baseline_grid_minutes=args.baseline_grid_minutes, max_forward_bars=args.max_forward_bars,
        risk_atr_multiple=args.risk_atr_multiple, roundtrip_cost_pct=args.roundtrip_cost_pct,
        stress_cost_pct=args.stress_cost_pct, max_per_timestamp=args.max_per_timestamp,
        max_trades_per_day=args.max_trades_per_day, symbol_cooldown_minutes=args.symbol_cooldown_minutes,
        min_validation_trades=args.min_validation_trades, top_percentiles=tuple(args.top_percentiles),
    )
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    grouped = discover_files(Path(args.data_root))
    symbols = sorted(grouped)
    if args.symbol_limit > 0:
        symbols = symbols[:args.symbol_limit]
    if not symbols:
        raise RuntimeError(f"No minute CSVs under {args.data_root}")

    candidates, usable = build_candidates(grouped, symbols, split.train_start, split.validation_end, cfg)
    candidates = add_split(candidates, split)
    if (candidates["split"] == "holdout").any():
        raise AssertionError("Holdout leak in discovery mode")
    train = candidates[candidates["split"] == "train"].copy()
    valid = candidates[candidates["split"] == "validation"].copy()
    label = "target_1p5r_before_stop"
    if len(train) < 100 or train[label].nunique() < 2:
        raise RuntimeError("Insufficient train candidates/classes")
    if valid.empty:
        raise RuntimeError("No validation candidates")

    mono = monotonicity(train)
    mono.to_csv(out / "train_feature_monotonicity.csv", index=False)
    model = ranker()
    model.fit(train[MODEL_FEATURES], train[label].astype(int))
    joblib.dump({"model": model, "features": MODEL_FEATURES, "label": label}, out / "ranker_logistic.joblib")
    train_scored, valid_scored = score(model, train), score(model, valid)
    thresholds = train_thresholds(train_scored, cfg.top_percentiles)
    table = bucket_table(valid_scored, thresholds, cfg)
    table.to_csv(out / "validation_rank_buckets.csv", index=False)
    coefficient_table(model).to_csv(out / "ranker_coefficients.csv", index=False)
    valid_scored.to_parquet(out / "validation_candidates_scored.parquet", index=False)
    train_scored.sample(min(100_000, len(train_scored)), random_state=42).to_parquet(out / "train_candidates_sample.parquet", index=False)

    selected = choose_candidate(table, cfg)
    robust = robustness(valid_scored, thresholds[selected], cfg) if selected is not None else {}
    inventory = {
        "data_root": str(args.data_root), "files_found": sum(len(v) for v in grouped.values()),
        "symbols_discovered": len(grouped), "symbols_attempted": len(symbols), "symbols_usable": usable,
        "train_candidates": len(train), "validation_candidates": len(valid),
        "split_fingerprint": split.fingerprint(), "config_fingerprint": cfg.fingerprint(),
    }
    (out / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    if selected is not None:
        row = table[table["train_top_percent"] == selected].iloc[0].to_dict()
        manifest = make_manifest(split, cfg, selected, thresholds[selected], row)
        (out / "FROZEN_CANDIDATE_MANIFEST.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_report(out / "DISCOVERY_REPORT.md", inventory, table, selected, robust)
    print(json.dumps({"selected_train_top_percent": selected, **inventory}, indent=2))
    return 0 if selected is not None else 2


def verify_manifest(manifest: dict, split: FrozenSplit, cfg: ResearchConfig) -> None:
    if manifest.get("split_fingerprint") != split.fingerprint():
        raise RuntimeError("split fingerprint mismatch")
    if manifest.get("config_fingerprint") != cfg.fingerprint():
        raise RuntimeError("config fingerprint mismatch")
    if manifest.get("holdout_used"):
        raise RuntimeError("holdout already used; refusing a second peek")


def run_holdout(args) -> int:
    if not args.unlock_holdout:
        raise RuntimeError("Holdout locked; --unlock-holdout is required")
    split = FrozenSplit()
    out = Path(args.output_dir)
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_cfg = dict(manifest["config"])
    raw_cfg["top_percentiles"] = tuple(raw_cfg["top_percentiles"])
    cfg = ResearchConfig(**raw_cfg)
    verify_manifest(manifest, split, cfg)
    bundle = joblib.load(out / manifest["model_file"])
    model = bundle["model"]
    grouped = discover_files(Path(args.data_root))
    symbols = sorted(grouped)
    if args.symbol_limit > 0:
        symbols = symbols[:args.symbol_limit]
    # Load all history so same-time RVOL in holdout uses prior sessions; only holdout labels are evaluated.
    all_candidates, usable = build_candidates(grouped, symbols, split.train_start, split.holdout_end, cfg)
    all_candidates = add_split(all_candidates, split)
    holdout = all_candidates[all_candidates["split"] == "holdout"].copy()
    if holdout.empty:
        raise RuntimeError("No holdout candidates")
    scored = score(model, holdout)
    threshold = float(manifest["score_threshold"])
    picks = execution_policy(scored, threshold, cfg)
    result = robustness(scored, threshold, cfg)
    result.update({
        "selected_train_top_percent": manifest["selected_train_top_percent"],
        "score_threshold": threshold,
        "symbols_usable": usable,
        "holdout_sessions": int(holdout["session_date"].nunique()),
        "evaluated_once": True,
    })
    scored.to_parquet(out / "HOLDOUT_candidates_scored.parquet", index=False)
    picks.to_csv(out / "HOLDOUT_selected_trades.csv", index=False)
    (out / "HOLDOUT_RESULT.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    manifest["holdout_used"] = True
    manifest["holdout_result_file"] = "HOLDOUT_RESULT.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["discovery", "holdout"], default="discovery")
    p.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    p.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--symbol-limit", type=int, default=0)
    p.add_argument("--min-price", type=float, default=20.0)
    p.add_argument("--min-median-turnover-lakh", type=float, default=2.5)
    p.add_argument("--baseline-grid-minutes", type=int, default=5)
    p.add_argument("--max-forward-bars", type=int, default=30)
    p.add_argument("--risk-atr-multiple", type=float, default=1.0)
    p.add_argument("--roundtrip-cost-pct", type=float, default=0.20)
    p.add_argument("--stress-cost-pct", type=float, default=0.30)
    p.add_argument("--max-per-timestamp", type=int, default=5)
    p.add_argument("--max-trades-per-day", type=int, default=20)
    p.add_argument("--symbol-cooldown-minutes", type=int, default=30)
    p.add_argument("--min-validation-trades", type=int, default=30)
    p.add_argument("--top-percentiles", nargs="+", type=int, default=[1, 2, 5, 10, 20])
    p.add_argument("--manifest", default=str(DEFAULT_OUT_DIR / "FROZEN_CANDIDATE_MANIFEST.json"))
    p.add_argument("--unlock-holdout", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return run_discovery(args) if args.mode == "discovery" else run_holdout(args)


if __name__ == "__main__":
    raise SystemExit(main())

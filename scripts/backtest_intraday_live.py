#!/usr/bin/env python3
"""
Intraday Live-Simulation Backtest — validates AutoPilot detection pipeline.

Uses MINUTE candles from Kite API to simulate the tick scanner minute-by-minute:
  1. Scan daily OHLCV cache for 3%+ intraday movers (cheap local scan)
  2. Pull minute candles from Kite for those candidates
  3. Simulate: entry detection, simplified BlindBuyScorer, fade detector, EOD exit
  4. Report: win rate, avg profit, fade accuracy, P&L

Usage:
    python scripts/backtest_intraday_live.py                      # last 5 trading days
    python scripts/backtest_intraday_live.py --days 10            # last 10 days
    python scripts/backtest_intraday_live.py --date 2026-04-07    # specific date
    python scripts/backtest_intraday_live.py --top 30             # top N per day
"""

import argparse
import csv
import glob
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import warnings
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "results", "Data", "kite_history_cache", "day")

# ──────────────────────────────────────────────────────────────────────
# Thresholds (match AutoPilotInvestor.py)
# ──────────────────────────────────────────────────────────────────────
TICK_ENTRY_PCT = 2.0          # Minimum % change to flag in tick scan
TICK_ALREADY_MOVED_PCT = 16.0 # Too late — train left station
MIN_PRICE = 10.0              # Penny stock filter (scorer hard block)
OHLCV_MIN_PRICE = 50.0       # OHLCV confirmation gate
MIN_VOLUME_DAILY = 100_000    # Minimum daily volume for candidate scan

FADE_DROP_FROM_ENTRY_PCT = 2.0
FADE_DROP_FROM_HIGH_PCT = 4.0
FADE_COOLDOWN_MIN = 10

# Scorer thresholds — aligned with live AutoPilotConfig
SCORE_BLIND_BUY = 65          # Legacy — no longer used as floor
SCORE_CONFIRM_MIN = 52        # Minimum to confirm (low price stocks)
SCORE_MAX = 40                # Score CAP: high score = oversold trap (live: blind_buy_max_score)
MIN_VOL_RATIO = 5.0           # Live: min_volume_ratio = 5.0 (backtest-validated #1 predictor)
MAX_GAP_PCT = 5.0             # Live: max_entry_gap_pct = 5.0 (distance to buy trigger)
MIN_CONFIDENCE = 15.0         # Live: min_confidence = 15.0
MIN_RR2 = 2.0                 # Live: reward-to-risk floor at Target2
VWAP_ENTRY_BUFFER = 0.995     # Live: price must be above VWAP within 0.5%
NEAR_HIGH_BUFFER = 0.97       # Live: price must be within 3% of day high
MORNING_WINDOW_START = 9 * 60 + 20
MORNING_WINDOW_END = 11 * 60 + 30
AFTERNOON_WINDOW_START = 13 * 60 + 30
AFTERNOON_WINDOW_END = 14 * 60 + 30

# Stop loss
SL_ATR_MULTIPLIER = 1.5
SL_FALLBACK_PCT = 0.05       # 5% below entry


# ──────────────────────────────────────────────────────────────────────
# Indicators (simplified — match backtest_intraday_v5.py)
# ──────────────────────────────────────────────────────────────────────

def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


def compute_cci(highs: list, lows: list, closes: list, period: int = 20) -> float:
    if len(closes) < period:
        return 0.0
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(-period, 0)]
    tp_mean = sum(tp) / period
    mean_dev = sum(abs(t - tp_mean) for t in tp) / period
    if mean_dev == 0:
        return 0.0
    return (tp[-1] - tp_mean) / (0.015 * mean_dev)


def compute_volume_ratio(volumes: list, period: int = 20) -> float:
    if len(volumes) < period + 1:
        return 1.0
    avg_vol = sum(volumes[-(period + 1):-1]) / period
    if avg_vol <= 0:
        return 1.0
    return volumes[-1] / avg_vol


def compute_mfi(highs: list, lows: list, closes: list, volumes: list, period: int = 14) -> float:
    """Compute Money Flow Index (0-100). Like RSI but volume-weighted."""
    if len(closes) < period + 1:
        return 50.0
    pos_flow = 0.0
    neg_flow = 0.0
    for i in range(-period, 0):
        tp_now = (highs[i] + lows[i] + closes[i]) / 3
        tp_prev = (highs[i - 1] + lows[i - 1] + closes[i - 1]) / 3
        raw_flow = tp_now * volumes[i]
        if tp_now > tp_prev:
            pos_flow += raw_flow
        else:
            neg_flow += raw_flow
    if neg_flow == 0:
        return 100.0
    mf_ratio = pos_flow / neg_flow
    return 100 - (100 / (1 + mf_ratio))


def compute_daily_turnover_cr(volume: float, close: float) -> float:
    """Compute approximate daily turnover in Crores."""
    return (volume * close) / 1e7 if close > 0 else 0


# Minimum daily turnover (₹ Cr) — from Quantopian Volume/Slippage lecture
# Stocks below this can't absorb ₹50K orders without significant slippage
MIN_TURNOVER_CR = 2.0

_TRADING_SIGNALS = None
_GARCH_VOLATILITY = None


def _rows_to_ohlcv_frame(rows: List[dict]) -> pd.DataFrame:
    """Convert cached OHLCV dict rows into a normalized DataFrame."""
    records = []
    for row in rows:
        try:
            records.append({
                "datetime": row.get("datetime", ""),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
        except Exception:
            continue
    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(records)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    return df[["open", "high", "low", "close", "volume"]]


def _get_trading_signals():
    global _TRADING_SIGNALS
    if _TRADING_SIGNALS is None:
        from pkscreener.classes.screening.signals import TradingSignals
        _TRADING_SIGNALS = TradingSignals()
    return _TRADING_SIGNALS


def _get_garch_volatility():
    global _GARCH_VOLATILITY
    if _GARCH_VOLATILITY is None:
        from pkscreener.classes.AdvancedFilters import GARCHVolatility
        _GARCH_VOLATILITY = GARCHVolatility()
    return _GARCH_VOLATILITY


def _signal_to_action(signal_name: str) -> str:
    """Mirror ActionableReport._signal_to_action()."""
    normalized = str(signal_name).strip().upper()
    if normalized in {"STRONG_BUY", "BUY"}:
        return "BUY"
    if normalized == "WEAK_BUY":
        return "WATCH BUY"
    if normalized in {"STRONG_SELL", "SELL"}:
        return "SELL/EXIT"
    if normalized == "WEAK_SELL":
        return "WATCH SELL"
    return "HOLD"


def _build_trade_plan(close: float, atr: float, action: str, garch_atr_mult: float = 2.0) -> Tuple[float, float, float, float, float]:
    """Mirror ActionableReport._build_trade_plan()."""
    close = max(float(close or 0.0), 0.01)
    atr = max(float(atr or 0.0), 0.0)
    buffer_amt = max(close * 0.002, atr * 0.10)
    risk_mult = max(float(garch_atr_mult or 2.0) * 0.75, 1.0)
    risk_amt = max(close * 0.015, atr * risk_mult)

    if action in {"BUY", "WATCH BUY"}:
        entry = round(close + buffer_amt, 2)
        stop_loss = round(max(entry - risk_amt, 0.01), 2)
        risk = max(entry - stop_loss, 0.01)
        target_1 = round(entry + (risk * 1.5), 2)
        target_2 = round(entry + (risk * 2.5), 2)
    elif action in {"SELL/EXIT", "WATCH SELL"}:
        entry = round(max(close - buffer_amt, 0.01), 2)
        stop_loss = round(entry + risk_amt, 2)
        risk = max(stop_loss - entry, 0.01)
        target_1 = round(max(entry - (risk * 1.5), 0.01), 2)
        target_2 = round(max(entry - (risk * 2.5), 0.01), 2)
    else:
        return round(close, 2), 0.0, 0.0, 0.0, 0.0

    risk_pct = round((risk * 100.0 / entry), 2) if entry > 0 else 0.0
    return entry, stop_loss, target_1, target_2, risk_pct


def _compute_intraday_vwap(candles: List[dict], upto_idx: int) -> float:
    """Compute cumulative intraday VWAP through the given minute."""
    pv_sum = 0.0
    vol_sum = 0.0
    for candle in candles[:upto_idx + 1]:
        vol = float(candle.get("volume") or 0.0)
        if vol <= 0:
            continue
        typical_price = (
            float(candle["high"]) + float(candle["low"]) + float(candle["close"])
        ) / 3.0
        pv_sum += typical_price * vol
        vol_sum += vol
    return pv_sum / vol_sum if vol_sum > 0 else 0.0


def _minutes_of_day(time_text: str) -> int:
    try:
        hh, mm = time_text.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return -1


def _in_prime_trading_window(time_text: str) -> bool:
    mins = _minutes_of_day(time_text)
    if mins < 0:
        return False
    in_morning = MORNING_WINDOW_START <= mins <= MORNING_WINDOW_END
    in_afternoon = AFTERNOON_WINDOW_START <= mins <= AFTERNOON_WINDOW_END
    return in_morning or in_afternoon


def _build_partial_day_frame(history_frame: pd.DataFrame, date_str: str, day_open: float, candles: List[dict], upto_idx: int) -> pd.DataFrame:
    """Append a partial current-day candle to prior daily history."""
    high_so_far = max(float(c["high"]) for c in candles[:upto_idx + 1])
    low_so_far = min(float(c["low"]) for c in candles[:upto_idx + 1])
    close_now = float(candles[upto_idx]["close"])
    cum_volume = float(sum(float(c.get("volume") or 0.0) for c in candles[:upto_idx + 1]))
    partial = pd.DataFrame(
        [{
            "open": float(day_open),
            "high": high_so_far,
            "low": low_so_far,
            "close": close_now,
            "volume": cum_volume,
        }],
        index=[pd.Timestamp(date_str)],
    )
    if history_frame is None or history_frame.empty:
        return partial
    return pd.concat([history_frame, partial])


def _build_live_signal_snapshot(symbol: str, date_str: str, candles: List[dict], entry_idx: int, daily_info: dict) -> Optional[dict]:
    """Build a live-like signal row using history up to the candidate entry minute."""
    history_frame = daily_info.get("history_frame")
    if history_frame is None or history_frame.empty or len(history_frame) < 20:
        return None

    day_open = float(candles[0]["open"])
    snapshot_df = _build_partial_day_frame(history_frame, date_str, day_open, candles, entry_idx)
    if snapshot_df is None or snapshot_df.empty or len(snapshot_df) < 20:
        return None

    signal_result = _get_trading_signals().analyze(snapshot_df)
    close_now = float(snapshot_df["close"].iloc[-1])
    prev_close = float(history_frame["close"].iloc[-1]) if len(history_frame) else close_now
    avg_volume_20 = float(history_frame["volume"].tail(20).mean()) if len(history_frame) else 0.0
    current_volume = float(snapshot_df["volume"].iloc[-1]) if "volume" in snapshot_df.columns else 0.0
    raw_volume_ratio = (current_volume / avg_volume_20) if avg_volume_20 > 0 else 0.0
    mins_from_open = max(1, _minutes_of_day(candles[entry_idx]["time"]) - (9 * 60 + 15) + 1)
    day_fraction = mins_from_open / 375.0
    projected_volume_ratio = raw_volume_ratio / day_fraction if day_fraction > 0 else raw_volume_ratio

    highs = snapshot_df["high"].astype(float).tolist()
    lows = snapshot_df["low"].astype(float).tolist()
    closes = snapshot_df["close"].astype(float).tolist()
    atr = compute_atr(highs, lows, closes)
    returns = snapshot_df["close"].pct_change().dropna().values
    garch_mult = _get_garch_volatility().adaptive_atr_multiplier(returns, base_multiplier=2.0) if len(returns) else 2.0
    action = _signal_to_action(getattr(signal_result.signal, "name", str(signal_result.signal)))
    entry_trigger, stop_loss, _, target_2, _ = _build_trade_plan(close_now, atr, action, garch_atr_mult=garch_mult)
    if entry_trigger <= 0:
        return None

    entry_fill_price = round(entry_trigger, 2)
    entry_gap_pct = round((max(entry_trigger - close_now, 0.0) * 100.0 / close_now), 2) if close_now > 0 else 0.0
    vwap = _compute_intraday_vwap(candles, entry_idx)
    high_so_far = max(float(c["high"]) for c in candles[:entry_idx + 1])
    cum_volume = float(sum(float(c.get("volume") or 0.0) for c in candles[:entry_idx + 1]))
    turnover_price = vwap if vwap > 0 else close_now
    turnover_cr = compute_daily_turnover_cr(cum_volume, turnover_price)
    change_pct = ((entry_fill_price - prev_close) * 100.0 / prev_close) if prev_close > 0 else 0.0
    change_from_open = ((entry_fill_price - day_open) * 100.0 / day_open) if day_open > 0 else 0.0
    gap_pct = ((day_open - prev_close) * 100.0 / prev_close) if prev_close > 0 else 0.0
    rr2 = 0.0
    risk = entry_fill_price - stop_loss
    if stop_loss > 0 and target_2 > 0 and risk > 0:
        rr2 = (target_2 - entry_fill_price) / risk

    blind_score = simplified_blind_score(
        price=entry_fill_price,
        change_pct=change_from_open,
        rsi=float(signal_result.indicators.get("RSI", 50.0)),
        cci=float(signal_result.indicators.get("CCI", 0.0)),
        mfi=float(signal_result.indicators.get("MFI", 50.0)),
        volume_ratio=projected_volume_ratio,
        gap_pct=gap_pct,
    )

    return {
        "action": action,
        "confidence": round(float(signal_result.confidence), 1),
        "score": int(round(float(blind_score))),
        "score_reasons": "",
        "entry_price": entry_fill_price,
        "entry_trigger": round(entry_trigger, 2),
        "entry_gap_pct": entry_gap_pct,
        "stop_loss": round(stop_loss, 2),
        "target_2": round(target_2, 2),
        "rr2": round(rr2, 2),
        "volume_ratio": round(projected_volume_ratio, 2),
        "vwap": round(vwap, 2),
        "turnover_cr": round(turnover_cr, 2),
        "high_so_far": round(high_so_far, 2),
        "prev_close": round(prev_close, 2),
        "change_pct": round(change_pct, 2),
    }


def _load_float_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name, default)
    try:
        return float(raw_value)
    except Exception:
        return float(default)


DEFAULT_BUY_COST_PCT = _load_float_env("CHARAN_EST_BUY_COST_PCT", 0.00118)
DEFAULT_SELL_COST_PCT = _load_float_env("CHARAN_EST_SELL_COST_PCT", 0.00104)
DEFAULT_SLIPPAGE_BPS = _load_float_env("CHARAN_EST_SLIPPAGE_BPS", 0.0)


@dataclass(frozen=True)
class CostModel:
    buy_cost_pct: float = DEFAULT_BUY_COST_PCT
    sell_cost_pct: float = DEFAULT_SELL_COST_PCT
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS


def _apply_trade_costs(entry_price: float, exit_price: float, cost_model: CostModel) -> dict:
    """Convert raw entry/exit prices into net-of-cost trade economics."""
    if entry_price <= 0 or exit_price <= 0:
        return {
            "exec_entry_price": entry_price,
            "exec_exit_price": exit_price,
            "gross_pnl_pct": 0.0,
            "net_pnl_pct": 0.0,
            "entry_charges": 0.0,
            "exit_charges": 0.0,
            "slippage_cost": 0.0,
        }

    slip_frac = max(0.0, cost_model.slippage_bps) / 10000.0
    exec_entry = entry_price * (1 + slip_frac)
    exec_exit = exit_price * (1 - slip_frac)
    entry_charges = exec_entry * max(0.0, cost_model.buy_cost_pct)
    exit_charges = exec_exit * max(0.0, cost_model.sell_cost_pct)
    gross_pnl_pct = ((exit_price - entry_price) / entry_price) * 100
    net_capital = exec_entry + entry_charges
    net_pnl = exec_exit - exec_entry - entry_charges - exit_charges
    net_pnl_pct = (net_pnl / net_capital) * 100 if net_capital > 0 else 0.0
    slippage_cost = max(0.0, exec_entry - entry_price) + max(0.0, exit_price - exec_exit)
    return {
        "exec_entry_price": round(exec_entry, 2),
        "exec_exit_price": round(exec_exit, 2),
        "gross_pnl_pct": round(gross_pnl_pct, 2),
        "net_pnl_pct": round(net_pnl_pct, 2),
        "entry_charges": round(entry_charges, 2),
        "exit_charges": round(exit_charges, 2),
        "slippage_cost": round(slippage_cost, 2),
    }


# ──────────────────────────────────────────────────────────────────────
# Simplified BlindBuyScorer (matches CONSOLIDATED oscillator logic)
# ──────────────────────────────────────────────────────────────────────

def simplified_blind_score(
    price: float,
    change_pct: float,
    rsi: float,
    cci: float,
    mfi: float,
    volume_ratio: float,
    gap_pct: float = 0.0,
) -> int:
    """Simplified scorer matching the consolidated oscillator BlindBuyScorer.

    Uses single median of RSI/CCI(normalized)/MFI instead of scoring each
    independently — reduces correlated factor triple-counting.
    """
    if price < MIN_PRICE:
        return 0

    score = 0
    _high_vol_momentum = volume_ratio >= 2.0

    # CONSOLIDATED OSCILLATOR: median of RSI, CCI(norm), MFI
    _cci_norm = max(0.0, min(100.0, (cci + 200) / 4.0))  # -200→0, 0→50, +200→100
    _osc_values = sorted([rsi, _cci_norm, mfi])
    _osc_median = _osc_values[1]  # median of 3

    if _osc_median < 25:
        score += 25
    elif _osc_median < 35:
        score += 18
    elif _osc_median < 50:
        score += 10
    elif _osc_median > 80:
        if change_pct >= 5.0 or _high_vol_momentum:
            score -= 5
        else:
            score -= 12
    elif _osc_median > 65:
        if not (change_pct >= 5.0 or _high_vol_momentum):
            score -= 5

    # Volume component
    if volume_ratio >= 2.5:
        score += 20
    elif volume_ratio >= 1.5:
        score += 15
    elif volume_ratio >= 1.0:
        score += 10
    elif volume_ratio >= 0.7:
        score += 5

    # Gap-up bonus
    if gap_pct >= 7:
        score += 15
    elif gap_pct >= 4:
        score += 10

    # Change% / green candle
    _change_cap = 10 if gap_pct >= 4 else 15
    if change_pct >= 5:
        score += _change_cap
    elif change_pct >= 2.5:
        score += min(12, _change_cap)
    elif change_pct >= 1:
        score += min(10, _change_cap)
    elif change_pct > 0:
        score += 6
    elif change_pct > -1:
        score += 2
    else:
        score -= 5

    return max(0, min(100, score))


# ──────────────────────────────────────────────────────────────────────
# Trade result
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    date: str
    signal_action: str
    entry_time: str       # HH:MM when we entered
    entry_price: float
    exit_price: float
    exec_entry_price: float
    exec_exit_price: float
    exit_time: str
    exit_reason: str      # "EOD", "FADE_WARNING", "FADE_EXIT", "STOP_HIT"
    change_at_entry: float  # % change from open when we entered
    high_after_entry: float
    low_after_entry: float
    score: int
    confidence_pct: float
    rr2: float
    entry_gap_pct: float
    stop_loss: float
    target_2: float
    vwap_at_entry: float
    volume_ratio: float
    gross_pnl_pct: float
    pnl_pct: float        # net-of-costs return%
    max_profit_pct: float  # (high - entry) / entry * 100
    max_drawdown_pct: float  # (entry - low) / entry * 100
    entry_charges: float
    exit_charges: float
    slippage_cost: float
    faded: bool           # Did close < entry?
    fade_detected: bool   # Did our fade detector catch it?

    @property
    def won(self) -> bool:
        return self.pnl_pct > 0


# ──────────────────────────────────────────────────────────────────────
# Step 1: Scan daily OHLCV for candidates
# ──────────────────────────────────────────────────────────────────────

def scan_daily_candidates(target_dates: List[str], top_n: int = 0) -> Dict[str, List[dict]]:
    """
    Scan daily OHLCV cache for stocks with 2%+ intraday high on target dates.
    Uses 2% (not 3%) to capture ALL stocks the tick scanner would flag,
    eliminating look-ahead / survivorship bias.
    Returns {date_str: [candidate_dicts]} sorted by high_change desc.
    If top_n > 0, limits per day (for API budget); 0 = all candidates.
    """
    files = glob.glob(os.path.join(DATA_DIR, "*.csv"))
    candidates = defaultdict(list)

    for fpath in files:
        sym = os.path.basename(fpath).replace(".csv", "")
        try:
            with open(fpath) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception:
            continue

        # Build history for indicators (need last ~30 rows before target)
        for row in rows:
            dt = row.get("datetime", "")[:10]
            if dt not in target_dates:
                continue

            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
            v = float(row["volume"])

            if o <= 0 or v < MIN_VOLUME_DAILY or o < OHLCV_MIN_PRICE:
                continue

            high_chg = (h - o) / o * 100
            close_chg = (c - o) / o * 100

            # Use TICK_ENTRY_PCT (2%) — the SAME threshold the live scanner uses.
            # This eliminates look-ahead bias: we include every stock the scanner
            # would have flagged, not just the ones that went on to 3%+.
            if high_chg < TICK_ENTRY_PCT:
                continue
            if high_chg > 25.0:
                continue  # Circuit hits — not tradeable

            # Use only history available BEFORE the target session to avoid
            # leaking the full day candle into pre-entry indicators.
            idx = rows.index(row)
            history_rows = rows[max(0, idx - 90):idx]
            if len(history_rows) < 20:
                continue
            prior_closes = [float(r["close"]) for r in history_rows]
            prior_highs = [float(r["high"]) for r in history_rows]
            prior_lows = [float(r["low"]) for r in history_rows]
            prior_volumes = [float(r["volume"]) for r in history_rows]

            # Find prev_close for gap calculation
            prev_close = float(rows[idx - 1]["close"]) if idx > 0 else o
            gap_pct = (o - prev_close) / prev_close * 100 if prev_close > 0 else 0

            rsi = compute_rsi(prior_closes)
            atr = compute_atr(prior_highs, prior_lows, prior_closes)
            cci = compute_cci(prior_highs, prior_lows, prior_closes)
            vol_ratio = compute_volume_ratio(prior_volumes)
            mfi = compute_mfi(prior_highs, prior_lows, prior_closes, prior_volumes)
            turnover_cr = compute_daily_turnover_cr(v, c)

            # Liquidity gate — skip stocks that can't absorb ₹50K orders
            if turnover_cr < MIN_TURNOVER_CR:
                continue

            candidates[dt].append({
                "symbol": sym,
                "open": o, "high": h, "low": l, "close": c, "volume": v,
                "high_chg": round(high_chg, 2),
                "close_chg": round(close_chg, 2),
                "gap_pct": round(gap_pct, 2),
                "rsi": round(rsi, 1),
                "atr": round(atr, 2),
                "cci": round(cci, 1),
                "mfi": round(mfi, 1),
                "vol_ratio": round(vol_ratio, 2),
                "turnover_cr": round(turnover_cr, 1),
                "faded": close_chg < high_chg * 0.5,
                "prev_close": prev_close,
                "history_frame": _rows_to_ohlcv_frame(history_rows),
            })

    # Sort and optionally limit per day
    for dt in candidates:
        candidates[dt].sort(key=lambda x: -x["high_chg"])
        if top_n > 0:
            candidates[dt] = candidates[dt][:top_n]

    return dict(candidates)


# ──────────────────────────────────────────────────────────────────────
# Step 2: Pull minute candles from Kite API
# ──────────────────────────────────────────────────────────────────────

def pull_minute_candles(symbols: List[str], date_str: str) -> Dict[str, list]:
    """
    Pull minute candles for a list of symbols for a single date.
    Returns {symbol: [(datetime, open, high, low, close, volume), ...]}.
    Rate-limited to ~3 req/sec to respect Kite API limits.
    """
    from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider
    provider = KiteHistoricalDataProvider()

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    from_date = dt.replace(hour=9, minute=15, second=0)
    to_date = dt.replace(hour=15, minute=30, second=0)

    minute_data = {}
    total = len(symbols)

    for i, sym in enumerate(symbols):
        try:
            df = provider.get_history_range(
                sym, interval="minute",
                from_date=from_date, to_date=to_date,
            )
            if df is not None and not df.empty:
                candles = []
                for ts, row in df.iterrows():
                    # Handle timezone-aware timestamps
                    try:
                        t_str = ts.strftime("%H:%M")
                    except Exception:
                        t_str = str(ts)[-14:-9]  # fallback
                    candles.append({
                        "time": t_str,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(row["volume"]),
                    })
                if len(candles) >= 5:  # Need at least 5 minute candles
                    minute_data[sym] = candles
                if (i + 1) % 10 == 0:
                    print(f"  Pulled {i + 1}/{total} symbols...")
            time.sleep(0.35)  # ~3 req/sec rate limit
        except Exception as exc:
            print(f"  Failed {sym}: {exc}")
            time.sleep(0.5)

    return minute_data


# ──────────────────────────────────────────────────────────────────────
# Step 3: Simulate minute-by-minute
# ──────────────────────────────────────────────────────────────────────

def simulate_intraday(
    symbol: str,
    date_str: str,
    candles: list,
    daily_info: dict,
    cost_model: CostModel,
) -> Optional[Trade]:
    """
    Simulate the tick scanner + fade detector on minute candles.

    Entry: first minute where cumulative change from open >= TICK_ENTRY_PCT
    Score: simplified scorer at entry time
    Fade: monitor for WARNING/EXIT/STOP after entry
    Exit: fade exit or EOD close
    """
    if not candles or len(candles) < 5:
        return None

    day_open = candles[0]["open"]
    if day_open <= 0:
        return None

    entry_idx = None
    entry_time = None
    entry_price = 0.0
    change_at_entry = 0.0
    score = 0
    vol_ratio = 0.0
    stop_loss = 0.0
    confidence_pct = 0.0
    rr2 = 0.0
    entry_gap_pct = 0.0
    target_2 = 0.0
    vwap_at_entry = 0.0
    signal_action = ""
    high_so_far = day_open

    # ── Find the first minute where a live-like buy trigger would have passed ──
    last_eval_high = day_open
    for i, candle in enumerate(candles):
        candidate_time = candle["time"]
        if not _in_prime_trading_window(candidate_time):
            continue
        candle_high = float(candle["high"])
        if candle_high < day_open * (1 + TICK_ENTRY_PCT / 100.0):
            continue
        should_recheck = candle_high > last_eval_high or (i % 5 == 0)
        last_eval_high = max(last_eval_high, candle_high)
        if not should_recheck:
            continue

        signal_state = _build_live_signal_snapshot(symbol, date_str, candles, i, daily_info)
        if signal_state is None:
            continue

        candidate_entry = float(signal_state["entry_price"])
        if candle_high < candidate_entry:
            continue

        candidate_change = max(
            (candidate_entry - day_open) / day_open * 100,
            (candle_high - day_open) / day_open * 100,
        )
        if candidate_change >= TICK_ALREADY_MOVED_PCT:
            continue

        candidate_score = int(signal_state["score"])
        if candidate_score < SCORE_CONFIRM_MIN and candidate_entry < OHLCV_MIN_PRICE:
            continue
        if candidate_score >= SCORE_MAX:
            continue
        if signal_state["action"] not in {"BUY", "WATCH BUY"}:
            continue
        if float(signal_state["confidence"]) < MIN_CONFIDENCE:
            continue
        if float(signal_state["volume_ratio"]) < MIN_VOL_RATIO:
            continue
        if float(signal_state["entry_gap_pct"]) > MAX_GAP_PCT:
            continue
        if float(signal_state["rr2"]) < MIN_RR2:
            continue
        if float(signal_state["vwap"]) > 0 and candidate_entry < float(signal_state["vwap"]) * VWAP_ENTRY_BUFFER:
            continue
        if float(signal_state["high_so_far"]) > 0 and candidate_entry < float(signal_state["high_so_far"]) * NEAR_HIGH_BUFFER:
            continue

        mins_from_open = _minutes_of_day(candidate_time) - (9 * 60 + 15)
        min_turnover = 0.5 if mins_from_open <= 30 else MIN_TURNOVER_CR
        if float(signal_state["turnover_cr"]) > 0 and float(signal_state["turnover_cr"]) < min_turnover:
            continue

        entry_idx = i
        entry_time = candidate_time
        entry_price = round(candidate_entry, 2)
        change_at_entry = round(candidate_change, 2)
        score = candidate_score
        vol_ratio = round(float(signal_state["volume_ratio"]), 2)
        stop_loss = round(float(signal_state["stop_loss"]), 2)
        confidence_pct = round(float(signal_state["confidence"]), 1)
        rr2 = round(float(signal_state["rr2"]), 2)
        entry_gap_pct = round(float(signal_state["entry_gap_pct"]), 2)
        target_2 = round(float(signal_state["target_2"]), 2)
        vwap_at_entry = round(float(signal_state["vwap"]), 2)
        signal_action = str(signal_state["action"])
        high_so_far = float(signal_state["high_so_far"])
        break

    if entry_idx is None:
        return None

    # ── Monitor after entry: fade detection + exit ──
    high_after = entry_price
    low_after = entry_price
    exit_price = None
    exit_time = None
    exit_reason = "EOD"
    fade_detected = False
    last_fade_alert_idx = -999

    for j in range(entry_idx + 1, len(candles)):
        c = candles[j]
        price = c["close"]
        high_after = max(high_after, c["high"])
        low_after = min(low_after, c["low"])

        # Check STOP HIT (worst case: low touched SL)
        if c["low"] <= stop_loss and (j - last_fade_alert_idx) >= FADE_COOLDOWN_MIN:
            exit_price = stop_loss
            exit_time = c["time"]
            exit_reason = "STOP_HIT"
            fade_detected = True
            break

        # Check FADE EXIT: drop from high >= 4%
        if high_after > 0:
            drop_from_high = (high_after - price) / high_after * 100
            if drop_from_high >= FADE_DROP_FROM_HIGH_PCT and (j - last_fade_alert_idx) >= FADE_COOLDOWN_MIN:
                exit_price = price
                exit_time = c["time"]
                exit_reason = "FADE_EXIT"
                fade_detected = True
                last_fade_alert_idx = j
                break

        # Check FADE WARNING: drop from entry >= 2%
        drop_from_entry = (entry_price - price) / entry_price * 100
        if drop_from_entry >= FADE_DROP_FROM_ENTRY_PCT and (j - last_fade_alert_idx) >= FADE_COOLDOWN_MIN:
            # Log warning but don't exit (just flag)
            last_fade_alert_idx = j

    if exit_price is None:
        # EOD exit at last candle close
        exit_price = candles[-1]["close"]
        exit_time = candles[-1]["time"]

    gross_pnl_pct = (exit_price - entry_price) / entry_price * 100
    max_profit = (high_after - entry_price) / entry_price * 100
    max_dd = (entry_price - low_after) / entry_price * 100
    actual_close = candles[-1]["close"]
    did_fade = actual_close < entry_price
    cost_result = _apply_trade_costs(entry_price, exit_price, cost_model)

    return Trade(
        symbol=symbol,
        date=date_str,
        signal_action=signal_action,
        entry_time=entry_time,
        entry_price=round(entry_price, 2),
        exit_price=round(exit_price, 2),
        exec_entry_price=cost_result["exec_entry_price"],
        exec_exit_price=cost_result["exec_exit_price"],
        exit_time=exit_time,
        exit_reason=exit_reason,
        change_at_entry=round(change_at_entry, 2),
        high_after_entry=round(high_after, 2),
        low_after_entry=round(low_after, 2),
        score=score,
        confidence_pct=confidence_pct,
        rr2=rr2,
        entry_gap_pct=entry_gap_pct,
        stop_loss=round(stop_loss, 2),
        target_2=target_2,
        vwap_at_entry=vwap_at_entry,
        volume_ratio=round(vol_ratio, 2),
        gross_pnl_pct=round(gross_pnl_pct, 2),
        pnl_pct=cost_result["net_pnl_pct"],
        max_profit_pct=round(max_profit, 2),
        max_drawdown_pct=round(max_dd, 2),
        entry_charges=cost_result["entry_charges"],
        exit_charges=cost_result["exit_charges"],
        slippage_cost=cost_result["slippage_cost"],
        faded=did_fade,
        fade_detected=fade_detected,
    )


# ──────────────────────────────────────────────────────────────────────
# Step 4: Report
# ──────────────────────────────────────────────────────────────────────

def print_report(all_trades: List[Trade], candidates_per_day: Dict[str, int], cost_model: CostModel):
    """Print comprehensive backtest report."""
    if not all_trades:
        print("\n❌ No trades executed. Check data availability.")
        return

    print("\n" + "=" * 90)
    print("  INTRADAY LIVE-SIMULATION BACKTEST REPORT")
    print("=" * 90)

    # Overall stats
    total = len(all_trades)
    winners = [t for t in all_trades if t.won]
    losers = [t for t in all_trades if not t.won]
    faders = [t for t in all_trades if t.faded]
    fade_caught = [t for t in all_trades if t.faded and t.fade_detected]
    fade_false = [t for t in all_trades if not t.faded and t.fade_detected]

    avg_gross_pnl = sum(t.gross_pnl_pct for t in all_trades) / total
    avg_pnl = sum(t.pnl_pct for t in all_trades) / total
    avg_win = sum(t.pnl_pct for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t.pnl_pct for t in losers) / len(losers) if losers else 0
    avg_max_profit = sum(t.max_profit_pct for t in all_trades) / total
    avg_max_dd = sum(t.max_drawdown_pct for t in all_trades) / total
    avg_cost_drag = sum((t.gross_pnl_pct - t.pnl_pct) for t in all_trades) / total
    gross_winners = [t for t in all_trades if t.gross_pnl_pct > 0]

    win_rate = len(winners) / total * 100
    gross_win_rate = len(gross_winners) / total * 100 if total else 0
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    print(f"\n  Total Candidates Scanned: {sum(candidates_per_day.values())} across {len(candidates_per_day)} days")
    print(f"  Total Trades Simulated:  {total}")
    print(
        f"  Net Assumptions:         buy {cost_model.buy_cost_pct*100:.3f}% | "
        f"sell {cost_model.sell_cost_pct*100:.3f}% | "
        f"slippage {cost_model.slippage_bps:.1f} bps/side"
    )
    print(f"  Gross Win Rate:          {gross_win_rate:.1f}%")
    print(f"  Net Win Rate:            {win_rate:.1f}% ({len(winners)}W / {len(losers)}L)")
    print(f"  Avg Gross P&L/Trade:     {avg_gross_pnl:+.2f}%")
    print(f"  Avg Net P&L/Trade:       {avg_pnl:+.2f}%")
    print(f"  Avg Cost Drag:           -{avg_cost_drag:.2f}%")
    print(f"  Avg Net Winner:          {avg_win:+.2f}%")
    print(f"  Avg Net Loser:           {avg_loss:+.2f}%")
    print(f"  Net Expectancy:          {expectancy:+.3f}% per trade")
    print(f"  Avg Max Profit (peak):   +{avg_max_profit:.2f}%")
    print(f"  Avg Max Drawdown:        -{avg_max_dd:.2f}%")

    # Fade detection accuracy
    print(f"\n  ── Fade Detector Accuracy ──")
    print(f"  Total Faders (close < entry):  {len(faders)}/{total} ({len(faders)/total*100:.0f}%)")
    if faders:
        print(f"  Fade Caught (early exit):      {len(fade_caught)}/{len(faders)} ({len(fade_caught)/len(faders)*100:.0f}%)")
        fade_saved_pnl = sum(t.pnl_pct for t in fade_caught)
        alt_pnl = sum((t.exit_price - t.entry_price) / t.entry_price * 100 for t in fade_caught)
        print(f"  Fade Saved (avoided loss):     {fade_saved_pnl:+.2f}% total")
    print(f"  False Fade Exits (good stock): {len(fade_false)}")
    if fade_false:
        # These are stocks that were HELD at close but fade detector kicked us out early
        missed_profit = sum(
            ((candle_close - t.exit_price) / t.exit_price * 100)
            for t in fade_false
            for candle_close in [t.exit_price]  # We exited at fade price, but stock recovered
        )

    # Spearman rank correlation: score vs P&L
    print(f"\n  ── Scorer Validation (Spearman) ──")
    if total >= 10:
        import math
        scores = [t.score for t in all_trades]
        pnls = [t.pnl_pct for t in all_trades]
        n = len(scores)
        def _rank(arr):
            sorted_idx = sorted(range(n), key=lambda i: arr[i])
            ranks = [0.0] * n
            for rv, idx in enumerate(sorted_idx):
                ranks[idx] = rv + 1
            return ranks
        r_scores = _rank(scores)
        r_pnls = _rank(pnls)
        d_sq = sum((r_scores[i] - r_pnls[i]) ** 2 for i in range(n))
        rho = 1 - (6 * d_sq) / (n * (n ** 2 - 1))
        if rho >= 0.20:
            verdict = "🟢 STRONG EDGE"
        elif rho >= 0.10:
            verdict = "🟡 WEAK EDGE"
        elif rho >= 0.05:
            verdict = "🟠 MARGINAL"
        elif rho >= -0.05:
            verdict = "🔴 NO EDGE (random)"
        else:
            verdict = "⛔ ANTI-PREDICTIVE"
        print(f"  Score vs Return ρ: {rho:+.3f}  → {verdict}")
        # Also test volume_ratio
        vols = [t.volume_ratio for t in all_trades]
        r_vols = _rank(vols)
        d_sq_v = sum((r_vols[i] - r_pnls[i]) ** 2 for i in range(n))
        rho_vol = 1 - (6 * d_sq_v) / (n * (n ** 2 - 1))
        print(f"  Volume vs Return ρ: {rho_vol:+.3f}")
    else:
        print(f"  Need ≥10 trades for Spearman validation (have {total})")

    # Score distribution
    print(f"\n  ── Score Distribution ──")
    score_buckets = defaultdict(list)
    for t in all_trades:
        if t.score >= 70:
            score_buckets["A+ (70+)"].append(t)
        elif t.score >= 60:
            score_buckets["A  (60-69)"].append(t)
        elif t.score >= 52:
            score_buckets["B  (52-59)"].append(t)
        else:
            score_buckets["C  (<52)"].append(t)

    for bucket in ["A+ (70+)", "A  (60-69)", "B  (52-59)", "C  (<52)"]:
        trades = score_buckets.get(bucket, [])
        if not trades:
            continue
        wr = sum(1 for t in trades if t.won) / len(trades) * 100
        ap = sum(t.pnl_pct for t in trades) / len(trades)
        gross_ap = sum(t.gross_pnl_pct for t in trades) / len(trades)
        print(f"    {bucket}: {len(trades):3d} trades | WR {wr:5.1f}% | Net {ap:+.2f}% | Gross {gross_ap:+.2f}%")

    # Per-day breakdown
    print(f"\n  ── Per-Day Breakdown ──")
    by_date = defaultdict(list)
    for t in all_trades:
        by_date[t.date].append(t)

    print(f"  {'Date':<12s} {'Candidates':>10s} {'Trades':>7s} {'Wins':>5s} {'Win%':>6s} {'Net Avg':>8s} {'Gross':>8s} {'Best':>8s} {'Worst':>8s}")
    print(f"  {'─' * 12} {'─' * 10} {'─' * 7} {'─' * 5} {'─' * 6} {'─' * 8} {'─' * 8} {'─' * 8} {'─' * 8}")
    for dt in sorted(by_date.keys()):
        trades = by_date[dt]
        n = len(trades)
        w = sum(1 for t in trades if t.won)
        wr = w / n * 100 if n else 0
        ap = sum(t.pnl_pct for t in trades) / n
        gross_ap = sum(t.gross_pnl_pct for t in trades) / n
        best = max(t.pnl_pct for t in trades)
        worst = min(t.pnl_pct for t in trades)
        cand = candidates_per_day.get(dt, 0)
        print(f"  {dt:<12s} {cand:>10d} {n:>7d} {w:>5d} {wr:>5.1f}% {ap:>+7.2f}% {gross_ap:>+7.2f}% {best:>+7.2f}% {worst:>+7.2f}%")

    # Top 10 best trades
    print(f"\n  ── Top 10 Winners ──")
    top = sorted(all_trades, key=lambda t: -t.pnl_pct)[:10]
    print(f"  {'Symbol':<15s} {'Date':<12s} {'Entry':>6s} {'Exit':>6s} {'Net':>7s} {'Gross':>7s} {'Score':>6s} {'Entry@':>7s} {'Exit@':>7s} {'Reason':<12s}")
    print(f"  {'─' * 15} {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 12}")
    for t in top:
        print(f"  {t.symbol:<15s} {t.date:<12s} {t.entry_price:>6.0f} {t.exit_price:>6.0f} {t.pnl_pct:>+6.2f}% {t.gross_pnl_pct:>+6.2f}% {t.score:>5d} {t.entry_time:>7s} {t.exit_time:>7s} {t.exit_reason:<12s}")

    # Top 10 worst trades
    print(f"\n  ── Top 10 Losers ──")
    bottom = sorted(all_trades, key=lambda t: t.pnl_pct)[:10]
    print(f"  {'Symbol':<15s} {'Date':<12s} {'Entry':>6s} {'Exit':>6s} {'Net':>7s} {'Gross':>7s} {'Score':>6s} {'Entry@':>7s} {'Exit@':>7s} {'Reason':<12s}")
    print(f"  {'─' * 15} {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 12}")
    for t in bottom:
        print(f"  {t.symbol:<15s} {t.date:<12s} {t.entry_price:>6.0f} {t.exit_price:>6.0f} {t.pnl_pct:>+6.2f}% {t.gross_pnl_pct:>+6.2f}% {t.score:>5d} {t.entry_time:>7s} {t.exit_time:>7s} {t.exit_reason:<12s}")

    # Exit reason breakdown
    print(f"\n  ── Exit Reason Breakdown ──")
    by_reason = defaultdict(list)
    for t in all_trades:
        by_reason[t.exit_reason].append(t)
    for reason in ["EOD", "FADE_EXIT", "STOP_HIT"]:
        trades = by_reason.get(reason, [])
        if not trades:
            continue
        wr = sum(1 for t in trades if t.won) / len(trades) * 100
        ap = sum(t.pnl_pct for t in trades) / len(trades)
        gross_ap = sum(t.gross_pnl_pct for t in trades) / len(trades)
        print(f"    {reason:<12s}: {len(trades):3d} trades | WR {wr:5.1f}% | Net {ap:+.2f}% | Gross {gross_ap:+.2f}%")

    print("\n" + "=" * 90)


# ──────────────────────────────────────────────────────────────────────
# Step 5: CSV export
# ──────────────────────────────────────────────────────────────────────

def export_trades_csv(trades: List[Trade], filepath: str):
    """Export trades to CSV for further analysis."""
    if not trades:
        return
    fieldnames = [
        "date", "symbol", "entry_time", "entry_price", "exit_time", "exit_price",
        "exec_entry_price", "exec_exit_price",
        "exit_reason", "gross_pnl_pct", "net_pnl_pct", "max_profit_pct", "max_drawdown_pct",
        "signal_action", "score", "confidence_pct", "rr2", "entry_gap_pct",
        "stop_loss", "target_2", "vwap_at_entry", "volume_ratio", "change_at_entry", "entry_charges", "exit_charges",
        "slippage_cost", "faded", "fade_detected",
    ]
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            writer.writerow({
                "date": t.date,
                "symbol": t.symbol,
                "entry_time": t.entry_time,
                "entry_price": t.entry_price,
                "exit_time": t.exit_time,
                "exit_price": t.exit_price,
                "exec_entry_price": t.exec_entry_price,
                "exec_exit_price": t.exec_exit_price,
                "exit_reason": t.exit_reason,
                "gross_pnl_pct": t.gross_pnl_pct,
                "net_pnl_pct": t.pnl_pct,
                "max_profit_pct": t.max_profit_pct,
                "max_drawdown_pct": t.max_drawdown_pct,
                "signal_action": t.signal_action,
                "score": t.score,
                "confidence_pct": t.confidence_pct,
                "rr2": t.rr2,
                "entry_gap_pct": t.entry_gap_pct,
                "stop_loss": t.stop_loss,
                "target_2": t.target_2,
                "vwap_at_entry": t.vwap_at_entry,
                "volume_ratio": t.volume_ratio,
                "change_at_entry": t.change_at_entry,
                "entry_charges": t.entry_charges,
                "exit_charges": t.exit_charges,
                "slippage_cost": t.slippage_cost,
                "faded": t.faded,
                "fade_detected": t.fade_detected,
            })
    print(f"\n  Trades exported to: {filepath}")


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def get_recent_trading_days(n: int = 5) -> List[str]:
    """Get the last N trading dates from daily OHLCV cache."""
    # Pick the most liquid stock to find recent dates
    sample_files = glob.glob(os.path.join(DATA_DIR, "*.csv"))[:20]
    all_dates = set()
    for fpath in sample_files:
        try:
            with open(fpath) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    all_dates.add(row["datetime"][:10])
        except Exception:
            continue
    sorted_dates = sorted(all_dates, reverse=True)
    return sorted_dates[:n]


def main():
    parser = argparse.ArgumentParser(description="Intraday live-simulation backtest")
    parser.add_argument("--days", type=int, default=5, help="Number of recent trading days")
    parser.add_argument("--date", type=str, default=None, help="Specific date (YYYY-MM-DD)")
    parser.add_argument("--top", type=int, default=0, help="Top N candidates per day (0 = all, for honest test)")
    parser.add_argument("--no-api", action="store_true", help="Skip Kite API, use daily-only simulation")
    parser.add_argument("--export", type=str, default=None, help="Export trades to CSV file")
    parser.add_argument("--buy-cost-pct", type=float, default=DEFAULT_BUY_COST_PCT, help="Estimated buy-side cost percentage")
    parser.add_argument("--sell-cost-pct", type=float, default=DEFAULT_SELL_COST_PCT, help="Estimated sell-side cost percentage")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS, help="Estimated slippage in basis points per side")
    parser.add_argument("--min-vol-ratio", type=float, default=None, help="Override min volume ratio filter")
    parser.add_argument("--morning-start", type=str, default=None, help="Morning window start HH:MM (override)")
    parser.add_argument("--morning-end", type=str, default=None, help="Morning window end HH:MM (override)")
    parser.add_argument("--afternoon-start", type=str, default=None, help="Afternoon window start HH:MM (override)")
    parser.add_argument("--afternoon-end", type=str, default=None, help="Afternoon window end HH:MM (override)")
    parser.add_argument("--no-afternoon", action="store_true", help="Disable afternoon window (morning-only)")
    args = parser.parse_args()

    global MIN_VOL_RATIO, MORNING_WINDOW_START, MORNING_WINDOW_END, AFTERNOON_WINDOW_START, AFTERNOON_WINDOW_END
    if args.min_vol_ratio is not None:
        MIN_VOL_RATIO = max(0.0, float(args.min_vol_ratio))
    if args.morning_start:
        try:
            hh, mm = args.morning_start.split(":")
            MORNING_WINDOW_START = int(hh) * 60 + int(mm)
        except Exception:
            pass
    if args.morning_end:
        try:
            hh, mm = args.morning_end.split(":")
            MORNING_WINDOW_END = int(hh) * 60 + int(mm)
        except Exception:
            pass
    if args.afternoon_start:
        try:
            hh, mm = args.afternoon_start.split(":")
            AFTERNOON_WINDOW_START = int(hh) * 60 + int(mm)
        except Exception:
            pass
    if args.afternoon_end:
        try:
            hh, mm = args.afternoon_end.split(":")
            AFTERNOON_WINDOW_END = int(hh) * 60 + int(mm)
        except Exception:
            pass
    if args.no_afternoon:
        AFTERNOON_WINDOW_START = -1
        AFTERNOON_WINDOW_END = -1
    cost_model = CostModel(
        buy_cost_pct=max(0.0, float(args.buy_cost_pct)),
        sell_cost_pct=max(0.0, float(args.sell_cost_pct)),
        slippage_bps=max(0.0, float(args.slippage_bps)),
    )

    # Determine target dates
    if args.date:
        target_dates = [args.date]
    else:
        target_dates = get_recent_trading_days(args.days)

    if not target_dates:
        print("❌ No trading dates found in cache.")
        return

    print(f"Target Dates: {', '.join(sorted(target_dates))}")
    print(f"Scanning daily OHLCV cache ({len(glob.glob(os.path.join(DATA_DIR, '*.csv')))} files)...")

    # Step 1: Find candidates from daily OHLCV
    candidates = scan_daily_candidates(target_dates, top_n=args.top)

    candidates_per_day = {}
    total_cand = 0
    for dt in sorted(candidates.keys()):
        n = len(candidates[dt])
        candidates_per_day[dt] = n
        total_cand += n
        print(f"  {dt}: {n} candidates with 2%+ intraday high")

    if total_cand == 0:
        print("❌ No candidates found.")
        return

    all_trades: List[Trade] = []

    if args.no_api:
        # ── Daily-only simulation (no minute candles) ──
        # Approximation only: this mode cannot model intraday VWAP, confidence,
        # or trigger timing with the same fidelity as minute mode.
        print(f"\n[Daily-only mode] Simulating with open/high/low/close...")
        for dt in sorted(candidates.keys()):
            for cand in candidates[dt]:
                # Simulate: entry at open * (1 + 2%), exit at close
                entry_price = cand["open"] * (1 + TICK_ENTRY_PCT / 100)
                if entry_price > cand["high"]:
                    continue  # Never reached entry
                
                # ── Live-aligned filters ──
                score = simplified_blind_score(
                    price=entry_price,
                    change_pct=(entry_price - cand["open"]) / cand["open"] * 100,
                    rsi=cand["rsi"],
                    cci=cand["cci"],
                    mfi=cand.get("mfi", 50.0),
                    volume_ratio=cand["vol_ratio"],
                    gap_pct=cand["gap_pct"],
                )
                
                # Score cap: high score = oversold trap
                if score >= SCORE_MAX:
                    continue
                # Volume ratio: #1 edge predictor
                if cand["vol_ratio"] < MIN_VOL_RATIO:
                    continue
                # Overnight-gap approximation only
                if cand["gap_pct"] > MAX_GAP_PCT:
                    continue

                exit_price = cand["close"]
                gross_pnl_pct = (exit_price - entry_price) / entry_price * 100
                max_profit = (cand["high"] - entry_price) / entry_price * 100
                max_dd = (entry_price - cand["low"]) / entry_price * 100

                # Fade simulation from daily: did low drop 4%+ from high?
                drop_from_high = (cand["high"] - cand["close"]) / cand["high"] * 100
                fade_detected = drop_from_high >= FADE_DROP_FROM_HIGH_PCT

                if fade_detected:
                    # Conservative: exit at high - 4%
                    exit_price = cand["high"] * (1 - FADE_DROP_FROM_HIGH_PCT / 100)
                    gross_pnl_pct = (exit_price - entry_price) / entry_price * 100
                    exit_reason = "FADE_EXIT"
                else:
                    exit_reason = "EOD"

                # SL check
                atr = cand.get("atr", 0)
                sl = entry_price - SL_ATR_MULTIPLIER * atr if atr > 0 else entry_price * (1 - SL_FALLBACK_PCT)
                if cand["low"] <= sl:
                    exit_price = sl
                    gross_pnl_pct = (exit_price - entry_price) / entry_price * 100
                    exit_reason = "STOP_HIT"
                    fade_detected = True
                cost_result = _apply_trade_costs(entry_price, exit_price, cost_model)

                trade = Trade(
                    symbol=cand["symbol"],
                    date=dt,
                    signal_action="BUY",
                    entry_time="~09:20",
                    entry_price=round(entry_price, 2),
                    exit_price=round(exit_price, 2),
                    exec_entry_price=cost_result["exec_entry_price"],
                    exec_exit_price=cost_result["exec_exit_price"],
                    exit_time="15:15" if exit_reason == "EOD" else "~?",
                    exit_reason=exit_reason,
                    change_at_entry=TICK_ENTRY_PCT,
                    high_after_entry=round(cand["high"], 2),
                    low_after_entry=round(cand["low"], 2),
                    score=score,
                    confidence_pct=0.0,
                    rr2=0.0,
                    entry_gap_pct=0.0,
                    stop_loss=round(sl, 2),
                    target_2=0.0,
                    vwap_at_entry=0.0,
                    volume_ratio=cand["vol_ratio"],
                    gross_pnl_pct=round(gross_pnl_pct, 2),
                    pnl_pct=cost_result["net_pnl_pct"],
                    max_profit_pct=round(max_profit, 2),
                    max_drawdown_pct=round(max_dd, 2),
                    entry_charges=cost_result["entry_charges"],
                    exit_charges=cost_result["exit_charges"],
                    slippage_cost=cost_result["slippage_cost"],
                    faded=cand["close"] < entry_price,
                    fade_detected=fade_detected,
                )
                all_trades.append(trade)
    else:
        # ── Full minute-candle simulation ──
        print(f"\nPulling minute candles from Kite API...")
        for dt in sorted(candidates.keys()):
            cands = candidates[dt]
            symbols = [c["symbol"] for c in cands]
            print(f"\n  {dt}: pulling {len(symbols)} symbols...")

            minute_data = pull_minute_candles(symbols, dt)
            print(f"  Got minute data for {len(minute_data)}/{len(symbols)} symbols")

            for cand in cands:
                sym = cand["symbol"]
                if sym not in minute_data:
                    continue
                trade = simulate_intraday(sym, dt, minute_data[sym], cand, cost_model)
                if trade is not None:
                    all_trades.append(trade)

    # Step 4: Report
    print_report(all_trades, candidates_per_day, cost_model)

    # Step 5: Export
    if args.export:
        export_trades_csv(all_trades, args.export)
    else:
        default_path = os.path.join(BASE_DIR, "results", "backtest_intraday_live.csv")
        export_trades_csv(all_trades, default_path)


if __name__ == "__main__":
    main()

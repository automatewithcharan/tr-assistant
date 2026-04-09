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

# Scorer thresholds
SCORE_BLIND_BUY = 60          # Definitely buy
SCORE_CONFIRM_MIN = 52        # Minimum to confirm

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
    entry_time: str       # HH:MM when we entered
    entry_price: float
    exit_price: float
    exit_time: str
    exit_reason: str      # "EOD", "FADE_WARNING", "FADE_EXIT", "STOP_HIT"
    change_at_entry: float  # % change from open when we entered
    high_after_entry: float
    low_after_entry: float
    score: int
    volume_ratio: float
    pnl_pct: float        # (exit - entry) / entry * 100
    max_profit_pct: float  # (high - entry) / entry * 100
    max_drawdown_pct: float  # (entry - low) / entry * 100
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

            # Get prior rows for RSI/ATR
            idx = rows.index(row)
            prior_closes = [float(r["close"]) for r in rows[max(0, idx - 30):idx + 1]]
            prior_highs = [float(r["high"]) for r in rows[max(0, idx - 30):idx + 1]]
            prior_lows = [float(r["low"]) for r in rows[max(0, idx - 30):idx + 1]]
            prior_volumes = [float(r["volume"]) for r in rows[max(0, idx - 30):idx + 1]]

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

    prev_close = daily_info.get("prev_close", day_open)
    gap_pct = (day_open - prev_close) / prev_close * 100 if prev_close > 0 else 0

    # ── Find entry point ──
    entry_idx = None
    for i, c in enumerate(candles):
        change_from_open = (c["close"] - day_open) / day_open * 100
        if TICK_ENTRY_PCT <= change_from_open < TICK_ALREADY_MOVED_PCT:
            entry_idx = i
            break

    if entry_idx is None:
        return None  # Stock never crossed 2% or was already > 16%

    entry_candle = candles[entry_idx]
    entry_price = entry_candle["close"]
    entry_time = entry_candle["time"]
    change_at_entry = (entry_price - day_open) / day_open * 100

    # ── Compute score at entry ──
    rsi = daily_info.get("rsi", 50.0)
    cci = daily_info.get("cci", 0.0)
    mfi = daily_info.get("mfi", 50.0)
    vol_ratio = daily_info.get("vol_ratio", 1.0)

    score = simplified_blind_score(
        price=entry_price,
        change_pct=change_at_entry,
        rsi=rsi,
        cci=cci,
        mfi=mfi,
        volume_ratio=vol_ratio,
        gap_pct=gap_pct,
    )

    if score < SCORE_CONFIRM_MIN and entry_price < OHLCV_MIN_PRICE:
        return None  # Would not pass OHLCV confirmation

    # ── Compute stop loss ──
    atr = daily_info.get("atr", 0)
    if atr > 0:
        stop_loss = entry_price - SL_ATR_MULTIPLIER * atr
    else:
        stop_loss = entry_price * (1 - SL_FALLBACK_PCT)

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

    pnl_pct = (exit_price - entry_price) / entry_price * 100
    max_profit = (high_after - entry_price) / entry_price * 100
    max_dd = (entry_price - low_after) / entry_price * 100
    actual_close = candles[-1]["close"]
    did_fade = actual_close < entry_price

    return Trade(
        symbol=symbol,
        date=date_str,
        entry_time=entry_time,
        entry_price=round(entry_price, 2),
        exit_price=round(exit_price, 2),
        exit_time=exit_time,
        exit_reason=exit_reason,
        change_at_entry=round(change_at_entry, 2),
        high_after_entry=round(high_after, 2),
        low_after_entry=round(low_after, 2),
        score=score,
        volume_ratio=round(vol_ratio, 2),
        pnl_pct=round(pnl_pct, 2),
        max_profit_pct=round(max_profit, 2),
        max_drawdown_pct=round(max_dd, 2),
        faded=did_fade,
        fade_detected=fade_detected,
    )


# ──────────────────────────────────────────────────────────────────────
# Step 4: Report
# ──────────────────────────────────────────────────────────────────────

def print_report(all_trades: List[Trade], candidates_per_day: Dict[str, int]):
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

    avg_pnl = sum(t.pnl_pct for t in all_trades) / total
    avg_win = sum(t.pnl_pct for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t.pnl_pct for t in losers) / len(losers) if losers else 0
    avg_max_profit = sum(t.max_profit_pct for t in all_trades) / total
    avg_max_dd = sum(t.max_drawdown_pct for t in all_trades) / total

    win_rate = len(winners) / total * 100
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    print(f"\n  Total Candidates Scanned: {sum(candidates_per_day.values())} across {len(candidates_per_day)} days")
    print(f"  Total Trades Simulated:  {total}")
    print(f"  Win Rate:                {win_rate:.1f}% ({len(winners)}W / {len(losers)}L)")
    print(f"  Avg P&L per Trade:       {avg_pnl:+.2f}%")
    print(f"  Avg Winner:              {avg_win:+.2f}%")
    print(f"  Avg Loser:               {avg_loss:+.2f}%")
    print(f"  Expectancy:              {expectancy:+.3f}% per trade")
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
        print(f"    {bucket}: {len(trades):3d} trades | WR {wr:5.1f}% | Avg {ap:+.2f}%")

    # Per-day breakdown
    print(f"\n  ── Per-Day Breakdown ──")
    by_date = defaultdict(list)
    for t in all_trades:
        by_date[t.date].append(t)

    print(f"  {'Date':<12s} {'Candidates':>10s} {'Trades':>7s} {'Wins':>5s} {'Win%':>6s} {'Avg P&L':>8s} {'Best':>8s} {'Worst':>8s}")
    print(f"  {'─' * 12} {'─' * 10} {'─' * 7} {'─' * 5} {'─' * 6} {'─' * 8} {'─' * 8} {'─' * 8}")
    for dt in sorted(by_date.keys()):
        trades = by_date[dt]
        n = len(trades)
        w = sum(1 for t in trades if t.won)
        wr = w / n * 100 if n else 0
        ap = sum(t.pnl_pct for t in trades) / n
        best = max(t.pnl_pct for t in trades)
        worst = min(t.pnl_pct for t in trades)
        cand = candidates_per_day.get(dt, 0)
        print(f"  {dt:<12s} {cand:>10d} {n:>7d} {w:>5d} {wr:>5.1f}% {ap:>+7.2f}% {best:>+7.2f}% {worst:>+7.2f}%")

    # Top 10 best trades
    print(f"\n  ── Top 10 Winners ──")
    top = sorted(all_trades, key=lambda t: -t.pnl_pct)[:10]
    print(f"  {'Symbol':<15s} {'Date':<12s} {'Entry':>6s} {'Exit':>6s} {'P&L':>7s} {'Score':>6s} {'Entry@':>7s} {'Exit@':>7s} {'Reason':<12s}")
    print(f"  {'─' * 15} {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 12}")
    for t in top:
        print(f"  {t.symbol:<15s} {t.date:<12s} {t.entry_price:>6.0f} {t.exit_price:>6.0f} {t.pnl_pct:>+6.2f}% {t.score:>5d} {t.entry_time:>7s} {t.exit_time:>7s} {t.exit_reason:<12s}")

    # Top 10 worst trades
    print(f"\n  ── Top 10 Losers ──")
    bottom = sorted(all_trades, key=lambda t: t.pnl_pct)[:10]
    print(f"  {'Symbol':<15s} {'Date':<12s} {'Entry':>6s} {'Exit':>6s} {'P&L':>7s} {'Score':>6s} {'Entry@':>7s} {'Exit@':>7s} {'Reason':<12s}")
    print(f"  {'─' * 15} {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 12}")
    for t in bottom:
        print(f"  {t.symbol:<15s} {t.date:<12s} {t.entry_price:>6.0f} {t.exit_price:>6.0f} {t.pnl_pct:>+6.2f}% {t.score:>5d} {t.entry_time:>7s} {t.exit_time:>7s} {t.exit_reason:<12s}")

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
        print(f"    {reason:<12s}: {len(trades):3d} trades | WR {wr:5.1f}% | Avg {ap:+.2f}%")

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
        "exit_reason", "pnl_pct", "max_profit_pct", "max_drawdown_pct",
        "score", "volume_ratio", "change_at_entry", "faded", "fade_detected",
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
                "exit_reason": t.exit_reason,
                "pnl_pct": t.pnl_pct,
                "max_profit_pct": t.max_profit_pct,
                "max_drawdown_pct": t.max_drawdown_pct,
                "score": t.score,
                "volume_ratio": t.volume_ratio,
                "change_at_entry": t.change_at_entry,
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
    args = parser.parse_args()

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
        print(f"\n[Daily-only mode] Simulating with open/high/low/close...")
        for dt in sorted(candidates.keys()):
            for cand in candidates[dt]:
                # Simulate: entry at open * (1 + 2%), exit at close
                entry_price = cand["open"] * (1 + TICK_ENTRY_PCT / 100)
                if entry_price > cand["high"]:
                    continue  # Never reached entry
                exit_price = cand["close"]
                pnl = (exit_price - entry_price) / entry_price * 100
                max_profit = (cand["high"] - entry_price) / entry_price * 100
                max_dd = (entry_price - cand["low"]) / entry_price * 100

                score = simplified_blind_score(
                    price=entry_price,
                    change_pct=(entry_price - cand["open"]) / cand["open"] * 100,
                    rsi=cand["rsi"],
                    cci=cand["cci"],
                    mfi=cand.get("mfi", 50.0),
                    volume_ratio=cand["vol_ratio"],
                    gap_pct=cand["gap_pct"],
                )

                # Fade simulation from daily: did low drop 4%+ from high?
                drop_from_high = (cand["high"] - cand["close"]) / cand["high"] * 100
                fade_detected = drop_from_high >= FADE_DROP_FROM_HIGH_PCT

                if fade_detected:
                    # Conservative: exit at high - 4%
                    exit_price = cand["high"] * (1 - FADE_DROP_FROM_HIGH_PCT / 100)
                    pnl = (exit_price - entry_price) / entry_price * 100
                    exit_reason = "FADE_EXIT"
                else:
                    exit_reason = "EOD"

                # SL check
                atr = cand.get("atr", 0)
                sl = entry_price - SL_ATR_MULTIPLIER * atr if atr > 0 else entry_price * (1 - SL_FALLBACK_PCT)
                if cand["low"] <= sl:
                    exit_price = sl
                    pnl = (exit_price - entry_price) / entry_price * 100
                    exit_reason = "STOP_HIT"
                    fade_detected = True

                trade = Trade(
                    symbol=cand["symbol"],
                    date=dt,
                    entry_time="~09:20",
                    entry_price=round(entry_price, 2),
                    exit_price=round(exit_price, 2),
                    exit_time="15:15" if exit_reason == "EOD" else "~?",
                    exit_reason=exit_reason,
                    change_at_entry=TICK_ENTRY_PCT,
                    high_after_entry=round(cand["high"], 2),
                    low_after_entry=round(cand["low"], 2),
                    score=score,
                    volume_ratio=cand["vol_ratio"],
                    pnl_pct=round(pnl, 2),
                    max_profit_pct=round(max_profit, 2),
                    max_drawdown_pct=round(max_dd, 2),
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
                trade = simulate_intraday(sym, dt, minute_data[sym], cand)
                if trade is not None:
                    all_trades.append(trade)

    # Step 4: Report
    print_report(all_trades, candidates_per_day)

    # Step 5: Export
    if args.export:
        export_trades_csv(all_trades, args.export)
    else:
        default_path = os.path.join(BASE_DIR, "results", "backtest_intraday_live.csv")
        export_trades_csv(all_trades, default_path)


if __name__ == "__main__":
    main()

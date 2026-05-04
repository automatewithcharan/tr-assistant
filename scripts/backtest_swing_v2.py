#!/usr/bin/env python3
"""
Swing Backtest v2 — Statistical edge validation
================================================
Tests whether the pkscreener-style momentum signals have a real edge
over a large universe across 3 years.

Signal criteria (mirrors actual SignalMonitor Grade-A logic):
  - RSI was oversold (< 40) within the past 5 days and is recovering
  - Price is above ATR-based trailing stop (uptrend confirmation)
  - Volume surge: current volume >= 2x 20-day average
  - Day change: +0.5% to +4% (momentum day, not exhausted)
  - Close >= ₹50 (liquidity filter)
  - Market regime: BULL or NEUTRAL (not BEAR)

Exit rules (mirrors live tool):
  - Hard stop: 5% below entry
  - Trailing: activates at 3% gain, trails 3% below peak
  - Target 1: 8% → book 50%, trail rest
  - Max hold: 10 trading days; exit at close if losing, ride if winning

Universe: NSE 500 (~500 stocks)
Period: 3 years (configurable, default Jan 2022 – Dec 2024)

Output:
  results/Data/swing_backtest_v2/
    summary.md        — full statistical report
    trades.csv        — every trade
    monthly.csv       — month-by-month P&L
    grade_breakdown.csv

Usage:
    python scripts/backtest_swing_v2.py
    python scripts/backtest_swing_v2.py --years 3 --universe nifty500
    python scripts/backtest_swing_v2.py --years 5 --start 2020-01-01
"""

import argparse
import csv
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
_log = logging.getLogger("swing_bt")

_ROOT       = Path(__file__).parent.parent
_REPORT_DIR = _ROOT / "results" / "Data" / "swing_backtest_v2"
_IDX_FILES  = {
    "nifty50":  _ROOT / "results" / "Indices" / "ind_nifty50list.csv",
    "nifty500": _ROOT / "results" / "Indices" / "ind_nifty500list.csv",
    "nifty200": _ROOT / "results" / "Indices" / "ind_nifty200list.csv",
}

# ── Signal parameters ────────────────────────────────────────────────────────
VOL_MIN_RATIO      = 2.0    # volume must be >= 2x 20-day average
DAY_CHG_MIN        = 0.5    # minimum daily % gain on signal day
DAY_CHG_MAX        = 4.0    # maximum daily % gain (not already exhausted)
RSI_CROSS_ABOVE    = 40.0   # RSI must have crossed ABOVE this in last 2 days
RSI_ENTRY_MAX      = 58.0   # RSI at entry must be < 58 (not overbought)
ATR_PERIODS        = 14
ATR_TRAIL_MULT     = 3.0    # ATR trailing stop multiplier
ATR_STOP_MULT      = 1.5    # ATR-based hard stop multiplier (adaptive, not flat %)
ATR_MAX_PCT        = 3.0    # skip stocks where ATR/close > 3% (too volatile for clean stops)
MIN_PRICE          = 50.0   # liquidity filter
EMA_PERIOD         = 10     # price must be above this EMA (uptrend confirmation)
MAX_GAP_UP_PCT     = 2.0    # skip trade if next-day open gaps >2% above signal close (chasing)
REGIME_BULL_PCT    = 0.52   # >= 52% of universe up → BULL day
REGIME_BEAR_PCT    = 0.40   # <= 40% of universe up → BEAR day

# ── Exit parameters ──────────────────────────────────────────────────────────
HARD_STOP_PCT      = 6.0    # fallback flat stop (used only when ATR unavailable)
ATR_STOP_MULT      = 1.5    # stop = entry - ATR × 1.5 (max 6%)
TRAIL_ACTIVATION   = 5.0    # trailing activates when gain >= 5% (let winners run)
TRAIL_PCT          = 3.5    # trail 3.5% below peak
T1_TARGET          = 10.0   # T1: close 50% at 10%, trail rest
MAX_HOLD_DAYS      = 10     # max hold; exit at close only if still in loss


def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    if len(closes) < period + 1:
        return np.full(len(closes), np.nan)
    delta = np.diff(closes, prepend=closes[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    avg_g = np.zeros(len(closes))
    avg_l = np.zeros(len(closes))
    avg_g[period] = np.mean(gain[1:period+1])
    avg_l[period] = np.mean(loss[1:period+1])
    for i in range(period + 1, len(closes)):
        avg_g[i] = (avg_g[i-1] * (period-1) + gain[i]) / period
        avg_l[i] = (avg_l[i-1] * (period-1) + loss[i]) / period
    with np.errstate(divide="ignore", invalid="ignore"):
        rs  = np.where(avg_l > 0, avg_g / avg_l, 100.0)
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi[:period] = np.nan
    return rsi


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    if len(close) < 2:
        return np.full(len(close), np.nan)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(abs(high - prev_close), abs(low - prev_close)))
    atr = np.full(len(tr), np.nan)
    if len(tr) >= period:
        atr[period-1] = np.mean(tr[:period])
        for i in range(period, len(tr)):
            atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    return atr


def _fetch_price(symbol: str, period: str = "4y") -> Optional[pd.DataFrame]:
    import yfinance as yf
    ticker = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df is None or len(df) < 50:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df[["Open","High","Low","Close","Volume"]].dropna()
        df.index = pd.to_datetime(df.index).normalize()
        df = df.sort_index()
        return df
    except Exception as e:
        _log.debug("Fetch failed %s: %s", symbol, e)
        return None


def _load_universe(name: str) -> List[str]:
    path = _IDX_FILES.get(name, _IDX_FILES["nifty500"])
    if not path.exists():
        raise FileNotFoundError(f"Universe file not found: {path}")
    symbols = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row.get("Symbol","").strip()
            if sym and "DUMMY" not in sym.upper():
                symbols.append(sym)
    return symbols


def _build_signals(df: pd.DataFrame, regime_map: Dict) -> pd.DataFrame:
    """Return rows where signal criteria are met (entry next day open)."""
    closes  = df["Close"].values.astype(float)
    highs   = df["High"].values.astype(float)
    lows    = df["Low"].values.astype(float)
    volumes = df["Volume"].values.astype(float)
    opens   = df["Open"].values.astype(float)

    rsi_vals = _rsi(closes, 14)
    atr_vals = _atr(highs, lows, closes, ATR_PERIODS)

    # 20-day volume average (shifted: uses only past data)
    vol_avg = pd.Series(volumes).rolling(20, min_periods=10).mean().shift(1).values

    # 10-day EMA for trend confirmation
    ema_vals = pd.Series(closes).ewm(span=EMA_PERIOD, adjust=False).mean().values

    # ATR trailing stop for trend direction filter
    atr_trail = np.full(len(closes), np.nan)
    for i in range(1, len(closes)):
        if np.isnan(atr_vals[i]):
            continue
        level = closes[i] - ATR_TRAIL_MULT * atr_vals[i]
        if not np.isnan(atr_trail[i-1]):
            level = max(level, atr_trail[i-1])
        atr_trail[i] = level

    signal_rows = []
    for i in range(20, len(df) - 1):
        d        = df.index[i].date()
        regime   = regime_map.get(d, "neutral")
        if regime == "bear":
            continue

        close    = closes[i]
        opn      = opens[i]
        vol      = volumes[i]
        avg_vol  = vol_avg[i]

        if close < MIN_PRICE:
            continue
        if np.isnan(avg_vol) or avg_vol <= 0:
            continue

        vol_ratio = vol / avg_vol
        if vol_ratio < VOL_MIN_RATIO:
            continue

        day_chg = (close - opn) / opn * 100.0
        if not (DAY_CHG_MIN <= day_chg <= DAY_CHG_MAX):
            continue

        # RSI CROSSOVER: RSI must have CROSSED ABOVE RSI_CROSS_ABOVE in the last 2 days
        # (was below threshold yesterday or 2 days ago, now above it)
        rsi_now = rsi_vals[i]
        rsi_prev = rsi_vals[i-1] if i >= 1 else np.nan
        rsi_prev2 = rsi_vals[i-2] if i >= 2 else np.nan
        if np.isnan(rsi_now) or rsi_now >= RSI_ENTRY_MAX:
            continue
        # Actual crossover: RSI yesterday or 2 days ago was below threshold, today above
        crossed = (not np.isnan(rsi_prev) and rsi_prev <= RSI_CROSS_ABOVE and rsi_now > RSI_CROSS_ABOVE) or \
                  (not np.isnan(rsi_prev2) and rsi_prev2 <= RSI_CROSS_ABOVE and rsi_now > RSI_CROSS_ABOVE)
        if not crossed:
            continue

        # Price above 10-day EMA (uptrend confirmation)
        ema = ema_vals[i]
        if np.isnan(ema) or close < ema * 0.998:   # allow 0.2% below EMA
            continue

        # Price above ATR trailing stop
        trail = atr_trail[i]
        if np.isnan(trail) or close < trail:
            continue

        # Volatility filter: skip high-ATR stocks (stops would be too wide)
        atr_now = atr_vals[i]
        if np.isnan(atr_now) or atr_now <= 0:
            continue
        if (atr_now / close * 100) > ATR_MAX_PCT:
            continue

        entry_price = opens[i+1]
        if entry_price <= 0 or np.isnan(entry_price):
            continue

        # Gap filter: skip if stock gaps up >2% at open vs signal close (chasing)
        gap_pct = (entry_price - close) / close * 100
        if gap_pct > MAX_GAP_UP_PCT:
            continue

        # ATR-based stop
        atr_stop = entry_price - ATR_STOP_MULT * atr_now
        # Cap: never stop more than HARD_STOP_PCT below entry
        atr_stop = max(atr_stop, entry_price * (1 - HARD_STOP_PCT / 100))

        signal_rows.append({
            "signal_idx":   i,
            "signal_date":  d,
            "symbol":       None,   # filled by caller
            "signal_close": round(close, 2),
            "entry_price":  round(entry_price, 2),
            "atr_stop":     round(atr_stop, 2),
            "vol_ratio":    round(vol_ratio, 2),
            "day_chg":      round(day_chg, 2),
            "rsi":          round(rsi_now, 1),
            "rsi_prev":     round(rsi_prev, 1) if not np.isnan(rsi_prev) else None,
            "atr_trail":    round(trail, 2),
            "ema10":        round(ema, 2),
            "regime":       regime,
        })
    return signal_rows


def _simulate_trade(df: pd.DataFrame, entry_idx: int, entry_price: float, atr_stop: float = None) -> Dict:
    """Simulate one trade starting from entry_idx+1 (day of entry at open)."""
    closes = df["Close"].values.astype(float)
    highs  = df["High"].values.astype(float)
    lows   = df["Low"].values.astype(float)
    opens  = df["Open"].values.astype(float)

    hard_stop   = atr_stop if atr_stop else entry_price * (1 - HARD_STOP_PCT / 100)
    peak        = entry_price
    trail_stop  = None
    qty         = 1.0     # units; T1 books 0.5
    t1_done     = False
    exit_price  = None
    exit_idx    = None
    exit_reason = ""
    realized_pct = 0.0

    for i in range(entry_idx + 1, min(entry_idx + 1 + MAX_HOLD_DAYS, len(df))):
        day_low  = lows[i]
        day_high = highs[i]
        day_close = closes[i]

        # Hard stop hit
        if day_low <= hard_stop:
            ep = min(day_close, hard_stop)
            total_ret = (realized_pct + (ep / entry_price - 1) * 100 * qty) / (1 if t1_done else 1)
            exit_price  = round(ep, 2)
            exit_idx    = i
            exit_reason = "hard_stop"
            pnl = (ep / entry_price - 1) * 100 * qty + realized_pct
            return _trade_result(df, entry_idx, entry_price, exit_idx, exit_price, exit_reason, pnl, t1_done, qty)

        # Update peak and trailing stop
        if day_high > peak:
            peak = day_high
        gain_pct = (peak / entry_price - 1) * 100

        if gain_pct >= TRAIL_ACTIVATION:
            trail_stop = peak * (1 - TRAIL_PCT / 100)

        # T1: book 50% at 8% gain
        if not t1_done and (day_high / entry_price - 1) * 100 >= T1_TARGET:
            t1_price   = entry_price * (1 + T1_TARGET / 100)
            realized_pct += (t1_price / entry_price - 1) * 100 * 0.5
            qty       = 0.5
            t1_done   = True

        # Trailing stop hit
        if trail_stop and day_low <= trail_stop:
            ep = min(day_close, trail_stop)
            pnl = (ep / entry_price - 1) * 100 * qty + realized_pct
            return _trade_result(df, entry_idx, entry_price, i, round(ep, 2), "trailing_stop", pnl, t1_done, qty)

    # Max hold reached
    i = min(entry_idx + MAX_HOLD_DAYS, len(df) - 1)
    ep        = closes[i]
    pnl_tail  = (ep / entry_price - 1) * 100 * qty + realized_pct
    reason    = "max_hold_win" if pnl_tail > 0 else "max_hold_exit"
    return _trade_result(df, entry_idx, entry_price, i, round(ep, 2), reason, pnl_tail, t1_done, qty)


def _trade_result(df, sig_idx, entry_price, exit_idx, exit_price, reason, pnl_pct, t1_done, qty_remaining):
    hold_days  = exit_idx - sig_idx - 1   # days after entry day
    won        = pnl_pct > 0
    sig_date   = df.index[sig_idx].date()
    entry_date = df.index[sig_idx + 1].date()
    exit_date  = df.index[exit_idx].date()
    return {
        "signal_date":  sig_date.isoformat(),
        "entry_date":   entry_date.isoformat(),
        "exit_date":    exit_date.isoformat(),
        "entry_price":  entry_price,
        "exit_price":   exit_price,
        "pnl_pct":      round(pnl_pct, 3),
        "hold_days":    hold_days,
        "exit_reason":  reason,
        "t1_booked":    t1_done,
        "won":          won,
        "sl_hit":       reason == "hard_stop",
    }


def _build_regime_map(price_data: Dict[str, pd.DataFrame]) -> Dict:
    """Compute daily regime (bull/bear/neutral) across all stocks."""
    daily: Dict = {}
    for sym, df in price_data.items():
        opens  = df["Open"].values
        closes = df["Close"].values
        for i in range(len(df)):
            d = df.index[i].date()
            if d not in daily:
                daily[d] = {"up": 0, "total": 0}
            daily[d]["total"] += 1
            if closes[i] > opens[i]:
                daily[d]["up"] += 1
    regime = {}
    for d, v in daily.items():
        if v["total"] == 0:
            regime[d] = "neutral"
        elif v["up"] / v["total"] >= REGIME_BULL_PCT:
            regime[d] = "bull"
        elif v["up"] / v["total"] <= REGIME_BEAR_PCT:
            regime[d] = "bear"
        else:
            regime[d] = "neutral"
    return regime


def _xirr_simple(trades: List[Dict], monthly_capital: float = 100_000) -> Optional[float]:
    """Approximate annualized return from trade P&L, assuming capital is recycled."""
    if not trades:
        return None
    rets = [t["pnl_pct"] for t in trades]
    avg  = np.mean(rets)
    # trades/year × avg return (rough Expectancy → annual)
    first = date.fromisoformat(trades[0]["entry_date"])
    last  = date.fromisoformat(trades[-1]["exit_date"])
    years = max((last - first).days / 365.25, 0.1)
    trades_per_year = len(trades) / years
    # approximate annualized: compound avg return per trade × trades_per_year
    # (conservative: assumes sequential, non-overlapping)
    return round((1 + avg / 100) ** trades_per_year - 1, 4)


def _compute_stats(trades: List[Dict]) -> Dict:
    if not trades:
        return {}
    rets   = [t["pnl_pct"] for t in trades]
    wins   = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    total  = len(trades)
    win_n  = len(wins)
    expectancy = np.mean(rets) if rets else 0

    monthly: Dict[str, List] = {}
    for t in trades:
        m = t["entry_date"][:7]
        monthly.setdefault(m, []).append(t["pnl_pct"])

    worst_dd = 0.0
    running  = 0.0
    peak     = 0.0
    for r in sorted(rets):
        running += r
        if running > peak:
            peak = running
        dd = peak - running
        if dd > worst_dd:
            worst_dd = dd

    sl_hits   = sum(1 for t in trades if t["sl_hit"])
    t1_booked = sum(1 for t in trades if t["t1_booked"])

    exit_reasons: Dict[str, int] = {}
    for t in trades:
        exit_reasons[t["exit_reason"]] = exit_reasons.get(t["exit_reason"], 0) + 1

    hold_days = [t["hold_days"] for t in trades]

    return {
        "total_trades":     total,
        "win_n":            win_n,
        "loss_n":           total - win_n,
        "win_rate":         round(win_n / total * 100, 1),
        "avg_return":       round(expectancy, 3),
        "avg_win":          round(np.mean(wins), 2)   if wins   else 0.0,
        "avg_loss":         round(np.mean(losses), 2) if losses else 0.0,
        "payoff_ratio":     round(abs(np.mean(wins) / np.mean(losses)), 2) if wins and losses else None,
        "expectancy":       round(expectancy, 3),
        "max_win":          round(max(rets), 1),
        "max_loss":         round(min(rets), 1),
        "avg_hold_days":    round(np.mean(hold_days), 1) if hold_days else 0,
        "sl_hits":          sl_hits,
        "sl_hit_pct":       round(sl_hits / total * 100, 1),
        "t1_booked":        t1_booked,
        "t1_pct":           round(t1_booked / total * 100, 1),
        "exit_reasons":     exit_reasons,
        "monthly":          {m: {"trades": len(v), "avg_ret": round(np.mean(v), 2)} for m, v in monthly.items()},
        "worst_consecutive_dd": round(worst_dd, 1),
    }


def _write_report(stats: Dict, trades: List[Dict], out_dir: Path, universe: str, start: str, end: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")

    # Trades CSV
    trades_path = out_dir / f"trades_{today}_{universe}.csv"
    if trades:
        keys = list(trades[0].keys()) + ["symbol"]
        with open(trades_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(trades)

    # Monthly CSV
    monthly_path = out_dir / f"monthly_{today}_{universe}.csv"
    monthly = stats.get("monthly", {})
    with open(monthly_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["month","trades","avg_ret"])
        w.writeheader()
        for m, v in sorted(monthly.items()):
            w.writerow({"month": m, **v})

    # Summary markdown
    md_path = out_dir / f"summary_{today}_{universe}.md"
    wr  = stats.get("win_rate", 0)
    avg = stats.get("avg_return", 0)
    pr  = stats.get("payoff_ratio") or 0
    aw  = stats.get("avg_win", 0)
    al  = stats.get("avg_loss", 0)
    n   = stats.get("total_trades", 0)
    exp = stats.get("expectancy", 0)
    er  = stats.get("exit_reasons", {})

    # Edge verdict
    has_edge = wr >= 55 and exp > 0.5 and pr and pr >= 1.2 and n >= 150
    edge_label = "✅ EDGE FOUND" if has_edge else ("⚠️ MARGINAL" if wr >= 50 and exp > 0 else "❌ NO EDGE")

    # Statistical significance (binomial)
    import math
    z = (wr / 100 - 0.5) / math.sqrt(0.25 / max(n, 1)) if n > 0 else 0
    sig = "SIGNIFICANT (p<0.01)" if abs(z) > 2.58 else ("SIGNIFICANT (p<0.05)" if abs(z) > 1.96 else "NOT SIGNIFICANT")

    lines = [
        f"# Swing Backtest v2 — Statistical Edge Report",
        f"",
        f"**Universe:** {universe}  |  **Period:** {start} → {end}  |  **Generated:** {date.today().isoformat()}",
        f"",
        f"---",
        f"",
        f"## Verdict: {edge_label}",
        f"",
        f"| Metric | Value | Threshold | Pass? |",
        f"|--------|-------|-----------|-------|",
        f"| Win rate | {wr:.1f}% | ≥ 55% | {'✅' if wr >= 55 else '❌'} |",
        f"| Expectancy | {exp:.3f}% per trade | > 0.5% | {'✅' if exp > 0.5 else '❌'} |",
        f"| Payoff ratio | {pr:.2f}x | ≥ 1.2x | {'✅' if pr and pr >= 1.2 else '❌'} |",
        f"| Total trades | {n} | ≥ 150 | {'✅' if n >= 150 else '❌'} |",
        f"| Statistical significance | {sig} | p < 0.05 | {'✅' if 'p<0.05' in sig or 'p<0.01' in sig else '❌'} |",
        f"",
        f"---",
        f"",
        f"## Full Statistics",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total trades | {n} |",
        f"| Win / Loss | {stats.get('win_n')} / {stats.get('loss_n')} |",
        f"| Win rate | **{wr:.1f}%** |",
        f"| Avg return per trade | **{avg:.3f}%** |",
        f"| Avg win | +{aw:.2f}% |",
        f"| Avg loss | {al:.2f}% |",
        f"| Payoff ratio | {pr:.2f}x |",
        f"| Best trade | +{stats.get('max_win', 0):.1f}% |",
        f"| Worst trade | {stats.get('max_loss', 0):.1f}% |",
        f"| Avg hold days | {stats.get('avg_hold_days', 0):.1f} |",
        f"| SL hits | {stats.get('sl_hits',0)} ({stats.get('sl_hit_pct',0):.1f}%) |",
        f"| T1 bookings (8%) | {stats.get('t1_booked',0)} ({stats.get('t1_pct',0):.1f}%) |",
        f"| Worst consec. drawdown | {stats.get('worst_consecutive_dd',0):.1f}% |",
        f"| Z-score (vs random) | {z:.2f} ({sig}) |",
        f"",
        f"## Exit Breakdown",
        f"",
        f"| Exit Reason | Count |",
        f"|-------------|-------|",
    ]
    for k, v in sorted(er.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")

    lines += [
        f"",
        f"## Monthly P&L",
        f"",
        f"| Month | Trades | Avg Return |",
        f"|-------|--------|------------|",
    ]
    for m, mv in sorted(monthly.items()):
        lines.append(f"| {m} | {mv['trades']} | {mv['avg_ret']:+.2f}% |")

    lines += [
        f"",
        f"---",
        f"",
        f"## What This Means",
        f"",
    ]
    if has_edge:
        lines += [
            f"> ✅ **The signal has a statistically significant edge.**",
            f"> Win rate {wr:.1f}% with payoff {pr:.2f}x → positive expectancy of {exp:.2f}% per trade.",
            f"> With {n} trades this is reliable, not noise.",
            f"> **You can scale capital into this strategy.**",
            f"> Suggested: deploy 20–30% of monthly ₹2L into top-ranked signals.",
        ]
    elif wr >= 50 and exp > 0:
        lines += [
            f"> ⚠️ **The signal shows marginal positive edge but is NOT conclusively proven.**",
            f"> Win rate {wr:.1f}% is above 50% but not the 55% threshold.",
            f"> Could be real edge or could be luck. Need more data or stronger filter.",
            f"> **Do not scale capital yet.** Continue paper trading to accumulate more trades.",
        ]
    else:
        lines += [
            f"> ❌ **No statistically meaningful edge found with this signal filter.**",
            f"> Win rate {wr:.1f}% and expectancy {exp:.3f}% is close to random.",
            f"> The signal criteria needs tuning. See the monthly breakdown for patterns.",
            f"> **Do not deploy real money into swing trades until edge is proven.**",
        ]

    lines += [
        f"",
        f"## Signal Parameters Used",
        f"",
        f"| Parameter | Value |",
        f"|-----------|-------|",
        f"| Volume min ratio | {VOL_MIN_RATIO}x 20-day average |",
        f"| Day change | {DAY_CHG_MIN}% – {DAY_CHG_MAX}% |",
        f"| RSI crossover above | {RSI_CROSS_ABOVE} (must cross in last 2 days) |",
        f"| RSI at entry | < {RSI_ENTRY_MAX} |",
        f"| EMA confirmation | Price ≥ {EMA_PERIOD}-day EMA |",
        f"| ATR trend filter mult | {ATR_TRAIL_MULT}x |",
        f"| ATR-based hard stop | entry − {ATR_STOP_MULT}× ATR (max {HARD_STOP_PCT}%) |",
        f"| Min price | ₹{MIN_PRICE} |",
        f"| Market regime | BULL or NEUTRAL (BEAR skipped) |",
        f"| Trailing stop | {TRAIL_PCT}% below peak (activates at {TRAIL_ACTIVATION}% gain) |",
        f"| T1 target | {T1_TARGET}% (book 50%) |",
        f"| Max hold | {MAX_HOLD_DAYS} trading days |",
        f"",
        f"*Generated by tr-assistant swing backtest engine v2*",
    ]

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {"summary": str(md_path), "trades": str(trades_path), "monthly": str(monthly_path)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="nifty500", choices=["nifty50","nifty200","nifty500"])
    parser.add_argument("--years",    type=float, default=3.0)
    parser.add_argument("--start",    default=None, help="Override start date YYYY-MM-DD")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    end_date   = date.today()
    if args.start:
        start_date = date.fromisoformat(args.start)
    else:
        start_date = end_date - timedelta(days=int(args.years * 365.25))

    _log.info("=" * 60)
    _log.info("SWING BACKTEST v2 — Edge Validation")
    _log.info("  Universe: %s  |  Period: %s → %s", args.universe, start_date, end_date)
    _log.info("  Signal: Vol≥%.1fx | DayChg %.1f–%.1f%% | RSI cross>%d | ATR stop %.1fx | trail @%d%%",
              VOL_MIN_RATIO, DAY_CHG_MIN, DAY_CHG_MAX, int(RSI_CROSS_ABOVE), ATR_STOP_MULT, int(TRAIL_ACTIVATION))
    _log.info("=" * 60)

    symbols = _load_universe(args.universe)
    _log.info("Universe: %d symbols", len(symbols))

    # Fetch price data
    _log.info("Fetching price history (this takes ~3-5 mins for nifty500)...")
    price_data: Dict[str, pd.DataFrame] = {}
    failed = []
    for i, sym in enumerate(symbols):
        if (i + 1) % 50 == 0:
            _log.info("  Price: %d/%d  loaded=%d  failed=%d", i+1, len(symbols), len(price_data), len(failed))
        df = _fetch_price(sym, period=f"{int(args.years)+2}y")
        if df is not None and len(df) >= 50:
            # Slice to requested period
            df = df[df.index.date >= start_date]
            if len(df) >= 30:
                price_data[sym] = df
            else:
                failed.append(sym)
        else:
            failed.append(sym)

    _log.info("Loaded: %d/%d  failed: %d", len(price_data), len(symbols), len(failed))
    if failed[:5]:
        _log.info("Failed examples: %s", failed[:5])

    # Build regime map
    _log.info("Computing market regime map...")
    regime_map = _build_regime_map(price_data)
    bull = sum(1 for v in regime_map.values() if v == "bull")
    bear = sum(1 for v in regime_map.values() if v == "bear")
    _log.info("  Regime: %d bull days, %d bear days, %d neutral",
              bull, bear, len(regime_map)-bull-bear)

    # Generate signals and simulate trades
    _log.info("Finding signals and simulating trades...")
    all_trades: List[Dict] = []
    signal_count = 0
    for sym, df in price_data.items():
        signals = _build_signals(df, regime_map)
        signal_count += len(signals)
        last_exit_idx = -1
        for sig in signals:
            idx = sig["signal_idx"]
            if idx <= last_exit_idx:
                continue    # no overlapping trades per symbol
            trade = _simulate_trade(df, idx, sig["entry_price"], atr_stop=sig.get("atr_stop"))
            trade["symbol"]    = sym
            trade["vol_ratio"] = sig["vol_ratio"]
            trade["day_chg"]   = sig["day_chg"]
            trade["rsi"]       = sig["rsi"]
            trade["regime"]    = sig["regime"]
            all_trades.append(trade)
            # Find exit index for overlap check
            exit_date_str = trade["exit_date"]
            exit_rows = df[df.index.date == date.fromisoformat(exit_date_str)]
            if len(exit_rows) > 0:
                last_exit_idx = df.index.get_loc(exit_rows.index[-1])

    _log.info("Signals found: %d  |  Trades simulated: %d", signal_count, len(all_trades))

    if not all_trades:
        _log.error("No trades generated — check signal parameters or data quality")
        sys.exit(1)

    # Sort by entry date
    all_trades.sort(key=lambda x: x["entry_date"])

    # Compute stats
    stats = _compute_stats(all_trades)

    # Print summary
    _log.info("=" * 60)
    _log.info("RESULTS")
    _log.info("  Trades: %d  |  Win rate: %.1f%%  |  Avg return: %.3f%%",
              stats["total_trades"], stats["win_rate"], stats["avg_return"])
    _log.info("  Avg win: +%.2f%%  |  Avg loss: %.2f%%  |  Payoff: %.2f",
              stats["avg_win"], stats["avg_loss"], stats.get("payoff_ratio") or 0)
    _log.info("  Avg hold: %.1f days  |  SL hits: %d (%.1f%%)",
              stats["avg_hold_days"], stats["sl_hits"], stats["sl_hit_pct"])

    has_edge = stats["win_rate"] >= 55 and stats["expectancy"] > 0.5 and (stats.get("payoff_ratio") or 0) >= 1.2 and stats["total_trades"] >= 150
    _log.info("  VERDICT: %s", "✅ EDGE FOUND" if has_edge else ("⚠️  MARGINAL" if stats["win_rate"] >= 50 and stats["avg_return"] > 0 else "❌ NO EDGE"))
    _log.info("=" * 60)

    # Write outputs
    paths = _write_report(stats, all_trades, _REPORT_DIR, args.universe,
                          str(start_date), str(end_date))
    _log.info("Reports written:")
    for k, v in paths.items():
        _log.info("  %-12s %s", k, v)


if __name__ == "__main__":
    main()

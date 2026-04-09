#!/usr/bin/env python3
"""
Historical Backtest: V4 Big Runner Scorer across ~1 year of daily OHLCV data.

Uses cached daily candles (results/Data/kite_history_cache/day/*.csv) to
simulate what alerts would have fired on EACH trading day, then measures
actual next-day and intraday outcomes.

Strategy simulated (per day):
  1. Compute RSI(14), CCI(20), MFI(14), VolumeRatio(20-day avg) from daily OHLCV
  2. Detect stocks gapping up / opening above previous close by 3%+
  3. Score each 3%+ mover using BlindBuyScorer logic (V3 vs V4)
  4. Apply quality gates (action, RSI, confidence, targets, volume)
  5. Track outcome: did the stock go higher from 3% entry?
     - Intraday: entry at open (which is ~3%+ above prev close), exit at day high
     - Next-day: did the stock close higher the next trading day?
     - Max drawdown: how far did it fall from entry?

Key difference from live system:
  - Live system has real-time tick data + engine NowAction recommendations
  - Historical backtest only has daily OHLCV, so we simulate:
    - NowAction ≈ "Buy Now" if RSI < 65 and CCI rising and day is green
    - Confidence ≈ derived from indicator alignment count
    - Targets ≈ ATR-based (always present since we compute them)
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import warnings
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "results", "Data", "kite_history_cache", "day")


# ──────────────────────────────────────────────────────────────────────
# Indicator calculations from raw OHLCV
# ──────────────────────────────────────────────────────────────────────

def compute_rsi(closes: list, period: int = 14) -> float:
    """Compute RSI from a list of close prices (most recent last)."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_cci(highs: list, lows: list, closes: list, period: int = 20) -> float:
    """Compute CCI from OHLC data."""
    if len(closes) < period:
        return 0.0
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(-period, 0)]
    tp_mean = sum(tp) / period
    mean_dev = sum(abs(t - tp_mean) for t in tp) / period
    if mean_dev == 0:
        return 0.0
    return (tp[-1] - tp_mean) / (0.015 * mean_dev)


def compute_mfi(highs: list, lows: list, closes: list, volumes: list,
                period: int = 14) -> float:
    """Compute MFI (Money Flow Index) from OHLCV data."""
    if len(closes) < period + 1:
        return 50.0
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(-period-1, 0)]
    pos_flow = 0.0
    neg_flow = 0.0
    for i in range(1, period + 1):
        mf = tp[i] * volumes[-(period + 1 - i)]
        if tp[i] > tp[i-1]:
            pos_flow += mf
        else:
            neg_flow += mf
    if neg_flow == 0:
        return 100.0
    mfr = pos_flow / neg_flow
    return 100 - (100 / (1 + mfr))


def compute_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Compute ATR from OHLC data."""
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        h, l, pc = highs[i], lows[i], closes[i-1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


# ──────────────────────────────────────────────────────────────────────
# Scorer (matches production BlindBuyScorer V4 logic)
# ──────────────────────────────────────────────────────────────────────

def score_stock(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                price_now, prev_close, reason_flags, version="v4"):
    """Score a stock — simplified version of BlindBuyScorer for historical data."""
    score = 0.0
    reasons = []

    # Penny stock filter
    if price_now > 0 and price_now < 10.0:
        return 0, "PENNY"

    # RSI
    if rsi < 30: score += 25; reasons.append("RSI<30")
    elif rsi < 40: score += 18; reasons.append("RSI<40")
    elif rsi < 50: score += 10
    elif rsi > 70 and change_pct < 5.0:
        score -= 8; reasons.append("RSI>70")

    # CCI with V4 volume-halved penalties
    _high_vol = version == "v4" and vol_ratio >= 2.0
    if cci < -100: score += 12
    elif cci < -50: score += 6
    elif cci > 200:
        _pen = 8 if _high_vol else 15
        score -= _pen
    elif cci > 150:
        _pen = 4 if _high_vol else 8
        score -= _pen

    # MFI
    if mfi < 20: score += 10
    elif mfi < 40: score += 5
    elif mfi > 80:
        _pen = 4 if _high_vol else 8
        score -= _pen

    # Overbought combo
    if 70 <= mfi <= 80 and cci > 100:
        _pen = 3 if _high_vol else 5
        score -= _pen

    # Volume (no time-adjustment needed for daily data — just raw ratio)
    if vol_ratio >= 2.5: score += 20; reasons.append(f"vol {vol_ratio:.1f}x")
    elif vol_ratio >= 1.5: score += 15; reasons.append(f"vol {vol_ratio:.1f}x")
    elif vol_ratio >= 1.0: score += 10
    elif vol_ratio >= 0.7: score += 5

    # Gap-up bonus
    if prev_close > 0 and price_now > 0:
        _gap_pct = (price_now - prev_close) / prev_close * 100
        _gap_bonus = 0
        if _gap_pct >= 7.0:
            _gap_bonus = 15; score += 15; reasons.append(f"gap {_gap_pct:.0f}%")
        elif _gap_pct >= 4.0:
            _gap_bonus = 10; score += 10; reasons.append(f"gap {_gap_pct:.0f}%")
    else:
        _gap_bonus = 0

    # Change %
    _cap = 10 if _gap_bonus >= 10 else 15
    if change_pct >= 5.0: score += _cap
    elif change_pct >= 2.5: score += min(12, _cap)
    elif change_pct >= 1.0: score += min(10, _cap)
    elif change_pct > 0: score += 6

    # ATR / trend (from reason_flags)
    if reason_flags.get("above_atr"): score += 12
    if reason_flags.get("macd_bull"): score += 10
    elif reason_flags.get("macd_hist_up"): score += 5

    # Entry confirmed (we simulate: price > prev_close * 1.03 = confirmed)
    score += 8; reasons.append("entry")

    # Confidence
    if confidence >= 25: score += 10
    elif confidence >= 15: score += 5

    # Bearish penalties (from reason_flags)
    if reason_flags.get("bearish_ma"): score -= 50
    if reason_flags.get("death_cross"): score -= 40

    return round(max(0, min(score, 100)), 1), ", ".join(reasons)


def simulate_action(rsi, cci, mfi, vol_ratio, change_pct, prev_change_pct):
    """
    Simulate NowAction from daily OHLCV indicators.
    In the live system, the engine evaluates 7 indicators and outputs
    Buy Now / Watch for Buy / Hold / Sell. We approximate this.
    """
    bullish_count = 0
    if rsi < 65: bullish_count += 1
    if cci < 150: bullish_count += 1
    if mfi < 80: bullish_count += 1
    if vol_ratio >= 1.0: bullish_count += 1
    if change_pct > 0: bullish_count += 1
    if prev_change_pct is not None and change_pct > prev_change_pct: bullish_count += 1

    if bullish_count >= 5:
        return "Buy Now", bullish_count
    elif bullish_count >= 4:
        return "Watch for Buy", bullish_count
    else:
        return "Hold / Do Nothing", bullish_count


def simulate_confidence(bullish_count, vol_ratio, rsi):
    """Approximate engine confidence from indicator alignment."""
    base = bullish_count * 8  # 0-48 from alignment
    if vol_ratio >= 2.0: base += 10
    elif vol_ratio >= 1.5: base += 5
    if rsi < 50: base += 5
    return min(base, 100)


# ──────────────────────────────────────────────────────────────────────
# Load all stock daily data
# ──────────────────────────────────────────────────────────────────────

def load_all_stocks():
    """Load all daily OHLCV cached data into a dict of lists."""
    stocks = {}
    for f in sorted(os.listdir(DATA_DIR)):
        if not f.endswith(".csv"):
            continue
        sym = f.replace(".csv", "")
        path = os.path.join(DATA_DIR, f)
        try:
            rows = []
            with open(path, "r") as fh:
                reader = csv.DictReader(fh)
                for r in reader:
                    rows.append({
                        "date": r["datetime"][:10],
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": int(float(r["volume"])),
                    })
            if len(rows) >= 50:  # Need at least 50 days for indicators
                stocks[sym] = rows
        except Exception:
            pass
    return stocks


# ──────────────────────────────────────────────────────────────────────
# Daily scan simulation
# ──────────────────────────────────────────────────────────────────────

def scan_day(stocks, date_idx, all_dates, nifty_data=None):
    """
    Simulate one trading day's scanner run.
    Returns list of alert candidates with scores and outcomes.
    """
    results = []
    # Get Nifty/market change for this day (if available)
    nifty_chg = 0.0
    if nifty_data and date_idx < len(nifty_data) and date_idx > 0:
        nc = nifty_data[date_idx]["close"]
        np_prev = nifty_data[date_idx - 1]["close"]
        if np_prev > 0:
            nifty_chg = (nc - np_prev) / np_prev * 100

    _is_broad_rally = nifty_chg >= 2.0

    for sym, daily in stocks.items():
        # Find this date in the stock's data
        target_date = all_dates[date_idx]
        idx = None
        for i, row in enumerate(daily):
            if row["date"] == target_date:
                idx = i
                break
        if idx is None or idx < 30:  # Need 30+ history for indicators
            continue

        prev = daily[idx - 1]
        cur = daily[idx]

        # Skip if no previous close
        if prev["close"] <= 0:
            continue

        # Intraday change (simulated: open relative to prev close)
        open_gap_pct = (cur["open"] - prev["close"]) / prev["close"] * 100
        day_change_pct = (cur["close"] - prev["close"]) / prev["close"] * 100
        max_gain_pct = (cur["high"] - prev["close"]) / prev["close"] * 100
        max_draw_pct = (cur["low"] - prev["close"]) / prev["close"] * 100

        # FILTER: Only stocks that open 3%+ above prev close (our trigger)
        if open_gap_pct < 3.0:
            continue

        # Compute indicators using data up to previous day (what we'd know at open)
        closes = [daily[j]["close"] for j in range(max(0, idx-30), idx)]
        highs = [daily[j]["high"] for j in range(max(0, idx-30), idx)]
        lows = [daily[j]["low"] for j in range(max(0, idx-30), idx)]
        volumes = [daily[j]["volume"] for j in range(max(0, idx-30), idx)]

        if len(closes) < 20:
            continue

        rsi = compute_rsi(closes, 14)
        cci = compute_cci(highs, lows, closes, 20)
        mfi = compute_mfi(highs, lows, closes, volumes, 14)
        atr = compute_atr(highs, lows, closes, 14)

        # Volume ratio: today's volume vs 20-day average
        avg_vol = sum(volumes[-20:]) / min(20, len(volumes[-20:]))
        vol_ratio = cur["volume"] / avg_vol if avg_vol > 0 else 0

        # Previous day's change (for action simulation)
        prev_change = None
        if idx >= 2:
            prev_change = (daily[idx-1]["close"] - daily[idx-2]["close"]) / daily[idx-2]["close"] * 100

        # Simulate engine action and confidence
        action, bullish_count = simulate_action(rsi, cci, mfi, vol_ratio, day_change_pct, prev_change)
        confidence = simulate_confidence(bullish_count, vol_ratio, rsi)

        # Trend flags (simplified from OHLCV)
        reason_flags = {
            "above_atr": cur["close"] > prev["close"] + atr * 0.5 if atr > 0 else False,
            "macd_bull": False,  # Can't compute from daily alone without EMA history
            "macd_hist_up": False,
        }

        # Stop loss / targets (ATR-based)
        stop_loss = cur["open"] - 2 * atr if atr > 0 else 0
        target1 = cur["open"] + 1.5 * atr if atr > 0 else 0
        target2 = cur["open"] + 3 * atr if atr > 0 else 0

        # Score V3 and V4
        v3_score, v3_reasons = score_stock(rsi, cci, mfi, vol_ratio, open_gap_pct,
                                           confidence, cur["open"], prev["close"],
                                           reason_flags, version="v3")
        v4_score, v4_reasons = score_stock(rsi, cci, mfi, vol_ratio, open_gap_pct,
                                           confidence, cur["open"], prev["close"],
                                           reason_flags, version="v4")

        # ── Quality gates ──
        _is_buy = "buy" in action.lower()

        # Gate A: Action gate (require Buy Now, override RSI < 45)
        action_blocked = not _is_buy and rsi >= 45

        # Gate B: RSI < 80
        rsi_blocked = rsi >= 80

        # Gate C: Confidence >= 20%
        conf_blocked = confidence < 20 and rsi >= 45

        # Gate D: Real targets (always true since ATR-derived)
        tgt_blocked = False

        # Gate E: Volume gate
        _vol_gate = 1.2 if _is_broad_rally else 1.5
        vol_blocked = vol_ratio < _vol_gate

        gates_ok = not action_blocked and not rsi_blocked and not conf_blocked and not tgt_blocked and not vol_blocked

        # V3: score >= 58
        v3_pass = v3_score >= 58 and gates_ok
        # V4: halved penalties + lower threshold for Buy+vol>=2x
        _high_vol_buy = _is_buy and vol_ratio >= 2.0
        v4_eff_min = 50.0 if _high_vol_buy else 58.0
        v4_pass = v4_score >= v4_eff_min and gates_ok

        # Outcome: entry at open (which is ~3%+ above prev close)
        # Profit = day high - open (intraday upside from entry)
        entry_price = cur["open"]
        intraday_profit_pct = (cur["high"] - entry_price) / entry_price * 100
        intraday_loss_pct = (cur["low"] - entry_price) / entry_price * 100
        eod_pnl_pct = (cur["close"] - entry_price) / entry_price * 100

        # Next day outcome (if exists)
        next_day_pnl = None
        next_day_high_pnl = None
        if idx + 1 < len(daily):
            nxt = daily[idx + 1]
            next_day_pnl = (nxt["close"] - entry_price) / entry_price * 100
            next_day_high_pnl = (nxt["high"] - entry_price) / entry_price * 100

        # Multi-day outcome: max high in next 3 trading days
        max_3d_high = cur["high"]
        for j in range(1, 4):
            if idx + j < len(daily):
                max_3d_high = max(max_3d_high, daily[idx + j]["high"])
        multi_day_profit_pct = (max_3d_high - entry_price) / entry_price * 100

        results.append({
            "sym": sym,
            "date": target_date,
            "open_gap": open_gap_pct,
            "day_change": day_change_pct,
            "max_gain": max_gain_pct,
            "vol_ratio": vol_ratio,
            "rsi": rsi,
            "cci": cci,
            "mfi": mfi,
            "confidence": confidence,
            "action": action,
            "v3_score": v3_score,
            "v4_score": v4_score,
            "v3_pass": v3_pass,
            "v4_pass": v4_pass,
            "v4_eff_min": v4_eff_min,
            "entry_price": entry_price,
            "intraday_profit": intraday_profit_pct,
            "intraday_loss": intraday_loss_pct,
            "eod_pnl": eod_pnl_pct,
            "next_day_pnl": next_day_pnl,
            "next_day_high_pnl": next_day_high_pnl,
            "multi_day_profit": multi_day_profit_pct,
            "nifty_chg": nifty_chg,
            "action_blocked": action_blocked,
            "vol_blocked": vol_blocked,
            "rsi_blocked": rsi_blocked,
            "gates_ok": gates_ok,
        })

    return results


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 100)
    print("  HISTORICAL BACKTEST: V4 Big Runner Scorer")
    print(f"  Data source: {DATA_DIR}")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 100)

    print("\nLoading daily OHLCV data for all stocks...")
    stocks = load_all_stocks()
    print(f"Loaded {len(stocks)} stocks with 50+ days of data")

    # Build a common date index (intersection of major stocks)
    # Use a large-cap stock to get the trading calendar
    ref_sym = None
    for candidate in ["RELIANCE", "TCS", "INFY", "HDFC", "SBIN"]:
        if candidate in stocks and len(stocks[candidate]) >= 200:
            ref_sym = candidate
            break
    if not ref_sym:
        ref_sym = max(stocks.keys(), key=lambda s: len(stocks[s]))

    ref_data = stocks[ref_sym]
    all_dates = [r["date"] for r in ref_data]
    print(f"Trading calendar from {ref_sym}: {len(all_dates)} days")
    print(f"Date range: {all_dates[0]} to {all_dates[-1]}")

    # Use Nifty 50 ETF or a proxy for market direction
    nifty_data = stocks.get("NIFTYBEES", stocks.get("NIFTY", None))

    # Scan parameters
    WARMUP = 30  # Skip first 30 days for indicator warmup
    start_idx = WARMUP
    end_idx = len(all_dates) - 1  # Leave last day for next-day outcome

    print(f"\nScanning {end_idx - start_idx} trading days ({all_dates[start_idx]} to {all_dates[end_idx - 1]})...")

    all_results = []
    days_scanned = 0
    for i in range(start_idx, end_idx):
        day_results = scan_day(stocks, i, all_dates, nifty_data)
        all_results.extend(day_results)
        days_scanned += 1
        if days_scanned % 50 == 0:
            print(f"  Scanned {days_scanned} days, {len(all_results)} candidates so far...")

    print(f"\nTotal: {days_scanned} days scanned, {len(all_results)} stocks opened 3%+ above prev close")

    # ──────────────────────────────────────────────────────────────────
    # Results Analysis
    # ──────────────────────────────────────────────────────────────────

    v3_alerts = [r for r in all_results if r["v3_pass"]]
    v4_alerts = [r for r in all_results if r["v4_pass"]]
    v4_new = [r for r in all_results if r["v4_pass"] and not r["v3_pass"]]

    print(f"\n{'='*100}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*100}")
    print(f"\n  Total 3%+ gap-up movers scanned: {len(all_results)}")
    print(f"  V3 alerts (quality gates + score>=58): {len(v3_alerts)}")
    print(f"  V4 alerts (V3 + halved penalties + lower threshold): {len(v4_alerts)}")
    print(f"  V4 new additions (V4 only, not V3): {len(v4_new)}")

    def analyze(alerts, label):
        if not alerts:
            print(f"\n  {label}: no alerts")
            return
        n = len(alerts)

        # Intraday P&L (entry at open of gap-up day, exit at day high)
        intraday_profits = [r["intraday_profit"] for r in alerts]
        intraday_losses = [r["intraday_loss"] for r in alerts]
        eod_pnls = [r["eod_pnl"] for r in alerts]
        multi_day = [r["multi_day_profit"] for r in alerts]

        # Win definition: intraday profit >= 2% from entry (open)
        intraday_wins = sum(1 for p in intraday_profits if p >= 2.0)
        intraday_wins_1 = sum(1 for p in intraday_profits if p >= 1.0)
        profit_3 = sum(1 for p in intraday_profits if p >= 3.0)
        profit_5 = sum(1 for p in intraday_profits if p >= 5.0)
        profit_7 = sum(1 for p in intraday_profits if p >= 7.0)
        profit_10 = sum(1 for p in intraday_profits if p >= 10.0)

        # EOD positive
        eod_positive = sum(1 for p in eod_pnls if p > 0)

        # Max drawdown from entry
        avg_drawdown = sum(intraday_losses) / n
        worst_drawdown = min(intraday_losses)

        # Multi-day profit
        md_wins = sum(1 for p in multi_day if p >= 2.0)

        print(f"\n  {label} ({n} alerts across {len(set(r['date'] for r in alerts))} trading days)")
        print(f"    Avg alerts per day: {n / max(1, len(set(r['date'] for r in alerts))):.1f}")
        print(f"")
        print(f"    INTRADAY (entry at gap-up open → day high):")
        print(f"      Avg upside to high  : {sum(intraday_profits)/n:+.2f}%")
        print(f"      Best                : {max(intraday_profits):+.2f}%")
        print(f"      Avg max drawdown    : {avg_drawdown:+.2f}%")
        print(f"      Worst drawdown      : {worst_drawdown:+.2f}%")
        print(f"")
        print(f"      Win 1%+ from entry  : {intraday_wins_1}/{n} ({intraday_wins_1/n*100:.1f}%)")
        print(f"      Win 2%+ from entry  : {intraday_wins}/{n} ({intraday_wins/n*100:.1f}%)")
        print(f"      Win 3%+ from entry  : {profit_3}/{n} ({profit_3/n*100:.1f}%)")
        print(f"      Win 5%+ from entry  : {profit_5}/{n} ({profit_5/n*100:.1f}%)")
        print(f"      Win 7%+ from entry  : {profit_7}/{n} ({profit_7/n*100:.1f}%)")
        print(f"      Win 10%+ from entry : {profit_10}/{n} ({profit_10/n*100:.1f}%)")
        print(f"")
        print(f"    END OF DAY:")
        print(f"      EOD positive        : {eod_positive}/{n} ({eod_positive/n*100:.1f}%)")
        print(f"      Avg EOD P&L         : {sum(eod_pnls)/n:+.2f}%")
        print(f"")
        print(f"    MULTI-DAY (3-day max high):")
        print(f"      Avg 3-day upside    : {sum(multi_day)/n:+.2f}%")
        print(f"      Win 2%+ in 3 days   : {md_wins}/{n} ({md_wins/n*100:.1f}%)")

    analyze(all_results, "ALL 3%+ gap-ups (no filter)")
    analyze(v3_alerts, "V3 (ultra-selective)")
    analyze(v4_alerts, "V4 (big runner mode)")
    if v4_new:
        analyze(v4_new, "V4 NEW (added by V4, not in V3)")

    # ──────────────────────────────────────────────────────────────────
    # Market condition breakdown
    # ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  MARKET CONDITION BREAKDOWN")
    print(f"{'='*100}")

    for cond_label, cond_fn in [
        ("Bull days (Nifty >= +1%)", lambda r: r["nifty_chg"] >= 1.0),
        ("Flat days (Nifty -1% to +1%)", lambda r: -1.0 <= r["nifty_chg"] < 1.0),
        ("Bear days (Nifty < -1%)", lambda r: r["nifty_chg"] < -1.0),
    ]:
        sub_v4 = [r for r in v4_alerts if cond_fn(r)]
        if sub_v4:
            n = len(sub_v4)
            wins = sum(1 for r in sub_v4 if r["intraday_profit"] >= 2.0)
            avg_p = sum(r["intraday_profit"] for r in sub_v4) / n
            avg_d = sum(r["intraday_loss"] for r in sub_v4) / n
            print(f"\n  {cond_label}: {n} V4 alerts")
            print(f"    Win rate (2%+): {wins}/{n} ({wins/n*100:.0f}%)")
            print(f"    Avg intraday upside: {avg_p:+.2f}%")
            print(f"    Avg max drawdown: {avg_d:+.2f}%")
        else:
            print(f"\n  {cond_label}: no V4 alerts")

    # ──────────────────────────────────────────────────────────────────
    # Monthly breakdown
    # ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  MONTHLY BREAKDOWN (V4)")
    print(f"{'='*100}")
    print(f"  {'Month':10s} {'Alerts':>7s} {'Win 2%+':>8s} {'Win%':>6s} {'Avg Up':>8s} {'Avg DD':>8s} {'Avg EOD':>8s}")

    monthly = defaultdict(list)
    for r in v4_alerts:
        month = r["date"][:7]  # YYYY-MM
        monthly[month].append(r)

    for month in sorted(monthly.keys()):
        alerts = monthly[month]
        n = len(alerts)
        wins = sum(1 for r in alerts if r["intraday_profit"] >= 2.0)
        avg_up = sum(r["intraday_profit"] for r in alerts) / n
        avg_dd = sum(r["intraday_loss"] for r in alerts) / n
        avg_eod = sum(r["eod_pnl"] for r in alerts) / n
        print(f"  {month:10s} {n:7d} {wins:5d}/{n:<3d} {wins/n*100:5.1f}% {avg_up:+7.2f}% {avg_dd:+7.2f}% {avg_eod:+7.2f}%")

    # ──────────────────────────────────────────────────────────────────
    # Volume analysis
    # ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  VOLUME IMPACT (V4 alerts)")
    print(f"{'='*100}")
    for vol_label, vol_lo, vol_hi in [
        ("vol < 1.5x", 0, 1.5),
        ("1.5x <= vol < 2.0x", 1.5, 2.0),
        ("2.0x <= vol < 3.0x", 2.0, 3.0),
        ("vol >= 3.0x", 3.0, 999),
    ]:
        sub = [r for r in v4_alerts if vol_lo <= r["vol_ratio"] < vol_hi]
        if sub:
            n = len(sub)
            wins = sum(1 for r in sub if r["intraday_profit"] >= 2.0)
            avg_up = sum(r["intraday_profit"] for r in sub) / n
            print(f"  {vol_label:25s}: {n:4d} alerts, win {wins}/{n} ({wins/n*100:.0f}%), avg up {avg_up:+.2f}%")

    # ──────────────────────────────────────────────────────────────────
    # Worst trades (biggest losers)
    # ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  WORST V4 TRADES (bottom 20 by intraday drawdown)")
    print(f"{'='*100}")
    worst = sorted(v4_alerts, key=lambda x: x["intraday_loss"])[:20]
    print(f"  {'Date':12s} {'Sym':15s} {'Open':>7s} {'Gap%':>6s} {'High%':>6s} {'Low%':>7s} {'EOD%':>6s} "
          f"{'Vol':>5s} {'RSI':>4s} {'CCI':>5s} {'Score':>5s}")
    for r in worst:
        print(f"  {r['date']:12s} {r['sym']:15s} {r['entry_price']:7.1f} {r['open_gap']:+5.1f}% "
              f"{r['intraday_profit']:+5.1f}% {r['intraday_loss']:+6.1f}% {r['eod_pnl']:+5.1f}% "
              f"{r['vol_ratio']:4.1f}x {r['rsi']:4.0f} {r['cci']:5.0f} {r['v4_score']:5.0f}")

    # ──────────────────────────────────────────────────────────────────
    # Best trades
    # ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  BEST V4 TRADES (top 20 by intraday profit)")
    print(f"{'='*100}")
    best = sorted(v4_alerts, key=lambda x: x["intraday_profit"], reverse=True)[:20]
    print(f"  {'Date':12s} {'Sym':15s} {'Open':>7s} {'Gap%':>6s} {'High%':>6s} {'Low%':>7s} {'EOD%':>6s} "
          f"{'Vol':>5s} {'RSI':>4s} {'CCI':>5s} {'Score':>5s}")
    for r in best:
        print(f"  {r['date']:12s} {r['sym']:15s} {r['entry_price']:7.1f} {r['open_gap']:+5.1f}% "
              f"{r['intraday_profit']:+5.1f}% {r['intraday_loss']:+6.1f}% {r['eod_pnl']:+5.1f}% "
              f"{r['vol_ratio']:4.1f}x {r['rsi']:4.0f} {r['cci']:5.0f} {r['v4_score']:5.0f}")

    # ──────────────────────────────────────────────────────────────────
    # FINAL VERDICT
    # ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  FINAL VERDICT")
    print(f"{'='*100}")
    if v4_alerts:
        n = len(v4_alerts)
        w2 = sum(1 for r in v4_alerts if r["intraday_profit"] >= 2.0)
        w1 = sum(1 for r in v4_alerts if r["intraday_profit"] >= 1.0)
        avg = sum(r["intraday_profit"] for r in v4_alerts) / n
        eod_pos = sum(1 for r in v4_alerts if r["eod_pnl"] > 0)
        print(f"\n  V4 across {len(set(r['date'] for r in v4_alerts))} trading days:")
        print(f"  Total alerts: {n}")
        print(f"  Win rate (1%+ intraday profit): {w1}/{n} = {w1/n*100:.1f}%")
        print(f"  Win rate (2%+ intraday profit): {w2}/{n} = {w2/n*100:.1f}%")
        print(f"  EOD positive: {eod_pos}/{n} = {eod_pos/n*100:.1f}%")
        print(f"  Avg intraday upside: {avg:+.2f}%")
        print(f"  Strategy: buy at open on 3%+ gap-up days that pass V4 filters")
    print()


if __name__ == "__main__":
    main()

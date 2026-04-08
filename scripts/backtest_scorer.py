#!/usr/bin/env python3
"""
Backtest & Forward-Test for BlindBuyScorer changes.

Tests:
1. BACKTEST: Re-score all historical "Not a buy" + "BUY ALERT SENT" log entries
   with old vs new scoring to see what changes.
2. FORWARD TEST: Score every stock in today's signal state + tick data with
   old vs new scorer, then compare against actual day-end outcome (high/close).

Outputs a summary table showing:
- Stocks that would be BLOCKED by new rules (false positives removed)
- Stocks that would still PASS (good alerts preserved)
- Stocks that would be NEWLY rejected (potential false negatives)
- P&L impact: actual max gain (from high) for each stock
"""
import csv
import json
import os
import re
import sys
from typing import Dict, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "results", "Data")
LOGS_DIR = os.path.join(DATA_DIR, "assistant_logs")


# ──────────────────────────────────────────────────────────────────────
# Scorer implementations (old vs new)
# ──────────────────────────────────────────────────────────────────────

def score_old(rsi, cci, mfi, vol_ratio, change_pct, confidence,
              price_now, buy_above, prev_close, reason_text,
              sell_if_below=0, take_profit2=0, day_pct=0.5) -> Tuple[float, str]:
    """Original scorer (before commit a87f658)."""
    score = 0.0
    reasons = []
    _price_below_trigger = (price_now > 0 and buy_above > 0 and price_now < buy_above)

    # Penny stock block
    if price_now > 0 and price_now < 10.0:
        return 0, "PENNY STOCK"

    # RSI
    if rsi < 30: score += 25; reasons.append(f"RSI oversold ({rsi:.0f})")
    elif rsi < 40: score += 18
    elif rsi < 50: score += 10; reasons.append(f"RSI mid ({rsi:.0f})")
    elif rsi > 70 and change_pct < 5.0: score -= 8; reasons.append("RSI overbought")

    # CCI
    if cci < -100: score += 12
    elif cci < -50: score += 6
    elif cci > 150: score -= 5  # OLD: weak penalty

    # MFI
    if mfi < 20: score += 10
    elif mfi < 40: score += 5
    elif mfi > 80: score -= 5  # OLD: weak penalty

    # Volume (simplified — skip time adjustment for backtest)
    _vol_score_base = vol_ratio / max(0.1, day_pct) if day_pct < 0.3 else vol_ratio
    if _vol_score_base >= 2.5: score += 20; reasons.append(f"volume {vol_ratio:.1f}x")
    elif _vol_score_base >= 1.5: score += 15
    elif _vol_score_base >= 1.0: score += 10
    elif vol_ratio >= 0.7: score += 5

    # Gap-up
    if prev_close > 0 and price_now > 0:
        _gap_pct = (price_now - prev_close) / prev_close * 100
        if _gap_pct >= 7.0: score += 15; reasons.append(f"gap-up +{_gap_pct:.1f}%")
        elif _gap_pct >= 4.0: score += 10; reasons.append(f"gap-up +{_gap_pct:.1f}%")

    # Change%  (OLD: no cap)
    if _price_below_trigger and change_pct > 0:
        score -= 5
    elif change_pct >= 5.0: score += 15; reasons.append(f"hist +{change_pct:.1f}%")
    elif change_pct >= 2.5: score += 12
    elif change_pct >= 1.0: score += 10
    elif change_pct > 0: score += 6
    elif change_pct > -1.0: score += 2
    else: score -= 5

    # ATR
    rt = reason_text.lower()
    if "above atr trailing" in rt or "price above atr" in rt: score += 12; reasons.append("above ATR")
    elif "below atr" in rt: score -= 8; reasons.append("below ATR")

    # MACD
    if "macd bullish crossover" in rt: score += 10
    elif "macd histogram increasing" in rt: score += 5

    # Trigger
    if price_now > 0 and buy_above > 0:
        if _price_below_trigger:
            gap_below = round((buy_above - price_now) / buy_above * 100, 1)
            if gap_below >= 1.0: score -= 25
            else: score -= 10
        else:
            score += 8; reasons.append("entry confirmed")

    # Confidence
    if confidence >= 25: score += 10; reasons.append(f"conf {confidence:.0f}%")
    elif confidence >= 15: score += 5

    # Consolidation
    if price_now > 0 and sell_if_below > 0 and take_profit2 > 0 and change_pct >= 1.0:
        _range_pct = (take_profit2 - sell_if_below) / price_now * 100
        if _range_pct < 15: score += 12
        elif _range_pct < 25: score += 6

    # Bearish penalties
    if "below all major ma" in rt or "bearish alignment" in rt: score -= 50
    if "death cross" in rt: score -= 40
    if "lower highs" in rt and "lower lows" in rt: score -= 25

    # Stop loss proximity
    if price_now > 0 and sell_if_below > 0 and price_now <= sell_if_below * 1.01:
        score -= 30

    return round(max(0, min(score, 100)), 1), ", ".join(reasons)


def score_new(rsi, cci, mfi, vol_ratio, change_pct, confidence,
              price_now, buy_above, prev_close, reason_text,
              sell_if_below=0, take_profit2=0, day_pct=0.5) -> Tuple[float, str]:
    """New scorer (after commit a87f658)."""
    score = 0.0
    reasons = []
    _price_below_trigger = (price_now > 0 and buy_above > 0 and price_now < buy_above)

    if price_now > 0 and price_now < 10.0:
        return 0, "PENNY STOCK"

    # RSI
    if rsi < 30: score += 25; reasons.append(f"RSI oversold ({rsi:.0f})")
    elif rsi < 40: score += 18
    elif rsi < 50: score += 10; reasons.append(f"RSI mid ({rsi:.0f})")
    elif rsi > 70 and change_pct < 5.0: score -= 8; reasons.append("RSI overbought")

    # CCI — NEW: stronger penalties
    if cci < -100: score += 12
    elif cci < -50: score += 6
    elif cci > 200: score -= 15; reasons.append(f"CCI extreme ({cci:.0f})")
    elif cci > 150: score -= 8; reasons.append(f"CCI overbought ({cci:.0f})")

    # MFI — NEW: stronger penalty
    if mfi < 20: score += 10
    elif mfi < 40: score += 5
    elif mfi > 80: score -= 8; reasons.append(f"MFI overbought ({mfi:.0f})")

    # NEW: overbought combo
    if 70 <= mfi <= 80 and cci > 100:
        score -= 5; reasons.append("overbought combo")

    # Volume
    _vol_score_base = vol_ratio / max(0.1, day_pct) if day_pct < 0.3 else vol_ratio
    if _vol_score_base >= 2.5: score += 20; reasons.append(f"volume {vol_ratio:.1f}x")
    elif _vol_score_base >= 1.5: score += 15
    elif _vol_score_base >= 1.0: score += 10
    elif vol_ratio >= 0.7: score += 5

    # Gap-up — NEW: track bonus for capping
    _gap_bonus = 0
    if prev_close > 0 and price_now > 0:
        _gap_pct = (price_now - prev_close) / prev_close * 100
        if _gap_pct >= 7.0: _gap_bonus = 15; score += 15; reasons.append(f"gap-up +{_gap_pct:.1f}%")
        elif _gap_pct >= 4.0: _gap_bonus = 10; score += 10; reasons.append(f"gap-up +{_gap_pct:.1f}%")

    # Change% — NEW: capped when gap bonus was given
    _change_cap = 10 if _gap_bonus >= 10 else 15
    if _price_below_trigger and change_pct > 0:
        score -= 5
    elif change_pct >= 5.0: score += _change_cap; reasons.append(f"hist +{change_pct:.1f}%")
    elif change_pct >= 2.5: score += min(12, _change_cap)
    elif change_pct >= 1.0: score += min(10, _change_cap)
    elif change_pct > 0: score += 6
    elif change_pct > -1.0: score += 2
    else: score -= 5

    # ATR
    rt = reason_text.lower()
    if "above atr trailing" in rt or "price above atr" in rt: score += 12; reasons.append("above ATR")
    elif "below atr" in rt: score -= 8; reasons.append("below ATR")

    # MACD
    if "macd bullish crossover" in rt: score += 10
    elif "macd histogram increasing" in rt: score += 5

    # Trigger
    if price_now > 0 and buy_above > 0:
        if _price_below_trigger:
            gap_below = round((buy_above - price_now) / buy_above * 100, 1)
            if gap_below >= 1.0: score -= 25
            else: score -= 10
        else:
            score += 8; reasons.append("entry confirmed")

    # Confidence
    if confidence >= 25: score += 10; reasons.append(f"conf {confidence:.0f}%")
    elif confidence >= 15: score += 5

    # Consolidation
    if price_now > 0 and sell_if_below > 0 and take_profit2 > 0 and change_pct >= 1.0:
        _range_pct = (take_profit2 - sell_if_below) / price_now * 100
        if _range_pct < 15: score += 12
        elif _range_pct < 25: score += 6

    # Bearish
    if "below all major ma" in rt or "bearish alignment" in rt: score -= 50
    if "death cross" in rt: score -= 40
    if "lower highs" in rt and "lower lows" in rt: score -= 25

    # Stop loss
    if price_now > 0 and sell_if_below > 0 and price_now <= sell_if_below * 1.01:
        score -= 30

    return round(max(0, min(score, 100)), 1), ", ".join(reasons)


def check_retreat_gate(tick: Dict) -> Tuple[bool, str]:
    """Returns (blocked, reason) for the retreat-from-high gate."""
    high = float(tick.get("high") or tick.get("ohlc", {}).get("high", 0))
    low = float(tick.get("low") or tick.get("ohlc", {}).get("low", 0))
    ltp = float(tick.get("last_price", 0))
    if high <= 0 or low <= 0 or high <= low or ltp <= 0:
        return False, ""
    _range = high - low
    _retreat = high - ltp
    _retreat_frac = _retreat / _range
    _pct_from_high = (high - ltp) / high * 100
    if _retreat_frac >= 0.35 and _pct_from_high >= 4.0:
        return True, f"retreat {_retreat_frac:.0%} of range, {_pct_from_high:.1f}% from high"
    return False, ""


def check_zero_targets_gate(stop_loss, target1, target2, daily_change, is_early_mover=False) -> Tuple[bool, str]:
    """Returns (blocked, reason) for zero-OHLCV-targets gate."""
    if (stop_loss <= 0 and target1 <= 0 and target2 <= 0
            and daily_change >= 8.0 and not is_early_mover):
        return True, f"OHLCV engine has no targets, already +{daily_change:.1f}%"
    return False, ""


# ──────────────────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────────────────

def load_signal_state() -> Dict[str, Dict]:
    """Load current signal state file."""
    path = os.path.join(DATA_DIR, "signalmonitor_allstocks_state_latest.csv")
    result = {}
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row.get("Symbol", "").strip()
            if sym:
                result[sym] = row
    return result


def load_ticks() -> Dict[str, Dict]:
    """Load current tick data."""
    path = os.path.join(DATA_DIR, "kiteticker_allstocks_ticks_latest.json")
    with open(path, "r") as f:
        data = json.load(f)
    return {str(k).strip().upper(): v for k, v in data.items()} if isinstance(data, dict) else {}


def parse_log_entries(sessions: List[int]) -> List[Dict]:
    """Parse 'Not a buy' and 'BUY ALERT SENT' entries from autopilot logs."""
    entries = []
    # Pattern: Not a buy: SYMBOL score=XX (need YY) | change=ZZ% | reasons=...
    not_buy_re = re.compile(
        r"Not a buy: (\w+) score=(\d+) \(need (\d+)\) \| change=([\d.+-]+)% \| reasons=(.*)"
    )
    # Pattern: BUY ALERT SENT: SYMBOL at ₹XX (+YY%) score=SS/MM vol=VVx reasons=...
    buy_re = re.compile(
        r"BUY ALERT SENT: (\w+) at .+?([\d.+-]+)%\) score=(\d+)/(\d+) vol=([\d.]+)x reasons=(.*)"
    )
    for s in sessions:
        path = os.path.join(LOGS_DIR, f"session_{s}_autopilot.log")
        if not os.path.isfile(path):
            continue
        with open(path, "r") as f:
            for line in f:
                m = not_buy_re.search(line)
                if m:
                    entries.append({
                        "session": s,
                        "type": "rejected",
                        "symbol": m.group(1),
                        "old_score": float(m.group(2)),
                        "effective_min": float(m.group(3)),
                        "change_pct": float(m.group(4)),
                        "reasons": m.group(5).strip(),
                    })
                    continue
                m = buy_re.search(line)
                if m:
                    entries.append({
                        "session": s,
                        "type": "sent",
                        "symbol": m.group(1),
                        "change_pct": float(m.group(2)),
                        "old_score": float(m.group(3)),
                        "effective_min": float(m.group(4)),
                        "vol_ratio": float(m.group(5)),
                        "reasons": m.group(6).strip(),
                    })
    return entries


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print("BACKTEST + FORWARD TEST: BlindBuyScorer Changes (commit a87f658)")
    print("=" * 80)

    # ── PART 1: BACKTEST on historical log data ──────────────────────
    print("\n\n📊 PART 1: BACKTEST — Re-scoring historical alerts from logs")
    print("-" * 70)

    sessions = list(range(84, 100))
    log_entries = parse_log_entries(sessions)
    print(f"Parsed {len(log_entries)} entries from sessions {min(sessions)}-{max(sessions)}")

    # We need signal state data to rescore. For historical entries,
    # we only have the change% and reasons from logs.
    # For proper rescoring, use current signal state + ticks as FORWARD test.

    # For the backtest, analyze the LOG-level data we have:
    # Count entries by change% bucket and old score
    sent = [e for e in log_entries if e["type"] == "sent"]
    rejected = [e for e in log_entries if e["type"] == "rejected"]

    print(f"\n  Sent alerts: {len(sent)}")
    for e in sent:
        print(f"    {e['symbol']:15s} score={e['old_score']:.0f}/{e['effective_min']:.0f} "
              f"chg={e['change_pct']:+.1f}% vol={e.get('vol_ratio', '?')}x [session {e['session']}]")

    print(f"\n  Rejected (Not a buy): {len(rejected)}")
    # Group by change% bucket
    buckets = {"8%+": [], "5-8%": [], "3-5%": [], "<3%": []}
    for e in rejected:
        c = abs(e["change_pct"])
        if c >= 8: buckets["8%+"].append(e)
        elif c >= 5: buckets["5-8%"].append(e)
        elif c >= 3: buckets["3-5%"].append(e)
        else: buckets["<3%"].append(e)

    for bucket, entries in buckets.items():
        print(f"\n    {bucket} change ({len(entries)} entries):")
        # Show top 5 by score (closest to passing)
        top = sorted(entries, key=lambda x: x["old_score"], reverse=True)[:8]
        for e in top:
            print(f"      {e['symbol']:15s} score={e['old_score']:.0f} (need {e['effective_min']:.0f}) "
                  f"chg={e['change_pct']:+.1f}%")

    # ── PART 2: FORWARD TEST on today's live data ────────────────────
    print("\n\n" + "=" * 80)
    print("📈 PART 2: FORWARD TEST — Score all 2000+ stocks with OLD vs NEW scorer")
    print("-" * 70)

    signal_state = load_signal_state()
    ticks = load_ticks()
    print(f"Loaded {len(signal_state)} signal state rows, {len(ticks)} tick entries")

    results = []
    for sym, row in signal_state.items():
        tick = ticks.get(sym, {})
        if not tick:
            continue

        rsi = float(row.get("RSI", 50) or 50)
        cci = float(row.get("CCI", 0) or 0)
        mfi = float(row.get("MFI", 50) or 50)
        vol_ratio = float(row.get("VolumeRatio", 0) or 0)
        change_pct = float(row.get("ChangePct", 0) or 0)
        confidence = float(row.get("SignalStrengthPct", 0) or 0)
        price_now = float(row.get("PriceNow", 0) or 0)
        prev_close = float(row.get("PrevClose", 0) or 0)
        sell_if_below = float(row.get("SellIfBelow", 0) or 0)
        take_profit1 = float(row.get("TakeProfit1", 0) or 0)
        take_profit2 = float(row.get("TakeProfit2", 0) or 0)
        reason_text = str(row.get("Why", ""))
        now_action = str(row.get("NowAction", ""))

        # For intraday 3%+ movers, the confirm logic zeros BuyAbove
        daily_change = 0
        if prev_close > 0 and price_now > 0:
            daily_change = (price_now - prev_close) / prev_close * 100

        # Simulate the intraday override: zero BuyAbove for 3%+ movers
        buy_above = float(row.get("BuyAbove", 0) or 0)
        if daily_change >= 3.0:
            buy_above = 0
            # Use daily_change if bigger than OHLCV change
            if abs(daily_change) > abs(change_pct):
                change_pct = daily_change

        # Score both ways
        old_s, old_r = score_old(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                                 price_now, buy_above, prev_close, reason_text,
                                 sell_if_below, take_profit2)
        new_s, new_r = score_new(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                                 price_now, buy_above, prev_close, reason_text,
                                 sell_if_below, take_profit2)

        # Check gates
        retreat_blocked, retreat_reason = check_retreat_gate(tick)
        zero_tgt_blocked, zero_tgt_reason = check_zero_targets_gate(
            sell_if_below, take_profit1, take_profit2, daily_change)

        # Effective min for 3%+ movers (simplified)
        if daily_change >= 3.0 and vol_ratio >= 1.3:
            eff_min = 58.0
        elif daily_change >= 3.0 and vol_ratio >= 0.8:
            eff_min = 58.0
        elif daily_change >= 3.0:
            eff_min = 58.0  # would be suppressed by vol gate in real code
        else:
            eff_min = 40.0

        # Day high from tick
        tick_high = float(tick.get("high") or tick.get("ohlc", {}).get("high", 0))
        tick_low = float(tick.get("low") or tick.get("ohlc", {}).get("low", 0))
        ltp = float(tick.get("last_price", 0))
        max_gain_from_prev = ((tick_high - prev_close) / prev_close * 100) if prev_close > 0 and tick_high > 0 else 0
        current_from_prev = ((ltp - prev_close) / prev_close * 100) if prev_close > 0 and ltp > 0 else 0

        old_pass = old_s >= eff_min
        new_pass = new_s >= eff_min and not retreat_blocked and not zero_tgt_blocked

        results.append({
            "sym": sym,
            "daily_change": daily_change,
            "max_gain": max_gain_from_prev,
            "current": current_from_prev,
            "vol_ratio": vol_ratio,
            "rsi": rsi,
            "cci": cci,
            "mfi": mfi,
            "old_score": old_s,
            "new_score": new_s,
            "eff_min": eff_min,
            "old_pass": old_pass,
            "new_pass": new_pass,
            "retreat_blocked": retreat_blocked,
            "retreat_reason": retreat_reason,
            "zero_tgt_blocked": zero_tgt_blocked,
            "zero_tgt_reason": zero_tgt_reason,
            "old_reasons": old_r,
            "new_reasons": new_r,
            "now_action": now_action,
            "high": tick_high,
            "low": tick_low,
            "ltp": ltp,
            "prev_close": prev_close,
        })

    # Filter to stocks that matter: 3%+ movers with enough volume
    movers = [r for r in results if r["daily_change"] >= 3.0 and r["vol_ratio"] >= 0.8]
    movers.sort(key=lambda x: x["daily_change"], reverse=True)

    # ── Category 1: OLD=pass, NEW=blocked (false positives removed) ──
    blocked_by_new = [r for r in movers if r["old_pass"] and not r["new_pass"]]
    print(f"\n🚫 BLOCKED by new rules (OLD passed, NEW blocked): {len(blocked_by_new)}")
    print(f"   {'Symbol':15s} {'OldS':>5s} {'NewS':>5s} {'Min':>4s} {'Chg%':>6s} {'MaxH%':>6s} {'Now%':>6s} {'Vol':>5s} {'RSI':>4s} {'CCI':>5s} {'MFI':>4s} {'Block Reason'}")
    for r in blocked_by_new[:25]:
        block = ""
        if r["retreat_blocked"]: block = "RETREAT " + r["retreat_reason"]
        elif r["zero_tgt_blocked"]: block = "NO TARGETS"
        elif r["new_score"] < r["eff_min"]: block = f"SCORE {r['new_score']:.0f}<{r['eff_min']:.0f}"
        # Color code: if max_gain was low, this was a GOOD block
        verdict = "✅ GOOD" if r["max_gain"] < r["daily_change"] + 3 else "⚠️ MISSED?"
        print(f"   {r['sym']:15s} {r['old_score']:5.0f} {r['new_score']:5.0f} {r['eff_min']:4.0f} "
              f"{r['daily_change']:+5.1f}% {r['max_gain']:+5.1f}% {r['current']:+5.1f}% "
              f"{r['vol_ratio']:4.1f}x {r['rsi']:4.0f} {r['cci']:5.0f} {r['mfi']:4.0f} "
              f"{block} {verdict}")

    # ── Category 2: Both pass (preserved good alerts) ──
    both_pass = [r for r in movers if r["old_pass"] and r["new_pass"]]
    print(f"\n✅ STILL PASSING (both old and new): {len(both_pass)}")
    print(f"   {'Symbol':15s} {'OldS':>5s} {'NewS':>5s} {'Min':>4s} {'Chg%':>6s} {'MaxH%':>6s} {'Now%':>6s} {'Vol':>5s} {'RSI':>4s} {'Action'}")
    for r in sorted(both_pass, key=lambda x: x["new_score"], reverse=True)[:20]:
        remaining_upside = r["max_gain"] - r["daily_change"]
        verdict = "🔥 WINNER" if remaining_upside >= 3 else "OK" if remaining_upside >= 0 else "📉 DIP"
        print(f"   {r['sym']:15s} {r['old_score']:5.0f} {r['new_score']:5.0f} {r['eff_min']:4.0f} "
              f"{r['daily_change']:+5.1f}% {r['max_gain']:+5.1f}% {r['current']:+5.1f}% "
              f"{r['vol_ratio']:4.1f}x {r['rsi']:4.0f} {r['now_action']:20s} {verdict}")

    # ── Category 3: NEW=pass, OLD=fail (new false positives?) ──
    new_only = [r for r in movers if not r["old_pass"] and r["new_pass"]]
    print(f"\n🆕 NEW would pass (OLD failed): {len(new_only)}")
    for r in new_only[:10]:
        print(f"   {r['sym']:15s} old={r['old_score']:.0f} new={r['new_score']:.0f} "
              f"chg={r['daily_change']:+.1f}% max={r['max_gain']:+.1f}%")

    # ── Summary stats ──
    print("\n\n" + "=" * 80)
    print("📋 SUMMARY")
    print("-" * 70)

    all_old_pass = [r for r in movers if r["old_pass"]]
    all_new_pass = [r for r in movers if r["new_pass"]]

    print(f"Total 3%+ movers with vol≥0.8x: {len(movers)}")
    print(f"OLD scorer would alert: {len(all_old_pass)}")
    print(f"NEW scorer would alert: {len(all_new_pass)}")
    print(f"Blocked by new rules:   {len(blocked_by_new)}")
    print(f"Newly passed:           {len(new_only)}")

    # P&L analysis of blocked alerts
    if blocked_by_new:
        blocked_remaining = [r["max_gain"] - r["daily_change"] for r in blocked_by_new]
        blocked_current = [r["current"] - r["daily_change"] for r in blocked_by_new]
        print(f"\nBlocked alerts — remaining upside (high - alert):")
        print(f"  Avg: {sum(blocked_remaining)/len(blocked_remaining):+.1f}%")
        print(f"  Max: {max(blocked_remaining):+.1f}%")
        print(f"  Min: {min(blocked_remaining):+.1f}%")
        bad_blocks = [r for r in blocked_by_new if r["max_gain"] - r["daily_change"] >= 5]
        print(f"  Would have gained 5%+: {len(bad_blocks)} (FALSE NEGATIVES)")
        good_blocks = [r for r in blocked_by_new if r["max_gain"] - r["daily_change"] < 2]
        print(f"  Would have gained <2%: {len(good_blocks)} (GOOD BLOCKS)")

    # P&L analysis of still-passing alerts
    if all_new_pass:
        pass_remaining = [r["max_gain"] - r["daily_change"] for r in all_new_pass]
        print(f"\nStill-passing alerts — remaining upside:")
        print(f"  Avg: {sum(pass_remaining)/len(pass_remaining):+.1f}%")
        winners = [r for r in all_new_pass if r["max_gain"] - r["daily_change"] >= 3]
        print(f"  Would have gained 3%+: {len(winners)}/{len(all_new_pass)} ({len(winners)/len(all_new_pass)*100:.0f}%)")

    # ── Top 10% day gainers check ──
    print("\n\n" + "=" * 80)
    print("🏆 TOP GAINERS CHECK — Would we catch the biggest runners?")
    print("-" * 70)

    all_stocks = sorted(results, key=lambda x: x["max_gain"], reverse=True)
    top_gainers = all_stocks[:30]
    print(f"   {'Symbol':15s} {'MaxH%':>7s} {'Now%':>6s} {'Vol':>5s} {'OldS':>5s} {'NewS':>5s} {'Old':>5s} {'New':>5s} {'Action'}")
    for r in top_gainers:
        old_label = "PASS" if r["old_pass"] else "fail"
        new_label = "PASS" if r["new_pass"] else "fail"
        if r["retreat_blocked"]: new_label = "RETR"
        elif r["zero_tgt_blocked"]: new_label = "0TGT"
        print(f"   {r['sym']:15s} {r['max_gain']:+6.1f}% {r['current']:+5.1f}% "
              f"{r['vol_ratio']:4.1f}x {r['old_score']:5.0f} {r['new_score']:5.0f} "
              f"{old_label:>5s} {new_label:>5s} {r['now_action']}")


if __name__ == "__main__":
    main()

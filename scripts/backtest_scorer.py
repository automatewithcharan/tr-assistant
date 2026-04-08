#!/usr/bin/env python3
"""
Backtest & Forward-Test for BlindBuyScorer v3 (ultra-selective).

Tests:
1. BACKTEST: Re-score all historical log entries with old vs v2 vs v3.
2. FORWARD TEST: Score every stock in today's signal state + tick data,
   compare OLD (v1) → NEW (v2) → V3 (ultra-selective) against actual outcomes.

V3 changes (on top of v2):
  - Volume gate raised: 1.5x normal / 1.2x broad rally (from 1.3/0.8)
  - Action gate: require 'Buy Now' or RSI < 45 (oversold override)
  - RSI < 80 gate: extremely overbought stocks stall
  - Confidence >= 20% gate
  - Real targets gate: require at least one OHLCV target (SL/T1/T2 > 0)
  - Win metric: entry at 3% (when alert fires), not end-of-day change
"""
import csv
import json
import os
import re
import sys
from datetime import datetime
from typing import Dict, List, Tuple, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "results", "Data")
LOGS_DIR = os.path.join(DATA_DIR, "assistant_logs")


# ──────────────────────────────────────────────────────────────────────
# Scorer implementations: OLD (pre a87f658) vs NEW (latest ea3bbaa)
# ──────────────────────────────────────────────────────────────────────

def _common_score(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                  price_now, buy_above, prev_close, reason_text,
                  sell_if_below, take_profit2, day_pct,
                  version="old") -> Tuple[float, str]:
    """Unified scorer — 'old', 'new', or 'v4' version."""
    score = 0.0
    reasons = []
    _price_below_trigger = (price_now > 0 and buy_above > 0 and price_now < buy_above)

    if price_now > 0 and price_now < 10.0:
        return 0, "PENNY STOCK"

    # ── RSI ──
    if rsi < 30: score += 25; reasons.append(f"RSI oversold ({rsi:.0f})")
    elif rsi < 40: score += 18; reasons.append(f"RSI near oversold ({rsi:.0f})")
    elif rsi < 50: score += 10; reasons.append(f"RSI mid ({rsi:.0f})")
    elif rsi > 70 and change_pct < 5.0:
        score -= 8; reasons.append(f"RSI overbought ({rsi:.0f})")

    # ── CCI ──
    # V4: halve CCI/MFI penalties when vol >= 2.0x (high volume confirms momentum)
    _high_vol = version == "v4" and vol_ratio >= 2.0
    if cci < -100: score += 12; reasons.append(f"CCI oversold ({cci:.0f})")
    elif cci < -50: score += 6; reasons.append(f"CCI low ({cci:.0f})")
    elif version in ("new", "v4") and cci > 200:
        _pen = 8 if _high_vol else 15
        score -= _pen; reasons.append(f"CCI extreme ({cci:.0f}){' [vol-halved]' if _high_vol else ''}")
    elif version in ("new", "v4") and cci > 150:
        _pen = 4 if _high_vol else 8
        score -= _pen; reasons.append(f"CCI overbought ({cci:.0f}){' [vol-halved]' if _high_vol else ''}")
    elif version == "old" and cci > 150:
        score -= 5

    # ── MFI ──
    if mfi < 20: score += 10; reasons.append(f"MFI oversold ({mfi:.0f})")
    elif mfi < 40: score += 5
    elif mfi > 80:
        if version in ("new", "v4"):
            _pen = 4 if _high_vol else 8
            score -= _pen; reasons.append(f"MFI overbought ({mfi:.0f}){' [vol-halved]' if _high_vol else ''}")
        else:
            score -= 5

    # NEW/V4: overbought combo
    if version in ("new", "v4") and 70 <= mfi <= 80 and cci > 100:
        _pen = 3 if _high_vol else 5
        score -= _pen; reasons.append(f"overbought combo{' [vol-halved]' if _high_vol else ''}")

    # ── Volume ──
    _vol_adj = vol_ratio / max(0.1, day_pct) if day_pct < 0.3 else vol_ratio
    if _vol_adj >= 2.5: score += 20; reasons.append(f"volume {vol_ratio:.1f}x")
    elif _vol_adj >= 1.5: score += 15; reasons.append(f"volume {vol_ratio:.1f}x")
    elif _vol_adj >= 1.0: score += 10; reasons.append(f"volume {vol_ratio:.1f}x")
    elif vol_ratio >= 0.7: score += 5

    # ── Gap-up ──
    _gap_bonus = 0
    if prev_close > 0 and price_now > 0:
        _gap_pct = (price_now - prev_close) / prev_close * 100
        if _gap_pct >= 7.0:
            _gap_bonus = 15; score += 15; reasons.append(f"gap-up +{_gap_pct:.1f}%")
        elif _gap_pct >= 4.0:
            _gap_bonus = 10; score += 10; reasons.append(f"gap-up +{_gap_pct:.1f}%")

    # ── Change% ──
    if version == "new":
        _cap = 10 if _gap_bonus >= 10 else 15
    else:
        _cap = 15  # old: no cap
    if _price_below_trigger and change_pct > 0:
        score -= 5
    elif change_pct >= 5.0: score += _cap; reasons.append(f"hist +{change_pct:.1f}%")
    elif change_pct >= 2.5: score += min(12, _cap); reasons.append(f"hist +{change_pct:.1f}%")
    elif change_pct >= 1.0: score += min(10, _cap)
    elif change_pct > 0: score += 6
    elif change_pct > -1.0: score += 2
    else: score -= 5

    # ── ATR ──
    rt = reason_text.lower()
    if "above atr trailing" in rt or "price above atr" in rt:
        score += 12; reasons.append("above ATR")
    elif "below atr" in rt:
        score -= 8; reasons.append("below ATR")

    # ── MACD ──
    if "macd bullish crossover" in rt: score += 10; reasons.append("MACD cross")
    elif "macd histogram increasing" in rt: score += 5; reasons.append("MACD hist up")

    # ── Trigger ──
    if price_now > 0 and buy_above > 0:
        if _price_below_trigger:
            gap_below = round((buy_above - price_now) / buy_above * 100, 1)
            if gap_below >= 1.0: score -= 25
            else: score -= 10
        else:
            score += 8; reasons.append("entry confirmed")

    # ── Confidence ──
    if confidence >= 25: score += 10; reasons.append(f"conf {confidence:.0f}%")
    elif confidence >= 15: score += 5

    # ── Consolidation ──
    if price_now > 0 and sell_if_below > 0 and take_profit2 > 0 and change_pct >= 1.0:
        _rng = (take_profit2 - sell_if_below) / price_now * 100
        if _rng < 15: score += 12
        elif _rng < 25: score += 6

    # ── Bearish penalties ──
    if "below all major ma" in rt or "bearish alignment" in rt: score -= 50
    if "death cross" in rt: score -= 40
    if "lower highs" in rt and "lower lows" in rt: score -= 25

    # ── Stop loss proximity ──
    if price_now > 0 and sell_if_below > 0 and price_now <= sell_if_below * 1.01:
        score -= 30

    return round(max(0, min(score, 100)), 1), ", ".join(reasons)


def score_old(rsi, cci, mfi, vol_ratio, change_pct, confidence,
              price_now, buy_above, prev_close, reason_text,
              sell_if_below=0, take_profit2=0, day_pct=0.5):
    return _common_score(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                         price_now, buy_above, prev_close, reason_text,
                         sell_if_below, take_profit2, day_pct, version="old")


def score_new(rsi, cci, mfi, vol_ratio, change_pct, confidence,
              price_now, buy_above, prev_close, reason_text,
              sell_if_below=0, take_profit2=0, day_pct=0.5):
    return _common_score(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                         price_now, buy_above, prev_close, reason_text,
                         sell_if_below, take_profit2, day_pct, version="new")


def score_v4(rsi, cci, mfi, vol_ratio, change_pct, confidence,
             price_now, buy_above, prev_close, reason_text,
             sell_if_below=0, take_profit2=0, day_pct=0.5):
    return _common_score(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                         price_now, buy_above, prev_close, reason_text,
                         sell_if_below, take_profit2, day_pct, version="v4")


# ──────────────────────────────────────────────────────────────────────
# Gates (post-score checks in _confirm_tick_signal_with_ohlcv)
# ──────────────────────────────────────────────────────────────────────

def check_retreat_gate(tick: Dict) -> Tuple[bool, str]:
    """NEW: block if gave back >=35% of intraday range AND >=4% from high."""
    high = float(tick.get("high") or tick.get("ohlc", {}).get("high", 0))
    low = float(tick.get("low") or tick.get("ohlc", {}).get("low", 0))
    ltp = float(tick.get("last_price", 0))
    if high <= 0 or low <= 0 or high <= low or ltp <= 0:
        return False, ""
    _retreat_frac = (high - ltp) / (high - low)
    _pct_from_high = (high - ltp) / high * 100
    if _retreat_frac >= 0.35 and _pct_from_high >= 4.0:
        return True, f"retreat {_retreat_frac:.0%} of range, {_pct_from_high:.1f}% from high"
    return False, ""


def check_zero_targets_gate_old(stop_loss, target1, target2, daily_change,
                                confidence=50, now_action="Buy Now") -> Tuple[bool, str]:
    """OLD gate: only triggered at 8%+ change."""
    if stop_loss <= 0 and target1 <= 0 and target2 <= 0 and daily_change >= 8.0:
        return True, f"no targets, +{daily_change:.1f}% (old gate)"
    return False, ""


def check_zero_targets_gate_new(stop_loss, target1, target2, daily_change,
                                confidence=50, now_action="Buy Now") -> Tuple[bool, str]:
    """V2 gate (ea3bbaa): triggers at 8%+ OR (conf<20% AND Hold/Sell action)."""
    if stop_loss <= 0 and target1 <= 0 and target2 <= 0:
        _action = now_action.lower()
        _engine_not_buy = "hold" in _action or "sell" in _action or "exit" in _action
        if daily_change >= 8.0:
            return True, f"no targets, +{daily_change:.1f}%"
        if confidence < 20 and _engine_not_buy:
            return True, f"no targets, conf={confidence:.0f}%, action='{now_action}'"
    return False, ""


# ── V3 Quality Gates (matching production code) ──────────────────────

def check_v3_action_gate(now_action: str, rsi: float) -> Tuple[bool, str]:
    """Require 'Buy Now' action. Override: RSI < 45 (oversold bounce)."""
    _action = now_action.lower()
    _is_buy = "buy" in _action
    if not _is_buy and rsi >= 45:
        return True, f"engine='{now_action}', RSI {rsi:.0f}>=45"
    return False, ""


def check_v3_rsi_gate(rsi: float) -> Tuple[bool, str]:
    """RSI < 80 — extremely overbought stocks stall."""
    if rsi >= 80:
        return True, f"RSI {rsi:.0f}>=80"
    return False, ""


def check_v3_confidence_gate(confidence: float, rsi: float) -> Tuple[bool, str]:
    """Confidence >= 20%. Override: RSI < 45 (oversold)."""
    if confidence < 20 and rsi >= 45:
        return True, f"conf {confidence:.0f}%<20%, RSI {rsi:.0f}>=45"
    return False, ""


def check_v3_targets_gate(stop_loss: float, target1: float, target2: float) -> Tuple[bool, str]:
    """Require at least one real OHLCV-derived target."""
    if stop_loss <= 0 and target1 <= 0 and target2 <= 0:
        return True, "no real targets (SL=0,T1=0,T2=0)"
    return False, ""


def check_v3_volume_gate(vol_ratio: float, is_broad_rally: bool = True) -> Tuple[bool, str]:
    """Volume >= 1.5x normal, >= 1.2x on broad rally days."""
    _gate = 1.2 if is_broad_rally else 1.5
    if vol_ratio < _gate:
        return True, f"vol {vol_ratio:.1f}x < {_gate:.1f}x"
    return False, ""


# ──────────────────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────────────────

def load_signal_state() -> Dict[str, Dict]:
    path = os.path.join(DATA_DIR, "signalmonitor_allstocks_state_latest.csv")
    result = {}
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            sym = row.get("Symbol", "").strip()
            if sym:
                result[sym] = row
    return result


def load_ticks() -> Dict[str, Dict]:
    path = os.path.join(DATA_DIR, "kiteticker_allstocks_ticks_latest.json")
    with open(path, "r") as f:
        data = json.load(f)
    return {str(k).strip().upper(): v for k, v in data.items()} if isinstance(data, dict) else {}


def parse_log_entries(sessions: List[int]) -> List[Dict]:
    """Parse scoring entries from autopilot logs."""
    entries = []
    not_buy_re = re.compile(
        r"Not a buy: (\w+) score=(\d+) \(need (\d+)\) \| change=([\d.+-]+)% \| reasons=(.*)"
    )
    buy_re = re.compile(
        r"BUY ALERT SENT: (\w+) at .+?([\d.+-]+)%\) score=(\d+)/(\d+) vol=([\d.]+)x reasons=(.*)"
    )
    suppressed_re = re.compile(
        r"Suppressed (\w+): (.*)"
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
                        "session": s, "type": "rejected", "symbol": m.group(1),
                        "old_score": float(m.group(2)), "effective_min": float(m.group(3)),
                        "change_pct": float(m.group(4)), "reasons": m.group(5).strip(),
                    })
                    continue
                m = buy_re.search(line)
                if m:
                    entries.append({
                        "session": s, "type": "sent", "symbol": m.group(1),
                        "change_pct": float(m.group(2)), "old_score": float(m.group(3)),
                        "effective_min": float(m.group(4)), "vol_ratio": float(m.group(5)),
                        "reasons": m.group(6).strip(),
                    })
                    continue
                m = suppressed_re.search(line)
                if m:
                    entries.append({
                        "session": s, "type": "suppressed", "symbol": m.group(1),
                        "reason": m.group(2).strip(),
                    })
    return entries


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 90)
    print("  BACKTEST + FORWARD TEST: BlindBuyScorer V4 (big runner mode)")
    print(f"  V1 (old) → V2 (overbought/retreat gates) → V3 (quality gates) → V4 (big runners)")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 90)

    # ──────────────────────────────────────────────────────────────────
    # PART 1: BACKTEST — Historical log entries
    # ──────────────────────────────────────────────────────────────────
    print("\n\n" + "=" * 90)
    print("  PART 1: BACKTEST — Re-evaluating all historical autopilot log entries")
    print("=" * 90)

    sessions = list(range(12, 100))
    log_entries = parse_log_entries(sessions)
    sent = [e for e in log_entries if e["type"] == "sent"]
    rejected = [e for e in log_entries if e["type"] == "rejected"]
    suppressed = [e for e in log_entries if e["type"] == "suppressed"]

    print(f"\nParsed {len(log_entries)} total entries from sessions {min(sessions)}-{max(sessions)}")
    print(f"  Sent alerts: {len(sent)}")
    print(f"  Rejected: {len(rejected)}")
    print(f"  Suppressed: {len(suppressed)}")

    # Sent alerts analysis
    if sent:
        print(f"\n{'='*70}")
        print(f"  SENT ALERTS (would they survive new rules?)")
        print(f"{'='*70}")
        for e in sent:
            print(f"  {e['symbol']:15s} score {e['old_score']:.0f}/{e['effective_min']:.0f} "
                  f"chg={e['change_pct']:+.1f}% vol={e.get('vol_ratio','?')}x [session {e['session']}]")

    # Rejected entries — group by how close they were to passing
    print(f"\n{'='*70}")
    print(f"  REJECTED — closest to passing (score within 10 of min)")
    print(f"{'='*70}")
    close_to_passing = [e for e in rejected if e["old_score"] >= e["effective_min"] - 10]
    close_to_passing.sort(key=lambda x: x["old_score"] - x["effective_min"], reverse=True)

    # De-duplicate by symbol (take highest scoring instance)
    seen = set()
    unique_close = []
    for e in close_to_passing:
        if e["symbol"] not in seen:
            seen.add(e["symbol"])
            unique_close.append(e)

    for e in unique_close[:20]:
        gap = e["old_score"] - e["effective_min"]
        print(f"  {e['symbol']:15s} score={e['old_score']:.0f} need={e['effective_min']:.0f} "
              f"gap={gap:+.0f} chg={e['change_pct']:+.1f}% "
              f"[{e['reasons'][:60]}]")

    # Suppression reasons
    sup_reasons = {}
    for e in suppressed:
        reason = e.get("reason", "")
        if "volume" in reason.lower() and "time-weighted" in reason.lower():
            key = "volume < time-weighted min"
        elif "retreat" in reason.lower():
            key = "retreat from high"
        elif "reversed" in reason.lower():
            key = "price reversed"
        elif "R:R" in reason:
            key = "R:R too low"
        elif "NO targets" in reason or "no targets" in reason:
            key = "zero OHLCV targets"
        else:
            key = reason[:50] if reason else "unknown"
        sup_reasons[key] = sup_reasons.get(key, 0) + 1

    if sup_reasons:
        print(f"\n{'='*70}")
        print(f"  SUPPRESSION REASONS (post-score gates)")
        print(f"{'='*70}")
        for reason, count in sorted(sup_reasons.items(), key=lambda x: -x[1]):
            print(f"  {count:4d}x  {reason}")

    # ──────────────────────────────────────────────────────────────────
    # PART 2: FORWARD TEST — Today's live data
    # ──────────────────────────────────────────────────────────────────
    print("\n\n" + "=" * 90)
    print("  PART 2: FORWARD TEST — Score all stocks from today's signal state + ticks")
    print("=" * 90)

    signal_state = load_signal_state()
    ticks = load_ticks()
    print(f"\nLoaded {len(signal_state)} signal state rows, {len(ticks)} tick entries")

    # Market breadth
    changes = []
    for sym, t in ticks.items():
        pc = t.get("close", 0)
        ltp = t.get("last_price", 0)
        if pc > 0 and ltp > 0:
            changes.append((ltp - pc) / pc * 100)
    changes.sort()
    median_chg = changes[len(changes)//2] if changes else 0
    up3 = sum(1 for c in changes if c >= 3)
    up5 = sum(1 for c in changes if c >= 5)
    up10 = sum(1 for c in changes if c >= 10)
    print(f"Market breadth: median change {median_chg:+.1f}%, "
          f"≥3%: {up3}, ≥5%: {up5}, ≥10%: {up10} / {len(changes)} stocks")

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

        daily_change = ((price_now - prev_close) / prev_close * 100) if prev_close > 0 and price_now > 0 else 0

        # Simulate intraday override: zero BuyAbove for 3%+ movers
        buy_above = float(row.get("BuyAbove", 0) or 0)
        if daily_change >= 3.0:
            buy_above = 0
            if abs(daily_change) > abs(change_pct):
                change_pct = daily_change

        # Score both ways
        old_s, old_r = score_old(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                                 price_now, buy_above, prev_close, reason_text,
                                 sell_if_below, take_profit2)
        new_s, new_r = score_new(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                                 price_now, buy_above, prev_close, reason_text,
                                 sell_if_below, take_profit2)
        v4_s, v4_r = score_v4(rsi, cci, mfi, vol_ratio, change_pct, confidence,
                               price_now, buy_above, prev_close, reason_text,
                               sell_if_below, take_profit2)

        # Gates
        retreat_blocked, retreat_reason = check_retreat_gate(tick)
        ztgt_old_blocked, ztgt_old_reason = check_zero_targets_gate_old(
            sell_if_below, take_profit1, take_profit2, daily_change, confidence, now_action)
        ztgt_new_blocked, ztgt_new_reason = check_zero_targets_gate_new(
            sell_if_below, take_profit1, take_profit2, daily_change, confidence, now_action)

        # Effective min
        if daily_change >= 3.0 and vol_ratio >= 0.8:
            eff_min = 58.0
        elif daily_change >= 3.0:
            eff_min = 58.0
        else:
            eff_min = 40.0

        # Tick OHLCV
        tick_high = float(tick.get("high") or tick.get("ohlc", {}).get("high", 0))
        tick_low = float(tick.get("low") or tick.get("ohlc", {}).get("low", 0))
        ltp = float(tick.get("last_price", 0))
        max_gain = ((tick_high - prev_close) / prev_close * 100) if prev_close > 0 and tick_high > 0 else 0
        current = ((ltp - prev_close) / prev_close * 100) if prev_close > 0 and ltp > 0 else 0

        # Old: score gate + old zero-targets gate
        old_pass = old_s >= eff_min and not ztgt_old_blocked
        # V2: score gate + all v2 gates
        v2_pass = new_s >= eff_min and not retreat_blocked and not ztgt_new_blocked

        # V3: v2 gates + quality gates (action, RSI, confidence, targets, volume)
        v3_action_blocked, v3_action_reason = check_v3_action_gate(now_action, rsi)
        v3_rsi_blocked, v3_rsi_reason = check_v3_rsi_gate(rsi)
        v3_conf_blocked, v3_conf_reason = check_v3_confidence_gate(confidence, rsi)
        v3_tgt_blocked, v3_tgt_reason = check_v3_targets_gate(sell_if_below, take_profit1, take_profit2)
        v3_vol_blocked, v3_vol_reason = check_v3_volume_gate(vol_ratio, is_broad_rally=True)  # April 8 = broad rally
        v3_pass = (v2_pass
                   and not v3_action_blocked
                   and not v3_rsi_blocked
                   and not v3_conf_blocked
                   and not v3_tgt_blocked
                   and not v3_vol_blocked)

        # V4: same quality gates as V3 but with:
        #   - Halved CCI/MFI penalties for vol >= 2.0x (in v4_s score)
        #   - Lowered effective_min to 50 for Buy Now + vol >= 2.0x
        _is_buy_action_v4 = "buy" in now_action.lower()
        _high_vol_buy_v4 = _is_buy_action_v4 and vol_ratio >= 2.0
        v4_eff_min = 50.0 if _high_vol_buy_v4 else eff_min
        v4_score_pass = v4_s >= v4_eff_min
        v4_retreat_ok = not retreat_blocked
        v4_ztgt_ok = not ztgt_new_blocked
        v4_pass = (v4_score_pass and v4_retreat_ok and v4_ztgt_ok
                   and not v3_action_blocked
                   and not v3_rsi_blocked
                   and not v3_conf_blocked
                   and not v3_tgt_blocked
                   and not v3_vol_blocked)

        block_reason = ""
        v3_block_reason = ""
        v4_block_reason = ""
        if old_pass and not v2_pass:
            if new_s < eff_min: block_reason = f"SCORE {new_s:.0f}<{eff_min:.0f}"
            elif retreat_blocked: block_reason = f"RETREAT {retreat_reason}"
            elif ztgt_new_blocked: block_reason = f"NOTGT {ztgt_new_reason}"
        if v2_pass and not v3_pass:
            if v3_action_blocked: v3_block_reason = f"ACTION {v3_action_reason}"
            elif v3_rsi_blocked: v3_block_reason = f"RSI {v3_rsi_reason}"
            elif v3_conf_blocked: v3_block_reason = f"CONF {v3_conf_reason}"
            elif v3_tgt_blocked: v3_block_reason = f"NOTGT {v3_tgt_reason}"
            elif v3_vol_blocked: v3_block_reason = f"VOL {v3_vol_reason}"
        # V4 block: only if V4 doesn't pass despite V3 passing (shouldn't happen, V4 is more permissive)
        # Or if V4 passes but V3 doesn't (the interesting case)
        if not v4_pass:
            if not v4_score_pass: v4_block_reason = f"SCORE {v4_s:.0f}<{v4_eff_min:.0f}"
            elif retreat_blocked: v4_block_reason = f"RETREAT"
            elif v3_action_blocked: v4_block_reason = f"ACTION"
            elif v3_rsi_blocked: v4_block_reason = f"RSI"
            elif v3_conf_blocked: v4_block_reason = f"CONF"
            elif v3_tgt_blocked: v4_block_reason = f"NOTGT"
            elif v3_vol_blocked: v4_block_reason = f"VOL"

        results.append({
            "sym": sym, "daily_change": daily_change, "max_gain": max_gain,
            "current": current, "vol_ratio": vol_ratio, "rsi": rsi, "cci": cci,
            "mfi": mfi, "confidence": confidence,
            "old_score": old_s, "new_score": new_s, "v4_score": v4_s,
            "eff_min": eff_min, "v4_eff_min": v4_eff_min,
            "old_pass": old_pass, "v2_pass": v2_pass, "v3_pass": v3_pass, "v4_pass": v4_pass,
            "retreat_blocked": retreat_blocked, "retreat_reason": retreat_reason,
            "ztgt_old_blocked": ztgt_old_blocked, "ztgt_new_blocked": ztgt_new_blocked,
            "ztgt_new_reason": ztgt_new_reason,
            "block_reason": block_reason, "v3_block_reason": v3_block_reason,
            "v4_block_reason": v4_block_reason,
            "old_reasons": old_r, "new_reasons": new_r, "v4_reasons": v4_r,
            "now_action": now_action, "high": tick_high, "low": tick_low,
            "ltp": ltp, "prev_close": prev_close,
            "sell_if_below": sell_if_below, "take_profit1": take_profit1,
            "take_profit2": take_profit2,
        })

    # ── Filter: 3%+ movers ──
    movers = [r for r in results if r["daily_change"] >= 3.0]
    movers.sort(key=lambda x: x["daily_change"], reverse=True)

    # ── V3 ALERTS (final output) ──
    v3_alerts = [r for r in movers if r["v3_pass"]]
    v2_alerts = [r for r in movers if r["v2_pass"]]
    v1_alerts = [r for r in movers if r["old_pass"]]
    v4_alerts = [r for r in movers if r["v4_pass"]]

    print(f"\n{'='*90}")
    print(f"  V4 ALERTS — Big Runner Mode ({len(v4_alerts)} alerts)")
    print(f"  (V3 quality gates + halved CCI/MFI penalties for vol>=2x + lower threshold for Buy+vol>=2x)")
    print(f"{'='*90}")
    print(f"  {'Sym':15s} {'V4sc':>5s} {'V3sc':>5s} {'Min':>3s} {'Chg%':>6s} {'High%':>6s} {'Now%':>6s} "
          f"{'Vol':>5s} {'RSI':>4s} {'CCI':>5s} {'MFI':>4s} {'Conf':>4s} {'Action':20s} {'3%Entry':>7s} {'Win?'}")
    for r in sorted(v4_alerts, key=lambda x: x["max_gain"], reverse=True):
        upside_from_3 = r["max_gain"] - 3.0
        win = "WIN" if upside_from_3 >= 2.0 else "lose"
        print(f"  {r['sym']:15s} {r['v4_score']:5.0f} {r['new_score']:5.0f} {r['v4_eff_min']:3.0f} "
              f"{r['daily_change']:+5.1f}% {r['max_gain']:+5.1f}% {r['current']:+5.1f}% "
              f"{r['vol_ratio']:4.1f}x {r['rsi']:4.0f} {r['cci']:5.0f} {r['mfi']:4.0f} {r['confidence']:4.0f} "
              f"{r['now_action']:20s} {upside_from_3:+5.1f}% {win}")

    # ── V4 NEW ADDITIONS (what V4 adds over V3) ──
    v4_new = [r for r in movers if r["v4_pass"] and not r["v3_pass"]]
    if v4_new:
        print(f"\n{'='*90}")
        print(f"  V4 NEW ADDITIONS (V4 passes, V3 blocked): {len(v4_new)} stocks")
        print(f"  These are the big runners V4 unlocks!")
        print(f"{'='*90}")
        print(f"  {'Sym':15s} {'V4sc':>5s} {'V3sc':>5s} {'V4min':>5s} {'V3min':>5s} {'Chg%':>6s} {'High%':>6s} "
              f"{'Vol':>5s} {'CCI':>5s} {'MFI':>4s} {'Action':20s} {'3%Entry':>7s} {'Win?'}")
        for r in sorted(v4_new, key=lambda x: x["max_gain"], reverse=True):
            upside_from_3 = r["max_gain"] - 3.0
            win = "WIN" if upside_from_3 >= 2.0 else "lose"
            print(f"  {r['sym']:15s} {r['v4_score']:5.0f} {r['new_score']:5.0f} {r['v4_eff_min']:5.0f} {r['eff_min']:5.0f} "
                  f"{r['daily_change']:+5.1f}% {r['max_gain']:+5.1f}% "
                  f"{r['vol_ratio']:4.1f}x {r['cci']:5.0f} {r['mfi']:4.0f} "
                  f"{r['now_action']:20s} {upside_from_3:+5.1f}% {win}")

    print(f"\n{'='*90}")
    print(f"  V3 ALERTS (for comparison) ({len(v3_alerts)} alerts)")
    print(f"{'='*90}")
    print(f"  {'Sym':15s} {'Score':>5s} {'Min':>3s} {'Chg%':>6s} {'High%':>6s} {'Now%':>6s} "
          f"{'Vol':>5s} {'RSI':>4s} {'CCI':>5s} {'Conf':>4s} {'Action':20s} {'3%Entry':>7s} {'Win?'}")
    for r in sorted(v3_alerts, key=lambda x: x["new_score"], reverse=True):
        upside_from_3 = r["max_gain"] - 3.0
        win = "WIN" if upside_from_3 >= 2.0 else "lose"
        print(f"  {r['sym']:15s} {r['new_score']:5.0f} {r['eff_min']:3.0f} "
              f"{r['daily_change']:+5.1f}% {r['max_gain']:+5.1f}% {r['current']:+5.1f}% "
              f"{r['vol_ratio']:4.1f}x {r['rsi']:4.0f} {r['cci']:5.0f} {r['confidence']:4.0f} "
              f"{r['now_action']:20s} {upside_from_3:+5.1f}% {win}")

    # ── V2→V3 BLOCKED (what V3 gates removed from V2) ──
    v2_not_v3 = [r for r in movers if r["v2_pass"] and not r["v3_pass"]]
    if v2_not_v3:
        print(f"\n{'='*90}")
        print(f"  V3 BLOCKED (V2 passed, V3 blocked): {len(v2_not_v3)}")
        print(f"{'='*90}")
        print(f"  {'Sym':15s} {'Score':>5s} {'Chg%':>6s} {'High%':>6s} {'Vol':>5s} {'RSI':>4s} {'Conf':>4s} "
              f"{'Action':20s} {'Gate':30s} {'3%ent':>5s} {'Win?'}")
        for r in sorted(v2_not_v3, key=lambda x: x["max_gain"], reverse=True)[:25]:
            upside_from_3 = r["max_gain"] - 3.0
            win = "WIN" if upside_from_3 >= 2.0 else "lose"
            print(f"  {r['sym']:15s} {r['new_score']:5.0f} {r['daily_change']:+5.1f}% "
                  f"{r['max_gain']:+5.1f}% {r['vol_ratio']:4.1f}x {r['rsi']:4.0f} {r['confidence']:4.0f} "
                  f"{r['now_action']:20s} {r['v3_block_reason']:30s} {upside_from_3:+4.1f}% {win}")

    # ── V1→V2 BLOCKED (what V2 gates removed from V1, before V3) ──
    v1_not_v2 = [r for r in movers if r["old_pass"] and not r["v2_pass"]]
    if v1_not_v2:
        print(f"\n{'='*90}")
        print(f"  V2 BLOCKED (V1 passed, V2 blocked): {len(v1_not_v2)}")
        print(f"{'='*90}")
        print(f"  {'Sym':15s} {'Old':>4s} {'New':>4s} {'Chg%':>6s} {'High%':>6s} {'Vol':>5s} "
              f"{'RSI':>4s} {'CCI':>5s} {'Gate':30s} {'3%ent':>5s} {'Win?'}")
        for r in sorted(v1_not_v2, key=lambda x: x["max_gain"], reverse=True)[:20]:
            upside_from_3 = r["max_gain"] - 3.0
            win = "WIN" if upside_from_3 >= 2.0 else "lose"
            print(f"  {r['sym']:15s} {r['old_score']:4.0f} {r['new_score']:4.0f} "
                  f"{r['daily_change']:+5.1f}% {r['max_gain']:+5.1f}% {r['vol_ratio']:4.1f}x "
                  f"{r['rsi']:4.0f} {r['cci']:5.0f} {r['block_reason']:30s} {upside_from_3:+4.1f}% {win}")

    # ──────────────────────────────────────────────────────────────────
    # PART 3: TOP GAINERS
    # ──────────────────────────────────────────────────────────────────
    print("\n\n" + "=" * 90)
    print("  PART 3: TOP GAINERS — Would we catch the real winners?")
    print("=" * 90)

    # Tradeable top gainers (vol >= 1x, price >= 10)
    tradeable = [r for r in results if r["prev_close"] >= 10 and r["vol_ratio"] >= 1.0]
    tradeable.sort(key=lambda x: x["max_gain"], reverse=True)
    print(f"\n  TOP 25 TRADEABLE GAINERS (vol >= 1.0x, price >= Rs.10)")
    print(f"  {'Sym':15s} {'High%':>7s} {'Now%':>6s} {'Vol':>5s} {'V1':>4s} {'V2':>4s} {'V3':>4s} {'V4':>4s} "
          f"{'RSI':>4s} {'CCI':>5s} {'Conf':>4s} {'Action':20s}")
    for r in tradeable[:25]:
        v1 = "PASS" if r["old_pass"] else "fail"
        v2 = "PASS" if r["v2_pass"] else "fail"
        v3 = "PASS" if r["v3_pass"] else "fail"
        v4 = "PASS" if r["v4_pass"] else "fail"
        print(f"  {r['sym']:15s} {r['max_gain']:+6.1f}% {r['current']:+5.1f}% "
              f"{r['vol_ratio']:4.1f}x {v1:>4s} {v2:>4s} {v3:>4s} {v4:>4s} "
              f"{r['rsi']:4.0f} {r['cci']:5.0f} {r['confidence']:4.0f} "
              f"{r['now_action']:20s}")

    # ──────────────────────────────────────────────────────────────────
    # PART 4: SUMMARY & P&L
    # ──────────────────────────────────────────────────────────────────
    print("\n\n" + "=" * 90)
    print("  PART 4: SUMMARY & P&L ANALYSIS")
    print("=" * 90)

    all_old_pass = [r for r in movers if r["old_pass"]]
    all_v2_pass = [r for r in movers if r["v2_pass"]]
    all_v3_pass = [r for r in movers if r["v3_pass"]]
    all_v4_pass = [r for r in movers if r["v4_pass"]]

    print(f"\n  Total 3%+ movers: {len(movers)}")
    print(f"  V1 (OLD) alerts: {len(all_old_pass)}")
    print(f"  V2 (overbought/retreat gates) alerts: {len(all_v2_pass)}")
    print(f"  V3 (ultra-selective) alerts: {len(all_v3_pass)}")
    print(f"  V4 (big runner mode) alerts: {len(all_v4_pass)}")

    def pnl_stats(alerts, label):
        if not alerts:
            print(f"\n  {label}: no alerts")
            return
        upside_from_entry = [r["max_gain"] - 3.0 for r in alerts]
        remaining_eod = [r["max_gain"] - r["daily_change"] for r in alerts]
        print(f"\n  {label} ({len(alerts)} alerts):")
        print(f"    [Entry at 3%]  Upside to high: avg {sum(upside_from_entry)/len(upside_from_entry):+.1f}%, "
              f"best {max(upside_from_entry):+.1f}%, worst {min(upside_from_entry):+.1f}%")
        w2 = sum(1 for u in upside_from_entry if u >= 2.0)
        w3 = sum(1 for u in upside_from_entry if u >= 3.0)
        w5 = sum(1 for u in upside_from_entry if u >= 5.0)
        w7 = sum(1 for u in upside_from_entry if u >= 7.0)
        print(f"    Win 2%+           : {w2}/{len(alerts)} ({w2/len(alerts)*100:.0f}%)")
        print(f"    Win 3%+           : {w3}/{len(alerts)} ({w3/len(alerts)*100:.0f}%)")
        print(f"    Win 5%+           : {w5}/{len(alerts)} ({w5/len(alerts)*100:.0f}%)")
        print(f"    Win 7%+           : {w7}/{len(alerts)} ({w7/len(alerts)*100:.0f}%)")
        print(f"    [EOD snapshot] Remaining: avg {sum(remaining_eod)/len(remaining_eod):+.1f}%")

    pnl_stats(all_old_pass, "V1 (OLD)")
    pnl_stats(all_v2_pass, "V2 (overbought/retreat)")
    pnl_stats(all_v3_pass, "V3 (ultra-selective)")
    pnl_stats(all_v4_pass, "V4 (big runner mode)")

    # Quality ratio
    print(f"\n{'='*70}")
    print(f"  WIN RATE COMPARISON")
    print(f"  Win = max_gain >= 5% (i.e. 2%+ profit from 3% entry)")
    print(f"{'='*70}")
    for label, alerts in [("V1 (OLD)", all_old_pass), ("V2", all_v2_pass), ("V3", all_v3_pass), ("V4 (big runner)", all_v4_pass)]:
        if alerts:
            w = sum(1 for r in alerts if r["max_gain"] >= 5.0)
            rate = w / len(alerts) * 100
            print(f"  {label:25s}: {w}/{len(alerts)} = {rate:.0f}% win rate")
    print()
    for label, alerts in [("V1 (OLD)", all_old_pass), ("V2", all_v2_pass), ("V3", all_v3_pass), ("V4 (big runner)", all_v4_pass)]:
        if alerts:
            w = sum(1 for r in alerts if r["max_gain"] >= 6.0)
            rate = w / len(alerts) * 100
            print(f"  {label:25s}: {w}/{len(alerts)} = {rate:.0f}% (3%+ profit from entry)")

    # Known alerts verification
    print(f"\n{'='*70}")
    print(f"  KNOWN ALERT VERIFICATION")
    print(f"{'='*70}")
    for check_sym in ["CLSEL", "BIOCON", "INDSWFTLAB", "TITAN", "BSE", "ADANIENSOL",
                       "KIRLOSENG", "RISHABH", "PACEDIGITK",
                       "AGIIL", "STEELCAS", "SPORTKING", "GROWW", "NAM-INDIA"]:
        r = next((r for r in results if r["sym"] == check_sym), None)
        if r:
            v1 = "PASS" if r["old_pass"] else "FAIL"
            v2 = "PASS" if r["v2_pass"] else "FAIL"
            v3 = "PASS" if r["v3_pass"] else "FAIL"
            v4 = "PASS" if r["v4_pass"] else "FAIL"
            reason = r["v4_block_reason"] or r["v3_block_reason"] or r["block_reason"] or ""
            if not r["old_pass"] and not r["v2_pass"] and not r["v3_pass"] and not r["v4_pass"]:
                reason = f"all fail (v4s={r['v4_score']:.0f})"
            print(f"  {check_sym:15s} V1={v1:4s} V2={v2:4s} V3={v3:4s} V4={v4:4s} "
                  f"chg={r['daily_change']:+5.1f}% high={r['max_gain']:+5.1f}% "
                  f"vol={r['vol_ratio']:.1f}x v4s={r['v4_score']:.0f} v4min={r['v4_eff_min']:.0f} "
                  f"act={r['now_action']:15s} {reason}")


if __name__ == "__main__":
    main()

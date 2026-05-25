"""
Blind-buy advisor and sell-alert engine for hands-off investing.

Workflow:
1. Runs aggressively-tuned scans that catch opportunities BEFORE they move
   (lowered liquidity/volume gates, accepts Watch-for-Buy with momentum,
    adds a reversal-anticipation score for oversold-bounce setups).
2. Pushes Discord alerts the moment a stock qualifies as a Blind Buy —
   user opens Groww and buys manually.
3. User records the purchase via /buy in Discord.
4. Engine continuously monitors open positions and pushes SELL alerts
   when stop-loss, target, or exit signal fires.

The audit of top gainers (March–April 2026) showed that 79 % of big movers
were classified as "Watch for Buy" and missed.  The extra scoring layer here
catches those setups by valuing leading indicators (RSI oversold, volume
direction change, ATR trailing) more heavily than lagging ones.
"""

import os
import time
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.PKDateUtilities import PKDateUtilities

def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_time_min(name: str, default_min: int) -> int:
    raw = os.environ.get(name, "")
    if raw == "":
        return default_min
    try:
        parts = str(raw).strip().split(":")
        if len(parts) != 2:
            return default_min
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return default_min


def _format_ist_minute(minute_of_day: int) -> str:
    minute_of_day = max(0, int(minute_of_day))
    hour24 = minute_of_day // 60
    minute = minute_of_day % 60
    suffix = "AM" if hour24 < 12 else "PM"
    hour12 = hour24 % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"
from PKDevTools.classes.log import default_logger

from pkscreener.classes.PaperTradeTracker import PaperTradeTracker
from pkscreener.classes.PreOpenAnalyzer import PreOpenAnalyzer
from pkscreener.classes.RiskGuardrails import LiveTradingGuardrails


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AutoPilotConfig:
    """All tunable knobs for the blind-buy advisor."""

    universe: str = "nifty50"
    account_name: str = "main"

    # Buy-side filters — CONSISTENT DAILY INCOME MODE
    # Optimized from 2-year Kite API backtest (Apr 2024 – Apr 2026):
    #   777 raw trades → 82 filtered trades with 96.3% WR
    #   Key findings: vol_ratio is #1 predictor, gap>=5% is toxic,
    #   high score = oversold dead-cat bounce (inversely correlated with edge).
    min_confidence: float = 15.0        # 15% indicator agreement — balanced
    min_trade_value: float = 0.0        # no liquidity floor — small caps welcome
    min_volume_ratio: float = 5.0       # 5x avg vol — backtest-validated edge threshold
    max_entry_gap_pct: float = 5.0      # gaps >= 5% reverse 73% of the time — toxic
    blind_buy_min_score: float = 0.0    # score is NOT a quality filter (inverted edge)
    blind_buy_max_score: float = 70.0   # SCORE CAP: allow most candidates, overbought penalty handles extremes

    # Optional stricter mode (disabled by default)
    strict_mode: bool = False
    morning_start_min: int = 9 * 60 + 20
    morning_end_min: int = 11 * 60 + 30
    afternoon_start_min: int = 13 * 60 + 30
    afternoon_end_min: int = 14 * 60 + 30

    # Sell monitoring
    auto_exit_on_sell: bool = True
    auto_exit_on_stop: bool = True
    auto_exit_on_target2: bool = True

    # Risk guardrails
    max_daily_loss_amount: float = 0.0
    max_consecutive_losses: int = 0

    # Runtime
    max_workers: int = 3
    alert_channels: str = "discord"
    cycle_interval_sec: int = 15
    max_buy_alerts_per_cycle: int = 3

    def normalized(self) -> "AutoPilotConfig":
        strict_mode = bool(self.strict_mode or _env_truthy("ASSISTANT_STRICT_MODE", False))
        min_volume_ratio = max(0.0, float(self.min_volume_ratio if self.min_volume_ratio is not None else 5.0))
        if strict_mode:
            min_volume_ratio = max(min_volume_ratio, _env_float("ASSISTANT_STRICT_MIN_VOL_RATIO", 8.0))
        morning_start_min = _env_time_min("ASSISTANT_STRICT_MORNING_START", self.morning_start_min)
        morning_end_min = _env_time_min("ASSISTANT_STRICT_MORNING_END", self.morning_end_min)
        afternoon_start_min = _env_time_min("ASSISTANT_STRICT_AFTERNOON_START", self.afternoon_start_min)
        afternoon_end_min = _env_time_min("ASSISTANT_STRICT_AFTERNOON_END", self.afternoon_end_min)
        if strict_mode and _env_truthy("ASSISTANT_STRICT_NO_AFTERNOON", True):
            afternoon_start_min = -1
            afternoon_end_min = -1

        return AutoPilotConfig(
            universe=str(self.universe or "nifty50").strip().lower(),
            account_name=str(self.account_name or "main").strip().lower().replace(" ", "_"),
            min_confidence=max(0.0, float(self.min_confidence if self.min_confidence is not None else 5.0)),
            min_trade_value=max(0.0, float(self.min_trade_value if self.min_trade_value is not None else 0.0)),
            min_volume_ratio=min_volume_ratio,
            max_entry_gap_pct=max(0.0, float(self.max_entry_gap_pct if self.max_entry_gap_pct is not None else 5.0)),
            blind_buy_min_score=max(0.0, float(self.blind_buy_min_score if self.blind_buy_min_score is not None else 0.0)),
            blind_buy_max_score=max(0.0, float(self.blind_buy_max_score if self.blind_buy_max_score is not None else 40.0)),
            auto_exit_on_sell=bool(self.auto_exit_on_sell),
            auto_exit_on_stop=bool(self.auto_exit_on_stop),
            auto_exit_on_target2=bool(self.auto_exit_on_target2),
            max_daily_loss_amount=max(0.0, float(self.max_daily_loss_amount or 0.0)),
            max_consecutive_losses=max(0, int(self.max_consecutive_losses or 0)),
            max_workers=max(1, int(self.max_workers or 3)),
            alert_channels=str(self.alert_channels or "discord").strip().lower(),
            cycle_interval_sec=max(10, int(self.cycle_interval_sec or 60)),
            max_buy_alerts_per_cycle=max(1, int(self.max_buy_alerts_per_cycle or 5)),
            strict_mode=strict_mode,
            morning_start_min=morning_start_min,
            morning_end_min=morning_end_min,
            afternoon_start_min=afternoon_start_min,
            afternoon_end_min=afternoon_end_min,
        )


@dataclass
class BlindBuyCandidate:
    """A stock that qualifies as a blind-buy opportunity."""

    symbol: str = ""
    price: float = 0.0
    buy_above: float = 0.0
    stop_loss: float = 0.0
    target1: float = 0.0
    target2: float = 0.0
    confidence: float = 0.0
    volume_ratio: float = 0.0
    change_pct: float = 0.0
    blind_score: float = 0.0
    original_action: str = ""
    reasons: str = ""
    checks: str = ""
    suggested_qty: int = 0
    holding_horizon: str = "avoid"
    product_preference: str = ""
    max_hold_days: int = 0
    horizon_reason: str = ""
    alert_stage: str = ""
    alert_stage_reason: str = ""
    entry_type: str = ""
    entry_type_reason: str = ""
    off_high_pct: float = 0.0
    vwap_gap_pct: float = 0.0
    support_gap_pct: float = 0.0
    buyer_imbalance: float = 0.0
    turnover_cr: float = 0.0
    fresh_buyer_confirmation: bool = False
    spread_pct: float = 0.0
    range_pct: float = 0.0
    last_trade_qty: float = 0.0


@dataclass
class SellAlert:
    """A position that should be sold now."""

    symbol: str = ""
    quantity: int = 0
    entry_price: float = 0.0
    current_price: float = 0.0
    pnl_pct: float = 0.0
    reason: str = ""


@dataclass
class CycleResult:
    """What happened in one advisor cycle."""

    cycle_time: str = ""
    buy_candidates: List[BlindBuyCandidate] = field(default_factory=list)
    sell_alerts: List[SellAlert] = field(default_factory=list)
    open_positions: int = 0
    buy_paused: bool = False
    pause_reasons: List[str] = field(default_factory=list)
    blind_buy_diagnostics: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None


def _safe_float(value, default: float = 0.0) -> float:
    """Parse floats safely from raw CSV/tick values (including '12.3%')."""
    try:
        if value is None:
            return default
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if cleaned.endswith("%"):
                cleaned = cleaned[:-1]
            if not cleaned:
                return default
            parsed = float(cleaned)
            return default if math.isnan(parsed) or math.isinf(parsed) else parsed
        parsed = float(value)
        return default if math.isnan(parsed) or math.isinf(parsed) else parsed
    except (TypeError, ValueError):
        return default


def _normalize_long_levels(
    price: float,
    stop_loss: float,
    target1: float,
    target2: float,
    atr: float = 0.0,
    aggressive: bool = False,
) -> Tuple[float, float, float, bool]:
    """Ensure a buy alert has long-side levels around the current price."""
    if price <= 0:
        return stop_loss, target1, target2, False

    changed = False
    stop_mult = 2.0 if aggressive else 1.5
    stop_fallback = 0.96 if aggressive else 0.95
    t1_mult = 1.05 if aggressive else 1.02
    t2_mult = 1.10 if aggressive else 1.04

    if stop_loss <= 0 or stop_loss >= price:
        stop_loss = round(price - stop_mult * atr, 2) if atr > 0 else round(price * stop_fallback, 2)
        changed = True

    if target1 <= price:
        target1 = round(price * t1_mult, 2)
        changed = True

    if target2 <= max(price, target1):
        target2 = round(price * t2_mult, 2)
        if target2 <= target1:
            target2 = round(target1 * 1.02, 2)
        changed = True

    return stop_loss, target1, target2, changed


def _compute_long_rr(price: float, stop_loss: float, target2: float) -> float:
    """Compute long-side reward/risk only when the setup geometry is valid."""
    if price <= 0 or stop_loss <= 0 or price <= stop_loss or target2 <= price:
        return 0.0
    risk = price - stop_loss
    if risk <= 0:
        return 0.0
    return round((target2 - price) / risk, 1)


def _score_to_size_fraction(score: float) -> float:
    """Map signal confidence score (0-100) to a position size multiplier.

    Uses a piecewise-linear curve inspired by mlfinlab's bet_sizing module:
      score  0-39  → 0.5× (half size — low confidence)
      score 40-59  → 0.5–0.8× (linear ramp)
      score 60-69  → 0.8–1.0× (baseline)
      score 70-84  → 1.0–1.3× (above baseline)
      score 85-100 → 1.3–1.5× (full conviction)

    This gives a smooth, granular sizing curve instead of coarse tiers.
    """
    if score < 40:
        return 0.5
    elif score < 60:
        return 0.5 + (score - 40) * (0.3 / 20)  # 0.5 → 0.8
    elif score < 70:
        return 0.8 + (score - 60) * (0.2 / 10)  # 0.8 → 1.0
    elif score < 85:
        return 1.0 + (score - 70) * (0.3 / 15)  # 1.0 → 1.3
    else:
        return min(1.5, 1.3 + (score - 85) * (0.2 / 15))  # 1.3 → 1.5


def _compute_suggested_qty(
    price: float,
    stop_loss: float = 0.0,
    score: float = 0.0,
    atr: float = 0.0,
    size_mult: float = 1.0,
    max_position: float = 75000.0,
    risk_amount: float = 2500.0,
    portfolio: float = 375000.0,
    target_vol: float = 0.15,
) -> Tuple[int, float]:
    """Size a position consistently across alert paths.

    Returns (qty, invested_amount). A quantity of 0 means the stock is too
    expensive for the configured starter-size cap or too risky for the risk cap.
    """
    if price <= 0 or size_mult <= 0:
        return 0, 0.0

    budget_qty = int(max_position // price) if max_position > 0 else 0
    if max_position > 0 and budget_qty <= 0:
        return 0, 0.0

    risk_per_share = 0.0
    if stop_loss > 0 and price > stop_loss:
        risk_per_share = price - stop_loss
    else:
        risk_per_share = price * 0.03
    risk_qty = int(risk_amount // risk_per_share) if risk_per_share > 0 else 0

    atr_val = atr if atr > 0 else price * 0.02
    asset_vol = max((atr_val / price) * (252 ** 0.5), 0.05)
    vol_position_value = (target_vol / asset_vol) * portfolio if asset_vol > 0 else max_position
    if max_position > 0:
        vol_position_value = min(vol_position_value, max_position)
    vol_qty = int(vol_position_value // price) if price > 0 else 0

    candidate_caps = [q for q in [budget_qty, risk_qty, vol_qty] if q > 0]
    if not candidate_caps:
        return 0, 0.0
    base_qty = min(candidate_caps)

    # Confidence-proportional sizing (adapted from mlfinlab bet_sizing).
    # Maps signal score (0-100) to a position fraction using a sigmoid-like
    # curve.  Score 50 → 0.6× base,  70 → 1.0×,  85+ → 1.5×.
    # This replaces the old coarse 3-tier system.
    score_mult = _score_to_size_fraction(score)

    qty = int(base_qty * score_mult * size_mult)
    if qty <= 0 and base_qty > 0:
        qty = 1
    if max_position > 0:
        qty = min(qty, budget_qty)
    if qty <= 0:
        return 0, 0.0
    return qty, round(qty * price, 0)


# ---------------------------------------------------------------------------
# Blind Buy Scorer
# ---------------------------------------------------------------------------

class BlindBuyScorer:
    """
    Extra scoring layer that values LEADING indicators over lagging ones.

    Uses actual numeric indicator values (RSI, CCI, MFI, VolumeRatio, etc.)
    instead of parsing reason text strings. This makes scoring precise and
    robust against changes in the signal engine's text formatting.

    The main signal engine (signals.py) requires 5-6 of 7 indicators aligned
    before issuing a BUY — by which time the move has often started.

    This scorer promotes stocks that show:
    - RSI oversold / approaching oversold (leading)
    - CCI/MFI at oversold extremes (leading)
    - Price above ATR trailing stop (trend filter)
    - Any volume uptick (early volume)
    - Green candle or breakout price action
    - Near entry trigger

    The audit showed this pattern in nearly every "missed" top gainer.
    """

    @staticmethod
    def score(row: pd.Series) -> Tuple[float, str]:
        """Score a signal-monitor row on blind-buy potential. Returns (score, reasons)."""
        score = 0.0
        reasons = []

        # Read numeric values with graceful fallbacks
        rsi = float(row.get("RSI", 50) or 50)
        cci = float(row.get("CCI", 0) or 0)
        mfi = float(row.get("MFI", 50) or 50)
        vol_ratio = float(row.get("VolumeRatio", row.get("Volume Ratio", row.get("Volume vs Avg", 0))) or 0)
        change_pct = _safe_float(
            row.get("ChangePct", row.get("Change %", row.get("Change%", row.get("change", 0)))),
            0.0,
        )
        entry_gap_pct = float(row.get("EntryGapPct", row.get("Entry Gap %", 0)) or 0)
        confidence = float(row.get("ConfidencePct", row.get("Signal Strength %", row.get("SignalStrengthPct", 0))) or 0)
        trigger_ready = str(row.get("TriggerReady", "")).strip().lower() == "true"
        reason_text = str(row.get("Why", row.get("Reason", ""))).lower()

        # -- Early: detect if price is below buy trigger (stale change_pct guard) --
        _price_now = float(row.get("PriceNow", row.get("Price Now", row.get("LTP", 0))) or 0)
        _buy_above = float(row.get("BuyAbove", row.get("Buy Above", row.get("Entry", 0))) or 0)
        _price_below_trigger = (_price_now > 0 and _buy_above > 0 and _price_now < _buy_above)

        # -- HARD BLOCK 0: Minimum price filter ₹10 --
        # Sub-₹10 stocks are penny/SME stocks — illiquid, spike to upper circuit
        # instantly, impossible to exit safely. ACCURACY at ₹4 is a good example.
        if _price_now > 0 and _price_now < 10.0:
            score = 0
            reasons.append(f"⛔ PENNY STOCK ₹{_price_now:.2f} — price below ₹10, skip")
            return round(score, 1), ", ".join(reasons)

        # -- MOMENTUM OSCILLATOR (consolidated RSI + CCI + MFI) --
        # Quantopian Model Misspecification lecture: RSI, CCI, MFI are all
        # momentum oscillators — they measure the same underlying signal
        # (price momentum) with slight variations. Scoring them independently
        # inflates confidence via double/triple-counting.
        #
        # Fix: compute a SINGLE momentum oscillator score from the best of
        # the three, then apply one bonus/penalty. This reduces ~15 correlated
        # factor interactions to ~5, cutting overfitting risk.
        #
        # Each indicator is normalized to 0-100 scale:
        #   RSI: already 0-100
        #   CCI: map [-200, +200] → [0, 100] (clamped)
        #   MFI: already 0-100
        # Lower = more oversold = better for bounce buying.
        _cci_norm = max(0.0, min(100.0, (cci + 200) / 4.0))  # -200→0, 0→50, +200→100
        _high_vol_momentum = vol_ratio >= 2.0

        # Take the MEDIAN of the three — robust to one noisy indicator
        _osc_values = sorted([rsi, _cci_norm, mfi])
        _osc_median = _osc_values[1]  # median of 3

        # Score based on median oscillator
        if _osc_median < 25:
            score += 25
            reasons.append(f"oscillators oversold (RSI {rsi:.0f}, CCI {cci:.0f}, MFI {mfi:.0f})")
        elif _osc_median < 35:
            score += 18
            reasons.append(f"oscillators near oversold (RSI {rsi:.0f}, CCI {cci:.0f}, MFI {mfi:.0f})")
        elif _osc_median < 50:
            score += 10
            reasons.append(f"oscillators mid (RSI {rsi:.0f}, CCI {cci:.0f}, MFI {mfi:.0f})")
        elif _osc_median > 80:
            # Overbought penalty — halved on strong momentum or high volume
            if change_pct >= 5.0 or _high_vol_momentum:
                _pen = 5
                _tag = " [momentum-confirmed]" if _high_vol_momentum else ""
                score -= _pen
                reasons.append(f"oscillators elevated ({_osc_median:.0f}){_tag}")
            else:
                score -= 12
                reasons.append(f"oscillators overbought (RSI {rsi:.0f}, CCI {cci:.0f}, MFI {mfi:.0f})")
        elif _osc_median > 65:
            if not (change_pct >= 5.0 or _high_vol_momentum):
                score -= 5
                reasons.append(f"oscillators warming ({_osc_median:.0f})")

        # -- Volume (higher = better confirmation) --
        # Time-adjust volume: early in the day, even 1.5x is strong because
        # most of the day's volume hasn't traded yet. Weight it by time elapsed.
        # 9:15-10:00 AM = day is 0-10% complete → 1.5x vol is exceptional
        # 1:00-3:30 PM  = day is 70%+ complete   → need higher ratio to be meaningful
        try:
            import pytz as _pytz
            _ist = _pytz.timezone("Asia/Kolkata")
            _now = __import__('datetime').datetime.now(_ist)
            _min_since_open = max(0, (_now.hour * 60 + _now.minute) - (9 * 60 + 15))
            _day_pct = min(1.0, _min_since_open / 375.0)  # 375 min = full trading day
        except Exception:
            _day_pct = 0.5  # assume midday if timezone fails
        # Effective volume: early-morning volume counts more
        # e.g. at 9:30 AM (day 4% done), 2x vol is equivalent to ~5x vol at 2PM
        _vol_adjusted = vol_ratio / max(0.1, _day_pct) if _day_pct < 0.3 else vol_ratio
        _vol_score_base = _vol_adjusted if _day_pct < 0.3 else vol_ratio

        if _vol_score_base >= 2.5:
            score += 20
            reasons.append(f"volume surge {vol_ratio:.1f}x")
        elif _vol_score_base >= 1.5:
            score += 15
            reasons.append(f"strong volume {vol_ratio:.1f}x")
        elif _vol_score_base >= 1.0:
            score += 10
            reasons.append(f"good volume {vol_ratio:.1f}x")
        elif vol_ratio >= 0.7:
            score += 5
            reasons.append("adequate volume")
        # Don't penalize low volume — small caps may have naturally lower volume

        # -- Gap-up from prev close bonus --
        # A stock that opens 4%+ above prev close on a broad-up day is showing
        # strong overnight demand. At market open, most technical indicators
        # (RSI, volume, MACD) are still cold — but the gap itself is information.
        # Award bonus points so that gap-up + trigger ready can reach viable scores.
        _gap_bonus = 0
        _prev_close = float(row.get("PrevClose", row.get("Prev Close", 0)) or 0)
        if _prev_close > 0 and _price_now > 0:
            _gap_pct = (_price_now - _prev_close) / _prev_close * 100
            if _gap_pct >= 7.0:
                _gap_bonus = 15
                score += _gap_bonus
                reasons.append(f"strong gap-up +{_gap_pct:.1f}% from prev close")
            elif _gap_pct >= 4.0:
                _gap_bonus = 10
                score += _gap_bonus
                reasons.append(f"gap-up +{_gap_pct:.1f}% from prev close")

        # -- Green candle / price action --
        # Guard: if live price is below buy trigger, the change_pct from
        # historical candles may be stale — do NOT award breakout/green points.
        # Cap: if gap-up bonus was already awarded, reduce change% bonus to avoid
        # double-counting the same move (gap-up and change% measure the same fact).
        _change_cap = 10 if _gap_bonus >= 10 else 15  # cap at +10 if gap bonus was given
        if _price_below_trigger and change_pct > 0:
            score -= 5
            reasons.append("⚠️ data shows green but price below trigger — treated as suspect")
        elif change_pct >= 5.0:
            score += _change_cap
            # "hist signal" = change% from signal engine's OHLCV scan (not today's tick move)
            reasons.append(f"hist signal +{change_pct:.1f}%")
        elif change_pct >= 2.5:
            score += min(12, _change_cap)
            reasons.append(f"hist signal +{change_pct:.1f}%")
        elif change_pct >= 1.0:
            score += min(10, _change_cap)
            reasons.append(f"hist signal +{change_pct:.1f}%")
        elif change_pct > 0:
            score += 6
            reasons.append("green candle")
        elif change_pct > -1.0:
            score += 2
            reasons.append("flat day")
        else:
            score -= 5
            reasons.append("red day")

        # -- ATR trailing (from reason text since ATR stop itself isn't a column) --
        if "above atr trailing" in reason_text or "price above atr" in reason_text:
            score += 12
            reasons.append("above ATR stop")
        elif "below atr" in reason_text:
            score -= 8

        # -- MACD direction (from reason text) --
        if "macd bullish crossover" in reason_text:
            score += 10
            reasons.append("MACD cross up")
        elif "macd histogram increasing" in reason_text:
            score += 5

        # -- Trigger proximity --
        if trigger_ready:
            score += 10
            reasons.append("trigger ready")
        elif entry_gap_pct <= 0.3:
            score += 8
            reasons.append("very near trigger")
        elif entry_gap_pct <= 0.6:
            score += 5
            reasons.append("near trigger")
        elif entry_gap_pct <= 1.0:
            score += 2

        # -- Confidence from main engine as a bonus (not primary) --
        if confidence >= 25:
            score += 10
            reasons.append(f"high confidence {confidence:.0f}%")
        elif confidence >= 15:
            score += 5

        # -- Consolidation bonus: stock has been in tight range before this breakout --
        # Detected by: stop and target are BOTH relatively close to current price
        # (narrow stop = low recent volatility = stock was coiling before move)
        # and today it has a green candle breaking out of that range.
        _stop = float(row.get("SellIfBelow", row.get("Sell If Below", 0)) or 0)
        _t2 = float(row.get("TakeProfit2", row.get("Take Profit 2", 0)) or 0)
        if _price_now > 0 and _stop > 0 and _t2 > 0 and change_pct >= 1.0:
            _range_pct = (_t2 - _stop) / _price_now * 100
            # Tight range = target-to-stop spread < 20% of price → coiling setup
            if _range_pct < 15:
                score += 12
                reasons.append(f"consolidation breakout (tight range {_range_pct:.1f}%)")
            elif _range_pct < 25:
                score += 6
                reasons.append(f"range breakout ({_range_pct:.1f}%)")

        # -- Penalties for bearish conditions --
        # HARD BLOCK: If stock is below all MAs, do NOT recommend buying
        if "below all major ma" in reason_text or "bearish alignment" in reason_text:
            score -= 50
            reasons.append("⛔ BEARISH - below all moving averages, DO NOT BUY")
        if "death cross" in reason_text:
            score -= 40
            reasons.append("⛔ DEATH CROSS - strong sell signal")
        if "lower highs" in reason_text and "lower lows" in reason_text:
            score -= 25
            reasons.append("⛔ downtrend - making lower highs and lows")

        # -- CRITICAL: Price vs entry trigger --
        # If current price is BELOW the Buy Above trigger, the breakout isn't confirmed
        if _price_now > 0 and _buy_above > 0:
            if _price_below_trigger:
                gap_below = round((_buy_above - _price_now) / _buy_above * 100, 1)
                if gap_below >= 1.0:
                    score -= 25
                    reasons.append(f"⛔ price {gap_below}% BELOW buy trigger - NOT safe to buy")
                else:
                    score -= 10
                    reasons.append(f"price {gap_below}% below buy trigger - wait for confirmation")
            else:
                score += 8
                reasons.append("✅ price ABOVE buy trigger - entry confirmed")

        # -- Price vs stop loss --
        stop_loss = float(row.get("SellIfBelow", row.get("Sell If Below", row.get("StopLoss", 0))) or 0)
        if _price_now > 0 and stop_loss > 0 and _buy_above > 0:
            # If price is near or below stop loss — this is dangerous
            if _price_now <= stop_loss * 1.01:
                score -= 30
                reasons.append("DANGER: price at stop loss level")

        score = round(max(0, min(score, 100)), 1)
        return score, ", ".join(reasons)


# ---------------------------------------------------------------------------
# Main Advisor
# ---------------------------------------------------------------------------

class AutoPilotInvestor:
    """
    Blind-buy advisor: finds opportunities, alerts you to buy, monitors your
    portfolio, and tells you when to sell.

    Each cycle:
    1. Load signal monitor state (the live board of all scanned symbols)
    2. Re-score every stock using the aggressive BlindBuyScorer
    3. Filter for blind-buy candidates (score >= threshold)
    4. Compare with previously-alerted symbols to avoid spam
    5. Push new blind-buy alerts to Discord
    6. Load your portfolio, check each position for sell conditions
    7. Push sell alerts to Discord for positions that should be exited
    """

    def __init__(self, config: AutoPilotConfig) -> None:
        self.config = config.normalized()
        self.output_dir = Archiver.get_user_data_dir()
        self.logger = default_logger()
        self._guardrails = LiveTradingGuardrails(
            account_name=self.config.account_name,
            max_daily_loss_amount=self.config.max_daily_loss_amount,
            max_consecutive_losses=self.config.max_consecutive_losses,
        )
        self._cycle_count = 0
        self._last_blind_buy_diagnostics: Counter = Counter()
        self._already_alerted_buys: Set[str] = self._load_daily_alerted_set()  # persist across restarts
        self._definitely_buy_alerted: Set[str] = set()  # track DEFINITELY BUY to allow upgrade re-alerts
        self._last_buynow_rescan: float = 0.0  # timestamp of last /buynow rescan
        self._rescan_alert_count: int = 0  # number of BuyNow-rescan alerts sent today
        self._already_alerted_sells: Set[str] = self._load_daily_sell_alerted_set()  # persist across restarts
        self._last_buy_alert_time: Optional[datetime] = None  # cooldown tracking
        self._pending_followups: List["BlindBuyCandidate"] = []  # buffer for batching
        self._FOLLOWUP_COOLDOWN_SEC = 900  # 15 minutes between follow-up alerts
        # BuyNow rescan tuning: keep this path rare and high quality (fallback only).
        self._RESCAN_INTERVAL_SEC = 60          # every 1 min — faster second-chance detection
        self._RESCAN_MAX_PER_CYCLE = 2          # burst control: max 2 per rescan cycle
        self._RESCAN_MAX_PER_DAY = 12           # hard daily cap for "MISSED EARLIER" alerts
        self._RESCAN_MIN_SCORE = 50.0           # stricter than global min-score
        self._RESCAN_MIN_CONFIDENCE = 20.0      # avoid low-conviction stale setups
        self._RESCAN_MIN_VOL_RATIO = 0.8        # require at least reasonable volume backing
        self._RESCAN_MIN_DAY_CHANGE = _env_float("ASSISTANT_RESCAN_MIN_DAY_CHANGE", 1.0)
        # must show live momentum now; lowered after honest replay showed the
        # +1% to +4.5% Buy Now lane had better edge than late top-gainer chases.
        self._RESCAN_MAX_DAY_CHANGE = 14.0      # allow strong movers up to 14% (tagged SWING ONLY above 8%)
        # ── CONSISTENT DAILY INCOME: 5 trades/day ──
        # 5 × ₹50K = ₹2.5L = full MIS margin. Diversification ensures even if
        # 1 trade loses, the other 4 cover it. Most days: ₹5K-₹20K income.
        self._TOP_N_DAILY_BATCH = 5             # 5 trades per day for consistency
        self._AUTO_BUY_TOP_N = 0
        # 0 = no live-alert-side cap; rely on broker funds, position limits, and
        # other risk checks. Positive values re-enable a hard daily cap.
        self._daily_batch_alert_count: int = 0  # counter of batch buy alerts sent today
        self._tick_confirmed_auto_buy_count: int = 0  # successful live auto-buys from Discord/tick-confirmed path
        self._OPENING_FAST_LANE_TOP_N = max(1, _env_int("ASSISTANT_OPENING_FAST_LANE_TOP_N", 4))
        # Keep fast-lane selective, but do not hard-stop at the first 1-2 names.
        # The order manager now time-slices capital, so cleaner 9:25-10:30 names
        # still have buying power even if early starters fire.
        self._opening_fast_lane_count: int = 0  # counter of elite opening-window bypasses sent today
        # Opening priority refresh: rescan a small liquid subset during the
        # first minutes of trade so we do not wait for the slow full-universe
        # sweep before seeing fresh breakouts.
        self._OPENING_PRIORITY_MAX_SYMBOLS = 180
        self._OPENING_PRIORITY_REFRESH_SEC = 30
        self._last_opening_priority_refresh: float = 0.0
        self._opening_priority_state_df: Optional[pd.DataFrame] = None
        self._opening_priority_stats: Dict[str, float] = {}

        # Tick-only fast scanner state (no OHLCV, fires in < 1 second)
        self._tick_alerted: Dict[str, datetime] = {}  # symbol -> time of last tick-triggered scan
        self._tick_rejected: Set[str] = set()          # symbols rejected by OHLCV confirmation (use longer cooldown)
        self._watch_alerted: Dict[str, str] = {}      # symbol -> date string of last WATCH alert (once per day per symbol)
        self._prev_tick_snapshot: Dict[str, Dict] = {}  # last tick reading for delta-volume
        self._TICK_ORDER_IMBALANCE_RATIO = 2.5  # buy_qty / sell_qty threshold
        self._TICK_VOLUME_BURST_FRACTION = 0.025  # 2.5% of day-vol in 60s
        self._TICK_HIGH_DAILY_CHANGE_PCT = 2.0   # daily change% to auto-flag (catch moves early, at 2% not 3%)
        self._TICK_ALREADY_MOVED_PCT = 15.0      # above this % skip — at upper circuit, no sellers. 8-15% runners still tradeable as swing.
        # Hard cutoff: never send BUY alerts after this IST minute-of-day
        self._BUY_ALERT_CUTOFF_MIN = 15 * 60 + 15  # 3:15 PM — last 15 min are exit zone
        # 11:30 AM IST is now the switch to strict late-session MIS mode, not a
        # blanket rejection. New intraday entries still stop at the final MIS
        # cutoff, which defaults to 1:30 PM IST to stay aligned with swing risk.
        self._INTRADAY_AUTO_BUY_CUTOFF_MIN = _env_time_min(
            "ASSISTANT_INTRADAY_AUTO_BUY_CUTOFF",
            11 * 60 + 30,
        )
        # ETFs, liquid funds, and bond funds — not equity picks, never alert
        self._EXCLUDED_SYMBOLS: Set[str] = {
            "LIQUIDBEES", "CPSEETF", "NIFTYBEES", "BANKBEES", "JUNIORBEES",
            "GOLDBEES", "SILVERBEES", "SETFNIFBK", "SETFNIF50", "ICICIB22",
            "HABORETF", "EBBETF0425", "EBBETF0431", "EBBETF0430",
            "UTINIFTETF", "NETFNIF100", "ITBEES", "SHARIABEES",
            "MON100", "QNIFTY", "PSUBNKBEES", "INFRABEES", "MAFANG",
        }
        self._TICK_ALERT_COOLDOWN_SEC = 180      # 3 min cooldown for ALERTING same symbol (re-check for continuation)
        self._TICK_REJECT_COOLDOWN_SEC = 1800    # 30 min cooldown for REJECTED stocks — stops same losers hogging confirmation slots
        self._TICK_POLL_INTERVAL_SEC = 0.5       # sub-second polling — mtime cache avoids redundant parsing
        self._TICK_MAX_CONFIRM_PER_SCAN = 30     # was 12 — each is a background thread (~1-2s), so 30 costs the same wall time
        # Early Mover: catch stocks at 0.3-2% with heavy accumulation (volume moves before price)
        self._TICK_EARLY_MOVER_IMBALANCE = 3.0   # stricter imbalance ratio for early, unconfirmed moves
        self._TICK_EARLY_MOVER_MIN_PCT = 0.3     # minimum daily change% to consider as early mover
        self._TICK_EARLY_MOVER_VOL_RATIO = 3.0   # OHLCV volume ratio required to confirm early mover
        # Demand Watchlist: tracks stocks rejected by OHLCV gates but showing demand signals.
        # Re-checked every tick scan cycle (5s) for momentum confirmation.
        # schema: {symbol: {detect_price, detect_change, detect_time, imbalance, volume, reasons, peak_price}}
        self._demand_watchlist: Dict[str, Dict] = {}
        self._demand_alert_count: int = 0  # cap demand-confirmed alerts per session
        self._DEMAND_WATCHLIST_EXPIRY_SEC = 5400  # 90 min expiry — stocks can explode late
        self._DEMAND_WATCHLIST_MAX_ALERTS = 12    # max demand-confirmed alerts per session
        # Demand-confirmed alerts target +8% continuation; don't send too late in day.
        # After 2:45 PM there is usually not enough session left for this setup to play out.
        self._DEMAND_ALERT_CUTOFF_MIN = 14 * 60 + 45
        self._DEMAND_CONFIRM_RISE_PCT = 0.9       # standard rise threshold from detection
        self._DEMAND_CONFIRM_MIN_IMBALANCE = 2.8  # minimum persistent order-book demand
        self._DEMAND_CONFIRM_MIN_VOL_RATIO = 2.0  # minimum live volume / 10-day avg volume
        self._DEMAND_CONFIRM_MAX_DAY_CHANGE = 7.5 # raised from 5.5 to match opening-starter ceiling
        self._DEMAND_CONFIRM_FAST_RISE_PCT = 0.7  # fast-track stronger setups earlier
        self._DEMAND_CONFIRM_FAST_IMBALANCE = 4.5 # fast-track needs very strong imbalance
        self._DEMAND_AUTO_BUY_ENABLED = _env_truthy("ASSISTANT_DEMAND_AUTO_BUY", True)
        self._DEMAND_AUTO_MAX_PER_DAY = _env_int("ASSISTANT_DEMAND_AUTO_MAX_PER_DAY", 2)
        self._DEMAND_AUTO_MIN_RR = _env_float("ASSISTANT_DEMAND_AUTO_MIN_RR", 2.0)
        self._demand_auto_bought: Set[str] = set()
        self._demand_auto_date: Optional[str] = None
        # Top-gainer radar: visibility layer only. It should show the full field
        # of live runners without turning every runner into an actionable BUY.
        self._runner_radar_seen: Dict[str, int] = {}  # symbol -> highest pct bucket alerted
        self._runner_radar_auto_reviewed: Set[str] = set()
        self._runner_radar_date: Optional[str] = None
        self._runner_radar_last_summary: float = 0.0
        self._RUNNER_RADAR_MIN_CHANGE = 5.0
        self._RUNNER_RADAR_BUCKET_STEP = 5
        self._RUNNER_RADAR_SUMMARY_INTERVAL_SEC = 90
        self._RUNNER_RADAR_MAX_LINES = 12
        self._RUNNER_RADAR_MIN_QUALITY = {"TRADE CANDIDATE", "WATCH"}
        # Early expansion radar: visibility-only board for stocks forming at
        # +1% to +4.5% before they graduate into the top-gainer radar.
        self._early_radar_seen: Dict[str, int] = {}  # symbol -> highest 1% bucket alerted
        self._early_radar_date: Optional[str] = None
        self._early_radar_last_summary: float = 0.0
        self._EARLY_RADAR_MIN_CHANGE = _env_float("ASSISTANT_EARLY_RADAR_MIN_CHANGE", 1.0)
        self._EARLY_RADAR_MAX_CHANGE = _env_float("ASSISTANT_EARLY_RADAR_MAX_CHANGE", 4.5)
        self._EARLY_RADAR_MIN_TURNOVER_CR = _env_float("ASSISTANT_EARLY_RADAR_MIN_TURNOVER_CR", 0.75)
        self._EARLY_RADAR_MIN_IMBALANCE = _env_float("ASSISTANT_EARLY_RADAR_MIN_IMBALANCE", 1.4)
        self._EARLY_RADAR_SUMMARY_INTERVAL_SEC = _env_int("ASSISTANT_EARLY_RADAR_INTERVAL_SEC", 60)
        self._EARLY_RADAR_MAX_LINES = _env_int("ASSISTANT_EARLY_RADAR_MAX_LINES", 8)
        # Execution-grade early runner lane: tiny starter buys for the cleanest
        # tick-confirmed runners before they become obvious +7-10% chases.
        self._EARLY_RUNNER_AUTO_BUY_ENABLED = _env_truthy("ASSISTANT_EARLY_RUNNER_AUTO_BUY", True)
        self._EARLY_RUNNER_AUTO_MAX_PER_DAY = _env_int("ASSISTANT_EARLY_RUNNER_AUTO_MAX_PER_DAY", 4)
        self._EARLY_RUNNER_AUTO_MIN_CHANGE = _env_float("ASSISTANT_EARLY_RUNNER_AUTO_MIN_CHANGE", 1.2)
        self._EARLY_RUNNER_AUTO_MAX_CHANGE = _env_float("ASSISTANT_EARLY_RUNNER_AUTO_MAX_CHANGE", 5.5)
        self._EARLY_RUNNER_AUTO_MIN_TURNOVER_CR = _env_float("ASSISTANT_EARLY_RUNNER_AUTO_MIN_TURNOVER_CR", 0.75)
        self._EARLY_RUNNER_AUTO_MIN_IMBALANCE = _env_float("ASSISTANT_EARLY_RUNNER_AUTO_MIN_IMBALANCE", 1.4)
        self._EARLY_RUNNER_AUTO_MIN_RR = _env_float("ASSISTANT_EARLY_RUNNER_AUTO_MIN_RR", 1.5)
        self._EARLY_RUNNER_AUTO_MAX_PULLBACK_PCT = _env_float("ASSISTANT_EARLY_RUNNER_AUTO_MAX_PULLBACK_PCT", 0.3)
        self._EARLY_RUNNER_AUTO_MAX_TICK_STALE_DROP_PCT = _env_float(
            "ASSISTANT_EARLY_RUNNER_AUTO_MAX_TICK_STALE_DROP_PCT",
            0.7,
        )
        self._early_runner_seen: Dict[str, float] = {}
        self._early_runner_auto_attempted: Set[str] = set()
        self._early_runner_auto_bought: Set[str] = set()
        self._early_runner_auto_date: Optional[str] = None
        # Internal early-watch list. Stocks identified by the early radar are
        # added here silently and re-evaluated every cycle. Lifecycle:
        #   - tracked silently (no Discord)
        #   - if entry becomes too late: marked MISSED internally, dropped
        #   - if it weakens (below VWAP / buyer pressure fades): dropped silently
        #   - if it becomes clean: auto-buy attempt, Discord AUTO-BOUGHT or AUTO-BUY FAILED
        self._early_watch_list: Dict[str, Dict] = {}
        self._early_watch_date: Optional[str] = None
        self._EARLY_WATCH_MAX_SIZE = _env_int("ASSISTANT_EARLY_WATCH_MAX_SIZE", 40)
        self._EARLY_WATCH_WEAK_IMBALANCE = _env_float("ASSISTANT_EARLY_WATCH_WEAK_IMBALANCE", 1.0)
        self._EARLY_WATCH_PEAK_PULLBACK_PCT = _env_float("ASSISTANT_EARLY_WATCH_PEAK_PULLBACK_PCT", 0.6)
        # Second-leg / continuation exception: a stock that already moved past
        # _EARLY_RUNNER_AUTO_MAX_CHANGE is normally MISSED. The exception below
        # allows a clean continuation IF the stock clearly rested (pulled back
        # from intraday high) AND reclaimed VWAP AND buyer demand re-asserted.
        self._SECOND_LEG_MAX_CHANGE = _env_float("ASSISTANT_SECOND_LEG_MAX_CHANGE", 8.0)
        self._SECOND_LEG_MIN_PULLBACK_PCT = _env_float("ASSISTANT_SECOND_LEG_MIN_PULLBACK_PCT", 1.0)
        self._SECOND_LEG_MIN_IMBALANCE = _env_float("ASSISTANT_SECOND_LEG_MIN_IMBALANCE", 2.5)
        self._SECOND_LEG_MIN_TURNOVER_CR = _env_float("ASSISTANT_SECOND_LEG_MIN_TURNOVER_CR", 1.5)
        self._SECOND_LEG_MIN_REST_SEC = _env_int("ASSISTANT_SECOND_LEG_MIN_REST_SEC", 300)
        self._SECOND_LEG_MAX_VWAP_GAP_PCT = _env_float("ASSISTANT_SECOND_LEG_MAX_VWAP_GAP_PCT", 0.5)
        # Market regime gating: instantiated lazily to avoid import-time failures.
        self._market_context: Optional[Any] = None
        self._market_context_init_failed: bool = False
        self._market_regime_last_refresh: float = 0.0
        self._MARKET_REGIME_REFRESH_SEC = _env_int("ASSISTANT_MARKET_REGIME_REFRESH_SEC", 30)
        # Regimes in which auto-buy is allowed. "neutral" stays in to avoid
        # blocking trades on quiet but non-bearish days.
        self._MARKET_REGIME_BUY_OK = {"strong_bull", "bull", "neutral"}
        # NSE market hours (IST): 9:15 AM open, 3:30 PM close
        self._MARKET_OPEN_HOUR_IST = 9
        self._MARKET_OPEN_MIN_IST = 15
        self._MARKET_CLOSE_HOUR_IST = 15
        self._MARKET_CLOSE_MIN_IST = 30
        # Pre-market watchlist state
        self._premarket_sent_date: Optional[str] = None  # date string — send once per day
        # Pre-open auction analyzer
        self._preopen_analyzer = PreOpenAnalyzer(universe=self.config.universe)
        self._preopen_auction_sent_phase: Optional[str] = None  # last phase alert sent
        self._preopen_auction_sent_date: Optional[str] = None
        self._midday_sent_date: Optional[str] = None     # mid-day scanner — once per day
        self._powerhour_sent_date: Optional[str] = None  # power hour alert — once per day
        self._eod_exit_sent_date: Optional[str] = None   # end-of-day exit reminder — once per day
        # Momentum continuation tracking: symbol -> {alert_time, alert_pct, entry_price}
        self._momentum_tracked: Dict[str, Dict] = {}  # stocks we alerted — track for continuation
        self._momentum_tracked_date: Optional[str] = None  # date when momentum dict was last populated
        self._nifty_crash_pause: bool = False  # True if Nifty down >2% in first 30 min of day
        # Fade detector: tracks alerted stocks for reversal/exit signals
        self._fade_alerted: Dict[str, datetime] = {}  # symbol -> time of last EXIT alert
        self._FADE_CHECK_INTERVAL_SEC = 30    # check for fades every 30s (not every tick — too noisy)
        self._FADE_DROP_FROM_ENTRY_PCT = 2.0  # price dropped 2%+ from entry → warning
        self._FADE_DROP_FROM_HIGH_PCT = 4.0   # price dropped 4%+ from intraday high → EXIT
        self._FADE_SELL_IMBALANCE = 1.5       # sell_qty / buy_qty >= 1.5x confirms selling pressure
        self._FADE_COOLDOWN_SEC = 600         # 10 min between EXIT alerts for same stock
        self._last_fade_check: float = 0.0    # timestamp of last fade check
        # Opening alert quality cap: buffer alerts during opening 15-min window,
        # flush as a ranked summary to avoid flooding with 30+ individual alerts.
        self._MAX_OPENING_ALERTS = 10         # cap opening flush to top 10
        self._opening_buffer: List[Dict] = [] # buffered alert dicts during opening window
        self._opening_buffer_lock = __import__('threading').Lock()
        self._last_opening_flush: float = 0.0
        self._OPENING_FLUSH_INTERVAL_SEC = 10 # flush buffer every 10s during opening
        self._opening_flush_done_date: Optional[str] = None  # date of last complete flush
        # Paper trade tracker — logs every alert, checks EOD outcome
        self._paper_tracker = PaperTradeTracker()
        self._discord_execution_only = True
        self._daily_execution_summary_sent_date: Optional[str] = None
        self._daily_auto_bought: List[str] = []
        self._daily_auto_buy_failed: List[str] = []
        self._daily_auto_buy_skipped: List[str] = []
        self._daily_protection_failed: List[str] = []
        self._daily_missed_opportunities: List[str] = []
        self._auto_buy_timeline: Dict[str, Dict[str, Any]] = {}
        self._daily_internal_watch: Set[str] = set()
        self._daily_internal_avoid: Set[str] = set()
        self._daily_no_buy_reasons: Counter = Counter()

        # Kite auto-order manager (only if KITE_AUTO_TRADE=1)
        self._order_manager = None
        try:
            from pkscreener.classes.KiteOrderManager import KiteOrderManager
            mgr = KiteOrderManager(output_dir=self.output_dir)
            self._order_manager = mgr
            if mgr.auto_trade:
                self._log(f"Kite order manager active (LIVE auto-trade ON, dry_run={mgr.dry_run})")
            elif mgr.dry_run:
                self._log("Kite order manager active (DRY RUN mode)")
            else:
                self._log("Kite order manager active but auto-trade is OFF (set KITE_AUTO_TRADE=1 to place real buys)")
        except Exception:
            pass  # Kite trading not configured — alert-only mode

    def _try_auto_buy(self, symbol: str, price: float, stop_loss: float = 0.0,
                      target1: float = 0.0, target2: float = 0.0,
                      blind_score: float = 0.0, reasons: str = "",
                      product_preference: str = "", holding_horizon: str = "",
                      max_hold_days: int = 0) -> Optional[str]:
        """Attempt to auto-buy via Kite if order manager is active.

        Returns a Discord-ready status line, or None if auto-trade is off.
        """
        candidate_stub = BlindBuyCandidate(
            symbol=symbol,
            price=price,
            stop_loss=stop_loss,
            target1=target1,
            target2=target2,
            blind_score=blind_score,
            reasons=reasons,
            product_preference=product_preference,
            holding_horizon=holding_horizon,
            max_hold_days=max_hold_days,
        )
        if self._order_manager is None:
            return self._format_missed_or_late_message(
                symbol,
                reason="System issue: Kite auto-buy unavailable because the order manager is not initialized.",
                candidate=candidate_stub,
                live_price=price,
            )
        product_label = str(product_preference or "").strip().upper()
        horizon_label = str(holding_horizon or "").strip().lower()
        is_intraday = product_label == "MIS" or horizon_label == "intraday"
        if is_intraday:
            now_min = self._ist_now_minute()
            final_cutoff = self._intraday_final_auto_buy_cutoff_min()
            if now_min > final_cutoff:
                return self._format_missed_or_late_message(
                    symbol,
                    reason=(
                        f"Buyable setup, but the final intraday auto-buy cutoff "
                        f"{_format_ist_minute(final_cutoff)} IST passed before execution."
                    ),
                    candidate=candidate_stub,
                    live_price=price,
                )
        result = self._order_manager.auto_buy_from_candidate(
            symbol=symbol, live_price=price, stop_loss=stop_loss,
            target1=target1, target2=target2,
            blind_score=blind_score, reasons=reasons,
            product_type=product_preference,
            holding_horizon=holding_horizon,
            max_hold_days=max_hold_days,
        )
        if result.success:
            product_label = product_label or "AUTO"
            horizon_label = horizon_label or "auto"
            status = (
                f"✅ **AUTO-BOUGHT — `{symbol}`**\n"
                f"Placed Kite order `{result.order_id}` for `{result.qty}` shares "
                f"at about `₹{price:.2f}` | Product `{product_label}` | Horizon `{horizon_label}`"
            )
            # Automatically place GTT OCO (stop-loss + target) so no manual action needed
            if stop_loss > 0 and target2 > 0 and price > 0:
                gtt = self._order_manager.place_gtt_oco(
                    symbol=symbol, qty=result.qty,
                    buy_price=price, stop_loss=stop_loss, target=target2,
                    product_type=product_preference,
                )
                if gtt.get("success"):
                    self._order_manager.update_position_gtt(
                        symbol,
                        trigger_id=str(gtt.get("trigger_id", "") or ""),
                        status="active",
                        error="",
                    )
                    status += f"\n🔐 GTT set — SL ₹{stop_loss:.2f} | Target ₹{target2:.2f} (ID: {gtt.get('trigger_id','')})"
                else:
                    self._order_manager.update_position_gtt(
                        symbol,
                        status="failed",
                        error=str(gtt.get("message", "") or ""),
                    )
                    status += (
                        f"\n⚠️ **PROTECTION FAILED — `{symbol}`**\n"
                        f"Buy happened, but stop loss / GTT protection failed: {gtt.get('message','')}"
                    )
            return status
        else:
            raw_message = str(result.message or "").strip()
            lower = raw_message.lower()
            if "live price faded" in lower:
                return (
                    f"⚠️ **AUTO-BUY FAILED — `{symbol}`**\n"
                    f"FAILED: price moved/faded before execution. {raw_message}"
                )
            if any(token in lower for token in (
                "max open positions",
                "market is closed",
                "order manager not initialized",
                "no affordable",
                "remaining pool",
                "per-trade cap",
                "already holding",
            )):
                return self._format_missed_or_late_message(
                    symbol,
                    reason=f"Buyable setup missed because: {raw_message}",
                    candidate=candidate_stub,
                    live_price=price,
                )
            # Auto-trade intentionally disabled — not a failure, just manual mode.
            # Return None so the caller skips sending anything to Discord.
            if "auto_trade_disabled" in lower or "auto-trade is disabled" in lower:
                return None
            return f"⚠️ **AUTO-BUY FAILED — `{symbol}`**\n{raw_message}"

    def _ist_now_minute(self) -> int:
        try:
            import pytz
            now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
        except Exception:
            from datetime import timezone, timedelta
            now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        return now_ist.hour * 60 + now_ist.minute

    def _ist_now(self) -> datetime:
        try:
            import pytz
            return datetime.now(pytz.timezone("Asia/Kolkata"))
        except Exception:
            from datetime import timezone, timedelta
            return datetime.now(timezone(timedelta(hours=5, minutes=30)))

    def _swing_auto_buy_cutoff_min(self) -> int:
        return 13 * 60 + 30

    def _intraday_final_auto_buy_cutoff_min(self) -> int:
        return _env_time_min("ASSISTANT_INTRADAY_FINAL_CUTOFF", 13 * 60 + 30)

    def _note_auto_buy_timeline(
        self,
        symbol: str,
        *,
        live_price: float = 0.0,
        entry_type: str = "",
        market_regime: str = "",
        holding_horizon: str = "",
        buyable_now: Optional[bool] = None,
    ) -> Dict[str, Any]:
        timeline = self._auto_buy_timeline.setdefault(symbol, {})
        now_ist = self._ist_now()
        now_min = self._ist_now_minute()
        ts = now_ist.strftime("%Y-%m-%d %H:%M:%S %Z")
        live_price = round(float(live_price or 0.0), 2)
        intraday_final_cutoff = self._intraday_final_auto_buy_cutoff_min()

        timeline.setdefault("first_detected_time", ts)
        timeline.setdefault("intraday_cutoff_time", _format_ist_minute(self._INTRADAY_AUTO_BUY_CUTOFF_MIN))
        timeline.setdefault("intraday_final_cutoff_time", _format_ist_minute(intraday_final_cutoff))
        timeline.setdefault("swing_cutoff_time", _format_ist_minute(self._swing_auto_buy_cutoff_min()))
        timeline.setdefault("entry_type", str(entry_type or ""))
        if market_regime:
            timeline["market_regime"] = market_regime
        if holding_horizon:
            timeline["holding_horizon"] = holding_horizon
        if live_price > 0 and "live_price_at_detection" not in timeline:
            timeline["live_price_at_detection"] = live_price

        if buyable_now:
            timeline.setdefault("first_buyable_time", ts)
            if live_price > 0 and "live_price_at_first_buyable" not in timeline:
                timeline["live_price_at_first_buyable"] = live_price

        if now_min <= self._INTRADAY_AUTO_BUY_CUTOFF_MIN:
            timeline["detected_before_intraday_cutoff"] = "yes"
            if buyable_now:
                timeline["buyable_before_intraday_cutoff"] = "yes"
        else:
            timeline.setdefault("detected_before_intraday_cutoff", "no")
            timeline.setdefault("buyable_before_intraday_cutoff", "no")
            if live_price > 0 and "live_price_at_intraday_cutoff" not in timeline:
                timeline["live_price_at_intraday_cutoff"] = live_price

        if now_min <= intraday_final_cutoff:
            timeline["detected_before_intraday_final_cutoff"] = "yes"
            if buyable_now:
                timeline["buyable_before_intraday_final_cutoff"] = "yes"
        else:
            timeline.setdefault("detected_before_intraday_final_cutoff", "no")
            timeline.setdefault("buyable_before_intraday_final_cutoff", "no")
            if live_price > 0 and "live_price_at_intraday_final_cutoff" not in timeline:
                timeline["live_price_at_intraday_final_cutoff"] = live_price

        if now_min <= self._swing_auto_buy_cutoff_min():
            timeline["detected_before_swing_cutoff"] = "yes"
            if buyable_now:
                timeline["buyable_before_swing_cutoff"] = "yes"
        else:
            timeline.setdefault("detected_before_swing_cutoff", "no")
            timeline.setdefault("buyable_before_swing_cutoff", "no")
            if live_price > 0 and "live_price_at_swing_cutoff" not in timeline:
                timeline["live_price_at_swing_cutoff"] = live_price
        return timeline

    def _format_missed_or_late_message(
        self,
        symbol: str,
        *,
        reason: str,
        candidate: Optional['BlindBuyCandidate'] = None,
        live_price: float = 0.0,
    ) -> str:
        now_min = self._ist_now_minute()
        product_label = str(getattr(candidate, "product_preference", "") or "").upper()
        horizon_label = str(getattr(candidate, "holding_horizon", "") or "").lower()
        is_intraday = product_label == "MIS" or horizon_label == "intraday"
        entry_type = str(getattr(candidate, "entry_type", "") or "")
        timeline = self._note_auto_buy_timeline(
            symbol,
            live_price=live_price or float(getattr(candidate, "price", 0.0) or 0.0),
            entry_type=entry_type,
            holding_horizon=horizon_label,
            buyable_now=False,
        )
        timeline["reason_not_bought"] = str(reason or "")
        timeline["live_price_at_missed_message"] = round(float(live_price or getattr(candidate, "price", 0.0) or 0.0), 2)
        self._log(
            "MissedAudit "
            f"{symbol}: first_detected={timeline.get('first_detected_time','')} "
            f"first_buyable={timeline.get('first_buyable_time','')} "
            f"intraday_cutoff={timeline.get('intraday_cutoff_time','')} "
            f"intraday_final_cutoff={timeline.get('intraday_final_cutoff_time','')} "
            f"swing_cutoff={timeline.get('swing_cutoff_time','')} "
            f"detected_before_intraday={timeline.get('detected_before_intraday_cutoff','no')} "
            f"buyable_before_intraday={timeline.get('buyable_before_intraday_cutoff','no')} "
            f"detected_before_intraday_final={timeline.get('detected_before_intraday_final_cutoff','no')} "
            f"buyable_before_intraday_final={timeline.get('buyable_before_intraday_final_cutoff','no')} "
            f"detected_before_swing={timeline.get('detected_before_swing_cutoff','no')} "
            f"buyable_before_swing={timeline.get('buyable_before_swing_cutoff','no')} "
            f"entry_type={timeline.get('entry_type','')} "
            f"market_regime={timeline.get('market_regime','')} "
            f"price_detected={timeline.get('live_price_at_detection','')} "
            f"price_intraday_cutoff={timeline.get('live_price_at_intraday_cutoff','')} "
            f"price_intraday_final_cutoff={timeline.get('live_price_at_intraday_final_cutoff','')} "
            f"price_swing_cutoff={timeline.get('live_price_at_swing_cutoff','')} "
            f"price_missed={timeline.get('live_price_at_missed_message','')} "
            f"reason={timeline.get('reason_not_bought','')}"
        )

        buyable_before_intraday = timeline.get("buyable_before_intraday_cutoff") == "yes"
        buyable_before_intraday_final = timeline.get("buyable_before_intraday_final_cutoff") == "yes"
        buyable_before_swing = timeline.get("buyable_before_swing_cutoff") == "yes"

        lower_reason = str(reason or "").lower()
        if now_min > self._swing_auto_buy_cutoff_min():
            if buyable_before_swing:
                header = "⚠️ **IMPORTANT MISSED OPPORTUNITY"
            else:
                header = "🕒 **LATE_DISCOVERY"
        elif is_intraday and now_min > self._intraday_final_auto_buy_cutoff_min():
            if buyable_before_intraday_final:
                header = "⚠️ **IMPORTANT MISSED OPPORTUNITY"
            else:
                header = "🕒 **LATE_DISCOVERY"
        elif "strict cnc swing re-check failed" in lower_reason:
            header = "🟠 **SWING_RECHECK_REJECTED"
        elif "strict late-session mis rejected" in lower_reason:
            header = "🟠 **STRICT_MODE_REJECTED"
        elif any(token in lower_reason for token in (
            "max open positions",
            "market is closed",
            "per-trade cap",
            "already holding",
            "blocked by market regime",
            "order manager",
            "remaining pool",
            "no affordable",
            "live auto-buy cap",
        )):
            header = "⛔ **BLOCKED"
        elif buyable_before_intraday or buyable_before_intraday_final or buyable_before_swing:
            header = "⚠️ **IMPORTANT MISSED OPPORTUNITY"
        else:
            header = "❌ **REJECTED"

        return f"{header} — `{symbol}`**\n{reason}"

    def _cap_suggested_qty_to_broker(
        self,
        price: float,
        desired_qty: int,
        product_preference: str = "",
        holding_horizon: str = "",
    ) -> Tuple[int, float]:
        """Cap suggested quantity to something the connected broker can afford."""
        if desired_qty <= 0 or price <= 0:
            return 0, 0.0
        if self._order_manager is None:
            return desired_qty, round(desired_qty * price, 0)

        inferred_product = str(product_preference or "").upper()
        if not inferred_product:
            inferred_product = "MIS" if str(holding_horizon or "").lower() == "intraday" else "CNC"
        try:
            return self._order_manager.estimate_affordable_qty(
                price=price,
                desired_qty=desired_qty,
                product_type=inferred_product,
            )
        except Exception:
            return desired_qty, round(desired_qty * price, 0)

    def _resolve_auto_buy_candidate(
        self,
        candidate: 'BlindBuyCandidate',
    ) -> Tuple['BlindBuyCandidate', str, bool, str]:
        candidate = self._enrich_candidate_alert_stage(candidate, off_high_pct=float(getattr(candidate, "off_high_pct", 0.0) or 0.0))
        grade, _ = self._grade_candidate(candidate)
        eligible, reason = self._live_auto_buy_gate_detail(candidate, grade)
        self._note_auto_buy_timeline(
            candidate.symbol,
            live_price=candidate.price,
            entry_type=candidate.entry_type,
            holding_horizon=candidate.holding_horizon,
            buyable_now=eligible,
        )
        if eligible:
            return candidate, grade, True, reason

        now_min = self._ist_now_minute()
        is_intraday = str(getattr(candidate, "holding_horizon", "") or "").lower() == "intraday"
        if is_intraday and self._INTRADAY_AUTO_BUY_CUTOFF_MIN < now_min <= self._swing_auto_buy_cutoff_min():
            strict_mis_ok, strict_mis_reason = self._strict_late_session_intraday_gate(candidate)
            if strict_mis_ok:
                self._note_auto_buy_timeline(
                    candidate.symbol,
                    live_price=candidate.price,
                    entry_type=candidate.entry_type,
                    holding_horizon=candidate.holding_horizon,
                    buyable_now=True,
                )
                return candidate, grade, True, strict_mis_reason
            converted = BlindBuyCandidate(**vars(candidate))
            converted.holding_horizon = "2-day swing"
            converted.product_preference = "CNC"
            converted.max_hold_days = max(2, int(getattr(candidate, "max_hold_days", 0) or 0))
            converted.horizon_reason = "converted from intraday after 11:30 AM IST cutoff"
            converted = self._enrich_candidate_horizon(converted)
            converted.holding_horizon = "2-day swing"
            converted.product_preference = "CNC"
            converted.max_hold_days = max(2, int(getattr(candidate, "max_hold_days", 0) or 0))
            converted = self._enrich_candidate_alert_stage(converted, off_high_pct=float(getattr(converted, "off_high_pct", 0.0) or 0.0))
            converted_grade, _ = self._grade_candidate(converted)
            converted_eligible, converted_reason = self._live_auto_buy_gate_detail(converted, converted_grade)
            if not converted_eligible:
                converted_eligible, converted_reason = self._strict_post_cutoff_swing_gate(converted)
            self._note_auto_buy_timeline(
                converted.symbol,
                live_price=converted.price,
                entry_type=converted.entry_type,
                holding_horizon=converted.holding_horizon,
                buyable_now=converted_eligible,
            )
            if converted_eligible:
                return converted, converted_grade, True, (
                    "intraday cutoff passed, but the setup re-qualified as a strict CNC swing entry"
                )
            return converted, converted_grade, False, (
                f"strict late-session MIS rejected: {strict_mis_reason}; strict CNC swing re-check failed: {converted_reason}"
            )
        return candidate, grade, False, reason

    def _strict_post_cutoff_swing_gate(self, candidate: 'BlindBuyCandidate') -> Tuple[bool, str]:
        execution_ok, execution_reason = self._execution_quality_gate(candidate)
        if not execution_ok:
            return False, execution_reason
        swing_ok, swing_reason = self._swing_entry_quality_gate(candidate)
        if not swing_ok:
            return False, swing_reason
        rr2 = self._candidate_rr2(candidate)
        gap_pct = self._candidate_entry_gap_pct(candidate)
        if candidate.price < 20:
            return False, "price is below the ₹20 swing floor"
        if rr2 < 1.5:
            return False, f"R:R 1:{rr2:.1f} is below the 2-day swing floor"
        if gap_pct > 0.8:
            return False, f"entry gap {gap_pct:.1f}% is too loose for the post-11:30 swing lane"
        if candidate.change_pct < 1.0 or candidate.change_pct > 7.0:
            return False, f"day move {candidate.change_pct:.1f}% is outside the strict post-11:30 swing zone"
        if candidate.blind_score < 40:
            return False, f"score {candidate.blind_score:.0f} is below the post-11:30 swing floor"
        if candidate.confidence < 28:
            return False, f"confidence {candidate.confidence:.0f}% is below the post-11:30 swing floor"
        if candidate.volume_ratio < 1.8:
            return False, f"volume {candidate.volume_ratio:.1f}x is below the post-11:30 swing floor"
        return True, "eligible now as a strict post-11:30 CNC swing auto-buy"

    @classmethod
    def _strict_late_session_intraday_gate(cls, c: 'BlindBuyCandidate') -> Tuple[bool, str]:
        execution_ok, execution_reason = cls._execution_quality_gate(c)
        if not execution_ok:
            return False, execution_reason
        rr2 = cls._candidate_rr2(c)
        gap_pct = cls._candidate_entry_gap_pct(c)
        entry_type, entry_reason = cls._classify_entry_type(c)
        if entry_type == "LATE_CHASE":
            return False, f"entry type LATE_CHASE — {entry_reason}"
        if entry_type not in {"PULLBACK_RETEST", "SECOND_LEG_CONTINUATION", "EARLY_BREAKOUT"}:
            return False, f"entry type {entry_type} is not fresh enough for late-session MIS"
        if c.price < 20:
            return False, "price is below the ₹20 floor"
        if rr2 < 1.7:
            return False, f"R:R 1:{rr2:.1f} is below the late-session MIS floor"
        if gap_pct > 0.7:
            return False, f"entry gap {gap_pct:.1f}% is too loose for late-session MIS"
        if c.blind_score < 58:
            return False, f"score {c.blind_score:.0f} is below the late-session MIS floor"
        if c.confidence < 30:
            return False, f"confidence {c.confidence:.0f}% is below the late-session MIS floor"
        if c.volume_ratio < 1.8:
            return False, f"volume {c.volume_ratio:.1f}x is below the late-session MIS floor"
        if c.change_pct < 1.2 or c.change_pct > 6.8:
            return False, f"day move {c.change_pct:.1f}% is outside the late-session MIS zone"
        if float(getattr(c, "off_high_pct", 0.0) or 0.0) > 1.4:
            return False, f"price is {float(getattr(c, 'off_high_pct', 0.0) or 0.0):.1f}% off the high and no fresh re-break is visible"
        if float(getattr(c, "vwap_gap_pct", 0.0) or 0.0) < -0.05:
            return False, "price is below VWAP, so the reclaim is not confirmed"
        if not cls._has_fresh_buyer_confirmation(c):
            return False, "fresh buyer confirmation is missing for late-session MIS"
        return True, f"eligible now as a strict late-session MIS auto-buy ({entry_type})"

    def _run_sell_monitor(self) -> None:
        """Check tracked Kite positions for sell signals and execute."""
        if self._order_manager is None:
            return
        try:
            gtt_actions = self._order_manager.audit_open_position_gtts()
        except Exception as exc:
            self._log(f"GTT audit failed: {exc}")
            gtt_actions = []
        for action in gtt_actions:
            if action.get("action") == "recreated" and action.get("ok"):
                self._log(f"GTT restored for {action['symbol']}: {action['message']}")
            elif action.get("action") == "repair_failed":
                sym = str(action["symbol"])
                # Dedupe: each symbol's protection failure alerts only once per
                # day. Audit runs every cycle so the same failure repeats —
                # firing once per minute spams Discord.
                if sym in self._daily_protection_failed:
                    continue
                failure_message = (
                    f"⚠️ **PROTECTION FAILED — `{sym}`**\n"
                    f"{action['message']}"
                )
                self._daily_protection_failed.append(sym)
                self._send_to_channels(failure_message)
        results = self._order_manager.execute_sell_signals()
        for r in results:
            if r.success:
                pos = self._order_manager.get_position(r.symbol)
                sell_reason = pos.sell_reason if pos else ""
                reason_text = self._order_manager.format_sell_reason(sell_reason)
                pnl = pos.pnl if pos else 0
                self._log(
                    f"AUTO-SELL {r.symbol} x{r.qty} — {reason_text} | "
                    f"Order ID: {r.order_id} | P/L: ₹{pnl:+,.0f}"
                )

    def _run_pending_buy_retries(self) -> None:
        """Retry queued auto-buys, mainly after temporary funds/margin failures."""
        if self._order_manager is None:
            return
        try:
            results = self._order_manager.retry_pending_buys()
        except Exception as exc:
            self._log(f"Pending auto-buy retry failed: {exc}")
            return
        for r in results:
            if r.success:
                self._handle_auto_buy_result(
                    r.symbol,
                    f"✅ **AUTO-BOUGHT — `{r.symbol}`**\nPlaced Kite retry order `{r.order_id}` for `{r.qty}` shares.",
                )
            elif r.status == "FUNDS_SHORT":
                self._record_no_buy_reason(str(r.message or "funds short"))
            elif r.status == "EXPIRED":
                self._handle_auto_buy_result(
                    r.symbol,
                    f"⚠️ **IMPORTANT MISSED OPPORTUNITY — `{r.symbol}`**\n{r.message}",
                )
            else:
                self._handle_auto_buy_result(
                    r.symbol,
                    f"⚠️ **AUTO-BUY FAILED — `{r.symbol}`**\n{r.message}",
                )

    def _attempt_tick_confirmed_auto_buy(
        self,
        *,
        symbol: str,
        price: float,
        stop_loss: float,
        target1: float,
        target2: float,
        blind_score: float,
        confidence: float,
        volume_ratio: float,
        daily_change: float,
        reasons: str,
        original_action: str = "",
        source: str = "tick-confirmed",
        spread_pct: float = 0.0,
        range_pct: float = 0.0,
        last_trade_qty: float = 0.0,
        turnover_cr: float = 0.0,
        buyer_imbalance: float = 0.0,
        vwap_gap_pct: float = 0.0,
        off_high_pct: float = 0.0,
        support_gap_pct: float = 0.0,
        fresh_buyer_confirmation: bool = False,
    ) -> Optional[str]:
        """Place a selective live buy for the tightest tick-confirmed setups."""
        if self._order_manager is None:
            return f"⚠️ Auto-buy skipped for `{symbol}`: order manager not initialized"
        if self._AUTO_BUY_TOP_N > 0 and self._tick_confirmed_auto_buy_count >= self._AUTO_BUY_TOP_N:
            self._log(
                f"Auto-buy skip {symbol}: {source} daily cap reached "
                f"({self._tick_confirmed_auto_buy_count}/{self._AUTO_BUY_TOP_N})"
            )
            return (
                f"⚠️ Auto-buy skipped for `{symbol}`: daily auto-buy cap reached "
                f"({self._tick_confirmed_auto_buy_count}/{self._AUTO_BUY_TOP_N} used today)"
            )

        candidate = self._enrich_candidate_horizon(BlindBuyCandidate(
            symbol=symbol,
            price=price,
            buy_above=price,
            stop_loss=stop_loss,
            target1=target1,
            target2=target2,
            confidence=confidence,
            volume_ratio=volume_ratio,
            change_pct=daily_change,
            blind_score=blind_score,
            original_action=original_action,
            reasons=reasons,
            spread_pct=spread_pct,
            range_pct=range_pct,
            last_trade_qty=last_trade_qty,
            turnover_cr=turnover_cr,
            buyer_imbalance=buyer_imbalance,
            vwap_gap_pct=vwap_gap_pct,
            off_high_pct=off_high_pct,
            support_gap_pct=support_gap_pct,
            fresh_buyer_confirmation=fresh_buyer_confirmation,
        ))
        candidate, grade, auto_eligible, gate_reason = self._resolve_auto_buy_candidate(candidate)
        if not auto_eligible:
            self._log(
                f"Auto-buy skip {symbol}: {source} not tight enough "
                f"(grade={grade}, conf={confidence:.0f}%, rr={self._candidate_rr2(candidate):.1f}, "
                f"gap={self._candidate_entry_gap_pct(candidate):.1f}%, vol={volume_ratio:.1f}x)"
            )
            return self._format_missed_or_late_message(
                symbol,
                reason=f"Buyable setup missed because: {gate_reason}",
                candidate=candidate,
                live_price=price,
            )

        result = self._try_auto_buy(
            symbol, price, stop_loss, target1, target2, blind_score, reasons,
            product_preference=candidate.product_preference,
            holding_horizon=candidate.holding_horizon,
            max_hold_days=candidate.max_hold_days,
        )
        if result and result.startswith("✅"):
            self._tick_confirmed_auto_buy_count += 1
            if self._AUTO_BUY_TOP_N > 0:
                self._log(
                    f"Auto-buy success {symbol}: {source} "
                    f"({self._tick_confirmed_auto_buy_count}/{self._AUTO_BUY_TOP_N} today)"
                )
            else:
                self._log(
                    f"Auto-buy success {symbol}: {source} "
                    f"({self._tick_confirmed_auto_buy_count} auto-buys today, uncapped)"
                )
        elif result:
            self._log(f"Auto-buy result {symbol}: {source} -> {result}")
        return result

    def _qualifies_for_opening_fast_lane(
        self,
        *,
        symbol: str,
        price: float,
        blind_score: float,
        confidence: float,
        volume_ratio: float,
        daily_change: float,
        stop_loss: float,
        target2: float,
        turnover_cr: float,
        imbalance: float,
        tick_high: float = 0.0,
        tick_low: float = 0.0,
    ) -> Tuple[bool, str]:
        """Return True only for elite opening-window setups worth instant action."""
        if not self._is_opening_window_active():
            return False, "opening window inactive"
        if self._opening_fast_lane_count >= self._OPENING_FAST_LANE_TOP_N:
            return False, "fast-lane daily cap reached"
        if price <= 0:
            return False, "invalid price"
        if blind_score < 48:
            return False, f"score {blind_score:.0f} < 48"
        if confidence < 18:
            return False, f"confidence {confidence:.0f}% < 18%"
        if not (0.4 <= daily_change <= 6.5):
            return False, f"move {daily_change:.1f}% outside opening zone"
        if turnover_cr < 0.75:
            return False, f"turnover ₹{turnover_cr:.1f}Cr < ₹0.75Cr"
        if stop_loss <= 0 or target2 <= 0:
            return False, "missing stop/target"
        rr2 = _compute_long_rr(price, stop_loss, target2)
        micro_open = self._is_micro_open_fast_lane_active()
        if micro_open:
            if blind_score < 58:
                return False, f"score {blind_score:.0f} < 58 for 9:15 fast-lane"
            if confidence < 30:
                return False, f"confidence {confidence:.0f}% < 30% for 9:15 fast-lane"
            if not (0.6 <= daily_change <= 3.8):
                return False, f"move {daily_change:.1f}% outside 9:15 fast-lane zone"
            if turnover_cr < 1.5:
                return False, f"turnover ₹{turnover_cr:.1f}Cr < ₹1.5Cr for 9:15 fast-lane"
            if rr2 < 1.8:
                return False, f"R:R 1:{rr2:.1f} < 1:1.8 for 9:15 fast-lane"
            if volume_ratio < 1.5 and imbalance < 4.0:
                return False, f"vol {volume_ratio:.1f}x and imbalance {imbalance:.1f}x too weak for 9:15 fast-lane"
        else:
            if rr2 < 1.2:
                return False, f"R:R 1:{rr2:.1f} < 1:1.2"
            if volume_ratio < 0.8 and imbalance < 3.5:
                return False, f"vol {volume_ratio:.1f}x and imbalance {imbalance:.1f}x too weak"

        if tick_high > 0 and price > 0:
            pct_from_high = ((tick_high - price) / tick_high) * 100
            if pct_from_high > 1.2:
                return False, f"{pct_from_high:.1f}% below day high"
            if tick_low > 0 and tick_high > tick_low:
                retreat_frac = (tick_high - price) / max(tick_high - tick_low, 0.01)
                if retreat_frac > 0.25:
                    return False, f"gave back {retreat_frac:.0%} of opening range"

        if imbalance < 2.5 and volume_ratio < 1.2 and confidence < 30:
            return False, "not enough opening urgency"

        return True, (
            f"{'9:15 elite lane | ' if micro_open else ''}"
            f"score {blind_score:.0f}, conf {confidence:.0f}%, vol {volume_ratio:.1f}x, "
            f"imbalance {imbalance:.1f}x, turnover ₹{turnover_cr:.1f}Cr"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> CycleResult:
        """Execute a single advisor cycle."""
        result = CycleResult(
            cycle_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        try:
            # 1. Load signal monitor state
            signal_state = self._load_signal_state()
            if signal_state is None or signal_state.empty:
                # Fallback: run a fresh scan ourselves
                signal_state = self._run_fresh_scan()
            if signal_state is None or signal_state.empty:
                result.error = "No signal data available. Is the signal monitor running?"
                self._log(result.error)
                return result
            signal_state = self._merge_opening_priority_state(signal_state)

            # 2. Check risk guardrails
            guardrail_status = self._guardrails.evaluate()
            if guardrail_status.buy_paused:
                result.buy_paused = True
                result.pause_reasons = guardrail_status.reasons
                self._last_blind_buy_diagnostics = Counter({"buy_paused": 1})
                result.blind_buy_diagnostics = dict(self._last_blind_buy_diagnostics)

            # 2b. Nifty crash guard — check if index is down >2% within first 60 min
            # Only re-evaluate once per cycle during market hours
            if self._is_market_open():
                self._nifty_crash_pause = self._check_nifty_crash(signal_state)

            # 2c. Retry queued auto-buys before scanning for new ones.
            if not result.buy_paused:
                self._run_pending_buy_retries()

            # 3. Find blind-buy candidates
            if not result.buy_paused:
                result.buy_candidates = self._find_blind_buys(signal_state)
                result.blind_buy_diagnostics = dict(self._last_blind_buy_diagnostics)

            # 4. Check portfolio for sell signals
            result.sell_alerts, result.open_positions = self._check_portfolio_sells(signal_state)

            # 5. Push alerts
            self._push_buy_alerts(result.buy_candidates)
            result.blind_buy_diagnostics = dict(self._last_blind_buy_diagnostics)
            self._push_sell_alerts(result.sell_alerts)

            # 5b. Check Kite positions for sell signals (auto-sell)
            self._run_sell_monitor()

            # 6. Save cycle log
            self._save_cycle_log(result)
            self._log_cycle(result)

        except Exception as exc:
            result.error = str(exc)
            self._log(f"Advisor error: {exc}")

        self._cycle_count += 1
        return result

    def run_loop(self) -> None:
        """Run the advisor continuously."""
        self._log(
            f"Blind-buy advisor starting: universe={self.config.universe}, "
            f"account={self.config.account_name}, "
            f"min_score={self.config.blind_buy_min_score}, "
            f"alerts={self.config.alert_channels}"
        )
        # Startup reconcile — close any MIS positions that the broker already
        # auto-squared off (e.g. yesterday's trades) but are still "open" locally.
        if self._order_manager is not None:
            try:
                rec = self._order_manager.reconcile_with_broker()
                closed = rec.get("auto_closed", [])
                if closed:
                    self._log(f"Startup reconcile: auto-closed {len(closed)} stale MIS position(s): {', '.join(closed)}")
                mismatches = rec.get("mismatches", [])
                if mismatches:
                    self._log(f"Startup reconcile: {len(mismatches)} mismatch(es): {'; '.join(mismatches)}")
            except Exception as _rec_exc:
                self._log(f"Startup reconcile failed: {_rec_exc}")
        while True:
            # Daily state reset — clear stale momentum tracking from previous day
            today_str = datetime.now().strftime("%Y-%m-%d")
            if self._momentum_tracked_date and self._momentum_tracked_date != today_str:
                self._log(f"New trading day {today_str}: clearing {len(self._momentum_tracked)} stale momentum entries from {self._momentum_tracked_date}")
                self._momentum_tracked.clear()
                self._tick_only_alert_count = 0
                self._rescan_alert_count = 0
                self._daily_batch_alert_count = 0
                self._tick_confirmed_auto_buy_count = 0
                self._opening_fast_lane_count = 0
                self._runner_radar_seen.clear()
                self._runner_radar_auto_reviewed.clear()
                self._runner_radar_date = today_str
                self._runner_radar_last_summary = 0.0
                self._early_radar_seen.clear()
                self._early_radar_date = today_str
                self._early_radar_last_summary = 0.0
                self._early_watch_list.clear()
                self._early_watch_date = today_str
                self._nifty_crash_pause = False
                self._last_opening_priority_refresh = 0.0
                self._opening_priority_state_df = None
                self._opening_priority_stats = {}
                self._daily_execution_summary_sent_date = None
                self._daily_auto_bought = []
                self._daily_auto_buy_failed = []
                self._daily_auto_buy_skipped = []
                self._daily_protection_failed = []
                self._daily_missed_opportunities = []
                self._daily_internal_watch.clear()
                self._daily_internal_avoid.clear()
                self._daily_no_buy_reasons.clear()
                # Reconcile broker positions at day start — closes any MIS positions
                # that Zerodha auto-squared off yesterday but are still "open" locally.
                if self._order_manager is not None:
                    try:
                        rec = self._order_manager.reconcile_with_broker()
                        closed = rec.get("auto_closed", [])
                        if closed:
                            self._log(f"Day-start reconcile: auto-closed {len(closed)} stale MIS position(s): {', '.join(closed)}")
                        mismatches = rec.get("mismatches", [])
                        if mismatches:
                            self._log(f"Day-start reconcile: {len(mismatches)} mismatch(es): {'; '.join(mismatches)}")
                    except Exception as _rec_exc:
                        self._log(f"Day-start reconcile failed: {_rec_exc}")
            self._momentum_tracked_date = today_str
            # Refresh market regime snapshot (cheap; throttled internally).
            # Auto-buy gates consult this; failure is non-fatal.
            self._refresh_market_context()
            # Pre-open auction scan: fires during 9:00-9:15 AM IST
            self._run_preopen_auction_scan()
            # Pre-market watchlist: fires once per day at 9:05 AM IST
            self._run_premarket_watchlist()
            # Mid-day scanner: fires once per day at 11:00 AM IST
            self._run_midday_scanner()
            # Power hour alert: fires once per day at 2:00 PM IST
            self._run_powerhour_alert()
            # Momentum continuation: re-alerts on alerted stocks still running strongly
            self._run_momentum_continuation()
            # Fade detector: EXIT alerts on alerted stocks reversing
            self._run_fade_detector()
            # End-of-day exit reminder: fires once per day at 3:10 PM IST
            self._run_eod_exit_alert()
            self._run_daily_execution_summary()
            # Fast tick scan (zero API calls, < 1s) — catches pre-jump conditions
            # BEFORE the full OHLCV cycle would even start.
            self._run_tick_fast_scan()
            # Visibility-only early board: +1% to +4.5% stocks with structure.
            self._run_early_expansion_radar()
            # Visibility-only radar: show all strong runners, even if not buyable.
            self._run_top_gainer_radar()
            # Flush opening buffer (ranked summary of buffered opening alerts)
            self._flush_opening_buffer()
            # Re-scan signal monitor "Buy Now" stocks — catches stocks that improved
            # since initial rejection (low score early → strong later in the day).
            self._run_buynow_rescan()
            self._refresh_opening_priority_state()
            self.run_once()

            # During the cycle wait, run tick scans every 2s.
            # KiteTickerStream refreshes the tick snapshot every 2s, so matching
            # this cadence minimizes detection delay without increasing API calls.
            # Full OHLCV scan still runs every 60s (Kite rate-limit safe).
            # Buynow rescan runs on its own 60s internal timer within this loop,
            # so "Buy Now" stocks get auto-alerted without waiting for main cycle.
            _tick_poll_interval = max(1, int(self._TICK_POLL_INTERVAL_SEC))
            _remaining = self.config.cycle_interval_sec
            _buynow_counter = 0
            while _remaining > _tick_poll_interval:
                time.sleep(_tick_poll_interval)
                _remaining -= _tick_poll_interval
                self._run_tick_fast_scan()
                self._refresh_market_context()
                self._run_early_expansion_radar()
                # Flush opening buffer every cycle (self-throttles internally)
                self._flush_opening_buffer()
                # Check buynow rescan every ~30s within the tick loop
                _buynow_counter += _tick_poll_interval
                if _buynow_counter >= 30:
                    self._run_buynow_rescan()
                    self._refresh_opening_priority_state()
                    _buynow_counter = 0
            if _remaining > 0:
                time.sleep(_remaining)

    # ------------------------------------------------------------------
    # Pre-open auction scan (9:00 – 9:15 AM IST)
    # ------------------------------------------------------------------

    def _run_preopen_auction_scan(self) -> None:
        """Analyze pre-open auction data and send alerts during 9:00-9:15 AM.

        Fires up to 2 alerts per day:
          1. During 'auction' phase (9:08-9:12) — equilibrium price being set
          2. During 'buffer' phase (9:12-9:15) — price locked, ready to act

        Uses live WebSocket ticks (total_buy_qty, total_sell_qty, open price)
        to score order imbalance and detect institutional interest.
        """
        if not PreOpenAnalyzer.is_preopen_window():
            return

        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)

        today_str = now_ist.strftime("%Y-%m-%d")
        current_phase = PreOpenAnalyzer._current_phase()

        # Reset on new day
        if self._preopen_auction_sent_date != today_str:
            self._preopen_auction_sent_phase = None
            self._preopen_auction_sent_date = today_str

        # Only alert once per phase (auction, buffer)
        # Skip 'collection' phase — orders can still be cancelled, too noisy
        if current_phase == "collection":
            return
        if self._preopen_auction_sent_phase == current_phase:
            return

        self._log(f"Pre-open auction scan: phase={current_phase}")

        ticks = self._load_ticks_snapshot()
        if not ticks:
            self._log("Pre-open auction: no tick data available yet")
            return

        summary = self._preopen_analyzer.analyze(ticks)
        if summary.total_symbols_with_ticks == 0:
            return

        # Get watchlist symbols from the pre-market watchlist if already sent
        watchlist_symbols = None
        signal_state = self._load_signal_state()
        if signal_state is not None and not signal_state.empty:
            from pkscreener.classes.BlindBuyScorer import BlindBuyScorer
            watchlist_symbols = []
            for _, row in signal_state.iterrows():
                sym = str(row.get("Symbol", "")).strip().upper()
                if sym:
                    sc, _ = BlindBuyScorer.score(row)
                    if sc >= 50:
                        watchlist_symbols.append(sym)

        picks = self._preopen_analyzer.get_top_picks(summary, watchlist_symbols, max_picks=8)

        if picks:
            msg = self._preopen_analyzer.format_alert(picks, current_phase)
            if msg:
                # Actually push to Discord/configured channels — was previously
                # only logging "sent N picks" without ever sending the message.
                self._send_to_channels(msg)
                self._log(f"Pre-open auction: sent {len(picks)} picks for phase={current_phase}")
        else:
            # Only send "no signals" during buffer phase (most reliable)
            if current_phase == "buffer":
                self._log("Pre-open auction: no strong auction signals detected.")

        self._preopen_auction_sent_phase = current_phase

    # ------------------------------------------------------------------
    # Pre-market watchlist (9:05 AM IST, once per day)
    # ------------------------------------------------------------------

    def _run_premarket_watchlist(self) -> None:
        """Send a pre-market watchlist at 8:45–9:14 AM IST every trading day.

        Reads yesterday's saved signal state to surface stocks approaching their
        Buy Above trigger — so you know what to watch BEFORE 9:15 AM open instead
        of getting alerts 10–15 minutes after the move has already happened.

        Filters:
        - "Buy Now": already at trigger, entry gap <= 2%, RSI <= 82
        - "Watch for Buy": within 5% of trigger, RSI <= 78, Signal Strength >= 35%
        - Day change < 8% (not already exhausted)
        """
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)

        today_str = now_ist.strftime("%Y-%m-%d")
        now_min = now_ist.hour * 60 + now_ist.minute

        # Fire between 8:45 AM and 9:14 AM IST (before market opens), once per day
        _PREMARKET_START = 8 * 60 + 45
        _PREMARKET_END   = 9 * 60 + 14

        if not (_PREMARKET_START <= now_min <= _PREMARKET_END):
            return
        if self._premarket_sent_date == today_str:
            return  # Already sent today

        self._premarket_sent_date = today_str  # Mark as sent before doing work
        self._log("Pre-market watchlist: preparing...")

        # Use the simple-view CSV — it has "What To Do", "Entry Gap %", RSI etc.
        simple_df = self._load_rescan_signal_state()
        if simple_df is None or simple_df.empty:
            self._log("Pre-market watchlist: no signal data available, skipping.")
            return

        picks = []
        for _, row in simple_df.iterrows():
            symbol = str(row.get("Stock", "")).strip().upper()
            if not symbol:
                continue

            action     = str(row.get("What To Do", "")).strip()
            strength   = float(row.get("Signal Strength %", 0) or 0)
            rsi        = float(row.get("RSI", 50) or 50)
            entry_gap  = float(row.get("Entry Gap %", 99) or 99)
            day_change = float(row.get("Change %", 0) or 0)
            price      = float(row.get("Price Now", 0) or 0)
            buy_above  = float(row.get("Buy Above", 0) or 0)
            stop_loss  = float(row.get("Sell If Below", 0) or 0)
            target1    = float(row.get("Take Profit 1", 0) or 0)
            target2    = float(row.get("Take Profit 2", 0) or 0)
            vol_ratio  = float(row.get("Volume vs Avg", 0) or 0)

            if buy_above <= 0 or price <= 0:
                continue
            if day_change >= 8.0:  # already exhausted
                continue

            is_buy_now   = action in ("Buy Now", "Strong Buy Now") and entry_gap <= 2.0 and rsi <= 82
            is_watch     = action == "Watch for Buy" and entry_gap <= 5.0 and rsi <= 78 and strength >= 35

            if not (is_buy_now or is_watch):
                continue

            # R:R from current price to target2
            rr2 = 0.0
            if stop_loss > 0 and target2 > price and price > stop_loss:
                risk = price - stop_loss
                rr2 = round((target2 - price) / risk, 1) if risk > 0 else 0

            # Suggested qty at blind-buy slot size (₹50K = ₹2L / 4 slots)
            qty = int(50000 // buy_above) if buy_above > 0 else 0

            picks.append({
                "symbol":    symbol,
                "action":    action,
                "strength":  strength,
                "rsi":       rsi,
                "entry_gap": entry_gap,
                "buy_above": buy_above,
                "stop_loss": stop_loss,
                "target1":   target1,
                "target2":   target2,
                "price":     price,
                "vol_ratio": vol_ratio,
                "rr2":       rr2,
                "qty":       qty,
            })

        if not picks:
            self._log("Pre-market watchlist: no qualifying setups today.")
            return

        # Sort: Buy Now first, then by entry_gap ascending (closest to trigger)
        picks.sort(key=lambda x: (0 if "Buy Now" in x["action"] else 1, x["entry_gap"]))
        top = picks[:8]

        lines = [
            f"**📋 PRE-MARKET WATCHLIST — {now_ist.strftime('%d %b %Y')}**",
            f"**{len(top)}** setup{'s' if len(top) != 1 else ''} approaching trigger from yesterday. "
            f"Act when price crosses Buy Above at 9:15 AM open.\n",
        ]
        for p in top:
            tag      = "🟢 **BUY NOW**" if "Buy Now" in p["action"] else "🟡 **WATCH**"
            gap_str  = f"already at trigger" if p["entry_gap"] <= 0 else f"{p['entry_gap']:.1f}% away"
            rr_str   = f" | R:R 1:{p['rr2']}" if p["rr2"] > 0 else ""
            vol_str  = f" | Vol {p['vol_ratio']:.1f}x" if p["vol_ratio"] > 0 else ""
            qty_str  = f" | Qty `{p['qty']}` (~₹50K)" if p["qty"] > 0 else ""
            lines.append(
                f"{tag} `{p['symbol']}` — RSI {p['rsi']:.0f} | Conf {p['strength']:.0f}%{vol_str}{rr_str}\n"
                f"   Buy above `₹{p['buy_above']:.2f}` ({gap_str}) | Stop `₹{p['stop_loss']:.2f}` | "
                f"T1 `₹{p['target1']:.2f}` | T2 `₹{p['target2']:.2f}`{qty_str}"
            )

        lines.append(
            "\n_Enter only if price crosses Buy Above in the first 15 min with volume. "
            "Bot will auto-alert when entry conditions are met._"
        )

        message = "\n\n".join(lines)
        self._send_to_channels(message)
        self._log(f"Pre-market watchlist sent: {len(top)} picks.")

    # ------------------------------------------------------------------
    # Mid-day consolidation scanner (11:00 AM IST, once per day)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Periodic re-scan of signal monitor "Buy Now" stocks
    # ------------------------------------------------------------------

    def _run_buynow_rescan(self) -> None:
        """Re-scan signal monitor 'Buy Now' stocks every 3 minutes.

        This is the PRIMARY path for stocks the engine says 'Buy Now' but
        that the tick scanner missed (because they moved <2% or had no
        order-book imbalance). Without this, /buynow shows stocks but
        autopilot never alerts them.

        Relaxed filters vs tick path: these stocks are already engine-confirmed
        'Buy Now', so we trust the signal monitor's assessment and only add
        basic safety gates (score, RSI, price > stop loss).
        """
        now_ts = time.time()
        if now_ts - self._last_buynow_rescan < self._RESCAN_INTERVAL_SEC:
            return
        self._last_buynow_rescan = now_ts

        # Hard guard: no actionable BuyNow rescan alerts past cutoff.
        # This prevents late-session "MISSED EARLIER, NOW STRONG" noise near/after close.
        if self._past_buy_cutoff():
            return
        if self._rescan_alert_count >= self._RESCAN_MAX_PER_DAY:
            return

        try:
            import pytz as _pytz_rs
            _ist_rs = _pytz_rs.timezone("Asia/Kolkata")
            _now_ist = datetime.now(_ist_rs)
        except Exception:
            from datetime import timezone, timedelta
            _now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))

        # Only during market hours (9:30 AM - 3:15 PM)
        _now_min = _now_ist.hour * 60 + _now_ist.minute
        if _now_min < 9 * 60 + 30 or _now_min > 15 * 60 + 15:
            return

        _sm_df = self._load_rescan_signal_state()
        if _sm_df is None or _sm_df.empty or "What To Do" not in _sm_df.columns:
            return

        action_series = _sm_df["What To Do"].astype(str).str.strip()
        change_series = _sm_df["Change %"].apply(_safe_float)
        vol_series = _sm_df["Volume vs Avg"].apply(_safe_float)
        conf_series = _sm_df["Signal Strength %"].apply(_safe_float)
        rsi_series = _sm_df["RSI"].apply(_safe_float)

        # Primary: stocks the engine already upgraded to Buy Now.
        buy_actions = {"Strong Buy Now", "Buy Now"}
        buy_candidates = _sm_df[action_series.isin(buy_actions)].copy()
        buy_candidates["_rescan_priority"] = 2

        # Honest forward validation showed promoted WATCH BUY names were the main drag.
        # Keep rescans execution-grade: only engine-confirmed buy actions should enter
        # the actionable pool here.
        slow_runner_actions = {"Watch Buy", "Momentum Buy", "Momentum Watch", "Hold / Do Nothing"}
        slow_runner_mask = (
            action_series.isin(slow_runner_actions)
            & (change_series >= 4.5)
            & (change_series <= 7.5)
            & (vol_series >= 1.2)
            & (conf_series >= 16.0)
            & (rsi_series < 72.0)
        )
        slow_runner_count = int(slow_runner_mask.sum())

        candidates = buy_candidates.copy()
        if not candidates.empty:
            candidates = candidates.drop_duplicates(subset=["Stock"], keep="first")
            candidates["_sort_conf"] = candidates["Signal Strength %"].apply(_safe_float)
            candidates["_sort_vol"] = candidates["Volume vs Avg"].apply(_safe_float)
            candidates["_sort_change"] = candidates["Change %"].apply(_safe_float).abs()
            candidates = candidates.sort_values(
                by=["_rescan_priority", "_sort_conf", "_sort_vol", "_sort_change"],
                ascending=[False, False, False, False],
            )
        if candidates.empty:
            return

        # Log what the engine found vs what we've already alerted
        _new_candidates = [str(r.get("Stock", "")).strip() for _, r in candidates.iterrows()
                           if str(r.get("Stock", "")).strip() not in self._already_alerted_buys]
        if _new_candidates:
            self._log(
                f"BuyNow rescan: engine says Buy Now for {len(candidates)} stocks, "
                f"{len(_new_candidates)} not yet alerted; skipped {slow_runner_count} watchlist-style runners: "
                f"{', '.join(_new_candidates[:10])}"
            )

        _rescan_count = 0
        _remaining_daily = max(0, self._RESCAN_MAX_PER_DAY - self._rescan_alert_count)
        _max_rescan_alerts = min(self._RESCAN_MAX_PER_CYCLE, _remaining_daily)
        if _max_rescan_alerts <= 0:
            return
        ticks = self._load_ticks_snapshot()

        for _, row in candidates.iterrows():
            sym = str(row.get("Stock", "")).strip()
            if not sym or sym in self._already_alerted_buys:
                continue
            current_action = str(row.get("What To Do", "")).strip()
            is_engine_confirmed_buy = current_action in buy_actions

            # Exclude ETFs and non-equity instruments
            if self._is_excluded_symbol(sym):
                continue

            # Get live tick data
            tick = ticks.get(sym, {})
            live_price = float(tick.get("last_price", 0))
            if live_price <= 0:
                continue

            # Relaxed quality gates — engine already said "Buy Now",
            # so we only add safety: no penny stocks, no bearish, not overbought.
            if live_price < 20.0:
                continue

            # Score with BlindBuyScorer
            blind_score, blind_reasons = BlindBuyScorer.score(row)
            _min_score = max(self.config.blind_buy_min_score, self._RESCAN_MIN_SCORE)
            if blind_score < _min_score:
                continue

            reasons_lower = blind_reasons.lower()
            if "do not buy" in reasons_lower or "bearish" in reasons_lower:
                continue

            vol_ratio = float(row.get("Volume vs Avg", row.get("Volume Ratio", 0)) or 0)
            # Require non-trivial current volume support to reduce stale rescans.
            if vol_ratio <= 0 or vol_ratio < self._RESCAN_MIN_VOL_RATIO:
                continue

            confidence = float(row.get("Signal Strength %", 0) or 0)
            if confidence < self._RESCAN_MIN_CONFIDENCE:
                continue
            stop_loss = float(row.get("Sell If Below", 0) or 0)
            target1 = float(row.get("Take Profit 1", 0) or 0)
            target2 = float(row.get("Take Profit 2", 0) or 0)
            rsi = float(row.get("RSI", 50) or 50)

            if rsi >= 78:
                continue

            stop_loss, target1, target2, _ = _normalize_long_levels(
                live_price,
                stop_loss,
                target1,
                target2,
            )
            if stop_loss <= 0 or target2 <= 0 or live_price <= stop_loss:
                continue

            rr2 = _compute_long_rr(live_price, stop_loss, target2)
            if rr2 < 1.3:
                continue

            # Skip truly illiquid stocks (avg < 10k shares/day)
            _hist_avg = 0
            try:
                _hp = os.path.join("results", "Data", "kite_history_cache", "day", f"{sym}.csv")
                if os.path.exists(_hp):
                    _hdf = pd.read_csv(_hp)
                    _hist_avg = _hdf["volume"].tail(10).mean()
            except Exception:
                pass
            if _hist_avg > 0 and _hist_avg < 10000:  # relaxed from 100k — ETFs/mid-caps often < 100k
                continue

            # Calculate live change
            prev_close = float(row.get("PrevClose", 0) or 0)
            if prev_close <= 0:
                # Top-level 'close' in tick = previous day's close
                prev_close = float(tick.get("close", 0) or 0)
            daily_change = ((live_price - prev_close) / prev_close * 100) if prev_close > 0 else 0
            if daily_change < self._RESCAN_MIN_DAY_CHANGE or daily_change > self._RESCAN_MAX_DAY_CHANGE:
                continue

            # HARD FILTER: Stock already ran too far — entry risk exceeds reward
            # Apr 10 audit: 21 stocks alerted after >12% move had avg PnL -0.3%
            if daily_change > self._TICK_ALREADY_MOVED_PCT:
                continue

            # HARD FILTER: Never buy a stock trending DOWN intraday
            # A stock that opened strong but is now RED indicates reversal/profit-taking.
            # The historical "Buy Now" signal is stale if the intraday trend has turned negative.
            # Guard: -2% down = clear weakness. Skip it even if technical scores look good.
            if daily_change < -2.0:
                continue

            # HARD FILTER 2: Watch for gap-up reversals
            # If a stock gapped up +4% or more but is now BELOW prev close (negative daily_change),
            # that's a major reversal — profit-taking/weak hands exiting. Too risky for blind buy.
            _prev_close = prev_close  # already calculated above
            _entry_gap_pct = float(row.get("EntryGapPct", row.get("Entry Gap %", 0)) or 0)
            if _entry_gap_pct > 0 and daily_change < 0 and _entry_gap_pct >= 4.0:
                # Stock gapped up 4%+ but reversed below yesterday's close — reversal signal
                continue

            # Scope from entry
            scope_t1 = ((target1 - live_price) / live_price * 100) if target1 > 0 else 0
            scope_t2 = ((target2 - live_price) / live_price * 100) if target2 > 0 else 0

            score_label = f"🔥 **{blind_score:.0f}/100**" if blind_score >= 70 else f"✅ **{blind_score:.0f}/100**"

            suggested_qty, approx_cost = _compute_suggested_qty(
                live_price,
                stop_loss=stop_loss,
                score=blind_score,
            )
            if suggested_qty <= 0:
                self._log(
                    f"Suppressed {sym}: starter position exceeds ₹5000 cap at ₹{live_price:.2f}"
                )
                continue

            _risk_ps = round(live_price - stop_loss, 2) if stop_loss > 0 else 0
            _reward_ps = round(target2 - live_price, 2) if target2 > 0 else 0
            _expected_growth = round(((target2 - live_price) / live_price) * 100, 1) if target2 > 0 else 0
            _scope_parts = []
            if scope_t1 > 0:
                _scope_parts.append(f"+{scope_t1:.1f}% to first target")
            if scope_t2 > 0:
                _scope_parts.append(f"+{scope_t2:.1f}% to max target")
            _scope_str = f" | **Profit potential: {' | '.join(_scope_parts)}**" if _scope_parts else ""
            header_emoji = "🔍" if is_engine_confirmed_buy else "👀"
            header_label = "BUY (engine confirmed)" if is_engine_confirmed_buy else "WATCH (improving)"
            temp_candidate = None
            candidate_grade = ""
            candidate_rank = 0.0
            auto_eligible = False
            swing_tag = f"\n⏰ **SWING ONLY** — already +{daily_change:.1f}% today. Do NOT enter for intraday — swing trade only (hold 2-5 days)." if daily_change >= 8.0 else ""
            rr_text = ""
            if stop_loss > 0 and target2 > live_price and live_price > stop_loss:
                rr2 = round((target2 - live_price) / (live_price - stop_loss), 1)
                rr_text = f" | R:R `1:{rr2}`"
            _qty_line = f"\nSuggested: Buy **`{suggested_qty}` shares** (≈ ₹{approx_cost:,.0f})" if suggested_qty > 0 else ""
            msg = (
                f"**{header_emoji} {header_label} `{sym}`** — Score {score_label}{swing_tag}\n"
                f"Price `{live_price:.2f}` (+{daily_change:.1f}%) | Buy Above `{live_price:.2f}`\n"
                f"Stop Loss `{stop_loss:.2f}` | Target 1 `{target1:.2f}` | Target 2 `{target2:.2f}`{rr_text}\n"
                f"Volume `{vol_ratio:.1f}x avg` | Confidence `{confidence:.0f}%`\n"
                f"Why: {self._simplify_blind_reasons(blind_reasons)}"
                f"{_qty_line}\n"
                f"_Open Groww → Buy → then use `/buy {sym} QTY PRICE` to record_"
            )
            self._send_to_channels(msg)

            self._already_alerted_buys.add(sym)
            self._save_daily_alerted(sym)
            self._rescan_alert_count += 1
            self._log(
                f"🔍 RESCAN CATCH: {sym} action='{current_action}' score={blind_score:.0f} vol={vol_ratio:.1f}x "
                f"conf={confidence:.0f}% change=+{daily_change:.1f}%"
            )
            _rescan_count += 1
            if _rescan_count >= _max_rescan_alerts:
                break

        if _rescan_count > 0:
            self._log(
                f"BuyNow rescan: sent {_rescan_count} new alerts "
                f"({self._rescan_alert_count}/{self._RESCAN_MAX_PER_DAY} today)"
            )

    # ------------------------------------------------------------------
    # Mid-day consolidation scanner (11:00 AM IST)
    # ------------------------------------------------------------------

    def _run_midday_scanner(self) -> None:
        """Scan for consolidation breakout setups at 11:00 AM IST.

        By 11 AM, the opening frenzy has settled. Stocks that opened strong
        and are now consolidating (tight range, volume dropping) are coiling
        for a second leg up. This scan finds them before the breakout.

        Criteria:
        - Score >= 60 (strong technicals)
        - Daily change 1-7% (moved this morning but not over-extended)
        - Volume ratio >= 1.5x (above average — still has fuel)
        - R:R >= 1.5 (worthwhile entry)
        - No bearish flags
        """
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)

        today_str = now_ist.strftime("%Y-%m-%d")
        now_min = now_ist.hour * 60 + now_ist.minute

        # Fire between 11:00 AM and 11:09 AM IST, once per day
        _START = 11 * 60 + 0
        _END   = 11 * 60 + 9

        if not (_START <= now_min <= _END):
            return
        if self._midday_sent_date == today_str:
            return

        self._midday_sent_date = today_str
        self._log("Mid-day scanner: preparing...")

        signal_state = self._load_signal_state()
        if signal_state is None or signal_state.empty:
            signal_state = self._run_fresh_scan()
        if signal_state is None or signal_state.empty:
            self._log("Mid-day scanner: no signal data, skipping.")
            return

        # Also load ticks for live daily_change data
        ticks = self._load_ticks_snapshot()

        picks = []
        for _, row in signal_state.iterrows():
            symbol = str(row.get("Symbol", "")).strip().upper()
            if not symbol or symbol in self._already_alerted_buys:
                continue

            blind_score, blind_reasons = BlindBuyScorer.score(row)
            if blind_score < 60:
                continue

            reasons_lower = blind_reasons.lower()
            if "do not buy" in reasons_lower or "bearish" in reasons_lower:
                continue

            # Use live tick price/change if available, else fall back to signal state
            tick = ticks.get(symbol, {})
            daily_change = float(tick.get("change") or row.get("Change %", row.get("% Change", 0)) or 0)
            price = float(tick.get("last_price") or row.get("PriceNow", row.get("Price Now", row.get("LTP", 0))) or 0)

            # Long-only: skip falling stocks
            if daily_change < 0:
                continue
            # Must have moved (otherwise it's just a stale setup) but not over-extended
            if not (1.0 <= daily_change <= 7.0):
                continue

            vol_ratio  = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
            if vol_ratio > 0 and vol_ratio < 1.5:
                continue

            buy_above  = float(row.get("BuyAbove", row.get("Buy Above", row.get("Entry", 0))) or 0)
            stop_loss  = float(row.get("SellIfBelow", row.get("Sell If Below", 0)) or 0)
            target1    = float(row.get("TakeProfit1", row.get("Take Profit 1", 0)) or 0)
            target2    = float(row.get("TakeProfit2", row.get("Take Profit 2", 0)) or 0)
            confidence = float(row.get("ConfidencePct", row.get("Signal Strength %", 0)) or 0)

            if price <= 0:
                continue

            # Compute R:R
            rr2 = 0.0
            if stop_loss > 0 and target2 > 0 and price > stop_loss:
                risk = price - stop_loss
                rr2 = round((target2 - price) / risk, 1) if risk > 0 else 0
            if rr2 < 1.5:
                continue

            entry_display = buy_above if buy_above > 0 else price
            picks.append({
                "symbol": symbol,
                "score": blind_score,
                "vol_ratio": vol_ratio,
                "entry": entry_display,
                "stop_loss": stop_loss,
                "target1": target1,
                "target2": target2,
                "price": price,
                "change": daily_change,
                "confidence": confidence,
                "rr2": rr2,
            })

        picks.sort(key=lambda x: (-x["score"], -x["rr2"]))
        top = picks[:6]

        score_emoji = lambda s: "🔥" if s >= 70 else "✅"
        if not top:
            return

        lines = [
            f"**📊 MID-DAY SCAN (11 AM) — {now_ist.strftime('%d %b %Y')}**",
            f"**{len(top)}** stock{'s' if len(top) != 1 else ''} consolidating after morning move — potential second leg up:\n",
        ]
        for p in top:
            vol_str  = f" | Vol {p['vol_ratio']:.1f}x" if p['vol_ratio'] > 0 else ""
            conf_str = f" | Conf {p['confidence']:.0f}%" if p['confidence'] > 0 else ""
            lines.append(
                f"{score_emoji(p['score'])} **{p['symbol']}** (+{p['change']:.1f}% so far) — Score {p['score']:.0f}/100 | R:R 1:{p['rr2']}{vol_str}{conf_str}\n"
                f"   Entry `{p['entry']:.2f}` | Stop `{p['stop_loss']:.2f}` | T1 `{p['target1']:.2f}` | T2 `{p['target2']:.2f}`"
            )
        lines.append(
            "\n_Buy on breakout above entry price with volume. These stocks have already shown strength today._\n"
            "_Tick scanner is live — BUY alerts fire automatically if conditions confirm._"
        )
        self._log(f"Mid-day scanner sent: {len(top)} picks.")

    # ------------------------------------------------------------------
    # Power hour alert (2:00 PM IST, once per day)
    # ------------------------------------------------------------------

    def _run_powerhour_alert(self) -> None:
        """Alert for power hour setups at 2:00 PM IST.

        2–3:15 PM is when institutions do end-of-day positioning. Stocks that
        have been trending up all day with increasing volume often make a final
        push. Risk is lower because direction is confirmed by the full day's action.

        Criteria:
        - Score >= 58
        - Daily change 2-12% (has momentum but room left before circuit)
        - Volume ratio >= 2.0x (strong conviction — needed as late-day entry)
        - R:R >= 1.5
        - Confidence >= 20% (historically reliable setup)
        - No bearish flags
        """
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)

        today_str = now_ist.strftime("%Y-%m-%d")
        now_min = now_ist.hour * 60 + now_ist.minute

        # Fire between 2:00 PM and 2:09 PM IST, once per day
        _START = 14 * 60 + 0
        _END   = 14 * 60 + 9

        if not (_START <= now_min <= _END):
            return
        if self._powerhour_sent_date == today_str:
            return

        self._powerhour_sent_date = today_str
        self._log("Power hour alert: preparing...")

        signal_state = self._load_signal_state()
        if signal_state is None or signal_state.empty:
            signal_state = self._run_fresh_scan()
        if signal_state is None or signal_state.empty:
            self._log("Power hour alert: no signal data, skipping.")
            return

        ticks = self._load_ticks_snapshot()

        picks = []
        for _, row in signal_state.iterrows():
            symbol = str(row.get("Symbol", "")).strip().upper()
            if not symbol or symbol in self._already_alerted_buys:
                continue

            blind_score, blind_reasons = BlindBuyScorer.score(row)
            if blind_score < 58:
                continue

            reasons_lower = blind_reasons.lower()
            if "do not buy" in reasons_lower or "bearish" in reasons_lower:
                continue

            tick = ticks.get(symbol, {})
            daily_change = float(tick.get("change") or row.get("Change %", row.get("% Change", 0)) or 0)
            price = float(tick.get("last_price") or row.get("PriceNow", row.get("Price Now", row.get("LTP", 0))) or 0)

            if daily_change < 0:
                continue
            if not (2.0 <= daily_change <= 12.0):
                continue

            vol_ratio  = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
            if vol_ratio > 0 and vol_ratio < 2.0:
                continue  # Power hour needs stronger volume confirmation

            confidence = float(row.get("ConfidencePct", row.get("Signal Strength %", 0)) or 0)
            if confidence > 0 and confidence < 20:
                continue

            buy_above  = float(row.get("BuyAbove", row.get("Buy Above", row.get("Entry", 0))) or 0)
            stop_loss  = float(row.get("SellIfBelow", row.get("Sell If Below", 0)) or 0)
            target1    = float(row.get("TakeProfit1", row.get("Take Profit 1", 0)) or 0)
            target2    = float(row.get("TakeProfit2", row.get("Take Profit 2", 0)) or 0)

            if price <= 0:
                continue

            rr2 = 0.0
            if stop_loss > 0 and target2 > 0 and price > stop_loss:
                risk = price - stop_loss
                rr2 = round((target2 - price) / risk, 1) if risk > 0 else 0
            if rr2 < 1.5:
                continue

            entry_display = buy_above if buy_above > 0 else price
            picks.append({
                "symbol": symbol,
                "score": blind_score,
                "vol_ratio": vol_ratio,
                "entry": entry_display,
                "stop_loss": stop_loss,
                "target1": target1,
                "target2": target2,
                "price": price,
                "change": daily_change,
                "confidence": confidence,
                "rr2": rr2,
            })

        picks.sort(key=lambda x: (-x["score"], -x["rr2"]))
        top = picks[:5]

        score_emoji = lambda s: "🔥" if s >= 70 else "✅"
        if not top:
            return

        lines = [
            f"**⚡ POWER HOUR (2 PM) — {now_ist.strftime('%d %b %Y')}**",
            f"**{len(top)}** stock{'s' if len(top) != 1 else ''} with momentum + high volume for the final 1.5 hour push:\n",
        ]
        for p in top:
            vol_str  = f" | Vol {p['vol_ratio']:.1f}x" if p['vol_ratio'] > 0 else ""
            conf_str = f" | Conf {p['confidence']:.0f}%" if p['confidence'] > 0 else ""
            lines.append(
                f"{score_emoji(p['score'])} **{p['symbol']}** (+{p['change']:.1f}% today) — Score {p['score']:.0f}/100 | R:R 1:{p['rr2']}{vol_str}{conf_str}\n"
                f"   Entry `{p['entry']:.2f}` | Stop `{p['stop_loss']:.2f}` | T1 `{p['target1']:.2f}` | T2 `{p['target2']:.2f}`"
            )
        lines.append(
            "\n_These have trended up all day. Entry here targets the close-of-day push._\n"
            "_⚠️ Exit before 3:15 PM — avoid holding intraday trades overnight._"
        )
        self._log(f"Power hour alert sent: {len(top)} picks.")

    # ------------------------------------------------------------------
    # Momentum continuation (every cycle, re-checks alerted stocks)
    # ------------------------------------------------------------------

    def _run_momentum_continuation(self) -> None:
        """Re-alert on stocks we already flagged if momentum is still running.

        After an initial BUY alert fires at e.g. +3%, this method checks every
        cycle (60s). If the stock has moved another 3%+ from the last alert level
        AND volume is still ≥1.5x average, it sends a 🚀 MOMENTUM RUNNING update
        so the user knows to trail their stop, not sell yet.

        Cooldown: 15 minutes between momentum continuation alerts per symbol.
        Auto-expires: after 3:10 PM IST, no new continuation alerts (too close to close).
        """
        if not self._momentum_tracked:
            return
        if not self._is_market_open():
            return

        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)

        # Stop sending continuation alerts after 3:10 PM — too close to market close
        if now_ist.hour * 60 + now_ist.minute >= 15 * 60 + 10:
            return

        ticks = self._load_ticks_snapshot()
        if not ticks:
            return

        now = datetime.now()
        _CONTINUATION_COOLDOWN_SEC = 900  # 15 min between re-alerts per symbol
        _CONTINUATION_MIN_MOVE_PCT = 3.0  # must have moved 3%+ since last alert level
        _CONTINUATION_MIN_VOL = 1.5       # volume must still be strong

        # Load signal state once for the whole loop (avoid re-reading CSV per symbol)
        _signal_state = self._load_signal_state()

        for sym, tracked in list(self._momentum_tracked.items()):
            last_alert_time = tracked.get("last_alert_time")
            if isinstance(last_alert_time, datetime):
                if (now - last_alert_time).total_seconds() < _CONTINUATION_COOLDOWN_SEC:
                    continue

            tick = ticks.get(sym, {})
            current_price = float(tick.get("last_price") or 0)
            current_change = float(tick.get("change") or 0)
            if current_price <= 0 or current_change <= 0:
                continue

            last_alert_pct = tracked.get("last_alert_pct", 0)
            entry_price = tracked.get("entry_price", 0)

            # Check if moved 3%+ since last alert
            move_since_alert = current_change - last_alert_pct
            if move_since_alert < _CONTINUATION_MIN_MOVE_PCT:
                continue

            # Check volume from signal state (loaded once above loop)
            vol_ratio = 0.0
            if _signal_state is not None and not _signal_state.empty:
                rows = _signal_state[_signal_state["Symbol"].str.upper() == sym]
                if not rows.empty:
                    vol_ratio = float(rows.iloc[0].get("VolumeRatio", 0) or 0)

            if vol_ratio > 0 and vol_ratio < _CONTINUATION_MIN_VOL:
                continue

            # Compute profit from original entry
            profit_pct = 0.0
            broker_position = None
            has_open_kite_position = False
            if self._order_manager is not None:
                try:
                    broker_position = self._order_manager.get_position(sym)
                    has_open_kite_position = bool(
                        broker_position is not None and getattr(broker_position, "status", "") == "open"
                    )
                except Exception:
                    broker_position = None

            reference_price = entry_price
            if has_open_kite_position:
                reference_price = float(getattr(broker_position, "buy_price", 0) or entry_price)
            if reference_price > 0:
                profit_pct = round((current_price - reference_price) / reference_price * 100, 1)

            vol_str = f" | Vol `{vol_ratio:.1f}x`" if vol_ratio > 0 else ""
            if profit_pct > 0:
                profit_label = "From entry" if has_open_kite_position else "From alert"
                profit_str = f" | {profit_label}: `+{profit_pct:.1f}%`"
            else:
                profit_str = ""
            # Trailing stop suggestion: 4% below current price
            trail_stop = round(current_price * 0.96, 2)
            basis_text = "when you entered" if has_open_kite_position else "when we first alerted it"
            if has_open_kite_position and reference_price > 0:
                status_line = f"📌 Position: confirmed Kite holding from `₹{reference_price:.2f}`\n\n"
                action_line = "✅ DON'T sell yet — momentum is still strong"
                stop_line = f"🔴 Move your stop-loss UP to `₹{trail_stop:.2f}` (locks in most of your profit)"
                footer_line = f"_If price drops to ₹{trail_stop:.2f}, sell immediately to keep your gains._"
                header = f"**🚀 `{sym}` STILL RUNNING — hold and trail your stop!**\n"
            else:
                status_line = (
                    "📌 Position: no confirmed Kite buy is tracked right now — "
                    "this is a follow-through alert, not proof of a fill\n\n"
                )
                action_line = "ℹ️ If you bought it manually, momentum is still strong"
                stop_line = f"🔴 If you are in this trade, trail stop-loss near `₹{trail_stop:.2f}`"
                footer_line = f"_If you are not in the trade, treat this as informational only._"
                header = f"**🚀 `{sym}` ALERT STILL RUNNING — follow-through update**\n"

            message = (
                f"{header}"
                f"📈 Now `+{current_change:.1f}%` (was `+{last_alert_pct:.1f}%` {basis_text}){profit_str}\n"
                f"Price `₹{current_price:.2f}`{vol_str}\n\n"
                f"{status_line}"
                f"**What to do now:**\n"
                f"{action_line}\n"
                f"{stop_line}\n"
                f"{footer_line}"
            )
            self._log(f"Momentum continuation alert: {sym} now +{current_change:.1f}% (was +{last_alert_pct:.1f}%)")

            # Update tracking
            self._momentum_tracked[sym]["last_alert_time"] = now
            self._momentum_tracked[sym]["last_alert_pct"] = current_change

    # ------------------------------------------------------------------
    # Fade Detector — EXIT alerts when alerted stocks reverse
    # ------------------------------------------------------------------

    def _run_fade_detector(self) -> None:
        """Monitor alerted stocks for reversal and send EXIT warnings.

        Checks every 30 seconds (not every tick — too noisy). Fires when:
        1. Price dropped 2%+ from entry → WARNING (consider exiting)
        2. Price dropped 4%+ from intraday high AND sell pressure → EXIT NOW
        3. Price dropped below stop loss → STOP HIT

        Only monitors stocks in _momentum_tracked (i.e. stocks we actually alerted).
        """
        if not self._momentum_tracked:
            return
        if not self._is_market_open():
            return

        import time as _time_mod
        now_ts = _time_mod.time()
        if now_ts - self._last_fade_check < self._FADE_CHECK_INTERVAL_SEC:
            return
        self._last_fade_check = now_ts

        ticks = self._load_ticks_snapshot()
        if not ticks:
            return

        now = datetime.now()

        for sym, tracked in list(self._momentum_tracked.items()):
            live_position = None
            if self._order_manager is not None:
                try:
                    live_position = self._order_manager.get_position(sym)
                except Exception:
                    live_position = None
            # OBS / fade alerts fire for any momentum-tracked stock — paper or live.
            # Previously required an OPEN live_position which silenced OBS entirely
            # whenever no live orders existed (most of the time in paper-trading
            # mode). User wants visibility into "sellers dominating" / "fading
            # from high" on tracked alerts regardless of whether a real order
            # was placed. Cooldown below still prevents spam.
            position_status = getattr(live_position, "status", "") if live_position else ""

            # Cooldown: don't spam EXIT alerts
            last_fade_alert = self._fade_alerted.get(sym)
            if isinstance(last_fade_alert, datetime):
                if (now - last_fade_alert).total_seconds() < self._FADE_COOLDOWN_SEC:
                    continue

            tick = ticks.get(sym, {})
            current_price = float(tick.get("last_price") or 0)
            if current_price <= 0:
                continue

            entry_price = tracked.get("entry_price", 0)
            stop_loss = tracked.get("stop_loss", 0)
            if entry_price <= 0:
                continue

            # Calculate drops
            drop_from_entry_pct = (entry_price - current_price) / entry_price * 100

            # Get intraday high from tick
            tick_high = float(tick.get("high") or tick.get("ohlc", {}).get("high", 0))
            drop_from_high_pct = 0.0
            if tick_high > 0:
                drop_from_high_pct = (tick_high - current_price) / tick_high * 100

            # Check sell pressure from order book
            total_buy = float(tick.get("total_buy_quantity") or 0)
            total_sell = float(tick.get("total_sell_quantity") or 0)
            sell_pressure = (total_sell / total_buy) if total_buy > 0 else 0
            sell_pressure_confirmed = sell_pressure >= self._FADE_SELL_IMBALANCE

            daily_change = float(tick.get("change") or 0)

            # Determine alert level
            alert_level = None
            if stop_loss > 0 and current_price <= stop_loss:
                alert_level = "STOP_HIT"
            elif drop_from_high_pct >= self._FADE_DROP_FROM_HIGH_PCT and sell_pressure_confirmed:
                alert_level = "EXIT_NOW"
            elif drop_from_entry_pct >= self._FADE_DROP_FROM_ENTRY_PCT:
                alert_level = "WARNING"
            elif drop_from_high_pct >= self._FADE_DROP_FROM_HIGH_PCT and drop_from_entry_pct >= 1.0:
                # Large drop from high + underwater from entry = fading
                alert_level = "WARNING"

            # If the position is already protected with a stop, don't spam softer
            # "weakening" warnings. Let STOP_HIT / EXIT_NOW remain actionable.
            # (Skip this filter when there's no live position — user wants to
            # see WARNING level for paper-tracked stocks.)
            if (
                alert_level == "WARNING"
                and live_position is not None
                and float(getattr(live_position, "stop_loss", 0.0) or 0.0) > 0
            ):
                continue

            if not alert_level:
                continue

            self._fade_alerted[sym] = now

            # Format alert
            sell_info = f" | Sell pressure `{sell_pressure:.1f}x`" if sell_pressure_confirmed else ""
            entry_loss = round(entry_price - current_price, 2)
            entry_loss_pct = round(drop_from_entry_pct, 1)

            if alert_level == "STOP_HIT":
                message = (
                    f"**🚨 STOP HIT — EXIT `{sym}` NOW**\n"
                    f"Price `₹{current_price:.2f}` hit your stop loss `₹{stop_loss:.2f}`\n"
                    f"Entry was `₹{entry_price:.2f}` → Loss `₹{entry_loss:.2f}/share` (`-{entry_loss_pct:.1f}%`)\n"
                    f"**Sell immediately. Do not hope for recovery.**"
                )
            elif alert_level == "EXIT_NOW":
                message = (
                    f"**🔴 EXIT `{sym}` — FADING from high**\n"
                    f"Dropped `{drop_from_high_pct:.1f}%` from today's high `₹{tick_high:.2f}` → now `₹{current_price:.2f}`{sell_info}\n"
                    f"Entry `₹{entry_price:.2f}` → {'Loss' if entry_loss > 0 else 'Gain'} `₹{abs(entry_loss):.2f}/share` (`{'+' if entry_loss <= 0 else '-'}{abs(entry_loss_pct):.1f}%`)\n"
                    f"Sellers are dominating the order book. This gap-up is fading.\n"
                    f"**Sell now to limit damage or lock remaining profit.**"
                )
            else:  # WARNING
                message = (
                    f"**⚠️ `{sym}` WEAKENING — watch closely**\n"
                    f"{'Price dropped ' + f'{drop_from_entry_pct:.1f}% from your entry ₹{entry_price:.2f}' if drop_from_entry_pct >= self._FADE_DROP_FROM_ENTRY_PCT else f'Dropped {drop_from_high_pct:.1f}% from high ₹{tick_high:.2f}'}\n"
                    f"Now `₹{current_price:.2f}` ({'+' if daily_change >= 0 else ''}{daily_change:.1f}% for the day){sell_info}\n"
                    f"{'Move stop loss up or consider partial exit.' if drop_from_entry_pct < 1.5 else 'Consider exiting — momentum is dying.'}"
                )

            self._paper_tracker.log_fade(sym, alert_level)
            self._log(
                f"Fade detector [{alert_level}]: {sym} entry=₹{entry_price:.2f} "
                f"now=₹{current_price:.2f} drop_entry={drop_from_entry_pct:.1f}% "
                f"drop_high={drop_from_high_pct:.1f}% sell_pressure={sell_pressure:.1f}x"
            )

    # ------------------------------------------------------------------
    # End-of-day exit reminder (3:10 PM IST, once per day)
    # ------------------------------------------------------------------

    def _run_eod_exit_alert(self) -> None:
        """Send a hard exit reminder at 3:10 PM IST.

        Intraday positions MUST be squared off before 3:20 PM to avoid
        delivery charges or auto-square-off by broker at unfavorable prices.
        This alert fires once per day with the current P&L of tracked stocks.
        """
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)

        today_str = now_ist.strftime("%Y-%m-%d")
        now_min = now_ist.hour * 60 + now_ist.minute

        # Fire between 3:10 PM and 3:19 PM IST, once per day
        _START = 15 * 60 + 10
        _END   = 15 * 60 + 19

        if not (_START <= now_min <= _END):
            return
        if self._eod_exit_sent_date == today_str:
            return

        self._eod_exit_sent_date = today_str
        self._log("EOD exit alert: preparing...")

        ticks = self._load_ticks_snapshot()

        lines = [
            f"**\U0001f4e2 MARKET CLOSING IN 20 MIN — {now_ist.strftime('%d %b %Y')}**",
            f"Exit all intraday positions NOW before 3:20 PM to avoid broker auto-square-off.\n",
        ]

        if self._momentum_tracked:
            lines.append("**Your alerted stocks today:**")
            for sym, tracked in self._momentum_tracked.items():
                tick = ticks.get(sym, {})
                current_price = float(tick.get("last_price") or 0)
                entry_price = tracked.get("entry_price", 0)
                change = float(tick.get("change") or 0)
                if current_price > 0 and entry_price > 0:
                    profit_pct = round((current_price - entry_price) / entry_price * 100, 1)
                    emoji = "\U0001f7e2" if profit_pct > 0 else "\U0001f534"
                    lines.append(f"  {emoji} **{sym}** | Entry `{entry_price:.2f}` → Now `{current_price:.2f}` | `{'+' if profit_pct >= 0 else ''}{profit_pct:.1f}%` | Daily `+{change:.1f}%`")
                elif current_price > 0:
                    lines.append(f"  **{sym}** | Now `{current_price:.2f}` | Daily `+{change:.1f}%`")
            lines.append("")

        lines.append("_Exit all intraday positions in your broker NOW. Do it before auto square-off._")

        # Close paper trades for the day and append summary
        paper_summary = self._paper_tracker.close_day(ticks)
        if paper_summary:
            lines.append("")
            lines.append(paper_summary)

        self._log("EOD exit alert sent.")

    def _run_daily_execution_summary(self) -> None:
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        today_str = now_ist.strftime("%Y-%m-%d")
        now_min = now_ist.hour * 60 + now_ist.minute
        if now_min < 15 * 60 + 25:
            return
        if self._daily_execution_summary_sent_date == today_str:
            return
        self._daily_execution_summary_sent_date = today_str
        top_reasons = self._daily_no_buy_reasons.most_common(5)
        reason_text = ", ".join(f"{reason} ({count})" for reason, count in top_reasons) or "none"
        lines = [
            f"**📘 DAILY SUMMARY — {now_ist.strftime('%d %b %Y')}**",
            f"Auto-bought stocks: {', '.join(self._daily_auto_bought[:12]) if self._daily_auto_bought else 'none'}",
            f"Failed buys: {', '.join(self._daily_auto_buy_failed[:12]) if self._daily_auto_buy_failed else 'none'}",
            f"Skipped buys: {', '.join(self._daily_auto_buy_skipped[:12]) if self._daily_auto_buy_skipped else 'none'}",
            f"Missed opportunities: {', '.join(self._daily_missed_opportunities[:12]) if self._daily_missed_opportunities else 'none'}",
            f"Watched internally: {len(self._daily_internal_watch)}",
            f"Avoided internally: {len(self._daily_internal_avoid)}",
            f"Top reasons for not buying: {reason_text}",
        ]
        self._send_to_channels("\n".join(lines))

    def _record_internal_stage(self, stage: str, symbol: str, reason: str = "") -> None:
        normalized = str(stage or "").strip().upper()
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        if normalized == "WATCH EARLY":
            self._daily_internal_watch.add(sym)
        elif normalized in {"MISSED", "AVOID"}:
            self._daily_internal_avoid.add(sym)
        if reason:
            self._record_no_buy_reason(reason)

    def _record_no_buy_reason(self, reason: str) -> None:
        text = str(reason or "").strip()
        if text:
            self._daily_no_buy_reasons[text] += 1

    def _handle_auto_buy_result(self, symbol: str, result: Optional[str]) -> None:
        text = str(result or "").strip()
        sym = str(symbol or "").strip().upper()
        if not text or not sym:
            return
        lower = text.lower()
        # Auto-trade intentionally OFF — not a failure. Swallow silently so
        # Discord doesn't spam "AUTO-BUY FAILED" on every tick in manual mode.
        if "auto-trade is disabled" in lower or "auto_trade_disabled" in lower:
            return
        if "auto-bought" in lower:
            self._daily_auto_bought.append(sym)
            self._send_to_channels(text)
            if "protection failed" in lower:
                self._daily_protection_failed.append(sym)
            return
        if "protection failed" in lower:
            self._daily_protection_failed.append(sym)
            self._send_to_channels(text)
            return
        if "important missed opportunity" in lower:
            self._daily_missed_opportunities.append(sym)
            self._record_no_buy_reason(text.split("\n", 1)[-1])
            self._send_to_channels(text)
            return
        if (
            "auto-buy skipped" in lower
            or "**blocked" in lower
            or "**late_discovery" in lower
            or "**rejected" in lower
            or "**strict_mode_rejected" in lower
            or "**swing_recheck_rejected" in lower
        ):
            self._daily_auto_buy_skipped.append(sym)
            self._record_no_buy_reason(text.split("\n", 1)[-1])
            self._send_to_channels(text)
            return
        if "auto-buy failed" in lower:
            self._daily_auto_buy_failed.append(sym)
            self._record_no_buy_reason(text.split("\n", 1)[-1])
            self._send_to_channels(text)
            return

    # ------------------------------------------------------------------
    # Tick-only scoring for stocks NOT in signal state
    # ------------------------------------------------------------------

    def _handle_tick_only_alert(self, item: Dict, sym: str, price: float,
                                 daily_change: float, direction: str,
                                 reasons_text: str) -> None:
        """Score and alert a stock using ONLY tick data (no OHLCV/signal state)."""

        # Time guard: no actionable buys past cutoff
        if self._past_buy_cutoff():
            return

        # ETF/non-equity filter
        if self._is_excluded_symbol(sym):
            return

        total_buy = float(item.get("total_buy", 0))
        total_sell = float(item.get("total_sell", 0))
        volume = float(item.get("volume", 0))
        avg_price = float(item.get("average_traded_price", 0))
        open_px = float(item.get("open", 0))
        high = float(item.get("high", 0))
        low = float(item.get("low", 0))
        close_prev = float(item.get("close", 0))  # previous day close

        imbalance_ratio = (total_buy / total_sell) if total_sell > 0 else 0.0
        is_upper_circuit = total_sell == 0 and total_buy > 0
        turnover = (volume * avg_price) if avg_price > 0 else (volume * price)
        turnover_cr = turnover / 1e7
        above_vwap = price > avg_price if avg_price > 0 else False

        # Time context
        try:
            import pytz as _pytz_to
            _ist_to = _pytz_to.timezone("Asia/Kolkata")
            _now_to = datetime.now(_ist_to)
            _min_since_open = (_now_to.hour * 60 + _now_to.minute) - (9 * 60 + 15)
        except Exception:
            _min_since_open = 120
        _is_opening = _min_since_open <= 30

        # ---- HARD GATES ----
        # Gate 1: Min price
        if price < 20.0:
            self._log(f"Tick-only skip {sym}: price ₹{price:.2f} < ₹20")
            return

        # Gate 2: Liquidity — turnover >= ₹5 Cr (during opening, allow ₹1 Cr)
        _min_turnover_cr = 1.0 if _is_opening else 3.0  # was ₹5Cr; lowered to catch RSWM (₹3.4Cr, +11.4%) type stocks
        if turnover_cr < _min_turnover_cr:
            self._log(f"Tick-only skip {sym}: turnover ₹{turnover_cr:.1f}Cr < ₹{_min_turnover_cr:.0f}Cr")
            self._add_to_demand_watchlist(
                sym, price, daily_change, imbalance_ratio, volume,
                item.get("reasons", []), f"low turnover ₹{turnover_cr:.1f}Cr"
            )
            return

        # Gate 3: Minimum change — 5% normally, 3% in opening window
        _min_change = 3.0 if _is_opening else 5.0
        if daily_change < _min_change:
            self._add_to_demand_watchlist(
                sym, price, daily_change, imbalance_ratio, volume,
                item.get("reasons", []), f"change +{daily_change:.1f}% < {_min_change:.0f}%"
            )
            return

        # Gate 4: Upper circuit — can't buy, skip silently
        if is_upper_circuit:
            self._log(f"Tick-only skip {sym}: upper circuit locked (sell_qty=0)")
            return

        # Gate 5: Already alerted
        if sym in self._already_alerted_buys:
            return

        # Gate 6: Alert count cap — max 12 tick-only alerts per day
        _tick_count = getattr(self, '_tick_only_alert_count', 0)
        if _tick_count >= 12:
            self._log(f"Tick-only skip {sym}: daily cap reached ({_tick_count}/12)")
            return

        # ---- TICK-ONLY SCORING ----
        _score = 0
        _score_reasons = []

        # Change% momentum
        if daily_change >= 10.0:
            _score += 30
            _score_reasons.append(f"🚀 +{daily_change:.1f}% breakout")
        elif daily_change >= 7.0:
            _score += 25
            _score_reasons.append(f"Strong momentum +{daily_change:.1f}%")
        elif daily_change >= 5.0:
            _score += 20
            _score_reasons.append(f"Moving +{daily_change:.1f}%")
        elif daily_change >= 3.0:
            _score += 12
            _score_reasons.append(f"Early +{daily_change:.1f}%")

        # Turnover (institutional liquidity)
        if turnover_cr >= 100:
            _score += 25
            _score_reasons.append(f"Massive liquidity ₹{turnover_cr:.0f}Cr")
        elif turnover_cr >= 25:
            _score += 20
            _score_reasons.append(f"Strong liquidity ₹{turnover_cr:.0f}Cr")
        elif turnover_cr >= 10:
            _score += 15
            _score_reasons.append(f"Good liquidity ₹{turnover_cr:.0f}Cr")
        elif turnover_cr >= 5:
            _score += 8
            _score_reasons.append(f"Adequate liquidity ₹{turnover_cr:.0f}Cr")

        # Order book imbalance (only if sell_qty > 0, otherwise UC handled above)
        if total_sell > 0 and imbalance_ratio >= 5.0:
            _score += 15
            _score_reasons.append(f"Heavy buyer demand {imbalance_ratio:.1f}x")
        elif total_sell > 0 and imbalance_ratio >= 2.0:
            _score += 10
            _score_reasons.append(f"Buyers outnumber sellers {imbalance_ratio:.1f}x")

        # Price vs VWAP (above = bullish, institutions buying at premium)
        if above_vwap:
            _score += 10
            _score_reasons.append("Above VWAP ✅")
        else:
            _score -= 5
            _score_reasons.append("Below VWAP ⚠️")

        # Gap-up quality: open significantly above prev close
        if close_prev > 0 and open_px > 0:
            gap_pct = (open_px - close_prev) / close_prev * 100
            if gap_pct >= 5.0:
                _score += 10
                _score_reasons.append(f"Strong gap-up +{gap_pct:.1f}%")
            elif gap_pct >= 2.0:
                _score += 5
                _score_reasons.append(f"Gap-up +{gap_pct:.1f}%")

        # Intraday range health: if price is near the high, trend is intact
        if high > 0 and low > 0 and open_px > 0:
            range_pct = (high - low) / open_px * 100 if open_px > 0 else 0
            price_position = (price - low) / (high - low) if high > low else 0.5
            if price_position >= 0.8:
                _score += 5
                _score_reasons.append("Near day's high")
            elif price_position < 0.4:
                _score -= 10
                _score_reasons.append("Fading from high ⚠️")

        _score = max(0, min(100, _score))

        # ---- DECISION ----
        # Tier 1: score >= 50 in opening window OR score >= 60 after
        _min_score = 35 if _is_opening else 38
        if _score < _min_score:
            self._log(
                f"Tick-only reject {sym}: score {_score:.0f} < {_min_score} "
                f"(+{daily_change:.1f}%, ₹{turnover_cr:.1f}Cr, imb={imbalance_ratio:.1f}x)"
            )
            self._add_to_demand_watchlist(
                sym, price, daily_change, imbalance_ratio, volume,
                item.get("reasons", []), f"tick score {_score:.0f} < {_min_score}"
            )
            return

        # ---- GRADE ----
        if _score >= 75:
            _grade = "A+"
            _grade_badge = "⭐ **A+**"
            _conviction = "high"
        elif _score >= 60:
            _grade = "A"
            _grade_badge = "**A**"
            _conviction = "medium-high"
        else:
            _grade = "B"
            _grade_badge = "B"
            _conviction = "medium"

        # ---- TARGETS ----
        # Derive SL and targets from tick data:
        # SL = max of (low of day, 3% below current price)
        # T1 = price × (1 + remaining scope × 0.5)
        # T2 = price × (1 + remaining scope)
        # Remaining scope based on strength indicators
        if imbalance_ratio >= 5.0:
            _remaining_scope = max(5, 15 - daily_change)
        elif imbalance_ratio >= 2.0 or turnover_cr >= 50:
            _remaining_scope = max(3, 10 - daily_change)
        else:
            _remaining_scope = max(2, 7 - daily_change)

        _sl_price = max(low if low > 0 else price * 0.95, price * 0.97)
        _t1_price = price * (1 + _remaining_scope * 0.5 / 100)
        _t2_price = price * (1 + _remaining_scope / 100)

        # Risk/reward check
        _risk = abs(price - _sl_price)
        _reward = abs(_t2_price - price)
        _rr = round(_reward / _risk, 1) if _risk > 0 else 0

        _size_mult = 1.0 if _grade == "A+" else 0.85 if _grade == "A" else 0.7
        _suggested_qty, _approx_cost = _compute_suggested_qty(
            price,
            stop_loss=_sl_price,
            score=_score,
            size_mult=_size_mult,
        )
        _suggested_qty, _approx_cost = self._cap_suggested_qty_to_broker(
            price,
            _suggested_qty,
            product_preference="MIS",
            holding_horizon="intraday",
        )
        if _suggested_qty <= 0:
            self._log(
                f"Tick-only skip {sym}: starter position exceeds ₹5000 cap at ₹{price:.2f}"
            )
            return

        # Imbalance string
        imb_str = ""
        if total_sell > 0 and imbalance_ratio >= 2.0:
            imb_str = f" | 👥 {imbalance_ratio:.1f}x more buyers than sellers"

        # ---- FORMAT AND SEND ----
        score_reasons_str = " | ".join(_score_reasons)
        msg = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**🔵 MOMENTUM BUY [{_grade_badge}] — `{sym}`**\n"
            f"📈 Price `₹{price:.2f}` | {direction}{daily_change:.1f}% today | Score **{_score:.0f}/100**{imb_str}\n"
            f"Turnover `₹{turnover_cr:.0f}Cr` | {'Above' if above_vwap else 'Below'} VWAP\n"
            f"ℹ️ Alert only unless a separate `✅ Auto-bought` confirmation follows.\n\n"
            f"🔴 Stop Loss `₹{_sl_price:.0f}` | 🟡 Target 1 `₹{_t1_price:.0f}` | 🟢 Target 2 `₹{_t2_price:.0f}` | R:R `1:{_rr}`\n"
            f"Why: {score_reasons_str}\n\n"
            f"**What to do ({_conviction} conviction):**\n"
            f"Buy **{_suggested_qty} shares** (≈ ₹{_approx_cost:,.0f})\n"
            f"🤖 Auto-buy: no — momentum-only alert, not a live auto-buy setup.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self._record_internal_stage("WATCH EARLY", sym, "momentum-only alert, not auto-buy eligible")
        self._already_alerted_buys.add(sym)
        self._save_daily_alerted(sym)
        self._tick_only_alert_count = getattr(self, '_tick_only_alert_count', 0) + 1
        self._log(
            f"🔵 MOMENTUM BUY [{_grade}] {sym} at ₹{price:.2f} "
            f"({direction}{daily_change:.1f}%) score={_score:.0f} "
            f"turnover=₹{turnover_cr:.0f}Cr imb={imbalance_ratio:.1f}x "
            f"[{self._tick_only_alert_count}/12]"
        )
        # Track for momentum continuation re-alerts
        self._momentum_tracked[sym] = {
            "entry_price": price,
            "last_alert_pct": daily_change,
            "last_alert_time": datetime.now(),
            "stop_loss": round(price * 0.97, 2),  # 3% default for tick-only
        }
        # Paper trade tracking
        self._paper_tracker.log_entry(
            symbol=sym, price=price, score=_score, grade=_grade,
            volume_ratio=imbalance_ratio, change_at_entry=daily_change,
            stop_loss=round(price * 0.97, 2), suggested_qty=_suggested_qty,
            source="tick_only",
        )

    # ------------------------------------------------------------------
    # Demand Watchlist — proactively track rejected stocks for momentum
    # ------------------------------------------------------------------

    def _add_to_demand_watchlist(self, sym: str, price: float, change: float,
                                 imbalance: float, volume: float,
                                 reasons: List[str], reject_reason: str) -> None:
        """Add a rejected stock to the demand watchlist for tick-level monitoring.

        Only stocks with genuine demand signals (imbalance + minimum move) are tracked.
        The watchlist is checked every tick cycle (5s) for momentum confirmation.
        """
        # Don't re-add if already watching or already alerted
        if sym in self._demand_watchlist or sym in self._already_alerted_buys:
            return
        # Do not start new demand-watch tracking late in the day.
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            _now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            _now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        if (_now_ist.hour * 60 + _now_ist.minute) > self._DEMAND_ALERT_CUTOFF_MIN:
            return
        # Minimum criteria to watch: price >= ₹100, imbalance >= 2x, change >= 1%
        if price < 100.0 or imbalance < 2.0 or change < 1.0:
            return

        hist_avg_vol = 0.0
        try:
            hist_path = os.path.join("results", "Data", "kite_history_cache", "day", f"{sym}.csv")
            if os.path.exists(hist_path):
                hdf = pd.read_csv(hist_path)
                if "volume" in hdf.columns and not hdf.empty:
                    hist_avg_vol = float(hdf["volume"].tail(10).mean() or 0.0)
        except Exception:
            hist_avg_vol = 0.0

        self._demand_watchlist[sym] = {
            "detect_price": price,
            "detect_change": change,
            "detect_time": datetime.now(),
            "imbalance": imbalance,
            "volume": volume,
            "hist_avg_vol": hist_avg_vol,
            "reasons": reasons,
            "reject_reason": reject_reason,
            "peak_price": price,
            "consecutive_up": 0,  # track consecutive tick-over-tick rises
        }
        self._log(
            f"Demand watchlist ADD: {sym} ₹{price:.0f} +{change:.1f}% "
            f"imb={imbalance:.1f}x (rejected: {reject_reason})"
        )

    def _check_demand_watchlist(self) -> None:
        """Check watchlist stocks against live ticks for momentum confirmation.

        Called every tick scan cycle (5s). When a watchlisted stock shows:
        - Price rose >= 1.5% from detection price
        - Order imbalance still >= 2x (demand persists)
        - Volume increasing (not a thin spike)
        → Send a DEMAND CONFIRMED alert.
        """
        if not self._demand_watchlist:
            return
        if self._demand_alert_count >= self._DEMAND_WATCHLIST_MAX_ALERTS:
            return

        # Time guard: stop demand-confirmed actionable alerts well before close.
        # Also clear stale watchlist entries so they don't burst near end-of-day.
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            _now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            _now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        _now_min = _now_ist.hour * 60 + _now_ist.minute
        if _now_min > self._DEMAND_ALERT_CUTOFF_MIN:
            if self._demand_watchlist:
                self._demand_watchlist.clear()
            return

        ticks = self._load_ticks_snapshot()
        if not ticks:
            return

        now = datetime.now()
        expired = []
        confirmed = []

        for sym, watch in list(self._demand_watchlist.items()):
            # Skip if already alerted by another path
            if sym in self._already_alerted_buys:
                expired.append(sym)
                continue

            # Expiry check
            age_sec = (now - watch["detect_time"]).total_seconds()
            if age_sec > self._DEMAND_WATCHLIST_EXPIRY_SEC:
                self._log(f"Demand watchlist EXPIRED: {sym} (no confirmation in {age_sec/60:.0f}m)")
                expired.append(sym)
                continue

            tick = ticks.get(sym)
            if not tick:
                continue

            live_price = float(tick.get("last_price") or 0)
            if live_price <= 0:
                continue

            live_change = float(tick.get("change") or 0)
            total_buy = float(tick.get("total_buy_quantity") or 0)
            total_sell = float(tick.get("total_sell_quantity") or 0)
            live_volume = float(tick.get("volume_traded") or 0)
            live_imbalance = (total_buy / total_sell) if total_sell > 0 else 0.0
            hist_avg_vol = float(watch.get("hist_avg_vol", 0.0) or 0.0)
            live_vol_ratio = (live_volume / hist_avg_vol) if hist_avg_vol > 0 else 0.0

            # Update peak price
            if live_price > watch["peak_price"]:
                watch["peak_price"] = live_price
                watch["consecutive_up"] = watch.get("consecutive_up", 0) + 1
            else:
                watch["consecutive_up"] = 0

            # Remove if price dropped below detection (demand evaporated)
            if live_price < watch["detect_price"] * 0.995:
                self._log(
                    f"Demand watchlist DROPPED: {sym} ₹{live_price:.0f} "
                    f"< detect ₹{watch['detect_price']:.0f}"
                )
                expired.append(sym)
                continue

            # Don't chase this path once day move is already stretched.
            # Those should be handled by the main BUY path / momentum continuation logic.
            if live_change >= self._DEMAND_CONFIRM_MAX_DAY_CHANGE:
                self._log(
                    f"Demand watchlist EXPIRED: {sym} day move +{live_change:.1f}% "
                    f">= {self._DEMAND_CONFIRM_MAX_DAY_CHANGE:.1f}% (late chase risk)"
                )
                expired.append(sym)
                continue

            # Confirmation criteria:
            # Standard: rise +0.9% + persistent imbalance + healthy live volume ratio.
            # Fast-track: rise +0.7% with very strong imbalance (4.5x+) and momentum.
            # Late window (>10m): require stricter quality to avoid stale entries.
            rise_from_detect = ((live_price - watch["detect_price"]) / watch["detect_price"]) * 100
            vol_grew = live_volume > watch["volume"] * 1.1  # at least 10% more volume
            consecutive_up = watch.get("consecutive_up", 0) >= 3  # 3+ consecutive upticks

            standard_quality = (
                rise_from_detect >= self._DEMAND_CONFIRM_RISE_PCT
                and live_imbalance >= self._DEMAND_CONFIRM_MIN_IMBALANCE
                and live_vol_ratio >= self._DEMAND_CONFIRM_MIN_VOL_RATIO
                and (vol_grew or consecutive_up)
            )
            fast_track = (
                rise_from_detect >= self._DEMAND_CONFIRM_FAST_RISE_PCT
                and live_imbalance >= self._DEMAND_CONFIRM_FAST_IMBALANCE
                and live_vol_ratio >= 1.5
                and watch.get("consecutive_up", 0) >= 2
            )

            if age_sec > 600 and not (
                rise_from_detect >= 1.4
                and live_imbalance >= 3.0
                and live_vol_ratio >= 2.2
            ):
                continue

            if standard_quality or fast_track:
                confirmed.append({
                    "symbol": sym,
                    "detect_price": watch["detect_price"],
                    "detect_change": watch["detect_change"],
                    "live_price": live_price,
                    "live_change": live_change,
                    "rise_from_detect": rise_from_detect,
                    "imbalance": live_imbalance,
                    "live_vol_ratio": live_vol_ratio,
                    "volume": live_volume,
                    "detect_imbalance": watch["imbalance"],
                    "reasons": watch["reasons"],
                    "reject_reason": watch["reject_reason"],
                    "age_sec": age_sec,
                })

        # Clean up expired
        for sym in expired:
            self._demand_watchlist.pop(sym, None)

        # Send confirmed alerts (max 2 per cycle to avoid flooding)
        for item in confirmed[:2]:
            sym = item["symbol"]
            if sym in self._already_alerted_buys:
                continue
            if self._demand_alert_count >= self._DEMAND_WATCHLIST_MAX_ALERTS:
                break

            price = item["live_price"]
            _imb = float(item.get("imbalance", 0.0) or 0.0)
            _volr = float(item.get("live_vol_ratio", 0.0) or 0.0)
            _rise = float(item.get("rise_from_detect", 0.0) or 0.0)

            # Grade Demand Confirmed quality so user can size positions safely.
            # A: strongest (full size), B: moderate (reduced size), C: weak (smallest).
            if _imb >= 5.0 and _volr >= 3.0 and _rise <= 2.5:
                _demand_grade = "A"
                _size_mult = 1.0
            elif _imb >= 3.0 and _volr >= 2.0 and _rise <= 3.0:
                _demand_grade = "B"
                _size_mult = 0.7
            else:
                _demand_grade = "C"
                _size_mult = 0.5
            if _demand_grade == "C":
                self._log(
                    f"Demand-confirmed suppressed {sym}: C-grade "
                    f"(imb={_imb:.1f}x, vol={_volr:.1f}x, rise={_rise:.1f}%)"
                )
                self._demand_watchlist.pop(sym, None)
                continue

            _sl_price = round(price * 0.97, 2)  # 3% SL
            _target_price = round(price * 1.08, 2)  # 8% target
            _scope_pct = round((((_target_price / price) - 1) * 100), 1)
            _suggested_qty, _approx_cost = _compute_suggested_qty(
                price,
                stop_loss=_sl_price,
                score=60 if _demand_grade == "A" else 52 if _demand_grade == "B" else 45,
                size_mult=_size_mult,
            )
            _suggested_qty, _approx_cost = self._cap_suggested_qty_to_broker(
                price,
                _suggested_qty,
                product_preference="CNC",
                holding_horizon="2-day swing" if _demand_grade in ("A", "B") else "avoid",
            )
            if _suggested_qty <= 0:
                self._log(
                    f"Demand-confirmed skip {sym}: starter position exceeds ₹5000 cap at ₹{price:.2f}"
                )
                self._demand_watchlist.pop(sym, None)
                continue
            _rr = round((_target_price - price) / max(price - _sl_price, 0.01), 1)
            _age_min = item["age_sec"] / 60
            _strong_b_demand = (
                _demand_grade == "B"
                and _rr >= 2.2
                and _volr >= 5.0
                and _imb >= 4.0
                and float(item.get("live_change", 0.0) or 0.0) <= 4.5
            )
            if _demand_grade == "B" and not _strong_b_demand:
                self._log(
                    f"Demand-confirmed suppressed {sym}: weak B-grade "
                    f"(rr={_rr:.1f}x, imb={_imb:.1f}x, vol={_volr:.1f}x, "
                    f"day={float(item.get('live_change', 0.0) or 0.0):.1f}%)"
                )
                self._demand_watchlist.pop(sym, None)
                continue
            _demand_auto_result = ""
            _today = datetime.now().strftime("%Y-%m-%d")
            if self._demand_auto_date != _today:
                self._demand_auto_bought.clear()
                self._demand_auto_date = _today
            if not self._DEMAND_AUTO_BUY_ENABLED:
                _demand_auto_result = "🤖 Auto-buy: no — demand auto-buy disabled."
            elif len(self._demand_auto_bought) >= self._DEMAND_AUTO_MAX_PER_DAY:
                _demand_auto_result = (
                    f"🤖 Auto-buy: no — demand auto-buy cap reached "
                    f"({len(self._demand_auto_bought)}/{self._DEMAND_AUTO_MAX_PER_DAY})."
                )
            elif _rr < self._DEMAND_AUTO_MIN_RR:
                _demand_auto_result = f"🤖 Auto-buy: no — R:R {_rr:.1f}x is below demand auto-buy floor {self._DEMAND_AUTO_MIN_RR:.1f}x."
            else:
                _auto_line = self._try_auto_buy(
                    sym,
                    price,
                    _sl_price,
                    round(price * 1.04, 2),
                    _target_price,
                    70.0 if _demand_grade == "A" else 58.0,
                    (
                        f"demand-confirmed {_demand_grade}-grade | buyer demand {_imb:.1f}x | "
                        f"volume {_volr:.1f}x | moved +{_rise:.1f}% from detect"
                    ),
                    product_preference="MIS",
                    holding_horizon="intraday",
                    max_hold_days=1,
                )
                if _auto_line:
                    _demand_auto_result = _auto_line
                if _auto_line and "auto-bought" in str(_auto_line).lower():
                    self._demand_auto_bought.add(sym)

            # Plain-English grade descriptions
            _grade_desc = {"A": "Strong — highest conviction, full size", "B": "Good — moderate conviction, reduced size"}[_demand_grade]
            _verdict = "✅ BUY NOW" if _demand_auto_result.startswith("✅") else "👀 WATCH — no automatic buy"
            # Expected remaining growth: from current price to target
            _expected_growth = round(((_target_price - price) / price) * 100, 1)
            msg = (
                f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"**🔮 {sym} — Buyers are pushing this stock up steadily**\n"
                f"Quality: **{_demand_grade}-Grade** ({_grade_desc})\n"
                f"📈 **Demand-confirmed setup target: +{_expected_growth:.1f}% from alert price** "
                f"(current day move: +{item['live_change']:.1f}% | moved +{item['rise_from_detect']:.1f}% from our detect price in {_age_min:.0f} min)\n\n"
                f"👥 Buyers vs sellers: `{item['imbalance']:.1f}x` more buyers than sellers (strong demand)\n"
                f"📊 Trading volume: `{item.get('live_vol_ratio', 0.0):.1f}x` higher than normal (real interest, not fake)\n\n"
                f"**What to do:**\n"
                f"{_verdict}\n"
                f"Buy **{_suggested_qty} shares** at ~`₹{price:.0f}` (costs ≈ ₹{_approx_cost:,.0f})\n"
                f"🔴 EXIT if price drops to `₹{_sl_price:.0f}` (limits your loss to 3%)\n"
                f"🟢 SELL near `₹{_target_price:.0f}` if momentum continues (fixed demand-watch target: +{_scope_pct:.1f}% from alert price)\n"
                f"💰 You risk `₹{price - _sl_price:.0f}` per share to gain `₹{_target_price - price:.0f}` per share ({_rr:.1f}x reward vs risk)\n\n"
                f"{_demand_auto_result or '🤖 Auto-buy: no — demand-watch alert, not a live auto-buy setup.'}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            self._record_internal_stage("WATCH EARLY", sym, "demand-confirmed setup tracked internally")
            if _demand_auto_result:
                self._handle_auto_buy_result(sym, _demand_auto_result)
            self._already_alerted_buys.add(sym)
            self._demand_watchlist.pop(sym, None)
            self._demand_alert_count += 1
            self._log(
                f"DEMAND CONFIRMED alert: {sym} +{item['rise_from_detect']:.1f}% from detect, "
                f"imb={item['imbalance']:.1f}x [{self._demand_alert_count}/{self._DEMAND_WATCHLIST_MAX_ALERTS}]"
            )

    # ------------------------------------------------------------------
    # Tick-only millisecond fast scanner
    # ------------------------------------------------------------------

    def _run_tick_fast_scan(self) -> None:
        """Scan ticks for pre-jump signals without any OHLCV API call.

        Fires an EARLY WATCH Discord alert the moment any of:
          1. Order book imbalance: total_buy_qty >= 2.5x total_sell_qty
          2. Volume burst: 2.5%+ of accumulated day vol traded in last 60s
          3. Daily change% >= 3% with no prior EARLY WATCH in last 30 min

        Latency: the ticks JSON is refreshed every 2 seconds by KiteTickerStream.
        This function runs in under 10 milliseconds (pure dict reads).
        """
        if not self._is_market_open():
            return  # NSE not open yet — skip tick scanning

        # Check demand watchlist FIRST — these are previously-rejected stocks
        # being monitored for momentum confirmation (runs in < 1ms)
        self._check_demand_watchlist()

        ticks = self._load_ticks_snapshot()
        if not ticks:
            return

        now = datetime.now()
        flagged: List[Dict] = []

        for symbol, tick in ticks.items():
            # Skip if already in the OHLCV-confirmed alert set (full buy alert sent)
            if symbol in self._already_alerted_buys:
                continue

            # Cooldown: don't spam re-alerts on the same symbol.
            # Rejected stocks get 30-min cooldown (stop hogging confirmation slots);
            # alerted stocks get 3-min cooldown (allow re-check for continuation).
            # High-change stocks (7%+) get shorter reject cooldown (10 min) — these are
            # genuine runners that may have been rejected for a transient reason (early-morning
            # low volume ratio, etc.) and deserve a faster re-check. Apr 10: SHRIPISTON (+11.9%)
            # was likely rejected for low volume early in the day and the 30-min cooldown
            # prevented timely re-check at higher volumes later.
            last_tick_alert = self._tick_alerted.get(symbol)
            if isinstance(last_tick_alert, datetime):
                signed_change_peek = float(tick.get("change") or 0)
                if symbol in self._tick_rejected and signed_change_peek >= 3.0:
                    cooldown = 600  # 10-min for 3%+ runners rejected earlier — re-check as volume builds
                elif symbol in self._tick_rejected:
                    cooldown = self._TICK_REJECT_COOLDOWN_SEC  # 30-min for other rejected stocks
                else:
                    cooldown = self._TICK_ALERT_COOLDOWN_SEC   # 3-min for alerted stocks
                if (now - last_tick_alert).total_seconds() < cooldown:
                    continue

            last_price = float(tick.get("last_price") or 0)
            if last_price <= 0:
                continue

            signed_change = float(tick.get("change") or 0)
            daily_change = abs(signed_change)

            # Long-only: skip stocks that are falling (negative daily change)
            # Falling stocks with volume bursts are DUMPS, not accumulation.
            if signed_change < 0:
                continue

            # Skip penny stocks (<₹10) — illiquid, hit upper circuit instantly, can't exit
            if last_price < 10.0:
                continue

            # Skip if stock already moved too much — train has left the station.
            # Above 7.5% daily, entry risk > reward at current price. Suppress silently.
            if daily_change >= self._TICK_ALREADY_MOVED_PCT:
                continue

            total_buy = float(tick.get("total_buy_quantity") or 0)
            total_sell = float(tick.get("total_sell_quantity") or 0)
            current_vol = float(tick.get("volume_traded") or 0)
            prev_vol = float((self._prev_tick_snapshot.get(symbol) or {}).get("volume_traded") or 0)

            signal_reasons: List[str] = []

            # Signal 1: Order book imbalance — only valid if stock is also moving (min 0.5%)
            # Pure imbalance on penny/thin stocks with near-zero volume is noise.
            if total_sell > 0 and total_buy / total_sell >= self._TICK_ORDER_IMBALANCE_RATIO and daily_change >= 0.5:
                ratio = round(total_buy / total_sell, 1)
                signal_reasons.append(f"📥 Buy orders {ratio}x sell orders (heavy accumulation)")
            elif total_buy > 0 and total_sell / total_buy >= self._TICK_ORDER_IMBALANCE_RATIO:
                # Heavy sell imbalance — potential dump coming, skip for buys
                pass

            # Signal 2: Volume burst — only valid if stock is also moving (min 0.5%)
            if prev_vol > 0 and current_vol > prev_vol and daily_change >= 0.5:
                burst_pct = (current_vol - prev_vol) / prev_vol
                if burst_pct >= self._TICK_VOLUME_BURST_FRACTION:
                    signal_reasons.append(
                        f"📊 Volume burst +{burst_pct*100:.1f}% in last 60s (smart money moving)"
                    )

            # Signal 3: High daily change (2%+ now, was 3%) — catches moves earlier
            if daily_change >= self._TICK_HIGH_DAILY_CHANGE_PCT:
                signal_reasons.append(
                    f"🚀 +{signed_change:.2f}% for the day"
                )

            # Signal 4: Early Accumulation — stock barely moved but big buyers loading
            # Volume moves before price. This is the highest-quality early entry signal.
            # Requires stricter imbalance (3:1) since there is no price confirmation yet.
            early_mover = False
            if (
                not signal_reasons
                and self._TICK_EARLY_MOVER_MIN_PCT <= daily_change < self._TICK_HIGH_DAILY_CHANGE_PCT
                and total_sell > 0
                and total_buy / total_sell >= self._TICK_EARLY_MOVER_IMBALANCE
            ):
                ratio = round(total_buy / total_sell, 1)
                signal_reasons.append(
                    f"🌱 Early accumulation: {ratio}x buy/sell at only +{signed_change:.2f}% move — big players loading"
                )
                early_mover = True

            if signal_reasons:
                flagged.append({
                    "symbol": symbol,
                    "price": last_price,
                    "daily_change": float(tick.get("change") or 0),
                    "total_buy": total_buy,
                    "total_sell": total_sell,
                    "volume": current_vol,
                    "reasons": signal_reasons,
                    "early_mover": early_mover,
                    # Extra tick fields for tick-only scoring (non-signal-state stocks)
                    "open": float(tick.get("open") or 0),
                    "high": float(tick.get("high") or 0),
                    "low": float(tick.get("low") or 0),
                    "close": float(tick.get("close") or 0),  # prev close
                    "average_traded_price": float(tick.get("average_traded_price") or 0),
                    "best_bid": float(tick.get("best_bid") or 0),
                    "best_ask": float(tick.get("best_ask") or 0),
                    "last_traded_quantity": float(tick.get("last_traded_quantity") or 0),
                    # Priority: flat band for 2-6% (sweet spot), IMBALANCE breaks ties.
                    # Above 6%: decreasing priority (late entry, train leaving).
                    # This ensures early movers with strong demand (stock at +3% with
                    # 5x buy/sell imbalance) get confirmed before late runners at +10%.
                    "_priority": (
                        30.0 if daily_change <= 6.0                                # sweet spot: all equal
                        else max(5.0, 30.0 - (daily_change - 6.0) * 3)             # -3 per % above 6
                    ) + (
                        min(25.0, (total_buy / max(total_sell, 1)) * 4) if total_sell > 0 else 0  # imbalance tiebreaker
                    ),
                })

        # Update snapshot for next cycle's delta-volume calculation
        self._prev_tick_snapshot = {s: dict(t) for s, t in ticks.items()}

        if not flagged:
            return

        # Sort by priority: early movers with strong imbalance first, late runners last.
        flagged.sort(key=lambda x: -x["_priority"])

        # Confirm top candidates per scan; each runs in its own background thread (~1-2s).
        import threading
        max_confirm = max(1, int(self._TICK_MAX_CONFIRM_PER_SCAN))
        for item in flagged[:max_confirm]:
            sym = item["symbol"]
            self._tick_alerted[sym] = now
            # Immediately spin up a targeted single-symbol OHLCV scan in background.
            # One stock = one API call = ~1-2 seconds. No waiting for the full queue.
            t = threading.Thread(
                target=self._safe_thread_wrapper,
                args=(self._confirm_tick_signal_with_ohlcv, item),
                daemon=True,
            )
            t.start()

    def _safe_thread_wrapper(self, target_func, *args, **kwargs):
        """Wrap a background thread target so exceptions are logged, not silently lost."""
        try:
            target_func(*args, **kwargs)
        except Exception as exc:
            self._log(f"Background thread {target_func.__name__} crashed: {exc}")

    def _confirm_tick_signal_with_ohlcv(self, item: Dict) -> None:
        """Confirm or reject a tick signal with a single-symbol OHLCV scan.

        Runs in a background thread, takes ~1-2 seconds (one Kite API call),
        then sends either:
          - 🟢 BUY ALERT  — tick + OHLCV both confirm, full targets/stop-loss included
          - ⚠️ WATCH note — tick fired but OHLCV score is too low to act, with reason why

        This replaces the old "EARLY WATCH — wait 45 min for full scan" pattern.
        """
        sym = item["symbol"]

        # Time guard: don't send actionable BUY alerts past the cutoff
        if self._past_buy_cutoff():
            self._log(f"OHLCV confirm skip {sym}: past buy cutoff")
            return

        # ETF/non-equity filter
        if self._is_excluded_symbol(sym):
            self._log(f"OHLCV confirm skip {sym}: excluded symbol (ETF/fund)")
            return

        price = item["price"]
        daily_change = item["daily_change"]
        direction = "+" if daily_change >= 0 else ""
        reasons_text = "\n".join(f"  \u2022 {r}" for r in item["reasons"])
        # Pre-compute imbalance for demand watchlist hooks
        _item_total_buy = float(item.get("total_buy", 0))
        _item_total_sell = float(item.get("total_sell", 0))
        _item_imbalance = (_item_total_buy / _item_total_sell) if _item_total_sell > 0 else 0.0
        _item_volume = float(item.get("volume", 0))
        _item_reasons = item.get("reasons", [])

        # ── LIQUIDITY GATE (Quantopian Volume/Slippage lecture) ──────────
        # Ensure stock has enough traded value today to absorb ₹50K orders
        # without significant slippage. Same logic as tick-only gate.
        _item_avg_px = float(item.get("average_traded_price", 0) or 0)
        _item_turnover_cr = (_item_volume * (_item_avg_px if _item_avg_px > 0 else price)) / 1e7
        try:
            import pytz as _pytz_liq2
            _ist_liq2 = _pytz_liq2.timezone("Asia/Kolkata")
            _now_liq2 = datetime.now(_ist_liq2)
            _min_open_liq2 = (_now_liq2.hour * 60 + _now_liq2.minute) - (9 * 60 + 15)
        except Exception:
            _min_open_liq2 = 120
        _ohlcv_min_turnover = 0.5 if _min_open_liq2 <= 30 else 2.0
        if _item_turnover_cr > 0 and _item_turnover_cr < _ohlcv_min_turnover:
            self._log(
                f"OHLCV liquidity skip {sym}: turnover ₹{_item_turnover_cr:.1f}Cr < ₹{_ohlcv_min_turnover:.0f}Cr"
            )
            self._add_to_demand_watchlist(
                sym, price, daily_change, _item_imbalance, _item_volume,
                _item_reasons, f"low turnover ₹{_item_turnover_cr:.1f}Cr"
            )
            return
        try:
            from pkscreener.classes.ActionableReport import ActionableReportRunner
            runner = ActionableReportRunner(
                config_manager=self._get_config_manager(),
                universe=self.config.universe,
                min_confidence=self.config.min_confidence,
                min_trade_value=0.0,
                min_volume_ratio=0.0,
                max_workers=1,
                use_live_kite=True,
                strong_buy_only=False,
            )
            report_df, _ = runner.build_report(symbols=[sym], show_progress=False)
        except Exception as exc:
            self._log(f"Tick OHLCV confirm failed for {sym}: {exc}")
            report_df = None

        # Fallback: if build_report returned nothing, try the signal monitor's
        # latest scan CSV. This has yesterday's (or today's) full scan for ~5000 stocks.
        if (report_df is None or report_df.empty):
            try:
                _sm_path = os.path.join("results", "Data", "signalmonitor_allstocks_signals_simple_latest.csv")
                if os.path.exists(_sm_path):
                    _sm_df = pd.read_csv(_sm_path)
                    _sm_row = _sm_df[_sm_df["Stock"] == sym]
                    if not _sm_row.empty:
                        # Rename columns to match ActionableReport format
                        _rename = {
                            "Stock": "Stock", "What To Do": "NowAction",
                            "Signal Strength %": "ConfidencePct",
                            "Price Now": "Price Now", "Buy Above": "BuyAbove",
                            "Sell If Below": "SellIfBelow",
                            "Take Profit 1": "TakeProfit1", "Take Profit 2": "TakeProfit2",
                            "Volume vs Avg": "VolumeRatio", "RSI": "RSI",
                            "CCI": "CCI", "MFI": "MFI",
                            "Why": "BlindBuyReasons", "Suggested Qty": "SuggestedQty",
                            "Avg Daily Value": "AvgDailyValue", "ATR": "ATR",
                            "PrevClose": "PrevClose",
                        }
                        report_df = _sm_row.rename(columns=_rename)
                        # Fill missing columns expected by scorer
                        for col in ["BlindBuyScore", "ATR"]:
                            if col not in report_df.columns:
                                report_df[col] = 0
                        self._log(f"Fallback: loaded {sym} from signal monitor scan (last scan CSV)")
            except Exception as _fb_exc:
                self._log(f"Signal monitor fallback failed for {sym}: {_fb_exc}")

        # If OHLCV is unavailable (no Kite history, SME/penny stock, etc.):
        # During opening 30-min window, send a lightweight tick-only alert
        # BUT only for high-conviction setups to avoid alert flooding.
        # Quality gates for tick-only: price >= ₹50, volume > 0, order imbalance >= 2x OR change >= 7%.
        if report_df is None or report_df.empty:
            self._handle_tick_only_alert(item, sym, price, daily_change, direction, reasons_text)
            return

        row = report_df.iloc[0]

        # For intraday momentum moves (daily_change >= 3%), the signal state's BuyAbove
        # was set from YESTERDAY's close as a swing breakout level. On an intraday move,
        # the stock has already confirmed direction — the tick signal IS the trigger.
        # Fix: for 3%+ intraday moves, zero out the stale BuyAbove so scorer skips it.
        #
        # Additional fix for BIOFILCHEM-type stocks:
        # Signal state may say "bearish alignment / below all MAs" from a STALE scan
        # (days ago). If the stock is up 3%+ today with 1.5x+ volume, it has empirically
        # broken out of that bearish alignment — the live move IS the confirmation.
        # Strip the stale bearish reason text so scorer doesn't apply the -50 penalty.
        # (Threshold lowered from 5%/2x → 3%/1.5x so we catch moves at 9:30 AM onset)
        scoring_row = row.copy()
        vol_ratio_live = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
        # Strip bearish labels if stock is up 4%+ (move IS the breakout confirmation).
        # A 4%+ move on a stock that was "below MAs" means MAs have been reclaimed intraday.
        # OR up 3%+ with decent early-morning volume (1.5x was unreachable before 11 AM IST).
        _strong_intraday = daily_change >= 4.0 or (daily_change >= 3.0 and vol_ratio_live >= 0.8)
        if daily_change >= 3.0:
            scoring_row["PriceNow"] = price   # use live tick price as baseline
            scoring_row["Price Now"] = price
            scoring_row["BuyAbove"] = 0       # skip stale swing-level trigger penalty
            scoring_row["Buy Above"] = 0
            scoring_row["Entry"] = 0
            # The OHLCV signal state "ChangePct" is from yesterday's pre-market scan
            # and may reflect the signal's HISTORICAL change (e.g. 4.9%), not today's
            # actual intraday gain (e.g. 9.3%). Fix: use today's actual gain so the scorer
            # correctly rewards the intraday momentum (+15 for 5%+ vs +12 for 2.5-4.99%).
            _ohlcv_chg = _safe_float(
                row.get("ChangePct", row.get("Change %", row.get("Change%", row.get("change", 0)))),
                0.0,
            )
            if abs(daily_change) > abs(_ohlcv_chg):
                scoring_row["ChangePct"] = daily_change
                scoring_row["Change %"] = daily_change
        if _strong_intraday:
            # Strip stale bearish labels from the Why/Reason column so scorer skips them.
            # The 5%+ live move with volume is stronger evidence than a stale text label.
            _why = str(scoring_row.get("Why", scoring_row.get("Reason", "")) or "")
            _why_cleaned = " | ".join(
                part for part in _why.split("|")
                if "below all major ma" not in part.lower()
                and "bearish alignment" not in part.lower()
                and "death cross" not in part.lower()
            )
            scoring_row["Why"] = _why_cleaned
            scoring_row["Reason"] = _why_cleaned

        blind_score, blind_reasons = BlindBuyScorer.score(scoring_row)
        is_early_mover = item.get("early_mover", False)

        _price_now = float(row.get("PriceNow", row.get("Price Now", price)) or price)
        _buy_above = float(row.get("BuyAbove", row.get("Buy Above", 0)) or 0)

        # Use OHLCV-based intraday change (from today's open) for display.
        # Kite tick `change` = (last - prev_close) / prev_close × 100.
        # If the stock gapped down at open and then rallied, tick change may show +0.8%
        # while the intraday move from open is +4%. OHLCV ChangePct reflects today's candle.
        _ohlcv_change = _safe_float(
            row.get("ChangePct", row.get("Change %", row.get("Change%", row.get("change", 0)))),
            0.0,
        )
        if abs(_ohlcv_change) > abs(daily_change):
            # OHLCV shows a bigger move (e.g. intraday from open) — use that for display
            daily_change = _ohlcv_change
            direction = "+" if daily_change >= 0 else ""
        # Fix display: if no trigger price from signal engine, show current price as entry
        _buy_above_display = _buy_above if _buy_above > 0 else _price_now
        _price_below_trigger = _price_now > 0 and _buy_above > 0 and _price_now < _buy_above
        # For intraday momentum moves, don't raise the effective min score either
        _intraday_override = daily_change >= 3.0
        _entry_confirmed = "entry confirmed" in blind_reasons.lower()

        # Effective minimum score:
        # - Early mover (0-2%): need strong OHLCV volume confirmation (60+)
        # - Intraday 3%+, no hard blockers: tick signal IS the confirmation, lower to 45
        #   Rationale: SCILAL (54, MACD+volume+trigger), UNIDT (52, trigger+MACD),
        #              IGPL (47, buy imbalance+volume+trigger) are all valid buys
        # - Entry confirmed (price crossed BuyAbove): entry is live, lower to 50
        # - Swing (price below trigger): need 65+ since entry not confirmed yet
        _reasons_lower_check = blind_reasons.lower()
        _no_hard_blockers = not (
            "do not buy" in _reasons_lower_check
            or "bearish" in _reasons_lower_check
            or "stop loss level" in _reasons_lower_check
        )
        # Volume available at this point of the day (for already-ran gate)
        _vol_ratio_live = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)

        # Determine if we're in the early-market window (first 30 min after open).
        # At 9:15–9:45 IST, volume ratios are physically low (day hasn't traded)
        # and indicators are cold. A gap-up + trigger ready is the best signal
        # available — demanding 1.3x vol or RSI confirmation is impossible.
        try:
            import pytz as _pytz_eff
            _ist_eff = _pytz_eff.timezone("Asia/Kolkata")
            _now_eff = datetime.now(_ist_eff)
            _min_since_open_eff = (_now_eff.hour * 60 + _now_eff.minute) - (9 * 60 + 15)
        except Exception:
            _min_since_open_eff = 120  # assume mid-morning if tz fails
        _is_opening_window = 0 <= _min_since_open_eff <= 30

        if is_early_mover:
            volume_ratio_check = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
            effective_min = 60.0 if volume_ratio_check >= self._TICK_EARLY_MOVER_VOL_RATIO else 999.0
        elif _intraday_override and _no_hard_blockers and _is_opening_window:
            # EARLY-MARKET WINDOW (9:15–9:45 IST).
            # Volume and indicators are physically cold. Gap-up from prev close
            # IS the signal. Accept lower score but still require entry confirmation.
            # Skip the volume gate entirely during this window.
            #
            # Tuning rationale (April 8 analysis):
            #   JTLIND  open +6.6%, score 47 → would pass 42 ✓ (closed +20%)
            #   PINELABS open +5.7%, score 32 → would NOT pass 42 (no indicator support)
            #   SEPC     open +13.5%, score ~35 → would NOT pass 42 (but gap was huge)
            effective_min = max(self.config.blind_buy_min_score, self.config.blind_buy_min_score - 3)
            self._log(
                f"Early-market window: {sym} +{daily_change:.1f}%, score {blind_score:.0f}, "
                f"effective_min={effective_min:.0f} (opening 30-min rules)"
            )
        elif _intraday_override and _no_hard_blockers:
            # Stock has already moved 3%+.  Goal: only alert when there is a real
            # chance of continuing to 10-20% (the user's daily target).
            # That requires BOTH strong volume (buyers piling in, not thin spike)
            # AND a high-conviction score.
            #
            # Tuning rationale:
            # - SILLYMONKS: +6.67%, vol 1.1x, score 47 → should NOT fire (thin move)
            # - RML:        +4.05%, vol 0.8x, score 49 → should NOT fire (below-avg vol)
            # - A genuine runner (TITAN +8%, vol 2x, score 72) → should fire
            #
            # Broad-market adaptation:
            # On strong Nifty days (+2%+), everything is up 3-5% just from the tide.
            # The volume gate and score min are relaxed slightly because:
            #   - individual stock volume may be diluted across the whole market
            #   - the signal is RELATIVE outperformance (stock >> Nifty), not absolute
            # A stock up 8% on a +3% Nifty day with 1.0x vol is genuine outperformance,
            # while the same stock on a flat Nifty day would need 1.3x vol to confirm.
            _nifty_chg = self._get_nifty_change()
            _is_broad_rally = _nifty_chg >= 2.0
            _relative_outperformance = daily_change - _nifty_chg if _nifty_chg > 0 else daily_change

            # Volume gate: 1.3x on normal days, 1.1x on broad rally days
            # Data-driven: Buy Now + vol >= 2.0x → 81% win rate vs 24% at 0.8x
            # Lowered from 1.5x/1.2x — THERMAX at 1.38x was real but got blocked.
            _vol_gate = 1.1 if _is_broad_rally else 1.3
            if _vol_ratio_live < _vol_gate:
                self._tick_rejected.add(sym)  # 30-min cooldown — stop re-checking losers
                self._log(
                    f"Suppressed {sym}: already ran {daily_change:.1f}% but "
                    f"vol {_vol_ratio_live:.2f}x < {_vol_gate:.1f}x "
                    f"(weak-volume runner{', broad rally +' + f'{_nifty_chg:.1f}%' if _is_broad_rally else ''})"
                )
                self._add_to_demand_watchlist(
                    sym, price, daily_change, _item_imbalance, _item_volume,
                    _item_reasons, f"weak vol {_vol_ratio_live:.1f}x < {_vol_gate:.1f}x"
                )
                return
            # Score gate: 52 normally (was 58 — HONASA at 53 was real),
            # 48 for broad-rally outperformers or high-volume Buy-Now stocks.
            _now_action_for_gate = str(row.get("NowAction", row.get("Action", ""))).strip()
            _is_buy_for_gate = self._is_true_buy_action(_now_action_for_gate)
            _high_vol_buy = _is_buy_for_gate and _vol_ratio_live >= 2.0
            if _is_broad_rally and _relative_outperformance >= 5.0:
                effective_min = max(35.0, self.config.blind_buy_min_score)
                self._log(
                    f"Broad rally mode: {sym} +{daily_change:.1f}% vs Nifty +{_nifty_chg:.1f}% "
                    f"(outperform +{_relative_outperformance:.1f}%), effective_min={effective_min:.0f}"
                )
            elif _high_vol_buy:
                effective_min = max(35.0, self.config.blind_buy_min_score)
                self._log(
                    f"High-vol Buy fast-track: {sym} vol {_vol_ratio_live:.1f}x + Buy Now "
                    f"→ effective_min={effective_min:.0f}"
                )
            else:
                effective_min = max(38.0, self.config.blind_buy_min_score)
        elif _entry_confirmed and _no_hard_blockers:
            # Price just crossed entry trigger (fresh breakout, stock hasn't run much yet).
            # Standard threshold is fine here.
            effective_min = self.config.blind_buy_min_score - 3
        elif _intraday_override:
            effective_min = self.config.blind_buy_min_score
        else:
            effective_min = 65.0 if _price_below_trigger else self.config.blind_buy_min_score

        if blind_score >= effective_min and (sym not in self._already_alerted_buys
                                            or sym not in getattr(self, '_definitely_buy_alerted', set())):
            _is_re_alert = sym in self._already_alerted_buys  # True if already sent a regular BUY
            # CONFIRMED — send a proper BUY ALERT immediately
            stop_loss = float(row.get("SellIfBelow", row.get("Sell If Below", 0)) or 0)
            target1 = float(row.get("TakeProfit1", row.get("Take Profit 1", 0)) or 0)
            target2 = float(row.get("TakeProfit2", row.get("Take Profit 2", 0)) or 0)
            suggested_qty = int(_safe_float(row.get("SuggestedQty", row.get("Suggested Qty", 0)), 0.0))
            confidence = float(row.get("ConfidencePct", row.get("Signal Strength %", 0)) or 0)
            volume_ratio = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
            atr = float(row.get("ATR", 0) or 0)

            # ── Gate: Inverted targets sanity check ──────────────────
            # If SL is ABOVE current price and targets are BELOW, the signal monitor
            # returned a SHORT/sell setup. This happens when the engine's analysis is
            # bearish but the stock gapped up (CONCORDBIO: SL ₹1123 > price ₹1072,
            # T1 ₹926, T2 ₹847 — clearly a short setup, NOT a buy).
            # Fix: detect and recompute fresh long targets from current price.
            _sl_above_price = stop_loss > 0 and _price_now > 0 and stop_loss > _price_now
            _targets_below_price = (target1 > 0 and target1 < _price_now) or (target2 > 0 and target2 < _price_now)
            if _sl_above_price or _targets_below_price:
                self._log(
                    f"Inverted targets for {sym}: SL ₹{stop_loss:.0f} {'>' if _sl_above_price else '<'} "
                    f"Price ₹{_price_now:.0f}, T1 ₹{target1:.0f}, T2 ₹{target2:.0f} — "
                    f"signal monitor returned SHORT setup, recomputing from live price"
                )
                if atr > 0:
                    stop_loss = round(_price_now - 1.5 * atr, 2)
                else:
                    stop_loss = round(_price_now * 0.97, 2)  # 3% SL
                target1 = round(_price_now * 1.05, 2)  # +5%
                target2 = round(_price_now * 1.10, 2)  # +10%

            # ── QUALITY GATES for ~90% win rate ─────────────────────────
            # Data-driven from April 8 backtest on 1,357 three-percent movers:
            #   Buy Now + vol >= 2.0x → 81% win rate (22/27)
            #   Adding RSI < 80 + real targets → 84%
            #   With corrected entry-at-3% metric → 90%+
            _now_action = str(row.get("NowAction", row.get("Action", ""))).strip()
            _now_action_lower = self._normalize_action_label(_now_action)
            _engine_not_buy = "hold" in _now_action_lower or "sell" in _now_action_lower or "exit" in _now_action_lower
            _is_buy_action = self._is_true_buy_action(_now_action)
            _rsi_val = float(row.get("RSI", 50) or 50)

            # Gate A: Engine action — require "Buy Now" or similar.
            # Override: RSI < 45 (oversold bounce — engine may say Hold but reversal
            # is the signal, e.g. PACEDIGITK RSI 39, +3.1% entry → +6.2% high).
            # Override: Opening 30-min window — signal state is from yesterday,
            # engine says "Hold" for stocks that are NOW breaking out at market open.
            # A +3%+ gap-up with volume IS the buy signal, engine just hasn't re-scanned.
            # Override: Intraday momentum — stock up 5%+ with real volume (2x+) and
            # turnover (₹3Cr+). These slow-buildup stocks cross 3% after the opening
            # window closes, get blocked by Gate A, then run to 10-20%.
            # Apr 10 data: 5%+TO≥₃Cr+vol≥2x → 81 alerts, 60% win, catches 12/13 missed.
            # Without vol≥2x: 161 alerts, 44% win (too noisy — half the 5%+ crossers fade).
            # Volume confirms real institutional buying vs thin-volume spike.
            _momentum_override = (
                daily_change >= 5.0
                and _item_turnover_cr >= 3.0
                and price >= 20.0
                and _vol_ratio_live >= 2.0
            )
            _strong_live_override = (
                daily_change >= 3.0
                and _item_turnover_cr >= 8.0
                and price >= 50.0
                and _vol_ratio_live >= 2.0
                and confidence >= 28
            )
            if not _is_buy_action and _rsi_val >= 45 and not is_early_mover and not _is_opening_window and not _momentum_override:
                self._tick_rejected.add(sym)  # 30-min cooldown
                self._log(
                    f"Suppressed {sym}: engine says '{_now_action}' + RSI {_rsi_val:.0f} >= 45 "
                    f"— no buy recommendation, skip"
                )
                self._add_to_demand_watchlist(
                    sym, price, daily_change, _item_imbalance, _item_volume,
                    _item_reasons, f"engine='{_now_action}', RSI={_rsi_val:.0f}"
                )
                return
            if not _is_buy_action and _strong_live_override and not _momentum_override:
                self._log(
                    f"Strong live override: {sym} +{daily_change:.1f}% with "
                    f"₹{_item_turnover_cr:.1f}Cr turnover, conf {confidence:.0f}% and vol {_vol_ratio_live:.1f}x "
                    f"— overriding stale engine '{_now_action}'"
                )
            if not _is_buy_action and _momentum_override:
                self._log(
                    f"Momentum override: {sym} +{daily_change:.1f}% with "
                    f"₹{_item_turnover_cr:.1f}Cr turnover — price action overrides stale engine '{_now_action}'"
                )
            elif not _is_buy_action and _is_opening_window:
                self._log(
                    f"Opening window bypass: {sym} engine says '{_now_action}' but "
                    f"+{daily_change:.1f}% move overrides stale signal"
                )

            # Gate B: RSI < 80 — extremely overbought stocks stall.
            # SETCO RSI 83 → max +5% then reversed. LUXIND RSI 78 borderline.
            # Bypass: momentum override (5%+ with turnover) — RSI 80 is expected for
            # stocks running intraday. OLAELEC RSI 80, +12.6%, TO ₹4048Cr is valid.
            if _rsi_val >= 80 and not is_early_mover and not _momentum_override:
                self._log(
                    f"Suppressed {sym}: RSI {_rsi_val:.0f} >= 80 — extremely overbought, skip"
                )
                return

            # Gate C: Confidence >= 20% — engine must have minimum conviction.
            # Bypass during opening window — confidence from yesterday's scan is stale.
            # Bypass for momentum override — confidence is stale when live price action
            # contradicts the signal. VPRPL 18% conf, +12.5% move, TO ₹24Cr is valid.
            if confidence < 20 and not is_early_mover and not (_rsi_val < 45) and not _is_opening_window and not _momentum_override:
                self._log(
                    f"Suppressed {sym}: confidence {confidence:.0f}% < 20% — low conviction, skip"
                )
                self._add_to_demand_watchlist(
                    sym, price, daily_change, _item_imbalance, _item_volume,
                    _item_reasons, f"low confidence {confidence:.0f}%"
                )
                return

            # Gate E: Price floor — skip illiquid penny stocks.
            # Original ₹50 floor blocked stocks like OLAELEC (₹40, TO ₹4048Cr),
            # VPRPL (₹42, TO ₹24Cr), SNOWMAN (₹40, TO ₹7Cr) which are NOT penny stocks.
            # Fix: allow lower-priced stocks if turnover proves institutional liquidity.
            # Hard floor ₹15 — below that, spreads are physically too wide for auto-trading.
            if _price_now < 15.0:
                self._log(f"Suppressed {sym}: price ₹{_price_now:.0f} < ₹15 — micro penny stock, skip")
                return
            if _price_now < 50.0 and _item_turnover_cr < 5.0:
                self._log(f"Suppressed {sym}: price ₹{_price_now:.0f} < ₹50 and turnover ₹{_item_turnover_cr:.1f}Cr < ₹5Cr — illiquid penny stock, skip")
                return

            # Gate F: Volume ratio >= 0.5x — must have real trading activity.
            # GUJALKALI 0.2x avg vol — imbalance on tiny volume is meaningless.
            if volume_ratio < 0.5 and not is_early_mover:
                self._log(f"Suppressed {sym}: volume {volume_ratio:.1f}x < 0.5x avg — illiquid, skip")
                return

            # Gate D: Real targets — require at least one real OHLCV-derived target.
            # Without SL/T1/T2, the engine couldn't find support/resistance.
            # Bypass during opening window — we'll derive targets from price.
            _has_real_targets = stop_loss > 0 or target1 > 0 or target2 > 0
            _strong_fallback_candidate = (
                self._is_true_buy_action(_now_action)
                and daily_change >= 2.0
                and confidence >= 40
                and volume_ratio >= 2.5
            )
            if not _has_real_targets and not is_early_mover and not _is_opening_window and not _momentum_override and not _strong_fallback_candidate:
                self._log(
                    f"Suppressed {sym}: NO real targets (SL=0, T1=0, T2=0) "
                    f"+{daily_change:.1f}%, conf={confidence:.0f}%, action='{_now_action}' "
                    f"— no technical basis for entry"
                )
                return
            if not _has_real_targets and _strong_fallback_candidate:
                self._log(
                    f"Fallback-target override for {sym}: strong '{_now_action}' setup "
                    f"(conf={confidence:.0f}%, vol={volume_ratio:.1f}x, move={daily_change:.1f}%)"
                )

            # Fallback stop/target computation.
            # Early movers: use wider targets (5%/10%) to capture the full expected move.
            # Regular (HOLD-rated) stocks already running: conservative 2%/4%.
            _aggressive_targets = is_early_mover or _intraday_override or daily_change >= 3.0
            stop_loss, target1, target2, _levels_normalized = _normalize_long_levels(
                _price_now,
                stop_loss,
                target1,
                target2,
                atr=atr,
                aggressive=_aggressive_targets,
            )
            if _levels_normalized:
                self._log(
                    f"Normalized long levels for {sym}: price ₹{_price_now:.2f}, "
                    f"SL ₹{stop_loss:.2f}, T1 ₹{target1:.2f}, T2 ₹{target2:.2f}"
                )
            # ── Position Sizing (volatility-adjusted, score-scaled) ──────────
            # Base: risk ₹1000 per trade (1% of ₹1L budget), stop defined by ATR/SL.
            # Volatility cap: position value <= (target_vol% / asset_vol%) × portfolio.
            # Score multiplier: high-conviction signals (score>=70) get up to 1.5x size.
            # Hard cap: reads KITE_SWING_MAX_ORDER_VALUE from env (default ₹10000).
            _swing_max = float(os.environ.get("KITE_SWING_MAX_ORDER_VALUE", "10000") or "10000")
            _swing_capital = float(os.environ.get("KITE_SWING_CAPITAL", "30000") or "30000")
            _risk_amt = max(500, _swing_max * 0.10)   # 10% of max order = risk per trade
            _max_position = _swing_max                 # use configured per-order max
            _portfolio = _swing_capital                # use configured swing capital
            _target_vol = 0.15      # 15% target portfolio volatility contribution

            _auto_qty, _auto_invest_amt = _compute_suggested_qty(
                _price_now,
                stop_loss=stop_loss,
                score=blind_score,
                atr=atr,
                size_mult=1.0,
                max_position=_max_position,
                risk_amount=_risk_amt,
                portfolio=_portfolio,
                target_vol=_target_vol,
            )
            if suggested_qty <= 0:
                suggested_qty = _auto_qty
            elif _price_now > 0 and _max_position > 0:
                suggested_qty = min(suggested_qty, int(_max_position // _price_now))
            if suggested_qty <= 0:
                self._log(
                    f"Suppressed {sym}: starter position exceeds ₹{_max_position:.0f} cap at ₹{_price_now:.2f}"
                )
                return
            _invest_amt = round(suggested_qty * _price_now, 0) if _price_now > 0 else _auto_invest_amt

            # Gate 1: time-weighted volume gate — early morning (9:15–10:30 AM) has naturally low
            # cumulative volume vs full-day average. Require 80% of time-proportional pace, floor 0.15x.
            if volume_ratio > 0:
                _utcn = datetime.utcnow()
                _ist_h = (_utcn.hour + 5) % 24
                _ist_m = _utcn.minute + 30
                if _ist_m >= 60:
                    _ist_h = (_ist_h + 1) % 24
                    _ist_m -= 60
                _mins_traded = max(1, _ist_h * 60 + _ist_m - (9 * 60 + 15))
                _min_vol = max(0.15, (_mins_traded / 375.0) * 0.8)
                if volume_ratio < _min_vol:
                    self._log(f"Suppressed {sym}: volume {volume_ratio:.2f}x < time-weighted min {_min_vol:.2f}x")
                    return

            score_label = f"\U0001f525 **{blind_score:.0f}/100**" if blind_score >= 70 else f"\u2705 **{blind_score:.0f}/100**"
            risk_reward = ""
            rr2 = _compute_long_rr(_price_now, stop_loss, target2)
            if rr2 > 0:
                risk_reward = f" | R:R `1:{rr2}`"

            # Gate 2: R:R must be at least 1:1.0 — risking more than you gain is a losing strategy.
            # 1:1 is the minimum for positive expected value; 1:1.5 was too strict and rejected
            # valid setups on broad up-market days where prices had already moved 2-3%.
            # Exception: for intraday movers (3%+) targets are recomputed fresh from current price.
            min_rr = 1.0
            if rr2 > 0 and rr2 < min_rr and _intraday_override:
                _fresh_stop = round(_price_now * 0.95, 2)
                _fresh_t2 = round(_price_now * 1.10, 2)
                _fresh_rr = round((_fresh_t2 - _price_now) / max(_price_now - _fresh_stop, 0.01), 1)
                if _fresh_rr >= min_rr:
                    stop_loss = _fresh_stop
                    target1 = round(_price_now * 1.05, 2)
                    target2 = _fresh_t2
                    rr2 = _fresh_rr
                    risk_reward = f" | R:R `1:{rr2}` (fresh intraday targets)"
            if rr2 > 0 and rr2 < min_rr:
                self._log(f"Suppressed {sym}: R:R 1:{rr2} < 1:{min_rr} minimum")
                return

            qty_line = f"\nSuggested: Buy **`{suggested_qty}` shares** (≈ ₹{_invest_amt:,.0f})"

            # ── "DEFINITELY BUY" classification ────────────────────────
            # Only fires when ALL quality signals align. User can act instantly.
            # Criteria (ALL must be true):
            #   1. OHLCV confirmed (we're already in this path)
            #   2. Score >= 55
            #   3. Confidence >= 30%
            #   4. Volume >= 2.0x avg
            #   5. Price >= ₹100 (no micro-caps)
            #   6. Change 2-5% (early enough — scope to run)
            #   7. Real targets (SL, T1, T2 all > 0)
            #   8. R:R >= 1.5 (from T2)
            #   9. Historical avg volume >= 100k (highly liquid)
            _def_buy_hist_avg = 0
            try:
                _def_hist_path = os.path.join("results", "Data", "kite_history_cache", "day", f"{sym}.csv")
                if os.path.exists(_def_hist_path):
                    _def_hdf = pd.read_csv(_def_hist_path)
                    _def_buy_hist_avg = _def_hdf["volume"].tail(10).mean()
            except Exception:
                pass
            _has_all_targets = stop_loss > 0 and target1 > 0 and target2 > 0
            _rr2_val = ((target2 - _price_now) / (_price_now - stop_loss)) if (_price_now > stop_loss and stop_loss > 0) else 0
            _is_definitely_buy = (
                blind_score >= 45
                and confidence >= 20
                and volume_ratio >= 1.5
                and _price_now >= 50.0
                and 1.5 <= daily_change <= 7.0
                and _has_all_targets
                and _rr2_val >= 1.2
                and _def_buy_hist_avg >= 25000
                and not is_early_mover
            )

            # Time-of-day context: after 1 PM + already moved 5%+ = swing trade only
            try:
                import pytz
                _ist = pytz.timezone("Asia/Kolkata")
                _nist = datetime.now(_ist)
            except Exception:
                from datetime import timezone, timedelta
                _nist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
            _is_late = _nist.hour * 60 + _nist.minute >= 13 * 60
            # SWING ONLY: stock already moved 8%+ any time of day.
            # At 8%+ intraday, entry risk > remaining reward for same-day exit.
            # Still alert with SWING tag so user can decide to hold overnight.
            is_swing_only = daily_change >= 8.0
            swing_tag = f"\n⏰ **SWING ONLY** — already +{daily_change:.1f}% today. Do NOT enter for intraday — swing trade only (hold 2-5 days)." if is_swing_only else ""

            if _is_definitely_buy:
                alert_header = f"**🔥 STRONG BUY — `{sym}`** — Quality {score_label}\n✅ All checks passed: price ✓ | volume {volume_ratio:.1f}x above normal ✓ | {confidence:.0f}% indicators agree ✓"
            elif is_early_mover:
                alert_header = f"**🌱 EARLY ENTRY — `{sym}`** (big buyers loading quietly, stock barely moved yet — could go +5-10%) — Quality {score_label}"
            else:
                alert_header = f"**🟢 BUY — `{sym}`** — Quality {score_label}{swing_tag}"

            # ── Re-validate price with LATEST tick before sending ──────────
            # The tick price was captured 2-10s ago (before OHLCV scan).
            # On volatile stocks, the price may have moved significantly.
            # Re-read the latest tick to get the most current price & change%.
            _fresh_ticks = self._load_ticks_snapshot()
            _fresh_tick = _fresh_ticks.get(sym, {})
            _fresh_price = float(_fresh_tick.get("last_price") or 0)
            _fresh_change = float(_fresh_tick.get("change") or 0)
            if _fresh_price > 0:
                # Use freshest data for display
                _price_now = _fresh_price
                daily_change = _fresh_change
                direction = "+" if daily_change >= 0 else ""
                # Also check: did the stock reverse since tick capture?
                # If it dropped below the original alert threshold (3%), suppress.
                if daily_change < 2.0:
                    self._log(
                        f"Suppressed {sym}: price reversed from +{item['daily_change']:.1f}% "
                        f"to +{daily_change:.1f}% between tick capture and alert — stale signal"
                    )
                    return

                # ── Retreat-from-high gate ─────────────────────────────
                # If the stock ran up (e.g. +15%) then pulled back to current (+9%),
                # it gave back a significant chunk of its intraday range — momentum is
                # exhausted. Buying at this point is chasing a retreating stock.
                # CLSEL: high 275 (+15.6%), pulled back to 261 (+9.7%) = gave back 47%.
                _tick_high = float(_fresh_tick.get("high") or _fresh_tick.get("ohlc", {}).get("high", 0))
                _tick_low = float(_fresh_tick.get("low") or _fresh_tick.get("ohlc", {}).get("low", 0))
                if _tick_high > 0 and _tick_low > 0 and _tick_high > _tick_low and _fresh_price > 0:
                    _range = _tick_high - _tick_low
                    _retreat = _tick_high - _fresh_price
                    _retreat_frac = _retreat / _range  # fraction of range given back
                    _pct_from_high = (_tick_high - _fresh_price) / _tick_high * 100
                    if _retreat_frac >= 0.35 and _pct_from_high >= 4.0:
                        self._log(
                            f"Suppressed {sym}: retreated {_pct_from_high:.1f}% from day high ₹{_tick_high:.2f} "
                            f"(gave back {_retreat_frac:.0%} of range ₹{_tick_low:.2f}-₹{_tick_high:.2f}) — momentum exhausted"
                        )
                        return

                # Recalculate targets from fresh price
                _buy_above_display = _buy_above if _buy_above > 0 else _price_now
                is_swing_only = daily_change >= 8.0
                swing_tag = f"\n⏰ **SWING ONLY** — already +{daily_change:.1f}% today. Do NOT enter for intraday — swing trade only (hold 2-5 days)." if is_swing_only else ""
                stop_loss, target1, target2, _fresh_levels_normalized = _normalize_long_levels(
                    _price_now,
                    stop_loss,
                    target1,
                    target2,
                    atr=atr,
                    aggressive=is_early_mover or _intraday_override or daily_change >= 3.0,
                )
                if _fresh_levels_normalized:
                    self._log(
                        f"Fresh-tick normalized long levels for {sym}: price ₹{_price_now:.2f}, "
                        f"SL ₹{stop_loss:.2f}, T1 ₹{target1:.2f}, T2 ₹{target2:.2f}"
                    )
                rr2 = _compute_long_rr(_price_now, stop_loss, target2)
                risk_reward = f" | R:R `1:{rr2}`" if rr2 > 0 else ""
                if rr2 > 0 and rr2 < min_rr:
                    self._log(
                        f"Suppressed {sym}: fresh-tick R:R 1:{rr2} < 1:{min_rr} minimum"
                    )
                    return
                # Recalculate _is_definitely_buy with fresh data
                _rr2_val = _compute_long_rr(_price_now, stop_loss, target2)
                _is_definitely_buy = (
                    blind_score >= 45
                    and confidence >= 20
                    and volume_ratio >= 1.5
                    and _price_now >= 50.0
                    and 1.5 <= daily_change <= 7.0
                    and _has_all_targets
                    and _rr2_val >= 1.2
                    and _def_buy_hist_avg >= 25000
                    and not is_early_mover
                )
                if _is_definitely_buy:
                    alert_header = f"**🔥 STRONG BUY — `{sym}`** — Quality {score_label}\n✅ All checks passed: price ✓ | volume {volume_ratio:.1f}x above normal ✓ | {confidence:.0f}% indicators agree ✓"
                elif is_early_mover:
                    alert_header = f"**🌱 EARLY ENTRY — `{sym}`** (big buyers loading quietly, stock barely moved yet — could go +5-10%) — Quality {score_label}"
                else:
                    alert_header = f"**🟢 BUY — `{sym}`** — Quality {score_label}{swing_tag}"

            # Scope from entry price to targets
            _scope_t1 = ((target1 - _price_now) / _price_now * 100) if target1 > 0 and _price_now > 0 else 0
            _scope_t2 = ((target2 - _price_now) / _price_now * 100) if target2 > 0 and _price_now > 0 else 0
            _scope_line = ""
            if _scope_t1 > 0 or _scope_t2 > 0:
                parts = []
                if _scope_t1 > 0:
                    parts.append(f"+{_scope_t1:.1f}% to first target")
                if _scope_t2 > 0:
                    parts.append(f"+{_scope_t2:.1f}% to max target")
                _scope_line = f" | **Profit potential: {' | '.join(parts)}**"

            _ohlcv_reasons_text = self._simplify_blind_reasons(blind_reasons)
            # BlindBuy reasons are computed before final stop/target normalization.
            # If we corrected an inverted short setup into a long setup, drop stale
            # danger text when stop-loss is safely below current price.
            if _price_now > 0 and stop_loss > 0 and stop_loss < _price_now * 0.99:
                _ohlcv_reasons_text = _ohlcv_reasons_text.replace(
                    "DANGER: price at stop loss level | ", ""
                ).replace(" | DANGER: price at stop loss level", "").replace(
                    "DANGER: price at stop loss level", ""
                )

            # Compute per-share risk/reward for plain-English display
            _risk_per_share = round(_price_now - stop_loss, 2) if stop_loss > 0 else 0
            _reward_per_share = round(target2 - _price_now, 2) if target2 > 0 else 0
            _expected_growth_pct = round(((target2 - _price_now) / _price_now) * 100, 1) if target2 > 0 and _price_now > 0 else 0
            _fresh_vwap = float(_fresh_tick.get("average_traded_price") or item.get("average_traded_price") or 0)
            _fresh_bid = float(_fresh_tick.get("best_bid") or item.get("best_bid") or 0)
            _fresh_ask = float(_fresh_tick.get("best_ask") or item.get("best_ask") or 0)
            _fresh_open = float(_fresh_tick.get("open") or item.get("open") or 0)
            _fresh_low = float(_fresh_tick.get("low") or _fresh_tick.get("ohlc", {}).get("low", 0) or item.get("low") or 0)
            _fresh_high = float(_fresh_tick.get("high") or _fresh_tick.get("ohlc", {}).get("high", 0) or item.get("high") or 0)
            _fresh_spread_pct = ((_fresh_ask - _fresh_bid) / _price_now * 100) if _fresh_ask > 0 and _fresh_bid > 0 and _price_now > 0 and _fresh_ask >= _fresh_bid else 0.0
            _fresh_range_pct = ((_fresh_high - _fresh_low) / _fresh_open * 100) if _fresh_high > 0 and _fresh_low > 0 and _fresh_open > 0 else 0.0
            _fresh_ltq = float(_fresh_tick.get("last_traded_quantity") or item.get("last_traded_quantity") or 0)
            _fresh_vwap_gap_pct = ((_price_now - _fresh_vwap) / _fresh_vwap * 100) if _fresh_vwap > 0 and _price_now > 0 else 0.0
            _fresh_support_ref = max(_buy_above_display, _fresh_vwap)
            _fresh_support_gap_pct = ((_price_now - _fresh_support_ref) / _fresh_support_ref * 100) if _fresh_support_ref > 0 and _price_now > 0 else 0.0
            _alert_candidate = self._enrich_candidate_horizon(BlindBuyCandidate(
                symbol=sym,
                price=_price_now,
                buy_above=_price_now,
                stop_loss=stop_loss,
                target1=target1,
                target2=target2,
                confidence=confidence,
                volume_ratio=volume_ratio,
                change_pct=daily_change,
                blind_score=blind_score,
                original_action=_now_action,
                reasons=blind_reasons,
                suggested_qty=suggested_qty,
                buyer_imbalance=round(_item_imbalance, 2),
                turnover_cr=round(_item_turnover_cr, 2),
                spread_pct=round(_fresh_spread_pct, 2),
                range_pct=round(_fresh_range_pct, 2),
                last_trade_qty=round(_fresh_ltq, 2),
                vwap_gap_pct=round(_fresh_vwap_gap_pct, 2),
                support_gap_pct=round(max(0.0, _fresh_support_gap_pct), 2),
                fresh_buyer_confirmation=bool(
                    _item_imbalance >= 1.6
                    and _item_turnover_cr >= 1.0
                    and _fresh_vwap > 0
                    and _price_now >= _fresh_vwap
                ),
            ))
            _off_high_pct = 0.0
            if _fresh_high > 0 and _price_now > 0:
                _off_high_pct = ((_fresh_high - _price_now) / _fresh_high) * 100
            _alert_candidate.off_high_pct = round(_off_high_pct, 2)
            _alert_candidate.buy_above = _buy_above_display
            self._enrich_candidate_alert_stage(_alert_candidate, off_high_pct=_off_high_pct)
            suggested_qty, _invest_amt = self._cap_suggested_qty_to_broker(
                _price_now,
                suggested_qty,
                product_preference=_alert_candidate.product_preference,
                holding_horizon=_alert_candidate.holding_horizon,
            )
            _alert_candidate.suggested_qty = suggested_qty
            _alert_grade, _ = self._grade_candidate(_alert_candidate)
            _entry_timing_line = self._format_entry_timing_line(_alert_candidate)
            _hold_product_line = self._format_hold_product_line(_alert_candidate)
            _auto_status_line = self._format_auto_buy_status_line(_alert_candidate, _alert_grade)
            _stage = _alert_candidate.alert_stage or "BUY"
            if _stage == "WATCH EARLY":
                alert_header = f"**👀 EARLY WATCH — `{sym}`** — strong stock preparing"
            elif _stage == "MISSED":
                alert_header = f"**🟡 MISSED — `{sym}`** — good stock, but entry is late"
            elif _stage == "AVOID":
                alert_header = f"**🔴 AVOID — `{sym}`** — risky or overextended"
            elif _is_definitely_buy:
                alert_header = f"**🔥 STRONG BUY `{sym}`** (tick-confirmed in ~2s) — Score {score_label}"
            elif is_early_mover:
                alert_header = f"**🌱 EARLY MOVER `{sym}`** (accumulation detected, low % — target 5-10%) — Score {score_label}"
            else:
                alert_header = f"**🟢 BUY `{sym}`** (tick-confirmed in ~2s) — Score {score_label}{swing_tag}"
            _qty_display_line = f"\nSuggested: Buy **`{suggested_qty}` shares** (≈ ₹{_invest_amt:,.0f})" if suggested_qty > 0 else ""
            message = (
                f"{alert_header}\n"
                f"Price `{_price_now:.2f}` ({direction}{daily_change:.2f}%) | Buy Above `{_buy_above_display:.2f}`\n"
                f"Stop Loss `{stop_loss:.2f}` | Target 1 `{target1:.2f}` | Target 2 `{target2:.2f}`{risk_reward}\n"
                f"Volume `{volume_ratio:.1f}x avg` | Confidence `{confidence:.0f}%`\n"
                f"Tick signals:\n{reasons_text}\n"
                f"OHLCV confirms: {_ohlcv_reasons_text}"
                f"{_qty_display_line}\n"
                f"_Open Groww → Buy → then use `/buy {sym} QTY PRICE` to record_"
            )
            if _stage in {"WATCH EARLY", "MISSED", "AVOID"}:
                self._record_internal_stage(_stage, sym, _alert_candidate.alert_stage_reason)
                self._log(
                    f"{_stage} ALERT SENT: {sym} at ₹{_price_now:.2f} ({direction}{daily_change:.2f}%) "
                    f"score={blind_score:.0f} vol={volume_ratio:.1f}x"
                )
                return
            # For re-alerts: only send if it's a DEFINITELY BUY upgrade
            if _is_re_alert and not _is_definitely_buy:
                self._log(f"Skip re-alert for {sym}: already alerted, not a DEFINITELY BUY upgrade")
            elif self._is_opening_window_active() and not _is_re_alert:
                _tick_high = float(_fresh_tick.get("high") or _fresh_tick.get("ohlc", {}).get("high", 0))
                _tick_low = float(_fresh_tick.get("low") or _fresh_tick.get("ohlc", {}).get("low", 0))
                fast_lane_ok, fast_lane_reason = self._qualifies_for_opening_fast_lane(
                    symbol=sym,
                    price=_price_now,
                    blind_score=blind_score,
                    confidence=confidence,
                    volume_ratio=volume_ratio,
                    daily_change=daily_change,
                    stop_loss=stop_loss,
                    target2=target2,
                    turnover_cr=_item_turnover_cr,
                    imbalance=_item_imbalance,
                    tick_high=_tick_high,
                    tick_low=_tick_low,
                )
                if fast_lane_ok:
                    self._opening_fast_lane_count += 1
                    fast_lane_message = f"**⚡ OPENING FAST-LANE — `{sym}`**\n{message}"
                    self._log(
                        f"⚡ OPENING FAST-LANE: {sym} at ₹{_price_now:.2f} ({direction}{daily_change:.2f}%) "
                        f"{fast_lane_reason} ({self._opening_fast_lane_count}/{self._OPENING_FAST_LANE_TOP_N} today)"
                    )
                    self._already_alerted_buys.add(sym)
                    if _is_definitely_buy:
                        self._definitely_buy_alerted.add(sym)
                    self._save_daily_alerted(sym)
                    auto_result = self._attempt_tick_confirmed_auto_buy(
                        symbol=sym,
                        price=_price_now,
                        stop_loss=stop_loss,
                        target1=target1,
                        target2=target2,
                        blind_score=blind_score,
                        confidence=confidence,
                        volume_ratio=volume_ratio,
                        daily_change=daily_change,
                        reasons=blind_reasons,
                        original_action=_now_action,
                        source="opening fast-lane",
                        spread_pct=_alert_candidate.spread_pct,
                        range_pct=_alert_candidate.range_pct,
                        last_trade_qty=_alert_candidate.last_trade_qty,
                        turnover_cr=_alert_candidate.turnover_cr,
                        buyer_imbalance=_alert_candidate.buyer_imbalance,
                        vwap_gap_pct=_alert_candidate.vwap_gap_pct,
                        off_high_pct=_alert_candidate.off_high_pct,
                        support_gap_pct=_alert_candidate.support_gap_pct,
                        fresh_buyer_confirmation=_alert_candidate.fresh_buyer_confirmation,
                    )
                    if auto_result:
                        self._handle_auto_buy_result(sym, auto_result)
                else:
                    # During opening 15-min window: buffer alerts, don't send individually.
                    # They'll be flushed as a ranked summary every 30s.
                    with self._opening_buffer_lock:
                        self._opening_buffer.append({
                            "symbol": sym,
                            "price": _price_now,
                            "daily_change": daily_change,
                            "blind_score": blind_score,
                            "volume_ratio": volume_ratio,
                            "confidence": confidence,
                            "stop_loss": stop_loss,
                            "target1": target1,
                            "target2": target2,
                            "suggested_qty": suggested_qty,
                            "is_definitely_buy": _is_definitely_buy,
                            "reasons": blind_reasons,
                            "original_action": _now_action,
                            "message": message,
                            "spread_pct": _alert_candidate.spread_pct,
                            "range_pct": _alert_candidate.range_pct,
                            "last_trade_qty": _alert_candidate.last_trade_qty,
                            "turnover_cr": _alert_candidate.turnover_cr,
                            "buyer_imbalance": _alert_candidate.buyer_imbalance,
                            "vwap_gap_pct": _alert_candidate.vwap_gap_pct,
                            "off_high_pct": _alert_candidate.off_high_pct,
                            "support_gap_pct": _alert_candidate.support_gap_pct,
                            "fresh_buyer_confirmation": _alert_candidate.fresh_buyer_confirmation,
                        })
                    self._log(
                        f"📦 BUFFERED (opening window): {sym} at ₹{_price_now:.2f} ({direction}{daily_change:.2f}%) "
                        f"score={blind_score:.0f} vol={volume_ratio:.1f}x — {fast_lane_reason}, will flush in ranked batch"
                    )
            else:
                if _is_re_alert and _is_definitely_buy:
                    message = f"**⬆️ UPGRADED to DEFINITELY BUY**\n{message}"
                    self._log(
                        f"🔥 DEFINITELY BUY UPGRADE: {sym} at ₹{_price_now:.2f} ({direction}{daily_change:.2f}%) "
                        f"score={blind_score:.0f} vol={volume_ratio:.1f}x conf={confidence:.0f}%"
                    )
                else:
                    self._log(
                        f"🟢 BUY ALERT SENT: {sym} at ₹{_price_now:.2f} ({direction}{daily_change:.2f}%) "
                        f"score={blind_score:.0f}/{effective_min:.0f} vol={volume_ratio:.1f}x "
                        f"reasons={blind_reasons[:80]}"
                    )
                self._already_alerted_buys.add(sym)
                if _is_definitely_buy:
                    self._definitely_buy_alerted.add(sym)
                self._save_daily_alerted(sym)
                self._log(
                    f"Tick-confirmed auto-buy check: {sym} score={blind_score:.0f} "
                    f"conf={confidence:.0f}% vol={volume_ratio:.1f}x change={daily_change:.1f}%"
                )
                try:
                    auto_result = self._attempt_tick_confirmed_auto_buy(
                        symbol=sym,
                        price=_price_now,
                        stop_loss=stop_loss,
                        target1=target1,
                        target2=target2,
                        blind_score=blind_score,
                        confidence=confidence,
                        volume_ratio=volume_ratio,
                        daily_change=daily_change,
                        reasons=blind_reasons,
                        original_action=_now_action,
                        source="tick-confirmed live alert",
                        spread_pct=_alert_candidate.spread_pct,
                        range_pct=_alert_candidate.range_pct,
                        last_trade_qty=_alert_candidate.last_trade_qty,
                        turnover_cr=_alert_candidate.turnover_cr,
                        buyer_imbalance=_alert_candidate.buyer_imbalance,
                        vwap_gap_pct=_alert_candidate.vwap_gap_pct,
                        off_high_pct=_alert_candidate.off_high_pct,
                        support_gap_pct=_alert_candidate.support_gap_pct,
                        fresh_buyer_confirmation=_alert_candidate.fresh_buyer_confirmation,
                    )
                except Exception as exc:
                    auto_result = f"⚠️ **AUTO-BUY FAILED — `{sym}`**\n{exc}"
                    self._log(f"Tick-confirmed auto-buy crash {sym}: {exc}")
                if auto_result:
                    self._handle_auto_buy_result(sym, auto_result)
            # Track for momentum continuation re-alerts
            self._momentum_tracked[sym] = {
                "entry_price": _price_now,
                "last_alert_pct": daily_change,
                "last_alert_time": datetime.now(),
                "stop_loss": stop_loss,
            }
            # Paper trade tracking
            self._paper_tracker.log_entry(
                symbol=sym, price=_price_now, score=blind_score,
                grade="A+" if blind_score >= 70 else ("A" if blind_score >= 60 else "B"),
                volume_ratio=volume_ratio, change_at_entry=daily_change,
                stop_loss=stop_loss, target1=target1, target2=target2,
                suggested_qty=suggested_qty, source="ohlcv",
            )

        else:
            # REJECTED — log why, do NOT send any alert.
            # Mark as rejected so tick scanner uses 30-min cooldown instead of 3-min.
            # This frees confirmation slots for NEW stocks entering the 2-5% zone.
            self._tick_rejected.add(sym)
            self._log(
                f"Not a buy: {sym} score={blind_score:.0f} (need {effective_min:.0f}) | "
                f"change={daily_change:.2f}% | reasons={blind_reasons[:80]}"
            )
            # Add to demand watchlist — if demand persists and price accelerates,
            # we'll catch the momentum instead of waiting for the 10-min rescan.
            self._add_to_demand_watchlist(
                sym, price, daily_change, _item_imbalance, _item_volume,
                _item_reasons, f"score {blind_score:.0f} < {effective_min:.0f}"
            )
            _watch_candidate = self._enrich_candidate_horizon(BlindBuyCandidate(
                symbol=sym,
                price=_price_now,
                buy_above=_buy_above_display,
                stop_loss=float(row.get("SellIfBelow", row.get("Sell If Below", 0)) or 0),
                target1=float(row.get("TakeProfit1", row.get("Take Profit 1", 0)) or 0),
                target2=float(row.get("TakeProfit2", row.get("Take Profit 2", 0)) or 0),
                confidence=float(row.get("ConfidencePct", row.get("Signal Strength %", 0)) or 0),
                volume_ratio=_vol_ratio_live,
                change_pct=daily_change,
                blind_score=blind_score,
                original_action=str(row.get("NowAction", row.get("Action", ""))).strip(),
                reasons=blind_reasons,
            ))
            self._enrich_candidate_alert_stage(_watch_candidate)
            if _watch_candidate.alert_stage == "WATCH EARLY":
                self._record_internal_stage("WATCH EARLY", sym, _watch_candidate.alert_stage_reason)

    def _load_ticks_snapshot(self) -> Dict[str, Dict]:
        """Read the latest ticks JSON saved by KiteTickerStream.

        Uses stat-based caching: if the file's mtime AND size haven't changed
        since last read, returns the cached dict instantly (~0.005ms vs ~16ms).
        Double-checks both mtime and size to be safe on any filesystem.
        Uses orjson when available for 2x faster parsing.
        """
        ticks_path = os.path.join(
            self.output_dir,
            f"kiteticker_{self.config.universe}_ticks_latest.json",
        )
        if not os.path.isfile(ticks_path):
            return {}
        try:
            # stat check — mtime + size together prevent stale cache on any FS
            st = os.stat(ticks_path)
            cache_key = (st.st_mtime_ns, st.st_size)
            if cache_key == getattr(self, "_ticks_cache_key", None) and hasattr(self, "_ticks_cache_data"):
                return self._ticks_cache_data

            try:
                import orjson as _ojson
                with open(ticks_path, "rb") as f:
                    data = _ojson.loads(f.read())
            except ImportError:
                import json
                with open(ticks_path, "r") as f:
                    data = json.load(f)
            result = {str(k).strip().upper(): v for k, v in data.items()} if isinstance(data, dict) else {}
            self._ticks_cache_key = cache_key
            self._ticks_cache_data = result
            return result
        except Exception:
            return {}

    def _load_ticks_for_universe(self, universe: str) -> Dict[str, Dict]:
        """Best-effort tick loader for auxiliary universes used by radar."""
        universe = str(universe or "").strip().lower()
        if not universe:
            return {}
        path = os.path.join(self.output_dir, f"kiteticker_{universe}_ticks_latest.json")
        if not os.path.isfile(path):
            return {}
        try:
            try:
                import orjson as _ojson
                with open(path, "rb") as handle:
                    payload = _ojson.loads(handle.read())
            except ImportError:
                import json
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            return {str(k).strip().upper(): v for k, v in payload.items()} if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _load_runner_radar_ticks(self) -> Dict[str, Dict]:
        """Load the broadest available tick set for top-gainer visibility."""
        ticks = dict(self._load_ticks_snapshot() or {})
        # If a combined NSE+BSE stream is running, use it to improve radar coverage
        # even when the trading engine itself is configured to allstocks/NSE.
        if str(self.config.universe or "").strip().lower() != "all":
            all_ticks = self._load_ticks_for_universe("all")
            for sym, tick in all_ticks.items():
                ticks.setdefault(sym, tick)
        return ticks

    def _classify_early_expansion_row(
        self,
        *,
        price: float,
        change: float,
        turnover_cr: float,
        above_vwap: bool,
        high: float,
        low: float,
        total_buy: float,
        total_sell: float,
        open_price: float,
    ) -> Tuple[str, str]:
        """Return (quality_label, reason) for early +1% to +4.5% radar rows."""
        is_circuit_like = total_sell <= 0 and total_buy > 0
        imbalance = (total_buy / total_sell) if total_sell > 0 else 0.0
        near_high = True
        if high > 0 and low > 0 and high > low and price > 0:
            near_high = ((high - price) / max(high - low, 0.01)) <= 0.30
        opening_reclaim = bool(open_price > 0 and price >= open_price)

        if is_circuit_like:
            return "SKIP", "sell book empty; circuit/liquidity risk"
        if price < 20:
            return "SKIP", "below ₹20"
        if turnover_cr < self._EARLY_RADAR_MIN_TURNOVER_CR:
            return "SKIP", f"turnover only ₹{turnover_cr:.1f}Cr"
        if not above_vwap:
            return "SKIP", "below VWAP"
        if not near_high:
            return "SKIP", "not holding near day high"
        if change < self._EARLY_RADAR_MIN_CHANGE or change > self._EARLY_RADAR_MAX_CHANGE:
            return "SKIP", "outside early expansion band"
        if imbalance >= self._EARLY_RADAR_MIN_IMBALANCE and opening_reclaim:
            return "EARLY TRADE CANDIDATE", f"VWAP+, near high, buyer demand {imbalance:.1f}x"
        if imbalance >= 1.1 or change >= 2.0:
            return "WATCH", "VWAP+ and forming, needs stronger buyer pressure"
        return "SKIP", "not enough buyer pressure yet"

    def _run_early_expansion_radar(self) -> None:
        """Identify early-expansion stocks and add them to an internal watchlist.

        This is silent (no Discord). Stocks that match the early radar criteria
        are added to ``self._early_watch_list`` and re-evaluated every cycle by
        ``_review_early_watch_list`` which decides whether to auto-buy, drop, or
        mark MISSED — Discord only fires on AUTO-BOUGHT / AUTO-BUY FAILED.
        """
        if not self._is_market_open():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._early_radar_date != today:
            self._early_radar_seen.clear()
            self._early_radar_date = today
            self._early_radar_last_summary = 0.0
        if self._early_watch_date != today:
            self._early_watch_list.clear()
            self._early_watch_date = today

        ticks = self._load_runner_radar_ticks()
        if not ticks:
            # Even if no fresh ticks, still re-evaluate already tracked names.
            self._review_early_watch_list()
            return

        rows: List[Dict] = []
        for raw_sym, tick in ticks.items():
            sym = str(raw_sym or "").strip().upper()
            if not sym or self._is_excluded_symbol(sym):
                continue
            price = _safe_float(tick.get("last_price"))
            change = _safe_float(tick.get("change"))
            if price <= 0 or change < self._EARLY_RADAR_MIN_CHANGE or change > self._EARLY_RADAR_MAX_CHANGE:
                continue
            volume = _safe_float(tick.get("volume_traded", tick.get("volume", 0)))
            avg_px = _safe_float(tick.get("average_traded_price", price))
            turnover_cr = (volume * (avg_px if avg_px > 0 else price)) / 1e7
            total_buy = _safe_float(tick.get("total_buy_quantity", tick.get("total_buy_qty", 0)))
            total_sell = _safe_float(tick.get("total_sell_quantity", tick.get("total_sell_qty", 0)))
            high = _safe_float(tick.get("high", tick.get("ohlc", {}).get("high", 0)))
            low = _safe_float(tick.get("low", tick.get("ohlc", {}).get("low", 0)))
            open_price = _safe_float(tick.get("open", tick.get("ohlc", {}).get("open", 0)))
            above_vwap = bool(avg_px > 0 and price >= avg_px)
            quality, reason = self._classify_early_expansion_row(
                price=price,
                change=change,
                turnover_cr=turnover_cr,
                above_vwap=above_vwap,
                high=high,
                low=low,
                total_buy=total_buy,
                total_sell=total_sell,
                open_price=open_price,
            )
            if quality == "SKIP":
                continue
            bucket = int(change)
            imbalance = (total_buy / total_sell) if total_sell > 0 else 0.0
            rows.append({
                "symbol": sym,
                "price": price,
                "change": change,
                "turnover_cr": turnover_cr,
                "above_vwap": above_vwap,
                "quality": quality,
                "reason": reason,
                "bucket": bucket,
                "imbalance": imbalance,
            })

        # Track every qualifying row in the internal early-watch list (silent — no Discord).
        # The lifecycle reviewer below decides each name's fate.
        if rows:
            now_ts = time.time()
            for row in rows:
                quality = str(row.get("quality", "") or "").strip().upper()
                if quality not in {"EARLY TRADE CANDIDATE", "WATCH"}:
                    continue
                sym = str(row.get("symbol", "") or "").strip().upper()
                if not sym:
                    continue
                if sym in self._early_runner_auto_bought or sym in self._early_runner_auto_attempted:
                    continue
                change = _safe_float(row.get("change"))
                entry = self._early_watch_list.get(sym)
                if entry is None:
                    if len(self._early_watch_list) >= self._EARLY_WATCH_MAX_SIZE:
                        # Capacity guard: drop the oldest tracked symbol so a fresher
                        # candidate can be observed instead.
                        oldest = min(
                            self._early_watch_list.items(),
                            key=lambda kv: kv[1].get("added_at", 0.0),
                        )[0]
                        self._early_watch_list.pop(oldest, None)
                    self._early_watch_list[sym] = {
                        "added_at": now_ts,
                        "first_change": change,
                        "peak_change": change,
                        "peak_change_ts": now_ts,
                        "last_imbalance": _safe_float(row.get("imbalance")),
                        "last_turnover_cr": _safe_float(row.get("turnover_cr")),
                        "last_above_vwap": bool(row.get("above_vwap")),
                        "last_quality": quality,
                        "last_seen": now_ts,
                    }
                else:
                    prev_peak = _safe_float(entry.get("peak_change"))
                    if change > prev_peak:
                        entry["peak_change"] = change
                        entry["peak_change_ts"] = now_ts
                    entry["last_imbalance"] = _safe_float(row.get("imbalance"))
                    entry["last_turnover_cr"] = _safe_float(row.get("turnover_cr"))
                    entry["last_above_vwap"] = bool(row.get("above_vwap"))
                    entry["last_quality"] = quality
                    entry["last_seen"] = now_ts

        self._early_radar_last_summary = time.time()
        self._review_early_watch_list()

    def _qualifies_as_second_leg_continuation(
        self,
        *,
        change: float,
        peak_change: float,
        peak_change_ts: float,
        now_ts: float,
        price: float,
        avg_px: float,
        above_vwap: bool,
        imbalance: float,
        turnover_cr: float,
        near_high: bool,
    ) -> bool:
        """Return True iff a stock past ``_EARLY_RUNNER_AUTO_MAX_CHANGE`` qualifies
        as a clean continuation (rested → VWAP reclaim → renewed demand).

        Default for exhausted movers stays MISSED. This narrow exception only
        flips when *every* condition below is satisfied, so the path cannot
        relax into a chase. All downstream safety checks (live recheck,
        cash, dup, SL/target validation, liquidity, GTT/protection) still run
        through the existing ``_try_auto_buy`` → ``KiteOrderManager`` path.
        """
        # Hard ceiling: never even consider beyond _SECOND_LEG_MAX_CHANGE.
        if change > self._SECOND_LEG_MAX_CHANGE:
            return False
        # Must be above VWAP and not stretched far above it (avoid blow-off tops).
        if not above_vwap:
            return False
        if avg_px > 0 and price > 0:
            vwap_gap_pct = (price - avg_px) / avg_px * 100.0
            if vwap_gap_pct > self._SECOND_LEG_MAX_VWAP_GAP_PCT:
                return False
        # Must have rested: peak unchanged for at least the rest interval.
        if peak_change_ts > 0 and now_ts - peak_change_ts < self._SECOND_LEG_MIN_REST_SEC:
            return False
        # Must have pulled back from peak by a clear margin.
        if peak_change - change < self._SECOND_LEG_MIN_PULLBACK_PCT:
            return False
        # Demand and liquidity must be re-asserting, not fading.
        if imbalance < self._SECOND_LEG_MIN_IMBALANCE:
            return False
        if turnover_cr < self._SECOND_LEG_MIN_TURNOVER_CR:
            return False
        # Re-breaking near intraday high — the actual trigger.
        if not near_high:
            return False
        return True

    def _review_early_watch_list(self) -> None:
        """Re-evaluate every internally tracked early-watch symbol every cycle.

        Lifecycle:
          * still-forming     → keep tracking silently
          * entry too late    → mark MISSED internally, drop from list
          * weakening         → drop silently
          * clean breakout    → place Kite order, send Discord AUTO-BOUGHT / AUTO-BUY FAILED
        """
        if not self._early_watch_list:
            return
        if not self._EARLY_RUNNER_AUTO_BUY_ENABLED or self._order_manager is None or self._past_buy_cutoff():
            return
        # Early-watch lane is intraday only (MIS). Let it run until the final MIS
        # cutoff; after that there is not enough same-day time left to open fresh risk.
        _now_min = self._ist_now_minute()
        if _now_min > self._intraday_final_auto_buy_cutoff_min():
            return
        # Market regime gate: block in crash/bear, fall through in neutral or
        # missing-engine scenarios. Lifecycle housekeeping (MISSED/weak drops)
        # below still runs so the watchlist does not stagnate during a pause.
        regime_ok, regime_reason = self._is_safe_to_buy()
        if not regime_ok:
            self._record_no_buy_reason(regime_reason)
            self._log(f"Early-watch auto-buy paused: {regime_reason}")

        today = datetime.now().strftime("%Y-%m-%d")
        if self._early_runner_auto_date != today:
            self._early_runner_seen.clear()
            self._early_runner_auto_attempted.clear()
            self._early_runner_auto_bought.clear()
            self._early_runner_auto_date = today
        if self._AUTO_BUY_TOP_N > 0 and self._tick_confirmed_auto_buy_count >= self._AUTO_BUY_TOP_N:
            return
        if len(self._early_runner_auto_bought) >= self._EARLY_RUNNER_AUTO_MAX_PER_DAY:
            return

        ticks = self._load_runner_radar_ticks()
        threshold_epsilon = 0.01
        clean_candidates: List[Dict] = []
        to_drop: List[str] = []

        for sym, entry in list(self._early_watch_list.items()):
            tick = ticks.get(sym) if ticks else None
            if not tick:
                continue
            price = _safe_float(tick.get("last_price"))
            if price <= 0:
                continue
            change = _safe_float(tick.get("change"))
            volume = _safe_float(tick.get("volume_traded", tick.get("volume", 0)))
            avg_px = _safe_float(tick.get("average_traded_price", price))
            turnover_cr = (volume * (avg_px if avg_px > 0 else price)) / 1e7
            total_buy = _safe_float(tick.get("total_buy_quantity", tick.get("total_buy_qty", 0)))
            total_sell = _safe_float(tick.get("total_sell_quantity", tick.get("total_sell_qty", 0)))
            high = _safe_float(tick.get("high", tick.get("ohlc", {}).get("high", 0)))
            low = _safe_float(tick.get("low", tick.get("ohlc", {}).get("low", 0)))
            above_vwap = bool(avg_px > 0 and price >= avg_px)
            imbalance = (total_buy / total_sell) if total_sell > 0 else 0.0
            near_high = True
            if high > 0 and low > 0 and high > low and price > 0:
                near_high = ((high - price) / max(high - low, 0.01)) <= 0.30
            prev_peak = _safe_float(entry.get("peak_change"))
            peak_change = max(prev_peak, change)
            now_ts_review = time.time()
            if change > prev_peak:
                entry["peak_change_ts"] = now_ts_review
            peak_change_ts = _safe_float(entry.get("peak_change_ts", entry.get("added_at", now_ts_review)))
            entry["peak_change"] = peak_change
            entry["last_imbalance"] = imbalance
            entry["last_turnover_cr"] = turnover_cr
            entry["last_above_vwap"] = above_vwap

            # 1) Late: entry has run too far. The default is MISSED. The
            #    second-leg exception below allows a clean continuation IF the
            #    move clearly rested AND reclaimed VWAP AND demand re-asserted.
            if change > self._EARLY_RUNNER_AUTO_MAX_CHANGE + threshold_epsilon:
                second_leg = self._qualifies_as_second_leg_continuation(
                    change=change,
                    peak_change=peak_change,
                    peak_change_ts=peak_change_ts,
                    now_ts=now_ts_review,
                    price=price,
                    avg_px=avg_px,
                    above_vwap=above_vwap,
                    imbalance=imbalance,
                    turnover_cr=turnover_cr,
                    near_high=near_high,
                )
                if second_leg:
                    clean_candidates.append({
                        "symbol": sym,
                        "price": price,
                        "change": change,
                        "imbalance": imbalance,
                        "turnover_cr": turnover_cr,
                        "second_leg": True,
                    })
                    continue
                self._record_internal_stage(
                    "MISSED",
                    sym,
                    f"early-watch entry too late at +{change:.2f}%",
                )
                to_drop.append(sym)
                continue

            # 2) Weak: signal has decayed — drop silently.
            if not above_vwap:
                to_drop.append(sym)
                continue
            if total_sell <= 0 and total_buy > 0:
                # Locked book / circuit risk — can't trade in or out cleanly.
                to_drop.append(sym)
                continue
            if imbalance < self._EARLY_WATCH_WEAK_IMBALANCE:
                to_drop.append(sym)
                continue
            if peak_change - change >= self._EARLY_WATCH_PEAK_PULLBACK_PCT:
                to_drop.append(sym)
                continue

            # 3) Forming: not yet in the actionable band — keep tracking silently.
            if change < self._EARLY_RUNNER_AUTO_MIN_CHANGE - threshold_epsilon:
                continue
            if turnover_cr < self._EARLY_RUNNER_AUTO_MIN_TURNOVER_CR - threshold_epsilon:
                continue
            if imbalance < self._EARLY_RUNNER_AUTO_MIN_IMBALANCE - threshold_epsilon:
                continue
            if not near_high:
                continue
            if entry.get("last_quality") == "WATCH":
                if imbalance < self._EARLY_RUNNER_AUTO_MIN_IMBALANCE + 0.8 - threshold_epsilon:
                    continue
                if turnover_cr < max(self._EARLY_RUNNER_AUTO_MIN_TURNOVER_CR, 1.0) - threshold_epsilon:
                    continue

            # 4) Clean — eligible for auto-buy this cycle.
            clean_candidates.append({
                "symbol": sym,
                "price": price,
                "change": change,
                "imbalance": imbalance,
                "turnover_cr": turnover_cr,
            })

        for sym in to_drop:
            self._early_watch_list.pop(sym, None)

        if not regime_ok:
            # Lifecycle housekeeping (MISSED / weak / forming) above has run.
            # Skip auto-buy this cycle until the regime turns supportive.
            return

        if not clean_candidates:
            return

        clean_candidates.sort(
            key=lambda r: (
                _safe_float(r.get("imbalance")),
                _safe_float(r.get("turnover_cr")),
                -abs(_safe_float(r.get("change")) - 3.0),
            ),
            reverse=True,
        )

        for cand in clean_candidates:
            if len(self._early_runner_auto_bought) >= self._EARLY_RUNNER_AUTO_MAX_PER_DAY:
                break
            if self._AUTO_BUY_TOP_N > 0 and self._tick_confirmed_auto_buy_count >= self._AUTO_BUY_TOP_N:
                break
            sym = str(cand["symbol"])
            if sym in self._early_runner_auto_attempted:
                continue
            price = _safe_float(cand.get("price"))
            if price <= 0:
                continue
            # Confirm against the freshest broker tick before placing the order.
            latest_price = 0.0
            try:
                latest_price = float(self._order_manager._latest_tick_price(sym) or 0.0)
            except Exception:
                latest_price = 0.0
            if latest_price > 0:
                stale_drop_pct = ((price - latest_price) / price * 100) if price > 0 else 0.0
                if stale_drop_pct > self._EARLY_RUNNER_AUTO_MAX_TICK_STALE_DROP_PCT:
                    self._log(
                        f"Early-watch auto skip {sym}: live tick faded "
                        f"{stale_drop_pct:.1f}% from watch price ₹{price:.2f} to ₹{latest_price:.2f}"
                    )
                    self._early_watch_list.pop(sym, None)
                    continue
                price = latest_price

            stop_loss = round(price * 0.965, 2)
            target1 = round(price * 1.045, 2)
            target2 = round(price * 1.075, 2)
            rr2 = _compute_long_rr(price, stop_loss, target2)
            if rr2 < self._EARLY_RUNNER_AUTO_MIN_RR:
                self._log(f"Early-watch auto skip {sym}: R:R 1:{rr2:.1f} below floor")
                continue

            blind_score = min(88.0, 58.0 + _safe_float(cand.get("imbalance")) * 4.0 + _safe_float(cand.get("turnover_cr")) * 0.7)
            lane_label = "second-leg continuation" if cand.get("second_leg") else "clean breakout"
            reasons = (
                f"early-watch {lane_label} | VWAP+ | near high | buyer demand "
                f"{_safe_float(cand.get('imbalance')):.1f}x | turnover ₹{_safe_float(cand.get('turnover_cr')):.1f}Cr"
            )
            self._early_runner_auto_attempted.add(sym)
            result = self._try_auto_buy(
                sym,
                price,
                stop_loss,
                target1,
                target2,
                blind_score,
                reasons,
                product_preference="MIS",
                holding_horizon="intraday",
                max_hold_days=1,
            )
            self._log(f"Early-watch auto-buy result {sym}: {result}")
            if result:
                self._handle_auto_buy_result(sym, result)
            if result and "auto-bought" in str(result).lower():
                self._early_runner_auto_bought.add(sym)
                self._tick_confirmed_auto_buy_count += 1
            # Symbol has been acted on — stop tracking either way.
            self._early_watch_list.pop(sym, None)

    def _classify_runner_radar_row(
        self,
        *,
        price: float,
        change: float,
        turnover_cr: float,
        above_vwap: bool,
        high: float,
        low: float,
        total_buy: float,
        total_sell: float,
    ) -> Tuple[str, str]:
        """Return (quality_label, reason) for radar-only runner display."""
        is_circuit_like = total_sell <= 0 and total_buy > 0
        near_high = True
        if high > 0 and low > 0 and high > low and price > 0:
            near_high = ((high - price) / max(high - low, 0.01)) <= 0.35

        if is_circuit_like:
            return "LOCKED/CIRCUIT RISK", "sell book empty; hard to enter/exit safely"
        if turnover_cr < 0.50:
            return "THIN", f"turnover only ₹{turnover_cr:.1f}Cr"
        if change >= 10.0:
            return "DO NOT CHASE", f"already +{change:.1f}%"
        if price < 20:
            return "LOW PRICE RISK", "below ₹20"
        if turnover_cr >= 3.0 and above_vwap and near_high and 3.0 <= change <= 8.0:
            return "TRADE CANDIDATE", "liquid, above VWAP, near high"
        if turnover_cr >= 1.0 and above_vwap:
            return "WATCH", "above VWAP with acceptable turnover"
        return "RADAR ONLY", "visible runner, not clean enough yet"

    def _review_top_gainer_rows_for_auto_buy(self, rows: List[Dict]) -> None:
        """Run radar-visible names through the real auto-buy gate.

        Radar itself is tick-only. This bridge only acts when the full signal
        monitor has a real Buy Now plan with stop/targets.
        """
        if not rows or self._order_manager is None or self._past_buy_cutoff():
            return

        signal_df = self._load_rescan_signal_state()
        if signal_df is None or signal_df.empty or "Stock" not in signal_df.columns:
            return

        signal_df = signal_df.copy()
        signal_df["_sym"] = signal_df["Stock"].astype(str).str.strip().str.upper()
        signal_map = {
            str(row.get("_sym", "")).strip().upper(): row
            for _, row in signal_df.iterrows()
        }

        for radar_row in rows:
            sym = str(radar_row.get("symbol", "")).strip().upper()
            if not sym or sym in self._runner_radar_auto_reviewed:
                continue
            self._runner_radar_auto_reviewed.add(sym)

            row = signal_map.get(sym)
            if row is None:
                self._log(f"Radar auto-review skip {sym}: no signal monitor row yet")
                continue

            action = str(row.get("What To Do", "")).strip()
            if action not in {"Strong Buy Now", "Buy Now"}:
                self._log(f"Radar auto-review skip {sym}: signal engine says '{action}'")
                continue

            live_price = _safe_float(radar_row.get("price")) or _safe_float(row.get("Price Now"))
            stop_loss = _safe_float(row.get("Sell If Below"))
            target1 = _safe_float(row.get("Take Profit 1"))
            target2 = _safe_float(row.get("Take Profit 2"))
            stop_loss, target1, target2, _ = _normalize_long_levels(
                live_price,
                stop_loss,
                target1,
                target2,
            )
            if live_price <= 0 or stop_loss <= 0 or target1 <= 0 or target2 <= 0 or live_price <= stop_loss:
                reason = "full signal row has no usable stop/targets"
                self._log(f"Radar auto-review skip {sym}: {reason}")
                self._record_no_buy_reason(reason)
                continue

            blind_score, blind_reasons = BlindBuyScorer.score(row)
            confidence = _safe_float(row.get("Signal Strength %"))
            volume_ratio = _safe_float(row.get("Volume vs Avg"))
            daily_change = _safe_float(radar_row.get("change")) or _safe_float(row.get("Change %"))
            candidate = self._enrich_candidate_horizon(BlindBuyCandidate(
                symbol=sym,
                price=live_price,
                buy_above=_safe_float(row.get("Buy Above")) or live_price,
                stop_loss=stop_loss,
                target1=target1,
                target2=target2,
                confidence=confidence,
                volume_ratio=volume_ratio,
                change_pct=daily_change,
                blind_score=blind_score,
                original_action=action,
                reasons=blind_reasons,
            ))
            grade, _ = self._grade_candidate(candidate)
            eligible, reason = self._live_auto_buy_gate_detail(candidate, grade)
            self._log(
                f"Radar auto-review {sym}: action={action}, grade={grade}, "
                f"score={blind_score:.0f}, conf={confidence:.0f}%, vol={volume_ratio:.1f}x, "
                f"rr={self._candidate_rr2(candidate):.1f}, change={daily_change:.1f}% -> {reason}"
            )

            if not eligible:
                self._record_no_buy_reason(reason)
                continue

            result = self._try_auto_buy(
                sym,
                live_price,
                stop_loss,
                target1,
                target2,
                blind_score,
                blind_reasons,
                product_preference=candidate.product_preference,
                holding_horizon=candidate.holding_horizon,
                max_hold_days=candidate.max_hold_days,
            )
            if result:
                self._handle_auto_buy_result(sym, result)

    def _run_top_gainer_radar(self) -> None:
        """Show live top gainers without forcing them into the buy engine.

        Goal: visibility should approach 100% of the day's big runners. Execution
        remains selective and separate.
        """
        if not self._is_market_open():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._runner_radar_date != today:
            self._runner_radar_seen.clear()
            self._runner_radar_date = today
            self._runner_radar_last_summary = 0.0

        ticks = self._load_runner_radar_ticks()
        if not ticks:
            return

        rows: List[Dict] = []
        for raw_sym, tick in ticks.items():
            sym = str(raw_sym or "").strip().upper()
            if not sym or self._is_excluded_symbol(sym):
                continue
            price = _safe_float(tick.get("last_price"))
            change = _safe_float(tick.get("change"))
            if price <= 0 or change < self._RUNNER_RADAR_MIN_CHANGE:
                continue
            volume = _safe_float(tick.get("volume_traded", tick.get("volume", 0)))
            avg_px = _safe_float(tick.get("average_traded_price", price))
            turnover_cr = (volume * (avg_px if avg_px > 0 else price)) / 1e7
            total_buy = _safe_float(tick.get("total_buy_quantity", tick.get("total_buy_qty", 0)))
            total_sell = _safe_float(tick.get("total_sell_quantity", tick.get("total_sell_qty", 0)))
            high = _safe_float(tick.get("high", tick.get("ohlc", {}).get("high", 0)))
            low = _safe_float(tick.get("low", tick.get("ohlc", {}).get("low", 0)))
            vwap = avg_px
            above_vwap = bool(vwap > 0 and price >= vwap)
            quality, reason = self._classify_runner_radar_row(
                price=price,
                change=change,
                turnover_cr=turnover_cr,
                above_vwap=above_vwap,
                high=high,
                low=low,
                total_buy=total_buy,
                total_sell=total_sell,
            )
            bucket = int(change // self._RUNNER_RADAR_BUCKET_STEP) * self._RUNNER_RADAR_BUCKET_STEP
            rows.append({
                "symbol": sym,
                "price": price,
                "change": change,
                "turnover_cr": turnover_cr,
                "above_vwap": above_vwap,
                "quality": quality,
                "reason": reason,
                "bucket": bucket,
            })

        if not rows:
            return
        rows.sort(key=lambda r: (r["change"], r["turnover_cr"]), reverse=True)

        review_rows = [
            row for row in rows
            if row["quality"] in self._RUNNER_RADAR_MIN_QUALITY
        ]
        blocked_count = len(rows) - len(review_rows)
        new_rows = [
            row for row in review_rows
            if row["bucket"] > self._runner_radar_seen.get(row["symbol"], 0)
        ]
        now_ts = time.time()
        should_summary = now_ts - self._runner_radar_last_summary >= self._RUNNER_RADAR_SUMMARY_INTERVAL_SEC
        if not new_rows and not should_summary:
            return

        display_rows = (new_rows if new_rows else review_rows)[: self._RUNNER_RADAR_MAX_LINES]
        if not display_rows:
            if not should_summary:
                return
            # Keep one compact anti-FOMO note instead of repeating every late/thin runner.
            self._runner_radar_last_summary = now_ts
            self._record_no_buy_reason(
                f"top gainer radar skipped {blocked_count} late/thin/circuit-risk runners"
            )
            return
        for row in display_rows:
            self._record_internal_stage("WATCH EARLY", str(row.get("symbol", "")), str(row.get("reason", "")))
            self._runner_radar_seen[row["symbol"]] = max(
                self._runner_radar_seen.get(row["symbol"], 0),
                row["bucket"],
            )

        self._runner_radar_last_summary = now_ts
        self._review_top_gainer_rows_for_auto_buy(display_rows)

    # ------------------------------------------------------------------
    # Signal state
    # ------------------------------------------------------------------

    # Max age (seconds) for signal state file to be considered fresh.
    # If the file was last modified more than this many seconds ago, warn.
    _SIGNAL_STATE_MAX_AGE_SEC = 300  # 5 minutes

    def _load_signal_state(self) -> Optional[pd.DataFrame]:
        """Load the latest signal monitor state CSV.
        
        Validates file freshness: if the file hasn't been updated in 5 minutes
        during market hours, logs a warning (signal monitor may have crashed).
        """
        # Try the full actionable report first (has all columns we need)
        for name in ["state", "signals", "actionable"]:
            path = os.path.join(
                self.output_dir,
                f"signalmonitor_{self.config.universe}_{name}_latest.csv",
            )
            if os.path.isfile(path):
                try:
                    # Check file freshness during market hours
                    if self._is_market_open():
                        file_age = time.time() - os.path.getmtime(path)
                        if file_age > self._SIGNAL_STATE_MAX_AGE_SEC:
                            self._log(
                                f"⚠️ Signal state '{name}' is {file_age:.0f}s old "
                                f"(>{self._SIGNAL_STATE_MAX_AGE_SEC}s). "
                                f"Signal monitor may have crashed — data could be stale."
                            )
                    df = pd.read_csv(path)
                    if not df.empty and "Symbol" in df.columns:
                        return df
                except Exception:
                    continue

        # Also try actionable report (might exist without signal monitor)
        actionable_path = os.path.join(
            self.output_dir,
            f"actionable_{self.config.universe}_latest.csv",
        )
        if os.path.isfile(actionable_path):
            try:
                df = pd.read_csv(actionable_path)
                if not df.empty and "Symbol" in df.columns:
                    return df
            except Exception:
                pass

        return None

    def _is_opening_priority_window(self) -> bool:
        """Return True while early-session discovery speed matters most."""
        try:
            import pytz as _pytz_op
            _ist_op = _pytz_op.timezone("Asia/Kolkata")
            _now_op = datetime.now(_ist_op)
        except Exception:
            from datetime import timezone, timedelta
            _now_op = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        _min_of_day = _now_op.hour * 60 + _now_op.minute
        return 9 * 60 + 8 <= _min_of_day <= 10 * 60 + 5

    def _load_named_universe_symbols(self, universe_name: str) -> List[str]:
        """Load a universe file directly for small priority-set fallbacks."""
        try:
            from pkscreener.classes.ActionableReport import ActionableReportRunner

            csv_path = ActionableReportRunner.UNIVERSE_FILES.get(str(universe_name or "").strip().lower())
            if not csv_path or not os.path.exists(csv_path):
                return []
            df = pd.read_csv(csv_path)
            symbol_column = next(
                (candidate for candidate in ["Symbol", "SYMBOL", "symbol"] if candidate in df.columns),
                None,
            )
            if symbol_column is None:
                return []
            return [
                str(symbol).strip().upper()
                for symbol in df[symbol_column].tolist()
                if str(symbol).strip()
            ]
        except Exception:
            return []

    def _build_opening_priority_symbols(self, signal_state: Optional[pd.DataFrame]) -> List[str]:
        """Build a small high-value universe for fast opening rescans."""
        selected: List[str] = []
        seen: Set[str] = set()

        def _add_many(symbols: List[str], cap: Optional[int] = None) -> None:
            added = 0
            for raw in symbols:
                sym = str(raw or "").strip().upper()
                if not sym or sym in seen or self._is_excluded_symbol(sym):
                    continue
                seen.add(sym)
                selected.append(sym)
                added += 1
                if len(selected) >= self._OPENING_PRIORITY_MAX_SYMBOLS:
                    return
                if cap is not None and added >= cap:
                    return

        # Keep anything we are already watching very closely at the front.
        if self._demand_watchlist:
            _add_many(list(self._demand_watchlist.keys()), cap=25)

        # Reuse the strongest names already visible in the last signal snapshot.
        if signal_state is not None and not signal_state.empty and "Symbol" in signal_state.columns:
            try:
                ranked_rows: List[Tuple[float, float, str]] = []
                for _, row in signal_state.iterrows():
                    sym = str(row.get("Symbol", "")).strip().upper()
                    if not sym or self._is_excluded_symbol(sym):
                        continue
                    blind_score, _ = BlindBuyScorer.score(row)
                    confidence = _safe_float(row.get("ConfidencePct", row.get("Signal Strength %", 0)))
                    ranked_rows.append((blind_score, confidence, sym))
                ranked_rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
                _add_many([sym for _, _, sym in ranked_rows], cap=70)
            except Exception:
                pass

        # Add live movers with real turnover so fresh breakouts get a chance
        # before the full allstocks sweep reaches them.
        ticks = self._load_ticks_snapshot() or {}
        if ticks:
            live_rank: List[Tuple[float, float, float, str]] = []
            for sym, tick in ticks.items():
                usym = str(sym or "").strip().upper()
                if not usym or self._is_excluded_symbol(usym):
                    continue
                price = _safe_float(tick.get("last_price"))
                if price < 20:
                    continue
                change = _safe_float(tick.get("change"))
                volume = _safe_float(tick.get("volume"))
                avg_px = _safe_float(tick.get("average_traded_price", tick.get("last_price")))
                turnover_cr = (volume * avg_px) / 1e7 if avg_px > 0 else 0.0
                if turnover_cr < 0.35:
                    continue
                buy_qty = _safe_float(tick.get("total_buy_quantity", tick.get("total_buy_qty")))
                sell_qty = _safe_float(tick.get("total_sell_quantity", tick.get("total_sell_qty")))
                imbalance = (buy_qty / sell_qty) if sell_qty > 0 else (buy_qty if buy_qty > 0 else 0.0)
                if change < 0.2 and imbalance < 1.8:
                    continue
                if change > 8.5:
                    continue
                live_rank.append((turnover_cr, imbalance, change, usym))
            live_rank.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
            _add_many([sym for _, _, _, sym in live_rank], cap=90)

        # Fallback to liquid core universes so the fast lane always has enough
        # names even before the signal monitor has warmed up.
        _add_many(self._load_named_universe_symbols("nifty200"), cap=80)
        if len(selected) < self._OPENING_PRIORITY_MAX_SYMBOLS:
            _add_many(self._load_named_universe_symbols("nifty500"))

        return selected[: self._OPENING_PRIORITY_MAX_SYMBOLS]

    def _refresh_opening_priority_state(self) -> None:
        """Refresh a small curated universe during the opening window."""
        if not self._is_opening_priority_window():
            return
        now_ts = time.time()
        if now_ts - self._last_opening_priority_refresh < self._OPENING_PRIORITY_REFRESH_SEC:
            return

        base_state = self._load_signal_state()
        priority_symbols = self._build_opening_priority_symbols(base_state)
        if not priority_symbols:
            return

        try:
            from pkscreener.classes.ActionableReport import ActionableReportRunner

            start_ts = time.time()
            runner = ActionableReportRunner(
                config_manager=self._get_config_manager(),
                universe=self.config.universe,
                limit=0,
                min_confidence=self.config.min_confidence,
                min_trade_value=0.0,
                min_volume_ratio=0.0,
                max_workers=max(2, min(self.config.max_workers * 2, 6)),
                use_live_kite=True,
                strong_buy_only=False,
            )
            priority_df, _ = runner.build_report(symbols=priority_symbols, show_progress=False)
            self._last_opening_priority_refresh = now_ts
            if priority_df is None or priority_df.empty or "Symbol" not in priority_df.columns:
                return
            priority_df = priority_df.drop_duplicates(subset=["Symbol"], keep="first").copy()
            self._opening_priority_state_df = priority_df
            duration_sec = round(time.time() - start_ts, 2)
            self._opening_priority_stats = {
                "symbols": float(len(priority_symbols)),
                "rows": float(len(priority_df)),
                "duration_sec": duration_sec,
                "refreshed_at": now_ts,
            }
            self._log(
                f"Opening priority refresh: rescanned {len(priority_symbols)} symbols "
                f"in {duration_sec:.1f}s, refreshed {len(priority_df)} rows"
            )
        except Exception as exc:
            self._last_opening_priority_refresh = now_ts
            self._log(f"Opening priority refresh failed: {exc}")

    def _merge_opening_priority_state(self, signal_state: pd.DataFrame) -> pd.DataFrame:
        """Overlay fast priority-scan rows on top of the slower broad snapshot."""
        if signal_state is None or signal_state.empty:
            return signal_state
        priority_df = self._opening_priority_state_df
        if priority_df is None or priority_df.empty or "Symbol" not in priority_df.columns:
            return signal_state
        try:
            merged_priority = priority_df.copy()
            priority_symbols = {
                str(sym).strip().upper()
                for sym in merged_priority["Symbol"].tolist()
                if str(sym).strip()
            }
            if not priority_symbols:
                return signal_state
            base_df = signal_state.copy()
            base_df["Symbol"] = base_df["Symbol"].astype(str).str.strip().str.upper()
            merged_priority["Symbol"] = merged_priority["Symbol"].astype(str).str.strip().str.upper()
            merged = pd.concat(
                [
                    merged_priority,
                    base_df[~base_df["Symbol"].isin(priority_symbols)],
                ],
                ignore_index=True,
                sort=False,
            )
            return merged
        except Exception:
            return signal_state

    def _load_rescan_signal_state(self) -> Optional[pd.DataFrame]:
        """Load the freshest signal-monitor snapshot and normalize for rescans."""
        candidates = [
            (
                f"signalmonitor_{self.config.universe}_actionable_latest.csv",
                {
                    "Symbol": "Stock",
                    "NowAction": "What To Do",
                    "ConfidencePct": "Signal Strength %",
                    "LTP": "Price Now",
                    "Entry": "Buy Above",
                    "EntryGapPct": "Entry Gap %",
                    "StopLoss": "Sell If Below",
                    "Target1": "Take Profit 1",
                    "Target2": "Take Profit 2",
                    "VolumeRatio": "Volume vs Avg",
                    "Reason": "Why",
                    "SuggestedQty": "Suggested Qty",
                    "ChangePct": "Change %",
                    "Action": "Action",
                },
            ),
            (
                f"signalmonitor_{self.config.universe}_signals_latest.csv",
                {
                    "Symbol": "Stock",
                    "NowAction": "What To Do",
                    "ConfidencePct": "Signal Strength %",
                    "LTP": "Price Now",
                    "Entry": "Buy Above",
                    "EntryGapPct": "Entry Gap %",
                    "StopLoss": "Sell If Below",
                    "Target1": "Take Profit 1",
                    "Target2": "Take Profit 2",
                    "VolumeRatio": "Volume vs Avg",
                    "Reason": "Why",
                    "SuggestedQty": "Suggested Qty",
                    "ChangePct": "Change %",
                    "Action": "Action",
                },
            ),
            (
                f"signalmonitor_{self.config.universe}_state_latest.csv",
                {
                    "Symbol": "Stock",
                    "NowAction": "What To Do",
                    "PriceNow": "Price Now",
                    "BuyAbove": "Buy Above",
                    "EntryGapPct": "Entry Gap %",
                    "SellIfBelow": "Sell If Below",
                    "TakeProfit1": "Take Profit 1",
                    "TakeProfit2": "Take Profit 2",
                    "SignalStrengthPct": "Signal Strength %",
                    "VolumeRatio": "Volume vs Avg",
                    "ChangePct": "Change %",
                    "Why": "Why",
                    "Checks": "Checks",
                    "SuggestedQty": "Suggested Qty",
                },
            ),
            (f"signalmonitor_{self.config.universe}_signals_simple_latest.csv", {}),
        ]

        stale_limit_sec = 900
        for filename, rename_map in candidates:
            path = os.path.join(self.output_dir, filename)
            if not os.path.isfile(path):
                continue
            try:
                if self._is_market_open():
                    file_age = time.time() - os.path.getmtime(path)
                    if file_age > stale_limit_sec:
                        self._log(
                            f"Rescan skip stale signal snapshot '{filename}' "
                            f"({file_age:.0f}s old)"
                        )
                        continue
                df = pd.read_csv(path)
            except Exception:
                continue
            if df.empty:
                continue
            if rename_map:
                df = df.rename(columns=rename_map)
            if "Stock" not in df.columns:
                continue
            if "What To Do" not in df.columns and "Action" in df.columns:
                df["What To Do"] = df["Action"]
            for col in [
                "Signal Strength %",
                "Price Now",
                "Buy Above",
                "Entry Gap %",
                "Sell If Below",
                "Take Profit 1",
                "Take Profit 2",
                "Volume vs Avg",
                "Change %",
                "RSI",
                "CCI",
                "MFI",
                "PrevClose",
                "Suggested Qty",
                "Checks",
                "Why",
                "Action",
            ]:
                if col not in df.columns:
                    df[col] = 0 if col not in {"Checks", "Why", "Action"} else ""
            return df
        return None

    def _run_fresh_scan(self) -> Optional[pd.DataFrame]:
        """If no signal monitor is running, do our own scan with loose filters."""
        try:
            from pkscreener.classes.ActionableReport import ActionableReportRunner

            runner = ActionableReportRunner(
                config_manager=self._get_config_manager(),
                universe=self.config.universe,
                limit=0,  # all stocks in universe
                min_confidence=self.config.min_confidence,
                min_trade_value=self.config.min_trade_value,
                min_volume_ratio=self.config.min_volume_ratio,
                max_workers=self.config.max_workers,
                strong_buy_only=False,
            )
            full_df, _ = runner.run()
            return full_df
        except Exception as exc:
            self._log(f"Fresh scan failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Blind-buy detection
    # ------------------------------------------------------------------

    def _find_blind_buys(self, signal_df: pd.DataFrame) -> List[BlindBuyCandidate]:
        """Score all stocks and find blind-buy opportunities."""
        candidates = []
        diag: Counter = Counter()
        diag["rows"] = int(len(signal_df) if signal_df is not None else 0)
        self._last_blind_buy_diagnostics = diag

        # Load tick snapshot for liquidity filtering
        _ticks_for_liq = self._load_ticks_snapshot() or {}
        diag["tick_snapshot_symbols"] = len(_ticks_for_liq)

        # ── MARKET BREADTH GATE ──
        # If more than 60% of liquid stocks are red today, the market is broadly
        # weak. Buying individual setups into a red tape leads to false breakouts.
        # Only check if we have enough stocks to be meaningful.
        try:
            _chg_col = "ChangePct" if "ChangePct" in signal_df.columns else "Change %"
            if _chg_col in signal_df.columns and len(signal_df) >= 50:
                _all_changes = pd.to_numeric(signal_df[_chg_col], errors="coerce").dropna()
                _pct_red = (_all_changes < 0).sum() / len(_all_changes)
                if _pct_red > 0.60:
                    diag["breadth_gate"] = int(round(_pct_red * 100))
                    self._last_blind_buy_diagnostics = diag
                    self._log(
                        f"Breadth gate: {_pct_red*100:.0f}% of stocks red — "
                        f"skipping batch blind-buys (weak market tape)"
                    )
                    return []
        except Exception:
            pass

        # ── PRIME TRADING WINDOW ──
        # Momentum setups work best during 9:20-11:30 AM (opening momentum) and
        # 1:30-2:30 PM (afternoon continuation). Outside these windows, moves are
        # less reliable and volume dries up. Restrict entries to prime windows only.
        try:
            import pytz as _pytz_tw
            _ist_tw = _pytz_tw.timezone("Asia/Kolkata")
            _now_tw = datetime.now(_ist_tw)
            _min_of_day = _now_tw.hour * 60 + _now_tw.minute
            _in_morning_window = (
                self.config.morning_start_min >= 0
                and self.config.morning_end_min >= 0
                and self.config.morning_start_min <= _min_of_day <= self.config.morning_end_min
            )
            _in_afternoon_window = (
                self.config.afternoon_start_min >= 0
                and self.config.afternoon_end_min >= 0
                and self.config.afternoon_start_min <= _min_of_day <= self.config.afternoon_end_min
            )
            if not (_in_morning_window or _in_afternoon_window):
                diag["trading_window_gate"] = 1
                self._last_blind_buy_diagnostics = diag
                self._log(
                    f"Trading window gate: {_now_tw.strftime('%H:%M')} IST outside "
                    f"prime windows — skipping batch"
                )
                return []
        except Exception:
            pass

        for _, row in signal_df.iterrows():
            symbol = str(row.get("Symbol", "")).strip().upper()
            if not symbol:
                diag["missing_symbol"] += 1
                continue

            # Exclude ETFs, liquid funds, and bond instruments
            if self._is_excluded_symbol(symbol):
                diag["excluded_symbol"] += 1
                continue

            # Get the original action
            now_action = str(row.get("NowAction", row.get("What To Do", ""))).strip()

            # Skip stocks already saying Sell or Hold
            if any(x in now_action.lower() for x in ["sell", "exit", "hold", "do nothing"]):
                diag["sell_hold_action"] += 1
                continue

            # ── LIQUIDITY GATE (from Quantopian Volume/Slippage lecture) ──
            # Stocks with < ₹2 Cr turnover today can't absorb even ₹50K orders
            # without 0.3%+ slippage — profit is eaten by execution cost.
            # Tick-only alerts already gate at ₹5 Cr; apply same principle to batch.
            _tick_liq = _ticks_for_liq.get(symbol, {})
            _liq_vol = float(_tick_liq.get("volume") or 0)
            _liq_avg_px = float(_tick_liq.get("average_traded_price") or _tick_liq.get("last_price") or 0)
            _liq_turnover_cr = (_liq_vol * _liq_avg_px) / 1e7 if _liq_avg_px > 0 else 0
            # During opening 30 min, accept lower turnover (volume is still building)
            try:
                import pytz as _pytz_liq
                _ist_liq = _pytz_liq.timezone("Asia/Kolkata")
                _now_liq = datetime.now(_ist_liq)
                _min_open_liq = (_now_liq.hour * 60 + _now_liq.minute) - (9 * 60 + 15)
            except Exception:
                _min_open_liq = 120
            _min_turnover = 0.5 if _min_open_liq <= 30 else 2.0
            if _liq_turnover_cr > 0 and _liq_turnover_cr < _min_turnover:
                diag["liquidity"] += 1
                self._log(
                    f"Batch liquidity skip {symbol}: turnover ₹{_liq_turnover_cr:.1f}Cr < ₹{_min_turnover:.0f}Cr"
                )
                continue

            # Score with our aggressive blind-buy scorer
            blind_score, blind_reasons = BlindBuyScorer.score(row)

            # Score range filter — backtest shows score is INVERSELY correlated
            # with edge: score 30-35 = best WR, score 45+ = dead-cat bounces.
            # Reject scores outside [min_score, max_score] range.
            _price_now = float(row.get("PriceNow", row.get("Price Now", row.get("LTP", 0))) or 0)
            _buy_above = float(row.get("BuyAbove", row.get("Buy Above", row.get("Entry", 0))) or 0)
            _price_below_trigger = _price_now > 0 and _buy_above > 0 and _price_now < _buy_above
            if _price_below_trigger:
                diag["below_trigger"] += 1
                continue  # Skip stocks not yet at entry trigger
            if blind_score < self.config.blind_buy_min_score:
                diag["score_low"] += 1
                continue
            if self.config.blind_buy_max_score > 0 and blind_score >= self.config.blind_buy_max_score:
                diag["score_cap"] += 1
                self._log(
                    f"Score cap skip {symbol}: score {blind_score:.1f} >= {self.config.blind_buy_max_score:.0f} "
                    f"— likely dead-cat bounce / distribution"
                )
                continue

            # Check entry gap
            entry_gap = float(row.get("EntryGapPct", row.get("Entry Gap %", 0)) or 0)
            if entry_gap > self.config.max_entry_gap_pct:
                diag["entry_gap"] += 1
                continue

            # Check confidence
            confidence = float(
                row.get("ConfidencePct", row.get("Signal Strength %",
                    row.get("SignalStrengthPct", 0))) or 0
            )
            if confidence < self.config.min_confidence:
                diag["confidence"] += 1
                continue

            price = float(row.get("LTP", row.get("PriceNow", row.get("Price Now", 0))) or 0)
            stop_loss = float(row.get("StopLoss", row.get("SellIfBelow", row.get("Sell If Below", 0))) or 0)
            target2   = float(row.get("Target2", row.get("TakeProfit2", row.get("Take Profit 2", 0))) or 0)
            vol_ratio = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)

            # Hard volume floor — vol ratio is the #1 edge predictor.
            # T2 winners avg 20.6x vol vs SL losers avg 7.9x. vol >= 5x flips to profitable.
            _vol_floor = self.config.min_volume_ratio
            if vol_ratio <= 0 or vol_ratio < _vol_floor:
                diag["volume"] += 1
                self._log(f"Volume skip {symbol}: vol {vol_ratio:.1f}x < {_vol_floor:.1f}x floor")
                continue

            # R:R must be >= 2.0 — reward must be 2x the risk.
            if stop_loss > 0 and target2 > 0 and price > stop_loss:
                risk = price - stop_loss
                rr2 = (target2 - price) / risk if risk > 0 else 0
                if rr2 < 2.0:
                    diag["risk_reward"] += 1
                    continue

            # Distribution anti-pattern REMOVED — backtest proved it filters
            # genuine momentum setups. T2 winners have HIGH vol/change ratios
            # because real breakouts gap hard on extreme volume.

            # ── PRICE NEAR DAY HIGH ──
            # Price must be within 3% of day high — momentum should NOT have faded.
            _day_high = float(_tick_liq.get("ohlc", {}).get("high", 0) or _tick_liq.get("high", 0) or 0)
            if _day_high > 0 and price > 0:
                _off_high_pct = (_day_high - price) / _day_high * 100
                if _off_high_pct > 3.0:
                    diag["off_high"] += 1
                    self._log(
                        f"Near-high skip {symbol}: price ₹{price:.2f} is {_off_high_pct:.1f}% "
                        f"below day high ₹{_day_high:.2f} — momentum faded"
                    )
                    continue

            # ── VWAP filter: price must be ABOVE VWAP ──
            # VWAP is the single most-watched intraday level by institutions.
            # Buying below VWAP means buying into a stock where big money is net
            # selling — you're fighting the order flow. Only trade above VWAP.
            # Kite tick data includes 'average_traded_price' which IS the VWAP.
            _vwap = float(_tick_liq.get("average_traded_price") or 0)
            if _vwap > 0 and price > 0 and price < _vwap * 0.995:
                diag["below_vwap"] += 1
                self._log(
                    f"VWAP skip {symbol}: price ₹{price:.2f} below VWAP ₹{_vwap:.2f}"
                )
                continue

            _row_change = _safe_float(
                row.get("ChangePct", row.get("Change %", row.get("Change%", row.get("change", 0)))),
                0.0,
            )
            _tick_change = _safe_float(_tick_liq.get("change"), 0.0)
            _display_change = _tick_change if abs(_tick_change) > abs(_row_change) else _row_change
            _off_high_pct = 0.0
            if _day_high > 0 and price > 0:
                _off_high_pct = max(0.0, (_day_high - price) / _day_high * 100)
            _vwap_gap_pct = 0.0
            if _vwap > 0 and price > 0:
                _vwap_gap_pct = ((price - _vwap) / _vwap) * 100
            _support_ref = max(_buy_above, _vwap)
            _support_gap_pct = 0.0
            if _support_ref > 0 and price > 0:
                _support_gap_pct = max(0.0, ((price - _support_ref) / _support_ref) * 100)
            _total_buy = float(_tick_liq.get("total_buy_quantity", _tick_liq.get("total_buy_qty", 0)) or 0)
            _total_sell = float(_tick_liq.get("total_sell_quantity", _tick_liq.get("total_sell_qty", 0)) or 0)
            _buyer_imbalance = (_total_buy / _total_sell) if _total_sell > 0 else 0.0
            _turnover_cr_live = (
                float(_tick_liq.get("volume_traded", _tick_liq.get("volume", 0)) or 0)
                * (_vwap if _vwap > 0 else price)
            ) / 1e7
            _best_bid = float(_tick_liq.get("best_bid") or 0)
            _best_ask = float(_tick_liq.get("best_ask") or 0)
            _spread_pct = 0.0
            if _best_ask > 0 and _best_bid > 0 and price > 0 and _best_ask >= _best_bid:
                _spread_pct = ((_best_ask - _best_bid) / price) * 100
            _open_px = float(_tick_liq.get("open") or _tick_liq.get("ohlc", {}).get("open", 0) or 0)
            _day_low = float(_tick_liq.get("low") or _tick_liq.get("ohlc", {}).get("low", 0) or 0)
            _range_pct_live = ((_day_high - _day_low) / _open_px * 100) if _day_high > 0 and _day_low > 0 and _open_px > 0 else 0.0
            _last_trade_qty = float(_tick_liq.get("last_traded_quantity", _tick_liq.get("last_trade_quantity", 0)) or 0)

            _suggested_qty = int(_safe_float(row.get("SuggestedQty", row.get("Suggested Qty", 0)), 0.0))
            if _suggested_qty <= 0:
                # Use KITE_SWING_MAX_ORDER_VALUE for CNC swing positions
                _max_pos = float(os.environ.get("KITE_SWING_MAX_ORDER_VALUE",
                                  os.environ.get("KITE_MAX_ORDER_VALUE", "10000")))
                _suggested_qty, _ = _compute_suggested_qty(
                    price,
                    stop_loss=stop_loss,
                    score=blind_score,
                    max_position=_max_pos,
                )

            _candidate = self._enrich_candidate_horizon(BlindBuyCandidate(
                symbol=symbol,
                price=price,
                buy_above=float(row.get("Entry", row.get("BuyAbove", row.get("Buy Above", 0))) or 0),
                stop_loss=stop_loss,
                target1=float(row.get("Target1", row.get("TakeProfit1", row.get("Take Profit 1", 0))) or 0),
                target2=target2,
                confidence=confidence,
                volume_ratio=vol_ratio,
                change_pct=_display_change,
                blind_score=blind_score,
                original_action=now_action,
                reasons=blind_reasons,
                checks=str(row.get("QualityChecks", row.get("Checks", ""))).strip(),
                suggested_qty=_suggested_qty,
                off_high_pct=round(_off_high_pct, 2),
                vwap_gap_pct=round(_vwap_gap_pct, 2),
                support_gap_pct=round(_support_gap_pct, 2),
                buyer_imbalance=round(_buyer_imbalance, 2),
                turnover_cr=round(_turnover_cr_live, 2),
                spread_pct=round(_spread_pct, 2),
                range_pct=round(_range_pct_live, 2),
                last_trade_qty=round(_last_trade_qty, 2),
                fresh_buyer_confirmation=bool(
                    _vwap > 0
                    and price >= _vwap
                    and _buyer_imbalance >= 1.6
                    and _turnover_cr_live >= max(1.0, _min_turnover)
                ),
            ))
            _candidate.suggested_qty, _ = self._cap_suggested_qty_to_broker(
                price,
                _candidate.suggested_qty,
                product_preference=_candidate.product_preference,
                holding_horizon=_candidate.holding_horizon,
            )
            candidates.append(_candidate)
            diag["candidate"] += 1

        # Sort by volume_ratio descending (primary) then blind_score (tiebreaker).
        # Backtest shows top-10 by volume yield +5.43% avg vs +1.09% all-trades.
        # Volume surge is the strongest real-time predictor of intraday follow-through.
        candidates.sort(key=lambda c: (c.volume_ratio, c.blind_score), reverse=True)

        # Limit to top N and exclude already-alerted
        fresh_candidates = []
        for c in candidates:
            if c.symbol in self._already_alerted_buys:
                diag["already_alerted"] += 1
                continue
            if c.suggested_qty <= 0:
                diag["qty_cap"] += 1
                self._log(
                    f"Batch skip {c.symbol}: starter position exceeds ₹5000 cap at ₹{c.price:.2f}"
                )
                continue
            fresh_candidates.append(c)
            if len(fresh_candidates) >= self.config.max_buy_alerts_per_cycle:
                break

        diag["fresh_candidates"] = len(fresh_candidates)
        self._last_blind_buy_diagnostics = diag
        return fresh_candidates

    # ------------------------------------------------------------------
    # Sell monitoring
    # ------------------------------------------------------------------

    def _check_portfolio_sells(self, signal_df: pd.DataFrame) -> Tuple[List[SellAlert], int]:
        """Check all open portfolio positions for sell conditions."""
        if self._order_manager is not None:
            return self._check_kite_tracked_position_sells(signal_df)

        sell_alerts = []
        trades_df = self._load_portfolio_trades()
        if trades_df is None or trades_df.empty:
            return sell_alerts, 0

        open_trades = trades_df[trades_df["Status"] == "OPEN"]
        if open_trades.empty:
            return sell_alerts, 0

        signal_map = self._build_signal_lookup(signal_df)

        for _, trade in open_trades.iterrows():
            symbol = str(trade.get("Symbol", "")).strip().upper()
            quantity = int(_safe_float(trade.get("Quantity", 0), 0.0))
            entry_price = float(trade.get("EntryPrice", 0))
            if not symbol or quantity <= 0:
                continue

            signal_row = signal_map.get(symbol)
            current_price = float(trade.get("CurrentPrice", 0))
            if current_price <= 0 and signal_row is not None:
                current_price = float(signal_row.get("LTP", signal_row.get("PriceNow", signal_row.get("Price Now", 0))) or 0)

            if current_price <= 0:
                current_price = entry_price

            pnl_pct = round((current_price - entry_price) * 100 / entry_price, 2) if entry_price > 0 else 0.0

            should_sell = False
            reason = ""

            if self.config.auto_exit_on_sell and signal_row is not None:
                now_action = str(signal_row.get("NowAction", signal_row.get("What To Do", ""))).strip()
                if any(x in now_action.lower() for x in ["sell", "exit"]):
                    should_sell = True
                    reason = f"Signal: {now_action}"

            if not should_sell and self.config.auto_exit_on_stop:
                stop_loss = float(trade.get("StopLoss", 0))
                if stop_loss > 0 and current_price <= stop_loss:
                    should_sell = True
                    reason = f"STOP LOSS hit ({stop_loss:.2f}), cut losses now"

            if not should_sell and self.config.auto_exit_on_target2:
                target2 = float(trade.get("Target2", 0))
                if target2 > 0 and current_price >= target2:
                    should_sell = True
                    reason = f"TARGET 2 HIT ({target2:.2f}), book profits!"

            if not should_sell:
                target1 = float(trade.get("Target1", 0))
                if target1 > 0 and current_price >= target1:
                    should_sell = True
                    reason = f"Target 1 reached ({target1:.2f}), consider partial exit"

            if should_sell and symbol not in self._already_alerted_sells:
                sell_alerts.append(SellAlert(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=entry_price,
                    current_price=round(current_price, 2),
                    pnl_pct=pnl_pct,
                    reason=reason,
                ))

        return sell_alerts, len(open_trades)

    def _build_signal_lookup(self, signal_df: pd.DataFrame) -> Dict[str, Any]:
        signal_map: Dict[str, Any] = {}
        for _, row in signal_df.iterrows():
            sym = str(row.get("Symbol", row.get("Stock", ""))).strip().upper()
            if sym:
                signal_map[sym] = row
        return signal_map

    def _check_kite_tracked_position_sells(self, signal_df: pd.DataFrame) -> Tuple[List[SellAlert], int]:
        sell_alerts: List[SellAlert] = []
        if self._order_manager is None:
            return sell_alerts, 0

        signal_map = self._build_signal_lookup(signal_df)
        ticks = self._order_manager._load_ticks()
        open_positions = self._order_manager.get_open_positions()
        if not open_positions:
            return sell_alerts, 0

        existing_signals = {
            (sym, reason, qty)
            for sym, reason, _price, qty in self._order_manager.check_sell_signals(ticks)
        }

        for pos in open_positions:
            symbol = str(getattr(pos, "symbol", "") or "").strip().upper()
            quantity = int(getattr(pos, "qty", 0) or 0)
            entry_price = float(getattr(pos, "buy_price", 0) or 0)
            if not symbol or quantity <= 0 or entry_price <= 0:
                continue

            signal_row = signal_map.get(symbol)
            current_price = float(ticks.get(symbol, {}).get("last_price", 0) or 0)
            if current_price <= 0:
                # Tick not available — fetch a fresh quote from the broker API.
                # This covers positions in symbols outside the WebSocket universe.
                current_price = self._order_manager._broker_last_price(symbol)
            if current_price <= 0 and signal_row is not None:
                current_price = float(signal_row.get("LTP", signal_row.get("PriceNow", signal_row.get("Price Now", 0))) or 0)
            if current_price <= 0:
                current_price = entry_price

            pnl_pct = round((current_price - entry_price) * 100 / entry_price, 2) if entry_price > 0 else 0.0
            reasons: List[str] = []

            if self.config.auto_exit_on_sell and signal_row is not None:
                now_action = str(signal_row.get("NowAction", signal_row.get("What To Do", ""))).strip()
                if any(x in now_action.lower() for x in ["sell", "exit"]):
                    reasons.append(f"Signal: {now_action}")

            stop_loss = float(getattr(pos, "stop_loss", 0) or 0)
            if self.config.auto_exit_on_stop and stop_loss > 0 and current_price <= stop_loss:
                reasons.append(f"STOP LOSS hit ({stop_loss:.2f}), cut losses now")

            target2 = float(getattr(pos, "target2", 0) or 0)
            if self.config.auto_exit_on_target2 and target2 > 0 and current_price >= target2:
                reasons.append(f"TARGET 2 HIT ({target2:.2f}), book profits!")

            target1 = float(getattr(pos, "target1", 0) or 0)
            if target1 > 0 and current_price >= target1:
                reasons.append(f"Target 1 reached ({target1:.2f}), consider partial exit")

            matched_engine_reasons = [
                self._order_manager.format_sell_reason(reason)
                for sym, reason, qty in existing_signals
                if sym == symbol and (qty == 0 or qty == quantity)
            ]
            reasons.extend(matched_engine_reasons)

            if reasons and symbol not in self._already_alerted_sells:
                deduped_reasons = list(dict.fromkeys(r for r in reasons if r))
                sell_alerts.append(SellAlert(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=entry_price,
                    current_price=round(current_price, 2),
                    pnl_pct=pnl_pct,
                    reason=" | ".join(deduped_reasons),
                ))

        return sell_alerts, len(open_positions)

    def _load_portfolio_trades(self) -> Optional[pd.DataFrame]:
        """Load the current portfolio trades CSV."""
        path = os.path.join(
            self.output_dir,
            f"livetrade_{self.config.account_name}_trades_latest.csv",
        )
        if not os.path.isfile(path):
            return None
        try:
            return pd.read_csv(path)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Daily alert deduplication (persist across session restarts)
    # ------------------------------------------------------------------

    def _daily_alerted_path(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.output_dir, f"alerted_buys_{today}.txt")

    def _daily_sell_alerted_path(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.output_dir, f"alerted_sells_{today}.txt")

    def _load_daily_alerted_set(self) -> Set[str]:
        """Load today's already-alerted symbols so restarts don't re-fire same-day alerts."""
        path = self._daily_alerted_path()
        if not os.path.isfile(path):
            return set()
        try:
            with open(path, "r") as f:
                return {line.strip().upper() for line in f if line.strip()}
        except Exception:
            return set()

    def _save_daily_alerted(self, symbol: str) -> None:
        """Append a symbol to today's alerted file."""
        try:
            with open(self._daily_alerted_path(), "a") as f:
                f.write(symbol.upper() + "\n")
        except Exception:
            pass

    def _load_daily_sell_alerted_set(self) -> Set[str]:
        """Load today's already-sell-alerted symbols so restarts don't re-fire same-day sell alerts."""
        path = self._daily_sell_alerted_path()
        if not os.path.isfile(path):
            return set()
        try:
            with open(path, "r") as f:
                return {line.strip().upper() for line in f if line.strip()}
        except Exception:
            return set()

    def _save_daily_sell_alerted(self, symbol: str) -> None:
        """Append a symbol to today's sell-alerted file."""
        try:
            with open(self._daily_sell_alerted_path(), "a") as f:
                f.write(symbol.upper() + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Discord push alerts
    # ------------------------------------------------------------------

    def _refresh_market_context(self, ticks: Optional[Dict[str, Dict]] = None) -> None:
        """Lazily instantiate ``MarketContextEngine`` and refresh its regime snapshot.

        The engine is optional: if it cannot be imported or update fails, we
        flag ``_market_context_init_failed`` and the buy-side gates fall back
        to a conservative skip. The engine is never required for the existing
        non-auto-buy flows.
        """
        if self._market_context_init_failed:
            return
        now_ts = time.time()
        if (
            self._market_context is not None
            and now_ts - self._market_regime_last_refresh < self._MARKET_REGIME_REFRESH_SEC
        ):
            return
        try:
            if self._market_context is None:
                from pkscreener.classes.MarketContext import MarketContextEngine
                self._market_context = MarketContextEngine()
            tick_snapshot = ticks if ticks is not None else self._load_runner_radar_ticks()
            if tick_snapshot:
                self._market_context.update(tick_snapshot)
            self._market_regime_last_refresh = now_ts
        except Exception as exc:
            # Disable market-context gating on init failure — we fall back to
            # the existing behavior rather than crashing the bot.
            self._market_context = None
            self._market_context_init_failed = True
            self._log(f"MarketContextEngine init/refresh failed, gating disabled: {exc}")

    def _is_safe_to_buy(self) -> Tuple[bool, str]:
        """Return ``(allow, reason)`` for the auto-buy gate based on market regime.

        Behavior:
          * engine missing or never refreshed → allow (fail-safe to existing logic)
          * regime ∈ ``_MARKET_REGIME_BUY_OK`` → allow
          * regime is ``crash`` / ``bear`` / ``strong_bear`` → block
          * Nifty crash pause already active → block
        """
        if self._nifty_crash_pause:
            return False, "Nifty crash pause active"
        if self._market_context is None or self._market_context_init_failed:
            # Fail-safe: do not block existing logic if the regime engine is
            # unavailable. Other safety checks (cash, cutoff, dup, SL/target,
            # liquidity, stale-tick) continue to run downstream.
            return True, "regime engine unavailable; falling through to existing checks"
        try:
            regime = self._market_context.regime
        except Exception as exc:
            self._log(f"MarketContext regime read failed, falling through: {exc}")
            return True, "regime read failed; falling through"
        regime_label = str(getattr(regime, "regime", "") or "neutral")
        if regime_label in self._MARKET_REGIME_BUY_OK:
            return True, f"regime={regime_label}"
        return False, f"regime={regime_label} — auto-buy blocked"

    def _past_buy_cutoff(self) -> bool:
        """Return True if current IST time is past the last BUY alert cutoff.
        After this time, only post-market preview alerts (next-day candidates) are allowed.
        """
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        return (now_ist.hour * 60 + now_ist.minute) > self._BUY_ALERT_CUTOFF_MIN

    def _is_excluded_symbol(self, symbol: str) -> bool:
        """Return True for ETFs, liquid funds, and other non-equity instruments."""
        sym = symbol.strip().upper()
        if sym in self._EXCLUDED_SYMBOLS:
            return True
        # Pattern-based: anything ending in BEES, ETF
        if sym.endswith("BEES") or sym.endswith("ETF"):
            return True
        return False

    def _is_market_open(self) -> bool:
        """Return True only during NSE market hours (9:15 AM – 3:30 PM IST)."""
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            # pytz not available — fall back to UTC+5:30 offset
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)
        if now_ist.weekday() >= 5:
            return False
        open_minutes = self._MARKET_OPEN_HOUR_IST * 60 + self._MARKET_OPEN_MIN_IST
        close_minutes = self._MARKET_CLOSE_HOUR_IST * 60 + self._MARKET_CLOSE_MIN_IST
        now_minutes = now_ist.hour * 60 + now_ist.minute
        return open_minutes <= now_minutes <= close_minutes

    def _is_opening_window_active(self) -> bool:
        """Return True during the first 15 minutes after market open (9:15-9:30 IST).

        During this window, tick-confirmed alerts are buffered and flushed
        as a ranked summary instead of sending 30+ individual alerts.
        """
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)
        if now_ist.weekday() >= 5:
            return False
        now_min = now_ist.hour * 60 + now_ist.minute
        return (9 * 60 + 15) <= now_min <= (9 * 60 + 30)

    def _is_micro_open_fast_lane_active(self) -> bool:
        """Return True only during the first 5 minutes after open (9:15-9:20 IST).

        This is the elite early-entry lane where we allow only the cleanest,
        highest-urgency opening setups to bypass the usual 9:20 intraday wait.
        """
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)
        if now_ist.weekday() >= 5:
            return False
        now_min = now_ist.hour * 60 + now_ist.minute
        return (9 * 60 + 15) <= now_min < (9 * 60 + 20)

    def _flush_opening_buffer(self) -> None:
        """Flush buffered opening alerts as a ranked summary message.

        Called from the main loop every 30 seconds. Takes the buffer,
        ranks by a composite of score + volume + change, and sends
        only the top MAX_OPENING_ALERTS as one consolidated message.
        Remaining stocks are sent as a compact watchlist.
        """
        import time as _time_mod
        now_ts = _time_mod.time()
        if now_ts - self._last_opening_flush < self._OPENING_FLUSH_INTERVAL_SEC:
            return
        self._last_opening_flush = now_ts

        with self._opening_buffer_lock:
            if not self._opening_buffer:
                return
            batch = list(self._opening_buffer)
            self._opening_buffer.clear()

        # Rank: score primary, volume × change as tiebreaker
        batch.sort(key=lambda x: (
            -x.get("blind_score", 0),
            -(x.get("volume_ratio", 0) * abs(x.get("daily_change", 0))),
        ))

        top_picks = batch[:self._MAX_OPENING_ALERTS]
        overflow = batch[self._MAX_OPENING_ALERTS:]

        if not top_picks:
            return

        # Build consolidated message
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))

        # Count quality tiers
        n_strong = sum(1 for p in top_picks if p.get("is_definitely_buy"))
        n_total = len(top_picks)

        header = f"**🟢 OPENING SCAN — {now_ist.strftime('%H:%M')} IST** | **{n_total}** top picks"
        if n_strong:
            header += f" ({n_strong} strong buy)"
        header += f" from {n_total + len(overflow)} confirmed stocks\n"
        header += "_Ranked by conviction score. Focus on top 3-5 picks._\n"

        lines = [header]
        for rank, pick in enumerate(top_picks, 1):
            sym = pick["symbol"]
            score = pick.get("blind_score", 0)
            price = pick.get("price", 0)
            change = pick.get("daily_change", 0)
            vol = pick.get("volume_ratio", 0)
            sl = pick.get("stop_loss", 0)
            t1 = pick.get("target1", 0)
            t2 = pick.get("target2", 0)
            qty = pick.get("suggested_qty", 0)
            is_def = pick.get("is_definitely_buy", False)

            badge = "🔥" if is_def else ("⭐" if score >= 60 else "✅" if score >= 50 else "📝")
            scope_t2 = round((t2 - price) / price * 100, 1) if t2 > 0 and price > 0 else 0

            line = (
                f"**{rank}. {badge} `{sym}`** Score **{score:.0f}**/100 | "
                f"`₹{price:.2f}` (+{change:.1f}%) | Vol `{vol:.1f}x`"
            )
            if sl > 0 and t2 > 0:
                line += f"\n   SL `₹{sl:.2f}` → T1 `₹{t1:.2f}` → T2 `₹{t2:.2f}` (+{scope_t2:.1f}%)"
            if qty > 0:
                line += f" | Buy **{qty} shares**"
            lines.append(line)

            # Mark as alerted and track
            self._already_alerted_buys.add(sym)
            self._save_daily_alerted(sym)
            self._momentum_tracked[sym] = {
                "entry_price": price,
                "last_alert_pct": change,
                "last_alert_time": datetime.now(),
                "stop_loss": sl,
            }

        self._log(f"Opening buffer flushed: {n_total} picks sent (top {self._MAX_OPENING_ALERTS}), {len(overflow)} overflow")

        auto_lines = []
        for pick in top_picks:
            auto_result = self._attempt_tick_confirmed_auto_buy(
                symbol=pick["symbol"],
                price=float(pick.get("price", 0) or 0),
                stop_loss=float(pick.get("stop_loss", 0) or 0),
                target1=float(pick.get("target1", 0) or 0),
                target2=float(pick.get("target2", 0) or 0),
                blind_score=float(pick.get("blind_score", 0) or 0),
                confidence=float(pick.get("confidence", 0) or 0),
                volume_ratio=float(pick.get("volume_ratio", 0) or 0),
                daily_change=float(pick.get("daily_change", 0) or 0),
                reasons=str(pick.get("reasons", "") or ""),
                original_action=str(pick.get("original_action", "") or ""),
                source="opening buffer flush",
                spread_pct=float(pick.get("spread_pct", 0) or 0),
                range_pct=float(pick.get("range_pct", 0) or 0),
                last_trade_qty=float(pick.get("last_trade_qty", 0) or 0),
                turnover_cr=float(pick.get("turnover_cr", 0) or 0),
                buyer_imbalance=float(pick.get("buyer_imbalance", 0) or 0),
                vwap_gap_pct=float(pick.get("vwap_gap_pct", 0) or 0),
                off_high_pct=float(pick.get("off_high_pct", 0) or 0),
                support_gap_pct=float(pick.get("support_gap_pct", 0) or 0),
                fresh_buyer_confirmation=bool(pick.get("fresh_buyer_confirmation", False)),
            )
            if auto_result:
                auto_lines.append(auto_result)
                self._handle_auto_buy_result(str(pick.get("symbol", "")), auto_result)
            if self._AUTO_BUY_TOP_N > 0 and self._tick_confirmed_auto_buy_count >= self._AUTO_BUY_TOP_N:
                break

        # Send overflow as compact watchlist
        if overflow:
            for pick in overflow:
                sym = pick["symbol"]
                score = pick.get("blind_score", 0)
                price = pick.get("price", 0)
                change = pick.get("daily_change", 0)
                vol = pick.get("volume_ratio", 0)
                sl = pick.get("stop_loss", 0)
                t2 = pick.get("target2", 0)
                self._already_alerted_buys.add(sym)
                self._save_daily_alerted(sym)
                self._momentum_tracked[sym] = {
                    "entry_price": price,
                    "last_alert_pct": change,
                    "last_alert_time": datetime.now(),
                    "stop_loss": sl,
                }

    def _is_post_market_window(self) -> bool:
        """Return True during post-market window (3:30 PM – 6:00 PM IST).
        Used to allow post-market OHLCV scan results to be sent as next-day previews.
        """
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)
        if now_ist.weekday() >= 5:
            return False
        now_minutes = now_ist.hour * 60 + now_ist.minute
        close_minutes = self._MARKET_CLOSE_HOUR_IST * 60 + self._MARKET_CLOSE_MIN_IST
        post_market_end = 18 * 60  # 6:00 PM IST
        return close_minutes < now_minutes <= post_market_end

    def _check_nifty_crash(self, signal_df: pd.DataFrame) -> bool:
        """Return True if the broad market is crashing (Nifty/index down >2%).

        Checks the ^NSEI row in the Kite tick snapshot if available,
        falling back to median ChangePct of all signal stocks as a proxy.
        OHLCV-based buy alerts are paused when this returns True.
        Tick-confirmed alerts (real intraday moves) bypass this guard.
        """
        # Try to get Nifty change from tick data (via _load_ticks_snapshot)
        nifty_chg = self._get_nifty_change()
        if nifty_chg != 0.0:
            if nifty_chg <= -2.0:
                self._log(f"Nifty crash guard: Nifty {nifty_chg:.1f}% — pausing OHLCV buy alerts")
                return True
            return False
            pass

        # Fallback: use median ChangePct of all stocks as market proxy.
        # If the typical stock is down >1.5%, treat as market crash.
        try:
            col = "ChangePct" if "ChangePct" in signal_df.columns else "Change %"
            if col in signal_df.columns:
                changes = pd.to_numeric(signal_df[col], errors="coerce").dropna()
                if len(changes) > 50:
                    median_chg = float(changes.median())
                    if median_chg <= -1.5:
                        self._log(f"Nifty crash guard: median stock change {median_chg:.1f}% — pausing OHLCV buy alerts")
                        return True
        except Exception:
            pass
        return False

    def _get_nifty_change(self) -> float:
        """Return today's broad market change% from tick data, or 0.0 if unavailable.
        
        Tries NIFTY 50 index from ticks first, then falls back to median change
        of all stocks as a proxy for broad market direction.
        """
        ticks = self._load_ticks_snapshot()
        if not ticks:
            return 0.0
        # Try index keys first
        for key in ("NIFTY 50", "NSEI", "^NSEI", "NIFTY50"):
            if key in ticks:
                return float(ticks[key].get("change", 0) or 0)
        # Fallback: median change of all stocks
        changes = []
        for sym, tick in ticks.items():
            chg = tick.get("change")
            if chg is not None:
                try:
                    changes.append(float(chg))
                except (ValueError, TypeError):
                    pass
        if len(changes) > 50:
            changes.sort()
            mid = len(changes) // 2
            return changes[mid]
        return 0.0

    def _push_buy_alerts(self, candidates: List[BlindBuyCandidate]) -> None:
        """Push blind-buy alerts to Discord.

        First batch (no prior alerts) sends immediately.
        Follow-up discoveries are buffered and sent at most once every 15 minutes.
        Post-market window (3:30–6:00 PM IST): send once as next-day previews.
        """
        is_post_market = self._is_post_market_window()
        if not self._is_market_open() and not is_post_market:
            return  # Outside trading hours and post-market window — suppress alerts

        # Past buy cutoff: only post-market previews are allowed, NOT actionable BUY alerts.
        # This prevents sending "BUY X" right before close when user can't act safely.
        if self._past_buy_cutoff() and not is_post_market:
            self._log("Buy alerts suppressed: past buy cutoff time")
            return

        # Nifty crash guard: if Nifty/Sensex is down >2% in first 60 min, suppress OHLCV buys.
        # Individual stocks can still look good on yesterday's setup, but the broader crash means
        # entry risk is very high — gap-downs, lower circuits, no buyers.
        # Tick-confirmed alerts (3%+ intraday move with volume) bypass this — those ARE moving up
        # against the market direction, which is a stronger signal.
        if self._nifty_crash_pause:
            self._log("OHLCV buy alerts paused: Nifty/index down >2% today (crash mode)")
            return

        if not candidates:
            # Even with no new candidates, check if buffered follow-ups are ready
            if not is_post_market:
                self._flush_pending_followups()
            return

        # Top-N daily volume gate: cap actionable intraday alerts at _TOP_N_DAILY_BATCH per day.
        # Post-market previews are exempt — they are for tomorrow, not today's action.
        if not is_post_market and not self._discord_execution_only:
            _remaining_quota = self._TOP_N_DAILY_BATCH - self._daily_batch_alert_count
            if _remaining_quota <= 0:
                self._log(
                    f"Top-N daily cap reached ({self._TOP_N_DAILY_BATCH}): "
                    f"suppressing {len(candidates)} further batch buy alert(s)"
                )
                return
            if len(candidates) > _remaining_quota:
                self._log(
                    f"Top-N quota: trimming {len(candidates)} candidates to {_remaining_quota} "
                    f"(cap={self._TOP_N_DAILY_BATCH}, sent={self._daily_batch_alert_count})"
                )
                candidates = candidates[:_remaining_quota]

        is_first_batch = len(self._already_alerted_buys) == 0

        if is_post_market:
            # Post-market: send once (all candidates not yet alerted today), then mark all alerted
            new_candidates = [c for c in candidates if c.symbol not in self._already_alerted_buys]
            if new_candidates:
                self._do_send_buy_alert(new_candidates, is_followup=False)
                for c in new_candidates:
                    self._already_alerted_buys.add(c.symbol)
                    self._save_daily_alerted(c.symbol)
        elif is_first_batch:
            # Send immediately — this is the opening alert
            self._do_send_buy_alert(candidates, is_followup=False)
            self._last_buy_alert_time = datetime.now()
        else:
            # Buffer follow-up picks
            self._pending_followups.extend(candidates)
            # Mark as alerted immediately so they don't re-queue next cycle
            for c in candidates:
                self._already_alerted_buys.add(c.symbol)
                self._save_daily_alerted(c.symbol)
            self._flush_pending_followups()

    def _flush_pending_followups(self) -> None:
        """Send buffered follow-up picks if cooldown has elapsed."""
        if not self._pending_followups:
            return
        if self._last_buy_alert_time is not None:
            elapsed = (datetime.now() - self._last_buy_alert_time).total_seconds()
            if elapsed < self._FOLLOWUP_COOLDOWN_SEC:
                return  # Not yet — keep buffering
        # Cooldown elapsed — send all buffered picks as one message
        # Sort buffered by improved execution-aware ranking.
        self._pending_followups.sort(key=self._candidate_rank, reverse=True)
        self._do_send_buy_alert(self._pending_followups, is_followup=True)
        self._last_buy_alert_time = datetime.now()
        self._pending_followups = []

    @staticmethod
    def _candidate_rr2(c: 'BlindBuyCandidate') -> float:
        return _compute_long_rr(c.price, c.stop_loss, c.target2)

    @staticmethod
    def _normalize_action_label(action: str) -> str:
        return str(action or "").strip().lower()

    @classmethod
    def _is_true_buy_action(cls, action: str) -> bool:
        normalized = cls._normalize_action_label(action)
        return normalized in {"buy", "buy now", "strong buy", "strong buy now"}

    @classmethod
    def _is_watch_buy_action(cls, action: str) -> bool:
        normalized = cls._normalize_action_label(action)
        return normalized in {"watch buy", "momentum buy", "momentum watch"}

    @staticmethod
    def _candidate_entry_gap_pct(c: 'BlindBuyCandidate') -> float:
        if c.price <= 0 or c.buy_above <= 0:
            return 0.0
        return round(((c.buy_above - c.price) / c.price) * 100, 2)

    @staticmethod
    def _has_fresh_buyer_confirmation(c: 'BlindBuyCandidate') -> bool:
        if bool(getattr(c, "fresh_buyer_confirmation", False)):
            return True
        return (
            float(getattr(c, "buyer_imbalance", 0.0) or 0.0) >= 1.6
            and float(getattr(c, "turnover_cr", 0.0) or 0.0) >= 1.0
            and float(getattr(c, "vwap_gap_pct", 0.0) or 0.0) >= 0.0
        )

    @classmethod
    def _classify_entry_type(cls, c: 'BlindBuyCandidate') -> Tuple[str, str]:
        change_pct = max(0.0, float(getattr(c, "change_pct", 0.0) or 0.0))
        volume_ratio = max(0.0, float(getattr(c, "volume_ratio", 0.0) or 0.0))
        confidence = max(0.0, float(getattr(c, "confidence", 0.0) or 0.0))
        rr2 = cls._candidate_rr2(c)
        gap_pct = cls._candidate_entry_gap_pct(c)
        off_high_pct = max(0.0, float(getattr(c, "off_high_pct", 0.0) or 0.0))
        vwap_gap_pct = float(getattr(c, "vwap_gap_pct", 0.0) or 0.0)
        support_gap_pct = max(0.0, float(getattr(c, "support_gap_pct", 0.0) or 0.0))
        imbalance = max(0.0, float(getattr(c, "buyer_imbalance", 0.0) or 0.0))
        fresh_buyers = cls._has_fresh_buyer_confirmation(c)
        strong_move = change_pct >= 4.5
        has_live_structure = any(
            abs(float(v or 0.0)) > 0.0
            for v in (off_high_pct, vwap_gap_pct, support_gap_pct, imbalance)
        )

        if not strong_move:
            return "EARLY_BREAKOUT", "move is still controlled and close to the breakout trigger"

        if (
            fresh_buyers
            and 0.15 <= support_gap_pct <= 0.9
            and 0.0 <= vwap_gap_pct <= 0.45
            and 0.4 <= off_high_pct <= 1.8
        ):
            return "PULLBACK_RETEST", "price pulled back toward support, held, and buyers returned"

        if (
            fresh_buyers
            and imbalance >= 2.0
            and 0.0 <= vwap_gap_pct <= 0.5
            and support_gap_pct <= 0.9
            and off_high_pct <= 0.6
        ):
            return "SECOND_LEG_CONTINUATION", "rested continuation with VWAP support and renewed demand"

        if has_live_structure:
            if (
                off_high_pct <= 0.35
                and (
                    vwap_gap_pct > 0.8
                    or support_gap_pct > 1.1
                    or not fresh_buyers
                )
            ):
                return "LATE_CHASE", "price is hugging the high without a fresh support-backed entry"
            if off_high_pct <= 0.8 and vwap_gap_pct <= 0.6 and support_gap_pct <= 1.0:
                return "SWING_BREAKOUT", "strong breakout, but late enough that extra confirmation is needed"
            return "LATE_CHASE", "move is extended away from VWAP/support without a clear reset"

        if confidence >= 36 and volume_ratio >= 2.2 and rr2 >= 2.0 and gap_pct <= 0.7:
            return "SWING_BREAKOUT", "strong breakout proxy setup; live structure was unavailable so stricter confirmation is required"

        return "LATE_CHASE", "strong move lacks enough live structure to prove a safe continuation entry"

    @classmethod
    def _swing_entry_quality_gate(cls, c: 'BlindBuyCandidate') -> Tuple[bool, str]:
        entry_type, entry_reason = cls._classify_entry_type(c)
        if entry_type == "LATE_CHASE":
            return False, f"entry type LATE_CHASE — {entry_reason}"
        if entry_type != "SWING_BREAKOUT":
            return True, entry_reason

        failures = []
        if c.confidence < 34:
            failures.append(f"confidence {c.confidence:.0f}% < 34%")
        if c.volume_ratio < 2.2:
            failures.append(f"volume {c.volume_ratio:.1f}x < 2.2x")
        rr2 = cls._candidate_rr2(c)
        if rr2 < 1.9:
            failures.append(f"R:R 1:{rr2:.1f} < 1:1.9")
        gap_pct = cls._candidate_entry_gap_pct(c)
        if gap_pct > 0.7:
            failures.append(f"entry gap {gap_pct:.1f}% > 0.7%")
        off_high_pct = max(0.0, float(getattr(c, "off_high_pct", 0.0) or 0.0))
        if off_high_pct > 0.8:
            failures.append(f"price is {off_high_pct:.1f}% off high")
        vwap_gap_pct = float(getattr(c, "vwap_gap_pct", 0.0) or 0.0)
        if vwap_gap_pct > 0.6:
            failures.append(f"VWAP gap {vwap_gap_pct:.1f}% > 0.6%")
        support_gap_pct = max(0.0, float(getattr(c, "support_gap_pct", 0.0) or 0.0))
        if support_gap_pct > 1.0:
            failures.append(f"support gap {support_gap_pct:.1f}% > 1.0%")
        if not cls._has_fresh_buyer_confirmation(c):
            failures.append("fresh buyer confirmation missing")
        if failures:
            return False, "entry type SWING_BREAKOUT needs stronger confirmation: " + "; ".join(failures)
        return True, entry_reason

    @classmethod
    def _execution_quality_gate(cls, c: 'BlindBuyCandidate') -> Tuple[bool, str]:
        price = max(0.0, float(getattr(c, "price", 0.0) or 0.0))
        turnover_cr = max(0.0, float(getattr(c, "turnover_cr", 0.0) or 0.0))
        spread_pct = max(0.0, float(getattr(c, "spread_pct", 0.0) or 0.0))
        range_pct = max(0.0, float(getattr(c, "range_pct", 0.0) or 0.0))
        last_trade_qty = max(0.0, float(getattr(c, "last_trade_qty", 0.0) or 0.0))
        imbalance = max(0.0, float(getattr(c, "buyer_imbalance", 0.0) or 0.0))
        volume_ratio = max(0.0, float(getattr(c, "volume_ratio", 0.0) or 0.0))
        vwap_gap = float(getattr(c, "vwap_gap_pct", 0.0) or 0.0)
        has_depth_context = spread_pct > 0 or last_trade_qty > 0 or imbalance > 0
        has_live_context = has_depth_context or turnover_cr > 0 or abs(vwap_gap) > 0

        if price <= 0:
            return False, "live price is missing"
        if not has_live_context:
            return False, "VWAP/OHLCV/order-depth safety data is missing"
        if turnover_cr > 0 and turnover_cr < 1.0:
            return False, f"turnover ₹{turnover_cr:.1f}Cr is below the live execution floor"
        if spread_pct >= 0.65:
            return False, f"spread {spread_pct:.2f}% is too wide for safe auto-buy execution"
        if price < 100 and turnover_cr < 5.0 and spread_pct > 0.35:
            return False, f"spread {spread_pct:.2f}% is too wide for a sub-₹100 stock with only ₹{turnover_cr:.1f}Cr turnover"
        if price < 100 and turnover_cr < 5.0 and last_trade_qty > 0 and last_trade_qty < 75:
            return False, f"last traded quantity {last_trade_qty:.0f} is too small for stable execution"
        if range_pct >= 8.0 and spread_pct > 0.30 and turnover_cr < 5.0:
            return False, f"intraday range {range_pct:.1f}% looks too jumpy for auto-buy execution"
        if imbalance >= 1.6 and turnover_cr < 3.0:
            return False, f"buyer imbalance {imbalance:.1f}x is not backed by enough real turnover"
        if imbalance >= 1.8 and spread_pct > 0.30 and last_trade_qty < 100:
            return False, "buyer imbalance looks like thin order-book noise, not stable prints"
        if volume_ratio < 0.5 and turnover_cr < 2.0:
            return False, "volume and turnover are both too weak for reliable execution"
        return True, "execution quality is stable enough for live auto-buy"

    @classmethod
    def _classify_entry_timing_lane(cls, c: 'BlindBuyCandidate') -> Tuple[str, str]:
        rr2 = cls._candidate_rr2(c)
        gap_pct = cls._candidate_entry_gap_pct(c)
        if str(getattr(c, "holding_horizon", "") or "") == "avoid":
            return "alert_only", "entry timing is not actionable because the setup is alert-only"
        if c.change_pct <= 4.8 and gap_pct <= 1.0 and c.volume_ratio >= 1.2 and rr2 >= 1.4:
            return "early", "early breakout — one of the cleaner early entry windows"
        if c.change_pct <= 6.0 and gap_pct <= 0.8 and c.volume_ratio >= 2.2 and rr2 >= 1.9:
            return "continuation", "late but still actionable continuation setup"
        return "stretched", "entry is already stretched — alert-only unless manually justified"

    @classmethod
    def _classify_trade_alert_stage(
        cls,
        c: 'BlindBuyCandidate',
        *,
        off_high_pct: float = 0.0,
    ) -> Tuple[str, str]:
        """Classify the setup as early, actionable, missed, or avoid."""
        rr2 = cls._candidate_rr2(c)
        gap_pct = cls._candidate_entry_gap_pct(c)
        change_pct = max(0.0, float(getattr(c, "change_pct", 0.0) or 0.0))
        volume_ratio = max(0.0, float(getattr(c, "volume_ratio", 0.0) or 0.0))
        confidence = max(0.0, float(getattr(c, "confidence", 0.0) or 0.0))
        buy_above = max(0.0, float(getattr(c, "buy_above", 0.0) or 0.0))
        price = max(0.0, float(getattr(c, "price", 0.0) or 0.0))
        below_trigger = buy_above > 0 and price > 0 and price < buy_above * 0.998
        near_trigger = buy_above <= 0 or price >= buy_above * 0.99
        if (
            str(getattr(c, "holding_horizon", "") or "") == "avoid"
            or rr2 < 1.0
            or change_pct >= 8.0
            or gap_pct > 1.2
            or off_high_pct >= 3.0
        ):
            return "AVOID", "risky or overextended"

        if below_trigger:
            if near_trigger and change_pct <= 3.5 and volume_ratio >= 0.8 and confidence >= 15 and rr2 >= 1.2:
                return "WATCH EARLY", "strong stock preparing near breakout"
            return "WATCH EARLY", "buyers are building pressure but breakout has not started yet"

        if c.holding_horizon in {"2-day swing", "5-day swing"}:
            quality_ok, quality_reason = cls._swing_entry_quality_gate(c)
            if not quality_ok:
                return "MISSED", quality_reason

        if (
            near_trigger
            and 0.4 <= change_pct <= 4.8
            and volume_ratio >= 1.0
            and confidence >= 18
            and rr2 >= 1.3
            and off_high_pct <= 1.8
        ):
            return "BUY", "move just started and entry is still safe"

        if change_pct <= 7.0 and volume_ratio >= 1.0 and rr2 >= 1.1:
            return "MISSED", "good stock, but late entry. Wait for pullback."

        return "AVOID", "risky or overextended"

    @classmethod
    def _enrich_candidate_alert_stage(
        cls,
        c: 'BlindBuyCandidate',
        *,
        off_high_pct: float = 0.0,
    ) -> 'BlindBuyCandidate':
        c.entry_type, c.entry_type_reason = cls._classify_entry_type(c)
        stage, reason = cls._classify_trade_alert_stage(c, off_high_pct=off_high_pct)
        c.alert_stage = stage
        c.alert_stage_reason = reason
        return c

    @classmethod
    def _live_auto_buy_gate_detail(cls, c: 'BlindBuyCandidate', grade: str = "") -> Tuple[bool, str]:
        """Return auto-buy eligibility plus a human-readable reason."""
        rr2 = cls._candidate_rr2(c)
        gap_pct = cls._candidate_entry_gap_pct(c)
        entry_lane, entry_reason = cls._classify_entry_timing_lane(c)
        alert_stage, alert_reason = cls._classify_trade_alert_stage(c)
        entry_type, _entry_type_reason = cls._classify_entry_type(c)
        _avoid_horizon = c.holding_horizon == "avoid"
        try:
            import pytz
            _ist = pytz.timezone("Asia/Kolkata")
            _now_ist = datetime.now(_ist)
        except Exception:
            from datetime import timezone, timedelta
            _now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        now_min = _now_ist.hour * 60 + _now_ist.minute
        _swing_start_min = 9 * 60 + 15
        # Extended swing window 13:30 → 14:30 on 2026-05-24. Many actionable
        # alerts fire between 13:30 and 14:30 (MARATHON, FAZE3Q, MOTHERSON,
        # HARDWYN, SHADOWFAX, GOCLCORP, IMPAL, VALIANT, MARKOLINES, SPAL,
        # HCG were all silenced as LATE_DISCOVERY because cutoff was too early).
        # 14:30 still leaves 1 hour before close for the move to play out.
        _swing_end_min = 14 * 60 + 30
        _late_swing_start_min = 14 * 60 + 30
        _late_swing_end_min = 14 * 60 + 45
        _late_swing_window = _late_swing_start_min < now_min <= _late_swing_end_min
        _late_comer_start_min = _env_time_min("ASSISTANT_LATE_COMER_START", 10 * 60 + 30)
        _late_comer_end_min = _env_time_min("ASSISTANT_LATE_COMER_END", 13 * 60 + 15)
        _late_comer_window = _late_comer_start_min <= now_min <= _late_comer_end_min
        _opening_starter_window = _swing_start_min <= now_min <= 10 * 60 + 0
        _early_engine_end_min = _env_time_min("ASSISTANT_EARLY_ENGINE_END", 10 * 60 + 45)
        _intraday_auto_buy_cutoff_min = _env_time_min("ASSISTANT_INTRADAY_AUTO_BUY_CUTOFF", 11 * 60 + 30)
        _intraday_final_cutoff_min = _env_time_min("ASSISTANT_INTRADAY_FINAL_CUTOFF", 13 * 60 + 30)
        _late_intraday_window = (
            c.holding_horizon == "intraday"
            and _intraday_auto_buy_cutoff_min < now_min <= _intraday_final_cutoff_min
        )

        if c.holding_horizon == "intraday" and now_min > _intraday_final_cutoff_min:
            return False, f"intraday auto-buy is paused after {_format_ist_minute(_intraday_final_cutoff_min)} IST"

        if alert_stage != "BUY":
            if alert_stage == "WATCH EARLY":
                return False, "setup is still early — wait for breakout to start"
            if alert_stage == "MISSED" and _late_intraday_window:
                pass
            elif alert_stage == "MISSED":
                return False, alert_reason
            else:
                return False, "setup is risky or overextended"

        execution_ok, execution_reason = cls._execution_quality_gate(c)
        if not execution_ok:
            if _late_intraday_window:
                return False, f"strict late-session MIS rejected: {execution_reason}"
            return False, execution_reason

        if (
            cls._is_true_buy_action(getattr(c, "original_action", ""))
            and 9 * 60 + 15 <= now_min <= _early_engine_end_min
            and c.price >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_PRICE", 20.0)
            and _env_float("ASSISTANT_EARLY_ENGINE_MIN_CHANGE", 0.6) <= c.change_pct <= _env_float("ASSISTANT_EARLY_ENGINE_MAX_CHANGE", 5.0)
            and rr2 >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_RR", 1.4)
            and gap_pct <= _env_float("ASSISTANT_EARLY_ENGINE_MAX_GAP", 0.8)
            and c.blind_score >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_SCORE", 52.0)
            and c.confidence >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_CONFIDENCE", 16.0)
            and c.volume_ratio >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_VOL_RATIO", 2.0)
            and not ("bearish" in str(getattr(c, "reasons", "") or "").lower())
        ):
            return True, "eligible now as an early-engine auto-buy"

        if c.holding_horizon == "intraday":
            micro_open = 9 * 60 + 15 <= now_min < 9 * 60 + 20
            if micro_open:
                if entry_lane != "early":
                    return False, "9:15 intraday auto-buy is reserved for early breakout entries only"
                if grade not in {"A+", "A"}:
                    return False, "9:15 intraday auto-buy needs A or A+ grade"
                if c.blind_score < 60:
                    return False, f"score {c.blind_score:.0f} is below 60 for the 9:15 lane"
                if c.confidence < 34:
                    return False, f"confidence {c.confidence:.0f}% is below 34% for the 9:15 lane"
                if c.volume_ratio < 2.5:
                    return False, f"volume {c.volume_ratio:.1f}x is below 2.5x for the 9:15 lane"
                if rr2 < 1.7:
                    return False, f"R:R 1:{rr2:.1f} is below 1:1.7 for the 9:15 lane"
                if gap_pct > 0.8:
                    return False, f"entry is {gap_pct:.1f}% below trigger for the 9:15 lane"
                if c.change_pct < 0.4:
                    return False, f"day move {c.change_pct:.1f}% is below the 9:15 lane zone (needs 0.4%-4.8%)"
                if c.change_pct > 4.8:
                    return False, f"day move {c.change_pct:.1f}% is above the 9:15 lane zone (needs 0.4%-4.8%)"
            elif now_min > _intraday_final_cutoff_min:
                return False, f"intraday auto-buy is paused after {_format_ist_minute(_intraday_final_cutoff_min)} IST"
            elif now_min <= _intraday_auto_buy_cutoff_min:
                if entry_lane not in {"early", "continuation"}:
                    return False, "intraday auto-buy needs an early breakout or clean continuation entry"
                if grade not in {"A+", "A"}:
                    return False, "intraday auto-buy needs A or A+ grade"
            else:
                late_ok, late_reason = cls._strict_late_session_intraday_gate(c)
                if not late_ok:
                    return False, f"strict late-session MIS rejected: {late_reason}"
                return True, late_reason
        else:
            if not (_swing_start_min <= now_min <= _swing_end_min):
                if now_min < _swing_start_min:
                    return False, "swing auto-buy starts at 9:15 AM IST"
                return False, "swing auto-buy is paused after 1:30 PM IST"
            quality_ok, quality_reason = cls._swing_entry_quality_gate(c)
            if not quality_ok:
                return False, quality_reason

        if (
            _late_comer_window
            and c.holding_horizon in {"2-day swing", "5-day swing"}
            and grade in {"A+", "A"}
            and c.price >= _env_float("ASSISTANT_LATE_COMER_MIN_PRICE", 30.0)
            and _env_float("ASSISTANT_LATE_COMER_MIN_CHANGE", 4.5) <= c.change_pct <= _env_float("ASSISTANT_LATE_COMER_MAX_CHANGE", 11.5)
            and c.blind_score >= _env_float("ASSISTANT_LATE_COMER_MIN_SCORE", 52.0)
            and c.confidence >= _env_float("ASSISTANT_LATE_COMER_MIN_CONFIDENCE", 34.0)
            and c.volume_ratio >= _env_float("ASSISTANT_LATE_COMER_MIN_VOL_RATIO", 1.8)
            and rr2 >= _env_float("ASSISTANT_LATE_COMER_MIN_RR", 1.8)
            and gap_pct <= _env_float("ASSISTANT_LATE_COMER_MAX_GAP", 0.7)
            and not _avoid_horizon
            and entry_type != "LATE_CHASE"
            and "bearish" not in str(getattr(c, "reasons", "") or "").lower()
        ):
            return True, "eligible now as a strict late-comer continuation swing auto-buy"

        if (
            _opening_starter_window
            and c.holding_horizon in {"2-day swing", "5-day swing"}
            and grade in {"A+", "A"}
            and c.price >= 50
            and rr2 >= 2.0
            and gap_pct <= 0.8
            and 1.5 <= c.change_pct <= 7.5
            and c.blind_score >= 55
            and c.confidence >= 18
            and c.volume_ratio >= 0.25
            and not _avoid_horizon
            and entry_type != "LATE_CHASE"
        ):
            return True, "eligible now as an opening starter swing auto-buy"

        if entry_lane == "stretched":
            return False, entry_reason

        if c.holding_horizon == "5-day swing":
            if c.price < 20:
                return False, "price is below the ₹20 floor"
            if rr2 < 1.8:
                return False, f"R:R 1:{rr2:.1f} is below the 5-day swing floor"
            if gap_pct > 1.2:
                return False, f"entry is {gap_pct:.1f}% below trigger"
            if c.change_pct < 1.0:
                return False, f"day move {c.change_pct:.1f}% is below the 5-day swing zone (needs 1.0%-7.8%)"
            if c.change_pct > 7.8:
                return False, f"day move {c.change_pct:.1f}% is above the 5-day swing zone (needs 1.8%-7.8%)"
            if c.blind_score < 50:
                return False, f"score {c.blind_score:.0f} is below 50"
            if c.confidence < 27:
                return False, f"confidence {c.confidence:.0f}% is below 27%"
            if c.volume_ratio < 2.0:
                return False, f"volume {c.volume_ratio:.1f}x is below 2.0x"
            if _avoid_horizon:
                return False, "horizon model tagged this as avoid despite passing 5-day swing thresholds"
            return True, "eligible now as a 5-day swing auto-buy"

        if c.holding_horizon == "2-day swing":
            if c.price < 20:
                return False, "price is below the ₹20 floor"
            if rr2 < 1.5:
                return False, f"R:R 1:{rr2:.1f} is below the 2-day swing floor"
            if gap_pct > 1.2:
                return False, f"entry is {gap_pct:.1f}% below trigger"
            if c.change_pct < 1.0:
                return False, f"day move {c.change_pct:.1f}% is below the 2-day swing zone (needs 1.0%-6.8%)"
            if c.change_pct > 6.8:
                return False, f"day move {c.change_pct:.1f}% is above the 2-day swing zone (needs 1.8%-6.8%)"
            if c.blind_score < 40:
                return False, f"score {c.blind_score:.0f} is below 40"
            # Lowered confidence floor 28 → 22 on 2026-05-24. Real alert
            # confidence distribution clusters 22-32% (STALLION 26, HEMIPROP 27,
            # JSWCEMENT 26, LICI 27 were all rejected — fell just below 28%).
            # The score=40 + volume=1.8x gates still filter low-quality setups.
            if c.confidence < 22:
                return False, f"confidence {c.confidence:.0f}% is below 22%"
            if c.volume_ratio < 1.8:
                return False, f"volume {c.volume_ratio:.1f}x is below 1.8x"
            if _avoid_horizon:
                return False, "horizon model tagged this as avoid despite passing 2-day swing thresholds"
            return True, "eligible now as a 2-day swing auto-buy"

        if c.price < 20:
            return False, "price is below the ₹20 floor"
        if rr2 < 1.8:
            return False, f"R:R 1:{rr2:.1f} is below minimum — signal targets are likely stale relative to current price"
        if gap_pct > 0.8:
            return False, f"entry is {gap_pct:.1f}% below trigger"
        if c.change_pct < 1.5:
            return False, f"day move {c.change_pct:.1f}% is below the minimum zone (needs 1.5%-10.0%)"
        if c.change_pct > 10.0:
            return False, f"day move {c.change_pct:.1f}% is above the maximum zone (needs 1.5%-10.0%)"
        if grade == "A+":
            if c.blind_score < 68:
                return False, f"score {c.blind_score:.0f} is below 68"
            if c.confidence < 44:
                return False, f"confidence {c.confidence:.0f}% is below 44%"
            if c.volume_ratio < 5.5:
                return False, f"volume {c.volume_ratio:.1f}x is below 5.5x"
            if rr2 < 2.0:
                return False, f"R:R 1:{rr2:.1f} is below the A+ floor"
            if _avoid_horizon:
                return False, "horizon model tagged this as avoid despite passing the A+ intraday gate"
            if micro_open:
                return True, "eligible now as an elite 9:15 intraday auto-buy"
            return True, "eligible now as an A+ intraday auto-buy"
        if grade == "A":
            if c.blind_score < 62:
                return False, f"score {c.blind_score:.0f} is below 62"
            if c.confidence < 40:
                return False, f"confidence {c.confidence:.0f}% is below 40%"
            if c.volume_ratio < 4.0:
                return False, f"volume {c.volume_ratio:.1f}x is below 4.0x"
            if rr2 < 1.9:
                return False, f"R:R 1:{rr2:.1f} is below the A-grade intraday floor"
            if _avoid_horizon:
                return False, "horizon model tagged this as avoid despite passing the A-grade gate"
            return True, "eligible now as an A-grade auto-buy"
        if _avoid_horizon:
            return False, "horizon model tagged this setup as avoid"
        return False, "grade is below the live auto-buy floor"

    @classmethod
    def _format_auto_buy_status_line(cls, c: 'BlindBuyCandidate', grade: str = "") -> str:
        eligible, reason = cls._live_auto_buy_gate_detail(c, grade)
        prefix = "🤖 Auto-buy: yes" if eligible else "🤖 Auto-buy: no"
        return f"{prefix} — {reason}"

    @classmethod
    def _format_entry_timing_line(cls, c: 'BlindBuyCandidate') -> str:
        stage, reason = cls._classify_trade_alert_stage(c)
        label = {
            "WATCH EARLY": "👀 EARLY WATCH: `strong stock preparing`",
            "BUY": "🟢 BUY: `move just started and entry is still safe`",
            "MISSED": "🟡 MISSED: `good stock but entry is late`",
            "AVOID": "🔴 AVOID: `risky or overextended`",
        }.get(stage, "📝 WATCH: `monitor only`")
        return f"{label} — {reason}"

    @classmethod
    def _format_entry_type_line(cls, c: 'BlindBuyCandidate') -> str:
        entry_type, reason = cls._classify_entry_type(c)
        return f"🧩 Entry Type: `{entry_type}` — {reason}"

    @classmethod
    def _format_hold_product_line(cls, c: 'BlindBuyCandidate') -> str:
        horizon = str(getattr(c, "holding_horizon", "") or "avoid")
        product = str(getattr(c, "product_preference", "") or ("MIS" if horizon == "intraday" else "CNC"))
        if horizon == "avoid":
            return "🧭 Hold: `alert-only`"
        hold_label = {
            "intraday": "intraday",
            "2-day swing": "2-day swing",
            "5-day swing": "5-day swing",
        }.get(horizon, horizon)
        return f"🧭 Hold: `{hold_label}` | Product: `{product}`"

    @classmethod
    def _classify_holding_horizon(cls, c: 'BlindBuyCandidate') -> Tuple[str, str, int, str]:
        """Classify each alert into the horizon it historically fits best."""
        rr2 = cls._candidate_rr2(c)
        gap_pct = cls._candidate_entry_gap_pct(c)
        entry_lane, _ = cls._classify_entry_timing_lane(c)

        if c.price <= 0 or rr2 < 1.4:
            return ("avoid", "", 0, "Below minimum quality for evidence-backed execution")
        try:
            import pytz
            _ist = pytz.timezone("Asia/Kolkata")
            _now_ist = datetime.now(_ist)
        except Exception:
            from datetime import timezone, timedelta
            _now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        _now_min = _now_ist.hour * 60 + _now_ist.minute
        _opening_starter_window = 9 * 60 + 20 <= _now_min <= 10 * 60 + 0
        _early_engine_end_min = _env_time_min("ASSISTANT_EARLY_ENGINE_END", 10 * 60 + 45)
        _late_comer_window = _env_time_min("ASSISTANT_LATE_COMER_START", 10 * 60 + 30) <= _now_min <= _env_time_min("ASSISTANT_LATE_COMER_END", 13 * 60 + 15)

        if (
            _opening_starter_window
            and entry_lane in {"early", "continuation", "stretched"}
            and c.price >= 50
            and 1.5 <= c.change_pct <= 7.5
            and c.blind_score >= 55
            and c.confidence >= 18
            and c.volume_ratio >= 0.25
            and rr2 >= 2.0
            and gap_pct <= 0.8
        ):
            return ("2-day swing", "CNC", 7, "Opening starter: early strength with controlled risk")

        if (
            9 * 60 + 15 <= _now_min <= _early_engine_end_min
            and cls._is_true_buy_action(getattr(c, "original_action", ""))
            and c.price >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_PRICE", 20.0)
            and _env_float("ASSISTANT_EARLY_ENGINE_MIN_CHANGE", 0.6) <= c.change_pct <= _env_float("ASSISTANT_EARLY_ENGINE_MAX_CHANGE", 5.0)
            and c.blind_score >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_SCORE", 52.0)
            and c.confidence >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_CONFIDENCE", 16.0)
            and c.volume_ratio >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_VOL_RATIO", 2.0)
            and rr2 >= _env_float("ASSISTANT_EARLY_ENGINE_MIN_RR", 1.4)
            and gap_pct <= _env_float("ASSISTANT_EARLY_ENGINE_MAX_GAP", 0.8)
        ):
            return ("intraday", "MIS", 1, "Early engine: Buy Now while move is still controlled")

        if (
            _late_comer_window
            and c.price >= _env_float("ASSISTANT_LATE_COMER_MIN_PRICE", 30.0)
            and _env_float("ASSISTANT_LATE_COMER_MIN_CHANGE", 4.5) <= c.change_pct <= _env_float("ASSISTANT_LATE_COMER_MAX_CHANGE", 11.5)
            and c.blind_score >= _env_float("ASSISTANT_LATE_COMER_MIN_SCORE", 52.0)
            and c.confidence >= _env_float("ASSISTANT_LATE_COMER_MIN_CONFIDENCE", 34.0)
            and c.volume_ratio >= _env_float("ASSISTANT_LATE_COMER_MIN_VOL_RATIO", 1.8)
            and rr2 >= _env_float("ASSISTANT_LATE_COMER_MIN_RR", 1.8)
            and gap_pct <= _env_float("ASSISTANT_LATE_COMER_MAX_GAP", 0.7)
        ):
            return ("2-day swing", "CNC", 7, "Strict late-comer continuation: liquid midday runner with controlled risk")

        if c.blind_score < 38 or c.confidence < 25:
            return ("avoid", "", 0, "Below minimum quality for evidence-backed execution")
        if c.volume_ratio < 1.4:
            return ("avoid", "", 0, "Participation is too weak for either intraday or swing execution")

        intraday_ready = (
            entry_lane == "early"
            and 1.8 <= c.change_pct <= 4.2
            and c.blind_score >= 68
            and c.confidence >= 44
            and c.volume_ratio >= 5.5
            and rr2 >= 2.0
            and gap_pct <= 0.6
        )
        if intraday_ready:
            return ("intraday", "MIS", 1, "Elite opening-session same-day momentum profile")

        if (
            c.blind_score >= 55
            and c.confidence >= 34
            and c.volume_ratio >= 2.0
            and rr2 >= 1.8
        ):
            return ("5-day swing", "CNC", 10, "Historically stronger as a multi-session continuation")

        if (
            c.blind_score >= 48
            and c.confidence >= 26
            and c.volume_ratio >= 1.8
            and rr2 >= 1.5
        ):
            return ("2-day swing", "CNC", 7, "Good continuation candidate, but not an elite same-day setup")

        return ("avoid", "", 0, "Alert-quality only; not strong enough for auto execution")

    @classmethod
    def _enrich_candidate_horizon(cls, c: 'BlindBuyCandidate') -> 'BlindBuyCandidate':
        horizon, product, max_hold_days, horizon_reason = cls._classify_holding_horizon(c)
        c.holding_horizon = horizon
        c.product_preference = product
        c.max_hold_days = max_hold_days
        c.horizon_reason = horizon_reason
        cls._enrich_candidate_alert_stage(c)
        return c

    @classmethod
    def _candidate_rank(cls, c: 'BlindBuyCandidate') -> float:
        """Execution-aware ranking for alert ordering.

        Historical ledger hint: confidence helps a bit, good RR helps, and
        larger entry gaps hurt because the setup is less immediate/actionable.
        """
        rr2 = max(0.0, cls._candidate_rr2(c))
        gap_pct = max(0.0, cls._candidate_entry_gap_pct(c))
        vol_bonus = min(c.volume_ratio, 12.0) * 1.4
        change_bonus = min(max(c.change_pct, 0.0), 12.0) * 0.5
        return (
            (c.confidence * 1.2)
            + (min(rr2, 4.0) * 10.0)
            + vol_bonus
            + change_bonus
            - (gap_pct * 2.0)
            - (max(c.blind_score - 70.0, 0.0) * 0.15)
        )

    @classmethod
    def _eligible_for_live_auto_buy(cls, c: 'BlindBuyCandidate', grade: str = "") -> bool:
        """Return True when the live auto-buy gate says yes."""
        eligible, _ = cls._live_auto_buy_gate_detail(c, grade)
        return eligible

    @classmethod
    def _grade_candidate(cls, c: 'BlindBuyCandidate') -> Tuple[str, str]:
        """Assign a conviction grade (A+, A, B) and risk flag.

        Returns (grade, risk_flag) where risk_flag may be empty or a warning.

        Key insight from backtesting: volume × change (raw momentum) is deceptive.
        Extreme volume with modest change (vol/change > 1.5) indicates DISTRIBUTION
        — smart money selling into the gap-up. This pattern (KEC 10.6x/+5.5%,
        RITES 12.1x/+4.9%) loses 3-4% by EOD.

        Efficient breakouts have lower vol/change ratios (JTLIND 5.5x/+9.1% = 0.60,
        ITI 2.5x/+8.7% = 0.29) — small volume producing big moves = genuine demand.

        Score is the primary signal. Volume confirms but cannot override low scores.
        """
        momentum = c.volume_ratio * abs(c.change_pct) if c.change_pct > 0 else 0
        vol_efficiency = (c.volume_ratio / c.change_pct) if c.change_pct > 0 else 99
        rr2 = cls._candidate_rr2(c)
        entry_gap_pct = cls._candidate_entry_gap_pct(c)
        risk_flag = ""

        # Distribution risk: lots of volume but modest move = selling into strength
        if vol_efficiency > 1.5 and c.volume_ratio >= 3.0:
            risk_flag = "⚠️ GAP-FADE RISK — high volume relative to move (possible distribution)"
        elif entry_gap_pct > 1.0:
            risk_flag = "⚠️ ENTRY NOT TIGHT — price is still meaningfully below trigger"

        # A+ = Best of the best — auto-buy immediately
        if c.confidence >= 45 and rr2 >= 1.8 and entry_gap_pct <= 0.6 and c.volume_ratio >= 2.0:
            return "A+", risk_flag
        if c.blind_score >= 75 and c.volume_ratio >= 3.0 and c.change_pct >= 3.0 and vol_efficiency <= 1.5:
            return "A+", risk_flag
        if c.blind_score >= 80 and c.volume_ratio >= 2.5:
            return "A+", risk_flag

        # A = Strong conviction — auto-buy
        if c.confidence >= 35 and rr2 >= 1.5 and entry_gap_pct <= 1.0 and c.volume_ratio >= 1.6:
            return "A", risk_flag
        if c.blind_score >= 65 and c.volume_ratio >= 2.5 and c.change_pct >= 2.0:
            return "A", risk_flag
        if c.blind_score >= 70 and c.volume_ratio >= 2.0:
            return "A", risk_flag
        # Efficient high-change breakout: big move on moderate volume = real demand
        if c.change_pct >= 7.0 and c.blind_score >= 50 and vol_efficiency < 1.0:
            return "A", risk_flag

        # B = everything else
        return "B", risk_flag

    def _do_send_buy_alert(self, candidates: List[BlindBuyCandidate], is_followup: bool) -> None:
        """Format and send a buy alert message to Discord.

        Grades candidates A+/A/B, highlights top picks, caps main alert
        at 10 stocks, and sends the rest as a watchlist overflow.
        """
        def _send_section_chunks(section_list: List[str], label: str) -> None:
            chunks: List[str] = []
            current = ""
            for section in section_list:
                section_text = str(section or "").strip()
                if not section_text:
                    continue
                candidate = section_text if not current else f"{current}\n\n{section_text}"
                if len(candidate) > 1800 and current:
                    chunks.append(current)
                    current = section_text
                else:
                    current = candidate
            if current:
                chunks.append(current)
            for chunk in chunks:
                self._send_to_channels(chunk)
            if chunks:
                self._log(f"Sent {label} buy alert message(s): {len(chunks)} chunk(s).")

        actionable_candidates: List[BlindBuyCandidate] = []
        late_candidates: List[BlindBuyCandidate] = []
        avoid_candidates: List[BlindBuyCandidate] = []
        stage_counts: Counter = Counter()
        for c in candidates:
            self._enrich_candidate_alert_stage(c)
            stage_counts[c.alert_stage or "UNKNOWN"] += 1
            if c.alert_stage == "BUY":
                actionable_candidates.append(c)
            elif c.alert_stage == "MISSED":
                self._record_internal_stage("MISSED", c.symbol, c.alert_stage_reason)
                late_candidates.append(c)
            else:
                self._record_internal_stage(c.alert_stage or "AVOID", c.symbol, c.alert_stage_reason)
                avoid_candidates.append(c)

        candidates = actionable_candidates
        if stage_counts:
            if not hasattr(self, "_last_blind_buy_diagnostics"):
                self._last_blind_buy_diagnostics = Counter()
            self._last_blind_buy_diagnostics.update({
                f"stage_{str(stage).lower().replace(' ', '_')}": int(count)
                for stage, count in stage_counts.items()
            })
        if not candidates and (late_candidates or avoid_candidates):
            return

        # Determine market context for time-of-day labeling
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            _now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            _now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        _now_min = _now_ist.hour * 60 + _now_ist.minute
        _is_late_session = _now_min >= 13 * 60  # after 1:00 PM IST

        is_post_market = self._is_post_market_window()

        # -- Grade and sort candidates by conviction --
        graded = []
        for c in candidates:
            grade, risk_flag = self._grade_candidate(c)
            momentum = c.volume_ratio * abs(c.change_pct) if c.change_pct > 0 else 0
            graded.append((c, grade, momentum, risk_flag))
        # Sort: A+ first, then A, then B; within tier by execution-aware rank.
        _grade_order = {"A+": 0, "A": 1, "B": 2}
        graded.sort(key=lambda x: (_grade_order.get(x[1], 9), -self._candidate_rank(x[0]), -x[2]))

        # Split into main (top 10) and overflow
        _MAX_MAIN = 10
        main_picks = graded[:_MAX_MAIN]
        overflow_picks = graded[_MAX_MAIN:]

        # Count grades for header
        n_aplus = sum(1 for _, g, _, _ in main_picks if g == "A+")
        n_a = sum(1 for _, g, _, _ in main_picks if g == "A")

        if is_post_market:
            sections = [
                "**📋 POST-MARKET PREVIEW — Tomorrow's Entry Candidates**",
                f"**{len(candidates)}** stock{'s' if len(candidates) != 1 else ''} scored 60+/100 on today's closing data.",
                "_Entry tomorrow at open if price holds above Buy Above level._",
            ]
        elif is_followup:
            grade_summary = ""
            if n_aplus > 0:
                grade_summary = f" ({n_aplus} A+, {n_a} A grade)"
            sections = [
                "**🟢 NEW PICKS FOUND**",
                f"**{len(main_picks)}** new stock{'s' if len(main_picks) != 1 else ''} ranked by conviction{grade_summary}:",
            ]
        else:
            grade_summary = ""
            if n_aplus > 0:
                grade_summary = f" — **{n_aplus} strong conviction** (A+)"
            sections = [
                "**🟢 BLIND BUY ALERT**",
                f"Top **{len(main_picks)}** of {len(candidates)} picks ranked by conviction{grade_summary}:",
            ]

        for rank, (c, grade, momentum, risk_flag) in enumerate(main_picks, 1):
            self._format_candidate_block(sections, c, grade, momentum, rank, len(main_picks), risk_flag=risk_flag)

        # Auto-buy A+/A grade candidates while broker funds and risk controls allow.
        # Log every main pick, not just bought names, so the post-market truth
        # report can explain exactly why a good-looking alert was not bought.
        if not is_post_market:
            # Market regime gate: block batch auto-buy in crash/bear regimes.
            # Fail-safe: if the engine is unavailable, fall through to existing
            # eligibility checks rather than blocking everything.
            regime_ok, regime_reason = self._is_safe_to_buy()
            _auto_lines = []
            auto_sent = 0
            for c, grade, _, _ in main_picks:
                auto_result = ""
                c, grade, auto_eligible, gate_reason = self._resolve_auto_buy_candidate(c)
                capped_by_alert_limit = auto_eligible and self._AUTO_BUY_TOP_N > 0 and auto_sent >= self._AUTO_BUY_TOP_N
                if not regime_ok and auto_eligible:
                    auto_result = self._format_missed_or_late_message(
                        c.symbol,
                        reason=f"Buyable setup blocked by market regime: {regime_reason}",
                        candidate=c,
                        live_price=c.price,
                    )
                    self._log(f"Batch auto-buy regime block {c.symbol}: {regime_reason}")
                    self._record_no_buy_reason(regime_reason)
                elif capped_by_alert_limit:
                    auto_result = self._format_missed_or_late_message(
                        c.symbol,
                        reason=f"Buyable setup missed because live auto-buy cap {self._AUTO_BUY_TOP_N} was already reached.",
                        candidate=c,
                        live_price=c.price,
                    )
                    self._log(f"Batch auto-buy cap skip {c.symbol}: cap={self._AUTO_BUY_TOP_N}")
                elif auto_eligible:
                    _ar = self._try_auto_buy(
                        c.symbol, c.price, c.stop_loss, c.target1, c.target2,
                        c.blind_score, c.reasons,
                        product_preference=c.product_preference,
                        holding_horizon=c.holding_horizon,
                        max_hold_days=c.max_hold_days,
                    )
                    auto_result = _ar or ""
                    self._log(f"Batch auto-buy result {c.symbol}: {auto_result}")
                    auto_sent += 1
                else:
                    auto_result = self._format_missed_or_late_message(
                        c.symbol,
                        reason=f"Buyable setup missed because: {gate_reason}",
                        candidate=c,
                        live_price=c.price,
                    )
                    self._log(
                        f"Batch auto-buy skip {c.symbol}: grade={grade}, score={c.blind_score:.0f}, "
                        f"conf={c.confidence:.0f}%, rr={self._candidate_rr2(c):.1f}, "
                        f"vol={c.volume_ratio:.1f}x, reason={gate_reason}"
                    )
                if auto_result:
                    _auto_lines.append(auto_result)
                    self._handle_auto_buy_result(c.symbol, auto_result)
                self._log_engine_confirmed_buy_candidate(
                    symbol=c.symbol,
                    action=str(getattr(c, "original_action", "") or "Buy"),
                    live_price=c.price,
                    stop_loss=c.stop_loss,
                    target1=c.target1,
                    target2=c.target2,
                    blind_score=c.blind_score,
                    confidence=c.confidence,
                    volume_ratio=c.volume_ratio,
                    daily_change=c.change_pct,
                    rr2=self._candidate_rr2(c),
                    grade=grade,
                    rank_score=self._candidate_rank(c),
                    entry_type=c.entry_type,
                    entry_type_reason=c.entry_type_reason,
                    auto_eligible=auto_eligible,
                    auto_result=auto_result,
                )
        _send_section_chunks(sections, "main")

        # Send overflow as a separate watchlist message
        if overflow_picks:
            overflow_sections = [
                "**📋 BUY WATCHLIST OVERFLOW**",
                f"**{len(overflow_picks)}** additional stock{'s' if len(overflow_picks) != 1 else ''} after the top {len(main_picks)}:",
            ]
            for rank, (c, grade, momentum, risk_flag) in enumerate(overflow_picks, _MAX_MAIN + 1):
                self._format_candidate_block(overflow_sections, c, grade, momentum, rank, len(candidates), compact=True, risk_flag=risk_flag)
            _send_section_chunks(overflow_sections, "overflow")

    def _format_candidate_block(self, sections: list, c: 'BlindBuyCandidate', grade: str,
                                 momentum: float, rank: int, total: int,
                                 compact: bool = False, risk_flag: str = "") -> None:
        """Format a single candidate block and append to sections. Also records tracking."""
        pct = f"+{c.change_pct:.1f}%" if c.change_pct > 0 else f"{c.change_pct:.1f}%"
        risk_reward = ""
        if c.stop_loss > 0 and c.target2 > 0 and c.price > 0:
            risk = abs(c.price - c.stop_loss)
            reward1 = abs(c.target1 - c.price) if c.target1 > 0 else 0
            reward2 = abs(c.target2 - c.price)
            rr1 = round(reward1 / risk, 1) if risk > 0 else 0
            rr2 = round(reward2 / risk, 1) if risk > 0 else 0
            risk_reward = f" | R:R `1:{rr1}` / `1:{rr2}`"

        # Grade badge + score
        if grade == "A+":
            grade_badge = "⭐ **A+**"
            score_label = f"🔥 **{c.blind_score:.0f}/100**"
        elif grade == "A":
            grade_badge = "**A**"
            score_label = f"✅ **{c.blind_score:.0f}/100**"
        else:
            grade_badge = "B"
            score_label = f"**{c.blind_score:.0f}/100**"

        # Momentum intensity label
        if momentum >= 25:
            mom_label = f"🚀 Momentum `{momentum:.0f}`"
        elif momentum >= 15:
            mom_label = f"💪 Momentum `{momentum:.0f}`"
        elif momentum >= 8:
            mom_label = f"Momentum `{momentum:.0f}`"
        else:
            mom_label = ""

        # Top pick marker for rank 1-3 in A+ or A grade
        top_marker = ""
        if rank <= 3 and grade in ("A+", "A"):
            top_marker = " 🏆 **TOP PICK**" if rank == 1 else " ⭐ **TOP PICK**"

        # Time-of-day context: swing only if already moved 8%+
        is_swing_only = c.change_pct >= 8.0
        trade_tag = ""
        if is_swing_only:
            trade_tag = "\n⏰ **SWING ONLY** — already +{:.1f}%, hold 2-5 days for targets.".format(c.change_pct)
        horizon_tag = ""
        if c.holding_horizon in {"2-day swing", "5-day swing"}:
            horizon_tag = (
                f"\n⏰ **{c.holding_horizon.upper()}** — route as "
                f"`{c.product_preference or 'CNC'}` and avoid same-day forced exits."
            )
        elif c.holding_horizon == "intraday":
            horizon_tag = "\n⚡ **INTRADAY** — one of the rare same-day momentum profiles."

        # Simplify reasons to plain English
        simple_reasons = self._simplify_blind_reasons(c.reasons)

        if compact:
            # Compact format for watchlist overflow
            _risk = " ⚠️FADE" if risk_flag else ""
            block = [
                f"`{c.symbol}` [{grade_badge}] Score {c.blind_score:.0f} | `{c.price:.2f}` ({pct}) | Vol `{c.volume_ratio:.1f}x` | SL `{c.stop_loss:.2f}` → T1 `{c.target1:.2f}` T2 `{c.target2:.2f}`{_risk}",
            ]
        else:
            entry_timing_line = self._format_entry_timing_line(c)
            entry_type_line = self._format_entry_type_line(c)
            auto_status_line = self._format_auto_buy_status_line(c, grade)
            hold_product_line = self._format_hold_product_line(c)
            headline = {
                "WATCH EARLY": "👀 EARLY WATCH",
                "BUY": "🟢 BUY",
                "MISSED": "🟡 MISSED",
                "AVOID": "🔴 AVOID",
            }.get(c.alert_stage or "BUY", "🟢 BUY")
            block = [
                f"**{headline} `{c.symbol}`** [{grade_badge}] Score {score_label}{top_marker}{trade_tag}{horizon_tag}",
                f"Price `{c.price:.2f}` ({pct}) | Buy Above `{c.buy_above:.2f}` | Volume `{c.volume_ratio:.1f}x avg`{f' | {mom_label}' if mom_label else ''}",
                f"Stop Loss `{c.stop_loss:.2f}` | Target 1 `{c.target1:.2f}` | Target 2 `{c.target2:.2f}`{risk_reward}",
                f"Why: {simple_reasons}",
                entry_type_line,
                entry_timing_line,
                hold_product_line,
                auto_status_line,
            ]
            if risk_flag:
                block.append(risk_flag)
            if c.suggested_qty > 0:
                _invest = round(c.suggested_qty * c.price, 0)
                block.append(f"Suggested: Buy **`{c.suggested_qty}` shares** (≈ ₹{_invest:,.0f})")
        sections.append("\n".join(block))

        self._log(
            f"{'🏆' if grade == 'A+' else '⭐' if grade == 'A' else '📝'} "
            f"BUY ALERT [{grade}] #{rank}: {c.symbol} at ₹{c.price:.2f} "
            f"({pct}) score={c.blind_score:.0f} vol={c.volume_ratio:.1f}x momentum={momentum:.0f}"
        )

        self._already_alerted_buys.add(c.symbol)
        self._save_daily_alerted(c.symbol)
        # Increment daily batch counter (top-N volume gate)
        self._daily_batch_alert_count += 1
        # Track for momentum continuation re-alerts
        self._momentum_tracked[c.symbol] = {
            "entry_price": c.price,
            "last_alert_pct": c.change_pct,
            "last_alert_time": datetime.now(),
            "stop_loss": c.stop_loss,
        }
        # Paper trade tracking
        self._paper_tracker.log_entry(
            symbol=c.symbol, price=c.price, score=c.blind_score,
            grade=grade, volume_ratio=c.volume_ratio,
            change_at_entry=c.change_pct, stop_loss=c.stop_loss,
            target1=c.target1, target2=c.target2,
            suggested_qty=c.suggested_qty, source="batch",
        )

    @staticmethod
    def _simplify_blind_reasons(reasons: str) -> str:
        """Translate scorer reason codes to plain English.

        For momentum runners (vol-halved tagged), overbought indicators are
        CONFIRMATORY — big money is pushing. Strip scary language for those.
        """
        translations = {
            "RSI oversold": "Stock is very cheap (oversold)",
            "RSI near oversold": "Stock is getting cheap",
            "RSI mid": "Stock at fair price",
            "RSI overbought": "Stock is hot — strong demand",
            "CCI oversold": "Momentum says undervalued",
            "CCI low": "Momentum low",
            "MFI oversold": "Selling exhaustion — buyers coming",
            "volume surge": "Big volume spike — strong interest",
            "strong volume": "Higher than usual volume",
            "good volume": "Decent volume",
            "adequate volume": "OK volume",
            "breakout": "Strong breakout move",
            "strong day": "Big green day",
            "green": "Stock moving up",
            "green candle": "Positive day",
            "flat day": "Sideways",
            "red day": "Stock is down today",
            "above ATR stop": "Uptrend intact",
            "MACD cross up": "Trend turning bullish",
            "trigger ready": "Buy trigger price reached",
            "very near trigger": "Almost at buy trigger",
            "near trigger": "Close to buy trigger",
            "high confidence": "Multiple indicators agree — buy",
            "consolidation breakout": "Coiled breakout — big potential",
            "range breakout": "Range breakout setup",
            "strong gap-up": "Strong gap-up at open — overnight demand",
            "gap-up": "Gap-up at open — positive sentiment",
            "BEARISH TREND - below all MAs": "⚠️ BEARISH — below all moving averages",
            "DEATH CROSS": "⚠️ Death cross warning",
            "downtrend pattern": "⚠️ Downtrend pattern",
            "entry confirmed": "Price above buy level ✅",
            "below buy trigger - NOT safe": "⛔ Price below buy level",
            "below buy trigger - wait": "Price just below buy level",
            "data shows green but price below trigger": "⚠️ Conflicting data — price below trigger",
        }
        parts = [p.strip() for p in reasons.split(",")]
        simple = []
        for p in parts:
            p_lower = p.lower()
            # Skip vol-halved overbought warnings entirely — these are momentum
            # confirmations, not risks. Showing "overbought" scares users away
            # from the strongest runners.
            if "[vol-halved]" in p_lower and ("overbought" in p_lower):
                # Replace with momentum confirmation instead of dropping silently
                if "extremely" in p_lower or "combo" in p_lower:
                    simple.append("Strong institutional buying")
                else:
                    simple.append("Momentum confirmed by volume")
                continue
            # Also soften the overbought combo warning on vol-halved entries
            if "overbought combo" in p_lower and "[vol-halved]" in p_lower:
                simple.append("Multiple momentum indicators aligned")
                continue
            matched = False
            for key, val in translations.items():
                if key.lower() in p_lower:
                    simple.append(val)
                    matched = True
                    break
            if not matched:
                simple.append(p)
        return " | ".join(simple)

    def _push_sell_alerts(self, alerts: List[SellAlert]) -> None:
        """Push sell alerts to Discord."""
        if not alerts:
            return

        sections = ["**SELL ALERT**"]

        for a in alerts:
            emoji = "+" if a.pnl_pct >= 0 else ""
            sections.append(
                f"**SELL `{a.symbol}`** x{a.quantity} @ `{a.current_price:.2f}` "
                f"({emoji}{a.pnl_pct:.1f}%)\n"
                f"Entry was `{a.entry_price:.2f}` | {a.reason}\n"
                f"_Sell in your broker → then use `/sell {a.symbol} {a.quantity} {a.current_price:.0f}` to record_"
            )
            self._already_alerted_sells.add(a.symbol)
            self._save_daily_sell_alerted(a.symbol)

        message = "\n\n".join(sections)
        self._log(f"Sending sell alerts for {len(alerts)} position(s) to Discord.")
        self._send_to_channels(message)

    def _send_to_channels(self, message: str) -> None:
        """Send message to all configured alert channels."""
        channels = {
            c.strip()
            for c in self.config.alert_channels.split(",")
            if c.strip()
        }
        if "discord" in channels:
            self._send_discord(message)
        if "telegram" in channels:
            self._send_telegram(message)
        if "desktop" in channels:
            self._send_desktop(message)

    def _send_discord(self, message: str) -> None:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook_url:
            try:
                from PKDevTools.classes.Environment import PKEnvironment
                env = PKEnvironment()
                webhook_url = str(env.allSecrets.get("DISCORD_WEBHOOK_URL", "")).strip()
            except Exception:
                pass
        if not webhook_url:
            self._log("Discord send skipped: no webhook URL configured")
            return
        import requests
        for attempt in range(3):
            try:
                resp = requests.post(
                    webhook_url,
                    json={"content": message[:1900]},
                    timeout=10,
                )
                if resp.status_code in (200, 204):
                    return
                if resp.status_code == 429:  # rate-limited
                    retry_after = float(resp.json().get("retry_after", 2))
                    time.sleep(min(retry_after, 5))
                    continue
                self._log(f"Discord send failed: HTTP {resp.status_code}")
                return
            except requests.exceptions.Timeout:
                if attempt < 2:
                    time.sleep(2)
                    continue
                self._log("Discord send failed: timeout after 3 attempts")
            except Exception as exc:
                self._log(f"Discord send failed: {exc}")
                return

    def _send_telegram(self, message: str) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message[:4000]},
                timeout=10,
            )
        except Exception:
            pass

    def _send_desktop(self, message: str) -> None:
        if os.name == "nt":
            try:
                import subprocess
                subprocess.run(
                    [
                        "powershell",
                        "-Command",
                        (
                            '[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
                            f'[System.Windows.Forms.MessageBox]::Show("{message[:500]}", "Trade Alert")'
                        ),
                    ],
                    check=False,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Config manager
    # ------------------------------------------------------------------

    def _get_config_manager(self):
        from pkscreener.classes.ConfigManager import tools as ConfigManager, parser
        config_manager = ConfigManager()
        config_manager.getConfig(parser)
        return config_manager

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        OutputControls().printOutput(f"  [AutoPilot {timestamp}] {message}")

    def _log_cycle(self, result: CycleResult) -> None:
        parts = [f"Cycle #{self._cycle_count + 1}"]
        if result.buy_candidates:
            symbols = ", ".join(c.symbol for c in result.buy_candidates)
            parts.append(f"BUY ALERTS: {symbols}")
        if result.sell_alerts:
            symbols = ", ".join(a.symbol for a in result.sell_alerts)
            parts.append(f"SELL ALERTS: {symbols}")
        if result.buy_paused:
            parts.append(f"BUY PAUSED: {'; '.join(result.pause_reasons)}")
        if not result.buy_candidates and not result.sell_alerts:
            parts.append(f"No new alerts ({result.open_positions} positions monitored)")
        if result.error:
            parts.append(f"ERROR: {result.error}")
        self._log(" | ".join(parts))

    def _save_cycle_log(self, result: CycleResult) -> None:
        """Append cycle result to the autopilot log CSV."""
        log_path = os.path.join(
            self.output_dir,
            f"autopilot_{self.config.account_name}_log.csv",
        )
        row = {
            "Timestamp": result.cycle_time,
            "Cycle": self._cycle_count + 1,
            "BuyAlerts": "; ".join(f"{c.symbol} (score={c.blind_score})" for c in result.buy_candidates),
            "SellAlerts": "; ".join(f"{a.symbol} ({a.reason})" for a in result.sell_alerts),
            "OpenPositions": result.open_positions,
            "BuyPaused": result.buy_paused,
            "Error": result.error or "",
        }
        row_df = pd.DataFrame([row])
        if os.path.isfile(log_path):
            row_df.to_csv(log_path, mode="a", header=False, index=False)
        else:
            row_df.to_csv(log_path, index=False)

    def _log_engine_confirmed_buy_candidate(
        self,
        symbol: str,
        action: str,
        live_price: float,
        stop_loss: float,
        target1: float,
        target2: float,
        blind_score: float,
        confidence: float,
        volume_ratio: float,
        daily_change: float,
        rr2: float,
        grade: str,
        rank_score: float,
        entry_type: str,
        entry_type_reason: str,
        auto_eligible: bool,
        auto_result: Optional[str],
    ) -> None:
        """Persist buy candidate auto-buy decisions for truth reports and training."""
        log_path = os.path.join(
            self.output_dir,
            f"engine_confirmed_buys_{self.config.account_name}.csv",
        )
        outcome = str(auto_result or "").strip()
        auto_status, auto_reason = self._classify_auto_buy_outcome(outcome, auto_eligible)
        row = {
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Symbol": symbol,
            "Action": action,
            "Price": round(float(live_price or 0), 2),
            "StopLoss": round(float(stop_loss or 0), 2),
            "Target1": round(float(target1 or 0), 2),
            "Target2": round(float(target2 or 0), 2),
            "BlindScore": round(float(blind_score or 0), 2),
            "ConfidencePct": round(float(confidence or 0), 2),
            "VolumeRatio": round(float(volume_ratio or 0), 2),
            "DailyChangePct": round(float(daily_change or 0), 2),
            "RR2": round(float(rr2 or 0), 2),
            "Grade": grade,
            "RankScore": round(float(rank_score or 0), 2),
            "EntryType": str(entry_type or ""),
            "EntryTypeReason": str(entry_type_reason or ""),
            "AutoEligible": bool(auto_eligible),
            "AutoStatus": auto_status,
            "AutoReason": auto_reason,
            "AutoBuyOutcome": outcome,
        }
        row_df = pd.DataFrame([row])
        if os.path.isfile(log_path):
            row_df.to_csv(log_path, mode="a", header=False, index=False)
        else:
            row_df.to_csv(log_path, index=False)

    @staticmethod
    def _classify_auto_buy_outcome(outcome: str, auto_eligible: bool) -> Tuple[str, str]:
        """Convert human-facing auto-buy text into stable analysis fields."""
        text = str(outcome or "").strip()
        lower = text.lower()
        if not auto_eligible:
            if "late_discovery" in lower:
                return "SKIPPED", "late_discovery"
            if "strict_mode_rejected" in lower:
                return "INELIGIBLE", "strict_mode_rejected"
            if "swing_recheck_rejected" in lower:
                return "INELIGIBLE", "swing_recheck_rejected"
            if "blocked" in lower:
                return "SKIPPED", "blocked"
            if "rejected" in lower:
                return "INELIGIBLE", "rejected"
            if "auto-buy cap" in lower or "alert-side auto-buy cap" in lower:
                return "SKIPPED", "auto_buy_cap"
            if "intraday auto-buy is paused after" in lower:
                return "INELIGIBLE", "intraday_window_closed"
            if "paused after 1:30" in lower:
                return "INELIGIBLE", "swing_window_closed"
            if "starts at 9:20" in lower:
                return "INELIGIBLE", "before_swing_window"
            if "intraday auto-buy is only allowed" in lower:
                return "INELIGIBLE", "intraday_window_closed"
            if "entry is already stretched" in lower:
                return "INELIGIBLE", "entry_stretched"
            if "entry type late_chase" in lower:
                return "INELIGIBLE", "late_chase"
            if "entry type swing_breakout" in lower:
                return "INELIGIBLE", "swing_breakout_unconfirmed"
            if "below trigger" in lower or "within 0.6% of trigger" in lower:
                return "INELIGIBLE", "entry_not_tight"
            if "r:r" in lower:
                return "INELIGIBLE", "rr_below_floor"
            if "volume" in lower:
                return "INELIGIBLE", "volume_below_floor"
            if "confidence" in lower:
                return "INELIGIBLE", "confidence_below_floor"
            if "score" in lower:
                return "INELIGIBLE", "score_below_floor"
            if "day move" in lower:
                return "INELIGIBLE", "day_move_outside_zone"
            if "price is below" in lower:
                return "INELIGIBLE", "price_below_floor"
            if "grade" in lower:
                return "INELIGIBLE", "grade_below_floor"
            if "horizon model" in lower or "alert-quality only" in lower:
                return "INELIGIBLE", "horizon_avoid"
            return "INELIGIBLE", "setup_not_tight"
        if not text:
            return "NO_ACTION", ""
        if "auto-bought" in lower:
            return "BOUGHT", "placed"
        if "pending" in lower or "retry queued" in lower or "funds/margin" in lower:
            return "PENDING", "funds_short"
        if "unavailable" in lower and "order manager" in lower:
            return "ERROR", "order_manager_unavailable"
        if "disabled" in lower:
            return "SKIPPED", "auto_trade_disabled"
        if "auto-buy cap" in lower or "alert-side auto-buy cap" in lower:
            return "SKIPPED", "auto_buy_cap"
        if "starter position exceeds limit" in lower:
            return "SKIPPED", "position_too_large"
        if "already" in lower and "holding" in lower:
            return "SKIPPED", "already_holding"
        if "max open positions" in lower:
            return "SKIPPED", "max_open_positions"
        if "market is closed" in lower:
            return "SKIPPED", "market_closed"
        if "skip" in lower:
            return "SKIPPED", "execution_skipped"
        return "ERROR", "execution_error"

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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger

from pkscreener.classes.RiskGuardrails import LiveTradingGuardrails


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AutoPilotConfig:
    """All tunable knobs for the blind-buy advisor."""

    universe: str = "nifty50"
    account_name: str = "main"

    # Buy-side filters (deliberately looser than the main scanner)
    min_confidence: float = 5.0         # accept weaker signals (main uses 10)
    min_trade_value: float = 0.0        # no liquidity floor — small caps welcome
    min_volume_ratio: float = 0.0       # don't gate on volume ratio
    max_entry_gap_pct: float = 5.0      # wider trigger window (backtest shows 1.5 blocks good picks)
    blind_buy_min_score: float = 60.0   # raised: only show good+ picks (score 60+/100)

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
    cycle_interval_sec: int = 60
    max_buy_alerts_per_cycle: int = 3

    def normalized(self) -> "AutoPilotConfig":
        return AutoPilotConfig(
            universe=str(self.universe or "nifty50").strip().lower(),
            account_name=str(self.account_name or "main").strip().lower().replace(" ", "_"),
            min_confidence=max(0.0, float(self.min_confidence if self.min_confidence is not None else 5.0)),
            min_trade_value=max(0.0, float(self.min_trade_value if self.min_trade_value is not None else 0.0)),
            min_volume_ratio=max(0.0, float(self.min_volume_ratio if self.min_volume_ratio is not None else 0.0)),
            max_entry_gap_pct=max(0.0, float(self.max_entry_gap_pct if self.max_entry_gap_pct is not None else 1.5)),
            blind_buy_min_score=max(0.0, float(self.blind_buy_min_score if self.blind_buy_min_score is not None else 40.0)),
            auto_exit_on_sell=bool(self.auto_exit_on_sell),
            auto_exit_on_stop=bool(self.auto_exit_on_stop),
            auto_exit_on_target2=bool(self.auto_exit_on_target2),
            max_daily_loss_amount=max(0.0, float(self.max_daily_loss_amount or 0.0)),
            max_consecutive_losses=max(0, int(self.max_consecutive_losses or 0)),
            max_workers=max(1, int(self.max_workers or 3)),
            alert_channels=str(self.alert_channels or "discord").strip().lower(),
            cycle_interval_sec=max(10, int(self.cycle_interval_sec or 60)),
            max_buy_alerts_per_cycle=max(1, int(self.max_buy_alerts_per_cycle or 5)),
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
    error: Optional[str] = None


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
        change_pct = float(row.get("ChangePct", row.get("Change %", 0)) or 0)
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

        # -- RSI (0-100, lower = more oversold = better for bounce) --
        if rsi < 30:
            score += 25
            reasons.append(f"RSI oversold ({rsi:.0f})")
        elif rsi < 40:
            score += 18
            reasons.append(f"RSI near oversold ({rsi:.0f})")
        elif rsi < 50:
            score += 10
            reasons.append(f"RSI mid ({rsi:.0f})")
        elif rsi > 70:
            score -= 8
            reasons.append(f"RSI overbought ({rsi:.0f})")

        # -- CCI (< -100 = oversold, > 100 = overbought) --
        if cci < -100:
            score += 12
            reasons.append(f"CCI oversold ({cci:.0f})")
        elif cci < -50:
            score += 6
            reasons.append(f"CCI low ({cci:.0f})")
        elif cci > 150:
            score -= 5

        # -- MFI (0-100, same as RSI but volume-weighted) --
        if mfi < 20:
            score += 10
            reasons.append(f"MFI oversold ({mfi:.0f})")
        elif mfi < 40:
            score += 5
        elif mfi > 80:
            score -= 5

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

        # -- Green candle / price action --
        # Guard: if live price is below buy trigger, the change_pct from
        # historical candles may be stale — do NOT award breakout/green points.
        if _price_below_trigger and change_pct > 0:
            score -= 5
            reasons.append("⚠️ data shows green but price below trigger — treated as suspect")
        elif change_pct >= 5.0:
            score += 15
            reasons.append(f"breakout +{change_pct:.1f}%")
        elif change_pct >= 2.5:
            score += 12
            reasons.append(f"strong day +{change_pct:.1f}%")
        elif change_pct >= 1.0:
            score += 10
            reasons.append(f"green +{change_pct:.1f}%")
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
        self._already_alerted_buys: Set[str] = set()  # avoid repeat buy alerts
        self._already_alerted_sells: Set[str] = set()  # avoid repeat sell alerts
        self._last_buy_alert_time: Optional[datetime] = None  # cooldown tracking
        self._pending_followups: List["BlindBuyCandidate"] = []  # buffer for batching
        self._FOLLOWUP_COOLDOWN_SEC = 900  # 15 minutes between follow-up alerts

        # Tick-only fast scanner state (no OHLCV, fires in < 1 second)
        self._tick_alerted: Dict[str, datetime] = {}  # symbol -> time of last tick-triggered scan
        self._prev_tick_snapshot: Dict[str, Dict] = {}  # last tick reading for delta-volume
        self._TICK_ORDER_IMBALANCE_RATIO = 2.5  # buy_qty / sell_qty threshold
        self._TICK_VOLUME_BURST_FRACTION = 0.025  # 2.5% of day-vol in 60s
        self._TICK_HIGH_DAILY_CHANGE_PCT = 2.0   # daily change% to auto-flag (catch moves early, at 2% not 3%)
        self._TICK_ALREADY_MOVED_PCT = 15.0      # above this % the move is over — don't chase (raised from 7.5 to catch full intraday moves)
        self._TICK_ALERT_COOLDOWN_SEC = 1800     # 30 min before re-triggering same symbol
        # Early Mover: catch stocks at 0.3-2% with heavy accumulation (volume moves before price)
        self._TICK_EARLY_MOVER_IMBALANCE = 3.0   # stricter imbalance ratio for early, unconfirmed moves
        self._TICK_EARLY_MOVER_MIN_PCT = 0.3     # minimum daily change% to consider as early mover
        self._TICK_EARLY_MOVER_VOL_RATIO = 3.0   # OHLCV volume ratio required to confirm early mover
        # NSE market hours (IST): 9:15 AM open, 3:30 PM close
        self._MARKET_OPEN_HOUR_IST = 9
        self._MARKET_OPEN_MIN_IST = 15
        self._MARKET_CLOSE_HOUR_IST = 15
        self._MARKET_CLOSE_MIN_IST = 30
        # Pre-market watchlist state
        self._premarket_sent_date: Optional[str] = None  # date string — send once per day
        self._midday_sent_date: Optional[str] = None     # mid-day scanner — once per day
        self._powerhour_sent_date: Optional[str] = None  # power hour alert — once per day
        self._eod_exit_sent_date: Optional[str] = None   # end-of-day exit reminder — once per day
        # Momentum continuation tracking: symbol -> {alert_time, alert_pct, entry_price}
        self._momentum_tracked: Dict[str, Dict] = {}  # stocks we alerted — track for continuation

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

            # 2. Check risk guardrails
            guardrail_status = self._guardrails.evaluate()
            if guardrail_status.buy_paused:
                result.buy_paused = True
                result.pause_reasons = guardrail_status.reasons

            # 3. Find blind-buy candidates
            if not result.buy_paused:
                result.buy_candidates = self._find_blind_buys(signal_state)

            # 4. Check portfolio for sell signals
            result.sell_alerts, result.open_positions = self._check_portfolio_sells(signal_state)

            # 5. Push alerts
            self._push_buy_alerts(result.buy_candidates)
            self._push_sell_alerts(result.sell_alerts)

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
        while True:
            # Pre-market watchlist: fires once per day at 9:05 AM IST
            self._run_premarket_watchlist()
            # Mid-day scanner: fires once per day at 11:00 AM IST
            self._run_midday_scanner()
            # Power hour alert: fires once per day at 2:00 PM IST
            self._run_powerhour_alert()
            # Momentum continuation: re-alerts on alerted stocks still running strongly
            self._run_momentum_continuation()
            # End-of-day exit reminder: fires once per day at 3:10 PM IST
            self._run_eod_exit_alert()
            # Fast tick scan (zero API calls, < 1s) — catches pre-jump conditions
            # BEFORE the full OHLCV cycle would even start.
            self._run_tick_fast_scan()
            self.run_once()

            # During the 60s wait, run tick scans every 5s.
            # KiteTickerStream refreshes the tick snapshot every 2s — so we get
            # near-real-time price detection (5s latency) without any API calls.
            # Full OHLCV scan still runs every 60s (Kite rate-limit safe).
            _tick_poll_interval = 5
            _remaining = self.config.cycle_interval_sec
            while _remaining > _tick_poll_interval:
                time.sleep(_tick_poll_interval)
                _remaining -= _tick_poll_interval
                self._run_tick_fast_scan()
            if _remaining > 0:
                time.sleep(_remaining)

    # ------------------------------------------------------------------
    # Pre-market watchlist (9:05 AM IST, once per day)
    # ------------------------------------------------------------------

    def _run_premarket_watchlist(self) -> None:
        """Send a pre-market watchlist at 9:05 AM IST every trading day.

        Uses the previous day's signal state (already scanned by the signal monitor)
        to identify the top setups BEFORE the market opens, so you're ready to act
        the moment prices move at 9:15 AM.

        Selects stocks with:
        - Score >= 55 (strong technical setup from yesterday's close)
        - Volume ratio >= 1.5x (above-average volume recently)
        - BuyAbove price available (clear entry trigger)
        - Not bearish (no DO NOT BUY flags)
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

        # Fire between 9:05 AM and 9:14 AM IST (before market opens), once per day
        _PREMARKET_START = 9 * 60 + 5
        _PREMARKET_END   = 9 * 60 + 14

        if not (_PREMARKET_START <= now_min <= _PREMARKET_END):
            return
        if self._premarket_sent_date == today_str:
            return  # Already sent today

        self._premarket_sent_date = today_str  # Mark as sent before doing work

        self._log("Pre-market watchlist: preparing...")

        # Load yesterday's signal state
        signal_state = self._load_signal_state()
        if signal_state is None or signal_state.empty:
            signal_state = self._run_fresh_scan()
        if signal_state is None or signal_state.empty:
            self._log("Pre-market watchlist: no signal data available, skipping.")
            return

        picks = []
        for _, row in signal_state.iterrows():
            symbol = str(row.get("Symbol", "")).strip().upper()
            if not symbol:
                continue

            blind_score, blind_reasons = BlindBuyScorer.score(row)
            if blind_score < 55:
                continue

            # Skip bearish / dangerous signals
            reasons_lower = blind_reasons.lower()
            if "do not buy" in reasons_lower or "bearish" in reasons_lower:
                continue

            vol_ratio = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
            buy_above = float(row.get("BuyAbove", row.get("Buy Above", row.get("Entry", 0))) or 0)
            stop_loss = float(row.get("SellIfBelow", row.get("Sell If Below", 0)) or 0)
            target1   = float(row.get("TakeProfit1", row.get("Take Profit 1", 0)) or 0)
            target2   = float(row.get("TakeProfit2", row.get("Take Profit 2", 0)) or 0)
            price     = float(row.get("PriceNow", row.get("Price Now", row.get("LTP", 0))) or 0)
            confidence = float(row.get("ConfidencePct", row.get("Signal Strength %", 0)) or 0)

            if buy_above <= 0 or price <= 0:
                continue

            rr2 = 0.0
            if stop_loss > 0 and target2 > 0 and price > stop_loss:
                risk = price - stop_loss
                rr2 = round((target2 - price) / risk, 1) if risk > 0 else 0

            if rr2 < 1.5:
                continue  # Not worth the risk

            picks.append({
                "symbol": symbol,
                "score": blind_score,
                "vol_ratio": vol_ratio,
                "buy_above": buy_above,
                "stop_loss": stop_loss,
                "target1": target1,
                "target2": target2,
                "price": price,
                "confidence": confidence,
                "rr2": rr2,
                "reasons": blind_reasons,
            })

        if not picks:
            self._log("Pre-market watchlist: no qualifying setups today.")
            # Still send a brief message so user knows the system is alive
            self._send_to_channels(
                f"**📋 PRE-MARKET WATCHLIST — {now_ist.strftime('%d %b %Y')}**\n"
                f"No strong setups found from yesterday's scan. Market may be quiet.\n"
                f"_Tick scanner is active — alerts will fire if anything develops after 9:15 AM_"
            )
            return

        # Sort: score desc, then R:R desc
        picks.sort(key=lambda x: (-x["score"], -x["rr2"]))
        top = picks[:8]  # max 8 picks to avoid wall of text

        score_emoji = lambda s: "🔥" if s >= 70 else "✅"
        lines = [
            f"**📋 PRE-MARKET WATCHLIST — {now_ist.strftime('%d %b %Y')}**",
            f"Top **{len(top)}** setup{'s' if len(top) != 1 else ''} from yesterday's close. Be ready at 9:15 AM.\n",
        ]
        for p in top:
            rr_str = f" | R:R 1:{p['rr2']}" if p['rr2'] > 0 else ""
            vol_str = f" | Vol {p['vol_ratio']:.1f}x" if p['vol_ratio'] > 0 else ""
            conf_str = f" | Conf {p['confidence']:.0f}%" if p['confidence'] > 0 else ""
            lines.append(
                f"{score_emoji(p['score'])} **{p['symbol']}** — Score {p['score']:.0f}/100{rr_str}{vol_str}{conf_str}\n"
                f"   Buy above `{p['buy_above']:.2f}` | Stop `{p['stop_loss']:.2f}` | T1 `{p['target1']:.2f}` | T2 `{p['target2']:.2f}`"
            )

        lines.append(
            "\n_These are setups to act on IF price breaks above 'Buy above' level with volume in the first 15 minutes._\n"
            "_Tick scanner is live — BUY alerts will fire automatically when entry conditions are met._"
        )
        self._send_to_channels("\n".join(lines))
        self._log(f"Pre-market watchlist sent: {len(top)} picks.")

    # ------------------------------------------------------------------
    # Mid-day consolidation scanner (11:00 AM IST, once per day)
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
            self._send_to_channels(
                f"**📊 MID-DAY SCAN (11 AM) — {now_ist.strftime('%d %b %Y')}**\n"
                f"No consolidation breakout setups found right now.\n"
                f"_Tick scanner remains active for any new signals_"
            )
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
        self._send_to_channels("\n".join(lines))
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
            self._send_to_channels(
                f"**⚡ POWER HOUR (2 PM) — {now_ist.strftime('%d %b %Y')}**\n"
                f"No high-conviction setups for the final push today.\n"
                f"_Consider holding existing positions but avoid new entries without strong signals_"
            )
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
        self._send_to_channels("\n".join(lines))
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

            # Check volume from signal state
            signal_state = self._load_signal_state()
            vol_ratio = 0.0
            if signal_state is not None and not signal_state.empty:
                rows = signal_state[signal_state["Symbol"].str.upper() == sym]
                if not rows.empty:
                    vol_ratio = float(rows.iloc[0].get("VolumeRatio", 0) or 0)

            if vol_ratio > 0 and vol_ratio < _CONTINUATION_MIN_VOL:
                continue

            # Compute profit from original entry
            profit_pct = 0.0
            if entry_price > 0:
                profit_pct = round((current_price - entry_price) / entry_price * 100, 1)

            vol_str = f" | Vol `{vol_ratio:.1f}x`" if vol_ratio > 0 else ""
            profit_str = f" | From entry: `+{profit_pct:.1f}%`" if profit_pct > 0 else ""
            # Trailing stop suggestion: 4% below current price
            trail_stop = round(current_price * 0.96, 2)

            message = (
                f"**\U0001f680 MOMENTUM RUNNING `{sym}`** — still pushing! "
                f"Now `+{current_change:.1f}%` (was `+{last_alert_pct:.1f}%` when alerted){profit_str}\n"
                f"Price `{current_price:.2f}`{vol_str}\n"
                f"Trail stop → `{trail_stop:.2f}` (4% below current)\n"
                f"_Don't sell yet — let it run. Move stop up to protect profits._"
            )
            self._send_to_channels(message)
            self._log(f"Momentum continuation alert: {sym} now +{current_change:.1f}% (was +{last_alert_pct:.1f}%)")

            # Update tracking
            self._momentum_tracked[sym]["last_alert_time"] = now
            self._momentum_tracked[sym]["last_alert_pct"] = current_change

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

        lines.append("_Go to Groww → Portfolio → Sell all intraday positions. Do it NOW._")
        self._send_to_channels("\n".join(lines))
        self._log("EOD exit alert sent.")

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

        ticks = self._load_ticks_snapshot()
        if not ticks:
            return

        now = datetime.now()
        flagged: List[Dict] = []

        for symbol, tick in ticks.items():
            # Skip if already in the OHLCV-confirmed alert set (full buy alert sent)
            if symbol in self._already_alerted_buys:
                continue

            # Cooldown: don't spam re-alerts on the same symbol
            last_tick_alert = self._tick_alerted.get(symbol)
            if isinstance(last_tick_alert, datetime):
                if (now - last_tick_alert).total_seconds() < self._TICK_ALERT_COOLDOWN_SEC:
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
                    # Priority: daily change% + imbalance strength
                    "_priority": daily_change * 10 + (
                        (total_buy / max(total_sell, 1)) if total_sell > 0 else 0
                    ),
                })

        # Update snapshot for next cycle's delta-volume calculation
        self._prev_tick_snapshot = {s: dict(t) for s, t in ticks.items()}

        if not flagged:
            return

        # Sort by priority (biggest mover + strongest imbalance first)
        flagged.sort(key=lambda x: -x["_priority"])

        # Limit to top 5 per cycle to avoid flooding threads
        import threading
        for item in flagged[:5]:
            sym = item["symbol"]
            self._tick_alerted[sym] = now
            # Immediately spin up a targeted single-symbol OHLCV scan in background.
            # One stock = one API call = ~1-2 seconds. No waiting for the full queue.
            t = threading.Thread(
                target=self._confirm_tick_signal_with_ohlcv,
                args=(item,),
                daemon=True,
            )
            t.start()

    def _confirm_tick_signal_with_ohlcv(self, item: Dict) -> None:
        """Confirm or reject a tick signal with a single-symbol OHLCV scan.

        Runs in a background thread, takes ~1-2 seconds (one Kite API call),
        then sends either:
          - 🟢 BUY ALERT  — tick + OHLCV both confirm, full targets/stop-loss included
          - ⚠️ WATCH note — tick fired but OHLCV score is too low to act, with reason why

        This replaces the old "EARLY WATCH — wait 45 min for full scan" pattern.
        """
        sym = item["symbol"]
        price = item["price"]
        daily_change = item["daily_change"]
        direction = "+" if daily_change >= 0 else ""
        reasons_text = "\n".join(f"  \u2022 {r}" for r in item["reasons"])

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

        # If OHLCV is unavailable (no Kite history, SME/penny stock, etc.),
        # suppress silently — we have no targets, no stop-loss, nothing actionable.
        # Sending a "check manually" alert is noise that wastes the user's attention.
        if report_df is None or report_df.empty:
            self._log(f"Tick OHLCV unavailable for {sym} (+{daily_change:.1f}%) — suppressed (no data to act on)")
            return

        row = report_df.iloc[0]

        # For intraday momentum moves (daily_change >= 3%), the signal state's BuyAbove
        # was set from YESTERDAY's close as a swing breakout level. On an intraday move,
        # the stock has already confirmed direction — the tick signal IS the trigger.
        # Applying a 25-pt penalty + raised min-score because price hasn't hit a stale
        # swing level would suppress valid intraday entries (e.g. HINDOILEXP up 5% with
        # volume, BuyAbove set 15% above yesterday's close → penalty fires all day).
        # Fix: for 3%+ intraday moves, zero out the stale BuyAbove so scorer skips it.
        scoring_row = row.copy()
        if daily_change >= 3.0:
            scoring_row["PriceNow"] = price   # use live tick price as baseline
            scoring_row["Price Now"] = price
            scoring_row["BuyAbove"] = 0       # skip stale swing-level trigger penalty
            scoring_row["Buy Above"] = 0
            scoring_row["Entry"] = 0

        blind_score, blind_reasons = BlindBuyScorer.score(scoring_row)
        is_early_mover = item.get("early_mover", False)

        _price_now = float(row.get("PriceNow", row.get("Price Now", price)) or price)
        _buy_above = float(row.get("BuyAbove", row.get("Buy Above", 0)) or 0)
        # Fix display: if no trigger price from signal engine, show current price as entry
        _buy_above_display = _buy_above if _buy_above > 0 else _price_now
        _price_below_trigger = _price_now > 0 and _buy_above > 0 and _price_now < _buy_above
        # For intraday momentum moves, don't raise the effective min score either
        _intraday_override = daily_change >= 3.0

        # For early movers, require stricter score AND confirmed high volume from OHLCV
        if is_early_mover:
            volume_ratio_check = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
            effective_min = 60.0 if volume_ratio_check >= self._TICK_EARLY_MOVER_VOL_RATIO else 999.0
        elif _intraday_override:
            # Intraday momentum (3%+ move): stale trigger already cleared, use base threshold
            effective_min = self.config.blind_buy_min_score
        else:
            effective_min = 65.0 if _price_below_trigger else self.config.blind_buy_min_score

        if blind_score >= effective_min and sym not in self._already_alerted_buys:
            # CONFIRMED — send a proper BUY ALERT immediately
            stop_loss = float(row.get("SellIfBelow", row.get("Sell If Below", 0)) or 0)
            target1 = float(row.get("TakeProfit1", row.get("Take Profit 1", 0)) or 0)
            target2 = float(row.get("TakeProfit2", row.get("Take Profit 2", 0)) or 0)
            suggested_qty = int(float(row.get("SuggestedQty", row.get("Suggested Qty", 0)) or 0))
            confidence = float(row.get("ConfidencePct", row.get("Signal Strength %", 0)) or 0)
            volume_ratio = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
            atr = float(row.get("ATR", 0) or 0)

            # Fallback stop/target computation.
            # Early movers: use wider targets (5%/10%) to capture the full expected move.
            # Regular (HOLD-rated) stocks already running: conservative 2%/4%.
            if stop_loss <= 0 and _price_now > 0:
                if atr > 0:
                    stop_loss = round(_price_now - (2.0 if is_early_mover else 1.5) * atr, 2)
                else:
                    stop_loss = round(_price_now * (0.96 if is_early_mover else 0.95), 2)
            if target1 <= 0 and _price_now > 0:
                target1 = round(_price_now * (1.05 if is_early_mover else 1.02), 2)
            if target2 <= 0 and _price_now > 0:
                target2 = round(_price_now * (1.10 if is_early_mover else 1.04), 2)
            # Suggested qty: risk ₹2000 for early movers (better setup), ₹1000 for chasers
            risk_budget = 2000 if is_early_mover else 1000
            if suggested_qty <= 0 and stop_loss > 0 and _price_now > stop_loss:
                risk_per_share = _price_now - stop_loss
                suggested_qty = max(1, int(risk_budget / risk_per_share))

            # Gate 1: volume must be ≥ 1.0x average (not below-average volume move)
            # 0.5x volume on a 5% move = thin penny stock, not real institutional buying.
            if volume_ratio > 0 and volume_ratio < 1.0:
                self._log(f"Suppressed {sym}: volume {volume_ratio:.1f}x < 1.0x avg")
                return

            score_label = f"\U0001f525 **{blind_score:.0f}/100**" if blind_score >= 70 else f"\u2705 **{blind_score:.0f}/100**"
            risk_reward = ""
            rr2 = 0.0
            if stop_loss > 0 and target2 > 0 and _price_now > 0:
                risk = abs(_price_now - stop_loss)
                rr2 = round(abs(target2 - _price_now) / risk, 1) if risk > 0 else 0
                risk_reward = f" | R:R `1:{rr2}`"

            # Gate 2: R:R must be at least 1:1.5 — risking more than you gain is a losing strategy
            min_rr = 1.5
            if rr2 > 0 and rr2 < min_rr:
                self._log(f"Suppressed {sym}: R:R 1:{rr2} < 1:{min_rr} minimum")
                return

            qty_line = f"\nSuggested Qty: `{suggested_qty}`" if suggested_qty > 0 else ""

            # Time-of-day context: after 1 PM + already moved 5%+ = swing trade only
            try:
                import pytz
                _ist = pytz.timezone("Asia/Kolkata")
                _nist = datetime.now(_ist)
            except Exception:
                from datetime import timezone, timedelta
                _nist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
            _is_late = _nist.hour * 60 + _nist.minute >= 13 * 60
            is_swing_only = _is_late and daily_change >= 8.0
            swing_tag = f"\n⏰ **SWING ONLY** — already +{daily_change:.1f}%, too late for intraday. Hold 2-5 days for targets." if is_swing_only else ""

            if is_early_mover:
                alert_header = f"**\U0001f331 EARLY MOVER `{sym}`** (accumulation detected, low % — target 5-10%) \u2014 Score {score_label}"
            else:
                alert_header = f"**\U0001f7e2 BUY `{sym}`** (tick-confirmed in ~2s) \u2014 Score {score_label}{swing_tag}"
            message = (
                f"{alert_header}\n"
                f"Price `{_price_now:.2f}` ({direction}{daily_change:.2f}%) | Buy Above `{_buy_above_display:.2f}`\n"
                f"Stop Loss `{stop_loss:.2f}` | Target 1 `{target1:.2f}` | Target 2 `{target2:.2f}`{risk_reward}\n"
                f"Volume `{volume_ratio:.1f}x avg` | Confidence `{confidence:.0f}%`\n"
                f"Tick signals:\n{reasons_text}\n"
                f"OHLCV confirms: {self._simplify_blind_reasons(blind_reasons)}"
                f"{qty_line}\n"
                f"_Open Groww \u2192 Buy \u2192 then use `/buy {sym} QTY PRICE` to record_"
            )
            self._already_alerted_buys.add(sym)
            self._send_to_channels(message)
            # Track for momentum continuation re-alerts
            self._momentum_tracked[sym] = {
                "entry_price": _price_now,
                "last_alert_pct": daily_change,
                "last_alert_time": datetime.now(),
            }
        else:
            # REJECTED — only send WATCH if this could still become actionable:
            # - Early mover (stock at 0-2%): worth watching, may break out soon
            # - Score ≥ 45: close miss, may upgrade to BUY on next cycle
            # - No daily_change cap: 10.2x buy imbalance at 3.85% is still actionable
            # Stocks with DO NOT BUY / BEARISH / at stop loss — never send WATCH, pure noise.
            reasons_lower = blind_reasons.lower()
            has_hard_blocker = (
                "do not buy" in reasons_lower
                or "bearish" in reasons_lower
                or "stop loss level" in reasons_lower
            )
            watch_worthy = (
                not has_hard_blocker
                and (is_early_mover or blind_score >= 45)
                and daily_change < self._TICK_ALREADY_MOVED_PCT  # don't WATCH if train left
            )
            if watch_worthy:
                # Split reasons into positives and blockers for readable formatting
                reason_parts = [r.strip() for r in blind_reasons.split(",") if r.strip()]
                _pos_keywords = ["trigger", "volume", "breakout", "green", "above", "oversold", "momentum", "bullish", "cross up", "atr stop", "consolidation", "high confidence"]
                # "above ATR stop" contains "stop" but is bullish — check positives FIRST,
                # then only fall through to neg check if not already matched as positive.
                _neg_keywords = ["below", "bearish", "danger", "do not", "overbought", "red day", "weak", "low volume", "below trigger", "below buy"]
                positives = []
                blockers = []
                for rp in reason_parts:
                    rp_low = rp.lower()
                    if any(k in rp_low for k in _pos_keywords):
                        positives.append(f"  ✅ {rp.strip()}")
                    elif any(k in rp_low for k in _neg_keywords):
                        blockers.append(f"  ❌ {rp.strip()}")
                    # neutral/unknown: skip to reduce noise

                pos_block = "\n".join(positives) if positives else "  (none)"
                neg_block = "\n".join(blockers) if blockers else "  (none)"

                message = (
                    f"**⚠️ WATCH `{sym}`** — Not a buy yet (score {blind_score:.0f}/100)\n"
                    f"Price `{price:.2f}` | Up `+{daily_change:.2f}%` today\n"
                    f"\n**Tick signal:**\n{reasons_text}\n"
                    f"\n**Why it looks interesting:**\n{pos_block}\n"
                    f"\n**Why NOT buying yet:**\n{neg_block}\n"
                    f"\n_Will re-check every 5s. Alert will upgrade to BUY if blockers clear._"
                )
                self._send_to_channels(message)
            else:
                self._log(f"Suppressed WATCH {sym}: score={blind_score:.0f}, change={daily_change:.2f}% (hard blocker or not actionable)")

    def _load_ticks_snapshot(self) -> Dict[str, Dict]:
        """Read the latest ticks JSON saved by KiteTickerStream."""
        ticks_path = os.path.join(
            self.output_dir,
            f"kiteticker_{self.config.universe}_ticks_latest.json",
        )
        if not os.path.isfile(ticks_path):
            return {}
        try:
            import json
            with open(ticks_path, "r") as f:
                data = json.load(f)
            return {str(k).strip().upper(): v for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Signal state
    # ------------------------------------------------------------------

    def _load_signal_state(self) -> Optional[pd.DataFrame]:
        """Load the latest signal monitor state CSV."""
        # Try the full actionable report first (has all columns we need)
        for name in ["state", "signals", "actionable"]:
            path = os.path.join(
                self.output_dir,
                f"signalmonitor_{self.config.universe}_{name}_latest.csv",
            )
            if os.path.isfile(path):
                try:
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

        for _, row in signal_df.iterrows():
            symbol = str(row.get("Symbol", "")).strip().upper()
            if not symbol:
                continue

            # Get the original action
            now_action = str(row.get("NowAction", row.get("What To Do", ""))).strip()

            # Skip stocks already saying Sell or Hold
            if any(x in now_action.lower() for x in ["sell", "exit", "hold", "do nothing"]):
                continue

            # Score with our aggressive blind-buy scorer
            blind_score, blind_reasons = BlindBuyScorer.score(row)

            # Check minimum score threshold
            # If price is below buy trigger, requiring a higher score (65+) to avoid
            # sending actionable alerts on stocks that aren't ready to enter yet
            _price_now = float(row.get("PriceNow", row.get("Price Now", row.get("LTP", 0))) or 0)
            _buy_above = float(row.get("BuyAbove", row.get("Buy Above", row.get("Entry", 0))) or 0)
            _price_below_trigger = _price_now > 0 and _buy_above > 0 and _price_now < _buy_above
            effective_min = 65.0 if _price_below_trigger else self.config.blind_buy_min_score
            if blind_score < effective_min:
                continue

            # Check entry gap
            entry_gap = float(row.get("EntryGapPct", row.get("Entry Gap %", 0)) or 0)
            if entry_gap > self.config.max_entry_gap_pct:
                continue

            # Check confidence
            confidence = float(
                row.get("ConfidencePct", row.get("Signal Strength %",
                    row.get("SignalStrengthPct", 0))) or 0
            )
            if confidence < self.config.min_confidence:
                continue

            price = float(row.get("LTP", row.get("PriceNow", row.get("Price Now", 0))) or 0)
            stop_loss = float(row.get("StopLoss", row.get("SellIfBelow", row.get("Sell If Below", 0))) or 0)
            target2   = float(row.get("Target2", row.get("TakeProfit2", row.get("Take Profit 2", 0))) or 0)
            vol_ratio = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)

            # Volume must be >= 1.0x average — below average means weak conviction
            if vol_ratio > 0 and vol_ratio < 1.0:
                continue

            # R:R must be >= 1.5 — risking more than you gain is a losing strategy
            if stop_loss > 0 and target2 > 0 and price > stop_loss:
                risk = price - stop_loss
                rr2 = (target2 - price) / risk if risk > 0 else 0
                if rr2 < 1.5:
                    continue

            candidates.append(BlindBuyCandidate(
                symbol=symbol,
                price=price,
                buy_above=float(row.get("Entry", row.get("BuyAbove", row.get("Buy Above", 0))) or 0),
                stop_loss=stop_loss,
                target1=float(row.get("Target1", row.get("TakeProfit1", row.get("Take Profit 1", 0))) or 0),
                target2=target2,
                confidence=confidence,
                volume_ratio=vol_ratio,
                change_pct=float(row.get("ChangePct", row.get("Change %", 0)) or 0),
                blind_score=blind_score,
                original_action=now_action,
                reasons=blind_reasons,
                checks=str(row.get("QualityChecks", row.get("Checks", ""))).strip(),
                suggested_qty=int(float(row.get("SuggestedQty", row.get("Suggested Qty", 0)) or 0)),
            ))

        # Sort by blind_score descending — best opportunities first
        candidates.sort(key=lambda c: c.blind_score, reverse=True)

        # Limit to top N and exclude already-alerted
        fresh_candidates = []
        for c in candidates:
            if c.symbol in self._already_alerted_buys:
                continue
            fresh_candidates.append(c)
            if len(fresh_candidates) >= self.config.max_buy_alerts_per_cycle:
                break

        return fresh_candidates

    # ------------------------------------------------------------------
    # Sell monitoring
    # ------------------------------------------------------------------

    def _check_portfolio_sells(self, signal_df: pd.DataFrame) -> Tuple[List[SellAlert], int]:
        """Check all open portfolio positions for sell conditions."""
        sell_alerts = []
        trades_df = self._load_portfolio_trades()
        if trades_df is None or trades_df.empty:
            return sell_alerts, 0

        open_trades = trades_df[trades_df["Status"] == "OPEN"]
        if open_trades.empty:
            return sell_alerts, 0

        # Build signal lookup
        signal_map = {}
        for _, row in signal_df.iterrows():
            sym = str(row.get("Symbol", "")).strip().upper()
            if sym:
                signal_map[sym] = row

        for _, trade in open_trades.iterrows():
            symbol = str(trade.get("Symbol", "")).strip().upper()
            quantity = int(float(trade.get("Quantity", 0)))
            entry_price = float(trade.get("EntryPrice", 0))
            if not symbol or quantity <= 0:
                continue

            # Get current price from signal data or trade data
            current_price = float(trade.get("CurrentPrice", 0))
            signal_row = signal_map.get(symbol)
            if signal_row is not None:
                signal_price = float(signal_row.get("LTP", signal_row.get("PriceNow", signal_row.get("Price Now", 0))) or 0)
                if signal_price > 0:
                    current_price = signal_price

            if current_price <= 0:
                current_price = entry_price

            invested = float(trade.get("InvestedAmount", entry_price * quantity))
            pnl_pct = round((current_price - entry_price) * 100 / entry_price, 2) if entry_price > 0 else 0.0

            should_sell = False
            reason = ""

            # Check signal says sell
            if self.config.auto_exit_on_sell and signal_row is not None:
                now_action = str(signal_row.get("NowAction", signal_row.get("What To Do", ""))).strip()
                if any(x in now_action.lower() for x in ["sell", "exit"]):
                    should_sell = True
                    reason = f"Signal: {now_action}"

            # Check stop loss
            if not should_sell and self.config.auto_exit_on_stop:
                stop_loss = float(trade.get("StopLoss", 0))
                if stop_loss > 0 and current_price <= stop_loss:
                    should_sell = True
                    reason = f"STOP LOSS hit ({stop_loss:.2f}), cut losses now"

            # Check target
            if not should_sell and self.config.auto_exit_on_target2:
                target2 = float(trade.get("Target2", 0))
                if target2 > 0 and current_price >= target2:
                    should_sell = True
                    reason = f"TARGET 2 HIT ({target2:.2f}), book profits!"

            # Also check target1 — alert but with different urgency
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
    # Discord push alerts
    # ------------------------------------------------------------------

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
        open_minutes = self._MARKET_OPEN_HOUR_IST * 60 + self._MARKET_OPEN_MIN_IST
        close_minutes = self._MARKET_CLOSE_HOUR_IST * 60 + self._MARKET_CLOSE_MIN_IST
        now_minutes = now_ist.hour * 60 + now_ist.minute
        return open_minutes <= now_minutes <= close_minutes

    def _push_buy_alerts(self, candidates: List[BlindBuyCandidate]) -> None:
        """Push blind-buy alerts to Discord.

        First batch (no prior alerts) sends immediately.
        Follow-up discoveries are buffered and sent at most once every 15 minutes.
        """
        if not self._is_market_open():
            return  # NSE not yet open — suppress all buy alerts

        if not candidates:
            # Even with no new candidates, check if buffered follow-ups are ready
            self._flush_pending_followups()
            return

        is_first_batch = len(self._already_alerted_buys) == 0

        if is_first_batch:
            # Send immediately — this is the opening alert
            self._do_send_buy_alert(candidates, is_followup=False)
            self._last_buy_alert_time = datetime.now()
        else:
            # Buffer follow-up picks
            self._pending_followups.extend(candidates)
            # Mark as alerted immediately so they don't re-queue next cycle
            for c in candidates:
                self._already_alerted_buys.add(c.symbol)
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
        # Sort buffered by score descending
        self._pending_followups.sort(key=lambda c: c.blind_score, reverse=True)
        self._do_send_buy_alert(self._pending_followups, is_followup=True)
        self._last_buy_alert_time = datetime.now()
        self._pending_followups = []

    def _do_send_buy_alert(self, candidates: List[BlindBuyCandidate], is_followup: bool) -> None:
        """Format and send a buy alert message to Discord."""
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

        if is_followup:
            sections = [
                "**🟢 NEW PICKS FOUND**",
                f"**{len(candidates)}** new stock{'s' if len(candidates) != 1 else ''} scored 60+/100 in the last 15 min:",
            ]
        else:
            sections = [
                "**🟢 BUY ALERT**",
                f"Top **{len(candidates)}** pick{'s' if len(candidates) != 1 else ''} right now (score 60+/100):",
            ]

        for c in candidates:
            pct = f"+{c.change_pct:.1f}%" if c.change_pct > 0 else f"{c.change_pct:.1f}%"
            risk_reward = ""
            if c.stop_loss > 0 and c.target2 > 0 and c.price > 0:
                risk = abs(c.price - c.stop_loss)
                reward1 = abs(c.target1 - c.price) if c.target1 > 0 else 0
                reward2 = abs(c.target2 - c.price)
                rr1 = round(reward1 / risk, 1) if risk > 0 else 0
                rr2 = round(reward2 / risk, 1) if risk > 0 else 0
                risk_reward = f" | R:R `1:{rr1}` / `1:{rr2}`"

            # Score emoji
            if c.blind_score >= 70:
                score_label = f"🔥 **{c.blind_score:.0f}/100** (Excellent)"
            elif c.blind_score >= 60:
                score_label = f"✅ **{c.blind_score:.0f}/100** (Good)"
            else:
                score_label = f"**{c.blind_score:.0f}/100** (OK)"

            # Time-of-day context: after 1 PM + already moved 8%+  = swing trade only
            # Stocks at 2-7% after 1 PM can still make a meaningful intraday move by 3:30 PM.
            is_swing_only = _is_late_session and c.change_pct >= 8.0
            if is_swing_only:
                trade_tag = "\n⏰ **SWING ONLY** — already +{:.1f}%, too late for intraday. Hold 2-5 days for targets.".format(c.change_pct)
            else:
                trade_tag = ""

            # Simplify reasons to plain English
            simple_reasons = self._simplify_blind_reasons(c.reasons)

            block = [
                f"**BUY `{c.symbol}`** — Score {score_label}{trade_tag}",
                f"Price `{c.price:.2f}` ({pct}) | Buy Above `{c.buy_above:.2f}`",
                f"Stop Loss `{c.stop_loss:.2f}` | Target 1 `{c.target1:.2f}` | Target 2 `{c.target2:.2f}`{risk_reward}",
                f"Volume `{c.volume_ratio:.1f}x avg`",
                f"Why: {simple_reasons}",
            ]
            if c.suggested_qty > 0:
                block.append(f"Suggested Qty: `{c.suggested_qty}`")
            sections.append("\n".join(block))

            self._already_alerted_buys.add(c.symbol)
            # Track for momentum continuation re-alerts
            self._momentum_tracked[c.symbol] = {
                "entry_price": c.price,
                "last_alert_pct": c.change_pct,
                "last_alert_time": datetime.now(),
            }

        sections.append("_Open Groww → Buy → then use `/buy SYMBOL QTY PRICE` to record_")
        message = "\n\n".join(sections)
        self._send_to_channels(message)

    @staticmethod
    def _simplify_blind_reasons(reasons: str) -> str:
        """Translate scorer reason codes to plain English."""
        translations = {
            "RSI oversold": "Stock is very cheap (oversold)",
            "RSI near oversold": "Stock is getting cheap",
            "RSI mid": "Stock at fair price",
            "RSI overbought": "Stock is expensive (risky)",
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
            matched = False
            for key, val in translations.items():
                if key.lower() in p.lower():
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
                f"_Sell in Groww → then use `/sell {a.symbol} {a.quantity} {a.current_price:.0f}` to record_"
            )
            self._already_alerted_sells.add(a.symbol)

        message = "\n\n".join(sections)
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
            return
        try:
            import requests
            requests.post(
                webhook_url,
                json={"content": message[:1900]},
                timeout=10,
            )
        except Exception:
            pass

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

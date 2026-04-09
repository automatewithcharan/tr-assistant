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

    # Buy-side filters (deliberately looser than the main scanner)
    min_confidence: float = 5.0         # accept weaker signals (main uses 10)
    min_trade_value: float = 0.0        # no liquidity floor — small caps welcome
    min_volume_ratio: float = 0.0       # don't gate on volume ratio
    max_entry_gap_pct: float = 5.0      # wider trigger window (backtest shows 1.5 blocks good picks)
    blind_buy_min_score: float = 40.0   # min score to alert; intraday path uses max(34, config-6)

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
            # On strong momentum (change >= 5%), RSI overbought is EXPECTED and not a risk signal.
            # A stock up 5%+ intraday that is also "overbought" is showing momentum, not overextension.
            # Only penalise overbought RSI for weak/early-stage moves where it signals chasing.
            if change_pct < 5.0:
                score -= 8
                reasons.append(f"RSI overbought ({rsi:.0f})")
            # else: overbought on a strong-momentum day — no penalty, no mention

        # -- CCI (< -100 = oversold, > 100 = overbought) --
        # Volume-confirmed momentum override: when vol >= 2.0x, CCI/MFI
        # being "overbought" is confirmatory (big money pushing), not risky.
        # Halve penalties so genuine runners (AGIIL +15%, STEELCAS +11%)
        # aren't killed by momentum indicators doing their job.
        _high_vol_momentum = vol_ratio >= 2.0
        if cci < -100:
            score += 12
            reasons.append(f"CCI oversold ({cci:.0f})")
        elif cci < -50:
            score += 6
            reasons.append(f"CCI low ({cci:.0f})")
        elif cci > 200:
            _pen = 8 if _high_vol_momentum else 15
            score -= _pen
            reasons.append(f"CCI extremely overbought ({cci:.0f}){' [vol-halved]' if _high_vol_momentum else ''}")
        elif cci > 150:
            _pen = 4 if _high_vol_momentum else 8
            score -= _pen
            reasons.append(f"CCI overbought ({cci:.0f}){' [vol-halved]' if _high_vol_momentum else ''}")

        # -- MFI (0-100, same as RSI but volume-weighted) --
        if mfi < 20:
            score += 10
            reasons.append(f"MFI oversold ({mfi:.0f})")
        elif mfi < 40:
            score += 5
        elif mfi > 80:
            _pen = 4 if _high_vol_momentum else 8
            score -= _pen
            reasons.append(f"MFI overbought ({mfi:.0f}){' [vol-halved]' if _high_vol_momentum else ''}")

        # -- Approaching-overbought combo: MFI 70-80 AND CCI > 100 --
        # If BOTH oscillators are elevated, the stock is overextended.
        if 70 <= mfi <= 80 and cci > 100:
            _pen = 3 if _high_vol_momentum else 5
            score -= _pen
            reasons.append(f"⚠️ overbought combo MFI {mfi:.0f} + CCI {cci:.0f}{' [vol-halved]' if _high_vol_momentum else ''}")

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
        self._already_alerted_buys: Set[str] = self._load_daily_alerted_set()  # persist across restarts
        self._definitely_buy_alerted: Set[str] = set()  # track DEFINITELY BUY to allow upgrade re-alerts
        self._last_buynow_rescan: float = 0.0  # timestamp of last /buynow rescan
        self._already_alerted_sells: Set[str] = set()  # avoid repeat sell alerts
        self._last_buy_alert_time: Optional[datetime] = None  # cooldown tracking
        self._pending_followups: List["BlindBuyCandidate"] = []  # buffer for batching
        self._FOLLOWUP_COOLDOWN_SEC = 900  # 15 minutes between follow-up alerts

        # Tick-only fast scanner state (no OHLCV, fires in < 1 second)
        self._tick_alerted: Dict[str, datetime] = {}  # symbol -> time of last tick-triggered scan
        self._tick_rejected: Set[str] = set()          # symbols rejected by OHLCV confirmation (use longer cooldown)
        self._watch_alerted: Dict[str, str] = {}      # symbol -> date string of last WATCH alert (once per day per symbol)
        self._prev_tick_snapshot: Dict[str, Dict] = {}  # last tick reading for delta-volume
        self._TICK_ORDER_IMBALANCE_RATIO = 2.5  # buy_qty / sell_qty threshold
        self._TICK_VOLUME_BURST_FRACTION = 0.025  # 2.5% of day-vol in 60s
        self._TICK_HIGH_DAILY_CHANGE_PCT = 2.0   # daily change% to auto-flag (catch moves early, at 2% not 3%)
        self._TICK_ALREADY_MOVED_PCT = 16.0      # above this % skip — too late to enter intraday (was 12%, raised to catch 12-15% runners)
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
        self._DEMAND_CONFIRM_RISE_PCT = 0.9       # standard rise threshold from detection
        self._DEMAND_CONFIRM_MIN_IMBALANCE = 2.8  # minimum persistent order-book demand
        self._DEMAND_CONFIRM_MIN_VOL_RATIO = 2.0  # minimum live volume / 10-day avg volume
        self._DEMAND_CONFIRM_MAX_DAY_CHANGE = 5.5 # avoid late chase entries in this path
        self._DEMAND_CONFIRM_FAST_RISE_PCT = 0.7  # fast-track stronger setups earlier
        self._DEMAND_CONFIRM_FAST_IMBALANCE = 4.5 # fast-track needs very strong imbalance
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
        self._OPENING_FLUSH_INTERVAL_SEC = 30 # flush buffer every 30s during opening
        self._opening_flush_done_date: Optional[str] = None  # date of last complete flush

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

            # 2b. Nifty crash guard — check if index is down >2% within first 60 min
            # Only re-evaluate once per cycle during market hours
            if self._is_market_open():
                self._nifty_crash_pause = self._check_nifty_crash(signal_state)

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
            # Daily state reset — clear stale momentum tracking from previous day
            today_str = datetime.now().strftime("%Y-%m-%d")
            if self._momentum_tracked_date and self._momentum_tracked_date != today_str:
                self._log(f"New trading day {today_str}: clearing {len(self._momentum_tracked)} stale momentum entries from {self._momentum_tracked_date}")
                self._momentum_tracked.clear()
                self._tick_only_alert_count = 0
                self._nifty_crash_pause = False
            self._momentum_tracked_date = today_str
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
            # Fast tick scan (zero API calls, < 1s) — catches pre-jump conditions
            # BEFORE the full OHLCV cycle would even start.
            self._run_tick_fast_scan()
            # Flush opening buffer (ranked summary of buffered opening alerts)
            self._flush_opening_buffer()
            # Re-scan signal monitor "Buy Now" stocks — catches stocks that improved
            # since initial rejection (low score early → strong later in the day).
            self._run_buynow_rescan()
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
                # Flush opening buffer every cycle (self-throttles internally)
                self._flush_opening_buffer()
                # Check buynow rescan every ~30s within the tick loop
                _buynow_counter += _tick_poll_interval
                if _buynow_counter >= 30:
                    self._run_buynow_rescan()
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
                self._send_to_channels(msg)
                self._log(f"Pre-open auction: sent {len(picks)} picks for phase={current_phase}")
        else:
            # Only send "no signals" during buffer phase (most reliable)
            if current_phase == "buffer":
                self._send_to_channels(
                    f"**🔔 PRE-OPEN AUCTION — {now_ist.strftime('%d %b %Y %H:%M')}**\n"
                    f"No strong auction signals detected. Market may open flat.\n"
                    f"_Tick scanner active — alerts will fire if anything develops after 9:15_"
                )

        self._preopen_auction_sent_phase = current_phase

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

        # Fire between 8:45 AM and 9:14 AM IST (before market opens), once per day
        _PREMARKET_START = 8 * 60 + 45
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
        if now_ts - self._last_buynow_rescan < 60:  # every 60 seconds — match signal monitor cadence
            return
        self._last_buynow_rescan = now_ts

        try:
            import pytz as _pytz_rs
            _ist_rs = _pytz_rs.timezone("Asia/Kolkata")
            _now_ist = datetime.now(_ist_rs)
        except Exception:
            from datetime import timezone, timedelta
            _now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))

        # Only during market hours (9:30 AM - 3:15 PM)
        _h, _m = _now_ist.hour, _now_ist.minute
        if _h < 9 or (_h == 9 and _m < 30) or _h >= 15 and _m > 15:
            return

        _sm_path = os.path.join(self.output_dir, f"signalmonitor_{self.config.universe}_signals_simple_latest.csv")
        if not os.path.exists(_sm_path):
            return

        try:
            _sm_df = pd.read_csv(_sm_path)
        except Exception:
            return

        if "What To Do" not in _sm_df.columns:
            return

        # Only look at stocks the engine says "Buy Now" or "Strong Buy Now"
        buy_actions = {"Strong Buy Now", "Buy Now"}
        candidates = _sm_df[_sm_df["What To Do"].astype(str).str.strip().isin(buy_actions)]
        if candidates.empty:
            return

        # Log what the engine found vs what we've already alerted
        _new_candidates = [str(r.get("Stock", "")).strip() for _, r in candidates.iterrows()
                           if str(r.get("Stock", "")).strip() not in self._already_alerted_buys]
        if _new_candidates:
            self._log(f"BuyNow rescan: engine says Buy Now for {len(candidates)} stocks, {len(_new_candidates)} not yet alerted: {', '.join(_new_candidates[:10])}")

        _rescan_count = 0
        _max_rescan_alerts = 6  # max 6 per rescan cycle (was 3 — too restrictive)
        ticks = self._load_ticks_snapshot()

        for _, row in candidates.iterrows():
            sym = str(row.get("Stock", "")).strip()
            if not sym or sym in self._already_alerted_buys:
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
            if blind_score < 40:  # relaxed from 50 — engine already confirmed Buy Now
                continue

            reasons_lower = blind_reasons.lower()
            if "do not buy" in reasons_lower or "bearish" in reasons_lower:
                continue

            vol_ratio = float(row.get("Volume vs Avg", row.get("Volume Ratio", 0)) or 0)
            # No strict volume gate — engine already checked volume.
            # Just skip if volume is clearly dead (< 0.3x)
            if vol_ratio > 0 and vol_ratio < 0.3:
                continue

            confidence = float(row.get("Signal Strength %", 0) or 0)
            stop_loss = float(row.get("Sell If Below", 0) or 0)
            target1 = float(row.get("Take Profit 1", 0) or 0)
            target2 = float(row.get("Take Profit 2", 0) or 0)
            rsi = float(row.get("RSI", 50) or 50)

            if rsi >= 78:
                continue

            if stop_loss <= 0 or target2 <= 0 or live_price <= stop_loss:
                continue

            rr2 = (target2 - live_price) / (live_price - stop_loss)
            if rr2 < 1.0:  # relaxed from 1.5 — 1:1 is breakeven, anything above is worth it
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
                prev_close = float(tick.get("ohlc", {}).get("close", 0))
            daily_change = ((live_price - prev_close) / prev_close * 100) if prev_close > 0 else 0

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

            suggested_qty = max(1, int(4000 / live_price))
            approx_cost = suggested_qty * live_price

            _risk_ps = round(live_price - stop_loss, 2) if stop_loss > 0 else 0
            _reward_ps = round(target2 - live_price, 2) if target2 > 0 else 0
            _expected_growth = round(((target2 - live_price) / live_price) * 100, 1) if target2 > 0 else 0
            _scope_parts = []
            if scope_t1 > 0:
                _scope_parts.append(f"+{scope_t1:.1f}% to first target")
            if scope_t2 > 0:
                _scope_parts.append(f"+{scope_t2:.1f}% to max target")
            _scope_str = f" | **Profit potential: {' | '.join(_scope_parts)}**" if _scope_parts else ""
            msg = (
                f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"**🔍 MISSED EARLIER, NOW STRONG — `{sym}`** — Quality {score_label}\n"
                f"📈 **Expected to grow +{_expected_growth:.1f}% today** | Price now: `₹{live_price:.2f}` (+{daily_change:.1f}% so far){_scope_str}\n"
                f"📊 Volume: `{vol_ratio:.1f}x` above normal | `{confidence:.0f}%` indicators agree\n\n"
                f"**What to do:**\n"
                f"Buy **{suggested_qty} shares** (≈ ₹{approx_cost:,.0f})\n"
                f"🔴 EXIT if price drops to `₹{stop_loss:.2f}` (you lose ₹{_risk_ps:.0f}/share)\n"
                f"🟢 SELL at `₹{target1:.2f}` (safe profit) or `₹{target2:.2f}` (max profit — ₹{_reward_ps:.0f}/share gain)\n\n"
                f"**Why:** {self._simplify_blind_reasons(blind_reasons)}\n"
                f"_Open Groww → Buy → then use `/buy {sym} {suggested_qty} {live_price:.0f}` to record_"
            )

            self._send_to_channels(msg)
            self._already_alerted_buys.add(sym)
            self._save_daily_alerted(sym)
            self._log(
                f"🔍 RESCAN CATCH: {sym} score={blind_score:.0f} vol={vol_ratio:.1f}x "
                f"conf={confidence:.0f}% change=+{daily_change:.1f}% — was missed earlier"
            )
            _rescan_count += 1
            if _rescan_count >= _max_rescan_alerts:
                break

        if _rescan_count > 0:
            self._log(f"BuyNow rescan: sent {_rescan_count} new alerts from signal monitor")

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
            if entry_price > 0:
                profit_pct = round((current_price - entry_price) / entry_price * 100, 1)

            vol_str = f" | Vol `{vol_ratio:.1f}x`" if vol_ratio > 0 else ""
            profit_str = f" | From entry: `+{profit_pct:.1f}%`" if profit_pct > 0 else ""
            # Trailing stop suggestion: 4% below current price
            trail_stop = round(current_price * 0.96, 2)

            message = (
                f"**🚀 `{sym}` STILL RUNNING — hold and trail your stop!**\n"
                f"📈 Now `+{current_change:.1f}%` (was `+{last_alert_pct:.1f}%` when you entered){profit_str}\n"
                f"Price `₹{current_price:.2f}`{vol_str}\n\n"
                f"**What to do now:**\n"
                f"✅ DON'T sell yet — momentum is still strong\n"
                f"🔴 Move your stop-loss UP to `₹{trail_stop:.2f}` (locks in most of your profit)\n"
                f"_If price drops to ₹{trail_stop:.2f}, sell immediately to keep your gains._"
            )
            self._send_to_channels(message)
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

            self._send_to_channels(message)
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

        lines.append("_Go to Groww → Portfolio → Sell all intraday positions. Do it NOW._")
        self._send_to_channels("\n".join(lines))
        self._log("EOD exit alert sent.")

    # ------------------------------------------------------------------
    # Tick-only scoring for stocks NOT in signal state
    # ------------------------------------------------------------------

    def _handle_tick_only_alert(self, item: Dict, sym: str, price: float,
                                 daily_change: float, direction: str,
                                 reasons_text: str) -> None:
        """Score and alert a stock using ONLY tick data (no OHLCV/signal state).

        This catches big movers like ITDC (+20%), AWHCL (+14%), BHAGYANGR (+14%)
        that are in our tick subscription (2305 stocks) but NOT in the signal
        monitor's scanned set (~600 stocks).

        Available data per tick:
            price, change%, volume, avg_traded_price (VWAP), open, high, low,
            close (prev), total_buy_qty, total_sell_qty, best_bid, best_ask

        Quality gates (all must pass):
            1. Price >= ₹20 (no penny stocks)
            2. Turnover >= ₹5 Cr today (real institutional liquidity)
            3. Change >= 5% (meaningful momentum) — OR 3%+ in opening window
            4. Not upper-circuit locked (sell_qty=0 → can't buy)
            5. Price above VWAP (bullish — buying at premium to avg)
            6. Max 12 tick-only alerts per day (prevent alert flooding)
        """
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
        _min_turnover_cr = 1.0 if _is_opening else 5.0
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
        _min_score = 45 if _is_opening else 55
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

        # Suggest qty based on budget ₹4000
        _suggested_qty = max(1, int(4000 / price))
        _approx_cost = _suggested_qty * price

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
            f"Turnover `₹{turnover_cr:.0f}Cr` | {'Above' if above_vwap else 'Below'} VWAP\n\n"
            f"🔴 Stop Loss `₹{_sl_price:.0f}` | 🟡 Target 1 `₹{_t1_price:.0f}` | 🟢 Target 2 `₹{_t2_price:.0f}` | R:R `1:{_rr}`\n"
            f"Why: {score_reasons_str}\n\n"
            f"**What to do ({_conviction} conviction):**\n"
            f"Buy **{_suggested_qty} shares** (≈ ₹{_approx_cost:,.0f})\n"
            f"_Open Groww → Buy → then use `/buy {sym} {_suggested_qty} {price:.0f}` to record_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        self._send_to_channels(msg)
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

            _sl_price = round(price * 0.97, 2)  # 3% SL
            _target_price = round(price * 1.08, 2)  # 8% target
            _scope_pct = round((((_target_price / price) - 1) * 100), 1)
            _base_qty = max(1, int(4000 / price))
            _suggested_qty = max(1, int(_base_qty * _size_mult))
            _approx_cost = _suggested_qty * price
            _rr = round((_target_price - price) / max(price - _sl_price, 0.01), 1)
            _age_min = item["age_sec"] / 60

            # Plain-English grade descriptions
            _grade_desc = {"A": "Strong — highest conviction, full size", "B": "Good — moderate conviction, reduced size", "C": "Weak — low conviction, smallest size"}[_demand_grade]
            _verdict = "✅ BUY NOW" if _demand_grade in ("A", "B") else "⚠️ RISKY — only if you accept higher risk"
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
                f"_Open Groww → Buy → then use `/buy {sym} {_suggested_qty} {price:.0f}` to record_\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            self._send_to_channels(msg)
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
            last_tick_alert = self._tick_alerted.get(symbol)
            if isinstance(last_tick_alert, datetime):
                cooldown = self._TICK_REJECT_COOLDOWN_SEC if symbol in self._tick_rejected else self._TICK_ALERT_COOLDOWN_SEC
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
            _ohlcv_chg = float(row.get("ChangePct", row.get("Change %", 0)) or 0)
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
        _ohlcv_change = float(row.get("ChangePct", row.get("Change %", 0)) or 0)
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
            effective_min = max(42.0, self.config.blind_buy_min_score - 5)
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
            _now_action_for_gate = str(row.get("NowAction", row.get("Action", ""))).lower()
            _is_buy_for_gate = "buy" in _now_action_for_gate
            _high_vol_buy = _is_buy_for_gate and _vol_ratio_live >= 2.0
            if _is_broad_rally and _relative_outperformance >= 5.0:
                effective_min = max(48.0, self.config.blind_buy_min_score)
                self._log(
                    f"Broad rally mode: {sym} +{daily_change:.1f}% vs Nifty +{_nifty_chg:.1f}% "
                    f"(outperform +{_relative_outperformance:.1f}%), effective_min={effective_min:.0f}"
                )
            elif _high_vol_buy:
                effective_min = max(48.0, self.config.blind_buy_min_score)
                self._log(
                    f"High-vol Buy fast-track: {sym} vol {_vol_ratio_live:.1f}x + Buy Now "
                    f"→ effective_min={effective_min:.0f}"
                )
            else:
                effective_min = max(52.0, self.config.blind_buy_min_score)
        elif _entry_confirmed and _no_hard_blockers:
            # Price just crossed entry trigger (fresh breakout, stock hasn't run much yet).
            # Standard threshold is fine here.
            effective_min = max(40.0, self.config.blind_buy_min_score - 5)
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
            suggested_qty = int(float(row.get("SuggestedQty", row.get("Suggested Qty", 0)) or 0))
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
            _now_action = str(row.get("NowAction", row.get("Action", ""))).lower()
            _engine_not_buy = "hold" in _now_action or "sell" in _now_action or "exit" in _now_action
            _is_buy_action = "buy" in _now_action
            _rsi_val = float(row.get("RSI", 50) or 50)

            # Gate A: Engine action — require "Buy Now" or similar.
            # Override: RSI < 45 (oversold bounce — engine may say Hold but reversal
            # is the signal, e.g. PACEDIGITK RSI 39, +3.1% entry → +6.2% high).
            # Override: Opening 30-min window — signal state is from yesterday,
            # engine says "Hold" for stocks that are NOW breaking out at market open.
            # A +3%+ gap-up with volume IS the buy signal, engine just hasn't re-scanned.
            if not _is_buy_action and _rsi_val >= 45 and not is_early_mover and not _is_opening_window:
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
            if not _is_buy_action and _is_opening_window:
                self._log(
                    f"Opening window bypass: {sym} engine says '{_now_action}' but "
                    f"+{daily_change:.1f}% move overrides stale signal"
                )

            # Gate B: RSI < 80 — extremely overbought stocks stall.
            # SETCO RSI 83 → max +5% then reversed. LUXIND RSI 78 borderline.
            if _rsi_val >= 80 and not is_early_mover:
                self._log(
                    f"Suppressed {sym}: RSI {_rsi_val:.0f} >= 80 — extremely overbought, skip"
                )
                return

            # Gate C: Confidence >= 20% — engine must have minimum conviction.
            # Bypass during opening window — confidence from yesterday's scan is stale.
            if confidence < 20 and not is_early_mover and not (_rsi_val < 45) and not _is_opening_window:
                self._log(
                    f"Suppressed {sym}: confidence {confidence:.0f}% < 20% — low conviction, skip"
                )
                self._add_to_demand_watchlist(
                    sym, price, daily_change, _item_imbalance, _item_volume,
                    _item_reasons, f"low confidence {confidence:.0f}%"
                )
                return

            # Gate E: Price >= ₹50 — skip penny stocks entirely.
            # OLAELEC ₹32 — wide spreads, hard to exit, unreliable volume data.
            if _price_now < 50.0:
                self._log(f"Suppressed {sym}: price ₹{_price_now:.0f} < ₹50 — penny stock, skip")
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
            if not _has_real_targets and not is_early_mover and not _is_opening_window:
                self._log(
                    f"Suppressed {sym}: NO real targets (SL=0, T1=0, T2=0) "
                    f"+{daily_change:.1f}%, conf={confidence:.0f}%, action='{_now_action}' "
                    f"— no technical basis for entry"
                )
                return

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
            # ── Position Sizing (volatility-adjusted, score-scaled) ──────────
            # Base: risk ₹1000 per trade (1% of ₹1L budget), stop defined by ATR/SL.
            # Volatility cap: position value <= (target_vol% / asset_vol%) × portfolio.
            # Score multiplier: high-conviction signals (score>=70) get up to 1.5x size.
            # Hard cap: ₹5000 max per position (5% of ₹1L budget).
            _risk_amt = 1000        # ₹1000 base risk per trade
            _max_position = 5000    # ₹5000 hard cap per position
            _portfolio = 100000     # assumed ₹1L trading budget
            _target_vol = 0.15      # 15% target portfolio volatility contribution

            if suggested_qty <= 0 and _price_now > 0:
                # Step 1: Risk-per-share sizing (primary method)
                if stop_loss > 0 and _price_now > stop_loss:
                    risk_per_share = _price_now - stop_loss
                    _qty_risk = max(1, int(_risk_amt / risk_per_share))
                else:
                    _qty_risk = max(1, int(_risk_amt / (_price_now * 0.03)))

                # Step 2: Volatility-targeting cap (from Auto-Alpha-Forge position_sizer)
                # Estimate annualised asset vol from ATR proxy: vol ≈ (ATR / price) × sqrt(252)
                # Note: BlindBuyCandidate has no .atr field — use 2% daily price proxy
                _atr_val = _price_now * 0.02
                _asset_vol = (_atr_val / _price_now) * (252 ** 0.5)
                _asset_vol = max(_asset_vol, 0.05)  # floor at 5% annualised
                _vol_position_value = (_target_vol / _asset_vol) * _portfolio
                _qty_vol = max(1, int(min(_vol_position_value, _max_position) / _price_now))

                # Step 3: Score confidence multiplier (0.8x to 1.5x)
                # Low score (<=55): size down 20%; high score (>=70): size up 50%
                if blind_score >= 70:
                    _score_mult = 1.5
                elif blind_score >= 60:
                    _score_mult = 1.0
                else:
                    _score_mult = 0.8

                # Take the more conservative of risk-based and vol-based, then apply score
                suggested_qty = max(1, int(min(_qty_risk, _qty_vol) * _score_mult))

            # Hard cap to max position value
            if _price_now > 0:
                max_shares = max(1, int(_max_position / _price_now))
                suggested_qty = min(suggested_qty, max_shares)
            _invest_amt = round(suggested_qty * _price_now, 0) if _price_now > 0 else 0

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
            rr2 = 0.0
            if stop_loss > 0 and target2 > 0 and _price_now > 0:
                risk = abs(_price_now - stop_loss)
                rr2 = round(abs(target2 - _price_now) / risk, 1) if risk > 0 else 0
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
                # Recalculate _is_definitely_buy with fresh data
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
            message = (
                f"{alert_header}\n"
                f"📈 **Expected to grow +{_expected_growth_pct:.1f}% today** | Price now: `₹{_price_now:.2f}` ({direction}{daily_change:.2f}% so far){_scope_line}\n"
                f"📊 Volume: `{volume_ratio:.1f}x` above normal | `{confidence:.0f}%` indicators agree\n\n"
                f"**What to do:**\n"
                f"Buy {f'**{suggested_qty} shares** (≈ ₹{_invest_amt:,.0f})' if suggested_qty > 0 else 'at market price'}\n"
                f"🔴 EXIT if price drops to `₹{stop_loss:.2f}` (you lose ₹{_risk_per_share:.0f}/share)\n"
                f"🟢 SELL at `₹{target1:.2f}` (safe profit) or `₹{target2:.2f}` (max profit — ₹{_reward_per_share:.0f}/share gain){risk_reward}\n\n"
                f"**Why this stock:**\n{reasons_text}\n"
                f"{_ohlcv_reasons_text}\n\n"
                f"_Open Groww → Buy → then use `/buy {sym} {suggested_qty} {_price_now:.0f}` to record_"
            )
            # For re-alerts: only send if it's a DEFINITELY BUY upgrade
            if _is_re_alert and not _is_definitely_buy:
                self._log(f"Skip re-alert for {sym}: already alerted, not a DEFINITELY BUY upgrade")
            elif self._is_opening_window_active() and not _is_re_alert:
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
                        "message": message,
                    })
                self._log(
                    f"📦 BUFFERED (opening window): {sym} at ₹{_price_now:.2f} ({direction}{daily_change:.2f}%) "
                    f"score={blind_score:.0f} vol={volume_ratio:.1f}x — will flush in ranked batch"
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
                self._send_to_channels(message)
            # Track for momentum continuation re-alerts
            self._momentum_tracked[sym] = {
                "entry_price": _price_now,
                "last_alert_pct": daily_change,
                "last_alert_time": datetime.now(),
                "stop_loss": stop_loss,
            }
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

            # Volume gate: time-weighted minimum so early trading (9:15–10:30 AM) isn't unfairly penalised.
            # At 9:20 AM only 5 min of volume has accumulated vs a full 375-min day, so 0.3x IS good volume.
            # Formula: require vol >= (minutes_traded / 375) * 0.8 — i.e. 80% of time-proportional pace.
            if vol_ratio > 0:
                _utcn = datetime.utcnow()
                _ist_h = (_utcn.hour + 5) % 24
                _ist_m = _utcn.minute + 30
                if _ist_m >= 60:
                    _ist_h = (_ist_h + 1) % 24
                    _ist_m -= 60
                _minutes_traded = max(1, _ist_h * 60 + _ist_m - (9 * 60 + 15))
                _min_vol = max(0.15, (_minutes_traded / 375.0) * 0.8)  # floor at 0.15x always
                if vol_ratio < _min_vol:
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
    # Daily alert deduplication (persist across session restarts)
    # ------------------------------------------------------------------

    def _daily_alerted_path(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.output_dir, f"alerted_buys_{today}.txt")

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
        now_min = now_ist.hour * 60 + now_ist.minute
        return (9 * 60 + 15) <= now_min <= (9 * 60 + 30)

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

        lines.append("\n_Open Groww → Buy your top picks_")
        self._send_to_channels("\n\n".join(lines))
        self._log(f"Opening buffer flushed: {n_total} picks sent (top {self._MAX_OPENING_ALERTS}), {len(overflow)} overflow")

        # Send overflow as compact watchlist
        if overflow:
            overflow_lines = [f"**📝 WATCHLIST** — {len(overflow)} more confirmed stocks (lower conviction):"]
            for pick in overflow:
                sym = pick["symbol"]
                score = pick.get("blind_score", 0)
                price = pick.get("price", 0)
                change = pick.get("daily_change", 0)
                vol = pick.get("volume_ratio", 0)
                sl = pick.get("stop_loss", 0)
                t2 = pick.get("target2", 0)
                overflow_lines.append(
                    f"`{sym}` Score {score:.0f} | `₹{price:.2f}` (+{change:.1f}%) | "
                    f"Vol `{vol:.1f}x` | SL `₹{sl:.2f}` → T2 `₹{t2:.2f}`"
                )
                self._already_alerted_buys.add(sym)
                self._save_daily_alerted(sym)
                self._momentum_tracked[sym] = {
                    "entry_price": price,
                    "last_alert_pct": change,
                    "last_alert_time": datetime.now(),
                    "stop_loss": sl,
                }
            self._send_to_channels("\n".join(overflow_lines))

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
        # Sort buffered by score descending
        self._pending_followups.sort(key=lambda c: c.blind_score, reverse=True)
        self._do_send_buy_alert(self._pending_followups, is_followup=True)
        self._last_buy_alert_time = datetime.now()
        self._pending_followups = []

    @staticmethod
    def _grade_candidate(c: 'BlindBuyCandidate') -> Tuple[str, str]:
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
        risk_flag = ""

        # Distribution risk: lots of volume but modest move = selling into strength
        if vol_efficiency > 1.5 and c.volume_ratio >= 3.0:
            risk_flag = "⚠️ GAP-FADE RISK — high volume relative to move (possible distribution)"

        # A+ = high score WITH volume confirmation (score is primary, volume confirms)
        if c.blind_score >= 65 and c.volume_ratio >= 2.0 and c.change_pct >= 3.0:
            return "A+", risk_flag
        if c.blind_score >= 70 and c.volume_ratio >= 1.5:
            return "A+", risk_flag

        # A = good score with some backing OR efficient breakout
        if c.blind_score >= 55 and c.volume_ratio >= 1.5 and c.change_pct >= 3.0:
            return "A", risk_flag
        if c.blind_score >= 60:
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
        # Sort: A+ first, then A, then B; within tier by score desc (not raw momentum)
        _grade_order = {"A+": 0, "A": 1, "B": 2}
        graded.sort(key=lambda x: (_grade_order.get(x[1], 9), -x[0].blind_score, -x[2]))

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
                "**🟢 BUY ALERT**",
                f"Top **{len(main_picks)}** of {len(candidates)} picks ranked by conviction{grade_summary}:",
            ]

        for rank, (c, grade, momentum, risk_flag) in enumerate(main_picks, 1):
            self._format_candidate_block(sections, c, grade, momentum, rank, len(main_picks), risk_flag=risk_flag)

        sections.append("_Open Groww → Buy → then use `/buy SYMBOL QTY PRICE` to record_")
        message = "\n\n".join(sections)
        self._send_to_channels(message)

        # Send overflow as a separate watchlist message
        if overflow_picks:
            overflow_sections = [
                f"**📝 WATCHLIST** — {len(overflow_picks)} more picks (lower conviction):",
            ]
            for rank, (c, grade, momentum, risk_flag) in enumerate(overflow_picks, _MAX_MAIN + 1):
                self._format_candidate_block(overflow_sections, c, grade, momentum, rank, len(candidates), compact=True, risk_flag=risk_flag)
            overflow_msg = "\n\n".join(overflow_sections)
            self._send_to_channels(overflow_msg)

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

        # Simplify reasons to plain English
        simple_reasons = self._simplify_blind_reasons(c.reasons)

        if compact:
            # Compact format for watchlist overflow
            _risk = " ⚠️FADE" if risk_flag else ""
            block = [
                f"`{c.symbol}` [{grade_badge}] Score {c.blind_score:.0f} | `{c.price:.2f}` ({pct}) | Vol `{c.volume_ratio:.1f}x` | SL `{c.stop_loss:.2f}` → T1 `{c.target1:.2f}` T2 `{c.target2:.2f}`{_risk}",
            ]
        else:
            block = [
                f"**BUY `{c.symbol}`** [{grade_badge}] Score {score_label}{top_marker}{trade_tag}",
                f"Price `{c.price:.2f}` ({pct}) | Buy Above `{c.buy_above:.2f}` | Volume `{c.volume_ratio:.1f}x avg`{f' | {mom_label}' if mom_label else ''}",
                f"Stop Loss `{c.stop_loss:.2f}` | Target 1 `{c.target1:.2f}` | Target 2 `{c.target2:.2f}`{risk_reward}",
                f"Why: {simple_reasons}",
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
        # Track for momentum continuation re-alerts
        self._momentum_tracked[c.symbol] = {
            "entry_price": c.price,
            "last_alert_pct": c.change_pct,
            "last_alert_time": datetime.now(),
            "stop_loss": c.stop_loss,
        }

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

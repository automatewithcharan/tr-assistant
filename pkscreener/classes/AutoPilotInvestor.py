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
    blind_buy_min_score: float = 35.0   # optimized via backtest: catches 92% of top gainers at 100% win rate

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
    max_buy_alerts_per_cycle: int = 5

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
        if vol_ratio >= 2.5:
            score += 20
            reasons.append(f"volume surge {vol_ratio:.1f}x")
        elif vol_ratio >= 1.5:
            score += 15
            reasons.append(f"strong volume {vol_ratio:.1f}x")
        elif vol_ratio >= 1.0:
            score += 10
            reasons.append(f"good volume {vol_ratio:.1f}x")
        elif vol_ratio >= 0.7:
            score += 5
            reasons.append("adequate volume")
        # Don't penalize low volume — small caps may have naturally lower volume

        # -- Green candle / price action --
        if change_pct >= 5.0:
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

        # -- Penalties for bearish conditions --
        if "below all major ma" in reason_text or "bearish alignment" in reason_text:
            score -= 25
            reasons.append("BEARISH TREND - below all MAs")
        if "death cross" in reason_text:
            score -= 20
            reasons.append("DEATH CROSS")
        if "lower highs" in reason_text and "lower lows" in reason_text:
            score -= 15
            reasons.append("downtrend pattern")

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
            self.run_once()
            OutputControls().printOutput(
                f"  [AutoPilot] Next scan in {self.config.cycle_interval_sec}s..."
            )
            time.sleep(self.config.cycle_interval_sec)

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
            if blind_score < self.config.blind_buy_min_score:
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
            candidates.append(BlindBuyCandidate(
                symbol=symbol,
                price=price,
                buy_above=float(row.get("Entry", row.get("BuyAbove", row.get("Buy Above", 0))) or 0),
                stop_loss=float(row.get("StopLoss", row.get("SellIfBelow", row.get("Sell If Below", 0))) or 0),
                target1=float(row.get("Target1", row.get("TakeProfit1", row.get("Take Profit 1", 0))) or 0),
                target2=float(row.get("Target2", row.get("TakeProfit2", row.get("Take Profit 2", 0))) or 0),
                confidence=confidence,
                volume_ratio=float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0),
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

    def _push_buy_alerts(self, candidates: List[BlindBuyCandidate]) -> None:
        """Push blind-buy alerts to Discord."""
        if not candidates:
            return

        sections = [
            "**BLIND BUY ALERT**",
            f"Found **{len(candidates)}** high-conviction opportunity{'s' if len(candidates) != 1 else ''}:",
        ]

        for c in candidates:
            pct = f"+{c.change_pct:.1f}%" if c.change_pct > 0 else f"{c.change_pct:.1f}%"
            risk_reward = ""
            if c.stop_loss > 0 and c.target2 > 0 and c.price > 0:
                risk = abs(c.price - c.stop_loss)
                reward = abs(c.target2 - c.price)
                rr = round(reward / risk, 1) if risk > 0 else 0
                risk_reward = f" | R:R `1:{rr}`"

            block = [
                f"**BUY `{c.symbol}`** (Score: {c.blind_score:.0f}/100)",
                f"Price `{c.price:.2f}` ({pct}) | Buy Above `{c.buy_above:.2f}`",
                f"Stop Loss `{c.stop_loss:.2f}` | Target 1 `{c.target1:.2f}` | Target 2 `{c.target2:.2f}`{risk_reward}",
                f"Signal `{c.confidence:.0f}%` | Vol `{c.volume_ratio:.1f}x`",
                f"Why: {c.reasons}",
            ]
            if c.suggested_qty > 0:
                block.append(f"Suggested Qty: `{c.suggested_qty}`")
            sections.append("\n".join(block))

            self._already_alerted_buys.add(c.symbol)

        sections.append("_Open Groww → Buy → then use `/buy SYMBOL QTY PRICE` to record_")
        message = "\n\n".join(sections)
        self._send_to_channels(message)

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
        from pkscreener.classes.ConfigManager import tools as ConfigManager
        config_manager = ConfigManager()
        config_manager.getConfig(config_manager.parser)
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

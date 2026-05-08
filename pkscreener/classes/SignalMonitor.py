"""
Continuous monitor-only signal scanning for local PKScreener usage.

This mode continuously scans a universe, saves plain-English signal outputs,
tracks the last seen decision per symbol, and sends alerts when a symbol
freshly changes into Buy Now or Sell Now, with a small cooldown-based re-alert
for active buy candidates so valid setups do not go silent.
"""

import html
import json
import os
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import requests

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger

from pkscreener.classes.ActionableReport import ActionableReportRunner
from pkscreener.classes.AnomalyDetector import VolumeAnomalyDetector
from pkscreener.classes.PaperTradeTracker import PaperTradeTracker
from pkscreener.classes.RiskGuardrails import LiveTradingGuardrails
from pkscreener.classes.SignalPerformanceTracker import SignalPerformanceTracker


class SignalMonitorRunner:
    """Continuously monitor a universe and alert on fresh Buy Now / Sell Now changes."""

    FULL_REFRESH_EVERY_CYCLES = int(os.environ.get("SIGNALMONITOR_FULL_REFRESH_EVERY_CYCLES", "200") or 200)
    MEANINGFUL_PRICE_MOVE_PCT = 0.5   # Tick-to-tick price move threshold (lowered for faster pick-up)
    TRIGGER_PROXIMITY_PCT = 1.0
    STREAM_BOOTSTRAP_MAX_SYMBOLS = int(os.environ.get("SIGNALMONITOR_STREAM_BOOTSTRAP_MAX_SYMBOLS", "75") or 75)
    MAX_CHANGED_SYMBOLS_PER_CYCLE = int(os.environ.get("SIGNALMONITOR_MAX_CHANGED_PER_CYCLE", "75") or 75)
    HIGH_DAILY_CHANGE_PCT = 2.0        # Force full OHLCV rescan when live daily change exceeds this
    ORDER_BOOK_IMBALANCE_RATIO = 2.5   # Pre-jump: buyers/sellers ratio threshold
    VOLUME_BURST_FRACTION = 0.025      # Pre-jump: 2.5% of accumulated day vol traded in last 60s
    MAX_BUY_DAILY_CHANGE_PCT = 10.0   # Alert up to 10% moves; tag 8%+ as SWING ONLY
    MIN_MOMENTUM_EXTENSION_PCT = 3.0  # Minimum daily gain to qualify as a momentum extension alert (3% → 20% chaser)
    REPEAT_BUY_ALERT_SECONDS = int(os.environ.get("SIGNALMONITOR_REPEAT_BUY_ALERT_SECONDS", "60") or 60)
    FAST_MODE = str(os.environ.get("SIGNALMONITOR_FAST_MODE", "0") or "0").strip().lower() in {"1", "true", "yes", "y", "on"}
    MARKET_OPEN_MINUTES_IST = 9 * 60 + 15
    MARKET_CLOSE_MINUTES_IST = 15 * 60 + 30

    SUMMARY_COLUMNS = [
        "Universe",
        "ScannedSymbols",
        "StrongBuyNowCount",
        "BuyNowCount",
        "MomentumBuyCount",
        "SellNowCount",
        "WatchForBuyCount",
        "WatchForSellCount",
        "HoldCount",
        "FreshAlertsThisRun",
        "CycleScannedSymbols",
        "CycleSeconds",
        "SymbolsPerSecond",
        "CycleLagWarning",
        "LastRunAt",
    ]

    STATE_COLUMNS = [
        "Symbol",
        "NowAction",
        "PriceNow",
        "PrevClose",
        "BuyAbove",
        "SellIfBelow",
        "TakeProfit1",
        "TakeProfit2",
        "SuggestedQty",
        "SignalStrengthPct",
        "VolumeRatio",
        "ChangePct",
        "EntryGapPct",
        "RSI",
        "CCI",
        "MFI",
        "Checks",
        "Why",
        "CheckedAt",
        "Week52High",
    ]

    ALERT_COLUMNS = [
        "When",
        "Symbol",
        "PreviousAction",
        "NowAction",
        "PriceNow",
        "ChangePct",
        "EntryGapPct",
        "RSI",
        "BuyAbove",
        "SellIfBelow",
        "TakeProfit1",
        "TakeProfit2",
        "SuggestedQty",
        "SignalStrengthPct",
        "Checks",
        "Why",
    ]

    def __init__(
        self,
        config_manager,
        universe: str = "nifty50",
        limit: int = 50,
        min_confidence: float = 10.0,
        budget_amount: Optional[float] = None,
        risk_percent: float = 1.0,
        min_trade_value: float = 10000000.0,
        min_volume_ratio: float = 1.0,
        max_workers: int = 3,
        use_live_kite: bool = False,
        alert_channels: str = "none",
        reset_state: bool = False,
        strong_buy_only: bool = False,
        streaming_mode: bool = False,
        guardrail_account_name: str = "main",
        guardrail_max_daily_loss_amount: float = 0.0,
        guardrail_max_consecutive_losses: int = 0,
    ):
        self.config_manager = config_manager
        self.universe = universe
        self.limit = limit
        self.min_confidence = float(min_confidence)
        self.budget_amount = float(budget_amount) if budget_amount not in [None, ""] else None
        self.risk_percent = float(risk_percent)
        self.min_trade_value = float(min_trade_value)
        self.min_volume_ratio = float(min_volume_ratio)
        self.max_workers = max(1, int(max_workers or 1))
        self.use_live_kite = bool(use_live_kite)
        self.alert_channels = self._normalize_alert_channels(alert_channels)
        self.reset_state = bool(reset_state)
        self.strong_buy_only = bool(strong_buy_only)
        self.streaming_mode = bool(streaming_mode) or bool(use_live_kite)
        self.guardrail_account_name = str(guardrail_account_name or "main").strip().lower().replace(" ", "_")
        self.guardrail_max_daily_loss_amount = max(0.0, float(guardrail_max_daily_loss_amount or 0.0))
        self.guardrail_max_consecutive_losses = max(0, int(guardrail_max_consecutive_losses or 0))
        self.output_dir = Archiver.get_user_data_dir()
        self.run_at = PKDateUtilities.currentDateTime()
        self.run_at_str = self.run_at.strftime("%Y-%m-%d %H:%M:%S")
        self.timestamp = self.run_at.strftime("%Y%m%d_%H%M%S")
        self._previous_live_ticks_by_symbol: Dict[str, Dict] = {}
        self._cycle_number = 0
        self._preload_done = False
        self.fast_mode = bool(self.FAST_MODE)
        self.enable_dashboard = not self.fast_mode and str(os.environ.get("SIGNALMONITOR_ENABLE_DASHBOARD", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.enable_anomaly_detection = str(os.environ.get("SIGNALMONITOR_ENABLE_ANOMALY", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.enable_followthrough = not self.fast_mode and str(os.environ.get("SIGNALMONITOR_ENABLE_FOLLOWTHROUGH", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.enable_missed_opportunity = not self.fast_mode and str(os.environ.get("SIGNALMONITOR_ENABLE_MISSED_OPPORTUNITY", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.enable_scorecard = not self.fast_mode and str(os.environ.get("SIGNALMONITOR_ENABLE_SCORECARD", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.enable_paper_tracking = not self.fast_mode and str(os.environ.get("SIGNALMONITOR_ENABLE_PAPER_TRACKING", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.save_full_outputs = not self.fast_mode and str(os.environ.get("SIGNALMONITOR_SAVE_FULL_OUTPUTS", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.actionable_runner = ActionableReportRunner(
            config_manager=config_manager,
            universe=universe,
            limit=limit,
            min_confidence=min_confidence,
            budget_amount=budget_amount,
            risk_percent=risk_percent,
            min_trade_value=min_trade_value,
            min_volume_ratio=min_volume_ratio,
            max_workers=max_workers,
            use_live_kite=use_live_kite,
            strong_buy_only=strong_buy_only,
        )
        self.performance_tracker = SignalPerformanceTracker(
            config_manager=config_manager,
            universe=universe,
            use_live_kite=use_live_kite,
        ) if self.enable_scorecard else None
        self.last_guardrail_status = None
        self._anomaly_detector: Optional[VolumeAnomalyDetector] = None
        self._anomaly_profiles_loaded = False
        self._week52_high_map: dict = {}
        self._market_context = None  # Lazy-init MarketContextEngine
        self._last_partial_save_ts = 0.0
        self._paper_tracker = PaperTradeTracker()
        self._paper_closed_date: Optional[str] = None
        # Wall-clock tracking: first time we alert a symbol today → skip repeat caps.
        # Keyed by symbol, value is "YYYY-MM-DD" of last alert day.
        self._daily_alerted: Dict[str, str] = {}

    def run(
        self,
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """Run one monitor cycle and persist the current outputs."""
        cycle_started = time.time()
        self.run_at = PKDateUtilities.currentDateTime()
        self.run_at_str = self.run_at.strftime("%Y-%m-%d %H:%M:%S")
        self.timestamp = self.run_at.strftime("%Y%m%d_%H%M%S")
        symbols = self.actionable_runner.get_universe_symbols()
        if self.limit and self.limit > 0:
            symbols = symbols[: self.limit]

        total = len(symbols)
        if total == 0:
            OutputControls().printOutput(
                "  [+] No symbols found for monitor mode."
            )
            return None, None, None, None

        if self.streaming_mode and self.use_live_kite and not self._preload_done:
            loaded, missing = self.actionable_runner.preload_symbols(
                symbols,
                cache_only=True,
                show_progress=True,
            )
            self._preload_done = True
            OutputControls().printOutput(
                f"  [+] Live memory preload complete for {loaded}/{total} symbols"
                + (f" ({missing} missing from cache)." if missing else ".")
            )

        previous_state_df = self._load_previous_state()
        previous_simple_df = self._load_previous_simple()
        cycle_symbols = self._select_symbols_for_cycle(symbols, previous_simple_df)
        cycle_total = len(cycle_symbols)
        if cycle_total == 0:
            OutputControls().printOutput(
                f"  [+] No changed symbols detected for {self.universe}. Reusing the last live board."
            )
            summary_df = self._build_summary_from_simple(previous_simple_df, total, 0)
            summary_df = self._annotate_summary_timing(summary_df, cycle_started, 0)
            _, scorecard_df = self.performance_tracker.register_and_update()
            self._save_outputs(
                pd.DataFrame(),
                pd.DataFrame(),
                previous_simple_df,
                previous_state_df,
                pd.DataFrame(columns=self.ALERT_COLUMNS),
                summary_df,
            )
            self._print_summary(summary_df, previous_simple_df, pd.DataFrame(columns=self.ALERT_COLUMNS), scorecard_df)
            self._close_paper_trades_if_eod()
            self._cycle_number += 1
            return pd.DataFrame(), previous_simple_df, pd.DataFrame(columns=self.ALERT_COLUMNS), summary_df

        label = (
            f"  [+] Monitoring {cycle_total}/{total} changed symbols from {self.universe} with {self.max_workers} workers"
            if cycle_total != total
            else f"  [+] Monitoring {total} symbols from {self.universe} with {self.max_workers} workers"
        )
        OutputControls().printOutput(
            label + (" using official Kite live ticks..." if self.use_live_kite else "...")
        )

        report_df, actionable_df = self.actionable_runner.build_report(
            symbols=cycle_symbols,
            show_progress=True,
            progress_label="Checked",
            progress_callback=lambda rows, completed, total_rows: self._save_partial_outputs(
                rows,
                completed,
                total_rows,
                previous_simple_df,
                previous_state_df,
                total,
            ),
        )
        if report_df is None:
            report_df = pd.DataFrame()
        if actionable_df is None:
            actionable_df = pd.DataFrame()

        simple_df = self.actionable_runner.build_simple_view(report_df)
        state_df = self._build_state(simple_df)
        merged_simple_df, merged_state_df = self._merge_with_previous(
            previous_simple_df,
            previous_state_df,
            simple_df,
            state_df,
        )
        merged_simple_df, merged_state_df = self._refresh_live_prices_from_ticks(
            merged_simple_df,
            merged_state_df,
        )
        merged_simple_df, merged_state_df = self._apply_buy_guardrails(
            merged_simple_df,
            merged_state_df,
        )
        alerts_df = self._build_alerts(previous_state_df, merged_state_df)
        momentum_alerts_df = self._build_momentum_alerts(previous_simple_df, merged_simple_df)
        if not momentum_alerts_df.empty:
            # Drop Momentum Buy rows for symbols already emitted as Buy Now this cycle
            already_alerted = set(alerts_df["Symbol"].str.upper()) if not alerts_df.empty else set()
            momentum_alerts_df = momentum_alerts_df[
                ~momentum_alerts_df["Symbol"].str.upper().isin(already_alerted)
            ]
            if not momentum_alerts_df.empty:
                alerts_df = pd.concat([alerts_df, momentum_alerts_df], ignore_index=True)
        anomaly_alerts_df = self._run_anomaly_detection()
        if not anomaly_alerts_df.empty:
            alerts_df = pd.concat([alerts_df, anomaly_alerts_df], ignore_index=True)
        # V2: Follow-through check + exit monitoring
        self._check_followthrough_and_exits()
        # V2: End-of-day missed opportunity analysis
        self._run_missed_opportunity_analysis()
        self._paper_track_alerts(alerts_df)
        self._close_paper_trades_if_eod()
        summary_df = self._build_summary_from_simple(merged_simple_df, total, len(alerts_df))
        summary_df = self._annotate_summary_timing(summary_df, cycle_started, cycle_total)
        scorecard_df = None
        if self.performance_tracker is not None:
            _, scorecard_df = self.performance_tracker.register_and_update(alerts_df)

        self._save_outputs(report_df, actionable_df, merged_simple_df, merged_state_df, alerts_df, summary_df)
        self._print_summary(summary_df, merged_simple_df, alerts_df, scorecard_df)
        self._send_alerts(alerts_df, summary_df)
        self._cycle_number += 1
        return report_df, merged_simple_df, alerts_df, summary_df

    def _save_partial_outputs(
        self,
        rows: List[Dict],
        completed: int,
        cycle_total: int,
        previous_simple_df: pd.DataFrame,
        previous_state_df: pd.DataFrame,
        universe_total: int,
    ) -> None:
        """Publish fresh latest boards during long live scans.

        The final cycle still writes dated files and alerts. Partial writes only
        keep the latest CSVs fresh so autopilot does not trade from stale state.
        """
        now_ts = time.time()
        if completed < cycle_total and now_ts - self._last_partial_save_ts < 45:
            return
        if not rows:
            return

        try:
            report_df, actionable_df = self.actionable_runner._build_from_rows(list(rows))
            if report_df is None:
                return
            if actionable_df is None:
                actionable_df = pd.DataFrame()

            simple_df = self.actionable_runner.build_simple_view(report_df)
            state_df = self._build_state(simple_df)
            merged_simple_df, merged_state_df = self._merge_with_previous(
                previous_simple_df,
                previous_state_df,
                simple_df,
                state_df,
            )
            merged_simple_df, merged_state_df = self._refresh_live_prices_from_ticks(
                merged_simple_df,
                merged_state_df,
            )
            merged_simple_df, merged_state_df = self._apply_buy_guardrails(
                merged_simple_df,
                merged_state_df,
            )
            summary_df = self._build_summary_from_simple(
                merged_simple_df,
                universe_total,
                0,
            )
            self._save_latest_outputs_only(
                report_df,
                actionable_df,
                merged_simple_df,
                merged_state_df,
                pd.DataFrame(columns=self.ALERT_COLUMNS),
                summary_df,
            )
            self._last_partial_save_ts = now_ts
            OutputControls().printOutput(
                f"  [+] Published partial monitor board: {completed}/{cycle_total} checked...",
                end="\r",
            )
        except Exception as exc:
            default_logger().debug(f"Partial monitor board save failed: {exc}", exc_info=True)

    def _select_symbols_for_cycle(self, symbols: List[str], previous_simple_df: pd.DataFrame) -> List[str]:
        """Return either the full universe or only changed live-tick symbols for this cycle."""
        if not self.use_live_kite:
            return symbols

        self.actionable_runner._refresh_live_ticks()
        current_ticks = self.actionable_runner.live_ticks or {}
        if not current_ticks:
            return symbols
        current_tick_snapshot = {
            str(symbol).strip().upper(): tick
            for symbol, tick in current_ticks.items()
        }
        if self._cycle_number <= 0:
            if self.streaming_mode:
                bootstrap_symbols = self._bootstrap_symbols_for_first_cycle(
                    symbols,
                    current_ticks,
                    previous_simple_df,
                )
                if bootstrap_symbols:
                    self._previous_live_ticks_by_symbol = current_tick_snapshot
                    return bootstrap_symbols
            self._previous_live_ticks_by_symbol = current_tick_snapshot
            return self._sort_symbols_by_change(symbols, current_ticks)[: self.STREAM_BOOTSTRAP_MAX_SYMBOLS]

        previous_ticks = self._previous_live_ticks_by_symbol
        self._previous_live_ticks_by_symbol = current_tick_snapshot
        if not previous_ticks:
            return self._sort_symbols_by_change(symbols, current_ticks)[: self.STREAM_BOOTSTRAP_MAX_SYMBOLS]
        if self._cycle_number % self.FULL_REFRESH_EVERY_CYCLES == 0:
            return self._sort_symbols_by_change(symbols, current_ticks)[: self.MAX_CHANGED_SYMBOLS_PER_CYCLE]

        previous_simple_map = self._simple_row_map(previous_simple_df)
        changed_symbols: List[Tuple[float, str]] = []
        for symbol in symbols:
            normalized_symbol = str(symbol).strip().upper()
            current_tick = current_ticks.get(normalized_symbol)
            if not current_tick:
                continue
            previous_tick = previous_ticks.get(normalized_symbol)
            if previous_tick is None:
                # New symbol — prioritize by current daily change% so big movers
                # aren't stuck behind alphabetically-first stocks on first encounter.
                try:
                    close = float(current_tick.get("close", 0) or 0)
                    ltp = float(current_tick.get("last_price", 0) or 0)
                    init_priority = abs((ltp - close) / close * 100.0) if close > 0 else 0.0
                except Exception:
                    init_priority = 0.0
                changed_symbols.append((init_priority, normalized_symbol))
                continue
            current_marker = (
                current_tick.get("last_price"),
                current_tick.get("volume_traded"),
                current_tick.get("last_trade_time"),
                current_tick.get("exchange_timestamp"),
                current_tick.get("received_at"),
            )
            previous_marker = (
                previous_tick.get("last_price"),
                previous_tick.get("volume_traded"),
                previous_tick.get("last_trade_time"),
                previous_tick.get("exchange_timestamp"),
                previous_tick.get("received_at"),
            )
            if current_marker != previous_marker and self._symbol_needs_refresh(
                normalized_symbol,
                previous_tick,
                current_tick,
                previous_simple_map.get(normalized_symbol),
            ):
                priority = self._symbol_priority(current_tick, previous_tick)
                changed_symbols.append((priority, normalized_symbol))

        # Process highest-priority symbols first so that if the cycle is long,
        # the most impactful stocks (big movers / strong order-book imbalance) are done first.
        changed_symbols.sort(key=lambda item: -item[0])
        return [sym for _, sym in changed_symbols[: self.MAX_CHANGED_SYMBOLS_PER_CYCLE]]

    def _bootstrap_symbols_for_first_cycle(
        self,
        symbols: List[str],
        current_ticks: Dict[str, Dict],
        previous_simple_df: pd.DataFrame,
    ) -> List[str]:
        """Use the previous board to narrow the first live cycle in streaming mode."""
        previous_map = self._simple_row_map(previous_simple_df)
        if not previous_map:
            return []

        prioritized: List[Tuple[float, str]] = []
        selected: Set[str] = set()
        symbol_set = {str(symbol).strip().upper() for symbol in symbols}

        for symbol, row in previous_map.items():
            if symbol not in symbol_set:
                continue
            tick = current_ticks.get(symbol)
            if not tick:
                continue

            action = str(row.get("What To Do", "")).strip()
            strength = self._to_float(row.get("Signal Strength %"))
            price = self._to_float(tick.get("last_price"))
            if price <= 0:
                continue

            score = strength
            if action in ["Strong Buy Now", "Buy Now", "Sell / Exit"]:
                score += 1000.0
            elif action in ["Watch for Buy", "Watch for Sell"]:
                score += 100.0

            if self._near_level(price, self._to_float(row.get("Buy Above"))):
                score += 50.0
            if self._near_level(price, self._to_float(row.get("Sell If Below"))):
                score += 50.0
            if self._near_level(price, self._to_float(row.get("Take Profit 1"))):
                score += 25.0
            if self._near_level(price, self._to_float(row.get("Take Profit 2"))):
                score += 25.0

            prioritized.append((score, symbol))

        if not prioritized:
            return []

        prioritized.sort(key=lambda item: (-item[0], item[1]))
        for _, symbol in prioritized[: self.STREAM_BOOTSTRAP_MAX_SYMBOLS]:
            selected.add(symbol)

        return [symbol for symbol in symbols if str(symbol).strip().upper() in selected]

    def _simple_row_map(self, simple_df: pd.DataFrame) -> Dict[str, Dict]:
        """Build a fast lookup map from the previous simple board."""
        if simple_df is None or simple_df.empty or "Stock" not in simple_df.columns:
            return {}
        prepared = simple_df.copy()
        prepared["Stock"] = prepared["Stock"].astype(str).str.strip().str.upper()
        prepared_records = prepared.to_dict(orient="records")
        return {
            str(rec["Stock"]).strip().upper(): rec
            for rec in prepared_records
        }

    def _symbol_needs_refresh(
        self,
        symbol: str,
        previous_tick: Dict,
        current_tick: Dict,
        previous_simple_row: Optional[Dict],
    ) -> bool:
        """Decide whether a changed symbol needs a full signal recomputation."""
        current_price = self._to_float(current_tick.get("last_price"))
        previous_price = self._to_float(previous_tick.get("last_price"))
        if current_price <= 0:
            return False

        # --- PRE-JUMP SIGNAL 1: Order book imbalance ---
        # total_buy_quantity >> total_sell_quantity means buyers are overwhelming sellers
        # BEFORE price has moved — strong indicator of an imminent spike.
        total_buy = self._to_float(current_tick.get("total_buy_quantity"))
        total_sell = self._to_float(current_tick.get("total_sell_quantity"))
        if total_sell > 0 and total_buy / total_sell >= self.ORDER_BOOK_IMBALANCE_RATIO:
            return True
        # Inverse: heavy sell-side imbalance = potential dump
        if total_buy > 0 and total_sell / total_buy >= self.ORDER_BOOK_IMBALANCE_RATIO:
            return True

        # --- PRE-JUMP SIGNAL 2: Volume burst ---
        # A large chunk of the day's accumulated volume traded in the last 60s
        # means institutional/large-hand accumulation even if price hasn't moved yet.
        current_vol = self._to_float(current_tick.get("volume_traded"))
        previous_vol = self._to_float(previous_tick.get("volume_traded"))
        if previous_vol > 0 and current_vol > previous_vol:
            delta_vol_fraction = (current_vol - previous_vol) / previous_vol
            if delta_vol_fraction >= self.VOLUME_BURST_FRACTION:
                return True

        # --- SIGNAL 3: Large live daily move (gap-up/gap-down) ---
        # tick["change"] is the official Kite daily change% from previous session close
        live_daily_change = abs(self._to_float(current_tick.get("change")))
        if live_daily_change >= self.HIGH_DAILY_CHANGE_PCT:
            return True

        if previous_price <= 0:
            return True

        price_move_pct = abs(current_price - previous_price) * 100.0 / previous_price
        if price_move_pct >= self.MEANINGFUL_PRICE_MOVE_PCT:
            return True

        if not previous_simple_row:
            return False

        previous_action = str(previous_simple_row.get("What To Do", "")).strip()
        if previous_action in ["Strong Buy Now", "Buy Now", "Sell / Exit"]:
            return True

        buy_above = self._to_float(previous_simple_row.get("Buy Above"))
        sell_below = self._to_float(previous_simple_row.get("Sell If Below"))
        target_1 = self._to_float(previous_simple_row.get("Take Profit 1"))
        target_2 = self._to_float(previous_simple_row.get("Take Profit 2"))

        if self._crossed_level(previous_price, current_price, buy_above):
            return True
        if self._crossed_level(previous_price, current_price, sell_below):
            return True
        if self._crossed_level(previous_price, current_price, target_1):
            return True
        if self._crossed_level(previous_price, current_price, target_2):
            return True

        if previous_action in ["Watch for Buy", "Watch for Sell"]:
            if self._near_level(current_price, buy_above):
                return True
            if self._near_level(current_price, sell_below):
                return True
            if self._near_level(current_price, target_1):
                return True
            if self._near_level(current_price, target_2):
                return True

        return False

    def _sort_symbols_by_change(self, symbols: List[str], current_ticks: Dict) -> List[str]:
        """Return symbols sorted by abs daily change% descending so big movers are seen first."""
        scored = []
        for sym in symbols:
            normalized = str(sym).strip().upper()
            tick = current_ticks.get(normalized)
            if tick:
                try:
                    close = float(tick.get("close", 0) or 0)
                    ltp = float(tick.get("last_price", 0) or 0)
                    pct = abs((ltp - close) / close * 100.0) if close > 0 else 0.0
                except Exception:
                    pct = 0.0
            else:
                pct = 0.0
            scored.append((-pct, normalized))
        scored.sort()
        return [sym for _, sym in scored]

    def _symbol_priority(self, current_tick: Dict, previous_tick: Dict) -> float:
        """Compute a sortable priority score for a flagged symbol.

        Higher = process first in the cycle.  Components:
          - abs daily change% (big movers go first)
          - order book imbalance (strong buy pressure = early)
          - volume burst in last 60s
        """
        score = 0.0
        # Daily move component
        daily_change = abs(self._to_float(current_tick.get("change")))
        score += daily_change * 10.0

        # Order book imbalance component
        total_buy = self._to_float(current_tick.get("total_buy_quantity"))
        total_sell = self._to_float(current_tick.get("total_sell_quantity"))
        denom = max(total_sell, total_buy, 1.0)
        imbalance = abs(total_buy - total_sell) / denom  # 0..1
        score += imbalance * 50.0

        # Volume burst component
        current_vol = self._to_float(current_tick.get("volume_traded"))
        previous_vol = self._to_float(previous_tick.get("volume_traded")) if previous_tick else 0.0
        if previous_vol > 0 and current_vol > previous_vol:
            burst = (current_vol - previous_vol) / previous_vol
            score += burst * 100.0

        return score

    def _crossed_level(self, previous_price: float, current_price: float, level: float) -> bool:
        """Return True when price crossed a relevant threshold between ticks."""
        if level <= 0 or previous_price <= 0 or current_price <= 0:
            return False
        return (
            (previous_price < level <= current_price)
            or (previous_price > level >= current_price)
        )

    def _near_level(self, price: float, level: float) -> bool:
        """Return True when price is close enough to a threshold to matter."""
        if price <= 0 or level <= 0:
            return False
        return abs(price - level) * 100.0 / level <= self.TRIGGER_PROXIMITY_PCT

    def _to_float(self, value) -> float:
        """Best-effort numeric conversion helper."""
        try:
            if value in [None, "", "None"]:
                return 0.0
            return float(value)
        except Exception:
            return 0.0

    def _load_previous_simple(self) -> pd.DataFrame:
        """Load the last simple board so changed-only cycles can reuse unchanged rows."""
        path = self._latest_path("signals_simple")
        if not os.path.exists(path):
            return pd.DataFrame()
        try:
            return pd.read_csv(path)
        except Exception as exc:
            default_logger().debug(f"Failed loading monitor simple board: {exc}", exc_info=True)
            return pd.DataFrame()

    def _merge_with_previous(
        self,
        previous_simple_df: pd.DataFrame,
        previous_state_df: pd.DataFrame,
        current_simple_df: pd.DataFrame,
        current_state_df: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Merge changed rows into the previous board/state so unchanged symbols persist."""
        merged_simple_df = self._merge_df_by_symbol(previous_simple_df, current_simple_df, "Stock")
        merged_state_df = self._merge_df_by_symbol(previous_state_df, current_state_df, "Symbol")
        return merged_simple_df, merged_state_df

    def _refresh_live_prices_from_ticks(
        self,
        simple_df: pd.DataFrame,
        state_df: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Refresh visible prices from the latest live ticks without forcing a full recompute."""
        live_ticks = self.actionable_runner.live_ticks or {}
        if not live_ticks:
            return simple_df, state_df

        refreshed_simple_df = simple_df.copy() if simple_df is not None else pd.DataFrame()
        refreshed_state_df = state_df.copy() if state_df is not None else pd.DataFrame()

        if not refreshed_simple_df.empty and "Stock" in refreshed_simple_df.columns and "Price Now" in refreshed_simple_df.columns:
            stock_series = refreshed_simple_df["Stock"].astype(str).str.strip().str.upper()
            for index, symbol in stock_series.items():
                tick = live_ticks.get(symbol)
                if not tick:
                    continue
                live_price = self._to_float(tick.get("last_price"))
                if live_price <= 0:
                    continue
                refreshed_simple_df.loc[index, "Price Now"] = round(live_price, 2)
                buy_above = self._to_float(refreshed_simple_df.loc[index].get("Buy Above"))
                if buy_above > 0 and "Entry Gap %" in refreshed_simple_df.columns:
                    entry_gap_pct = round(((buy_above - live_price) * 100.0) / buy_above, 2)
                    refreshed_simple_df.loc[index, "Entry Gap %"] = entry_gap_pct
                # Use tick's 'close' (Kite prev-close) to fill Change % on NaN rows
                if "Change %" in refreshed_simple_df.columns:
                    tick_prev_close_s = self._to_float(tick.get("close"))
                    prev_close_simple = tick_prev_close_s if tick_prev_close_s > 0 else 0.0
                    if prev_close_simple > 0:
                        refreshed_simple_df.loc[index, "Change %"] = round(
                            ((live_price - prev_close_simple) * 100 / prev_close_simple), 2
                        )

        if not refreshed_state_df.empty and "Symbol" in refreshed_state_df.columns and "PriceNow" in refreshed_state_df.columns:
            symbol_series = refreshed_state_df["Symbol"].astype(str).str.strip().str.upper()
            for index, symbol in symbol_series.items():
                tick = live_ticks.get(symbol)
                if not tick:
                    continue
                live_price = self._to_float(tick.get("last_price"))
                if live_price <= 0:
                    continue
                refreshed_state_df.loc[index, "PriceNow"] = round(live_price, 2)
                # Always use tick's 'close' as PrevClose — it is Kite's official
                # previous-session close, which is more reliable than OHLCV iloc[-2]
                # (which gives prev-minute close on intraday data, not prev-day close).
                tick_prev_close = self._to_float(tick.get("close"))
                if tick_prev_close > 0 and "PrevClose" in refreshed_state_df.columns:
                    refreshed_state_df.loc[index, "PrevClose"] = round(tick_prev_close, 2)
                    prev_close = tick_prev_close
                else:
                    prev_close = self._to_float(refreshed_state_df.loc[index].get("PrevClose"))
                if prev_close > 0:
                    refreshed_state_df.loc[index, "ChangePct"] = round(
                        ((live_price - prev_close) * 100 / prev_close), 2
                    )

        return refreshed_simple_df, refreshed_state_df

    def _merge_df_by_symbol(
        self,
        previous_df: pd.DataFrame,
        current_df: pd.DataFrame,
        key_column: str,
    ) -> pd.DataFrame:
        """Replace previous rows with current rows keyed by symbol-like column."""
        if previous_df is None or previous_df.empty:
            return current_df.copy() if current_df is not None else pd.DataFrame()
        if current_df is None or current_df.empty:
            return previous_df.copy()
        previous = previous_df.copy()
        current = current_df.copy()
        previous[key_column] = previous[key_column].astype(str).str.strip().str.upper()
        current[key_column] = current[key_column].astype(str).str.strip().str.upper()
        previous = previous[~previous[key_column].isin(set(current[key_column].tolist()))]
        merged = pd.concat([previous, current], ignore_index=True)
        return merged

    def _apply_buy_guardrails(
        self,
        simple_df: pd.DataFrame,
        state_df: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Downgrade buy-side actions when the linked live account is in risk-off mode."""
        guardrails = LiveTradingGuardrails(
            account_name=self.guardrail_account_name,
            max_daily_loss_amount=self.guardrail_max_daily_loss_amount,
            max_consecutive_losses=self.guardrail_max_consecutive_losses,
        )
        status = guardrails.evaluate()
        self.last_guardrail_status = status
        if not status.buy_paused:
            return simple_df, state_df

        pause_reason = "Risk-off: " + "; ".join(status.reasons)
        updated_simple_df = simple_df.copy() if simple_df is not None else pd.DataFrame()
        updated_state_df = state_df.copy() if state_df is not None else pd.DataFrame()

        if not updated_simple_df.empty and "What To Do" in updated_simple_df.columns:
            buy_mask = updated_simple_df["What To Do"].astype(str).isin(["Strong Buy Now", "Buy Now"])
            if buy_mask.any():
                updated_simple_df.loc[buy_mask, "What To Do"] = "Watch for Buy"
                if "Suggested Qty" in updated_simple_df.columns:
                    updated_simple_df.loc[buy_mask, "Suggested Qty"] = 0
                if "Checks" in updated_simple_df.columns:
                    updated_simple_df.loc[buy_mask, "Checks"] = updated_simple_df.loc[buy_mask, "Checks"].astype(str).apply(
                        lambda value: f"{value}, buy paused" if value and value != "nan" else "buy paused"
                    )
                if "Why" in updated_simple_df.columns:
                    updated_simple_df.loc[buy_mask, "Why"] = updated_simple_df.loc[buy_mask, "Why"].astype(str).apply(
                        lambda value: f"{value} | {pause_reason}" if value and value != "nan" else pause_reason
                    )

        if not updated_state_df.empty and "NowAction" in updated_state_df.columns:
            buy_mask = updated_state_df["NowAction"].astype(str).isin(["Strong Buy Now", "Buy Now"])
            if buy_mask.any():
                updated_state_df.loc[buy_mask, "NowAction"] = "Watch for Buy"
                if "SuggestedQty" in updated_state_df.columns:
                    updated_state_df.loc[buy_mask, "SuggestedQty"] = 0
                if "Checks" in updated_state_df.columns:
                    updated_state_df.loc[buy_mask, "Checks"] = updated_state_df.loc[buy_mask, "Checks"].astype(str).apply(
                        lambda value: f"{value}, buy paused" if value and value != "nan" else "buy paused"
                    )
                if "Why" in updated_state_df.columns:
                    updated_state_df.loc[buy_mask, "Why"] = updated_state_df.loc[buy_mask, "Why"].astype(str).apply(
                        lambda value: f"{value} | {pause_reason}" if value and value != "nan" else pause_reason
                    )

        return updated_simple_df, updated_state_df

    def _build_summary_from_simple(self, simple_df: pd.DataFrame, scanned_symbols: int, alert_count: int) -> pd.DataFrame:
        """Create summary counts from the merged simple board."""
        what_to_do_counts = (
            simple_df["What To Do"].value_counts().to_dict()
            if simple_df is not None and not simple_df.empty and "What To Do" in simple_df.columns
            else {}
        )
        momentum_counts = (
            simple_df["Momentum Setup"].value_counts().to_dict()
            if simple_df is not None and not simple_df.empty and "Momentum Setup" in simple_df.columns
            else {}
        )
        return pd.DataFrame(
            [
                {
                    "Universe": self.universe,
                    "ScannedSymbols": int(scanned_symbols),
                    "StrongBuyNowCount": int(what_to_do_counts.get("Strong Buy Now", 0)),
                    "BuyNowCount": int(what_to_do_counts.get("Buy Now", 0)),
                    "MomentumBuyCount": int(momentum_counts.get("Momentum Buy", 0)),
                    "SellNowCount": int(what_to_do_counts.get("Sell / Exit", 0)),
                    "WatchForBuyCount": int(what_to_do_counts.get("Watch for Buy", 0)),
                    "WatchForSellCount": int(what_to_do_counts.get("Watch for Sell", 0)),
                    "HoldCount": int(what_to_do_counts.get("Hold / Do Nothing", 0)),
                    "FreshAlertsThisRun": int(alert_count),
                    "CycleScannedSymbols": 0,
                    "CycleSeconds": 0.0,
                    "SymbolsPerSecond": 0.0,
                    "CycleLagWarning": "",
                    "LastRunAt": self.run_at_str,
                }
            ],
            columns=self.SUMMARY_COLUMNS,
        )

    def _annotate_summary_timing(self, summary_df: pd.DataFrame, cycle_started: float, cycle_scanned_symbols: int) -> pd.DataFrame:
        """Add speed diagnostics so we can see if slow cycles are costing entries."""
        if summary_df is None or summary_df.empty:
            return summary_df
        elapsed = max(0.001, time.time() - cycle_started)
        scanned = max(0, int(cycle_scanned_symbols or 0))
        symbols_per_second = scanned / elapsed if scanned > 0 else 0.0
        warning = ""
        if scanned >= 500 and elapsed > 90:
            warning = "slow_cycle_gt_90s"
        elif scanned >= 100 and elapsed > 45:
            warning = "slow_cycle_gt_45s"
        summary_df = summary_df.copy()
        summary_df["CycleScannedSymbols"] = scanned
        summary_df["CycleSeconds"] = round(elapsed, 2)
        summary_df["SymbolsPerSecond"] = round(symbols_per_second, 2)
        summary_df["CycleLagWarning"] = warning
        return summary_df[self.SUMMARY_COLUMNS].copy()

    def _build_state(self, simple_df: pd.DataFrame) -> pd.DataFrame:
        """Build a compact state table used for change detection."""
        if simple_df is None or simple_df.empty:
            return pd.DataFrame(columns=self.STATE_COLUMNS)

        state_df = pd.DataFrame(
            {
                "Symbol": simple_df["Stock"],
                "NowAction": simple_df["What To Do"],
                "PriceNow": simple_df["Price Now"],
                "PrevClose": (
                    simple_df["PrevClose"]
                    if "PrevClose" in simple_df.columns
                    else simple_df["Price Now"]
                ),
                "BuyAbove": simple_df["Buy Above"],
                "SellIfBelow": simple_df["Sell If Below"],
                "TakeProfit1": simple_df["Take Profit 1"],
                "TakeProfit2": simple_df["Take Profit 2"],
                "SuggestedQty": (
                    simple_df["Suggested Qty"]
                    if "Suggested Qty" in simple_df.columns
                    else 0
                ),
                "SignalStrengthPct": simple_df["Signal Strength %"],
                "VolumeRatio": (
                    simple_df["Volume vs Avg"]
                    if "Volume vs Avg" in simple_df.columns
                    else 0.0
                ),
                "ChangePct": (
                    simple_df["Change %"]
                    if "Change %" in simple_df.columns
                    else 0.0
                ),
                "EntryGapPct": (
                    simple_df["Entry Gap %"]
                    if "Entry Gap %" in simple_df.columns
                    else 0.0
                ),
                "RSI": (
                    simple_df["RSI"]
                    if "RSI" in simple_df.columns
                    else 50.0
                ),
                "CCI": (
                    simple_df["CCI"]
                    if "CCI" in simple_df.columns
                    else 0.0
                ),
                "MFI": (
                    simple_df["MFI"]
                    if "MFI" in simple_df.columns
                    else 50.0
                ),
                "Checks": simple_df["Checks"],
                "Why": simple_df["Why"],
                "CheckedAt": simple_df["Checked At"],
                "Week52High": (
                    simple_df["Week52High"]
                    if "Week52High" in simple_df.columns
                    else 0.0
                ),
            }
        )
        result = state_df[self.STATE_COLUMNS].copy()
        # Build a symbol→52wk-high lookup so anomaly detection can filter too
        if "Week52High" in result.columns:
            self._week52_high_map = dict(
                zip(result["Symbol"].str.upper(), result["Week52High"].astype(float))
            )
        return result

    def _load_previous_state(self) -> pd.DataFrame:
        """Load the last saved state so we only alert on fresh changes."""
        if self.reset_state:
            return pd.DataFrame(columns=self.STATE_COLUMNS)

        state_path = self._latest_path("state")
        if not os.path.exists(state_path):
            return pd.DataFrame(columns=self.STATE_COLUMNS)

        try:
            state_df = pd.read_csv(state_path)
        except Exception as exc:
            default_logger().debug(f"Failed loading monitor state: {exc}", exc_info=True)
            return pd.DataFrame(columns=self.STATE_COLUMNS)

        # Auto-reset if the state file is from a previous trading day.
        # This prevents stale cross-day state (e.g. from another machine) from
        # generating false alerts on stocks that already ran 3-9% since then.
        if "CheckedAt" in state_df.columns:
            today_str = self.run_at.strftime("%Y-%m-%d")
            checked_dates = state_df["CheckedAt"].astype(str).str[:10]
            if not checked_dates.empty and (checked_dates != today_str).all():
                stale_date = checked_dates.max()
                OutputControls().printOutput(
                    f"  [!] State file for {self.universe} is from a previous day ({stale_date}) — "
                    f"resetting to avoid stale alerts on stocks that already moved."
                )
                return pd.DataFrame(columns=self.STATE_COLUMNS)

        for column in self.STATE_COLUMNS:
            if column not in state_df.columns:
                state_df[column] = ""
        return state_df[self.STATE_COLUMNS].copy()

    def _build_alerts(self, previous_state_df: pd.DataFrame, state_df: pd.DataFrame) -> pd.DataFrame:
        """Return newly actionable rows that changed into Buy Now or Sell Now."""
        if state_df is None or state_df.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        previous_map = {}
        previous_checked_at_map = {}
        if previous_state_df is not None and not previous_state_df.empty:
            previous_map = {
                str(rec["Symbol"]).strip().upper(): str(rec["NowAction"]).strip()
                for rec in previous_state_df.to_dict(orient="records")
            }
            previous_checked_at_map = {
                str(rec["Symbol"]).strip().upper(): str(rec.get("CheckedAt", "")).strip()
                for rec in previous_state_df.to_dict(orient="records")
            }

        alert_rows = []
        skipped_already_ran = []
        today_str = self.run_at.strftime("%Y-%m-%d")
        # Cap how many first-alerts fire in one cycle to avoid Discord bursts.
        # Remaining un-alerted stocks will fire in subsequent cycles as they pass filters.
        MAX_FIRST_ALERTS_PER_CYCLE = 5
        first_alerts_this_cycle = 0
        allowed_buy_actions = ["Strong Buy Now"] if self.strong_buy_only else ["Strong Buy Now", "Buy Now"]
        for row in state_df.to_dict(orient="records"):
            symbol = str(row["Symbol"]).strip().upper()
            now_action = str(row["NowAction"]).strip()
            if now_action not in (allowed_buy_actions + ["Sell / Exit"]):
                continue

            previous_action = previous_map.get(symbol, "")
            is_repeat_buy = previous_action == now_action

            # First-alert promotion: if this symbol has NOT been alerted today (even if it
            # was already Buy Now pre-market), treat it as a fresh alert so it gets one
            # chance to fire against the day-gain / RSI / 52wk filters below.
            already_alerted_today = self._daily_alerted.get(symbol) == today_str
            if is_repeat_buy and not already_alerted_today and now_action in allowed_buy_actions:
                if first_alerts_this_cycle >= MAX_FIRST_ALERTS_PER_CYCLE:
                    # Defer to a later cycle so we don't burst the Discord channel.
                    continue
                is_repeat_buy = False  # promote to fresh alert path

            if is_repeat_buy:
                if now_action not in allowed_buy_actions:
                    continue
                previous_checked_at = self._parse_state_timestamp(previous_checked_at_map.get(symbol, ""))
                current_checked_at = self._parse_state_timestamp(row.get("CheckedAt"))
                if previous_checked_at is not None and current_checked_at is not None:
                    elapsed_seconds = (current_checked_at - previous_checked_at).total_seconds()
                    if elapsed_seconds < self.REPEAT_BUY_ALERT_SECONDS:
                        continue
                # else: timestamps missing → treat cooldown as expired, send the alert
                # For repeat Buy Now alerts, use a tighter day-gain cap (5%) — the stock
                # was already in Buy Now last cycle, so anything above 5% means the entry
                # window closed and the stock has already made its intraday move.
                repeat_change_pct = self._to_float(row.get("ChangePct"))
                if repeat_change_pct >= 5.0:
                    skipped_already_ran.append(
                        f"{symbol} ({repeat_change_pct:+.1f}% repeat)"
                    )
                    continue
                # RSI > 80 on a repeat alert = overbought, likely to pull back, skip
                repeat_rsi = self._to_float(row.get("RSI"))
                if repeat_rsi is not None and repeat_rsi > 80:
                    skipped_already_ran.append(
                        f"{symbol} (RSI {repeat_rsi:.0f} overbought repeat)"
                    )
                    continue

            # Smart "already ran" filter for buy alerts:
            # Suppress regular Buy Now when the stock has moved >= 5% today AND
            # is already AT or PAST its entry trigger (the breakout window has closed).
            # KEEP the alert when the stock is still approaching its trigger even if
            # it is up 5%+ today — e.g. TITAN +4.45% but still 0.29% BELOW BuyAbove
            # is a valid first-time approach alert.
            #
            # Stocks at/past trigger + 5%+ daily gain will instead be caught by the
            # dedicated _build_momentum_alerts() path (requires vol >= 1.3x + score >= 75).
            if now_action in allowed_buy_actions:
                change_pct = self._to_float(row.get("ChangePct"))
                # Hard cap: if stock is up 10%+ today, suppress regardless of trigger
                # position. SCILAL +14% with BuyAbove ₹0.4 above current price was being
                # allowed via the "still approaching" exception — too late to enter.
                if change_pct >= self.MAX_BUY_DAILY_CHANGE_PCT:
                    skipped_already_ran.append(
                        f"{symbol} ({change_pct:+.1f}% day gain over cap)"
                    )
                    continue
                # Opening gap exception: first 45 min (9:15-10:00 IST), a stock
                # gapping 5-7% with high volume is a gap-and-go setup, not a chase.
                # Raise the suppress threshold to 8% during opening window only.
                try:
                    _now_ist = PKDateUtilities.currentDateTime()
                except Exception:
                    _now_ist = datetime.now()
                _ist_min = _now_ist.hour * 60 + _now_ist.minute
                _opening_window = (9 * 60 + 15) <= _ist_min <= (10 * 60 + 0)
                _suppress_threshold = 8.0 if _opening_window else 5.0
                if change_pct >= _suppress_threshold:
                    entry_gap_pct = self._to_float(row.get("EntryGapPct"))
                    already_at_or_past_trigger = entry_gap_pct <= 0.0
                    if already_at_or_past_trigger:
                        buy_above = self._to_float(row.get("BuyAbove"))
                        price_now = self._to_float(row.get("PriceNow"))
                        pct_past = (
                            (price_now - buy_above) / buy_above * 100.0
                            if buy_above > 0 and price_now >= buy_above
                            else 0.0
                        )
                        skipped_already_ran.append(
                            f"{symbol} ({change_pct:+.1f}% day, {pct_past:.1f}% past entry)"
                        )
                        continue

            # Block any Buy Now alert (fresh or repeat) when RSI is severely overbought —
            # RSI > 85 means the stock has already run hard and "may pull back" is real.
            if now_action in allowed_buy_actions:
                rsi_val = self._to_float(row.get("RSI"))
                if rsi_val is not None and rsi_val > 85:
                    skipped_already_ran.append(
                        f"{symbol} (RSI {rsi_val:.0f} — overbought, skip)"
                    )
                    continue

            # Block Buy Now when 52-week high resistance is within 3% above current price
            # and the stock hasn't broken above it — almost no upside room before annual sellers.
            if now_action in allowed_buy_actions:
                week52_high = self._to_float(row.get("Week52High"))
                price_now = self._to_float(row.get("PriceNow"))
                if week52_high and week52_high > 0 and price_now and price_now > 0 and price_now < week52_high:
                    gap_to_52wk = (week52_high - price_now) / price_now * 100.0
                    if gap_to_52wk < 3.0:
                        skipped_already_ran.append(
                            f"{symbol} (52wk-high ₹{week52_high:.2f} only {gap_to_52wk:.1f}% away)"
                        )
                        continue

            alert_rows.append(
                {
                    "When": self.run_at_str,
                    "Symbol": symbol,
                    "PreviousAction": previous_action or "None",
                    "NowAction": now_action,
                    "PriceNow": row["PriceNow"],
                    "ChangePct": row.get("ChangePct", ""),
                    "EntryGapPct": row.get("EntryGapPct", ""),
                    "RSI": row.get("RSI", ""),
                    "BuyAbove": row["BuyAbove"],
                    "SellIfBelow": row["SellIfBelow"],
                    "TakeProfit1": row["TakeProfit1"],
                    "TakeProfit2": row["TakeProfit2"],
                    "SuggestedQty": row["SuggestedQty"],
                    "SignalStrengthPct": row["SignalStrengthPct"],
                    "Checks": row["Checks"],
                    "Why": row["Why"],
                }
            )
            if now_action in allowed_buy_actions:
                self._daily_alerted[symbol] = today_str
                if not already_alerted_today:
                    first_alerts_this_cycle += 1

        if skipped_already_ran:
            OutputControls().printOutput(
                f"  [!] Suppressed {len(skipped_already_ran)} buy alert(s) — stocks already up "
                f">={self.MAX_BUY_DAILY_CHANGE_PCT:.0f}% today (already ran): "
                + ", ".join(skipped_already_ran[:10])
                + (" ..." if len(skipped_already_ran) > 10 else "")
            )

        alerts_df = pd.DataFrame(alert_rows, columns=self.ALERT_COLUMNS)
        if not alerts_df.empty:
            alerts_df.sort_values(
                by=["NowAction", "SignalStrengthPct", "Symbol"],
                ascending=[True, False, True],
                inplace=True,
            )
        return alerts_df[self.ALERT_COLUMNS].copy()

    def _parse_state_timestamp(self, value) -> Optional[datetime]:
        """Parse saved monitor timestamps safely for cooldown checks."""
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() == "nan":
            return None
        try:
            parsed = pd.to_datetime(text, errors="coerce")
        except Exception:
            return None
        if pd.isna(parsed):
            return None
        if hasattr(parsed, "to_pydatetime"):
            return parsed.to_pydatetime()
        return parsed

    def _run_anomaly_detection(self) -> pd.DataFrame:
        """Run V2 volume anomaly detection with market context awareness.

        Returns an alerts DataFrame matching ALERT_COLUMNS for any detected
        volume anomaly candidates (labelled by confidence tier).
        """
        if not self.enable_anomaly_detection:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        if not self.use_live_kite:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        # Lazy-init the detector and load profiles once
        if self._anomaly_detector is None:
            self._anomaly_detector = VolumeAnomalyDetector()
        if not self._anomaly_profiles_loaded:
            try:
                count = self._anomaly_detector.load_historical_profiles(None)
                self._anomaly_profiles_loaded = count > 0
                if count > 0:
                    OutputControls().printOutput(
                        f"  [+] Anomaly detector: loaded {count} volume profiles (all cached)"
                    )
            except Exception as exc:
                default_logger().warning(f"AnomalyDetector profile load failed: {exc}")
                return pd.DataFrame(columns=self.ALERT_COLUMNS)

        # Lazy-init market context engine
        if self._market_context is None:
            try:
                from pkscreener.classes.MarketContext import MarketContextEngine
                self._market_context = MarketContextEngine()
            except Exception as exc:
                default_logger().warning(f"MarketContext init failed: {exc}")

        # Get current ticks
        current_ticks = self.actionable_runner.live_ticks or {}
        if not current_ticks:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        try:
            candidates = self._anomaly_detector.update_from_ticks(
                current_ticks, market_context=self._market_context
            )
        except Exception as exc:
            default_logger().warning(f"AnomalyDetector update failed: {exc}")
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        if not candidates:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        # Convert V2 candidates to alert rows
        alert_rows = []
        # Late-day cutoff: don't fire intraday-style anomaly alerts in the last
        # 45 minutes. Liquidity dries up, prices jump erratically, and the user
        # has no time to act before close.
        try:
            _now_ist = PKDateUtilities.currentDateTime()
        except Exception:
            _now_ist = datetime.now()
        _ist_minute = int(_now_ist.hour) * 60 + int(_now_ist.minute)
        _late_day_cutoff_min = 14 * 60 + 45  # 14:45 IST
        if _ist_minute > _late_day_cutoff_min:
            OutputControls().printOutput(
                f"  [!] Anomaly batch skipped — too late ({_now_ist.strftime('%H:%M')} IST > 14:45)"
            )
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        for c in candidates:
            # Only Grade-A "Runner" alerts pass — Grade-B and Grade-C generate
            # too much volume-spike noise without strong setup confirmation.
            if c.confidence_tier != "A":
                continue
            # Skip when sell pressure dominates — more sellers than buyers
            # means the institutional volume is being absorbed, not driving the price up
            if c.buy_sell_ratio < 1.0:
                continue
            # Day gain cap: stock has already moved too far from open — chasing
            # at +10%+ usually means buying the top of the move
            if c.price_from_open_pct > 10.0:
                OutputControls().printOutput(
                    f"  [!] Anomaly {c.symbol} skipped — already +{c.price_from_open_pct:.1f}% from open (cap 10%)"
                )
                continue
            # Min price filter: penny stocks (< ₹50) are easily manipulated and
            # have wide bid-ask spreads. Skip them even on volume anomalies.
            if c.last_price < 50.0:
                OutputControls().printOutput(
                    f"  [!] Anomaly {c.symbol} skipped — price ₹{c.last_price:.2f} below ₹50 floor"
                )
                continue

            entry = c.last_price
            # V2: Tighter SL for high-quality, wider for lower
            sl_pct = 0.03 if c.confidence_tier == "A" else 0.05
            stop_loss = round(entry * (1 - sl_pct), 2)
            target1 = round(entry * 1.08, 2)
            target2 = round(entry * 1.15, 2) if c.confidence_tier == "A" else round(entry * 1.12, 2)

            # Action label encodes confidence
            tier_labels = {
                "A": "🔥 Grade-A Runner",
                "B": "⚡ Grade-B Anomaly",
                "C": "👀 Pre-Move Watch",
            }
            action_label = tier_labels.get(c.confidence_tier, "Volume Anomaly")

            # V2: Rich checks with quality score and sector
            checks_parts = [
                f"Q:{c.quality_score:.0f}/100 [{c.confidence_tier}]",
                f"Vol {c.volume_ratio_time_adj:.1f}x",
                f"+{c.price_from_open_pct:.1f}%",
            ]
            if c.sector_confirmed:
                checks_parts.append(f"Sector✓ ({c.sector})")
            if c.buy_sell_ratio > 1.5:
                checks_parts.append(f"B/S {c.buy_sell_ratio:.1f}x")
            if c.range_compression:
                checks_parts.append("Compressed📐")
            if not c.market_safe:
                checks_parts.append("⚠️CRASH")

            # Skip if 52-week high resistance is within 3% — very little upside room
            week52 = self._week52_high_map.get(str(c.symbol).strip().upper(), 0.0)
            if week52 > 0 and entry > 0 and entry < week52:
                gap = (week52 - entry) / entry * 100.0
                if gap < 3.0:
                    OutputControls().printOutput(
                        f"  [!] Anomaly {c.symbol} skipped — 52wk-high ₹{week52:.2f} only {gap:.1f}% away"
                    )
                    continue

            # Suggested quantity from budget — same approach as OHLCV alerts
            suggested_qty = 0
            if self.budget_amount and self.budget_amount > 0 and entry > 0:
                suggested_qty = int(self.budget_amount // float(entry))

            alert_rows.append({
                "When": self.run_at_str,
                "Symbol": c.symbol,
                "PreviousAction": "None",
                    "NowAction": action_label,
                    "PriceNow": c.last_price,
                    "ChangePct": round(c.price_from_open_pct, 2),
                    "EntryGapPct": 0.0,
                    "RSI": "",
                    "BuyAbove": entry,
                    "SellIfBelow": stop_loss,
                    "TakeProfit1": target1,
                    "TakeProfit2": target2,
                "SuggestedQty": suggested_qty,
                "SignalStrengthPct": round(c.quality_score, 1),
                "Checks": " | ".join(checks_parts),
                "Why": c.narrative,
            })

        if alert_rows:
            summary = ", ".join(
                f"{c.symbol}[{c.confidence_tier}] +{c.price_from_open_pct:.1f}% Q:{c.quality_score:.0f}"
                for c in candidates[:5]
            )
            regime_str = candidates[0].market_regime if candidates else "?"
            OutputControls().printOutput(
                f"  [+] 🔥 V2 Anomaly ({regime_str} market): {len(alert_rows)} candidate(s) — {summary}"
            )

        # Register for follow-through monitoring (5-min recheck)
        if candidates:
            self._anomaly_detector.register_for_followthrough(candidates)

        return pd.DataFrame(alert_rows, columns=self.ALERT_COLUMNS)

    def _check_followthrough_and_exits(self) -> None:
        """Check follow-through on recent alerts and exit signals on active positions."""
        if not self.enable_followthrough:
            return
        if not self.use_live_kite or self._anomaly_detector is None:
            return

        current_ticks = self.actionable_runner.live_ticks or {}
        if not current_ticks:
            return

        # 1. Follow-through check (5 min after alert)
        try:
            ft_results = self._anomaly_detector.check_followthrough(current_ticks)
            for ft in ft_results:
                sym = ft["symbol"]
                if ft["confirmed"]:
                    emoji = "✅" if ft["action"] == "hold_strong" else "👍"
                    OutputControls().printOutput(
                        f"  [{emoji}] Follow-through {sym}: {ft['change_pct']:+.2f}% "
                        f"after {ft['minutes_elapsed']:.0f}min → {ft['action'].upper()}"
                    )
                    # Auto-register as active position for exit monitoring
                    self._anomaly_detector.register_position(
                        sym, ft["alert_price"], ft["tier"]
                    )
                else:
                    OutputControls().printOutput(
                        f"  [❌] Follow-through FAILED {sym}: {ft['change_pct']:+.2f}% "
                        f"after {ft['minutes_elapsed']:.0f}min — consider EXIT"
                    )
        except Exception as exc:
            default_logger().debug(f"Follow-through check error: {exc}")

        # 2. Exit signal check on active positions
        try:
            exits = self._anomaly_detector.check_exits(current_ticks)
            for ex in exits:
                pnl_emoji = "🟢" if ex["pnl_pct"] > 0 else "🔴"
                OutputControls().printOutput(
                    f"  [{pnl_emoji}] EXIT {ex['symbol']}: {ex['pnl_pct']:+.2f}% "
                    f"(₹{ex['entry_price']:.2f} → ₹{ex['current_price']:.2f}) | "
                    f"{ex['exit_reason']}"
                )
        except Exception as exc:
            default_logger().debug(f"Exit check error: {exc}")

    def _run_missed_opportunity_analysis(self) -> None:
        """Run end-of-day missed opportunity analysis. Call after 15:00."""
        if not self.enable_missed_opportunity:
            return
        if not self.use_live_kite or self._anomaly_detector is None:
            return

        minutes = 0
        try:
            import datetime
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
            market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
            minutes = int((now - market_open).total_seconds() / 60)
        except Exception:
            return

        # Only run after 15:00 IST (345 min after open) and only once
        if minutes < 345 or getattr(self, "_missed_analysis_done", False):
            return
        self._missed_analysis_done = True

        current_ticks = self.actionable_runner.live_ticks or {}
        if not current_ticks:
            return

        try:
            missed = self._anomaly_detector.analyze_missed_opportunities(current_ticks)
            if missed:
                OutputControls().printOutput(
                    f"\n  [📊] MISSED OPPORTUNITY ANALYSIS — {len(missed)} stocks ran 5%+ today but were NOT alerted:"
                )
                for i, m in enumerate(missed[:10], 1):
                    OutputControls().printOutput(
                        f"  {i}. {m['symbol']}: +{m['from_open_pct']:.1f}% from open | "
                        f"Vol {m['volume_ratio']:.1f}x | Gap {m['gap_pct']:+.1f}% | "
                        f"WHY MISSED: {m['reasons']}"
                    )
            else:
                OutputControls().printOutput(
                    "  [📊] Missed opportunity analysis: No major runners missed today 🎯"
                )
        except Exception as exc:
            default_logger().debug(f"Missed opportunity analysis error: {exc}")

    def _build_momentum_alerts(
        self,
        previous_simple_df: pd.DataFrame,
        current_simple_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Alert when a stock freshly enters Momentum Buy with a significant daily gain.

        This catches the '3% to 20%' scenario: a stock that is already running hard
        and NOW qualifies as a Momentum Buy (strong volume + trend + breakout day).
        These alerts are labelled 'Momentum Buy' so they are visually distinct from
        regular 'Buy Now' entry alerts.
        """
        if current_simple_df is None or current_simple_df.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        if "Momentum Setup" not in current_simple_df.columns:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        # Previous momentum state: symbol -> previous setup value
        prev_momentum_map: Dict[str, str] = {}
        if (
            previous_simple_df is not None
            and not previous_simple_df.empty
            and "Momentum Setup" in previous_simple_df.columns
            and "Stock" in previous_simple_df.columns
        ):
            for rec in previous_simple_df.to_dict(orient="records"):
                sym = str(rec.get("Stock", "")).strip().upper()
                if sym:
                    prev_momentum_map[sym] = str(rec.get("Momentum Setup", "")).strip()

        alert_rows = []
        for rec in current_simple_df.to_dict(orient="records"):
            current_setup = str(rec.get("Momentum Setup", "")).strip()
            if current_setup != "Momentum Buy":
                continue

            sym = str(rec.get("Stock", "")).strip().upper()
            if not sym:
                continue

            prev_setup = prev_momentum_map.get(sym, "")
            if prev_setup == "Momentum Buy":
                continue  # Already was Momentum Buy — no transition, no alert

            # Only fire when stock has meaningful intraday gain (it is already in motion)
            change_pct = self._to_float(rec.get("Change %"))
            if change_pct < self.MIN_MOMENTUM_EXTENSION_PCT:
                continue

            # Apply same quality filters as Buy Now alerts
            # Cap: day gain too high — already ran past viable entry
            if change_pct >= 8.0:
                continue
            # RSI severely overbought — scanner says "may pull back"
            rsi_val = self._to_float(rec.get("RSI"))
            if rsi_val is not None and rsi_val > 80:
                continue
            # 52-week high resistance within 3% — limited upside before annual sellers
            week52 = self._to_float(rec.get("Week52High"))
            price_now = self._to_float(rec.get("Price Now"))
            if week52 and week52 > 0 and price_now and price_now > 0 and price_now < week52:
                if (week52 - price_now) / price_now * 100.0 < 3.0:
                    continue

            alert_rows.append(
                {
                    "When": self.run_at_str,
                    "Symbol": sym,
                    "PreviousAction": prev_setup or "None",
                    "NowAction": "Momentum Buy",
                    "PriceNow": rec.get("Price Now", ""),
                    "ChangePct": change_pct,
                    "EntryGapPct": rec.get("Entry Gap %", ""),
                    "RSI": rec.get("RSI", ""),
                    "BuyAbove": rec.get("Buy Above", ""),
                    "SellIfBelow": rec.get("Sell If Below", ""),
                    "TakeProfit1": rec.get("Take Profit 1", ""),
                    "TakeProfit2": rec.get("Take Profit 2", ""),
                    "SuggestedQty": rec.get("Suggested Qty", 0),
                    "SignalStrengthPct": rec.get("Signal Strength %", 0),
                    "Checks": rec.get("Checks", ""),
                    "Why": str(rec.get("Why", "")) + f" | Momentum extension: day gain +{change_pct:.1f}%",
                }
            )

        if not alert_rows:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        mom_df = pd.DataFrame(alert_rows, columns=self.ALERT_COLUMNS)
        mom_df.sort_values(
            by=["SignalStrengthPct", "Symbol"],
            ascending=[False, True],
            inplace=True,
        )
        if not mom_df.empty:
            OutputControls().printOutput(
                f"  [+] {len(mom_df)} momentum extension alert(s): "
                + ", ".join(
                    f"{r['Symbol']} (+{self._to_float(r.get('Why','').split('+')[-1].replace('%','').strip()) if '+' in str(r.get('Why','')) else '?'}%)"
                    for _, r in mom_df.head(5).iterrows()
                )
            )
        return mom_df[self.ALERT_COLUMNS].copy()

    def _build_summary(self, report_df: pd.DataFrame, scanned_symbols: int, alert_count: int) -> pd.DataFrame:
        """Create a compact monitor summary for CSV and dashboard use."""
        decision_counts = (
            report_df["NowAction"].value_counts().to_dict()
            if report_df is not None and not report_df.empty and "NowAction" in report_df.columns
            else {}
        )
        return pd.DataFrame(
            [
                {
                    "Universe": self.universe,
                    "ScannedSymbols": int(scanned_symbols),
                    "StrongBuyNowCount": int(decision_counts.get("Strong Buy Now", 0)),
                    "BuyNowCount": int(decision_counts.get("Buy Now", 0)),
                    "SellNowCount": int(decision_counts.get("Sell Now", 0)),
                    "WatchForBuyCount": int(decision_counts.get("Watch for Buy", 0)),
                    "WatchForSellCount": int(decision_counts.get("Watch for Sell", 0)),
                    "HoldCount": int(decision_counts.get("Hold / Do Nothing", 0)),
                    "FreshAlertsThisRun": int(alert_count),
                    "LastRunAt": self.run_at_str,
                }
            ],
            columns=self.SUMMARY_COLUMNS,
        )

    def _save_outputs(
        self,
        report_df: pd.DataFrame,
        actionable_df: pd.DataFrame,
        simple_df: pd.DataFrame,
        state_df: pd.DataFrame,
        alerts_df: pd.DataFrame,
        summary_df: pd.DataFrame,
    ) -> None:
        """Persist CSV and HTML monitor outputs.

        Always overwrites the *_latest files (what the bot reads).
        Once per trading day, near market close (after 3:20 PM IST), also
        writes a *_YYYYMMDD daily snapshot for forensic use.  Snapshots
        older than 30 days are deleted automatically.
        """
        report_to_save = report_df.copy()
        actionable_to_save = actionable_df.copy()
        report_to_save.drop(columns=["ActionRank", "NowActionRank"], inplace=True, errors="ignore")
        actionable_to_save.drop(columns=["ActionRank", "NowActionRank"], inplace=True, errors="ignore")
        momentum_df = (
            self._build_momentum_board(simple_df)
            if not self.fast_mode
            else pd.DataFrame()
        )

        # Always write _latest (overwrite)
        if self.save_full_outputs:
            report_to_save.to_csv(self._latest_path("signals"), index=False)
        simple_df.to_csv(self._latest_path("signals_simple"), index=False)
        momentum_df.to_csv(self._latest_path("signals_momentum"), index=False)
        if self.save_full_outputs:
            actionable_to_save.to_csv(self._latest_path("actionable"), index=False)
        summary_df.to_csv(self._latest_path("summary"), index=False)
        state_df.to_csv(self._latest_path("state"), index=False)
        alerts_df.to_csv(self._latest_path("alerts"), index=False)
        dashboard_latest = self._dashboard_latest_path()
        if self.enable_dashboard:
            self._save_dashboard(
                latest_path=dashboard_latest,
                dated_path=dashboard_latest,
                summary_df=summary_df,
                alerts_df=alerts_df,
                simple_df=simple_df,
            )

        OutputControls().printOutput(f"  [+] Saved monitor summary: {self._latest_path('summary')}")
        OutputControls().printOutput(f"  [+] Saved monitor board: {self._latest_path('signals_simple')}")
        if not self.fast_mode:
            OutputControls().printOutput(f"  [+] Saved momentum board: {self._latest_path('signals_momentum')}")
        OutputControls().printOutput(f"  [+] Saved monitor alerts: {self._latest_path('alerts')}")
        if self.enable_dashboard:
            OutputControls().printOutput(f"  [+] Saved monitor dashboard: {dashboard_latest}")

        # Write one daily end-of-day snapshot after 3:20 PM IST (once per day)
        self._maybe_write_daily_snapshot(
            report_to_save, actionable_to_save, simple_df,
            momentum_df, state_df, alerts_df, summary_df,
        )

    def _maybe_write_daily_snapshot(
        self,
        report_df: "pd.DataFrame",
        actionable_df: "pd.DataFrame",
        simple_df: "pd.DataFrame",
        momentum_df: "pd.DataFrame",
        state_df: "pd.DataFrame",
        alerts_df: "pd.DataFrame",
        summary_df: "pd.DataFrame",
    ) -> None:
        """Write one end-of-day snapshot per trading day; purge files older than 30 days."""
        import glob as _glob
        try:
            import pytz as _pytz
            _ist = _pytz.timezone("Asia/Kolkata")
            _now = datetime.now(_ist)
        except Exception:
            _now = datetime.now()
        # Only snapshot in the 3:20–3:35 PM IST window
        if not (15 * 60 + 20 <= _now.hour * 60 + _now.minute <= 15 * 60 + 35):
            return
        today_stamp = _now.strftime("%Y%m%d")
        # Check if today's snapshot was already written (any of the 9 files)
        sentinel = self._daily_path("signals", today_stamp)
        if os.path.exists(sentinel):
            return
        # Write all daily snapshots
        report_df.to_csv(self._daily_path("signals", today_stamp), index=False)
        simple_df.to_csv(self._daily_path("signals_simple", today_stamp), index=False)
        momentum_df.to_csv(self._daily_path("signals_momentum", today_stamp), index=False)
        actionable_df.to_csv(self._daily_path("actionable", today_stamp), index=False)
        summary_df.to_csv(self._daily_path("summary", today_stamp), index=False)
        state_df.to_csv(self._daily_path("state", today_stamp), index=False)
        alerts_df.to_csv(self._daily_path("alerts", today_stamp), index=False)
        OutputControls().printOutput(f"  [+] Saved daily EOD snapshot: {today_stamp}")
        # Purge daily snapshots older than 30 days
        self._purge_old_daily_snapshots(keep_days=30)

    def _save_latest_outputs_only(
        self,
        report_df: pd.DataFrame,
        actionable_df: pd.DataFrame,
        simple_df: pd.DataFrame,
        state_df: pd.DataFrame,
        alerts_df: pd.DataFrame,
        summary_df: pd.DataFrame,
    ) -> None:
        """Persist only latest CSVs for in-progress monitor scans."""
        report_to_save = report_df.copy() if report_df is not None else pd.DataFrame()
        actionable_to_save = actionable_df.copy() if actionable_df is not None else pd.DataFrame()
        simple_to_save = simple_df.copy() if simple_df is not None else pd.DataFrame()
        state_to_save = state_df.copy() if state_df is not None else pd.DataFrame()
        alerts_to_save = alerts_df.copy() if alerts_df is not None else pd.DataFrame(columns=self.ALERT_COLUMNS)
        summary_to_save = summary_df.copy() if summary_df is not None else pd.DataFrame(columns=self.SUMMARY_COLUMNS)

        report_to_save.drop(columns=["ActionRank", "NowActionRank"], inplace=True, errors="ignore")
        actionable_to_save.drop(columns=["ActionRank", "NowActionRank"], inplace=True, errors="ignore")

        report_to_save.to_csv(self._latest_path("signals"), index=False)
        simple_to_save.to_csv(self._latest_path("signals_simple"), index=False)
        momentum_to_save = self._build_momentum_board(simple_to_save) if not self.fast_mode else pd.DataFrame()
        momentum_to_save.to_csv(self._latest_path("signals_momentum"), index=False)
        actionable_to_save.to_csv(self._latest_path("actionable"), index=False)
        summary_to_save.to_csv(self._latest_path("summary"), index=False)
        state_to_save.to_csv(self._latest_path("state"), index=False)
        alerts_to_save.to_csv(self._latest_path("alerts"), index=False)

    def _print_summary(self, summary_df: pd.DataFrame, simple_df: pd.DataFrame, alerts_df: pd.DataFrame, scorecard_df: Optional[pd.DataFrame] = None) -> None:
        """Print a short monitor summary after each cycle."""
        summary = summary_df.iloc[0].to_dict() if summary_df is not None and not summary_df.empty else {}
        OutputControls().printOutput(
            "  [+] Monitor summary: "
            f"Scanned {summary.get('ScannedSymbols', 0)} | "
            f"Strong Buy Now {summary.get('StrongBuyNowCount', 0)} | "
            f"Buy Now {summary.get('BuyNowCount', 0)} | "
            f"Momentum Buy {summary.get('MomentumBuyCount', 0)} | "
            f"Sell Now {summary.get('SellNowCount', 0)} | "
            f"Fresh alerts {summary.get('FreshAlertsThisRun', 0)} | "
            f"Cycle {float(summary.get('CycleSeconds', 0.0) or 0.0):.1f}s "
            f"({float(summary.get('SymbolsPerSecond', 0.0) or 0.0):.1f}/s)"
        )
        if str(summary.get("CycleLagWarning", "") or "").strip():
            OutputControls().printOutput(
                f"  [!] Monitor speed warning: {summary.get('CycleLagWarning')} — "
                "increase workers/prewarm or reduce universe if this repeats."
            )

        if alerts_df is not None and not alerts_df.empty:
            OutputControls().printOutput("  [+] Fresh monitor alerts:")
            preview_cols = ["Symbol", "NowAction", "PriceNow", "BuyAbove", "SellIfBelow", "SuggestedQty"]
            OutputControls().printOutput(
                alerts_df[preview_cols].head(12).to_string(index=False),
                enableMultipleLineOutput=True,
            )
        elif simple_df is not None and not simple_df.empty:
            preview_df = simple_df[
                simple_df["What To Do"].isin(["Strong Buy Now", "Buy Now", "Sell / Exit"])
            ].head(12)
            if preview_df.empty:
                OutputControls().printOutput(
                    "  [+] No actionable Buy Now / Sell Now rows in this cycle.",
                    enableMultipleLineOutput=True,
                )
            else:
                OutputControls().printOutput(
                    preview_df.to_string(index=False),
                    enableMultipleLineOutput=True,
                )

        if self.last_guardrail_status is not None and self.last_guardrail_status.buy_paused:
            OutputControls().printOutput(
                "  [+] Buy-side guardrail active: " + "; ".join(self.last_guardrail_status.reasons)
            )

        if scorecard_df is not None and not scorecard_df.empty:
            all_row = scorecard_df[scorecard_df["Segment"] == "ALL"]
            if not all_row.empty:
                metrics = all_row.iloc[0].to_dict()
                OutputControls().printOutput(
                    "  [+] Scorecard: "
                    f"15m win {float(metrics.get('WinRate15mPct', 0.0)):.1f}% | "
                    f"60m win {float(metrics.get('WinRate60mPct', 0.0)):.1f}% | "
                    f"EOD win {float(metrics.get('WinRateEODPct', 0.0)):.1f}% | "
                    f"Next-day win {float(metrics.get('WinRateNextDayPct', 0.0)):.1f}%"
                )

    def _build_momentum_board(self, simple_df: pd.DataFrame) -> pd.DataFrame:
        """Return a ranked board of momentum promotions and near-promotions."""
        if simple_df is None or simple_df.empty or "Momentum Setup" not in simple_df.columns:
            return pd.DataFrame()
        momentum_df = simple_df[
            simple_df["Momentum Setup"].astype(str).isin(["Momentum Buy", "Momentum Watch"])
        ].copy()
        if momentum_df.empty:
            return momentum_df
        if "Momentum Score" in momentum_df.columns:
            momentum_df["Momentum Score"] = pd.to_numeric(momentum_df["Momentum Score"], errors="coerce").fillna(0.0)
        momentum_df.sort_values(
            ["Momentum Setup", "Momentum Score", "Signal Strength %", "Stock"],
            ascending=[True, False, False, True],
            inplace=True,
        )
        return momentum_df.head(50).copy()

    def _paper_track_alerts(self, alerts_df: pd.DataFrame) -> None:
        """Paper-track every live buy-style alert so we can prove signal quality safely."""
        if not self.enable_paper_tracking:
            return
        if alerts_df is None or alerts_df.empty:
            return
        if not self._is_regular_market_hours():
            return

        tracked = 0
        for _, row in alerts_df.iterrows():
            action = str(row.get("NowAction", "") or "")
            action_lower = action.lower()
            is_buy_style = (
                action in {"Strong Buy Now", "Buy Now", "Momentum Buy"}
                or "anomaly" in action_lower
                or "runner" in action_lower
                or "pre-move" in action_lower
            )
            if not is_buy_style:
                continue

            symbol = str(row.get("Symbol", "") or "").strip().upper()
            price = self._to_float(row.get("PriceNow"))
            if not symbol or price <= 0:
                continue

            score = self._to_float(row.get("SignalStrengthPct"))
            qty = self._safe_int(row.get("SuggestedQty"))
            source = self._paper_source_for_action(action)
            grade = self._paper_grade_for_action(action)
            volume_ratio = self._extract_volume_ratio(row.get("Checks"))
            self._paper_tracker.log_entry(
                symbol=symbol,
                price=price,
                score=score,
                grade=grade,
                volume_ratio=volume_ratio,
                change_at_entry=0.0,
                stop_loss=self._to_float(row.get("SellIfBelow")),
                target1=self._to_float(row.get("TakeProfit1")),
                target2=self._to_float(row.get("TakeProfit2")),
                suggested_qty=qty,
                source=source,
            )
            tracked += 1

        if tracked:
            OutputControls().printOutput(f"  [+] Paper-tracked {tracked} live alert(s) for truth validation.")

    def _close_paper_trades_if_eod(self) -> None:
        """Close monitor paper trades near EOD using the latest Kite tick snapshot."""
        if not self.enable_paper_tracking:
            return
        try:
            now_ist = PKDateUtilities.currentDateTime()
        except Exception:
            now_ist = datetime.now()
        today = now_ist.strftime("%Y-%m-%d")
        minute_of_day = int(now_ist.hour) * 60 + int(now_ist.minute)
        if minute_of_day < (15 * 60 + 10) or self._paper_closed_date == today:
            return
        ticks = self.actionable_runner.live_ticks or {}
        summary = self._paper_tracker.close_day(ticks)
        self._paper_closed_date = today
        if summary:
            OutputControls().printOutput(summary, enableMultipleLineOutput=True)

    @staticmethod
    def _safe_int(value) -> int:
        try:
            if value is None or pd.isna(value):
                return 0
            text = str(value).strip()
            if not text or text.lower() == "nan":
                return 0
            return int(float(text))
        except Exception:
            return 0

    @staticmethod
    def _paper_source_for_action(action: str) -> str:
        action_lower = str(action or "").lower()
        if "anomaly" in action_lower or "runner" in action_lower or "pre-move" in action_lower:
            return "signal_monitor_anomaly"
        if "momentum" in action_lower:
            return "signal_monitor_momentum"
        return "signal_monitor_buy"

    @staticmethod
    def _paper_grade_for_action(action: str) -> str:
        action_lower = str(action or "").lower()
        if "grade-a" in action_lower or "strong" in action_lower:
            return "A"
        if "grade-b" in action_lower or "buy now" in action_lower:
            return "B"
        if "momentum" in action_lower:
            return "MOM"
        return "C"

    @staticmethod
    def _extract_volume_ratio(checks) -> float:
        import re

        text = str(checks or "")
        for pattern in (r"Vol\s+([0-9]+(?:\.[0-9]+)?)x", r"volume\s+([0-9]+(?:\.[0-9]+)?)x"):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1))
                except Exception:
                    return 0.0
        return 0.0

    def _send_alerts(self, alerts_df: pd.DataFrame, summary_df: pd.DataFrame) -> None:
        """Send desktop / Telegram / Discord alerts for fresh Buy Now / Sell Now changes."""
        if not self.alert_channels or alerts_df is None or alerts_df.empty:
            return
        if not self._is_regular_market_hours():
            return

        alerts_df = self._filter_alerts_for_portfolio(alerts_df)
        if alerts_df is None or alerts_df.empty:
            return

        summary = summary_df.iloc[0].to_dict() if summary_df is not None and not summary_df.empty else {}
        event_lines = self._build_plain_event_lines(alerts_df, max_alerts=8)
        if self.last_guardrail_status is not None and self.last_guardrail_status.buy_paused:
            event_lines = [
                f"[Risk Off] buy-side alerts paused for {self.last_guardrail_status.account_name}"
            ] + event_lines

        title = f"PKScreener {self.universe} monitor"
        message = "\n".join(
            [
                f"{title} @ {self.run_at_str}",
                *event_lines,
                (
                    f"Scanned: {summary.get('ScannedSymbols', 0)} | "
                    f"Strong Buy Now: {summary.get('StrongBuyNowCount', 0)} | "
                    f"Buy Now: {summary.get('BuyNowCount', 0)} | "
                    f"Sell Now: {summary.get('SellNowCount', 0)} | "
                    f"Fresh alerts: {summary.get('FreshAlertsThisRun', 0)}"
                ),
            ]
        )

        if "desktop" in self.alert_channels:
            self._send_desktop_alert(title, "\n".join(event_lines[:5]))
        if "telegram" in self.alert_channels:
            self._send_telegram_alert(message)
        if "discord" in self.alert_channels:
            self._send_discord_alert(
                self._build_discord_monitor_message(alerts_df, summary)
            )

    def _is_regular_market_hours(self) -> bool:
        """Only send live monitor alerts during NSE regular market hours."""
        try:
            now_ist = PKDateUtilities.currentDateTime()
        except Exception:
            now_ist = datetime.now()
        minute_of_day = int(now_ist.hour) * 60 + int(now_ist.minute)
        return self.MARKET_OPEN_MINUTES_IST <= minute_of_day <= self.MARKET_CLOSE_MINUTES_IST

    def _load_tracked_portfolio_symbols(self) -> Set[str]:
        """Load open tracked Kite symbols so generic market monitor sells stay portfolio-aware."""
        tracked_file = os.path.join(self.output_dir, "kite_tracked_positions.json")
        if not os.path.exists(tracked_file):
            return set()
        try:
            raw = json.loads(open(tracked_file, "r", encoding="utf-8").read())
        except Exception:
            return set()
        symbols: Set[str] = set()
        if not isinstance(raw, dict):
            return symbols
        for sym, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            if str(payload.get("status", "")).lower() != "open":
                continue
            symbols.add(str(sym or payload.get("symbol", "")).strip().upper())
        return symbols

    def _filter_alerts_for_portfolio(self, alerts_df: pd.DataFrame) -> pd.DataFrame:
        """Suppress generic market sell alerts for symbols not in the tracked portfolio."""
        if alerts_df is None or alerts_df.empty or "NowAction" not in alerts_df.columns:
            return alerts_df
        tracked_symbols = self._load_tracked_portfolio_symbols()
        if not tracked_symbols:
            sell_mask = alerts_df["NowAction"].astype(str).eq("Sell / Exit")
            return alerts_df[~sell_mask].copy()
        symbol_series = alerts_df["Symbol"].astype(str).str.upper() if "Symbol" in alerts_df.columns else pd.Series("", index=alerts_df.index)
        sell_mask = alerts_df["NowAction"].astype(str).eq("Sell / Exit")
        keep_mask = (~sell_mask) | (symbol_series.isin(tracked_symbols))
        return alerts_df[keep_mask].copy()

    def _build_plain_event_lines(self, alerts_df: pd.DataFrame, max_alerts: int = 8) -> List[str]:
        """Build short single-line alert summaries for desktop and log output."""
        event_lines: List[str] = []
        for _, row in alerts_df.head(max_alerts).iterrows():
            qty = int(float(row["SuggestedQty"])) if str(row["SuggestedQty"]).strip() else 0
            qty_text = f" | qty {qty}" if qty > 0 else ""
            action = str(row["NowAction"])
            prefix = "🚀 " if action == "Momentum Buy" else "🔥 " if action == "Volume Anomaly" else ""
            event_lines.append(
                f"{prefix}[{action}] {row['Symbol']} @ {self._fmt_price(row['PriceNow'])} | "
                f"buy>{self._fmt_price(row['BuyAbove'])} | stop<{self._fmt_price(row['SellIfBelow'])}{qty_text}"
            )
        return event_lines

    @staticmethod
    def _simplify_checks_text(checks: str) -> str:
        """Convert internal check flags to a readable quality summary."""
        cl = checks.lower()
        positives = []
        if "strong confidence" in cl:
            positives.append("high confidence")
        elif "confidence ok" in cl:
            positives.append("good confidence")
        if "strong volume" in cl:
            positives.append("strong volume")
        elif "volume ok" in cl:
            positives.append("good volume")
        if "strong liquidity" in cl:
            positives.append("very liquid")
        elif "liquidity ok" in cl:
            positives.append("liquid")
        if "near trigger" in cl:
            positives.append("near buy price")
        if not positives:
            return checks  # fallback to original
        return ", ".join(positives)

    @staticmethod
    def _simplify_why_text(why: str) -> str:
        """Translate technical indicator jargon into plain English."""
        import re
        parts = [p.strip() for p in why.split("|")]
        simple = []
        for p in parts:
            pl = p.lower().strip()
            if not pl:
                continue
            # RSI
            m = re.search(r"rsi oversold \((\d+\.?\d*)\)", pl)
            if m:
                simple.append(f"Stock is heavily oversold (RSI {m.group(1)}) — bounce likely")
                continue
            m = re.search(r"rsi (?:approaching|near) oversold \((\d+\.?\d*)\)", pl)
            if m:
                simple.append(f"Stock is getting cheap (RSI {m.group(1)})")
                continue
            m = re.search(r"rsi overbought \((\d+\.?\d*)\)", pl)
            if m:
                simple.append(f"Stock is overheated (RSI {m.group(1)}) — may pull back")
                continue
            m = re.search(r"rsi (?:approaching|near) overbought \((\d+\.?\d*)\)", pl)
            if m:
                simple.append(f"Stock is gaining momentum (RSI {m.group(1)})")
                continue
            # ATR trailing stop
            if "price above atr trailing stop" in pl:
                simple.append("Uptrend intact — price holding above safety level")
                continue
            if "price below atr trailing stop" in pl:
                simple.append("Uptrend broken — price fell below safety level")
                continue
            # Volume
            m = re.search(r"volume surge \((\d+\.?\d*)x\) with price increase", pl)
            if m:
                simple.append(f"Big buyers stepping in ({m.group(1)}x normal volume)")
                continue
            m = re.search(r"above average volume \((\d+\.?\d*)x\) with gain", pl)
            if m:
                simple.append(f"More buyers than usual ({m.group(1)}x volume) pushing price up")
                continue
            m = re.search(r"volume surge \((\d+\.?\d*)x\) with price decrease", pl)
            if m:
                simple.append(f"Heavy selling pressure ({m.group(1)}x volume)")
                continue
            m = re.search(r"above average volume \((\d+\.?\d*)x\) with loss", pl)
            if m:
                simple.append(f"More sellers than usual ({m.group(1)}x volume)")
                continue
            # CCI / MFI
            if "cci oversold" in pl:
                simple.append("Momentum indicator says stock is undervalued")
                continue
            if "cci overbought" in pl:
                simple.append("Momentum indicator says stock is overvalued")
                continue
            if "mfi oversold" in pl:
                simple.append("Money flow shows heavy selling exhaustion")
                continue
            if "mfi overbought" in pl:
                simple.append("Money flow shows buying frenzy")
                continue
            # MACD
            if "macd bullish crossover" in pl:
                simple.append("Trend just turned bullish")
                continue
            if "macd bearish crossover" in pl:
                simple.append("Trend just turned bearish")
                continue
            if "macd histogram increasing" in pl:
                simple.append("Bullish momentum building")
                continue
            if "macd histogram decreasing" in pl:
                simple.append("Bearish momentum building")
                continue
            # MA alignment
            if "bullish alignment" in pl:
                simple.append("Strong uptrend — price above all key averages")
                continue
            if "bearish alignment" in pl:
                simple.append("Strong downtrend — price below all key averages")
                continue
            # Golden/Death cross
            if "golden cross" in pl:
                simple.append("Bullish crossover signal — short-term trend beating long-term")
                continue
            if "death cross" in pl:
                simple.append("Bearish crossover — short-term trend falling behind")
                continue
            # Price action
            if "higher highs and higher lows" in pl:
                simple.append("Healthy uptrend — making new highs")
                continue
            if "lower highs and lower lows" in pl:
                simple.append("Downtrend — making new lows")
                continue
            if "higher lows" in pl:
                simple.append("Buyers defending at higher prices — possible reversal")
                continue
            if "lower highs" in pl:
                simple.append("Sellers capping gains — momentum weakening")
                continue
            # Fallback: pass through as-is (for any new indicators)
            simple.append(p.strip())
        return " | ".join(simple) if simple else why

    def _build_discord_monitor_message(self, alerts_df: pd.DataFrame, summary: Dict, max_alerts: int = 3) -> str:
        """Build a phone-friendly Discord webhook message."""
        sections = [
            f"**PKScreener Live Alert**",
            (
                f"Universe `{self.universe}` | Time `{self.run_at_str}` | "
                f"Fresh `{int(summary.get('FreshAlertsThisRun', 0) or 0)}`"
            ),
            (
                f"Scanned `{int(summary.get('ScannedSymbols', 0) or 0)}` | "
                f"Strong Buy Now `{int(summary.get('StrongBuyNowCount', 0) or 0)}` | "
                f"Buy Now `{int(summary.get('BuyNowCount', 0) or 0)}` | "
                f"Sell Now `{int(summary.get('SellNowCount', 0) or 0)}`"
            ),
        ]

        if self.last_guardrail_status is not None and self.last_guardrail_status.buy_paused:
            sections.append(
                "Guardrail `" + "; ".join(self.last_guardrail_status.reasons) + "`"
            )

        for _, row in alerts_df.head(max_alerts).iterrows():
            qty = int(float(row["SuggestedQty"])) if str(row["SuggestedQty"]).strip() else 0
            strength = f"{float(row['SignalStrengthPct']):.1f}%"
            checks = str(row["Checks"]).strip()
            why = str(row["Why"]).strip()
            change_pct = self._to_float(row.get("ChangePct"))
            entry_gap_pct = self._to_float(row.get("EntryGapPct"))
            rsi = self._to_float(row.get("RSI"))
            # Calculate risk:reward for clarity
            try:
                price_now = float(row["PriceNow"])
                stop = float(row["SellIfBelow"])
                t1 = float(row["TakeProfit1"])
                t2 = float(row["TakeProfit2"])
                risk = price_now - stop
                reward1 = t1 - price_now
                reward2 = t2 - price_now
                rr1 = f"{reward1/risk:.1f}" if risk > 0 else "?"
                rr2 = f"{reward2/risk:.1f}" if risk > 0 else "?"
                rr_text = f" | R:R `1:{rr1}` / `1:{rr2}`"
            except (ValueError, ZeroDivisionError):
                rr_text = ""
            # Simplify action label
            action = str(row["NowAction"])
            if action == "Strong Buy Now":
                action_emoji = "🟢🟢"
            elif action == "Buy Now":
                action_emoji = "🟢"
            elif action == "Momentum Buy":
                action_emoji = "🚀"
            elif action == "Volume Anomaly":
                action_emoji = "🔥"
            elif action.startswith("🔥") or action.startswith("⚡") or action.startswith("👀"):
                # Anomaly buy candidates — green dot for "go/buy"
                action_emoji = "🟢"
            elif action == "Sell / Exit":
                action_emoji = "🔴"
            else:
                action_emoji = "🔴"
            # Show day % change so user immediately sees how much it has already moved
            try:
                day_pct = float(row.get("ChangePct", 0) or 0)
                day_pct_text = f" (Day {day_pct:+.1f}%)" if day_pct else ""
            except (TypeError, ValueError):
                day_pct_text = ""
            # Buy zone: trigger to trigger × 1.015 (1.5% chase tolerance).
            # Above this ceiling the move has run too far to enter safely.
            try:
                _buy_above_val = float(row.get("BuyAbove", 0) or 0)
            except (TypeError, ValueError):
                _buy_above_val = 0.0
            _buy_ceiling_val = _buy_above_val * 1.015 if _buy_above_val > 0 else 0.0
            if _buy_above_val > 0:
                buy_zone_line = (
                    f"🎯 **BUY ZONE**: ₹{self._fmt_price(_buy_above_val)} – ₹{self._fmt_price(_buy_ceiling_val)}  "
                    f"(skip if price > ₹{self._fmt_price(_buy_ceiling_val)})"
                )
            else:
                buy_zone_line = f"🎯 **BUY ZONE**: ₹{self._fmt_price(row['BuyAbove'])}"
            block_lines = [
                f"{action_emoji} **{action}** `{row['Symbol']}` — Strength `{strength}`",
                buy_zone_line,
                f"At alert: ₹{self._fmt_price(row['PriceNow'])}{day_pct_text}",
                f"Stop Loss `{self._fmt_price(row['SellIfBelow'])}` | Target 1 `{self._fmt_price(row['TakeProfit1'])}` | Target 2 `{self._fmt_price(row['TakeProfit2'])}`{rr_text}",
                f"Qty `{qty}`",
            ]
            if checks:
                block_lines.append(f"Quality: `{self._simplify_checks_text(checks)}`")
            if why:
                block_lines.append(f"Why: {self._simplify_why_text(why)}")
            sections.append("\n".join(block_lines))

        return "\n\n".join(sections)[:1900]

    def _fmt_price(self, value) -> str:
        """Format numeric prices consistently for alerts."""
        try:
            return f"{float(value):.2f}"
        except Exception:
            return str(value)

    def _send_desktop_alert(self, title: str, message: str) -> None:
        """Show a desktop alert on Windows using built-in PowerShell support."""
        try:
            safe_title = str(title).replace("'", "''")
            safe_message = str(message).replace("'", "''")
            command = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$notify = New-Object System.Windows.Forms.NotifyIcon; "
                "$notify.Icon = [System.Drawing.SystemIcons]::Information; "
                f"$notify.BalloonTipTitle = '{safe_title}'; "
                f"$notify.BalloonTipText = '{safe_message}'; "
                "$notify.Visible = $true; "
                "$notify.ShowBalloonTip(5000); "
                "Start-Sleep -Seconds 6; "
                "$notify.Dispose();"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            default_logger().debug(f"Desktop alert failed: {exc}", exc_info=True)

    def _send_telegram_alert(self, message: str) -> None:
        """Send a Telegram text alert when bot credentials are configured."""
        token = os.environ.get("TOKEN", "").strip()
        chat_id = os.environ.get("CHAT_ID", "").strip()
        if not token or not chat_id:
            try:
                from PKDevTools.classes.Environment import PKEnvironment

                env = PKEnvironment()
                token = str(env.allSecrets.get("TOKEN", token)).strip()
                chat_id = str(env.allSecrets.get("CHAT_ID", chat_id)).strip()
            except Exception:
                pass

        if not token or not chat_id:
            OutputControls().printOutput(
                "  [+] Telegram alerts skipped because TOKEN / CHAT_ID are not configured."
            )
            return

        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message},
                timeout=10,
            )
        except Exception as exc:
            default_logger().debug(f"Telegram alert failed: {exc}", exc_info=True)

    def _send_discord_alert(self, message: str) -> None:
        """Send a Discord webhook alert when configured."""
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook_url:
            try:
                from PKDevTools.classes.Environment import PKEnvironment

                env = PKEnvironment()
                webhook_url = str(env.allSecrets.get("DISCORD_WEBHOOK_URL", webhook_url)).strip()
            except Exception:
                pass

        if not webhook_url:
            OutputControls().printOutput(
                "  [+] Discord alerts skipped because DISCORD_WEBHOOK_URL is not configured."
            )
            return

        try:
            requests.post(
                webhook_url,
                json={"content": message[:1900]},
                timeout=10,
            )
        except Exception as exc:
            default_logger().debug(f"Discord alert failed: {exc}", exc_info=True)

    def _normalize_alert_channels(self, value: str) -> Set[str]:
        """Normalize requested alert channels into a set."""
        normalized = str(value or "none").strip().lower()
        if normalized in ["", "none"]:
            return set()
        if normalized == "both":
            return {"desktop", "telegram"}

        separators = [",", "+", " "]
        pieces = [normalized]
        for separator in separators:
            if separator in normalized:
                pieces = [
                    part.strip()
                    for part in normalized.replace("+", ",").replace(" ", ",").split(",")
                    if part.strip()
                ]
                break

        allowed = {"desktop", "telegram", "discord"}
        return {piece for piece in pieces if piece in allowed}

    def _latest_path(self, name: str) -> str:
        """Return the latest CSV path for a named monitor output."""
        return os.path.join(self.output_dir, f"signalmonitor_{self.universe}_{name}_latest.csv")

    def _dated_path(self, name: str) -> str:
        """Return the dated CSV path for a named monitor output."""
        return os.path.join(self.output_dir, f"signalmonitor_{self.universe}_{name}_{self.timestamp}.csv")

    def _daily_path(self, name: str, date_stamp: str) -> str:
        """Return the daily end-of-day snapshot path (one per trading day)."""
        return os.path.join(self.output_dir, f"signalmonitor_{self.universe}_{name}_{date_stamp}.csv")

    def _purge_old_daily_snapshots(self, keep_days: int = 30) -> None:
        """Delete daily snapshots older than keep_days from the output directory."""
        import glob as _glob
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=keep_days)
        pattern = os.path.join(self.output_dir, f"signalmonitor_{self.universe}_*_????????.csv")
        for path in _glob.glob(pattern):
            fname = os.path.basename(path)
            # Extract 8-digit date stamp from filename
            parts = fname.replace(".csv", "").rsplit("_", 1)
            if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
                try:
                    file_date = datetime.strptime(parts[1], "%Y%m%d")
                    if file_date < cutoff:
                        os.remove(path)
                except Exception:
                    pass

    def _dashboard_latest_path(self) -> str:
        """Return the latest monitor dashboard path."""
        return os.path.join(self.output_dir, f"signalmonitor_{self.universe}_dashboard_latest.html")

    def _dashboard_dated_path(self) -> str:
        """Return the dated monitor dashboard path."""
        return os.path.join(self.output_dir, f"signalmonitor_{self.universe}_dashboard_{self.timestamp}.html")

    def _save_dashboard(
        self,
        latest_path: str,
        dated_path: str,
        summary_df: pd.DataFrame,
        alerts_df: pd.DataFrame,
        simple_df: pd.DataFrame,
    ) -> None:
        """Save an auto-refreshing HTML monitor dashboard."""
        html_text = self._build_dashboard_html(summary_df, alerts_df, simple_df)
        with open(latest_path, "w", encoding="utf-8") as handle:
            handle.write(html_text)
        with open(dated_path, "w", encoding="utf-8") as handle:
            handle.write(html_text)

    def _build_dashboard_html(
        self,
        summary_df: pd.DataFrame,
        alerts_df: pd.DataFrame,
        simple_df: pd.DataFrame,
    ) -> str:
        """Render a lightweight monitor dashboard as a single HTML file."""
        summary_row = (
            summary_df.iloc[0].to_dict()
            if summary_df is not None and not summary_df.empty
            else {}
        )

        strong_buy_df = pd.DataFrame()
        buy_now_df = pd.DataFrame()
        momentum_df = pd.DataFrame()
        sell_now_df = pd.DataFrame()
        if simple_df is not None and not simple_df.empty:
            strong_buy_df = simple_df[simple_df["What To Do"] == "Strong Buy Now"].head(25).copy()
            buy_now_df = simple_df[simple_df["What To Do"] == "Buy Now"].head(25).copy()
            momentum_df = self._build_momentum_board(simple_df).head(25).copy()
            sell_now_df = simple_df[simple_df["What To Do"] == "Sell / Exit"].head(25).copy()

        cards = [
            ("Universe", summary_row.get("Universe", self.universe)),
            ("Scanned", summary_row.get("ScannedSymbols", 0)),
            ("Strong Buy Now", summary_row.get("StrongBuyNowCount", 0)),
            ("Buy Now", summary_row.get("BuyNowCount", 0)),
            ("Momentum Buy", summary_row.get("MomentumBuyCount", 0)),
            ("Sell Now", summary_row.get("SellNowCount", 0)),
            ("Fresh Alerts", summary_row.get("FreshAlertsThisRun", 0)),
        ]

        card_html = "".join(
            [
                f"<div class='card'><div class='label'>{html.escape(str(label))}</div>"
                f"<div class='value'>{html.escape(str(value))}</div></div>"
                for label, value in cards
            ]
        )

        def _table_html(df: pd.DataFrame, empty_message: str) -> str:
            if df is None or df.empty:
                return f"<p class='empty'>{html.escape(empty_message)}</p>"
            return df.to_html(index=False, classes="table", border=0)

        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="20">
  <title>PKScreener Signal Monitor</title>
  <style>
    :root {{
      --bg: #f4f1ea;
      --panel: #fffaf1;
      --ink: #1f2933;
      --muted: #5b6b7a;
      --line: #d9cfbf;
      --accent: #0f766e;
      --good: #166534;
      --bad: #b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Segoe UI", serif;
      background: radial-gradient(circle at top, #fff8eb 0%, var(--bg) 60%);
      color: var(--ink);
    }}
    .wrap {{
      max-width: 1320px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
      font-weight: 700;
    }}
    .sub {{
      color: var(--muted);
      margin-bottom: 24px;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .card, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 12px 30px rgba(68, 54, 30, 0.08);
    }}
    .card {{
      padding: 18px;
    }}
    .card .label {{
      font-size: 13px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .card .value {{
      margin-top: 10px;
      font-size: 28px;
      font-weight: 700;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
    }}
    .panel {{
      padding: 18px;
      overflow: auto;
    }}
    .table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    .table th, .table td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      white-space: nowrap;
    }}
    .table th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.07em;
    }}
    .empty {{
      color: var(--muted);
      margin: 0;
    }}
    @media (max-width: 800px) {{
      .wrap {{
        padding: 16px;
      }}
      .card .value {{
        font-size: 22px;
      }}
      .table {{
        font-size: 13px;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>PKScreener Signal Monitor</h1>
    <div class="sub">Auto-refreshes every 20 seconds. Last checked: {html.escape(str(summary_row.get("LastRunAt", self.run_at_str)))}</div>
    <div class="cards">{card_html}</div>
    <div class="grid">
      <section class="panel">
        <h2>Fresh Alerts</h2>
        {_table_html(alerts_df, "No fresh Strong Buy Now, Buy Now, or Sell / Exit alerts in this cycle.")}
      </section>
      <section class="panel">
        <h2>Strong Buy Now Board</h2>
        {_table_html(strong_buy_df, "No Strong Buy Now signals right now.")}
      </section>
      <section class="panel">
        <h2>Buy Now Board</h2>
        {_table_html(buy_now_df, "No Buy Now signals right now.")}
      </section>
      <section class="panel">
        <h2>Momentum Board</h2>
        {_table_html(momentum_df, "No momentum promotions right now.")}
      </section>
      <section class="panel">
        <h2>Sell Now Board</h2>
        {_table_html(sell_now_df, "No Sell / Exit signals right now.")}
      </section>
    </div>
  </div>
</body>
</html>"""

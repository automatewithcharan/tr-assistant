"""
Continuous monitor-only signal scanning for local PKScreener usage.

This mode continuously scans a universe, saves plain-English signal outputs,
tracks the last seen decision per symbol, and sends alerts only when a symbol
freshly changes into Buy Now or Sell Now.
"""

import html
import os
import subprocess
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import requests

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger

from pkscreener.classes.ActionableReport import ActionableReportRunner
from pkscreener.classes.RiskGuardrails import LiveTradingGuardrails
from pkscreener.classes.SignalPerformanceTracker import SignalPerformanceTracker


class SignalMonitorRunner:
    """Continuously monitor a universe and alert on fresh Buy Now / Sell Now changes."""

    FULL_REFRESH_EVERY_CYCLES = 20
    MEANINGFUL_PRICE_MOVE_PCT = 0.75
    TRIGGER_PROXIMITY_PCT = 1.0
    STREAM_BOOTSTRAP_MAX_SYMBOLS = 600

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
        "LastRunAt",
    ]

    STATE_COLUMNS = [
        "Symbol",
        "NowAction",
        "PriceNow",
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
    ]

    ALERT_COLUMNS = [
        "When",
        "Symbol",
        "PreviousAction",
        "NowAction",
        "PriceNow",
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
        )
        self.last_guardrail_status = None

    def run(
        self,
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """Run one monitor cycle and persist the current outputs."""
        self.run_at = PKDateUtilities.currentDateTime()
        self.run_at_str = self.run_at.strftime("%Y-%m-%d %H:%M:%S")
        self.timestamp = self.run_at.strftime("%Y%m%d_%H%M%S")
        self.performance_tracker = SignalPerformanceTracker(
            config_manager=self.config_manager,
            universe=self.universe,
            use_live_kite=self.use_live_kite,
        )
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
        summary_df = self._build_summary_from_simple(merged_simple_df, total, len(alerts_df))
        _, scorecard_df = self.performance_tracker.register_and_update(alerts_df)

        self._save_outputs(report_df, actionable_df, merged_simple_df, merged_state_df, alerts_df, summary_df)
        self._print_summary(summary_df, merged_simple_df, alerts_df, scorecard_df)
        self._send_alerts(alerts_df, summary_df)
        self._cycle_number += 1
        return report_df, merged_simple_df, alerts_df, summary_df

    def _select_symbols_for_cycle(self, symbols: List[str], previous_simple_df: pd.DataFrame) -> List[str]:
        """Return either the full universe or only changed live-tick symbols for this cycle."""
        if not self.use_live_kite:
            return symbols

        self.actionable_runner._refresh_live_ticks()
        current_ticks = self.actionable_runner.live_ticks or {}
        if not current_ticks:
            return symbols
        if self._cycle_number <= 0:
            if self.streaming_mode:
                bootstrap_symbols = self._bootstrap_symbols_for_first_cycle(
                    symbols,
                    current_ticks,
                    previous_simple_df,
                )
                if bootstrap_symbols:
                    return bootstrap_symbols
            return symbols

        previous_ticks = self._previous_live_ticks_by_symbol
        self._previous_live_ticks_by_symbol = {
            str(symbol).strip().upper(): tick
            for symbol, tick in current_ticks.items()
        }
        if not previous_ticks:
            return symbols
        if self._cycle_number % self.FULL_REFRESH_EVERY_CYCLES == 0:
            return symbols

        previous_simple_map = self._simple_row_map(previous_simple_df)
        changed_symbols: List[str] = []
        for symbol in symbols:
            normalized_symbol = str(symbol).strip().upper()
            current_tick = current_ticks.get(normalized_symbol)
            if not current_tick:
                continue
            previous_tick = previous_ticks.get(normalized_symbol)
            if previous_tick is None:
                changed_symbols.append(normalized_symbol)
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
                changed_symbols.append(normalized_symbol)

        return changed_symbols or symbols

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
        return {
            str(row["Stock"]).strip().upper(): row.to_dict()
            for _, row in prepared.iterrows()
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
                    "LastRunAt": self.run_at_str,
                }
            ],
            columns=self.SUMMARY_COLUMNS,
        )

    def _build_state(self, simple_df: pd.DataFrame) -> pd.DataFrame:
        """Build a compact state table used for change detection."""
        if simple_df is None or simple_df.empty:
            return pd.DataFrame(columns=self.STATE_COLUMNS)

        state_df = pd.DataFrame(
            {
                "Symbol": simple_df["Stock"],
                "NowAction": simple_df["What To Do"],
                "PriceNow": simple_df["Price Now"],
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
            }
        )
        return state_df[self.STATE_COLUMNS].copy()

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

        for column in self.STATE_COLUMNS:
            if column not in state_df.columns:
                state_df[column] = ""
        return state_df[self.STATE_COLUMNS].copy()

    def _build_alerts(self, previous_state_df: pd.DataFrame, state_df: pd.DataFrame) -> pd.DataFrame:
        """Return newly actionable rows that changed into Buy Now or Sell Now."""
        if state_df is None or state_df.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        previous_map = {}
        if previous_state_df is not None and not previous_state_df.empty:
            previous_map = {
                str(row["Symbol"]).strip().upper(): str(row["NowAction"]).strip()
                for _, row in previous_state_df.iterrows()
            }

        alert_rows = []
        allowed_buy_actions = ["Strong Buy Now"] if self.strong_buy_only else ["Strong Buy Now", "Buy Now"]
        for _, row in state_df.iterrows():
            symbol = str(row["Symbol"]).strip().upper()
            now_action = str(row["NowAction"]).strip()
            if now_action not in (allowed_buy_actions + ["Sell / Exit"]):
                continue

            previous_action = previous_map.get(symbol, "")
            if previous_action == now_action:
                continue

            alert_rows.append(
                {
                    "When": self.run_at_str,
                    "Symbol": symbol,
                    "PreviousAction": previous_action or "None",
                    "NowAction": now_action,
                    "PriceNow": row["PriceNow"],
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

        alerts_df = pd.DataFrame(alert_rows, columns=self.ALERT_COLUMNS)
        if not alerts_df.empty:
            alerts_df.sort_values(
                by=["NowAction", "SignalStrengthPct", "Symbol"],
                ascending=[True, False, True],
                inplace=True,
            )
        return alerts_df[self.ALERT_COLUMNS].copy()

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
        """Persist CSV and HTML monitor outputs."""
        outputs = {
            "signals_latest": self._latest_path("signals"),
            "signals_dated": self._dated_path("signals"),
            "signals_simple_latest": self._latest_path("signals_simple"),
            "signals_simple_dated": self._dated_path("signals_simple"),
            "signals_momentum_latest": self._latest_path("signals_momentum"),
            "signals_momentum_dated": self._dated_path("signals_momentum"),
            "actionable_latest": self._latest_path("actionable"),
            "actionable_dated": self._dated_path("actionable"),
            "summary_latest": self._latest_path("summary"),
            "summary_dated": self._dated_path("summary"),
            "state_latest": self._latest_path("state"),
            "state_dated": self._dated_path("state"),
            "alerts_latest": self._latest_path("alerts"),
            "alerts_dated": self._dated_path("alerts"),
            "dashboard_latest": self._dashboard_latest_path(),
            "dashboard_dated": self._dashboard_dated_path(),
        }

        report_to_save = report_df.copy()
        actionable_to_save = actionable_df.copy()
        report_to_save.drop(columns=["ActionRank", "NowActionRank"], inplace=True, errors="ignore")
        actionable_to_save.drop(columns=["ActionRank", "NowActionRank"], inplace=True, errors="ignore")

        report_to_save.to_csv(outputs["signals_latest"], index=False)
        report_to_save.to_csv(outputs["signals_dated"], index=False)
        simple_df.to_csv(outputs["signals_simple_latest"], index=False)
        simple_df.to_csv(outputs["signals_simple_dated"], index=False)
        momentum_df = self._build_momentum_board(simple_df)
        momentum_df.to_csv(outputs["signals_momentum_latest"], index=False)
        momentum_df.to_csv(outputs["signals_momentum_dated"], index=False)
        actionable_to_save.to_csv(outputs["actionable_latest"], index=False)
        actionable_to_save.to_csv(outputs["actionable_dated"], index=False)
        summary_df.to_csv(outputs["summary_latest"], index=False)
        summary_df.to_csv(outputs["summary_dated"], index=False)
        state_df.to_csv(outputs["state_latest"], index=False)
        state_df.to_csv(outputs["state_dated"], index=False)
        alerts_df.to_csv(outputs["alerts_latest"], index=False)
        alerts_df.to_csv(outputs["alerts_dated"], index=False)
        self._save_dashboard(
            latest_path=outputs["dashboard_latest"],
            dated_path=outputs["dashboard_dated"],
            summary_df=summary_df,
            alerts_df=alerts_df,
            simple_df=simple_df,
        )

        OutputControls().printOutput(f"  [+] Saved monitor summary: {outputs['summary_latest']}")
        OutputControls().printOutput(f"  [+] Saved monitor board: {outputs['signals_simple_latest']}")
        OutputControls().printOutput(f"  [+] Saved momentum board: {outputs['signals_momentum_latest']}")
        OutputControls().printOutput(f"  [+] Saved monitor alerts: {outputs['alerts_latest']}")
        OutputControls().printOutput(f"  [+] Saved monitor dashboard: {outputs['dashboard_latest']}")

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
            f"Fresh alerts {summary.get('FreshAlertsThisRun', 0)}"
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

    def _send_alerts(self, alerts_df: pd.DataFrame, summary_df: pd.DataFrame) -> None:
        """Send desktop / Telegram / Discord alerts for fresh Buy Now / Sell Now changes."""
        if not self.alert_channels or alerts_df is None or alerts_df.empty:
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

    def _build_plain_event_lines(self, alerts_df: pd.DataFrame, max_alerts: int = 8) -> List[str]:
        """Build short single-line alert summaries for desktop and log output."""
        event_lines: List[str] = []
        for _, row in alerts_df.head(max_alerts).iterrows():
            qty = int(float(row["SuggestedQty"])) if str(row["SuggestedQty"]).strip() else 0
            qty_text = f" | qty {qty}" if qty > 0 else ""
            event_lines.append(
                f"[{row['NowAction']}] {row['Symbol']} @ {self._fmt_price(row['PriceNow'])} | "
                f"buy>{self._fmt_price(row['BuyAbove'])} | stop<{self._fmt_price(row['SellIfBelow'])}{qty_text}"
            )
        return event_lines

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
            block_lines = [
                f"**{row['NowAction']}** `{row['Symbol']}`",
                f"Price `{self._fmt_price(row['PriceNow'])}` | Buy Above `{self._fmt_price(row['BuyAbove'])}`",
                f"Stop `{self._fmt_price(row['SellIfBelow'])}` | Target 1 `{self._fmt_price(row['TakeProfit1'])}` | Target 2 `{self._fmt_price(row['TakeProfit2'])}`",
                f"Qty `{qty}` | Strength `{strength}`",
            ]
            if checks:
                block_lines.append(f"Checks: `{checks}`")
            if why:
                block_lines.append(f"Why: {why}")
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

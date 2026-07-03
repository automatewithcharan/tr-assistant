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
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
    MAX_BUY_DAILY_CHANGE_PCT = 9.0    # Hard cap: anything up 9%+ has already made its move
    MIN_MOMENTUM_EXTENSION_PCT = 3.0  # Minimum daily gain to qualify as a momentum extension alert (3% → 20% chaser)
    RUNNER_CONTINUATION_MIN_PCT = 5.0
    RUNNER_CONTINUATION_MAX_PCT = 12.5
    RUNNER_CONTINUATION_MAX_PULLBACK_PCT = 1.5
    EARLY_BREAKOUT_MIN_PCT = float(os.environ.get("SIGNALMONITOR_EARLY_BREAKOUT_MIN_PCT", "1.0") or 1.0)
    EARLY_BREAKOUT_MAX_PCT = float(os.environ.get("SIGNALMONITOR_EARLY_BREAKOUT_MAX_PCT", "3.5") or 3.5)
    EARLY_BREAKOUT_MIN_VOLUME_RATIO = float(os.environ.get("SIGNALMONITOR_EARLY_BREAKOUT_MIN_VOLUME_RATIO", "1.3") or 1.3)
    EARLY_BREAKOUT_MAX_PER_CYCLE = int(os.environ.get("SIGNALMONITOR_EARLY_BREAKOUT_MAX_PER_CYCLE", "4") or 4)
    TOP_GAINER_RADAR_MIN_PCT = float(os.environ.get("SIGNALMONITOR_TOP_GAINER_RADAR_MIN_PCT", "1.5") or 1.5)
    TOP_GAINER_RADAR_MAX_PCT = float(os.environ.get("SIGNALMONITOR_TOP_GAINER_RADAR_MAX_PCT", "3.5") or 3.5)
    TOP_GAINER_RADAR_MAX_PER_CYCLE = int(os.environ.get("SIGNALMONITOR_TOP_GAINER_RADAR_MAX_PER_CYCLE", "1") or 1)
    LEADER_IGNITION_MIN_OPEN_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MIN_OPEN_PCT", "0.5") or 0.5)
    LEADER_IGNITION_MAX_OPEN_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MAX_OPEN_PCT", "2.8") or 2.8)
    LEADER_IGNITION_MIN_DAY_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MIN_DAY_PCT", "0.5") or 0.5)
    LEADER_IGNITION_MAX_DAY_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MAX_DAY_PCT", "4.5") or 4.5)
    LEADER_IGNITION_MAX_PULLBACK_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MAX_PULLBACK_PCT", "0.8") or 0.8)
    LEADER_IGNITION_MIN_BS_RATIO = float(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MIN_BS_RATIO", "1.4") or 1.4)
    LEADER_IGNITION_MIN_VOLUME_DELTA_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MIN_VOLUME_DELTA_PCT", "0.6") or 0.6)
    LEADER_IGNITION_MIN_TRADED_VALUE = float(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MIN_TRADED_VALUE", "10000000") or 10000000)
    LEADER_IGNITION_MAX_PER_CYCLE = int(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MAX_PER_CYCLE", "8") or 8)
    LEADER_IGNITION_MAX_PER_DAY = int(os.environ.get("SIGNALMONITOR_LEADER_IGNITION_MAX_PER_DAY", "120") or 120)
    LEADER_PULLBACK_MIN_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_PULLBACK_MIN_PCT", "0.4") or 0.4)
    LEADER_PULLBACK_MAX_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_PULLBACK_MAX_PCT", "2.0") or 2.0)
    LEADER_RECLAIM_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_RECLAIM_PCT", "0.35") or 0.35)
    LEADER_RECLAIM_MAX_PULLBACK_PCT = float(os.environ.get("SIGNALMONITOR_LEADER_RECLAIM_MAX_PULLBACK_PCT", "1.0") or 1.0)
    LEADER_CANDIDATE_TTL_MINUTES = float(os.environ.get("SIGNALMONITOR_LEADER_CANDIDATE_TTL_MINUTES", "45") or 45)
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
        self.enable_standalone_ob_alerts = str(os.environ.get("SIGNALMONITOR_ENABLE_STANDALONE_OB", "0") or "0").strip().lower() in {"1", "true", "yes", "y", "on"}
        self.enable_early_breakout_alerts = str(os.environ.get("SIGNALMONITOR_ENABLE_EARLY_BREAKOUT", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.enable_leader_ignition = self._env_truthy("SIGNALMONITOR_ENABLE_LEADER_IGNITION", "1")
        self.leader_ignition_discord_alerts = self._env_truthy("SIGNALMONITOR_LEADER_IGNITION_DISCORD", "0")
        self.enable_intraday_goal_mode = str(os.environ.get("SIGNALMONITOR_INTRADAY_GOAL_MODE", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.goal_max_trades_per_day = max(1, int(float(os.environ.get("SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY", "50") or "50")))
        self.goal_stop_pct = max(0.25, float(os.environ.get("SIGNALMONITOR_GOAL_STOP_PCT", "1.0") or "1.0"))
        self.goal_target1_pct = max(0.5, float(os.environ.get("SIGNALMONITOR_GOAL_TARGET1_PCT", "1.5") or "1.5"))
        self.goal_target2_pct = max(self.goal_target1_pct, float(os.environ.get("SIGNALMONITOR_GOAL_TARGET2_PCT", "3.0") or "3.0"))
        self.goal_anomaly_only = str(os.environ.get("SIGNALMONITOR_GOAL_ANOMALY_ONLY", "0") or "0").strip().lower() in {"1", "true", "yes", "y", "on"}
        self.goal_allow_direct_buy = str(os.environ.get("SIGNALMONITOR_GOAL_ALLOW_DIRECT_BUY", "1") or "1").strip().lower() not in {"0", "false", "no", "n", "off"}
        self.radar_max_alerts_per_day = max(0, int(float(os.environ.get("SIGNALMONITOR_RADAR_MAX_ALERTS_PER_DAY", "3") or "3")))
        self.radar_min_strength = max(0.0, float(os.environ.get("SIGNALMONITOR_RADAR_MIN_STRENGTH", "38") or "38"))
        self.radar_min_volume_ratio = max(0.0, float(os.environ.get("SIGNALMONITOR_RADAR_MIN_VOLUME_RATIO", "1.0") or "1.0"))
        self.radar_min_bs_ratio = max(0.0, float(os.environ.get("SIGNALMONITOR_RADAR_MIN_BS_RATIO", "1.2") or "1.2"))
        self.monitor_heartbeat_minutes = max(0.0, float(os.environ.get("SIGNALMONITOR_HEARTBEAT_MINUTES", "10") or "10"))
        self.autotrade_enabled = self._env_truthy("SIGNALMONITOR_AUTOTRADE_ENABLED", "0")
        self.autotrade_real_orders = self._env_truthy("SIGNALMONITOR_AUTOTRADE_REAL_ORDERS", "0")
        self.autotrade_buy_enabled = self._env_truthy("KITE_AUTO_BUY", "0")
        self.autotrade_sell_enabled = self._env_truthy("KITE_AUTO_SELL", "0")
        self.autotrade_confirm_pct = max(0.05, float(os.environ.get("SIGNALMONITOR_AUTOTRADE_CONFIRM_PCT", "0.25") or "0.25"))
        self.autotrade_target_pct = max(0.1, float(os.environ.get("SIGNALMONITOR_AUTOTRADE_TARGET_PCT", "0.7") or "0.7"))
        self.autotrade_stop_pct = max(0.25, float(os.environ.get("SIGNALMONITOR_AUTOTRADE_STOP_PCT", "2.0") or "2.0"))
        self.autotrade_entry_window_minutes = max(1.0, float(os.environ.get("SIGNALMONITOR_AUTOTRADE_ENTRY_WINDOW_MINUTES", "15") or "15"))
        self.autotrade_max_entry_slippage_pct = max(0.0, float(os.environ.get("SIGNALMONITOR_AUTOTRADE_MAX_ENTRY_SLIPPAGE_PCT", "0.4") or "0.4"))
        self.autotrade_limit_buffer_pct = max(0.0, float(os.environ.get("SIGNALMONITOR_AUTOTRADE_LIMIT_BUFFER_PCT", "0.10") or "0.10"))
        self.autotrade_strategy = str(os.environ.get("SIGNALMONITOR_AUTOTRADE_STRATEGY", "grade_b_confirmation") or "grade_b_confirmation").strip()
        self._autotrade_state_path = os.path.join(self.output_dir, "signalmonitor_autotrade_state.json")
        self._autotrade_journal_path = os.path.join(self.output_dir, "autotrade_event_journal.csv")
        self._autotrade_journal_jsonl_path = os.path.join(self.output_dir, "autotrade_event_journal.jsonl")
        self._autotrade_candidates: Dict[str, Dict[str, Any]] = self._load_autotrade_state()
        self._autotrade_order_manager = None
        self._leader_ignition_state_path = os.path.join(self.output_dir, "signalmonitor_leader_ignition_state.json")
        self._leader_candidates: Dict[str, Dict[str, Any]] = self._load_leader_ignition_state()
        self._leader_last_ticks_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._last_discord_heartbeat_ts = 0.0
        self._last_goal_suppressed_reasons: List[str] = []
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
        # Wall-clock tracking: first time we alert a symbol today -> skip repeat caps.
        # Persisted because the live monitor is often launched by schedulers where
        # process memory resets between cycles.
        self._daily_alerted: Dict[str, str] = self._load_daily_alerted_map()
        # Order book history per symbol: list of (timestamp, b/s ratio).
        # Rolling 5-minute window for shift detection.
        self._ob_history: Dict[str, List[Tuple[datetime, float]]] = {}
        # Track which symbols already fired an order-book imbalance alert today.
        self._ob_alerted_today: Set[str] = set()
        # Track which symbols have been shown in the "Current Buy Now board"
        # section today — each stock only gets surfaced once per day to prevent
        # the repeat-display fatigue (JKPAPER seen 25 times in one day).
        self._board_shown_date: str = self._today_key()
        self._board_shown_today: Set[str] = self._load_daily_symbol_set("buy_board")
        # Runner continuation alerts are intentionally separate from first-entry
        # Buy Now alerts. Each symbol gets one continuation alert per day.
        self._runner_alerted_today: Set[str] = set()
        self._runner_alerted_date: str = ""
        self._early_breakout_alerted_date: str = ""
        self._early_breakout_alerted_today: Set[str] = set()
        self._top_gainer_radar_date: str = ""
        self._top_gainer_radar_today: Set[str] = set()
        self._leader_ignition_date: str = ""
        self._leader_ignition_today: Set[str] = set()
        self._leader_reclaim_date: str = ""
        self._leader_reclaim_today: Set[str] = set()
        self._leader_audit_date: str = ""
        self._leader_audit_seen_today: Set[str] = set()
        # Tick-velocity tracking: rolling 5-min history of (timestamp, vol_per_sec)
        # per symbol. Used to detect when a quiet stock suddenly gets active
        # and boost its scan priority before price has time to confirm.
        self._vol_rate_history: Dict[str, List[Tuple[datetime, float]]] = {}
        # Sector-spread tracking: sectors with 2+ stocks moving >= 3% from prev close
        # become "hot" — all peers in those sectors get priority boost so we catch
        # sector rotations early (e.g. PHARMA spike where MANKIND/BIOCON/LALPATHLAB
        # move together — when 2 light up, scan all 30 PHARMA stocks immediately).
        self._hot_sectors: Set[str] = set()
        self._hot_sectors_refreshed_at: Optional[datetime] = None

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
            leader_ignition_df = self._run_leader_ignition_lane()
            self._send_leader_ignition_shadow_alerts(leader_ignition_df)
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
        leader_ignition_df = self._run_leader_ignition_lane()
        self._send_leader_ignition_shadow_alerts(leader_ignition_df)

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
        self._last_simple_df = merged_simple_df
        alerts_df = self._build_alerts(previous_state_df, merged_state_df)
        early_breakout_alerts_df = self._build_early_breakout_alerts(merged_simple_df)
        if not early_breakout_alerts_df.empty:
            already_alerted = set(alerts_df["Symbol"].astype(str).str.upper()) if not alerts_df.empty else set()
            if already_alerted:
                early_breakout_alerts_df = early_breakout_alerts_df[
                    ~early_breakout_alerts_df["Symbol"].astype(str).str.upper().isin(already_alerted)
                ]
            if not early_breakout_alerts_df.empty:
                alerts_df = pd.concat([alerts_df, early_breakout_alerts_df], ignore_index=True)
        top_gainer_radar_df = self._build_top_gainer_radar_alerts(merged_simple_df)
        if not top_gainer_radar_df.empty:
            already_alerted = set(alerts_df["Symbol"].astype(str).str.upper()) if not alerts_df.empty else set()
            if already_alerted:
                top_gainer_radar_df = top_gainer_radar_df[
                    ~top_gainer_radar_df["Symbol"].astype(str).str.upper().isin(already_alerted)
                ]
            if not top_gainer_radar_df.empty:
                alerts_df = pd.concat([alerts_df, top_gainer_radar_df], ignore_index=True)
        momentum_alerts_df = self._build_momentum_alerts(previous_simple_df, merged_simple_df)
        if not momentum_alerts_df.empty:
            # Drop Momentum Buy rows for symbols already emitted as Buy Now this cycle
            already_alerted = set(alerts_df["Symbol"].str.upper()) if not alerts_df.empty else set()
            momentum_alerts_df = momentum_alerts_df[
                ~momentum_alerts_df["Symbol"].str.upper().isin(already_alerted)
            ]
            if not momentum_alerts_df.empty:
                alerts_df = pd.concat([alerts_df, momentum_alerts_df], ignore_index=True)
        runner_alerts_df = self._build_runner_continuation_alerts(merged_simple_df)
        if not runner_alerts_df.empty:
            already_alerted = set(alerts_df["Symbol"].astype(str).str.upper()) if not alerts_df.empty else set()
            if already_alerted:
                runner_alerts_df = runner_alerts_df[
                    ~runner_alerts_df["Symbol"].astype(str).str.upper().isin(already_alerted)
                ]
            if not runner_alerts_df.empty:
                alerts_df = pd.concat([alerts_df, runner_alerts_df], ignore_index=True)
        anomaly_alerts_df = self._run_anomaly_detection()
        if not anomaly_alerts_df.empty:
            # De-dupe: skip anomaly alerts for symbols already alerted as Buy Now /
            # Momentum Buy this cycle (they'd just spam Discord with the same stock).
            already_alerted_now = set(alerts_df["Symbol"].astype(str).str.upper()) if not alerts_df.empty else set()
            if already_alerted_now:
                anomaly_alerts_df = anomaly_alerts_df[
                    ~anomaly_alerts_df["Symbol"].astype(str).str.upper().isin(already_alerted_now)
                ]
            if not anomaly_alerts_df.empty:
                alerts_df = pd.concat([alerts_df, anomaly_alerts_df], ignore_index=True)

        # Order book imbalance: catches institutional accumulation in real time
        # by tracking B/S ratio velocity (1.5x → 3x+ in 5 min). De-dupe against
        # everything else so we don't repeat a stock already alerted via OHLCV
        # or volume anomaly paths.
        ob_alerts_df = self._run_orderbook_imbalance()
        if not ob_alerts_df.empty:
            alerts_df = self._annotate_ob_confirmations(alerts_df, ob_alerts_df)
            if not self.enable_standalone_ob_alerts:
                OutputControls().printOutput(
                    f"  [+] OB confirmation only: {len(ob_alerts_df)} symbol(s), standalone OB alerts disabled"
                )
                ob_alerts_df = pd.DataFrame(columns=self.ALERT_COLUMNS)
            already = set(alerts_df["Symbol"].astype(str).str.upper()) if not alerts_df.empty else set()
            if already:
                ob_alerts_df = ob_alerts_df[
                    ~ob_alerts_df["Symbol"].astype(str).str.upper().isin(already)
                ]
            if not ob_alerts_df.empty:
                alerts_df = pd.concat([alerts_df, ob_alerts_df], ignore_index=True)
        alerts_df = self._apply_intraday_goal_mode(alerts_df)
        self._run_autotrade_lane(alerts_df)
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
        self._send_monitor_heartbeat(summary_df, alerts_df)
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
        # Refresh hot sectors at most once every 60s — cheap O(N_ticks) scan
        # over current ticks finds sectors with 2+ movers >= 3%.
        self._refresh_hot_sectors(current_ticks)
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
                priority = self._symbol_priority(current_tick, previous_tick, symbol=normalized_symbol)
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

    def _refresh_hot_sectors(self, current_ticks: Dict[str, Dict]) -> None:
        """Identify sectors with 2+ stocks moving >= 3% from prev close.

        These are likely sector rotations — boost priority for all peers
        so we catch the leaders AND laggards within the rotating sector.
        Throttled: only recomputes once per 60s.
        """
        try:
            now = PKDateUtilities.currentDateTime()
        except Exception:
            now = datetime.now()
        if self._hot_sectors_refreshed_at is not None:
            if (now - self._hot_sectors_refreshed_at).total_seconds() < 60:
                return
        try:
            from pkscreener.classes.MarketContext import get_sector
        except Exception:
            return
        sector_movers: Dict[str, int] = {}
        for sym, tick in current_ticks.items():
            try:
                price = float(tick.get("last_price", 0) or 0)
                pc = float(tick.get("close", 0) or 0)
                if pc <= 0 or price <= 0:
                    continue
                day_chg = abs((price - pc) / pc * 100.0)
                if day_chg < 3.0:
                    continue
                vol = float(tick.get("volume_traded", 0) or 0)
                if vol < 50000:
                    continue  # ignore illiquid noise
            except (TypeError, ValueError):
                continue
            sector = get_sector(str(sym).strip().upper())
            if not sector or sector == "UNKNOWN":
                continue
            sector_movers[sector] = sector_movers.get(sector, 0) + 1
        new_hot = {s for s, n in sector_movers.items() if n >= 2}
        if new_hot != self._hot_sectors:
            added = new_hot - self._hot_sectors
            if added:
                OutputControls().printOutput(
                    f"  [+] 🔥 Hot sector(s): {', '.join(sorted(added))} "
                    f"({sum(sector_movers[s] for s in added)} movers >=3%)"
                )
        self._hot_sectors = new_hot
        self._hot_sectors_refreshed_at = now

    def _symbol_priority(self, current_tick: Dict, previous_tick: Dict, symbol: Optional[str] = None) -> float:
        """Compute a sortable priority score for a flagged symbol.

        Higher = process first in the cycle.  Components:
          - abs daily change% (big movers go first)
          - order book imbalance (strong buy pressure = early)
          - volume burst in last 60s (cycle-to-cycle ratio)
          - tick-velocity surge: current vol/sec vs 5-min baseline
            (catches quiet stocks that just turned active — the
            critical "missed by the 40-stock cap" case)
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

        # Volume burst component (cycle-to-cycle delta ratio)
        current_vol = self._to_float(current_tick.get("volume_traded"))
        previous_vol = self._to_float(previous_tick.get("volume_traded")) if previous_tick else 0.0
        if previous_vol > 0 and current_vol > previous_vol:
            burst = (current_vol - previous_vol) / previous_vol
            score += burst * 100.0

        # Hot-sector boost: if this symbol's sector has 2+ peers running today,
        # bump its priority so we scan it before unrelated quiet stocks.
        if symbol is not None and self._hot_sectors:
            try:
                from pkscreener.classes.MarketContext import get_sector
                sym_sector = get_sector(symbol)
                if sym_sector and sym_sector in self._hot_sectors:
                    score += 80.0  # comparable to a strong B/S imbalance signal
            except Exception:
                pass

        # Tick-velocity surge: compare current cycle's vol/sec to the
        # symbol's own 5-min rolling baseline. A previously-quiet stock
        # that just got active scores high here even when its absolute
        # vol burst from previous tick is modest.
        if symbol is not None and previous_vol > 0 and current_vol > previous_vol:
            try:
                now = PKDateUtilities.currentDateTime()
            except Exception:
                now = datetime.now()
            # Estimate this cycle's vol/sec — use received_at delta if
            # available, fall back to nominal 10s scan interval.
            try:
                cur_t = current_tick.get("received_at")
                prev_t = previous_tick.get("received_at") if previous_tick else None
                if isinstance(cur_t, str) and isinstance(prev_t, str):
                    cur_dt = datetime.fromisoformat(cur_t.replace("Z", ""))
                    prev_dt = datetime.fromisoformat(prev_t.replace("Z", ""))
                    elapsed = max(1.0, (cur_dt - prev_dt).total_seconds())
                else:
                    elapsed = 10.0
            except Exception:
                elapsed = 10.0
            cur_rate = (current_vol - previous_vol) / elapsed
            history = self._vol_rate_history.setdefault(symbol, [])
            cutoff = now - timedelta(minutes=5)
            history[:] = [(ts, r) for ts, r in history if ts >= cutoff]
            if len(history) >= 6 and cur_rate > 0:
                baseline = sum(r for _, r in history) / len(history)
                if baseline > 0 and cur_rate > baseline * 2.0:
                    # Cap the multiplier so a single huge spike doesn't
                    # drown out everything else in the cycle.
                    surge_factor = min(10.0, cur_rate / baseline)
                    score += surge_factor * 30.0
            history.append((now, cur_rate))

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

    def _parse_value_rupees(self, value) -> float:
        """Parse display values like '16.99 Cr' or '72.11 L' into rupees."""
        text = str(value or "").strip().replace(",", "")
        if not text:
            return 0.0
        parts = text.split()
        number = self._to_float(parts[0] if parts else text)
        suffix = parts[1].lower() if len(parts) > 1 else ""
        if suffix.startswith("cr"):
            return number * 10_000_000.0
        if suffix.startswith("l"):
            return number * 100_000.0
        return number

    def _load_previous_simple(self) -> pd.DataFrame:
        """Load the last simple board so changed-only cycles can reuse unchanged rows."""
        path = self._latest_path("signals_simple")
        if not os.path.exists(path):
            return pd.DataFrame()
        try:
            simple_df = pd.read_csv(path)
        except Exception as exc:
            default_logger().debug(f"Failed loading monitor simple board: {exc}", exc_info=True)
            return pd.DataFrame()
        return self._drop_stale_previous_rows(simple_df, "Checked At", "simple board")

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

        # Drop stale cross-day rows before merging changed-only live scans.
        # Otherwise the board can show fresh Kite prices on top of yesterday's
        # action/levels/RSI and accidentally alert from mixed data.
        state_df = self._drop_stale_previous_rows(state_df, "CheckedAt", "state")

        for column in self.STATE_COLUMNS:
            if column not in state_df.columns:
                state_df[column] = ""
        return state_df[self.STATE_COLUMNS].copy()

    def _drop_stale_previous_rows(
        self,
        df: pd.DataFrame,
        timestamp_column: str,
        label: str,
    ) -> pd.DataFrame:
        """Keep only rows checked today from a persisted monitor board/state."""
        if df is None or df.empty or timestamp_column not in df.columns:
            return df

        today_str = self.run_at.strftime("%Y-%m-%d")
        checked_dates = df[timestamp_column].astype(str).str[:10]
        stale_mask = checked_dates.ne(today_str)
        if not stale_mask.any():
            return df

        stale_count = int(stale_mask.sum())
        stale_dates = sorted(
            date
            for date in checked_dates[stale_mask].dropna().unique().tolist()
            if date and date.lower() != "nan"
        )
        OutputControls().printOutput(
            f"  [!] Dropping {stale_count} stale {label} row(s) for {self.universe}"
            + (f" from {', '.join(stale_dates[:3])}" if stale_dates else "")
            + " before live merge."
        )
        return df.loc[~stale_mask].copy()

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
                # RSI >= 70 on a repeat alert = overbought, skip
                repeat_rsi = self._to_float(row.get("RSI"))
                if repeat_rsi is not None and repeat_rsi >= 70:
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
                    # Unconditional: a stock up 5%+ post-opening (8%+ in opening
                    # window) has already made its move. Don't alert regardless of
                    # where price sits relative to BuyAbove — entering a 5%+ gapper
                    # is chasing, not a blind-buyable setup.
                    skipped_already_ran.append(
                        f"{symbol} ({change_pct:+.1f}% day — already ran)"
                    )
                    continue

            # Block Buy Now when RSI is at or above pkscreener's own overbought
            # line (~70). If the scanner itself labels the stock "overbought", the
            # Why text says "overheated — may pull back" which directly contradicts
            # a Buy Now action. Only surface stocks with room to run (RSI < 70).
            if now_action in allowed_buy_actions:
                rsi_val = self._to_float(row.get("RSI"))
                if rsi_val is not None and rsi_val >= 70:
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
                        strength = self._to_float(row.get("SignalStrengthPct"))
                        volume_ratio = self._to_float(row.get("VolumeRatio"))
                        rsi_val = self._to_float(row.get("RSI"))
                        change_pct = self._to_float(row.get("ChangePct"))
                        why_text = str(row.get("Why", "") or "").lower()
                        near_high_breakout = (
                            strength >= 40.0
                            and volume_ratio >= 1.3
                            and (rsi_val is None or rsi_val < 70.0)
                            and change_pct <= 5.0
                            and "price above atr trailing stop" in why_text
                        )
                        if not near_high_breakout:
                            skipped_already_ran.append(
                                f"{symbol} (52wk-high ₹{week52_high:.2f} only {gap_to_52wk:.1f}% away)"
                            )
                            continue

            # Suppress when live price has already moved above the buy zone ceiling.
            # EntryGapPct in state can be stale (computed at scan time, not updated
            # by tick refresh), so recompute from live PriceNow vs BuyAbove.
            if now_action in allowed_buy_actions:
                _buy_above = self._to_float(row.get("BuyAbove"))
                _price_now = self._to_float(row.get("PriceNow"))
                if _buy_above > 0 and _price_now > 0:
                    _buy_ceiling = _buy_above * 1.015
                    if _price_now > _buy_ceiling:
                        skipped_already_ran.append(
                            f"{symbol} (price ₹{_price_now:.2f} > ceiling ₹{_buy_ceiling:.2f})"
                        )
                        continue

            # Minimum signal strength: skip weak signals.
            # Reverted to 40% floor on 2026-05-19 after audit of historical
            # alert quality: April scored median 70%, p75 75%; recent week
            # dropped to median 42%, only 3% above 50%. The strength score
            # has drifted DOWNWARD — emitting alerts the system would not
            # have flagged earlier. Lowering the floor surfaces that noise.
            # Better: keep the 40% bar (the historical "good signal" line)
            # and accept fewer alerts during periods of weak setups.
            # The underlying scoring drift is a separate research thread.
            if now_action in allowed_buy_actions:
                strength = self._to_float(row.get("SignalStrengthPct"))
                try:
                    _now_ist_for_strength = PKDateUtilities.currentDateTime()
                except Exception:
                    _now_ist_for_strength = datetime.now()
                _ist_min_strength = _now_ist_for_strength.hour * 60 + _now_ist_for_strength.minute
                _morning_window = (9 * 60 + 15) <= _ist_min_strength <= (10 * 60 + 0)
                _strength_floor = 35.0 if _morning_window else 40.0
                if strength is not None and strength < _strength_floor:
                    skipped_already_ran.append(
                        f"{symbol} (strength {strength:.0f}% < {_strength_floor:.0f}% min)"
                    )
                    continue

            # "Already played out" check: if today's high (from live tick) is more
            # than 2% above the current price, the breakout already played out and
            # reversed. Don't fire — bought into the fade. (Same logic as anomaly
            # path; e.g. MIRCELECTR peaked at 09:28 ₹44.75, fired at 11:29 ₹42.30
            # = 5.8% below peak, sell pressure 2.5x buy pressure.)
            if now_action in allowed_buy_actions:
                _live_ticks_bn = self.actionable_runner.live_ticks or {}
                _bn_tick = _live_ticks_bn.get(symbol, {})
                _bn_day_high = self._to_float(_bn_tick.get("high", 0))
                _bn_price = self._to_float(row.get("PriceNow"))
                if _bn_day_high > 0 and _bn_price > 0:
                    _bn_pullback = (_bn_day_high - _bn_price) / _bn_price * 100.0
                    if _bn_pullback > 2.0:
                        skipped_already_ran.append(
                            f"{symbol} (pulled back {_bn_pullback:.1f}% from day high ₹{_bn_day_high:.2f})"
                        )
                        continue

            # Suppress when budget can't afford even one share. 139 historical
            # alerts fired with Qty 0 (e.g., ESABINDIA at ₹5174 with ₹10k budget).
            # A zero-qty alert is useless — user can't act on it.
            if now_action in allowed_buy_actions:
                _qty_check = self._to_float(row.get("SuggestedQty"))
                if _qty_check is None or _qty_check < 1:
                    skipped_already_ran.append(
                        f"{symbol} (budget can't afford 1 share)"
                    )
                    continue

            checks_text = str(row["Checks"] or "")
            if now_action in allowed_buy_actions:
                volume_ratio = self._to_float(row.get("VolumeRatio"))
                if volume_ratio > 0 and "vol " not in checks_text.lower():
                    checks_text = f"{checks_text} | Vol {volume_ratio:.1f}x" if checks_text else f"Vol {volume_ratio:.1f}x"

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
                    "Checks": checks_text,
                    "Why": row["Why"],
                }
            )
            if now_action in allowed_buy_actions:
                self._mark_daily_alerted(symbol, today_str)
                if not already_alerted_today:
                    first_alerts_this_cycle += 1

        if skipped_already_ran:
            OutputControls().printOutput(
                f"  [!] Suppressed {len(skipped_already_ran)} buy alert(s) — entry gate filtered "
                f"day gain / RSI / strength / resistance / pullback risk: "
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

    def _today_key(self) -> str:
        try:
            run_at = getattr(self, "run_at", None)
            if run_at is not None:
                return run_at.strftime("%Y-%m-%d")
            return PKDateUtilities.currentDateTime().strftime("%Y-%m-%d")
        except Exception:
            return datetime.now().strftime("%Y-%m-%d")

    def _daily_symbol_path(self, kind: str, today: Optional[str] = None) -> str:
        output_dir = getattr(self, "output_dir", "") or Archiver.get_user_data_dir()
        safe_kind = str(kind or "symbols").strip().lower().replace(" ", "_")
        day = today or self._today_key()
        return os.path.join(output_dir, f"signalmonitor_{safe_kind}_{day}.txt")

    def _load_daily_symbol_set(self, kind: str, today: Optional[str] = None) -> Set[str]:
        path = self._daily_symbol_path(kind, today=today)
        if not os.path.isfile(path):
            return set()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return {line.strip().upper() for line in handle if line.strip()}
        except Exception:
            return set()

    def _append_daily_symbol(self, kind: str, symbol: str, today: Optional[str] = None) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        try:
            path = self._daily_symbol_path(kind, today=today)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(sym + "\n")
        except Exception:
            default_logger().debug(
                "Failed to persist SignalMonitor %s symbol %s", kind, sym, exc_info=True
            )

    def _load_daily_alerted_map(self) -> Dict[str, str]:
        today = self._today_key()
        return {symbol: today for symbol in self._load_daily_symbol_set("alerted_buys", today=today)}

    def _mark_daily_alerted(self, symbol: str, today: Optional[str] = None) -> None:
        sym = str(symbol or "").strip().upper()
        day = today or self._today_key()
        if not sym:
            return
        if self._daily_alerted.get(sym) == day:
            return
        self._daily_alerted[sym] = day
        self._append_daily_symbol("alerted_buys", sym, today=day)

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
                if count < 1000:
                    OutputControls().printOutput(
                        f"  [!] Anomaly detector coverage low: {count} profiles for universe `{self.universe}`"
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
            OutputControls().printOutput("  [!] Anomaly detector skipped — no live Kite ticks loaded")
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        try:
            candidates = self._anomaly_detector.update_from_ticks(
                current_ticks, market_context=self._market_context
            )
        except Exception as exc:
            default_logger().warning(f"AnomalyDetector update failed: {exc}")
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        if not candidates:
            if int(getattr(self, "_cycle_number", 0) or 0) <= 1 or int(getattr(self, "_cycle_number", 0) or 0) % 20 == 0:
                OutputControls().printOutput(
                    f"  [+] Anomaly detector: 0 candidates from {len(current_ticks)} live ticks"
                )
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
        # 15:15 IST cutoff (was 14:45). Anomaly alerts during the last 45
        # minutes were being suppressed entirely, including high-quality
        # Tier C signals. The "no time to act" rationale is overly strict
        # — many anomaly setups play out in 15-20 minutes. Tightened to the
        # final 15 minutes only (15:15-15:30 IST), where exit liquidity
        # genuinely deteriorates.
        _late_day_cutoff_min = 15 * 60 + 15  # 15:15 IST
        if _ist_minute > _late_day_cutoff_min:
            OutputControls().printOutput(
                f"  [!] Anomaly batch skipped — too late ({_now_ist.strftime('%H:%M')} IST > 15:15)"
            )
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        # Look up live ticks once for day-high comparisons.
        _live_ticks_for_anomaly = self.actionable_runner.live_ticks or {}
        for c in candidates:
            # Compute day-high pullback up front — used for both lane logic and
            # the "already played out" skip below.
            _tick = _live_ticks_for_anomaly.get(str(c.symbol).strip().upper(), {})
            _day_high = float(_tick.get("high", 0) or 0)
            _pullback_pct = (
                (_day_high - c.last_price) / c.last_price * 100.0
                if _day_high > 0 and c.last_price > 0
                else 0.0
            )
            _near_new_high = _day_high > 0 and _pullback_pct < 0.5

            # Grade-A: sector-confirmed runners — always pass quality bar.
            # Grade-B: three acceptance lanes (whichever passes first wins):
            #   (a) Strong directional: B/S >= 2.0  — confirmed buying overpowering selling.
            #   (b) Early-breakout: B/S >= 1.5 AND day gain <= 3%  — volume building
            #       BEFORE the price has run, the highest-edge anomaly window.
            #       Pattern: DIACABS at 70x volume, 10x B/S, only +0.9% from open.
            #   (c) Continuation breakout: at/near new day high (within 0.5%) with
            #       strong volume (>=5x) and 2-7% from open and basic buy pressure.
            #       Pattern: SURYODAY at 10:30 — making new day high, +3.4% from open,
            #       moderate B/S ~1.2x — too tame for (a) or (b) but a real breakout.
            # Grade-B and Grade-C: require evidence of legitimate buying via
            # one of three patterns. Grade-C was previously hard-suppressed,
            # but a 35-sample audit (anomaly_history.csv) showed C has
            # shallower drawdowns (-2.32% vs -3.72%) and comparable upside
            # to B, so we now apply the same gate to both tiers.
            if c.confidence_tier in ("B", "C"):
                _strong_directional = c.buy_sell_ratio >= 2.0
                _early_breakout = c.buy_sell_ratio >= 1.5 and c.price_from_open_pct <= 3.0
                _continuation = (
                    _near_new_high
                    and c.volume_ratio_time_adj >= 5.0
                    and 2.0 <= c.price_from_open_pct <= 7.0
                    and c.buy_sell_ratio >= 1.0
                )
                if not (_strong_directional or _early_breakout or _continuation):
                    continue
            # Skip when sell pressure dominates overall
            if c.buy_sell_ratio < 1.0:
                continue
            # "Already played out" check: if today's high is more than 2% above the
            # current price, the breakout already happened and reversed. The volume
            # spike is historical, not actionable. (e.g. THYROCARE: high ₹526 at
            # 09:45, alerting at ₹494 at 11:02 = 6% off the peak — not "early".)
            if _pullback_pct > 2.0:
                OutputControls().printOutput(
                    f"  [!] Anomaly {c.symbol} skipped — already pulled back "
                    f"{_pullback_pct:.1f}% from day high ₹{_day_high:.2f} (move played out)"
                )
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

            # Match broker convention: report ChangePct from PREVIOUS CLOSE,
            # not today's open. Google/Kite/Groww all show "today %" against
            # prev close — using open caused +2.8% vs +2.57% confusion on UNOMINDA.
            _pct_from_prev_close = (
                (c.last_price - c.prev_close) / c.prev_close * 100.0
                if c.prev_close and c.prev_close > 0
                else c.price_from_open_pct
            )
            alert_rows.append({
                "When": self.run_at_str,
                "Symbol": c.symbol,
                "PreviousAction": "None",
                    "NowAction": action_label,
                    "PriceNow": c.last_price,
                    "ChangePct": round(_pct_from_prev_close, 2),
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
            self._persist_anomaly_history(alert_rows, regime_str)

        # Register for follow-through monitoring (5-min recheck)
        if candidates:
            self._anomaly_detector.register_for_followthrough(candidates)

        return pd.DataFrame(alert_rows, columns=self.ALERT_COLUMNS)

    def _persist_anomaly_history(self, alert_rows: List[Dict], regime: str) -> None:
        """Append-only log of first V2 Anomaly detection per (symbol, date).

        Writes to results/Data/anomaly_history.csv. Deduplicates against
        existing rows so re-detections within the same day don't pile up.
        """
        import csv as _csv
        try:
            out_dir = Archiver.get_user_data_dir()
        except Exception:
            out_dir = os.path.join(os.getcwd(), "results", "Data")
        out_path = os.path.join(out_dir, "anomaly_history.csv")

        fieldnames = [
            "Date", "When", "Symbol", "Tier", "Action", "PriceNow",
            "ChangePct", "BuyAbove", "SellIfBelow", "TakeProfit1", "TakeProfit2",
            "SuggestedQty", "QualityScore", "Regime", "Checks", "Why",
        ]
        existing: Set[Tuple[str, str]] = set()
        if os.path.exists(out_path):
            try:
                with open(out_path, newline="") as f:
                    for r in _csv.DictReader(f):
                        existing.add(((r.get("Symbol") or "").strip().upper(), (r.get("Date") or "")[:10]))
            except Exception:
                existing = set()

        new_rows = []
        for r in alert_rows:
            sym = str(r.get("Symbol", "")).strip().upper()
            when = str(r.get("When", ""))
            date = when[:10]
            if not sym or not date or (sym, date) in existing:
                continue
            action = str(r.get("NowAction", ""))
            tier = "?"
            if "Grade-A" in action:
                tier = "A"
            elif "Grade-B" in action:
                tier = "B"
            elif "Pre-Move" in action:
                tier = "C"
            new_rows.append({
                "Date": date, "When": when, "Symbol": sym, "Tier": tier,
                "Action": action, "PriceNow": r.get("PriceNow", ""),
                "ChangePct": r.get("ChangePct", ""),
                "BuyAbove": r.get("BuyAbove", ""), "SellIfBelow": r.get("SellIfBelow", ""),
                "TakeProfit1": r.get("TakeProfit1", ""), "TakeProfit2": r.get("TakeProfit2", ""),
                "SuggestedQty": r.get("SuggestedQty", ""),
                "QualityScore": r.get("SignalStrengthPct", ""),
                "Regime": regime,
                "Checks": r.get("Checks", ""), "Why": r.get("Why", ""),
            })
            existing.add((sym, date))

        if not new_rows:
            return
        write_header = not os.path.exists(out_path)
        try:
            with open(out_path, "a", newline="") as f:
                w = _csv.DictWriter(f, fieldnames=fieldnames)
                if write_header:
                    w.writeheader()
                w.writerows(new_rows)
        except Exception as exc:
            default_logger().warning(f"Failed to persist anomaly history: {exc}")

    def _run_orderbook_imbalance(self) -> pd.DataFrame:
        """Detect institutional accumulation via buy/sell ratio velocity.

        Tracks B/S ratio per symbol in a rolling 5-min window. Fires an alert
        when current ratio jumps from <=1.5x to >=3x within 5 minutes — a
        signature of fresh institutional buying that the regular anomaly
        path (which uses point-in-time B/S) would only catch later.

        Also flags "0 sellers" books as a NO-BUY warning (RPSGVENT pattern:
        thin liquidity, can't actually fill orders at current price).
        """
        if not self.use_live_kite:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        if not self._is_regular_market_hours():
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        # OB is intraday-only — exit deadline is 3:15 PM. Firing an OB alert
        # after 3:00 PM gives the user no time to enter and exit, so suppress.
        try:
            _now = PKDateUtilities.currentDateTime()
        except Exception:
            _now = datetime.now()
        if _now.hour * 60 + _now.minute >= 15 * 60:  # 3:00 PM IST
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        live_ticks = self.actionable_runner.live_ticks or {}
        if not live_ticks:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        try:
            now_ist = PKDateUtilities.currentDateTime()
        except Exception:
            now_ist = datetime.now()

        # Drop history samples older than 6 minutes (keep ~5-min window + buffer)
        cutoff = now_ist - timedelta(minutes=6)
        for sym in list(self._ob_history.keys()):
            self._ob_history[sym] = [
                (ts, r) for ts, r in self._ob_history[sym] if ts >= cutoff
            ]
            if not self._ob_history[sym]:
                del self._ob_history[sym]

        alert_rows: List[Dict] = []
        today_str = now_ist.strftime("%Y-%m-%d")

        for sym, tick in live_ticks.items():
            sym_u = str(sym).strip().upper()
            try:
                bq = float(tick.get("total_buy_quantity", 0) or 0)
                sq = float(tick.get("total_sell_quantity", 0) or 0)
                price = float(tick.get("last_price", 0) or 0)
                day_high = float(tick.get("high", 0) or 0)
                day_open = float(tick.get("open", 0) or 0)
                prev_close = float(tick.get("close", 0) or 0)
                volume = float(tick.get("volume_traded", 0) or 0)
            except (TypeError, ValueError):
                continue

            # Liquidity & price floors — no penny stocks, no thinly-depth books
            if price < 50.0:
                continue
            if volume < 50000:
                continue
            traded_value = volume * price
            if traded_value < 30_000_000:
                continue  # standing depth must be backed by at least ₹3Cr traded value
            if bq < 50000:
                continue  # not enough buy depth to be meaningful
            # Value-based depth floor: 50k shares of a ₹58 stock is only ₹29
            # lakhs — not institutional. AGIIL had ₹55 crore in buy depth.
            # Require at least ₹2 crore of standing buy interest.
            if bq * price < 20_000_000:
                continue

            # Compute current ratio. 0 sellers gets a special handling.
            zero_sellers = sq == 0
            ratio = bq / sq if sq > 0 else float("inf")

            # Update history
            self._ob_history.setdefault(sym_u, []).append((now_ist, ratio if not zero_sellers else 99.0))

            # Need at least 90s of history to detect a shift
            history = self._ob_history[sym_u]
            if len(history) < 6:  # ~60-90s at 10s cycles
                continue

            # Find earliest sample within last 5 minutes
            five_min_ago = now_ist - timedelta(minutes=5)
            old_samples = [r for ts, r in history if ts <= five_min_ago + timedelta(seconds=30)]
            if not old_samples:
                # Don't have 5 min of history yet
                continue

            old_avg = sum(old_samples) / len(old_samples)

            # Skip if already alerted on this symbol today
            if sym_u in self._ob_alerted_today:
                continue

            # Same pullback-from-peak guard as other paths — don't catch fades
            if day_high > 0 and price > 0:
                pullback_pct = (day_high - price) / price * 100.0
                if pullback_pct > 2.0:
                    continue

            # Price confirmation. OB depth alone is not enough: passive buy
            # orders can be pulled, spoofed, or sit below market while price
            # keeps falling. Require the stock to be green vs both prev close
            # and open so the imbalance is showing up in actual prints.
            day_change_pct = (price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
            open_change_pct = (price - day_open) / day_open * 100.0 if day_open > 0 else 0.0
            if day_change_pct < 0.5 or open_change_pct < 0.2:
                continue

            # Day gain cap — skip exhausted runners
            if day_change_pct > 8.0:
                continue

            # Detection criteria:
            #   sustained = B/S >= 8.0 (institutional-grade, persistent imbalance)
            # OB Surge (sudden B/S jump from <=1.5x to >=3x) is removed — too
            # speculative, frequently reverses before fill. Only sustained heavy
            # imbalance is blind-buyable.
            sustained = ratio >= 8.0 and not zero_sellers
            if not sustained:
                continue

            # Make "sustained" real: at least part of the 5-minute history must
            # also show heavy imbalance. A single late spike in visible depth is
            # too easy to misread.
            if old_avg < 5.0:
                continue

            # Build alert row in the standard ALERT_COLUMNS shape so the message
            # builder + de-dup logic work without changes.
            buy_above = round(price, 2)
            stop_loss = round(price * 0.95, 2)  # -5% stop
            target1 = round(price * 1.06, 2)  # +6% — modest first target since unconfirmed by OHLCV
            target2 = round(price * 1.10, 2)  # +10%
            qty = int(self.budget_amount // price) if self.budget_amount and price > 0 else 0
            tag = "🌊 OB Sustained"
            why_parts = [
                f"Buy/Sell ratio sustained at {ratio:.1f}x — heavy directional buying",
                f"Buy depth: {int(bq):,} shares vs Sell depth: {int(sq):,} shares",
            ]
            if day_change_pct >= 1.0:
                why_parts.append(f"Day +{day_change_pct:.1f}% (still actionable)")
            why_parts.append("⚡ INTRADAY ONLY — exit before market close, do not hold overnight")
            checks = (
                f"OB B/S {ratio:.1f}x | depth {int(bq/1000)}k buy | "
                f"turnover ₹{traded_value/10_000_000:.1f}Cr | day {day_change_pct:+.1f}%"
            )

            alert_rows.append({
                "When": self.run_at_str,
                "Symbol": sym_u,
                "PreviousAction": "None",
                "NowAction": tag,
                "PriceNow": price,
                "ChangePct": round(day_change_pct, 2),
                "EntryGapPct": 0.0,
                "RSI": "",
                "BuyAbove": buy_above,
                "SellIfBelow": stop_loss,
                "TakeProfit1": target1,
                "TakeProfit2": target2,
                "SuggestedQty": qty,
                "SignalStrengthPct": round(min(ratio / 20.0 * 100, 100.0), 1),
                "Checks": checks,
                "Why": " | ".join(why_parts),
            })
            self._ob_alerted_today.add(sym_u)

        if alert_rows:
            OutputControls().printOutput(
                f"  [+] 🌊 Order book surge: {len(alert_rows)} alert(s) — "
                + ", ".join(r["Symbol"] for r in alert_rows[:5])
            )

        return pd.DataFrame(alert_rows, columns=self.ALERT_COLUMNS)

    def _annotate_ob_confirmations(self, alerts_df: pd.DataFrame, ob_alerts_df: pd.DataFrame) -> pd.DataFrame:
        """Use OB as confirmation on stronger lanes instead of a standalone trigger."""
        if alerts_df is None or alerts_df.empty or ob_alerts_df is None or ob_alerts_df.empty:
            return alerts_df
        out = alerts_df.copy()
        ob_map = {
            str(row.get("Symbol", "")).strip().upper(): row
            for row in ob_alerts_df.to_dict(orient="records")
        }
        for idx, row in out.iterrows():
            symbol = str(row.get("Symbol", "")).strip().upper()
            ob_row = ob_map.get(symbol)
            if not ob_row:
                continue
            action = str(row.get("NowAction", "")).strip()
            if action == "Sell / Exit":
                continue
            checks = str(row.get("Checks", "") or "")
            why = str(row.get("Why", "") or "")
            ob_checks = str(ob_row.get("Checks", "") or "")
            ob_reason = "OB confirms visible buyer depth"
            out.at[idx, "Checks"] = (checks + " | " if checks else "") + ob_checks
            out.at[idx, "Why"] = (why + " | " if why else "") + ob_reason
        return out

    def _apply_intraday_goal_mode(self, alerts_df: pd.DataFrame) -> pd.DataFrame:
        """Keep only alerts that fit the strict 3%-burst intraday playbook.

        This is not a promise of 3% daily returns. It is a capital-allocation
        gate: one high-conviction buy-style candidate per day by default, with
        tight intraday exits. Sell/exit alerts are always preserved.
        """
        if not getattr(self, "enable_intraday_goal_mode", True):
            return alerts_df
        if alerts_df is None or alerts_df.empty:
            return alerts_df

        today = self._today_key()
        max_per_day = max(1, int(getattr(self, "goal_max_trades_per_day", 1) or 1))
        already_goal_symbols = self._load_daily_symbol_set("goal_burst", today=today)
        remaining_slots = max(0, max_per_day - len(already_goal_symbols))
        max_radar_per_day = max(0, int(getattr(self, "radar_max_alerts_per_day", 3) or 0))
        already_radar_symbols = self._load_daily_symbol_set("top_gainer_radar_goal", today=today)
        remaining_radar_slots = max(0, max_radar_per_day - len(already_radar_symbols))

        sell_rows: List[Dict] = []
        candidates: List[Tuple[float, Dict]] = []
        radar_candidates: List[Tuple[float, Dict]] = []
        suppressed: List[str] = []
        self._last_goal_suppressed_reasons = []

        for row in alerts_df.to_dict(orient="records"):
            action = str(row.get("NowAction", "") or "").strip()
            symbol = str(row.get("Symbol", "") or "").strip().upper()
            if action == "Sell / Exit":
                sell_rows.append(row)
                continue
            if not self._is_goal_mode_buy_action(action):
                suppressed.append(f"{symbol} ({action or 'unknown'})")
                continue
            qualifies, score, reason = self._intraday_goal_decision(row)
            if not qualifies:
                suppressed.append(f"{symbol} ({reason})")
                continue
            if self._is_radar_action(action):
                if symbol in already_radar_symbols:
                    suppressed.append(f"{symbol} (radar already surfaced today)")
                    continue
                tuned = self._tune_radar_alert_row(row, reason)
                radar_candidates.append((score, tuned))
                continue
            if symbol in already_goal_symbols:
                suppressed.append(f"{symbol} (goal-mode already surfaced today)")
                continue
            tuned = self._tune_goal_mode_alert_row(row, reason)
            candidates.append((score, tuned))

        candidates.sort(key=lambda item: (-item[0], str(item[1].get("Symbol", ""))))
        selected_rows = []
        for _, row in candidates[:remaining_slots]:
            selected_rows.append(row)
            symbol = str(row.get("Symbol", "") or "").strip().upper()
            if symbol:
                already_goal_symbols.add(symbol)
                self._append_daily_symbol("goal_burst", symbol, today=today)
        suppressed.extend(
            f"{str(row.get('Symbol', '')).strip().upper()} (daily goal slots full)"
            for _, row in candidates[remaining_slots:]
        )
        radar_candidates.sort(key=lambda item: (-item[0], str(item[1].get("Symbol", ""))))
        selected_radar_rows = []
        for _, row in radar_candidates[:remaining_radar_slots]:
            selected_radar_rows.append(row)
            symbol = str(row.get("Symbol", "") or "").strip().upper()
            if symbol:
                already_radar_symbols.add(symbol)
                self._append_daily_symbol("top_gainer_radar_goal", symbol, today=today)
        suppressed.extend(
            f"{str(row.get('Symbol', '')).strip().upper()} (daily radar slots full)"
            for _, row in radar_candidates[remaining_radar_slots:]
        )
        self._last_goal_suppressed_reasons = suppressed[:25]

        if suppressed:
            OutputControls().printOutput(
                f"  [!] Intraday goal mode suppressed {len(suppressed)} buy alert(s): "
                + ", ".join(suppressed[:10])
                + (" ..." if len(suppressed) > 10 else "")
            )
        if selected_rows:
            OutputControls().printOutput(
                "  [+] Intraday goal mode: "
                f"{len(selected_rows)}/{max_per_day} tradeable burst candidate(s) selected — "
                + ", ".join(str(r.get("Symbol", "")).strip().upper() for r in selected_rows)
            )
        if selected_radar_rows:
            OutputControls().printOutput(
                "  [+] Top-gainer radar mode: "
                f"{len(selected_radar_rows)}/{max_radar_per_day} separate radar candidate(s) selected — "
                + ", ".join(str(r.get("Symbol", "")).strip().upper() for r in selected_radar_rows)
            )

        return pd.DataFrame(sell_rows + selected_rows + selected_radar_rows, columns=self.ALERT_COLUMNS)

    @staticmethod
    def _is_radar_action(action: str) -> bool:
        return "radar" in str(action or "").strip().lower()

    @staticmethod
    def _is_goal_mode_buy_action(action: str) -> bool:
        text = str(action or "").strip()
        lower = text.lower()
        return (
            text in {"Strong Buy Now", "Buy Now", "Momentum Buy"}
            or "anomaly" in lower
            or "breakout" in lower
            or "radar" in lower
            or "runner" in lower
            or "pre-move" in lower
            or "ob" in lower
            or text.startswith("🔥")
            or text.startswith("⚡")
            or text.startswith("👀")
        )

    def _intraday_goal_decision(self, row: Dict) -> Tuple[bool, float, str]:
        action = str(row.get("NowAction", "") or "").strip()
        action_lower = action.lower()
        checks = str(row.get("Checks", "") or "")
        checks_lower = checks.lower()
        strength = self._to_float(row.get("SignalStrengthPct"))
        change_pct = self._to_float(row.get("ChangePct"))
        entry_gap_pct = self._to_float(row.get("EntryGapPct"))
        rsi = self._to_float(row.get("RSI"))
        volume_ratio = self._extract_volume_ratio(checks)
        bs_ratio = self._extract_buy_sell_ratio(checks)
        has_ob_confirmation = "ob b/s" in checks_lower or "ob confirms" in checks_lower
        has_sector_confirmation = "sector" in checks_lower
        has_compression = "compressed" in checks_lower

        strength = strength if strength is not None else 0.0
        change_pct = change_pct if change_pct is not None else 0.0
        entry_gap_pct = entry_gap_pct if entry_gap_pct is not None else 0.0
        rsi = rsi if rsi is not None else 0.0

        if "ob" in action_lower and not has_ob_confirmation:
            return False, 0.0, "OB standalone blocked"
        if change_pct > 7.0:
            return False, 0.0, f"day move {change_pct:+.1f}% too stretched"
        if rsi >= 74.0:
            return False, 0.0, f"RSI {rsi:.0f} too hot"

        is_grade_a_b_anomaly = (
            "grade-a" in action_lower
            or "grade-b" in action_lower
            or action.startswith("🔥")
            or action.startswith("⚡")
        )
        if getattr(self, "goal_anomaly_only", False) and not is_grade_a_b_anomaly:
            return False, 0.0, "anomaly-only goal mode"

        # Highest priority: anomaly-style setups. The alert backtest only showed
        # a 3% burst profile here; direct/OB-only signals were not robust.
        if "grade-a" in action_lower or "runner" in action_lower:
            if strength >= 60.0 and change_pct <= 7.0:
                return True, 1000.0 + strength + min(volume_ratio, 10.0) * 5.0, "Grade-A/runner burst setup"
            return False, 0.0, "runner quality below goal mode"

        if "pre-move" in action_lower or action.startswith("👀"):
            if strength >= 55.0 and change_pct <= 4.0 and (volume_ratio >= 3.0 or bs_ratio >= 1.2 or has_sector_confirmation or has_compression):
                return True, 930.0 + strength + min(volume_ratio, 10.0) * 4.0 + bs_ratio * 3.0, "Pre-move burst setup"
            return False, 0.0, "pre-move lacks volume/buy pressure"

        if "anomaly" in action_lower or action.startswith("⚡") or action.startswith("🔥"):
            directional_confirmation = bs_ratio >= 1.5 or has_sector_confirmation or has_compression or has_ob_confirmation
            if change_pct < 0.8:
                return False, 0.0, f"day move {change_pct:+.1f}% too flat for anomaly scalp"
            if strength >= 60.0 and volume_ratio >= 5.0 and change_pct <= 6.0 and directional_confirmation:
                return True, 900.0 + strength + min(volume_ratio, 15.0) * 4.0 + bs_ratio * 5.0, "Grade-B anomaly burst setup"
            return False, 0.0, "anomaly below 3% burst bar"

        if "breakout" in action_lower:
            if (
                strength >= 32.0
                and self.EARLY_BREAKOUT_MIN_PCT <= change_pct <= self.EARLY_BREAKOUT_MAX_PCT
                and (volume_ratio >= self.EARLY_BREAKOUT_MIN_VOLUME_RATIO or bs_ratio >= 1.5 or has_ob_confirmation)
                and entry_gap_pct <= 1.5
                and (rsi == 0.0 or rsi < 70.0)
            ):
                return True, 860.0 + strength + min(volume_ratio, 8.0) * 5.0 + bs_ratio * 3.0, "Early breakout before top-gainer stretch"
            return False, 0.0, "early breakout below goal bar"

        if "radar" in action_lower:
            if (
                strength >= float(getattr(self, "radar_min_strength", 38.0) or 38.0)
                and self.TOP_GAINER_RADAR_MIN_PCT <= change_pct <= self.TOP_GAINER_RADAR_MAX_PCT
                and volume_ratio >= float(getattr(self, "radar_min_volume_ratio", 1.0) or 1.0)
                and bs_ratio >= float(getattr(self, "radar_min_bs_ratio", 1.2) or 1.2)
                and (rsi == 0.0 or rsi < 70.0)
            ):
                return True, 845.0 + strength + min(volume_ratio, 5.0) * 5.0 + bs_ratio * 4.0, "Top-gainer radar before full scanner confirmation"
            return False, 0.0, "top-gainer radar below goal bar"

        # Direct Buy Now only becomes goal-mode tradeable when another lane confirms.
        if action in {"Strong Buy Now", "Buy Now"}:
            if not getattr(self, "goal_allow_direct_buy", True):
                return False, 0.0, "direct buy disabled for goal mode"
            bearish_text = f"{checks_lower} | {str(row.get('Why', '') or '').lower()}"
            bearish_markers = (
                "sell / exit",
                "bearish trend",
                "kalman downtrend",
                "price below atr trailing stop",
                "uptrend broken",
                "strong downtrend",
                "macd bearish",
                "death cross",
                "lower highs and lower lows",
            )
            clean_direct_buy = (
                strength >= 40.0
                and 1.0 <= change_pct <= 5.0
                and volume_ratio >= 1.3
                and entry_gap_pct <= 1.5
                and (rsi == 0.0 or rsi < 70.0)
                and not any(marker in bearish_text for marker in bearish_markers)
                and (
                    "price above atr trailing stop" in bearish_text
                    or "momentum promotion" in checks_lower
                    or "volume ok" in checks_lower
                    or "strong volume" in checks_lower
                )
            )
            if clean_direct_buy:
                return True, 835.0 + strength + min(volume_ratio, 6.0) * 6.0, "Quality direct Buy Now goal setup"
            if (
                has_ob_confirmation
                and strength >= 60.0
                and change_pct <= 3.5
                and entry_gap_pct <= 1.5
                and (rsi == 0.0 or rsi < 68.0)
            ):
                return True, 820.0 + strength + bs_ratio * 5.0, "Direct buy with OB confirmation"
            return False, 0.0, "direct buy lacks goal confirmation"

        if action == "Momentum Buy":
            if has_ob_confirmation and strength >= 80.0 and 2.0 <= change_pct <= 7.0:
                return True, 780.0 + strength, "Momentum with OB confirmation"
            return False, 0.0, "momentum not confirmed for goal mode"

        return False, 0.0, "not a goal-mode setup"

    def _tune_goal_mode_alert_row(self, row: Dict, reason: str) -> Dict:
        tuned = dict(row)
        entry = self._to_float(tuned.get("BuyAbove")) or self._to_float(tuned.get("PriceNow")) or 0.0
        if entry > 0:
            stop_pct = float(getattr(self, "goal_stop_pct", 1.0) or 1.0)
            target1_pct = float(getattr(self, "goal_target1_pct", 3.0) or 3.0)
            target2_pct = float(getattr(self, "goal_target2_pct", 5.0) or 5.0)
            tuned["BuyAbove"] = round(entry, 2)
            tuned["SellIfBelow"] = round(entry * (1.0 - stop_pct / 100.0), 2)
            tuned["TakeProfit1"] = round(entry * (1.0 + target1_pct / 100.0), 2)
            tuned["TakeProfit2"] = round(entry * (1.0 + target2_pct / 100.0), 2)

        checks = str(tuned.get("Checks", "") or "").strip()
        goal_check = (
            f"GoalMode✓ 3% burst | SL {float(getattr(self, 'goal_stop_pct', 1.0) or 1.0):.1f}% | "
            f"T1 {float(getattr(self, 'goal_target1_pct', 3.0) or 3.0):.1f}% | "
            f"T2 {float(getattr(self, 'goal_target2_pct', 5.0) or 5.0):.1f}%"
        )
        tuned["Checks"] = f"{goal_check} | {checks}" if checks else goal_check
        why = str(tuned.get("Why", "") or "").strip()
        goal_why = f"Goal mode: {reason}; max one high-conviction intraday attempt by default"
        tuned["Why"] = f"{goal_why} | {why}" if why else goal_why
        return tuned

    def _tune_radar_alert_row(self, row: Dict, reason: str) -> Dict:
        tuned = dict(row)
        entry = self._to_float(tuned.get("BuyAbove")) or self._to_float(tuned.get("PriceNow")) or 0.0
        if entry > 0:
            stop_pct = float(getattr(self, "goal_stop_pct", 1.0) or 1.0)
            target1_pct = float(getattr(self, "goal_target1_pct", 1.5) or 1.5)
            target2_pct = float(getattr(self, "goal_target2_pct", 3.0) or 3.0)
            tuned["BuyAbove"] = round(entry, 2)
            tuned["SellIfBelow"] = round(entry * (1.0 - stop_pct / 100.0), 2)
            tuned["TakeProfit1"] = round(entry * (1.0 + target1_pct / 100.0), 2)
            tuned["TakeProfit2"] = round(entry * (1.0 + target2_pct / 100.0), 2)

        checks = str(tuned.get("Checks", "") or "").strip()
        radar_check = (
            f"RadarMode separate watch | SL {float(getattr(self, 'goal_stop_pct', 1.0) or 1.0):.1f}% | "
            f"T1 {float(getattr(self, 'goal_target1_pct', 1.5) or 1.5):.1f}% | "
            f"T2 {float(getattr(self, 'goal_target2_pct', 3.0) or 3.0):.1f}%"
        )
        tuned["Checks"] = f"{radar_check} | {checks}" if checks else radar_check
        why = str(tuned.get("Why", "") or "").strip()
        radar_why = f"Radar mode: {reason}; separate from the 20 day-goal basket, optional only"
        tuned["Why"] = f"{radar_why} | {why}" if why else radar_why
        return tuned

    @staticmethod
    def _extract_buy_sell_ratio(checks) -> float:
        import re

        text = str(checks or "")
        for pattern in (r"B/S\s+([0-9]+(?:\.[0-9]+)?)x", r"Buy/Sell\s+([0-9]+(?:\.[0-9]+)?)x"):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1))
                except Exception:
                    return 0.0
        return 0.0

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

        # 2. Exit signal check on active positions — push to Discord, not just console
        try:
            exits = self._anomaly_detector.check_exits(current_ticks)
            for ex in exits:
                pnl_emoji = "🟢" if ex["pnl_pct"] > 0 else "🔴"
                console_line = (
                    f"  [{pnl_emoji}] EXIT {ex['symbol']}: {ex['pnl_pct']:+.2f}% "
                    f"(₹{ex['entry_price']:.2f} → ₹{ex['current_price']:.2f}) | "
                    f"{ex['exit_reason']}"
                )
                OutputControls().printOutput(console_line)
                # Push to Discord too — this is what the user needs to act
                if "discord" in self.alert_channels and self._is_regular_market_hours():
                    discord_msg = (
                        f"{pnl_emoji} **EXIT — `{ex['symbol']}`**\n"
                        f"P&L: **{ex['pnl_pct']:+.2f}%** (entry ₹{ex['entry_price']:.2f} → now ₹{ex['current_price']:.2f})\n"
                        f"Reason: {ex['exit_reason']}\n"
                        f"_Sell in Groww / use `/sell {ex['symbol']} QTY PRICE` to record_"
                    )
                    self._send_discord_alert(discord_msg)
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

            # Same buy-zone-ceiling check as Buy Now: skip if live price already past ceiling
            buy_above = self._to_float(rec.get("Buy Above"))
            if buy_above > 0 and price_now > 0 and price_now > buy_above * 1.015:
                continue

            # Same minimum strength as Buy Now: 35% floor in opening window
            # (9:15-10:00 IST), 40% floor afterwards.
            mom_strength = self._to_float(rec.get("Signal Strength %"))
            try:
                _mom_now = PKDateUtilities.currentDateTime()
            except Exception:
                _mom_now = datetime.now()
            _mom_min = _mom_now.hour * 60 + _mom_now.minute
            _mom_morning = (9 * 60 + 15) <= _mom_min <= (10 * 60 + 0)
            _mom_floor = 35.0 if _mom_morning else 40.0
            if mom_strength is not None and mom_strength < _mom_floor:
                continue

            # Same pullback-from-peak guard as Buy Now: skip if stock already
            # rolled over more than 2% from today's high.
            _mom_ticks = self.actionable_runner.live_ticks or {}
            _mom_tick = _mom_ticks.get(sym, {})
            _mom_day_high = self._to_float(_mom_tick.get("high", 0))
            if _mom_day_high > 0 and price_now > 0:
                _mom_pullback = (_mom_day_high - price_now) / price_now * 100.0
                if _mom_pullback > 2.0:
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

    def _build_early_breakout_alerts(self, current_simple_df: pd.DataFrame) -> pd.DataFrame:
        """Catch fresh +1% to +3.5% movers before they become top-gainer chases."""
        if not getattr(self, "enable_early_breakout_alerts", True):
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        if current_simple_df is None or current_simple_df.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        try:
            today = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d")
        except Exception:
            today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_early_breakout_alerted_date", "") != today:
            self._early_breakout_alerted_date = today
            self._early_breakout_alerted_today = self._load_daily_symbol_set("early_breakout", today=today)

        rows = []
        live_ticks = self.actionable_runner.live_ticks or {}
        for rec in current_simple_df.to_dict(orient="records"):
            sym = str(rec.get("Stock", "") or "").strip().upper()
            if not sym or sym in self._early_breakout_alerted_today:
                continue

            change_pct = self._to_float(rec.get("Change %"))
            if not (self.EARLY_BREAKOUT_MIN_PCT <= change_pct <= self.EARLY_BREAKOUT_MAX_PCT):
                continue

            action = str(rec.get("What To Do", "") or "").strip()
            setup = str(rec.get("Momentum Setup", "") or "").strip()
            if action not in {"Strong Buy Now", "Buy Now"} and setup != "Momentum Buy":
                continue

            price_now = self._to_float(rec.get("Price Now"))
            if price_now <= 0:
                continue

            strength = self._to_float(rec.get("Signal Strength %"))
            rsi_val = self._to_float(rec.get("RSI"))
            volume_ratio = self._to_float(rec.get("Volume vs Avg"))
            tick = live_ticks.get(sym, {})
            total_buy = self._to_float(tick.get("total_buy_quantity", 0))
            total_sell = self._to_float(tick.get("total_sell_quantity", 0))
            bs_ratio = total_buy / total_sell if total_sell > 0 else (9.99 if total_buy > 0 else 0.0)

            try:
                now_ist = PKDateUtilities.currentDateTime()
            except Exception:
                now_ist = datetime.now()
            minute_of_day = int(now_ist.hour) * 60 + int(now_ist.minute)
            opening_window = (9 * 60 + 15) <= minute_of_day <= (10 * 60 + 15)
            strength_floor = 32.0 if opening_window else 38.0
            if strength < strength_floor:
                continue
            if rsi_val and rsi_val >= 70.0:
                continue
            if volume_ratio < self.EARLY_BREAKOUT_MIN_VOLUME_RATIO and bs_ratio < 1.5:
                continue

            week52 = self._to_float(rec.get("Week52High"))
            if week52 and week52 > 0 and price_now < week52:
                room_to_high = (week52 - price_now) / price_now * 100.0
                if room_to_high < 2.0:
                    continue

            day_high = self._to_float(tick.get("high", 0))
            pullback = 0.0
            if day_high > 0 and price_now > 0:
                pullback = (day_high - price_now) / price_now * 100.0
                if pullback > 1.2:
                    continue

            suggested_qty = self._to_float(rec.get("Suggested Qty"))
            if suggested_qty < 1:
                continue

            entry = price_now
            stop = round(entry * (1.0 - float(getattr(self, "goal_stop_pct", 1.0) or 1.0) / 100.0), 2)
            target1 = round(entry * (1.0 + float(getattr(self, "goal_target1_pct", 1.5) or 1.5) / 100.0), 2)
            target2 = round(entry * (1.0 + float(getattr(self, "goal_target2_pct", 3.0) or 3.0) / 100.0), 2)
            checks = str(rec.get("Checks", "") or "").strip()
            breakout_checks = [
                checks,
                f"early breakout +{change_pct:.1f}%",
                f"vol {volume_ratio:.1f}x",
            ]
            if bs_ratio > 0:
                breakout_checks.append(f"B/S {bs_ratio:.1f}x")
            if day_high > 0:
                breakout_checks.append(f"near high pullback {pullback:.1f}%")

            rows.append(
                {
                    "When": self.run_at_str,
                    "Symbol": sym,
                    "PreviousAction": action or setup or "None",
                    "NowAction": "🌱 Early Breakout",
                    "PriceNow": entry,
                    "ChangePct": round(change_pct, 2),
                    "EntryGapPct": rec.get("Entry Gap %", ""),
                    "RSI": rec.get("RSI", ""),
                    "BuyAbove": entry,
                    "SellIfBelow": stop,
                    "TakeProfit1": target1,
                    "TakeProfit2": target2,
                    "SuggestedQty": int(suggested_qty),
                    "SignalStrengthPct": strength,
                    "Checks": " | ".join(p for p in breakout_checks if p),
                    "Why": str(rec.get("Why", "") or "").strip()
                    + " | Early breakout lane: +1% to +3.5% move before top-gainer stretch",
                }
            )

        if not rows:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        out = pd.DataFrame(rows, columns=self.ALERT_COLUMNS)
        out.sort_values(
            by=["SignalStrengthPct", "ChangePct", "Symbol"],
            ascending=[False, False, True],
            inplace=True,
        )
        max_per_cycle = max(1, int(getattr(self, "EARLY_BREAKOUT_MAX_PER_CYCLE", 4) or 4))
        out = out.head(max_per_cycle)
        for sym in out["Symbol"].astype(str).str.upper().tolist():
            self._early_breakout_alerted_today.add(sym)
            self._append_daily_symbol("early_breakout", sym, today=today)
        OutputControls().printOutput(
            "  [+] Early breakout alert(s): "
            + ", ".join(f"{r['Symbol']} (+{float(r['ChangePct']):.1f}%)" for _, r in out.iterrows())
        )
        return out[self.ALERT_COLUMNS].copy()

    def _run_leader_ignition_lane(self) -> pd.DataFrame:
        """Raw-tick early leader research lane.

        This lane is intentionally independent of the scanner's Buy Now/Q score
        pipeline. It watches every live tick for stocks beginning to lead while
        they are still in a small move window, persists pass/reject evidence,
        and then tracks whether a cleaner pullback-reclaim entry appears.
        """
        if not getattr(self, "enable_leader_ignition", False):
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        if not self.use_live_kite or not self._is_regular_market_hours():
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        live_ticks = self.actionable_runner.live_ticks or {}
        if not live_ticks:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        try:
            now = PKDateUtilities.currentDateTime()
        except Exception:
            now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if getattr(self, "_leader_ignition_date", "") != today:
            self._leader_ignition_date = today
            self._leader_ignition_today = self._load_daily_symbol_set("leader_ignition_watch", today=today)
        if getattr(self, "_leader_reclaim_date", "") != today:
            self._leader_reclaim_date = today
            self._leader_reclaim_today = self._load_daily_symbol_set("leader_pullback_reclaim", today=today)

        metrics_by_symbol: Dict[str, Dict[str, Any]] = {}
        for sym, tick in live_ticks.items():
            metrics = self._leader_tick_metrics(str(sym).strip().upper(), tick, now)
            if metrics:
                metrics_by_symbol[metrics["Symbol"]] = metrics

        audit_rows: List[Dict[str, Any]] = []
        alert_rows: List[Dict[str, Any]] = []
        state_changed = False

        reclaim_rows, reclaim_changed = self._update_leader_pullback_reclaims(
            metrics_by_symbol,
            now,
            today,
            audit_rows,
        )
        alert_rows.extend(reclaim_rows)
        state_changed = state_changed or reclaim_changed

        max_per_day = max(0, int(getattr(self, "LEADER_IGNITION_MAX_PER_DAY", 120) or 120))
        remaining_day_slots = max(0, max_per_day - len(getattr(self, "_leader_ignition_today", set())))
        watch_candidates: List[Tuple[float, Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []

        if remaining_day_slots > 0:
            for symbol, metrics in metrics_by_symbol.items():
                if symbol in getattr(self, "_leader_ignition_today", set()):
                    continue
                existing = self._leader_candidates.get(symbol, {})
                if str(existing.get("status", "")).lower() in {"watch", "pullback_seen", "reclaim_alerted"}:
                    continue

                interesting = (
                    metrics["OpenMovePct"] >= self.LEADER_IGNITION_MIN_OPEN_PCT
                    or metrics["DayChangePct"] >= self.LEADER_IGNITION_MIN_DAY_PCT
                    or metrics["BuyerSellerRatio"] >= self.LEADER_IGNITION_MIN_BS_RATIO
                    or metrics["VolumeDeltaPct"] >= self.LEADER_IGNITION_MIN_VOLUME_DELTA_PCT
                )
                if not interesting:
                    continue

                reject_reason = ""
                if metrics["PriceNow"] < 50.0:
                    reject_reason = "price below floor"
                elif metrics["TradedValue"] < self.LEADER_IGNITION_MIN_TRADED_VALUE:
                    reject_reason = "traded value below floor"
                elif (
                    metrics["OpenMovePct"] < self.LEADER_IGNITION_MIN_OPEN_PCT
                    or metrics["DayChangePct"] < self.LEADER_IGNITION_MIN_DAY_PCT
                ):
                    reject_reason = "move below ignition window"
                elif (
                    metrics["OpenMovePct"] > self.LEADER_IGNITION_MAX_OPEN_PCT
                    or metrics["DayChangePct"] > self.LEADER_IGNITION_MAX_DAY_PCT
                ):
                    reject_reason = "already stretched"
                elif metrics["PullbackPct"] > self.LEADER_IGNITION_MAX_PULLBACK_PCT:
                    reject_reason = "pulled back before watch"
                elif (
                    metrics["BuyerSellerRatio"] < self.LEADER_IGNITION_MIN_BS_RATIO
                    and metrics["VolumeDeltaPct"] < self.LEADER_IGNITION_MIN_VOLUME_DELTA_PCT
                ):
                    reject_reason = "no buyer pressure or volume acceleration"

                score = self._leader_ignition_score(metrics)
                if reject_reason:
                    audit_rows.append(
                        self._leader_ignition_audit_row(metrics, "ignition_watch", "reject", reject_reason, score)
                    )
                    continue

                alert_row = self._leader_alert_row(
                    metrics,
                    action="Leader Ignition Watch",
                    score=score,
                    why=(
                        "Shadow lane: raw-tick early leader watch before scanner confirmation; "
                        "not auto-traded."
                    ),
                )
                expires_ts = now.timestamp() + float(self.LEADER_CANDIDATE_TTL_MINUTES) * 60.0
                state = {
                    "today": today,
                    "symbol": symbol,
                    "status": "watch",
                    "created_at": now.isoformat(timespec="seconds"),
                    "created_ts": now.timestamp(),
                    "expires_ts": expires_ts,
                    "watch_price": round(metrics["PriceNow"], 2),
                    "high_price": round(max(metrics["DayHigh"], metrics["PriceNow"]), 2),
                    "pullback_seen": False,
                    "pullback_low": 0.0,
                    "reclaim_trigger": 0.0,
                    "score": round(score, 2),
                }
                audit_row = self._leader_ignition_audit_row(metrics, "ignition_watch", "pass", "early leader watch", score)
                watch_candidates.append((score, alert_row, state, audit_row))

        watch_candidates.sort(key=lambda item: (-item[0], str(item[1].get("Symbol", ""))))
        max_per_cycle = max(1, int(getattr(self, "LEADER_IGNITION_MAX_PER_CYCLE", 8) or 8))
        selected_watch = watch_candidates[: min(max_per_cycle, remaining_day_slots)]
        selected_symbols = {str(row.get("Symbol", "")).strip().upper() for _, row, _, _ in selected_watch}
        for score, alert_row, state, audit_row in selected_watch:
            symbol = str(alert_row.get("Symbol", "")).strip().upper()
            if not symbol:
                continue
            self._leader_candidates[symbol] = state
            self._leader_ignition_today.add(symbol)
            self._append_daily_symbol("leader_ignition_watch", symbol, today=today)
            alert_rows.append(alert_row)
            audit_rows.append(audit_row)
            state_changed = True

        for score, alert_row, _state, audit_row in watch_candidates[len(selected_watch):]:
            symbol = str(alert_row.get("Symbol", "")).strip().upper()
            if symbol in selected_symbols:
                continue
            audit_row = dict(audit_row)
            audit_row["Decision"] = "reject"
            audit_row["Reason"] = "cycle or daily leader slot not selected"
            audit_rows.append(audit_row)

        self._append_leader_ignition_audit(audit_rows, today=today)
        if state_changed:
            self._save_leader_ignition_state()

        if alert_rows:
            preview = ", ".join(
                f"{row['Symbol']} {row['NowAction']} (+{float(row['ChangePct']):.1f}%)"
                for row in alert_rows[:8]
            )
            OutputControls().printOutput(f"  [+] Leader ignition shadow lane: {len(alert_rows)} event(s) — {preview}")

        return pd.DataFrame(alert_rows, columns=self.ALERT_COLUMNS)

    def _leader_tick_metrics(self, symbol: str, tick: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        """Compute one raw-tick metric bundle and update lane-local tick history."""
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return None
        price = self._to_float(tick.get("last_price"))
        open_price = self._to_float(tick.get("open"))
        prev_close = self._to_float(tick.get("close"))
        if price <= 0 or open_price <= 0 or prev_close <= 0:
            return None

        previous = (getattr(self, "_leader_last_ticks_by_symbol", {}) or {}).get(symbol, {})
        previous_price = self._to_float(previous.get("last_price"))
        previous_volume = self._to_float(previous.get("volume_traded"))
        volume = self._to_float(tick.get("volume_traded"))
        volume_delta = max(0.0, volume - previous_volume) if previous_volume > 0 else 0.0
        volume_delta_pct = (volume_delta / previous_volume * 100.0) if previous_volume > 0 else 0.0
        price_delta_pct = ((price - previous_price) / previous_price * 100.0) if previous_price > 0 else 0.0

        day_high = max(self._to_float(tick.get("high")), price)
        pullback_pct = ((day_high - price) / price * 100.0) if day_high > 0 else 0.0
        day_change_pct = self._to_float(tick.get("change"))
        if abs(day_change_pct) < 0.0001:
            day_change_pct = (price - prev_close) / prev_close * 100.0
        open_move_pct = (price - open_price) / open_price * 100.0
        total_buy = self._to_float(tick.get("total_buy_quantity"))
        total_sell = self._to_float(tick.get("total_sell_quantity"))
        buyer_seller_ratio = total_buy / total_sell if total_sell > 0 else (9.99 if total_buy > 0 else 0.0)

        self._leader_last_ticks_by_symbol[symbol] = {
            "last_price": price,
            "volume_traded": volume,
            "seen_ts": now.timestamp(),
        }
        return {
            "When": self.run_at_str,
            "Symbol": symbol,
            "PriceNow": round(price, 2),
            "Open": round(open_price, 2),
            "PrevClose": round(prev_close, 2),
            "DayHigh": round(day_high, 2),
            "OpenMovePct": round(open_move_pct, 3),
            "DayChangePct": round(day_change_pct, 3),
            "PullbackPct": round(max(0.0, pullback_pct), 3),
            "BuyerSellerRatio": round(buyer_seller_ratio, 3),
            "TotalBuyQty": round(total_buy, 2),
            "TotalSellQty": round(total_sell, 2),
            "Volume": round(volume, 2),
            "VolumeDelta": round(volume_delta, 2),
            "VolumeDeltaPct": round(volume_delta_pct, 3),
            "PriceDeltaPct": round(price_delta_pct, 3),
            "TradedValue": round(price * volume, 2),
        }

    def _update_leader_pullback_reclaims(
        self,
        metrics_by_symbol: Dict[str, Dict[str, Any]],
        now: datetime,
        today: str,
        audit_rows: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Advance watch candidates into pullback-seen or reclaim-alerted states."""
        alert_rows: List[Dict[str, Any]] = []
        state_changed = False
        for symbol, state in list((getattr(self, "_leader_candidates", {}) or {}).items()):
            if str(state.get("today", "")) != today:
                continue
            status = str(state.get("status", "") or "").lower()
            if status in {"expired", "invalidated", "reclaim_alerted"}:
                continue
            metrics = metrics_by_symbol.get(str(symbol).strip().upper())
            if not metrics:
                continue

            if float(state.get("expires_ts", 0.0) or 0.0) < now.timestamp():
                state["status"] = "expired"
                state["closed_at"] = now.isoformat(timespec="seconds")
                audit_rows.append(
                    self._leader_ignition_audit_row(metrics, "pullback_reclaim", "reject", "watch expired", state.get("score", 0.0))
                )
                state_changed = True
                continue

            high_price = max(
                self._to_float(state.get("high_price")),
                metrics["DayHigh"],
                metrics["PriceNow"],
            )
            state["high_price"] = round(high_price, 2)
            pullback_from_high = ((high_price - metrics["PriceNow"]) / high_price * 100.0) if high_price > 0 else 0.0
            if pullback_from_high > self.LEADER_PULLBACK_MAX_PCT:
                state["status"] = "invalidated"
                state["closed_at"] = now.isoformat(timespec="seconds")
                audit_rows.append(
                    self._leader_ignition_audit_row(metrics, "pullback_reclaim", "reject", "pullback too deep", state.get("score", 0.0))
                )
                state_changed = True
                continue

            if (
                not bool(state.get("pullback_seen", False))
                and pullback_from_high >= self.LEADER_PULLBACK_MIN_PCT
            ):
                state["pullback_seen"] = True
                state["status"] = "pullback_seen"
                state["pullback_low"] = round(metrics["PriceNow"], 2)
                state["reclaim_trigger"] = round(
                    metrics["PriceNow"] * (1.0 + float(self.LEADER_RECLAIM_PCT) / 100.0),
                    2,
                )
                audit_rows.append(
                    self._leader_ignition_audit_row(metrics, "pullback_reclaim", "watch", "healthy pullback seen", state.get("score", 0.0))
                )
                state_changed = True
                continue

            if bool(state.get("pullback_seen", False)):
                pullback_low = self._to_float(state.get("pullback_low"))
                if pullback_low <= 0 or metrics["PriceNow"] < pullback_low:
                    pullback_low = metrics["PriceNow"]
                    state["pullback_low"] = round(pullback_low, 2)
                    state["reclaim_trigger"] = round(
                        pullback_low * (1.0 + float(self.LEADER_RECLAIM_PCT) / 100.0),
                        2,
                    )
                    state_changed = True

                reclaim_trigger = self._to_float(state.get("reclaim_trigger"))
                if (
                    reclaim_trigger > 0
                    and metrics["PriceNow"] >= reclaim_trigger
                    and pullback_from_high <= self.LEADER_RECLAIM_MAX_PULLBACK_PCT
                    and metrics["BuyerSellerRatio"] >= max(1.1, self.LEADER_IGNITION_MIN_BS_RATIO * 0.8)
                    and symbol not in getattr(self, "_leader_reclaim_today", set())
                ):
                    score = self._leader_ignition_score(metrics) + 75.0
                    row = self._leader_alert_row(
                        metrics,
                        action="Leader Pullback Reclaim",
                        score=score,
                        why=(
                            "Shadow lane: early leader pulled back, then reclaimed with buyer pressure; "
                            "not auto-traded."
                        ),
                    )
                    alert_rows.append(row)
                    state["status"] = "reclaim_alerted"
                    state["closed_at"] = now.isoformat(timespec="seconds")
                    state["reclaim_price"] = round(metrics["PriceNow"], 2)
                    self._leader_reclaim_today.add(symbol)
                    self._append_daily_symbol("leader_pullback_reclaim", symbol, today=today)
                    audit_rows.append(
                        self._leader_ignition_audit_row(metrics, "pullback_reclaim", "pass", "reclaim after healthy pullback", score)
                    )
                    state_changed = True

        return alert_rows, state_changed

    def _leader_ignition_score(self, metrics: Dict[str, Any]) -> float:
        """Score early leaders using only raw tape features."""
        return round(
            500.0
            + metrics["OpenMovePct"] * 35.0
            + metrics["DayChangePct"] * 20.0
            + min(metrics["BuyerSellerRatio"], 10.0) * 8.0
            + min(metrics["VolumeDeltaPct"], 5.0) * 12.0
            + min(metrics["TradedValue"] / 10_000_000.0, 10.0) * 2.0
            - metrics["PullbackPct"] * 25.0,
            2,
        )

    def _leader_alert_row(self, metrics: Dict[str, Any], action: str, score: float, why: str) -> Dict[str, Any]:
        entry = metrics["PriceNow"]
        stop_pct = float(getattr(self, "goal_stop_pct", 1.0) or 1.0)
        target1_pct = float(getattr(self, "goal_target1_pct", 1.5) or 1.5)
        target2_pct = float(getattr(self, "goal_target2_pct", 3.0) or 3.0)
        suggested_qty = int(self.budget_amount // entry) if self.budget_amount and entry > 0 else 0
        checks = (
            "LeaderIgnition shadow | "
            f"open {metrics['OpenMovePct']:+.1f}% | day {metrics['DayChangePct']:+.1f}% | "
            f"B/S {metrics['BuyerSellerRatio']:.1f}x | "
            f"vol_delta {metrics['VolumeDeltaPct']:.1f}% | "
            f"pullback {metrics['PullbackPct']:.1f}%"
        )
        return {
            "When": self.run_at_str,
            "Symbol": metrics["Symbol"],
            "PreviousAction": "None",
            "NowAction": action,
            "PriceNow": round(entry, 2),
            "ChangePct": round(metrics["DayChangePct"], 2),
            "EntryGapPct": 0.0,
            "RSI": "",
            "BuyAbove": round(entry, 2),
            "SellIfBelow": round(entry * (1.0 - stop_pct / 100.0), 2),
            "TakeProfit1": round(entry * (1.0 + target1_pct / 100.0), 2),
            "TakeProfit2": round(entry * (1.0 + target2_pct / 100.0), 2),
            "SuggestedQty": suggested_qty,
            "SignalStrengthPct": round(score, 1),
            "Checks": checks,
            "Why": why,
        }

    def _leader_ignition_audit_row(
        self,
        metrics: Dict[str, Any],
        stage: str,
        decision: str,
        reason: str,
        score: float,
    ) -> Dict[str, Any]:
        row = {
            "When": self.run_at_str,
            "Stage": stage,
            "Decision": decision,
            "Reason": reason,
            "Score": round(self._to_float(score), 2),
        }
        row.update(metrics)
        return row

    def _load_leader_ignition_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            path = Path(getattr(self, "_leader_ignition_state_path", "") or "")
            if not path.exists():
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
            candidates = payload.get("candidates", payload) if isinstance(payload, dict) else {}
            if not isinstance(candidates, dict):
                return {}
            today = self._today_key()
            return {
                str(key).strip().upper(): value
                for key, value in candidates.items()
                if isinstance(value, dict) and str(value.get("today", today)) == today
            }
        except Exception as exc:
            default_logger().debug(f"Could not load leader ignition state: {exc}", exc_info=True)
            return {}

    def _save_leader_ignition_state(self) -> None:
        try:
            path = Path(getattr(self, "_leader_ignition_state_path", "") or "")
            if not path:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": PKDateUtilities.currentDateTime().isoformat(timespec="seconds"),
                "lane": "leader_ignition_shadow",
                "candidates": getattr(self, "_leader_candidates", {}) or {},
            }
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception as exc:
            default_logger().debug(f"Could not save leader ignition state: {exc}", exc_info=True)

    def _append_leader_ignition_audit(self, rows: List[Dict[str, Any]], today: str = "") -> None:
        """Append compact pass/reject evidence for raw-tick leader candidates."""
        if not rows:
            return
        try:
            day = str(today or PKDateUtilities.currentDateTime().strftime("%Y-%m-%d"))
        except Exception:
            day = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_leader_audit_date", "") != day:
            self._leader_audit_date = day
            self._leader_audit_seen_today = set()
        filtered_rows: List[Dict[str, Any]] = []
        seen = getattr(self, "_leader_audit_seen_today", set())
        for row in rows:
            key = "|".join(
                [
                    str(row.get("Symbol", "")).strip().upper(),
                    str(row.get("Stage", "")).strip(),
                    str(row.get("Decision", "")).strip(),
                    str(row.get("Reason", "")).strip(),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            filtered_rows.append(row)
        self._leader_audit_seen_today = seen
        if not filtered_rows:
            return
        try:
            out_dir = Path(self.output_dir) / "leader_ignition"
            out_dir.mkdir(parents=True, exist_ok=True)
            daily_path = out_dir / f"leader_ignition_{day}.csv"
            latest_path = out_dir / "leader_ignition_latest.csv"
            df = pd.DataFrame(filtered_rows)
            df.to_csv(daily_path, mode="a", header=not daily_path.exists(), index=False)
            df.to_csv(latest_path, index=False)
        except Exception as exc:
            default_logger().debug(f"Could not append leader ignition audit: {exc}", exc_info=True)

    def _send_leader_ignition_shadow_alerts(self, alerts_df: pd.DataFrame) -> None:
        """Optional Discord visibility for the shadow lane. Disabled by default."""
        if not getattr(self, "leader_ignition_discord_alerts", False):
            return
        if "discord" not in getattr(self, "alert_channels", set()):
            return
        if alerts_df is None or alerts_df.empty:
            return
        try:
            sent_at = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "**Leader Ignition Shadow - NOT A BUY SIGNAL**",
            f"Source `{self._alert_source_label()}` | Sent `{sent_at}`",
            "Raw-tick early leader research lane; no automatic orders.",
        ]
        for _, row in alerts_df.head(8).iterrows():
            lines.append(
                f"`{row['Symbol']}` {row['NowAction']} @ ₹{float(row['PriceNow']):.2f} "
                f"| day {float(row['ChangePct']):+.1f}% | score {float(row['SignalStrengthPct']):.0f}"
            )
        self._send_discord_alert("\n".join(lines))

    def _build_top_gainer_radar_alerts(self, current_simple_df: pd.DataFrame) -> pd.DataFrame:
        """Fallback for live top-gainer moves the scanner labels Hold/Watch."""
        if not getattr(self, "enable_early_breakout_alerts", True):
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        if current_simple_df is None or current_simple_df.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        try:
            today = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d")
        except Exception:
            today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_top_gainer_radar_date", "") != today:
            self._top_gainer_radar_date = today
            self._top_gainer_radar_today = self._load_daily_symbol_set("top_gainer_radar", today=today)

        live_ticks = self.actionable_runner.live_ticks or {}
        rows = []
        audit_rows = []
        for rec in current_simple_df.to_dict(orient="records"):
            sym = str(rec.get("Stock", "") or "").strip().upper()
            if not sym or sym in getattr(self, "_top_gainer_radar_today", set()):
                continue
            tick = live_ticks.get(sym, {})
            if not tick:
                continue

            price_now = self._to_float(tick.get("last_price")) or self._to_float(rec.get("Price Now"))
            change_pct = self._to_float(tick.get("change")) or self._to_float(rec.get("Change %"))
            if not (self.TOP_GAINER_RADAR_MIN_PCT <= change_pct <= self.TOP_GAINER_RADAR_MAX_PCT):
                continue

            action_text = str(rec.get("What To Do", "") or "").strip().lower()
            checks_text = str(rec.get("Checks", "") or "").strip().lower()
            why_text = str(rec.get("Why", "") or "").strip().lower()
            bearish_text = f"{action_text} | {checks_text} | {why_text}"
            strength = self._to_float(rec.get("Signal Strength %"))
            rsi_val = self._to_float(rec.get("RSI"))
            volume_ratio = self._to_float(rec.get("Volume vs Avg"))
            avg_value = self._parse_value_rupees(rec.get("Avg Daily Value"))
            day_high = self._to_float(tick.get("high", 0))
            pullback = (day_high - price_now) / price_now * 100.0 if day_high > 0 and price_now > 0 else 0.0
            total_buy = self._to_float(tick.get("total_buy_quantity", 0))
            total_sell = self._to_float(tick.get("total_sell_quantity", 0))
            bs_ratio = total_buy / total_sell if total_sell > 0 else (9.99 if total_buy > 0 else 0.0)

            def audit(decision: str, reason: str) -> None:
                audit_rows.append(
                    {
                        "When": self.run_at_str,
                        "Symbol": sym,
                        "Decision": decision,
                        "Reason": reason,
                        "Action": str(rec.get("What To Do", "") or ""),
                        "PriceNow": round(price_now, 2),
                        "ChangePct": round(change_pct, 2),
                        "SignalStrengthPct": round(strength, 2),
                        "VolumeVsAvg": round(volume_ratio, 2),
                        "BuyerSellerRatio": round(bs_ratio, 2),
                        "RSI": round(rsi_val, 2),
                        "PullbackPct": round(pullback, 2),
                        "Checks": str(rec.get("Checks", "") or ""),
                        "Why": str(rec.get("Why", "") or ""),
                    }
                )

            bearish_markers = (
                "sell / exit",
                "bearish trend",
                "kalman downtrend",
                "price below atr trailing stop",
                "uptrend broken",
                "strong downtrend",
                "macd bearish",
                "macd histogram decreasing",
                "death cross",
                "lower highs and lower lows",
            )
            if any(marker in bearish_text for marker in bearish_markers):
                audit("reject", "bearish scanner text")
                continue

            if price_now < 50.0:
                audit("reject", "price below floor")
                continue

            if avg_value and avg_value < self.min_trade_value:
                audit("reject", "liquidity below floor")
                continue
            if rsi_val and rsi_val >= 70.0:
                audit("reject", "RSI overbought")
                continue

            if pullback > 1.5:
                audit("reject", "pulled back from day high")
                continue

            if volume_ratio < float(getattr(self, "radar_min_volume_ratio", 1.0) or 1.0):
                audit("reject", "volume below radar floor")
                continue
            if bs_ratio < float(getattr(self, "radar_min_bs_ratio", 1.2) or 1.2):
                audit("reject", "buyer/seller below radar floor")
                continue
            if strength < float(getattr(self, "radar_min_strength", 38.0) or 38.0):
                audit("reject", "strength below radar floor")
                continue

            week52 = self._to_float(rec.get("Week52High"))
            if week52 and week52 > 0 and price_now < week52:
                room_to_high = (week52 - price_now) / price_now * 100.0
                if room_to_high < 2.0:
                    audit("reject", "too close to 52-week high")
                    continue

            budget_amount = getattr(self, "budget_amount", None)
            suggested_qty = int(budget_amount // price_now) if budget_amount and price_now > 0 else int(self._to_float(rec.get("Suggested Qty")))
            if suggested_qty < 1:
                audit("reject", "quantity below one")
                continue

            stop = round(price_now * (1.0 - float(getattr(self, "goal_stop_pct", 1.0) or 1.0) / 100.0), 2)
            target1 = round(price_now * (1.0 + float(getattr(self, "goal_target1_pct", 1.5) or 1.5) / 100.0), 2)
            target2 = round(price_now * (1.0 + float(getattr(self, "goal_target2_pct", 3.0) or 3.0) / 100.0), 2)
            checks = [
                f"top-gainer radar +{change_pct:.1f}%",
                f"Q:{strength:.0f}/100",
                f"vol {volume_ratio:.1f}x",
                f"B/S {bs_ratio:.1f}x",
                f"pullback {pullback:.1f}%",
            ]
            rows.append(
                {
                    "When": self.run_at_str,
                    "Symbol": sym,
                    "PreviousAction": str(rec.get("What To Do", "") or "None"),
                    "NowAction": "📡 Top Gainer Radar",
                    "PriceNow": round(price_now, 2),
                    "ChangePct": round(change_pct, 2),
                    "EntryGapPct": rec.get("Entry Gap %", ""),
                    "RSI": rec.get("RSI", ""),
                    "BuyAbove": round(price_now, 2),
                    "SellIfBelow": stop,
                    "TakeProfit1": target1,
                    "TakeProfit2": target2,
                    "SuggestedQty": suggested_qty,
                    "SignalStrengthPct": strength,
                    "Checks": " | ".join(checks),
                    "Why": str(rec.get("Why", "") or "").strip()
                    + " | Live top-gainer radar: controlled +1.5% to +3.5% move not surfaced by core scanner",
                }
            )
            audit("pass", "radar candidate passed")

        if not rows:
            self._append_top_gainer_radar_audit(audit_rows, today=today)
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        out = pd.DataFrame(rows, columns=self.ALERT_COLUMNS)
        out.sort_values(by=["SignalStrengthPct", "ChangePct", "Symbol"], ascending=[False, False, True], inplace=True)
        max_per_cycle = max(1, int(getattr(self, "TOP_GAINER_RADAR_MAX_PER_CYCLE", 4) or 4))
        out = out.head(max_per_cycle)
        for sym in out["Symbol"].astype(str).str.upper().tolist():
            self._top_gainer_radar_today.add(sym)
            self._append_daily_symbol("top_gainer_radar", sym, today=today)
        OutputControls().printOutput(
            "  [+] Top-gainer radar alert(s): "
            + ", ".join(f"{r['Symbol']} (+{float(r['ChangePct']):.1f}%)" for _, r in out.iterrows())
        )
        selected_symbols = set(out["Symbol"].astype(str).str.upper().tolist())
        for audit_row in audit_rows:
            if audit_row.get("Decision") == "pass" and audit_row.get("Symbol") not in selected_symbols:
                audit_row["Decision"] = "reject"
                audit_row["Reason"] = "cycle radar slot not selected"
        self._append_top_gainer_radar_audit(audit_rows, today=today)
        return out[self.ALERT_COLUMNS].copy()

    def _build_runner_continuation_alerts(self, current_simple_df: pd.DataFrame) -> pd.DataFrame:
        """Surface controlled continuation entries for stocks already up 5-12%.

        The regular Buy Now lane intentionally avoids chasing 5%+ moves. This
        lane covers the separate case where a stock is still near the day high
        with strong volume and enough quality to justify a smaller runner entry.
        """
        if current_simple_df is None or current_simple_df.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        try:
            today = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d")
        except Exception:
            today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_runner_alerted_date", "") != today:
            self._runner_alerted_today = set()
            self._runner_alerted_date = today

        live_ticks = self.actionable_runner.live_ticks or {}
        rows = []
        for rec in current_simple_df.to_dict(orient="records"):
            sym = str(rec.get("Stock", "")).strip().upper()
            if not sym or sym in self._runner_alerted_today:
                continue

            action = str(rec.get("What To Do", "")).strip()
            setup = str(rec.get("Momentum Setup", "")).strip()
            if action not in {"Strong Buy Now", "Buy Now"} and setup != "Momentum Buy":
                continue

            change_pct = self._to_float(rec.get("Change %"))
            if not (self.RUNNER_CONTINUATION_MIN_PCT <= change_pct <= self.RUNNER_CONTINUATION_MAX_PCT):
                continue

            volume_ratio = self._to_float(rec.get("Volume vs Avg"))
            if volume_ratio < 2.0:
                continue

            strength = self._to_float(rec.get("Signal Strength %"))
            if strength < 30.0:
                continue

            rsi_val = self._to_float(rec.get("RSI"))
            if rsi_val is not None and rsi_val >= 88.0:
                continue

            price_now = self._to_float(rec.get("Price Now"))
            if price_now <= 0:
                continue

            tick = live_ticks.get(sym, {})
            day_high = self._to_float(tick.get("high", 0))
            if day_high > 0:
                pullback = (day_high - price_now) / price_now * 100.0
                if pullback > self.RUNNER_CONTINUATION_MAX_PULLBACK_PCT:
                    continue
            else:
                pullback = 0.0

            total_buy = self._to_float(tick.get("total_buy_quantity", 0))
            total_sell = self._to_float(tick.get("total_sell_quantity", 0))
            bs_ratio = total_buy / total_sell if total_sell > 0 else (9.99 if total_buy > 0 else 0.0)
            if total_sell > 0 and bs_ratio < 0.8:
                continue

            suggested_qty = self._to_float(rec.get("Suggested Qty"))
            if suggested_qty < 1:
                continue

            entry = price_now
            stop = max(self._to_float(rec.get("Sell If Below")), round(entry * 0.965, 2))
            target1 = max(self._to_float(rec.get("Take Profit 1")), round(entry * 1.04, 2))
            target2 = max(self._to_float(rec.get("Take Profit 2")), round(entry * 1.08, 2))
            checks = str(rec.get("Checks", "")).strip()
            runner_checks = [
                checks,
                f"runner continuation +{change_pct:.1f}%",
                f"vol {volume_ratio:.1f}x",
            ]
            if day_high > 0:
                runner_checks.append(f"near high pullback {pullback:.1f}%")
            if bs_ratio > 0:
                runner_checks.append(f"B/S {bs_ratio:.1f}x")

            rows.append(
                {
                    "When": self.run_at_str,
                    "Symbol": sym,
                    "PreviousAction": action or setup or "None",
                    "NowAction": "🚀 Runner Continuation",
                    "PriceNow": entry,
                    "ChangePct": round(change_pct, 2),
                    "EntryGapPct": rec.get("Entry Gap %", ""),
                    "RSI": rec.get("RSI", ""),
                    "BuyAbove": entry,
                    "SellIfBelow": stop,
                    "TakeProfit1": target1,
                    "TakeProfit2": target2,
                    "SuggestedQty": int(suggested_qty),
                    "SignalStrengthPct": strength,
                    "Checks": " | ".join(p for p in runner_checks if p),
                    "Why": str(rec.get("Why", "")).strip()
                    + " | Continuation lane: already moving but still near day high",
                }
            )
            self._runner_alerted_today.add(sym)

        if not rows:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        out = pd.DataFrame(rows, columns=self.ALERT_COLUMNS)
        out.sort_values(by=["SignalStrengthPct", "ChangePct", "Symbol"], ascending=[False, False, True], inplace=True)
        OutputControls().printOutput(
            "  [+] Runner continuation alert(s): "
            + ", ".join(f"{r['Symbol']} (+{float(r['ChangePct']):.1f}%)" for _, r in out.head(5).iterrows())
        )
        return out[self.ALERT_COLUMNS].copy()

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

    @staticmethod
    def _env_truthy(name: str, default: str = "0") -> bool:
        return str(os.environ.get(name, default) or default).strip().lower() in {"1", "true", "yes", "y", "on"}

    def _load_autotrade_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            path = Path(getattr(self, "_autotrade_state_path", "") or "")
            if not path.exists():
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            today = self._today_key()
            candidates = payload.get("candidates", payload)
            if not isinstance(candidates, dict):
                return {}
            return {
                str(key): value
                for key, value in candidates.items()
                if isinstance(value, dict) and str(value.get("today", today)) == today
            }
        except Exception as exc:
            default_logger().debug(f"Could not load autotrade state: {exc}", exc_info=True)
            return {}

    def _save_autotrade_state(self) -> None:
        try:
            path = Path(getattr(self, "_autotrade_state_path", "") or "")
            if not path:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": PKDateUtilities.currentDateTime().isoformat(timespec="seconds"),
                "strategy": getattr(self, "autotrade_strategy", "grade_b_confirmation"),
                "candidates": getattr(self, "_autotrade_candidates", {}) or {},
            }
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception as exc:
            default_logger().debug(f"Could not save autotrade state: {exc}", exc_info=True)

    def _journal_autotrade_event(self, event: str, symbol: str = "", **details: Any) -> None:
        """Write a durable audit event for every autotrade decision/action."""
        try:
            now = PKDateUtilities.currentDateTime().isoformat(timespec="seconds")
            payload = {
                "timestamp": now,
                "event": str(event or ""),
                "symbol": str(symbol or details.get("symbol") or "").upper(),
                "strategy": getattr(self, "autotrade_strategy", "grade_b_confirmation"),
                **details,
            }
            csv_row = {
                "timestamp": payload.get("timestamp", ""),
                "event": payload.get("event", ""),
                "symbol": payload.get("symbol", ""),
                "side": payload.get("side", ""),
                "qty": payload.get("qty", ""),
                "price": payload.get("price", ""),
                "status": payload.get("status", ""),
                "order_id": payload.get("order_id", ""),
                "strategy": payload.get("strategy", ""),
                "reason": payload.get("reason", ""),
                "message": str(payload.get("message", ""))[:500],
                "details_json": json.dumps(payload, default=str, sort_keys=True),
            }
            path = Path(getattr(self, "_autotrade_journal_path", "") or "")
            jsonl_path = Path(getattr(self, "_autotrade_journal_jsonl_path", "") or "")
            if not path:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([csv_row]).to_csv(
                path,
                mode="a",
                header=not path.exists(),
                index=False,
            )
            with jsonl_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
        except Exception as exc:
            default_logger().debug(f"Could not write autotrade journal event: {exc}", exc_info=True)

    def _get_autotrade_order_manager(self):
        if self._autotrade_order_manager is None:
            from pkscreener.classes.KiteOrderManager import KiteOrderManager

            self._autotrade_order_manager = KiteOrderManager(output_dir=self.output_dir)
        return self._autotrade_order_manager

    def _run_autotrade_lane(self, alerts_df: pd.DataFrame) -> None:
        """Optional real-order lane for the historically strongest Grade-B setup."""
        if not getattr(self, "autotrade_enabled", False):
            return
        if not self.use_live_kite or not self._is_regular_market_hours():
            return
        try:
            self._register_autotrade_candidates(alerts_df)
            self._process_autotrade_entries()
            self._process_autotrade_exits()
        finally:
            self._save_autotrade_state()

    def _register_autotrade_candidates(self, alerts_df: pd.DataFrame) -> None:
        if alerts_df is None or alerts_df.empty:
            return
        today = self._today_key()
        now = PKDateUtilities.currentDateTime()
        expires_ts = now.timestamp() + float(self.autotrade_entry_window_minutes) * 60.0
        for row in alerts_df.to_dict(orient="records"):
            action = str(row.get("NowAction", "") or "")
            if "grade-b" not in action.lower():
                continue
            symbol = str(row.get("Symbol", "") or "").strip().upper()
            alert_price = self._to_float(row.get("PriceNow")) or self._to_float(row.get("BuyAbove"))
            if not symbol or alert_price <= 0:
                continue
            key = f"{today}:{symbol}"
            existing = self._autotrade_candidates.get(key, {})
            if str(existing.get("status", "")).lower() in {"bought", "open", "closed", "expired", "missed_slippage", "buy_failed", "armed_no_real_order"}:
                continue
            trigger_price = round(alert_price * (1.0 + float(self.autotrade_confirm_pct) / 100.0), 2)
            self._autotrade_candidates[key] = {
                "today": today,
                "symbol": symbol,
                "status": "waiting_confirmation",
                "strategy": self.autotrade_strategy,
                "action": action,
                "created_at": now.isoformat(timespec="seconds"),
                "created_ts": now.timestamp(),
                "expires_ts": expires_ts,
                "alert_price": round(alert_price, 2),
                "trigger_price": trigger_price,
                "confirm_pct": float(self.autotrade_confirm_pct),
                "target_pct": float(self.autotrade_target_pct),
                "stop_pct": float(self.autotrade_stop_pct),
                "strength": self._to_float(row.get("SignalStrengthPct")),
                "checks": str(row.get("Checks", "") or ""),
            }
            self._journal_autotrade_event(
                "candidate_registered",
                symbol=symbol,
                status="waiting_confirmation",
                price=round(alert_price, 2),
                trigger_price=trigger_price,
                strength=self._to_float(row.get("SignalStrengthPct")),
                reason=action,
                message=str(row.get("Checks", "") or "")[:500],
            )

    def _process_autotrade_entries(self) -> None:
        ticks = self.actionable_runner.live_ticks or {}
        if not ticks:
            return
        now = PKDateUtilities.currentDateTime()
        now_ts = now.timestamp()
        for key, candidate in list((self._autotrade_candidates or {}).items()):
            if str(candidate.get("status", "")).lower() != "waiting_confirmation":
                continue
            if float(candidate.get("expires_ts", 0.0) or 0.0) < now_ts:
                candidate["status"] = "expired"
                candidate["expired_at"] = now.isoformat(timespec="seconds")
                self._journal_autotrade_event(
                    "candidate_expired",
                    symbol=str(candidate.get("symbol", "") or ""),
                    status="expired",
                    price=self._to_float(candidate.get("alert_price")),
                    trigger_price=self._to_float(candidate.get("trigger_price")),
                    reason="entry_window_expired",
                )
                continue
            symbol = str(candidate.get("symbol", "") or "").strip().upper()
            tick = ticks.get(symbol, {}) if isinstance(ticks, dict) else {}
            live_price = self._to_float(tick.get("last_price"))
            best_ask = self._to_float(tick.get("best_ask"))
            trigger_price = self._to_float(candidate.get("trigger_price"))
            if live_price <= 0 or trigger_price <= 0 or live_price < trigger_price:
                continue
            max_entry = trigger_price * (1.0 + float(self.autotrade_max_entry_slippage_pct) / 100.0)
            if max_entry > 0 and live_price > max_entry:
                candidate["status"] = "missed_slippage"
                candidate["triggered_at"] = now.isoformat(timespec="seconds")
                candidate["triggered_price"] = round(live_price, 2)
                candidate["message"] = f"Skipped chase: live ₹{live_price:.2f} > max entry ₹{max_entry:.2f}"
                self._journal_autotrade_event(
                    "entry_skipped",
                    symbol=symbol,
                    status="missed_slippage",
                    price=round(live_price, 2),
                    trigger_price=round(trigger_price, 2),
                    max_entry=round(max_entry, 2),
                    reason="live_price_above_chase_guard",
                    message=candidate["message"],
                )
                continue
            if not (getattr(self, "autotrade_real_orders", False) and getattr(self, "autotrade_buy_enabled", False)):
                candidate["status"] = "armed_no_real_order"
                candidate["triggered_at"] = now.isoformat(timespec="seconds")
                candidate["triggered_price"] = round(live_price, 2)
                candidate["message"] = "Confirmation hit, but real auto-buy flags are disabled."
                self._journal_autotrade_event(
                    "entry_armed_no_order",
                    symbol=symbol,
                    status="armed_no_real_order",
                    price=round(live_price, 2),
                    trigger_price=round(trigger_price, 2),
                    reason="real_order_flags_disabled",
                    message=candidate["message"],
                )
                self._notify_autotrade(candidate, "armed")
                continue

            entry_price = live_price
            if best_ask > 0:
                entry_price = max(live_price, best_ask)
            elif self.autotrade_limit_buffer_pct > 0:
                entry_price = live_price * (1.0 + float(self.autotrade_limit_buffer_pct) / 100.0)
            entry_price = round(entry_price, 2)
            if max_entry > 0 and entry_price > max_entry:
                candidate["status"] = "missed_slippage"
                candidate["triggered_at"] = now.isoformat(timespec="seconds")
                candidate["triggered_price"] = round(live_price, 2)
                candidate["best_ask"] = round(best_ask, 2) if best_ask > 0 else 0.0
                candidate["entry_order_price"] = entry_price
                candidate["message"] = f"Skipped chase: buyable ₹{entry_price:.2f} > max entry ₹{max_entry:.2f}"
                self._journal_autotrade_event(
                    "entry_skipped",
                    symbol=symbol,
                    status="missed_slippage",
                    price=entry_price,
                    trigger_price=round(trigger_price, 2),
                    best_ask=round(best_ask, 2) if best_ask > 0 else 0.0,
                    max_entry=round(max_entry, 2),
                    reason="buyable_price_above_chase_guard",
                    message=candidate["message"],
                )
                continue

            stop_pct = float(candidate.get("stop_pct", self.autotrade_stop_pct) or self.autotrade_stop_pct)
            target_pct = float(candidate.get("target_pct", self.autotrade_target_pct) or self.autotrade_target_pct)
            stop_loss = round(entry_price * (1.0 - stop_pct / 100.0), 2)
            target1 = round(entry_price * (1.0 + target_pct / 100.0), 2)
            self._journal_autotrade_event(
                "entry_attempt",
                symbol=symbol,
                side="BUY",
                status="SUBMITTING",
                price=entry_price,
                trigger_price=round(trigger_price, 2),
                best_ask=round(best_ask, 2) if best_ask > 0 else 0.0,
                stop_loss=stop_loss,
                target1=target1,
                reason="grade_b_confirmation_hit",
            )
            result = self._get_autotrade_order_manager().auto_buy_from_candidate(
                symbol=symbol,
                live_price=entry_price,
                stop_loss=stop_loss,
                target1=target1,
                target2=target1,
                blind_score=float(candidate.get("strength", 0.0) or 0.0),
                reasons=(
                    f"AutoTrade {self.autotrade_strategy}: Grade-B alert confirmed "
                    f"+{self.autotrade_confirm_pct:.2f}% above alert price"
                ),
                product_type="MIS",
                holding_horizon="intraday",
                max_hold_days=0,
                bot_owned=True,
                auto_exit_allowed=True,
                strategy=self.autotrade_strategy,
            )
            candidate["triggered_at"] = now.isoformat(timespec="seconds")
            candidate["triggered_price"] = round(live_price, 2)
            candidate["best_ask"] = round(best_ask, 2) if best_ask > 0 else 0.0
            candidate["entry_order_price"] = entry_price
            candidate["target1"] = target1
            candidate["stop_loss"] = stop_loss
            candidate["order_status"] = str(getattr(result, "status", "") or "")
            candidate["order_message"] = str(getattr(result, "message", "") or "")
            candidate["order_id"] = str(getattr(result, "order_id", "") or "")
            candidate["status"] = "bought" if getattr(result, "success", False) else "buy_failed"
            self._journal_autotrade_event(
                "entry_result",
                symbol=symbol,
                side="BUY",
                qty=int(getattr(result, "qty", 0) or 0),
                price=float(getattr(result, "price", 0.0) or 0.0),
                status=str(getattr(result, "status", "") or ""),
                order_id=str(getattr(result, "order_id", "") or ""),
                stop_loss=stop_loss,
                target1=target1,
                reason="buy" if getattr(result, "success", False) else "buy_failed",
                message=str(getattr(result, "message", "") or ""),
            )
            self._notify_autotrade(candidate, "buy" if getattr(result, "success", False) else "buy_failed")

    def _process_autotrade_exits(self) -> None:
        if not (
            getattr(self, "autotrade_real_orders", False)
            and getattr(self, "autotrade_sell_enabled", False)
        ):
            return
        manager = self._get_autotrade_order_manager()
        if not getattr(manager, "auto_trade", False):
            return
        try:
            manager.execute_sell_signals(self.actionable_runner.live_ticks or {})
        except Exception as exc:
            default_logger().warning(f"Autotrade exit check failed: {exc}", exc_info=True)

    def _notify_autotrade(self, candidate: Dict[str, Any], event: str) -> None:
        if "discord" not in getattr(self, "alert_channels", set()):
            return
        symbol = str(candidate.get("symbol", "") or "").upper()
        if not symbol:
            return
        if event == "armed":
            line = (
                f"🤖 AutoTrade armed only, no real order: {symbol} "
                f"confirmed at ₹{self._to_float(candidate.get('triggered_price')):.2f}. "
                "Real order flags are disabled."
            )
        elif event == "buy":
            line = (
                f"🤖 AutoTrade BUY placed: {symbol} @ ₹{self._to_float(candidate.get('triggered_price')):.2f} "
                f"| target +{self._to_float(candidate.get('target_pct')):.2f}% "
                f"| stop -{self._to_float(candidate.get('stop_pct')):.2f}%"
            )
        else:
            line = (
                f"🤖 AutoTrade BUY failed: {symbol} "
                f"| {str(candidate.get('order_message', '') or candidate.get('message', ''))[:300]}"
            )
        self._send_discord_alert(line)

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
                or "breakout" in action_lower
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
                change_at_entry=self._to_float(row.get("ChangePct")),
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
        if "breakout" in action_lower:
            return "signal_monitor_early_breakout"
        if "anomaly" in action_lower or "runner" in action_lower or "pre-move" in action_lower:
            return "signal_monitor_anomaly"
        if "momentum" in action_lower:
            return "signal_monitor_momentum"
        return "signal_monitor_buy"

    @staticmethod
    def _paper_grade_for_action(action: str) -> str:
        action_lower = str(action or "").lower()
        if "breakout" in action_lower:
            return "EARLY"
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
            self._send_discord_monitor_alerts(
                alerts_df,
                summary,
                current_simple_df=getattr(self, "_last_simple_df", None),
            )

    def _send_monitor_heartbeat(self, summary_df: pd.DataFrame, alerts_df: pd.DataFrame) -> None:
        """Send periodic Discord status so quiet periods do not look like downtime."""
        if "discord" not in self.alert_channels:
            return
        if not self._is_regular_market_hours():
            return
        interval_minutes = float(getattr(self, "monitor_heartbeat_minutes", 10.0) or 0.0)
        if interval_minutes <= 0:
            return
        now_ts = time.time()
        if now_ts - float(getattr(self, "_last_discord_heartbeat_ts", 0.0) or 0.0) < interval_minutes * 60:
            return

        summary = summary_df.iloc[0].to_dict() if summary_df is not None and not summary_df.empty else {}
        goal_symbols = self._load_daily_symbol_set("goal_burst", today=self._today_key())
        max_goal = max(1, int(getattr(self, "goal_max_trades_per_day", 1) or 1))
        fresh_alerts = int(summary.get("FreshAlertsThisRun", 0) or 0)
        buy_alerts = 0
        if alerts_df is not None and not alerts_df.empty and "NowAction" in alerts_df.columns:
            buy_alerts = int(alerts_df["NowAction"].astype(str).apply(self._is_goal_mode_buy_action).sum())

        try:
            sent_at = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        status_line = "No fresh goal buy sent this cycle"
        if buy_alerts > 0:
            status_line = f"Fresh goal buy alert(s) sent this cycle: `{buy_alerts}`"
        elif fresh_alerts > 0:
            status_line = f"Fresh non-buy alert(s) this cycle: `{fresh_alerts}`"

        suppressed = list(getattr(self, "_last_goal_suppressed_reasons", []) or [])
        suppressed_line = "Goal suppressions: `none this cycle`"
        if suppressed:
            suppressed_line = "Goal suppressions: " + ", ".join(f"`{item}`" for item in suppressed[:6])
            if len(suppressed) > 6:
                suppressed_line += f" ... +{len(suppressed) - 6}"

        lines = [
            "**Intraday Goal Scanner Heartbeat**",
            f"Source `{self._alert_source_label()}` | Sent `{sent_at}`",
            f"Universe `{self.universe}` | Scanner `active` | Scanned `{int(summary.get('ScannedSymbols', 0) or 0)}`",
            (
                f"Buy Now `{int(summary.get('BuyNowCount', 0) or 0)}` | "
                f"Momentum `{int(summary.get('MomentumBuyCount', 0) or 0)}` | "
                f"Fresh `{fresh_alerts}`"
            ),
            f"Goal alerts today `{len(goal_symbols)}/{max_goal}`",
            status_line,
            suppressed_line,
        ]
        self._send_discord_alert("\n".join(lines))
        self._last_discord_heartbeat_ts = now_ts

    def _is_regular_market_hours(self) -> bool:
        """Only send live monitor alerts during NSE regular market hours."""
        try:
            now_ist = PKDateUtilities.currentDateTime()
        except Exception:
            now_ist = datetime.now()
        minute_of_day = int(now_ist.hour) * 60 + int(now_ist.minute)
        return self.MARKET_OPEN_MINUTES_IST <= minute_of_day <= self.MARKET_CLOSE_MINUTES_IST

    def _alert_source_label(self) -> str:
        return str(
            os.environ.get("SIGNALMONITOR_ALERT_SOURCE", "")
            or os.environ.get("SIGNALMONITOR_ALERT_SOURCE_LABEL", "")
            or "unknown launcher"
        ).strip()

    def _send_discord_monitor_alerts(
        self,
        alerts_df: pd.DataFrame,
        summary: Dict,
        current_simple_df: Optional[pd.DataFrame] = None,
    ) -> None:
        """Send all ranked Discord alerts without silently dropping symbols on length limits."""
        remaining_df = alerts_df.copy() if alerts_df is not None else pd.DataFrame()
        if remaining_df.empty:
            return

        sent_any = False
        for _ in range(12):
            if remaining_df.empty:
                break
            discord_message = self._build_discord_monitor_message(
                remaining_df,
                summary,
                current_simple_df=current_simple_df,
            )
            sent_symbols = self._symbols_present_in_discord_message(remaining_df, discord_message)
            if not sent_symbols:
                default_logger().warning("Discord alert payload omitted every remaining symbol; sending first row fallback")
                remaining_df = remaining_df.head(1).copy()
                discord_message = self._build_discord_monitor_message(
                    remaining_df,
                    summary,
                    current_simple_df=None,
                )
                sent_symbols = self._symbols_present_in_discord_message(remaining_df, discord_message)
                if not sent_symbols:
                    break

            sent_df = remaining_df[
                remaining_df["Symbol"].astype(str).str.upper().isin(sent_symbols)
            ].copy()
            self._journal_discord_alerts(sent_df, summary, discord_message)
            self._send_discord_alert(discord_message)
            sent_any = True
            remaining_df = remaining_df[
                ~remaining_df["Symbol"].astype(str).str.upper().isin(sent_symbols)
            ].copy()

        if sent_any and not remaining_df.empty:
            missing = ", ".join(remaining_df["Symbol"].astype(str).str.upper().tolist())
            default_logger().warning(f"Discord alert send loop ended with unsent symbol(s): {missing}")

    @staticmethod
    def _symbols_present_in_discord_message(alerts_df: pd.DataFrame, message: str) -> Set[str]:
        if alerts_df is None or alerts_df.empty or "Symbol" not in alerts_df.columns:
            return set()
        text = str(message or "")
        sent_symbols: Set[str] = set()
        for symbol in alerts_df["Symbol"].astype(str).str.upper():
            if f"`{symbol}`" in text:
                sent_symbols.add(symbol)
        return sent_symbols

    def _journal_discord_alerts(self, alerts_df: pd.DataFrame, summary: Dict, message: str) -> None:
        """Persist every Discord alert attempt for live/paper audit."""
        if alerts_df is None or alerts_df.empty:
            return
        try:
            sent_at = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        source_label = self._alert_source_label()
        alert_batch_id = uuid.uuid4().hex[:12]
        journal_dir = Path(self.output_dir) / "intraday_goal_alerts"
        journal_dir.mkdir(parents=True, exist_ok=True)
        today = sent_at[:10]
        journal_path = journal_dir / f"intraday_goal_alerts_{today}.csv"
        latest_path = journal_dir / "intraday_goal_alerts_latest.csv"
        rows = []
        for _, row in alerts_df.iterrows():
            checks = str(row.get("Checks", "") or "")
            buy_above = self._to_float(row.get("BuyAbove"))
            rows.append(
                {
                    "AlertBatchId": alert_batch_id,
                    "Source": source_label,
                    "Universe": self.universe,
                    "GeneratedAt": self.run_at_str,
                    "SentAt": sent_at,
                    "Symbol": str(row.get("Symbol", "") or "").strip().upper(),
                    "Action": str(row.get("NowAction", "") or "").strip(),
                    "PriceNow": self._to_float(row.get("PriceNow")),
                    "BuyAbove": buy_above,
                    "BuyZoneHigh": round(buy_above * 1.015, 2) if buy_above > 0 else 0.0,
                    "StopLoss": self._to_float(row.get("SellIfBelow")),
                    "Target1": self._to_float(row.get("TakeProfit1")),
                    "Target2": self._to_float(row.get("TakeProfit2")),
                    "SuggestedQty": self._safe_int(row.get("SuggestedQty")),
                    "SignalStrengthPct": self._to_float(row.get("SignalStrengthPct")),
                    "ChangePct": self._to_float(row.get("ChangePct")),
                    "EntryGapPct": self._to_float(row.get("EntryGapPct")),
                    "RSI": self._to_float(row.get("RSI")),
                    "IsGoalMode": "goalmode" in checks.lower(),
                    "FreshAlertsThisRun": int(summary.get("FreshAlertsThisRun", 0) or 0),
                    "ScannedSymbols": int(summary.get("ScannedSymbols", 0) or 0),
                    "Checks": checks,
                    "Why": str(row.get("Why", "") or "").strip(),
                    "DiscordMessage": str(message or "")[:1900],
                }
            )
        if not rows:
            return
        df = pd.DataFrame(rows)
        header = not journal_path.exists()
        df.to_csv(journal_path, mode="a", header=header, index=False)
        df.to_csv(latest_path, index=False)
        OutputControls().printOutput(f"  [+] Saved Discord alert journal: {journal_path}")

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

    @staticmethod
    def _is_direct_buy_action(action: str) -> bool:
        return str(action or "").strip() in {"Strong Buy Now", "Buy Now"}

    @staticmethod
    def _is_anomaly_action(action: str) -> bool:
        action_text = str(action or "").strip()
        action_lower = action_text.lower()
        return (
            "anomaly" in action_lower
            or "runner" in action_lower
            or "pre-move" in action_lower
            or action_text.startswith("🔥")
            or action_text.startswith("⚡")
            or action_text.startswith("👀")
        )

    def _rank_discord_alert_rows(self, alerts_df: pd.DataFrame, max_alerts: int) -> pd.DataFrame:
        """Put fresh direct Buy Now alerts before anomaly/runner alerts."""
        if alerts_df is None or alerts_df.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        work = alerts_df.copy()
        work["_discord_rank"] = work["NowAction"].astype(str).apply(
            lambda action: (
                0 if action == "Strong Buy Now"
                else 1 if action == "Buy Now"
                else 2 if "Early Breakout" in action
                else 3 if action == "Momentum Buy"
                else 4 if self._is_anomaly_action(action)
                else 5 if action == "Sell / Exit"
                else 6
            )
        )
        work["_discord_strength"] = pd.to_numeric(work.get("SignalStrengthPct", 0), errors="coerce").fillna(0.0)
        work.sort_values(
            by=["_discord_rank", "_discord_strength", "Symbol"],
            ascending=[True, False, True],
            inplace=True,
        )
        return work.drop(columns=["_discord_rank", "_discord_strength"], errors="ignore").head(max_alerts)

    def _current_buy_board_rows(
        self,
        current_simple_df: Optional[pd.DataFrame],
        exclude_symbols: Set[str],
        limit: int = 2,
    ) -> pd.DataFrame:
        """Return active Buy Now board rows when fresh alerts are anomaly-only."""
        if current_simple_df is None or current_simple_df.empty or "What To Do" not in current_simple_df.columns:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        board = current_simple_df[
            current_simple_df["What To Do"].astype(str).isin(["Strong Buy Now", "Buy Now"])
        ].copy()
        if board.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)
        if "Stock" in board.columns and exclude_symbols:
            board = board[~board["Stock"].astype(str).str.upper().isin(exclude_symbols)]
        if board.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        board["_strength"] = pd.to_numeric(board.get("Signal Strength %", 0), errors="coerce").fillna(0.0)
        board["_gap"] = pd.to_numeric(board.get("Entry Gap %", 999), errors="coerce").fillna(999.0)
        board["_change"] = pd.to_numeric(board.get("Change %", 0), errors="coerce").fillna(0.0)
        board["_rsi"] = pd.to_numeric(board.get("RSI", 0), errors="coerce").fillna(0.0)

        # Apply the same blind-buy filters as the live alert path — the board must
        # only show stocks that would pass the alert gate, otherwise it misleads.
        # Also enforce the strength floor (40% — matches fresh alert path) so we
        # never surface low-conviction names like JKPAPER 21.7% or JBMA 21.5%.
        board = board[
            (board["_change"] < 5.0)
            & ((board["_rsi"] == 0.0) | (board["_rsi"] < 70.0))
            & (board["_strength"] >= 40.0)
        ]
        if board.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        # Alert-fatigue guard: each symbol surfaces in the board ONCE per day.
        # Reset the set at day boundary so tomorrow starts fresh.
        try:
            _today = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d")
        except Exception:
            _today = datetime.now().strftime("%Y-%m-%d")
        if self._board_shown_date != _today:
            self._board_shown_date = _today
            self._board_shown_today = self._load_daily_symbol_set("buy_board", today=_today)
        if "Stock" in board.columns and self._board_shown_today:
            board = board[~board["Stock"].astype(str).str.upper().isin(self._board_shown_today)]
        if board.empty:
            return pd.DataFrame(columns=self.ALERT_COLUMNS)

        board.sort_values(
            by=["What To Do", "_gap", "_strength", "_change", "Stock"],
            ascending=[False, True, False, True, True],
            inplace=True,
        )

        rows = []
        for rec in board.head(limit).to_dict(orient="records"):
            _sym = str(rec.get("Stock", "")).strip().upper()
            self._board_shown_today.add(_sym)
            self._append_daily_symbol("buy_board", _sym, today=_today)
            rows.append(
                {
                    "When": self.run_at_str,
                    "Symbol": _sym,
                    "PreviousAction": "Already Active",
                    "NowAction": str(rec.get("What To Do", "")).strip(),
                    "PriceNow": rec.get("Price Now", ""),
                    "ChangePct": rec.get("Change %", ""),
                    "EntryGapPct": rec.get("Entry Gap %", ""),
                    "RSI": rec.get("RSI", ""),
                    "BuyAbove": rec.get("Buy Above", ""),
                    "SellIfBelow": rec.get("Sell If Below", ""),
                    "TakeProfit1": rec.get("Take Profit 1", ""),
                    "TakeProfit2": rec.get("Take Profit 2", ""),
                    "SuggestedQty": rec.get("Suggested Qty", 0),
                    "SignalStrengthPct": rec.get("Signal Strength %", 0),
                    "Checks": rec.get("Checks", ""),
                    "Why": rec.get("Why", ""),
                }
            )
        return pd.DataFrame(rows, columns=self.ALERT_COLUMNS)

    def _build_discord_monitor_message(
        self,
        alerts_df: pd.DataFrame,
        summary: Dict,
        max_alerts: int = 12,
        current_simple_df: Optional[pd.DataFrame] = None,
    ) -> str:
        """Build a phone-friendly Discord webhook message."""
        source_label = self._alert_source_label()
        try:
            sent_dt = PKDateUtilities.currentDateTime()
            sent_at = sent_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            sent_dt = datetime.now()
            sent_at = sent_dt.strftime("%Y-%m-%d %H:%M:%S")
        try:
            alert_age_seconds = max(0, int((sent_dt - self.run_at).total_seconds()))
        except Exception:
            alert_age_seconds = 0
        sections = [
            f"**Intraday Goal Scanner Alert**",
            f"Source `{source_label}`",
            (
                f"Universe `{self.universe}` | Generated `{self.run_at_str}` | Sent `{sent_at}` | "
                f"Age `{alert_age_seconds}s` | Fresh `{int(summary.get('FreshAlertsThisRun', 0) or 0)}`"
            ),
            (
                f"Scanned `{int(summary.get('ScannedSymbols', 0) or 0)}` | "
                f"Strong Buy Now `{int(summary.get('StrongBuyNowCount', 0) or 0)}` | "
                f"Buy Now `{int(summary.get('BuyNowCount', 0) or 0)}` | "
                f"Exit States `{int(summary.get('SellNowCount', 0) or 0)}`"
            ),
        ]

        if self.last_guardrail_status is not None and self.last_guardrail_status.buy_paused:
            sections.append(
                "Guardrail `" + "; ".join(self.last_guardrail_status.reasons) + "`"
            )

        ranked_alerts = self._rank_discord_alert_rows(alerts_df, max_alerts=max_alerts)
        has_fresh_direct_buy = (
            not ranked_alerts.empty
            and ranked_alerts["NowAction"].astype(str).apply(self._is_direct_buy_action).any()
        )
        if not has_fresh_direct_buy:
            exclude_symbols = (
                set(alerts_df["Symbol"].astype(str).str.upper())
                if alerts_df is not None and not alerts_df.empty and "Symbol" in alerts_df.columns
                else set()
            )
            board_rows = self._current_buy_board_rows(current_simple_df, exclude_symbols, limit=2)
            if not board_rows.empty:
                sections.append("**Current Buy Now board** _(already active; not new this cycle)_")
                ranked_alerts = pd.concat([board_rows, ranked_alerts], ignore_index=True).head(max_alerts)

        for _, row in ranked_alerts.iterrows():
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
            elif "Early Breakout" in action:
                action_emoji = "🌱"
            elif "Radar" in action:
                action_emoji = "📡"
            elif action == "Volume Anomaly":
                action_emoji = "🔥"
            elif action.startswith("🔥") or action.startswith("⚡") or action.startswith("👀"):
                # Anomaly buy candidates — green dot for "go/buy"
                action_emoji = "🟢"
            elif action == "Sell / Exit":
                action_emoji = "🔴"
            elif "OB" in action:
                action_emoji = "🌊"
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
            trade_lane_line = self._trade_lane_line(action, checks=checks)
            zone_label = "BUY ZONE" if self._is_direct_buy_action(action) else "ENTRY ZONE"
            if _buy_above_val > 0:
                buy_zone_line = (
                    f"🎯 **{zone_label}**: ₹{self._fmt_price(_buy_above_val)} – ₹{self._fmt_price(_buy_ceiling_val)}  "
                    f"(skip if price > ₹{self._fmt_price(_buy_ceiling_val)})"
                )
            else:
                buy_zone_line = f"🎯 **{zone_label}**: ₹{self._fmt_price(row['BuyAbove'])}"
            # Compute SL/T1/T2 percentages off the trigger price (BuyAbove),
            # which is what Kite GTT uses. Groww users can read the ₹ amount;
            # Kite users can read the % offset.
            try:
                _sl_val = float(row.get("SellIfBelow", 0) or 0)
                _t1_val = float(row.get("TakeProfit1", 0) or 0)
                _t2_val = float(row.get("TakeProfit2", 0) or 0)
            except (TypeError, ValueError):
                _sl_val = _t1_val = _t2_val = 0.0
            if _buy_above_val > 0:
                _sl_pct = (_sl_val - _buy_above_val) / _buy_above_val * 100.0 if _sl_val > 0 else 0.0
                _t1_pct = (_t1_val - _buy_above_val) / _buy_above_val * 100.0 if _t1_val > 0 else 0.0
                _t2_pct = (_t2_val - _buy_above_val) / _buy_above_val * 100.0 if _t2_val > 0 else 0.0
                sl_t_line = (
                    f"Stop Loss `₹{self._fmt_price(_sl_val)}` (`{_sl_pct:+.1f}%`) | "
                    f"Target 1 `₹{self._fmt_price(_t1_val)}` (`{_t1_pct:+.1f}%`) | "
                    f"Target 2 `₹{self._fmt_price(_t2_val)}` (`{_t2_pct:+.1f}%`){rr_text}"
                )
            else:
                sl_t_line = (
                    f"Stop Loss `{self._fmt_price(row['SellIfBelow'])}` | "
                    f"Target 1 `{self._fmt_price(row['TakeProfit1'])}` | "
                    f"Target 2 `{self._fmt_price(row['TakeProfit2'])}`{rr_text}"
                )
            qty_label = self._qty_label(action)
            block_lines = [
                f"{action_emoji} **{action}** `{row['Symbol']}` — Strength `{strength}`",
            ]
            if trade_lane_line:
                block_lines.append(trade_lane_line)
            block_lines.extend([
                buy_zone_line,
                f"At scan: ₹{self._fmt_price(row['PriceNow'])}{day_pct_text}",
                sl_t_line,
                f"{qty_label} `{qty}`",
            ])
            if "goalmode" in checks.lower():
                block_lines.append("Plan: book `70-80%` at Target 1, keep only runner for Target 2, move runner stop near entry")
            if "OB" in action:
                block_lines.append("**⚡ INTRADAY SCALP ONLY** — exit before 3:15 PM, not a swing trade")
            if checks:
                block_lines.append(f"Quality: `{self._simplify_checks_text(checks)}`")
            if why:
                block_lines.append(f"Why: {self._simplify_why_text(why)}")
            sections.append("\n".join(block_lines))

        msg = "\n\n".join(sections)
        if len(msg) <= 1900:
            return msg
        # Truncate at the last complete section boundary so we never cut mid-alert.
        # A partial alert (missing Qty, Target, Why) is worse than a missing alert.
        cut = msg[:1900].rfind("\n\n")
        return msg[:cut] if cut > 0 else msg[:1900]

    def _trade_lane_line(self, action: str, checks: str = "") -> str:
        """Make risk lane explicit so alerts are not all read as swing buys."""
        text = str(action or "").strip()
        if "goalmode" in str(checks or "").lower():
            return "**🎯 DAY-GOAL SCALP + RUNNER** — intraday only; lock the first spike, runner is optional"
        if "Early Breakout" in text:
            return "**🌱 EARLY BREAKOUT SCALP** — early +1% to +3.5% move; take the first target seriously"
        if "Radar" in text:
            return "**📡 TOP-GAINER RADAR SCALP** — live +1.5% to +3.5% mover; use only inside entry zone"
        if text in {"Strong Buy Now", "Buy Now"}:
            return "**✅ SWING BUY CANDIDATE** — enter only inside the zone with stop in place"
        if "OB" in text:
            return "**⚡ INTRADAY SCALP** — order-book imbalance only, not a swing buy"
        if text == "Momentum Buy" or "Runner" in text:
            return "**⚠️ RUNNER / CONTINUATION** — reduced size; avoid if momentum fades"
        if text.startswith("⚡") or text.startswith("👀") or "Anomaly" in text:
            return "**⚠️ ANOMALY WATCH** — alert-only until price confirms; use smaller size"
        return ""

    def _qty_label(self, action: str) -> str:
        text = str(action or "").strip()
        if text in {"Strong Buy Now", "Buy Now"}:
            return "Qty"
        if "OB" in text:
            return "Scalp qty"
        return "Max qty"

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

    def _append_top_gainer_radar_audit(self, rows: List[Dict], today: str = "") -> None:
        """Append compact pass/reject evidence for radar-window movers."""
        if not rows:
            return
        try:
            day = str(today or PKDateUtilities.currentDateTime().strftime("%Y-%m-%d"))
        except Exception:
            day = datetime.now().strftime("%Y-%m-%d")
        try:
            path = os.path.join(self.output_dir, f"signalmonitor_top_gainer_radar_audit_{day}.csv")
            df = pd.DataFrame(rows)
            header = not os.path.exists(path)
            df.to_csv(path, mode="a", header=header, index=False)
        except Exception as exc:
            default_logger().debug(f"Could not append top-gainer radar audit: {exc}")

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

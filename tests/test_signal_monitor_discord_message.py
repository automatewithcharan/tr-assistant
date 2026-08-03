import pandas as pd
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.LiveFeatureRecorder import LiveFeatureRecorder
from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from pkscreener.classes.TopGainerLifecycleRecorder import TopGainerLifecycleRecorder
import pytest


def _make_runner() -> SignalMonitorRunner:
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.universe = "all"
    runner.run_at_str = "2026-05-08 10:15:00"
    runner.run_at = datetime(2026, 5, 8, 10, 15, 0)
    runner.output_dir = tempfile.mkdtemp()
    runner.last_guardrail_status = None
    runner.alert_channels = {"discord"}
    runner.use_live_kite = False
    runner._board_shown_today = set()
    runner._board_shown_date = ""
    runner.enable_live_feature_recorder = False
    runner._live_feature_recorder = None
    runner.enable_intraday_goal_mode = True
    runner.goal_selector_validated = True
    runner.goal_max_trades_per_day = 1
    runner.goal_daily_capital = 200000.0
    runner.goal_stop_pct = 1.0
    runner.goal_target1_pct = 1.5
    runner.goal_target2_pct = 3.0
    runner.goal_anomaly_only = False
    runner.goal_allow_direct_buy = True
    runner.anomaly_alert_only = False
    runner.anomaly_alert_only_max_per_day = 20
    runner.anomaly_alert_stop_pct = 1.0
    runner.anomaly_alert_target1_pct = 1.0
    runner.anomaly_alert_target2_pct = 3.0
    runner.radar_max_alerts_per_day = 3
    runner.monitor_heartbeat_minutes = 10
    runner._last_discord_heartbeat_ts = 0.0
    runner._last_goal_suppressed_reasons = []
    runner.min_trade_value = 50000000.0
    runner.budget_amount = 50000.0
    runner._top_gainer_radar_date = ""
    runner._top_gainer_radar_today = set()
    runner.enable_leader_ignition = True
    runner.leader_ignition_discord_alerts = False
    runner._leader_ignition_date = ""
    runner._leader_ignition_today = set()
    runner._leader_reclaim_date = ""
    runner._leader_reclaim_today = set()
    runner._leader_candidates = {}
    runner._leader_last_ticks_by_symbol = {}
    runner._leader_ignition_state_path = f"{runner.output_dir}/signalmonitor_leader_ignition_state.json"
    return runner


@pytest.mark.parametrize("lane", ["early_breakout", "top_gainer_radar"])
def test_direct_lifecycle_recorder_failure_is_non_blocking(lane):
    runner = _make_runner()

    class FailingRecorder:
        def record_observation(self, *args, **kwargs):
            raise OSError("evidence storage unavailable")

    runner._top_gainer_lifecycle_recorder = FailingRecorder()
    assert runner._record_direct_lane_observation(
        lane, {"symbol": "FAIL", "feature_available_at": runner.run_at}, "qualified"
    ) == ""


def test_paper_tracking_env_override_can_enable_fast_mode(monkeypatch):
    monkeypatch.setattr(SignalMonitorRunner, "FAST_MODE", True)
    monkeypatch.setenv("SIGNALMONITOR_ENABLE_PAPER_TRACKING", "1")

    runner = SignalMonitorRunner(
        config_manager=None,
        universe="all",
        limit=0,
        use_live_kite=True,
        alert_channels="discord",
    )

    assert runner.fast_mode is True
    assert runner.enable_paper_tracking is True


def test_no_changed_symbols_does_not_require_scorecard(monkeypatch):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.universe = "all"
    runner.limit = 0
    runner.streaming_mode = True
    runner.use_live_kite = True
    runner._preload_done = True
    runner._cycle_number = 1
    runner.reset_state = False
    runner.performance_tracker = None
    runner.ALERT_COLUMNS = SignalMonitorRunner.ALERT_COLUMNS
    runner.actionable_runner = SimpleNamespace(
        get_universe_symbols=lambda: ["ABC"],
    )

    monkeypatch.setattr(runner, "_load_previous_state", lambda: pd.DataFrame())
    monkeypatch.setattr(runner, "_load_previous_simple", lambda: pd.DataFrame())
    monkeypatch.setattr(runner, "_select_symbols_for_cycle", lambda symbols, previous: [])
    monkeypatch.setattr(runner, "_record_intraday_event_shadow", lambda: None)
    monkeypatch.setattr(runner, "_run_leader_ignition_lane", lambda: pd.DataFrame())
    monkeypatch.setattr(runner, "_send_leader_ignition_shadow_alerts", lambda frame: None)
    monkeypatch.setattr(runner, "_record_live_feature_snapshot", lambda frame: None)
    monkeypatch.setattr(
        runner,
        "_build_summary_from_simple",
        lambda previous, total, alerts: pd.DataFrame([{"ScannedSymbols": total}]),
    )
    monkeypatch.setattr(runner, "_annotate_summary_timing", lambda summary, started, scanned: summary)
    monkeypatch.setattr(runner, "_save_outputs", lambda *args: None)
    monkeypatch.setattr(runner, "_print_summary", lambda *args: None)
    monkeypatch.setattr(runner, "_close_paper_trades_if_eod", lambda: None)
    monkeypatch.setattr(runner, "_evaluate_event_shadow_outcomes_if_eod", lambda: None)

    _, _, alerts, summary = runner.run()

    assert alerts.empty
    assert int(summary.iloc[0]["ScannedSymbols"]) == 1
    assert runner._cycle_number == 2


def _alert(symbol: str, action: str, strength: float) -> dict:
    return {
        "When": "2026-05-08 10:15:00",
        "Symbol": symbol,
        "PreviousAction": "None",
        "NowAction": action,
        "PriceNow": 100.0,
        "ChangePct": 2.0,
        "EntryGapPct": 0.3,
        "RSI": 65.0,
        "BuyAbove": 101.0,
        "SellIfBelow": 95.0,
        "TakeProfit1": 110.0,
        "TakeProfit2": 120.0,
        "SuggestedQty": 10,
        "SignalStrengthPct": strength,
        "Checks": "confidence ok, near trigger, liquidity ok",
        "Why": "Price above ATR trailing stop",
    }


def test_discord_message_prioritizes_fresh_buy_now_over_anomaly():
    runner = _make_runner()
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(runner.output_dir)
    alerts_df = pd.DataFrame(
        [
            _alert("AEROFLEX", "⚡ Grade-B Anomaly", 65.0),
            _alert("WELCORP", "Buy Now", 42.0),
        ],
        columns=runner.ALERT_COLUMNS,
    )

    message = runner._build_discord_monitor_message(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 2, "StrongBuyNowCount": 0, "BuyNowCount": 12, "SellNowCount": 0},
    )

    assert message.index("`WELCORP`") < message.index("`AEROFLEX`")


def test_discord_message_includes_source_and_sent_time(monkeypatch):
    monkeypatch.setenv("SIGNALMONITOR_ALERT_SOURCE", "intraday-goal-paper")
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 10, 15, 9)),
    )
    runner = _make_runner()
    alerts_df = pd.DataFrame([_alert("WELCORP", "Buy Now", 42.0)], columns=runner.ALERT_COLUMNS)

    message = runner._build_discord_monitor_message(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 1, "StrongBuyNowCount": 0, "BuyNowCount": 1, "SellNowCount": 0},
    )

    assert "Source `intraday-goal-paper`" in message
    assert "Generated `2026-05-08 10:15:00`" in message
    assert "Sent `2026-05-08 10:15:09`" in message


def test_discord_alert_journal_persists_structured_rows(monkeypatch):
    monkeypatch.setenv("SIGNALMONITOR_ALERT_SOURCE", "intraday-goal-paper")
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 10, 16, 5)),
    )
    runner = _make_runner()
    row = _alert("WELCORP", "Buy Now", 42.0)
    row["Checks"] = "GoalMode✓ day basket | confidence ok"
    alerts_df = pd.DataFrame([row], columns=runner.ALERT_COLUMNS)

    runner._journal_discord_alerts(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 1},
        "discord message",
    )

    journal = pd.read_csv(f"{runner.output_dir}/intraday_goal_alerts/intraday_goal_alerts_2026-05-08.csv")
    assert list(journal["Symbol"]) == ["WELCORP"]
    assert journal.iloc[0]["Source"] == "intraday-goal-paper"
    assert journal.iloc[0]["GeneratedAt"] == "2026-05-08 10:15:00"
    assert journal.iloc[0]["SentAt"] == "2026-05-08 10:16:05"
    assert journal.iloc[0]["BuyZoneHigh"] == 102.51
    assert bool(journal.iloc[0]["IsGoalMode"]) is True


def test_discord_message_shows_current_buy_board_when_fresh_alert_is_anomaly_only():
    runner = _make_runner()
    alerts_df = pd.DataFrame(
        [_alert("AEROFLEX", "⚡ Grade-B Anomaly", 65.0)],
        columns=runner.ALERT_COLUMNS,
    )
    current_simple_df = pd.DataFrame(
        [
            {
                "Stock": "WELCORP",
                "What To Do": "Buy Now",
                "Price Now": 100.0,
                "Change %": 2.0,
                "Entry Gap %": 0.2,
                "RSI": 65.0,
                "Buy Above": 101.0,
                "Sell If Below": 95.0,
                "Take Profit 1": 110.0,
                "Take Profit 2": 120.0,
                "Suggested Qty": 10,
                "Signal Strength %": 42.0,
                "Checks": "confidence ok, near trigger, liquidity ok",
                "Why": "Price above ATR trailing stop",
            }
        ]
    )

    message = runner._build_discord_monitor_message(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 1, "StrongBuyNowCount": 0, "BuyNowCount": 1, "SellNowCount": 0},
        current_simple_df=current_simple_df,
    )

    assert "Current Buy Now board" in message
    assert message.index("`WELCORP`") < message.index("`AEROFLEX`")


def test_runner_continuation_surfaces_five_to_twelve_percent_runner():
    runner = _make_runner()
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(runner.output_dir)
    runner.actionable_runner = SimpleNamespace(
        live_ticks={
            "BBOX": {
                "high": 110.5,
                "total_buy_quantity": 250000,
                "total_sell_quantity": 120000,
            }
        }
    )
    runner._runner_alerted_today = set()
    runner._runner_alerted_date = ""

    current_simple_df = pd.DataFrame(
        [
            {
                "Stock": "BBOX",
                "What To Do": "Buy Now",
                "Momentum Setup": "Momentum Buy",
                "Price Now": 110.0,
                "Change %": 8.5,
                "Volume vs Avg": 3.0,
                "Signal Strength %": 42.0,
                "RSI": 72.0,
                "Entry Gap %": 0.2,
                "Buy Above": 111.0,
                "Sell If Below": 104.0,
                "Take Profit 1": 118.0,
                "Take Profit 2": 124.0,
                "Suggested Qty": 10,
                "Checks": "confidence ok, volume ok",
                "Why": "Price above ATR trailing stop",
            }
        ]
    )

    alerts = runner._build_runner_continuation_alerts(current_simple_df)

    assert list(alerts["Symbol"]) == ["BBOX"]
    assert alerts.iloc[0]["NowAction"] == "🚀 Runner Continuation"


def test_early_breakout_surfaces_one_to_three_percent_mover(monkeypatch):
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 9, 32, 0)),
    )
    runner = _make_runner()
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(runner.output_dir)
    runner.actionable_runner = SimpleNamespace(
        live_ticks={
            "RICOAUTO": {
                "high": 102.4,
                "total_buy_quantity": 200000,
                "total_sell_quantity": 100000,
            }
        }
    )

    current_simple_df = pd.DataFrame(
        [
            {
                "Stock": "RICOAUTO",
                "What To Do": "Buy Now",
                "Momentum Setup": "",
                "Price Now": 102.0,
                "Change %": 2.2,
                "Volume vs Avg": 1.8,
                "Signal Strength %": 36.0,
                "RSI": 62.0,
                "Entry Gap %": 0.2,
                "Buy Above": 102.5,
                "Sell If Below": 99.0,
                "Take Profit 1": 105.0,
                "Take Profit 2": 108.0,
                "Suggested Qty": 10,
                "Checks": "confidence ok, liquidity ok",
                "Why": "Price above ATR trailing stop",
                "Week52High": 140.0,
            }
        ]
    )

    alerts = runner._build_early_breakout_alerts(current_simple_df)

    assert list(alerts["Symbol"]) == ["RICOAUTO"]
    assert alerts.iloc[0]["NowAction"] == "🌱 Early Breakout"
    assert alerts.iloc[0]["TakeProfit1"] == 103.53
    assert alerts.iloc[0]["LifecycleObservationId"]
    assert runner._top_gainer_lifecycle_recorder.replay()[0]["source_lane"] == "early_breakout"


def test_early_breakout_blocks_already_ran_mover(monkeypatch):
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 9, 45, 0)),
    )
    runner = _make_runner()
    runner.actionable_runner = SimpleNamespace(live_ticks={"MSTCLTD": {"high": 106.0}})

    current_simple_df = pd.DataFrame(
        [
            {
                "Stock": "MSTCLTD",
                "What To Do": "Buy Now",
                "Momentum Setup": "",
                "Price Now": 106.0,
                "Change %": 5.1,
                "Volume vs Avg": 3.0,
                "Signal Strength %": 50.0,
                "RSI": 65.0,
                "Entry Gap %": 0.1,
                "Buy Above": 106.0,
                "Sell If Below": 100.0,
                "Take Profit 1": 110.0,
                "Take Profit 2": 114.0,
                "Suggested Qty": 10,
                "Checks": "confidence ok, liquidity ok",
                "Why": "Price above ATR trailing stop",
                "Week52High": 150.0,
            }
        ]
    )

    alerts = runner._build_early_breakout_alerts(current_simple_df)

    assert alerts.empty


def test_top_gainer_radar_surfaces_watch_mover_from_live_ticks(monkeypatch):
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 9, 42, 0)),
    )
    runner = _make_runner()
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(runner.output_dir)
    runner.use_live_kite = True
    runner._is_regular_market_hours = lambda: True
    runner.enable_live_feature_recorder = True
    runner._live_feature_recorder = LiveFeatureRecorder(
        output_dir=runner.output_dir,
        interval_seconds=60,
        top_gainer_pct=5.0,
    )
    runner.actionable_runner = SimpleNamespace(
        live_ticks={
            "CUPID": {
                "last_price": 136.85,
                "change": 2.83,
                "high": 136.95,
                "volume_traded": 175000,
                "total_buy_quantity": 333000,
                "total_sell_quantity": 100000,
                "depth": {
                    "buy": [{"price": 136.80, "quantity": 1000}],
                    "sell": [{"price": 136.90, "quantity": 1000}],
                },
            }
        }
    )

    current_simple_df = pd.DataFrame(
        [
            {
                "Stock": "CUPID",
                "What To Do": "Watch for Buy",
                "Momentum Setup": "Momentum Watch",
                "Price Now": 136.85,
                "Change %": 2.83,
                "Volume vs Avg": 1.24,
                "Signal Strength %": 42.1,
                "RSI": 69.3,
                "Entry Gap %": 0.25,
                "Buy Above": 137.0,
                "Sell If Below": 130.0,
                "Take Profit 1": 145.0,
                "Take Profit 2": 150.0,
                "Suggested Qty": 10,
                "Avg Daily Value": "172.00 Cr",
                "Checks": "confidence ok, near trigger",
                "Why": "Price above ATR trailing stop",
                "Week52High": 200.0,
            }
        ]
    )

    alerts = runner._build_top_gainer_radar_alerts(current_simple_df)

    assert list(alerts["Symbol"]) == ["CUPID"]
    assert alerts.iloc[0]["NowAction"] == "📡 Top Gainer Radar"
    assert alerts.iloc[0]["LifecycleObservationId"]
    assert runner._top_gainer_lifecycle_recorder.replay()[0]["source_lane"] == "top_gainer_radar"
    evidence = runner._top_gainer_lifecycle_recorder.replay()[0]["signal_time_fields"]
    assert evidence["volume"] == 175000
    assert evidence["bid"] == 136.80
    assert evidence["ask"] == 136.90
    journal = pd.read_csv(f"{runner.output_dir}/live_feature_recorder/lane_decisions_2026-05-08.csv")
    assert list(journal["Lane"]) == ["top_gainer_radar"]
    assert list(journal["Symbol"]) == ["CUPID"]
    assert list(journal["Decision"]) == ["pass"]
    assert journal.iloc[0]["Reason"] == "radar candidate passed"
    assert journal.iloc[0]["BestBid"] == 136.80


def test_top_gainer_radar_blocks_chased_mover(monkeypatch):
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 9, 45, 0)),
    )
    runner = _make_runner()
    runner.actionable_runner = SimpleNamespace(
        live_ticks={
            "ITDC": {
                "last_price": 668.10,
                "change": 13.4,
                "high": 671.0,
                "total_buy_quantity": 570000,
                "total_sell_quantity": 320000,
            }
        }
    )
    current_simple_df = pd.DataFrame(
        [
            {
                "Stock": "ITDC",
                "What To Do": "Buy Now",
                "Momentum Setup": "Momentum Buy",
                "Price Now": 668.10,
                "Change %": 13.4,
                "Volume vs Avg": 12.7,
                "Signal Strength %": 32.5,
                "RSI": 72.6,
                "Entry Gap %": 0.8,
                "Suggested Qty": 10,
                "Avg Daily Value": "11.38 Cr",
                "Why": "Price above ATR trailing stop",
                "Week52High": 712.8,
            }
        ]
    )

    alerts = runner._build_top_gainer_radar_alerts(current_simple_df)

    assert alerts.empty


def test_top_gainer_radar_blocks_weak_confirmation(monkeypatch):
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 9, 48, 0)),
    )
    runner = _make_runner()
    runner.actionable_runner = SimpleNamespace(
        live_ticks={
            "AETHER": {
                "last_price": 1161.50,
                "change": 1.6,
                "high": 1174.0,
                "total_buy_quantity": 80000,
                "total_sell_quantity": 100000,
            }
        }
    )
    current_simple_df = pd.DataFrame(
        [
            {
                "Stock": "AETHER",
                "What To Do": "Watch for Buy",
                "Momentum Setup": "Momentum Watch",
                "Price Now": 1161.50,
                "Change %": 1.6,
                "Volume vs Avg": 0.2,
                "Signal Strength %": 35.1,
                "RSI": 57.7,
                "Entry Gap %": 1.1,
                "Suggested Qty": 10,
                "Avg Daily Value": "20.00 Cr",
                "Why": "Price above ATR trailing stop",
                "Week52High": 1500.0,
            }
        ]
    )

    alerts = runner._build_top_gainer_radar_alerts(current_simple_df)

    assert alerts.empty


def test_leader_ignition_watch_uses_raw_ticks_without_simple_board(monkeypatch):
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 9, 24, 0)),
    )
    runner = _make_runner()
    runner.use_live_kite = True
    runner._is_regular_market_hours = lambda: True
    runner.enable_live_feature_recorder = True
    runner._live_feature_recorder = LiveFeatureRecorder(
        output_dir=runner.output_dir,
        interval_seconds=60,
        top_gainer_pct=5.0,
    )
    runner.actionable_runner = SimpleNamespace(
        live_ticks={
            "EARLY": {
                "last_price": 101.2,
                "open": 100.0,
                "close": 100.0,
                "change": 1.2,
                "high": 101.25,
                "volume_traded": 102000,
                "total_buy_quantity": 220000,
                "total_sell_quantity": 100000,
            }
        }
    )
    runner._leader_last_ticks_by_symbol = {
        "EARLY": {"last_price": 100.8, "volume_traded": 100000}
    }

    alerts = runner._run_leader_ignition_lane()

    assert list(alerts["Symbol"]) == ["EARLY"]
    assert alerts.iloc[0]["NowAction"] == "Leader Ignition Watch"
    audit = pd.read_csv(f"{runner.output_dir}/leader_ignition/leader_ignition_2026-05-08.csv")
    assert list(audit["Decision"]) == ["pass"]
    assert audit.iloc[0]["Reason"] == "early leader watch"
    journal = pd.read_csv(f"{runner.output_dir}/live_feature_recorder/lane_decisions_2026-05-08.csv")
    assert list(journal["Lane"]) == ["leader_ignition"]
    assert list(journal["Stage"]) == ["ignition_watch"]
    assert list(journal["Decision"]) == ["pass"]


def test_leader_ignition_rejects_already_stretched_mover(monkeypatch):
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 8, 9, 31, 0)),
    )
    runner = _make_runner()
    runner.use_live_kite = True
    runner._is_regular_market_hours = lambda: True
    runner.actionable_runner = SimpleNamespace(
        live_ticks={
            "CHASE": {
                "last_price": 106.0,
                "open": 100.0,
                "close": 100.0,
                "change": 6.0,
                "high": 106.2,
                "volume_traded": 200000,
                "total_buy_quantity": 240000,
                "total_sell_quantity": 100000,
            }
        }
    )

    alerts = runner._run_leader_ignition_lane()

    assert alerts.empty
    audit = pd.read_csv(f"{runner.output_dir}/leader_ignition/leader_ignition_2026-05-08.csv")
    assert list(audit["Decision"]) == ["reject"]
    assert audit.iloc[0]["Reason"] == "already stretched"


def test_leader_pullback_reclaim_after_watch(monkeypatch):
    current_time = datetime(2026, 5, 8, 9, 24, 0)
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: current_time),
    )
    runner = _make_runner()
    runner.use_live_kite = True
    runner._is_regular_market_hours = lambda: True

    runner.actionable_runner = SimpleNamespace(
        live_ticks={
            "RECLAIM": {
                "last_price": 101.2,
                "open": 100.0,
                "close": 100.0,
                "change": 1.2,
                "high": 101.25,
                "volume_traded": 102000,
                "total_buy_quantity": 220000,
                "total_sell_quantity": 100000,
            }
        }
    )
    first = runner._run_leader_ignition_lane()
    assert list(first["NowAction"]) == ["Leader Ignition Watch"]

    runner.actionable_runner.live_ticks["RECLAIM"].update(
        {
            "last_price": 100.7,
            "change": 0.7,
            "high": 101.5,
            "volume_traded": 104000,
        }
    )
    pullback = runner._run_leader_ignition_lane()
    assert pullback.empty
    assert runner._leader_candidates["RECLAIM"]["status"] == "pullback_seen"

    runner.actionable_runner.live_ticks["RECLAIM"].update(
        {
            "last_price": 101.1,
            "change": 1.1,
            "high": 101.5,
            "volume_traded": 107000,
            "total_buy_quantity": 250000,
            "total_sell_quantity": 100000,
        }
    )
    reclaim = runner._run_leader_ignition_lane()

    assert list(reclaim["Symbol"]) == ["RECLAIM"]
    assert reclaim.iloc[0]["NowAction"] == "Leader Pullback Reclaim"
    assert runner._leader_candidates["RECLAIM"]["status"] == "reclaim_alerted"


def test_discord_message_labels_direct_buy_as_swing_candidate():
    runner = _make_runner()
    alerts_df = pd.DataFrame(
        [_alert("WELCORP", "Buy Now", 42.0)],
        columns=runner.ALERT_COLUMNS,
    )

    message = runner._build_discord_monitor_message(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 1, "StrongBuyNowCount": 0, "BuyNowCount": 1, "SellNowCount": 0},
    )

    assert "SWING BUY CANDIDATE" in message
    assert "**BUY ZONE**" in message
    assert "Qty `10`" in message


def test_discord_message_labels_anomaly_as_watch_not_swing_buy():
    runner = _make_runner()
    alerts_df = pd.DataFrame(
        [_alert("UNOMINDA", "⚡ Grade-B Anomaly", 52.0)],
        columns=runner.ALERT_COLUMNS,
    )

    message = runner._build_discord_monitor_message(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 1, "StrongBuyNowCount": 0, "BuyNowCount": 0, "SellNowCount": 0},
    )

    assert "ANOMALY WATCH" in message
    assert "**ENTRY ZONE**" in message
    assert "Max qty `10`" in message
    assert "SWING BUY CANDIDATE" not in message


def test_discord_message_makes_alert_only_anomaly_non_tradeable():
    runner = _make_runner()
    alert = _alert("UNOMINDA", "⚡ Grade-B Anomaly — ALERT ONLY", 52.0)
    alert["SuggestedQty"] = 0
    alert["Checks"] = "AlertOnly no auto-buy | research ladder 1% / 2% / 3%"
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    message = runner._build_discord_monitor_message(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 1},
    )

    assert "ANOMALY ALERT ONLY" in message
    assert "WATCH RANGE" in message
    assert "Auto-buy `DISABLED`" in message
    assert "Broker quantity `0`" in message
    assert "BUY ZONE" not in message


def test_discord_message_labels_orderbook_as_intraday_scalp():
    runner = _make_runner()
    alerts_df = pd.DataFrame(
        [_alert("TFCILTD", "🌊 OB Sustained", 67.8)],
        columns=runner.ALERT_COLUMNS,
    )

    message = runner._build_discord_monitor_message(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 1, "StrongBuyNowCount": 0, "BuyNowCount": 0, "SellNowCount": 0},
    )

    assert "INTRADAY SCALP" in message
    assert "not a swing buy" in message
    assert "**ENTRY ZONE**" in message
    assert "Scalp qty `10`" in message


def test_goal_mode_orderbook_alert_is_paper_tracked():
    runner = _make_runner()
    runner.enable_paper_tracking = True
    runner._is_regular_market_hours = lambda: True
    tracked = []
    runner._paper_tracker = SimpleNamespace(log_entry=lambda **kwargs: tracked.append(kwargs))

    row = _alert("TFCILTD", "🌊 OB Sustained", 67.8)
    row["Checks"] = "goalmode selected | OB B/S 3.0x"
    runner._paper_track_alerts(pd.DataFrame([row], columns=runner.ALERT_COLUMNS))

    assert len(tracked) == 1
    assert tracked[0]["symbol"] == "TFCILTD"


def _make_ob_runner(monkeypatch, tick):
    now = datetime(2026, 5, 12, 9, 52, 0)
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: now),
    )
    runner = _make_runner()
    runner.use_live_kite = True
    runner.budget_amount = 50000
    runner._ob_alerted_today = set()
    runner._is_regular_market_hours = lambda: True
    runner.actionable_runner = SimpleNamespace(live_ticks={"TEST": tick})
    runner._ob_history = {
        "TEST": [
            (now - timedelta(minutes=5, seconds=20), 8.5),
            (now - timedelta(minutes=5, seconds=10), 8.2),
            (now - timedelta(minutes=5), 8.0),
            (now - timedelta(minutes=4), 8.4),
            (now - timedelta(minutes=3), 8.7),
            (now - timedelta(minutes=2), 8.8),
        ]
    }
    return runner


def test_orderbook_sustained_blocks_negative_day_move(monkeypatch):
    runner = _make_ob_runner(
        monkeypatch,
        {
            "total_buy_quantity": 2_000_000,
            "total_sell_quantity": 150_000,
            "last_price": 335.15,
            "high": 336.0,
            "open": 338.0,
            "close": 340.0,
            "volume_traded": 200_000,
        },
    )

    alerts = runner._run_orderbook_imbalance()

    assert alerts.empty


def test_orderbook_sustained_requires_price_confirmation_from_open(monkeypatch):
    runner = _make_ob_runner(
        monkeypatch,
        {
            "total_buy_quantity": 2_000_000,
            "total_sell_quantity": 150_000,
            "last_price": 101.0,
            "high": 101.5,
            "open": 101.0,
            "close": 100.0,
            "volume_traded": 400_000,
        },
    )

    alerts = runner._run_orderbook_imbalance()

    assert alerts.empty


def test_orderbook_sustained_passes_with_confirmed_price_and_turnover(monkeypatch):
    runner = _make_ob_runner(
        monkeypatch,
        {
            "total_buy_quantity": 2_000_000,
            "total_sell_quantity": 150_000,
            "last_price": 102.0,
            "high": 102.5,
            "open": 101.0,
            "close": 100.0,
            "volume_traded": 400_000,
        },
    )

    alerts = runner._run_orderbook_imbalance()

    assert list(alerts["Symbol"]) == ["TEST"]
    assert "turnover" in alerts.iloc[0]["Checks"]


def test_orderbook_confirmation_annotates_existing_buy_alert():
    runner = _make_runner()
    alerts_df = pd.DataFrame(
        [_alert("WELCORP", "Buy Now", 42.0)],
        columns=runner.ALERT_COLUMNS,
    )
    ob_df = pd.DataFrame(
        [_alert("WELCORP", "🌊 OB Sustained", 67.8)],
        columns=runner.ALERT_COLUMNS,
    )
    ob_df.loc[0, "Checks"] = "OB B/S 9.0x | turnover ₹4.0Cr"

    out = runner._annotate_ob_confirmations(alerts_df, ob_df)

    assert "OB B/S 9.0x" in out.iloc[0]["Checks"]
    assert "OB confirms visible buyer depth" in out.iloc[0]["Why"]


def test_orderbook_confirmation_does_not_annotate_sell_alert():
    runner = _make_runner()
    alerts_df = pd.DataFrame(
        [_alert("WELCORP", "Sell / Exit", 42.0)],
        columns=runner.ALERT_COLUMNS,
    )
    ob_df = pd.DataFrame(
        [_alert("WELCORP", "🌊 OB Sustained", 67.8)],
        columns=runner.ALERT_COLUMNS,
    )

    out = runner._annotate_ob_confirmations(alerts_df, ob_df)

    assert "OB confirms" not in out.iloc[0]["Why"]


def test_intraday_goal_mode_keeps_strong_anomaly_and_tightens_targets():
    runner = _make_runner()
    alert = _alert("AEROFLEX", "⚡ Grade-B Anomaly", 72.0)
    alert["Checks"] = "Q:72/100 [B] | Vol 8.9x | +1.2% | B/S 2.1x"
    alert["BuyAbove"] = 100.0
    alert["PriceNow"] = 100.0
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert list(out["Symbol"]) == ["AEROFLEX"]
    assert out.iloc[0]["SellIfBelow"] == 99.0
    assert out.iloc[0]["TakeProfit1"] == 101.5
    assert out.iloc[0]["TakeProfit2"] == 103.0
    assert "GoalMode" in out.iloc[0]["Checks"]

    message = runner._build_discord_monitor_message(
        out,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 1, "StrongBuyNowCount": 0, "BuyNowCount": 0, "SellNowCount": 0},
    )
    assert "DAY-GOAL SCALP + RUNNER" in message
    assert "book `70-80%` at Target 1" in message


def test_intraday_goal_mode_blocks_flat_anomaly_scalp():
    runner = _make_runner()
    alert = _alert("YUKEN", "⚡ Grade-B Anomaly", 62.0)
    alert["Checks"] = "Q:62/100 [B] | Vol 5.2x | +1.1% | B/S 3.7x"
    alert["ChangePct"] = 0.01
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert out.empty
    assert any("too flat for anomaly scalp" in reason for reason in runner._last_goal_suppressed_reasons)


def test_intraday_goal_mode_anomaly_only_blocks_direct_buy_and_premove():
    runner = _make_runner()
    runner.goal_anomaly_only = True
    direct = _alert("KIMS", "Buy Now", 80.0)
    direct["Checks"] = "confidence ok | volume ok | Vol 3.0x"
    premove = _alert("QUESS", "👀 Pre-Move Watch", 80.0)
    premove["Checks"] = "Q:80/100 [C] | Vol 8.0x | +2.0% | B/S 3.0x"
    alerts_df = pd.DataFrame([direct, premove], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert out.empty
    assert any("KIMS (anomaly-only goal mode)" in reason for reason in runner._last_goal_suppressed_reasons)
    assert any("QUESS (anomaly-only goal mode)" in reason for reason in runner._last_goal_suppressed_reasons)


def test_intraday_goal_mode_anomaly_only_keeps_grade_b():
    runner = _make_runner()
    runner.goal_anomaly_only = True
    alert = _alert("PUNJABCHEM", "⚡ Grade-B Anomaly", 72.0)
    alert["Checks"] = "Q:72/100 [B] | Vol 8.9x | +1.2% | B/S 2.1x"
    alert["ChangePct"] = 1.2
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert list(out["Symbol"]) == ["PUNJABCHEM"]
    assert "Grade-B anomaly burst setup" in out.iloc[0]["Why"]


def test_intraday_goal_mode_keeps_radar_separate_from_goal_cap(monkeypatch):
    runner = _make_runner()
    runner.goal_max_trades_per_day = 1
    monkeypatch.setattr(runner, "_load_daily_symbol_set", lambda name, **_kwargs: {"AEROFLEX"} if name == "goal_burst" else set())
    appended = []
    runner._append_daily_symbol = lambda name, symbol, **_kwargs: appended.append((name, symbol))
    radar = _alert("CUPID", "📡 Top Gainer Radar", 42.0)
    radar["Checks"] = "top-gainer radar +2.7% | Q:42/100 | vol 1.2x | B/S 2.1x | pullback 0.2%"
    radar["ChangePct"] = 2.7
    radar["RSI"] = 63.0
    alerts_df = pd.DataFrame([radar], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert list(out["Symbol"]) == ["CUPID"]
    assert "RadarMode" in out.iloc[0]["Checks"]
    assert "GoalMode" not in out.iloc[0]["Checks"]
    assert appended == [("top_gainer_radar_goal", "CUPID")]


def test_intraday_goal_mode_applies_separate_radar_cap(monkeypatch):
    runner = _make_runner()
    runner.radar_max_alerts_per_day = 1
    monkeypatch.setattr(runner, "_load_daily_symbol_set", lambda *_args, **_kwargs: set())
    appended = []
    runner._append_daily_symbol = lambda name, symbol, **_kwargs: appended.append((name, symbol))
    first = _alert("CUPID", "📡 Top Gainer Radar", 42.0)
    first["Checks"] = "top-gainer radar +2.7% | vol 1.2x | B/S 2.1x"
    first["ChangePct"] = 2.7
    first["RSI"] = 63.0
    second = _alert("PINELABS", "📡 Top Gainer Radar", 40.0)
    second["Checks"] = "top-gainer radar +3.0% | vol 1.1x | B/S 2.0x"
    second["ChangePct"] = 3.0
    second["RSI"] = 61.0
    alerts_df = pd.DataFrame([first, second], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert len(out) == 1
    assert list(out["Symbol"]) == ["CUPID"]
    assert appended == [("top_gainer_radar_goal", "CUPID")]
    assert any("daily radar slots full" in reason for reason in runner._last_goal_suppressed_reasons)


def test_discord_send_chunks_goal_alerts_that_do_not_fit_one_message():
    runner = _make_runner()
    sent_messages = []
    journaled_symbols = []
    runner._send_discord_alert = sent_messages.append
    runner._journal_discord_alerts = lambda df, _summary, _message: journaled_symbols.extend(
        df["Symbol"].astype(str).tolist()
    )

    first = _alert("RHIM", "🔥 Grade-A Runner", 82.5)
    first["Checks"] = "GoalMode✓ 3% burst | SL 1.0% | T1 1.5% | T2 3.0% | Q:82/100 [A] | Vol 8.7x | +5.6% | Sector✓ (CAPGOODS) | B/S 1.7x"
    first["Why"] = " | ".join(["Goal mode: Grade-A/runner burst setup"] * 8)
    second = _alert("VOLTAS", "🔥 Grade-A Runner", 79.0)
    second["Checks"] = "GoalMode✓ 3% burst | SL 1.0% | T1 1.5% | T2 3.0% | Q:79/100 [A] | Vol 4.9x | +5.7% | Sector✓ (CONSUMER) | B/S 2.4x"
    second["Why"] = " | ".join(["Goal mode: Grade-A/runner burst setup"] * 8)
    alerts_df = pd.DataFrame([first, second], columns=runner.ALERT_COLUMNS)

    runner._send_discord_monitor_alerts(
        alerts_df,
        {"ScannedSymbols": 2990, "FreshAlertsThisRun": 2, "StrongBuyNowCount": 0, "BuyNowCount": 6, "SellNowCount": 384},
    )

    assert len(sent_messages) == 2
    assert "`RHIM`" in sent_messages[0]
    assert "`VOLTAS`" not in sent_messages[0]
    assert "`VOLTAS`" in sent_messages[1]
    assert journaled_symbols == ["RHIM", "VOLTAS"]


def test_monitor_heartbeat_reports_active_scanner_without_fresh_buy(monkeypatch):
    runner = _make_runner()
    runner.goal_max_trades_per_day = 20
    sent_messages = []
    runner._send_discord_alert = sent_messages.append
    runner._is_regular_market_hours = lambda: True
    runner._last_goal_suppressed_reasons = [
        "VOLTAS (goal-mode already surfaced today)",
        "VENUSREM (RSI 86 too hot)",
    ]
    monkeypatch.setattr(runner, "_load_daily_symbol_set", lambda *_args, **_kwargs: {"RHIM", "VOLTAS"})
    summary_df = pd.DataFrame(
        [
            {
                "ScannedSymbols": 2990,
                "FreshAlertsThisRun": 0,
                "BuyNowCount": 6,
                "MomentumBuyCount": 6,
                "SellNowCount": 350,
            }
        ]
    )

    runner._send_monitor_heartbeat(summary_df, pd.DataFrame(columns=runner.ALERT_COLUMNS))

    assert len(sent_messages) == 1
    assert "Intraday Goal Scanner Heartbeat" in sent_messages[0]
    assert "Scanner `active`" in sent_messages[0]
    assert "Goal alerts today `2/20`" in sent_messages[0]
    assert "Buy promotion `eligible" in sent_messages[0]
    assert "VOLTAS" in sent_messages[0]


def test_monitor_heartbeat_explains_unvalidated_selector_lock(monkeypatch):
    runner = _make_runner()
    runner.goal_selector_validated = False
    runner._is_regular_market_hours = lambda: True
    monkeypatch.setattr(runner, "_load_daily_symbol_set", lambda *_args, **_kwargs: set())
    summary_df = pd.DataFrame(
        [
            {
                "ScannedSymbols": 2929,
                "FreshAlertsThisRun": 0,
                "BuyNowCount": 5,
                "MomentumBuyCount": 5,
            }
        ]
    )

    captured = []
    runner._send_discord_alert = captured.append
    runner._send_monitor_heartbeat(summary_df, pd.DataFrame(columns=runner.ALERT_COLUMNS))

    assert len(captured) == 1
    assert "Buy promotion `BLOCKED — selector not validated; paper evidence continues`" in captured[0]


def test_monitor_heartbeat_previews_board_names_and_overlap(monkeypatch):
    runner = _make_runner()
    runner.goal_selector_validated = False
    runner._is_regular_market_hours = lambda: True
    monkeypatch.setattr(runner, "_load_daily_symbol_set", lambda *_args, **_kwargs: set())
    summary_df = pd.DataFrame(
        [{"ScannedSymbols": 2925, "FreshAlertsThisRun": 0, "BuyNowCount": 3, "MomentumBuyCount": 4}]
    )
    simple_df = pd.DataFrame(
        [
            {"Stock": "BETA", "What To Do": "Buy Now", "Momentum Setup": "Momentum Buy", "Signal Strength %": 50},
            {"Stock": "ALPHA", "What To Do": "Buy Now", "Momentum Setup": "Momentum Buy", "Signal Strength %": 80},
            {"Stock": "GAMMA", "What To Do": "Buy Now", "Momentum Setup": "Momentum Buy", "Signal Strength %": 60},
            {"Stock": "DELTA", "What To Do": "Hold / Do Nothing", "Momentum Setup": "Momentum Buy", "Signal Strength %": 70},
        ]
    )

    captured = []
    runner._send_discord_alert = captured.append
    runner._send_monitor_heartbeat(
        summary_df,
        pd.DataFrame(columns=runner.ALERT_COLUMNS),
        current_simple_df=simple_df,
    )

    assert len(captured) == 1
    assert "Board preview `status only — not approved buy signals`" in captured[0]
    assert "Buy Now: `ALPHA`, `GAMMA`, `BETA`" in captured[0]
    assert "Momentum-only: `DELTA`" in captured[0]
    assert "Buy/Momentum overlap `3`" in captured[0]


def test_monitor_heartbeat_throttle_survives_runner_restart(monkeypatch, tmp_path):
    runner = _make_runner()
    runner.output_dir = str(tmp_path)
    runner._heartbeat_state_path = str(tmp_path / "heartbeat_state.json")
    runner.goal_max_trades_per_day = 20
    runner._is_regular_market_hours = lambda: True
    sent_messages = []
    runner._send_discord_alert = sent_messages.append
    monkeypatch.setattr(signal_monitor_module.time, "time", lambda: 1000.0)
    monkeypatch.setattr(runner, "_load_daily_symbol_set", lambda *_args, **_kwargs: set())
    summary_df = pd.DataFrame(
        [
            {
                "ScannedSymbols": 2990,
                "FreshAlertsThisRun": 0,
                "BuyNowCount": 0,
                "MomentumBuyCount": 0,
                "SellNowCount": 0,
            }
        ]
    )

    runner._send_monitor_heartbeat(summary_df, pd.DataFrame(columns=runner.ALERT_COLUMNS))
    assert len(sent_messages) == 1

    runner._last_discord_heartbeat_ts = 0.0
    monkeypatch.setattr(signal_monitor_module.time, "time", lambda: 1060.0)
    runner._send_monitor_heartbeat(summary_df, pd.DataFrame(columns=runner.ALERT_COLUMNS))
    assert len(sent_messages) == 1

    monkeypatch.setattr(signal_monitor_module.time, "time", lambda: 1601.0)
    runner._send_monitor_heartbeat(summary_df, pd.DataFrame(columns=runner.ALERT_COLUMNS))
    assert len(sent_messages) == 2


def test_regular_market_hours_blocks_weekend(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 7, 5, 10, 28, 0)),
    )

    assert runner._is_regular_market_hours() is False


def test_regular_market_hours_blocks_trading_holiday(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 7, 6, 10, 28, 0)),
    )
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "isHoliday",
        staticmethod(lambda _date: (True, "Exchange holiday")),
    )

    assert runner._is_regular_market_hours() is False


def test_intraday_goal_mode_blocks_buys_without_validated_selector():
    runner = _make_runner()
    runner.goal_selector_validated = False
    alert = _alert("AEROFLEX", "⚡ Grade-B Anomaly", 72.0)
    alert["Checks"] = "Q:72/100 [B] | Vol 8.9x | +1.2% | B/S 2.1x"
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert out.empty


def test_intraday_goal_mode_allows_anomaly_watch_without_trade_promotion(monkeypatch):
    runner = _make_runner()
    runner.goal_selector_validated = False
    runner.anomaly_alert_only = True
    monkeypatch.setattr(runner, "_load_daily_symbol_set", lambda *_args, **_kwargs: set())
    persisted = []
    monkeypatch.setattr(runner, "_append_daily_symbol", lambda kind, symbol, **_kwargs: persisted.append((kind, symbol)))
    alert = _alert("AEROFLEX", "⚡ Grade-B Anomaly", 72.0)
    alert["PriceNow"] = 100.0
    alert["BuyAbove"] = 100.0
    alert["Checks"] = "Q:72/100 [B] | Vol 8.9x | +1.2% | B/S 2.1x"
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert len(out) == 1
    row = out.iloc[0]
    assert "ALERT ONLY" in row["NowAction"]
    assert "AlertOnly no auto-buy" in row["Checks"]
    assert int(row["SuggestedQty"]) == 0
    assert float(row["SellIfBelow"]) == 99.0
    assert float(row["TakeProfit1"]) == 101.0
    assert float(row["TakeProfit2"]) == 103.0
    assert persisted == [("anomaly_alert_only", "AEROFLEX")]


def test_alert_only_anomaly_never_registers_for_autotrade():
    runner = _make_runner()
    runner._autotrade_candidates = {}
    runner.autotrade_entry_window_minutes = 15.0
    alert = _alert("AEROFLEX", "⚡ Grade-B Anomaly — ALERT ONLY", 72.0)
    alert["Checks"] = "AlertOnly no auto-buy"
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    runner._register_autotrade_candidates(alerts_df)

    assert runner._autotrade_candidates == {}


def test_goal_selector_accepts_profitable_forward_backtest_summary(tmp_path):
    selector = tmp_path / "daily_goal_summary.csv"
    pd.DataFrame(
        [
            {
                "outcome_source_filter": "post_alert_intraday",
                "rank_by": "quality",
                "days": 14,
                "goal_hit_pct": 50.0,
                "avg_daily_return_pct": 0.1761,
                "worst_day_pct": -1.15,
                "leakage_audit_passed": True,
                "feature_availability": "signal_close_or_earlier",
                "candidate_availability": "first_threshold_eligible_signal_batch",
                "evidence_version": 3,
            }
        ]
    ).to_csv(selector, index=False)

    assert SignalMonitorRunner._goal_selector_is_validated(str(selector)) is True


def test_goal_selector_rejects_full_day_ohlc_backtest_summary(tmp_path):
    selector = tmp_path / "daily_goal_summary.csv"
    pd.DataFrame(
        [
            {
                "outcome_source_filter": "any",
                "rank_by": "quality",
                "days": 14,
                "goal_hit_pct": 50.0,
                "avg_daily_return_pct": 0.1761,
                "worst_day_pct": -1.15,
                "leakage_audit_passed": True,
                "feature_availability": "signal_close_or_earlier",
                "candidate_availability": "first_threshold_eligible_signal_batch",
                "evidence_version": 3,
            }
        ]
    ).to_csv(selector, index=False)

    assert SignalMonitorRunner._goal_selector_is_validated(str(selector)) is False


def test_goal_selector_rejects_v2_all_day_best_candidate_selection(tmp_path):
    selector = tmp_path / "v2_selector.csv"
    pd.DataFrame(
        [
            {
                "outcome_source_filter": "post_alert_intraday",
                "rank_by": "quality",
                "days": 413,
                "goal_hit_pct": 50.0,
                "avg_daily_return_pct": 1.0,
                "worst_day_pct": -1.0,
                "leakage_audit_passed": True,
                "feature_availability": "signal_close_or_earlier",
                "evidence_version": 2,
            }
        ]
    ).to_csv(selector, index=False)

    assert SignalMonitorRunner._goal_selector_is_validated(str(selector)) is False


def test_goal_selector_rejects_legacy_accepted_file_without_leakage_audit(tmp_path):
    selector = tmp_path / "legacy_selector.csv"
    pd.DataFrame(
        [
            {
                "selector": "leaked_entry_bar_liquidity",
                "accepted": True,
                "final_avg_daily_return_pct": 0.60,
            }
        ]
    ).to_csv(selector, index=False)

    assert SignalMonitorRunner._goal_selector_is_validated(str(selector)) is False


def test_intraday_goal_mode_can_select_multiple_basket_slots():
    runner = _make_runner()
    runner.goal_max_trades_per_day = 2
    first = _alert("AEROFLEX", "⚡ Grade-B Anomaly", 72.0)
    first["Checks"] = "Q:72/100 [B] | Vol 8.9x | +1.2% | B/S 2.1x"
    second = _alert("WELCORP", "⚡ Grade-B Anomaly", 70.0)
    second["Checks"] = "Q:70/100 [B] | Vol 7.5x | +1.5% | B/S 2.0x"
    third = _alert("TFCILTD", "⚡ Grade-B Anomaly", 68.0)
    third["Checks"] = "Q:68/100 [B] | Vol 6.5x | +1.9% | B/S 1.8x"
    alerts_df = pd.DataFrame([third, first, second], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert list(out["Symbol"]) == ["AEROFLEX", "WELCORP"]


def test_intraday_goal_mode_blocks_direct_buy_without_confirmation():
    runner = _make_runner()
    alerts_df = pd.DataFrame(
        [_alert("WELCORP", "Buy Now", 80.0)],
        columns=runner.ALERT_COLUMNS,
    )

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert out.empty


def test_intraday_goal_mode_accepts_quality_direct_buy_now(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(runner, "_load_daily_symbol_set", lambda *_args, **_kwargs: set())
    appended = []
    runner._append_daily_symbol = lambda name, symbol, **_kwargs: appended.append((name, symbol))
    alert = _alert("INOXINDIA", "Buy Now", 42.3)
    alert["ChangePct"] = 2.63
    alert["EntryGapPct"] = 0.8
    alert["RSI"] = 68.4
    alert["Checks"] = "confidence ok, near trigger, liquidity ok, volume ok, early momentum promotion | Vol 1.5x"
    alert["Why"] = "RSI approaching overbought (68.4) | MACD histogram increasing | Price above ATR trailing stop"
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert list(out["Symbol"]) == ["INOXINDIA"]
    assert "GoalMode" in out.iloc[0]["Checks"]
    assert "Quality direct Buy Now goal setup" in out.iloc[0]["Why"]
    assert appended == [("goal_burst", "INOXINDIA")]


def test_intraday_goal_mode_blocks_overheated_direct_buy_now():
    runner = _make_runner()
    alert = _alert("NRBBEARING", "Buy Now", 43.9)
    alert["ChangePct"] = 4.2
    alert["EntryGapPct"] = 0.7
    alert["RSI"] = 78.8
    alert["Checks"] = "confidence ok, near trigger, liquidity ok, volume ok | Vol 2.5x"
    alert["Why"] = "RSI overbought (78.8) | MACD bullish crossover | Price above ATR trailing stop"
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert out.empty
    assert any("RSI 79 too hot" in reason for reason in runner._last_goal_suppressed_reasons)


def test_intraday_goal_mode_allows_direct_buy_with_ob_confirmation():
    runner = _make_runner()
    alert = _alert("WELCORP", "Buy Now", 80.0)
    alert["Checks"] = "confidence ok | OB B/S 9.0x | turnover ₹4.0Cr"
    alert["ChangePct"] = 2.0
    alert["EntryGapPct"] = 0.4
    alert["RSI"] = 62.0
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert list(out["Symbol"]) == ["WELCORP"]
    assert "Direct buy with OB confirmation" in out.iloc[0]["Why"]


def test_intraday_goal_mode_allows_early_breakout():
    runner = _make_runner()
    alert = _alert("RICOAUTO", "🌱 Early Breakout", 42.0)
    alert["Checks"] = "early breakout +2.2% | vol 1.8x"
    alert["ChangePct"] = 2.2
    alert["EntryGapPct"] = 0.2
    alert["RSI"] = 62.0
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert list(out["Symbol"]) == ["RICOAUTO"]
    assert "Early breakout before top-gainer stretch" in out.iloc[0]["Why"]


def test_intraday_goal_mode_blocks_standalone_ob():
    runner = _make_runner()
    alert = _alert("TFCILTD", "🌊 OB Sustained", 90.0)
    alert["Checks"] = "OB B/S 9.0x | turnover ₹4.0Cr"
    alerts_df = pd.DataFrame([alert], columns=runner.ALERT_COLUMNS)

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert out.empty

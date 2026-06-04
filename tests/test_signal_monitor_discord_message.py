import pandas as pd
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.SignalMonitor import SignalMonitorRunner


def _make_runner() -> SignalMonitorRunner:
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.universe = "all"
    runner.run_at_str = "2026-05-08 10:15:00"
    runner.run_at = datetime(2026, 5, 8, 10, 15, 0)
    runner.output_dir = tempfile.mkdtemp()
    runner.last_guardrail_status = None
    runner.alert_channels = {"discord"}
    runner._board_shown_today = set()
    runner._board_shown_date = ""
    runner.enable_intraday_goal_mode = True
    runner.goal_max_trades_per_day = 20
    runner.goal_stop_pct = 1.0
    runner.goal_target1_pct = 1.5
    runner.goal_target2_pct = 3.0
    runner.monitor_heartbeat_minutes = 10
    runner._last_discord_heartbeat_ts = 0.0
    runner._last_goal_suppressed_reasons = []
    return runner


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
    assert "VOLTAS" in sent_messages[0]


def test_intraday_goal_mode_blocks_direct_buy_without_confirmation():
    runner = _make_runner()
    alerts_df = pd.DataFrame(
        [_alert("WELCORP", "Buy Now", 80.0)],
        columns=runner.ALERT_COLUMNS,
    )

    out = runner._apply_intraday_goal_mode(alerts_df)

    assert out.empty


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

import pandas as pd
from types import SimpleNamespace

from pkscreener.classes.SignalMonitor import SignalMonitorRunner


def _make_runner() -> SignalMonitorRunner:
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.universe = "all"
    runner.run_at_str = "2026-05-08 10:15:00"
    runner.last_guardrail_status = None
    runner._board_shown_today = set()
    runner._board_shown_date = ""
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

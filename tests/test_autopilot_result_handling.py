from unittest.mock import MagicMock

from pkscreener.classes.AutoPilotInvestor import AutoPilotInvestor


def _make_investor() -> AutoPilotInvestor:
    investor = AutoPilotInvestor.__new__(AutoPilotInvestor)
    investor._daily_auto_bought = []
    investor._daily_auto_buy_failed = []
    investor._daily_auto_buy_skipped = []
    investor._daily_protection_failed = []
    investor._daily_missed_opportunities = []
    investor._daily_no_buy_reasons = {}
    investor._send_to_channels = MagicMock()
    investor._record_no_buy_reason = MagicMock()
    return investor


def test_handle_auto_buy_result_tracks_skipped_buy_message():
    investor = _make_investor()

    investor._handle_auto_buy_result(
        "sumichem",
        "⚠️ Auto-buy skipped for `SUMICHEM`: intraday auto-buy is paused after 11:30 AM IST",
    )

    assert investor._daily_auto_buy_skipped == ["SUMICHEM"]
    assert investor._daily_auto_buy_failed == []
    investor._record_no_buy_reason.assert_called_once_with(
        "⚠️ Auto-buy skipped for `SUMICHEM`: intraday auto-buy is paused after 11:30 AM IST"
    )
    investor._send_to_channels.assert_called_once()


def test_handle_auto_buy_result_tracks_blocked_header_as_skipped():
    investor = _make_investor()

    investor._handle_auto_buy_result(
        "sumichem",
        "⛔ **BLOCKED — `SUMICHEM`**\nBuyable setup missed because: Max intraday positions reached (3)",
    )

    assert investor._daily_auto_buy_skipped == ["SUMICHEM"]
    assert investor._daily_missed_opportunities == []
    investor._record_no_buy_reason.assert_called_once_with(
        "Buyable setup missed because: Max intraday positions reached (3)"
    )
    investor._send_to_channels.assert_called_once()

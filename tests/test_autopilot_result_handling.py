from unittest.mock import MagicMock

from pkscreener.classes.AutoPilotInvestor import AutoPilotInvestor, BlindBuyCandidate


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


def test_do_send_buy_alert_pushes_formatted_message_to_channels():
    investor = _make_investor()
    investor._already_alerted_buys = set()
    investor._daily_batch_alert_count = 0
    investor._momentum_tracked = {}
    investor._pending_followups = []
    investor._is_post_market_window = MagicMock(return_value=False)
    investor._is_safe_to_buy = MagicMock(return_value=(True, "ok"))
    investor._resolve_auto_buy_candidate = MagicMock(
        side_effect=lambda candidate: (candidate, "A", False, "test gate")
    )
    investor._format_missed_or_late_message = MagicMock(return_value="")
    investor._log_engine_confirmed_buy_candidate = MagicMock()
    investor._record_internal_stage = MagicMock()
    investor._save_daily_alerted = MagicMock()
    investor._paper_tracker = MagicMock()
    investor._paper_tracker.log_entry = MagicMock()
    investor._log = MagicMock()

    def mark_actionable(candidate):
        candidate.alert_stage = "BUY"

    investor._enrich_candidate_alert_stage = MagicMock(side_effect=mark_actionable)

    candidate = BlindBuyCandidate(
        symbol="WELCORP",
        price=100.0,
        buy_above=101.0,
        stop_loss=95.0,
        target1=110.0,
        target2=120.0,
        confidence=46.0,
        volume_ratio=3.2,
        change_pct=2.4,
        blind_score=82.0,
        original_action="Strong Buy Now",
        reasons="strong volume, breakout, near trigger",
        suggested_qty=10,
        holding_horizon="2-day swing",
        product_preference="CNC",
        max_hold_days=5,
        entry_type="EARLY_BREAKOUT",
        entry_type_reason="move is controlled and close to trigger",
    )

    investor._do_send_buy_alert([candidate], is_followup=False)

    assert investor._send_to_channels.call_count == 2
    paper_message = investor._send_to_channels.call_args_list[0].args[0]
    message = investor._send_to_channels.call_args_list[1].args[0]
    assert "BLIND-BUY ALERT" in paper_message
    assert "WELCORP" in paper_message
    assert "BUY ALERT" in message
    assert "WELCORP" in message
    assert "Suggested: Buy" in message
    assert "WELCORP" in investor._already_alerted_buys
    investor._save_daily_alerted.assert_called_once_with("WELCORP")

"""Actionable-exit invariants for NG + UT Bot v1.3.5.2."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "NG_Scalp_Alerts_v1_3_5_1_CLEAN_ACTION_ALERTS.pine"
REVISED = ROOT / "NG_Scalp_Alerts_v1_3_5_2_UT_SELL_ONLY.pine"
BASELINE_SHA256 = "13994be37e90169ef135a7f2270bcf0b90bbbefa6e4420f4fcb0f3b6f4827cea"


def section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    return text[start_at:text.index(end, start_at)]


def test_baseline_is_immutable_and_new_version_is_separate() -> None:
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == BASELINE_SHA256
    assert REVISED.exists()
    assert REVISED != BASELINE


def test_ng_qualification_ut_calculation_and_timing_are_unchanged() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    for start, end in (
        ("float utAtr =", "\n\nfloat recentBreakoutHigh"),
        ("    bool signalOk = (", "\n\n    // Pine-native gate telemetry"),
        ("    string ngSourceNow =", "\n\n    // If no recent confirmed UT transition"),
    ):
        assert section(revised, start, end) == section(baseline, start, end)


def test_buy_price_quantity_stop_and_alert_are_unchanged() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    for start, end in (
        ("calcQty(float entryValue, float stopValue) =>", "\ngrowBrokerage(float notionalValue) =>"),
        ("    float rawStop = low - atr1 * stopBufferAtr", "\n\n    bool signalOk = ("),
        ("        tradeActive := true", "\n        utTradeNgQualificationTime :="),
        ("        string buyMessage = (", "\n\n        if barstate.isrealtime"),
    ):
        assert section(revised, start, end) == section(baseline, start, end)
    assert "alert(buyMessage, alert.freq_once_per_bar_close)" in revised


def test_actionable_exit_predicate_is_only_ut_sell_then_eod() -> None:
    text = REVISED.read_text()
    live_exit = section(text, "// UT-managed Exit engine", "// NG qualification + UT timing entry engine")
    decision = section(live_exit, "    bool shouldExit = false", "\n\n    if shouldExit")
    assert decision == '''    bool shouldExit = false
    float exitReference = na

    if utFreshSellConfirmed
        shouldExit := true
        sellReasonNow := "UT_SELL"
        exitReference := close
    else if eodExit
        shouldExit := true
        sellReasonNow := "EOD"
        exitReference := close'''
    for forbidden in (
        "HARD_STOP", "targetHit", 'sellReasonNow := "TARGET"',
        "trendExit", "bearishFlip", "sessionVwap",
    ):
        assert forbidden not in decision


def test_old_ng_stop_is_telemetry_only() -> None:
    text = REVISED.read_text()
    live_exit = section(text, "// UT-managed Exit engine", "// NG qualification + UT timing entry engine")
    assert "bool oldNgStopTouchedNow = low <= tradeStop" in live_exit
    assert "utOldNgStopFirstTouchTime := oldNgStopTouchTimeLatched" in live_exit
    assert "utOldNgStopUtBullishAtTouch := oldNgStopBullishAtTouchLatched" in live_exit
    assert "utOldNgStopMfeAfterR :=" in live_exit
    assert "utLastUtSellPrice := exitReference" in live_exit
    assert "utLastUtSellOutcomeR := utCapturedR" in live_exit
    assert "if oldNgStopTouchedNow" not in section(live_exit, "    bool shouldExit = false", "\n\n    if shouldExit")
    assert '"Old NG Stop (Telemetry Only)"' in text


def test_sell_message_has_only_ut_sell_or_eod_reason() -> None:
    text = REVISED.read_text()
    sell = section(text, "            string sellMessage = (", "\n\n            if barstate.isrealtime")
    assert '"🔴 SELL | " + syminfo.ticker' in sell
    assert '"\\nPrice: ₹" + fmtPrice(exitReference)' in sell
    assert '"\\nQty: " + str.tostring(tradeQty)' in sell
    assert '"\\nReason: " + sellReasonDisplay' in sell
    assert '"\\nPnL: " + fmtMoney(grossPnl)' in sell
    assert '"\\nPnL/share: ₹" + fmtPrice(grossPnlPerShare)' in sell
    assert 'sellReasonNow == "UT_SELL" ? "UT SELL" : "EOD"' in text
    assert "HARD STOP" not in sell


def test_actionable_sell_label_can_only_follow_actionable_exit_latch() -> None:
    text = REVISED.read_text()
    visuals = section(text, "// Visuals", "// Research-only data-window outputs")
    assert "barstate.isconfirmed and exitAlertLatchedThisBar" in visuals
    assert '"SELL\\n₹" + fmtPrice(exitAlertLatchedReference)' in visuals
    assert "oldNgStopTouchedNow" not in visuals
    assert "utOldNgStopTouched" not in visuals


def test_standalone_ut_markers_remain_visible_and_unchanged() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    for title in ("UT BUY", "UT SELL"):
        baseline_line = next(line for line in baseline.splitlines() if f'title="{title}"' in line)
        revised_line = next(line for line in revised.splitlines() if f'title="{title}"' in line)
        assert revised_line == baseline_line


def test_old_stop_touch_does_not_exit_in_modeled_lifecycle() -> None:
    def actionable_exit(*, old_stop: bool, ut_sell: bool, eod: bool) -> str | None:
        _old_stop_telemetry = old_stop
        if ut_sell:
            return "UT_SELL"
        if eod:
            return "EOD"
        return None

    assert actionable_exit(old_stop=True, ut_sell=False, eod=False) is None
    assert actionable_exit(old_stop=True, ut_sell=True, eod=False) == "UT_SELL"
    assert actionable_exit(old_stop=True, ut_sell=False, eod=True) == "EOD"

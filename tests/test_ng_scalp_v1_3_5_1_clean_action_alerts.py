"""Message-only invariants for NG + UT Bot v1.3.5.1."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "NG_Scalp_Alerts_v1_3_5_0_UT_BOT_EXPERIMENT.pine"
REVISED = ROOT / "NG_Scalp_Alerts_v1_3_5_1_CLEAN_ACTION_ALERTS.pine"
BASELINE_SHA256 = "6fd33a8cfb3c0cff7eff73e788d3727f741372890b2285d6324c91aedf250fe6"


def section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    return text[start_at:text.index(end, start_at)]


def test_baseline_is_unchanged_and_revised_file_is_separate() -> None:
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == BASELINE_SHA256
    assert REVISED.exists()
    assert REVISED != BASELINE


def test_ng_qualification_and_htf_context_are_byte_identical() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    request_start = "[ctxOpen5, ctxClose5, ctxAtr5, ctxEma5, ctxEma5Prev, ctxVol5, ctxTimeClose5] = request.security("
    assert section(revised, request_start, "\n\nbool newCompleted5m") == section(
        baseline, request_start, "\n\nbool newCompleted5m"
    )
    assert section(revised, "    bool signalOk = (", "\n\n    // Pine-native gate telemetry") == section(
        baseline, "    bool signalOk = (", "\n\n    // Pine-native gate telemetry"
    )


def test_ut_calculation_and_timing_are_byte_identical() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    assert section(revised, "float utAtr =", "\n\nfloat recentBreakoutHigh") == section(
        baseline, "float utAtr =", "\n\nfloat recentBreakoutHigh"
    )
    assert "bool utFreshBuyConfirmed = barstate.isconfirmed and utBuyCross" in revised
    assert "bool utFreshSellConfirmed = barstate.isconfirmed and utSellCross" in revised


def test_quantity_risk_stop_and_entry_assignments_are_byte_identical() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    for start, end in (
        ("calcQty(float entryValue, float stopValue) =>", "\ngrowBrokerage(float notionalValue) =>"),
        ("    float rawStop = low - atr1 * stopBufferAtr", "\n\n    bool signalOk = ("),
        ("    if utEntryTimingReady and utExecutionRiskValid", "\n        // Strictly shadow-only anchor"),
    ):
        assert section(revised, start, end) == section(baseline, start, end)


def test_live_exit_predicates_and_eod_helper_are_byte_identical() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    assert section(revised, "forceExitNow(int ts) =>", "\ncalcQty(float entryValue") == section(
        baseline, "forceExitNow(int ts) =>", "\ncalcQty(float entryValue"
    )
    assert section(
        revised,
        "if engineEnabled and tradeActive and not na(tradeEntryBar) and bar_index > tradeEntryBar",
        "\n    if shouldExit",
    ) == section(
        baseline,
        "if engineEnabled and tradeActive and not na(tradeEntryBar) and bar_index > tradeEntryBar",
        "\n    if shouldExit",
    )


def test_buy_alert_is_exactly_the_short_three_line_format() -> None:
    text = REVISED.read_text()
    buy = section(text, "        string buyMessage = (", "\n\n        if barstate.isrealtime")
    assert buy == '''        string buyMessage = (
            "🟢 BUY | " + syminfo.ticker +
            "\\nPrice: ₹" + fmtPrice(tradeEntry) +
            "\\nQty: " + str.tostring(tradeQty)
        )'''
    for forbidden in ("Schema", "Version", "NGQualificationTime", "UTBuyTime", "UTTrail", "MFE", "MAE"):
        assert forbidden not in buy


def test_sell_alert_is_short_and_uses_full_position_quantity() -> None:
    text = REVISED.read_text()
    sell = section(text, "            string sellMessage = (", "\n\n            if barstate.isrealtime")
    for required in (
        '"🔴 SELL | " + syminfo.ticker',
        '"\\nPrice: ₹" + fmtPrice(exitReference)',
        '"\\nQty: " + str.tostring(tradeQty)',
        '"\\nReason: " + sellReasonDisplay',
        '"\\nPnL: " + fmtMoney(grossPnl)',
        '"\\nPnL/share: ₹" + fmtPrice(grossPnlPerShare)',
    ):
        assert required in sell
    for forbidden in ("Schema", "Version", "NGQualificationTime", "UTBuyTime", "UTTrailAtEntry", "MFE_R", "MAE_R", "Reach1", "Reach2"):
        assert forbidden not in sell
    assert 'sellReasonNow == "UT_SELL" ? "UT SELL"' in text
    assert 'sellReasonNow == "HARD_STOP" ? "HARD STOP" : "EOD"' in text


def test_action_alert_call_frequencies_are_unchanged() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    for call in (
        "alert(buyMessage, alert.freq_once_per_bar_close)",
        "alert(sellMessage, alert.freq_once_per_bar)",
    ):
        assert revised.count(call) == baseline.count(call) == 1


def test_dynamic_price_labels_are_display_only() -> None:
    text = REVISED.read_text()
    visuals = section(text, "// Visuals", "// Research-only data-window outputs")
    assert '"BUY\\n₹" + fmtPrice(tradeEntry)' in visuals
    assert '"SELL\\n₹" + fmtPrice(exitAlertLatchedReference)' in visuals
    assert "barstate.isconfirmed and exitAlertLatchedThisBar" in visuals
    assert "exitChartLabelEmittedThisBar := true" in visuals
    assert "tradeActive :=" not in visuals
    assert "tradeStop :=" not in visuals
    assert "tradeQty :=" not in visuals

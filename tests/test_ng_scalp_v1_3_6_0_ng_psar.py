"""NG + PSAR v1.3.6.0 live-timing and immutability invariants."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "NG_Scalp_Alerts_v1_3_5_3_FRESH_UT_BUY_ONLY.pine"
REVISED = ROOT / "NG_Scalp_Alerts_v1_3_6_0_NG_PSAR.pine"
SOURCE_SHA256 = "612fa6b5dd2d877fc21a78791c66219303c0d06721dac613be00a19877272d51"


def section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    return text[start_at:text.index(end, start_at)]


def entry_section(text: str) -> str:
    return section(text, "    string ngSourceNow =", "\n        // Strictly shadow-only anchor")


def exit_section(text: str) -> str:
    return section(text, "// PSAR-managed exit engine", "// NG qualification + PSAR timing entry engine")


def actionable_buy(*, enabled: bool, qualified: bool, armed: bool, fresh_psar_buy: bool) -> bool:
    current = enabled and qualified and fresh_psar_buy
    prior_arm = enabled and armed and fresh_psar_buy
    return current or prior_arm


def actionable_exit(*, active: bool, fresh_psar_sell: bool, eod: bool) -> str | None:
    if not active:
        return None
    if fresh_psar_sell:
        return "PSAR_SELL"
    if eod:
        return "EOD"
    return None


def test_01_source_v1353_is_immutable() -> None:
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == SOURCE_SHA256


def test_02_new_version_is_separate_and_named_v1360() -> None:
    text = REVISED.read_text()
    assert REVISED.exists() and REVISED != SOURCE
    assert 'indicator("NG Scalp Alerts v1.3.6.0 - NG + PSAR", shorttitle="NGS1360"' in text
    assert 'table.cell(dash, 1, 0, "v1.3.6.0"' in text


def test_03_everget_license_and_frozen_parameters_are_present() -> None:
    text = REVISED.read_text()
    assert "Copyright (c) 2019-present, Alex Orekhov (everget)" in text
    assert "GPL-3.0 license" in text
    assert "const float PSAR_START = 0.02" in text
    assert "const float PSAR_INCREMENT = 0.02" in text
    assert "const float PSAR_MAXIMUM = 0.20" in text
    assert "ta.sar(PSAR_START, PSAR_INCREMENT, PSAR_MAXIMUM)" in text


def test_04_psar_signals_require_confirmed_direction_flips() -> None:
    text = REVISED.read_text()
    assert "int psarDir = psar < close ? 1 : -1" in text
    assert "bool psarBuySignal = psarDir == 1 and psarDir[1] == -1" in text
    assert "bool psarSellSignal = psarDir == -1 and psarDir[1] == 1" in text
    assert "bool psarFreshBuyConfirmed = barstate.isconfirmed and psarBuySignal" in text
    assert "bool psarFreshSellConfirmed = barstate.isconfirmed and psarSellSignal" in text


def test_05_every_actionable_buy_requires_same_bar_fresh_psar_buy() -> None:
    entry = entry_section(REVISED.read_text())
    assert "bool enterFromCurrentNg = enablePsarManagedMode and qualified and psarFreshBuyConfirmed" in entry
    assert "bool enterFromArmedNg = enablePsarManagedMode and armedWindowValid and psarFreshBuyConfirmed" in entry
    assert "bool psarEntryTimingReady = enterFromCurrentNg or enterFromArmedNg" in entry
    assert "if psarEntryTimingReady and psarExecutionRiskValid" in entry


def test_06_stale_psar_buy_cannot_trigger_later_entry() -> None:
    entry = entry_section(REVISED.read_text())
    assert "ta.barssince(psarFreshBuyConfirmed)" not in entry
    assert "ta.valuewhen(psarFreshBuyConfirmed" not in entry
    assert "recentPsar" not in entry and "psarBarsSince" not in entry
    assert not actionable_buy(enabled=True, qualified=True, armed=False, fresh_psar_buy=False)


def test_07_same_bar_ng_and_psar_path_enters() -> None:
    assert actionable_buy(enabled=True, qualified=True, armed=False, fresh_psar_buy=True)
    assert 'enterFromCurrentNg ? "SAME_BAR_NG_PSAR" : "ARMED_NG_THEN_PSAR"' in REVISED.read_text()


def test_08_valid_armed_ng_and_fresh_psar_path_enters() -> None:
    assert actionable_buy(enabled=True, qualified=False, armed=True, fresh_psar_buy=True)


def test_09_expired_or_absent_arm_cannot_enter() -> None:
    text = REVISED.read_text()
    assert "bar_index - ngUtArmBar > psarConfirmationWindow" in text
    assert "bar_index - ngUtArmBar <= psarConfirmationWindow" in text
    assert not actionable_buy(enabled=True, qualified=False, armed=False, fresh_psar_buy=True)


def test_10_buy_price_is_confirmed_candle_close() -> None:
    entry = entry_section(REVISED.read_text())
    assert "tradeEntry := close" in entry
    assert "utTradeBuyTime := entryPsarBuyTime" in entry
    assert '"\\nPrice: ₹" + fmtPrice(tradeEntry)' in REVISED.read_text()


def test_11_buy_price_never_uses_psar_dot() -> None:
    entry = entry_section(REVISED.read_text())
    assert "tradeEntry := psar" not in entry
    assert "tradeEntry := close" in entry


def test_12_armed_entry_recalculates_current_risk_and_quantity() -> None:
    text = REVISED.read_text()
    block = section(text, "if canLookForBuy and barstate.isconfirmed", "    // Pine-native gate telemetry")
    assert "float stopCandidate = math.min(rawStop, minimumDistanceStop)" in block
    assert "float riskCandidate = close - stopCandidate" in block
    assert "int qtyCandidate = stopOk ? calcQty(close, stopCandidate) : 0" in block


def test_13_normal_exit_requires_fresh_confirmed_psar_sell() -> None:
    live_exit = exit_section(REVISED.read_text())
    assert "if psarFreshSellConfirmed" in live_exit
    assert 'sellReasonNow := "PSAR_SELL"' in live_exit
    assert actionable_exit(active=True, fresh_psar_sell=True, eod=False) == "PSAR_SELL"


def test_14_sell_price_is_confirmed_candle_close_not_psar_dot() -> None:
    live_exit = exit_section(REVISED.read_text())
    assert "exitReference := close" in live_exit
    assert "exitReference := psar" not in live_exit
    assert '"\\nPrice: ₹" + fmtPrice(exitReference)' in live_exit


def test_15_ut_buy_is_shadow_only() -> None:
    entry = entry_section(REVISED.read_text())
    assert "utFreshBuyConfirmed" not in entry
    assert "utBuyCross" not in entry


def test_16_ut_sell_cannot_close_live_psar_trade() -> None:
    live_exit = exit_section(REVISED.read_text())
    assert "utFreshSellConfirmed" not in live_exit
    assert "utSellCross" not in live_exit
    assert 'sellReasonNow := "UT_SELL"' not in live_exit


def test_17_old_ng_stop_is_measurement_only() -> None:
    live_exit = exit_section(REVISED.read_text())
    stop_measurement = section(live_exit, "bool oldNgStopTouchedNow", "    bool shouldExit = false")
    assert "low <= tradeStop" in stop_measurement
    assert "shouldExit" not in stop_measurement
    assert "sellSignalNow" not in stop_measurement
    assert "tradeActive := false" not in stop_measurement


def test_18_fixed_target_and_r_milestones_cannot_exit() -> None:
    predicate = section(exit_section(REVISED.read_text()), "    bool shouldExit = false", "    if shouldExit")
    assert "tradeTarget" not in predicate
    assert "targetR" not in predicate
    assert "1.5" not in predicate


def test_19_supertrend_and_vwap_cannot_exit() -> None:
    predicate = section(exit_section(REVISED.read_text()), "    bool shouldExit = false", "    if shouldExit")
    assert "bearishFlip" not in predicate
    assert "supertrend" not in predicate.lower()
    assert "vwap" not in predicate.lower()


def test_20_mandatory_eod_exit_remains_and_has_lower_priority() -> None:
    live_exit = exit_section(REVISED.read_text())
    assert "bool eodExit = barstate.isconfirmed and forceExitNow(time_close)" in live_exit
    assert live_exit.index("if psarFreshSellConfirmed") < live_exit.index("else if eodExit")
    assert actionable_exit(active=True, fresh_psar_sell=False, eod=True) == "EOD"
    assert actionable_exit(active=True, fresh_psar_sell=True, eod=True) == "PSAR_SELL"


def test_21_sell_alert_reasons_are_psar_sell_and_eod() -> None:
    live_exit = exit_section(REVISED.read_text())
    assert 'sellReasonDisplay = sellReasonNow == "PSAR_SELL" ? "PSAR SELL" : "EOD"' in live_exit
    assert '"\\nReason: " + sellReasonDisplay' in live_exit


def test_22_ng_qualification_expression_is_unchanged() -> None:
    source = SOURCE.read_text()
    revised = REVISED.read_text()
    start = "    float rawStop = low - atr1 * stopBufferAtr"
    end = "\n\n    // Pine-native gate telemetry"
    assert section(revised, start, end) == section(source, start, end)


def test_23_ut_control_calculations_are_unchanged() -> None:
    source = SOURCE.read_text()
    revised = REVISED.read_text()
    start = "float utAtr ="
    end = "\n\n// Exact Everget PSAR"
    source_end = "\n\nfloat recentBreakoutHigh"
    assert section(revised, start, end) == section(source, start, source_end)


def test_24_quantity_calculation_and_max_exposure_are_unchanged() -> None:
    source = SOURCE.read_text()
    revised = REVISED.read_text()
    calc_start = "calcQty(float entryValue, float stopValue) =>"
    calc_end = "\ngrowBrokerage(float notionalValue) =>"
    assert section(revised, calc_start, calc_end) == section(source, calc_start, calc_end)
    assert 'maxExposure = input.float(300000.0, "Maximum notional exposure (₹)"' in revised


def test_25_entry_window_is_unchanged() -> None:
    source = SOURCE.read_text()
    revised = REVISED.read_text()
    assert section(revised, "entryWindowOpen(int ts) =>", "\nforceExitNow(int ts) =>") == section(
        source, "entryWindowOpen(int ts) =>", "\nforceExitNow(int ts) =>"
    )
    assert "entryStartMinute = input.int(15" in revised
    assert "entryCutoffMinute = input.int(50" in revised


def test_26_trade_cap_and_active_position_gate_are_unchanged() -> None:
    text = REVISED.read_text()
    can_buy = section(text, "bool canLookForBuy = (", "\n\n// An arm is valid")
    assert "not tradeActive" in can_buy
    assert "tradesToday < maxTradesPerDay" in can_buy
    assert "tradesToday += 1" in entry_section(text)


def test_27_cooldown_starts_only_after_actual_sell() -> None:
    live_exit = exit_section(REVISED.read_text())
    entry = entry_section(REVISED.read_text())
    assert "cooldownUntilBar := bar_index + cooldownBars" in live_exit
    assert "cooldownUntilBar :=" not in entry


def test_28_peak_capture_and_giveback_math_is_unclamped() -> None:
    live_exit = exit_section(REVISED.read_text())
    assert "psarTradePeakPrice := high" in live_exit
    assert "psarTradeLowPrice := na(psarTradeLowPrice) ? low : math.min(psarTradeLowPrice, low)" in live_exit
    assert "exitProfitPerShare / peakProfitPerShare * 100.0" in live_exit
    assert "psarTradePeakPrice - exitReference" in live_exit
    assert "math.max(capturePct" not in live_exit and "math.min(capturePct" not in live_exit
    assert (-0.20 / 1.00) * 100.0 == -20.0


def test_29_no_duplicate_actionable_sell_on_same_bar() -> None:
    live_exit = exit_section(REVISED.read_text())
    assert "bool firstExitAlertThisBar = not exitAlertLatchedThisBar" in live_exit
    assert "if firstExitAlertThisBar" in live_exit
    assert "exitAlertLatchedThisBar := true" in live_exit


def test_30_no_second_buy_while_position_is_active() -> None:
    text = REVISED.read_text()
    can_buy = section(text, "bool canLookForBuy = (", "\n\n// An arm is valid")
    assert "not tradeActive" in can_buy
    assert not actionable_buy(enabled=True, qualified=False, armed=False, fresh_psar_buy=True)


def test_31_action_labels_use_stored_close_references() -> None:
    text = REVISED.read_text()
    assert '"BUY\\n₹" + fmtPrice(tradeEntry)' in text
    assert '"SELL\\n₹" + fmtPrice(exitAlertLatchedReference)' in text
    assert "PSAR dot / native PSAR marker position is a technical level." in text

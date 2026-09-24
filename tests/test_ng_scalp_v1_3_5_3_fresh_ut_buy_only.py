"""Fresh-UT-BUY timing invariants for NG + UT Bot v1.3.5.3."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "NG_Scalp_Alerts_v1_3_5_2_UT_SELL_ONLY.pine"
REVISED = ROOT / "NG_Scalp_Alerts_v1_3_5_3_FRESH_UT_BUY_ONLY.pine"
BASELINE_SHA256 = "6cdc66f677c9b35e50c3dfb00925310b55afc665dbb464f57902758e3301b8a2"


def section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    return text[start_at:text.index(end, start_at)]


def test_baseline_is_immutable_and_new_version_is_separate() -> None:
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == BASELINE_SHA256
    assert REVISED.exists()
    assert REVISED != BASELINE


def test_every_actionable_buy_requires_same_bar_fresh_confirmed_ut_buy() -> None:
    text = REVISED.read_text()
    entry = section(text, "    string ngSourceNow =", "\n        // Strictly shadow-only anchor")
    assert "bool enterFromCurrentNg = enableUtManagedMode and qualified and utFreshBuyConfirmed" in entry
    assert "bool enterFromArmedNg = enableUtManagedMode and armedWindowValid and utFreshBuyConfirmed" in entry
    assert "bool utEntryTimingReady = enterFromCurrentNg or enterFromArmedNg" in entry
    assert "if utEntryTimingReady and utExecutionRiskValid" in entry
    assert "recentConfirmedUtBuy" not in entry
    assert "utBarsSinceConfirmedBuy" not in entry
    assert "int entryUtBuyTime = time_close" in entry


def test_allowed_and_forbidden_entry_cases() -> None:
    def actionable_buy(*, enabled: bool, qualified: bool, armed: bool, fresh_ut_buy: bool) -> bool:
        enter_from_current_ng = enabled and qualified and fresh_ut_buy
        enter_from_armed_ng = enabled and armed and fresh_ut_buy
        return enter_from_current_ng or enter_from_armed_ng

    assert actionable_buy(enabled=True, qualified=False, armed=True, fresh_ut_buy=True)
    assert actionable_buy(enabled=True, qualified=True, armed=False, fresh_ut_buy=True)
    assert not actionable_buy(enabled=True, qualified=True, armed=False, fresh_ut_buy=False)
    assert not actionable_buy(enabled=True, qualified=False, armed=True, fresh_ut_buy=False)
    assert not actionable_buy(enabled=True, qualified=False, armed=False, fresh_ut_buy=True)


def test_ng_qualification_and_ut_bot_calculations_are_unchanged() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    for start, end in (
        ("float utAtr =", "\n\nfloat recentBreakoutHigh"),
        ("    bool signalOk = (", "\n\n    // Pine-native gate telemetry"),
    ):
        assert section(revised, start, end) == section(baseline, start, end)


def test_ng_arm_window_and_expiry_are_unchanged() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    expiry = "if ngUtArmed and (bar_index - ngUtArmBar > utConfirmationWindow or not entryWindow or tradeActive or tradesToday >= maxTradesPerDay)"
    armed_window = "bool armedWindowValid = ngUtArmed and not na(ngUtArmBar) and bar_index > ngUtArmBar and bar_index - ngUtArmBar <= utConfirmationWindow"
    assert expiry in baseline and expiry in revised
    assert armed_window in baseline and armed_window in revised
    assert "if enableUtManagedMode and qualified and not utFreshBuyConfirmed and not ngUtArmed" in revised


def test_quantity_risk_and_buy_alert_format_are_unchanged() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    for start, end in (
        ("calcQty(float entryValue, float stopValue) =>", "\ngrowBrokerage(float notionalValue) =>"),
        ("    float rawStop = low - atr1 * stopBufferAtr", "\n\n    bool signalOk = ("),
        ("        tradeActive := true", "\n        utTradeNgQualificationTime :="),
        ("        string buyMessage = (", "\n\n        if barstate.isrealtime"),
    ):
        assert section(revised, start, end) == section(baseline, start, end)


def test_ut_sell_only_and_eod_exit_behavior_is_unchanged() -> None:
    baseline = BASELINE.read_text()
    revised = REVISED.read_text()
    start = "// UT-managed Exit engine"
    end = "// NG qualification + UT timing entry engine"
    assert section(revised, start, end) == section(baseline, start, end)

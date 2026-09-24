"""Static and modeled checks for the NG + UT Bot v1.3.5.0 experiment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "NG_Scalp_Alerts_v1_3_4_4_0915_ENTRY_START.pine"
EXPERIMENT = ROOT / "NG_Scalp_Alerts_v1_3_5_0_UT_BOT_EXPERIMENT.pine"
BASELINE_SHA256 = "ad501e5fe25bf60986d027dd9379f5ce1b8df8f540677e50eb14e52f72a9a173"


def section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    return text[start_at:text.index(end, start_at)]


def test_baseline_is_immutable_and_new_file_is_separate() -> None:
    import hashlib

    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == BASELINE_SHA256
    assert EXPERIMENT.exists()
    assert EXPERIMENT != BASELINE


def test_pine_version_and_frozen_ut_defaults() -> None:
    text = EXPERIMENT.read_text()
    assert text.startswith("//@version=6\n")
    assert 'enableUtManagedMode = input.bool(true, "Enable UT-managed mode"' in text
    assert 'utKeyValue = input.float(3.0, "UT Key Value", step=0.5' in text
    assert 'utAtrPeriod = input.int(10, "UT ATR Period", minval=1' in text
    assert 'utConfirmationWindow = input.int(3, "UT Confirmation Window"' in text


def test_ut_v4_recursion_is_ported_without_an_alternate_indicator() -> None:
    text = EXPERIMENT.read_text()
    expected = (
        "utTrail := close > utPreviousTrail and close[1] > utPreviousTrail ? "
        "math.max(utPreviousTrail, close - utLoss) : close < utPreviousTrail and "
        "close[1] < utPreviousTrail ? math.min(utPreviousTrail, close + utLoss) : "
        "close > utPreviousTrail ? close - utLoss : close + utLoss"
    )
    assert "float utAtr = ta.atr(utAtrPeriod)" in text
    assert "float utLoss = utKeyValue * utAtr" in text
    assert expected in text
    assert "ta.crossover(close, utTrail)" in text
    assert "ta.crossunder(close, utTrail)" in text


def test_ut_actions_require_confirmed_chart_bars() -> None:
    text = EXPERIMENT.read_text()
    assert "bool utFreshBuyConfirmed = barstate.isconfirmed and utBuyCross" in text
    assert "bool utFreshSellConfirmed = barstate.isconfirmed and utSellCross" in text
    entry = section(text, "bool canLookForBuy = (", "// Visuals")
    assert "isOneMinute" in entry
    assert "if canLookForBuy and barstate.isconfirmed" in entry


def test_existing_ng_five_minute_request_is_byte_identical() -> None:
    baseline = BASELINE.read_text()
    experiment = EXPERIMENT.read_text()
    start = "[ctxOpen5, ctxClose5, ctxAtr5, ctxEma5, ctxEma5Prev, ctxVol5, ctxTimeClose5] = request.security("
    end = "\n\nbool newCompleted5m"
    assert section(experiment, start, end) == section(baseline, start, end)
    assert experiment.count("request.security(") == baseline.count("request.security(") == 1
    assert "[open[1], close[1], ta.atr(14)[1], ta.ema(close, 20)[1], ta.ema(close, 20)[2], volume[1], time_close[1]]" in experiment
    assert "lookahead=barmerge.lookahead_on" in experiment


def test_existing_ng_qualification_expression_is_unchanged() -> None:
    baseline = BASELINE.read_text()
    experiment = EXPERIMENT.read_text()
    start = "    bool signalOk = ("
    end = "\n\n    // Pine-native gate telemetry"
    assert section(experiment, start, end) == section(baseline, start, end)


def test_risk_budget_exposure_and_quantity_helper_are_unchanged() -> None:
    baseline = BASELINE.read_text()
    experiment = EXPERIMENT.read_text()
    for fragment in (
        'riskBudget = input.float(500.0, "Planned risk per trade (₹)"',
        'maxExposure = input.float(300000.0, "Maximum notional exposure (₹)"',
        'targetR = input.float(1.50, "Target (R)"',
    ):
        assert experiment.count(fragment) == baseline.count(fragment) == 1
    start = "calcQty(float entryValue, float stopValue) =>"
    end = "\ngrowBrokerage(float notionalValue) =>"
    assert section(experiment, start, end) == section(baseline, start, end)


def test_delayed_entry_recalculates_price_stop_quantity_and_costs() -> None:
    text = EXPERIMENT.read_text()
    entry = section(text, "if canLookForBuy and barstate.isconfirmed", "// Visuals")
    assert "float rawStop = low - atr1 * stopBufferAtr" in entry
    assert "float minimumDistanceStop = close - atr1 * minStopAtr" in entry
    assert "float stopCandidate = math.min(rawStop, minimumDistanceStop)" in entry
    assert "int qtyCandidate = stopOk ? calcQty(close, stopCandidate) : 0" in entry
    assert "growwRoundTripCost(close, targetCandidate, qtyCandidate, slippageTicks)" in entry
    assert entry.index("float stopCandidate") < entry.index("bool enterFromArmedNg")
    assert "tradeEntry := close" in entry
    assert "tradeStop := stopCandidate" in entry
    assert "tradeQty := qtyCandidate" in entry


def test_ut_live_exit_has_no_fixed_target_partial_or_supertrend_exit() -> None:
    text = EXPERIMENT.read_text()
    live_exit = section(text, "// UT-managed Exit engine", "// NG qualification + UT timing entry engine")
    assert "if stopHit" in live_exit
    assert "else if utFreshSellConfirmed" in live_exit
    assert "else if eodExit" in live_exit
    assert "targetHit" not in live_exit
    assert 'sellReasonNow := "TARGET"' not in live_exit
    assert "trendExit" not in live_exit
    assert "Partial" not in live_exit
    assert "tradeTarget := na" in text


def test_legacy_supertrend_exit_switch_is_declared_but_cannot_reach_live_ut_exit() -> None:
    text = EXPERIMENT.read_text()
    live_exit = section(text, "// UT-managed Exit engine", "// NG qualification + UT timing entry engine")
    shadow_engine = section(text, "// Shadow engine update helpers", "// UT-managed Exit engine")
    assert 'exitOnBearishSupertrend = input.bool(true, "Shadow models: exit on bearish Supertrend flip"' in text
    assert "exitOnBearishSupertrend" not in live_exit
    assert "exitOnBearishSupertrend" in shadow_engine


def test_state_machine_expiry_resets_and_action_alert_dedupe_are_present() -> None:
    text = EXPERIMENT.read_text()
    assert "bar_index - ngUtArmBar > utConfirmationWindow" in text
    assert text.count("ngUtArmed := false") >= 4
    assert "not tradeActive" in section(text, "bool canLookForBuy = (", "if canLookForBuy")
    assert "not exitAlertLatchedThisBar" in text
    assert "alert(buyMessage, alert.freq_once_per_bar_close)" in text
    assert "alert(sellMessage, alert.freq_once_per_bar)" in text


def test_required_ut_measurement_and_visual_fields_exist() -> None:
    text = EXPERIMENT.read_text()
    for field in (
        "NGQualificationTime=", "UTBuyTime=", "EntryTime=", "UTTrailAtEntry=",
        "HardStop=", "InitialRiskINR=", "MFE_R=", "MAE_R=", "PeakUnrealizedR=",
        "Reach05=", "Reach1=", "Reach15=", "Reach2=", "Reach3=", "Reach5=",
        "CapturedR=", "MFECapturePct=", "GivebackR=",
    ):
        assert field in text
    for title in ("UT Trail", "UT BUY", "UT SELL", "NG+UT BUY", "NG+UT SELL"):
        assert f'"{title}"' in text


@dataclass
class UtModel:
    window: int = 3
    armed_at: int | None = None
    active: bool = False
    buys: int = 0
    sells: int = 0

    def bar(
        self,
        index: int,
        *,
        ng_qualified: bool = False,
        bars_since_ut_buy: int | None = None,
        ut_bullish: bool = False,
        fresh_ut_buy: bool = False,
        fresh_ut_sell: bool = False,
        confirmed: bool = True,
        hard_stop: bool = False,
        eod: bool = False,
        mfe_r: float = 0.0,
    ) -> str | None:
        if self.active:
            if hard_stop:
                self.active = False
                self.sells += 1
                return "HARD_STOP"
            if confirmed and fresh_ut_sell:
                self.active = False
                self.sells += 1
                return "UT_SELL"
            if confirmed and eod:
                self.active = False
                self.sells += 1
                return "EOD"
            assert mfe_r >= 0  # Measurement alone never exits.
            return None

        if self.armed_at is not None and index - self.armed_at > self.window:
            self.armed_at = None

        recent_buy = (
            confirmed
            and bars_since_ut_buy is not None
            and bars_since_ut_buy <= self.window
            and ut_bullish
        )
        delayed_buy = (
            confirmed
            and self.armed_at is not None
            and 0 < index - self.armed_at <= self.window
            and fresh_ut_buy
        )
        if ng_qualified and recent_buy or delayed_buy:
            self.active = True
            self.armed_at = None
            self.buys += 1
            return "BUY"
        if confirmed and ng_qualified and self.armed_at is None:
            self.armed_at = index
        return None


def test_modeled_entry_scenarios_a_through_e() -> None:
    same_bar = UtModel()
    assert same_bar.bar(10, ng_qualified=True, bars_since_ut_buy=0, ut_bullish=True, fresh_ut_buy=True) == "BUY"

    prior_buy = UtModel()
    assert prior_buy.bar(10, ng_qualified=True, bars_since_ut_buy=2, ut_bullish=True) == "BUY"

    delayed = UtModel()
    assert delayed.bar(10, ng_qualified=True) is None
    assert delayed.bar(12, fresh_ut_buy=True) == "BUY"

    expired = UtModel()
    assert expired.bar(10, ng_qualified=True) is None
    assert expired.bar(14, fresh_ut_buy=True) is None
    assert not expired.active

    old_buy = UtModel()
    assert old_buy.bar(10, ng_qualified=True, bars_since_ut_buy=20, ut_bullish=True) is None
    assert not old_buy.active


def test_modeled_exit_scenarios_f_through_j() -> None:
    for mfe in (1.5, 3.0):
        model = UtModel(active=True)
        assert model.bar(20, mfe_r=mfe, ut_bullish=True) is None
        assert model.active

    ut_sell = UtModel(active=True)
    assert ut_sell.bar(20, fresh_ut_sell=True) == "UT_SELL"

    stopped = UtModel(active=True)
    assert stopped.bar(20, hard_stop=True, fresh_ut_sell=True) == "HARD_STOP"

    eod = UtModel(active=True)
    assert eod.bar(20, eod=True, ut_bullish=True) == "EOD"


def test_modeled_intrabar_signals_do_not_act_and_entries_do_not_duplicate() -> None:
    intrabar_buy = UtModel()
    assert intrabar_buy.bar(10, ng_qualified=True, bars_since_ut_buy=0, ut_bullish=True, fresh_ut_buy=True, confirmed=False) is None
    assert not intrabar_buy.active

    intrabar_sell = UtModel(active=True)
    assert intrabar_sell.bar(20, fresh_ut_sell=True, confirmed=False) is None
    assert intrabar_sell.active

    one_position = UtModel()
    assert one_position.bar(10, ng_qualified=True, bars_since_ut_buy=0, ut_bullish=True) == "BUY"
    assert one_position.bar(11, ng_qualified=True, bars_since_ut_buy=0, ut_bullish=True) is None
    assert one_position.buys == 1

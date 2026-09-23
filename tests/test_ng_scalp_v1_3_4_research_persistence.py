"""Static live-invariance audit for NG Scalp v1.3.4 research persistence.

TradingView's Pine compiler is not available in this repository.  These checks
freeze the causally relevant v1.3.3 source regions by SHA-256 and prove that the
new fields remain on the shadow side of the live qualification boundary.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINE = ROOT / "NG_Scalp_Alerts_v1_3_4_FORWARD_RESEARCH_PERSISTENCE.pine"


def source() -> str:
    return PINE.read_text()


def section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    return text[start_at : text.index(end, start_at)]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_frozen_v133_live_regions_are_byte_identical() -> None:
    """Hashes were recorded from NGScalp1.3.3Shadow before editing."""
    text = source()
    frozen = {
        ("grpMode =", "grpResearch ="): "f088a4b87939f8480825a27428f014726f8bd2713a8926fc4a3a910cfe93cf9e",
        ("bool isOneMinute =", "// Gate telemetry"): "ca65cb7b47e3d1fa63c25995ac5399c3aaaa7068bec7a0b7782d805200ef8b6f",
        (
            "if engineEnabled and tradeActive and not na(tradeEntryBar)",
            "    if shouldExit",
        ): "02fd6296e53add99156cd99c322de1598940e92b36935b1f7befd6c20f1de928",
        (
            "bool cooldownComplete =",
            "    // Pine-native gate telemetry",
        ): "74e27fec11ea0bc019b1f10a10d96d26894687d97c1049328ad3c7a33a67a0ac",
        (
            "        string buyMessage = (",
            "        if barstate.isrealtime\n            alert(buyMessage",
        ): "8f1d065d56953dca0c2679f1c3aa69440dd86bd60dd72d3b669df8cf28a05c53",
        (
            "            string sellMessage = (",
            "            if barstate.isrealtime\n                alert(sellMessage",
        ): "a85569985ff52560d718bfe28c9b503fe4180b0a6906015bf3e5de57bd7ab2df",
    }
    for markers, expected in frozen.items():
        assert digest(section(text, *markers)) == expected, markers


def test_live_target_stop_quantity_timing_and_limits_are_unchanged() -> None:
    text = source()
    expected = (
        'riskBudget = input.float(500.0, "Planned risk per trade (₹)"',
        'maxExposure = input.float(300000.0, "Maximum notional exposure (₹)"',
        'targetR = input.float(1.50, "Target (R)"',
        "float rawStop = low - atr1 * stopBufferAtr",
        "float minimumDistanceStop = close - atr1 * minStopAtr",
        "float stopCandidate = math.min(rawStop, minimumDistanceStop)",
        "int qtyCandidate = stopOk ? calcQty(close, stopCandidate) : 0",
        "float targetCandidate = qtyOk ? close + riskCandidate * targetR : na",
        "bool qualified = signalOk and stopOk and qtyOk and economicsOk",
        "bool eodExit = barstate.isconfirmed and forceExitNow(time_close)",
        "bool trendExit = barstate.isconfirmed and exitOnBearishSupertrend and bearishFlip",
        "cooldownUntilBar := bar_index + cooldownBars",
        "tradesToday < maxTradesPerDay",
    )
    for fragment in expected:
        assert fragment in text


def test_shadow_result_is_one_per_completed_anchor_and_measurement_only() -> None:
    text = source()
    shadow = section(text, "if shadowResearchEnabled and array.size(shadowStudies)", "// LIVE Exit engine")
    assert "if s.eodFinalized and not s.summaryCounted" in shadow
    assert "if emitShadowResultAlert and not s.resultEmitted" in shadow
    assert 'alert("NG SCALP BUY' not in shadow
    assert 'alert("NG SCALP SELL' not in shadow
    for live_name in (
        "tradeActive", "tradeEntry", "tradeStop", "tradeTarget",
        "tradeRiskPerShare", "tradePlannedRisk", "tradeQty",
        "cooldownUntilBar", "tradesToday",
    ):
        assert not re.search(rf"\b{live_name}\s*:=", shadow), live_name


def test_stop_b_and_entry_telemetry_cannot_feed_live_decisions() -> None:
    text = source()
    qualification = section(text, "bool cooldownComplete =", "    if qualified")
    for research_name in (
        "stopB", "stopBQty", "firstEntry", "researchReset", "entryVwapDistance",
        "entrySupertrend", "entryImpulse", "entryPullback", "sessionHighKnown",
    ):
        assert not re.search(rf"\b{research_name}\b", qualification)
    assert "tradeQty := stopBQtyCandidate" not in text
    assert "tradeStop := shadowStopB" not in text
    assert "tradeTarget := s." not in text
    assert "math.floor(riskBudget / stopBRiskPerShare)" in text
    assert "math.floor(maxExposure / close)" in text


def test_model_b_is_existing_structural_runner_not_fixed_3r() -> None:
    text = source()
    model_b = section(text, "// Model B: 50% at 1.5R", "// Model C:")
    assert "SHADOW_B_PARTIAL_R" in model_b
    assert "bool bStructural = structuralExitShadow" in model_b
    assert '"STRUCTURAL 5M"' in model_b
    assert "defendedFor5mExit" in text
    assert "ctxClose5 < defendedFor5mExit" in text
    assert "bFixed3" not in model_b


def test_payload_contains_required_forward_research_evidence() -> None:
    text = source()
    payload = section(text, "string shadowResult = (", "if emitShadowResultAlert")
    required = (
        "Schema=1", "TradingDate=", "TradeId=", "EntryTime=", "EntryRef=", "R0=",
        "LiveOutcomeR=", "LiveReason=", "LiveExitTime=", "AGrossR=", "BGrossR=",
        "CGrossR=", "DGrossR=", "ExactA=", "ExactB=", "ExactC=", "ExactD=",
        "StopA=", "StopATime=", "StopB=", "StopBTime=", "StopBQty=",
        "MFEBeforeStopA_R=", "MAEBeforeStopA_R=", "PostStopA_MFE_R=",
        "ATargetFirst=", "Reach15=", "Reach2=", "Reach3=", "Reach5=", "Reach8=",
        "FirstEntry=", "ReEntry=", "SecondsSincePrevExit=", "ResetVWAP=",
        "VWAPDist=", "VWAPAtr=", "STBull=", "STBear=", "BullSTAtr=", "ATR=",
        "ImpulsePct=", "BullExpansionBars=", "PullbackPct=", "DistHODPct=",
        "GrossTargetINR=", "CostINR=", "NetTargetINR=", "CostGrossPct=",
        "BPartialTime=", "BRunnerExit=", "BLegs=", "GivebackB_R=",
    )
    for field in required:
        assert field in payload, field
    # Keep enough headroom for formatted values under TradingView's alert limit.
    literal_chars = sum(len(item) for item in re.findall(r'"([^"\\]*)"', payload))
    assert literal_chars < 1800


def test_eod_summary_and_machine_readable_prefixes_are_exact() -> None:
    text = source()
    assert text.count('"SHADOW_RESULT |') == 1
    assert text.count('"SHADOW_EOD |') == 1
    assert '"NG SCALP BUY | "' in text
    assert '"NG SCALP SELL | "' in text
    for field in (
        "Anchors=", "Completed=", "FirstEntries=", "Reentries=", "StopAHits=",
        "StopBHits=", "StopASurvivedB=", "ATargetFirst=", "Reached3R=",
        "Reached5R=", "Reached8R=", "AvgNetR_A=", "AvgNetR_B=",
        "AvgNetR_C=", "AvgNetR_D=", "PairedAB=", "PairedAC=", "PairedAD=",
    ):
        assert field in text


def test_security_request_uses_only_completed_5m_values() -> None:
    text = source()
    calls = re.findall(r"request\.security\(.*?\n\)", text, flags=re.DOTALL)
    assert len(calls) == 1
    call = calls[0]
    assert "lookahead=barmerge.lookahead_on" in call
    assert "open[1]" in call and "close[1]" in call and "time_close[1]" in call
    assert "volume[1]" in call and "ta.atr(14)[1]" in call


def test_constructor_shape_and_delimiters_are_statically_balanced() -> None:
    text = source()
    type_body = text.split("type ShadowStudy\n", 1)[1].split("\n\nvar array", 1)[0]
    field_count = sum(
        line.startswith(("    string ", "    int ", "    float ", "    bool "))
        for line in type_body.splitlines()
    )
    constructor = text.split("ShadowStudy.new(", 1)[1]
    depth = 1
    commas = 0
    in_string = False
    escaped = False
    for char in constructor:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        elif char == "," and depth == 1:
            commas += 1
    assert depth == 0
    assert commas + 1 == field_count

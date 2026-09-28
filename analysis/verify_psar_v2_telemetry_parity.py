#!/usr/bin/env python3
"""Static behavior-parity guard for PSAR V2 telemetry instrumentation.

This is intentionally conservative: it pins the reviewed source SHA, requires
the original executable decision blocks to appear byte-for-byte in the
telemetry version, and compares every assignment to decision-driving state.
It cannot replace a TradingView compiler/runtime replay, but it will fail if a
reviewed predicate, sizing equation, stop equation, exit priority, or state
transition changes textually.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SOURCE_PATH = "PSAR_Liquidity_Trade_Manager_V2_INDICATOR.pine"
TELEMETRY_PATH = REPO / "PSAR_Liquidity_Trade_Manager_V2_TELEMETRY.pine"
FIELD_DICTIONARY_PATH = REPO / "analysis/PSAR_V2_TELEMETRY_FIELD_DICTIONARY.md"
SOURCE_REF = "origin/main"
EXPECTED_SOURCE_SHA256 = "a00ec8efe06eaecf205a59e265db35172e6a871793ae871012cc034d0f6878d5"


def source_text() -> str:
    local = REPO / SOURCE_PATH
    if local.exists():
        return local.read_text(encoding="utf-8")
    return subprocess.check_output(
        ["git", "show", f"{SOURCE_REF}:{SOURCE_PATH}"], cwd=REPO, text=True
    )


def span(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    end_at = text.index(end, start_at) + len(end)
    return text[start_at:end_at]


# Each span terminates before any intentional instrumentation insertion.
FROZEN_SPANS = {
    "inputs_psar_through_filters": (
        'string GRP_PSAR = "1. PSAR"',
        "[plusDi, minusDi, adx] = ta.dmi(dmiLength, adxSmoothing)",
    ),
    "session_and_liquidity_baseline": (
        'string GRP_SESSION = "6. Session"',
        "     (not useEntryRvolFilter or (not na(relativeVolume) and relativeVolume >= minEntryRvol))",
    ),
    "sizing_function": (
        "f_intendedQty(float assumedEntry, float initialStop) =>",
        "    [riskQty, exposureQty, intendedQty]",
    ),
    "liquidity_function": (
        "f_liquidity(int intendedQty) =>",
        "    [capacityQty, requiredCapacity, status]",
    ),
    "managed_stop_function": (
        "f_stopCandidate(float entry, float riskPerShare, float highestPrice, float currentAtr, float initialStop) =>",
        "    [f_roundDown(candidate), reason]",
    ),
    "fill_reference_functions": (
        "f_marketExitPrice(float observedPrice) =>",
        "    f_roundDown(reference - exitSlippageTicks * syminfo.mintick)",
    ),
    "bar_reset": (
        "if barstate.isnew",
        "    stopAtBarOpenIp := activeStopIp",
    ),
    "entry_plan_and_predicates": (
        "float previewSignalPrice = f_roundNearest(close)",
        "bool unknownLiquiditySkip = tradeSlotAvailable and rawEntrySignal and sizingValidPreview and liquidityStatusPreview == LIQUIDITY_UNKNOWN",
    ),
    "entry_state_transition": (
        "if buyReady and not entryAlertedThisBarIp",
        "    stopAtBarOpenIp := activeStopIp",
    ),
    "skip_predicate_and_state": (
        "if (lowLiquiditySkip or unknownLiquiditySkip) and emitLowLiquiditySkipAlert and not entryAlertedThisBarIp",
        "    entryAlertedThisBarIp := true",
    ),
    "realtime_and_historical_exit_decisions": (
        "    if barstate.isrealtime",
        "                activeStopReasonIp := candidateReasonHist",
    ),
    "exit_latch_and_pnl": (
        "    if shouldExit and not exitLatchedThisBarIp",
        "        float theoreticalPnl = not na(exitPrice) ? (exitPrice - assumedEntryIp) * quantityIp : na",
    ),
    "stop_update_decision": (
        "    // Optional stop update alert: once per confirmed bar, only for meaningful moves.",
        "            stopUpdateAlertedThisBarIp := true",
    ),
    "latched_exit_marker": (
        "if exitLatchedThisBarIp",
        "         size=size.small)",
    ),
    "scanner_outputs_and_conditions": (
        'plot(psarBuyConfirmed ? 1 : 0, "SCAN_PSAR_BUY", display=display.none)',
        'alertcondition(unknownLiquiditySkip, "LIQUIDITY UNKNOWN / SKIP", "PSAR V2 LIQUIDITY UNKNOWN / SKIP")',
    ),
}


# A second guard catches added writes to any variable capable of changing a
# trade decision, even if the surrounding frozen span still exists elsewhere.
DECISION_STATE = {
    "psar", "psarDir", "psarBuySignal", "psarSellSignal", "psarBuyConfirmed",
    "psarSellConfirmed", "isOneMinute", "barCloseMinute", "sessionStartMinute",
    "sessionEndMinute", "isLiquiditySessionBar", "isNewTradingDay",
    "isFirstLiquidityBar", "barsFromLiquiditySessionStart", "completedSessionBars",
    "liquidityBaselineReady", "priorBarVolumeSma", "priorBarTradedValueSma",
    "avgVolume", "avgTradedValue", "relativeVolume", "optionalFiltersPass",
    "inTradeIp", "signalPriceIp", "assumedEntryIp", "initialStopIp",
    "activeStopIp", "activeStopReasonIp", "targetPriceIp", "oneRPriceIp",
    "riskPerShareIp", "quantityIp", "entryBarIp", "entryTimeIp",
    "highestSinceEntryIp", "entryAlertedThisBarIp", "exitLatchedThisBarIp",
    "exitAlertedThisBarIp", "stopUpdateAlertedThisBarIp", "stopAtBarOpenIp",
    "previousPsarDirIp", "latchedExitReasonIp", "latchedExitPriceIp",
    "latchedOldStopIp", "latchedNewStopIp", "previewSignalPrice",
    "previewAssumedEntry", "previewRawStop", "previewMinStop",
    "previewInitialStop", "previewRiskPerShare", "riskQtyPreview",
    "exposureQtyPreview", "intendedQtyPreview", "liquidityCapacityPreview",
    "requiredCapacityPreview", "liquidityStatusPreview", "sizingValidPreview",
    "rawEntrySignal", "tradeSlotAvailable", "buyReady", "lowLiquiditySkip",
    "unknownLiquiditySkip", "oldStopThisExecution", "shouldExit",
    "ambiguousHistorical", "exitReason", "exitPrice", "candidateStopRt",
    "candidateReasonRt", "protectiveStopHitRt", "targetHitRt", "eodExitRt",
    "priorPsarDir", "psarFastFlipRt", "psarFallbackRt", "historicalStop",
    "protectiveStopHitHist", "targetHitHist", "candidateStopHist",
    "candidateReasonHist", "baseStop", "meaningfulRaise",
}


ASSIGNMENT_RE = re.compile(
    r"^\s*(?:varip\s+)?(?:bool|int|float|string)?\s*"
    r"(?:\[(?P<tuple>[^]]+)\]|(?P<single>[A-Za-z_]\w*))\s*(?::=|=)"
)


def decision_assignments(text: str) -> list[str]:
    selected: list[str] = []
    for line in text.splitlines():
        match = ASSIGNMENT_RE.match(line)
        if not match:
            continue
        names = (
            [part.strip() for part in match.group("tuple").split(",")]
            if match.group("tuple")
            else [match.group("single")]
        )
        if any(name in DECISION_STATE for name in names):
            selected.append(line.strip())
    return selected


def payload_fields(text: str, message_name: str) -> tuple[set[str], int]:
    start = text.index(f"string {message_name} =")
    end = text.index(f"alert({message_name}", start)
    block = text[start:end]
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    joined = "".join(literals)
    fields = set(re.findall(r"(?:^|\\n)([a-z0-9_]+)=", joined))
    static_chars = sum(len(bytes(item, "utf-8").decode("unicode_escape")) for item in literals)
    return fields, static_chars


def assert_balanced_delimiters(text: str) -> None:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    in_string = False
    escaped = False
    in_comment = False
    for pos, char in enumerate(text):
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
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
        elif char == "/" and pos + 1 < len(text) and text[pos + 1] == "/":
            in_comment = True
        elif char in "([{":
            stack.append((char, pos))
        elif char in pairs:
            if not stack or stack[-1][0] != pairs[char]:
                raise SystemExit(f"FAIL unbalanced delimiter {char!r} at offset {pos}")
            stack.pop()
    if in_string:
        raise SystemExit("FAIL unterminated Pine string literal")
    if stack:
        raise SystemExit(f"FAIL unclosed delimiter {stack[-1][0]!r} at offset {stack[-1][1]}")


def main() -> int:
    original = source_text()
    telemetry = TELEMETRY_PATH.read_text(encoding="utf-8")
    assert_balanced_delimiters(telemetry)
    digest = hashlib.sha256(original.encode()).hexdigest()
    if digest != EXPECTED_SOURCE_SHA256:
        raise SystemExit(
            f"FAIL source SHA-256 changed: expected {EXPECTED_SOURCE_SHA256}, got {digest}"
        )

    checked = []
    for name, (start, end) in FROZEN_SPANS.items():
        frozen = span(original, start, end)
        if frozen not in telemetry:
            raise SystemExit(f"FAIL frozen block differs or is missing: {name}")
        checked.append(name)

    original_assignments = decision_assignments(original)
    telemetry_assignments = decision_assignments(telemetry)
    if telemetry_assignments != original_assignments:
        for i, (old, new) in enumerate(zip(original_assignments, telemetry_assignments)):
            if old != new:
                raise SystemExit(
                    f"FAIL decision assignment #{i + 1} differs:\noriginal: {old}\ntelemetry: {new}"
                )
        raise SystemExit(
            "FAIL decision assignment count differs: "
            f"original={len(original_assignments)} telemetry={len(telemetry_assignments)}"
        )

    required_alert_sites = [
        "alert(buyMessage, alert.freq_once_per_bar_close)",
        "alert(exitMessage, alert.freq_all)",
        "alert(skipMessage, alert.freq_once_per_bar_close)",
        "alert(updateMessage, alert.freq_once_per_bar_close)",
    ]
    for site in required_alert_sites:
        if original.count(site) != 1 or telemetry.count(site) != 1:
            raise SystemExit(f"FAIL alert timing/frequency site count changed: {site}")

    expected_field_counts = {
        "buyMessage": 76,
        "skipMessage": 29,
        "exitMessage": 45,
        "updateMessage": 12,
    }
    emitted_fields: set[str] = set()
    for message_name, expected_count in expected_field_counts.items():
        fields, static_chars = payload_fields(telemetry, message_name)
        if len(fields) != expected_count:
            raise SystemExit(
                f"FAIL {message_name} field count: expected {expected_count}, got {len(fields)}"
            )
        if static_chars >= 1800:
            raise SystemExit(
                f"FAIL {message_name} static literal payload is {static_chars} chars (limit 1799)"
            )
        emitted_fields.update(fields)

    dictionary = FIELD_DICTIONARY_PATH.read_text(encoding="utf-8")
    documented_fields = set(re.findall(r"`([a-z0-9_]+)`", dictionary))
    missing_docs = emitted_fields - documented_fields
    if missing_docs:
        raise SystemExit(f"FAIL undocumented payload fields: {sorted(missing_docs)}")

    print("PASS: trading behavior changed: NO")
    print(f"source_sha256={digest}")
    print(f"frozen_blocks_verified={len(checked)}")
    print(f"decision_assignments_verified={len(original_assignments)}")
    print(f"alert_sites_verified={len(required_alert_sites)}")
    print(f"unique_payload_fields_documented={len(emitted_fields)}")
    print("delimiter_balance_verified=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

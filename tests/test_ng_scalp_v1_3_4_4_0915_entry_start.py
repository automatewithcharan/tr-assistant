"""09:15 entry-start cohort and live-invariance checks for NG Scalp v1.3.4.4."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINE = ROOT / "NG_Scalp_Alerts_v1_3_4_4_0915_ENTRY_START.pine"
PATCH = ROOT / "NG_Scalp_v1_3_4_3a_to_v1_3_4_4.diff"
AUDIT = ROOT / "NG_Scalp_v1_3_4_4_0915_ENTRY_AUDIT.md"
BASELINE_SHA256 = "c9ed65ab1b1ade0a8233ed5859573234f206ac1bb3341137aaef968bea2fa756"


def section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    return text[start_at:text.index(end, start_at)]


def test_recorded_patch_identifies_the_pinned_v1343a_baseline() -> None:
    patch = PATCH.read_text()
    audit = AUDIT.read_text()
    assert patch.startswith(
        "--- NG_Scalp_Alerts_v1_3_4_3a_OVERLAP_TELEMETRY_FIX.pine\n"
        "+++ NG_Scalp_Alerts_v1_3_4_4_0915_ENTRY_START.pine\n"
    )
    assert BASELINE_SHA256 in audit


def test_entry_start_is_0915_and_old_0920_default_is_absent() -> None:
    text = PINE.read_text()
    assert 'entryStartHour = input.int(9, "Entry start hour IST"' in text
    assert 'entryStartMinute = input.int(15, "Entry start minute IST"' in text
    assert 'entryStartMinute = input.int(20, "Entry start minute IST"' not in text
    assert "09:20" not in text
    assert "9, 20" not in text


def test_entry_end_and_exit_times_are_unchanged() -> None:
    fixed = PINE.read_text()
    for fragment in (
        'entryCutoffHour = input.int(14, "Entry cutoff hour IST"',
        'entryCutoffMinute = input.int(50, "Entry cutoff minute IST"',
        'forceExitHour = input.int(15, "Force-exit hour IST"',
        'forceExitMinute = input.int(15, "Force-exit minute IST"',
    ):
        assert fixed.count(fragment) == 1


def test_only_entry_window_helper_controls_the_start_time() -> None:
    text = PINE.read_text()
    helper = section(text, "entryWindowOpen(int ts) =>", "forceExitNow(int ts) =>")
    assert 'hour(ts, "Asia/Kolkata")' in helper
    assert 'minute(ts, "Asia/Kolkata")' in helper
    assert "mm >= entryStartMinute" in helper
    assert text.count("entryWindowOpen(time_close)") == 1
    time_scan = [
        line for line in text.splitlines()
        if re.search(r"09:20|9, 20|entryStartMinute", line)
    ]
    assert time_scan == [
        'entryStartMinute = input.int(15, "Entry start minute IST", minval=0, maxval=59, group=grpTime)',
        "    bool afterStart = hh > entryStartHour or (hh == entryStartHour and mm >= entryStartMinute)",
    ]


def test_confirmed_one_minute_entry_boundary_is_unchanged() -> None:
    fixed = PINE.read_text()
    entry = section(fixed, "bool cooldownComplete =", "    // Pine-native gate telemetry")
    assert "isOneMinute" in entry
    assert "if canLookForBuy and barstate.isconfirmed" in entry
    assert "entryWindowOpen(time_close)" in entry
    assert "bool canLookForBuy = (" in entry
    assert "    isOneMinute and" in entry
    assert "    entryWindow and" in entry


def test_live_exit_and_buy_sell_payloads_are_present_and_outside_the_patch() -> None:
    fixed = PINE.read_text()
    patch = PATCH.read_text()
    regions = (
        ("if engineEnabled and tradeActive and not na(tradeEntryBar)", "// LIVE Entry engine"),
        ("        string buyMessage = (", "        if barstate.isrealtime\n            alert(buyMessage"),
        ("            string sellMessage = (", "            if barstate.isrealtime\n                alert(sellMessage"),
    )
    for start, end in regions:
        assert section(fixed, start, end), start
        assert start not in patch


def test_risk_target_quantity_and_qualification_are_unchanged() -> None:
    fixed = PINE.read_text()
    fragments = (
        'riskBudget = input.float(500.0, "Planned risk per trade (₹)"',
        'maxExposure = input.float(300000.0, "Maximum notional exposure (₹)"',
        'targetR = input.float(1.50, "Target (R)"',
        "float stopCandidate = math.min(rawStop, minimumDistanceStop)",
        "int qtyCandidate = stopOk ? calcQty(close, stopCandidate) : 0",
        "float targetCandidate = qtyOk ? close + riskCandidate * targetR : na",
        "bool qualified = signalOk and stopOk and qtyOk and economicsOk",
        "cooldownUntilBar := bar_index + cooldownBars",
        "tradesToday < maxTradesPerDay",
    )
    for fragment in fragments:
        assert fixed.count(fragment) == 1


def test_fire_control_blocks_are_present_and_outside_the_patch() -> None:
    fixed = PINE.read_text()
    patch = PATCH.read_text()
    for start, end in (
        ("int shadowFireControlDayKey =", "if newDay or na(sessionHighKnown)"),
        ("bool shadowResultAlreadyEmittedIntrabar =", "s.lifecycle.summaryCounted := true"),
        ("bool shadowEodAlreadyEmittedIntrabar =", "// A chart without a 15:15"),
    ):
        assert section(fixed, start, end)
        assert start not in patch


def test_overlap_telemetry_and_research_payloads_are_present_and_outside_the_patch() -> None:
    fixed = PINE.read_text()
    patch = PATCH.read_text()
    regions = (
        ("// Freeze both overlap concepts", "float entryVwapDistanceValue"),
        ("ShadowReentryContext reentry =", "ShadowEntryTelemetry entryTelemetry ="),
        ("string shadowResult = (", "bool shadowResultAlreadyEmittedIntrabar"),
        ("string shadowEodMessage = (", "// Reserve the aggregate day"),
    )
    for start, end in regions:
        assert section(fixed, start, end), start
        assert start not in patch


def test_recorded_source_diff_has_only_the_four_expected_line_pairs() -> None:
    patch_lines = PATCH.read_text().splitlines()
    removed = [line[1:] for line in patch_lines if line.startswith("-") and not line.startswith("---")]
    added = [line[1:] for line in patch_lines if line.startswith("+") and not line.startswith("+++")]
    assert removed == [
        'indicator("NG Scalp Alerts v1.3.4.3a - Overlap Telemetry Fix", shorttitle="NGS1343A", overlay=true, max_labels_count=500)',
        "// v1.3.4.3a separates live Model-A overlap from broader A/B/C/D research overlap.",
        'entryStartMinute = input.int(20, "Entry start minute IST", minval=0, maxval=59, group=grpTime)',
        '    table.cell(dash, 1, 0, "v1.3.4.3a RESEARCH", text_color=color.white, bgcolor=color.new(color.blue, 35))',
    ]
    assert added == [
        'indicator("NG Scalp Alerts v1.3.4.4 - 09:15 Entry Start", shorttitle="NGS1344", overlay=true, max_labels_count=500)',
        "// v1.3.4.4 changes only the entry-window start to 09:15 IST (cohort 0915).",
        'entryStartMinute = input.int(15, "Entry start minute IST", minval=0, maxval=59, group=grpTime)',
        '    table.cell(dash, 1, 0, "v1.3.4.4 RESEARCH", text_color=color.white, bgcolor=color.new(color.blue, 35))',
    ]

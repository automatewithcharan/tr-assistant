"""Static safety checks for the v0.8.18 research-only gate ablation.

These tests deliberately compare against the requested immutable baseline commit.
They do not attempt to simulate TradingView's Pine runtime.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINE_NAME = "NG_Entry_Assistant_v0_8_18_DIRECT_CONFIRMED_MOMENTUM_SHADOW_COMPILE_FIX.pine"
PINE = ROOT / PINE_NAME
BASELINE = "992245806edb33a103596d93e44f7a7173ecfc9c"


def baseline_text() -> str:
    return subprocess.run(
        ["git", "show", f"{BASELINE}:{PINE_NAME}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def strip_research_additions(text: str) -> str:
    markers = (
        ("INPUTS", "INPUTS"),
        ("HELPERS", "HELPERS"),
        ("LOGIC", "LOGIC"),
    )
    for begin, end in markers:
        pattern = (
            rf"\n// BEGIN RESEARCH GATE ABLATION {begin}[^\n]*\n"
            rf".*?"
            rf"// END RESEARCH GATE ABLATION {end}[^\n]*\n"
        )
        text, count = re.subn(pattern, "", text, flags=re.DOTALL)
        assert count == 1, f"expected exactly one marked {begin} block"
    return text


def strip_coverage_observability_additions(text: str) -> str:
    for name in ("DECLARATIONS", "RESET", "CONFIRMED VOLUME", "PROBE", "ARMED", "RISK"):
        pattern = (
            rf"\n\s*// BEGIN COVERAGE OBSERVABILITY C {name}[^\n]*\n"
            rf".*?"
            rf"\s*// END COVERAGE OBSERVABILITY C {name}[^\n]*\n"
        )
        replacement = "" if name == "CONFIRMED VOLUME" else "\n"
        text, count = re.subn(pattern, replacement, text, flags=re.DOTALL)
        assert count == 1, f"expected exactly one C observability {name} block"
    return text


def replace_shadow_and_dashboard_with_baseline(current: str, baseline: str) -> str:
    """Canonicalize the two intentionally mutable, non-actionable regions."""
    f0_start = "// F0 — Direct Confirmed Momentum — FORWARD SHADOW ONLY."
    f0_end = "// existing script already operates close to Pine's plot-count ceiling."
    dashboard_start = "dashPosition = dashboardPosInput"

    current_f0_start = current.index(f0_start)
    current_f0_end = current.index(f0_end, current_f0_start) + len(f0_end)
    baseline_f0_start = baseline.index(f0_start)
    baseline_f0_end = baseline.index(f0_end, baseline_f0_start) + len(f0_end)
    current = (
        current[:current_f0_start]
        + baseline[baseline_f0_start:baseline_f0_end]
        + current[current_f0_end:]
    )

    current_dashboard = current.index(dashboard_start)
    baseline_dashboard = baseline.index(dashboard_start)
    return current[:current_dashboard] + baseline[baseline_dashboard:]


def test_only_research_shadow_and_display_regions_differ_from_baseline() -> None:
    """All bytes outside marked research, F0 shadow, and dashboard stay frozen."""
    baseline = baseline_text()
    current = strip_research_additions(PINE.read_text())
    current = strip_coverage_observability_additions(current)
    current = replace_shadow_and_dashboard_with_baseline(current, baseline)
    assert current == baseline


def test_actionable_lane_c_predicates_are_byte_identical() -> None:
    """The C momentum, arm and second-confirmation BUY predicates are frozen."""
    baseline = baseline_text()
    current = PINE.read_text()
    anchors = (
        ("cMomentumQuality =", "\n\n// Remember the reason"),
        ("if cState == 0 and (na(freshSetupAfterBar)", "\n    cEntryCandidate"),
        ("if cState == 2 and barstate.isrealtime", "\n    cEntryContextOk"),
    )
    for start, end in anchors:
        old = baseline[baseline.index(start):baseline.index(end, baseline.index(start))]
        new = current[current.index(start):current.index(end, current.index(start))]
        assert new == old, start


def test_f0_uses_existing_episode_controls_and_remains_shadow_only() -> None:
    text = PINE.read_text()
    f0 = text.split("// F0 — Direct Confirmed Momentum — FORWARD SHADOW ONLY.", 1)[1].split(
        "// F0 results are available", 1
    )[0]
    assert "f0StudyStartedToday" not in f0
    assert "f0StudyCountToday < FAST_MAX_WATCHES_DAY" in f0
    assert "f0EpisodeOffBars >= FAST_REARM_BARS" in f0
    assert "not f0StudyActive" in f0
    for name in (
        "autoState", "bState", "cState", "dState", "eState",
        "freshSetupAfterBar", "aEntryNow", "bEntryNow", "cEntryNow",
        "dEntryNow", "eEntryNow",
    ):
        assert not re.search(rf"\b{re.escape(name)}\s*:=", f0), name
    assert "buyTicket(" not in f0
    assert "partialTicket(" not in f0
    assert "stopTicket(" not in f0
    assert "\nrunF0Study() =>\n" in f0
    assert "\n[f0LaunchNow, f0StudyActive, f0LastDecision] = runF0Study()\n" in f0


def test_gate_ablation_is_scoped_to_avoid_ce10295() -> None:
    """S0-S5 declarations must stay inside one function, not global scope."""
    text = PINE.read_text()
    logic = text.split("// BEGIN RESEARCH GATE ABLATION LOGIC", 1)[1].split(
        "// END RESEARCH GATE ABLATION LOGIC", 1
    )[0]
    assert "\nrunGateAblation() =>\n" in logic
    assert "\nrunGateAblation()\n" in logic
    for line in logic.splitlines():
        if re.match(r"^(?:bool|int|float|string|var|varip)\s+ga", line):
            raise AssertionError(f"top-level GA declaration would risk CE10295: {line}")


def test_research_is_off_by_default_and_has_no_plot_or_alertcondition() -> None:
    text = PINE.read_text()
    logic = text.split("// BEGIN RESEARCH GATE ABLATION LOGIC", 1)[1].split(
        "// END RESEARCH GATE ABLATION LOGIC", 1
    )[0]
    assert 'enableGateAblationStudy = input.bool(false' in text
    assert 'enableGateAblationTelemetry = input.bool(false' in text
    assert "plot(" not in logic
    assert "plotshape(" not in logic
    assert "alertcondition(" not in logic
    assert "enableGateAblationTelemetry" in logic


def test_shadow_prefix_never_appears_in_frozen_actionable_baseline() -> None:
    baseline = baseline_text()
    assert not re.search(r"\bga[A-Z]", baseline)
    current_without_research = strip_research_additions(PINE.read_text())
    assert not re.search(r"\bga[A-Z]", current_without_research)


def test_shadow_block_cannot_assign_actionable_state() -> None:
    text = PINE.read_text()
    logic = text.split("// BEGIN RESEARCH GATE ABLATION LOGIC", 1)[1].split(
        "// END RESEARCH GATE ABLATION LOGIC", 1
    )[0]
    forbidden_assignments = (
        "autoState", "bState", "cState", "dState", "eState",
        "freshSetupAfterBar", "aBuyToday", "bBuyToday", "cBuyToday",
        "dBuyToday", "eBuyToday", "symbolBuysToday",
        "aEntryNow", "bEntryNow", "cEntryNow", "dEntryNow", "eEntryNow",
        "aStopNow", "bStopNowEv", "cStopNowEv", "dStopNowEv", "eStopNowEv",
    )
    for name in forbidden_assignments:
        assert not re.search(rf"\b{re.escape(name)}\s*:=", logic), name
    assert "buyTicket(" not in logic
    assert "partialTicket(" not in logic
    assert "stopTicket(" not in logic


def test_ellen_ohlc_forensic_math() -> None:
    open_, high, low, close = 360.60, 366.00, 360.05, 364.50
    candle_range = high - low
    body_fraction = (close - open_) / candle_range
    close_position = (close - low) / candle_range
    assert round(body_fraction * 100, 3) == 65.546
    assert round(close_position * 100, 3) == 74.790
    assert body_fraction >= 0.65
    assert close_position < 0.75

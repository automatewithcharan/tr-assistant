import pandas as pd
import subprocess
import sys
from pathlib import Path

from scripts.assess_full_rank_opportunity_ceiling import (
    SLOT_COUNTS,
    best_nonoverlapping_schedule,
    best_shared_capital_slot_schedule,
    evaluate_ceiling,
    evaluate_selector_gap,
    evaluate_slot_ceiling,
)


def events():
    return pd.DataFrame({
        "date": ["2026-07-23"] * 4,
        "Contract": ["t1_5_s1_0_h60"] * 4,
        "Symbol": ["A", "B", "C", "D"],
        "entry_time": pd.to_datetime(["2026-07-23 09:20", "2026-07-23 09:20", "2026-07-23 09:31", "2026-07-23 09:42"]),
        "exit_time": pd.to_datetime(["2026-07-23 09:30", "2026-07-23 09:40", "2026-07-23 09:41", "2026-07-23 09:50"]),
        "NetReturnPct": [1.4, 2.0, 1.4, -1.1],
        "DayChangeRank": [2, 1, 3, 4],
        "EntrySpreadBps": [5.0, 5.0, 5.0, 5.0],
        "RankVelocityPerMinute": [1.0, 2.0, 1.0, 0.0],
        "RankImprovement": [2.0, 3.0, 1.0, 0.0],
        "DayChangePct": [1.0, 1.1, 1.2, 1.3],
        "BalancedScore": [0.5, 0.6, 0.4, 0.3],
        "Cohort": ["test"] * 4,
        "EntryAsk": [100.0] * 4,
        "ExitBid": [101.4, 102.0, 101.4, 98.9],
        "ExitReason": ["target", "target", "target", "stop"],
    })


def test_ceiling_uses_best_nonoverlapping_positive_schedule():
    pnl, selected = best_nonoverlapping_schedule(events(), capital=500_000, max_trades=10)
    assert pnl == 14_000.0
    assert selected["Symbol"].tolist() == ["A", "C"]


def test_ceiling_honors_trade_cap():
    pnl, selected = best_nonoverlapping_schedule(events(), capital=500_000, max_trades=1)
    assert pnl == 10_000.0
    assert selected["Symbol"].tolist() == ["B"]


def test_shared_capital_slot_oracle_enforces_concurrency_and_symbol_uniqueness():
    frame = pd.DataFrame(
        {
            "date": ["2026-07-23"] * 4,
            "Contract": ["t1_5_s1_0_h60"] * 4,
            "Symbol": ["A", "B", "A", "C"],
            "entry_time": pd.to_datetime(
                [
                    "2026-07-23 09:20",
                    "2026-07-23 09:20",
                    "2026-07-23 09:31",
                    "2026-07-23 09:21",
                ]
            ),
            "exit_time": pd.to_datetime(
                [
                    "2026-07-23 09:30",
                    "2026-07-23 09:40",
                    "2026-07-23 09:41",
                    "2026-07-23 09:29",
                ]
            ),
            "EntryAsk": [100.0] * 4,
            "ExitBid": [102.0, 101.0, 103.0, 104.0],
            "StressPct": [0.1] * 4,
        }
    )

    one_pnl, one = best_shared_capital_slot_schedule(frame, slots=1)
    two_pnl, two = best_shared_capital_slot_schedule(frame, slots=2)

    assert one["Symbol"].tolist() == ["C", "A"]
    assert two["Symbol"].value_counts().max() == 1
    # Equal slots diversify execution capacity; they do not manufacture
    # capital. The clairvoyant concentrated schedule remains the upper bound.
    assert one_pnl == 34_000.0
    assert two_pnl == 19_250.0
    assert two_pnl <= one_pnl
    for instant in two["entry_time"]:
        concurrent = (
            two["entry_time"].le(instant) & two["exit_time"].gt(instant)
        ).sum()
        assert concurrent <= 2


def test_slot_ceiling_keeps_partial_sessions_out_of_summary():
    coverage = pd.DataFrame([{"date": "2026-07-23", "eligible": False}])
    daily, summary, selected = evaluate_slot_ceiling(events(), coverage)

    assert len(daily) == len(SLOT_COUNTS) * len(
        set(daily["contract"])
    )
    assert summary.empty
    assert not selected.empty
    assert not bool(daily["can_promote_strategy"].any())


def test_partial_session_never_enters_eligible_summary():
    coverage = pd.DataFrame([{"date": "2026-07-23", "eligible": False}])
    daily, summary, _ = evaluate_ceiling(events(), coverage)
    assert not daily.empty
    assert summary.empty
    assert not bool(daily["can_promote_strategy"].any())
    assert daily.loc[daily["contract"].eq("t1_5_s1_0_h60"), "first_observation_one_slot_pnl"].iloc[0] == 14_000.0


def test_command_line_help_runs_outside_repository(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/assess_full_rank_opportunity_ceiling.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_selector_gap_distinguishes_causal_capture_from_ceiling():
    coverage = pd.DataFrame([{"date": "2026-07-23", "eligible": True}])
    daily, _, _ = evaluate_ceiling(events(), coverage)
    gap = evaluate_selector_gap(events(), daily, ["2026-07-23"])
    row = gap[(gap["contract"] == "t1_5_s1_0_h60") & (gap["selector"] == "rank_first")].iloc[0]
    assert row["avg_first_observation_ceiling_pnl"] == 14_000.0
    assert row["avg_causal_pnl"] == 4_500.0
    assert row["opportunity_capture_pct"] == 32.14
    assert not bool(row["evidence_accepted"])


def test_partial_gap_is_explicitly_separate_and_non_promoting():
    coverage = pd.DataFrame([{"date": "2026-07-23", "eligible": False}])
    daily, _, _ = evaluate_ceiling(events(), coverage)
    gap = evaluate_selector_gap(
        events(), daily, ["2026-07-23"], eligible_only=False
    )
    assert not gap.empty
    assert set(gap["session_scope"]) == {"partial_incomplete_diagnostic"}
    assert not bool(gap["paper_promotion_allowed"].any())

import pandas as pd
import subprocess
import sys
from pathlib import Path

from scripts.assess_full_rank_opportunity_ceiling import (
    best_nonoverlapping_schedule,
    evaluate_ceiling,
    evaluate_selector_gap,
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

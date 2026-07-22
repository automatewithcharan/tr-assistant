import pandas as pd
import subprocess
import sys
from pathlib import Path

from scripts.assess_full_rank_opportunity_ceiling import (
    best_nonoverlapping_schedule,
    evaluate_ceiling,
)


def events():
    return pd.DataFrame({
        "date": ["2026-07-23"] * 4,
        "Contract": ["t1_5_s1_0_h60"] * 4,
        "Symbol": ["A", "B", "C", "D"],
        "entry_time": pd.to_datetime(["2026-07-23 09:20", "2026-07-23 09:22", "2026-07-23 09:31", "2026-07-23 09:42"]),
        "exit_time": pd.to_datetime(["2026-07-23 09:30", "2026-07-23 09:40", "2026-07-23 09:41", "2026-07-23 09:50"]),
        "NetReturnPct": [1.4, 2.0, 1.4, -1.1],
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

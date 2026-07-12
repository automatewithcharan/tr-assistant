import pandas as pd
import pytest

from scripts.audit_intraday_multi_trade_scaling import (
    build_daily,
    load_ranked_candidates,
    summarize,
)


def test_load_ranked_candidates_filters_and_deduplicates_symbol(tmp_path):
    path = tmp_path / "trades.csv"
    pd.DataFrame(
        [
            {"date": "2026-01-01", "symbol": "AAA", "event_type": "a", "trade_return_pct": 1.0, "rank_score": 0.5, "adjusted_score": 0.6, "vwap_dist_pct": 0.8},
            {"date": "2026-01-01", "symbol": "AAA", "event_type": "b", "trade_return_pct": -1.0, "rank_score": 0.4, "adjusted_score": 0.5, "vwap_dist_pct": 0.9},
            {"date": "2026-01-01", "symbol": "BBB", "event_type": "a", "trade_return_pct": 2.0, "rank_score": 0.7, "adjusted_score": 0.7, "vwap_dist_pct": 0.4},
        ]
    ).to_csv(path, index=False)

    result = load_ranked_candidates(str(path), min_vwap_dist_pct=0.7)

    assert len(result) == 1
    assert result.iloc[0]["event_type"] == "a"


def test_build_daily_keeps_unfilled_slot_capital_in_cash():
    candidates = pd.DataFrame(
        [
            {"date": "2026-01-01", "symbol": "AAA", "trade_return_pct": 2.0},
            {"date": "2026-01-01", "symbol": "BBB", "trade_return_pct": -1.0},
        ]
    )

    daily = build_daily(
        candidates,
        ["2026-01-01", "2026-01-02"],
        slots=4,
        capital=400000,
        daily_goal=10000,
    )

    assert daily.iloc[0]["deployed_capital"] == 200000
    assert daily.iloc[0]["pnl"] == 1000
    assert daily.iloc[0]["daily_return_pct"] == pytest.approx(0.25)
    assert daily.iloc[1]["pnl"] == 0


def test_summary_reports_goal_frequency_and_segments():
    daily = pd.DataFrame(
        {
            "trades": [1] * 413,
            "pnl": [10000.0] + [0.0] * 412,
            "daily_return_pct": [2.5] + [0.0] * 412,
            "daily_goal_hit": [True] + [False] * 412,
        }
    )

    result = summarize(daily, slots=1, capital=400000, daily_goal=10000)

    assert result["daily_goal_hit_days"] == 1
    assert result["daily_goal_hit_pct"] == pytest.approx(100 / 413, abs=0.0001)
    assert result["capital_reuse"] == "disabled_no_exact_exit_timestamps"

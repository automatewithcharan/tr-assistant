import numpy as np
import pandas as pd

from scripts.walk_forward_event_selector_exit_registry import acceptance, daily_returns, selected_indices


def test_selected_indices_use_first_timestamp_and_deduplicate_symbols():
    frame = pd.DataFrame(
        [
            {"date": "2026-01-02", "symbol": "EARLY", "event_type": "vwap_reclaim", "signal_time": "2026-01-02 09:20", "score": 5.0},
            {"date": "2026-01-02", "symbol": "EARLY", "event_type": "vwap_pullback_hold", "signal_time": "2026-01-02 09:20", "score": 4.0},
            {"date": "2026-01-02", "symbol": "LATE", "event_type": "vwap_reclaim", "signal_time": "2026-01-02 10:00", "score": 99.0},
        ]
    )
    frame["signal_time"] = pd.to_datetime(frame["signal_time"])
    indices = selected_indices(frame, "event_score", positions=2)
    assert indices.tolist() == [0]


def test_daily_returns_split_capital_equally_and_keep_cash_days():
    candidates = pd.DataFrame(
        [
            {"date": "2026-01-02", "symbol": "A", "event_type": "x", "signal_time": "2026-01-02 09:20"},
            {"date": "2026-01-02", "symbol": "B", "event_type": "x", "signal_time": "2026-01-02 09:20"},
        ]
    )
    daily = daily_returns(candidates, np.array([1.0, -0.5]), ["2026-01-01", "2026-01-02"], np.array([0, 1]))
    assert daily["daily_return_pct"].tolist() == [0.0, 0.25]
    assert daily["trades"].tolist() == [0, 2]


def test_acceptance_requires_untouched_final_goal_and_confidence():
    selected = pd.Series(
        {
            "train_avg_daily_return_pct": 1.0,
            "validation_avg_daily_return_pct": 1.0,
            "selection_block_floor_pct": 0.5,
            "final_days": 104,
            "final_lower_95_pct": 0.1,
            "final_avg_daily_pnl": 9000.0,
        }
    )
    result = acceptance(selected, min_final_days=100, required_avg_pnl=10000.0)
    assert result["prefinal_consistency_gate"]
    assert result["final_confidence_gate"]
    assert not result["final_goal_gate"]
    assert not result["accepted"]
    assert not result["real_orders_allowed"]

import pandas as pd
import pytest

from scripts.evaluate_historical_event_model_live_holdout import (
    build_daily,
    summarize,
    validate_temporal_boundary,
)


def test_temporal_boundary_rejects_overlapping_training_and_live_dates():
    historical = pd.DataFrame({"date": ["2026-07-13"]})
    live = pd.DataFrame({"date": ["2026-07-13"]})
    with pytest.raises(ValueError, match="date overlap"):
        validate_temporal_boundary(historical, live)


def test_daily_uses_first_eligible_batch_and_highest_score_inside_batch():
    scored = pd.DataFrame(
        [
            {"date": "2026-07-13", "symbol": "EARLY_LOW", "event_type": "vwap_reclaim", "signal_time": "09:30:00", "frozen_rank_score": 0.60, "net_return_pct": -1.15},
            {"date": "2026-07-13", "symbol": "EARLY_HIGH", "event_type": "or15_breakout", "signal_time": "09:30:00", "frozen_rank_score": 0.80, "net_return_pct": 1.35},
            {"date": "2026-07-13", "symbol": "LATE_BEST", "event_type": "vwap_reclaim", "signal_time": "10:30:00", "frozen_rank_score": 0.99, "net_return_pct": 1.35},
        ]
    )
    daily = build_daily(scored, ["2026-07-13", "2026-07-14"], 0.40, 0.10, 400000.0)
    assert daily.iloc[0]["symbol"] == "EARLY_HIGH"
    assert daily.iloc[0]["stressed_return_pct"] == 1.25
    assert daily.iloc[1]["trades"] == 0
    assert daily.iloc[1]["stressed_return_pct"] == 0.0


def test_summary_cannot_promote_with_tiny_forward_sample():
    daily = pd.DataFrame(
        {
            "trades": [1, 1, 0, 1],
            "stressed_return_pct": [1.25, 1.25, 0.0, 1.25],
            "stressed_pnl": [5000.0, 5000.0, 0.0, 5000.0],
        }
    )
    result = summarize(daily, 400000.0, 10000.0, min_forward_sessions=20)
    assert result["sample_gate"] is False
    assert result["paper_promotion_allowed"] is False
    assert result["real_orders_allowed"] is False

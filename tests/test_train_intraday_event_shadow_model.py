import pandas as pd

from pkscreener.classes.IntradayEventEngine import CAUSAL_MODEL_FEATURES
from scripts.train_intraday_event_shadow_model import daily_picks, metrics, split_dates


def test_split_dates_is_chronological_and_disjoint():
    frame = pd.DataFrame({"date": [f"2026-01-{day:02d}" for day in range(1, 21)]})
    train, validation, final = split_dates(frame)
    assert train.isdisjoint(validation | final)
    assert validation.isdisjoint(final)
    assert max(train) < min(validation) < min(final)


def test_daily_picks_uses_only_highest_prediction_per_day():
    frame = pd.DataFrame(
        [
            {"date": "2026-01-01", "symbol": "A", "prediction": 0.7, "net_return_pct": -1.0},
            {"date": "2026-01-01", "symbol": "B", "prediction": 0.8, "net_return_pct": 1.5},
        ]
    )
    picks = daily_picks(frame, 0.6)
    assert picks.iloc[0]["symbol"] == "B"


def test_metrics_counts_idle_days_and_rupee_goal():
    frame = pd.DataFrame(
        [
            {"date": "2026-01-01", "symbol": "A", "prediction": 0.9, "net_return_pct": 2.5},
            {"date": "2026-01-02", "symbol": "B", "prediction": 0.1, "net_return_pct": 5.0},
        ]
    )
    result = metrics(frame, 0.5, capital=400000, daily_goal=10000)
    assert result["trades"] == 1
    assert result["avg_daily_return_pct"] == 1.25
    assert result["daily_goal_hit_pct"] == 50.0


def test_model_schema_contains_no_outcomes():
    forbidden = {"target_before_stop", "net_return_pct", "max_favorable_pct", "max_adverse_pct"}
    assert forbidden.isdisjoint(CAUSAL_MODEL_FEATURES)

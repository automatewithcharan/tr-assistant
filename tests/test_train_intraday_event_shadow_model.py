import pandas as pd

from pkscreener.classes.IntradayEventEngine import CAUSAL_MODEL_FEATURES
from scripts.train_intraday_event_shadow_model import (
    daily_picks,
    frozen_baseline_metrics,
    load_outcomes,
    metrics,
    split_dates,
)


def test_split_dates_is_chronological_and_disjoint():
    frame = pd.DataFrame({"date": [f"2026-01-{day:02d}" for day in range(1, 21)]})
    train, validation, final = split_dates(frame)
    assert train.isdisjoint(validation | final)
    assert validation.isdisjoint(final)
    assert max(train) < min(validation) < min(final)


def test_daily_picks_uses_first_eligible_signal_not_future_highest_prediction():
    frame = pd.DataFrame(
        [
            {"date": "2026-01-01", "symbol": "A", "signal_time": "2026-01-01 09:30", "prediction": 0.7, "net_return_pct": -1.0},
            {"date": "2026-01-01", "symbol": "B", "signal_time": "2026-01-01 10:30", "prediction": 0.8, "net_return_pct": 1.5},
        ]
    )
    picks = daily_picks(frame, 0.6)
    assert picks.iloc[0]["symbol"] == "A"


def test_metrics_counts_idle_days_and_rupee_goal():
    frame = pd.DataFrame(
        [
            {"date": "2026-01-01", "symbol": "A", "signal_time": "2026-01-01 09:30", "prediction": 0.9, "net_return_pct": 2.5},
            {"date": "2026-01-02", "symbol": "B", "signal_time": "2026-01-02 09:30", "prediction": 0.1, "net_return_pct": 5.0},
        ]
    )
    result = metrics(frame, 0.5, capital=400000, daily_goal=10000)
    assert result["trades"] == 1
    assert result["avg_daily_return_pct"] == 1.25
    assert result["daily_goal_hit_pct"] == 50.0


def test_model_schema_contains_no_outcomes():
    forbidden = {"target_before_stop", "net_return_pct", "max_favorable_pct", "max_adverse_pct"}
    assert forbidden.isdisjoint(CAUSAL_MODEL_FEATURES)


def test_load_outcomes_keeps_only_frozen_first_batch_candidate(tmp_path):
    rows = []
    for symbol, selected in [("A", True), ("B", False)]:
        row = {feature: 0.0 for feature in CAUSAL_MODEL_FEATURES}
        row.update(
            {
                "date": "2026-01-01",
                "symbol": symbol,
                "signal_time": "2026-01-01 09:30",
                "complete_session": True,
                "target_before_stop": symbol == "A",
                "net_return_pct": 1.0 if symbol == "A" else -1.0,
                "first_batch_selected": selected,
            }
        )
        rows.append(row)
    path = tmp_path / "outcomes_2026-01-01.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    loaded = load_outcomes(str(path))
    assert loaded["symbol"].tolist() == ["A"]

    all_candidates = load_outcomes(str(path), selected_only=False)
    assert all_candidates["symbol"].tolist() == ["A", "B"]


def test_frozen_baseline_applies_stress_and_never_promotes_small_sample():
    frame = pd.DataFrame(
        [
            {
                "date": "2026-01-01", "symbol": "A", "signal_time": "2026-01-01 09:20",
                "net_return_pct": 1.35, "first_batch_selected": True,
            },
            {
                "date": "2026-01-02", "symbol": "B", "signal_time": "2026-01-02 09:20",
                "net_return_pct": -1.15, "first_batch_selected": True,
            },
        ]
    )

    result = frozen_baseline_metrics(frame, capital=400_000, daily_goal=10_000)

    assert result["stress_avg_daily_return_pct"] == 0.0
    assert result["stress_avg_daily_pnl"] == 0.0
    assert not result["sample_gate_passed"]
    assert not result["paper_promotion_allowed"]
    assert not result["real_orders_allowed"]

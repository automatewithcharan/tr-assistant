import pandas as pd

from scripts.walk_forward_live_microstructure_ranker import (
    accepted,
    replay_exact_capital,
)


def test_replay_reuses_slot_only_after_exact_exit():
    scored = pd.DataFrame(
        [
            {
                "date": "2026-07-22", "symbol": "A",
                "signal_time": pd.Timestamp("2026-07-22 09:30"),
                "exit_time": pd.Timestamp("2026-07-22 09:40"),
                "expected_return_pct": 0.5, "trade_return_pct": 1.0,
                "model_trained_through": "2026-07-21",
            },
            {
                "date": "2026-07-22", "symbol": "B",
                "signal_time": pd.Timestamp("2026-07-22 09:35"),
                "exit_time": pd.Timestamp("2026-07-22 09:45"),
                "expected_return_pct": 0.6, "trade_return_pct": 5.0,
                "model_trained_through": "2026-07-21",
            },
            {
                "date": "2026-07-22", "symbol": "C",
                "signal_time": pd.Timestamp("2026-07-22 09:40"),
                "exit_time": pd.Timestamp("2026-07-22 09:50"),
                "expected_return_pct": 0.4, "trade_return_pct": 1.0,
                "model_trained_through": "2026-07-21",
            },
        ]
    )

    daily, trades = replay_exact_capital(
        scored, ["2026-07-22"], threshold=0.2, slots=1, capital=500_000
    )

    assert trades["symbol"].tolist() == ["A", "C"]
    assert daily.iloc[0]["trades"] == 2
    assert daily.iloc[0]["pnl"] == 10_000.0


def test_acceptance_requires_goal_sized_stressed_forward_evidence():
    good = {
        "sessions": 20, "trade_sessions": 16, "trades": 35,
        "avg_daily_pnl": 10_500.0, "lower_95_pct": 0.2,
        "block_floor_pct": 0.3, "goal_hit_pct": 55.0,
        "worst_session_pct": -2.0,
    }
    assert accepted(good, good, 20, 15, 30, 10_000.0)
    weak = dict(good, lower_95_pct=-0.01)
    assert not accepted(good, weak, 20, 15, 30, 10_000.0)

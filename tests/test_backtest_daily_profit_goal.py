import pandas as pd

from scripts.backtest_daily_profit_goal import _filter_alerts, simulate


def test_empty_qualified_configuration_returns_complete_zero_summary():
    alerts = pd.DataFrame(
        [
            {
                "Date": "2026-05-05",
                "When": pd.Timestamp("2026-05-05 10:00:00"),
                "Symbol": "ABC",
                "Action": "Grade-B Anomaly",
                "QualityScore": 50.0,
                "MaxFavExcursionPct": 1.0,
                "MaxAdvExcursionPct": -0.5,
                "EODReturnPct": 0.2,
                "OutcomeSource": "kite_history_backfill/5minute/ABC.csv",
            }
        ]
    )

    daily, summary = simulate(
        alerts,
        capital=500000.0,
        ticket=100000.0,
        goal_pct=1.0,
        target_pct=1.0,
        stop_pct=0.5,
        cost_pct=0.15,
        min_quality=70.0,
        action_filter="all",
        outcome_source_filter="post_alert_intraday",
        rank_by="quality",
        compound=True,
    )

    assert daily.empty
    assert summary["days"] == 0
    assert summary["trade_count"] == 0
    assert summary["worst_day_pct"] == 0.0
    assert summary["total_pnl"] == 0.0
    assert summary["ending_capital"] == 500000.0


def test_post_alert_filter_accepts_minute_and_five_minute_sources():
    frame = pd.DataFrame(
        {
            "QualityScore": [60, 60, 60],
            "Action": ["Grade-B Anomaly"] * 3,
            "OutcomeSource": [
                "kite_history_backfill/minute/2026-07-31/AAA.csv",
                "kite_history_backfill/5minute/history/BBB.csv",
                "kite_history_cache/day/CCC.csv",
            ],
        }
    )

    filtered = _filter_alerts(frame, "all", 0, "post_alert_intraday")

    assert filtered.index.tolist() == [0, 1]

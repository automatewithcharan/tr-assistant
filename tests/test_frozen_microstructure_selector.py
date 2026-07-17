import pandas as pd

from scripts.assess_frozen_microstructure_selector import accepted, daily_returns, metrics


def test_metrics_include_cash_days_and_goal_rate():
    daily = pd.DataFrame(
        {
            "date": ["d1", "d2", "d3"],
            "trades": [1, 0, 1],
            "daily_return_pct": [3.0, 0.0, -1.0],
        }
    )
    result = metrics(daily, capital=400000.0, goal=10000.0)
    assert result["sessions"] == 3
    assert result["trade_sessions"] == 2
    assert result["candidates"] == 2
    assert result["goal_hit_pct"] == 33.33


def test_acceptance_requires_goal_sized_stressed_evidence():
    base = {
        "sessions": 20,
        "trade_sessions": 15,
        "candidates": 30,
        "avg_daily_return_pct": 3.0,
        "avg_daily_pnl": 12000.0,
        "lower_95_pct": 0.5,
        "block_floor_pct": 0.4,
        "goal_hit_pct": 60.0,
    }
    assert accepted(base, base, 20, 15, 30, 10000.0)
    weak = dict(base, avg_daily_pnl=9999.0)
    assert not accepted(base, weak, 20, 15, 30, 10000.0)


def test_metrics_accept_empty_forward_period():
    empty = pd.DataFrame(columns=["date", "trades", "daily_return_pct"])
    result = metrics(empty, capital=400000.0, goal=10000.0)
    assert result["sessions"] == 0
    assert result["lower_95_pct"] is None


def test_frozen_rule_excludes_zero_quote_rows():
    rows = pd.DataFrame(
        [
            {
                "Date": "2026-07-01",
                "When": pd.Timestamp("2026-07-01 09:30:00"),
                "Symbol": "ZERO",
                "FeatureBucket": "missed_stable_tight_sector",
                "MissedRunnerCandidate": True,
                "OrderBookStable": True,
                "SpreadOk": True,
                "BestBid": 0.0,
                "BestAsk": 0.0,
                "SpreadBps": 0.0,
                "Sector": "PHARMA",
                "SectorTop2Count": 2,
                "LastPrice": 100.0,
                "TradedValue": 50_000_000.0,
                "OutcomeAvailable": True,
                "ForwardReturnPct": 9.0,
            },
            {
                "Date": "2026-07-01",
                "When": pd.Timestamp("2026-07-01 09:31:00"),
                "Symbol": "REAL",
                "FeatureBucket": "missed_stable_tight_sector",
                "MissedRunnerCandidate": True,
                "OrderBookStable": True,
                "SpreadOk": True,
                "BestBid": 100.0,
                "BestAsk": 100.1,
                "SpreadBps": 9.995,
                "Sector": "PHARMA",
                "SectorTop2Count": 2,
                "LastPrice": 100.0,
                "TradedValue": 50_000_000.0,
                "OutcomeAvailable": True,
                "ForwardReturnPct": 0.5,
            },
        ]
    )
    daily, candidates = daily_returns(rows, ["2026-07-01"])
    assert candidates == 1
    assert daily.iloc[0]["daily_return_pct"] == 0.5

import pandas as pd

from scripts.assess_prior_session_ranker_paper import build_assessment


def _coverage(dates):
    return pd.DataFrame(
        {
            "date": dates,
            "full_session_eligible": True,
        }
    )


def _prediction(date, symbol, when, trained_through):
    return {
        "date": date,
        "when": pd.Timestamp(when),
        "symbol": symbol,
        "selected": True,
        "model_trained_through": trained_through,
        "model_source_sha256": "a" * 64,
    }


def _trade(date, symbol, entry, exit_at, pnl, trained_through):
    return {
        "date": date,
        "symbol": symbol,
        "entry_time": pd.Timestamp(entry),
        "exit_time": pd.Timestamp(exit_at),
        "deployed_capital": 500_000.0,
        "net_pnl": pnl,
        "closed": True,
        "model_trained_through": trained_through,
    }


def test_prior_session_ranker_accepts_only_strong_complete_forward_evidence():
    dates = pd.bdate_range("2026-07-27", periods=20).strftime("%Y-%m-%d").tolist()
    predictions = []
    trades = []
    for index, date in enumerate(dates):
        prior = (pd.Timestamp(date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        entry = f"{date} 09:30:00"
        predictions.append(
            _prediction(date, f"S{index}", entry, prior)
        )
        trades.append(
            _trade(
                date,
                f"S{index}",
                entry,
                f"{date} 09:45:00",
                12_000.0,
                prior,
            )
        )

    status, daily = build_assessment(
        pd.DataFrame(predictions),
        pd.DataFrame(trades),
        _coverage(dates),
        capital=500_000.0,
        daily_goal=10_000.0,
        stress_cost_pct=0.10,
        minimum_sessions=20,
        minimum_trade_sessions=10,
        minimum_trades=20,
        max_trades_per_day=10,
    )

    assert len(daily) == 20
    assert status["forward_stress"]["avg_daily_pnl"] == 11_500.0
    assert status["forward_stress"]["goal_hit_pct"] == 100.0
    assert status["accepted"] is True
    assert status["paper_promotion_allowed"] is False
    assert status["real_orders_allowed"] is False


def test_prior_session_ranker_keeps_no_trade_days_in_denominator():
    dates = pd.bdate_range("2026-07-27", periods=20).strftime("%Y-%m-%d").tolist()
    predictions = []
    trades = []
    for index, date in enumerate(dates):
        prior = (pd.Timestamp(date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if index < 10:
            entry = f"{date} 09:30:00"
            predictions.append(_prediction(date, f"S{index}", entry, prior))
            trades.append(
                _trade(
                    date,
                    f"S{index}",
                    entry,
                    f"{date} 09:45:00",
                    1_000.0,
                    prior,
                )
            )
        else:
            predictions.append(
                {
                    **_prediction(date, f"N{index}", f"{date} 09:30:00", prior),
                    "selected": False,
                }
            )

    status, daily = build_assessment(
        pd.DataFrame(predictions),
        pd.DataFrame(trades),
        _coverage(dates),
        capital=500_000.0,
        daily_goal=10_000.0,
        stress_cost_pct=0.10,
        minimum_sessions=20,
        minimum_trade_sessions=10,
        minimum_trades=10,
        max_trades_per_day=10,
    )

    assert len(daily) == 20
    assert status["forward"]["trade_sessions"] == 10
    assert status["forward"]["avg_daily_pnl"] == 500.0
    assert status["forward_stress"]["avg_daily_pnl"] == 250.0
    assert status["accepted"] is False


def test_prior_session_ranker_excludes_noncausal_model_provenance():
    date = "2026-07-27"
    entry = f"{date} 09:30:00"
    predictions = pd.DataFrame(
        [_prediction(date, "BAD", entry, date)]
    )
    trades = pd.DataFrame(
        [_trade(date, "BAD", entry, f"{date} 09:45:00", 12_000.0, date)]
    )

    status, daily = build_assessment(
        predictions,
        trades,
        _coverage([date]),
        capital=500_000.0,
        daily_goal=10_000.0,
        stress_cost_pct=0.10,
        minimum_sessions=1,
        minimum_trade_sessions=1,
        minimum_trades=1,
        max_trades_per_day=10,
    )

    assert daily.empty
    assert status["excluded_or_invalid_sessions"] == 1
    assert any("not prior-session-only" in issue for issue in status["issues"])
    assert status["accepted"] is False

import pandas as pd

from scripts.assess_historical_rank_displacement import replay


def _predictions():
    return pd.DataFrame(
        [
            {
                "date": "2026-07-23",
                "symbol": "OLD",
                "signal_time": pd.Timestamp("2026-07-23 10:00:00"),
                "exit_time": pd.Timestamp("2026-07-23 10:30:00"),
                "EntryAsk": 100.0,
                "trade_return_pct": -1.1,
                "expected_return_pct": 0.80,
            },
            {
                "date": "2026-07-23",
                "symbol": "NEW",
                "signal_time": pd.Timestamp("2026-07-23 10:04:00"),
                "exit_time": pd.Timestamp("2026-07-23 10:20:00"),
                "EntryAsk": 200.0,
                "trade_return_pct": 1.4,
                "expected_return_pct": 0.86,
            },
        ]
    )


def _tapes(when="2026-07-23 10:04:00"):
    return {
        "2026-07-23": {
            "OLD": pd.DataFrame(
                [
                    {
                        "when": pd.Timestamp(when),
                        "bid": 99.9,
                        "RecorderGeneration": 5,
                    }
                ]
            )
        }
    }


def test_historical_replay_uses_contemporaneous_bid_for_displacement():
    baseline_daily, _ = replay(
        _predictions(), _tapes(), displacement=False
    )
    displaced_daily, displaced_trades = replay(
        _predictions(), _tapes(), displacement=True
    )

    switch = displaced_trades[
        displaced_trades["actual_exit_reason"].eq("rank_displaced")
    ].iloc[0]
    assert switch["symbol"] == "OLD"
    assert switch["replacement_symbol"] == "NEW"
    assert switch["exit_bid"] == 99.9
    assert displaced_daily.iloc[0]["pnl"] > baseline_daily.iloc[0]["pnl"]


def test_historical_replay_rejects_stale_exit_quote():
    _, trades = replay(
        _predictions(),
        _tapes("2026-07-23 10:03:00"),
        displacement=True,
    )

    assert not trades["actual_exit_reason"].eq("rank_displaced").any()

import pandas as pd

from scripts.build_intraday_alert_whatif_dashboard import _simulate_trade, summarize


def _candles():
    return pd.DataFrame(
        {
            "open": [100.0, 99.2, 99.8, 100.6],
            "high": [100.4, 99.8, 101.0, 102.0],
            "low": [99.8, 98.9, 99.7, 100.1],
            "close": [100.1, 99.5, 100.7, 101.8],
        },
        index=pd.to_datetime(
            [
                "2026-06-12 09:25:00",
                "2026-06-12 09:26:00",
                "2026-06-12 09:27:00",
                "2026-06-12 09:28:00",
            ]
        ),
    )


def test_simulate_trade_waits_for_minus_one_pullback_then_hits_target():
    result = _simulate_trade(_candles(), alert_price=100.0, entry_offset_pct=-1.0, target_pct=1.5, stop_pct=1.0)

    assert result["entered"] is True
    assert result["entry_price"] == 99.0
    assert result["outcome"] == "target"
    assert result["gross_return_pct"] == 1.5


def test_simulate_trade_reports_no_entry_when_pullback_never_happens():
    result = _simulate_trade(_candles(), alert_price=100.0, entry_offset_pct=-3.0, target_pct=1.0, stop_pct=1.0)

    assert result["entered"] is False
    assert result["outcome"] == "no_entry"


def test_summarize_ranks_total_pnl_by_lane_entry_and_target():
    results = pd.DataFrame(
        [
            {"lane": "grade_a_anomaly", "entry_offset_pct": -1.0, "target_pct": 1.0, "stop_pct": 1.0, "entered": True, "outcome": "target", "net_return_pct": 0.7, "mfe_pct": 1.2, "mae_pct": -0.2},
            {"lane": "grade_a_anomaly", "entry_offset_pct": -1.0, "target_pct": 1.0, "stop_pct": 1.0, "entered": True, "outcome": "stop", "net_return_pct": -1.3, "mfe_pct": 0.4, "mae_pct": -1.0},
            {"lane": "top_gainer_radar", "entry_offset_pct": -2.0, "target_pct": 0.5, "stop_pct": 1.0, "entered": True, "outcome": "target", "net_return_pct": 0.2, "mfe_pct": 0.8, "mae_pct": -0.1},
        ]
    )

    summary = summarize(results, ticket=50000)

    assert set(summary["lane"]) == {"grade_a_anomaly", "top_gainer_radar"}
    row = summary[summary["lane"].eq("grade_a_anomaly")].iloc[0]
    assert row["entries"] == 2
    assert row["target_rate_pct"] == 50.0

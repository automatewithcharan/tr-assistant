from datetime import datetime

import pandas as pd

from scripts.audit_intraday_goal_alert_outcomes import (
    apply_costs,
    classify_lane,
    evaluate_alert,
    first_event,
)


def _candles():
    idx = pd.to_datetime(
        [
            "2026-06-12 09:25:00",
            "2026-06-12 09:26:00",
            "2026-06-12 09:27:00",
        ]
    )
    return pd.DataFrame(
        {
            "open": [100.0, 100.5, 101.8],
            "high": [101.0, 102.0, 103.5],
            "low": [99.8, 100.2, 101.0],
            "close": [100.6, 101.7, 103.0],
        },
        index=idx,
    )


def test_first_event_is_conservative_when_stop_and_target_share_candle():
    candles = pd.DataFrame(
        {"open": [100.0], "high": [102.0], "low": [98.5], "close": [101.0]},
        index=pd.to_datetime(["2026-06-12 09:25:00"]),
    )

    event, _, price = first_event(candles, entry_price=100.0, stop=99.0, target1=101.5, target2=103.0)

    assert event == "stop_first"
    assert price == 99.0


def test_evaluate_alert_next_open_uses_post_sent_entry_and_hits_t1():
    row = pd.Series(
        {
            "GeneratedAt": datetime(2026, 6, 12, 9, 25, 0),
            "SentAt": pd.Timestamp("2026-06-12 09:25:11"),
            "Symbol": "TEST",
            "Action": "🔥 Grade-A Runner",
            "Checks": "GoalMode✓ Q:80/100 [A]",
            "PriceNow": 100.0,
            "StopLoss": 99.5,
            "Target1": 102.0,
            "Target2": 104.0,
        }
    )

    out = evaluate_alert(row, _candles(), entry_model="next_open")

    assert out["has_minute_data"] is True
    assert out["entry_price"] == 100.5
    assert out["first_event"] == "t1_first"
    assert out["hit_1pct"] is True
    assert out["alert_price_inside_sent_bar"] is True


def test_evaluate_alert_preserves_fetch_error_without_crashing():
    row = pd.Series(
        {
            "GeneratedAt": datetime(2026, 6, 12, 9, 25, 0),
            "SentAt": pd.Timestamp("2026-06-12 09:25:11"),
            "Symbol": "TEST",
            "Action": "🔥 Grade-A Runner",
            "Checks": "GoalMode✓ Q:80/100 [A]",
            "PriceNow": 100.0,
            "StopLoss": 99.5,
            "Target1": 102.0,
            "Target2": 104.0,
        }
    )

    out = evaluate_alert(row, pd.DataFrame(), entry_model="next_open", fetch_error="403 forbidden")

    assert out["has_minute_data"] is False
    assert out["fetch_error"] == "403 forbidden"
    assert out["first_event"] == "no_data"


def test_classify_lane_separates_watch_from_anomaly():
    assert classify_lane("🧪 Engine Confirmed Watch", "EngineWatch✓ paper/watch only") == "engine_watch"
    assert classify_lane("🔥 Grade-A Runner", "GoalMode✓ Q:80/100 [A]") == "grade_a_anomaly"
    assert classify_lane("⚡ Grade-B Anomaly", "GoalMode✓ Q:67/100 [B]") == "grade_b_anomaly"


def test_apply_costs_reduces_gross_return():
    result = apply_costs(100.0, 101.5, buy_cost_pct=0.001, sell_cost_pct=0.001, slippage_bps=2.0)

    assert result.gross_return_pct == 1.5
    assert result.net_return_pct < result.gross_return_pct

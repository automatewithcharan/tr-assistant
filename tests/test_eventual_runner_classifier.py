import pandas as pd
from pathlib import Path
import subprocess
import sys

from scripts.walk_forward_eventual_runner_classifier import (
    FEATURES,
    build_runner_labels,
    build_target_before_stop_labels,
    evaluate_opportunity_bounds,
    evaluate_registry,
    optimistic_opportunity_bound,
    score_walk_forward,
)


def _feature_row(date, symbol, minute, label):
    row = {
        "date": date,
        "symbol": symbol,
        "signal_minute": minute,
        "entry_time": f"{date} 09:35",
        "eventual_runner_label": label,
    }
    for feature in FEATURES:
        row[feature] = 0.9 if label else 0.2
    row["signal_minute"] = minute
    return row


def test_runner_label_requires_cross_strictly_after_signal():
    candidates = pd.DataFrame(
        [
            {"date": "2026-01-02", "symbol": "AAA", "signal_minute": 570},
            {"date": "2026-01-02", "symbol": "BBB", "signal_minute": 600},
        ]
    )
    events = pd.DataFrame(
        [
            {
                "date": "2026-01-02",
                "symbol": "AAA",
                "runner_threshold_pct": 10.0,
                "first_cross_minute": 600,
            },
            {
                "date": "2026-01-02",
                "symbol": "BBB",
                "runner_threshold_pct": 10.0,
                "first_cross_minute": 600,
            },
        ]
    )
    labeled = build_runner_labels(candidates, events)
    assert labeled.set_index("symbol").loc["AAA", "eventual_runner_label"] == 1
    assert labeled.set_index("symbol").loc["BBB", "eventual_runner_label"] == 0


def test_direct_trade_label_requires_target_before_stop_exit():
    candidates = pd.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC"],
            "exit__test": ["target", "stop", "horizon"],
        }
    )
    labeled = build_target_before_stop_labels(candidates, "exit__test")
    assert labeled["target_before_stop_label"].tolist() == [1, 0, 0]


def test_research_entrypoints_run_without_external_pythonpath():
    root = Path(__file__).resolve().parents[1]
    for script in (
        "scripts/research_rank_acceleration_registry.py",
        "scripts/walk_forward_eventual_runner_classifier.py",
    ):
        result = subprocess.run(
            [sys.executable, str(root / script), "--help"],
            cwd=root,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_walk_forward_model_is_always_trained_through_prior_date():
    dates = pd.bdate_range("2025-01-01", periods=70).strftime("%Y-%m-%d").tolist()
    rows = []
    for index, date in enumerate(dates):
        for candidate in range(5):
            label = int(candidate == 0 and index % 2 == 0)
            rows.append(_feature_row(date, f"S{candidate}", 570, label))
    scored = score_walk_forward(
        pd.DataFrame(rows),
        dates,
        lookback_days=40,
        min_train_days=20,
        min_train_rows=50,
        min_positive_rows=5,
        retrain_every_days=5,
        min_samples_leaf=5,
    )
    assert not scored.empty
    assert all(
        str(trained) < str(date)
        for trained, date in zip(scored["model_trained_through"], scored["date"])
    )


def test_registry_uses_exact_unleveraged_reuse_and_keeps_orders_off():
    dates = pd.bdate_range("2026-01-01", periods=40).strftime("%Y-%m-%d").tolist()
    rows = []
    for date in dates:
        row = _feature_row(date, "AAA", 570, 1)
        row.update(
            {
                "runner_probability": 0.9,
                "entry_time": f"{date} 09:35",
                "exit_time__test": f"{date} 09:40",
                "return__test": 1.0,
            }
        )
        rows.append(row)
    summary, _ = evaluate_registry(
        pd.DataFrame(rows),
        dates,
        "return__test",
        "exit_time__test",
        thresholds=(0.8,),
    )
    assert not summary.empty
    assert set(summary["capital_reuse"]) == {"exact_exit_bar_unleveraged"}
    assert "stress_selection_block_floor_pct" in summary.columns
    assert not summary["real_orders_allowed"].any()


def test_signal_window_selection_uses_prefinal_period_only():
    dates = pd.bdate_range("2025-01-01", periods=120).strftime("%Y-%m-%d").tolist()
    rows = []
    for index, date in enumerate(dates):
        prefinal = index < 90
        for minute, value in ((570, 1.0 if prefinal else -1.0), (585, -1.0 if prefinal else 1.0)):
            row = _feature_row(date, f"S{minute}", minute, 1)
            row.update(
                {
                    "runner_probability": 0.9,
                    "entry_time": f"{date} {'09:35' if minute == 570 else '09:50'}",
                    "exit_time__test": f"{date} {'09:40' if minute == 570 else '09:55'}",
                    "return__test": value,
                }
            )
            rows.append(row)
    summary, _ = evaluate_registry(
        pd.DataFrame(rows),
        dates,
        "return__test",
        "exit_time__test",
        thresholds=(0.8,),
    )
    selected = summary.iloc[0]
    assert selected["signal_window"] == "through_09:30"
    assert bool(selected["selection_gate_passed"])
    assert selected["stress_final_avg_daily_pnl"] < 0.0


def test_optimistic_bound_uses_future_positive_returns_and_ignores_overlap():
    date = "2026-01-02"
    scored = pd.DataFrame(
        [
            {"date": date, "signal_minute": 570, "return__test": 1.5},
            {"date": date, "signal_minute": 570, "return__test": -1.0},
            {"date": date, "signal_minute": 585, "return__test": 1.5},
        ]
    )
    bound = optimistic_opportunity_bound(
        scored,
        [date],
        "return__test",
        capital=400_000.0,
        slots=1,
        daily_goal=10_000.0,
    )
    assert bound.iloc[0]["trades"] == 2
    assert bound.iloc[0]["pnl"] == 12_000.0
    assert bool(bound.iloc[0]["daily_goal_hit"])
    assert not bool(bound.iloc[0]["real_orders_allowed"])


def test_opportunity_bound_stress_and_goal_respect_arguments():
    dates = pd.bdate_range("2026-01-01", periods=40).strftime("%Y-%m-%d").tolist()
    scored = pd.DataFrame(
        [
            {"date": date, "signal_minute": 570, "return__test": 1.5}
            for date in dates
        ]
    )
    summary, daily = evaluate_opportunity_bounds(
        scored,
        dates,
        "return__test",
        capital=100_000.0,
        daily_goal=1_400.0,
        stress_cost_pct=0.2,
    )
    one_slot = summary.set_index("slots").loc[1]
    assert one_slot["final_goal_hit_pct"] == 100.0
    assert one_slot["stress_final_goal_hit_pct"] == 0.0
    assert set(daily) == {1, 2}
    assert not summary["paper_promotion_allowed"].any()
    assert not summary["real_orders_allowed"].any()

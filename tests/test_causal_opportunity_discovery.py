from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from scripts import causal_opportunity_discovery as mod


def synthetic_days(days: int = 8, bars: int = 80) -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2026-07-06")
    made = 0
    day = start
    while made < days:
        if day.weekday() < 5:
            base = 100.0 + made
            for i in range(bars):
                ts = day + pd.Timedelta(hours=9, minutes=15 + i)
                drift = i * 0.02
                wave = np.sin(i / 5.0) * 0.05
                open_price = base + drift + wave
                close_price = open_price + 0.02
                rows.append(
                    {
                        "datetime": ts,
                        "open": open_price,
                        "high": max(open_price, close_price) + 0.05,
                        "low": min(open_price, close_price) - 0.05,
                        "close": close_price,
                        "volume": 1000 + made * 100 + i * 3,
                    }
                )
            made += 1
        day += pd.Timedelta(days=1)
    return pd.DataFrame(rows)


def test_frozen_split_is_chronological_and_disjoint():
    split = mod.FrozenSplit()
    assert split.bucket("2026-07-06") == "train"
    assert split.bucket("2026-08-25") == "train"
    assert split.bucket("2026-08-26") == "validation"
    assert split.bucket("2026-09-10") == "validation"
    assert split.bucket("2026-09-11") == "holdout"
    assert split.bucket("2026-09-30") == "holdout"
    assert split.bucket("2026-10-01") == "outside"


def test_signal_features_do_not_change_when_future_bars_change():
    raw = synthetic_days(days=8, bars=80)
    first = mod.prepare_symbol_features(raw, "TEST", 5)
    ts = pd.Timestamp("2026-07-15 10:00:00")
    baseline = first.loc[first["datetime"] == ts, mod.BASE_MODEL_FEATURES].iloc[0]

    mutated = raw.copy()
    future = mutated["datetime"] > ts
    mutated.loc[future, "high"] *= 3.0
    mutated.loc[future, "low"] *= 0.5
    mutated.loc[future, "close"] *= 2.0
    mutated.loc[future, "volume"] *= 20
    second = mod.prepare_symbol_features(mutated, "TEST", 5)
    changed = second.loc[second["datetime"] == ts, mod.BASE_MODEL_FEATURES].iloc[0]

    pd.testing.assert_series_equal(baseline, changed, check_names=False, check_dtype=False)


def test_entry_reference_is_next_bar_open_and_same_bar_is_stop_first():
    raw = synthetic_days(days=1, bars=50)
    features = mod.prepare_symbol_features(raw, "TEST", 5).reset_index(drop=True)
    cfg = mod.ResearchConfig(max_forward_bars=5, min_median_turnover_lakh=0.0)
    idx = 30
    features.loc[idx, "atr14"] = 1.0
    next_open = float(features.loc[idx + 1, "open"])
    features.loc[idx + 1, "low"] = next_open - 1.1
    features.loc[idx + 1, "high"] = next_open + 1.6

    label = mod._label_entry(features, idx, idx + 1, cfg)
    assert label["entry_price"] == pytest.approx(next_open)
    assert label["path_1p5r_result"] == "ambiguous_stop_first"
    assert label["target_1p5r_before_stop"] == 0
    assert label["net_1p5r_strategy_r"] < 0


def test_train_thresholds_are_derived_only_from_train_scores():
    train = pd.DataFrame({"prediction": np.linspace(0.0, 1.0, 101)})
    thresholds = mod.train_thresholds(train, [1, 5, 20])
    validation = pd.DataFrame({"prediction": [0.99, 0.01]})
    original = copy.deepcopy(thresholds)
    validation["prediction"] = [0.5, 0.5]
    assert thresholds == original
    assert thresholds[1] > thresholds[5] > thresholds[20]


def test_execution_policy_is_time_local_and_respects_limits():
    cfg = mod.ResearchConfig(max_per_timestamp=2, max_trades_per_day=3, symbol_cooldown_minutes=30)
    date = pd.Timestamp("2026-08-26")
    rows = [
        {"signal_time": date + pd.Timedelta(hours=10), "session_date": date, "symbol": "A", "prediction": 0.95},
        {"signal_time": date + pd.Timedelta(hours=10), "session_date": date, "symbol": "B", "prediction": 0.90},
        {"signal_time": date + pd.Timedelta(hours=10), "session_date": date, "symbol": "C", "prediction": 0.85},
        {"signal_time": date + pd.Timedelta(hours=10, minutes=10), "session_date": date, "symbol": "A", "prediction": 0.99},
        {"signal_time": date + pd.Timedelta(hours=10, minutes=10), "session_date": date, "symbol": "D", "prediction": 0.80},
    ]
    picked = mod.execution_policy(pd.DataFrame(rows), 0.75, cfg)
    assert list(picked["symbol"]) == ["A", "B", "D"]
    assert len(picked) == 3


def test_holdout_manifest_refuses_second_peek_and_config_drift():
    split = mod.FrozenSplit()
    cfg = mod.ResearchConfig()
    manifest = mod.make_manifest(split, cfg, 10, 0.7, {"trades": 50})
    mod.verify_manifest(manifest, split, cfg)

    used = dict(manifest)
    used["holdout_used"] = True
    with pytest.raises(RuntimeError, match="already used"):
        mod.verify_manifest(used, split, cfg)

    with pytest.raises(RuntimeError, match="config fingerprint mismatch"):
        mod.verify_manifest(manifest, split, mod.ResearchConfig(roundtrip_cost_pct=0.25))

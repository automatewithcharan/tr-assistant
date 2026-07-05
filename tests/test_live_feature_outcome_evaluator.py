from pathlib import Path

import pandas as pd

from scripts.evaluate_live_feature_outcomes import (
    evaluate_rows,
    load_live_features,
    load_symbol_days,
    summarize,
)


def _write_symbol(path: Path, symbol: str, rows: list[dict]):
    pd.DataFrame(rows).to_csv(path / f"{symbol}.csv", index=False)


def test_evaluator_labels_target_before_stop(tmp_path):
    features_path = tmp_path / "live_features_2026-07-06.csv"
    candle_dir = tmp_path / "candles"
    candle_dir.mkdir()
    pd.DataFrame(
        [
            {
                "When": "2026-07-06 09:20:00",
                "Symbol": "AAA",
                "LastPrice": 100.0,
                "MissedRunnerCandidate": True,
                "OrderBookStable": True,
                "SpreadOk": True,
                "AlreadyAlertedToday": False,
                "SectorTop2Count": 2,
            }
        ]
    ).to_csv(features_path, index=False)
    _write_symbol(
        candle_dir,
        "AAA",
        [
            {"datetime": "2026-07-06 09:20:00", "high": 100.4, "low": 99.8, "close": 100.2},
            {"datetime": "2026-07-06 09:25:00", "high": 101.2, "low": 100.1, "close": 101.0},
            {"datetime": "2026-07-06 09:30:00", "high": 102.0, "low": 100.8, "close": 101.6},
        ],
    )

    features = load_live_features(str(features_path))
    days = load_symbol_days(str(candle_dir), {"AAA"})
    out = evaluate_rows(features, days)

    assert out.iloc[0]["FeatureBucket"] == "missed_stable_tight_sector"
    assert out.iloc[0]["MaxFavorablePct"] == 2.0
    assert out.iloc[0]["MaxAdversePct"] == -0.2
    assert out.iloc[0]["t1_0_s1_0_outcome"] == "target"
    assert out.iloc[0]["t1_0_s1_0_return_pct"] == 1.0


def test_evaluator_labels_stop_before_target(tmp_path):
    features = pd.DataFrame(
        [
            {
                "When": pd.Timestamp("2026-07-06 09:20:00"),
                "Date": "2026-07-06",
                "Symbol": "BBB",
                "LastPrice": 100.0,
                "MissedRunnerCandidate": False,
                "OrderBookStable": False,
                "SpreadOk": True,
                "AlreadyAlertedToday": True,
                "SectorTop2Count": 0,
            }
        ]
    )
    day = pd.DataFrame(
        [
            {"datetime": pd.Timestamp("2026-07-06 09:20:00"), "high": 100.3, "low": 98.8, "close": 99.0},
            {"datetime": pd.Timestamp("2026-07-06 09:25:00"), "high": 101.5, "low": 99.2, "close": 101.0},
        ]
    )

    out = evaluate_rows(features, {("BBB", "2026-07-06"): day})

    assert out.iloc[0]["FeatureBucket"] == "alerted_today"
    assert out.iloc[0]["t1_0_s1_0_outcome"] == "stop"
    assert out.iloc[0]["t1_0_s1_0_return_pct"] == -1.0


def test_summary_reports_bucket_hit_rates(tmp_path):
    features = pd.DataFrame(
        [
            {
                "When": pd.Timestamp("2026-07-06 09:20:00"),
                "Date": "2026-07-06",
                "Symbol": "AAA",
                "LastPrice": 100.0,
                "MissedRunnerCandidate": True,
                "OrderBookStable": True,
                "SpreadOk": True,
                "AlreadyAlertedToday": False,
                "SectorTop2Count": 2,
            },
            {
                "When": pd.Timestamp("2026-07-06 09:20:00"),
                "Date": "2026-07-06",
                "Symbol": "BBB",
                "LastPrice": 100.0,
                "MissedRunnerCandidate": True,
                "OrderBookStable": True,
                "SpreadOk": True,
                "AlreadyAlertedToday": False,
                "SectorTop2Count": 2,
            },
        ]
    )
    days = {
        ("AAA", "2026-07-06"): pd.DataFrame(
            [{"datetime": pd.Timestamp("2026-07-06 09:20:00"), "high": 101.2, "low": 99.8, "close": 101.0}]
        ),
        ("BBB", "2026-07-06"): pd.DataFrame(
            [{"datetime": pd.Timestamp("2026-07-06 09:20:00"), "high": 100.2, "low": 98.8, "close": 99.0}]
        ),
    }

    summary = summarize(evaluate_rows(features, days))

    row = summary.iloc[0]
    assert row["FeatureBucket"] == "missed_stable_tight_sector"
    assert row["Rows"] == 2
    assert row["t1_0_s1_0_hit_rate_pct"] == 50.0

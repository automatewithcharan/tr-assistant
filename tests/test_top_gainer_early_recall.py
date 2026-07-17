import pandas as pd

from scripts.audit_top_gainer_early_recall import (
    evaluate_recall,
    load_runner_events,
)


def _write_symbol(path, highs):
    times = pd.date_range("2026-01-02 09:15", periods=len(highs), freq="5min")
    pd.DataFrame(
        {
            "datetime": times,
            "open": [100.0] * len(highs),
            "high": highs,
            "close": [100.0] * len(highs),
            "volume": [100_000] * len(highs),
        }
    ).to_csv(path, index=False)


def test_runner_events_record_first_threshold_cross(tmp_path):
    _write_symbol(tmp_path / "AAA.csv", [100, 101, 102, 103, 104, 106, 111])
    events = load_runner_events(
        str(tmp_path),
        (5.0, 10.0),
        workers=1,
        min_daily_turnover=1.0,
    )
    five = events[events["runner_threshold_pct"].eq(5.0)].iloc[0]
    ten = events[events["runner_threshold_pct"].eq(10.0)].iloc[0]
    assert five["first_cross_minute"] == 580
    assert ten["first_cross_minute"] == 585


def test_recall_requires_detection_before_cross_and_reports_followthrough():
    events = pd.DataFrame(
        [
            {
                "date": "2026-01-02",
                "symbol": "AAA",
                "runner_threshold_pct": 5.0,
                "first_cross_minute": 600,
                "first_cross_time": "2026-01-02 10:00",
                "peak_from_open_pct": 7.0,
                "daily_turnover": 100_000_000.0,
            },
            {
                "date": "2026-01-02",
                "symbol": "BBB",
                "runner_threshold_pct": 5.0,
                "first_cross_minute": 565,
                "first_cross_time": "2026-01-02 09:25",
                "peak_from_open_pct": 8.0,
                "daily_turnover": 100_000_000.0,
            },
        ]
    )
    candidates = pd.DataFrame(
        [
            {
                "date": "2026-01-02",
                "symbol": "AAA",
                "signal_minute": 570,
                "move_from_open_pct": 1.0,
                "exit__target": "target",
            },
            {
                "date": "2026-01-02",
                "symbol": "BBB",
                "signal_minute": 570,
                "move_from_open_pct": 5.5,
                "exit__target": "target",
            },
        ]
    )
    summary, details = evaluate_recall(
        events,
        candidates,
        (570,),
        "exit__target",
    )
    row = summary.iloc[0]
    assert row["already_crossed_by_first_scan"] == 1
    assert row["catchable_after_first_scan"] == 1
    assert row["detected_before_cross"] == 1
    assert row["recall_catchable_pct"] == 100.0
    assert row["post_detection_target_hit_pct"] == 100.0
    assert details[details["symbol"].eq("BBB")].iloc[0]["detected_before_cross"] == False

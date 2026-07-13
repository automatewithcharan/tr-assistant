from pathlib import Path

import pandas as pd

from scripts.run_live_evidence_eod import (
    _coverage,
    evidence_symbols,
    evaluate_stream,
    load_daily_evidence,
    minute_day_coverage,
)


def test_coverage_parses_persisted_false_strings():
    outcomes = pd.DataFrame(
        {
            "EvaluationEligible": ["True", "True"],
            "OutcomeAvailable": ["False", "True"],
        }
    )
    assert _coverage(outcomes) == (2, 2, 1, 50.0)


def test_load_daily_evidence_and_symbol_union(tmp_path):
    pd.DataFrame(
        [{"When": "2026-07-13 09:30:00", "Symbol": "AAA", "LastPrice": 100.0}]
    ).to_csv(tmp_path / "live_features_2026-07-13.csv", index=False)
    pd.DataFrame(
        [{"When": "2026-07-13 09:31:00", "Symbol": "BBB", "PriceNow": 200.0}]
    ).to_csv(tmp_path / "lane_decisions_2026-07-13.csv", index=False)

    features, decisions = load_daily_evidence(str(tmp_path), "2026-07-13")

    assert len(features) == 1
    assert len(decisions) == 1
    assert evidence_symbols(features, decisions) == ["AAA", "BBB"]


def test_minute_day_coverage_requires_open_and_close_bars():
    complete_times = pd.date_range("2026-07-13 09:15:00", periods=375, freq="min")
    result = minute_day_coverage(pd.DataFrame({"datetime": complete_times}), "2026-07-13")
    assert result["complete"]
    assert result["rows"] == 375

    partial = minute_day_coverage(
        pd.DataFrame({"datetime": complete_times[:200]}), "2026-07-13"
    )
    assert not partial["complete"]


def test_evaluate_stream_dedupes_and_uses_minute_execution(tmp_path):
    minute_dir = tmp_path / "minute"
    minute_dir.mkdir()
    minute_rows = [
            {
                "datetime": "2026-07-13 09:31:00",
                "open": 100.0,
                "high": 101.2,
                "low": 99.9,
                "close": 101.0,
                "volume": 1000,
            }
        ]
    for minute in range(32, 41):
        minute_rows.append(
            {
                "datetime": f"2026-07-13 09:{minute:02d}:00",
                "open": 101.0,
                "high": 101.1,
                "low": 100.9,
                "close": 101.0,
                "volume": 1000,
            }
        )
    pd.DataFrame(minute_rows).to_csv(minute_dir / "AAA.csv", index=False)
    rows = []
    for second in (0, 10, 20):
        rows.append(
            {
                "When": pd.Timestamp("2026-07-13 09:30:30") + pd.Timedelta(seconds=second),
                "Date": "2026-07-13",
                "Symbol": "AAA",
                "LastPrice": 100.0,
                "Lane": "top_gainer_radar",
                "Stage": "screen",
                "Decision": "pass",
                "Reason": "candidate passed",
            }
        )

    outcomes, summary = evaluate_stream(
        pd.DataFrame(rows),
        minute_dir,
        tmp_path / "output" / "lane_decisions",
        horizon_minutes=10,
        fee_pct=0.10,
        slippage_bps=5.0,
    )

    assert len(outcomes) == 1
    assert bool(outcomes.iloc[0]["OutcomeAvailable"])
    assert outcomes.iloc[0]["EntryPrice"] == 100.05
    assert summary.iloc[0]["CoveragePct"] == 100.0
    assert Path(tmp_path / "output" / "lane_decisions_rows.csv").exists()

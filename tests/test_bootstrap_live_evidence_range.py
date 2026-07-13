from pathlib import Path

import pandas as pd

from scripts.bootstrap_live_evidence_range import (
    combine_outputs,
    discover_evidence_dates,
    load_evidence_range,
)


def test_discovers_only_requested_evidence_dates(tmp_path):
    for name in [
        "live_features_2026-07-05.csv",
        "live_features_2026-07-06.csv",
        "lane_decisions_2026-07-07.csv",
        "unrelated.csv",
    ]:
        (tmp_path / name).write_text("x\n", encoding="utf-8")

    result = discover_evidence_dates(str(tmp_path), "2026-07-06", "2026-07-07")

    assert result == ["2026-07-06", "2026-07-07"]


def test_load_range_builds_symbol_date_requirements(tmp_path):
    pd.DataFrame(
        [{"When": "2026-07-06 09:30:00", "Symbol": "AAA", "LastPrice": 100.0}]
    ).to_csv(tmp_path / "live_features_2026-07-06.csv", index=False)
    pd.DataFrame(
        [{"When": "2026-07-07 09:30:00", "Symbol": "AAA", "PriceNow": 101.0}]
    ).to_csv(tmp_path / "lane_decisions_2026-07-07.csv", index=False)

    frames, required = load_evidence_range(
        str(tmp_path), ["2026-07-06", "2026-07-07"]
    )

    assert set(frames) == {"2026-07-06", "2026-07-07"}
    assert required == {"AAA": {"2026-07-06", "2026-07-07"}}


def test_combine_outputs_rebuilds_cross_day_summary(tmp_path):
    rows = []
    for date, value in [("2026-07-06", 0.5), ("2026-07-07", -0.5)]:
        path = tmp_path / f"{date}.csv"
        pd.DataFrame(
            [
                {
                    "Date": date,
                    "Symbol": "AAA",
                    "FeatureBucket": "stable_order_book",
                    "EvaluationEligible": True,
                    "OutcomeAvailable": True,
                    "MaxFavorablePct": 1.0,
                    "MaxAdversePct": -1.0,
                    "EodReturnPct": float("nan"),
                    "ForwardReturnPct": value,
                    "t1_0_s1_0_outcome": "target" if value > 0 else "stop",
                    "t1_0_s1_0_return_pct": value,
                }
            ]
        ).to_csv(path, index=False)
        rows.append(path)

    outcomes, summary = combine_outputs(
        rows,
        tmp_path / "combined_rows.csv",
        tmp_path / "combined_summary.csv",
    )

    assert len(outcomes) == 2
    assert summary.iloc[0]["Rows"] == 2
    assert summary.iloc[0]["CoveragePct"] == 100.0
    assert summary.iloc[0]["t1_0_s1_0_hit_rate_pct"] == 50.0
    assert summary.iloc[0]["AvgDailyEqualReturnPct"] == 0.0
    assert Path(tmp_path / "combined_summary.csv").exists()

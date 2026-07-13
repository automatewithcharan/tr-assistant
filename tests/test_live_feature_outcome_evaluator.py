from pathlib import Path

import pandas as pd

from scripts.evaluate_live_feature_outcomes import (
    dedupe_first_evidence_state,
    evaluate_rows,
    evaluate_rows_from_minutes,
    load_lane_decisions,
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
    assert row["EvaluatedSessions"] == 1


def test_lane_decision_outcomes_use_price_now_and_reason_bucket(tmp_path):
    decisions_path = tmp_path / "lane_decisions_2026-07-06.csv"
    candle_dir = tmp_path / "candles"
    candle_dir.mkdir()
    pd.DataFrame(
        [
            {
                "When": "2026-07-06 09:25:00",
                "Lane": "top_gainer_radar",
                "Stage": "",
                "Symbol": "CUPID",
                "Decision": "pass",
                "Reason": "radar candidate passed",
                "PriceNow": 100.0,
                "ChangePct": 2.4,
                "BuyerSellerRatio": 2.0,
            }
        ]
    ).to_csv(decisions_path, index=False)
    _write_symbol(
        candle_dir,
        "CUPID",
        [
            {"datetime": "2026-07-06 09:25:00", "high": 100.3, "low": 99.8, "close": 100.1},
            {"datetime": "2026-07-06 09:30:00", "high": 101.6, "low": 100.2, "close": 101.3},
        ],
    )

    decisions = load_lane_decisions(str(decisions_path))
    days = load_symbol_days(str(candle_dir), {"CUPID"})
    out = evaluate_rows(decisions, days)

    assert out.iloc[0]["EvaluationSource"] == "lane_decision"
    assert out.iloc[0]["LastPrice"] == 100.0
    assert out.iloc[0]["FeatureBucket"] == "top_gainer_radar|no_stage|pass|radar candidate passed"
    assert out.iloc[0]["t1_5_s1_0_outcome"] == "target"
    assert out.iloc[0]["t1_5_s1_0_return_pct"] == 1.5


def test_lane_decision_summary_separates_pass_and_reject_reasons():
    outcomes = pd.DataFrame(
        [
            {
                "EvaluationSource": "lane_decision",
                "FeatureBucket": "top_gainer_radar|no_stage|pass|radar candidate passed",
                "Lane": "top_gainer_radar",
                "Stage": "",
                "Decision": "pass",
                "Reason": "radar candidate passed",
                "Symbol": "AAA",
                "MaxFavorablePct": 1.5,
                "MaxAdversePct": -0.2,
                "EodReturnPct": 1.0,
                "t1_0_s1_0_outcome": "target",
            },
            {
                "EvaluationSource": "lane_decision",
                "FeatureBucket": "top_gainer_radar|no_stage|reject|strength below radar floor",
                "Lane": "top_gainer_radar",
                "Stage": "",
                "Decision": "reject",
                "Reason": "strength below radar floor",
                "Symbol": "BBB",
                "MaxFavorablePct": 0.3,
                "MaxAdversePct": -1.2,
                "EodReturnPct": -0.8,
                "t1_0_s1_0_outcome": "stop",
            },
        ]
    )

    summary = summarize(outcomes)

    assert set(summary["Decision"]) == {"pass", "reject"}
    pass_row = summary[summary["Decision"] == "pass"].iloc[0]
    reject_row = summary[summary["Decision"] == "reject"].iloc[0]
    assert pass_row["Reason"] == "radar candidate passed"
    assert pass_row["t1_0_s1_0_hit_rate_pct"] == 100.0
    assert reject_row["Reason"] == "strength below radar floor"
    assert reject_row["t1_0_s1_0_hit_rate_pct"] == 0.0


def test_minute_evaluator_enters_next_full_minute_with_costs():
    features = pd.DataFrame(
        [
            {
                "When": pd.Timestamp("2026-07-06 09:20:30"),
                "Date": "2026-07-06",
                "Symbol": "AAA",
                "LastPrice": 100.0,
                "MissedRunnerCandidate": True,
                "OrderBookStable": True,
                "SpreadOk": True,
                "AlreadyAlertedToday": False,
                "SectorTop2Count": 2,
            }
        ]
    )
    candle_rows = [
            {
                "datetime": pd.Timestamp("2026-07-06 09:20:00"),
                "open": 99.0,
                "high": 200.0,
                "low": 50.0,
                "close": 100.0,
            },
            {
                "datetime": pd.Timestamp("2026-07-06 09:21:00"),
                "open": 100.0,
                "high": 101.2,
                "low": 99.9,
                "close": 101.0,
            },
        ]
    for minute in range(22, 31):
        candle_rows.append(
            {
                "datetime": pd.Timestamp(f"2026-07-06 09:{minute:02d}:00"),
                "open": 101.0,
                "high": 101.1,
                "low": 100.9,
                "close": 101.0,
            }
        )
    candles = pd.DataFrame(candle_rows)

    out = evaluate_rows_from_minutes(
        features,
        {("2026-07-06", "AAA"): candles},
        horizon_minutes=10,
        fee_pct=0.1,
        slippage_bps=5.0,
    )

    assert bool(out.iloc[0]["OutcomeAvailable"])
    assert out.iloc[0]["EntryPrice"] == 100.05
    assert out.iloc[0]["t1_0_s1_0_outcome"] == "target"
    assert out.iloc[0]["t1_0_s1_0_return_pct"] < 1.0


def test_late_state_is_ineligible_instead_of_shortened_to_partial_horizon():
    features = pd.DataFrame(
        [
            {
                "When": pd.Timestamp("2026-07-06 15:00:00"),
                "Date": "2026-07-06",
                "Symbol": "AAA",
                "LastPrice": 100.0,
                "MissedRunnerCandidate": True,
                "OrderBookStable": True,
                "SpreadOk": True,
                "AlreadyAlertedToday": False,
                "SectorTop2Count": 2,
            }
        ]
    )
    outcomes = evaluate_rows_from_minutes(features, {}, horizon_minutes=60)
    summary = summarize(outcomes)

    assert not bool(outcomes.iloc[0]["EvaluationEligible"])
    assert outcomes.iloc[0]["OutcomeSource"] == "insufficient_market_horizon"
    assert summary.iloc[0]["EligibleRows"] == 0


def test_missing_minute_candles_are_reported_not_counted_as_zero_returns():
    features = pd.DataFrame(
        [
            {
                "When": pd.Timestamp("2026-07-06 09:20:30"),
                "Date": "2026-07-06",
                "Symbol": "AAA",
                "LastPrice": 100.0,
                "MissedRunnerCandidate": True,
                "OrderBookStable": True,
                "SpreadOk": True,
                "AlreadyAlertedToday": False,
                "SectorTop2Count": 2,
            }
        ]
    )
    outcomes = evaluate_rows_from_minutes(features, {})
    summary = summarize(outcomes)

    assert not bool(outcomes.iloc[0]["OutcomeAvailable"])
    assert pd.isna(outcomes.iloc[0]["ForwardReturnPct"])
    assert summary.iloc[0]["EvaluatedRows"] == 0
    assert summary.iloc[0]["CoveragePct"] == 0.0
    assert pd.isna(summary.iloc[0]["AvgForwardReturnPct"])


def test_decision_evaluation_dedupes_repeated_monitor_cycles():
    rows = []
    for minute in (20, 21, 22):
        rows.append(
            {
                "When": pd.Timestamp(f"2026-07-06 09:{minute}:00"),
                "Date": "2026-07-06",
                "Symbol": "AAA",
                "Lane": "top_gainer_radar",
                "Stage": "screen",
                "Decision": "reject",
                "Reason": "low volume",
                "LastPrice": 100.0,
            }
        )
    deduped = dedupe_first_evidence_state(pd.DataFrame(rows))

    assert len(deduped) == 1
    assert deduped.iloc[0]["When"] == pd.Timestamp("2026-07-06 09:20:00")

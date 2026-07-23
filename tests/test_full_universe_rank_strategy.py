from pathlib import Path

import pandas as pd

from scripts.assess_full_universe_rank_strategy import (
    FEATURES,
    load_rank_outcomes,
    rank_outcome_coverage,
    raw_session_coverage,
    replay,
    run_audit,
)


def _raw_rows(date: str, first="09:16", snapshots=1440):
    start = pd.Timestamp(f"{date} {first}:00")
    return pd.DataFrame(
        {
            "When": [start + pd.Timedelta(seconds=15 * index) for index in range(snapshots)],
            "DayChangeRank": [1] * snapshots,
            "RecorderGeneration": [5] * snapshots,
        }
    )


def test_raw_coverage_rejects_partial_and_accepts_complete_session(tmp_path):
    partial = tmp_path / "live_features_2026-07-21.csv"
    complete = tmp_path / "live_features_2026-07-22.csv"
    _raw_rows("2026-07-21", first="12:00", snapshots=180).to_csv(partial, index=False)
    _raw_rows("2026-07-22").to_csv(complete, index=False)

    coverage = raw_session_coverage([str(tmp_path / "live_features_*.csv")])

    by_date = coverage.set_index("date")
    assert not bool(by_date.loc["2026-07-21", "full_session_eligible"])
    assert bool(by_date.loc["2026-07-22", "full_session_eligible"])


def test_raw_coverage_rejects_mid_session_recorder_gap(tmp_path):
    date = "2026-07-23"
    first = _raw_rows(date, snapshots=600)
    second_start = pd.Timestamp(f"{date} 12:26:00")
    second = pd.DataFrame(
        {
            "When": [second_start + pd.Timedelta(seconds=15 * index) for index in range(840)],
            "DayChangeRank": [1] * 840,
            "RecorderGeneration": [5] * 840,
        }
    )
    pd.concat([first, second], ignore_index=True).to_csv(
        tmp_path / f"live_features_{date}.csv", index=False
    )

    coverage = raw_session_coverage([str(tmp_path / "live_features_*.csv")])

    row = coverage.iloc[0]
    assert row["snapshots"] == 1440
    assert row["max_gap_seconds"] > 45
    assert not bool(row["continuity_ok"])
    assert not bool(row["full_session_eligible"])


def test_coverage_uses_only_latest_recorder_generation(tmp_path):
    path = tmp_path / "live_features_2026-07-23.csv"
    rows = []
    for minute in range(9 * 60 + 15, 15 * 60 + 30):
        hour, value = divmod(minute, 60)
        rows.append(
            {
                "When": f"2026-07-23 {hour:02d}:{value:02d}:00",
                "DayChangeRank": 1,
                "RecorderGeneration": "" if minute < 10 * 60 else 2,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)

    coverage = raw_session_coverage([str(path)], min_snapshots=300)

    assert len(coverage) == 1
    row = coverage.iloc[0]
    assert row["first_snapshot"] == "2026-07-23 10:00:00"
    assert int(row["snapshots"]) == 330
    assert not bool(row["full_session_eligible"])


def test_complete_older_generation_is_not_selection_eligible(tmp_path):
    path = tmp_path / "live_features_2026-07-22.csv"
    frame = _raw_rows("2026-07-22")
    frame["RecorderGeneration"] = 4
    frame.to_csv(path, index=False)

    coverage = raw_session_coverage([str(path)])

    row = coverage.iloc[0]
    assert int(row["recorder_generation"]) == 4
    assert int(row["snapshots"]) == 1440
    assert bool(row["continuity_ok"])
    assert not bool(row["full_session_eligible"])


def test_generation_five_requires_one_thousand_snapshots(tmp_path):
    date = "2026-07-22"
    timestamps = pd.date_range(
        f"{date} 09:16:00",
        f"{date} 15:15:00",
        periods=999,
    )
    pd.DataFrame(
        {
            "When": timestamps,
            "DayChangeRank": [1] * len(timestamps),
            "RecorderGeneration": [5] * len(timestamps),
        }
    ).to_csv(tmp_path / f"live_features_{date}.csv", index=False)

    coverage = raw_session_coverage(
        [str(tmp_path / "live_features_*.csv")]
    )

    row = coverage.iloc[0]
    assert int(row["snapshots"]) == 999
    assert int(row["required_min_snapshots"]) == 1000
    assert float(row["required_max_gap_seconds"]) == 45.0
    assert bool(row["continuity_ok"])
    assert not bool(row["full_session_eligible"])


def test_rank_outcomes_keep_only_buyable_executable_rank_states(tmp_path):
    path = tmp_path / "live_features_rows.csv"
    base = {
        "Date": "2026-07-22",
        "When": "2026-07-22 10:00:00",
        "OutcomeAvailable": True,
        "EntryPrice": 100.0,
        "LastPrice": 100.0,
        "BestBid": 99.95,
        "BestAsk": 100.05,
        "SpreadBps": 10.0,
        "SpreadOk": True,
        "TradedValue": 50_000_000,
        "DayChangePct": 1.5,
        "DayChangeRank": 25,
        "RankEvidenceBucket": "rapid|r011_025|m1_2",
        "t1_5_s1_0_return_pct": 1.4,
        "t1_5_s1_0_exit_time": "2026-07-22 10:20:00",
    }
    rows = [
        {**base, "Symbol": "GOOD"},
        {**base, "Symbol": "WIDE", "SpreadBps": 30.0},
        {**base, "Symbol": "CHEAP", "EntryPrice": 20.0},
        {**base, "Symbol": "THIN", "TradedValue": 1_000_000},
        {**base, "Symbol": "LATE", "DayChangePct": 8.0},
    ]
    pd.DataFrame(rows).to_csv(path, index=False)

    evidence = load_rank_outcomes([str(path)], {"2026-07-22"})

    assert evidence["symbol"].tolist() == ["GOOD"]
    assert evidence.iloc[0]["rank_band_011_025"] == 1.0
    assert evidence.iloc[0]["move_band_1_2"] == 1.0


def test_outcome_coverage_rejects_missing_candidates_but_keeps_zero_trade_day(tmp_path):
    path = tmp_path / "live_features_rows.csv"
    rows = [
        {
            "Date": "2026-07-22",
            "RankEvidenceBucket": "rapid|r011_025|m1_2",
            "OutcomeAvailable": False,
            "EntryPrice": 100.0,
            "BestBid": 99.95,
            "BestAsk": 100.05,
            "SpreadBps": 10.0,
            "SpreadOk": True,
            "TradedValue": 50_000_000,
            "DayChangePct": 1.5,
        },
        {
            "Date": "2026-07-23",
            "RankEvidenceBucket": "pool|r001_010|m1_2",
            "OutcomeAvailable": False,
            "EntryPrice": 20.0,
            "BestBid": 19.95,
            "BestAsk": 20.05,
            "SpreadBps": 50.0,
            "SpreadOk": False,
            "TradedValue": 1_000_000,
            "DayChangePct": 1.5,
        },
    ]
    pd.DataFrame(rows).to_csv(path, index=False)

    coverage = rank_outcome_coverage(
        [str(path)], {"2026-07-22", "2026-07-23"}
    ).set_index("date")

    assert coverage.loc["2026-07-22", "coverage_pct"] == 0.0
    assert not bool(coverage.loc["2026-07-22", "outcome_ready"])
    assert coverage.loc["2026-07-23", "candidate_rows"] == 0
    assert coverage.loc["2026-07-23", "coverage_pct"] == 100.0
    assert bool(coverage.loc["2026-07-23", "outcome_ready"])


def test_empty_replay_keeps_all_calendar_sessions():
    daily, trades = replay(
        pd.DataFrame(),
        ["2026-07-22", "2026-07-23"],
        capital=500_000,
        max_trades_per_day=10,
        extra_cost_pct=0.1,
    )
    assert daily["date"].tolist() == ["2026-07-22", "2026-07-23"]
    assert daily["trades"].sum() == 0
    assert trades.empty


def test_audit_waits_for_twelve_complete_sessions_and_never_allows_orders(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    _raw_rows("2026-07-22").to_csv(
        raw / "live_features_2026-07-22.csv", index=False
    )
    status = run_audit(
        outcome_patterns=[str(tmp_path / "missing" / "*.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=tmp_path / "output",
        capital=500_000,
        daily_goal=10_000,
        stress_cost_pct=0.1,
        max_trades_per_day=10,
        selection_sessions_required=12,
        min_snapshots=300,
    )

    assert status["status"] == "collecting_full_session_selection_evidence"
    assert status["raw_full_sessions"] == 1
    assert status["eligible_full_sessions"] == 0
    assert status["selection_sessions_remaining"] == 12
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]
    assert (Path(tmp_path) / "output" / "status.json").exists()


def _outcome_rows(date: str, count: int = 70):
    start = pd.Timestamp(f"{date} 10:00:00")
    rows = []
    for index in range(count):
        when = start + pd.Timedelta(minutes=index)
        row = {feature: 0.0 for feature in FEATURES}
        row.update(
            {
                "Date": date,
                "When": when,
                "Symbol": f"S{index:03d}",
                "OutcomeAvailable": True,
                "EntryPrice": 100.0,
                "LastPrice": 100.0,
                "BestBid": 99.95,
                "BestAsk": 100.05,
                "SpreadBps": 10.0,
                "SpreadOk": True,
                "TradedValue": 50_000_000 + index,
                "DayChangePct": 1.5 + index / 10_000,
                "DayChangeRank": index + 1,
                "RankEvidenceBucket": "pool|r026_050|m1_2",
                "t1_5_s1_0_return_pct": 1.4,
                "t1_5_s1_0_exit_time": when + pd.Timedelta(minutes=1),
            }
        )
        rows.append(row)
    return rows


def test_audit_freezes_selection_once_then_scores_only_later_sessions(tmp_path):
    raw = tmp_path / "raw"
    eod = tmp_path / "eod"
    raw.mkdir()
    eod.mkdir()
    all_outcomes = []
    selection_dates = pd.bdate_range("2026-06-01", periods=12)
    for timestamp in selection_dates:
        date = timestamp.strftime("%Y-%m-%d")
        _raw_rows(date).to_csv(
            raw / f"live_features_{date}.csv", index=False
        )
        all_outcomes.extend(_outcome_rows(date))
    pd.DataFrame(all_outcomes).to_csv(eod / "live_features_rows.csv", index=False)
    output = tmp_path / "output"

    selected = run_audit(
        outcome_patterns=[str(eod / "*.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
        capital=500_000,
        daily_goal=10_000,
        stress_cost_pct=0.1,
        max_trades_per_day=10,
        selection_sessions_required=12,
        min_snapshots=300,
    )

    assert selected["status"] == "collecting_untouched_forward_evidence"
    assert selected["selection"]["selection_status"] == "passed"
    assert selected["selection"]["selection_end"] == selection_dates[-1].strftime("%Y-%m-%d")
    assert selected["forward"]["sessions"] == 0
    assert (output / "frozen_selection.json").exists()
    assert (output / "frozen_model.joblib").exists()
    frozen_before = (output / "frozen_selection.json").read_text(encoding="utf-8")

    forward_date = pd.bdate_range(selection_dates[-1] + pd.Timedelta(days=1), periods=1)[0].strftime("%Y-%m-%d")
    _raw_rows(forward_date).to_csv(
        raw / f"live_features_{forward_date}.csv", index=False
    )
    pd.DataFrame(all_outcomes + _outcome_rows(forward_date)).to_csv(
        eod / "live_features_rows.csv", index=False
    )
    forward = run_audit(
        outcome_patterns=[str(eod / "*.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
        capital=500_000,
        daily_goal=10_000,
        stress_cost_pct=0.1,
        max_trades_per_day=10,
        selection_sessions_required=12,
        min_snapshots=300,
    )

    assert forward["forward"]["sessions"] == 1
    assert forward["forward"]["trades"] == 10
    assert (output / "frozen_selection.json").read_text(encoding="utf-8") == frozen_before
    assert not forward["accepted"]
    assert not forward["real_orders_allowed"]

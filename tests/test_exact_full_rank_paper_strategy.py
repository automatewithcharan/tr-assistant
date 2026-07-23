import json
from pathlib import Path

import pandas as pd

from scripts.assess_exact_full_rank_paper import (
    exact_session_coverage,
    load_exact_events,
    replay_one_slot,
    run_audit,
)


def _entry(date, symbol, minute, rank=1, velocity=10.0):
    when = pd.Timestamp(f"{date} 09:20:00") + pd.Timedelta(minutes=minute)
    return {
        "EventId": f"{date}:{symbol}:cohort",
        "Date": date,
        "EntryAt": when,
        "RecorderGeneration": 5,
        "Symbol": symbol,
        "Cohort": "rapid_rank_climber:r1_10:m1_2",
        "Source": "rapid_rank_climber",
        "EntryAsk": 100.0,
        "EntryBid": 99.95,
        "EntrySpreadBps": 5.0,
        "Quantity": 5000,
        "Capital": 500000,
        "DayChangePct": 1.2,
        "UniverseSize": 3000,
        "DayChangeRank": rank,
        "DayChangeRankPct": 99.0,
        "PreviousDayChangeRank": 100,
        "RankImprovement": 100 - rank,
        "RankVelocityPerMinute": velocity,
        "DayChangeDeltaPct": 0.3,
        "TradedValue": 50000000,
        "BuyerSellerRatio": 2.0,
    }


def _outcome(entry, minutes=5, net_return=1.4):
    return {
        "EventId": entry["EventId"],
        "ExitAt": pd.Timestamp(entry["EntryAt"]) + pd.Timedelta(minutes=minutes),
        "ExitReason": "target" if net_return > 0 else "stop",
        "ExitBid": 101.5 if net_return > 0 else 99.0,
        "HoldMinutes": minutes,
        "GrossReturnPct": net_return + 0.1,
        "NetReturnPct": net_return,
        "GrossPnl": 7500 if net_return > 0 else -5000,
        "StressCost": 500,
        "NetPnl": 7000 if net_return > 0 else -5500,
    }


def _raw(date):
    start = pd.Timestamp(f"{date} 09:16:00")
    return pd.DataFrame(
        {
            "When": [start + pd.Timedelta(minutes=i) for i in range(360)],
            "DayChangeRank": [1] * 360,
            "RecorderGeneration": [5] * 360,
        }
    )


def test_load_and_coverage_require_an_exact_exit_for_every_entry(tmp_path):
    date = "2026-07-22"
    first = _entry(date, "AAA", 0)
    second = _entry(date, "BBB", 1)
    pd.DataFrame([first, second]).to_csv(tmp_path / "entries.csv", index=False)
    pd.DataFrame([_outcome(first)]).to_csv(tmp_path / "outcomes.csv", index=False)

    entries, events = load_exact_events(
        [str(tmp_path / "entries.csv")], [str(tmp_path / "outcomes.csv")]
    )
    raw = pd.DataFrame([{"date": date, "full_session_eligible": True}])
    coverage = exact_session_coverage(entries, events, raw).iloc[0]

    assert len(entries) == 2
    assert len(events) == 1
    assert coverage["outcome_coverage_pct"] == 50.0
    assert not bool(coverage["eligible"])


def test_exact_loader_rejects_entries_from_older_recorder_generation(tmp_path):
    date = "2026-07-22"
    current = _entry(date, "CURRENT", 0)
    legacy = {**_entry(date, "LEGACY", 1), "RecorderGeneration": 4}
    pd.DataFrame([current, legacy]).to_csv(tmp_path / "entries.csv", index=False)
    pd.DataFrame([_outcome(current), _outcome(legacy)]).to_csv(
        tmp_path / "outcomes.csv", index=False
    )

    entries, events = load_exact_events(
        [str(tmp_path / "entries.csv")],
        [str(tmp_path / "outcomes.csv")],
    )

    assert entries["Symbol"].tolist() == ["CURRENT"]
    assert events["Symbol"].tolist() == ["CURRENT"]
    assert entries["RecorderGeneration"].tolist() == [5]


def test_one_slot_replay_uses_causal_selector_and_skips_overlaps(tmp_path):
    date = "2026-07-22"
    rank_one = _entry(date, "RANK1", 0, rank=1, velocity=2)
    fast = _entry(date, "FAST", 0, rank=2, velocity=100)
    overlap = _entry(date, "OVERLAP", 5, rank=1, velocity=200)
    later = _entry(date, "LATER", 21, rank=3, velocity=5)
    entries = [rank_one, fast, overlap, later]
    outcomes = [
        _outcome(rank_one, minutes=20, net_return=-1.1),
        _outcome(fast, minutes=3, net_return=1.4),
        _outcome(overlap, minutes=3, net_return=1.4),
        _outcome(later, minutes=3, net_return=1.4),
    ]
    pd.DataFrame(entries).to_csv(tmp_path / "entries.csv", index=False)
    pd.DataFrame(outcomes).to_csv(tmp_path / "outcomes.csv", index=False)
    _, events = load_exact_events(
        [str(tmp_path / "entries.csv")], [str(tmp_path / "outcomes.csv")]
    )

    daily, trades = replay_one_slot(events, [date], selector="rank_first")
    _, acceleration = replay_one_slot(events, [date], selector="acceleration_first")

    assert trades["symbol"].tolist() == ["RANK1", "LATER"]
    assert daily.iloc[0]["trades"] == 2
    assert daily.iloc[0]["pnl"] == 1500.0
    assert acceleration.iloc[0]["symbol"] == "FAST"


def test_audit_waits_for_twelve_complete_exact_sessions(tmp_path):
    date = "2026-07-22"
    raw = tmp_path / "raw"
    raw.mkdir()
    _raw(date).to_csv(raw / f"live_features_{date}.csv", index=False)
    output = tmp_path / "output"

    status = run_audit(
        entry_patterns=[str(tmp_path / "missing_entries*.csv")],
        outcome_patterns=[str(tmp_path / "missing_outcomes*.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
    )

    assert status["status"] == "collecting_exact_selection_sessions"
    assert status["eligible_exact_sessions"] == 1
    assert status["selection_dates"] == [date]
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]


def test_selection_freezes_once_and_cannot_change_with_forward_data(tmp_path):
    raw = tmp_path / "raw"
    paper = tmp_path / "paper"
    output = tmp_path / "output"
    raw.mkdir()
    paper.mkdir()
    entries = []
    outcomes = []
    dates = pd.bdate_range("2026-06-01", periods=12)
    for stamp in dates:
        date = stamp.strftime("%Y-%m-%d")
        _raw(date).to_csv(raw / f"live_features_{date}.csv", index=False)
        for index in range(3):
            entry = _entry(date, f"S{index}", index * 10, rank=index + 1)
            entries.append(entry)
            outcomes.append(_outcome(entry, minutes=3, net_return=1.4))
    pd.DataFrame(entries).to_csv(paper / "entries.csv", index=False)
    pd.DataFrame(outcomes).to_csv(paper / "outcomes.csv", index=False)

    selected = run_audit(
        entry_patterns=[str(paper / "entries.csv")],
        outcome_patterns=[str(paper / "outcomes.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
    )

    assert selected["status"] == "collecting_exact_forward_sessions"
    assert selected["frozen_selection"]["selection_status"] == "passed"
    assert selected["frozen_selection"]["selector"]
    frozen_before = (output / "frozen_selection.json").read_text(encoding="utf-8")

    forward_date = "2026-06-17"
    _raw(forward_date).to_csv(raw / f"live_features_{forward_date}.csv", index=False)
    entry = _entry(forward_date, "LOSS", 0)
    pd.DataFrame(entries + [entry]).to_csv(paper / "entries.csv", index=False)
    pd.DataFrame(outcomes + [_outcome(entry, net_return=-1.1)]).to_csv(
        paper / "outcomes.csv", index=False
    )
    forward = run_audit(
        entry_patterns=[str(paper / "entries.csv")],
        outcome_patterns=[str(paper / "outcomes.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
    )

    assert forward["forward"]["sessions"] == 1
    assert (output / "frozen_selection.json").read_text(encoding="utf-8") == frozen_before
    assert not forward["real_orders_allowed"]

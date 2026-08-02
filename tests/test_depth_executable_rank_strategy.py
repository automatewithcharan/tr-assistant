import json

import pandas as pd

from pkscreener.classes.FullUniverseRankContractMatrixObserver import CONTRACTS
from pkscreener.classes.OrderBookExecution import depth_json
from scripts.assess_depth_executable_rank_strategy import (
    depth_session_coverage,
    load_depth_events,
    replay_depth_slots,
)


def _depth(price, quantities, step):
    return depth_json(
        [
            {"price": price + step * index, "quantity": quantity, "orders": 1}
            for index, quantity in enumerate(quantities)
        ]
    )


def _entry(date="2026-07-29", symbol="AAA"):
    return {
        "EventId": f"{date}:{symbol}:g6:cohort",
        "Date": date,
        "EntryAt": f"{date} 09:20:00",
        "RecorderGeneration": 6,
        "Exchange": "NSE",
        "Symbol": symbol,
        "Cohort": "leader_pool:r1_25:m1_2",
        "Source": "leader_pool",
        "EntryAsk": 100.0,
        "EntrySpreadBps": 5.0,
        "ExecutionSchemaVersion": 2,
        "EntryDepthJson": _depth(100.0, [500, 500, 500], 0.05),
        "DayChangePct": 1.2,
        "UniverseSize": 3000,
        "DayChangeRank": 10,
        "PreviousDayChangeRank": 100,
        "RankImprovement": 90,
        "RankVelocityPerMinute": 90.0,
        "DayChangeDeltaPct": 0.3,
        "TradedValue": 100_000_000,
        "BuyerSellerRatio": 2.0,
        "StressPct": 0.1,
    }


def _outcome(entry, contract, *, exit_quantity=2000):
    return {
        "MatrixId": f"{entry['EventId']}|{contract}",
        "EventId": entry["EventId"],
        "Contract": contract,
        "ExitAt": f"{entry['Date']} 09:25:00",
        "ExitReason": "target",
        "ExecutionSchemaVersion": 2,
        "ExitDepthJson": _depth(101.0, [exit_quantity], -0.05),
    }


def test_loader_rejects_old_top_level_only_evidence(tmp_path):
    old = _entry()
    old.pop("ExecutionSchemaVersion")
    old.pop("EntryDepthJson")
    pd.DataFrame([old]).to_csv(tmp_path / "entries.csv", index=False)
    pd.DataFrame([_outcome(_entry(), next(iter(CONTRACTS)))]).to_csv(
        tmp_path / "outcomes.csv", index=False
    )

    entries, events = load_depth_events(
        [str(tmp_path / "entries.csv")],
        [str(tmp_path / "outcomes.csv")],
    )

    assert entries.empty
    assert events.empty


def test_replay_uses_depth_vwap_and_marks_thin_exit_incomplete(tmp_path):
    entry = _entry()
    contract = next(iter(CONTRACTS))
    outcomes = [
        _outcome(entry, contract, exit_quantity=100),
    ]
    pd.DataFrame([entry]).to_csv(tmp_path / "entries.csv", index=False)
    pd.DataFrame(outcomes).to_csv(tmp_path / "outcomes.csv", index=False)
    _, events = load_depth_events(
        [str(tmp_path / "entries.csv")],
        [str(tmp_path / "outcomes.csv")],
    )

    daily, trades = replay_depth_slots(
        events, [entry["Date"]], selector="rank_first", slots=5
    )

    assert int(daily.iloc[0]["trades"]) == 1
    assert not bool(daily.iloc[0]["execution_complete"])
    assert int(daily.iloc[0]["exit_depth_failures"]) == 1
    assert not bool(trades.iloc[0]["execution_complete"])


def test_complete_depth_replay_calculates_cost_adjusted_pnl(tmp_path):
    entry = _entry()
    contract = next(iter(CONTRACTS))
    pd.DataFrame([entry]).to_csv(tmp_path / "entries.csv", index=False)
    pd.DataFrame([_outcome(entry, contract)]).to_csv(
        tmp_path / "outcomes.csv", index=False
    )
    _, events = load_depth_events(
        [str(tmp_path / "entries.csv")],
        [str(tmp_path / "outcomes.csv")],
    )

    daily, trades = replay_depth_slots(
        events, [entry["Date"]], selector="rank_first", slots=5
    )

    assert bool(daily.iloc[0]["execution_complete"])
    assert int(trades.iloc[0]["quantity"]) >= 990
    assert float(trades.iloc[0]["entry_vwap"]) > 100.0
    assert float(trades.iloc[0]["pnl"]) > 0.0


def test_loader_normalizes_mixed_naive_and_aware_ist_timestamps(tmp_path):
    entry = {
        **_entry(),
        "EntryAt": "2026-07-29 09:20:00",
    }
    contract = next(iter(CONTRACTS))
    outcome = {
        **_outcome(entry, contract),
        "ExitAt": "2026-07-29T09:25:00+05:30",
    }
    pd.DataFrame([entry]).to_csv(tmp_path / "entries.csv", index=False)
    pd.DataFrame([outcome]).to_csv(tmp_path / "outcomes.csv", index=False)

    entries, events = load_depth_events(
        [str(tmp_path / "entries.csv")],
        [str(tmp_path / "outcomes.csv")],
    )

    assert len(events) == 1
    assert entries["entry_time"].dt.tz is None
    assert events["exit_time"].dt.tz is None
    assert events.iloc[0]["exit_time"] - events.iloc[0]["entry_time"] == pd.Timedelta(minutes=5)


def test_coverage_requires_all_contract_outcomes_for_schema_v2_entry():
    entry = _entry()
    entries = pd.DataFrame([{**entry, "date": entry["Date"]}])
    one = next(iter(CONTRACTS))
    events = pd.DataFrame(
        [{**entry, **_outcome(entry, one), "date": entry["Date"]}]
    )
    raw = pd.DataFrame(
        [{"date": entry["Date"], "full_session_eligible": True}]
    )

    row = depth_session_coverage(entries, events, raw).iloc[0]

    assert int(row["complete_contracts"]) == 1
    assert not bool(row["eligible"])

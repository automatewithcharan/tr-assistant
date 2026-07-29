import pandas as pd

from pkscreener.classes.FullUniverseRankContractMatrixObserver import CONTRACTS
from scripts.assess_full_rank_contract_matrix import (
    load_matrix_events,
    matrix_session_coverage,
    run_audit,
)


def _entry(date, symbol, minute):
    when = pd.Timestamp(f"{date} 09:20:00") + pd.Timedelta(minutes=minute)
    return {
        "EventId": f"{date}:{symbol}:cohort",
        "Date": date,
        "EntryAt": when,
        "RecorderGeneration": 6,
        "Exchange": "NSE",
        "Symbol": symbol,
        "Cohort": "rapid:r1_10:m1_2",
        "Source": "rapid_rank_climber",
        "EntryAsk": 100.0,
        "EntryBid": 99.95,
        "EntrySpreadBps": 5.0,
        "Quantity": 5000,
        "Capital": 500000,
        "DayChangePct": 1.2,
        "UniverseSize": 3000,
        "DayChangeRank": minute + 1,
        "RankImprovement": 100,
        "RankVelocityPerMinute": 100.0,
        "DayChangeDeltaPct": 0.3,
        "TradedValue": 50000000,
        "BuyerSellerRatio": 2.0,
    }


def _matrix_outcomes(entry, contracts=None, net_return=1.4):
    rows = []
    for contract in contracts or CONTRACTS:
        rows.append(
            {
                "MatrixId": f"{entry['EventId']}|{contract}",
                "EventId": entry["EventId"],
                "Contract": contract,
                "ExitAt": pd.Timestamp(entry["EntryAt"]) + pd.Timedelta(minutes=3),
                "ExitReason": "target" if net_return > 0 else "stop",
                "ExitBid": 101.5 if net_return > 0 else 99.0,
                "HoldMinutes": 3,
                "GrossReturnPct": net_return + 0.1,
                "NetReturnPct": net_return,
                "GrossPnl": 7500,
                "StressCost": 500,
                "NetPnl": net_return * 5000,
            }
        )
    return rows


def _raw(date):
    start = pd.Timestamp(f"{date} 09:16:00")
    return pd.DataFrame(
        {
            "When": [start + pd.Timedelta(seconds=15 * i) for i in range(1440)],
            "DayChangeRank": [1] * 1440,
            "RecorderGeneration": [6] * 1440,
        }
    )


def test_matrix_coverage_requires_every_predeclared_contract(tmp_path):
    date = "2026-07-22"
    entry = _entry(date, "AAA", 0)
    pd.DataFrame([entry]).to_csv(tmp_path / "entries.csv", index=False)
    only_one = [next(iter(CONTRACTS))]
    pd.DataFrame(_matrix_outcomes(entry, contracts=only_one)).to_csv(
        tmp_path / "outcomes.csv", index=False
    )
    entries, events = load_matrix_events(
        [str(tmp_path / "entries.csv")], [str(tmp_path / "outcomes.csv")]
    )
    raw = pd.DataFrame([{"date": date, "full_session_eligible": True}])

    coverage = matrix_session_coverage(entries, events, raw).iloc[0]

    assert coverage["complete_contracts"] == 1
    assert coverage["required_contracts"] == len(CONTRACTS)
    assert not bool(coverage["eligible"])


def test_unfillable_entry_does_not_invalidate_complete_executable_session(
    tmp_path,
):
    date = "2026-07-22"
    fillable = {
        **_entry(date, "FILLABLE", 0),
        "EntryTopLevelFillable": True,
    }
    unfillable = {
        **_entry(date, "UNFILLABLE", 1),
        "EntryTopLevelFillable": False,
    }
    pd.DataFrame([fillable, unfillable]).to_csv(
        tmp_path / "entries.csv", index=False
    )
    pd.DataFrame(_matrix_outcomes(fillable)).to_csv(
        tmp_path / "outcomes.csv", index=False
    )

    entries, events = load_matrix_events(
        [str(tmp_path / "entries.csv")],
        [str(tmp_path / "outcomes.csv")],
    )
    raw = pd.DataFrame(
        [{"date": date, "full_session_eligible": True}]
    )
    coverage = matrix_session_coverage(entries, events, raw).iloc[0]

    assert entries["Symbol"].tolist() == ["FILLABLE"]
    assert coverage["entries"] == 1
    assert coverage["complete_contracts"] == len(CONTRACTS)
    assert bool(coverage["eligible"])


def test_matrix_audit_freezes_combination_once_then_uses_forward_only(tmp_path):
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
            entry = _entry(date, f"S{index}", index * 10)
            entries.append(entry)
            outcomes.extend(_matrix_outcomes(entry))
    pd.DataFrame(entries).to_csv(paper / "entries.csv", index=False)
    pd.DataFrame(outcomes).to_csv(paper / "outcomes.csv", index=False)

    selected = run_audit(
        entry_patterns=[str(paper / "entries.csv")],
        outcome_patterns=[str(paper / "outcomes.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
    )

    assert selected["status"] == "collecting_contract_forward_sessions"
    assert selected["frozen_selection"]["contract"] in CONTRACTS
    assert selected["frozen_selection"]["selector"]
    frozen_before = (output / "frozen_selection.json").read_text(encoding="utf-8")

    forward_date = "2026-06-17"
    _raw(forward_date).to_csv(raw / f"live_features_{forward_date}.csv", index=False)
    loss = _entry(forward_date, "LOSS", 0)
    pd.DataFrame(entries + [loss]).to_csv(paper / "entries.csv", index=False)
    pd.DataFrame(outcomes + _matrix_outcomes(loss, net_return=-1.1)).to_csv(
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

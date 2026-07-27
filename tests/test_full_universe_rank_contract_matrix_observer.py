from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from pkscreener.classes.FullUniverseRankContractMatrixObserver import (
    CONTRACTS,
    FullUniverseRankContractMatrixObserver,
)


def _tick(bid, ask=None):
    ask = ask if ask is not None else bid + 0.1
    return {
        "depth": {
            "buy": [{"price": bid, "quantity": 1000}],
            "sell": [{"price": ask, "quantity": 1000}],
        }
    }


def _entry(now, symbol="AAA"):
    return {
        "event": "entry",
        "EventId": f"{now:%Y-%m-%d}:{symbol}:g5:rapid:r1_10:m1_2",
        "Date": now.strftime("%Y-%m-%d"),
        "EntryAt": now.strftime("%Y-%m-%d %H:%M:%S"),
        "EntryTs": now.timestamp(),
        "RecorderGeneration": 5,
        "Symbol": symbol,
        "Cohort": "rapid:r1_10:m1_2",
        "Source": "rapid_rank_climber",
        "EntryAsk": 100.1,
        "EntryBid": 100.0,
        "EntrySpreadBps": 9.995,
        "Quantity": 4995,
        "Capital": 499999.5,
        "DayChangePct": 1.2,
        "DayChangeRank": 5,
        "RankImprovement": 80,
        "RankVelocityPerMinute": 80.0,
        "BuyerSellerRatio": 2.0,
    }


def test_default_matrix_registers_every_frozen_contract(tmp_path):
    observer = FullUniverseRankContractMatrixObserver(str(tmp_path))
    now = datetime(2026, 7, 22, 9, 20)

    outcomes = observer.process([_entry(now)], {"AAA": _tick(100.0)}, now=now)

    assert outcomes == []
    assert len(observer.active) == len(CONTRACTS)
    status = pd.read_json(observer.status_path, typ="series")
    assert status["contracts"] == len(CONTRACTS)
    assert status["entry_labels"] == 1
    assert status["real_orders_allowed"] is False
    registrations = pd.read_csv(
        observer.root / "registrations_2026-07-22.csv"
    )
    assert registrations["RecorderGeneration"].tolist() == [5]


def test_contracts_close_independently_at_target_and_timeout(tmp_path):
    contracts = {
        "fast": {"target_pct": 0.5, "stop_pct": 0.5, "horizon_minutes": 15},
        "runner": {"target_pct": 1.5, "stop_pct": 1.0, "horizon_minutes": 60},
    }
    observer = FullUniverseRankContractMatrixObserver(
        str(tmp_path), contracts=contracts
    )
    now = datetime(2026, 7, 22, 9, 20)
    observer.process([_entry(now)], {"AAA": _tick(100.0)}, now=now)

    first = observer.process([], {"AAA": _tick(100.7)}, now=now + timedelta(minutes=5))
    second = observer.process([], {"AAA": _tick(100.4)}, now=now + timedelta(minutes=61))

    assert [(row["Contract"], row["ExitReason"]) for row in first] == [("fast", "target")]
    assert [(row["Contract"], row["ExitReason"]) for row in second] == [("runner", "timeout")]
    saved = pd.read_csv(observer.root / "outcomes_2026-07-22.csv")
    assert saved["Contract"].tolist() == ["fast", "runner"]
    assert saved["ExitBid"].tolist() == [100.7, 100.4]
    assert saved["RecorderGeneration"].tolist() == [5, 5]


def test_legacy_active_contract_without_generation_still_closes(tmp_path):
    contracts = {
        "fast": {
            "target_pct": 0.5,
            "stop_pct": 0.5,
            "horizon_minutes": 15,
        }
    }
    observer = FullUniverseRankContractMatrixObserver(
        str(tmp_path), contracts=contracts
    )
    now = datetime(2026, 7, 22, 9, 20)
    observer.process([_entry(now)], {"AAA": _tick(100.0)}, now=now)
    next(iter(observer.active.values())).pop("RecorderGeneration")

    outcomes = observer.process(
        [], {"AAA": _tick(100.7)}, now=now + timedelta(minutes=5)
    )

    assert outcomes[0]["RecorderGeneration"] == 0
    assert outcomes[0]["ExitReason"] == "target"


def test_registration_ledger_widens_old_same_day_schema(tmp_path):
    observer = FullUniverseRankContractMatrixObserver(str(tmp_path))
    now = datetime(2026, 7, 22, 9, 20)
    path = observer.root / "registrations_2026-07-22.csv"
    path.parent.mkdir(parents=True)
    old_columns = [
        column
        for column in observer.REGISTRATION_COLUMNS
        if column != "RecorderGeneration"
    ]
    old = _entry(now, symbol="OLD")
    pd.DataFrame(
        [{column: old.get(column, "") for column in old_columns}],
        columns=old_columns,
    ).to_csv(path, index=False)

    observer.process(
        [_entry(now, symbol="NEW")],
        {"NEW": _tick(100.0)},
        now=now,
    )

    saved = pd.read_csv(path)
    assert saved.columns.tolist() == observer.REGISTRATION_COLUMNS
    assert saved["Symbol"].tolist() == ["OLD", "NEW"]
    assert pd.isna(saved.iloc[0]["RecorderGeneration"])
    assert int(saved.iloc[1]["RecorderGeneration"]) == 5


def test_restart_preserves_contracts_and_does_not_duplicate_entry(tmp_path):
    now = datetime(2026, 7, 22, 9, 20)
    first = FullUniverseRankContractMatrixObserver(str(tmp_path))
    first.process([_entry(now)], {"AAA": _tick(100.0)}, now=now)

    restarted = FullUniverseRankContractMatrixObserver(str(tmp_path))
    restarted.process([_entry(now)], {"AAA": _tick(100.0)}, now=now + timedelta(minutes=1))

    assert len(restarted.active) == len(CONTRACTS)
    registrations = pd.read_csv(
        restarted.root / "registrations_2026-07-22.csv"
    )
    assert len(registrations) == 1


def test_custom_namespace_preserves_prediction_metadata(tmp_path):
    observer = FullUniverseRankContractMatrixObserver(
        str(tmp_path),
        contracts={
            "fast": {
                "target_pct": 0.5,
                "stop_pct": 0.5,
                "horizon_minutes": 15,
            }
        },
        root_name="frozen_prediction_matrix",
        metadata_columns=[
            "ExpectedReturnPct",
            "ModelSourceSha256",
        ],
    )
    now = datetime(2026, 7, 22, 9, 20)
    entry = _entry(now)
    entry["ExpectedReturnPct"] = 0.31
    entry["ModelSourceSha256"] = "abc123"

    observer.process([entry], {"AAA": _tick(100.0)}, now=now)
    observer.process(
        [], {"AAA": _tick(100.7)}, now=now + timedelta(minutes=5)
    )

    assert observer.root.name == "frozen_prediction_matrix"
    registration = pd.read_csv(
        observer.root / "registrations_2026-07-22.csv"
    )
    outcome = pd.read_csv(observer.root / "outcomes_2026-07-22.csv")
    assert registration.iloc[0]["ExpectedReturnPct"] == 0.31
    assert outcome.iloc[0]["ModelSourceSha256"] == "abc123"


def test_eod_closes_all_contracts_and_component_has_no_order_path(tmp_path):
    now = datetime(2026, 7, 22, 15, 20)
    observer = FullUniverseRankContractMatrixObserver(str(tmp_path))
    observer.process([_entry(now)], {"AAA": _tick(100.0)}, now=now)
    outcomes = observer.process(
        [], {"AAA": _tick(100.2)}, now=datetime(2026, 7, 22, 15, 25)
    )

    assert len(outcomes) == len(CONTRACTS)
    assert {row["ExitReason"] for row in outcomes} == {"eod"}
    source = Path(
        "pkscreener/classes/FullUniverseRankContractMatrixObserver.py"
    ).read_text(encoding="utf-8")
    assert "KiteOrderManager" not in source
    assert "send_discord" not in source.lower()

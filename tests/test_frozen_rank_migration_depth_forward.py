import json
from datetime import datetime, timedelta

import joblib
import pandas as pd

from pkscreener.classes.OrderBookExecution import depth_json
from scripts.assess_frozen_rank_migration_depth_forward import (
    CONTRACT,
    FORWARD_START,
    LANE_SOURCE,
    forward_coverage,
    replay_forward,
    run_audit,
)
from scripts.assess_depth_executable_rank_strategy import load_depth_events


MODEL_HASH = "frozen-rank-migration-test-hash"


def _depth(price, quantity, step=0.05):
    return depth_json(
        [
            {
                "price": price + index * step,
                "quantity": quantity,
                "orders": 1,
            }
            for index in range(5)
        ]
    )


def _registration(date, symbol="AAA", score=0.30, schema=2):
    event_id = f"{date}:{symbol}:0.5-1:{MODEL_HASH[:16]}"
    return {
        "EventId": event_id,
        "Date": date,
        "EntryAt": f"{date} 09:20:00",
        "RecorderGeneration": 6,
        "Exchange": "NSE",
        "Symbol": symbol,
        "Source": LANE_SOURCE,
        "EntryAsk": 100.0,
        "EntrySpreadBps": 5.0,
        "ExecutionSchemaVersion": schema,
        "EntryDepthJson": _depth(100.0, 2000),
        "ModelTrainedThrough": "2026-07-21",
        "ModelSourceSha256": MODEL_HASH,
        "ExpectedReturnPct": score,
        "StressPct": 0.10,
    }


def _outcome(entry, exit_quantity=2000, exit_price=101.5):
    return {
        "MatrixId": f"{entry['EventId']}|{CONTRACT}",
        "EventId": entry["EventId"],
        "Contract": CONTRACT,
        "ExitAt": f"{entry['Date']} 09:40:00",
        "ExitReason": "target",
        "ExecutionSchemaVersion": entry["ExecutionSchemaVersion"],
        "ExitDepthJson": _depth(exit_price, exit_quantity, step=-0.05),
        "StressPct": 0.10,
    }


def _raw_rows(date):
    start = datetime.fromisoformat(f"{date} 09:15:00")
    return [
        {
            "When": (start + timedelta(seconds=20 * index)).isoformat(),
            "DayChangeRank": 1,
            "RecorderGeneration": 6,
        }
        for index in range(1066)
    ]


def _write_model(path):
    joblib.dump(
        {
            "model": None,
            "metadata": {
                "source_sha256": MODEL_HASH,
                "trained_through": "2026-07-21",
                "valid_from": "2026-07-22",
                "paper_only": True,
                "real_orders_allowed": False,
            },
        },
        path,
    )


def _write_status(path, date):
    path.write_text(
        json.dumps(
            {
                "status": "recording_exact_exit_contract_matrix",
                "date": date,
                "updated_at": f"{date} 15:25:00",
                "execution_schema_version": 2,
                "contracts": 9,
                "paper_only": True,
                "alerts_allowed": False,
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )


def test_forward_audit_excludes_every_row_before_frozen_boundary(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    old = _registration("2026-07-29", "OLD")
    new = _registration(FORWARD_START, "NEW")
    pd.DataFrame([old, new]).to_csv(root / "registrations.csv", index=False)
    pd.DataFrame([_outcome(old), _outcome(new)]).to_csv(
        root / "outcomes.csv", index=False
    )
    pd.DataFrame(
        _raw_rows("2026-07-29") + _raw_rows(FORWARD_START)
    ).to_csv(root / "raw.csv", index=False)
    _write_status(root / "status.json", FORWARD_START)
    _write_model(root / "model.joblib")

    status = run_audit(
        registration_patterns=[str(root / "registrations.csv")],
        outcome_patterns=[str(root / "outcomes.csv")],
        raw_patterns=[str(root / "raw.csv")],
        lane_status_patterns=[str(root / "status.json")],
        model_path=root / "model.joblib",
        output_dir=root / "audit",
    )

    assert status["forward_dates"] == [FORWARD_START]
    assert status["forward"]["trades"] == 1
    assert status["accepted"] is False
    assert status["real_orders_allowed"] is False


def test_schema_v1_registration_makes_session_ineligible(tmp_path):
    old_schema = _registration(FORWARD_START, schema=1)
    registrations = tmp_path / "registrations.csv"
    outcomes = tmp_path / "outcomes.csv"
    pd.DataFrame([old_schema]).to_csv(registrations, index=False)
    pd.DataFrame([_outcome(old_schema)]).to_csv(outcomes, index=False)
    entries, events = load_depth_events(
        [str(registrations)], [str(outcomes)]
    )
    ledger = pd.DataFrame([old_schema])
    raw = pd.DataFrame(
        [{"date": FORWARD_START, "full_session_eligible": True}]
    )
    lane = pd.DataFrame(
        [{"date": FORWARD_START, "lane_heartbeat_complete": True}]
    )

    coverage = forward_coverage(raw, lane, ledger, entries, events).iloc[0]

    assert int(coverage["invalid_entry_evidence"]) == 1
    assert not bool(coverage["eligible"])


def test_wrong_model_registration_cannot_masquerade_as_zero_trade_day():
    wrong = _registration(FORWARD_START)
    wrong["ModelSourceSha256"] = "unexpected-model"
    raw = pd.DataFrame(
        [{"date": FORWARD_START, "full_session_eligible": True}]
    )
    lane = pd.DataFrame(
        [{"date": FORWARD_START, "lane_heartbeat_complete": True}]
    )

    coverage = forward_coverage(
        raw,
        lane,
        pd.DataFrame([wrong]),
        pd.DataFrame(),
        pd.DataFrame(),
    ).iloc[0]

    assert int(coverage["invalid_entry_evidence"]) == 1
    assert not bool(coverage["eligible"])


def test_same_minute_uses_highest_executable_expected_return(tmp_path):
    high = _registration(FORWARD_START, "HIGH", score=0.45)
    low = _registration(FORWARD_START, "LOW", score=0.25)
    registrations = tmp_path / "registrations.csv"
    outcomes = tmp_path / "outcomes.csv"
    pd.DataFrame([low, high]).to_csv(registrations, index=False)
    pd.DataFrame([_outcome(low), _outcome(high)]).to_csv(
        outcomes, index=False
    )
    _, events = load_depth_events(
        [str(registrations)], [str(outcomes)]
    )

    daily, trades = replay_forward(
        events, [FORWARD_START], capital=500_000.0
    )

    assert trades["symbol"].tolist() == ["HIGH"]
    assert int(daily.iloc[0]["trades"]) == 1


def test_unfillable_exit_marks_whole_session_incomplete(tmp_path):
    entry = _registration(FORWARD_START)
    registrations = tmp_path / "registrations.csv"
    outcomes = tmp_path / "outcomes.csv"
    pd.DataFrame([entry]).to_csv(registrations, index=False)
    pd.DataFrame([_outcome(entry, exit_quantity=100)]).to_csv(
        outcomes, index=False
    )
    _, events = load_depth_events(
        [str(registrations)], [str(outcomes)]
    )

    daily, trades = replay_forward(
        events, [FORWARD_START], capital=500_000.0
    )

    assert not bool(trades.iloc[0]["execution_complete"])
    assert int(daily.iloc[0]["exit_depth_failures"]) == 1
    assert not bool(daily.iloc[0]["execution_complete"])
    assert pd.isna(daily.iloc[0]["pnl"])

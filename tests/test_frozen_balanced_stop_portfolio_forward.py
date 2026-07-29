import json
from datetime import datetime, timedelta

import joblib
import pandas as pd

from pkscreener.classes.OrderBookExecution import depth_json
from scripts.assess_frozen_balanced_stop_portfolio_forward import (
    CONTRACT,
    FORWARD_START,
    LANE_CAPITAL,
    RUNNER_LANE,
    STOP_LANE,
    _combine_daily,
    run_audit,
)


def _depth(price, quantity, step):
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


def _registration(date, symbol, source, model_hash, score):
    return {
        "EventId": f"{date}:{symbol}:{model_hash[:12]}",
        "Date": date,
        "EntryAt": f"{date} 09:20:00",
        "RecorderGeneration": 6,
        "Exchange": "NSE",
        "Symbol": symbol,
        "Source": source,
        "EntryAsk": 100.0,
        "EntrySpreadBps": 5.0,
        "ExecutionSchemaVersion": 2,
        "EntryDepthJson": _depth(100.0, 1_000, 0.05),
        "ModelTrainedThrough": "2026-07-29",
        "ModelSourceSha256": model_hash,
        "ExpectedReturnPct": score,
        "StressPct": 0.10,
    }


def _outcome(entry, exit_price=101.5, exit_quantity=1_000):
    return {
        "MatrixId": f"{entry['EventId']}|{CONTRACT}",
        "EventId": entry["EventId"],
        "Contract": CONTRACT,
        "ExitAt": f"{entry['Date']} 09:40:00",
        "ExitReason": "target",
        "ExecutionSchemaVersion": 2,
        "ExitDepthJson": _depth(exit_price, exit_quantity, -0.05),
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
        for index in range(1_066)
    ]


def _write_model(path, model_hash, artifact_type):
    joblib.dump(
        {
            "model": None,
            "metadata": {
                "artifact_type": artifact_type,
                "source_sha256": model_hash,
                "trained_through": "2026-07-29",
                "valid_from": FORWARD_START,
                "paper_only": True,
                "alerts_allowed": False,
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


def test_balanced_forward_uses_two_fixed_half_capital_pools(tmp_path):
    stop_hash = "stop-model-hash"
    runner_hash = "runner-model-hash"
    stop_entry = _registration(
        FORWARD_START,
        "STOP",
        STOP_LANE["source"],
        stop_hash,
        0.90,
    )
    runner_entry = _registration(
        FORWARD_START,
        "RUNNER",
        RUNNER_LANE["source"],
        runner_hash,
        0.70,
    )
    pd.DataFrame([stop_entry]).to_csv(
        tmp_path / "stop_registrations.csv", index=False
    )
    pd.DataFrame([_outcome(stop_entry)]).to_csv(
        tmp_path / "stop_outcomes.csv", index=False
    )
    pd.DataFrame([runner_entry]).to_csv(
        tmp_path / "runner_registrations.csv", index=False
    )
    pd.DataFrame([_outcome(runner_entry)]).to_csv(
        tmp_path / "runner_outcomes.csv", index=False
    )
    pd.DataFrame(_raw_rows(FORWARD_START)).to_csv(
        tmp_path / "raw.csv", index=False
    )
    _write_status(tmp_path / "stop_status.json", FORWARD_START)
    _write_status(tmp_path / "runner_status.json", FORWARD_START)
    _write_model(
        tmp_path / "stop.joblib",
        stop_hash,
        "prior_session_rank_migration_stop_avoidance",
    )
    _write_model(
        tmp_path / "runner.joblib",
        runner_hash,
        "prior_session_runner_qualified_stop_avoidance",
    )

    status = run_audit(
        stop_registration_patterns=[
            str(tmp_path / "stop_registrations.csv")
        ],
        stop_outcome_patterns=[str(tmp_path / "stop_outcomes.csv")],
        stop_status_patterns=[str(tmp_path / "stop_status.json")],
        stop_model_path=tmp_path / "stop.joblib",
        runner_registration_patterns=[
            str(tmp_path / "runner_registrations.csv")
        ],
        runner_outcome_patterns=[str(tmp_path / "runner_outcomes.csv")],
        runner_status_patterns=[str(tmp_path / "runner_status.json")],
        runner_model_path=tmp_path / "runner.joblib",
        raw_patterns=[str(tmp_path / "raw.csv")],
        output_dir=tmp_path / "audit",
        forward_sessions_required=20,
    )

    trades = pd.read_csv(tmp_path / "audit" / "forward_trades.csv")
    assert status["eligible_sessions"] == 1
    assert status["forward"]["trades"] == 2
    assert status["accepted"] is False
    assert status["alerts_allowed"] is False
    assert status["real_orders_allowed"] is False
    assert set(trades["portfolio_lane"]) == {"stop_half", "runner_half"}
    assert (trades["deployed_capital"] <= LANE_CAPITAL).all()
    assert (
        status["frozen_hypothesis"]["allocation"][STOP_LANE["source"]]
        == LANE_CAPITAL
    )


def test_combined_daily_is_incomplete_when_either_half_cannot_exit():
    stop = pd.DataFrame(
        [
            {
                "date": FORWARD_START,
                "trades": 1,
                "executable_trades": 1,
                "exit_depth_failures": 0,
                "execution_complete": True,
                "pnl": 1_000.0,
            }
        ]
    )
    runner = pd.DataFrame(
        [
            {
                "date": FORWARD_START,
                "trades": 1,
                "executable_trades": 0,
                "exit_depth_failures": 1,
                "execution_complete": False,
                "pnl": pd.NA,
            }
        ]
    )

    combined = _combine_daily(stop, runner, [FORWARD_START]).iloc[0]

    assert not bool(combined["execution_complete"])
    assert int(combined["exit_depth_failures"]) == 1
    assert pd.isna(combined["pnl"])

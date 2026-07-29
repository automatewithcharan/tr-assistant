import json
from datetime import datetime, timedelta

import joblib
import pandas as pd

from pkscreener.classes.OrderBookExecution import depth_json
from scripts.assess_frozen_balanced_margin_forward import (
    FORWARD_START,
    RUNNER_LANE,
    STOP_LANE,
    run_audit,
)
from scripts.assess_frozen_balanced_stop_portfolio_forward import CONTRACT


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


def _original(symbol, source, model_hash, score):
    return {
        "EventId": f"{FORWARD_START}:{symbol}:{model_hash}",
        "Date": FORWARD_START,
        "EntryAt": f"{FORWARD_START} 09:20:00",
        "RecorderGeneration": 6,
        "Exchange": "NSE",
        "Symbol": symbol,
        "Source": source,
        "EntryAsk": 100.0,
        "ExecutionSchemaVersion": 2,
        "EntryDepthJson": _depth(100.0, 2000, 0.05),
        "ModelTrainedThrough": "2026-07-29",
        "ModelSourceSha256": model_hash,
        "ExpectedReturnPct": score,
    }


def _leveraged(original, multiplier):
    suffix = f"mis_{multiplier:.2f}x".replace(".", "_")
    row = dict(original)
    row.update(
        {
            "EventId": f"{original['EventId']}|{suffix}|post_margin",
            "OriginalEventId": original["EventId"],
            "EntryAt": f"{FORWARD_START} 09:21:00",
            "Source": f"{original['Source']}:{suffix}",
            "EntryDepthJson": _depth(100.0, 3000, 0.05),
            "MarginQuotedAt": f"{FORWARD_START} 09:20:30",
            "MarginPct": 20.0,
            "RequiredMarginEstimate": 75_000.0,
            "NotionalMultiplier": multiplier,
            "BaseLaneCapital": 250_000.0,
            "StressPct": 0.10,
        }
    )
    return row


def _outcome(entry):
    return {
        "MatrixId": f"{entry['EventId']}|{CONTRACT}",
        "EventId": entry["EventId"],
        "Contract": CONTRACT,
        "ExitAt": f"{FORWARD_START} 09:40:00",
        "ExitReason": "target",
        "ExecutionSchemaVersion": 2,
        "ExitDepthJson": _depth(101.5, 5000, -0.05),
        "StressPct": 0.10,
    }


def _raw_rows():
    start = datetime.fromisoformat(f"{FORWARD_START} 09:15:00")
    return [
        {
            "When": (start + timedelta(seconds=20 * index)).isoformat(),
            "DayChangeRank": 1,
            "RecorderGeneration": 6,
        }
        for index in range(1066)
    ]


def _status(path):
    path.write_text(
        json.dumps(
            {
                "status": "recording_exact_exit_contract_matrix",
                "date": FORWARD_START,
                "updated_at": f"{FORWARD_START} 15:25:00",
                "execution_schema_version": 2,
                "contracts": 9,
                "paper_only": True,
                "alerts_allowed": False,
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )


def _model(path, model_hash, artifact_type):
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


def test_fixed_margin_variants_require_post_quote_entries(tmp_path):
    stop = _original("STOP", STOP_LANE["source"], "stop-hash", 0.90)
    runner = _original(
        "RUNNER", RUNNER_LANE["source"], "runner-hash", 0.70
    )
    leveraged = [
        _leveraged(original, multiplier)
        for original in (stop, runner)
        for multiplier in (1.25, 1.50)
    ]
    pd.DataFrame([stop]).to_csv(tmp_path / "stop.csv", index=False)
    pd.DataFrame([runner]).to_csv(tmp_path / "runner.csv", index=False)
    pd.DataFrame(leveraged).to_csv(
        tmp_path / "leverage_registrations.csv", index=False
    )
    pd.DataFrame([_outcome(row) for row in leveraged]).to_csv(
        tmp_path / "leverage_outcomes.csv", index=False
    )
    pd.DataFrame(_raw_rows()).to_csv(tmp_path / "raw.csv", index=False)
    for name in ("stop_status", "runner_status", "leverage_status"):
        _status(tmp_path / f"{name}.json")
    _model(
        tmp_path / "stop.joblib",
        "stop-hash",
        "prior_session_rank_migration_stop_avoidance",
    )
    _model(
        tmp_path / "runner.joblib",
        "runner-hash",
        "prior_session_runner_qualified_stop_avoidance",
    )

    status = run_audit(
        stop_registration_patterns=[str(tmp_path / "stop.csv")],
        stop_status_patterns=[str(tmp_path / "stop_status.json")],
        stop_model_path=tmp_path / "stop.joblib",
        runner_registration_patterns=[str(tmp_path / "runner.csv")],
        runner_status_patterns=[str(tmp_path / "runner_status.json")],
        runner_model_path=tmp_path / "runner.joblib",
        leverage_registration_patterns=[
            str(tmp_path / "leverage_registrations.csv")
        ],
        leverage_outcome_patterns=[
            str(tmp_path / "leverage_outcomes.csv")
        ],
        leverage_status_patterns=[
            str(tmp_path / "leverage_status.json")
        ],
        raw_patterns=[str(tmp_path / "raw.csv")],
        output_dir=tmp_path / "audit",
    )

    assert status["status"] == "collecting_untouched_post_margin_sessions"
    assert status["primary_multiplier"] == 1.5
    assert status["variants"]["1.25x"]["eligible_sessions"] == 1
    assert status["variants"]["1.50x"]["eligible_sessions"] == 1
    assert status["paper_only"] is True
    assert status["alerts_allowed"] is False
    assert status["real_orders_allowed"] is False
    primary_trades = pd.read_csv(
        tmp_path
        / "audit"
        / "mis_1_50x"
        / "forward_trades.csv"
    )
    assert len(primary_trades) == 2
    assert (primary_trades["deployed_capital"] > 250_000).all()

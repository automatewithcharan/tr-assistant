from datetime import datetime, timedelta

import pandas as pd

from pkscreener.classes.FrozenRankDisplacementPaperTrader import (
    POLICY_SHA256,
    POLICY_VERSION,
)
from pkscreener.classes.OrderBookExecution import depth_json
from scripts.assess_frozen_rank_displacement_forward import assess


def _write_session(root, date, *, pnl_per_trade=5_000.0):
    start = datetime.fromisoformat(f"{date}T09:15:00")
    heartbeats = []
    for index in range(1_001):
        when = start + timedelta(seconds=22 * index)
        heartbeats.append(
            {
                "date": date,
                "when": when.isoformat(),
                "tick_symbols": 2_900,
                "candidate_events": 0,
                "eligible_candidates": 0,
                "emitted_events": 0,
                "open_position": False,
                "paper_only": True,
                "alerts_allowed": False,
                "real_orders_allowed": False,
                "policy_version": POLICY_VERSION,
                "policy_sha256": POLICY_SHA256,
            }
        )
    pd.DataFrame(heartbeats).to_csv(
        root / f"heartbeats_{date}.csv", index=False
    )
    levels = depth_json(
        [{"price": 100.0, "quantity": 10_000, "orders": 10}]
    )
    trades = []
    for index in range(2):
        entry = start + timedelta(minutes=10 + index * 30)
        exit_at = entry + timedelta(minutes=10)
        trades.append(
            {
                "date": date,
                "trade_id": f"{date}:{index}:AAA",
                "symbol": f"AAA{index}",
                "source_event_id": f"{date}:AAA{index}",
                "entry_time": entry.isoformat(),
                "exit_time": exit_at.isoformat(),
                "recorder_generation": 6,
                "exchange": "NSE",
                "model_trained_through": "2026-07-29",
                "model_source_sha256": "model-hash",
                "entry_score": 0.80,
                "replacement_score": 0.0,
                "entry_price": 100.0,
                "exit_price": 101.1,
                "quantity": 5_000,
                "deployed_capital": 500_000.0,
                "capital_utilization_pct": 100.0,
                "target_price": 101.5,
                "stop_price": 99.0,
                "exit_reason": "horizon",
                "gross_pnl": 5_500.0,
                "costs": 500.0,
                "net_pnl": pnl_per_trade,
                "net_return_pct": pnl_per_trade / 5_000.0,
                "hold_minutes": 10.0,
                "replaced_by_symbol": "",
                "execution_schema_version": 2,
                "entry_depth_json": levels,
                "exit_depth_json": levels,
                "entry_depth_fillable": True,
                "exit_depth_fillable": True,
                "entry_depth_vwap": 100.0,
                "exit_depth_vwap": 101.1,
                "closed": True,
                "paper_only": True,
                "alerts_allowed": False,
                "real_orders_allowed": False,
                "policy_version": POLICY_VERSION,
                "policy_sha256": POLICY_SHA256,
            }
        )
    pd.DataFrame(trades).to_csv(root / f"trades_{date}.csv", index=False)


def test_forward_judge_collects_before_twenty_sessions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _write_session(source, "2026-07-30")

    status = assess(
        [str(source / "heartbeats_*.csv")],
        [str(source / "trades_*.csv")],
        tmp_path / "out",
    )

    assert status["status"] == "collecting_untouched_forward_sessions"
    assert status["complete_evaluable_sessions"] == 1
    assert status["forward"]["trades"] == 2
    assert status["accepted"] is False
    assert status["alerts_allowed"] is False
    assert status["real_orders_allowed"] is False


def test_forward_judge_accepts_only_complete_profitable_depth_sample(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    first = datetime(2026, 7, 30)
    for offset in range(20):
        _write_session(
            source,
            (first + timedelta(days=offset)).strftime("%Y-%m-%d"),
        )

    status = assess(
        [str(source / "heartbeats_*.csv")],
        [str(source / "trades_*.csv")],
        tmp_path / "out",
    )

    assert status["complete_evaluable_sessions"] == 20
    assert status["forward"]["trades"] == 40
    assert status["forward"]["avg_daily_pnl"] == 10_000.0
    assert status["accepted"] is True
    assert status["status"] == "accepted"
    assert status["paper_promotion_allowed"] is True
    assert status["real_orders_allowed"] is False


def test_forward_judge_rejects_policy_hash_mismatch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _write_session(source, "2026-07-30")
    path = source / "heartbeats_2026-07-30.csv"
    heartbeats = pd.read_csv(path)
    heartbeats["policy_sha256"] = "changed"
    heartbeats.to_csv(path, index=False)

    status = assess(
        [str(source / "heartbeats_*.csv")],
        [str(source / "trades_*.csv")],
        tmp_path / "out",
    )

    assert status["eligible_complete_sessions"] == 0
    assert status["accepted"] is False

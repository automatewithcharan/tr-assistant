import pandas as pd

from scripts.assess_leader_pullback_reclaim import (
    audit,
    replay_one_slot,
)


def _candidate(date, symbol, signal, exit_time, score, ret):
    return {
        "date": date,
        "symbol": symbol,
        "signal_time": pd.Timestamp(signal),
        "entry_time": pd.Timestamp(signal).floor("min") + pd.Timedelta(minutes=1),
        "rank_score": score,
        "entry_price": 100.0,
        "t1_5_s1_0_return_pct": ret,
        "t1_5_s1_0_exit_time": pd.Timestamp(exit_time),
    }


def test_reclaim_replay_ranks_visible_batch_and_reuses_exact_slot():
    candidates = pd.DataFrame(
        [
            _candidate("2026-07-21", "LOW", "2026-07-21 09:20", "2026-07-21 09:30", 10, 5.0),
            _candidate("2026-07-21", "HIGH", "2026-07-21 09:20", "2026-07-21 09:30", 20, 1.0),
            _candidate("2026-07-21", "BLOCKED", "2026-07-21 09:25", "2026-07-21 09:35", 30, 5.0),
            _candidate("2026-07-21", "REUSED", "2026-07-21 09:30", "2026-07-21 09:40", 15, 1.0),
        ]
    )
    daily, trades = replay_one_slot(
        candidates,
        ["2026-07-21"],
        contract="t1_5_s1_0",
        capital=500_000,
    )
    assert trades["symbol"].tolist() == ["HIGH", "REUSED"]
    assert daily.iloc[0]["pnl"] == 10_000.0


def test_reclaim_audit_includes_zero_trade_calendar_sessions(tmp_path):
    candidate = pd.DataFrame(
        [_candidate("2026-07-21", "AAA", "2026-07-21 09:20", "2026-07-21 09:30", 20, 1.4)]
    )
    for contract in ("t0_5_s0_5", "t0_7_s0_5", "t1_0_s0_5"):
        candidate[f"{contract}_return_pct"] = 0.4
        candidate[f"{contract}_exit_time"] = pd.Timestamp("2026-07-21 09:30")
    status = audit(
        candidate,
        ["2026-07-21", "2026-07-22"],
        output_dir=tmp_path,
        capital=500_000,
        daily_goal=10_000,
        stress_cost_pct=0.1,
    )
    assert status["goal"]["sessions"] == 2
    assert status["goal"]["trade_sessions"] == 1
    assert status["goal"]["avg_daily_pnl"] == 3_500.0
    assert not status["accepted"]
    assert not status["real_orders_allowed"]

import pandas as pd

from scripts.assess_full_rank_capital_allocation import (
    SLOT_COUNTS,
    replay_concurrent_slots,
)


def event(symbol, entry, exit_at, entry_ask, exit_bid, rank):
    return {
        "date": "2026-07-23",
        "Contract": "t1_5_s1_0_h60",
        "Symbol": symbol,
        "Cohort": "test",
        "entry_time": pd.Timestamp(entry),
        "exit_time": pd.Timestamp(exit_at),
        "EntryAsk": entry_ask,
        "ExitBid": exit_bid,
        "StressPct": 0.1,
        "DayChangeRank": rank,
        "EntrySpreadBps": 5.0,
        "RankVelocityPerMinute": 100.0 - rank,
        "RankImprovement": 100 - rank,
        "DayChangePct": 1.0 + rank / 100.0,
        "BalancedScore": 1.0 - rank / 100.0,
        "ExitReason": "timeout",
    }


def test_two_slots_can_take_later_leader_without_exceeding_total_capital():
    frame = pd.DataFrame(
        [
            event("BLOCKER", "2026-07-23 09:20", "2026-07-23 10:20", 100, 99, 1),
            event("LEADER", "2026-07-23 09:21", "2026-07-23 09:30", 100, 102, 2),
        ]
    )
    one_daily, one_trades = replay_concurrent_slots(
        frame, ["2026-07-23"], selector="rank_first", slots=1
    )
    two_daily, two_trades = replay_concurrent_slots(
        frame, ["2026-07-23"], selector="rank_first", slots=2
    )

    assert SLOT_COUNTS == (1, 2, 3, 5)
    assert one_trades["symbol"].tolist() == ["BLOCKER"]
    assert two_trades["symbol"].tolist() == ["BLOCKER", "LEADER"]
    assert two_trades["slot_capital"].tolist() == [250000.0, 250000.0]
    assert two_trades["deployed_capital"].max() <= 250000.0
    assert two_daily.iloc[0]["peak_concurrent"] == 2
    assert two_daily.iloc[0]["pnl"] > one_daily.iloc[0]["pnl"]


def test_slots_are_reused_only_after_observed_exit_and_symbol_is_not_retraded():
    frame = pd.DataFrame(
        [
            event("FIRST", "2026-07-23 09:20", "2026-07-23 09:22", 100, 101, 1),
            event("BLOCKED", "2026-07-23 09:21", "2026-07-23 09:25", 100, 102, 2),
            event("FIRST", "2026-07-23 09:23", "2026-07-23 09:24", 100, 103, 1),
            event("AFTER", "2026-07-23 09:23", "2026-07-23 09:24", 100, 102, 3),
        ]
    )
    daily, trades = replay_concurrent_slots(
        frame, ["2026-07-23"], selector="rank_first", slots=1
    )
    assert trades["symbol"].tolist() == ["FIRST", "AFTER"]
    assert daily.iloc[0]["trades"] == 2


def test_invalid_slot_count_is_rejected():
    frame = pd.DataFrame([event("ONE", "2026-07-23 09:20", "2026-07-23 09:21", 100, 101, 1)])
    try:
        replay_concurrent_slots(frame, ["2026-07-23"], selector="rank_first", slots=4)
    except ValueError as exc:
        assert "unsupported slot count" in str(exc)
    else:
        raise AssertionError("unsupported slot count was accepted")

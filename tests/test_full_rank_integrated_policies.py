import pandas as pd

from pkscreener.classes.FullUniverseRankContractMatrixObserver import CONTRACTS
from scripts.assess_exact_full_rank_paper import SELECTORS
from scripts.assess_full_rank_capital_allocation import SLOT_COUNTS
from scripts.assess_full_rank_feature_gates import FEATURE_GATES
from scripts.assess_full_rank_integrated_policies import (
    INTEGRATED_POLICIES,
    replay_policy,
    validate_policy_registry,
)


def test_integrated_registry_is_fixed_and_internally_valid():
    validate_policy_registry()
    assert len(INTEGRATED_POLICIES) == 10
    for policy in INTEGRATED_POLICIES.values():
        assert policy["gate"] in FEATURE_GATES
        assert policy["selector"] in SELECTORS
        assert policy["contract"] in CONTRACTS
        assert policy["slots"] in SLOT_COUNTS


def test_integrated_policy_applies_gate_contract_and_capital_slots():
    rows = []
    for symbol, contract, hour, improvement in (
        ("GOOD", "t1_5_s1_0_h60", 10, 100),
        ("WRONG_CONTRACT", "t0_8_s0_5_h30", 10, 100),
        ("WRONG_TIME", "t1_5_s1_0_h60", 13, 100),
        ("WEAK", "t1_5_s1_0_h60", 10, 2),
    ):
        rows.append(
            {
                "date": "2026-07-23",
                "Contract": contract,
                "Symbol": symbol,
                "Cohort": "test",
                "entry_time": pd.Timestamp(f"2026-07-23 {hour:02d}:00:00"),
                "exit_time": pd.Timestamp(f"2026-07-23 {hour:02d}:05:00"),
                "EntryAsk": 100.0,
                "ExitBid": 101.5,
                "StressPct": 0.1,
                "DayChangeRank": 20,
                "EntrySpreadBps": 5.0,
                "RankVelocityPerMinute": float(improvement),
                "RankImprovement": improvement,
                "DayChangeDeltaPct": 0.2 if improvement > 10 else 0.01,
                "DayChangePct": 2.0,
                "BalancedScore": 0.8,
                "ExitReason": "target",
            }
        )
    daily, trades = replay_policy(
        pd.DataFrame(rows),
        ["2026-07-23"],
        policy_name="morning_acceleration",
        capital=500_000,
        max_trades_per_day=10,
    )

    assert trades["symbol"].tolist() == ["GOOD"]
    assert trades.iloc[0]["slots"] == 3
    assert trades.iloc[0]["slot_capital"] == round(500_000 / 3, 2)
    assert daily.iloc[0]["policy"] == "morning_acceleration"


def test_unknown_integrated_policy_is_rejected():
    try:
        replay_policy(
            pd.DataFrame(),
            ["2026-07-23"],
            policy_name="future_hindsight_policy",
            capital=500_000,
            max_trades_per_day=10,
        )
    except ValueError as exc:
        assert "unknown integrated policy" in str(exc)
    else:
        raise AssertionError("unknown policy was accepted")

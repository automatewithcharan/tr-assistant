import pandas as pd

from scripts.assess_event_shadow_exit_contracts import FROZEN_THROUGH, registry, summarize


def test_registry_is_fixed_and_contains_goal_sized_contracts():
    configs = registry()
    assert len(configs) == 48
    assert {row["positions"] for row in configs} == {1, 2}
    assert any(row["target_pct"] == 2.5 for row in configs)
    assert FROZEN_THROUGH == "2026-07-16"


def test_exploratory_sessions_cannot_promote_a_contract():
    daily = pd.DataFrame(
        [
            {"config": "t1_s1_n1", "target_pct": 1.0, "stop_pct": 1.0, "positions": 1, "date": "2026-07-15", "evidence_split": "exploratory", "trades": 1, "daily_return_pct": 0.75},
            {"config": "t1_s1_n1", "target_pct": 1.0, "stop_pct": 1.0, "positions": 1, "date": "2026-07-16", "evidence_split": "exploratory", "trades": 1, "daily_return_pct": 0.75},
        ]
    )
    result = summarize(daily, 400000.0, 10000.0, min_forward_sessions=20).iloc[0]
    assert result["exploratory_avg_daily_pnl"] == 3000.0
    assert result["forward_sessions"] == 0
    assert not bool(result["paper_candidate"])
    assert not bool(result["real_orders_allowed"])

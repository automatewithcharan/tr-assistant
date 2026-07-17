import pandas as pd

from scripts.assess_event_shadow_candidate_selectors import pick_candidate, selector_registry, summarize


def test_selector_registry_is_small_and_goal_target_is_present():
    configs = selector_registry()
    assert len(configs) == 12
    assert {row["target_pct"] for row in configs} == {1.5, 3.0}


def test_balanced_selector_uses_same_timestamp_cross_section():
    batch = pd.DataFrame(
        [
            {"symbol": "CHASER", "event_type": "vwap_reclaim", "event_score": 9.0, "cs_move_rank_pct": 100, "cs_vol_ratio_rank_pct": 100, "cs_relative_strength_rank_pct": 100, "cs_signal_value_rank_pct": 1, "cs_pullback_rank_pct": 100},
            {"symbol": "BALANCED", "event_type": "or15_breakout", "event_score": 5.0, "cs_move_rank_pct": 90, "cs_vol_ratio_rank_pct": 90, "cs_relative_strength_rank_pct": 90, "cs_signal_value_rank_pct": 90, "cs_pullback_rank_pct": 10},
        ]
    )
    pick = pick_candidate(batch, "balanced_leader")
    assert pick["symbol"] == "BALANCED"


def test_selector_exploration_never_promotes_without_forward_sessions():
    daily = pd.DataFrame(
        [
            {"config": "event_score_t3_s1", "selector": "event_score", "target_pct": 3.0, "stop_pct": 1.0, "evidence_split": "exploratory", "daily_return_pct": 2.75},
            {"config": "event_score_t3_s1", "selector": "event_score", "target_pct": 3.0, "stop_pct": 1.0, "evidence_split": "exploratory", "daily_return_pct": -1.25},
        ]
    )
    result = summarize(daily, 400000.0, 10000.0, 20).iloc[0]
    assert result["forward_sessions"] == 0
    assert not bool(result["paper_candidate"])
    assert not bool(result["real_orders_allowed"])

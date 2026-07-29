from dataclasses import replace

import pandas as pd

from pkscreener.classes.RankedConfirmationPaperTrader import RankedPaperConfig
from scripts.assess_ranked_confirmation_replay import assess_replay, configuration_registry


def _daily(days, pnl):
    return pd.DataFrame(
        {
            "date": [f"2026-07-{day:02d}" for day in range(1, days + 1)],
            "tape_complete": True,
            "net_pnl": pnl,
            "goal_amount": 10_000.0,
        }
    )


def _trades(days, pnl, per_day=2):
    rows = []
    for day in range(1, days + 1):
        for index in range(per_day):
            rows.append(
                {
                    "date": f"2026-07-{day:02d}",
                    "closed": True,
                    "tape_complete": True,
                    "net_pnl": pnl / per_day,
                    "entry_price": 100.0,
                    "qty": 500,
                }
            )
    return pd.DataFrame(rows)


def test_small_profitable_sample_cannot_pass_readiness_gate():
    row = assess_replay(
        "tiny",
        RankedPaperConfig(),
        _daily(4, 2_000.0),
        _trades(4, 2_000.0),
        capital=400_000.0,
        daily_goal=10_000.0,
    )

    assert not row["SampleGatePassed"]
    assert not row["ShadowCandidate"]
    assert not row["PaperPromotionAllowed"]
    assert not row["RealOrdersAllowed"]


def test_robust_large_sample_can_be_shadow_but_never_real_order_enabled():
    row = assess_replay(
        "robust",
        replace(RankedPaperConfig(), target_pct=0.5),
        _daily(20, 12_000.0),
        _trades(20, 12_000.0),
        capital=400_000.0,
        daily_goal=10_000.0,
    )

    assert row["SampleGatePassed"]
    assert row["PerformanceGatePassed"]
    assert row["ShadowCandidate"]
    assert row["GoalEvidencePassed"]
    assert row["PaperPromotionAllowed"]
    assert not row["RealOrdersAllowed"]


def test_configuration_registry_is_fixed_and_small():
    registry = configuration_registry()

    assert "baseline_current" in registry
    assert "patient_strong" in registry
    assert "lock_075_floor_025" in registry
    assert "trail_075_distance_035" in registry
    assert len(registry) == 14

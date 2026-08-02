import pandas as pd

from scripts.assess_predeclared_capacity_challenger import (
    CAPITAL,
    POLICIES,
    SLOTS,
    _policy_eligible_dates,
    policy_events_for_replay,
)


def test_capacity_challenger_has_fixed_five_slot_paper_policies():
    assert SLOTS == 5
    assert CAPITAL / SLOTS == 100000.0
    assert set(POLICIES) == {
        "acceleration_t1_5_s1_0",
        "acceleration_t1_5_s1_5",
        "sector_rotation_t1_5_s1_0",
        "rapid_climber_t1_0_s1_0",
    }
    assert POLICIES["sector_rotation_t1_5_s1_0"]["gate"] == "sector_rotation"
    assert POLICIES["rapid_climber_t1_0_s1_0"]["gate"] == "rapid_rank_climber"
    assert POLICIES["sector_rotation_t1_5_s1_0"]["slots"] == 3
    assert POLICIES["rapid_climber_t1_0_s1_0"]["slots"] == 5
    assert all("gate" in policy for policy in POLICIES.values())


def test_policy_gate_is_applied_before_replay():
    from scripts.assess_full_rank_feature_gates import apply_feature_gate

    events = pd.DataFrame(
        [
            {
                "date": "2026-08-03",
                "DayChangePct": 2.0,
                "SectorTop2Count": 2,
                "SectorBreadthPct": 55.0,
            },
            {
                "date": "2026-08-03",
                "DayChangePct": 2.0,
                "SectorTop2Count": 1,
                "SectorBreadthPct": 55.0,
            },
        ]
    )

    gated = apply_feature_gate(events, "sector_rotation")
    assert len(gated) == 1
    assert float(gated.iloc[0]["SectorTop2Count"]) == 2.0


def test_policy_replay_filters_declared_contract_before_gate():
    events = pd.DataFrame(
        [
            {
                "date": "2026-08-03",
                "Contract": "t1_5_s1_0_h60",
                "Symbol": "SECTOR",
                "DayChangePct": 2.0,
                "SectorTop2Count": 2,
                "SectorBreadthPct": 55.0,
            },
            {
                "date": "2026-08-03",
                "Contract": "t0_5_s0_5_h15",
                "Symbol": "WRONG_CONTRACT",
                "DayChangePct": 2.0,
                "SectorTop2Count": 2,
                "SectorBreadthPct": 55.0,
            },
        ]
    )

    gated = policy_events_for_replay(
        events,
        ["2026-08-03"],
        POLICIES["sector_rotation_t1_5_s1_0"],
    )
    assert gated["Symbol"].tolist() == ["SECTOR"]
    assert gated["Contract"].tolist() == ["t1_5_s1_0_h60"]


def test_policy_eligibility_requires_complete_contract_and_raw_session():
    entries = pd.DataFrame(
        [{"date": "2026-08-03", "EventId": "one"}, {"date": "2026-08-03", "EventId": "two"}]
    )
    events = pd.DataFrame(
        [
            {"date": "2026-08-03", "EventId": "one", "Contract": "t1_5_s1_0_h60"},
            {"date": "2026-08-03", "EventId": "two", "Contract": "t1_5_s1_0_h60"},
        ]
    )
    raw = pd.DataFrame([{"date": "2026-08-03", "full_session_eligible": True}])

    assert _policy_eligible_dates(
        entries, events, raw, "t1_5_s1_0_h60", "2026-08-03"
    ) == ["2026-08-03"]

    missing = events.iloc[:1]
    assert _policy_eligible_dates(
        entries, missing, raw, "t1_5_s1_0_h60", "2026-08-03"
    ) == []

import pandas as pd

from scripts.assess_predeclared_capacity_challenger import (
    CAPITAL,
    POLICIES,
    SLOTS,
    _policy_eligible_dates,
)


def test_capacity_challenger_has_fixed_five_slot_paper_policies():
    assert SLOTS == 5
    assert CAPITAL / SLOTS == 100000.0
    assert set(POLICIES) == {
        "acceleration_t1_5_s1_0",
        "acceleration_t1_5_s1_5",
    }
    assert all(policy["selector"] == "acceleration_first" for policy in POLICIES.values())


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

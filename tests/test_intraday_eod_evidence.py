import json
import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.check_intraday_eod_evidence import (
    ARTIFACTS,
    assess,
    predeclared_capacity_integrity,
    ranked_paper_ledger_integrity,
)


IST = ZoneInfo("Asia/Kolkata")


def test_entry_timing_artifact_points_to_observer_output():
    assert ARTIFACTS["entry_timing_matrix"] == (
        "results/Data/full_universe_rank_entry_timing/status.json"
    )


def write_json(path: Path, payload: dict, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def fixture_root(tmp_path: Path, base_status: str = "complete") -> tuple[Path, float]:
    day = date(2026, 7, 23)
    cutoff = datetime.combine(day, time(15, 30), tzinfo=IST).timestamp()
    fresh = cutoff + 600
    write_json(
        tmp_path / "results/Data/live_feature_recorder/eod/2026-07-23/status.json",
        {"status": base_status},
        fresh,
    )
    for relative in ARTIFACTS.values():
        payload = {"status": "collecting", "real_orders_allowed": False}
        if relative == ARTIFACTS["predeclared_capacity_challenger"]:
            payload.update(
                {
                    "policy_count": 11,
                    "policies": {
                        name: {} for name in [
                            "opening_ranked_t0_8_s0_5",
                            "early_rank_acceleration_t2_0_s1_0",
                            "acceleration_t1_5_s1_0",
                            "acceleration_t1_5_s1_5",
                            "sector_rotation_t1_5_s1_0",
                            "sector_rotation_acceleration_t1_5_s1_0",
                            "sector_rotation_acceleration_t1_5_s1_5",
                            "sector_rotation_t1_5_s1_5",
                            "rapid_climber_t1_0_s1_0",
                            "reusable_50k_early_acceleration_t1_0_s0_5",
                            "reusable_50k_early_acceleration_t2_0_s1_0",
                        ]
                    },
                    "selection_uses_forward_outcomes": False,
                    "paper_only": True,
                    "alerts_allowed": False,
                    "paper_promotion_allowed": False,
                }
            )
        write_json(
            tmp_path / relative,
            payload,
            fresh,
        )
    coverage = tmp_path / "results/Data/full_universe_rank_strategy/session_coverage.csv"
    coverage.parent.mkdir(parents=True, exist_ok=True)
    coverage.write_text(
        "date,full_session_eligible,max_gap_seconds\n2026-07-23,True,60\n",
        encoding="utf-8",
    )
    os.utime(coverage, (fresh, fresh))
    events = tmp_path / "results/Data/ranked_confirmation_paper/events.csv"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        "timestamp,event,symbol,status,reason,price,rank_score,qty,gross_pnl,costs,net_pnl,details_json\n"
        "2026-07-23T09:30:00+05:30,paper_entry,TEST,open,test,100,50,10,0,0,0,{}\n"
        "2026-07-23T10:00:00+05:30,paper_exit,TEST,closed,target,101,50,10,10,1,9,{}\n"
        '2026-07-23T15:10:00+05:30,paper_daily_summary,PORTFOLIO,complete,eod_summary,0,0,0,0,0,9,"{""trades"": 1, ""net_pnl"": 9.0}"\n',
        encoding="utf-8",
    )
    return tmp_path, cutoff


def test_complete_eligible_session_requires_every_fresh_artifact(tmp_path):
    root, cutoff = fixture_root(tmp_path)
    result = assess(root, "2026-07-23", cutoff)
    assert result["status"] == "complete_eligible_session"
    assert result["session_evidence_eligible"] is True
    assert result["artifacts_refreshed"] == result["artifacts_required"] == 27
    assert result["real_orders_allowed"] is False


def test_coverage_failure_is_processed_but_not_selection_evidence(tmp_path):
    root, cutoff = fixture_root(tmp_path, base_status="coverage_gate_failed")
    result = assess(root, "2026-07-23", cutoff)
    assert result["status"] == "complete_ineligible_session"
    assert result["session_evidence_eligible"] is False


def test_complete_labels_do_not_override_partial_rank_session(tmp_path):
    root, cutoff = fixture_root(tmp_path)
    coverage = root / "results/Data/full_universe_rank_strategy/session_coverage.csv"
    coverage.write_text(
        "date,full_session_eligible,max_gap_seconds\n2026-07-23,False,60\n",
        encoding="utf-8",
    )
    os.utime(coverage, (cutoff + 600, cutoff + 600))

    result = assess(root, "2026-07-23", cutoff)

    assert result["status"] == "complete_ineligible_session"
    assert result["rank_full_session_eligible"] is False
    assert result["artifacts_refreshed"] == result["artifacts_required"]


def test_stale_or_unsafe_artifact_fails_integrity(tmp_path):
    root, cutoff = fixture_root(tmp_path)
    stale = root / ARTIFACTS["entry_timing_matrix"]
    os.utime(stale, (cutoff - 1, cutoff - 1))
    unsafe = root / ARTIFACTS["capital_growth"]
    write_json(unsafe, {"status": "collecting", "real_orders_allowed": True}, cutoff + 1)
    result = assess(root, "2026-07-23", cutoff)
    assert result["status"] == "incomplete_eod_pipeline"
    assert result["artifacts_refreshed"] == 25


def test_paper_ledger_integrity_rejects_orphaned_entry(tmp_path):
    path = tmp_path / "events.csv"
    path.write_text(
        "timestamp,event,symbol,net_pnl,details_json\n"
        "2026-07-23T09:30:00+05:30,paper_entry,ORPHAN,0,{}\n"
        '2026-07-23T15:10:00+05:30,paper_daily_summary,PORTFOLIO,0,"{""trades"": 0, ""net_pnl"": 0.0}"\n',
        encoding="utf-8",
    )

    result = ranked_paper_ledger_integrity(path, "2026-07-23")

    assert result["passed"] is False
    assert result["unmatched_entries"] == ["ORPHAN"]


def test_predeclared_capacity_integrity_rejects_partial_or_live_payload():
    payload = {
        "policy_count": 4,
        "policies": {"only_one": {}},
        "selection_uses_forward_outcomes": False,
        "paper_only": True,
        "alerts_allowed": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    assert predeclared_capacity_integrity(payload)["passed"] is False

    payload["policy_count"] = 11
    payload["policies"] = {
        name: {}
        for name in [
            "opening_ranked_t0_8_s0_5",
            "early_rank_acceleration_t2_0_s1_0",
            "acceleration_t1_5_s1_0",
            "acceleration_t1_5_s1_5",
            "sector_rotation_t1_5_s1_0",
            "sector_rotation_acceleration_t1_5_s1_0",
            "sector_rotation_acceleration_t1_5_s1_5",
            "sector_rotation_t1_5_s1_5",
            "rapid_climber_t1_0_s1_0",
            "reusable_50k_early_acceleration_t1_0_s0_5",
            "reusable_50k_early_acceleration_t2_0_s1_0",
        ]
    }
    payload["real_orders_allowed"] = True
    assert predeclared_capacity_integrity(payload)["passed"] is False

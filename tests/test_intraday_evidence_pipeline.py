import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.check_intraday_evidence_pipeline import assess


IST = ZoneInfo("Asia/Kolkata")


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)


def fixture_root(tmp_path: Path) -> tuple[Path, datetime]:
    now = datetime(2026, 7, 23, 10, 0, tzinfo=IST)
    write(tmp_path / ".env.dev", "KITE_AUTO_TRADE=0\nENABLE_GRADE_B_AUTOTRADE=0\n")
    required = [
        "pkscreener/classes/FullUniverseRankPaperObserver.py",
        "scripts/assess_full_rank_contract_matrix.py",
        "scripts/assess_full_rank_entry_timing.py",
        "scripts/assess_full_rank_capital_allocation.py",
        "scripts/assess_full_rank_integrated_policies.py",
        "scripts/project_intraday_capital_growth.py",
        "scripts/assess_full_rank_opportunity_ceiling.py",
        "scripts/check_intraday_eod_evidence.py",
    ]
    for name in required:
        write(tmp_path / name, "# present\n")
    write(
        tmp_path / "scripts/assess_full_rank_capital_allocation.py",
        "SLOT_COUNTS = (1, 2, 3, 5)\n",
    )
    write(
        tmp_path / "scripts/assess_full_rank_integrated_policies.py",
        "INTEGRATED_POLICIES = {" + ",".join(f'\"p{i}\": {{}}' for i in range(10)) + "}\n",
    )
    write(tmp_path / "pkscreener/classes/FullUniverseRankContractMatrixObserver.py", "CONTRACTS = {" + ",".join(f'\"t{i}\": {{}}' for i in range(9)) + "}")
    write(tmp_path / "pkscreener/classes/FullUniverseRankEntryTimingObserver.py", "ENTRY_VARIANTS = {" + ",".join(f'\"v{i}\": {{\"mode\": \"dip\"}}' for i in range(6)) + "}")
    write(tmp_path / "scripts/assess_full_rank_feature_gates.py", "FEATURE_GATES = {" + ",".join(f'\"g{i}\": \"gate\"' for i in range(12)) + "}")
    write(tmp_path / "scripts/assess_exact_full_rank_paper.py", "SELECTORS = {" + ",".join(f'\"s{i}\": \"selector\"' for i in range(4)) + "}")
    calls = ["run_exact_full_rank_strategy", "run_full_rank_contract_matrix", "run_full_rank_entry_timing", "run_full_rank_feature_gates", "run_full_rank_capital_allocation", "run_full_rank_integrated_policies", "run_full_rank_opportunity_ceiling", "run_intraday_capital_growth", "run_eod_integrity"]
    write(tmp_path / "scripts/live_evidence_eod.sh", "\n".join(calls + calls))
    data = tmp_path / "results/Data"
    stamp = "2026-07-23 10:00:00"
    write(data / "kiteticker_all_status_latest.json", {"connected": True, "started_at": "2026-07-23 03:30:00", "last_tick_at": "2026-07-23 04:30:00", "snapshot_symbols": 2860})
    write(data / "live_feature_recorder/full_universe_rank_status.json", {"status": "recording_full_universe_rank", "date": "2026-07-23", "recorded_at": stamp, "universe_size": 2860, "rows_this_snapshot": 240, "interval_seconds": 60, "recorder_generation": 4, "leader_pool_size": 200, "leader_pool_candidates_this_snapshot": 200, "leader_pool_recorded_this_snapshot": 200, "leader_pool_recall_pct": 100.0, "rapid_candidates_this_snapshot": 20, "rapid_recorded_this_snapshot": 20, "rapid_recall_pct": 100.0, "alerts_allowed": False, "real_orders_allowed": False})
    write(
        data / "live_feature_recorder/live_features_2026-07-23.csv",
        "When,DayChangeRank\n"
        "2026-07-23 09:15:02,1\n"
        "2026-07-23 09:16:08,1\n",
    )
    statuses = [
        ("full_universe_rank_paper/status.json", "recording_exact_ask_to_bid_paper_outcomes", {}),
        ("full_universe_rank_contract_matrix/status.json", "recording_exact_exit_contract_matrix", {"contracts": 9}),
        ("full_universe_rank_entry_timing/status.json", "recording_exact_entry_timing_matrix", {"variants": 6}),
    ]
    for name, status, extra in statuses:
        write(data / name, {"status": status, "date": "2026-07-23", "updated_at": stamp, "paper_only": True, "alerts_allowed": False, "real_orders_allowed": False, **extra})
    return tmp_path, now


def run(root: Path, now: datetime):
    return assess(root, now, cron_text="5 10 * * 1-5 /repo/scripts/live_evidence_eod.sh", service_active=True, start_timer_enabled=True, prewarm_timer_enabled=True, disk_free_gb=60, disk_used_pct=36)


def test_ready_pipeline_is_not_strategy_acceptance(tmp_path):
    root, now = fixture_root(tmp_path)
    result = run(root, now)
    assert result["status"] == "ready_for_next_full_session"
    assert result["strategy_accepted"] is False
    assert result["real_orders_allowed"] is False


def test_unsafe_autotrade_flag_blocks_readiness(tmp_path):
    root, now = fixture_root(tmp_path)
    write(root / ".env.dev", "KITE_AUTO_TRADE=1\nENABLE_GRADE_B_AUTOTRADE=0\n")
    result = run(root, now)
    assert result["status"] == "operational_issues_detected"
    assert not next(item for item in result["checks"] if item["name"] == "real_orders_disabled")["passed"]


def test_stale_observer_blocks_readiness(tmp_path):
    root, now = fixture_root(tmp_path)
    status = root / "results/Data/full_universe_rank_paper/status.json"
    payload = json.loads(status.read_text())
    payload["updated_at"] = "2026-07-23 09:50:00"
    write(status, payload)
    result = run(root, now)
    assert result["status"] == "operational_issues_detected"
    assert not next(item for item in result["checks"] if item["name"] == "exact_ask_bid_observer")["passed"]


def test_historical_rank_snapshot_gap_blocks_live_readiness(tmp_path):
    root, now = fixture_root(tmp_path)
    write(
        root / "results/Data/live_feature_recorder/live_features_2026-07-23.csv",
        "When,DayChangeRank\n"
        "2026-07-23 09:15:02,1\n"
        "2026-07-23 09:18:08,1\n"
        "2026-07-23 09:59:30,1\n",
    )

    result = run(root, now)

    continuity = next(
        item for item in result["checks"]
        if item["name"] == "rank_session_continuity_so_far"
    )
    assert result["status"] == "operational_issues_detected"
    assert continuity["passed"] is False
    assert "max_gap=2482s" in continuity["detail"]


def test_live_readiness_uses_only_latest_recorder_generation(tmp_path):
    root, now = fixture_root(tmp_path)
    write(
        root / "results/Data/live_feature_recorder/live_features_2026-07-23.csv",
        "When,DayChangeRank,RecorderGeneration\n"
        "2026-07-23 09:15:02,1,\n"
        "2026-07-23 09:16:08,1,\n"
        "2026-07-23 09:45:00,1,2\n"
        "2026-07-23 09:46:00,1,2\n",
    )

    result = run(root, now)

    continuity = next(
        item for item in result["checks"]
        if item["name"] == "rank_session_continuity_so_far"
    )
    assert continuity["passed"] is False
    assert "generation=2" in continuity["detail"]
    assert "first=09:45:00" in continuity["detail"]


def test_missing_leader_pool_row_blocks_live_readiness(tmp_path):
    root, now = fixture_root(tmp_path)
    status_path = (
        root
        / "results/Data/live_feature_recorder/full_universe_rank_status.json"
    )
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["leader_pool_recorded_this_snapshot"] = 199
    status["leader_pool_recall_pct"] = 99.5
    write(status_path, status)

    result = run(root, now)

    recall = next(
        item for item in result["checks"]
        if item["name"] == "rank_leader_pool_recall"
    )
    assert result["status"] == "operational_issues_detected"
    assert recall["passed"] is False
    assert "recorded=199/200" in recall["detail"]


def test_missing_rapid_climber_blocks_live_readiness(tmp_path):
    root, now = fixture_root(tmp_path)
    status_path = (
        root
        / "results/Data/live_feature_recorder/full_universe_rank_status.json"
    )
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["rapid_recorded_this_snapshot"] = 19
    status["rapid_recall_pct"] = 95.0
    write(status_path, status)

    result = run(root, now)

    recall = next(
        item for item in result["checks"]
        if item["name"] == "rank_rapid_climber_recall"
    )
    assert result["status"] == "operational_issues_detected"
    assert recall["passed"] is False
    assert "recorded=19/20" in recall["detail"]


def test_post_close_stale_feeds_are_expected_when_eod_completed(tmp_path):
    root, _ = fixture_root(tmp_path)
    now = datetime(2026, 7, 23, 16, 15, tzinfo=IST)
    write(
        root / "results/Data/intraday_evidence_pipeline/eod_latest.json",
        {"status": "complete_eligible_session", "date": "2026-07-23"},
    )
    result = assess(
        root,
        now,
        cron_text="5 10 * * 1-5 /repo/scripts/live_evidence_eod.sh",
        service_active=False,
        start_timer_enabled=True,
        prewarm_timer_enabled=True,
        disk_free_gb=60,
        disk_used_pct=36,
    )
    assert result["status"] == "ready_for_next_full_session"
    assert result["market_phase"] == "post_close"


def test_post_close_missing_eod_is_an_operational_issue(tmp_path):
    root, _ = fixture_root(tmp_path)
    now = datetime(2026, 7, 23, 16, 15, tzinfo=IST)
    result = assess(
        root,
        now,
        cron_text="5 10 * * 1-5 /repo/scripts/live_evidence_eod.sh",
        service_active=False,
        start_timer_enabled=True,
        prewarm_timer_enabled=True,
        disk_free_gb=60,
        disk_used_pct=36,
    )
    assert result["status"] == "operational_issues_detected"
    assert not next(item for item in result["checks"] if item["name"] == "today_eod_evidence")["passed"]

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
        "scripts/project_intraday_capital_growth.py",
    ]
    for name in required:
        write(tmp_path / name, "# present\n")
    write(tmp_path / "pkscreener/classes/FullUniverseRankContractMatrixObserver.py", "\n".join(f'\"t{i}\": {{}}' for i in range(9)))
    write(tmp_path / "pkscreener/classes/FullUniverseRankEntryTimingObserver.py", "\n".join('x={\"mode\": \"dip\"}' for _ in range(6)))
    write(tmp_path / "scripts/assess_full_rank_feature_gates.py", "\n".join(f'\"g{i}\": \"gate\"' for i in range(9)))
    write(tmp_path / "scripts/assess_exact_full_rank_paper.py", "\n".join(f'\"s{i}\": \"selector\"' for i in range(4)))
    calls = ["run_exact_full_rank_strategy", "run_full_rank_contract_matrix", "run_full_rank_entry_timing", "run_full_rank_feature_gates", "run_intraday_capital_growth"]
    write(tmp_path / "scripts/live_evidence_eod.sh", "\n".join(calls + calls))
    data = tmp_path / "results/Data"
    stamp = "2026-07-23 10:00:00"
    write(data / "kiteticker_all_status_latest.json", {"connected": True, "last_tick_at": stamp, "snapshot_symbols": 2860})
    write(data / "live_feature_recorder/full_universe_rank_status.json", {"status": "recording_full_universe_rank", "date": "2026-07-23", "recorded_at": stamp, "universe_size": 2860, "rows_this_snapshot": 200, "interval_seconds": 60, "alerts_allowed": False, "real_orders_allowed": False})
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

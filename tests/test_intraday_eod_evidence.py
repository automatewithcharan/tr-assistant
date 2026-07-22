import json
import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.check_intraday_eod_evidence import ARTIFACTS, assess


IST = ZoneInfo("Asia/Kolkata")


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
        write_json(
            tmp_path / relative,
            {"status": "collecting", "real_orders_allowed": False},
            fresh,
        )
    return tmp_path, cutoff


def test_complete_eligible_session_requires_every_fresh_artifact(tmp_path):
    root, cutoff = fixture_root(tmp_path)
    result = assess(root, "2026-07-23", cutoff)
    assert result["status"] == "complete_eligible_session"
    assert result["session_evidence_eligible"] is True
    assert result["artifacts_refreshed"] == result["artifacts_required"] == 11
    assert result["real_orders_allowed"] is False


def test_coverage_failure_is_processed_but_not_selection_evidence(tmp_path):
    root, cutoff = fixture_root(tmp_path, base_status="coverage_gate_failed")
    result = assess(root, "2026-07-23", cutoff)
    assert result["status"] == "complete_ineligible_session"
    assert result["session_evidence_eligible"] is False


def test_stale_or_unsafe_artifact_fails_integrity(tmp_path):
    root, cutoff = fixture_root(tmp_path)
    stale = root / ARTIFACTS["entry_timing_matrix"]
    os.utime(stale, (cutoff - 1, cutoff - 1))
    unsafe = root / ARTIFACTS["capital_growth"]
    write_json(unsafe, {"status": "collecting", "real_orders_allowed": True}, cutoff + 1)
    result = assess(root, "2026-07-23", cutoff)
    assert result["status"] == "incomplete_eod_pipeline"
    assert result["artifacts_refreshed"] == 9

import json

import pandas as pd

from scripts.summarize_broad_backfill_batch import summarize, write_status


def _summary(path):
    path.write_text(
        json.dumps(
            {
                "exact_range_cache_files": 805,
                "mapped_symbols": 2955,
                "needs_fetch_symbols": 2150,
            }
        ),
        encoding="utf-8",
    )


def test_successful_batch_records_refreshed_coverage(tmp_path):
    summary_path = tmp_path / "summary.json"
    audit_path = tmp_path / "audit.csv"
    _summary(summary_path)
    pd.DataFrame({"status": ["complete", "complete", "warning"]}).to_csv(
        audit_path, index=False
    )
    status = summarize(summary_path, audit_path, 7, 0, 0, 0)
    assert status["successful"]
    assert status["cached_symbols"] == 805
    assert status["remaining_symbols"] == 2150
    assert status["audit_status_counts"] == {"complete": 2, "warning": 1}
    assert not status["real_orders_allowed"]


def test_missing_audit_or_fetch_error_fails_closed(tmp_path):
    summary_path = tmp_path / "summary.json"
    _summary(summary_path)
    missing = summarize(summary_path, tmp_path / "missing.csv", 8, 0, 0, 0)
    assert not missing["successful"]

    audit_path = tmp_path / "audit.csv"
    pd.DataFrame({"status": ["complete", "fetch_error"]}).to_csv(
        audit_path, index=False
    )
    failed = summarize(summary_path, audit_path, 8, 1, 0, 0)
    assert not failed["successful"]
    assert failed["fetch_errors"] == 1
    assert "NEEDS ATTENTION" in failed["message"]


def test_status_write_is_atomic_json(tmp_path):
    path = tmp_path / "state" / "latest.json"
    write_status(path, {"successful": False, "real_orders_allowed": False})
    assert json.loads(path.read_text(encoding="utf-8"))["real_orders_allowed"] is False

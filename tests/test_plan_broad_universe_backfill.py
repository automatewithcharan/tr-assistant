import os
from pathlib import Path

import pandas as pd

from scripts import kite_intraday_backfill
from scripts.kite_intraday_backfill import load_backfill_universe, save_cache, select_record_batch
from scripts.plan_broad_universe_backfill import retry_symbols_from_audits, write_pending_batch


def test_select_record_batch_keeps_benchmark_and_stable_slice():
    records = [{"symbol": "NIFTY 50"}] + [{"symbol": f"S{i}"} for i in range(7)]
    assert [r["symbol"] for r in select_record_batch(records, 3, 1)] == ["NIFTY 50", "S3", "S4", "S5"]
    assert [r["symbol"] for r in select_record_batch(records, 3, 2)] == ["NIFTY 50", "S6"]


def test_select_record_batch_zero_returns_all_records():
    records = [{"symbol": "NIFTY 50"}, {"symbol": "ABC"}]
    assert select_record_batch(records, 0, 99) == records


def test_select_record_batch_past_end_keeps_only_benchmark():
    records = [{"symbol": "NIFTY 50"}, {"symbol": "ABC"}]
    assert select_record_batch(records, 100, 1) == [{"symbol": "NIFTY 50"}]


def test_load_symbols_normalises_and_deduplicates(tmp_path: Path):
    path = tmp_path / "universe.csv"
    pd.DataFrame({"Symbol": [" abc ", "ABC", "xyz", ""]}).to_csv(path, index=False)
    assert load_backfill_universe(path) == ["ABC", "XYZ"]


def test_save_cache_atomically_detaches_seeded_hardlink(tmp_path, monkeypatch):
    monkeypatch.setattr(kite_intraday_backfill, "INTRADAY_ROOT", tmp_path)
    path = kite_intraday_backfill.cache_path("ABC", "5minute", "2026-01-01", "2026-01-02")
    source = tmp_path / "seed.csv"
    source.write_text(
        "datetime,open,high,low,close,volume,symbol\n"
        "2026-01-01 09:15:00,1,1,1,1,1,ABC\n"
    )
    path.hardlink_to(source)
    original_inode = path.stat().st_ino
    frame = pd.DataFrame(
        [
            {
                "datetime": "2026-01-02 09:15:00",
                "open": 2,
                "high": 2,
                "low": 2,
                "close": 2,
                "volume": 2,
            }
        ]
    )
    save_cache(frame, "ABC", "5minute", "2026-01-01", "2026-01-02")
    assert path.stat().st_ino != original_inode
    assert "2026-01-01" in source.read_text()
    assert "2026-01-02" in path.read_text()


def test_retry_symbols_uses_latest_batch_audit(tmp_path: Path):
    prefix = tmp_path / "broad"
    old = tmp_path / "broad_batch_0000_5minute_audit.csv"
    new = tmp_path / "broad_batch_0001_5minute_audit.csv"
    pd.DataFrame({"symbol": ["ABC", "OLD"], "status": ["fetch_error", "fetch_error"]}).to_csv(old, index=False)
    pd.DataFrame({"symbol": ["ABC", "XYZ"], "status": ["complete", "fetch_error"]}).to_csv(new, index=False)
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    assert retry_symbols_from_audits(prefix, "5minute") == {"OLD", "XYZ"}


def test_write_pending_batch_compacts_actual_missing_symbols(tmp_path: Path):
    manifest = pd.DataFrame(
        {
            "symbol": ["CACHED0", "MISS1", "CACHED2", "MISS3", "MISS4"],
            "needs_fetch": [False, True, False, True, True],
            "batch_index": [0, 0, 0, 1, 1],
        }
    )
    output = tmp_path / "pending.csv"

    count = write_pending_batch(manifest, output, batch_size=2)

    assert count == 2
    assert pd.read_csv(output)["symbol"].tolist() == ["MISS1", "MISS3"]

"""Read-only aggregation of NG publisher evidence into health snapshots."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
HEALTH_SCHEMA_VERSION = 1


def summarize_publisher_health(
    publisher: str,
    evidence_paths: Iterable[Path | str],
    session_date: str,
) -> dict[str, Any]:
    qualification_ids: set[str] = set()
    successful_ids: set[str] = set()
    attempts = 0
    status_counts = {"http_403_count": 0, "http_429_count": 0, "http_5xx_count": 0}
    source_files: list[str] = []
    for candidate in evidence_paths:
        path = Path(candidate)
        if path.is_dir():
            path = path / f"events_{session_date}.jsonl"
        if not path.exists():
            continue
        source_files.append(str(path.resolve()))
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if str(row.get("event_kind") or "").lower() == "operational":
                    continue
                state = str(row.get("delivery_state") or "").upper()
                identity = str(row.get("delivery_id") or row.get("event_id") or "")
                if state == "OBSERVED" and identity:
                    qualification_ids.add(identity)
                if state == "ATTEMPTED":
                    attempts += 1
                if state == "DELIVERED" and identity:
                    successful_ids.add(identity)
                if state != "FAILED":
                    continue
                try:
                    status = int(row.get("http_status"))
                except (TypeError, ValueError):
                    continue
                if status == 403:
                    status_counts["http_403_count"] += 1
                elif status == 429:
                    status_counts["http_429_count"] += 1
                elif 500 <= status <= 599:
                    status_counts["http_5xx_count"] += 1
    successes = len(successful_ids)
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "publisher": publisher,
        "session_date": session_date,
        "qualification_count": len(qualification_ids),
        "delivery_attempts": attempts,
        "successful_deliveries": successes,
        **status_counts,
        "delivery_success_rate": successes / attempts if attempts else None,
        "source_evidence": sorted(source_files),
    }


def write_publisher_health_snapshot(
    publisher: str,
    evidence_paths: Iterable[Path | str],
    session_date: str,
    output_path: Path | str,
) -> dict[str, Any]:
    """Atomically write operational metrics without modifying source evidence."""
    payload = summarize_publisher_health(publisher, evidence_paths, session_date)
    payload["generated_at"] = datetime.now(tz=IST).isoformat()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return payload


__all__ = ["summarize_publisher_health", "write_publisher_health_snapshot"]

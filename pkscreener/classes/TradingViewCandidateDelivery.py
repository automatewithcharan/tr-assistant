"""Reliable Discord delivery evidence for direct TradingView candidates."""

from __future__ import annotations

import json
import hashlib
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows remains single-process.
    fcntl = None


DIRECT_LANES = ("early_breakout", "top_gainer_radar")
TERMINAL_STATUSES = {"delivered", "terminal_failed", "delivery_uncertain"}
FALLBACK_KEY_VERSION = "tradingview_candidate_fallback_v1"


def deterministic_fallback_candidate_key(
    trading_date: str,
    source_lane: str,
    symbol: str,
) -> str:
    """Return a causal, restart-stable identity when lifecycle storage failed.

    Direct lanes already have one-alert-per-symbol/session semantics.  The
    fallback deliberately uses that same stable boundary instead of a monitor
    cycle timestamp, which would manufacture a new identity after restart.
    """
    material = "|".join(
        (
            FALLBACK_KEY_VERSION,
            str(trading_date or "").strip(),
            str(source_lane or "").strip().lower(),
            str(symbol or "").strip().upper(),
        )
    )
    return "fallback-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def journal_path(output_dir: str | Path, trading_date: str) -> Path:
    return (
        Path(output_dir)
        / "tradingview_candidate_deliveries"
        / f"tradingview_candidate_deliveries_{trading_date}.jsonl"
    )


@contextmanager
def _journal_lock(path: Path):
    lock_path = path.parent / ".delivery_journal.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_delivery_record(output_dir: str | Path, record: dict[str, Any]) -> None:
    """Append and fsync one attempt without persisting webhook credentials."""
    trading_date = str(record.get("trading_date") or "").strip()
    if not trading_date:
        raise ValueError("delivery record trading_date is required")
    path = journal_path(output_dir, trading_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    with _journal_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def load_delivery_records(output_dir: str | Path, trading_date: str) -> list[dict[str, Any]]:
    path = journal_path(output_dir, trading_date)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt delivery journal line {line_number}: {path}") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _normalized_status(row: dict[str, Any]) -> str:
    """Normalize current states and classify journals written by the prior version."""
    status = str(row.get("delivery_status") or "")
    if status in {
        "delivered",
        "terminal_failed",
        "retryable_failed",
        "delivery_uncertain",
    }:
        return status
    # A write-ahead reservation means an HTTP request may or may not have been
    # made.  If no later outcome exists, suppress retries rather than risk an
    # uncontrolled duplicate after a crash or outcome-persistence failure.
    if status == "attempt_reserved":
        return "delivery_uncertain"
    if status == "retrying":
        return "retryable_failed"
    if status != "failed":
        return status
    http_status = row.get("http_status")
    try:
        code = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        code = None
    error = str(row.get("error") or "").lower()
    if (code is not None and 400 <= code < 500 and code != 429) or "not configured" in error:
        return "terminal_failed"
    return "retryable_failed"


def reconstruct_delivery_states(
    output_dir: str | Path,
    trading_date: str,
) -> dict[str, dict[str, Any]]:
    """Reconstruct durable state and global attempt totals from append-only rows."""
    states: dict[str, dict[str, Any]] = {}
    for row in load_delivery_records(output_dir, trading_date):
        observation_id = str(row.get("lifecycle_observation_id") or "")
        if not observation_id:
            continue
        state = states.setdefault(
            observation_id,
            {
                "lifecycle_observation_id": observation_id,
                "total_attempts": 0,
                "first_attempt_at": None,
                "last_attempt_at": None,
            },
        )
        attempt_count = row.get("attempt_count", 0)
        try:
            state["total_attempts"] = max(state["total_attempts"], int(attempt_count or 0))
        except (TypeError, ValueError):
            pass
        attempted_at = row.get("send_attempt_at")
        if attempted_at and state["first_attempt_at"] is None:
            state["first_attempt_at"] = attempted_at
        if attempted_at:
            state["last_attempt_at"] = attempted_at
        state.update(row)
        state["delivery_status"] = _normalized_status(row)
        state["total_attempts"] = max(
            int(state.get("total_attempts", 0) or 0),
            int(attempt_count or 0),
        )
    return states


def terminal_delivery_ids(output_dir: str | Path, trading_date: str) -> set[str]:
    return {
        observation_id
        for observation_id, state in reconstruct_delivery_states(output_dir, trading_date).items()
        if state.get("delivery_status") in TERMINAL_STATUSES
    }


def _load_lifecycle_events(output_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(output_dir) / "top_gainer_lifecycle" / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt lifecycle evidence line {line_number}: {path}") from exc
        if isinstance(row, dict):
            events.append(row)
    return events


def reconcile_tradingview_candidates(
    output_dir: str | Path,
    trading_date: str,
    lanes: Iterable[str] = DIRECT_LANES,
    now: datetime | None = None,
    retry_ttl_seconds: float = 600.0,
    max_total_attempts: int = 6,
) -> dict[str, Any]:
    """Compare qualified direct-lane observations with delivery outcomes."""
    requested_lanes = tuple(str(lane) for lane in lanes)
    lifecycle_events = _load_lifecycle_events(output_dir)
    observations = {
        str(row.get("lifecycle_id") or ""): row
        for row in lifecycle_events
        if row.get("event_type") == "candidate_observed"
        and str(row.get("trading_date") or row.get("observation_timestamp", "")[:10]) == trading_date
        and str(row.get("source_lane") or "") in requested_lanes
    }
    delivery_rows = load_delivery_records(output_dir, trading_date)
    latest = reconstruct_delivery_states(output_dir, trading_date)
    delivered_record_counts: dict[str, int] = {}
    for row in delivery_rows:
        observation_id = str(row.get("lifecycle_observation_id") or "")
        if not observation_id:
            continue
        if row.get("delivery_status") == "delivered":
            delivered_record_counts[observation_id] = delivered_record_counts.get(observation_id, 0) + 1

    result: dict[str, Any] = {"trading_date": trading_date, "lanes": {}}
    audit_now = now or datetime.now().astimezone()

    def retry_is_stale(state: dict[str, Any]) -> bool:
        if int(state.get("total_attempts", 0) or 0) >= max(1, int(max_total_attempts)):
            return True
        try:
            detected = datetime.fromisoformat(
                str(state.get("detected_at") or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return True
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        reference = audit_now
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return (reference - detected).total_seconds() > max(0.0, float(retry_ttl_seconds))

    for lane in requested_lanes:
        lane_ids = sorted(
            observation_id for observation_id, observation in observations.items()
            if observation_id
            and str(observation.get("source_lane") or "") == lane
        )
        delivered = [oid for oid in lane_ids if latest.get(oid, {}).get("delivery_status") == "delivered"]
        retryable_failed = [
            oid for oid in lane_ids
            if latest.get(oid, {}).get("delivery_status") == "retryable_failed"
        ]
        terminal_failed = [
            oid for oid in lane_ids
            if latest.get(oid, {}).get("delivery_status") == "terminal_failed"
        ]
        delivery_uncertain = [
            oid for oid in lane_ids
            if latest.get(oid, {}).get("delivery_status") == "delivery_uncertain"
        ]
        stale_retryable = [oid for oid in retryable_failed if retry_is_stale(latest[oid])]
        missing = [oid for oid in lane_ids if oid not in latest]

        def identity(observation_id: str) -> dict[str, str]:
            observation = observations.get(observation_id, {})
            return {
                "lifecycle_observation_id": observation_id,
                "symbol": str(observation.get("symbol") or ""),
            }

        result["lanes"][lane] = {
            "detected": len(lane_ids),
            "delivered": len(delivered),
            "retryable_failed": len(retryable_failed),
            "terminal_failed": len(terminal_failed),
            "delivery_uncertain": len(delivery_uncertain),
            "retryable_stale": len(stale_retryable),
            "missing": len(missing),
            "duplicates": sum(max(0, delivered_record_counts.get(oid, 0) - 1) for oid in lane_ids),
            "retryable_failed_observations": [identity(oid) for oid in retryable_failed],
            "terminal_failed_observations": [identity(oid) for oid in terminal_failed],
            "delivery_uncertain_observations": [
                identity(oid) for oid in delivery_uncertain
            ],
            "retryable_stale_observations": [identity(oid) for oid in stale_retryable],
            "missing_observations": [identity(oid) for oid in missing],
        }
    return result


def format_reconciliation(report: dict[str, Any]) -> str:
    lines = [f"TradingView candidate delivery audit: {report.get('trading_date', '')}"]
    for lane, values in report.get("lanes", {}).items():
        lines.extend(
            [
                f"{lane} detected: {values['detected']}",
                f"{lane} delivered: {values['delivered']}",
                f"{lane} retryable_failed: {values['retryable_failed']}",
                f"{lane} terminal_failed: {values['terminal_failed']}",
                f"{lane} delivery_uncertain: {values['delivery_uncertain']}",
                f"{lane} retryable_stale: {values['retryable_stale']}",
                f"{lane} missing: {values['missing']}",
                f"{lane} duplicates: {values['duplicates']}",
            ]
        )
        for key in (
            "retryable_failed_observations",
            "terminal_failed_observations",
            "delivery_uncertain_observations",
            "retryable_stale_observations",
            "missing_observations",
        ):
            for item in values[key]:
                lines.append(
                    f"  {key.removesuffix('_observations')}: "
                    f"{item['lifecycle_observation_id']} {item['symbol']}"
                )
    return "\n".join(lines)


__all__ = [
    "DIRECT_LANES",
    "TERMINAL_STATUSES",
    "append_delivery_record",
    "deterministic_fallback_candidate_key",
    "format_reconciliation",
    "journal_path",
    "load_delivery_records",
    "reconcile_tradingview_candidates",
    "reconstruct_delivery_states",
    "terminal_delivery_ids",
]

"""Passive, opt-in tick freshness evidence for SignalMonitor.

This module records provenance only.  It intentionally contains no freshness
threshold, stale/fresh classification, or candidate-control behavior.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


_TRUTHY = {"1", "true", "yes", "y", "on"}
_STATUS_FIELDS = (
    "connected",
    "connected_at",
    "closed_at",
    "close_reason",
    "last_error",
    "last_tick_at",
    "last_save_at",
    "reconnect_attempt",
    "ticks_seen",
    "snapshot_symbols",
)


def _timestamp_evidence(value: Any) -> Dict[str, Any]:
    """Preserve a raw timestamp and normalize only explicit timezone values."""
    raw = "" if value is None else str(value)
    if not raw.strip():
        return {
            "raw": raw,
            "missing": True,
            "parsed_successfully": False,
            "parse_status": "missing",
            "normalized_utc": None,
        }
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {
            "raw": raw,
            "missing": False,
            "parsed_successfully": False,
            "parse_status": "malformed",
            "normalized_utc": None,
        }
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return {
            "raw": raw,
            "missing": False,
            "parsed_successfully": True,
            "parse_status": "naive_timezone_ambiguous",
            "normalized_utc": None,
        }
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return {
        "raw": raw,
        "missing": False,
        "parsed_successfully": True,
        "parse_status": "normalized",
        "normalized_utc": normalized.replace("+00:00", "Z"),
    }


def _json_scalar(value: Any) -> Any:
    """Keep evidence valid JSON without inventing values for missing scalars."""
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class TickFreshnessDiagnostics:
    """Best-effort append-only JSONL recorder, disabled by default."""

    SCHEMA_VERSION = "signalmonitor_tick_freshness.v1"
    ENABLE_ENV = "SIGNALMONITOR_TICK_FRESHNESS_DIAGNOSTICS"
    PATH_ENV = "SIGNALMONITOR_TICK_FRESHNESS_DIAGNOSTICS_PATH"

    def __init__(
        self,
        output_dir: Any,
        enabled: Optional[bool] = None,
        path: Optional[Any] = None,
    ) -> None:
        if enabled is None:
            enabled = str(os.environ.get(self.ENABLE_ENV, "0") or "0").strip().lower() in _TRUTHY
        self.enabled = bool(enabled)
        configured_path = path or os.environ.get(self.PATH_ENV, "")
        if not configured_path:
            configured_path = Path(output_dir) / "tick_freshness_diagnostics" / "evaluations.jsonl"
        self.path = Path(configured_path).expanduser() if self.enabled else None
        self._write_lock = threading.Lock()

    def snapshot_context(self, output_dir: Any, universe: str) -> Dict[str, Any]:
        """Capture local snapshot/status metadata once for a candidate batch."""
        if not self.enabled:
            return {}
        normalized_universe = str(universe or "").strip().lower()
        snapshot_path = Path(output_dir) / f"kiteticker_{normalized_universe}_ticks_latest.json"
        status_path = Path(output_dir) / f"kiteticker_{normalized_universe}_status_latest.json"
        context: Dict[str, Any] = {
            "snapshot_path": str(snapshot_path),
            "snapshot_file_mtime": None,
            "ticker_status": None,
        }
        try:
            context["snapshot_file_mtime"] = (
                datetime.fromtimestamp(snapshot_path.stat().st_mtime, timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
        except (OSError, ValueError, OverflowError):
            pass
        try:
            with status_path.open("r", encoding="utf-8") as handle:
                status = json.load(handle)
            if isinstance(status, dict):
                context["ticker_status"] = {
                    field: status.get(field) for field in _STATUS_FIELDS if field in status
                }
        except (OSError, ValueError, TypeError):
            pass
        return context

    def record(
        self,
        *,
        evaluation_timestamp: Any,
        cycle_timestamp: Any,
        lane: str,
        symbol: str,
        tick: Optional[Dict[str, Any]],
        snapshot_context: Optional[Dict[str, Any]],
        live_price_used: Any,
        live_tick_applied: bool,
        report_cache_fallback: bool,
        candidate_result: Any,
        lifecycle_observation_id: Any = None,
    ) -> None:
        """Append one diagnostic record; contain every recording failure."""
        if not self.enabled or self.path is None:
            return
        try:
            raw_tick = tick if isinstance(tick, dict) else {}
            record = {
                "schema_version": self.SCHEMA_VERSION,
                "evaluation_timestamp": _timestamp_evidence(evaluation_timestamp),
                "cycle_timestamp": _timestamp_evidence(cycle_timestamp),
                "lane": str(lane or ""),
                "symbol": str(symbol or "").strip().upper(),
                "received_at": _timestamp_evidence(raw_tick.get("received_at")),
                "exchange_timestamp": _timestamp_evidence(raw_tick.get("exchange_timestamp")),
                "last_trade_time": _timestamp_evidence(raw_tick.get("last_trade_time")),
                "any_tick_timestamp_missing": any(
                    not str(raw_tick.get(field, "") or "").strip()
                    for field in ("received_at", "exchange_timestamp", "last_trade_time")
                ),
                "snapshot_context": snapshot_context or {},
                "live_price_used": _json_scalar(live_price_used),
                "live_tick_available": bool(raw_tick),
                "live_tick_applied": bool(live_tick_applied),
                "report_cache_fallback": bool(report_cache_fallback),
                "candidate_qualified": True,
                "candidate_result": _json_scalar(candidate_result),
                "lifecycle_observation_id": _json_scalar(lifecycle_observation_id),
            }
            payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock:
                descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                try:
                    written = 0
                    while written < len(payload):
                        count = os.write(descriptor, payload[written:])
                        if count <= 0:
                            raise OSError("diagnostic append made no progress")
                        written += count
                finally:
                    os.close(descriptor)
        except Exception:
            # Evidence collection must never alter or interrupt candidate behavior.
            return

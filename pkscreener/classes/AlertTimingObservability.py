"""Fail-open causal alert-timing telemetry.

The journal is an observational side channel only.  Each event is appended to
``<output_dir>/alert_timing_observability/YYYY-MM-DD.jsonl``.  Callers supply
only values already available at the instrumented runtime boundary; this
module never reads market history or derives indicators.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from PKDevTools.classes.log import default_logger


SCHEMA_VERSION = 1
EVENT_TYPES = frozenset(
    {
        "SCANNER_VISIBLE",
        "OPERATIONALLY_ELIGIBLE",
        "SOURCE_EVALUATED",
        "SOURCE_REJECTED",
        "SOURCE_QUALIFIED",
        "DISCORD_ATTEMPTED",
        "DISCORD_SUCCESS",
        "DISCORD_FAILED",
    }
)
SOURCES = frozenset(
    {"anomaly", "pre_move", "early_breakout", "top_gainer_radar", "buy_now_board"}
)
CONTEXT_FIELDS = frozenset(
    {
        "price",
        "day_change_pct",
        "volume_ratio",
        "signal_strength",
        "rsi",
        "entry_gap_pct",
        "buy_above",
        "vwap",
        "vwap_state",
        "liquidity",
        "traded_value",
        "buy_sell_ratio",
        "near_high_pct",
        "pullback_pct",
        "confidence",
        "rejection_reason",
        "qualified_at",
        "http_status",
        "delivery_duration_ms",
        "failure_category",
        "attempt_number",
        "alert_batch_id",
    }
)
_DISCORD_WEBHOOK_RE = re.compile(
    r"https?://(?:[^/\s]+/)*api/webhooks/[^\s]+", re.IGNORECASE
)
_SECRET_KEY_RE = re.compile(
    r"(?:authorization|credential|password|secret|token|webhook|api[_-]?key)",
    re.IGNORECASE,
)


def _safe_value(value: Any) -> Any:
    """Return a JSON-safe value without manufacturing missing measurements."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, datetime):
        return _aware_timestamp(value).isoformat(timespec="microseconds")
    text = str(value)
    return _DISCORD_WEBHOOK_RE.sub("[REDACTED_DISCORD_WEBHOOK]", text)


def _aware_timestamp(value: Optional[datetime] = None) -> datetime:
    timestamp = value or datetime.now().astimezone()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return timestamp


class AlertTimingObserver:
    """Append schema-v1 events without allowing telemetry failures to escape."""

    def __init__(
        self,
        output_dir: str,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.output_dir = str(output_dir or "")
        self.clock = clock or (lambda: datetime.now().astimezone())

    def emit(
        self,
        event_type: str,
        symbol: str,
        source: str,
        *,
        event_timestamp: Optional[datetime] = None,
        trading_date: Optional[str] = None,
        observation_id: Optional[str] = None,
        candidate_id: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        return self.emit_many(
            [
                {
                    "event_type": event_type,
                    "symbol": symbol,
                    "source": source,
                    "event_timestamp": event_timestamp,
                    "trading_date": trading_date,
                    "observation_id": observation_id,
                    "candidate_id": candidate_id,
                    "context": context,
                }
            ]
        )

    def emit_many(self, events: Iterable[Mapping[str, Any]]) -> bool:
        """Append a batch with one open/write operation; return False on any error."""
        try:
            rows_by_date: Dict[str, list[bytes]] = {}
            for event in events:
                row = self._build_row(event)
                if row is None:
                    continue
                day = str(row["trading_date"])
                rows_by_date.setdefault(day, []).append(
                    (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
                )
            for day, rows in rows_by_date.items():
                self._append_bytes(day, b"".join(rows))
            return True
        except Exception as exc:
            default_logger().warning(
                f"Alert timing telemetry append failed: {type(exc).__name__}"
            )
            return False

    def _build_row(self, event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        event_type = str(event.get("event_type") or "").strip().upper()
        source = str(event.get("source") or "").strip().lower()
        symbol = str(event.get("symbol") or "").strip().upper()
        if event_type not in EVENT_TYPES or source not in SOURCES or not symbol:
            return None

        raw_timestamp = event.get("event_timestamp")
        timestamp = _aware_timestamp(
            raw_timestamp if isinstance(raw_timestamp, datetime) else self.clock()
        )
        trading_date = str(event.get("trading_date") or "").strip()
        if not trading_date:
            trading_date = timestamp.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()

        row: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "trading_date": trading_date,
            "event_timestamp": timestamp.isoformat(timespec="microseconds"),
            "symbol": symbol,
            "source": source,
            "event_type": event_type,
        }
        for identifier in ("observation_id", "candidate_id"):
            value = event.get(identifier)
            if value not in (None, ""):
                row[identifier] = _safe_value(value)

        context = event.get("context")
        if isinstance(context, Mapping):
            for key, value in context.items():
                normalized_key = str(key or "").strip()
                if (
                    normalized_key in CONTEXT_FIELDS
                    and not _SECRET_KEY_RE.search(normalized_key)
                ):
                    row[normalized_key] = _safe_value(value)
        return row

    def _append_bytes(self, trading_date: str, payload: bytes) -> None:
        if not payload:
            return
        journal_dir = Path(self.output_dir) / "alert_timing_observability"
        journal_dir.mkdir(parents=True, exist_ok=True)
        path = journal_dir / f"{trading_date}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short telemetry write")
                view = view[written:]
        finally:
            os.close(fd)


def deliver_with_observability(
    send: Callable[[str], Any],
    observer: Optional[AlertTimingObserver],
    message: str,
    events: Iterable[Mapping[str, Any]],
) -> Any:
    """Observe one existing send without changing its result or exception behavior."""
    contexts = [dict(event) for event in events]
    batch_id = os.urandom(6).hex()
    started = time.monotonic()
    if observer is not None:
        observer.emit_many(
            {
                **event,
                "event_type": "DISCORD_ATTEMPTED",
                "event_timestamp": None,
                "context": {**dict(event.get("context") or {}), "alert_batch_id": batch_id},
            }
            for event in contexts
        )
    try:
        result = send(message)
    except Exception:
        if observer is not None:
            observer.emit_many(
                {
                    **event,
                    "event_type": "DISCORD_FAILED",
                    "event_timestamp": None,
                    "context": {
                        **dict(event.get("context") or {}),
                        "alert_batch_id": batch_id,
                        "delivery_duration_ms": round((time.monotonic() - started) * 1000.0, 3),
                        "failure_category": "send_exception",
                    },
                }
                for event in contexts
            )
        raise

    if observer is not None and result is not None:
        event_type = "DISCORD_SUCCESS" if result is True else "DISCORD_FAILED"
        outcome_context = {
            "alert_batch_id": batch_id,
            "delivery_duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
        if event_type == "DISCORD_FAILED":
            outcome_context["failure_category"] = "send_returned_false"
        observer.emit_many(
            {
                **event,
                "event_type": event_type,
                "event_timestamp": None,
                "context": {**dict(event.get("context") or {}), **outcome_context},
            }
            for event in contexts
        )
    return result

"""Isolated human-review publisher for existing Leader Ignition PASS rows.

This module does not import SignalMonitor and never writes scanner state.  Its
only input is the append-only Leader Ignition audit CSV; all writes are kept
below the caller-provided publisher state directory.
"""

from __future__ import annotations

import csv
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from pkscreener.classes.NGP5EarlyWatchPublisher import (
    DeliveryResult,
    DiscordWebhookTransport,
    validate_webhook_url,
)


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCHEMA_VERSION = 1
POLICY_VERSION = "existing_leader_ignition_pass_v1"
DEDUPE_VERSION = "first_leader_ignition_pass_per_symbol_session_v1"
SOURCE_LANE = "leader_ignition"
SOURCE_CHECKPOINT_BYTES = 1024

REQUIRED_COLUMNS = {
    "When",
    "Stage",
    "Decision",
    "Reason",
    "Symbol",
    "PriceNow",
    "OpenMovePct",
    "DayChangePct",
    "PullbackPct",
    "BuyerSellerRatio",
    "Volume",
    "VolumeDelta",
    "VolumeDeltaPct",
    "TradedValue",
}


def _now_ist() -> dt.datetime:
    return dt.datetime.now(tz=IST)


def _parse_timestamp(value: Any) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_int(value: Any) -> Optional[int]:
    number = _finite_float(value)
    return int(number) if number is not None else None


def _display(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _finite_float(value)
    return "UNKNOWN" if number is None else f"{number:.{digits}f}{suffix}"


def _valid_session_date(value: str) -> str:
    parsed = dt.date.fromisoformat(str(value))
    return parsed.isoformat()


def deterministic_event_id(session_date: str, symbol: str) -> str:
    """Stable identity for the first Leader Ignition PASS per symbol/session."""
    material = f"ng_quality_watch_v1|{SOURCE_LANE}|{session_date}|{symbol.strip().upper()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def source_row_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def tradingview_link(symbol: str) -> str:
    ticker = urllib.parse.quote(f"NSE:{symbol.strip().upper()}", safe="")
    return f"https://www.tradingview.com/chart/?symbol={ticker}"


def build_quality_watch_message(event: Mapping[str, Any]) -> str:
    """Render informational-only Discord content from immutable causal fields."""
    fields = event.get("causal_fields") or {}
    symbol = str(event.get("symbol") or "UNKNOWN")
    volume = _finite_int(fields.get("Volume"))
    volume_text = str(volume) if volume is not None else "UNKNOWN"
    volume_delta = _finite_int(fields.get("VolumeDelta"))
    volume_delta_text = str(volume_delta) if volume_delta is not None else "UNKNOWN"
    turnover = _finite_float(fields.get("TradedValue"))
    turnover_text = "UNKNOWN" if turnover is None else f"₹{turnover / 10_000_000:.2f} Cr"
    return (
        "🟡 **NG QUALITY WATCH**\n"
        "**NOT A BUY SIGNAL**\n\n"
        "Source: **OPENING LEADER**\n"
        f"Symbol: **`{symbol}`**\n"
        f"Qualified: {event.get('qualification_timestamp') or 'UNKNOWN'} IST\n\n"
        f"LTP: ₹{_display(fields.get('PriceNow'))}\n"
        f"Day: {_display(fields.get('DayChangePct'), 3, '%')}\n"
        f"From open: {_display(fields.get('OpenMovePct'), 3, '%')}\n"
        f"Turnover: {turnover_text}\n"
        f"Volume / volume delta: {volume_text} / {volume_delta_text} "
        f"({_display(fields.get('VolumeDeltaPct'), 3, '%')})\n"
        f"B/S: {_display(fields.get('BuyerSellerRatio'), 3)}\n"
        f"Pullback: {_display(fields.get('PullbackPct'), 3, '%')}\n"
        "Rank: UNKNOWN\n\n"
        "Reason: **Leader Ignition PASS**\n"
        f"[Open TradingView]({tradingview_link(symbol)})\n"
        f"Event ID: `{event.get('event_id') or 'UNKNOWN'}`"
    )


@dataclass(frozen=True)
class SourceReadResult:
    observed: int = 0
    quarantined: bool = False


class NGLeaderIgnitionWatchPublisher:
    """Tail Leader Ignition audit rows and durably deliver first PASS events."""

    def __init__(
        self,
        source_dir: Path | str,
        state_dir: Path | str,
        *,
        transport: Optional[Any] = None,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 5.0,
        clock: Callable[[], dt.datetime] = _now_ist,
    ) -> None:
        self.source_dir = Path(source_dir)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        self.evidence_dir = self.state_dir / "evidence"
        self.transport = transport
        self.max_attempts = max(1, int(max_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.clock = clock
        self.state = self._load_state()
        self._state_dirty = False

    def _empty_state(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "dedupe_version": DEDUPE_VERSION,
            "sources": {},
            "sessions": {},
            "pending": {},
        }

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Leader Watch state cannot be read safely: {type(exc).__name__}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("Leader Watch state schema is incompatible")
        if value.get("policy_version") != POLICY_VERSION or value.get("dedupe_version") != DEDUPE_VERSION:
            raise RuntimeError("Leader Watch state policy identity is incompatible")
        for key in ("sources", "sessions", "pending"):
            if not isinstance(value.get(key), dict):
                raise RuntimeError(f"Leader Watch state field {key} is invalid")
        return value

    def _save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".state.",
            suffix=".tmp",
            dir=self.state_dir,
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(self.state, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            self._state_dirty = False
        finally:
            if temporary.exists():
                temporary.unlink()

    def _save_if_dirty(self) -> None:
        if self._state_dirty:
            self._save_state()

    def _session(self, day: str) -> Dict[str, Any]:
        sessions = self.state["sessions"]
        if day not in sessions:
            sessions[day] = {"observed": {}, "baseline_seen_pass_symbols": {}}
            self._state_dirty = True
        session = sessions[day]
        if not isinstance(session.get("observed"), dict):
            raise RuntimeError("Leader Watch observed-symbol state is invalid")
        if "baseline_seen_pass_symbols" not in session:
            session["baseline_seen_pass_symbols"] = {}
            self._state_dirty = True
        if not isinstance(session.get("baseline_seen_pass_symbols"), dict):
            raise RuntimeError("Leader Watch baseline PASS state is invalid")
        return session

    def _append_evidence(self, day: str, record: Mapping[str, Any]) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": self.clock().astimezone(IST).isoformat(),
            **dict(record),
        }
        path = self.evidence_dir / f"events_{day}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _quarantine(self, day: str, source_key: str, source: Dict[str, Any], reason: str) -> None:
        source["quarantined"] = True
        source["quarantine_reason"] = reason
        self._state_dirty = True
        self._append_evidence(
            day,
            {
                "event_kind": "operational",
                "delivery_state": "SOURCE_QUARANTINED",
                "source_path": source_key,
                "reason": reason,
                "terminal": True,
            },
        )
        self._save_state()

    @staticmethod
    def _read_header(path: Path) -> tuple[list[str], int]:
        with path.open("rb") as handle:
            raw = handle.readline()
        if not raw.endswith(b"\n"):
            raise ValueError("source header is partial")
        try:
            values = next(csv.reader([raw.decode("utf-8-sig").rstrip("\r\n")]))
        except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
            raise ValueError("source header is malformed") from exc
        if len(values) != len(set(values)) or not REQUIRED_COLUMNS.issubset(values):
            raise ValueError("source header is incompatible")
        return values, len(raw)

    @staticmethod
    def _checkpoint(path: Path, offset: int) -> Dict[str, Any]:
        """Fingerprint bytes immediately before the cursor to detect rewrites."""
        start = max(0, int(offset) - SOURCE_CHECKPOINT_BYTES)
        length = int(offset) - start
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(length)
        if len(raw) != length:
            raise OSError("source checkpoint is shorter than the durable cursor")
        return {
            "checkpoint_start": start,
            "checkpoint_length": length,
            "checkpoint_sha256": source_row_hash(raw),
        }

    def _set_checkpoint(self, path: Path, source: Dict[str, Any]) -> None:
        checkpoint = self._checkpoint(path, int(source.get("offset", 0)))
        if any(source.get(key) != value for key, value in checkpoint.items()):
            source.update(checkpoint)
            self._state_dirty = True

    @staticmethod
    def _checkpoint_matches(path: Path, source: Mapping[str, Any]) -> bool:
        expected_start = source.get("checkpoint_start")
        expected_length = source.get("checkpoint_length")
        expected_hash = source.get("checkpoint_sha256")
        if not isinstance(expected_start, int) or not isinstance(expected_length, int):
            return False
        if not isinstance(expected_hash, str) or expected_start < 0 or expected_length < 0:
            return False
        with path.open("rb") as handle:
            handle.seek(expected_start)
            raw = handle.read(expected_length)
        return len(raw) == expected_length and source_row_hash(raw) == expected_hash

    def _initial_source(self, path: Path, day: str) -> Optional[Dict[str, Any]]:
        key = str(path.resolve())
        source = self.state["sources"].get(key)
        if source is not None:
            return source
        if not path.exists():
            source = {
                "session_date": day,
                "awaiting_creation": True,
                "device": None,
                "inode": None,
                "offset": 0,
                "header": None,
                "prearm_partial": None,
                "quarantined": False,
            }
            self.state["sources"][key] = source
            self._state_dirty = True
            self._save_state()
            return source
        try:
            header, baseline_offset, baseline_seen, prearm_partial = self._scan_initial_baseline(path, day)
            stat = path.stat()
        except (OSError, ValueError) as exc:
            source = {
                "session_date": day,
                "awaiting_creation": False,
                "device": None,
                "inode": None,
                "offset": 0,
                "header": None,
                "prearm_partial": None,
                "quarantined": True,
                "quarantine_reason": str(exc),
            }
            self.state["sources"][key] = source
            self._state_dirty = True
            self._append_evidence(
                day,
                {
                    "event_kind": "operational",
                    "delivery_state": "SOURCE_QUARANTINED",
                    "source_path": key,
                    "reason": str(exc),
                    "terminal": True,
                },
            )
            self._save_state()
            return source
        session = self._session(day)
        baseline_state = session["baseline_seen_pass_symbols"]
        for symbol, provenance in baseline_seen.items():
            baseline_state.setdefault(symbol, provenance)
        if prearm_partial is not None:
            prearm_partial.update(
                {
                    "source_path": key,
                    "session_date": day,
                    "armed_device": int(stat.st_dev),
                    "armed_inode": int(stat.st_ino),
                    "armed_at": self.clock().astimezone(IST).isoformat(),
                }
            )
        # Live-only bootstrap: complete pre-arm rows are permanently suppressed,
        # while an incomplete final row retains durable pre-arm provenance.
        source = {
            "session_date": day,
            "awaiting_creation": False,
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "offset": int(baseline_offset),
            "header": header,
            "prearm_partial": prearm_partial,
            "quarantined": False,
        }
        self._set_checkpoint(path, source)
        self.state["sources"][key] = source
        self._state_dirty = True
        self._append_evidence(
            day,
            {
                "event_kind": "operational",
                "delivery_state": "LIVE_BASELINE_ESTABLISHED",
                "source_path": key,
                "byte_offset": int(baseline_offset),
                "baseline_seen_pass_count": len(baseline_seen),
                "source_size": int(stat.st_size),
                "partial_suffix_pending": int(baseline_offset) < int(stat.st_size),
                "reason": "complete pre-arm PASS rows were durably suppressed; partial suffix remains pending",
                "terminal": False,
            },
        )
        self._save_state()
        return source

    def _scan_initial_baseline(
        self,
        path: Path,
        day: str,
    ) -> tuple[list[str], int, Dict[str, Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Classify complete pre-arm PASS rows without making them deliverable."""
        header, header_end = self._read_header(path)
        offset = header_end
        seen: Dict[str, Dict[str, Any]] = {}
        prearm_partial: Optional[Dict[str, Any]] = None
        with path.open("rb") as handle:
            handle.seek(header_end)
            while True:
                start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # The row began before arming. Retain both its starting byte
                    # and content identity so its eventual completion remains
                    # pre-arm without suppressing unrelated replacement data.
                    offset = start
                    prearm_partial = {
                        "byte_offset_start": start,
                        "prefix_length": len(raw),
                        "prefix_sha256": source_row_hash(raw),
                    }
                    break
                end = handle.tell()
                try:
                    text = raw.decode("utf-8").rstrip("\r\n")
                    values = next(csv.reader([text]))
                except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
                    raise ValueError("source row is malformed during live baseline") from exc
                if len(values) != len(header):
                    raise ValueError("source row width does not match header during live baseline")
                row = dict(zip(header, values))
                if str(row.get("Decision") or "").strip().lower() == "pass":
                    symbol = str(row.get("Symbol") or "").strip().upper()
                    timestamp = _parse_timestamp(row.get("When"))
                    if not symbol or timestamp is None or timestamp.date().isoformat() != day:
                        raise ValueError("baseline PASS row has invalid symbol or session timestamp")
                    seen.setdefault(
                        symbol,
                        {
                            "qualification_timestamp": timestamp.isoformat(),
                            "stage": str(row.get("Stage") or "").strip(),
                            "source_record": {
                                "path": str(path.resolve()),
                                "byte_offset_start": start,
                                "byte_offset_end": end,
                                "row_sha256": source_row_hash(raw),
                            },
                        },
                    )
                offset = end
        return header, offset, seen, prearm_partial

    @staticmethod
    def _prearm_partial_matches(path: Path, source: Mapping[str, Any], day: str) -> bool:
        marker = source.get("prearm_partial")
        if marker is None:
            return True
        if not isinstance(marker, Mapping):
            return False
        start = marker.get("byte_offset_start")
        length = marker.get("prefix_length")
        expected_hash = marker.get("prefix_sha256")
        if marker.get("source_path") != str(path.resolve()) or marker.get("session_date") != day:
            return False
        if not isinstance(start, int) or not isinstance(length, int) or start < 0 or length <= 0:
            return False
        if not isinstance(expected_hash, str) or int(source.get("offset", -1)) != start:
            return False
        with path.open("rb") as handle:
            handle.seek(start)
            prefix = handle.read(length)
        return len(prefix) == length and source_row_hash(prefix) == expected_hash

    def _reset_for_replacement(self, path: Path, source: Dict[str, Any]) -> None:
        header, header_end = self._read_header(path)
        stat = path.stat()
        source.update(
            {
                "awaiting_creation": False,
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
                "offset": header_end,
                "header": header,
                "prearm_partial": None,
                "quarantined": False,
                "quarantine_reason": None,
            }
        )
        self._set_checkpoint(path, source)

    def _event_from_row(
        self,
        row: Mapping[str, str],
        raw: bytes,
        path: Path,
        start: int,
        end: int,
        day: str,
    ) -> Optional[Dict[str, Any]]:
        if str(row.get("Decision") or "").strip().lower() != "pass":
            return None
        symbol = str(row.get("Symbol") or "").strip().upper()
        timestamp = _parse_timestamp(row.get("When"))
        if not symbol or timestamp is None or timestamp.date().isoformat() != day:
            raise ValueError("PASS row has invalid symbol or session timestamp")
        event_id = deterministic_event_id(day, symbol)
        return {
            "kind": "leader_ignition",
            "event_id": event_id,
            "delivery_id": event_id,
            "source_lane": SOURCE_LANE,
            "source_label": "OPENING LEADER",
            "policy_version": POLICY_VERSION,
            "dedupe_version": DEDUPE_VERSION,
            "session_date": day,
            "symbol": symbol,
            "qualification_timestamp": timestamp.isoformat(),
            "qualification_reason": "Leader Ignition PASS",
            "stage": str(row.get("Stage") or "").strip(),
            "causal_fields": dict(row),
            "source_record": {
                "path": str(path.resolve()),
                "byte_offset_start": start,
                "byte_offset_end": end,
                "row_sha256": source_row_hash(raw),
            },
        }

    def process_source_file(self, path: Path | str, session_date: str) -> SourceReadResult:
        day = _valid_session_date(session_date)
        path = Path(path)
        source_key = str(path.resolve())
        source = self._initial_source(path, day)
        if source is None or not path.exists():
            return SourceReadResult()
        try:
            stat = path.stat()
            replaced = (
                source.get("awaiting_creation")
                or source.get("device") != int(stat.st_dev)
                or source.get("inode") != int(stat.st_ino)
                or int(stat.st_size) < int(source.get("offset", 0))
                or not self._checkpoint_matches(path, source)
                or not self._prearm_partial_matches(path, source, day)
            )
            if replaced:
                self._reset_for_replacement(path, source)
                self._save_state()
            elif source.get("quarantined"):
                return SourceReadResult(quarantined=True)
        except (OSError, ValueError) as exc:
            self._quarantine(day, source_key, source, str(exc))
            return SourceReadResult(quarantined=True)

        header = source.get("header")
        if not isinstance(header, list):
            self._quarantine(day, source_key, source, "source header state is unavailable")
            return SourceReadResult(quarantined=True)

        observed = 0
        with path.open("rb") as handle:
            handle.seek(int(source.get("offset", 0)))
            while True:
                start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    handle.seek(start)
                    break
                end = handle.tell()
                marker = source.get("prearm_partial")
                prearm_partial = isinstance(marker, Mapping) and start == marker.get("byte_offset_start")
                if prearm_partial:
                    prefix_length = marker.get("prefix_length")
                    prefix_hash = marker.get("prefix_sha256")
                    if (
                        not isinstance(prefix_length, int)
                        or prefix_length <= 0
                        or source_row_hash(raw[:prefix_length]) != prefix_hash
                    ):
                        self._reset_for_replacement(path, source)
                        self._save_state()
                        return SourceReadResult(observed=observed)
                try:
                    text = raw.decode("utf-8").rstrip("\r\n")
                    values = next(csv.reader([text]))
                except (UnicodeDecodeError, csv.Error, StopIteration):
                    self._quarantine(day, source_key, source, "source row is malformed")
                    return SourceReadResult(observed=observed, quarantined=True)
                if len(values) != len(header):
                    self._quarantine(day, source_key, source, "source row width does not match header")
                    return SourceReadResult(observed=observed, quarantined=True)
                row = dict(zip(header, values))
                try:
                    event = self._event_from_row(row, raw, path, start, end, day)
                except ValueError as exc:
                    self._quarantine(day, source_key, source, str(exc))
                    return SourceReadResult(observed=observed, quarantined=True)
                source["offset"] = end
                self._state_dirty = True
                if prearm_partial:
                    source["prearm_partial"] = None
                    if event is not None:
                        session = self._session(day)
                        session["baseline_seen_pass_symbols"].setdefault(
                            event["symbol"],
                            {
                                "qualification_timestamp": event["qualification_timestamp"],
                                "stage": event["stage"],
                                "source_record": event["source_record"],
                            },
                        )
                    continue
                if event is None:
                    continue
                session = self._session(day)
                symbol = event["symbol"]
                if (
                    symbol in session["observed"]
                    or symbol in session["baseline_seen_pass_symbols"]
                ):
                    continue
                session["observed"][symbol] = event
                self.state["pending"][event["event_id"]] = {
                    "event": event,
                    "message": build_quality_watch_message(event),
                    "attempts": 0,
                    "next_attempt_at": 0.0,
                    "last_delivery_state": "OBSERVED",
                }
                self._append_evidence(
                    day,
                    {
                        "event_id": event["event_id"],
                        "delivery_id": event["event_id"],
                        "event_kind": "leader_ignition",
                        "delivery_state": "OBSERVED",
                        "delivery_attempts": 0,
                        "http_status": None,
                        "delivered_timestamp": None,
                        "terminal": False,
                        "event": event,
                    },
                )
                observed += 1
        self._set_checkpoint(path, source)
        self._save_if_dirty()
        return SourceReadResult(observed=observed)

    def expire_stale_pending(self, current_session: str) -> int:
        current = _valid_session_date(current_session)
        expired = 0
        for delivery_id in list(self.state["pending"]):
            pending = self.state["pending"][delivery_id]
            event = pending.get("event") or {}
            original = str(event.get("session_date") or "")
            if not original or original >= current:
                continue
            self._append_evidence(
                original,
                {
                    "event_id": event.get("event_id") or delivery_id,
                    "delivery_id": delivery_id,
                    "event_kind": "leader_ignition",
                    "delivery_state": "EXPIRED_STALE_SESSION",
                    "delivery_attempts": int(pending.get("attempts", 0)),
                    "http_status": None,
                    "delivered_timestamp": None,
                    "original_session": original,
                    "expiry_timestamp": self.clock().astimezone(IST).isoformat(),
                    "reason": "pending event belongs to a prior IST trading session",
                    "terminal": True,
                },
            )
            self.state["pending"].pop(delivery_id, None)
            self._state_dirty = True
            expired += 1
        self._save_if_dirty()
        return expired

    def deliver_pending(self, current_session: str) -> int:
        current = _valid_session_date(current_session)
        today = self.clock().astimezone(IST).date().isoformat()
        if current != today:
            self.expire_stale_pending(today)
            return 0
        self.expire_stale_pending(current)
        if self.transport is None:
            return 0
        delivered = 0
        now = self.clock().astimezone(IST)
        for delivery_id in list(self.state["pending"]):
            pending = self.state["pending"][delivery_id]
            event = pending.get("event") or {}
            if event.get("session_date") != current:
                continue
            if float(pending.get("next_attempt_at", 0.0)) > now.timestamp():
                continue
            attempts = int(pending.get("attempts", 0))
            if attempts >= self.max_attempts:
                self._terminal_failure(event, delivery_id, attempts, None, "retry_budget_exhausted")
                continue
            attempt = attempts + 1
            pending["attempts"] = attempt
            pending["last_delivery_state"] = "ATTEMPTED"
            self._state_dirty = True
            self._append_evidence(
                current,
                {
                    "event_id": event["event_id"],
                    "delivery_id": delivery_id,
                    "event_kind": "leader_ignition",
                    "delivery_state": "ATTEMPTED",
                    "delivery_attempts": attempt,
                    "attempt_number": attempt,
                    "attempt_timestamp": now.isoformat(),
                    "http_status": None,
                    "delivered_timestamp": None,
                    "terminal": False,
                },
            )
            self._save_state()
            try:
                result = self.transport.post(str(pending["message"]))
                if not isinstance(result, DeliveryResult):
                    raise TypeError("transport returned non-DeliveryResult")
            except Exception as exc:  # transport failure must not escape into scanner/runtime
                result = DeliveryResult(
                    False,
                    None,
                    True,
                    error=f"unexpected_transport_exception:{type(exc).__name__}",
                )
            if result.delivered:
                delivered_at = self.clock().astimezone(IST).isoformat()
                self._append_evidence(
                    current,
                    {
                        "event_id": event["event_id"],
                        "delivery_id": delivery_id,
                        "event_kind": "leader_ignition",
                        "delivery_state": "DELIVERED",
                        "delivery_attempts": attempt,
                        "attempt_number": attempt,
                        "http_status": result.http_status,
                        "delivered_timestamp": delivered_at,
                        "qualification_timestamp": event.get("qualification_timestamp"),
                        "terminal": True,
                    },
                )
                self.state["pending"].pop(delivery_id, None)
                self._state_dirty = True
                self._save_state()
                delivered += 1
                continue
            terminal = (not result.retryable) or attempt >= self.max_attempts
            if terminal:
                self._terminal_failure(
                    event,
                    delivery_id,
                    attempt,
                    result.http_status,
                    result.error or "transport_failure",
                )
                continue
            delay = (
                max(0.0, float(result.retry_after_seconds))
                if result.retry_after_seconds is not None
                else self.retry_backoff_seconds * (2 ** (attempt - 1))
            )
            pending["next_attempt_at"] = now.timestamp() + delay
            pending["last_delivery_state"] = "FAILED"
            self._append_evidence(
                current,
                {
                    "event_id": event["event_id"],
                    "delivery_id": delivery_id,
                    "event_kind": "leader_ignition",
                    "delivery_state": "FAILED",
                    "delivery_attempts": attempt,
                    "attempt_number": attempt,
                    "http_status": result.http_status,
                    "failure": result.error,
                    "retryable": True,
                    "retry_after_seconds": delay,
                    "delivered_timestamp": None,
                    "terminal": False,
                },
            )
            self._state_dirty = True
            self._save_state()
        return delivered

    def _terminal_failure(
        self,
        event: Mapping[str, Any],
        delivery_id: str,
        attempts: int,
        http_status: Optional[int],
        error: str,
    ) -> None:
        day = str(event.get("session_date") or self.clock().astimezone(IST).date().isoformat())
        self._append_evidence(
            day,
            {
                "event_id": event.get("event_id") or delivery_id,
                "delivery_id": delivery_id,
                "event_kind": "leader_ignition",
                "delivery_state": "FAILED",
                "delivery_attempts": attempts,
                "http_status": http_status,
                "failure": error,
                "failure_category": str(error).split(":", 1)[0],
                "retryable": False,
                "delivered_timestamp": None,
                "terminal": True,
            },
        )
        self.state["pending"].pop(delivery_id, None)
        self._state_dirty = True
        self._save_state()

    def process_session(self, session_date: str) -> Dict[str, int]:
        day = _valid_session_date(session_date)
        today = self.clock().astimezone(IST).date().isoformat()
        if day != today:
            expired = self.expire_stale_pending(today)
            return {"observed": 0, "delivered": 0, "expired": expired, "quarantined": 0}
        expired = self.expire_stale_pending(day)
        result = self.process_source_file(
            self.source_dir / f"leader_ignition_{day}.csv",
            day,
        )
        delivered = self.deliver_pending(day)
        return {
            "observed": result.observed,
            "delivered": delivered,
            "expired": expired,
            "quarantined": int(result.quarantined),
        }


class PublisherLock:
    """Non-blocking process lock scoped to one publisher state directory."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.handle: Optional[Any] = None

    def __enter__(self) -> "PublisherLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


__all__ = [
    "DEDUPE_VERSION",
    "DeliveryResult",
    "DiscordWebhookTransport",
    "NGLeaderIgnitionWatchPublisher",
    "POLICY_VERSION",
    "PublisherLock",
    "SourceReadResult",
    "build_quality_watch_message",
    "deterministic_event_id",
    "validate_webhook_url",
]

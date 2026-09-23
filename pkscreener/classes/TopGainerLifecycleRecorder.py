"""Paper-only, append-only lifecycle evidence for top-gainer research.

Observations are immutable facts available at a timestamp.  Future outcomes
are separate enrichment events and are never returned by the live-observation
API.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback remains single-process.
    fcntl = None


SCHEMA_VERSION = 1
IST = ZoneInfo("Asia/Kolkata")
EVENT_TYPES = {
    "candidate_observed", "candidate_ranked", "rank_changed", "alert_emitted",
    "confirmation_armed", "confirmation_passed", "confirmation_failed",
    "paper_entry_assumed", "paper_entry_rejected", "paper_exit",
    "session_outcome_finalized", "data_incomplete", "lane_unavailable",
}
SIGNAL_FIELDS = {
    "trading_date", "symbol", "observation_timestamp", "feature_available_at",
    "source_lane", "experiment_version", "universe_id", "current_price",
    "previous_close", "day_open", "return_from_previous_close",
    "return_from_open", "gap_pct", "day_high_known", "pullback_pct",
    "volume", "relative_volume", "volume_acceleration", "traded_value",
    "bid", "ask", "spread", "buy_quantity", "sell_quantity",
    "order_book_ratio", "order_book_stability", "vwap", "vwap_distance",
    "opening_range_position", "sector", "sector_strength", "benchmark_strength",
    "current_rank", "rank_percentile", "previous_rank", "rank_improvement",
    "rank_velocity", "liquidity_state", "surveillance_health", "asm_gsm_state",
    "stale_data", "source_watermark", "missing_reasons", "qualification_reason",
    "first_eligible_time", "first_eligible_price", "queue_selected_time",
    "queue_selected_price", "analysis_completed_time", "qualified_time",
    "qualified_price", "source_exchange_timestamp", "tick_timestamp",
    "warning_flags", "multi_lane", "multi_lane_sources",
}


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=IST)
        return dt.astimezone(IST).isoformat()
    raw = str(value or "")
    if not raw:
        return raw
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST).isoformat(timespec="microseconds")
    except ValueError:
        return raw


def _timestamp(value: Any, label: str) -> datetime:
    """Return a strictly validated timestamp normalized to IST."""
    normalized = _iso(value)
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if parsed.tzinfo is None:  # Defensive: _iso normally attaches IST.
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def build_linked_event(observation: dict[str, Any], event_type: str,
                       event_timestamp: Any, source_lane: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
    """Build, but do not persist, an event linked to one observation."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported lifecycle event: {event_type}")
    lifecycle_id = str(observation.get("lifecycle_id") or "")
    if not lifecycle_id:
        raise ValueError("observation lifecycle_id is required")
    event_dt = _timestamp(event_timestamp, "event timestamp")
    observation_dt = _timestamp(observation.get("observation_timestamp"), "observation timestamp")
    ts = event_dt.isoformat(timespec="microseconds")
    observation_ts = observation_dt.isoformat(timespec="microseconds")
    if event_dt < observation_dt:
        raise ValueError("lifecycle event cannot precede observation")
    event_id = hashlib.sha256(
        json.dumps([lifecycle_id, event_type, ts, payload], sort_keys=True, default=str).encode()
    ).hexdigest()[:32]
    return {
        "schema_version": SCHEMA_VERSION, "event_id": event_id,
        "event_type": event_type, "lifecycle_id": lifecycle_id,
        "observation_id": lifecycle_id,
        "observation_timestamp": observation.get("observation_timestamp"),
        "experiment_version": observation.get("experiment_version"),
        "origin_source_lane": observation.get("source_lane"),
        "event_timestamp": ts, "source_lane": str(source_lane or "unknown"),
        "payload": payload, "signal_time_fields": None,
        "future_labels": payload if event_type == "session_outcome_finalized" else None,
        "paper_only": True,
        "partition": "historical" if event_type == "session_outcome_finalized" else "current_day",
    }


def calculate_outcome_labels(observation: dict[str, Any],
                             bars: Iterable[dict[str, Any]]) -> tuple[str, dict[str, Any], Optional[str]]:
    """Calculate causal labels from local bars without persistence or I/O."""
    def incomplete_labels() -> dict[str, Any]:
        labels = {
            "completeness_status": "incomplete",
            "later_intraday_high": None, "later_intraday_low": None,
            "mfe_pct": None, "remaining_upside_pct": None, "mae_pct": None,
            "time_to_later_high_minutes": None,
        }
        labels.update({f"return_{minutes}m_pct": None for minutes in (15, 30, 60, 120)})
        for threshold in (3, 5, 10):
            labels[f"reached_{threshold}pct"] = None
            labels[f"first_{threshold}pct_timestamp"] = None
        return labels

    observation_dt = _timestamp(observation.get("observation_timestamp"), "observation timestamp")
    observation_ts = observation_dt.isoformat(timespec="microseconds")
    trading_date = str(observation.get("trading_date") or observation_dt.date().isoformat())
    if trading_date != observation_dt.date().isoformat():
        raise ValueError("observation trading_date does not match observation timestamp")
    session_first_bar = observation_dt.replace(hour=9, minute=15, second=0, microsecond=0)
    session_last_bar = observation_dt.replace(hour=15, minute=29, second=0, microsecond=0)
    expected_first_bar = (
        session_first_bar if observation_dt < session_first_bar
        else observation_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    )
    ordered = []
    for bar in bars or []:
        try:
            bar_dt = _timestamp(
                bar.get("timestamp") or bar.get("When") or bar.get("datetime"),
                "bar timestamp",
            )
        except ValueError:
            continue
        if (bar_dt.date() != observation_dt.date() or bar_dt < session_first_bar
                or bar_dt <= observation_dt
                or bar_dt > session_last_bar):
            continue
        try:
            high = float(bar.get("high"))
            low = float(bar.get("low"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(high) or not math.isfinite(low):
            continue
        ordered.append((bar_dt, bar))
    ordered.sort(key=lambda item: item[0])
    event_ts = ordered[-1][0] if ordered else observation_ts
    try:
        price = float((observation.get("signal_time_fields") or {}).get("current_price"))
    except (TypeError, ValueError):
        price = math.nan
    if not math.isfinite(price) or price <= 0 or not ordered:
        reason = "no_valid_post_observation_bars" if not ordered else "observation_price_missing"
        return _iso(event_ts), incomplete_labels(), reason

    def numeric_values(field: str) -> list[float]:
        values = []
        for _, row in ordered:
            value = row.get(field)
            if value in (None, ""):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                values.append(number)
        return values

    highs = numeric_values("high")
    lows = numeric_values("low")
    closes = numeric_values("close")
    # Indian cash-market minute backfills normally end with the 15:29 candle.
    # Anything shorter is conservatively incomplete: absence of a later move
    # must never be converted into a negative outcome.
    unique_times = sorted({timestamp for timestamp, _ in ordered})
    coverage_issues = []
    if unique_times[0] != expected_first_bar:
        coverage_issues.append("first_expected_minute_missing")
    if any(later - earlier != timedelta(minutes=1)
           for earlier, later in zip(unique_times, unique_times[1:])):
        coverage_issues.append("internal_minute_gap")
    if unique_times[-1] != session_last_bar:
        coverage_issues.append("session_ended_before_15_29")
    if len(unique_times) != len(ordered):
        coverage_issues.append("duplicate_bar_timestamp")
    if len(closes) != len(ordered):
        coverage_issues.append("invalid_or_missing_close")
    complete = bool(
        highs and lows
        and len(highs) == len(ordered) and len(lows) == len(ordered)
        and not coverage_issues
    )
    labels: dict[str, Any] = {"completeness_status": "complete" if complete else "incomplete"}
    if coverage_issues:
        labels["coverage_issues"] = coverage_issues
    outcome_fields = (
        "later_intraday_high", "later_intraday_low", "mfe_pct",
        "remaining_upside_pct", "mae_pct", "time_to_later_high_minutes",
    )
    labels.update({field: None for field in outcome_fields})
    if complete:
        labels.update({
            "later_intraday_high": max(highs),
            "later_intraday_low": min(lows),
            "mfe_pct": ((max(highs) / price) - 1.0) * 100.0,
            "remaining_upside_pct": ((max(highs) / price) - 1.0) * 100.0,
            "mae_pct": ((min(lows) / price) - 1.0) * 100.0,
        })
    if complete:
        high_rows = []
        for index, (_, row) in enumerate(ordered):
            try:
                high = float(row["high"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(high):
                high_rows.append((index, high))
        high_index = max(high_rows, key=lambda item: item[1])[0]
        labels["time_to_later_high_minutes"] = round(
            (ordered[high_index][0] - observation_dt).total_seconds() / 60.0, 4
        )
    for minutes in (15, 30, 60, 120):
        cutoff = observation_dt + timedelta(minutes=minutes)
        eligible = [(ts, row) for ts, row in ordered if ts <= cutoff and row.get("close") not in (None, "")]
        try:
            close = float(eligible[-1][1]["close"]) if (
                complete and eligible and cutoff <= session_last_bar
                and cutoff - eligible[-1][0] < timedelta(minutes=1)
            ) else None
        except (TypeError, ValueError):
            close = None
        if close is not None and not math.isfinite(close):
            close = None
        labels[f"return_{minutes}m_pct"] = ((close / price) - 1.0) * 100.0 if close is not None else None
    for threshold in (3, 5, 10):
        crossing = None
        for ts, row in ordered:
            try:
                high = float(row["high"])
                reached = math.isfinite(high) and ((high / price) - 1.0) * 100.0 >= threshold
            except (KeyError, TypeError, ValueError):
                reached = False
            if reached:
                crossing = ts
                break
        labels[f"reached_{threshold}pct"] = crossing is not None if complete else None
        labels[f"first_{threshold}pct_timestamp"] = _iso(crossing) if complete and crossing else None
    missing_reason = None if complete else "post_observation_session_incomplete"
    return _iso(event_ts), labels, missing_reason


class TopGainerLifecycleRecorder:
    """Persist paper-only lifecycle events under a caller-provided evidence root."""

    def __init__(self, output_dir: str, experiment_version: str = "canonical_v1") -> None:
        self.root = Path(output_dir) / "top_gainer_lifecycle"
        self.root.mkdir(parents=True, exist_ok=True)
        self.experiment_version = str(experiment_version or "canonical_v1")
        self.events_path = self.root / "events.jsonl"
        self.derived_path = self.root / "joined_snapshot.csv"
        self._event_ids: set[str] = set()
        self._observations_by_id: dict[str, dict[str, Any]] = {}
        self._event_index_offset = 0
        self._event_index_inode: Optional[int] = None

    @staticmethod
    def lifecycle_id(trading_date: str, symbol: str, observation_timestamp: str,
                     source_lane: str, experiment_version: str) -> str:
        raw = "|".join((trading_date, symbol.upper(), observation_timestamp, source_lane, experiment_version))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @contextmanager
    def _file_lock(self):
        lock_path = self.root / ".events.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _refresh_event_index_locked(self) -> None:
        """Incrementally index durable events while holding the process lock."""
        if not self.events_path.exists():
            self._event_ids.clear()
            self._observations_by_id.clear()
            self._event_index_offset = 0
            self._event_index_inode = None
            return
        stat = self.events_path.stat()
        if (
            self._event_index_inode != stat.st_ino
            or stat.st_size < self._event_index_offset
        ):
            self._event_ids.clear()
            self._observations_by_id.clear()
            self._event_index_offset = 0
        self._event_index_inode = stat.st_ino
        with self.events_path.open("r", encoding="utf-8") as handle:
            handle.seek(self._event_index_offset)
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError("corrupt lifecycle JSONL record") from exc
                event_id = str(row.get("event_id", ""))
                if event_id:
                    self._event_ids.add(event_id)
                if row.get("event_type") == "candidate_observed":
                    lifecycle_id = str(row.get("lifecycle_id", ""))
                    if lifecycle_id:
                        self._observations_by_id[lifecycle_id] = row
            self._event_index_offset = handle.tell()

    def _append(self, event: dict[str, Any]) -> bool:
        event_json = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        with self._file_lock():
            self._refresh_event_index_locked()
            if event["event_id"] in self._event_ids:
                return False
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(event_json + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                self._event_index_offset = handle.tell()
                self._event_index_inode = os.fstat(handle.fileno()).st_ino
            self._event_ids.add(str(event["event_id"]))
            if event.get("event_type") == "candidate_observed":
                self._observations_by_id[str(event.get("lifecycle_id") or "")] = event
            return True

    def _observation(self, lifecycle_id: str) -> Optional[dict[str, Any]]:
        with self._file_lock():
            self._refresh_event_index_locked()
            return self._observations_by_id.get(str(lifecycle_id))

    def record_observation(self, snapshot: dict[str, Any], source_lane: str,
                           observation_timestamp: Any, universe_id: str = "",
                           experiment_version: Optional[str] = None) -> str:
        observation_dt = _timestamp(observation_timestamp, "observation timestamp")
        feature_dt = _timestamp(snapshot.get("feature_available_at") or observation_dt,
                                "feature availability timestamp")
        ts = observation_dt.isoformat(timespec="microseconds")
        feature_ts = feature_dt.isoformat(timespec="microseconds")
        if feature_dt > observation_dt:
            raise ValueError("feature availability cannot be after observation")
        symbol = str(snapshot.get("symbol") or snapshot.get("Symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("observation symbol is required")
        date = ts[:10]
        lane = str(source_lane or "unknown").strip() or "unknown"
        version = str(experiment_version or self.experiment_version)
        missing = list(snapshot.get("missing_reasons") or [])
        # Keep a stable normalized schema: unavailable measurements are null,
        # with the reason retained separately instead of silently disappearing.
        fields = {key: snapshot.get(key) for key in SIGNAL_FIELDS}
        fields["missing_reasons"] = missing
        fields["feature_available_at"] = feature_ts
        fields["trading_date"] = date
        fields["symbol"] = symbol
        fields["observation_timestamp"] = ts
        fields["source_lane"] = lane
        fields["experiment_version"] = version
        fields["universe_id"] = str(universe_id or "")
        lifecycle_id = self.lifecycle_id(date, symbol, ts, lane, version)
        event = {
            "schema_version": SCHEMA_VERSION, "event_id": hashlib.sha256(
                f"candidate_observed|{lifecycle_id}".encode()).hexdigest()[:32],
            "event_type": "candidate_observed", "lifecycle_id": lifecycle_id,
            "trading_date": date, "symbol": symbol,
            "observation_timestamp": ts, "event_timestamp": ts,
            "source_lane": lane, "experiment_version": version,
            "universe_id": str(universe_id or ""), "signal_time_fields": fields,
            "future_labels": None, "paper_only": True,
            "historical_reconstruction": bool(snapshot.get("historical_reconstruction", False)),
            "causal_evaluation_eligible": not bool(snapshot.get("historical_reconstruction", False)),
            "partition": "historical" if bool(snapshot.get("historical_reconstruction", False)) else "current_day",
        }
        self._append(event)
        return lifecycle_id

    def record_event(self, lifecycle_id: str, event_type: str, event_timestamp: Any,
                     source_lane: str, **payload: Any) -> str:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported lifecycle event: {event_type}")
        ts = _iso(event_timestamp)
        observation = self._observation(lifecycle_id)
        if observation is None:
            raise ValueError(f"unknown lifecycle observation: {lifecycle_id}")
        event = build_linked_event(observation, event_type, ts, source_lane, payload)
        self._append(event)
        return str(event["event_id"])

    def enrich_session(self, lifecycle_id: str, event_timestamp: Any, source_lane: str,
                       labels: dict[str, Any]) -> str:
        return self.record_event(lifecycle_id, "session_outcome_finalized", event_timestamp,
                                 source_lane, **dict(labels))

    def record_data_incomplete(self, event_timestamp: Any, source_lane: str,
                               symbol: str = "", reason: str = "") -> str:
        """Record an unlinked data-quality event without guessing an ID."""
        ts = _iso(event_timestamp)
        event_id = hashlib.sha256(
            f"data_incomplete|{source_lane}|{symbol}|{ts}|{reason}".encode("utf-8")
        ).hexdigest()[:32]
        self._append({
            "schema_version": SCHEMA_VERSION, "event_id": event_id,
            "event_type": "data_incomplete", "observation_id": None,
            "lifecycle_id": None, "event_timestamp": ts,
            "observation_timestamp": None,
            "origin_source_lane": None,
            "source_lane": str(source_lane or "unknown"),
            "experiment_version": self.experiment_version,
            "payload": {"symbol": symbol, "reason": reason},
            "signal_time_fields": None, "future_labels": None, "paper_only": True,
        })
        return event_id

    def enrich_from_bars(self, observation_id: str, bars: Iterable[dict[str, Any]],
                         source_lane: str = "eod_enrichment", **final_fields: Any) -> str:
        """Create causal end-of-session labels from bars strictly after observation.

        Bars are plain local dictionaries with ``timestamp`` and any available
        OHLC fields. No live or broker access is performed here.
        """
        observations = [row for row in self.replay()
                        if row.get("event_type") == "candidate_observed"
                        and row.get("lifecycle_id") == observation_id]
        if not observations:
            raise ValueError(f"unknown lifecycle observation: {observation_id}")
        observation = observations[0]
        event_ts, labels, missing_reason = calculate_outcome_labels(observation, bars)
        if missing_reason:
            incomplete_payload = {
                "completeness_status": "incomplete", "missing_reason": missing_reason,
            }
            if labels.get("coverage_issues"):
                incomplete_payload["coverage_issues"] = labels["coverage_issues"]
            self.record_event(observation_id, "data_incomplete", event_ts, source_lane,
                              **incomplete_payload)
        collisions = set(labels).intersection(final_fields)
        if collisions:
            raise ValueError(f"final_fields cannot override calculated labels: {sorted(collisions)}")
        labels.update(final_fields)
        return self.enrich_session(observation_id, event_ts, source_lane, labels)

    def replay(self) -> list[dict[str, Any]]:
        rows = []
        if not self.events_path.exists():
            return rows
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("corrupt lifecycle JSONL record") from exc
            if row.get("schema_version") != SCHEMA_VERSION or not row.get("event_id"):
                raise ValueError("invalid lifecycle schema")
            rows.append(row)
        return rows

    def derive_joined_snapshot(self) -> Path:
        with self._file_lock():
            all_events = self.replay()
            observations = {row["lifecycle_id"]: row for row in all_events
                            if row.get("event_type") == "candidate_observed"}
            labels = {}
            for row in all_events:
                if row.get("event_type") == "session_outcome_finalized":
                    labels[row["lifecycle_id"]] = row.get("future_labels") or {}
            fields = ["lifecycle_id", "trading_date", "symbol", "observation_timestamp",
                      "source_lane", "experiment_version", "universe_id", "signal_time_fields",
                      "future_labels"]
            lines = []
            for key, row in observations.items():
                lines.append({"lifecycle_id": key, "trading_date": row.get("trading_date"),
                              "symbol": row.get("symbol"), "observation_timestamp": row.get("observation_timestamp"),
                              "source_lane": row.get("source_lane"), "experiment_version": row.get("experiment_version"),
                              "universe_id": row.get("universe_id"),
                              "signal_time_fields": json.dumps(row.get("signal_time_fields") or {}, sort_keys=True),
                              "future_labels": json.dumps(labels.get(key), sort_keys=True) if key in labels else ""})
            import io
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fields)
            writer.writeheader(); writer.writerows(lines)
            _atomic_write(self.derived_path, buf.getvalue())
        return self.derived_path

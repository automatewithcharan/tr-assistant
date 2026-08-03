"""Paper-only, append-only lifecycle evidence for top-gainer research.

Observations are immutable facts available at a timestamp.  Future outcomes
are separate enrichment events and are never returned by the live-observation
API.
"""

from __future__ import annotations

import csv
import hashlib
import json
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


class TopGainerLifecycleRecorder:
    """Persist paper-only lifecycle events under a caller-provided evidence root."""

    def __init__(self, output_dir: str, experiment_version: str = "canonical_v1") -> None:
        self.root = Path(output_dir) / "top_gainer_lifecycle"
        self.root.mkdir(parents=True, exist_ok=True)
        self.experiment_version = str(experiment_version or "canonical_v1")
        self.events_path = self.root / "events.jsonl"
        self.derived_path = self.root / "joined_snapshot.csv"

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

    def _append(self, event: dict[str, Any]) -> bool:
        event_json = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        with self._file_lock():
            existing_ids = set()
            if self.events_path.exists():
                for row in self.events_path.read_text(encoding="utf-8").splitlines():
                    if not row.strip():
                        continue
                    try:
                        existing_ids.add(str(json.loads(row).get("event_id", "")))
                    except json.JSONDecodeError as exc:
                        raise ValueError("corrupt lifecycle JSONL record") from exc
            if event["event_id"] in existing_ids:
                return False
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(event_json + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True

    def record_observation(self, snapshot: dict[str, Any], source_lane: str,
                           observation_timestamp: Any, universe_id: str = "",
                           experiment_version: Optional[str] = None) -> str:
        ts = _iso(observation_timestamp)
        feature_ts = _iso(snapshot.get("feature_available_at") or ts)
        if feature_ts > ts:
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
        observations = [row for row in self.replay()
                        if row.get("event_type") == "candidate_observed"
                        and row.get("lifecycle_id") == lifecycle_id]
        if not observations:
            raise ValueError(f"unknown lifecycle observation: {lifecycle_id}")
        observation = observations[0]
        for prior in self.replay():
            if prior.get("lifecycle_id") == lifecycle_id and prior.get("event_type") == "candidate_observed":
                if ts < str(prior.get("observation_timestamp", "")):
                    raise ValueError("lifecycle event cannot precede observation")
                break
        event_id = hashlib.sha256(
            json.dumps([lifecycle_id, event_type, ts, payload], sort_keys=True, default=str).encode()
        ).hexdigest()[:32]
        event = {
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
        self._append(event)
        return event_id

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
        observation_ts = _iso(observation.get("observation_timestamp"))
        ordered = []
        for bar in bars or []:
            timestamp = _iso(bar.get("timestamp") or bar.get("When"))
            if timestamp and timestamp > observation_ts:
                ordered.append((timestamp, bar))
        ordered.sort(key=lambda item: item[0])
        event_ts = ordered[-1][0] if ordered else observation_ts
        price = float((observation.get("signal_time_fields") or {}).get("current_price") or 0.0)
        if price <= 0 or not ordered:
            self.record_event(observation_id, "data_incomplete", event_ts, source_lane,
                              completeness_status="incomplete",
                              missing_reason="no_valid_post_observation_bars" if not ordered else "observation_price_missing")
            return self.enrich_session(observation_id, event_ts, source_lane,
                                       completeness_status="incomplete", **final_fields)

        highs = [float(row.get("high")) for _, row in ordered if row.get("high") is not None]
        lows = [float(row.get("low")) for _, row in ordered if row.get("low") is not None]
        labels: dict[str, Any] = {
            "later_intraday_high": max(highs) if highs else None,
            "later_intraday_low": min(lows) if lows else None,
            "mfe_pct": ((max(highs) / price) - 1.0) * 100.0 if highs else None,
            "remaining_upside_pct": ((max(highs) / price) - 1.0) * 100.0 if highs else None,
            "mae_pct": ((min(lows) / price) - 1.0) * 100.0 if lows else None,
            "completeness_status": "complete" if highs and lows else "incomplete",
        }
        if highs:
            high_index = max(range(len(ordered)), key=lambda index: float(ordered[index][1].get("high") or 0.0))
            labels["time_to_later_high_minutes"] = round(
                (datetime.fromisoformat(ordered[high_index][0]) - datetime.fromisoformat(observation_ts)).total_seconds() / 60.0, 4
            )
        for minutes in (15, 30, 60, 120):
            cutoff = datetime.fromisoformat(observation_ts) + timedelta(minutes=minutes)
            eligible = [row for ts, row in ordered if datetime.fromisoformat(ts) <= cutoff and row.get("close") is not None]
            labels[f"return_{minutes}m_pct"] = ((float(eligible[-1]["close"]) / price) - 1.0) * 100.0 if eligible else None
        for threshold in (3, 5, 10):
            crossing = next(((ts, row) for ts, row in ordered
                             if row.get("high") is not None and ((float(row["high"]) / price) - 1.0) * 100.0 >= threshold), None)
            labels[f"reached_{threshold}pct"] = bool(crossing)
            labels[f"first_{threshold}pct_timestamp"] = crossing[0] if crossing else None
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

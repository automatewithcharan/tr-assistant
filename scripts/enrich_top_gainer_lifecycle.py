#!/usr/bin/env python3
"""Build an offline, enriched copy of top-gainer lifecycle evidence."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Iterable

from pkscreener.classes.TopGainerLifecycleRecorder import (
    EVENT_TYPES,
    SCHEMA_VERSION,
    SIGNAL_FIELDS,
    TopGainerLifecycleRecorder,
    _timestamp,
    build_linked_event,
    calculate_outcome_labels,
)


def _local_path(raw: str, label: str) -> Path:
    if "://" in raw:
        raise ValueError(f"{label} must be a local filesystem path")
    return Path(raw).expanduser().resolve()


def _parse_date(raw: str) -> str:
    return date.fromisoformat(raw).isoformat()


def _reject_nonfinite_json(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def load_events(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, parse_constant=_reject_nonfinite_json)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"corrupt lifecycle record at line {line_no}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"invalid lifecycle record at line {line_no}")
            if row.get("schema_version") != SCHEMA_VERSION or not row.get("event_id"):
                raise ValueError(f"invalid lifecycle schema at line {line_no}")
            rows.append(row)
    if not rows:
        raise ValueError("lifecycle evidence is empty")
    _validate_events(rows)
    return rows


def _validate_events(rows: list[dict]) -> None:
    """Fail closed on identity ambiguity or malformed lifecycle evidence."""
    event_ids: set[str] = set()
    observations: dict[str, dict] = {}
    finalized: set[str] = set()
    for line_no, row in enumerate(rows, 1):
        if row.get("schema_version") != SCHEMA_VERSION or not row.get("event_id"):
            raise ValueError(f"invalid lifecycle schema at line {line_no}")
        event_id = str(row.get("event_id") or "")
        if event_id in event_ids:
            raise ValueError(f"duplicate lifecycle event_id at line {line_no}")
        event_ids.add(event_id)
        event_type = str(row.get("event_type") or "")
        if event_type not in EVENT_TYPES:
            raise ValueError(f"invalid lifecycle event_type at line {line_no}")
        if event_type != "session_outcome_finalized" and row.get("future_labels") is not None:
            raise ValueError(f"future labels on non-outcome event at line {line_no}")
        if event_type == "session_outcome_finalized" and not isinstance(row.get("future_labels"), dict):
            raise ValueError(f"missing finalized future labels at line {line_no}")
        if row.get("paper_only") is not True:
            raise ValueError(f"non-paper lifecycle evidence at line {line_no}")
        if event_type != "candidate_observed" and row.get("signal_time_fields") is not None:
            raise ValueError(f"signal-time fields on linked event at line {line_no}")
        try:
            event_dt = _timestamp(row.get("event_timestamp"), "event timestamp")
        except ValueError as exc:
            raise ValueError(f"invalid lifecycle timestamp at line {line_no}") from exc
        if event_type != "candidate_observed":
            continue
        lifecycle_id = str(row.get("lifecycle_id") or "")
        if not lifecycle_id:
            raise ValueError(f"missing observation identity at line {line_no}")
        try:
            observation_dt = _timestamp(row.get("observation_timestamp"), "observation timestamp")
        except ValueError as exc:
            raise ValueError(f"invalid observation timestamp at line {line_no}") from exc
        if event_dt != observation_dt:
            raise ValueError(
                f"candidate event timestamp does not match observation timestamp at line {line_no}"
            )
        expected = TopGainerLifecycleRecorder.lifecycle_id(
            observation_dt.date().isoformat(), str(row.get("symbol") or "").strip().upper(),
            observation_dt.isoformat(timespec="microseconds"), str(row.get("source_lane") or ""),
            str(row.get("experiment_version") or ""),
        )
        if (str(row.get("trading_date") or "") != observation_dt.date().isoformat()
                or lifecycle_id != expected):
            raise ValueError(f"observation identity mismatch at line {line_no}")
        if lifecycle_id in observations:
            raise ValueError(f"duplicate lifecycle observation at line {line_no}")
        if row.get("future_labels") is not None or not isinstance(row.get("signal_time_fields"), dict):
            raise ValueError(f"invalid observation field separation at line {line_no}")
        if not set(row["signal_time_fields"]).issubset(SIGNAL_FIELDS):
            raise ValueError(f"unexpected signal-time fields at line {line_no}")
        observations[lifecycle_id] = row
    for line_no, row in enumerate(rows, 1):
        event_type = str(row.get("event_type") or "")
        if event_type == "candidate_observed":
            continue
        if not row.get("lifecycle_id"):
            if event_type == "session_outcome_finalized":
                raise ValueError(
                    f"terminal outcome missing observation identity at line {line_no}"
                )
            continue
        observation = observations.get(str(row.get("lifecycle_id")))
        if observation is None:
            raise ValueError(f"orphan lifecycle event at line {line_no}")
        event_dt = _timestamp(row.get("event_timestamp"), "event timestamp")
        observation_dt = _timestamp(observation.get("observation_timestamp"), "observation timestamp")
        if event_dt < observation_dt:
            raise ValueError(f"lifecycle event precedes observation at line {line_no}")
        if (row.get("lifecycle_id") != observation.get("lifecycle_id")
                or row.get("observation_id") != observation.get("lifecycle_id")
                or row.get("observation_timestamp") != observation.get("observation_timestamp")
                or row.get("origin_source_lane") != observation.get("source_lane")
                or row.get("experiment_version") != observation.get("experiment_version")):
            raise ValueError(f"linked lifecycle identity mismatch at line {line_no}")
        if event_type == "session_outcome_finalized":
            lifecycle_id = str(row.get("lifecycle_id"))
            if lifecycle_id in finalized:
                raise ValueError(f"duplicate finalized outcome at line {line_no}")
            if row.get("payload") != row.get("future_labels"):
                raise ValueError(f"finalized outcome payload mismatch at line {line_no}")
            if row["future_labels"].get("completeness_status") not in {
                "complete",
                "incomplete",
            }:
                raise ValueError(
                    f"invalid terminal completeness status at line {line_no}"
                )
            finalized.add(lifecycle_id)


def load_local_bars(bars_root: Path, trading_date: str, symbol: str) -> tuple[list[dict], str | None]:
    session_root = (bars_root / f"{trading_date}_{trading_date}").resolve()
    candidate = (session_root / f"{symbol}.csv").resolve()
    if session_root != candidate.parent or bars_root not in candidate.parents:
        return [], "unsafe_symbol_bar_path"
    if not candidate.is_file():
        return [], "bar_file_missing"
    bars = []
    try:
        with candidate.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            headers = set(reader.fieldnames or [])
            timestamp_headers = {"datetime", "timestamp", "When"}
            if not headers:
                return [], "bar_file_empty"
            if not headers.intersection(timestamp_headers) or not {"high", "low", "close"}.issubset(headers):
                return [], "bar_file_invalid"
            for row in reader:
                if None in row:
                    return [], "bar_file_invalid"
                bars.append({
                    "timestamp": row.get("datetime") or row.get("timestamp") or row.get("When"),
                    "open": row.get("open"), "high": row.get("high"),
                    "low": row.get("low"), "close": row.get("close"),
                    "volume": row.get("volume"),
                })
    except (OSError, csv.Error, UnicodeError):
        return [], "bar_file_unreadable"
    if not bars:
        return [], "bar_file_empty"
    if not any(bar.get("timestamp") for bar in bars):
        return [], "bar_file_invalid"
    return bars, None


def enrich(events: list[dict], bars_root: Path, start_date: str,
           end_date: str, *, eligible_only: bool = False) -> tuple[list[dict], dict]:
    _validate_events(events)
    existing_ids = {str(row.get("event_id")) for row in events}
    finalized_ids = {str(row.get("lifecycle_id")) for row in events
                     if row.get("event_type") == "session_outcome_finalized"}
    additions = []
    counts = Counter()
    observations = [row for row in events
                    if row.get("event_type") == "candidate_observed"
                    and start_date <= str(row.get("trading_date", "")) <= end_date
                    and (
                        not eligible_only
                        or (
                            row.get("causal_evaluation_eligible", True)
                            and not row.get("historical_reconstruction", False)
                        )
                    )]
    for observation in observations:
        lifecycle_id = str(observation.get("lifecycle_id") or "")
        if lifecycle_id in finalized_ids:
            counts["already_finalized"] += 1
            continue
        trading_date = str(observation.get("trading_date"))
        bars, acquisition_error = load_local_bars(
            bars_root, trading_date, str(observation.get("symbol") or "")
        )
        event_ts, labels, calculation_error = calculate_outcome_labels(observation, bars)
        missing_reason = acquisition_error or calculation_error
        if missing_reason:
            labels["completeness_status"] = "incomplete"
            labels["missing_reason"] = missing_reason
            incomplete_payload = {
                "completeness_status": "incomplete", "missing_reason": missing_reason,
            }
            if labels.get("coverage_issues"):
                incomplete_payload["coverage_issues"] = labels["coverage_issues"]
                for issue in labels["coverage_issues"]:
                    counts[f"coverage:{issue}"] += 1
            incomplete = build_linked_event(
                observation, "data_incomplete", event_ts, "eod_enrichment",
                incomplete_payload,
            )
            if incomplete["event_id"] not in existing_ids:
                additions.append(incomplete)
                existing_ids.add(incomplete["event_id"])
            counts[f"incomplete:{missing_reason}"] += 1
        else:
            counts["complete"] += 1
        outcome = build_linked_event(
            observation, "session_outcome_finalized", event_ts, "eod_enrichment", labels
        )
        if outcome["event_id"] not in existing_ids:
            additions.append(outcome)
            existing_ids.add(outcome["event_id"])
            finalized_ids.add(lifecycle_id)
    counts["selected_observations"] = len(observations)
    counts["events_added"] = len(additions)
    return additions, dict(sorted(counts.items()))


def write_events(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(
                    row, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False,
                ) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Linking is atomic and fails if another process creates the output
        # after CLI preflight; unlike replace(), it can never overwrite it.
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ValueError(f"output already exists; refusing to overwrite: {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, help="source lifecycle events.jsonl")
    parser.add_argument("--bars-root", required=True, help="local minute backfill directory")
    parser.add_argument("--start-date", required=True, type=_parse_date)
    parser.add_argument("--end-date", required=True, type=_parse_date)
    parser.add_argument("--output-events", help="new enriched JSONL; source is never modified")
    parser.add_argument("--dry-run", action="store_true", help="calculate and report without writing")
    args = parser.parse_args(argv)
    if args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    source = _local_path(args.events, "--events")
    bars_root = _local_path(args.bars_root, "--bars-root")
    if not source.is_file():
        parser.error("--events must name an existing local file")
    if not bars_root.is_dir():
        parser.error("--bars-root must name an existing local directory")
    if not args.dry_run and not args.output_events:
        parser.error("--output-events is required unless --dry-run is used")
    output = _local_path(args.output_events, "--output-events") if args.output_events else None
    if output == source:
        parser.error("in-place enrichment is refused; choose a separate --output-events path")
    if output and output.exists():
        parser.error("--output-events already exists; refusing to overwrite it")

    original = load_events(source)
    additions, summary = enrich(original, bars_root, args.start_date, args.end_date)
    summary.update({
        "source_events": len(original), "output_events": len(original) + len(additions),
        "start_date": args.start_date, "end_date": args.end_date,
        "dry_run": bool(args.dry_run),
    })
    if output and not args.dry_run:
        write_events(output, [*original, *additions])
        summary["output_path"] = str(output)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

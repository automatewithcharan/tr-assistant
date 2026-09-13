#!/usr/bin/env python3
"""Create an immutable, post-close top-gainer lifecycle evidence artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from pkscreener.classes.TopGainerLifecycleRecorder import SCHEMA_VERSION, _timestamp
from scripts.analyze_top_gainer_lifecycle import (
    incomplete_diagnostic_error,
    summarize,
    validate_terminal_outcomes,
)
from scripts.enrich_top_gainer_lifecycle import enrich, load_events, write_events


IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = time(15, 30)
FINALIZATION_CONTRACT_VERSION = 1
SESSION_CONTRACT = (
    "A closed trading date is finalized when every causally eligible observation "
    "for that date has exactly one session_outcome_finalized event whose "
    "completeness_status is complete or incomplete. Complete market-data coverage "
    "is not required; unavailable or gapped bars remain explicit incomplete outcomes."
)


def _json_line(row: dict) -> bytes:
    return (
        json.dumps(
            row, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _rows_sha256(rows: Iterable[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_json_line(row))
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_immutable_text(path: Path, text: str) -> None:
    """Publish text atomically without ever replacing an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ValueError(f"immutable artifact already exists: {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _normalized_now(now: datetime | None) -> datetime:
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    return current.astimezone(IST)


def require_closed_date(trading_date: str, now: datetime | None = None) -> None:
    target = date.fromisoformat(trading_date)
    current = _normalized_now(now)
    if target > current.date():
        raise ValueError(f"future trading date is not closed: {trading_date}")
    if target == current.date() and current.timetz().replace(tzinfo=None) < MARKET_CLOSE:
        raise ValueError(f"active trading date is not closed: {trading_date}")


def _events_through_date(events: list[dict], end_date: str) -> list[dict]:
    selected_ids = {
        str(row.get("lifecycle_id"))
        for row in events
        if row.get("event_type") == "candidate_observed"
        and str(row.get("trading_date", "")) <= end_date
        and row.get("lifecycle_id")
    }
    selected = []
    for row in events:
        event_type = str(row.get("event_type") or "")
        lifecycle_id = str(row.get("lifecycle_id") or "")
        event_dt = _timestamp(row.get("event_timestamp"), "event timestamp")
        event_date = event_dt.date().isoformat()
        if event_type == "candidate_observed":
            observation_dt = _timestamp(
                row.get("observation_timestamp"), "observation timestamp"
            )
            if event_dt != observation_dt:
                raise ValueError(
                    "candidate event timestamp does not match observation timestamp"
                )
            if lifecycle_id in selected_ids and event_date > end_date:
                raise ValueError(
                    "candidate event timestamp is after the artifact cutoff"
                )
            if lifecycle_id in selected_ids and event_date <= end_date:
                selected.append(row)
        elif lifecycle_id:
            if lifecycle_id in selected_ids:
                if event_date <= end_date:
                    selected.append(row)
        else:
            if event_date <= end_date:
                selected.append(row)
    return selected


def _events_for_single_date(events: list[dict], trading_date: str) -> list[dict]:
    """Select only one closed session for the normal automatic EOD path."""
    through_events = _events_through_date(events, trading_date)
    selected_ids = {
        str(row.get("lifecycle_id"))
        for row in through_events
        if row.get("event_type") == "candidate_observed"
        and str(row.get("trading_date") or "") == trading_date
        and row.get("lifecycle_id")
    }
    selected = []
    for row in through_events:
        lifecycle_id = str(row.get("lifecycle_id") or "")
        if lifecycle_id:
            if lifecycle_id in selected_ids:
                selected.append(row)
            continue
        event_date = _timestamp(
            row.get("event_timestamp"), "event timestamp"
        ).date().isoformat()
        if event_date == trading_date:
            selected.append(row)
    return selected


def _eligible_observations(events: list[dict]) -> list[dict]:
    return [
        row
        for row in events
        if row.get("event_type") == "candidate_observed"
        and row.get("causal_evaluation_eligible", True)
        and not row.get("historical_reconstruction", False)
    ]


def _bar_provenance(bars_root: Path, observations: list[dict]) -> dict:
    digest = hashlib.sha256()
    existing = 0
    missing = 0
    pairs = sorted(
        {
            (str(row.get("trading_date")), str(row.get("symbol")))
            for row in observations
        }
    )
    for trading_date, symbol in pairs:
        relative = Path(f"{trading_date}_{trading_date}") / f"{symbol}.csv"
        path = bars_root / relative
        if path.is_file():
            state = _file_sha256(path)
            existing += 1
        else:
            state = "MISSING"
            missing += 1
        digest.update(f"{relative.as_posix()}\0{state}\n".encode("utf-8"))
    return {
        "bar_inputs_sha256": digest.hexdigest(),
        "bar_input_symbol_dates": len(pairs),
        "bar_input_files": existing,
        "bar_input_files_missing": missing,
    }


def _validate_required_session_roots(
    bars_root: Path, observations: list[dict]
) -> None:
    for trading_date in sorted(
        {str(row.get("trading_date")) for row in observations}
    ):
        session_root = bars_root / f"{trading_date}_{trading_date}"
        if not session_root.is_dir() or not next(session_root.glob("*.csv"), None):
            raise ValueError(
                f"required minute-bar session data is unavailable: {trading_date}"
            )


def _session_finalization(events: list[dict], observations: list[dict]) -> dict:
    validate_terminal_outcomes(events)
    observation_by_id = {
        str(row.get("lifecycle_id")): row for row in observations
    }
    outcomes = [
        row
        for row in events
        if row.get("event_type") == "session_outcome_finalized"
        and str(row.get("lifecycle_id")) in observation_by_id
    ]
    outcome_counts = Counter(str(row.get("lifecycle_id")) for row in outcomes)
    invalid_status = [
        str(row.get("lifecycle_id"))
        for row in outcomes
        if (row.get("future_labels") or {}).get("completeness_status")
        not in {"complete", "incomplete"}
    ]
    not_exactly_once = sorted(
        identifier
        for identifier in observation_by_id
        if outcome_counts[identifier] != 1
    )
    incomplete_diagnostic_errors = {
        str(row.get("lifecycle_id")): error
        for row in outcomes
        if (row.get("future_labels") or {}).get("completeness_status")
        == "incomplete"
        if (error := incomplete_diagnostic_error(events, row)) is not None
    }
    if invalid_status:
        raise ValueError(
            f"terminal outcomes have invalid completeness status: {invalid_status[:5]}"
        )
    if not_exactly_once:
        raise ValueError(
            "eligible observations do not have exactly one terminal outcome: "
            f"{not_exactly_once[:5]}"
        )
    if incomplete_diagnostic_errors:
        raise ValueError(
            "incomplete terminal outcomes lack one matching EOD data_incomplete "
            f"diagnostic: {list(incomplete_diagnostic_errors.items())[:5]}"
        )

    observations_by_date = defaultdict(set)
    for identifier, observation in observation_by_id.items():
        observations_by_date[str(observation.get("trading_date"))].add(identifier)
    sessions = {}
    outcomes_by_id = {str(row.get("lifecycle_id")): row for row in outcomes}
    for trading_date, identifiers in sorted(observations_by_date.items()):
        complete = sum(
            (outcomes_by_id[identifier].get("future_labels") or {}).get(
                "completeness_status"
            )
            == "complete"
            for identifier in identifiers
        )
        incomplete = len(identifiers) - complete
        sessions[trading_date] = {
            "status": "finalized",
            "eligible_observations": len(identifiers),
            "terminal_outcomes": len(identifiers),
            "complete_outcomes": complete,
            "incomplete_outcomes": incomplete,
            "exactly_one_terminal_outcome_per_eligible_observation": True,
        }
    return sessions


def _verify_existing_json(
    path: Path, checksum_path: Path, expected: dict
) -> dict:
    try:
        manifest_bytes = path.read_bytes()
        checksum_text = checksum_path.read_text(encoding="utf-8")
        existing = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing immutable metadata is unreadable: {path}") from exc
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if checksum_text != f"{manifest_sha}\n":
        raise ValueError(f"existing immutable manifest checksum differs: {path}")

    if existing != expected:
        raise ValueError(f"existing immutable metadata differs: {path}")
    return existing


def _validate_expected_session_dates(
    sessions: dict, expected_dates: Iterable[str] | None
) -> list[str]:
    if expected_dates is None:
        raise ValueError("expected trading dates are required")
    normalized = [date.fromisoformat(str(value)).isoformat() for value in expected_dates]
    if not normalized:
        raise ValueError("expected trading dates cannot be empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError("expected trading dates contain duplicates")
    expected = set(normalized)
    actual = set(sessions)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(
            "finalized trading dates do not match the recovery acceptance set: "
            f"missing={missing} unexpected={unexpected}"
        )
    return sorted(normalized)


def finalize_closed_date(
    events_path: Path,
    bars_root: Path,
    trading_date: str,
    output_dir: Path,
    *,
    now: datetime | None = None,
    expected_dates: Iterable[str] | None = None,
) -> dict:
    """Finalize eligible observations through one closed date without mutation."""
    require_closed_date(trading_date, now)
    events_path = events_path.expanduser().resolve()
    bars_root = bars_root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not events_path.is_file():
        raise ValueError(f"lifecycle source is unavailable: {events_path}")
    if not bars_root.is_dir():
        raise ValueError(f"minute-bar root is unavailable: {bars_root}")

    raw_events = load_events(events_path)
    is_multi_date_recovery = expected_dates is not None
    selected_events = (
        _events_through_date(raw_events, trading_date)
        if is_multi_date_recovery
        else _events_for_single_date(raw_events, trading_date)
    )
    selected_source_sha = _rows_sha256(selected_events)
    eligible = _eligible_observations(selected_events)
    if not eligible:
        raise ValueError(f"no eligible lifecycle observations through {trading_date}")
    if trading_date not in {str(row.get("trading_date")) for row in eligible}:
        raise ValueError(f"no eligible lifecycle observations for {trading_date}")
    _validate_required_session_roots(bars_root, eligible)
    bar_provenance = _bar_provenance(bars_root, eligible)

    first_date = min(str(row.get("trading_date")) for row in eligible)
    additions, enrichment_summary = enrich(
        selected_events,
        bars_root,
        first_date,
        trading_date,
        eligible_only=True,
    )
    enriched_events = [*selected_events, *additions]
    sessions = _session_finalization(enriched_events, eligible)
    if any(row.get("status") != "finalized" for row in sessions.values()):
        raise ValueError("one or more closed lifecycle sessions were not finalized")
    accepted_expected_dates = _validate_expected_session_dates(
        sessions, expected_dates if is_multi_date_recovery else [trading_date]
    )
    latest_events = load_events(events_path)
    latest_selected_events = (
        _events_through_date(latest_events, trading_date)
        if is_multi_date_recovery
        else _events_for_single_date(latest_events, trading_date)
    )
    if _rows_sha256(latest_selected_events) != selected_source_sha:
        raise ValueError(
            "lifecycle source changed for the selected closed-date range during enrichment"
        )
    if _bar_provenance(bars_root, eligible) != bar_provenance:
        raise ValueError("minute-bar inputs changed during lifecycle enrichment")

    stem = f"events.enriched_through_{trading_date}"
    artifact_path = output_dir / f"{stem}.jsonl"
    manifest_path = output_dir / f"{stem}.manifest.json"
    manifest_checksum_path = output_dir / f"{stem}.manifest.sha256"
    analysis_path = output_dir / f"analysis.enriched_through_{trading_date}.json"
    if manifest_path.exists() != manifest_checksum_path.exists():
        raise ValueError(
            "immutable manifest/checksum pair is incomplete: "
            f"{manifest_path}"
        )
    expected_artifact_sha = _rows_sha256(enriched_events)
    if artifact_path.exists():
        load_events(artifact_path)
        if _file_sha256(artifact_path) != expected_artifact_sha:
            raise ValueError(
                f"existing immutable enriched artifact differs: {artifact_path}"
            )
        artifact_state = "verified_existing"
    else:
        write_events(artifact_path, enriched_events)
        if _file_sha256(artifact_path) != expected_artifact_sha:
            raise ValueError("published enriched artifact hash verification failed")
        artifact_state = "created"

    # The analyzer is deliberately given the enriched artifact path; its raw-file
    # default is never used by this EOD workflow.
    analysis = summarize(artifact_path)
    analysis_text = json.dumps(analysis, indent=2, sort_keys=True) + "\n"
    analysis_sha = hashlib.sha256(analysis_text.encode("utf-8")).hexdigest()
    if analysis_path.exists():
        if _file_sha256(analysis_path) != analysis_sha:
            raise ValueError(
                f"existing immutable lifecycle analysis differs: {analysis_path}"
            )
    else:
        _write_immutable_text(analysis_path, analysis_text)

    repo_root = Path(__file__).resolve().parents[1]
    labeler_path = repo_root / "pkscreener" / "classes" / "TopGainerLifecycleRecorder.py"
    enrichment_path = repo_root / "scripts" / "enrich_top_gainer_lifecycle.py"
    finalizer_path = Path(__file__).resolve()
    analyzer_path = repo_root / "scripts" / "analyze_top_gainer_lifecycle.py"
    through_session_close = datetime.combine(
        date.fromisoformat(trading_date), MARKET_CLOSE, tzinfo=IST
    ).isoformat(timespec="seconds")
    manifest = {
        "schema_version": 1,
        "artifact_type": "top_gainer_lifecycle_enriched",
        "finalization_contract_version": FINALIZATION_CONTRACT_VERSION,
        "session_finalization_contract": SESSION_CONTRACT,
        "finalization_scope": (
            "multi_date_recovery" if is_multi_date_recovery else "single_date_eod"
        ),
        "through_trading_date": trading_date,
        "through_session_close_timestamp": through_session_close,
        "paper_only": True,
        "raw_observations_modified": False,
        "source_events_path": str(events_path),
        "selected_source_events": len(selected_events),
        "selected_source_events_sha256": selected_source_sha,
        "eligible_observations": len(eligible),
        "enriched_artifact_path": str(artifact_path),
        "enriched_artifact_events": len(enriched_events),
        "enriched_artifact_sha256": expected_artifact_sha,
        "analysis_input_path": str(artifact_path),
        "analysis_artifact_path": str(analysis_path),
        "analysis_artifact_sha256": analysis_sha,
        "bars_root": str(bars_root),
        **bar_provenance,
        "lifecycle_schema_version": SCHEMA_VERSION,
        "labeler_module_sha256": _file_sha256(labeler_path),
        "enrichment_module_sha256": _file_sha256(enrichment_path),
        "finalizer_module_sha256": _file_sha256(finalizer_path),
        "analyzer_module_sha256": _file_sha256(analyzer_path),
        "experiment_versions": sorted(
            {str(row.get("experiment_version")) for row in eligible}
        ),
        "enrichment_summary": enrichment_summary,
        "finalized_trading_dates": sorted(sessions),
        "expected_trading_dates": accepted_expected_dates,
        "sessions": sessions,
    }
    if manifest_path.exists():
        manifest = _verify_existing_json(
            manifest_path, manifest_checksum_path, manifest
        )
    else:
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        _write_immutable_text(manifest_path, manifest_text)
        _write_immutable_text(
            manifest_checksum_path,
            hashlib.sha256(manifest_text.encode("utf-8")).hexdigest() + "\n",
        )
    return {
        "status": "finalized",
        "artifact_state": artifact_state,
        "through_trading_date": trading_date,
        "artifact_path": str(artifact_path),
        "manifest_path": str(manifest_path),
        "manifest_checksum_path": str(manifest_checksum_path),
        "analysis_path": str(analysis_path),
        "finalized_sessions": len(sessions),
        "eligible_observations": len(eligible),
        "complete_outcomes": sum(
            int(row["complete_outcomes"]) for row in sessions.values()
        ),
        "incomplete_outcomes": sum(
            int(row["incomplete_outcomes"]) for row in sessions.values()
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events", default="results/Data/top_gainer_lifecycle/events.jsonl"
    )
    parser.add_argument(
        "--bars-root", default="results/Data/kite_history_backfill/minute"
    )
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--output-dir", default="results/Data/top_gainer_lifecycle/enriched"
    )
    parser.add_argument(
        "--expected-date",
        action="append",
        dest="expected_dates",
        help="closed trading date required in the finalized session set; repeatable",
    )
    args = parser.parse_args(argv)
    try:
        result = finalize_closed_date(
            Path(args.events),
            Path(args.bars_root),
            args.date.isoformat(),
            Path(args.output_dir),
            expected_dates=args.expected_dates,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

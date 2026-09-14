#!/usr/bin/env python3
"""Read-only, timestamp-anchored analysis of the prospective timing funnel.

The analyzer joins immutable journals by NSE trading date and normalized symbol.
It never writes its inputs and never imports a scanner or trading runtime.  Raw
events are retained; first-stage rows are derived only for summary reporting.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
SCHEMA_VERSION = 1
IDENTITY_VERSION = "prospective_timing_funnel_v1"
STAGES = (
    "SOURCE_EVALUATED",
    "PRECURSOR_QUALIFIED",
    "BUY_NOW_BOARD_QUALIFIED",
    "GOAL_MODE_DECISION",
    "DISCORD_ATTEMPTED",
    "DISCORD_SUCCESSFUL",
    "TRADINGVIEW_CANDIDATE_ELIGIBLE",
    "TRADINGVIEW_ALERT",
)
PRECURSOR_SOURCES = {"anomaly", "pre_move", "early_breakout", "top_gainer_radar"}
HORIZONS = (5, 15, 30, 60, 120)
THRESHOLDS = (0.5, 1.0, 2.0, 3.0, 5.0)


def _timestamp(value: Any, label: str = "timestamp") -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def opportunity_id(trading_date: str, symbol: str) -> str:
    """Return a deterministic, date-scoped research identity."""
    day = str(trading_date or "").strip()
    normalized = _symbol(symbol)
    if not day or not normalized:
        raise ValueError("opportunity identity requires trading_date and symbol")
    material = "|".join((IDENTITY_VERSION, day, normalized))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _stage_event(
    stage: str,
    trading_date: str,
    symbol: str,
    timestamp: Any,
    source: str,
    *,
    price: Any = None,
    move_pct: Any = None,
    observation_id: Any = None,
    reason: Any = None,
    decision: Any = None,
    message_kind: Any = None,
    raw_kind: Any = None,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"unknown funnel stage: {stage}")
    parsed = _timestamp(timestamp, f"{stage} timestamp")
    day = str(trading_date or parsed.date().isoformat())
    normalized = _symbol(symbol)
    if day != parsed.date().isoformat():
        raise ValueError(f"{stage} trading_date does not match timestamp")
    return {
        "opportunity_id": opportunity_id(day, normalized),
        "trading_date": day,
        "symbol": normalized,
        "stage": stage,
        "event_timestamp": parsed.isoformat(timespec="microseconds"),
        "source": str(source or "unknown"),
        "price": _finite(price),
        "move_pct": _finite(move_pct),
        "observation_id": str(observation_id or "") or None,
        "reason": str(reason or "") or None,
        "decision": str(decision or "") or None,
        "message_kind": str(message_kind or "") or None,
        "raw_kind": str(raw_kind or "") or None,
    }


def load_jsonl(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"corrupt JSONL at {path}:{line_number}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"non-object JSONL at {path}:{line_number}")
                rows.append(row)
    return rows


def _coverage_state() -> dict[str, Any]:
    return {"first": None, "last": None, "max_gap_seconds": 0.0, "out_of_order": False}


def _observe_coverage(state: dict[str, Any], timestamp: datetime) -> None:
    if state["first"] is None or timestamp < state["first"]:
        state["first"] = timestamp
    previous = state["last"]
    if previous is not None:
        if timestamp < previous:
            state["out_of_order"] = True
        elif timestamp > previous:
            state["max_gap_seconds"] = max(
                state["max_gap_seconds"], (timestamp - previous).total_seconds()
            )
    if previous is None or timestamp > previous:
        state["last"] = timestamp


def _coverage_result(state: Mapping[str, Any], expected_seconds: float = 120.0) -> dict[str, Any]:
    if state.get("first") is None:
        return {"status": "unknown", "first": None, "last": None, "max_gap_seconds": None}
    gap = float(state.get("max_gap_seconds") or 0.0)
    status = "unknown_order" if state.get("out_of_order") else (
        "observed_with_gaps" if gap > expected_seconds else "observed"
    )
    return {
        "status": status,
        "first": state["first"].isoformat(timespec="microseconds"),
        "last": state["last"].isoformat(timespec="microseconds"),
        "max_gap_seconds": gap,
    }


def load_timing_compact(
    paths: Iterable[str | Path],
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], Counter, Counter]:
    """Stream large timing journals, retaining first events and exact counters."""
    first_events: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    raw_stage_counts: Counter = Counter()
    raw_by_opportunity: Counter = Counter()
    coverage_states: dict[str, dict[str, Any]] = defaultdict(_coverage_state)
    board_seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"corrupt JSONL at {path}:{line_number}") from exc
                day = str(row.get("trading_date") or "")
                if (start_date and day < start_date) or (end_date and day > end_date):
                    continue
                if str(row.get("event_type") or "").upper() == "SOURCE_EVALUATED":
                    _observe_coverage(coverage_states[day], _timestamp(row.get("event_timestamp")))
                if str(row.get("source") or "") == "buy_now_board":
                    board_seen.add(day)
                for event in events_from_timing([row]):
                    key = (
                        event["opportunity_id"], event["stage"], event["source"],
                        event.get("message_kind"),
                    )
                    raw_stage_counts[event["stage"]] += 1
                    raw_by_opportunity[(event["opportunity_id"], event["stage"], event.get("message_kind"))] += 1
                    current = first_events.get(key)
                    if current is None or event["event_timestamp"] < current["event_timestamp"]:
                        first_events[key] = event
    coverage = {}
    for day in sorted(set(coverage_states) | board_seen):
        coverage[day] = {
            "signal_monitor_timing": _coverage_result(coverage_states[day]),
            "live_feature_recording": {
                "status": "unknown", "first": None, "last": None, "max_gap_seconds": None
            },
            "buy_now_board_path": "observed" if day in board_seen else "unknown",
            "goal_mode_configuration": "unknown",
            "tradingview_pine_evidence": "unknown",
        }
    return list(first_events.values()), coverage, raw_stage_counts, raw_by_opportunity


def events_from_timing(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        event_type = str(row.get("event_type") or "").upper()
        source = str(row.get("source") or "").lower()
        stage = None
        message_kind = None
        if event_type == "SOURCE_EVALUATED":
            stage = "SOURCE_EVALUATED"
        elif event_type == "SOURCE_QUALIFIED" and source in PRECURSOR_SOURCES:
            stage = "PRECURSOR_QUALIFIED"
        elif event_type == "SOURCE_QUALIFIED" and source == "buy_now_board":
            stage = "BUY_NOW_BOARD_QUALIFIED"
        elif event_type == "DISCORD_ATTEMPTED":
            stage = "DISCORD_ATTEMPTED"
        elif event_type == "DISCORD_SUCCESS":
            stage = "DISCORD_SUCCESSFUL"
        if stage is None:
            continue
        if source == "buy_now_board":
            message_kind = "buy_now_board"
        elif source in {"early_breakout", "top_gainer_radar"}:
            message_kind = "candidate_or_trade_message"
        events.append(
            _stage_event(
                stage,
                str(row.get("trading_date") or ""),
                row.get("symbol"),
                row.get("event_timestamp"),
                f"alert_timing:{source}",
                price=row.get("price"),
                move_pct=row.get("day_change_pct"),
                observation_id=row.get("observation_id") or row.get("candidate_id"),
                reason=row.get("rejection_reason") or row.get("failure_category"),
                message_kind=message_kind,
                raw_kind=event_type,
            )
        )
    return events


def events_from_lifecycle(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], Counter]:
    observations = {
        str(row.get("lifecycle_id") or ""): row
        for row in rows
        if row.get("event_type") == "candidate_observed"
    }
    events: list[dict[str, Any]] = []
    repeats: Counter = Counter()
    for row in rows:
        if row.get("event_type") != "alert_emitted":
            continue
        observation_id = str(row.get("observation_id") or row.get("lifecycle_id") or "")
        observation = observations.get(observation_id)
        if observation is None:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        alert_type = str(payload.get("alert_type") or "").lower()
        if "pre-move" not in alert_type and "anomaly" not in alert_type:
            continue
        day = str(observation.get("trading_date") or "")
        symbol = _symbol(observation.get("symbol"))
        fields = observation.get("signal_time_fields") or {}
        event = _stage_event(
            "PRECURSOR_QUALIFIED",
            day,
            symbol,
            row.get("event_timestamp"),
            "lifecycle:alert_emitted",
            price=fields.get("current_price"),
            move_pct=payload.get("return_already_achieved"),
            observation_id=observation_id,
            raw_kind="alert_emitted",
        )
        events.append(event)
        repeats[event["opportunity_id"]] += 1
    return events, repeats


def events_from_goal_journal(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Load only explicit accepted goal-mode rows; the journal has no rejects."""
    events: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("IsGoalMode") or "").strip().lower() not in {"1", "true", "yes"}:
                    continue
                stamp = row.get("GeneratedAt")
                parsed = _timestamp(stamp, "goal decision timestamp")
                events.append(
                    _stage_event(
                        "GOAL_MODE_DECISION",
                        parsed.date().isoformat(),
                        row.get("Symbol"),
                        stamp,
                        "intraday_goal_alerts",
                        price=row.get("PriceNow"),
                        move_pct=row.get("ChangePct"),
                        reason=row.get("Why"),
                        decision="accepted",
                        message_kind="trade_notification",
                    )
                )
    return events


def events_from_candidate_delivery(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Load direct-candidate discovery and durable Discord delivery evidence."""
    events: list[dict[str, Any]] = []
    seen_eligible: set[tuple[str, str]] = set()
    for row in rows:
        observation_id = str(row.get("lifecycle_observation_id") or "")
        day = str(row.get("trading_date") or "")
        symbol = _symbol(row.get("symbol"))
        detected = row.get("detected_at")
        key = (observation_id, str(detected or ""))
        if detected and key not in seen_eligible:
            events.append(
                _stage_event(
                    "PRECURSOR_QUALIFIED",
                    day,
                    symbol,
                    detected,
                    "tradingview_candidate_delivery:direct_candidate_detected",
                    observation_id=observation_id,
                    message_kind="manual_tradingview_candidate",
                    raw_kind="detected",
                )
            )
            seen_eligible.add(key)
        status = str(row.get("delivery_status") or "")
        if row.get("send_attempt_at") and status in {
            "attempt_reserved", "retrying", "retryable_failed", "delivered"
        }:
            events.append(
                _stage_event(
                    "DISCORD_ATTEMPTED",
                    day,
                    symbol,
                    row.get("send_attempt_at"),
                    "tradingview_candidate_delivery",
                    observation_id=observation_id,
                    reason=row.get("failure_class"),
                    message_kind="manual_tradingview_candidate",
                    raw_kind=status,
                )
            )
        if status == "delivered" and row.get("delivered_at"):
            events.append(
                _stage_event(
                    "DISCORD_SUCCESSFUL",
                    day,
                    symbol,
                    row.get("delivered_at"),
                    "tradingview_candidate_delivery",
                    observation_id=observation_id,
                    message_kind="manual_tradingview_candidate",
                    raw_kind=status,
                )
            )
    return events


def events_from_tradingview_alerts(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Load optional authoritative Pine receipts; no candidate is treated as an alert."""
    events = []
    for row in rows:
        if str(row.get("event_type") or "").upper() != "TRADINGVIEW_ALERT":
            continue
        events.append(
            _stage_event(
                "TRADINGVIEW_ALERT",
                str(row.get("trading_date") or ""),
                row.get("symbol"),
                row.get("event_timestamp"),
                str(row.get("source") or "tradingview_receipt"),
                price=row.get("price"),
                move_pct=row.get("day_change_pct"),
                observation_id=row.get("observation_id"),
                message_kind="pine_alert",
                raw_kind=row.get("alert_type"),
            )
        )
    return events


def load_minute_bars(bars_root: str | Path, trading_date: str, symbol: str) -> list[dict[str, Any]]:
    root = Path(bars_root).resolve()
    path = (root / f"{trading_date}_{trading_date}" / f"{symbol}.csv").resolve()
    if root not in path.parents or not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _bar_timestamp(row: Mapping[str, Any]) -> datetime | None:
    try:
        return _timestamp(row.get("datetime") or row.get("timestamp") or row.get("When"))
    except ValueError:
        return None


def stage_outcome(event: Mapping[str, Any], bars: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute outcomes from bars strictly after the actual stage timestamp."""
    event_dt = _timestamp(event.get("event_timestamp"), "stage event timestamp")
    session_end = event_dt.replace(hour=15, minute=29, second=0, microsecond=0)
    bar_rows = list(bars)
    parsed = sorted(
        (stamp, row)
        for row in bar_rows
        if (stamp := _bar_timestamp(row)) is not None
        and stamp.date() == event_dt.date()
        and stamp > event_dt
        and stamp <= session_end
    )
    explicit_price = _finite(event.get("price"))
    reference_source = "event_price" if explicit_price and explicit_price > 0 else None
    reference_price = explicit_price if explicit_price and explicit_price > 0 else None
    if reference_price is None:
        completed = []
        for row in bar_rows:
            stamp = _bar_timestamp(row)
            close = _finite(row.get("close"))
            if (
                stamp is not None
                and stamp.date() == event_dt.date()
                and close is not None
                and stamp + timedelta(minutes=1) <= event_dt
            ):
                completed.append((stamp, close))
        if completed:
            reference_price = max(completed, key=lambda item: item[0])[1]
            reference_source = "last_completed_minute_close"

    result: dict[str, Any] = {
        "reference_price": reference_price,
        "reference_price_source": reference_source,
        "completeness_status": "incomplete",
        "missing_reason": None,
        "coverage_issues": [],
        "later_intraday_high": None,
        "later_intraday_low": None,
        "mfe_pct": None,
        "remaining_upside_pct": None,
        "mae_pct": None,
        "time_to_later_high_minutes": None,
    }
    result.update({f"return_{minutes}m_pct": None for minutes in HORIZONS})
    result.update({f"reached_{threshold:g}pct": None for threshold in THRESHOLDS})
    if reference_price is None or reference_price <= 0:
        result["missing_reason"] = "reference_price_unavailable"
        return result
    if not parsed:
        result["missing_reason"] = "no_strictly_post_stage_bars"
        return result

    # Minute-bar timestamps denote candle opens. The next minute boundary is
    # strictly after both an exact-minute event and an event inside a minute.
    expected_first = event_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    unique_times = [stamp for stamp, _ in parsed]
    issues = []
    if unique_times[0] != expected_first:
        issues.append("first_expected_minute_missing")
    if len(set(unique_times)) != len(unique_times):
        issues.append("duplicate_bar_timestamp")
    if any(b - a != timedelta(minutes=1) for a, b in zip(unique_times, unique_times[1:])):
        issues.append("internal_minute_gap")
    if unique_times[-1] != session_end:
        issues.append("session_ended_before_15_29")
    numeric = []
    for stamp, row in parsed:
        high, low, close = (_finite(row.get(key)) for key in ("high", "low", "close"))
        if high is None or low is None or close is None:
            issues.append("invalid_or_missing_ohlc")
            continue
        numeric.append((stamp, high, low, close))
    result["coverage_issues"] = sorted(set(issues))
    if issues or len(numeric) != len(parsed):
        result["missing_reason"] = "post_stage_session_incomplete"
        return result

    high_stamp, later_high, _, _ = max(numeric, key=lambda item: item[1])
    later_low = min(item[2] for item in numeric)
    result.update(
        {
            "completeness_status": "complete",
            "later_intraday_high": later_high,
            "later_intraday_low": later_low,
            "mfe_pct": (later_high / reference_price - 1.0) * 100.0,
            "remaining_upside_pct": (later_high / reference_price - 1.0) * 100.0,
            "mae_pct": (later_low / reference_price - 1.0) * 100.0,
            "time_to_later_high_minutes": (high_stamp - event_dt).total_seconds() / 60.0,
        }
    )
    for minutes in HORIZONS:
        cutoff = event_dt + timedelta(minutes=minutes)
        if cutoff > session_end:
            continue
        eligible = [
            item for item in numeric
            if item[0] + timedelta(minutes=1) <= cutoff
        ]
        if eligible:
            result[f"return_{minutes}m_pct"] = (
                eligible[-1][3] / reference_price - 1.0
            ) * 100.0
    for threshold in THRESHOLDS:
        result[f"reached_{threshold:g}pct"] = any(
            (high / reference_price - 1.0) * 100.0 >= threshold
            for _, high, _, _ in numeric
        )
    return result


def runtime_coverage(
    trading_date: str,
    timing_rows: Iterable[Mapping[str, Any]],
    live_feature_times: Iterable[Any] = (),
) -> dict[str, Any]:
    source_times = sorted(
        _timestamp(row.get("event_timestamp"))
        for row in timing_rows
        if str(row.get("trading_date") or "") == trading_date
        and str(row.get("event_type") or "").upper() == "SOURCE_EVALUATED"
    )
    feature_times = sorted(_timestamp(value) for value in live_feature_times)

    def component(times: list[datetime], expected_seconds: float) -> dict[str, Any]:
        if not times:
            return {"status": "unknown", "first": None, "last": None, "max_gap_seconds": None}
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        return {
            "status": "observed_with_gaps" if gaps and max(gaps) > expected_seconds else "observed",
            "first": times[0].isoformat(timespec="microseconds"),
            "last": times[-1].isoformat(timespec="microseconds"),
            "max_gap_seconds": max(gaps) if gaps else 0.0,
        }

    day_rows = [row for row in timing_rows if str(row.get("trading_date") or "") == trading_date]
    board_seen = any(str(row.get("source") or "") == "buy_now_board" for row in day_rows)
    return {
        "signal_monitor_timing": component(source_times, 120.0),
        "live_feature_recording": component(feature_times, 120.0),
        "buy_now_board_path": "observed" if board_seen else "unknown",
        "goal_mode_configuration": "unknown",
        "tradingview_pine_evidence": "unknown",
    }


def _quantiles(values: Iterable[float]) -> dict[str, float | None]:
    ordered = sorted(value for value in values if _finite(value) is not None)
    if not ordered:
        return {"p25": None, "p50": None, "p75": None}

    def at(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return round(ordered[lower], 4)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 4)

    return {"p25": at(0.25), "p50": at(0.5), "p75": at(0.75)}


def analyze(
    events: Iterable[Mapping[str, Any]],
    bars_by_opportunity: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    coverage_by_date: Mapping[str, Mapping[str, Any]] | None = None,
    lifecycle_repeat_counts: Mapping[str, int] | None = None,
    raw_stage_counts: Mapping[str, int] | None = None,
    raw_counts_by_opportunity: Mapping[tuple[str, str, str | None], int] | None = None,
) -> dict[str, Any]:
    raw = [dict(event) for event in events]
    raw.sort(key=lambda row: (row["event_timestamp"], row["stage"], row["source"]))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in raw:
        expected = opportunity_id(event["trading_date"], event["symbol"])
        if event.get("opportunity_id") != expected:
            raise ValueError("event opportunity identity/date/symbol mismatch")
        grouped[expected].append(event)

    diagnostics = []
    adjacent_delays: dict[str, list[float]] = defaultdict(list)
    stage_reached = Counter()
    raw_stage_opportunities = Counter()
    bars_by_opportunity = bars_by_opportunity or {}
    for oid, rows in sorted(grouped.items(), key=lambda item: (item[1][0]["trading_date"], item[1][0]["symbol"])):
        by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_stage[row["stage"]].append(row)
        for stage in by_stage:
            raw_stage_opportunities[stage] += 1
        first = {
            stage: min(values, key=lambda row: row["event_timestamp"])
            for stage, values in by_stage.items()
            if stage not in {"DISCORD_ATTEMPTED", "DISCORD_SUCCESSFUL"}
        }

        # Build the causal discovery path backward. A later precursor cannot be
        # reinterpreted as evidence that existed before an earlier board event;
        # similarly, source telemetry that begins later cannot prove the source
        # had evaluated this opportunity earlier.
        board = first.get("BUY_NOW_BOARD_QUALIFIED")
        if board is not None:
            board_time = _timestamp(board["event_timestamp"])
            causal_precursors = [
                event for event in by_stage.get("PRECURSOR_QUALIFIED", [])
                if _timestamp(event["event_timestamp"]) < board_time
            ]
            if causal_precursors:
                first["PRECURSOR_QUALIFIED"] = min(
                    causal_precursors, key=lambda row: row["event_timestamp"]
                )
            else:
                first.pop("PRECURSOR_QUALIFIED", None)
        first_downstream = first.get("PRECURSOR_QUALIFIED") or board
        if first_downstream is not None:
            downstream_time = _timestamp(first_downstream["event_timestamp"])
            causal_evaluations = [
                event for event in by_stage.get("SOURCE_EVALUATED", [])
                if _timestamp(event["event_timestamp"]) <= downstream_time
            ]
            if causal_evaluations:
                first["SOURCE_EVALUATED"] = min(
                    causal_evaluations, key=lambda row: row["event_timestamp"]
                )
            else:
                first.pop("SOURCE_EVALUATED", None)

        # Discord board, goal/trade, and manual-candidate messages are separate
        # paths. Select the message path belonging to the furthest causal stage
        # already reached while retaining every delivery in the raw counters.
        anchor = (
            first.get("GOAL_MODE_DECISION")
            or first.get("BUY_NOW_BOARD_QUALIFIED")
            or first.get("PRECURSOR_QUALIFIED")
        )
        desired_kind = (
            "trade_notification" if first.get("GOAL_MODE_DECISION")
            else "buy_now_board" if first.get("BUY_NOW_BOARD_QUALIFIED")
            else "manual_tradingview_candidate"
        )
        if anchor is not None:
            anchor_time = _timestamp(anchor["event_timestamp"])
            for stage in ("DISCORD_ATTEMPTED", "DISCORD_SUCCESSFUL"):
                eligible = [
                    event
                    for event in by_stage.get(stage, [])
                    if event.get("message_kind") == desired_kind
                    and _timestamp(event["event_timestamp"]) >= anchor_time
                ]
                if eligible:
                    first[stage] = min(
                        eligible, key=lambda row: row["event_timestamp"]
                    )
        # Aggregate reach follows the causally selected path. Raw stage counts
        # below continue to include events removed by causal ordering.
        for stage in first:
            stage_reached[stage] += 1

        order_violations = []
        previous_stage = None
        previous_time = None
        for stage in STAGES:
            event = first.get(stage)
            if event is None:
                continue
            current = _timestamp(event["event_timestamp"])
            if previous_time is not None and current < previous_time:
                order_violations.append(f"{stage}_before_{previous_stage}")
            previous_stage, previous_time = stage, current

        outcomes = {
            stage: stage_outcome(event, bars_by_opportunity.get(oid, []))
            for stage, event in first.items()
        }
        row: dict[str, Any] = {
            "opportunity_id": oid,
            "trading_date": rows[0]["trading_date"],
            "symbol": rows[0]["symbol"],
            "runtime_coverage_status": dict((coverage_by_date or {}).get(rows[0]["trading_date"], {})),
            "stage_order_status": "invalid" if order_violations else "causal",
            "stage_order_violations": order_violations,
            "repeated_board_message_count": max(0, int(
                (raw_counts_by_opportunity or {}).get(
                    (oid, "DISCORD_SUCCESSFUL", "buy_now_board"),
                    sum(
                        event["stage"] == "DISCORD_SUCCESSFUL"
                        and event.get("message_kind") == "buy_now_board"
                        for event in rows
                    ),
                )
            ) - 1),
            "repeated_lifecycle_event_count": max(
                0, int((lifecycle_repeat_counts or {}).get(oid, 0)) - 1
            ),
            "successful_discord_delivery_count": sum(
                int((raw_counts_by_opportunity or {}).get(
                    (oid, "DISCORD_SUCCESSFUL", message_kind), count
                ))
                for message_kind, count in Counter(
                    event.get("message_kind")
                    for event in rows
                    if event["stage"] == "DISCORD_SUCCESSFUL"
                ).items()
            ),
            "successful_board_delivery_count": int(
                (raw_counts_by_opportunity or {}).get(
                    (oid, "DISCORD_SUCCESSFUL", "buy_now_board"),
                    sum(
                        event["stage"] == "DISCORD_SUCCESSFUL"
                        and event.get("message_kind") == "buy_now_board"
                        for event in rows
                    ),
                )
            ),
            "successful_manual_candidate_delivery_count": int(
                (raw_counts_by_opportunity or {}).get(
                    (oid, "DISCORD_SUCCESSFUL", "manual_tradingview_candidate"),
                    sum(
                        event["stage"] == "DISCORD_SUCCESSFUL"
                        and event.get("message_kind") == "manual_tradingview_candidate"
                        for event in rows
                    ),
                )
            ),
        }
        previous_stage = None
        previous_event = None
        for stage in STAGES:
            event = first.get(stage)
            key = stage.lower()
            row[f"first_{key}_at"] = event["event_timestamp"] if event else None
            row[f"{key}_source"] = event["source"] if event else None
            row[f"{key}_move_pct"] = event.get("move_pct") if event else None
            row[f"{key}_outcome"] = outcomes.get(stage)
            if event is not None and previous_event is not None:
                start = _timestamp(previous_event["event_timestamp"])
                end = _timestamp(event["event_timestamp"])
                delay = (end - start).total_seconds()
                delay_key = f"{previous_stage.lower()}_to_{key}_seconds"
                if delay >= 0:
                    row[delay_key] = delay
                    adjacent_delays[f"{previous_stage}->{stage}"].append(delay)
                else:
                    row[delay_key] = None
                before_move = _finite(previous_event.get("move_pct"))
                after_move = _finite(event.get("move_pct"))
                row[f"{previous_stage.lower()}_to_{key}_move_consumed_pct"] = (
                    after_move - before_move
                    if before_move is not None and after_move is not None and delay >= 0
                    else None
                )
            if event is not None:
                previous_stage, previous_event = stage, event
        row["goal_decision"] = first.get("GOAL_MODE_DECISION", {}).get("decision")
        row["goal_decision_reason"] = first.get("GOAL_MODE_DECISION", {}).get("reason")
        diagnostics.append(row)

    opportunities = len(grouped)
    return {
        "schema_version": SCHEMA_VERSION,
        "identity_version": IDENTITY_VERSION,
        "frozen_stage_order": list(STAGES),
        "opportunities": opportunities,
        "stage_counts": {
            stage: {
                "opportunities_reached": stage_reached[stage],
                "raw_opportunities_present": raw_stage_opportunities[stage],
                "opportunity_pct": round(stage_reached[stage] * 100.0 / opportunities, 2) if opportunities else None,
                "raw_events": int((raw_stage_counts or {}).get(
                    stage, sum(event["stage"] == stage for event in raw)
                )),
                "repeated_events": max(
                    0,
                    int((raw_stage_counts or {}).get(
                        stage, sum(event["stage"] == stage for event in raw)
                    )) - raw_stage_opportunities[stage],
                ),
            }
            for stage in STAGES
        },
        "adjacent_stage_delay_seconds": {
            pair: _quantiles(values) for pair, values in sorted(adjacent_delays.items())
        },
        "runtime_coverage_by_date": dict(coverage_by_date or {}),
        "diagnostic_rows": diagnostics,
        "raw_stage_events": raw,
    }


def _expand(patterns: Iterable[str]) -> list[Path]:
    return sorted({Path(path) for pattern in patterns for path in glob.glob(pattern)})


def _live_feature_coverage(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = defaultdict(_coverage_state)
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("When"):
                    parsed = _timestamp(row["When"])
                    _observe_coverage(states[parsed.date().isoformat()], parsed)
    return {day: _coverage_result(state) for day, state in states.items()}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _validate_output_path(
    output: str | Path,
    input_paths: Iterable[str | Path],
    bars_root: str | Path | None = None,
) -> Path:
    """Reject any output that could replace or enter immutable evidence."""
    output_path = Path(output).expanduser()
    resolved_output = output_path.resolve()
    resolved_inputs = {
        Path(path).expanduser().resolve()
        for path in input_paths
        if Path(path).expanduser().is_file()
    }
    same_existing_file = resolved_output.exists() and any(
        os.path.samefile(resolved_output, input_path)
        for input_path in resolved_inputs
    )
    if resolved_output in resolved_inputs or same_existing_file:
        raise ValueError("--output must not overwrite an input evidence file")
    if bars_root:
        bars_path = Path(bars_root).expanduser()
        resolved_bars_root = bars_path.resolve()
        lexical_output = Path(os.path.abspath(output_path))
        lexical_bars_root = Path(os.path.abspath(bars_path))
        if (
            resolved_output == resolved_bars_root
            or resolved_bars_root in resolved_output.parents
            or lexical_output == lexical_bars_root
            or lexical_bars_root in lexical_output.parents
        ):
            raise ValueError("--output must not reside inside --bars-root")
    return resolved_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timing-glob", action="append", default=[])
    parser.add_argument("--lifecycle-events")
    parser.add_argument("--goal-journal-glob", action="append", default=[])
    parser.add_argument("--candidate-delivery-glob", action="append", default=[])
    parser.add_argument("--tradingview-alert-glob", action="append", default=[])
    parser.add_argument("--live-feature-glob", action="append", default=[])
    parser.add_argument("--bars-root")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    timing_paths = _expand(args.timing_glob)
    goal_paths = _expand(args.goal_journal_glob)
    candidate_delivery_paths = _expand(args.candidate_delivery_glob)
    tradingview_alert_paths = _expand(args.tradingview_alert_glob)
    live_feature_paths = _expand(args.live_feature_glob)
    input_paths: list[str | Path] = [
        *timing_paths,
        *goal_paths,
        *candidate_delivery_paths,
        *tradingview_alert_paths,
        *live_feature_paths,
    ]
    if args.lifecycle_events:
        input_paths.append(args.lifecycle_events)
    resolved_output = None
    if args.output:
        try:
            resolved_output = _validate_output_path(
                args.output, input_paths, args.bars_root
            )
        except ValueError as exc:
            parser.error(str(exc))

    events, coverage, raw_stage_counts, raw_counts_by_opportunity = load_timing_compact(
        timing_paths, args.start_date, args.end_date
    )
    lifecycle_counts: Counter = Counter()
    extra_events: list[dict[str, Any]] = []
    if args.lifecycle_events:
        lifecycle_events, lifecycle_counts = events_from_lifecycle(load_jsonl([args.lifecycle_events]))
        extra_events.extend(lifecycle_events)
    extra_events.extend(events_from_goal_journal(goal_paths))
    candidate_events = events_from_candidate_delivery(
        load_jsonl(candidate_delivery_paths)
    )
    # Direct-candidate delivery is durably journaled with attempt state. Avoid
    # counting its mirrored timing rows as additional HTTP sends. Board timing
    # events remain authoritative for the separate board-digest path.
    candidate_observation_ids = {
        event["observation_id"]
        for event in candidate_events
        if event.get("observation_id")
    }
    if candidate_observation_ids:
        candidate_opportunities = {
            event["opportunity_id"] for event in candidate_events
        }
        for oid in candidate_opportunities:
            for stage in ("DISCORD_ATTEMPTED", "DISCORD_SUCCESSFUL"):
                mirrored = int(raw_counts_by_opportunity.pop(
                    (oid, stage, "candidate_or_trade_message"), 0
                ))
                raw_stage_counts[stage] -= mirrored
        events = [
            event
            for event in events
            if not (
                event["stage"] in {"DISCORD_ATTEMPTED", "DISCORD_SUCCESSFUL"}
                and event.get("observation_id") in candidate_observation_ids
                and event["source"].startswith("alert_timing:")
            )
        ]
    extra_events.extend(candidate_events)
    extra_events.extend(events_from_tradingview_alerts(load_jsonl(tradingview_alert_paths)))
    if args.start_date:
        extra_events = [event for event in extra_events if event["trading_date"] >= args.start_date]
    if args.end_date:
        extra_events = [event for event in extra_events if event["trading_date"] <= args.end_date]
    for event in extra_events:
        raw_stage_counts[event["stage"]] += 1
        raw_counts_by_opportunity[
            (event["opportunity_id"], event["stage"], event.get("message_kind"))
        ] += 1
    events.extend(extra_events)
    if args.start_date:
        events = [event for event in events if event["trading_date"] >= args.start_date]
    if args.end_date:
        events = [event for event in events if event["trading_date"] <= args.end_date]

    feature_coverage = _live_feature_coverage(live_feature_paths)
    dates = sorted({event["trading_date"] for event in events})
    for day in dates:
        coverage.setdefault(day, runtime_coverage(day, []))
        coverage[day]["live_feature_recording"] = feature_coverage.get(
            day,
            {"status": "unknown", "first": None, "last": None, "max_gap_seconds": None},
        )
    bars: dict[str, list[dict[str, Any]]] = {}
    if args.bars_root:
        for event in events:
            oid = event["opportunity_id"]
            if oid not in bars:
                bars[oid] = load_minute_bars(args.bars_root, event["trading_date"], event["symbol"])
    result = analyze(
        events,
        bars,
        coverage,
        lifecycle_counts,
        raw_stage_counts,
        raw_counts_by_opportunity,
    )
    if resolved_output is not None:
        _atomic_json(resolved_output, result)
        print(json.dumps({"output": str(resolved_output), "opportunities": result["opportunities"]}, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

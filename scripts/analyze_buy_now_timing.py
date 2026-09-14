#!/usr/bin/env python3
"""Read-only event and opportunity analysis for Buy Now lifecycle timing.

Signal-time alert payloads are joined to terminal future labels by immutable
lifecycle identity. Inputs are never modified. When ``--output`` is supplied,
JSON is written there and a concise summary is printed to stdout. Without an
output path, JSON is printed to stdout and the summary is printed to stderr.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from pkscreener.classes.TopGainerLifecycleRecorder import IST, _timestamp
from scripts.analyze_top_gainer_lifecycle import load_events, validate_terminal_outcomes


SCHEMA_VERSION = 1
BUY_NOW = "Buy Now"
PRECURSOR_TYPES = {"Pre-Move Watch", "Grade-B Anomaly"}
KNOWN_TYPES = (
    "Sell / Exit",
    BUY_NOW,
    "Momentum Buy",
    "Pre-Move Watch",
    "Grade-B Anomaly",
    "Runner Continuation",
    "Grade-A Runner",
)
MFE_THRESHOLDS = (0.5, 1.0, 2.0, 3.0, 5.0)
HORIZONS = (15, 30, 60, 120)


def _alert_type(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if "sell" in lowered and "exit" in lowered:
        return "Sell / Exit"
    if "pre-move watch" in lowered:
        return "Pre-Move Watch"
    if "grade-b anomaly" in lowered:
        return "Grade-B Anomaly"
    if "runner continuation" in lowered:
        return "Runner Continuation"
    if "grade-a runner" in lowered:
        return "Grade-A Runner"
    if lowered == "buy now" or lowered.endswith(" buy now"):
        return BUY_NOW
    if "momentum buy" in lowered:
        return "Momentum Buy"
    return "other"


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _median(values: Iterable[Any]) -> float | None:
    numeric = [value for item in values if (value := _finite(item)) is not None]
    return _rounded(float(median(numeric))) if numeric else None


def _percent(numerator: int, denominator: int) -> float | None:
    return round(numerator * 100.0 / denominator, 2) if denominator else None


def _timestamp_summary(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    stamps = sorted(row["alert_datetime"] for row in rows)
    if not stamps:
        return {
            "first_timestamp": None,
            "median_timestamp": None,
            "last_timestamp": None,
        }
    epochs = [stamp.timestamp() for stamp in stamps]
    middle = datetime.fromtimestamp(float(median(epochs)), tz=IST)
    return {
        "first_timestamp": stamps[0].isoformat(timespec="microseconds"),
        "median_timestamp": middle.isoformat(timespec="microseconds"),
        "last_timestamp": stamps[-1].isoformat(timespec="microseconds"),
    }


def _quantiles(values: Iterable[Any]) -> dict[str, float | None]:
    ordered = sorted(value for item in values if (value := _finite(item)) is not None)

    def at(fraction: float) -> float | None:
        if not ordered:
            return None
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return _rounded(ordered[lower])
        weight = position - lower
        return _rounded(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)

    return {"p25": at(0.25), "p50": at(0.5), "p75": at(0.75)}


def _metric_statistics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [row for row in rows if row["completeness_status"] == "complete"]
    incomplete = [row for row in rows if row["completeness_status"] == "incomplete"]
    unknown_legacy = [
        row for row in rows if row["completeness_status"] == "unknown_legacy"
    ]
    missing = [row for row in rows if row["completeness_status"] == "missing"]
    metric_rows = [
        row for row in complete if row["outcome_metrics_are_post_alert_anchored"]
    ]
    mfe = [row["mfe_pct"] for row in metric_rows if row["mfe_pct"] is not None]
    mae = [row["mae_pct"] for row in metric_rows if row["mae_pct"] is not None]
    time_to_high = [
        row["time_to_later_high_minutes"]
        for row in metric_rows
        if row["time_to_later_high_minutes"] is not None
    ]
    horizon_values = {
        minutes: [
            row[f"return_{minutes}m_pct"]
            for row in metric_rows
            if row[f"return_{minutes}m_pct"] is not None
        ]
        for minutes in HORIZONS
    }
    result: dict[str, Any] = {
        "events": len(rows),
        "complete_outcomes": len(complete),
        "incomplete_outcomes": len(incomplete),
        "unknown_legacy_outcomes": len(unknown_legacy),
        "missing_terminal_outcomes": len(missing),
        "outcome_metric_eligible": len(metric_rows),
        "outcome_not_post_alert_anchored_count": sum(
            row["completeness_status"] == "complete"
            and not row["outcome_metrics_are_post_alert_anchored"]
            for row in rows
        ),
        **_timestamp_summary(rows),
        "median_move_already_achieved_pct": _median(
            row["move_pct"] for row in rows
        ),
        "median_post_alert_mfe_pct": _median(row["mfe_pct"] for row in metric_rows),
        "median_post_alert_mae_pct": _median(row["mae_pct"] for row in metric_rows),
        "median_time_to_later_high_minutes": _median(
            row["time_to_later_high_minutes"] for row in metric_rows
        ),
        "mfe_sample_count": len(mfe),
        "mae_sample_count": len(mae),
        "time_to_later_high_sample_count": len(time_to_high),
    }
    result["post_alert_mfe_threshold_pct"] = {
        str(threshold): _percent(sum(value >= threshold for value in mfe), len(mfe))
        for threshold in MFE_THRESHOLDS
    }
    result["median_return_pct"] = {
        f"{minutes}m": _median(horizon_values[minutes])
        for minutes in HORIZONS
    }
    result["return_sample_count"] = {
        f"{minutes}m": len(horizon_values[minutes]) for minutes in HORIZONS
    }
    return result


def _linked_alert_rows(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    observations = {
        str(row.get("lifecycle_id") or ""): row
        for row in events
        if row.get("event_type") == "candidate_observed"
        and row.get("causal_evaluation_eligible", True)
        and not row.get("historical_reconstruction", False)
    }
    outcomes: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        lifecycle_id = str(row.get("lifecycle_id") or "")
        if row.get("event_type") == "session_outcome_finalized" and lifecycle_id:
            if lifecycle_id in outcomes:
                raise ValueError(f"duplicate terminal outcome for {lifecycle_id}")
            outcomes[lifecycle_id] = row
        elif row.get("event_type") == "data_incomplete" and lifecycle_id:
            diagnostics[lifecycle_id].append(row)

    linked: list[dict[str, Any]] = []
    excluded_unlinked = 0
    for alert in events:
        if alert.get("event_type") != "alert_emitted":
            continue
        observation_id = str(alert.get("observation_id") or "")
        alert_lifecycle_id = str(alert.get("lifecycle_id") or "")
        if (
            observation_id
            and alert_lifecycle_id
            and observation_id != alert_lifecycle_id
        ):
            raise ValueError("alert lifecycle_id and observation_id disagree")
        lifecycle_id = observation_id or alert_lifecycle_id
        observation = observations.get(lifecycle_id)
        if observation is None:
            excluded_unlinked += 1
            continue
        alert_dt = _timestamp(alert.get("event_timestamp"), "alert event timestamp")
        observation_dt = _timestamp(
            observation.get("observation_timestamp"), "observation timestamp"
        )
        linked_observation_timestamp = alert.get("observation_timestamp")
        if linked_observation_timestamp not in (None, ""):
            linked_observation_dt = _timestamp(
                linked_observation_timestamp,
                "alert linked observation timestamp",
            )
            if linked_observation_dt != observation_dt:
                raise ValueError(
                    f"alert observation timestamp does not match {lifecycle_id}"
                )
        if alert_dt < observation_dt:
            raise ValueError(f"alert event precedes observation {lifecycle_id}")
        payload = alert.get("payload") if isinstance(alert.get("payload"), dict) else {}
        fields = (
            observation.get("signal_time_fields")
            if isinstance(observation.get("signal_time_fields"), dict)
            else {}
        )
        raw_type = str(payload.get("alert_type") or "").strip()
        outcome = outcomes.get(lifecycle_id)
        labels = (
            outcome.get("future_labels")
            if outcome is not None and isinstance(outcome.get("future_labels"), dict)
            else None
        )
        if labels is None:
            completeness = "missing"
            labels = {}
        else:
            completeness = (
                str(labels["completeness_status"])
                if "completeness_status" in labels
                else "unknown_legacy"
            )
        diagnostic_payloads = [
            row.get("payload")
            for row in diagnostics.get(lifecycle_id, [])
            if isinstance(row.get("payload"), dict)
        ]
        missing_reason = labels.get("missing_reason")
        if not missing_reason:
            missing_reason = next(
                (
                    item.get("missing_reason") or item.get("reason")
                    for item in diagnostic_payloads
                    if item.get("missing_reason") or item.get("reason")
                ),
                None,
            )
        coverage_issues = labels.get("coverage_issues")
        if not isinstance(coverage_issues, list):
            coverage_issues = next(
                (
                    item.get("coverage_issues")
                    for item in diagnostic_payloads
                    if isinstance(item.get("coverage_issues"), list)
                ),
                [],
            )
        move = _finite(payload.get("return_already_achieved"))
        if move is None:
            move = _finite(fields.get("return_from_previous_close"))
        linked.append(
            {
                "event_id": str(alert.get("event_id") or ""),
                "lifecycle_id": lifecycle_id,
                "trading_date": str(
                    observation.get("trading_date") or alert_dt.date().isoformat()
                ),
                "symbol": str(observation.get("symbol") or "").strip().upper(),
                "raw_alert_type": raw_type or "missing",
                "alert_type": _alert_type(raw_type),
                "alert_datetime": alert_dt,
                "alert_timestamp": alert_dt.isoformat(timespec="microseconds"),
                "observation_timestamp": observation_dt.isoformat(
                    timespec="microseconds"
                ),
                # Terminal labels are calculated from the observation time. A
                # delayed alert is validly linked, but those labels include a
                # pre-alert interval and therefore cannot be called post-alert.
                "outcome_metrics_are_post_alert_anchored": alert_dt == observation_dt,
                "move_pct": move,
                "completeness_status": completeness,
                "missing_reason": str(missing_reason) if missing_reason else None,
                "coverage_issues": [str(item) for item in coverage_issues],
                "mfe_pct": _finite(labels.get("mfe_pct")),
                "mae_pct": _finite(labels.get("mae_pct")),
                "time_to_later_high_minutes": _finite(
                    labels.get("time_to_later_high_minutes")
                ),
                **{
                    f"return_{minutes}m_pct": _finite(
                        labels.get(f"return_{minutes}m_pct")
                    )
                    for minutes in HORIZONS
                },
            }
        )
    linked.sort(key=lambda row: (row["alert_datetime"], row["event_id"]))
    return linked, excluded_unlinked


def _first_by_date_symbol(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        first.setdefault((row["trading_date"], row["symbol"]), row)
    return list(first.values())


def _hour_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[f"{row['alert_datetime'].hour:02d}:00-{row['alert_datetime'].hour:02d}:59"].append(row)
    result = {}
    for hour, items in sorted(grouped.items()):
        complete = [
            row
            for row in items
            if row["completeness_status"] == "complete"
            and row["outcome_metrics_are_post_alert_anchored"]
        ]
        mfe = [row["mfe_pct"] for row in complete if row["mfe_pct"] is not None]
        returns_15m = [
            row["return_15m_pct"]
            for row in complete
            if row["return_15m_pct"] is not None
        ]
        returns_30m = [
            row["return_30m_pct"]
            for row in complete
            if row["return_30m_pct"] is not None
        ]
        result[hour] = {
            "count": len(items),
            "complete_count": sum(
                row["completeness_status"] == "complete" for row in items
            ),
            "incomplete_count": sum(
                row["completeness_status"] == "incomplete" for row in items
            ),
            "unknown_legacy_count": sum(
                row["completeness_status"] == "unknown_legacy" for row in items
            ),
            "missing_terminal_count": sum(
                row["completeness_status"] == "missing" for row in items
            ),
            "median_move_already_achieved_pct": _median(
                row["move_pct"] for row in items
            ),
            "median_post_alert_mfe_pct": _median(row["mfe_pct"] for row in complete),
            "median_post_alert_mae_pct": _median(row["mae_pct"] for row in complete),
            "post_alert_mfe_at_least_1_pct": _percent(
                sum(value >= 1.0 for value in mfe), len(mfe)
            ),
            "post_alert_mfe_at_least_2_pct": _percent(
                sum(value >= 2.0 for value in mfe), len(mfe)
            ),
            "median_return_15m_pct": _median(returns_15m),
            "return_15m_sample_count": len(returns_15m),
            "median_return_30m_pct": _median(returns_30m),
            "return_30m_sample_count": len(returns_30m),
        }
    return result


def _incomplete_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    incomplete = [row for row in rows if row["completeness_status"] == "incomplete"]
    missing_reasons = Counter(
        row["missing_reason"] or "reason_not_recorded" for row in incomplete
    )
    coverage = Counter(
        issue for row in incomplete for issue in row["coverage_issues"]
    )

    def grouped(field: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            key = (
                row[field]
                if field == "alert_type"
                else f"{row['alert_datetime'].hour:02d}:00-{row['alert_datetime'].hour:02d}:59"
            )
            groups[str(key)].append(row)
        result = {}
        rates = []
        for key, items in sorted(groups.items()):
            count = sum(row["completeness_status"] == "incomplete" for row in items)
            rate = _percent(count, len(items))
            if rate is not None:
                rates.append(rate)
            result[key] = {
                "events": len(items),
                "complete": sum(
                    row["completeness_status"] == "complete" for row in items
                ),
                "incomplete": count,
                "unknown_legacy": sum(
                    row["completeness_status"] == "unknown_legacy" for row in items
                ),
                "missing_terminal": sum(
                    row["completeness_status"] == "missing" for row in items
                ),
                "incomplete_rate_pct": rate,
            }
        return {
            "groups": result,
            "descriptive_incomplete_rate_spread_percentage_points": (
                round(max(rates) - min(rates), 2) if rates else None
            ),
            "material_difference_assessment": (
                "not_estimated_from_descriptive single-session counts"
            ),
        }

    return {
        "scope": "all linked alert events; incomplete outcomes are never zeros",
        "alert_events": len(rows),
        "incomplete_count": len(incomplete),
        "incomplete_rate_pct": _percent(len(incomplete), len(rows)),
        "unknown_legacy_count": sum(
            row["completeness_status"] == "unknown_legacy" for row in rows
        ),
        "missing_terminal_count": sum(
            row["completeness_status"] == "missing" for row in rows
        ),
        "missing_reason_counts": dict(sorted(missing_reasons.items())),
        "coverage_issue_counts": dict(sorted(coverage.items())),
        "by_alert_type": grouped("alert_type"),
        "by_hour": grouped("hour"),
    }


def analyze(events_path: Path) -> dict[str, Any]:
    events = load_events(events_path)
    validate_terminal_outcomes(events)
    alerts, excluded_unlinked = _linked_alert_rows(events)
    taxonomy = Counter(row["alert_type"] for row in alerts)
    raw_other = Counter(
        row["raw_alert_type"] for row in alerts if row["alert_type"] == "other"
    )
    counts = {name: taxonomy.get(name, 0) for name in KNOWN_TYPES}
    counts["other"] = taxonomy.get("other", 0)

    raw_buys = [row for row in alerts if row["alert_type"] == BUY_NOW]
    first_buys = _first_by_date_symbol(raw_buys)
    unique_stats = _metric_statistics(first_buys)
    unique_stats.update(
        {
            "raw_buy_now_events": len(raw_buys),
            "unique_date_symbol_first_buys": len(first_buys),
            "repeated_buy_now_count": len(raw_buys) - len(first_buys),
            "analysis_role": "PRIMARY independent-opportunity analysis",
            "production_rule": "research deduplication only; not a production one-buy rule",
        }
    )

    repeated = []
    buys_by_date_symbol: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in raw_buys:
        buys_by_date_symbol[(row["trading_date"], row["symbol"])].append(row)
    for (trading_date, symbol), items in buys_by_date_symbol.items():
        items.sort(key=lambda row: row["alert_datetime"])
        if len(items) <= 1:
            continue
        repeated.append(
            {
                "trading_date": trading_date,
                "symbol": symbol,
                "count": len(items),
                "first_timestamp": items[0]["alert_timestamp"],
                "last_timestamp": items[-1]["alert_timestamp"],
                "elapsed_minutes": _rounded(
                    (items[-1]["alert_datetime"] - items[0]["alert_datetime"])
                    .total_seconds()
                    / 60.0
                ),
                "first_buy_now_move_pct": items[0]["move_pct"],
                "last_buy_now_move_pct": items[-1]["move_pct"],
            }
        )
    repeated.sort(
        key=lambda row: (-row["count"], row["trading_date"], row["symbol"])
    )

    precursors = [row for row in alerts if row["alert_type"] in PRECURSOR_TYPES]
    precursor_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in precursors:
        precursor_groups[(row["trading_date"], row["symbol"])].append(row)
    transitions = []
    for buy in first_buys:
        prior = [
            row
            for row in precursor_groups.get((buy["trading_date"], buy["symbol"]), [])
            if row["alert_datetime"] < buy["alert_datetime"]
        ]
        if not prior:
            continue
        precursor = min(prior, key=lambda row: (row["alert_datetime"], row["event_id"]))
        delay = (buy["alert_datetime"] - precursor["alert_datetime"]).total_seconds() / 60.0
        consumed = (
            buy["move_pct"] - precursor["move_pct"]
            if buy["move_pct"] is not None and precursor["move_pct"] is not None
            else None
        )
        transitions.append(
            {
                "trading_date": buy["trading_date"],
                "symbol": buy["symbol"],
                "precursor_type": precursor["alert_type"],
                "precursor_timestamp": precursor["alert_timestamp"],
                "precursor_move_pct": precursor["move_pct"],
                "buy_now_timestamp": buy["alert_timestamp"],
                "buy_now_move_pct": buy["move_pct"],
                "delay_minutes": _rounded(delay),
                "additional_move_consumed_pct": _rounded(consumed),
                "buy_now_mfe_pct": (
                    buy["mfe_pct"]
                    if buy["completeness_status"] == "complete"
                    and buy["outcome_metrics_are_post_alert_anchored"]
                    else None
                ),
                "buy_now_mae_pct": (
                    buy["mae_pct"]
                    if buy["completeness_status"] == "complete"
                    and buy["outcome_metrics_are_post_alert_anchored"]
                    else None
                ),
                "completeness_status": buy["completeness_status"],
            }
        )

    transition_summary = {
        "precursor_types": sorted(PRECURSOR_TYPES),
        "unique_first_buys": len(first_buys),
        "with_strictly_earlier_precursor": len(transitions),
        "precursor_coverage_pct": _percent(len(transitions), len(first_buys)),
        "median_precursor_to_buy_delay_minutes": _median(
            row["delay_minutes"] for row in transitions
        ),
        "delay_minutes_quantiles": _quantiles(
            row["delay_minutes"] for row in transitions
        ),
        "median_precursor_move_pct": _median(
            row["precursor_move_pct"] for row in transitions
        ),
        "median_buy_now_move_pct": _median(
            row["buy_now_move_pct"] for row in transitions
        ),
        "median_additional_move_consumed_pct": _median(
            row["additional_move_consumed_pct"] for row in transitions
        ),
        "additional_move_consumed_pct_quantiles": _quantiles(
            row["additional_move_consumed_pct"] for row in transitions
        ),
        "transitions": transitions,
    }

    transition_by_key = {
        (row["trading_date"], row["symbol"]): row for row in transitions
    }
    repetition_by_key = Counter(
        (row["trading_date"], row["symbol"]) for row in raw_buys
    )
    per_symbol = []
    for buy in first_buys:
        transition = transition_by_key.get((buy["trading_date"], buy["symbol"]), {})
        complete = (
            buy["completeness_status"] == "complete"
            and buy["outcome_metrics_are_post_alert_anchored"]
        )
        per_symbol.append(
            {
                "trading_date": buy["trading_date"],
                "symbol": buy["symbol"],
                "first_precursor_type": transition.get("precursor_type"),
                "first_precursor_timestamp": transition.get("precursor_timestamp"),
                "first_precursor_move_pct": transition.get("precursor_move_pct"),
                "first_buy_now_timestamp": buy["alert_timestamp"],
                "first_buy_now_move_pct": buy["move_pct"],
                "precursor_to_buy_delay_minutes": transition.get("delay_minutes"),
                "buy_now_mfe_pct": buy["mfe_pct"] if complete else None,
                "buy_now_mae_pct": buy["mae_pct"] if complete else None,
                **{
                    f"return_{minutes}m_pct": (
                        buy[f"return_{minutes}m_pct"] if complete else None
                    )
                    for minutes in HORIZONS
                },
                "time_to_later_high_minutes": (
                    buy["time_to_later_high_minutes"] if complete else None
                ),
                "completeness_status": buy["completeness_status"],
                "buy_now_alert_count": repetition_by_key[
                    (buy["trading_date"], buy["symbol"])
                ],
            }
        )
    per_symbol.sort(key=lambda row: (row["trading_date"], row["symbol"]))

    safeguards = {
        "interpretation_sequence": ["observation", "exploratory evidence", "hypothesis"],
        "validated_edge": False,
        "delivery_semantics": (
            "Lifecycle alert_emitted records identify the recorder's alert boundary; "
            "they do not by themselves prove a user-visible Discord delivery. "
            "Delivery claims require reconciliation with delivery telemetry or journals."
        ),
        "warnings": [
            "One trading day is insufficient for strategy changes.",
            "Event-level duplicate alerts are not independent trades.",
            "MFE is maximum favorable excursion, not realized profitability.",
            "Incomplete and missing outcomes are excluded from outcome statistics, never treated as failures or zeros.",
            "Legacy outcomes without an explicit completeness status are classified as unknown_legacy and excluded from outcome statistics.",
            "A delayed alert remains linked by lifecycle identity, but observation-anchored labels are excluded from post-alert statistics when the alert occurred later.",
            "Brokerage, slippage, taxes, queue position, and execution are not included.",
            "No production threshold should be changed from this analyzer alone.",
            "Hourly results are descriptive and do not establish a time-of-day trading rule.",
        ],
    }

    raw_stats = _metric_statistics(raw_buys)
    raw_stats.update(
        {
            "raw_buy_now_events": len(raw_buys),
            "analysis_role": "EVENT-LEVEL",
            "independence_warning": "Repeated date/symbol alerts are not independent observations.",
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis": "buy_now_timing",
        "source_events": str(events_path),
        "input_event_count": len(events),
        "linked_alert_event_count": len(alerts),
        "excluded_unlinked_alert_count": excluded_unlinked,
        "alert_taxonomy": {
            "counts_by_alert_type": counts,
            "other_observed_types": dict(sorted(raw_other.items())),
            "entry_and_exit_statistics_separated": True,
        },
        "raw_buy_now_event_statistics": raw_stats,
        "unique_first_buy_now_per_date_symbol": unique_stats,
        "buy_now_repetition_analysis": repeated,
        "unique_first_buy_now_by_hour": _hour_analysis(first_buys),
        "precursor_to_buy_now": transition_summary,
        "incomplete_data_audit": _incomplete_audit(alerts),
        "per_symbol_first_buy_now": per_symbol,
        "research_interpretation_safeguards": safeguards,
    }


def _summary_lines(result: dict[str, Any]) -> list[str]:
    raw = result["raw_buy_now_event_statistics"]
    unique = result["unique_first_buy_now_per_date_symbol"]
    transition = result["precursor_to_buy_now"]

    def display(value: Any, suffix: str = "") -> str:
        return "not available" if value is None else f"{value}{suffix}"

    return [
        "Buy Now timing research — exploratory evidence only",
        f"Raw Buy Now events: {raw['raw_buy_now_events']} (event-level; repeats are not independent)",
        f"Unique first Buy Now date/symbol opportunities: {unique['unique_date_symbol_first_buys']}",
        f"Repeated Buy Now events: {unique['repeated_buy_now_count']}",
        "Unknown-legacy outcomes (raw / first-buy): "
        f"{raw['unknown_legacy_outcomes']} / {unique['unknown_legacy_outcomes']} "
        "(excluded from outcome statistics)",
        "First-buy median post-alert MFE / MAE: "
        f"{display(unique['median_post_alert_mfe_pct'], '%')} / "
        f"{display(unique['median_post_alert_mae_pct'], '%')}",
        "Earlier precursor coverage: "
        f"{display(transition['precursor_coverage_pct'], '%')} | median delay "
        f"{display(transition['median_precursor_to_buy_delay_minutes'], ' min')}",
        "Median additional move consumed before Buy Now: "
        f"{display(transition['median_additional_move_consumed_pct'], '%')}",
        "No validated edge or production-rule change is implied.",
    ]


def _write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    source = args.events.resolve()
    if not source.is_file():
        parser.error("--events must name an existing local JSONL file")
    if args.output is not None and args.output.resolve() == source:
        parser.error("--output must not overwrite the lifecycle input")
    result = analyze(source)
    summary = "\n".join(_summary_lines(result))
    if args.output is not None:
        _write_json(args.output, result)
        print(summary)
        print(f"JSON output: {args.output}")
    else:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

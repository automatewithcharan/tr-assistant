#!/usr/bin/env python3
"""Read-only summary of canonical paper top-gainer lifecycle evidence."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

from pkscreener.classes.TopGainerLifecycleRecorder import _timestamp


def load_events(path: Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt lifecycle record at line {line_no}") from exc
        rows.append(row)
    return rows


def terminal_outcome_error(events: list[dict], outcome: dict) -> str | None:
    """Return why a terminal outcome violates the lifecycle contract."""
    lifecycle_id = str(outcome.get("lifecycle_id") or "")
    if not lifecycle_id:
        return "terminal outcome lacks lifecycle identity"
    observations = [
        row
        for row in events
        if row.get("event_type") == "candidate_observed"
        and str(row.get("lifecycle_id") or "") == lifecycle_id
    ]
    if len(observations) != 1:
        return f"expected one original lifecycle observation, found {len(observations)}"
    observation = observations[0]
    observation_id = str(observation.get("lifecycle_id") or "")
    if (
        not observation_id
        or outcome.get("lifecycle_id") != observation.get("lifecycle_id")
        or outcome.get("observation_id") != observation_id
        or outcome.get("observation_timestamp")
        != observation.get("observation_timestamp")
    ):
        return "terminal outcome observation identity does not match original observation"
    if (
        outcome.get("origin_source_lane") != observation.get("source_lane")
        or outcome.get("experiment_version") != observation.get("experiment_version")
    ):
        return "terminal outcome lifecycle identity does not match original observation"
    labels = outcome.get("future_labels")
    if not isinstance(labels, dict):
        return "terminal outcome lacks finalized future labels"
    if outcome.get("payload") != labels:
        return "terminal outcome payload does not match finalized future labels"
    if (
        "completeness_status" in labels
        and labels["completeness_status"] not in {"complete", "incomplete"}
    ):
        return "terminal outcome has invalid completeness status"
    return None


def incomplete_diagnostic_error(events: list[dict], outcome: dict) -> str | None:
    """Return why an incomplete terminal outcome lacks its one EOD diagnostic."""
    if error := terminal_outcome_error(events, outcome):
        return error
    lifecycle_id = str(outcome.get("lifecycle_id") or "")
    observation = next(
        row
        for row in events
        if row.get("event_type") == "candidate_observed"
        and str(row.get("lifecycle_id") or "") == lifecycle_id
    )
    observation_id = lifecycle_id
    labels = outcome["future_labels"]
    diagnostics = [
        row
        for row in events
        if row.get("event_type") == "data_incomplete"
        and str(row.get("lifecycle_id") or "") == lifecycle_id
        and row.get("source_lane") == "eod_enrichment"
    ]
    if len(diagnostics) != 1:
        return f"expected one linked EOD data_incomplete diagnostic, found {len(diagnostics)}"
    diagnostic = diagnostics[0]
    diagnostic_payload = diagnostic.get("payload")
    if not isinstance(diagnostic_payload, dict):
        return "linked EOD data_incomplete diagnostic lacks a payload"
    missing_reason = diagnostic_payload.get("missing_reason")
    if not isinstance(missing_reason, str) or not missing_reason:
        return "incomplete evidence lacks an explicit missing reason"
    expected_payload = {
        "completeness_status": "incomplete",
        "missing_reason": missing_reason,
    }
    if "coverage_issues" in labels:
        expected_payload["coverage_issues"] = labels["coverage_issues"]
    if (
        "missing_reason" in labels
        and labels["missing_reason"] != missing_reason
    ):
        return "terminal and diagnostic missing reasons do not match"
    try:
        diagnostic_dt = _timestamp(
            diagnostic.get("event_timestamp"), "diagnostic event timestamp"
        )
        observation_dt = _timestamp(
            observation.get("observation_timestamp"), "observation timestamp"
        )
    except ValueError:
        return "linked EOD data_incomplete diagnostic has an invalid timestamp"
    if diagnostic_dt < observation_dt:
        return "linked EOD data_incomplete diagnostic precedes original observation"
    if (
        diagnostic.get("lifecycle_id") != observation.get("lifecycle_id")
        or diagnostic.get("observation_id") != observation_id
        or diagnostic.get("observation_timestamp")
        != observation.get("observation_timestamp")
        or diagnostic.get("origin_source_lane") != observation.get("source_lane")
        or diagnostic.get("experiment_version")
        != observation.get("experiment_version")
        or diagnostic.get("payload") != expected_payload
        or diagnostic.get("paper_only") is not True
    ):
        return "linked EOD data_incomplete diagnostic does not match terminal outcome"
    return None


def validate_terminal_outcomes(events: list[dict]) -> None:
    """Validate the shared, backward-compatible terminal-event contract."""
    errors = []
    for index, outcome in enumerate(
        row
        for row in events
        if row.get("event_type") == "session_outcome_finalized"
    ):
        error = terminal_outcome_error(events, outcome)
        if error is not None:
            identifier = str(outcome.get("event_id") or f"terminal-index-{index}")
            errors.append(f"{identifier}: {error}")
    if errors:
        raise ValueError(f"invalid terminal lifecycle outcome: {errors[:5]}")


def summarize(path: Path) -> dict:
    events = load_events(path)
    validate_terminal_outcomes(events)
    all_observations = [e for e in events if e.get("event_type") == "candidate_observed"]
    observations = [e for e in all_observations
                    if e.get("causal_evaluation_eligible", True)
                    and not e.get("historical_reconstruction", False)]
    observations_by_id = {str(e.get("lifecycle_id")): e for e in observations}
    by_lane = Counter(str(e.get("source_lane", "unknown")) for e in observations)
    event_counts = Counter((str(e.get("event_type")), str(e.get("source_lane", "unknown"))) for e in events)
    dates = {str(e.get("trading_date", "")) for e in observations if e.get("trading_date")}
    symbols = {str(e.get("symbol", "")) for e in observations if e.get("symbol")}
    outcome_events = [e for e in events if e.get("event_type") == "session_outcome_finalized"
                      and e.get("lifecycle_id") in observations_by_id]
    complete_outcome_events = [e for e in outcome_events
                               if (e.get("future_labels") or {}).get("completeness_status", "complete") == "complete"]
    outcomes_by_id = Counter(str(e.get("lifecycle_id")) for e in outcome_events)
    eligible_by_date = defaultdict(set)
    for observation in observations:
        if observation.get("trading_date") and observation.get("lifecycle_id"):
            eligible_by_date[str(observation["trading_date"])].add(
                str(observation["lifecycle_id"])
            )
    session_finalization = {}
    for trading_date, lifecycle_ids in sorted(eligible_by_date.items()):
        date_outcomes = [
            row for row in outcome_events
            if str(row.get("lifecycle_id")) in lifecycle_ids
        ]
        complete_count = sum(
            (row.get("future_labels") or {}).get(
                "completeness_status", "complete"
            )
            == "complete"
            for row in date_outcomes
        )
        incomplete_count = sum(
            (row.get("future_labels") or {}).get("completeness_status") == "incomplete"
            for row in date_outcomes
        )
        exactly_once = all(outcomes_by_id[identifier] == 1 for identifier in lifecycle_ids)
        valid_terminal_statuses = (
            complete_count + incomplete_count == len(lifecycle_ids)
        )
        session_finalization[trading_date] = {
            "eligible_observations": len(lifecycle_ids),
            "terminal_outcomes": len(date_outcomes),
            "complete_outcomes": complete_count,
            "incomplete_outcomes": incomplete_count,
            "exactly_one_terminal_outcome_per_eligible_observation": exactly_once,
            "valid_terminal_statuses": valid_terminal_statuses,
            "finalized": (
                bool(lifecycle_ids)
                and exactly_once
                and valid_terminal_statuses
            ),
        }
    labels = [e.get("future_labels") or {} for e in complete_outcome_events]
    def pct(name: str) -> float | None:
        vals = [float(row[name]) for row in labels if row.get(name) is not None]
        return round(sum(vals) * 100.0 / len(vals), 2) if vals else None
    mfe = [float(row["mfe_pct"]) for row in labels if row.get("mfe_pct") is not None]
    mae = [float(row["mae_pct"]) for row in labels if row.get("mae_pct") is not None]
    upside = [float(row["remaining_upside_pct"]) for row in labels if row.get("remaining_upside_pct") is not None]
    def label_pct(name: str) -> float | None:
        return pct(name)
    alert_events = [e for e in events if e.get("event_type") == "alert_emitted"
                    and e.get("observation_id", e.get("lifecycle_id")) in observations_by_id]
    alerted_ids = {str(e.get("observation_id", e.get("lifecycle_id"))) for e in alert_events}
    labels_by_id = {str(e.get("lifecycle_id")): (e.get("future_labels") or {}) for e in complete_outcome_events}
    missing_by_lane = {}
    for observation in observations:
        lane = str(observation.get("source_lane", "unknown"))
        reasons = observation.get("signal_time_fields", {}).get("missing_reasons") or []
        for reason in reasons:
            missing_by_lane.setdefault(lane, Counter())[str(reason)] += 1
    # Lane overlap is counted once per date/symbol/time window. The lane set is
    # retained so two symbols with the same lane pair remain two cases.
    overlap_cases = []
    grouped = {}
    for observation in observations:
        try:
            stamp = datetime.fromisoformat(str(observation["observation_timestamp"]))
        except (KeyError, ValueError):
            continue
        grouped.setdefault((observation.get("trading_date"), observation.get("symbol")), []).append((stamp, observation.get("source_lane")))
    for (trading_date, symbol), rows in grouped.items():
        rows.sort(key=lambda item: item[0])
        window_start = None
        lanes = set()
        for stamp, lane in rows:
            if window_start is None or (stamp - window_start).total_seconds() > 60:
                if len(lanes) > 1:
                    overlap_cases.append({"trading_date": trading_date, "symbol": symbol,
                                          "lanes": sorted(lanes)})
                window_start, lanes = stamp, {lane}
            else:
                lanes.add(lane)
        if len(lanes) > 1:
            overlap_cases.append({"trading_date": trading_date, "symbol": symbol,
                                  "lanes": sorted(lanes)})

    candidate_alerts = {}
    for alert in alert_events:
        key = str(alert.get("observation_id", alert.get("lifecycle_id")))
        observation = observations_by_id.get(key)
        if observation is None:
            continue
        candidate_key = (observation.get("trading_date"), observation.get("symbol"), "first_alert")
        candidate_alerts.setdefault(candidate_key, []).append(key)
    complete_alerted_candidates = {
        candidate for candidate, ids in candidate_alerts.items()
        if any(identifier in labels_by_id for identifier in ids)
    }
    successful_alerted_candidates = {
        candidate for candidate, ids in candidate_alerts.items()
        if any(labels_by_id.get(identifier, {}).get("reached_5pct") is True for identifier in ids)
    }
    incomplete_alerted_candidates = set(candidate_alerts) - complete_alerted_candidates

    precision = {
        "value_pct": round(len(successful_alerted_candidates) * 100.0 / len(complete_alerted_candidates), 2) if complete_alerted_candidates else "not_available",
        "reason": "no complete alerted candidates" if not complete_alerted_candidates else "unique alerted candidates reaching 5% / unique alerted candidates with complete outcomes",
        "runner_threshold_pct": 5,
        "checkpoint": "first_alert",
        "denominator": len(complete_alerted_candidates),
        "unique_alerted_candidates": len(candidate_alerts),
        "unique_alerted_candidates_reaching_threshold": len(successful_alerted_candidates),
        "excluded_incomplete_candidates": len(incomplete_alerted_candidates),
        "observation_level_alert_count": len(alert_events),
    }
    recall = {
        "value_pct": "not_available",
        "reason": "unbiased eligible universe and actual_runner labels are not present in the lifecycle evidence",
        "denominator": 0,
    }
    detection_latencies = []
    for alert in alert_events:
        key = str(alert.get("observation_id", alert.get("lifecycle_id")))
        observation = observations_by_id.get(key)
        if not observation:
            continue
        try:
            detection_latencies.append((datetime.fromisoformat(str(alert["event_timestamp"])) - datetime.fromisoformat(str(observation["observation_timestamp"]))).total_seconds() / 60.0)
        except (KeyError, ValueError):
            continue
    lane_comparison = {}
    for lane in ("top_gainer_radar", "early_breakout"):
        lane_observations = [row for row in observations if row.get("source_lane") == lane]
        lane_ids = {str(row.get("lifecycle_id")) for row in lane_observations}
        lane_alerts = [row for row in alert_events if str(row.get("observation_id", row.get("lifecycle_id"))) in lane_ids]
        lane_mfe = [float(labels_by_id[key]["mfe_pct"]) for key in lane_ids
                    if labels_by_id.get(key, {}).get("mfe_pct") is not None]
        lane_upside = [float(labels_by_id[key]["remaining_upside_pct"]) for key in lane_ids
                       if labels_by_id.get(key, {}).get("remaining_upside_pct") is not None]
        lane_moves = [float((row.get("signal_time_fields") or {}).get("return_from_previous_close"))
                      for row in lane_observations
                      if (row.get("signal_time_fields") or {}).get("return_from_previous_close") is not None]
        complete_lane_ids = {key for key in lane_ids if key in labels_by_id}
        incomplete_lane_ids = {key for key in lane_ids if key not in complete_lane_ids}
        ineligible_lane_ids = {str(row.get("lifecycle_id")) for row in all_observations
                               if row.get("source_lane") == lane and str(row.get("lifecycle_id")) not in lane_ids}
        lane_comparison[lane] = {
            "unique_candidates": len({(row.get("trading_date"), row.get("symbol")) for row in lane_observations}),
            "observations": len(lane_observations),
            "alerts": len(lane_alerts),
            "median_observation_timestamp": (sorted(row.get("observation_timestamp") for row in lane_observations)[len(lane_observations) // 2]
                                               if lane_observations else "not_available"),
            "median_move_already_achieved_pct": median(lane_moves) if lane_moves else "not_available",
            "complete_outcomes": len(complete_lane_ids),
            "incomplete_outcomes": len(incomplete_lane_ids),
            "reconstructed_or_ineligible_outcomes": len(ineligible_lane_ids),
            "median_post_observation_mfe_pct": median(lane_mfe) if lane_mfe else "not_available",
            "reaching_3_pct": (round(sum(bool(labels_by_id[key].get("reached_3pct")) for key in complete_lane_ids) * 100.0 / len(complete_lane_ids), 2)
                               if complete_lane_ids else "not_available"),
            "reaching_5_pct": (round(sum(bool(labels_by_id[key].get("reached_5pct")) for key in complete_lane_ids) * 100.0 / len(complete_lane_ids), 2)
                               if complete_lane_ids else "not_available"),
            "reaching_10_pct": (round(sum(bool(labels_by_id[key].get("reached_10pct")) for key in complete_lane_ids) * 100.0 / len(complete_lane_ids), 2)
                                if complete_lane_ids else "not_available"),
            "median_remaining_upside_pct": median(lane_upside) if lane_upside else "not_available",
            "confirmation_pass_rate": "not_available",
            "paper_entry_rate": "not_available",
            "incomplete_outcome_rate": (round(len(incomplete_lane_ids) * 100.0 / len(lane_ids), 2)
                                         if lane_ids else "not_available"),
        }
    return {
        "unique_trading_dates": len(dates), "observations": len(observations),
        "unique_symbols": len(symbols), "observations_by_lane": dict(by_lane),
        "events_by_lane": {f"{event}:{lane}": count for (event, lane), count in sorted(event_counts.items())},
        "alerts_by_lane": {lane: count for (event, lane), count in event_counts.items() if event == "alert_emitted"},
        "confirmations_by_lane": {lane: count for (event, lane), count in event_counts.items() if event.startswith("confirmation_")},
        "paper_entries_by_lane": {lane: count for (event, lane), count in event_counts.items() if event == "paper_entry_assumed"},
        "reaching_3_pct": pct("reached_3pct"), "reaching_5_pct": pct("reached_5pct"),
        "reaching_10_pct": pct("reached_10pct"),
        "median_mfe_pct": median(mfe) if mfe else None, "median_mae_pct": median(mae) if mae else None,
        "median_remaining_upside_pct": median(upside) if upside else None,
        "top5_recall_pct": label_pct("top5_recall"),
        "top10_recall_pct": label_pct("top10_recall"),
        "top20_recall_pct": label_pct("top20_recall"),
        "precision_alerted_candidates_pct": precision["value_pct"],
        "precision_contract": precision,
        "recall_contract": recall,
        "median_detection_latency_minutes": median(detection_latencies) if detection_latencies else "not_available",
        "median_detection_time_minutes": "not_available",
        "detection_time_labels_ignored": True,
        "duplicate_lane_overlap_count": len(overlap_cases),
        "lane_overlap_cases": overlap_cases,
        "unique_date_symbol_candidates": len({(e.get("trading_date"), e.get("symbol")) for e in observations}),
        "incomplete_outcome_count": sum(
            1
            for e in outcome_events
            if (e.get("future_labels") or {}).get(
                "completeness_status", "complete"
            )
            != "complete"
        ),
        "finalized_sessions": sum(
            bool(row["finalized"]) for row in session_finalization.values()
        ),
        "session_finalization": session_finalization,
        "missing_data_rates_by_field_and_lane": {lane: dict(counts) for lane, counts in missing_by_lane.items()},
        "lane_comparison": lane_comparison,
        "signal_time_fields_only": True,
        "future_labels_used_for_ranking": False,
        "paper_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", default="results/Data/top_gainer_lifecycle/events.jsonl")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result = summarize(Path(args.events))
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

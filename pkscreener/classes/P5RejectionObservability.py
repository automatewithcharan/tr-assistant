"""Fail-open, append-only observability for frozen P5 evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from pkscreener.classes.NGP5EarlyWatchPublisher import (
    P5_END_MINUTE,
    P5_MAX_DAY_CHANGE_PCT,
    P5_MAX_RANK,
    P5_MIN_DAY_CHANGE_PCT,
    P5_MIN_MOVE_DELTA_PCT,
    P5_MIN_PRICE,
    P5_MIN_RANK_IMPROVEMENT,
    P5_MIN_TURNOVER,
    P5_START_MINUTE,
)


SCHEMA_VERSION = 1
OBSERVER_BUILD = "ng-p5-rejection-observability-v1"


def _safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    text = str(value).strip()
    return text or None


def _gate(observed: Any, threshold: Any, passed: Optional[bool]) -> dict[str, Any]:
    safe_observed = _safe(observed)
    status = "unknown" if safe_observed is None or passed is None else (
        "pass" if passed else "fail"
    )
    return {
        "evaluated": True,
        "status": status,
        "threshold": _safe(threshold),
        "observed_value": safe_observed,
    }


def build_p5_rejection_event(
    *,
    evaluation_inputs: Mapping[str, Any],
    qualification_result: bool,
    qualification_reason: str,
    source_record: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Describe all existing P5 gates from a detached evaluation snapshot."""
    inputs = {str(key): _safe(value) for key, value in evaluation_inputs.items()}
    timestamp = inputs.get("observation_timestamp")
    session_date = str(inputs.get("session_date") or "")
    symbol = str(inputs.get("symbol") or "").strip().upper()
    opening = inputs.get("was_major_opening_leader")
    price = inputs.get("last_price")
    change = inputs.get("day_change_pct")
    turnover = inputs.get("turnover")
    rank = inputs.get("current_rank")
    improvement = inputs.get("rank_improvement")
    move_delta = inputs.get("move_delta_pct")
    exchange = inputs.get("exchange")
    bucket = inputs.get("time_bucket")

    gates = {
        "session_window": _gate(
            {"timestamp": timestamp, "time_bucket": bucket} if timestamp else None,
            f"{P5_START_MINUTE} <= IST minute < {P5_END_MINUTE}",
            bool(bucket) if timestamp else None,
        ),
        "symbol_present": _gate(
            symbol or None, "non-empty", bool(symbol) if symbol else None
        ),
        "exchange_nse": _gate(
            exchange, "NSE", exchange == "NSE" if exchange else None
        ),
        "opening_leader_exclusion": _gate(
            opening, False, (not opening) if isinstance(opening, bool) else None
        ),
        "minimum_price": _gate(
            price,
            {"operator": ">=", "value": P5_MIN_PRICE},
            price >= P5_MIN_PRICE if price is not None else None,
        ),
        "day_change_band": _gate(
            change,
            {"operator": "between_inclusive", "min": P5_MIN_DAY_CHANGE_PCT, "max": P5_MAX_DAY_CHANGE_PCT},
            P5_MIN_DAY_CHANGE_PCT <= change <= P5_MAX_DAY_CHANGE_PCT if change is not None else None,
        ),
        "minimum_turnover": _gate(
            turnover,
            {"operator": ">=", "value": P5_MIN_TURNOVER},
            turnover >= P5_MIN_TURNOVER if turnover is not None else None,
        ),
        "maximum_rank": _gate(
            rank,
            {"operator": "between_inclusive", "min": 1, "max": P5_MAX_RANK},
            1 <= rank <= P5_MAX_RANK if rank is not None else None,
        ),
        "minimum_rank_improvement": _gate(
            improvement,
            {"operator": ">=", "value": P5_MIN_RANK_IMPROVEMENT},
            improvement >= P5_MIN_RANK_IMPROVEMENT if improvement is not None else None,
        ),
        "minimum_move_delta": _gate(
            move_delta,
            {"operator": ">=", "value": P5_MIN_MOVE_DELTA_PCT},
            move_delta >= P5_MIN_MOVE_DELTA_PCT if move_delta is not None else None,
        ),
    }
    reason_by_gate = {
        "session_window": "outside 09:30–15:00 IST",
        "symbol_present": "missing symbol",
        "exchange_nse": "exchange is not NSE",
        "opening_leader_exclusion": "major opening leader",
        "minimum_price": "price below ₹50",
        "day_change_band": "day change outside +1.0% to +3.5%",
        "minimum_turnover": "turnover below ₹3 crore",
        "maximum_rank": "rank above 200 or invalid",
        "minimum_rank_improvement": "rank improvement below 25",
        "minimum_move_delta": "move delta below +0.5 point",
    }
    unknown = [name for name, gate in gates.items() if gate["status"] == "unknown"]
    failed = [name for name, gate in gates.items() if gate["status"] == "fail"]
    if qualification_result:
        decision = "QUALIFIED"
        primary = None
        reasons: list[str] = []
    elif unknown:
        decision = "NOT_EVALUABLE"
        primary = str(qualification_reason or "unknown")
        reasons = list(
            dict.fromkeys(
                [primary]
                + [f"{name}: UNKNOWN" for name in unknown]
                + [reason_by_gate[name] for name in failed]
            )
        )
    else:
        decision = "REJECTED"
        primary = str(qualification_reason or "unknown")
        reasons = list(
            dict.fromkeys([primary] + [reason_by_gate[name] for name in failed])
        )

    candidate_key = f"{session_date}|{symbol}|{inputs.get('policy_version')}"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "trading_date": session_date or None,
        "timestamp_ist": timestamp,
        "symbol": symbol or None,
        "candidate_id": hashlib.sha256(candidate_key.encode("utf-8")).hexdigest(),
        "source": "ng_p5_early_watch",
        "selector_version": inputs.get("policy_version"),
        "observer_build": OBSERVER_BUILD,
        "inputs": inputs,
        "gates": gates,
        "final_decision": decision,
        "primary_rejection_reason": primary,
        "all_rejection_reasons": reasons,
        "source_record": _safe(source_record or {}),
    }


class P5RejectionObserver:
    """Append detached P5 traces; every failure is contained."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self._lock = threading.Lock()

    def record(self, **snapshot: Any) -> bool:
        try:
            event = build_p5_rejection_event(**snapshot)
            if not event["symbol"] or not event["trading_date"]:
                return False
            payload = (
                json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            journal_dir = self.output_dir / "p5_rejection_observability"
            journal_dir.mkdir(parents=True, exist_ok=True)
            path = journal_dir / f"{event['trading_date']}.jsonl"
            with self._lock:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
            return True
        except Exception:
            return False


def summarize_by_date_symbol(
    events: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep repeats while exposing each date-symbol's first P5 pass."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for event in events:
        key = (
            str(event.get("trading_date") or ""),
            str(event.get("symbol") or "").upper(),
        )
        if all(key):
            groups.setdefault(key, []).append(event)
    rows = []
    for (day, symbol), group in sorted(groups.items()):
        ordered = sorted(
            group, key=lambda event: str(event.get("timestamp_ist") or "")
        )
        rows.append(
            {
                "trading_date": day,
                "symbol": symbol,
                "evaluation_count": len(ordered),
                "first_evaluated_at": ordered[0].get("timestamp_ist"),
                "last_evaluated_at": ordered[-1].get("timestamp_ist"),
                "first_qualified_at": next(
                    (
                        event.get("timestamp_ist")
                        for event in ordered
                        if event.get("final_decision") == "QUALIFIED"
                    ),
                    None,
                ),
            }
        )
    return rows

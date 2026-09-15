"""Fail-open, append-only observability for Buy Now selector decisions.

The observer receives only signal-time values already computed by the live
selector.  It neither reads market history nor returns a value used by the
selector.  Journals are written below
``<output_dir>/buy_now_rejection_observability``.
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import math
import os
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SCHEMA_VERSION = 1
SELECTOR_BUILD = "actionable-report-buy-now-v1"
DECISIONS = frozenset({"QUALIFIED", "REJECTED", "NOT_EVALUABLE"})
GATE_STATUSES = frozenset({"pass", "fail", "unknown"})
ENV_FLAG = "SIGNALMONITOR_ENABLE_BUY_NOW_REJECTION_OBSERVABILITY"


def enabled_from_environment(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Require an explicit truthy flag; absence is always disabled."""
    source = os.environ if environ is None else environ
    return str(source.get(ENV_FLAG, "") or "").strip().lower() in {
        "1", "true", "yes", "y", "on",
    }


def _finite(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    if isinstance(value, datetime):
        timestamp = value if value.tzinfo is not None else value.replace(tzinfo=IST)
        return timestamp.astimezone(IST).isoformat(timespec="microseconds")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    text = str(value).strip()
    return text or None


def _timestamp(value: Any, clock: Callable[[], datetime]) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif value not in (None, ""):
        try:
            result = datetime.fromisoformat(str(value).strip())
        except ValueError:
            result = clock()
    else:
        result = clock()
    if result.tzinfo is None:
        result = result.replace(tzinfo=IST)
    return result.astimezone(IST)


def _gate(
    *,
    observed_value: Any,
    threshold: Any,
    passed: Optional[bool],
    evaluated: bool = True,
) -> dict[str, Any]:
    observed = _json_safe(observed_value)
    threshold_value = _json_safe(threshold)
    if not evaluated:
        status = "unknown"
    elif passed is None or observed is None:
        status = "unknown"
    else:
        status = "pass" if bool(passed) else "fail"
    return {
        "evaluated": bool(evaluated),
        "status": status,
        "threshold": threshold_value,
        "observed_value": observed,
    }


def _boolean_gate(value: Any, *, evaluated: bool = True) -> dict[str, Any]:
    observed = value if isinstance(value, bool) else None
    return _gate(
        observed_value=observed,
        threshold=True,
        passed=observed,
        evaluated=evaluated,
    )


def build_selector_event(
    *,
    symbol: str,
    source: str,
    event_timestamp: Any,
    selector_inputs: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    final_action: Optional[str],
    base_action: Optional[str] = None,
    prefilter_inputs: Optional[Mapping[str, Any]] = None,
    decision_override: Optional[str] = None,
    primary_rejection_reason: Optional[str] = None,
    evaluation_mode: str = "computed",
    clock: Optional[Callable[[], datetime]] = None,
) -> dict[str, Any]:
    """Build one immutable selector trace from already-available values."""
    now = clock or (lambda: datetime.now(tz=IST))
    timestamp = _timestamp(event_timestamp, now)
    trading_date = timestamp.date().isoformat()
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_source = str(source or "actionable_report").strip().lower()
    inputs = {str(key): _json_safe(value) for key, value in selector_inputs.items()}
    limits = {str(key): _json_safe(value) for key, value in thresholds.items()}
    prefilter = dict(prefilter_inputs or {})

    gates: dict[str, dict[str, Any]] = {}
    history_count = _finite(prefilter.get("history_count"))
    history_min = _finite(limits.get("prefilter_min_candles"))
    gates["history"] = _gate(
        observed_value=history_count,
        threshold={"operator": ">=", "value": history_min},
        passed=(history_count >= history_min) if history_count is not None and history_min is not None else None,
        evaluated=bool(prefilter),
    )
    price = _finite(prefilter.get("close"))
    price_min = _finite(limits.get("prefilter_min_price"))
    price_max = _finite(limits.get("prefilter_max_price"))
    gates["price_range"] = _gate(
        observed_value=price,
        threshold={"operator": "between_inclusive", "min": price_min, "max": price_max},
        passed=(price_min <= price <= price_max)
        if price is not None and price_min is not None and price_max is not None else None,
        evaluated=bool(prefilter),
    )
    avg_volume = _finite(prefilter.get("avg_volume_20"))
    avg_volume_min = _finite(limits.get("prefilter_min_avg_volume"))
    volume_column_present = prefilter.get("volume_column_present")
    gates["prefilter_average_volume"] = _gate(
        observed_value=avg_volume,
        threshold={"operator": ">=", "value": avg_volume_min},
        passed=(avg_volume >= avg_volume_min)
        if avg_volume is not None and avg_volume_min is not None else None,
        evaluated=bool(prefilter) and volume_column_present is True,
    )
    prefilter_trade_value = _finite(prefilter.get("avg_trade_value_20"))
    prefilter_trade_min = _finite(limits.get("prefilter_min_avg_trade_value"))
    gates["prefilter_average_traded_value"] = _gate(
        observed_value=prefilter_trade_value,
        threshold={"operator": ">=", "value": prefilter_trade_min},
        passed=(prefilter_trade_value >= prefilter_trade_min)
        if prefilter_trade_value is not None and prefilter_trade_min is not None else None,
        evaluated=bool(prefilter) and volume_column_present is True,
    )
    spread = _finite(prefilter.get("last_bar_spread_pct"))
    spread_max = _finite(limits.get("prefilter_max_effective_spread_pct"))
    gates["prefilter_last_bar_spread"] = _gate(
        observed_value=spread,
        threshold={"operator": "<=", "value": spread_max},
        passed=(spread <= spread_max) if spread is not None and spread_max is not None else None,
        evaluated=bool(prefilter) and prefilter.get("last_low_positive") is True,
    )

    adjusted_action = str(inputs.get("adjusted_action") or "").strip().upper()
    raw_action = str(inputs.get("raw_action") or "").strip().upper()
    buy_family = adjusted_action in {"BUY", "WATCH BUY"}
    gates["source_action_buy_family"] = _gate(
        observed_value=adjusted_action or None,
        threshold={"allowed": ["BUY", "WATCH BUY"]},
        passed=buy_family if adjusted_action else None,
    )
    kalman_quality = _finite(inputs.get("kalman_quality"))
    kalman_trend = str(inputs.get("kalman_trend") or "").strip().lower() or None
    noise_spike = inputs.get("noise_spike") if isinstance(inputs.get("noise_spike"), bool) else None
    anti_junk_down_clear = None
    if raw_action in {"BUY", "WATCH BUY"} and kalman_quality is not None and kalman_trend is not None:
        anti_junk_down_clear = not (kalman_trend == "down" and kalman_quality < 30.0)
    gates["anti_junk_downtrend"] = _gate(
        observed_value={"raw_action": raw_action or None, "kalman_trend": kalman_trend, "kalman_quality": kalman_quality},
        threshold={"reject_when": {"trend": "down", "quality_below": 30.0}},
        passed=anti_junk_down_clear,
        evaluated=raw_action in {"BUY", "WATCH BUY"},
    )
    noise_clear = None
    if raw_action in {"BUY", "WATCH BUY"} and noise_spike is not None and kalman_quality is not None:
        noise_clear = not (noise_spike and kalman_quality < 40.0)
    gates["anti_junk_noise_spike"] = _gate(
        observed_value={"noise_spike": noise_spike, "kalman_quality": kalman_quality},
        threshold={"demote_when": {"noise_spike": True, "quality_below": 40.0}},
        passed=noise_clear,
        evaluated=raw_action in {"BUY", "WATCH BUY"},
    )

    bearish_trend = inputs.get("bearish_trend") if isinstance(inputs.get("bearish_trend"), bool) else None
    stretched_chase = inputs.get("stretched_chase") if isinstance(inputs.get("stretched_chase"), bool) else None
    kalman_safety_ok = inputs.get("kalman_safety_ok") if isinstance(inputs.get("kalman_safety_ok"), bool) else None
    volatility_ok = inputs.get("volatility_regime_ok") if isinstance(inputs.get("volatility_regime_ok"), bool) else None
    gates["bearish_trend_clear"] = _gate(
        observed_value=bearish_trend,
        threshold=False,
        passed=(not bearish_trend) if bearish_trend is not None else None,
        evaluated=buy_family,
    )
    gates["stretched_chase_clear"] = _gate(
        observed_value=stretched_chase,
        threshold=False,
        passed=(not stretched_chase) if stretched_chase is not None else None,
        evaluated=buy_family,
    )
    gates["kalman_safety"] = _boolean_gate(kalman_safety_ok, evaluated=buy_family)
    gates["volatility_regime"] = _boolean_gate(volatility_ok, evaluated=buy_family)
    standard_route_enabled = inputs.get("standard_buy_route_enabled")
    gates["standard_buy_route_enabled"] = _gate(
        observed_value=inputs.get("strong_buy_only"),
        threshold={"strong_buy_only": False},
        passed=(
            standard_route_enabled
            if isinstance(standard_route_enabled, bool) else None
        ),
        evaluated=buy_family,
    )
    promotion_enabled = inputs.get("momentum_promotion_enabled")
    gates["momentum_promotion_enabled"] = _gate(
        observed_value=inputs.get("strong_buy_only"),
        threshold={"strong_buy_only": False},
        passed=promotion_enabled if isinstance(promotion_enabled, bool) else None,
        evaluated=buy_family,
    )

    base_label = str(base_action or "").strip()
    safety_clear = all(
        value is True
        for value in (
            (not bearish_trend) if bearish_trend is not None else None,
            (not stretched_chase) if stretched_chase is not None else None,
            kalman_safety_ok,
            volatility_ok,
        )
    )
    direct_evaluated = buy_family and safety_clear
    momentum_evaluated = buy_family
    promotion_evaluated = (
        buy_family
        and base_label == "Watch for Buy"
        and inputs.get("momentum_setup_eligible") is True
        and inputs.get("strong_buy_only") is False
    )
    gates["momentum_promotion_base_action"] = _gate(
        observed_value=base_label or None,
        threshold="Watch for Buy",
        passed=(base_label == "Watch for Buy") if base_label else None,
        evaluated=buy_family,
    )
    gates["momentum_promotion_confidence"] = _gate(
        observed_value=inputs.get("confidence"),
        threshold=limits.get("min_confidence"),
        passed=(
            inputs.get("confidence_ok")
            if isinstance(inputs.get("confidence_ok"), bool) else None
        ),
        evaluated=promotion_evaluated,
    )
    gate_specs = (
        ("minimum_confidence", "confidence_ok", "confidence", "min_confidence", direct_evaluated),
        ("near_trigger", "near_trigger", "entry_gap_pct", "buy_ready_max_gap_pct", direct_evaluated),
        ("actionable_traded_value", "liquidity_ok", "avg_trade_value_20", "min_trade_value", direct_evaluated),
        ("actionable_relative_volume", "volume_ok", "volume_ratio", "min_volume_ratio", direct_evaluated),
        ("actionable_kalman_quality", "actionable_kalman_ok", "kalman_quality", "actionable_min_kalman_quality", direct_evaluated),
        ("strong_source_action", "strong_source_action", "adjusted_action", "strong_source_action", direct_evaluated),
        ("strong_confidence", "strong_confidence_ok", "confidence", "strong_buy_min_confidence", direct_evaluated),
        ("strong_trigger", "strong_trigger", "entry_gap_pct", "strong_buy_max_gap_pct", direct_evaluated),
        ("strong_traded_value", "strong_liquidity_ok", "avg_trade_value_20", "strong_buy_min_trade_value", direct_evaluated),
        ("strong_relative_volume", "strong_volume_ok", "volume_ratio", "strong_buy_min_volume_ratio", direct_evaluated),
        ("strong_kalman_quality", "strong_kalman_ok", "kalman_quality", "strong_min_kalman_quality", direct_evaluated),
        ("momentum_setup", "momentum_setup_eligible", "momentum_setup", "momentum_setup_requirements", momentum_evaluated),
        ("momentum_score", "momentum_score_ok", "momentum_score", "momentum_buy_min_score", momentum_evaluated),
        ("momentum_positive_change", "momentum_positive_change_ok", "change_pct", "momentum_positive_change_pct", momentum_evaluated),
        ("momentum_relative_volume", "momentum_volume_ok", "volume_ratio", "momentum_min_volume_ratio", momentum_evaluated),
        ("momentum_entry_gap", "momentum_gap_ok", "entry_gap_pct", "momentum_max_entry_gap_pct", momentum_evaluated),
        ("momentum_ma_contradiction_clear", "momentum_ma_clear", "momentum_ma_contradiction", "momentum_ma_contradiction", momentum_evaluated),
        ("momentum_stretched_continuation_clear", "momentum_stretched_continuation_clear", "change_pct", "stretched_momentum_buy_min_change_pct", promotion_evaluated),
        ("early_momentum_score", "early_momentum_score_ok", "momentum_score", "early_momentum_buy_min_score", promotion_evaluated),
        ("early_momentum_volume", "early_momentum_volume_ok", "volume_ratio", "early_momentum_buy_min_volume_ratio", promotion_evaluated),
        ("early_momentum_entry_gap", "early_momentum_gap_ok", "entry_gap_pct", "early_momentum_buy_max_gap_pct", promotion_evaluated),
        ("early_momentum_change_ceiling", "early_momentum_change_ok", "change_pct", "early_momentum_buy_max_change_pct", promotion_evaluated),
        ("early_momentum_liquidity", "liquidity_ok", "avg_trade_value_20", "min_trade_value", promotion_evaluated),
        ("early_momentum_kalman_quality", "early_momentum_kalman_ok", "kalman_quality", "early_momentum_min_kalman_quality", promotion_evaluated),
        ("early_momentum_kalman_trend", "early_momentum_trend_ok", "kalman_trend", "early_momentum_required_trend", promotion_evaluated),
        ("early_momentum_rsi_ceiling", "early_momentum_rsi_ok", "rsi", "early_momentum_buy_max_rsi", promotion_evaluated),
        ("regular_momentum_volume", "regular_momentum_volume_ok", "volume_ratio", "buy_now_min_volume_ratio", promotion_evaluated),
        ("regular_momentum_kalman_quality", "regular_momentum_kalman_ok", "kalman_quality", "regular_momentum_min_kalman_quality", promotion_evaluated),
        ("regular_momentum_kalman_trend", "regular_momentum_trend_ok", "kalman_trend", "regular_momentum_forbidden_trend", promotion_evaluated),
    )
    for name, pass_name, observed_name, threshold_name, evaluated in gate_specs:
        passed_value = inputs.get(pass_name)
        passed = passed_value if isinstance(passed_value, bool) else None
        gates[name] = _gate(
            observed_value=inputs.get(observed_name),
            threshold=limits.get(threshold_name),
            passed=passed,
            evaluated=evaluated,
        )

    final_label = str(final_action or "").strip()
    if decision_override:
        decision = str(decision_override).strip().upper()
    elif final_label in {"Strong Buy Now", "Buy Now"}:
        decision = "QUALIFIED"
    else:
        decision = "REJECTED"
    if decision not in DECISIONS:
        decision = "NOT_EVALUABLE"

    failed = [name for name, gate in gates.items() if gate["status"] == "fail"]
    primary = str(primary_rejection_reason or "").strip() or None
    if decision == "QUALIFIED":
        primary = None
        rejection_reasons: list[str] = []
    else:
        rejection_reasons = list(dict.fromkeys(([primary] if primary else []) + failed))
        primary = rejection_reasons[0] if rejection_reasons else "unknown"

    candidate_key = f"{trading_date}|{normalized_symbol}|{normalized_source}|{SELECTOR_BUILD}"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "trading_date": trading_date,
        "timestamp_ist": timestamp.isoformat(timespec="microseconds"),
        "symbol": normalized_symbol,
        "candidate_id": hashlib.sha256(candidate_key.encode("utf-8")).hexdigest(),
        "source": normalized_source,
        "selector_version": SELECTOR_BUILD,
        "evaluation_mode": str(evaluation_mode or "computed"),
        "inputs": inputs,
        "thresholds": limits,
        "gates": gates,
        "final_action": final_label or None,
        "base_action": str(base_action or "").strip() or None,
        "final_decision": decision,
        "primary_rejection_reason": primary,
        "all_rejection_reasons": rejection_reasons,
        "rank_before_selector": None,
        "rank_after_selector": None,
        "ranking_is_selector_gate": False,
    }


class BuyNowRejectionObserver:
    """Submit selector traces to a bounded, fail-open background writer."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        buffer_size: int = 8192,
        batch_size: int = 256,
        batch_linger_seconds: float = 0.01,
        flush_timeout_seconds: float = 2.0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.clock = clock or (lambda: datetime.now(tz=IST))
        self.buffer_size = max(1, int(buffer_size))
        self.batch_size = max(1, int(batch_size))
        self.batch_linger_seconds = max(0.0, float(batch_linger_seconds))
        self.flush_timeout_seconds = max(0.0, float(flush_timeout_seconds))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=self.buffer_size
        )
        self._stats_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._accepting = True
        self._stop = threading.Event()
        self._stats: dict[str, Any] = {
            "submitted": 0,
            "enqueued": 0,
            "dropped_buffer_full": 0,
            "dropped_closed": 0,
            "submission_errors": 0,
            "invalid_events": 0,
            "written": 0,
            "write_errors": 0,
            "flush_timeouts": 0,
            "last_error_type": None,
        }
        self._worker = threading.Thread(
            target=self._writer_loop,
            name="buy-now-rejection-observer",
            daemon=True,
        )
        self._worker.start()
        atexit.register(self.close)

    def _increment(self, name: str, amount: int = 1, error: Any = None) -> None:
        with self._stats_lock:
            self._stats[name] += amount
            if error is not None:
                self._stats["last_error_type"] = type(error).__name__

    def health(self) -> dict[str, Any]:
        """Return counters without exposing mutable writer state."""
        with self._stats_lock:
            result = dict(self._stats)
        result.update(
            {
                "buffer_size": self.buffer_size,
                "queue_depth": self._queue.qsize(),
                "accepting": self._accepting,
                "worker_alive": self._worker.is_alive(),
            }
        )
        return result

    def record(self, **snapshot: Any) -> bool:
        """Deep-copy and enqueue without waiting for serialization or disk I/O."""
        self._increment("submitted")
        try:
            detached = copy.deepcopy(snapshot)
            if detached.get("event_timestamp") in (None, ""):
                detached["event_timestamp"] = self.clock()
            with self._state_lock:
                accepting = self._accepting
            if not accepting:
                self._increment("dropped_closed")
                return False
            try:
                self._queue.put_nowait(detached)
            except queue.Full:
                self._increment("dropped_buffer_full")
                return False
            self._increment("enqueued")
            return True
        except Exception as exc:
            self._increment("submission_errors", error=exc)
            return False

    def _writer_loop(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                first = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            batch = [first]
            if self.batch_linger_seconds and not self._stop.is_set():
                self._stop.wait(self.batch_linger_seconds)
            while len(batch) < self.batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write_batch(batch)
            except Exception as exc:
                self._increment("write_errors", len(batch), error=exc)
            finally:
                for _item in batch:
                    self._queue.task_done()

    def _write_batch(self, snapshots: list[dict[str, Any]]) -> None:
        payloads: dict[Path, list[bytes]] = {}
        for snapshot in snapshots:
            try:
                event = build_selector_event(clock=self.clock, **snapshot)
                if not event["symbol"]:
                    self._increment("invalid_events")
                    continue
                path = (
                    self.output_dir
                    / "buy_now_rejection_observability"
                    / f"{event['trading_date']}.jsonl"
                )
                payloads.setdefault(path, []).append(
                    (
                        json.dumps(
                            event,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            except Exception as exc:
                self._increment("write_errors", error=exc)
        for path, parts in payloads.items():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._append_payload(path, b"".join(parts))
                self._increment("written", len(parts))
            except Exception as exc:
                self._increment("write_errors", len(parts), error=exc)

    @staticmethod
    def _append_payload(path: Path, payload: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("telemetry append made no progress")
                view = view[written:]
        finally:
            os.close(fd)

    def close(self, timeout: Optional[float] = None) -> bool:
        """Stop accepting events and attempt a bounded queue flush."""
        with self._state_lock:
            self._accepting = False
        self._stop.set()
        wait_seconds = (
            self.flush_timeout_seconds if timeout is None else max(0.0, timeout)
        )
        self._worker.join(wait_seconds)
        if self._worker.is_alive():
            self._increment("flush_timeouts")
            return False
        return self._queue.empty()


def load_events(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(row, dict):
                    events.append(row)
    return events


def summarize_by_date_symbol(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group repeated evaluations without merging symbols across sessions."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for event in events:
        key = (
            str(event.get("trading_date") or ""),
            str(event.get("symbol") or "").strip().upper(),
        )
        if all(key):
            groups.setdefault(key, []).append(event)

    rows: list[dict[str, Any]] = []
    for (trading_date, symbol), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda item: str(item.get("timestamp_ist") or ""))
        gate_failures: dict[str, dict[str, Any]] = {}
        for event in ordered:
            timestamp = str(event.get("timestamp_ist") or "") or None
            gates = event.get("gates")
            if not isinstance(gates, Mapping):
                continue
            for name, gate in gates.items():
                if not isinstance(gate, Mapping):
                    continue
                gate_name = str(name)
                status = gate.get("status")
                if status == "fail":
                    detail = gate_failures.setdefault(
                        gate_name,
                        {
                            "failed_evaluations": 0,
                            "first_failed_at": timestamp,
                            "last_failed_at": timestamp,
                            "first_passed_after_failure_at": None,
                            "minutes_until_pass": None,
                            "unresolved": True,
                        },
                    )
                    detail["failed_evaluations"] += 1
                    detail["last_failed_at"] = timestamp
                elif status == "pass" and gate_name in gate_failures:
                    detail = gate_failures[gate_name]
                    if detail["first_passed_after_failure_at"] is None:
                        detail["first_passed_after_failure_at"] = timestamp
                        try:
                            started = datetime.fromisoformat(detail["first_failed_at"])
                            finished = datetime.fromisoformat(timestamp)
                            detail["minutes_until_pass"] = round(
                                (finished - started).total_seconds() / 60.0,
                                3,
                            )
                        except (TypeError, ValueError):
                            detail["minutes_until_pass"] = None
                        detail["unresolved"] = False
        rows.append(
            {
                "trading_date": trading_date,
                "symbol": symbol,
                "evaluation_count": len(ordered),
                "first_evaluated_at": ordered[0].get("timestamp_ist"),
                "last_evaluated_at": ordered[-1].get("timestamp_ist"),
                "qualified_at": next(
                    (
                        event.get("timestamp_ist")
                        for event in ordered
                        if event.get("final_decision") == "QUALIFIED"
                    ),
                    None,
                ),
                "gate_failures": gate_failures,
            }
        )
    return rows

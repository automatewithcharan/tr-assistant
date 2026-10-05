"""Display-only risk arithmetic for Emerging Leader research alerts."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP


RISK_BUDGET_RUPEES = 500.0
MAX_EXPOSURE_RUPEES = 300_000.0
MAX_QTY = 500
MAX_PRICE_AGE_SECONDS = 120.0
STOP_BUFFER_ATR = 0.10
MINIMUM_STOP_ATR = 0.50
LIVE_PARTIAL_1M = "LIVE_PARTIAL_1M"


@dataclass(frozen=True)
class ShadowPlanInputs:
    candidate_detected_at: dt.datetime
    reference_entry: float | None
    price_observed_at: dt.datetime | None = None
    atr14: float | None = None
    atr_input_at: dt.datetime | None = None
    signal_low: float | None = None
    signal_low_at: dt.datetime | None = None
    stop_input_mode: str | None = None
    tick_size: float | None = None


@dataclass(frozen=True)
class ShadowPlan:
    candidate_detected_at: str
    reference_entry: float | None
    price_observed_at: str | None
    shadow_plan_status: str
    missing_reason: str | None
    stop_input_mode: str | None
    atr_input_at: str | None
    atr14: float | None
    signal_low_at: str | None
    signal_low: float | None
    initial_stop: float | None = None
    risk_per_share: float | None = None
    risk_qty: int | None = None
    exposure_qty: int | None = None
    final_qty: int | None = None
    target_1r: float | None = None
    target_2r: float | None = None
    planned_rupee_risk: float | None = None
    position_exposure: float | None = None

    def audit_values(self) -> dict:
        return asdict(self)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value)) and float(value) > 0


def _round_to_tick(value: float, tick_size: float | None) -> float:
    if not _finite_positive(tick_size):
        return float(value)
    tick = Decimal(str(tick_size))
    rounded = (Decimal(str(value)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    return float(rounded)


def unavailable_shadow_plan(inputs: ShadowPlanInputs, reason: str, status="INPUTS_UNAVAILABLE") -> ShadowPlan:
    return ShadowPlan(
        candidate_detected_at=_iso(inputs.candidate_detected_at) or "",
        reference_entry=inputs.reference_entry,
        price_observed_at=_iso(inputs.price_observed_at),
        shadow_plan_status=status,
        missing_reason=reason,
        stop_input_mode=inputs.stop_input_mode,
        atr_input_at=_iso(inputs.atr_input_at),
        atr14=inputs.atr14,
        signal_low_at=_iso(inputs.signal_low_at),
        signal_low=inputs.signal_low,
    )


def calculate_shadow_plan(inputs: ShadowPlanInputs) -> ShadowPlan:
    """Calculate a display plan only when all causal Pine-style inputs exist."""
    missing = []
    if not _finite_positive(inputs.reference_entry):
        missing.append("reference_entry")
    if inputs.price_observed_at is None:
        missing.append("price_observed_at")
    if not _finite_positive(inputs.atr14) or inputs.atr_input_at is None:
        missing.append("causal ATR14")
    if not _finite_positive(inputs.signal_low) or inputs.signal_low_at is None:
        missing.append("causal current-minute low")
    if inputs.stop_input_mode != LIVE_PARTIAL_1M:
        missing.append("LIVE_PARTIAL_1M provenance")
    if missing:
        return unavailable_shadow_plan(inputs, ", ".join(missing) + " unavailable")

    candidate = inputs.candidate_detected_at
    price_at = inputs.price_observed_at
    atr_at = inputs.atr_input_at
    low_at = inputs.signal_low_at
    if price_at > candidate or atr_at > candidate or low_at > candidate:
        return unavailable_shadow_plan(inputs, "future-dated causal input rejected")
    price_age = (candidate - price_at).total_seconds()
    if price_age > MAX_PRICE_AGE_SECONDS:
        return unavailable_shadow_plan(inputs, f"reference price stale by {price_age:.1f}s")

    entry = _round_to_tick(float(inputs.reference_entry), inputs.tick_size)
    atr = float(inputs.atr14)
    raw_stop = float(inputs.signal_low) - atr * STOP_BUFFER_ATR
    minimum_stop = entry - atr * MINIMUM_STOP_ATR
    initial_stop = _round_to_tick(min(raw_stop, minimum_stop), inputs.tick_size)
    risk_per_share = entry - initial_stop
    if not math.isfinite(risk_per_share) or risk_per_share <= 0:
        return unavailable_shadow_plan(inputs, "reference_entry must exceed initial_stop")

    risk_qty = math.floor(RISK_BUDGET_RUPEES / risk_per_share)
    exposure_qty = math.floor(MAX_EXPOSURE_RUPEES / entry)
    qty = min(risk_qty, exposure_qty, MAX_QTY)
    if qty <= 0:
        return unavailable_shadow_plan(inputs, "calculated quantity is zero")
    target_1r = _round_to_tick(entry + risk_per_share, inputs.tick_size)
    target_2r = _round_to_tick(entry + 2 * risk_per_share, inputs.tick_size)
    planned_risk = risk_per_share * qty
    exposure = entry * qty
    if planned_risk > RISK_BUDGET_RUPEES + 1e-6:
        return unavailable_shadow_plan(inputs, "rounded plan exceeds risk budget")
    if exposure > MAX_EXPOSURE_RUPEES + 1e-6:
        return unavailable_shadow_plan(inputs, "rounded plan exceeds exposure cap")
    return ShadowPlan(
        candidate_detected_at=_iso(candidate) or "",
        reference_entry=entry,
        price_observed_at=_iso(price_at),
        shadow_plan_status="VALID",
        missing_reason=None,
        stop_input_mode=inputs.stop_input_mode,
        atr_input_at=_iso(atr_at),
        atr14=atr,
        signal_low_at=_iso(low_at),
        signal_low=float(inputs.signal_low),
        initial_stop=initial_stop,
        risk_per_share=risk_per_share,
        risk_qty=risk_qty,
        exposure_qty=exposure_qty,
        final_qty=qty,
        target_1r=target_1r,
        target_2r=target_2r,
        planned_rupee_risk=planned_risk,
        position_exposure=exposure,
    )


def format_shadow_plan(plan: ShadowPlan) -> str:
    entry = "N/A" if plan.reference_entry is None else f"₹{Decimal(str(plan.reference_entry)):,f}"
    lines = ["📋 SHADOW PLAN", f"Entry ref: {entry}"]
    if plan.shadow_plan_status == "VALID":
        lines.extend(
            [
                f"Qty: {plan.final_qty}",
                f"SL: ₹{plan.initial_stop:,.2f}",
                f"1R: ₹{plan.target_1r:,.2f}",
                f"2R: ₹{plan.target_2r:,.2f}",
                f"Risk/share: ₹{plan.risk_per_share:,.2f}",
                f"Planned risk: ₹{plan.planned_rupee_risk:,.2f}",
                f"Exposure: ₹{plan.position_exposure:,.0f}",
                f"Plan inputs: {plan.stop_input_mode}",
            ]
        )
    else:
        lines.extend(
            [
                "Qty: N/A",
                "SL: N/A",
                "1R: N/A",
                "2R: N/A",
                f"Plan: {plan.shadow_plan_status}",
                f"Reason: {plan.missing_reason or 'unavailable'}",
            ]
        )
    lines.append("⚠️ RESEARCH ONLY / NOT A BUY SIGNAL")
    return "\n".join(lines)


__all__ = [
    "LIVE_PARTIAL_1M",
    "MAX_EXPOSURE_RUPEES",
    "MAX_QTY",
    "RISK_BUDGET_RUPEES",
    "ShadowPlan",
    "ShadowPlanInputs",
    "calculate_shadow_plan",
    "format_shadow_plan",
    "unavailable_shadow_plan",
]

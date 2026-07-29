"""Deterministic order-book execution helpers for paper evidence."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List


EXECUTION_SCHEMA_VERSION = 2
MAX_DEPTH_LEVELS = 5


def depth_levels(
    tick: Dict[str, Any],
    side: str,
    *,
    max_levels: int = MAX_DEPTH_LEVELS,
) -> List[Dict[str, float]]:
    """Return normalized positive price/quantity levels in execution order."""
    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError(f"unsupported order-book side: {side}")
    depth = tick.get("depth") if isinstance(tick, dict) else {}
    raw_levels = depth.get(normalized_side, []) if isinstance(depth, dict) else []
    levels: List[Dict[str, float]] = []
    for raw in list(raw_levels or [])[: max(1, int(max_levels))]:
        if not isinstance(raw, dict):
            continue
        try:
            price = float(raw.get("price", 0) or 0)
            quantity = int(float(raw.get("quantity", 0) or 0))
            orders = int(float(raw.get("orders", 0) or 0))
        except (TypeError, ValueError):
            continue
        if price <= 0 or quantity <= 0:
            continue
        levels.append(
            {
                "price": round(price, 4),
                "quantity": quantity,
                "orders": max(0, orders),
            }
        )
    return levels


def depth_json(levels: Iterable[Dict[str, Any]]) -> str:
    """Serialize normalized depth compactly and deterministically."""
    return json.dumps(list(levels), separators=(",", ":"), sort_keys=True)


def parse_depth_json(value: Any) -> List[Dict[str, float]]:
    """Parse persisted depth while failing closed on malformed evidence."""
    if isinstance(value, list):
        raw_levels = value
    else:
        text = str(value or "").strip()
        if not text:
            return []
        try:
            raw_levels = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(raw_levels, list):
        return []
    return depth_levels({"depth": {"buy": raw_levels}}, "buy")


def market_fill(
    levels: Iterable[Dict[str, Any]],
    quantity: int,
) -> Dict[str, Any]:
    """Walk visible depth and return the exact displayed-volume fill."""
    normalized = list(levels)
    requested = max(0, int(quantity or 0))
    remaining = requested
    notional = 0.0
    filled = 0
    levels_used = 0
    worst_price = 0.0
    for raw in normalized:
        if remaining <= 0:
            break
        try:
            price = float(raw.get("price", 0) or 0)
            available = int(float(raw.get("quantity", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if price <= 0 or available <= 0:
            continue
        take = min(remaining, available)
        notional += take * price
        filled += take
        remaining -= take
        levels_used += 1
        worst_price = price
    return {
        "requested_quantity": requested,
        "quantity": requested,
        "filled_quantity": filled,
        "unfilled_quantity": remaining,
        "fillable": bool(requested > 0 and remaining == 0),
        "vwap": round(notional / filled, 6) if filled > 0 else 0.0,
        "notional": round(notional, 2),
        "worst_price": round(worst_price, 4) if filled > 0 else 0.0,
        "levels_used": levels_used,
        "visible_quantity": sum(
            max(0, int(float(level.get("quantity", 0) or 0)))
            for level in normalized
            if isinstance(level, dict)
        ),
    }


def affordable_market_fill(
    levels: Iterable[Dict[str, Any]],
    capital: float,
) -> Dict[str, Any]:
    """Return the largest whole-share visible-depth fill within capital."""
    normalized = list(levels)
    budget = max(0.0, float(capital or 0.0))
    quantity = 0
    spent = 0.0
    for raw in normalized:
        try:
            price = float(raw.get("price", 0) or 0)
            available = int(float(raw.get("quantity", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if price <= 0 or available <= 0:
            continue
        affordable = int((budget - spent) // price)
        take = min(available, max(0, affordable))
        if take <= 0:
            break
        quantity += take
        spent += take * price
        if take < available:
            break
    result = market_fill(normalized, quantity)
    result["capital"] = round(budget, 2)
    return result

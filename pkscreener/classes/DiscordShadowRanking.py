"""Causal, fail-open shadow review priority for direct Discord candidates.

The score is deliberately provisional and interpretable.  It is metadata for
ordering manual TradingView review, never a qualification, suppression, or
trade decision.  Only fields present at the Discord-attempt boundary are read.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "discord-shadow-ranking-v2"
DIRECT_LANES = frozenset({"early_breakout", "top_gainer_radar"})
IST = ZoneInfo("Asia/Kolkata")

_AMOUNT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(cr|crore|l|lac|lakh|k)?", re.I)
_BEARISH_MARKERS = (
    "sell / exit", "bearish trend", "kalman downtrend",
    "price below atr trailing stop", "uptrend broken", "strong downtrend",
    "macd bearish", "death cross", "lower highs and lower lows",
)


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"none", "nan", "na", "n/a"}
    try:
        return not math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if _missing(value):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "stale"}


def optional_float(value: Any) -> float | None:
    if _missing(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def amount_rupees(value: Any) -> float | None:
    number = optional_float(value)
    if number is not None:
        return number
    match = _AMOUNT_RE.search(str(value or "").replace(",", ""))
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    multiplier = {
        "cr": 10_000_000.0, "crore": 10_000_000.0,
        "l": 100_000.0, "lac": 100_000.0, "lakh": 100_000.0,
        "k": 1_000.0,
    }.get(unit, 1.0)
    return number * multiplier


def _timestamp(value: Any) -> datetime | None:
    if _missing(value):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def _seconds(start: Any, end: Any) -> float | None:
    first, second = _timestamp(start), _timestamp(end)
    if first is None or second is None:
        return None
    return round(max(0.0, (second - first).total_seconds()), 6)


def _drift(start: Any, end: Any) -> float | None:
    first, second = optional_float(start), optional_float(end)
    if first is None or second is None or first <= 0:
        return None
    return round((second / first - 1.0) * 100.0, 6)


def _present(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if not _missing(value):
            return value
    return None


def calculate_shadow_priority(
    row: Mapping[str, Any],
    attempted_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Return a causal v2 priority record without mutating *row*.

    Unknown values remain ``None`` and do not receive invented neutral values.
    Future/outcome-looking keys are intentionally never read.
    """
    attempt_time = attempted_at or _present(row, "DiscordAttemptTime", "discord_attempt_at")
    if isinstance(attempt_time, datetime):
        if attempt_time.tzinfo is None:
            attempt_time = attempt_time.replace(tzinfo=IST)
        attempt_iso = attempt_time.isoformat(timespec="microseconds")
    else:
        parsed_attempt = _timestamp(attempt_time)
        attempt_iso = parsed_attempt.isoformat(timespec="microseconds") if parsed_attempt else None

    first_time = _present(row, "FirstEligibleTime", "first_eligible_at")
    queue_time = _present(row, "QueueSelectedTime", "queue_selected_at")
    analysis_time = _present(row, "AnalysisCompletedTime", "analysis_completed_at")
    qualified_time = _present(row, "QualifiedTime", "qualified_at", "detected_at")
    first_price = _present(row, "FirstEligiblePrice", "first_eligible_price")
    queue_price = _present(row, "QueueSelectedPrice", "queue_selected_price")
    qualified_price = _present(row, "QualifiedPrice", "qualified_price", "PriceNow")
    attempt_price = _present(row, "DiscordAttemptPrice", "discord_attempt_price")

    latencies = {
        "first_eligible_to_queue_selected_seconds": _seconds(first_time, queue_time),
        "queue_selected_to_analysis_completed_seconds": _seconds(queue_time, analysis_time),
        "analysis_completed_to_qualified_seconds": _seconds(analysis_time, qualified_time),
        "first_eligible_to_qualified_seconds": _seconds(first_time, qualified_time),
        "qualified_to_discord_attempt_seconds": _seconds(qualified_time, attempt_iso),
    }
    price_drifts = {
        "first_eligible_to_queue_selected_pct": _drift(first_price, queue_price),
        "queue_selected_to_qualified_pct": _drift(queue_price, qualified_price),
        "first_eligible_to_qualified_pct": _drift(first_price, qualified_price),
        "qualified_to_discord_attempt_pct": _drift(qualified_price, attempt_price),
        "first_eligible_to_discord_attempt_pct": _drift(first_price, attempt_price),
    }

    latency_candidates = {
        key: value for key, value in latencies.items()
        if value is not None and key not in {"first_eligible_to_qualified_seconds"}
    }
    drift_candidates = {
        key: value for key, value in price_drifts.items()
        if value is not None and key not in {
            "first_eligible_to_qualified_pct", "first_eligible_to_discord_attempt_pct"
        }
    }
    largest_latency_stage = max(latency_candidates, key=latency_candidates.get) if latency_candidates else None
    largest_drift_stage = max(drift_candidates, key=lambda key: abs(drift_candidates[key])) if drift_candidates else None

    warning_text = " | ".join(
        str(value or "") for value in (
            row.get("WarningFlags"), row.get("Checks"), row.get("Why")
        )
    ).lower()
    low_liquidity = "low liquidity" in warning_text or "liquidity warning" in warning_text
    low_volume = "low volume" in warning_text or "volume warning" in warning_text
    bearish = any(marker in warning_text for marker in _BEARISH_MARKERS)
    stale_flag = _truthy(row.get("StaleData")) or _truthy(row.get("StaleTick")) or "stale" in warning_text

    source_tick = _present(row, "SourceExchangeTimestamp", "source_exchange_timestamp")
    received_tick = _present(row, "TickTimestamp", "tick_timestamp")
    tick_age = _seconds(source_tick or received_tick, attempt_iso)
    stale_tick = stale_flag or (tick_age is not None and tick_age > 15.0)

    features = {
        "traded_value": amount_rupees(_present(row, "TradedValue", "traded_value")),
        "average_daily_traded_value": amount_rupees(_present(row, "AvgDailyValue", "average_daily_traded_value")),
        "spread_bps": optional_float(_present(row, "SpreadBps", "spread_bps")),
        "rvol": optional_float(_present(row, "VolumeRatio", "volume_ratio")),
        "buyer_seller_ratio": optional_float(_present(row, "BuyerSellerRatio", "buy_sell_ratio")),
        # Only an explicit stock-minus-NIFTY field earns the RS component.
        "nifty_relative_strength_pct": optional_float(_present(row, "NiftyRelativeStrength", "nifty_relative_strength_pct")),
        "benchmark_strength_pct": optional_float(_present(row, "BenchmarkStrength", "benchmark_strength")),
        "sector_strength_pct": optional_float(_present(row, "SectorStrength", "sector_strength")),
        "hod_pullback_pct": optional_float(_present(row, "PullbackPct", "pullback_pct")),
        "vwap_distance_pct": optional_float(_present(row, "VWAPDistance", "vwap_distance_pct")),
        "vwap_distance_atr": optional_float(_present(row, "VWAPDistanceATR", "vwap_distance_atr")),
        "day_change_pct": optional_float(_present(row, "ChangePct", "day_change_pct")),
        "distance_to_52_week_high_pct": optional_float(_present(row, "Distance52WeekHighPct", "distance_to_52_week_high_pct")),
        "signal_strength": optional_float(_present(row, "SignalStrengthPct", "signal_strength")),
        "tick_age_seconds": tick_age,
        "multi_lane": _truthy(_present(row, "MultiLane", "multi_lane")),
        "early_breakout": str(_present(row, "Lane", "source_lane") or "") == "early_breakout",
        "top_gainer_radar": str(_present(row, "Lane", "source_lane") or "") == "top_gainer_radar",
    }

    missing_fields = sorted(key for key, value in features.items() if value is None)
    warnings = []
    for active, label in (
        (low_liquidity, "low_liquidity"), (low_volume, "low_volume"),
        (bearish, "bearish_upstream_text"), (stale_tick, "stale_tick"),
    ):
        if active:
            warnings.append(label)
    if missing_fields:
        warnings.append("missing_data")

    components: dict[str, int] = {}
    traded = features["traded_value"]
    average_value = features["average_daily_traded_value"]
    liquidity_reference = max(value for value in (traded, average_value) if value is not None) if any(value is not None for value in (traded, average_value)) else None
    if low_liquidity:
        components["liquidity"] = -2
    elif liquidity_reference is not None:
        components["liquidity"] = 2 if liquidity_reference >= 50_000_000 else 1 if liquidity_reference >= 10_000_000 else -1

    rvol = features["rvol"]
    if low_volume:
        components["rvol"] = -2
    elif rvol is not None:
        components["rvol"] = 2 if rvol >= 2.5 else 1 if rvol >= 1.5 else -1 if rvol < 1.0 else 0
    bs_ratio = features["buyer_seller_ratio"]
    if bs_ratio is not None:
        components["buyer_seller"] = 1 if bs_ratio >= 2.0 else -1 if bs_ratio < 1.0 else 0

    nifty_rs = features["nifty_relative_strength_pct"]
    if nifty_rs is not None:
        components["nifty_relative_strength"] = 1 if nifty_rs >= 0.5 else -1 if nifty_rs <= -0.5 else 0
    benchmark = features["benchmark_strength_pct"]
    if benchmark is not None:
        components["benchmark_strength"] = 1 if benchmark > 0 else -1 if benchmark < 0 else 0
    sector = features["sector_strength_pct"]
    if sector is not None:
        components["sector_strength"] = 1 if sector > 0 else -1 if sector < 0 else 0

    pullback = features["hod_pullback_pct"]
    if pullback is not None:
        components["hod_proximity"] = 1 if 0 <= pullback <= 0.75 else -1 if pullback > 1.5 else 0
    vwap_distance = features["vwap_distance_pct"]
    if vwap_distance is not None:
        components["vwap_extension"] = 1 if 0 <= vwap_distance <= 1.0 else -2 if vwap_distance > 2.0 else -1 if vwap_distance < 0 else 0
    signal_strength = features["signal_strength"]
    if signal_strength is not None:
        components["signal_strength"] = 1 if signal_strength >= 55 else -1 if signal_strength < 40 else 0

    first_to_qualified = latencies["first_eligible_to_qualified_seconds"]
    if first_to_qualified is not None:
        components["qualification_timeliness"] = 1 if first_to_qualified <= 15 else -1 if first_to_qualified > 60 else 0
    if tick_age is not None:
        components["tick_freshness"] = 1 if tick_age <= 3 else -2 if tick_age > 15 else 0
    consumed = price_drifts["first_eligible_to_discord_attempt_pct"]
    if consumed is not None:
        components["price_consumed"] = -2 if consumed > 1.0 else -1 if consumed > 0.5 else 0
    if features["multi_lane"]:
        components["multi_lane_agreement"] = 2
    if bearish:
        components["bearish_warning"] = -2
    if stale_tick and "tick_freshness" not in components:
        components["stale_warning"] = -2
    if len(missing_fields) >= 7:
        components["missing_data"] = -1

    score = int(sum(components.values()))
    priority = "A" if score >= 6 else "B" if score >= 2 else "C"
    qualified_dt = _timestamp(qualified_time)
    trading_date = str(row.get("trading_date") or "").strip() or (
        qualified_dt.astimezone(IST).date().isoformat() if qualified_dt else (attempt_iso or "")[:10]
    )
    candidate_id = str(_present(row, "LifecycleObservationId", "lifecycle_observation_id", "candidate_id") or "").strip()

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "direct_candidate_shadow_priority",
        "trading_date": trading_date,
        "symbol": str(_present(row, "Symbol", "symbol") or "").strip().upper(),
        "candidate_id": candidate_id,
        "lifecycle_observation_id": candidate_id,
        "lane": str(_present(row, "Lane", "source_lane") or "").strip(),
        "priority": priority,
        "priority_score": score,
        "priority_components": components,
        "features": features,
        "warnings": warnings,
        "missing_fields": missing_fields,
        "first_eligible_at": first_time,
        "first_eligible_price": optional_float(first_price),
        "queue_selected_at": queue_time,
        "queue_selected_price": optional_float(queue_price),
        "analysis_completed_at": analysis_time,
        "qualified_at": qualified_time,
        "qualified_price": optional_float(qualified_price),
        "discord_attempt_at": attempt_iso,
        "discord_attempt_price": optional_float(attempt_price),
        "source_exchange_timestamp": source_tick,
        "tick_timestamp": received_tick,
        "stage_latencies_seconds": latencies,
        "stage_price_drifts_pct": price_drifts,
        "largest_latency_stage": largest_latency_stage,
        "largest_latency_seconds": latency_candidates.get(largest_latency_stage) if largest_latency_stage else None,
        "largest_price_drift_stage": largest_drift_stage,
        "largest_price_drift_pct": drift_candidates.get(largest_drift_stage) if largest_drift_stage else None,
        "hypothesis_only": True,
    }


def append_shadow_priority_record(output_dir: str | Path, record: Mapping[str, Any]) -> bool:
    """Append one research row.  Failure is fail-open and never escapes."""
    try:
        trading_date = str(record.get("trading_date") or "").strip()
        if not trading_date:
            return False
        path = Path(output_dir) / "research" / "discord_shadow_ranking" / f"{trading_date}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(dict(record), sort_keys=True, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short shadow-ranking write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def load_shadow_priority_records(root: str | Path) -> list[dict[str, Any]]:
    path = Path(root)
    files = [path] if path.is_file() else sorted(path.glob("*.jsonl"))
    rows: list[dict[str, Any]] = []
    for file in files:
        for line in file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION:
                rows.append(value)
    return rows


__all__ = [
    "SCHEMA_VERSION", "append_shadow_priority_record", "amount_rupees",
    "calculate_shadow_priority", "load_shadow_priority_records", "optional_float",
]

"""Discord-only quality policy for already-detected Emerging Leaders."""

from __future__ import annotations

import datetime as dt
import math
import os
from dataclasses import asdict, dataclass, replace


DEFAULT_NOT_BEFORE_IST = "09:30"
DEFAULT_MIN_RANK_IMPROVEMENT_5M = 100.0
DEFAULT_MIN_RANK_VELOCITY_5M = 20.0
DEFAULT_MIN_RANK_ACCELERATION = 0.0
DEFAULT_MIN_PRICE_VS_VWAP_PCT = 0.75
DEFAULT_MIN_DISTANCE_FROM_DAY_HIGH_PCT = -0.50
DEFAULT_MIN_RECENT_CAPACITY_5PCT = 50
DEFAULT_MIN_RECENT_TRADED_VALUE_RATE = 500_000.0
DEFAULT_REQUIRE_COMPLETE_FEATURES = True


def _environment_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _environment_nonnegative_int(name: str, default: int) -> int:
    value = _environment_float(name, float(default))
    if value < 0 or not value.is_integer():
        raise ValueError(f"{name} must be a nonnegative integer")
    return int(value)


def _environment_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


def _parse_time(value: str) -> dt.time:
    raw = str(value or "").strip()
    try:
        parsed = dt.time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("NG_EMERGING_DISCORD_NOT_BEFORE_IST must be HH:MM") from exc
    if parsed.tzinfo is not None:
        raise ValueError("NG_EMERGING_DISCORD_NOT_BEFORE_IST must not include a timezone")
    return parsed


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class DiscordQualityThresholds:
    not_before_ist: str = DEFAULT_NOT_BEFORE_IST
    min_rank_improvement_5m: float = DEFAULT_MIN_RANK_IMPROVEMENT_5M
    min_rank_velocity_5m: float = DEFAULT_MIN_RANK_VELOCITY_5M
    min_rank_acceleration: float = DEFAULT_MIN_RANK_ACCELERATION
    min_price_vs_vwap_pct: float = DEFAULT_MIN_PRICE_VS_VWAP_PCT
    min_distance_from_day_high_pct: float = DEFAULT_MIN_DISTANCE_FROM_DAY_HIGH_PCT
    min_recent_capacity_5pct: int = DEFAULT_MIN_RECENT_CAPACITY_5PCT
    min_recent_traded_value_rate: float = DEFAULT_MIN_RECENT_TRADED_VALUE_RATE
    require_complete_features: bool = DEFAULT_REQUIRE_COMPLETE_FEATURES

    @classmethod
    def from_environment(cls) -> "DiscordQualityThresholds":
        not_before = str(
            os.environ.get("NG_EMERGING_DISCORD_NOT_BEFORE_IST", DEFAULT_NOT_BEFORE_IST)
        ).strip()
        _parse_time(not_before)
        return cls(
            not_before_ist=not_before,
            min_rank_improvement_5m=_environment_float(
                "NG_EMERGING_DISCORD_MIN_RANK_IMPROVEMENT_5M",
                DEFAULT_MIN_RANK_IMPROVEMENT_5M,
            ),
            min_rank_velocity_5m=_environment_float(
                "NG_EMERGING_DISCORD_MIN_RANK_VELOCITY_5M",
                DEFAULT_MIN_RANK_VELOCITY_5M,
            ),
            min_rank_acceleration=_environment_float(
                "NG_EMERGING_DISCORD_MIN_RANK_ACCELERATION",
                DEFAULT_MIN_RANK_ACCELERATION,
            ),
            min_price_vs_vwap_pct=_environment_float(
                "NG_EMERGING_DISCORD_MIN_PRICE_VS_VWAP_PCT",
                DEFAULT_MIN_PRICE_VS_VWAP_PCT,
            ),
            min_distance_from_day_high_pct=_environment_float(
                "NG_EMERGING_DISCORD_MIN_DISTANCE_FROM_DAY_HIGH_PCT",
                DEFAULT_MIN_DISTANCE_FROM_DAY_HIGH_PCT,
            ),
            min_recent_capacity_5pct=_environment_nonnegative_int(
                "NG_EMERGING_DISCORD_MIN_RECENT_CAPACITY_5PCT",
                DEFAULT_MIN_RECENT_CAPACITY_5PCT,
            ),
            min_recent_traded_value_rate=_environment_float(
                "NG_EMERGING_DISCORD_MIN_RECENT_TRADED_VALUE_RATE",
                DEFAULT_MIN_RECENT_TRADED_VALUE_RATE,
            ),
            require_complete_features=_environment_bool(
                "NG_EMERGING_DISCORD_REQUIRE_COMPLETE_QUALITY_FEATURES",
                DEFAULT_REQUIRE_COMPLETE_FEATURES,
            ),
        )

    @classmethod
    def permissive(cls) -> "DiscordQualityThresholds":
        return cls(
            not_before_ist="00:00",
            min_rank_improvement_5m=-1e12,
            min_rank_velocity_5m=-1e12,
            min_rank_acceleration=-1e12,
            min_price_vs_vwap_pct=-1e12,
            min_distance_from_day_high_pct=-1e12,
            min_recent_capacity_5pct=0,
            min_recent_traded_value_rate=-1e12,
            require_complete_features=False,
        )


@dataclass(frozen=True)
class DiscordQuality:
    discord_quality_status: str
    reasons: tuple[str, ...]
    candidate_time_ist: str
    rank_improvement_5m: float | None
    rank_velocity_5m: float | None
    rank_acceleration: float | None
    price_vs_vwap_pct: float | None
    distance_from_day_high_pct: float | None
    recent_capacity_5pct: int
    recent_traded_value_rate: float | None
    not_before_ist: str
    min_rank_improvement_5m: float
    min_rank_velocity_5m: float
    min_rank_acceleration: float
    min_price_vs_vwap_pct: float
    min_distance_from_day_high_pct: float
    min_recent_capacity_5pct: int
    min_recent_traded_value_rate: float
    require_complete_features: bool

    def audit_values(self) -> dict:
        values = asdict(self)
        values["reasons"] = ";".join(self.reasons)
        return values


def evaluate_discord_quality(
    candidate_detected_at: dt.datetime,
    rank_improvement_5m,
    rank_velocity_5m,
    rank_acceleration,
    price_vs_vwap_pct,
    distance_from_day_high_pct,
    last_price,
    volume_rate_5m,
    recent_capacity_5pct: int,
    thresholds: DiscordQualityThresholds,
) -> DiscordQuality:
    improvement = _finite(rank_improvement_5m)
    velocity = _finite(rank_velocity_5m)
    acceleration = _finite(rank_acceleration)
    vwap = _finite(price_vs_vwap_pct)
    distance_high = _finite(distance_from_day_high_pct)
    price = _finite(last_price)
    volume_rate = _finite(volume_rate_5m)
    recent_value_rate = (
        price * volume_rate if price is not None and volume_rate is not None else None
    )
    reasons: list[str] = []
    local_time = candidate_detected_at.timetz().replace(tzinfo=None)
    if local_time < _parse_time(thresholds.not_before_ist):
        reasons.append("BEFORE_ALERT_WINDOW")
    for value, minimum, missing_reason, low_reason in (
        (improvement, thresholds.min_rank_improvement_5m,
         "MISSING_RANK_IMPROVEMENT_5M", "LOW_RANK_IMPROVEMENT_5M"),
        (velocity, thresholds.min_rank_velocity_5m,
         "MISSING_RANK_VELOCITY_5M", "LOW_RANK_VELOCITY_5M"),
        (acceleration, thresholds.min_rank_acceleration,
         "MISSING_RANK_ACCELERATION", "LOW_RANK_ACCELERATION"),
        (vwap, thresholds.min_price_vs_vwap_pct,
         "MISSING_PRICE_VS_VWAP", "BELOW_VWAP_BUFFER"),
        (distance_high, thresholds.min_distance_from_day_high_pct,
         "MISSING_DISTANCE_FROM_DAY_HIGH", "TOO_FAR_FROM_DAY_HIGH"),
    ):
        if value is None:
            if thresholds.require_complete_features:
                reasons.append(missing_reason)
        elif value < minimum:
            reasons.append(low_reason)
    if int(recent_capacity_5pct) < thresholds.min_recent_capacity_5pct:
        reasons.append("LOW_RECENT_SHARE_CAPACITY")
    if recent_value_rate is None:
        if thresholds.require_complete_features:
            reasons.append("MISSING_RECENT_TRADED_VALUE_RATE")
    elif recent_value_rate < thresholds.min_recent_traded_value_rate:
        reasons.append("LOW_RECENT_TRADED_VALUE_RATE")
    return DiscordQuality(
        discord_quality_status="PASS" if not reasons else "SUPPRESSED_LOW_QUALITY",
        reasons=tuple(reasons),
        candidate_time_ist=candidate_detected_at.isoformat(),
        rank_improvement_5m=improvement,
        rank_velocity_5m=velocity,
        rank_acceleration=acceleration,
        price_vs_vwap_pct=vwap,
        distance_from_day_high_pct=distance_high,
        recent_capacity_5pct=int(recent_capacity_5pct),
        recent_traded_value_rate=recent_value_rate,
        not_before_ist=thresholds.not_before_ist,
        min_rank_improvement_5m=thresholds.min_rank_improvement_5m,
        min_rank_velocity_5m=thresholds.min_rank_velocity_5m,
        min_rank_acceleration=thresholds.min_rank_acceleration,
        min_price_vs_vwap_pct=thresholds.min_price_vs_vwap_pct,
        min_distance_from_day_high_pct=thresholds.min_distance_from_day_high_pct,
        min_recent_capacity_5pct=thresholds.min_recent_capacity_5pct,
        min_recent_traded_value_rate=thresholds.min_recent_traded_value_rate,
        require_complete_features=thresholds.require_complete_features,
    )


def superseded_same_event_quality(
    value: DiscordQuality,
) -> DiscordQuality:
    return replace(
        value,
        discord_quality_status="SUPPRESSED_SUPERSEDED_STAGE",
        reasons=("SUPERSEDED_SAME_EVENT",),
    )


__all__ = [
    "DiscordQuality",
    "DiscordQualityThresholds",
    "evaluate_discord_quality",
    "superseded_same_event_quality",
]

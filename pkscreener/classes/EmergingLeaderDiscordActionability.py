"""Discord-only liquidity policy for already-detected Emerging Leaders."""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass


DEFAULT_MIN_CUM_VOLUME = 1_000.0
DEFAULT_MIN_TURNOVER = 1_000_000.0


@dataclass(frozen=True)
class DiscordActionabilityThresholds:
    min_cumulative_volume: float = DEFAULT_MIN_CUM_VOLUME
    min_turnover: float = DEFAULT_MIN_TURNOVER

    @classmethod
    def from_environment(cls) -> "DiscordActionabilityThresholds":
        return cls(
            min_cumulative_volume=_nonnegative_environment_float(
                "NG_EMERGING_DISCORD_MIN_CUM_VOLUME", DEFAULT_MIN_CUM_VOLUME
            ),
            min_turnover=_nonnegative_environment_float(
                "NG_EMERGING_DISCORD_MIN_TURNOVER", DEFAULT_MIN_TURNOVER
            ),
        )


@dataclass(frozen=True)
class DiscordActionability:
    discord_actionability_status: str
    reasons: tuple[str, ...]
    cumulative_volume: float | None
    approx_turnover: float | None
    volume_rate_5m: float | None
    min_cumulative_volume: float
    min_turnover: float
    recent_5m_participation_capacity_5pct: int

    def audit_values(self) -> dict:
        values = asdict(self)
        values["reasons"] = ";".join(self.reasons)
        return values


def _nonnegative_environment_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate_discord_actionability(
    cumulative_volume,
    approx_turnover,
    volume_rate_5m,
    thresholds: DiscordActionabilityThresholds,
) -> DiscordActionability:
    volume = _finite(cumulative_volume)
    turnover = _finite(approx_turnover)
    recent_rate = _finite(volume_rate_5m)
    reasons = []
    if volume is None or volume < thresholds.min_cumulative_volume:
        reasons.append("LOW_CUM_VOLUME")
    if turnover is None or turnover < thresholds.min_turnover:
        reasons.append("LOW_TURNOVER")
    if recent_rate is None:
        reasons.append("NO_RECENT_VOLUME_RATE")
    elif recent_rate <= 0:
        reasons.append("NONPOSITIVE_RECENT_VOLUME_RATE")
    capacity = math.floor(max(recent_rate or 0.0, 0.0) * 0.05)
    return DiscordActionability(
        discord_actionability_status="PASS" if not reasons else "SUPPRESSED_LOW_LIQUIDITY",
        reasons=tuple(reasons),
        cumulative_volume=volume,
        approx_turnover=turnover,
        volume_rate_5m=recent_rate,
        min_cumulative_volume=thresholds.min_cumulative_volume,
        min_turnover=thresholds.min_turnover,
        recent_5m_participation_capacity_5pct=capacity,
    )


def format_discord_actionability(value: DiscordActionability) -> str:
    if value.discord_actionability_status != "PASS":
        raise ValueError("suppressed actionability must never be formatted for Discord")
    return "\n".join(
        [
            "💧 ACTIONABILITY",
            f"Volume: {value.cumulative_volume:,.0f}",
            f"Turnover: ₹{value.approx_turnover / 10_000_000:.2f} Cr",
            f"Recent volume rate: {value.volume_rate_5m:,.0f}/min",
            f"5% recent-volume capacity proxy: {value.recent_5m_participation_capacity_5pct:,} shares/min",
            "Liquidity gate: PASS",
        ]
    )


__all__ = [
    "DEFAULT_MIN_CUM_VOLUME",
    "DEFAULT_MIN_TURNOVER",
    "DiscordActionability",
    "DiscordActionabilityThresholds",
    "evaluate_discord_actionability",
    "format_discord_actionability",
]

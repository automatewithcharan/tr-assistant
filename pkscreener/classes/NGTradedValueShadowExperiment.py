"""Shadow-only NG traded-value experiment.

Signal-time observations are append-only and contain an explicit allow-list of
causal recorder fields.  Outcome labels are produced by a separate, post-session
finalizer and are never written into the observation journal.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from pkscreener.classes.NGP5EarlyWatchPublisher import (
    P5Evaluation,
    P5_MIN_TURNOVER,
    POLICY_VERSION,
    _finite_float,
    _parse_ist_timestamp,
    evaluate_p5,
)


IST = ZoneInfo("Asia/Kolkata")
EXPERIMENT_ID = "ng_experiment_4_traded_value_shadow"
OBSERVATION_SCHEMA_VERSION = 1
OUTCOME_SCHEMA_VERSION = 1
QUALIFICATION_LANE = "ng_p5_early_watch"
MARKET_OPEN_MINUTE = 9 * 60 + 15
POSITIVE_THRESHOLDS = (0.5, 1.0, 2.0, 3.0, 5.0)
NEGATIVE_THRESHOLDS = (-0.5, -1.0, -2.0)
HORIZONS = (5, 15, 30, 60)

# Deliberately explicit.  New recorder columns do not enter Experiment 4 until
# reviewed as signal-time-safe; fields with outcome/future semantics cannot be
# copied merely because they appear in the source CSV.
CAUSAL_SOURCE_FIELDS = (
    "Bucket",
    "RecorderGeneration",
    "Exchange",
    "InstrumentToken",
    "PrevClose",
    "UniverseSize",
    "DayChangeRankPct",
    "RankVelocityPerMinute",
    "EnteredTop100",
    "EnteredTop50",
    "RapidRankClimber",
    "Volume",
    "BuyQty",
    "SellQty",
    "BuyerSellerRatio",
    "BuyerSellerMedianWindow",
    "BuyerSellerMinWindow",
    "OrderBookStable",
    "BestBid",
    "BestAsk",
    "BestBidQty",
    "BestAskQty",
    "SpreadBps",
    "SpreadOk",
    "SectorCount",
    "SectorBreadthPct",
    "SectorTop2Count",
    "SectorTop5Count",
    "MarketBreadthPct",
    "IsTopGainerNow",
    "AlreadyAlerted",
)


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _first_present(row: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in row and str(row.get(name) or "").strip() != "":
            return row.get(name)
    return None


def _json_scalar(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    text = str(value).strip()
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        number = float(text)
    except ValueError:
        return text
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def is_sole_traded_value_failure(
    row: Mapping[str, Any],
    opening_leaders: Iterable[str],
    evaluation: Optional[P5Evaluation] = None,
) -> bool:
    """Use the frozen evaluator itself to prove turnover is the sole failure."""
    actual = evaluation or evaluate_p5(row, opening_leaders)
    if actual.qualifies or actual.reason != "turnover below ₹3 crore":
        return False
    counterfactual = dict(row)
    counterfactual["TradedValue"] = P5_MIN_TURNOVER
    return evaluate_p5(counterfactual, opening_leaders).qualifies


def build_signal_features(row: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = _parse_ist_timestamp(row.get("When"))
    price = _finite_float(row.get("LastPrice"))
    session_open = _finite_float(row.get("Open"))
    session_high = _finite_float(row.get("DayHigh"))
    traded_value = _finite_float(row.get("TradedValue"))
    vwap = _finite_float(_first_present(row, ("VWAP", "Vwap")))
    vwap_distance = _finite_float(
        _first_present(row, ("VWAPDistancePct", "VWAPDistance", "VwapDistancePct"))
    )
    if vwap_distance is None and price is not None and vwap not in (None, 0):
        vwap_distance = (price / vwap - 1.0) * 100.0
    if price is None or vwap is None:
        vwap_relationship = None
    elif price > vwap:
        vwap_relationship = "above"
    elif price < vwap:
        vwap_relationship = "below"
    else:
        vwap_relationship = "at"
    distance_from_high = _finite_float(row.get("PullbackFromHighPct"))
    if distance_from_high is None and price is not None and session_high not in (None, 0):
        distance_from_high = (session_high - price) / session_high * 100.0
    elapsed = None
    if timestamp is not None:
        elapsed = (
            timestamp.hour * 60
            + timestamp.minute
            + timestamp.second / 60.0
            + timestamp.microsecond / 60_000_000.0
            - MARKET_OPEN_MINUTE
        )

    features = {
        "price": price,
        "session_open": session_open,
        "day_move_pct": _finite_float(row.get("DayChangePct")),
        "open_move_pct": _finite_float(row.get("OpenChangePct")),
        "traded_value": traded_value,
        "production_traded_value_requirement": P5_MIN_TURNOVER,
        "traded_value_requirement_ratio": (
            traded_value / P5_MIN_TURNOVER if traded_value is not None else None
        ),
        "relative_volume": _finite_float(
            _first_present(row, ("RelativeVolume", "VolumeVsAvg", "VolumeRatio"))
        ),
        "recent_volume_acceleration": _finite_float(
            _first_present(row, ("VolumeAcceleration", "VolumeDeltaPct"))
        ),
        "recent_traded_value_acceleration": _finite_float(
            _first_present(
                row,
                (
                    "TradedValueAcceleration",
                    "TurnoverAcceleration",
                    "TradedValueDeltaPct",
                ),
            )
        ),
        "rank": _json_scalar(row.get("DayChangeRank")),
        "previous_rank": _json_scalar(row.get("PreviousDayChangeRank")),
        "rank_delta": _json_scalar(row.get("RankImprovement")),
        "rank_velocity_per_minute": _finite_float(row.get("RankVelocityPerMinute")),
        "move_delta_pct": _finite_float(row.get("DayChangeDeltaPct")),
        "price_acceleration_pct": _finite_float(
            _first_present(row, ("PriceAccelerationPct", "DayChangeAccelerationPct"))
        ),
        "relative_strength_vs_nifty_pct": _finite_float(
            _first_present(
                row,
                ("RelativeStrengthVsNiftyPct", "NiftyRelativeStrengthPct"),
            )
        ),
        "sector": str(row.get("Sector") or "").strip() or None,
        "sector_relative_strength_pct": _finite_float(
            _first_present(
                row,
                ("SectorRelativeStrengthPct", "SectorAvgChangePct"),
            )
        ),
        "vwap": vwap,
        "vwap_relationship": vwap_relationship,
        "vwap_distance_pct": vwap_distance,
        "session_high_known": session_high,
        "distance_from_session_high_pct": distance_from_high,
        "opening_range_state": _json_scalar(
            _first_present(row, ("OpeningRangeState", "OpeningRangePosition"))
        ),
        "ignition_state": _json_scalar(
            _first_present(row, ("IgnitionState", "LeaderIgnitionState"))
        ),
        "time_since_market_open_minutes": round(elapsed, 6) if elapsed is not None else None,
        "existing_causal_features": {
            name: _json_scalar(row.get(name)) for name in CAUSAL_SOURCE_FIELDS
        },
    }
    features["unavailable_causal_fields"] = sorted(
        key
        for key, value in features.items()
        if key not in {"existing_causal_features", "unavailable_causal_fields"}
        and value is None
    )
    return features


class NGTradedValueShadowRecorder:
    """Append-only Experiment 4 observation recorder with restart dedupe."""

    def __init__(
        self,
        output_dir: Path | str,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.clock = clock or _now_ist
        self._seen: set[str] = set()
        self._loaded_sessions: set[str] = set()

    def _path(self, session_date: str) -> Path:
        return self.output_dir / "observations" / f"observations_{session_date}.jsonl"

    def _load_seen(self, session_date: str) -> None:
        if session_date in self._loaded_sessions:
            return
        path = self._path(session_date)
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    observation_id = str(row.get("observation_id") or "")
                    if observation_id:
                        self._seen.add(observation_id)
        except OSError:
            pass
        self._loaded_sessions.add(session_date)

    def observe_p5(
        self,
        row: Mapping[str, Any],
        opening_leaders: Iterable[str],
        *,
        evaluation: Optional[P5Evaluation] = None,
        source_record: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        actual = evaluation or evaluate_p5(row, opening_leaders)
        if not is_sole_traded_value_failure(row, opening_leaders, actual):
            return False
        timestamp = _parse_ist_timestamp(row.get("When"))
        symbol = str(row.get("Symbol") or "").strip().upper()
        if timestamp is None or not symbol:
            return False
        session_date = timestamp.date().isoformat()
        source = dict(source_record or {})
        identity = "|".join(
            (
                EXPERIMENT_ID,
                POLICY_VERSION,
                session_date,
                symbol,
                timestamp.isoformat(),
                str(source.get("row_sha256") or ""),
            )
        )
        observation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self._load_seen(session_date)
        if observation_id in self._seen:
            return False
        payload = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "experiment_id": EXPERIMENT_ID,
            "record_kind": "signal_observation",
            "observation_id": observation_id,
            "recorded_at": self.clock().astimezone(IST).isoformat(),
            "session_date": session_date,
            "symbol": symbol,
            "observation_timestamp": timestamp.isoformat(),
            "qualification_lane": QUALIFICATION_LANE,
            "production_policy_version": POLICY_VERSION,
            "production_qualification_result": False,
            "production_rejection_reason": actual.reason,
            "sole_failing_production_gate": "traded_value",
            "shadow_only": True,
            "promotion_allowed": False,
            "signal_features": build_signal_features(row),
            "source_record": source,
        }
        path = self._path(session_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._seen.add(observation_id)
        return True


def _parse_bar_timestamp(value: Any) -> Optional[datetime]:
    parsed = _parse_ist_timestamp(value)
    return parsed.astimezone(IST) if parsed is not None else None


def _first_fully_future_minute(timestamp: datetime) -> datetime:
    result = timestamp.replace(second=0, microsecond=0)
    if timestamp.second or timestamp.microsecond:
        result += timedelta(minutes=1)
    return result


def _load_minute_bars(path: Path) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8", newline="")
    except OSError:
        return bars
    with handle:
        for row in csv.DictReader(handle):
            timestamp = _parse_bar_timestamp(row.get("datetime") or row.get("When"))
            high = _finite_float(row.get("high") or row.get("High"))
            low = _finite_float(row.get("low") or row.get("Low"))
            close = _finite_float(row.get("close") or row.get("Close"))
            if timestamp is None or high is None or low is None:
                continue
            bars.append({"timestamp": timestamp, "high": high, "low": low, "close": close})
    bars.sort(key=lambda item: item["timestamp"])
    return bars


def _load_feature_rows_by_symbol(
    path: Optional[Path], symbols: set[str]
) -> dict[str, list[dict[str, Any]]]:
    result = {symbol: [] for symbol in symbols}
    if path is None:
        return result
    try:
        handle = path.open("r", encoding="utf-8", newline="")
    except OSError:
        return result
    with handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("Symbol") or "").strip().upper()
            if symbol not in result:
                continue
            timestamp = _parse_ist_timestamp(row.get("When"))
            if timestamp is None:
                continue
            result[symbol].append({**row, "_timestamp": timestamp})
    for rows in result.values():
        rows.sort(key=lambda item: item["_timestamp"])
    return result


def _excursion(bars: list[dict[str, Any]], price: float) -> tuple[Optional[float], Optional[float]]:
    if not bars or price <= 0:
        return None, None
    return (
        (max(float(bar["high"]) for bar in bars) / price - 1.0) * 100.0,
        (min(float(bar["low"]) for bar in bars) / price - 1.0) * 100.0,
    )


def _threshold_evidence(
    bars: list[dict[str, Any]], price: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    first_seen: dict[str, Any] = {}
    events: dict[datetime, list[str]] = {}
    for threshold in POSITIVE_THRESHOLDS:
        key = f"plus_{str(threshold).replace('.', '_')}_pct"
        target = price * (1.0 + threshold / 100.0)
        matched = next((bar for bar in bars if float(bar["high"]) >= target), None)
        first_seen[key] = matched["timestamp"].isoformat() if matched else None
        if matched:
            events.setdefault(matched["timestamp"], []).append(key)
    for threshold in NEGATIVE_THRESHOLDS:
        key = f"minus_{str(abs(threshold)).replace('.', '_')}_pct"
        target = price * (1.0 + threshold / 100.0)
        matched = next((bar for bar in bars if float(bar["low"]) <= target), None)
        first_seen[key] = matched["timestamp"].isoformat() if matched else None
        if matched:
            events.setdefault(matched["timestamp"], []).append(key)
    ordering = [
        {
            "timestamp": timestamp.isoformat(),
            "thresholds": sorted(names),
            "within_bar_order": "indeterminate" if len(names) > 1 else "determinate",
        }
        for timestamp, names in sorted(events.items())
    ]
    return first_seen, ordering


class NGTradedValueShadowOutcomeFinalizer:
    """Post-session outcome writer; never mutates signal-time observations."""

    def __init__(self, output_dir: Path | str, *, clock: Optional[Callable[[], datetime]] = None):
        self.output_dir = Path(output_dir)
        self.clock = clock or _now_ist

    def finalize_session(
        self,
        session_date: str,
        minute_dir: Path | str,
        *,
        live_feature_path: Optional[Path | str] = None,
    ) -> int:
        observation_path = (
            self.output_dir / "observations" / f"observations_{session_date}.jsonl"
        )
        outcome_path = self.output_dir / "outcomes" / f"outcomes_{session_date}.jsonl"
        existing: set[str] = set()
        try:
            with outcome_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        existing.add(str(json.loads(line).get("observation_id") or ""))
                    except (TypeError, json.JSONDecodeError):
                        continue
        except OSError:
            pass
        try:
            observations = [json.loads(line) for line in observation_path.read_text().splitlines()]
        except OSError:
            return 0
        minute_root = Path(minute_dir)
        feature_path = Path(live_feature_path) if live_feature_path else None
        symbols = {
            str(observation.get("symbol") or "").strip().upper()
            for observation in observations
            if observation.get("symbol")
        }
        feature_rows = _load_feature_rows_by_symbol(feature_path, symbols)
        bar_cache: dict[str, list[dict[str, Any]]] = {}
        written = 0
        for observation in observations:
            observation_id = str(observation.get("observation_id") or "")
            if not observation_id or observation_id in existing:
                continue
            symbol = str(observation.get("symbol") or "").strip().upper()
            timestamp = _parse_ist_timestamp(observation.get("observation_timestamp"))
            signal_features = observation.get("signal_features") or {}
            price = _finite_float(signal_features.get("price"))
            session_open = _finite_float(signal_features.get("session_open"))
            required = _finite_float(signal_features.get("production_traded_value_requirement"))
            if symbol not in bar_cache:
                bar_cache[symbol] = (
                    _load_minute_bars(minute_root / f"{symbol}.csv") if symbol else []
                )
            bars = bar_cache[symbol]
            if timestamp is None or price in (None, 0):
                future_bars: list[dict[str, Any]] = []
            else:
                first_bar = _first_fully_future_minute(timestamp)
                future_bars = [bar for bar in bars if bar["timestamp"] >= first_bar]
            outcomes: dict[str, Any] = {}
            for minutes in HORIZONS:
                horizon_bars = (
                    [bar for bar in future_bars if bar["timestamp"] < timestamp + timedelta(minutes=minutes)]
                    if timestamp is not None
                    else []
                )
                mfe, mae = _excursion(horizon_bars, float(price or 0))
                outcomes[f"mfe_{minutes}m_pct"] = mfe
                outcomes[f"mae_{minutes}m_pct"] = mae
            mfe_eod, mae_eod = _excursion(future_bars, float(price or 0))
            outcomes["mfe_eod_pct"] = mfe_eod
            outcomes["mae_eod_pct"] = mae_eod
            thresholds, ordering = _threshold_evidence(future_bars, float(price or 0))

            floor_cross = None
            if timestamp is not None and required is not None:
                floor_cross = next(
                    (
                        row
                        for row in feature_rows.get(symbol, [])
                        if row["_timestamp"] > timestamp
                        and (_finite_float(row.get("TradedValue")) or -1.0) >= required
                    ),
                    None,
                )
            full_session_high = max((float(bar["high"]) for bar in bars), default=None)
            cross_price = _finite_float(floor_cross.get("LastPrice")) if floor_cross else None
            move_consumed = None
            if (
                cross_price is not None
                and session_open is not None
                and full_session_high is not None
                and full_session_high != session_open
            ):
                move_consumed = (cross_price - session_open) / (full_session_high - session_open) * 100.0
            payload = {
                "schema_version": OUTCOME_SCHEMA_VERSION,
                "experiment_id": EXPERIMENT_ID,
                "record_kind": "outcome_label",
                "observation_id": observation_id,
                "labeled_at": self.clock().astimezone(IST).isoformat(),
                "session_date": session_date,
                "symbol": symbol,
                "observation_timestamp": observation.get("observation_timestamp"),
                "outcome_source": {
                    "minute_path": str((minute_root / f"{symbol}.csv").resolve()),
                    "live_feature_path": str(feature_path.resolve()) if feature_path else None,
                    "first_fully_future_bar": (
                        future_bars[0]["timestamp"].isoformat() if future_bars else None
                    ),
                    "last_available_bar": bars[-1]["timestamp"].isoformat() if bars else None,
                    "eod_coverage_complete": bool(
                        bars and bars[-1]["timestamp"].hour * 60 + bars[-1]["timestamp"].minute >= 15 * 60 + 29
                    ),
                },
                "excursions": outcomes,
                "threshold_attainment": {
                    key: value is not None for key, value in thresholds.items()
                },
                "threshold_first_seen": thresholds,
                "first_threshold_ordering": ordering,
                "production_floor_cross": {
                    "crossed": floor_cross is not None,
                    "timestamp": floor_cross["_timestamp"].isoformat() if floor_cross else None,
                    "price": cross_price,
                    "traded_value": _finite_float(floor_cross.get("TradedValue")) if floor_cross else None,
                    "move_consumed_pct": move_consumed,
                },
            }
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            fd = os.open(outcome_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            existing.add(observation_id)
            written += 1
        return written


__all__ = [
    "CAUSAL_SOURCE_FIELDS",
    "EXPERIMENT_ID",
    "NGTradedValueShadowOutcomeFinalizer",
    "NGTradedValueShadowRecorder",
    "build_signal_features",
    "is_sole_traded_value_failure",
]

"""Causal observed-minute inputs for Emerging Leader shadow plans.

This module samples the existing atomic Kite latest-tick snapshot.  It never
uses Kite's ``low`` field (which is the session low), never calls a broker API,
and never writes to the frozen Emerging Leader radar database.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from pkscreener.classes.EmergingLeaderShadowPlan import (
    LIVE_PARTIAL_1M,
    ShadowPlanInputs,
)


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc
ATR_LENGTH = 14
MINUTE_MS = 60_000
OBSERVATION_RETENTION_MS = 3 * MINUTE_MS


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _parse_observed_at(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(IST)


def _minute_epoch_ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000) // MINUTE_MS * MINUTE_MS


def _as_datetime(epoch_ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch_ms / 1000, tz=IST)


@dataclass
class _MinuteBar:
    session_date: str
    symbol: str
    minute_epoch_ms: int
    open: float
    high: float
    low: float
    close: float
    first_observed_epoch_ms: int
    last_observed_epoch_ms: int
    observation_count: int = 1

    def update(self, price: float, observed_epoch_ms: int) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.last_observed_epoch_ms = observed_epoch_ms
        self.observation_count += 1


class EmergingLeaderCausalMinuteProvider:
    """Build causal one-minute inputs from observed atomic tick snapshots.

    Bars are deliberately described as *observed* bars: the upstream file is a
    two-second latest-value snapshot, not a lossless exchange tick tape.  The
    provider still enforces strict event-time causality and never consumes a
    later observation when calculating an earlier alert.
    """

    def __init__(
        self,
        ticks_path: Path,
        state_db: Path,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.ticks_path = Path(ticks_path)
        self.state_db = Path(state_db)
        self.clock = clock or (lambda: dt.datetime.now(IST))
        started = self.clock().astimezone(IST)
        self.started_epoch_ms = int(started.timestamp() * 1000)
        # The startup minute is incomplete by definition.  Fail closed until
        # the first minute whose boundary the provider actually witnessed.
        self.valid_from_minute_epoch_ms = _minute_epoch_ms(started) + MINUTE_MS
        self._last_seen_epoch_ms: dict[str, int] = {}
        self._active: dict[tuple[str, int], _MinuteBar] = {}
        self._observations: dict[str, deque[tuple[int, float]]] = defaultdict(deque)
        self._last_file_signature: tuple[int, int] | None = None
        self._initialize_state()

    def _state(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_db, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize_state(self) -> None:
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        with self._state() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_observed_minute_bars(
                    session_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    minute_epoch_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    first_observed_epoch_ms INTEGER NOT NULL,
                    last_observed_epoch_ms INTEGER NOT NULL,
                    observation_count INTEGER NOT NULL,
                    source_mode TEXT NOT NULL,
                    PRIMARY KEY(session_date, symbol, minute_epoch_ms)
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_minute_symbol_time
                    ON shadow_observed_minute_bars(session_date, symbol, minute_epoch_ms);
                """
            )

    def observe_latest(self) -> int:
        """Read one new atomic file generation and aggregate its fresh ticks."""
        stat = self.ticks_path.stat()
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        if signature == self._last_file_signature:
            self._finalize_completed(self.clock().astimezone(IST))
            return 0
        payload = json.loads(self.ticks_path.read_text(encoding="utf-8"))
        if not isinstance(payload, (dict, list)):
            raise ValueError("unsupported Kite latest-tick snapshot")
        self._last_file_signature = signature
        return self.observe_ticks(payload, provider_seen_at=self.clock().astimezone(IST))

    @staticmethod
    def _iter_ticks(payload) -> Iterable[tuple[str, Mapping]]:
        if isinstance(payload, dict):
            values = payload.items()
        else:
            values = ((str((tick or {}).get("symbol") or ""), tick) for tick in payload)
        for key, tick in values:
            if not isinstance(tick, Mapping):
                continue
            symbol = str(tick.get("symbol") or key or "").strip().upper()
            if symbol:
                yield symbol, tick

    def observe_ticks(self, payload, provider_seen_at: dt.datetime | None = None) -> int:
        """Aggregate only distinct prices whose ``received_at`` is now known."""
        seen_at = (provider_seen_at or self.clock()).astimezone(IST)
        seen_ms = int(seen_at.timestamp() * 1000)
        accepted = 0
        for symbol, tick in self._iter_ticks(payload):
            price = _finite_positive(tick.get("last_price"))
            observed_at = _parse_observed_at(tick.get("received_at"))
            if price is None or observed_at is None:
                continue
            observed_ms = int(observed_at.timestamp() * 1000)
            previous = self._last_seen_epoch_ms.get(symbol)
            if previous is not None and observed_ms <= previous:
                continue
            self._last_seen_epoch_ms[symbol] = observed_ms
            # Do not reinterpret a stale value found in the first file read as
            # historical tape.  A small allowance covers atomic-save latency.
            if observed_ms < self.started_epoch_ms - 5_000:
                continue
            # A timestamp beyond the reader's own clock is not causal input.
            if observed_ms > seen_ms + 2_000:
                continue
            minute_ms = observed_ms // MINUTE_MS * MINUTE_MS
            session_date = observed_at.date().isoformat()
            key = (symbol, minute_ms)
            bar = self._active.get(key)
            if bar is None:
                bar = _MinuteBar(
                    session_date=session_date,
                    symbol=symbol,
                    minute_epoch_ms=minute_ms,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    first_observed_epoch_ms=observed_ms,
                    last_observed_epoch_ms=observed_ms,
                )
                self._active[key] = bar
            else:
                bar.update(price, observed_ms)
            self._observations[symbol].append((observed_ms, price))
            accepted += 1
        self._trim_observations(seen_ms)
        self._finalize_completed(seen_at)
        return accepted

    def _trim_observations(self, now_epoch_ms: int) -> None:
        cutoff = now_epoch_ms - OBSERVATION_RETENTION_MS
        empty = []
        for symbol, values in self._observations.items():
            while values and values[0][0] < cutoff:
                values.popleft()
            if not values:
                empty.append(symbol)
        for symbol in empty:
            self._observations.pop(symbol, None)

    def _finalize_completed(self, now: dt.datetime) -> None:
        now_ms = int(now.timestamp() * 1000)
        complete_keys = [
            key for key, bar in self._active.items()
            if bar.minute_epoch_ms + MINUTE_MS <= now_ms
        ]
        if not complete_keys:
            return
        rows = []
        for key in complete_keys:
            bar = self._active.pop(key)
            # Never persist the incomplete startup minute as ATR evidence.
            if bar.minute_epoch_ms < self.valid_from_minute_epoch_ms:
                continue
            rows.append(
                (
                    bar.session_date, bar.symbol, bar.minute_epoch_ms,
                    bar.open, bar.high, bar.low, bar.close,
                    bar.first_observed_epoch_ms, bar.last_observed_epoch_ms,
                    bar.observation_count, "KITE_LATEST_SNAPSHOT_OBSERVED_1M",
                )
            )
        if rows:
            with self._state() as connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO shadow_observed_minute_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )

    @staticmethod
    def _last_consecutive_run(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        if not rows:
            return []
        run = [rows[-1]]
        for row in reversed(rows[:-1]):
            if int(run[0]["minute_epoch_ms"]) - int(row["minute_epoch_ms"]) != MINUTE_MS:
                break
            run.insert(0, row)
        return run

    @staticmethod
    def _wilder_atr(rows: list[sqlite3.Row]) -> float | None:
        if len(rows) < ATR_LENGTH + 1:
            return None
        true_ranges = []
        for previous, current in zip(rows, rows[1:]):
            high = float(current["high"])
            low = float(current["low"])
            previous_close = float(previous["close"])
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        if len(true_ranges) < ATR_LENGTH:
            return None
        atr = sum(true_ranges[:ATR_LENGTH]) / ATR_LENGTH
        for true_range in true_ranges[ATR_LENGTH:]:
            atr = ((atr * (ATR_LENGTH - 1)) + true_range) / ATR_LENGTH
        return atr if math.isfinite(atr) and atr > 0 else None

    def __call__(self, event) -> ShadowPlanInputs:
        candidate = dt.datetime.fromtimestamp(
            int(event["event_epoch_ms"]) / 1000, tz=IST
        )
        candidate_ms = int(event["event_epoch_ms"])
        entry = _finite_positive(event["last_price"])
        event_minute_ms = candidate_ms // MINUTE_MS * MINUTE_MS
        symbol = str(event["symbol"]).upper()
        causal_observations = [
            (observed_ms, price)
            for observed_ms, price in self._observations.get(symbol, ())
            if observed_ms <= candidate_ms
        ]
        matching_entry_observations = [
            (observed_ms, price)
            for observed_ms, price in causal_observations
            if entry is not None and math.isclose(price, entry, rel_tol=0.0, abs_tol=1e-9)
        ]
        price_observed_at = (
            _as_datetime(matching_entry_observations[-1][0])
            if matching_entry_observations else None
        )
        base = ShadowPlanInputs(
            candidate_detected_at=candidate,
            reference_entry=entry,
            price_observed_at=price_observed_at,
        )
        if event_minute_ms < self.valid_from_minute_epoch_ms:
            return base

        prices = [
            (observed_ms, price)
            for observed_ms, price in causal_observations
            if event_minute_ms <= observed_ms <= candidate_ms
        ]
        if not prices:
            return base
        low_at_ms, signal_low = min(prices, key=lambda item: (item[1], item[0]))

        session_date = candidate.date().isoformat()
        with self._state() as connection:
            rows = connection.execute(
                "SELECT minute_epoch_ms,high,low,close FROM shadow_observed_minute_bars "
                "WHERE session_date=? AND symbol=? AND minute_epoch_ms+?<=? "
                "ORDER BY minute_epoch_ms",
                (session_date, str(event["symbol"]).upper(), MINUTE_MS, candidate_ms),
            ).fetchall()
        run = self._last_consecutive_run(rows)
        atr14 = self._wilder_atr(run)
        if atr14 is None:
            return ShadowPlanInputs(
                candidate_detected_at=candidate,
                reference_entry=entry,
                price_observed_at=price_observed_at,
                signal_low=signal_low,
                signal_low_at=_as_datetime(low_at_ms),
                stop_input_mode=LIVE_PARTIAL_1M,
            )
        atr_at = _as_datetime(int(run[-1]["minute_epoch_ms"]) + MINUTE_MS)
        return ShadowPlanInputs(
            candidate_detected_at=candidate,
            reference_entry=entry,
            price_observed_at=price_observed_at,
            atr14=atr14,
            atr_input_at=atr_at,
            signal_low=signal_low,
            signal_low_at=_as_datetime(low_at_ms),
            stop_input_mode=LIVE_PARTIAL_1M,
        )

    def health(self) -> dict:
        with self._state() as connection:
            completed = int(
                connection.execute("SELECT COUNT(*) FROM shadow_observed_minute_bars").fetchone()[0]
            )
            symbols = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT symbol) FROM shadow_observed_minute_bars"
                ).fetchone()[0]
            )
        return {
            "source": "KITE_LATEST_SNAPSHOT_OBSERVED_1M",
            "completed_bars": completed,
            "symbols_with_completed_bars": symbols,
            "valid_from": _as_datetime(self.valid_from_minute_epoch_ms).isoformat(),
        }


__all__ = [
    "ATR_LENGTH",
    "EmergingLeaderCausalMinuteProvider",
]

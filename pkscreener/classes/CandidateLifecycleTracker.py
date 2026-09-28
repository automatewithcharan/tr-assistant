"""Durable observability for scanner candidates and optional Pine events.

This module records analytics only.  It does not qualify candidates, place
orders, or interpret a Pine event as evidence of a broker fill.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
SCHEMA_VERSION = 2
PINE_EVENTS = {"BUY_READY", "ENTRY", "LOW_LIQUIDITY_SKIP", "STOP_UPDATE", "EXIT"}
SECRET_MARKERS = (
    "secret", "token", "password", "authorization", "webhook", "api_key", "apikey",
)


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if ":" in symbol:
        prefix, remainder = symbol.split(":", 1)
        if prefix in {"NSE", "BSE"}:
            symbol = remainder
    if symbol.endswith("-EQ"):
        symbol = symbol[:-3]
    return symbol.strip()


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _utc(value: Any, default: Optional[datetime] = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value not in (None, ""):
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    elif default is not None:
        parsed = default
    else:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(UTC)


def _iso_utc(value: Any, default: Optional[datetime] = None) -> str:
    return _utc(value, default).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, Mapping):
        return {
            str(key): ("[REDACTED]" if any(marker in str(key).lower() for marker in SECRET_MARKERS)
                       else _json_safe(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        if bool(value != value):
            return None
    except Exception:
        pass
    return str(value)


def _json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))


class CandidateLifecycleTracker:
    """SQLite-backed candidate, outcome, Pine-event and paper-trade journal."""

    def __init__(
        self,
        output_dir: str,
        horizons: Iterable[int] = (1, 3, 5, 10, 15, 30),
        pine_match_max_age_minutes: float = 30.0,
        retention_days: int = 90,
    ) -> None:
        self.root = Path(output_dir) / "candidate_lifecycle"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "candidate_lifecycle.sqlite3"
        self.horizons = tuple(sorted({int(item) for item in horizons if int(item) > 0}))
        self.match_max_age_minutes = max(0.0, float(pine_match_max_age_minutes))
        self.retention_days = max(1, int(retention_days))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    detected_at_utc TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    lanes TEXT NOT NULL,
                    cycle_id TEXT,
                    rank_this_cycle INTEGER,
                    scanner_price REAL,
                    percentage_move REAL,
                    score_total REAL,
                    score_momentum REAL,
                    score_volume REAL,
                    score_liquidity REAL,
                    score_structure REAL,
                    score_penalties REAL,
                    movement_state TEXT,
                    liquidity_status TEXT,
                    snapshot_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    discord_selected INTEGER NOT NULL DEFAULT 1,
                    discord_sent INTEGER NOT NULL DEFAULT 0,
                    discord_sent_at_utc TEXT,
                    discord_message_id TEXT,
                    discord_delivery_result TEXT,
                    discord_delivery_error TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    pine_confirmed_at_utc TEXT,
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_symbol_time
                    ON candidates(symbol, detected_at_utc DESC);
                CREATE INDEX IF NOT EXISTS idx_candidates_date_status
                    ON candidates(trading_date, status);
                CREATE TABLE IF NOT EXISTS candidate_price_observations (
                    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    observed_at_utc TEXT NOT NULL,
                    price REAL NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, observed_at_utc)
                );
                CREATE INDEX IF NOT EXISTS idx_price_observations_candidate_time
                    ON candidate_price_observations(candidate_id, observed_at_utc);
                CREATE TABLE IF NOT EXISTS candidate_price_outcomes (
                    candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    highest_price REAL,
                    highest_at_utc TEXT,
                    lowest_price REAL,
                    lowest_at_utc TEXT,
                    mfe_pct REAL,
                    mae_pct REAL,
                    time_to_highest_seconds REAL,
                    time_to_lowest_seconds REAL,
                    eod_price REAL,
                    eod_at_utc TEXT,
                    monitoring_ended_at_utc TEXT,
                    completeness_status TEXT NOT NULL DEFAULT 'partial'
                );
                CREATE TABLE IF NOT EXISTS candidate_price_horizons (
                    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    horizon_minutes INTEGER NOT NULL,
                    price REAL,
                    observed_at_utc TEXT,
                    status TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, horizon_minutes)
                );
                CREATE TABLE IF NOT EXISTS pine_events (
                    pine_event_id TEXT PRIMARY KEY,
                    source_event_id TEXT,
                    idempotency_key TEXT,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    raw_symbol TEXT,
                    event_at_utc TEXT NOT NULL,
                    supplied_candidate_id TEXT,
                    normalized_json TEXT NOT NULL,
                    raw_payload_json TEXT NOT NULL,
                    ingestion_source TEXT NOT NULL,
                    received_at_utc TEXT NOT NULL,
                    processing_status TEXT NOT NULL DEFAULT 'accepted',
                    processing_note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pine_events_symbol_time
                    ON pine_events(symbol, event_at_utc);
                CREATE TABLE IF NOT EXISTS candidate_pine_links (
                    pine_event_id TEXT PRIMARY KEY REFERENCES pine_events(pine_event_id) ON DELETE CASCADE,
                    candidate_id TEXT REFERENCES candidates(candidate_id) ON DELETE SET NULL,
                    match_method TEXT NOT NULL,
                    match_confidence TEXT NOT NULL,
                    match_note TEXT,
                    alternatives_json TEXT,
                    seconds_from_candidate REAL,
                    scanner_price REAL,
                    pine_price REAL,
                    pre_entry_price_change REAL,
                    pre_entry_price_change_pct REAL,
                    pre_entry_mfe_pct REAL,
                    linked_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pine_links_candidate
                    ON candidate_pine_links(candidate_id);
                CREATE TABLE IF NOT EXISTS trade_lifecycle (
                    candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    entry_event_id TEXT REFERENCES pine_events(pine_event_id),
                    entry_at_utc TEXT,
                    signal_price REAL,
                    assumed_entry REAL,
                    quantity REAL,
                    initial_stop REAL,
                    one_r_level REAL,
                    target REAL,
                    risk_per_share REAL,
                    highest_price_since_entry REAL,
                    lowest_price_since_entry REAL,
                    stop_updates_json TEXT NOT NULL DEFAULT '[]',
                    exit_event_id TEXT REFERENCES pine_events(pine_event_id),
                    exit_at_utc TEXT,
                    exit_reason TEXT,
                    theoretical_exit_price REAL,
                    theoretical_pnl REAL,
                    theoretical_r REAL,
                    status TEXT NOT NULL DEFAULT 'OPEN'
                );
                """
            )
            # Additive migration for lifecycle databases created before inbound
            # webhook idempotency and ordering state were introduced.
            pine_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pine_events)")
            }
            for column, definition in (
                ("source_event_id", "TEXT"),
                ("idempotency_key", "TEXT"),
                ("raw_symbol", "TEXT"),
                ("processing_status", "TEXT NOT NULL DEFAULT 'accepted'"),
                ("processing_note", "TEXT"),
            ):
                if column not in pine_columns:
                    connection.execute(
                        f"ALTER TABLE pine_events ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_pine_events_idempotency
                   ON pine_events(idempotency_key) WHERE idempotency_key IS NOT NULL"""
            )
            connection.execute(
                "INSERT OR REPLACE INTO lifecycle_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _candidate_id(symbol: str, detected_at: datetime) -> str:
        local = detected_at.astimezone(IST)
        safe_symbol = "".join(char for char in symbol if char.isalnum())[:20] or "UNKNOWN"
        return f"NSE_{safe_symbol}_{local:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:12].upper()}"

    def create_candidate(
        self,
        row: Mapping[str, Any],
        detected_at: Any,
        cycle_id: str,
        config: Mapping[str, Any],
    ) -> str:
        detected = _utc(detected_at)
        symbol = normalize_symbol(row.get("Symbol"))
        if not symbol:
            raise ValueError("candidate symbol is required")
        existing = str(row.get("CandidateId") or row.get("candidate_id") or "").strip()
        candidate_id = existing or self._candidate_id(symbol, detected)
        config_json = _json(config)
        config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()[:16]
        snapshot = dict(row)
        snapshot["CandidateId"] = candidate_id
        snapshot["LifecycleSchemaVersion"] = SCHEMA_VERSION
        detected_iso = _iso_utc(detected)
        trading_date = detected.astimezone(IST).date().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO candidates(
                    candidate_id, symbol, exchange, detected_at_utc, trading_date,
                    lanes, cycle_id, rank_this_cycle, scanner_price, percentage_move,
                    score_total, score_momentum, score_volume, score_liquidity,
                    score_structure, score_penalties, movement_state, liquidity_status,
                    snapshot_json, config_hash, config_json, created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    candidate_id, symbol, str(row.get("Exchange") or "NSE").upper(),
                    detected_iso, trading_date,
                    str(row.get("Lanes") or row.get("Lane") or ""), str(cycle_id or ""),
                    int(_finite(row.get("CandidateRank")) or 0) or None,
                    _finite(row.get("PriceNow")), _finite(row.get("ChangePct")),
                    _finite(row.get("ScoreTotal")), _finite(row.get("ScoreMomentum")),
                    _finite(row.get("ScoreVolume")), _finite(row.get("ScoreLiquidity")),
                    _finite(row.get("ScoreStructure")), _finite(row.get("ScorePenalties")),
                    str(row.get("CandidateState") or "UNKNOWN"),
                    str(row.get("LiquidityStatus") or "UNKNOWN"),
                    _json(snapshot), config_hash, config_json, _iso_utc(datetime.now(UTC)),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO candidate_price_outcomes(candidate_id) VALUES(?)",
                (candidate_id,),
            )
            scanner_price = _finite(row.get("PriceNow"))
            if scanner_price is not None and scanner_price > 0:
                connection.execute(
                    """INSERT OR IGNORE INTO candidate_price_observations(
                        candidate_id, observed_at_utc, price, source
                    ) VALUES(?,?,?,'scanner_selected')""",
                    (candidate_id, detected_iso, scanner_price),
                )
                connection.execute(
                    """UPDATE candidate_price_outcomes SET
                       highest_price=COALESCE(highest_price, ?),
                       highest_at_utc=COALESCE(highest_at_utc, ?),
                       lowest_price=COALESCE(lowest_price, ?),
                       lowest_at_utc=COALESCE(lowest_at_utc, ?),
                       mfe_pct=COALESCE(mfe_pct, 0.0), mae_pct=COALESCE(mae_pct, 0.0),
                       time_to_highest_seconds=COALESCE(time_to_highest_seconds, 0.0),
                       time_to_lowest_seconds=COALESCE(time_to_lowest_seconds, 0.0)
                       WHERE candidate_id=?""",
                    (scanner_price, detected_iso, scanner_price, detected_iso, candidate_id),
                )
            connection.executemany(
                """INSERT OR IGNORE INTO candidate_price_horizons(
                    candidate_id, horizon_minutes, status
                ) VALUES(?,?, 'PENDING')""",
                [(candidate_id, horizon) for horizon in self.horizons],
            )
        return candidate_id

    def record_delivery(self, record: Mapping[str, Any]) -> None:
        candidate_id = str(record.get("candidate_id") or "").strip()
        if not candidate_id:
            return
        status = str(record.get("delivery_status") or "unknown")
        delivered = status == "delivered"
        delivered_at = record.get("delivered_at") or record.get("discord_success_at")
        with self._connect() as connection:
            connection.execute(
                """UPDATE candidates SET
                    discord_sent = CASE WHEN ? THEN 1 ELSE discord_sent END,
                    discord_sent_at_utc = CASE WHEN ? THEN ? ELSE discord_sent_at_utc END,
                    discord_message_id = COALESCE(?, discord_message_id),
                    discord_delivery_result = ?, discord_delivery_error = ?
                   WHERE candidate_id = ?""",
                (
                    delivered, delivered,
                    _iso_utc(delivered_at) if delivered and delivered_at else None,
                    record.get("discord_message_id"), status,
                    str(record.get("error") or "") or None, candidate_id,
                ),
            )

    def observe_prices(
        self,
        ticks: Mapping[str, Mapping[str, Any]],
        observed_at: Any,
        source: str = "kite_live_tick",
    ) -> int:
        observed = _utc(observed_at)
        observed_iso = _iso_utc(observed)
        trading_date = observed.astimezone(IST).date().isoformat()
        normalized_ticks = {normalize_symbol(symbol): tick for symbol, tick in ticks.items()}
        updated = 0
        with self._connect() as connection:
            candidates = connection.execute(
                """SELECT candidate_id, symbol, detected_at_utc, scanner_price, status
                   FROM candidates WHERE trading_date=?""",
                (trading_date,),
            ).fetchall()
            for candidate in candidates:
                tick = normalized_ticks.get(candidate["symbol"])
                price = _finite((tick or {}).get("last_price"))
                if price is None or price <= 0:
                    continue
                detected = _utc(candidate["detected_at_utc"])
                if observed < detected:
                    continue
                inserted = connection.execute(
                    """INSERT OR IGNORE INTO candidate_price_observations(
                        candidate_id, observed_at_utc, price, source
                    ) VALUES(?,?,?,?)""",
                    (candidate["candidate_id"], observed_iso, price, source),
                ).rowcount
                if not inserted:
                    continue
                updated += 1
                scanner_price = _finite(candidate["scanner_price"])
                outcome = connection.execute(
                    "SELECT * FROM candidate_price_outcomes WHERE candidate_id=?",
                    (candidate["candidate_id"],),
                ).fetchone()
                high = _finite(outcome["highest_price"]) if outcome else None
                low = _finite(outcome["lowest_price"]) if outcome else None
                high_at = outcome["highest_at_utc"] if outcome else None
                low_at = outcome["lowest_at_utc"] if outcome else None
                if high is None or price > high:
                    high, high_at = price, observed_iso
                if low is None or price < low:
                    low, low_at = price, observed_iso
                mfe = ((high / scanner_price) - 1.0) * 100.0 if scanner_price else None
                mae = ((low / scanner_price) - 1.0) * 100.0 if scanner_price else None
                high_delay = (
                    (_utc(high_at) - detected).total_seconds() if high_at else None
                )
                low_delay = (
                    (_utc(low_at) - detected).total_seconds() if low_at else None
                )
                connection.execute(
                    """UPDATE candidate_price_outcomes SET highest_price=?, highest_at_utc=?,
                       lowest_price=?, lowest_at_utc=?, mfe_pct=?, mae_pct=?,
                       time_to_highest_seconds=?, time_to_lowest_seconds=?
                       WHERE candidate_id=?""",
                    (
                        high, high_at, low, low_at, mfe, mae, high_delay, low_delay,
                        candidate["candidate_id"],
                    ),
                )
                elapsed_minutes = (observed - detected).total_seconds() / 60.0
                for horizon in self.horizons:
                    if elapsed_minutes >= horizon:
                        connection.execute(
                            """UPDATE candidate_price_horizons SET price=?, observed_at_utc=?,
                               status='OBSERVED' WHERE candidate_id=? AND horizon_minutes=?
                               AND status='PENDING'""",
                            (price, observed_iso, candidate["candidate_id"], horizon),
                        )
                if (
                    candidate["status"] == "PENDING"
                    and elapsed_minutes > self.match_max_age_minutes
                ):
                    connection.execute(
                        "UPDATE candidates SET status='NO_PINE_CONFIRMATION' WHERE candidate_id=?",
                        (candidate["candidate_id"],),
                    )
                trade = connection.execute(
                    "SELECT * FROM trade_lifecycle WHERE candidate_id=? AND status='OPEN'",
                    (candidate["candidate_id"],),
                ).fetchone()
                if trade and observed >= _utc(trade["entry_at_utc"]):
                    trade_high = _finite(trade["highest_price_since_entry"])
                    trade_low = _finite(trade["lowest_price_since_entry"])
                    connection.execute(
                        """UPDATE trade_lifecycle SET highest_price_since_entry=?,
                           lowest_price_since_entry=? WHERE candidate_id=?""",
                        (
                            max(price, trade_high) if trade_high is not None else price,
                            min(price, trade_low) if trade_low is not None else price,
                            candidate["candidate_id"],
                        ),
                    )
        return updated

    def finalize_eod(self, trading_date: str, finalized_at: Any = None) -> int:
        final_at = _utc(finalized_at or datetime.now(UTC))
        final_iso = _iso_utc(final_at)
        finalized = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT candidate_id, status FROM candidates WHERE trading_date=?",
                (trading_date,),
            ).fetchall()
            for row in rows:
                last = connection.execute(
                    """SELECT price, observed_at_utc FROM candidate_price_observations
                       WHERE candidate_id=? ORDER BY observed_at_utc DESC LIMIT 1""",
                    (row["candidate_id"],),
                ).fetchone()
                connection.execute(
                    """UPDATE candidate_price_outcomes SET eod_price=?, eod_at_utc=?,
                       monitoring_ended_at_utc=?, completeness_status=? WHERE candidate_id=?""",
                    (
                        last["price"] if last else None,
                        last["observed_at_utc"] if last else None,
                        final_iso, "complete" if last else "missing_eod_price", row["candidate_id"],
                    ),
                )
                connection.execute(
                    """UPDATE candidate_price_horizons SET status='MISSING'
                       WHERE candidate_id=? AND status='PENDING'""",
                    (row["candidate_id"],),
                )
                if row["status"] in {"PENDING", "NO_PINE_CONFIRMATION"}:
                    connection.execute(
                        "UPDATE candidates SET status='EOD_UNCONFIRMED' WHERE candidate_id=?",
                        (row["candidate_id"],),
                    )
                finalized += 1
        return finalized

    def _match_candidate(
        self,
        connection: sqlite3.Connection,
        supplied_id: str,
        symbol: str,
        event_at: datetime,
        event_type: str,
    ) -> tuple[Optional[sqlite3.Row], str, str, str, list[str]]:
        if supplied_id:
            exact = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (supplied_id,)
            ).fetchone()
            if exact and _utc(exact["detected_at_utc"]) <= event_at:
                return exact, "candidate_id", "exact", "", []
            return None, "unmatched", "unmatched", "candidate_id missing or event predates candidate", []
        if not symbol:
            return None, "unmatched", "unmatched", "symbol unavailable", []
        if event_type in {"STOP_UPDATE", "EXIT"}:
            open_trades = connection.execute(
                """SELECT c.* FROM candidates c JOIN trade_lifecycle t
                   ON t.candidate_id=c.candidate_id
                   WHERE c.symbol=? AND t.status='OPEN' AND t.entry_at_utc<=?
                   ORDER BY t.entry_at_utc DESC, c.candidate_id DESC""",
                (symbol, _iso_utc(event_at)),
            ).fetchall()
            if open_trades:
                alternatives = [row["candidate_id"] for row in open_trades[1:]]
                note = f"most recent of {len(open_trades)} open trades" if alternatives else ""
                return open_trades[0], "symbol_open_trade", "likely", note, alternatives
        lower = _iso_utc(event_at - timedelta(minutes=self.match_max_age_minutes))
        parameters: list[Any] = [symbol, lower, _iso_utc(event_at)]
        query = (
            "SELECT * FROM candidates WHERE symbol=? AND detected_at_utc>=? "
            "AND detected_at_utc<=?"
        )
        if event_type == "ENTRY":
            query += " AND status IN ('PENDING','NO_PINE_CONFIRMATION')"
        query += " ORDER BY detected_at_utc DESC, candidate_id DESC"
        matches = connection.execute(query, parameters).fetchall()
        if not matches:
            return None, "unmatched", "unmatched", "no candidate inside match window", []
        newest_time = matches[0]["detected_at_utc"]
        tied = [row for row in matches if row["detected_at_utc"] == newest_time]
        alternatives = [row["candidate_id"] for row in matches[1:]]
        if len(tied) > 1:
            return None, "unmatched", "ambiguous", "multiple candidates share newest timestamp", [row["candidate_id"] for row in tied]
        note = f"most recent of {len(matches)} eligible candidates" if len(matches) > 1 else ""
        return matches[0], "symbol_time_window", "likely", note, alternatives

    def ingest_pine_event(
        self,
        payload: Mapping[str, Any],
        ingestion_source: str = "manual_cli",
        received_at: Any = None,
    ) -> dict[str, Any]:
        raw = dict(payload)
        event_type = str(payload.get("event") or payload.get("event_type") or "").strip().upper()
        if event_type == "BUY":
            event_type = "ENTRY"
        if event_type not in PINE_EVENTS:
            raise ValueError(f"unsupported Pine lifecycle event: {event_type or 'missing'}")
        event_at = _utc(payload.get("timestamp") or payload.get("time"), _utc(received_at))
        received = _utc(received_at)
        raw_symbol = str(payload.get("symbol") or payload.get("ticker") or "").strip()
        symbol = normalize_symbol(raw_symbol)
        supplied_id = str(payload.get("candidate_id") or "").strip()
        source_event_id = str(payload.get("event_id") or "").strip()
        normalized = {
            "event": event_type,
            "symbol": symbol,
            "timestamp_utc": _iso_utc(event_at),
            "candidate_id": supplied_id or None,
            "signal_price": _finite(payload.get("signal_price")),
            "assumed_entry": _finite(payload.get("assumed_entry", payload.get("entry_price"))),
            "qty": _finite(payload.get("qty", payload.get("quantity"))),
            "initial_stop": _finite(payload.get("initial_stop", payload.get("stop"))),
            "one_r_level": _finite(payload.get("one_r_level", payload.get("one_r"))),
            "target": _finite(payload.get("target")),
            "risk_per_share": _finite(payload.get("risk_per_share")),
            "stop_price": _finite(payload.get("stop_price", payload.get("stop"))),
            "old_stop": _finite(payload.get("old_stop")),
            "new_stop": _finite(payload.get("new_stop")),
            "exit_price": _finite(payload.get("exit_price")),
            "theoretical_pnl": _finite(payload.get("theoretical_pnl")),
            "intended_qty": _finite(payload.get("intended_qty")),
            "liquidity_capacity": _finite(payload.get("liquidity_capacity")),
            "reason": str(payload.get("reason") or payload.get("exit_reason") or "").strip().upper() or None,
        }
        if event_type == "STOP_UPDATE" and normalized["stop_price"] is None:
            normalized["stop_price"] = normalized["new_stop"]
        # Explicit IDs are scoped separately from derived fingerprints.  The
        # fingerprint intentionally excludes unknown/raw fields so harmless
        # retries with metadata reordered or augmented remain idempotent.
        identity_material = (
            "explicit:" + source_event_id
            if source_event_id
            else "fingerprint:" + _json(normalized)
        )
        idempotency_key = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
        event_id = "pine-" + idempotency_key[:32]
        with self._connect() as connection:
            inserted = connection.execute(
                """INSERT OR IGNORE INTO pine_events(
                    pine_event_id, source_event_id, idempotency_key, event_type, symbol,
                    raw_symbol, event_at_utc, supplied_candidate_id, normalized_json,
                    raw_payload_json, ingestion_source, received_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, source_event_id or None, idempotency_key, event_type,
                    symbol or None, raw_symbol or None, _iso_utc(event_at), supplied_id or None,
                    _json(normalized), _json(raw), str(ingestion_source), _iso_utc(received),
                ),
            )
            if inserted.rowcount == 0:
                existing = connection.execute(
                    """SELECT e.pine_event_id, e.source_event_id, e.processing_status,
                              e.processing_note, l.candidate_id, l.match_method,
                              l.match_confidence, l.match_note, l.alternatives_json,
                              l.seconds_from_candidate, l.pre_entry_price_change,
                              l.pre_entry_price_change_pct, l.pre_entry_mfe_pct
                       FROM pine_events e LEFT JOIN candidate_pine_links l
                         ON l.pine_event_id=e.pine_event_id
                       WHERE e.idempotency_key=?""",
                    (idempotency_key,),
                ).fetchone()
                return {
                    "status": "duplicate",
                    "event_id": (existing["source_event_id"] or existing["pine_event_id"]),
                    "pine_event_id": existing["pine_event_id"],
                    "candidate_id": existing["candidate_id"],
                    "match_method": existing["match_method"],
                    "match_confidence": existing["match_confidence"],
                    "match_note": existing["match_note"],
                    "alternatives": json.loads(existing["alternatives_json"] or "[]"),
                    "seconds_from_candidate": existing["seconds_from_candidate"],
                    "pre_entry_price_change": existing["pre_entry_price_change"],
                    "pre_entry_price_change_pct": existing["pre_entry_price_change_pct"],
                    "pre_entry_mfe_pct": existing["pre_entry_mfe_pct"],
                    "processing_status": existing["processing_status"],
                    "processing_note": existing["processing_note"],
                }
            candidate, method, confidence, note, alternatives = self._match_candidate(
                connection, supplied_id, symbol, event_at, event_type
            )
            candidate_id = candidate["candidate_id"] if candidate else None
            scanner_price = _finite(candidate["scanner_price"]) if candidate else None
            pine_price = (
                normalized["assumed_entry"] or normalized["signal_price"]
                or normalized["exit_price"] or normalized["stop_price"]
            )
            seconds = (event_at - _utc(candidate["detected_at_utc"])).total_seconds() if candidate else None
            pre_change = pine_price - scanner_price if pine_price is not None and scanner_price else None
            pre_change_pct = pre_change / scanner_price * 100.0 if pre_change is not None and scanner_price else None
            pre_entry_mfe = None
            if candidate and event_type == "ENTRY" and scanner_price:
                high = connection.execute(
                    """SELECT MAX(price) AS high FROM candidate_price_observations
                       WHERE candidate_id=? AND observed_at_utc<=?""",
                    (candidate_id, _iso_utc(event_at)),
                ).fetchone()["high"]
                high = _finite(high)
                pre_entry_mfe = ((high / scanner_price) - 1.0) * 100.0 if high else None
            connection.execute(
                """INSERT OR REPLACE INTO candidate_pine_links(
                    pine_event_id, candidate_id, match_method, match_confidence, match_note,
                    alternatives_json, seconds_from_candidate, scanner_price, pine_price,
                    pre_entry_price_change, pre_entry_price_change_pct, pre_entry_mfe_pct,
                    linked_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, candidate_id, method, confidence, note or None,
                    _json(alternatives), seconds, scanner_price, pine_price,
                    pre_change, pre_change_pct, pre_entry_mfe, _iso_utc(received),
                ),
            )
            processing_status = "accepted_unmatched"
            processing_note = note or None
            if candidate:
                processing_status, processing_note = self._apply_pine_event(
                    connection, candidate, event_id, event_at, normalized
                )
            connection.execute(
                """UPDATE pine_events SET processing_status=?, processing_note=?
                   WHERE pine_event_id=?""",
                (processing_status, processing_note, event_id),
            )
        return {
            "status": "accepted",
            "event_id": source_event_id or event_id,
            "pine_event_id": event_id,
            "candidate_id": candidate_id,
            "match_method": method,
            "match_confidence": confidence,
            "match_note": note or None,
            "alternatives": alternatives,
            "seconds_from_candidate": seconds,
            "pre_entry_price_change": pre_change,
            "pre_entry_price_change_pct": pre_change_pct,
            "pre_entry_mfe_pct": pre_entry_mfe,
            "processing_status": processing_status,
            "processing_note": processing_note,
        }

    def _apply_pine_event(
        self,
        connection: sqlite3.Connection,
        candidate: sqlite3.Row,
        event_id: str,
        event_at: datetime,
        event: Mapping[str, Any],
    ) -> tuple[str, Optional[str]]:
        candidate_id = candidate["candidate_id"]
        event_type = event["event"]
        if event_type == "LOW_LIQUIDITY_SKIP":
            connection.execute(
                "UPDATE candidates SET status='PINE_LOW_LIQUIDITY_SKIP' WHERE candidate_id=?",
                (candidate_id,),
            )
            return "applied", None
        if event_type == "ENTRY":
            existing_trade = connection.execute(
                "SELECT entry_at_utc FROM trade_lifecycle WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if existing_trade:
                return "out_of_order", "ENTRY ignored because candidate already has a lifecycle"
            entry = event.get("assumed_entry") or event.get("signal_price")
            risk = event.get("risk_per_share")
            if risk is None and entry is not None and event.get("initial_stop") is not None:
                risk = abs(float(entry) - float(event["initial_stop"]))
            connection.execute(
                """INSERT INTO trade_lifecycle(
                    candidate_id, entry_event_id, entry_at_utc, signal_price,
                    assumed_entry, quantity, initial_stop, one_r_level, target,
                    risk_per_share, highest_price_since_entry, lowest_price_since_entry
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    entry_event_id=excluded.entry_event_id,
                    entry_at_utc=excluded.entry_at_utc,
                    signal_price=excluded.signal_price,
                    assumed_entry=excluded.assumed_entry,
                    quantity=excluded.quantity,
                    initial_stop=excluded.initial_stop,
                    one_r_level=excluded.one_r_level,
                    target=excluded.target,
                    risk_per_share=excluded.risk_per_share""",
                (
                    candidate_id, event_id, _iso_utc(event_at), event.get("signal_price"),
                    entry, event.get("qty"), event.get("initial_stop"), event.get("one_r_level"),
                    event.get("target"), risk, entry, entry,
                ),
            )
            connection.execute(
                "UPDATE candidates SET status='PINE_CONFIRMED', pine_confirmed_at_utc=? WHERE candidate_id=?",
                (_iso_utc(event_at), candidate_id),
            )
            return "applied", None
        trade = connection.execute(
            "SELECT * FROM trade_lifecycle WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if event_type == "STOP_UPDATE":
            if not trade:
                return "out_of_order", "STOP_UPDATE received before a matched ENTRY"
            updates = json.loads(trade["stop_updates_json"] or "[]")
            updates.append({
                "timestamp_utc": _iso_utc(event_at),
                "stop_price": event.get("stop_price"),
                "reason": event.get("reason"),
                "pine_event_id": event_id,
            })
            connection.execute(
                "UPDATE trade_lifecycle SET stop_updates_json=? WHERE candidate_id=?",
                (_json(updates), candidate_id),
            )
            return "applied", None
        if event_type == "EXIT":
            if not trade:
                return "out_of_order", "EXIT received before a matched ENTRY"
            if trade["status"] != "OPEN":
                return "out_of_order", "EXIT ignored because lifecycle is already closed"
            entry = _finite(trade["assumed_entry"] or trade["signal_price"])
            exit_price = event.get("exit_price")
            qty = _finite(trade["quantity"])
            risk = _finite(trade["risk_per_share"])
            pnl = (exit_price - entry) * qty if exit_price is not None and entry is not None and qty is not None else None
            result_r = (exit_price - entry) / risk if exit_price is not None and entry is not None and risk else None
            reason = str(event.get("reason") or "UNKNOWN").upper()
            connection.execute(
                """UPDATE trade_lifecycle SET exit_event_id=?, exit_at_utc=?, exit_reason=?,
                   theoretical_exit_price=?, theoretical_pnl=?, theoretical_r=?, status='CLOSED'
                   WHERE candidate_id=?""",
                (event_id, _iso_utc(event_at), reason, exit_price, pnl, result_r, candidate_id),
            )
            return "applied", None
        return "observed", None

    def inspect_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if not candidate:
                raise KeyError(candidate_id)
            def rows(query: str) -> list[dict[str, Any]]:
                return [dict(row) for row in connection.execute(query, (candidate_id,)).fetchall()]
            result = dict(candidate)
            result["snapshot"] = json.loads(result.pop("snapshot_json"))
            result["configuration"] = json.loads(result.pop("config_json"))
            result["price_outcome"] = rows(
                "SELECT * FROM candidate_price_outcomes WHERE candidate_id=?"
            )[0]
            result["horizons"] = rows(
                "SELECT * FROM candidate_price_horizons WHERE candidate_id=? ORDER BY horizon_minutes"
            )
            result["pine_links"] = rows(
                "SELECT * FROM candidate_pine_links WHERE candidate_id=? ORDER BY linked_at_utc"
            )
            trades = rows("SELECT * FROM trade_lifecycle WHERE candidate_id=?")
            result["trade"] = trades[0] if trades else None
            return result

    def daily_report(self, trading_date: str) -> dict[str, Any]:
        with self._connect() as connection:
            candidates = [dict(row) for row in connection.execute(
                "SELECT * FROM candidates WHERE trading_date=? ORDER BY score_total DESC, candidate_id",
                (trading_date,),
            ).fetchall()]
            ids = [row["candidate_id"] for row in candidates]
            if not ids:
                return {"trading_date": trading_date, "candidates_sent": 0}
            placeholders = ",".join("?" for _ in ids)
            outcomes = {row["candidate_id"]: dict(row) for row in connection.execute(
                f"SELECT * FROM candidate_price_outcomes WHERE candidate_id IN ({placeholders})", ids
            ).fetchall()}
            links = [dict(row) for row in connection.execute(
                f"""SELECT l.*, p.event_type, p.event_at_utc
                    FROM candidate_pine_links l JOIN pine_events p
                    ON p.pine_event_id=l.pine_event_id
                    WHERE l.candidate_id IN ({placeholders})""", ids
            ).fetchall()]
            trades = [dict(row) for row in connection.execute(
                f"SELECT * FROM trade_lifecycle WHERE candidate_id IN ({placeholders})", ids
            ).fetchall()]

        def median(values: Iterable[Any]) -> Optional[float]:
            numbers = [number for value in values if (number := _finite(value)) is not None]
            return statistics.median(numbers) if numbers else None

        lane_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        score_buckets: dict[str, int] = {}
        rank_counts: dict[str, int] = {}
        movement_counts: dict[str, int] = {}
        liquidity_counts: dict[str, int] = {}
        rvol_counts: dict[str, int] = {}
        acceleration_counts: dict[str, int] = {}
        rsi_counts: dict[str, int] = {}
        week52_counts: dict[str, int] = {}
        snapshots: dict[str, dict[str, Any]] = {}
        group_labels: dict[str, dict[str, str]] = {}

        def increment(target: dict[str, int], key: Any) -> None:
            label = str(key if key not in (None, "") else "UNKNOWN")
            target[label] = target.get(label, 0) + 1

        for row in candidates:
            snapshot = json.loads(row["snapshot_json"] or "{}")
            snapshots[row["candidate_id"]] = snapshot
            increment(lane_counts, row["lanes"])
            increment(status_counts, row["status"])
            score = _finite(row["score_total"])
            score_bucket = (
                "UNKNOWN" if score is None else "0-49" if score < 50 else
                "50-59" if score < 60 else "60-69" if score < 70 else
                "70-79" if score < 80 else "80+"
            )
            increment(score_buckets, score_bucket)
            increment(rank_counts, row["rank_this_cycle"])
            increment(movement_counts, row["movement_state"])
            increment(liquidity_counts, row["liquidity_status"])
            rvol = _finite(snapshot.get("VolumeRatio"))
            increment(rvol_counts, "UNKNOWN" if rvol is None else "<1.3" if rvol < 1.3 else "1.3-2.0" if rvol < 2 else "2.0+")
            acceleration = _finite(snapshot.get("MomentumAcceleration"))
            increment(acceleration_counts, "UNKNOWN" if acceleration is None else "FALLING" if acceleration < 0 else "FLAT" if acceleration == 0 else "RISING")
            rsi = _finite(snapshot.get("RSI"))
            increment(rsi_counts, "UNKNOWN" if rsi is None else "<60" if rsi < 60 else "60-69" if rsi < 70 else "70+")
            increment(week52_counts, snapshot.get("Week52Context"))
            group_labels[row["candidate_id"]] = {
                "lane": str(row["lanes"] or "UNKNOWN"),
                "score_bucket": score_bucket,
                "rank": str(row["rank_this_cycle"] or "UNKNOWN"),
                "movement_state": str(row["movement_state"] or "UNKNOWN"),
                "liquidity_status": str(row["liquidity_status"] or "UNKNOWN"),
                "rvol_bucket": "UNKNOWN" if rvol is None else "<1.3" if rvol < 1.3 else "1.3-2.0" if rvol < 2 else "2.0+",
                "acceleration_bucket": "UNKNOWN" if acceleration is None else "FALLING" if acceleration < 0 else "FLAT" if acceleration == 0 else "RISING",
                "rsi_bucket": "UNKNOWN" if rsi is None else "<60" if rsi < 60 else "60-69" if rsi < 70 else "70+",
                "week52_context": str(snapshot.get("Week52Context") or "UNKNOWN"),
            }
        entry_links = [
            row for row in links
            if row["event_type"] == "ENTRY" and row["seconds_from_candidate"] is not None
        ]
        closed = [row for row in trades if row["status"] == "CLOSED"]
        exit_reasons: dict[str, int] = {}
        for row in closed:
            reason = row["exit_reason"] or "UNKNOWN"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        entry_by_candidate = {row["candidate_id"]: row for row in entry_links}
        trade_by_candidate = {row["candidate_id"]: row for row in trades}

        def grouped_analytics(dimension: str) -> dict[str, dict[str, Any]]:
            grouped: dict[str, list[str]] = {}
            for candidate_id, labels in group_labels.items():
                grouped.setdefault(labels[dimension], []).append(candidate_id)
            result: dict[str, dict[str, Any]] = {}
            for label, candidate_ids in sorted(grouped.items()):
                confirmed = [candidate_id for candidate_id in candidate_ids if candidate_id in entry_by_candidate]
                result[label] = {
                    "candidates": len(candidate_ids),
                    "pine_confirmed": len(confirmed),
                    "pine_confirmation_rate_pct": len(confirmed) / len(candidate_ids) * 100.0,
                    "median_confirmation_delay_minutes": median(
                        entry_by_candidate[candidate_id]["seconds_from_candidate"] / 60.0
                        for candidate_id in confirmed
                    ),
                    "median_pre_entry_price_change_pct": median(
                        entry_by_candidate[candidate_id]["pre_entry_price_change_pct"]
                        for candidate_id in confirmed
                    ),
                    "median_mfe_pct": median(
                        outcomes[candidate_id]["mfe_pct"]
                        for candidate_id in candidate_ids if candidate_id in outcomes
                    ),
                    "median_mae_pct": median(
                        outcomes[candidate_id]["mae_pct"]
                        for candidate_id in candidate_ids if candidate_id in outcomes
                    ),
                    "average_theoretical_r": (
                        statistics.mean(values) if (values := [
                            trade_by_candidate[candidate_id]["theoretical_r"]
                            for candidate_id in candidate_ids
                            if candidate_id in trade_by_candidate
                            and trade_by_candidate[candidate_id]["theoretical_r"] is not None
                        ]) else None
                    ),
                }
            return result

        analytics_by = {
            dimension: grouped_analytics(dimension)
            for dimension in (
                "lane", "score_bucket", "rank", "movement_state", "liquidity_status",
                "rvol_bucket", "acceleration_bucket", "rsi_bucket", "week52_context",
            )
        }
        return {
            "trading_date": trading_date,
            "candidates_sent": sum(int(row["discord_sent"]) for row in candidates),
            "candidates_selected": len(candidates),
            "lane_distribution": lane_counts,
            "status_distribution": status_counts,
            "score_bucket_distribution": score_buckets,
            "rank_distribution": rank_counts,
            "movement_state_distribution": movement_counts,
            "liquidity_distribution": liquidity_counts,
            "rvol_distribution": rvol_counts,
            "acceleration_distribution": acceleration_counts,
            "rsi_distribution": rsi_counts,
            "week52_context_distribution": week52_counts,
            "analytics_by": analytics_by,
            "pine_confirmed_count": sum(row["status"] == "PINE_CONFIRMED" for row in candidates),
            "pine_confirmation_rate_pct": (
                sum(row["status"] == "PINE_CONFIRMED" for row in candidates) / len(candidates) * 100.0
            ),
            "median_confirmation_delay_minutes": median(
                (row["seconds_from_candidate"] / 60.0 for row in entry_links)
            ),
            "median_pre_entry_price_change_pct": median(
                row["pre_entry_price_change_pct"] for row in entry_links
            ),
            "confirmed_moved_over_1pct_before_entry": sum(
                abs(row["pre_entry_price_change_pct"] or 0.0) > 1.0 for row in entry_links
            ),
            "pine_entry_near_pre_entry_high_count": sum(
                row["pre_entry_mfe_pct"] is not None
                and row["pre_entry_price_change_pct"] is not None
                and abs(row["pre_entry_mfe_pct"] - row["pre_entry_price_change_pct"]) <= 0.25
                for row in entry_links
            ),
            "candidate_mfe_occurred_before_entry_count": sum(
                row["candidate_id"] in outcomes
                and outcomes[row["candidate_id"]].get("highest_at_utc") is not None
                and outcomes[row["candidate_id"]]["highest_at_utc"] <= row["event_at_utc"]
                for row in entry_links
            ),
            "median_candidate_mfe_pct": median(
                outcomes[candidate_id]["mfe_pct"] for candidate_id in ids if candidate_id in outcomes
            ),
            "median_candidate_mae_pct": median(
                outcomes[candidate_id]["mae_pct"] for candidate_id in ids if candidate_id in outcomes
            ),
            "closed_trade_exit_reasons": exit_reasons,
            "average_theoretical_r": (
                sum(row["theoretical_r"] for row in closed if row["theoretical_r"] is not None)
                / len([row for row in closed if row["theoretical_r"] is not None])
                if any(row["theoretical_r"] is not None for row in closed) else None
            ),
            "median_theoretical_r": median(row["theoretical_r"] for row in closed),
            "top_candidates": [
                {"candidate_id": row["candidate_id"], "symbol": row["symbol"],
                 "score_total": row["score_total"], "rank": row["rank_this_cycle"]}
                for row in candidates[:10]
            ],
            "cases_to_inspect": {
                "large_pre_entry_move": [
                    row["candidate_id"] for row in entry_links
                    if abs(row["pre_entry_price_change_pct"] or 0.0) > 1.0
                ],
                "high_score_immediate_fade": [
                    row["candidate_id"] for row in candidates
                    if (_finite(row["score_total"]) or 0) >= 70
                    and row["candidate_id"] in outcomes
                    and (_finite(outcomes[row["candidate_id"]].get("mae_pct")) or 0) <= -1.0
                ],
                "low_score_strong_continuation": [
                    row["candidate_id"] for row in candidates
                    if _finite(row["score_total"]) is not None
                    and float(row["score_total"]) < 50
                    and row["candidate_id"] in outcomes
                    and (_finite(outcomes[row["candidate_id"]].get("mfe_pct")) or 0) >= 2.0
                ],
                "python_pass_pine_liquidity_skip": [
                    row["candidate_id"] for row in candidates
                    if row["liquidity_status"] == "PASS"
                    and row["status"] == "PINE_LOW_LIQUIDITY_SKIP"
                ],
                "dual_lane": [
                    row["candidate_id"] for row in candidates if "," in row["lanes"]
                ],
            },
        }

    def prune(self, today: Optional[date] = None) -> int:
        cutoff = (today or datetime.now(IST).date()) - timedelta(days=self.retention_days)
        with self._connect() as connection:
            return connection.execute(
                "DELETE FROM candidates WHERE trading_date < ?", (cutoff.isoformat(),)
            ).rowcount

    def write_daily_report(self, trading_date: str) -> tuple[Path, Path]:
        report = self.daily_report(trading_date)
        report_dir = self.root / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        json_path = report_dir / f"candidate_lifecycle_{trading_date}.json"
        text_path = report_dir / f"candidate_lifecycle_{trading_date}.txt"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        text_path.write_text(format_daily_report(report) + "\n", encoding="utf-8")
        return json_path, text_path


def format_daily_report(report: Mapping[str, Any]) -> str:
    def metric(value: Any, suffix: str = "") -> str:
        number = _finite(value)
        return "UNKNOWN" if number is None else f"{number:.2f}{suffix}"

    lines = [
        f"CANDIDATE LIFECYCLE — {report.get('trading_date', '')}",
        "Descriptive research only; not trading advice or an execution record.",
        f"Candidates selected: {report.get('candidates_selected', 0)}",
        f"Discord sent: {report.get('candidates_sent', 0)}",
        f"Pine confirmed: {report.get('pine_confirmed_count', 0)} "
        f"({metric(report.get('pine_confirmation_rate_pct'), '%')})",
        f"Median confirmation delay: {metric(report.get('median_confirmation_delay_minutes'), ' min')}",
        f"Median pre-entry price change: {metric(report.get('median_pre_entry_price_change_pct'), '%')}",
        f"Median candidate MFE: {metric(report.get('median_candidate_mfe_pct'), '%')}",
        f"Median candidate MAE: {metric(report.get('median_candidate_mae_pct'), '%')}",
        f"Average theoretical R: {metric(report.get('average_theoretical_r'))}",
        f"Median theoretical R: {metric(report.get('median_theoretical_r'))}",
        "Lane distribution: " + _json(report.get("lane_distribution", {})),
        "Status distribution: " + _json(report.get("status_distribution", {})),
        "Score buckets: " + _json(report.get("score_bucket_distribution", {})),
        "Rank distribution: " + _json(report.get("rank_distribution", {})),
        "Movement states: " + _json(report.get("movement_state_distribution", {})),
        "Liquidity: " + _json(report.get("liquidity_distribution", {})),
        "Exit reasons: " + _json(report.get("closed_trade_exit_reasons", {})),
        "Top candidates:",
    ]
    for row in report.get("top_candidates", []):
        lines.append(
            f"- {row.get('candidate_id')} {row.get('symbol')} | score {row.get('score_total')} | rank {row.get('rank')}"
        )
    return "\n".join(lines)


__all__ = [
    "CandidateLifecycleTracker", "PINE_EVENTS", "SCHEMA_VERSION",
    "format_daily_report", "normalize_symbol",
]

"""Read-only Emerging Leader evidence subscriber with a durable Discord outbox."""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pkscreener.classes.EmergingLeaderShadowPlan import (
    ShadowPlan,
    ShadowPlanInputs,
    calculate_shadow_plan,
    format_shadow_plan,
    unavailable_shadow_plan,
)
from pkscreener.classes.EmergingLeaderDiscordActionability import (
    DiscordActionability,
    DiscordActionabilityThresholds,
    evaluate_discord_actionability,
    format_discord_actionability,
)


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STAGES = {
    300: ("TOP300", "🚀 EMERGING LEADER"),
    200: ("TOP200", "⚡ ACCELERATING LEADER"),
    125: ("TOP125", "🔥 STRONG EMERGENCE"),
    100: ("TOP100_GRADUATED", "🎓 EMERGING LEADER GRADUATED"),
}
STAGE_ORDER = {name: order for order, (name, _title) in enumerate(STAGES.values(), 1)}


class RadarUnavailable(RuntimeError):
    """The current session's frozen radar database cannot yet be read."""


@dataclass(frozen=True)
class ProcessResult:
    initialized: bool = False
    ingested: int = 0
    queued: int = 0
    delivered: int = 0
    pending: int = 0


def open_radar_read_only(path: Path) -> sqlite3.Connection:
    """Open a radar database without creating it or permitting writes."""
    path = Path(path).resolve(strict=True)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise RadarUnavailable("SQLite query_only mode could not be enabled")
    return connection


class EmergingLeaderAlertPublisher:
    """Consume frozen milestone evidence without modifying the source database."""

    def __init__(
        self,
        radar_root: Path,
        state_db: Path,
        transport=None,
        clock: Callable[[], dt.datetime] | None = None,
        retry_base_seconds: float = 5.0,
        shadow_plan_provider: Callable[[sqlite3.Row], ShadowPlanInputs] | None = None,
        actionability_thresholds: DiscordActionabilityThresholds | None = None,
    ) -> None:
        self.radar_root = Path(radar_root)
        self.state_db = Path(state_db)
        self.transport = transport
        self.clock = clock or (lambda: dt.datetime.now(IST))
        self.retry_base_seconds = max(0.1, float(retry_base_seconds))
        self.shadow_plan_provider = shadow_plan_provider or self._unavailable_live_inputs
        self.actionability_thresholds = (
            actionability_thresholds or DiscordActionabilityThresholds.from_environment()
        )
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_state()

    def _state(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_db, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_state(self) -> None:
        with self._state() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_state(
                    session_date TEXT PRIMARY KEY,
                    source_cursor_epoch_ms INTEGER NOT NULL,
                    initialized_epoch_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_events(
                    event_id INTEGER PRIMARY KEY,
                    session_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_epoch_ms INTEGER NOT NULL,
                    milestone INTEGER NOT NULL,
                    current_rank INTEGER NOT NULL,
                    pct_change REAL,
                    coverage_pct REAL,
                    last_price REAL,
                    cumulative_volume REAL,
                    approx_turnover REAL,
                    rank_5m_ago REAL,
                    rank_improvement_5m REAL,
                    rank_velocity_5m REAL,
                    rank_acceleration REAL,
                    rs_vs_nifty REAL,
                    rs_change_5m REAL,
                    turnover_acceleration REAL,
                    volume_rate_5m REAL,
                    distance_from_day_high_pct REAL,
                    price_vs_vwap_pct REAL,
                    applied INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(session_date, symbol, event_epoch_ms, milestone)
                );
                CREATE TABLE IF NOT EXISTS outbox(
                    outbox_id INTEGER PRIMARY KEY,
                    session_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    event_epoch_ms INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    delivery_state TEXT NOT NULL DEFAULT 'pending',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_epoch_ms INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    discord_message_id TEXT,
                    delivered_epoch_ms INTEGER,
                    UNIQUE(session_date, symbol, stage)
                );
                CREATE INDEX IF NOT EXISTS idx_source_events_unapplied
                    ON source_events(session_date, applied, event_epoch_ms);
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON outbox(delivery_state, next_attempt_epoch_ms, event_epoch_ms);
                CREATE TABLE IF NOT EXISTS shadow_plan_audit(
                    outbox_id INTEGER PRIMARY KEY,
                    session_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    candidate_detected_at TEXT NOT NULL,
                    reference_entry REAL,
                    price_observed_at TEXT,
                    shadow_plan_status TEXT NOT NULL,
                    missing_reason TEXT,
                    stop_input_mode TEXT,
                    atr_input_at TEXT,
                    atr14 REAL,
                    signal_low_at TEXT,
                    signal_low REAL,
                    initial_stop REAL,
                    risk_per_share REAL,
                    risk_qty INTEGER,
                    exposure_qty INTEGER,
                    final_qty INTEGER,
                    target_1r REAL,
                    target_2r REAL,
                    planned_rupee_risk REAL,
                    position_exposure REAL,
                    FOREIGN KEY(outbox_id) REFERENCES outbox(outbox_id)
                );
                CREATE TABLE IF NOT EXISTS discord_actionability_audit(
                    source_event_id INTEGER PRIMARY KEY,
                    session_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    candidate_detected_epoch_ms INTEGER NOT NULL,
                    discord_actionability_status TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    cumulative_volume REAL,
                    approx_turnover REAL,
                    volume_rate_5m REAL,
                    min_cumulative_volume REAL NOT NULL,
                    min_turnover REAL NOT NULL,
                    recent_5m_participation_capacity_5pct INTEGER NOT NULL,
                    FOREIGN KEY(source_event_id) REFERENCES source_events(event_id)
                );
                """
            )

    def radar_db_path(self, session_date: str) -> Path:
        return self.radar_root / session_date / "emerging_leader.sqlite"

    def _session_id(self, radar: sqlite3.Connection, session_date: str) -> int:
        row = radar.execute(
            "SELECT session_id FROM session_metadata WHERE session_date=?", (session_date,)
        ).fetchone()
        if row is None:
            raise RadarUnavailable(f"radar session {session_date} is not initialized")
        return int(row[0])

    def initialize_session(self, session_date: str) -> bool:
        """Set the first-deployment cursor to the current source maximum."""
        with self._state() as state:
            if state.execute(
                "SELECT 1 FROM session_state WHERE session_date=?", (session_date,)
            ).fetchone():
                return False
        try:
            with open_radar_read_only(self.radar_db_path(session_date)) as radar:
                session_id = self._session_id(radar, session_date)
                cursor = int(
                    radar.execute(
                        "SELECT COALESCE(MAX(timestamp_epoch_ms), 0) FROM rank_events "
                        "WHERE session_id=?",
                        (session_id,),
                    ).fetchone()[0]
                )
        except (FileNotFoundError, sqlite3.Error) as exc:
            raise RadarUnavailable(str(exc)) from exc
        initialized_ms = int(self.clock().timestamp() * 1000)
        with self._state() as state:
            inserted = state.execute(
                "INSERT OR IGNORE INTO session_state VALUES(?,?,?)",
                (session_date, cursor, initialized_ms),
            ).rowcount
        return bool(inserted)

    @staticmethod
    def _event_query() -> str:
        return """
            SELECT sy.symbol, r.timestamp_epoch_ms, r.milestone, r.broad_rank,
                   r.pct_change, r.coverage_pct, r.last_price,
                   r.cumulative_volume, r.approx_turnover,
                   f.rank_5m_ago, f.rank_improvement_5m, f.rank_velocity_5m,
                   f.rank_acceleration, f.rs_vs_nifty, f.rs_change_5m,
                   f.turnover_acceleration, f.volume_rate_5m,
                   f.distance_from_day_high_pct, f.price_vs_vwap_pct
              FROM rank_events r
              JOIN symbols sy ON sy.symbol_id=r.symbol_id
              LEFT JOIN snapshot_times st ON st.snapshot_id=(
                    SELECT st2.snapshot_id FROM snapshot_times st2
                     WHERE st2.session_id=r.session_id
                       AND st2.timestamp_epoch_ms<=r.timestamp_epoch_ms
                     ORDER BY st2.timestamp_epoch_ms DESC LIMIT 1
              )
              LEFT JOIN features f ON f.snapshot_id=st.snapshot_id
                                  AND f.symbol_id=r.symbol_id
             WHERE r.session_id=? AND r.timestamp_epoch_ms>?
               AND r.milestone IN (300,200,125,100)
             ORDER BY r.timestamp_epoch_ms, sy.symbol, r.milestone DESC
        """

    def ingest_new_events(self, session_date: str) -> int:
        with self._state() as state:
            row = state.execute(
                "SELECT source_cursor_epoch_ms FROM session_state WHERE session_date=?",
                (session_date,),
            ).fetchone()
        if row is None:
            raise RadarUnavailable("publisher session is not initialized")
        cursor = int(row[0])
        try:
            with open_radar_read_only(self.radar_db_path(session_date)) as radar:
                session_id = self._session_id(radar, session_date)
                events = radar.execute(self._event_query(), (session_id, cursor)).fetchall()
        except (FileNotFoundError, sqlite3.Error) as exc:
            raise RadarUnavailable(str(exc)) from exc
        if not events:
            return 0
        columns = (
            "session_date,symbol,event_epoch_ms,milestone,current_rank,pct_change,"
            "coverage_pct,last_price,cumulative_volume,approx_turnover,rank_5m_ago,"
            "rank_improvement_5m,rank_velocity_5m,rank_acceleration,rs_vs_nifty,"
            "rs_change_5m,turnover_acceleration,volume_rate_5m,"
            "distance_from_day_high_pct,price_vs_vwap_pct"
        )
        inserted = 0
        with self._state() as state:
            for event in events:
                values = (session_date,) + tuple(event)
                inserted += state.execute(
                    f"INSERT OR IGNORE INTO source_events({columns}) VALUES({','.join('?' * len(values))})",
                    values,
                ).rowcount
            state.execute(
                "UPDATE session_state SET source_cursor_epoch_ms=? WHERE session_date=?",
                (max(int(event[1]) for event in events), session_date),
            )
        return inserted

    @staticmethod
    def _number(value, digits: int = 2, suffix: str = "") -> str:
        if value is None:
            return "unavailable"
        return f"{float(value):+.{digits}f}{suffix}"

    @staticmethod
    def _unavailable_live_inputs(event: sqlite3.Row) -> ShadowPlanInputs:
        return ShadowPlanInputs(
            candidate_detected_at=dt.datetime.fromtimestamp(
                int(event["event_epoch_ms"]) / 1000, tz=IST
            ),
            reference_entry=event["last_price"],
        )

    def _shadow_plan(self, event: sqlite3.Row) -> ShadowPlan:
        try:
            return calculate_shadow_plan(self.shadow_plan_provider(event))
        except Exception as exc:
            inputs = self._unavailable_live_inputs(event)
            return unavailable_shadow_plan(
                inputs,
                f"shadow plan formatter error: {type(exc).__name__}",
                status="PLAN_ERROR",
            )

    @classmethod
    def format_message(
        cls,
        event: sqlite3.Row,
        plan: ShadowPlan | None = None,
        actionability: DiscordActionability | None = None,
    ) -> str:
        stage, title = STAGES[int(event["milestone"])]
        timestamp = dt.datetime.fromtimestamp(
            int(event["event_epoch_ms"]) / 1000, tz=IST
        ).strftime("%H:%M:%S IST")
        lines = [
            f"{title} — {event['symbol']}",
            "",
            f"Rank: >{event['milestone']} → {event['current_rank']}",
            f"Stage: {stage}",
            f"Move today: {cls._number(event['pct_change'], 2, '%')}",
        ]
        optional = [
            ("Rank 5m ago", event["rank_5m_ago"], lambda v: f"{int(v)}"),
            ("5m rank improvement", event["rank_improvement_5m"], lambda v: f"{v:+.0f}"),
            ("5m rank velocity", event["rank_velocity_5m"], lambda v: f"{v:+.1f} ranks/min"),
            ("Rank acceleration", event["rank_acceleration"], lambda v: f"{v:+.2f}"),
            ("Price", event["last_price"], lambda v: f"₹{v:,.2f}"),
            ("RS vs NIFTY", event["rs_vs_nifty"], lambda v: f"{v:+.2f}%"),
            ("RS change 5m", event["rs_change_5m"], lambda v: f"{v:+.2f}%"),
            ("Turnover acceleration", event["turnover_acceleration"], lambda v: f"₹{v / 10_000_000:+.2f} Cr"),
            ("From day high", event["distance_from_day_high_pct"], lambda v: f"{v:+.2f}%"),
            ("Vs causal VWAP", event["price_vs_vwap_pct"], lambda v: f"{v:+.2f}%"),
        ]
        if actionability is None:
            optional.extend(
                [
                    ("Turnover", event["approx_turnover"], lambda v: f"₹{v / 10_000_000:.2f} Cr"),
                    ("Volume", event["cumulative_volume"], lambda v: f"{v:,.0f}"),
                    ("Volume rate 5m", event["volume_rate_5m"], lambda v: f"{v:+,.0f}/min"),
                ]
            )
        lines.extend(f"{label}: {render(float(value))}" for label, value, render in optional if value is not None)
        lines.extend(
            [
                "",
                timestamp,
                f"Market coverage: {cls._number(event['coverage_pct'], 1, '%')}",
            ]
        )
        if actionability is not None:
            lines.extend(["", format_discord_actionability(actionability)])
        if plan is not None:
            lines.extend(["", format_shadow_plan(plan)])
        lines.extend(["", "Research watch only — not a buy signal"])
        return "\n".join(lines)

    def _actionability(self, event: sqlite3.Row) -> DiscordActionability:
        return evaluate_discord_actionability(
            event["cumulative_volume"],
            event["approx_turnover"],
            event["volume_rate_5m"],
            self.actionability_thresholds,
        )

    @staticmethod
    def _persist_actionability_audit(
        state: sqlite3.Connection,
        event: sqlite3.Row,
        stage: str,
        actionability: DiscordActionability,
    ) -> None:
        values = actionability.audit_values()
        state.execute(
            "INSERT OR IGNORE INTO discord_actionability_audit("
            "source_event_id,session_date,symbol,stage,candidate_detected_epoch_ms,"
            "discord_actionability_status,reasons,cumulative_volume,approx_turnover,"
            "volume_rate_5m,min_cumulative_volume,min_turnover,"
            "recent_5m_participation_capacity_5pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["event_id"], event["session_date"], event["symbol"], stage,
                event["event_epoch_ms"], values["discord_actionability_status"],
                values["reasons"], values["cumulative_volume"], values["approx_turnover"],
                values["volume_rate_5m"], values["min_cumulative_volume"],
                values["min_turnover"], values["recent_5m_participation_capacity_5pct"],
            ),
        )

    @staticmethod
    def _persist_plan_audit(
        state: sqlite3.Connection,
        outbox_id: int,
        session_date: str,
        symbol: str,
        stage: str,
        plan: ShadowPlan,
    ) -> None:
        values = plan.audit_values()
        columns = [
            "candidate_detected_at", "reference_entry", "price_observed_at",
            "shadow_plan_status", "missing_reason", "stop_input_mode",
            "atr_input_at", "atr14", "signal_low_at", "signal_low",
            "initial_stop", "risk_per_share", "risk_qty", "exposure_qty",
            "final_qty", "target_1r", "target_2r", "planned_rupee_risk",
            "position_exposure",
        ]
        state.execute(
            "INSERT OR IGNORE INTO shadow_plan_audit(outbox_id,session_date,symbol,stage,"
            + ",".join(columns) + ") VALUES(" + ",".join("?" * (4 + len(columns))) + ")",
            (outbox_id, session_date, symbol, stage, *(values[column] for column in columns)),
        )

    @staticmethod
    def _has_top300(state: sqlite3.Connection, session_date: str, symbol: str, delivered=False) -> bool:
        clause = " AND delivery_state='delivered'" if delivered else ""
        return state.execute(
            "SELECT 1 FROM outbox WHERE session_date=? AND symbol=? AND stage='TOP300'" + clause,
            (session_date, symbol),
        ).fetchone() is not None

    @staticmethod
    def _has_suppressed_top300(state: sqlite3.Connection, session_date: str, symbol: str) -> bool:
        return state.execute(
            "SELECT 1 FROM discord_actionability_audit WHERE session_date=? AND symbol=? "
            "AND stage='TOP300' AND discord_actionability_status='SUPPRESSED_LOW_LIQUIDITY'",
            (session_date, symbol),
        ).fetchone() is not None

    @staticmethod
    def _has_delivered_emerging(state: sqlite3.Connection, session_date: str, symbol: str) -> bool:
        return state.execute(
            "SELECT 1 FROM outbox WHERE session_date=? AND symbol=? "
            "AND stage IN ('TOP300','TOP200','TOP125') AND delivery_state='delivered'",
            (session_date, symbol),
        ).fetchone() is not None

    @staticmethod
    def _has_pending_emerging(state: sqlite3.Connection, session_date: str, symbol: str) -> bool:
        return state.execute(
            "SELECT 1 FROM outbox WHERE session_date=? AND symbol=? "
            "AND stage IN ('TOP300','TOP200','TOP125') AND delivery_state='pending'",
            (session_date, symbol),
        ).fetchone() is not None

    def queue_eligible(self, session_date: str) -> int:
        queued = 0
        with self._state() as state:
            events = state.execute(
                "SELECT * FROM source_events WHERE session_date=? AND applied=0 "
                "ORDER BY event_epoch_ms, symbol, milestone DESC",
                (session_date,),
            ).fetchall()
            for event in events:
                milestone = int(event["milestone"])
                current_rank = int(event["current_rank"])
                should_queue = False
                wait_for_emerging = False
                if milestone == 300:
                    should_queue = current_rank > 100
                elif milestone in (200, 125):
                    if self._has_pending_emerging(state, session_date, event["symbol"]):
                        wait_for_emerging = True
                    elif (
                        self._has_top300(state, session_date, event["symbol"], delivered=True)
                        or self._has_suppressed_top300(state, session_date, event["symbol"])
                    ):
                        should_queue = current_rank > 100
                elif milestone == 100:
                    if self._has_pending_emerging(state, session_date, event["symbol"]):
                        wait_for_emerging = True
                    else:
                        should_queue = self._has_delivered_emerging(
                            state, session_date, event["symbol"]
                        )
                if wait_for_emerging:
                    continue
                if should_queue:
                    stage = STAGES[milestone][0]
                    if state.execute(
                        "SELECT 1 FROM outbox WHERE session_date=? AND symbol=? AND stage=?",
                        (session_date, event["symbol"], stage),
                    ).fetchone():
                        state.execute("UPDATE source_events SET applied=1 WHERE event_id=?", (event["event_id"],))
                        continue
                    actionability = self._actionability(event)
                    self._persist_actionability_audit(state, event, stage, actionability)
                    if actionability.discord_actionability_status != "PASS":
                        state.execute(
                            "UPDATE source_events SET applied=1 WHERE event_id=?", (event["event_id"],)
                        )
                        continue
                    plan = self._shadow_plan(event)
                    inserted = state.execute(
                        "INSERT OR IGNORE INTO outbox(session_date,symbol,stage,event_epoch_ms,message) "
                        "VALUES(?,?,?,?,?)",
                        (
                            session_date,
                            event["symbol"],
                            stage,
                            event["event_epoch_ms"],
                            self.format_message(event, plan, actionability),
                        ),
                    )
                    queued += inserted.rowcount
                    if inserted.rowcount:
                        self._persist_plan_audit(
                            state, inserted.lastrowid, session_date, event["symbol"], stage, plan
                        )
                state.execute("UPDATE source_events SET applied=1 WHERE event_id=?", (event["event_id"],))
        return queued

    def deliver_pending(self) -> int:
        if self.transport is None:
            return 0
        now_ms = int(self.clock().timestamp() * 1000)
        delivered = 0
        with self._state() as state:
            pending = state.execute(
                "SELECT * FROM outbox WHERE delivery_state='pending' AND next_attempt_epoch_ms<=? "
                "ORDER BY event_epoch_ms, CASE stage WHEN 'TOP300' THEN 1 WHEN 'TOP200' THEN 2 "
                "WHEN 'TOP125' THEN 3 ELSE 4 END",
                (now_ms,),
            ).fetchall()
        for item in pending:
            try:
                result = self.transport.post(item["message"])
            except Exception as exc:
                result = None
                error = f"transport_exception:{type(exc).__name__}"
            else:
                error = result.error or (f"http_{result.http_status}" if not result.delivered else None)
            with self._state() as state:
                if result is not None and result.delivered:
                    delivered += state.execute(
                        "UPDATE outbox SET delivery_state='delivered',delivery_attempts=delivery_attempts+1,"
                        "last_error=NULL,discord_message_id=?,delivered_epoch_ms=? "
                        "WHERE outbox_id=? AND delivery_state='pending'",
                        (result.message_id, now_ms, item["outbox_id"]),
                    ).rowcount
                else:
                    attempts = int(item["delivery_attempts"]) + 1
                    retry_after = getattr(result, "retry_after_seconds", None) if result else None
                    delay = float(retry_after) if retry_after is not None else min(
                        300.0, self.retry_base_seconds * (2 ** min(attempts - 1, 6))
                    )
                    state.execute(
                        "UPDATE outbox SET delivery_attempts=?,next_attempt_epoch_ms=?,last_error=? "
                        "WHERE outbox_id=? AND delivery_state='pending'",
                        (attempts, now_ms + int(delay * 1000), error, item["outbox_id"]),
                    )
        return delivered

    def pending_count(self) -> int:
        with self._state() as state:
            return int(
                state.execute("SELECT count(*) FROM outbox WHERE delivery_state='pending'").fetchone()[0]
            )

    def process_once(self, session_date: str) -> ProcessResult:
        if self.initialize_session(session_date):
            return ProcessResult(initialized=True, pending=self.pending_count())
        delivered = self.deliver_pending()
        ingested = self.ingest_new_events(session_date)
        queued = self.queue_eligible(session_date)
        delivered += self.deliver_pending()
        queued += self.queue_eligible(session_date)
        delivered += self.deliver_pending()
        return ProcessResult(
            ingested=ingested,
            queued=queued,
            delivered=delivered,
            pending=self.pending_count(),
        )


__all__ = [
    "EmergingLeaderAlertPublisher",
    "IST",
    "ProcessResult",
    "RadarUnavailable",
    "STAGES",
    "open_radar_read_only",
]

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path

import pytest

from pkscreener.classes.EmergingLeaderAlertPublisher import (
    EmergingLeaderAlertPublisher,
    IST,
    open_radar_read_only,
)
from pkscreener.classes.NGP5EarlyWatchPublisher import DeliveryResult


class Clock:
    def __init__(self, epoch_ms: int = 1_800_000_000_000):
        self.epoch_ms = epoch_ms

    def __call__(self):
        return dt.datetime.fromtimestamp(self.epoch_ms / 1000, tz=IST)


class Transport:
    def __init__(self, *results):
        self.results = list(results)
        self.messages = []

    def post(self, message):
        self.messages.append(message)
        result = self.results.pop(0) if self.results else DeliveryResult(True, 200, False)
        if isinstance(result, BaseException):
            raise result
        return result


def make_radar(tmp_path: Path, session_date="2027-01-15"):
    root = tmp_path / "radar"
    path = root / session_date / "emerging_leader.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session_metadata(session_id INTEGER PRIMARY KEY, session_date TEXT);
            CREATE TABLE symbols(symbol_id INTEGER PRIMARY KEY, symbol TEXT UNIQUE);
            CREATE TABLE rank_events(
                session_id INTEGER, symbol_id INTEGER, timestamp_epoch_ms INTEGER,
                milestone INTEGER, broad_rank INTEGER, pct_change REAL,
                rank_universe_size INTEGER, expected_universe_size INTEGER,
                coverage_pct REAL, last_price REAL, cumulative_volume REAL,
                approx_turnover REAL,
                PRIMARY KEY(session_id,symbol_id,milestone)
            );
            CREATE TABLE snapshot_times(
                snapshot_id INTEGER PRIMARY KEY, session_id INTEGER,
                timestamp_epoch_ms INTEGER
            );
            CREATE TABLE features(
                snapshot_id INTEGER, symbol_id INTEGER, rank_5m_ago REAL,
                rank_improvement_5m REAL, rank_velocity_5m REAL,
                rank_acceleration REAL, rs_vs_nifty REAL, rs_change_5m REAL,
                turnover_acceleration REAL, volume_rate_5m REAL,
                distance_from_day_high_pct REAL, price_vs_vwap_pct REAL
            );
            INSERT INTO session_metadata VALUES(1, '2027-01-15');
            """
        )
    return root, path


def add_event(path: Path, symbol: str, timestamp: int, milestone: int, rank: int, features=True):
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT symbol_id FROM symbols WHERE symbol=?", (symbol,)).fetchone()
        if row:
            symbol_id = row[0]
        else:
            symbol_id = connection.execute(
                "INSERT INTO symbols(symbol) VALUES(?) RETURNING symbol_id", (symbol,)
            ).fetchone()[0]
        connection.execute(
            "INSERT OR REPLACE INTO rank_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, symbol_id, timestamp, milestone, rank, 2.41, 2180, 2337, 93.28, 125.5, 800000, 100000000),
        )
        if features:
            snapshot_id = connection.execute(
                "INSERT INTO snapshot_times(session_id,timestamp_epoch_ms) VALUES(?,?) RETURNING snapshot_id",
                (1, timestamp),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO features VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, symbol_id, 428, 152, 30.4, 2.2, 1.83, 0.4, 12000000, 40000, -0.35, 0.62),
            )


def publisher(tmp_path, transport=None, clock=None):
    root, radar = make_radar(tmp_path)
    value = EmergingLeaderAlertPublisher(
        root, tmp_path / "state" / "publisher.sqlite", transport, clock or Clock(), retry_base_seconds=0.1
    )
    return value, radar


def outbox_rows(value):
    with sqlite3.connect(value.state_db) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM outbox ORDER BY outbox_id").fetchall()


def test_radar_connection_is_read_only_and_source_is_unchanged(tmp_path):
    value, radar = publisher(tmp_path)
    before = hashlib.sha256(radar.read_bytes()).hexdigest()
    with open_radar_read_only(radar) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM rank_events")
    value.initialize_session("2027-01-15")
    assert hashlib.sha256(radar.read_bytes()).hexdigest() == before


def test_startup_cursor_prevents_historical_flood(tmp_path):
    value, radar = publisher(tmp_path, Transport())
    add_event(radar, "OLD", 1000, 300, 276)
    result = value.process_once("2027-01-15")
    assert result.initialized is True
    assert outbox_rows(value) == []
    assert value.process_once("2027-01-15").ingested == 0


def test_top300_delivery_and_duplicate_dedupe_survive_restart(tmp_path):
    transport = Transport(DeliveryResult(True, 200, False, message_id="one"))
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 2000, 300, 276)
    result = value.process_once("2027-01-15")
    assert result.delivered == 1
    assert len(transport.messages) == 1
    row = outbox_rows(value)[0]
    assert (row["stage"], row["delivery_state"], row["discord_message_id"]) == ("TOP300", "delivered", "one")
    restarted = EmergingLeaderAlertPublisher(value.radar_root, value.state_db, transport, Clock())
    assert restarted.process_once("2027-01-15").delivered == 0
    assert len(transport.messages) == 1


def test_progression_stages_require_delivered_emerging_and_graduate(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 2000, 300, 276)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 3000, 200, 188)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 4000, 125, 119)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 5000, 100, 96)
    value.process_once("2027-01-15")
    assert [row["stage"] for row in outbox_rows(value)] == [
        "TOP300", "TOP200", "TOP125", "TOP100_GRADUATED"
    ]
    assert len(transport.messages) == 4


@pytest.mark.parametrize("milestone,rank", [(200, 188), (125, 119), (100, 96)])
def test_generic_symbol_without_emerging_does_not_alert(tmp_path, milestone, rank):
    value, radar = publisher(tmp_path, Transport())
    value.process_once("2027-01-15")
    add_event(radar, "GENERIC", 2000, milestone, rank)
    value.process_once("2027-01-15")
    assert outbox_rows(value) == []


def test_top300_must_remain_outside_top100(tmp_path):
    value, radar = publisher(tmp_path, Transport())
    value.process_once("2027-01-15")
    add_event(radar, "JUMP", 2000, 300, 90)
    value.process_once("2027-01-15")
    assert outbox_rows(value) == []


def test_failed_delivery_stays_pending_and_retries(tmp_path):
    clock = Clock()
    transport = Transport(
        DeliveryResult(False, 503, True, error="http_503"),
        DeliveryResult(True, 200, False, message_id="retry-ok"),
    )
    value, radar = publisher(tmp_path, transport, clock)
    value.process_once("2027-01-15")
    add_event(radar, "RETRY", 2000, 300, 250)
    first = value.process_once("2027-01-15")
    assert first.pending == 1
    assert outbox_rows(value)[0]["delivery_attempts"] == 1
    clock.epoch_ms += 1000
    second = value.process_once("2027-01-15")
    assert second.delivered == 1 and second.pending == 0
    assert outbox_rows(value)[0]["discord_message_id"] == "retry-ok"


def test_missing_feature_enrichment_is_omitted(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(radar, "SPARSE", 2000, 300, 250, features=False)
    value.process_once("2027-01-15")
    message = transport.messages[0]
    assert "EMERGING LEADER — SPARSE" in message
    assert "Rank 5m ago" not in message
    assert "Research watch only — not a buy signal" in message


def test_transport_exception_is_isolated_and_pending(tmp_path):
    value, radar = publisher(tmp_path, Transport(RuntimeError("discord down")))
    value.process_once("2027-01-15")
    add_event(radar, "SAFE", 2000, 300, 250)
    result = value.process_once("2027-01-15")
    assert result.pending == 1
    row = outbox_rows(value)[0]
    assert row["delivery_state"] == "pending"
    assert row["last_error"] == "transport_exception:RuntimeError"


def test_systemd_flags_keep_legacy_alerts_disabled():
    unit = (Path(__file__).parents[1] / "deploy/systemd/ng-emerging-leader-alerts.service").read_text()
    assert "Environment=NG_EMERGING_LEADER_DISCORD_ENABLED=1" in unit
    assert "Environment=NG_TOP100_WATCH_DISCORD_ENABLED=0" in unit
    assert "Environment=NG_NIFTY50_WATCH_DISCORD_ENABLED=0" in unit

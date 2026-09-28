import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pkscreener.classes.CandidateLifecycleTracker import CandidateLifecycleTracker
from pkscreener.classes.PineWebhookReceiver import (
    PineWebhookApplication,
    PineWebhookConfig,
    SlidingWindowRateLimiter,
)


UTC = timezone.utc
RECEIVED = datetime(2026, 9, 27, 4, 6, tzinfo=UTC)
SECRET = "test-secret-at-least-sixteen-characters"


def config(**overrides):
    values = {"secret": SECRET}
    values.update(overrides)
    return PineWebhookConfig(**values)


def app(tmp_path, **overrides):
    tracker = CandidateLifecycleTracker(str(tmp_path))
    return PineWebhookApplication(tracker, config(**overrides)), tracker


def request(application, payload, **overrides):
    values = {
        "method": "POST",
        "raw_path": "/webhooks/tradingview/pine",
        "headers": {
            "Authorization": f"Bearer {SECRET}",
            "Content-Type": "application/json",
        },
        "body": json.dumps(payload).encode(),
        "peer_ip": "203.0.113.8",
        "received_at": RECEIVED,
    }
    values.update(overrides)
    return application.process(**values)


def create_candidate(tracker):
    return tracker.create_candidate(
        {"Symbol": "RELIANCE", "Exchange": "NSE", "PriceNow": 100},
        RECEIVED - timedelta(minutes=5), "cycle", {},
    )


def test_health_is_public_but_other_get_routes_are_not(tmp_path):
    application, _ = app(tmp_path)
    health = application.process("GET", "/health", {}, b"", "127.0.0.1")
    missing = application.process("GET", "/reports", {}, b"", "127.0.0.1")
    assert (health.status_code, health.body) == (200, {"status": "ok"})
    assert missing.status_code == 404


def test_authentication_and_json_content_type_are_required(tmp_path):
    application, _ = app(tmp_path)
    payload = {"event": "BUY_READY", "symbol": "RELIANCE"}
    unauthorized = request(application, payload, headers={"Content-Type": "application/json"})
    wrong_type = request(application, payload, headers={
        "Authorization": f"Bearer {SECRET}", "Content-Type": "text/plain"
    })
    assert unauthorized.status_code == 401
    assert wrong_type.status_code == 415


def test_accepts_new_event_then_treats_explicit_id_retry_as_success(tmp_path):
    application, tracker = app(tmp_path)
    candidate_id = create_candidate(tracker)
    payload = {
        "event_id": "TV-RELIANCE-ENTRY-1", "event": "ENTRY",
        "symbol": "NSE:RELIANCE", "candidate_id": candidate_id,
        "timestamp": RECEIVED.isoformat(), "assumed_entry": 101, "qty": 5,
    }
    first = request(application, payload)
    retry = request(application, {**payload, "ignored_metadata": "retry"})
    assert first.status_code == 201
    assert first.body["status"] == "accepted"
    assert retry.status_code == 200
    assert retry.body["status"] == "duplicate"
    with tracker._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM pine_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM trade_lifecycle").fetchone()[0] == 1


def test_derived_fingerprint_uses_normalized_fields(tmp_path):
    application, tracker = app(tmp_path)
    payload = {
        "event": "buy_ready", "symbol": "NSE:RELIANCE-EQ",
        "timestamp": RECEIVED.isoformat(), "signal_price": 101,
    }
    assert request(application, payload).status_code == 201
    assert request(application, {**payload, "unrecognized": "does-not-change-identity"}).status_code == 200
    with tracker._connect() as connection:
        row = connection.execute(
            "SELECT raw_symbol, symbol, idempotency_key FROM pine_events"
        ).fetchone()
    assert tuple(row)[:2] == ("NSE:RELIANCE-EQ", "RELIANCE")
    assert row[2]


@pytest.mark.parametrize("event", ["STOP_UPDATE", "EXIT"])
def test_out_of_order_event_is_retained_without_inventing_trade(tmp_path, event):
    application, tracker = app(tmp_path)
    candidate_id = create_candidate(tracker)
    response = request(application, {
        "event": event, "candidate_id": candidate_id, "symbol": "RELIANCE",
        "timestamp": RECEIVED.isoformat(), "new_stop": 99, "exit_price": 101,
    })
    assert response.status_code == 201
    assert response.body["processing_status"] == "out_of_order"
    assert tracker.inspect_candidate(candidate_id)["trade"] is None
    with tracker._connect() as connection:
        assert connection.execute(
            "SELECT processing_status FROM pine_events"
        ).fetchone()[0] == "out_of_order"


@pytest.mark.parametrize(
    "payload,error_fragment",
    [
        ({"event": "UNKNOWN", "symbol": "RELIANCE"}, "unsupported event"),
        ({"event": "ENTRY"}, "symbol or candidate_id"),
        ({"event": "ENTRY", "symbol": "RELIANCE", "timestamp": "2026-09-27T04:06:00"}, "timezone"),
        ({"event": "ENTRY", "symbol": "RELIANCE", "qty": "many"}, "finite number"),
        ({"event": "ENTRY", "symbol": "RELIANCE", "exchange": "BSE"}, "only NSE"),
    ],
)
def test_schema_failures_are_clear_400s(tmp_path, payload, error_fragment):
    application, _ = app(tmp_path)
    response = request(application, payload)
    assert response.status_code == 400
    assert error_fragment in response.body["error"]


def test_timestamp_tolerances_and_receive_time_default(tmp_path):
    application, tracker = app(tmp_path, max_past_age_seconds=60, max_future_skew_seconds=10)
    old = request(application, {
        "event": "BUY_READY", "symbol": "RELIANCE",
        "timestamp": (RECEIVED - timedelta(seconds=61)).isoformat(),
    })
    future = request(application, {
        "event": "BUY_READY", "symbol": "RELIANCE",
        "timestamp": (RECEIVED + timedelta(seconds=11)).isoformat(),
    })
    accepted = request(application, {"event": "BUY_READY", "symbol": "RELIANCE"})
    assert old.status_code == future.status_code == 400
    assert accepted.status_code == 201
    with tracker._connect() as connection:
        assert connection.execute("SELECT event_at_utc FROM pine_events").fetchone()[0].startswith(
            "2026-09-27T04:06:00"
        )


def test_payload_limit_malformed_json_and_path_secret(tmp_path):
    application, _ = app(tmp_path, max_body_bytes=20, allow_path_secret=True)
    large = request(application, {"event": "BUY_READY", "symbol": "RELIANCE"})
    malformed = request(application, {}, body=b"{")
    path_auth = request(
        application, {"event": "BUY_READY", "symbol": "R"},
        raw_path=f"/webhooks/tradingview/pine/{SECRET}",
        headers={"Content-Type": "application/json"},
    )
    assert large.status_code == 413
    assert malformed.status_code == 400
    assert path_auth.status_code == 413  # authenticated, then body limit applies

    path_application, _ = app(tmp_path / "path", allow_path_secret=True)
    accepted = request(
        path_application, {"event": "BUY_READY", "symbol": "R"},
        raw_path=f"/webhooks/tradingview/pine/{SECRET}",
        headers={"Content-Type": "application/json"},
    )
    assert accepted.status_code == 201


def test_untrusted_forwarded_for_is_ignored(tmp_path):
    application, _ = app(tmp_path)
    headers = {"X-Forwarded-For": "198.51.100.2"}
    assert application._client_ip("203.0.113.8", headers) == "203.0.113.8"
    trusted, _ = app(tmp_path / "trusted", trusted_proxy_networks=(
        __import__("ipaddress").ip_network("203.0.113.0/24"),
    ))
    assert trusted._client_ip("203.0.113.8", headers) == "198.51.100.2"


def test_rate_limiter_is_bounded_and_auth_failures_are_stricter(tmp_path):
    limiter = SlidingWindowRateLimiter(100, 2)
    assert all(limiter.allow(f"client-{number}", 1.0) for number in range(4))
    assert len(limiter._clients) == 2
    application, _ = app(tmp_path, auth_failure_rate_limit_per_minute=1)
    headers = {"Content-Type": "application/json"}
    first = request(application, {}, headers=headers)
    second = request(application, {}, headers=headers)
    assert first.status_code == 401
    assert second.status_code == 429


def test_secrets_are_redacted_from_storage_and_logs(tmp_path, caplog):
    application, tracker = app(tmp_path)
    with caplog.at_level(logging.INFO, logger="pkscreener.pine_webhook"):
        response = request(application, {
            "event": "BUY_READY", "symbol": "RELIANCE", "timestamp": RECEIVED.isoformat(),
            "apikey": "never-store", "authorization": "also-never-store",
        })
    assert response.status_code == 201
    with tracker._connect() as connection:
        raw = connection.execute("SELECT raw_payload_json FROM pine_events").fetchone()[0]
    assert "never-store" not in raw and "also-never-store" not in raw
    assert SECRET not in caplog.text


def test_existing_database_gets_additive_idempotency_migration(tmp_path):
    root = tmp_path / "candidate_lifecycle"
    root.mkdir()
    path = root / "candidate_lifecycle.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE pine_events (
                pine_event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, symbol TEXT,
                event_at_utc TEXT NOT NULL, supplied_candidate_id TEXT,
                normalized_json TEXT NOT NULL, raw_payload_json TEXT NOT NULL,
                ingestion_source TEXT NOT NULL, received_at_utc TEXT NOT NULL
            )"""
        )
    tracker = CandidateLifecycleTracker(str(tmp_path))
    with tracker._connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(pine_events)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(pine_events)")}
    assert {"source_event_id", "idempotency_key", "raw_symbol", "processing_status"} <= columns
    assert "idx_pine_events_idempotency" in indexes

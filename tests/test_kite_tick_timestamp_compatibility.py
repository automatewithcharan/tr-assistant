import json
import threading
from datetime import datetime, timedelta, timezone

import pkscreener.classes.KiteOrderManager as kite_order_manager_module
from pkscreener.classes.KiteOrderManager import KiteOrderManager
from pkscreener.classes.KiteTickerStream import (
    KiteTickerStreamRunner,
    load_latest_live_ticks,
)


def _ticker_runner():
    runner = KiteTickerStreamRunner.__new__(KiteTickerStreamRunner)
    runner._lock = threading.Lock()
    runner._token_to_symbol = {123: "ABC"}
    runner._token_to_exchange = {123: "NSE"}
    runner._ticks_by_symbol = {}
    runner._status = {"ticks_seen": 0, "last_tick_at": ""}
    return runner


def _order_manager_with_tick(received_at):
    manager = KiteOrderManager.__new__(KiteOrderManager)
    tick = {"last_price": 101.5}
    if received_at is not None:
        tick["received_at"] = received_at
    manager._load_ticks = lambda: {"ABC": tick}
    return manager


def test_new_received_at_is_timezone_aware_utc_and_round_trips():
    runner = _ticker_runner()
    runner._on_ticks(None, [{"instrument_token": 123, "last_price": 101.5}])

    raw = runner._ticks_by_symbol["ABC"]["received_at"]
    parsed = datetime.fromisoformat(raw)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert datetime.fromisoformat(parsed.isoformat()) == parsed
    assert runner._status["last_tick_at"] == raw


def test_snapshot_loader_preserves_aware_received_at(tmp_path):
    raw = "2026-08-17T06:02:05+00:00"
    path = tmp_path / "kiteticker_all_ticks_latest.json"
    path.write_text(
        json.dumps({"ABC": {"last_price": 101.5, "received_at": raw}}),
        encoding="utf-8",
    )

    loaded = load_latest_live_ticks("all")

    assert loaded["ABC"]["received_at"] == raw


def test_order_manager_accepts_fresh_aware_utc_tick():
    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manager = _order_manager_with_tick(received_at)

    assert manager._latest_tick_price("ABC", max_stale_seconds=300) == 101.5


def test_order_manager_rejects_stale_aware_utc_tick_without_mismatch_bypass():
    received_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(
        timespec="seconds"
    )
    manager = _order_manager_with_tick(received_at)

    assert manager._latest_tick_price("ABC", max_stale_seconds=300) == 0.0


def test_order_manager_normalizes_aware_non_utc_offset_before_age_check():
    ist = timezone(timedelta(hours=5, minutes=30))
    received_at = (datetime.now(ist) - timedelta(minutes=10)).isoformat(
        timespec="seconds"
    )
    manager = _order_manager_with_tick(received_at)

    assert manager._latest_tick_price("ABC", max_stale_seconds=300) == 0.0


def test_order_manager_preserves_historical_naive_timestamp_semantics():
    fresh = (datetime.now() - timedelta(seconds=10)).isoformat(timespec="seconds")
    stale = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")

    assert _order_manager_with_tick(fresh)._latest_tick_price(
        "ABC", max_stale_seconds=300
    ) == 101.5
    assert _order_manager_with_tick(stale)._latest_tick_price(
        "ABC", max_stale_seconds=300
    ) == 0.0


def test_order_manager_preserves_missing_and_malformed_timestamp_fallback():
    assert _order_manager_with_tick(None)._latest_tick_price(
        "ABC", max_stale_seconds=300
    ) == 101.5
    assert _order_manager_with_tick("not-a-time")._latest_tick_price(
        "ABC", max_stale_seconds=300
    ) == 101.5


def test_unexpected_aware_age_error_fails_closed_instead_of_accepting_price(
    monkeypatch,
):
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)

    class BrokenAwareClock:
        @staticmethod
        def fromisoformat(_value):
            return stale

        @staticmethod
        def now(_timezone=None):
            raise RuntimeError("clock unavailable")

    monkeypatch.setattr(kite_order_manager_module, "datetime", BrokenAwareClock)
    manager = _order_manager_with_tick(stale.isoformat())

    assert manager._latest_tick_price("ABC", max_stale_seconds=300) == 0.0

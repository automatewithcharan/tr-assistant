from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pkscreener.classes.NSETopGainersWatch import NSETopGainersWatch, fetch_board, format_alert


IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 17, 14, 20, tzinfo=IST)


class Response:
    status_code = 200

    def __init__(self, rows, timestamp="17-Sep-2026 14:19:00"):
        self.rows = rows
        self.timestamp = timestamp

    def raise_for_status(self):
        pass

    def json(self):
        return {"allSec": {"timestamp": self.timestamp, "data": self.rows}}


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *_args, **_kwargs):
        return next(self.responses)


class Transport:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.messages = []

    def post(self, message):
        self.messages.append(message)
        return next(self.outcomes)


def row(symbol, change=10):
    return {"symbol": symbol, "perChange": change, "ltp": 100}


def ok():
    return SimpleNamespace(delivered=True, http_status=200, retry_after_seconds=None)


def fail():
    return SimpleNamespace(delivered=False, http_status=429, retry_after_seconds=1)


def test_fetch_all_securities_board_preserves_every_symbol():
    rows = [row(f"S{i}") for i in range(20)]
    snapshot, found = fetch_board(Session([Response(rows)]), NOW)
    assert snapshot.date() == NOW.date()
    assert [r["symbol"] for r in found] == [r["symbol"] for r in rows]
    assert all(s in format_alert(snapshot, found) for s in ["**S0**", "**S19**"])


@pytest.mark.parametrize("timestamp", ["16-Sep-2026 15:30:00", "17-Sep-2026 13:00:00"])
def test_stale_board_cannot_create_false_alert(timestamp):
    with pytest.raises(ValueError, match="timestamp"):
        fetch_board(Session([Response([row("A")], timestamp)]), NOW)


def test_first_snapshot_all_twenty_then_new_entrant_once_and_restart(tmp_path):
    first = [row(f"S{i}") for i in range(20)]
    second = [row("NEW")] + first[:19]
    transport = Transport([ok(), ok()])
    session = Session([Response(first), Response(first), Response(second), Response(second)])
    watcher = NSETopGainersWatch(tmp_path, transport, lambda: NOW, session)
    assert len(watcher.process()["symbols"]) == 20
    assert watcher.process()["status"] == "up_to_date"
    restarted = NSETopGainersWatch(tmp_path, transport, lambda: NOW, session)
    assert restarted.process()["symbols"] == ["NEW"]
    assert restarted.process()["status"] == "up_to_date"
    assert len(transport.messages) == 2
    assert "**NEW**" in transport.messages[1]


def test_failed_delivery_is_retried_without_marking_sent(tmp_path):
    current = [NOW]
    transport = Transport([fail(), ok()])
    session = Session([Response([row("A")]), Response([row("A")])])
    watcher = NSETopGainersWatch(tmp_path, transport, lambda: current[0], session)
    assert watcher.process()["status"] == "delivery_failed"
    assert watcher.process()["status"] == "retry_wait"
    current[0] = NOW + timedelta(seconds=31)
    assert watcher.process()["symbols"] == ["A"]
    assert len(transport.messages) == 2


def test_official_board_does_not_repeat_broad_feed_symbols(tmp_path):
    import json

    broad = tmp_path / "kite_top100"
    broad.mkdir()
    (broad / "2026-09-17.json").write_text(json.dumps({"sent_symbols": ["A"]}))
    transport = Transport([ok()])
    watcher = NSETopGainersWatch(tmp_path, transport, lambda: NOW,
                                 Session([Response([row("A"), row("B")])]))
    assert watcher.process()["symbols"] == ["B"]
    assert "**A**" not in transport.messages[0]


def test_new_session_resets_deduplication(tmp_path):
    transport = Transport([ok(), ok()])
    first = NSETopGainersWatch(tmp_path, transport, lambda: NOW,
                               Session([Response([row("A")])]))
    assert first.process()["status"] == "delivered"
    next_day = NOW + timedelta(days=1)
    second = NSETopGainersWatch(tmp_path, transport, lambda: next_day,
                                Session([Response([row("A")], "18-Sep-2026 14:19:00")]))
    assert second.process()["status"] == "delivered"

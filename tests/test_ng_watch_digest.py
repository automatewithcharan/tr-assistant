from __future__ import annotations

import datetime as dt
import json

from pkscreener.classes.NGP5EarlyWatchPublisher import DeliveryResult, IST
from pkscreener.classes.NGP5EarlyWatchPublisher import NGP5EarlyWatchPublisher
from pkscreener.classes.NGLeaderIgnitionWatchPublisher import NGLeaderIgnitionWatchPublisher
from pkscreener.classes.NGWatchDigest import (
    NGWatchDigest, build_message, current_slot, eligible, eligible_opening,
)


DAY = "2026-09-16"


def event(symbol: str, at: str, **inputs):
    policy = {
        "symbol": symbol, "last_price": 250, "open_change_pct": 1.5,
        "turnover": 100_000_000, "spread_bps": 9,
        "buy_sell_ratio": 1.8, "rank_improvement": 50,
        "current_rank": 80, "move_delta_pct": 0.7,
    }
    policy.update(inputs)
    return {
        "kind": "p5", "event_id": f"id-{symbol}-{at}",
        "session_date": DAY,
        "observation_timestamp": f"{DAY}T{at}+05:30",
        "policy_inputs": policy,
    }


class Transport:
    def __init__(self):
        self.messages = []
        self.succeed = True

    def post(self, message):
        self.messages.append(message)
        return DeliveryResult(self.succeed, 204 if self.succeed else 500, not self.succeed)


def append_observed(root, payload):
    path = root / DAY / "evidence" / f"events_{DAY}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps({"delivery_state": "OBSERVED", "event": payload}) + "\n")


def opening_event(symbol: str, at: str, score: float, **fields):
    data = {
        "Score": score, "PriceNow": 250, "OpenMovePct": 1.0,
        "DayChangePct": 1.2, "TradedValue": 100_000_000,
        "BuyerSellerRatio": 2.0, "PullbackPct": 0.0,
    }
    data.update(fields)
    return {
        "kind": "leader_ignition", "event_id": f"leader-{symbol}",
        "session_date": DAY, "symbol": symbol,
        "qualification_timestamp": f"{DAY}T{at}+05:30",
        "causal_fields": data,
    }


def test_opening_digest_uses_two_slots_then_post_open_uses_three(tmp_path):
    leader = tmp_path / "leader"
    p5 = tmp_path / "p5"
    transport = Transport()
    now = [dt.datetime(2026, 9, 16, 9, 19, tzinfo=IST)]
    digest = NGWatchDigest(p5, tmp_path / "digest", transport,
                           leader_state_root=leader, clock=lambda: now[0])
    for symbol, score in [("FIRST", 700), ("SECOND", 680), ("THIRD", 660)]:
        append_observed(leader, opening_event(symbol, "09:18:00", score))
    append_observed(leader, opening_event("WEAK", "09:18:00", 800, BuyerSellerRatio=0.7))
    assert current_slot(now[0]) is None
    assert digest.process()["status"] == "outside_window"
    now[0] = dt.datetime(2026, 9, 16, 9, 20, tzinfo=IST)
    assert digest.process()["symbols"] == ["FIRST", "SECOND"]
    assert "Early, unconfirmed watch" in transport.messages[0]
    assert digest.process()["status"] == "already_processed"
    for symbol in ["FIRST", "POST1", "POST2", "POST3", "POST4"]:
        append_observed(p5, event(symbol, "09:40:00"))
    now[0] = dt.datetime(2026, 9, 16, 9, 45, tzinfo=IST)
    assert digest.process()["symbols"] == ["POST1", "POST2", "POST3"]
    assert len(transport.messages) == 2
    assert not eligible_opening(opening_event("BAD", "09:18:00", 800, PullbackPct=None))


def test_digest_suppresses_opening_and_limits_daily_symbols(tmp_path):
    p5 = tmp_path / "p5"
    state = tmp_path / "digest"
    transport = Transport()
    now = [dt.datetime(2026, 9, 16, 9, 45, tzinfo=IST)]
    digest = NGWatchDigest(p5, state, transport, clock=lambda: now[0])
    for symbol in ["A", "B", "C", "D", "E", "F", "G"]:
        append_observed(p5, event(symbol, "09:40:00"))
    append_observed(p5, event("OPENING", "09:20:00"))
    append_observed(p5, event("WIDE", "09:40:00", spread_bps=50))
    append_observed(p5, event("ILLQ", "09:40:00", turnover=1_000_000))
    result = digest.process()
    assert result["status"] == "delivered"
    assert len(result["symbols"]) == 5
    assert "OPENING" not in result["symbols"]
    assert "WIDE" not in result["symbols"]
    assert "NOT A BUY SIGNAL" in transport.messages[0]
    assert digest.process()["status"] == "already_processed"
    now[0] = dt.datetime(2026, 9, 16, 10, 15, tzinfo=IST)
    append_observed(p5, event("LATER", "10:12:00"))
    assert digest.process()["status"] == "no_candidates"
    assert len(transport.messages) == 1
    restarted = NGWatchDigest(p5, state, transport, clock=lambda: now[0])
    assert restarted.process()["status"] == "already_processed"


def test_failed_delivery_does_not_consume_slot_or_daily_cap(tmp_path):
    p5 = tmp_path / "p5"
    transport = Transport()
    transport.succeed = False
    now = [dt.datetime(2026, 9, 16, 10, 15, tzinfo=IST)]
    digest = NGWatchDigest(p5, tmp_path / "digest", transport, clock=lambda: now[0])
    append_observed(p5, event("RETRY", "10:10:00"))
    assert digest.process()["status"] == "failed"
    transport.succeed = True
    assert digest.process()["status"] == "retry_wait"
    now[0] += dt.timedelta(seconds=31)
    assert digest.process()["symbols"] == ["RETRY"]
    assert digest.process()["status"] == "already_processed"


def test_missing_liquidity_inputs_are_not_assumed_good():
    assert not eligible(event("BAD", "10:10:00", spread_bps=None))
    assert not eligible(event("BAD", "10:10:00", buy_sell_ratio=None))
    assert not eligible(event("BAD", "10:10:00", open_change_pct=5))
    assert "unvalidated for profit" in build_message([event("OK", "10:10:00")], "10:15")


def test_archive_is_terminal_and_does_not_post_raw_alerts(tmp_path):
    for cls in (NGP5EarlyWatchPublisher, NGLeaderIgnitionWatchPublisher):
        directory = tmp_path / cls.__name__
        transport = Transport()
        publisher = cls(tmp_path / "source", directory, transport=transport)
        payload = event("ARCHIVE", "10:10:00") if cls is NGP5EarlyWatchPublisher else {
            "event_id": "leader-archive", "session_date": DAY, "symbol": "ARCHIVE",
        }
        publisher.state["pending"][payload["event_id"]] = {
            "event": payload, "message": "raw", "attempts": 0, "next_attempt_at": 0,
        }
        assert publisher.archive_pending(DAY) == 1
        assert publisher.archive_pending(DAY) == 0
        assert transport.messages == []
        assert publisher.state["pending"] == {}
        journal = directory / "evidence" / f"events_{DAY}.jsonl"
        assert json.loads(journal.read_text().splitlines()[-1])["delivery_state"] == "ARCHIVED_FOR_DIGEST"
        assert cls(tmp_path / "source", directory).state["pending"] == {}

from __future__ import annotations

import csv
import datetime as dt
import json
import os
from pathlib import Path

import pytest

from pkscreener.classes.NGLeaderIgnitionWatchPublisher import (
    DeliveryResult,
    NGLeaderIgnitionWatchPublisher,
    build_quality_watch_message,
    deterministic_event_id,
)


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HEADER = [
    "When",
    "Stage",
    "Decision",
    "Reason",
    "Score",
    "Symbol",
    "PriceNow",
    "Open",
    "PrevClose",
    "DayHigh",
    "OpenMovePct",
    "DayChangePct",
    "PullbackPct",
    "BuyerSellerRatio",
    "TotalBuyQty",
    "TotalSellQty",
    "Volume",
    "VolumeDelta",
    "VolumeDeltaPct",
    "PriceDeltaPct",
    "TradedValue",
]


class Clock:
    def __init__(self, value: dt.datetime) -> None:
        self.value = value

    def __call__(self) -> dt.datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += dt.timedelta(seconds=seconds)


class Transport:
    def __init__(self, *results: DeliveryResult) -> None:
        self.results = list(results)
        self.messages: list[str] = []

    def post(self, message: str) -> DeliveryResult:
        self.messages.append(message)
        if not self.results:
            return DeliveryResult(True, 204, False)
        return self.results.pop(0)


def row(
    symbol: str = "IKIO",
    *,
    day: str = "2026-09-11",
    timestamp: str = "09:16:01",
    stage: str = "ignition_watch",
    decision: str = "pass",
    reason: str = "early leader watch",
) -> dict[str, str]:
    return {
        "When": f"{day} {timestamp}",
        "Stage": stage,
        "Decision": decision,
        "Reason": reason,
        "Score": "612.34",
        "Symbol": symbol,
        "PriceNow": "230.84",
        "Open": "225.00",
        "PrevClose": "219.93",
        "DayHigh": "231.50",
        "OpenMovePct": "2.596",
        "DayChangePct": "4.961",
        "PullbackPct": "0.286",
        "BuyerSellerRatio": "1.742",
        "TotalBuyQty": "174200",
        "TotalSellQty": "100000",
        "Volume": "1210608",
        "VolumeDelta": "41200",
        "VolumeDeltaPct": "3.522",
        "PriceDeltaPct": "0.611",
        "TradedValue": "107586732.96",
    }


def write_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=HEADER).writeheader()


def append_row(path: Path, value: dict[str, str], *, newline: bool = True) -> None:
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, lineterminator="\n" if newline else "")
        writer.writerow(value)


def events(state_dir: Path, day: str = "2026-09-11") -> list[dict]:
    path = state_dir / "evidence" / f"events_{day}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def setup_publisher(
    tmp_path: Path,
    *,
    day: str = "2026-09-11",
    transport=None,
    max_attempts: int = 3,
) -> tuple[NGLeaderIgnitionWatchPublisher, Path, Path, Clock]:
    source_dir = tmp_path / "source"
    state_dir = tmp_path / "state"
    source = source_dir / f"leader_ignition_{day}.csv"
    write_header(source)
    clock = Clock(dt.datetime.fromisoformat(f"{day}T09:00:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(
        source_dir,
        state_dir,
        transport=transport,
        max_attempts=max_attempts,
        retry_backoff_seconds=5,
        clock=clock,
    )
    assert publisher.process_session(day)["observed"] == 0  # live-only baseline
    return publisher, source, state_dir, clock


def setup_with_historical_pass(
    tmp_path: Path,
    *,
    symbol: str = "IKIO",
    transport=None,
) -> tuple[NGLeaderIgnitionWatchPublisher, Path, Path, Clock]:
    source_dir = tmp_path / "source"
    state_dir = tmp_path / "state"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row(symbol))
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T10:00:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(
        source_dir,
        state_dir,
        transport=transport,
        clock=clock,
    )
    assert publisher.process_session("2026-09-11")["observed"] == 0
    return publisher, source, state_dir, clock


def test_only_pass_is_observed_and_reject_is_ignored(tmp_path: Path) -> None:
    publisher, source, state, _ = setup_publisher(tmp_path)
    append_row(source, row("REJECTED", decision="reject", reason="traded value below floor"))
    append_row(source, row("PASSED"))
    result = publisher.process_session("2026-09-11")
    assert result["observed"] == 1
    assert set(publisher.state["sessions"]["2026-09-11"]["observed"]) == {"PASSED"}
    observed = [e for e in events(state) if e.get("delivery_state") == "OBSERVED"]
    assert len(observed) == 1
    assert observed[0]["event"]["qualification_reason"] == "Leader Ignition PASS"


def test_first_pass_per_symbol_session_suppresses_reclaim_duplicate(tmp_path: Path) -> None:
    publisher, source, _, _ = setup_publisher(tmp_path)
    append_row(source, row("IKIO"))
    append_row(
        source,
        row("IKIO", timestamp="09:23:00", stage="pullback_reclaim", reason="reclaim after healthy pullback"),
    )
    assert publisher.process_session("2026-09-11")["observed"] == 1
    assert len(publisher.state["pending"]) == 1


def test_same_symbol_is_allowed_in_next_session(tmp_path: Path) -> None:
    publisher, source, _, clock = setup_publisher(tmp_path)
    append_row(source, row("IKIO"))
    assert publisher.process_session("2026-09-11")["observed"] == 1
    clock.value = dt.datetime.fromisoformat("2026-09-12T09:00:00+05:30")
    next_source = source.parent / "leader_ignition_2026-09-12.csv"
    write_header(next_source)
    assert publisher.process_session("2026-09-12")["observed"] == 0
    append_row(next_source, row("IKIO", day="2026-09-12"))
    assert publisher.process_session("2026-09-12")["observed"] == 1
    assert "IKIO" in publisher.state["sessions"]["2026-09-12"]["observed"]


def test_restart_uses_durable_cursor_and_dedupe(tmp_path: Path) -> None:
    publisher, source, state, clock = setup_publisher(tmp_path)
    append_row(source, row("IKIO"))
    assert publisher.process_session("2026-09-11")["observed"] == 1
    restarted = NGLeaderIgnitionWatchPublisher(source.parent, state, clock=clock)
    append_row(source, row("IKIO", timestamp="09:17:00"))
    assert restarted.process_session("2026-09-11")["observed"] == 0
    assert len(restarted.state["pending"]) == 1


def test_stale_pending_expires_and_is_never_delivered(tmp_path: Path) -> None:
    transport = Transport(DeliveryResult(True, 204, False))
    publisher, source, state, clock = setup_publisher(tmp_path)
    append_row(source, row("IKIO"))
    assert publisher.process_source_file(source, "2026-09-11").observed == 1
    clock.value = dt.datetime.fromisoformat("2026-09-12T09:00:00+05:30")
    publisher.transport = transport
    assert publisher.deliver_pending("2026-09-12") == 0
    assert transport.messages == []
    assert publisher.state["pending"] == {}
    assert any(e.get("delivery_state") == "EXPIRED_STALE_SESSION" for e in events(state))


def test_partial_row_waits_for_newline(tmp_path: Path) -> None:
    publisher, source, _, _ = setup_publisher(tmp_path)
    values = [row("IKIO")[name] for name in HEADER]
    with source.open("a", encoding="utf-8") as handle:
        handle.write(",".join(values))
    assert publisher.process_session("2026-09-11")["observed"] == 0
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert publisher.process_session("2026-09-11")["observed"] == 1


def test_malformed_complete_row_quarantines_source(tmp_path: Path) -> None:
    publisher, source, state, _ = setup_publisher(tmp_path)
    with source.open("a", encoding="utf-8") as handle:
        handle.write("too,few,columns\n")
    result = publisher.process_session("2026-09-11")
    assert result.quarantined if hasattr(result, "quarantined") else result["quarantined"] == 1
    assert any(e.get("delivery_state") == "SOURCE_QUARANTINED" for e in events(state))
    assert publisher.state["pending"] == {}


def test_source_replacement_replays_safely_but_dedupe_blocks_old_symbol(tmp_path: Path) -> None:
    publisher, source, _, _ = setup_publisher(tmp_path)
    append_row(source, row("IKIO"))
    assert publisher.process_session("2026-09-11")["observed"] == 1
    replacement = source.with_suffix(".replacement")
    write_header(replacement)
    append_row(replacement, row("IKIO"))
    append_row(replacement, row("NEWLEADER", timestamp="09:18:00"))
    os.replace(replacement, source)
    assert publisher.process_session("2026-09-11")["observed"] == 1
    assert set(publisher.state["sessions"]["2026-09-11"]["observed"]) == {"IKIO", "NEWLEADER"}


def test_429_uses_retry_after_then_delivers(tmp_path: Path) -> None:
    transport = Transport(
        DeliveryResult(False, 429, True, retry_after_seconds=7, error="http_429"),
        DeliveryResult(True, 204, False),
    )
    publisher, source, state, clock = setup_publisher(tmp_path, transport=transport)
    append_row(source, row("IKIO"))
    assert publisher.process_session("2026-09-11")["delivered"] == 0
    assert len(publisher.state["pending"]) == 1
    clock.advance(6)
    assert publisher.deliver_pending("2026-09-11") == 0
    clock.advance(1)
    assert publisher.deliver_pending("2026-09-11") == 1
    assert [e["delivery_state"] for e in events(state) if e.get("event_kind") == "leader_ignition"][-1] == "DELIVERED"


def test_5xx_is_retryable(tmp_path: Path) -> None:
    transport = Transport(
        DeliveryResult(False, 503, True, error="http_503"),
        DeliveryResult(True, 204, False),
    )
    publisher, source, _, clock = setup_publisher(tmp_path, transport=transport)
    append_row(source, row("IKIO"))
    assert publisher.process_session("2026-09-11")["delivered"] == 0
    clock.advance(5)
    assert publisher.deliver_pending("2026-09-11") == 1


@pytest.mark.parametrize("status", [400, 401, 403])
def test_permanent_4xx_is_terminal(tmp_path: Path, status: int) -> None:
    transport = Transport(DeliveryResult(False, status, False, error=f"http_{status}"))
    publisher, source, state, _ = setup_publisher(tmp_path, transport=transport)
    append_row(source, row("IKIO"))
    assert publisher.process_session("2026-09-11")["delivered"] == 0
    assert publisher.state["pending"] == {}
    failure = [e for e in events(state) if e.get("delivery_state") == "FAILED"][-1]
    assert failure["terminal"] is True
    assert failure["http_status"] == status


def test_retry_budget_is_bounded(tmp_path: Path) -> None:
    transport = Transport(
        DeliveryResult(False, 503, True, error="http_503"),
        DeliveryResult(False, 503, True, error="http_503"),
    )
    publisher, source, state, clock = setup_publisher(tmp_path, transport=transport, max_attempts=2)
    append_row(source, row("IKIO"))
    publisher.process_session("2026-09-11")
    clock.advance(5)
    publisher.deliver_pending("2026-09-11")
    assert len(transport.messages) == 2
    assert publisher.state["pending"] == {}
    assert [e for e in events(state) if e.get("delivery_state") == "FAILED"][-1]["terminal"] is True


def test_no_transport_queues_without_delivery(tmp_path: Path) -> None:
    publisher, source, _, _ = setup_publisher(tmp_path)
    append_row(source, row("IKIO"))
    result = publisher.process_session("2026-09-11")
    assert result["observed"] == 1
    assert result["delivered"] == 0
    assert len(publisher.state["pending"]) == 1


def test_first_start_never_replays_existing_rows(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("HISTORICAL"))
    transport = Transport(DeliveryResult(True, 204, False))
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T10:00:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, tmp_path / "state", transport=transport, clock=clock)
    result = publisher.process_session("2026-09-11")
    assert result["observed"] == result["delivered"] == 0
    assert transport.messages == []
    assert publisher.state["pending"] == {}


def test_armed_before_source_creation_consumes_new_live_rows(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    state_dir = tmp_path / "state"
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:00:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, state_dir, clock=clock)
    assert publisher.process_session("2026-09-11")["observed"] == 0
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("FIRSTLIVE"))
    assert publisher.process_session("2026-09-11")["observed"] == 1
    assert "FIRSTLIVE" in publisher.state["sessions"]["2026-09-11"]["observed"]


def test_past_session_argument_never_replays_or_delivers(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source = source_dir / "leader_ignition_2026-09-10.csv"
    write_header(source)
    append_row(source, row("HISTORICAL", day="2026-09-10"))
    transport = Transport(DeliveryResult(True, 204, False))
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:00:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, tmp_path / "state", transport=transport, clock=clock)
    result = publisher.process_session("2026-09-10")
    assert result["observed"] == result["delivered"] == 0
    assert transport.messages == []


def test_unexpected_network_exception_is_bounded_and_retryable(tmp_path: Path) -> None:
    class NetworkThenSuccess:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, _message: str) -> DeliveryResult:
            self.calls += 1
            if self.calls == 1:
                raise OSError("offline")
            return DeliveryResult(True, 204, False)

    transport = NetworkThenSuccess()
    publisher, source, _, clock = setup_publisher(tmp_path, transport=transport)
    append_row(source, row("IKIO"))
    assert publisher.process_session("2026-09-11")["delivered"] == 0
    clock.advance(5)
    assert publisher.deliver_pending("2026-09-11") == 1
    assert transport.calls == 2


def test_writes_are_confined_to_state_directory(tmp_path: Path) -> None:
    publisher, source, state, _ = setup_publisher(tmp_path)
    before = source.read_bytes()
    append_row(source, row("IKIO"))
    expected_source = source.read_bytes()
    publisher.process_session("2026-09-11")
    assert source.read_bytes() == expected_source
    assert before != expected_source
    written = {p.relative_to(state).as_posix() for p in state.rglob("*") if p.is_file()}
    assert written == {"state.json", "evidence/events_2026-09-11.jsonl"}


def test_event_preserves_causal_source_and_message_contract(tmp_path: Path) -> None:
    publisher, source, _, _ = setup_publisher(tmp_path)
    append_row(source, row("IKIO"))
    publisher.process_source_file(source, "2026-09-11")
    event = publisher.state["sessions"]["2026-09-11"]["observed"]["IKIO"]
    assert event["event_id"] == deterministic_event_id("2026-09-11", "IKIO")
    assert event["source_record"]["path"] == str(source.resolve())
    assert event["source_record"]["byte_offset_end"] > event["source_record"]["byte_offset_start"]
    assert len(event["source_record"]["row_sha256"]) == 64
    assert event["qualification_timestamp"] == "2026-09-11T09:16:01+05:30"
    assert event["causal_fields"]["PriceNow"] == "230.84"
    message = build_quality_watch_message(event)
    assert message == (
        "🟡 **NG QUALITY WATCH**\n"
        "**NOT A BUY SIGNAL**\n\n"
        "Source: **OPENING LEADER**\n"
        "Symbol: **`IKIO`**\n"
        "Qualified: 2026-09-11T09:16:01+05:30 IST\n\n"
        "LTP: ₹230.84\n"
        "Day: 4.961%\n"
        "From open: 2.596%\n"
        "Turnover: ₹10.76 Cr\n"
        "Volume / volume delta: 1210608 / 41200 (3.522%)\n"
        "B/S: 1.742\n"
        "Pullback: 0.286%\n"
        "Rank: UNKNOWN\n\n"
        "Reason: **Leader Ignition PASS**\n"
        "[Open TradingView](https://www.tradingview.com/chart/?symbol=NSE%3AIKIO)\n"
        f"Event ID: `{event['event_id']}`"
    )
    for forbidden in ("Quantity", "Stop", "Target", "profit", "Buy above", "Entry"):
        assert forbidden.lower() not in message.lower()


def test_live_only_a_baseline_pass_suppresses_later_reclaim(tmp_path: Path) -> None:
    transport = Transport(DeliveryResult(True, 204, False))
    publisher, source, _, _ = setup_with_historical_pass(tmp_path, transport=transport)
    assert "IKIO" in publisher.state["sessions"]["2026-09-11"]["baseline_seen_pass_symbols"]
    append_row(source, row("IKIO", timestamp="10:01:00", stage="pullback_reclaim"))
    result = publisher.process_session("2026-09-11")
    assert result["observed"] == result["delivered"] == 0
    assert publisher.state["pending"] == {}
    assert transport.messages == []


def test_live_only_b_inode_replacement_cannot_resurrect_baseline_pass(tmp_path: Path) -> None:
    transport = Transport(DeliveryResult(True, 204, False))
    publisher, source, _, _ = setup_with_historical_pass(tmp_path, transport=transport)
    replacement = source.with_suffix(".replacement")
    write_header(replacement)
    append_row(replacement, row("IKIO", reason="replayed historical row"))
    os.replace(replacement, source)
    result = publisher.process_session("2026-09-11")
    assert result["observed"] == result["delivered"] == 0
    assert transport.messages == []


def test_live_only_c_same_inode_truncate_regrow_cannot_resurrect_pass(tmp_path: Path) -> None:
    publisher, source, _, _ = setup_with_historical_pass(tmp_path)
    original_inode = source.stat().st_ino
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerow(row("IKIO", reason="rewritten historical row"))
        writer.writerow(row("NEWLEADER", timestamp="10:02:00"))
    assert source.stat().st_ino == original_inode
    assert publisher.process_session("2026-09-11")["observed"] == 1
    observed = publisher.state["sessions"]["2026-09-11"]["observed"]
    assert set(observed) == {"NEWLEADER"}


def test_prearm_partial_a_historical_pass_completes_suppressed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("IKIO"), newline=False)
    transport = Transport(DeliveryResult(True, 204, False))
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:15:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(
        source_dir,
        tmp_path / "state",
        transport=transport,
        clock=clock,
    )
    assert publisher.process_session("2026-09-11")["observed"] == 0
    assert publisher.state["sessions"]["2026-09-11"]["baseline_seen_pass_symbols"] == {}
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    result = publisher.process_session("2026-09-11")
    assert result["observed"] == result["delivered"] == 0
    assert "IKIO" in publisher.state["sessions"]["2026-09-11"]["baseline_seen_pass_symbols"]
    assert publisher.state["pending"] == {}
    assert transport.messages == []


def test_prearm_partial_b_reject_does_not_suppress_later_pass(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("IKIO", decision="reject"), newline=False)
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:15:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, tmp_path / "state", clock=clock)
    assert publisher.process_session("2026-09-11")["observed"] == 0
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert publisher.process_session("2026-09-11")["observed"] == 0
    append_row(source, row("IKIO", timestamp="09:16:00"))
    assert publisher.process_session("2026-09-11")["observed"] == 1


def test_prearm_partial_c_following_live_pass_is_processed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("IKIO"), newline=False)
    transport = Transport(DeliveryResult(True, 204, False))
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:15:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(
        source_dir,
        tmp_path / "state",
        transport=transport,
        clock=clock,
    )
    assert publisher.process_session("2026-09-11")["observed"] == 0
    with source.open("a", newline="", encoding="utf-8") as handle:
        handle.write("\n")
        csv.DictWriter(handle, fieldnames=HEADER, lineterminator="\n").writerow(
            row("NEWLEADER", timestamp="09:15:01")
        )
    result = publisher.process_session("2026-09-11")
    assert result["observed"] == result["delivered"] == 1
    session = publisher.state["sessions"]["2026-09-11"]
    assert set(session["baseline_seen_pass_symbols"]) == {"IKIO"}
    assert set(session["observed"]) == {"NEWLEADER"}
    assert len(transport.messages) == 1


def test_prearm_partial_d_inode_replacement_does_not_suppress_unrelated_row(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("IKIO"), newline=False)
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:15:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, tmp_path / "state", clock=clock)
    assert publisher.process_session("2026-09-11")["observed"] == 0
    replacement = source.with_suffix(".replacement")
    write_header(replacement)
    append_row(replacement, row("REPLACEMENT", timestamp="09:16:00"))
    os.replace(replacement, source)
    assert publisher.process_session("2026-09-11")["observed"] == 1
    session = publisher.state["sessions"]["2026-09-11"]
    assert set(session["observed"]) == {"REPLACEMENT"}
    assert "REPLACEMENT" not in session["baseline_seen_pass_symbols"]


def test_prearm_partial_e_same_inode_regrow_does_not_suppress_unrelated_row(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("IKIO"), newline=False)
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:15:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, tmp_path / "state", clock=clock)
    assert publisher.process_session("2026-09-11")["observed"] == 0
    original_inode = source.stat().st_ino
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerow(row("REGROWN", timestamp="09:16:00"))
    assert source.stat().st_ino == original_inode
    assert publisher.process_session("2026-09-11")["observed"] == 1
    session = publisher.state["sessions"]["2026-09-11"]
    assert set(session["observed"]) == {"REGROWN"}
    assert "REGROWN" not in session["baseline_seen_pass_symbols"]


def test_prearm_partial_f_restart_while_waiting_preserves_suppression(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    state_dir = tmp_path / "state"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("IKIO"), newline=False)
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:15:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, state_dir, clock=clock)
    assert publisher.process_session("2026-09-11")["observed"] == 0
    marker = publisher.state["sources"][str(source.resolve())]["prearm_partial"]
    assert marker["prefix_length"] > 0
    assert len(marker["prefix_sha256"]) == 64
    restarted = NGLeaderIgnitionWatchPublisher(source_dir, state_dir, clock=clock)
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert restarted.process_session("2026-09-11")["observed"] == 0
    assert "IKIO" in restarted.state["sessions"]["2026-09-11"]["baseline_seen_pass_symbols"]


def test_prearm_partial_g_reclaim_suppressed_after_completion_restart(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    state_dir = tmp_path / "state"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("IKIO"), newline=False)
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:15:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, state_dir, clock=clock)
    assert publisher.process_session("2026-09-11")["observed"] == 0
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert publisher.process_session("2026-09-11")["observed"] == 0
    restarted = NGLeaderIgnitionWatchPublisher(source_dir, state_dir, clock=clock)
    append_row(source, row("IKIO", timestamp="09:17:00", stage="pullback_reclaim"))
    assert restarted.process_session("2026-09-11")["observed"] == 0
    assert restarted.state["pending"] == {}


def test_prearm_partial_h_symbol_allowed_next_session(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    state_dir = tmp_path / "state"
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("IKIO"), newline=False)
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:15:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, state_dir, clock=clock)
    assert publisher.process_session("2026-09-11")["observed"] == 0
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert publisher.process_session("2026-09-11")["observed"] == 0
    clock.value = dt.datetime.fromisoformat("2026-09-12T09:00:00+05:30")
    next_source = source_dir / "leader_ignition_2026-09-12.csv"
    write_header(next_source)
    assert publisher.process_session("2026-09-12")["observed"] == 0
    append_row(next_source, row("IKIO", day="2026-09-12"))
    assert publisher.process_session("2026-09-12")["observed"] == 1


def test_live_only_e_start_before_source_first_future_pass_alerts(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    clock = Clock(dt.datetime.fromisoformat("2026-09-11T09:00:00+05:30"))
    publisher = NGLeaderIgnitionWatchPublisher(source_dir, tmp_path / "state", clock=clock)
    assert publisher.process_session("2026-09-11")["observed"] == 0
    source = source_dir / "leader_ignition_2026-09-11.csv"
    write_header(source)
    append_row(source, row("FIRSTLIVE"))
    assert publisher.process_session("2026-09-11")["observed"] == 1


def test_live_only_f_start_after_empty_source_first_future_pass_alerts(tmp_path: Path) -> None:
    publisher, source, _, _ = setup_publisher(tmp_path)
    append_row(source, row("FIRSTLIVE"))
    assert publisher.process_session("2026-09-11")["observed"] == 1


def test_live_only_g_restart_preserves_baseline_and_accepts_unseen_symbol(tmp_path: Path) -> None:
    publisher, source, state, clock = setup_with_historical_pass(tmp_path)
    restarted = NGLeaderIgnitionWatchPublisher(source.parent, state, clock=clock)
    append_row(source, row("NEWLEADER", timestamp="10:01:00"))
    append_row(source, row("IKIO", timestamp="10:02:00", stage="pullback_reclaim"))
    assert restarted.process_session("2026-09-11")["observed"] == 1
    assert set(restarted.state["sessions"]["2026-09-11"]["observed"]) == {"NEWLEADER"}


def test_live_only_h_baseline_symbol_is_eligible_next_session(tmp_path: Path) -> None:
    publisher, source, _, clock = setup_with_historical_pass(tmp_path)
    clock.value = dt.datetime.fromisoformat("2026-09-12T09:00:00+05:30")
    next_source = source.parent / "leader_ignition_2026-09-12.csv"
    write_header(next_source)
    assert publisher.process_session("2026-09-12")["observed"] == 0
    append_row(next_source, row("IKIO", day="2026-09-12"))
    assert publisher.process_session("2026-09-12")["observed"] == 1


def test_live_only_i_replacement_cannot_redeliver_valid_live_pass(tmp_path: Path) -> None:
    transport = Transport(DeliveryResult(True, 204, False), DeliveryResult(True, 204, False))
    publisher, source, _, _ = setup_publisher(tmp_path, transport=transport)
    append_row(source, row("IKIO"))
    assert publisher.process_session("2026-09-11")["delivered"] == 1
    replacement = source.with_suffix(".replacement")
    write_header(replacement)
    append_row(replacement, row("IKIO", reason="replayed after delivery"))
    os.replace(replacement, source)
    result = publisher.process_session("2026-09-11")
    assert result["observed"] == result["delivered"] == 0
    assert len(transport.messages) == 1

from __future__ import annotations

import csv
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import urllib.error

import pytest

from pkscreener.classes.NGP5EarlyWatchPublisher import (
    DEDUPE_VERSION,
    DeliveryResult,
    DiscordWebhookTransport,
    EXPIRED_STALE_SESSION,
    IST,
    NGP5EarlyWatchPublisher,
    OPENING_COVERAGE_INVALID,
    OPENING_COVERAGE_UNKNOWN,
    OPENING_COVERAGE_VALID,
    POLICY_VERSION,
    RESEARCH_PROVENANCE,
    build_early_watch_message,
    evaluate_p5,
    validate_webhook_url,
)
from scripts.ng_p5_early_watch_publisher import _parser, main as cli_main


HEADER = [
    "When", "RecorderGeneration", "Exchange", "Symbol", "LastPrice", "PrevClose", "Open",
    "DayChangePct", "OpenChangePct", "DayChangeRank", "PreviousDayChangeRank",
    "RankImprovement", "RankVelocityPerMinute", "DayChangeDeltaPct",
    "Volume", "TradedValue", "BuyerSellerRatio", "SpreadBps",
    "PullbackFromHighPct", "MarketBreadthPct", "Sector",
    "SectorAvgChangePct", "SectorBreadthPct",
]


def row(**updates):
    data = {
        "When": "2026-09-09 10:37:08",
        "RecorderGeneration": "6",
        "Exchange": "NSE",
        "Symbol": "EXAMPLE",
        "LastPrice": "50",
        "PrevClose": "49",
        "Open": "49.5",
        "DayChangePct": "1.0",
        "OpenChangePct": "1.01",
        "DayChangeRank": "200",
        "PreviousDayChangeRank": "225",
        "RankImprovement": "25",
        "RankVelocityPerMinute": "100",
        "DayChangeDeltaPct": "0.5",
        "Volume": "600000",
        "TradedValue": "30000000",
        "BuyerSellerRatio": "0.1",
        "SpreadBps": "500",
        "PullbackFromHighPct": "9",
        "MarketBreadthPct": "51",
        "Sector": "UNKNOWN",
        "SectorAvgChangePct": "0",
        "SectorBreadthPct": "0",
    }
    data.update({key: str(value) for key, value in updates.items()})
    return data


def opening_scaffold(day: str, *, start="09:15:00", end="09:30:00"):
    current = datetime.fromisoformat(f"{day} {start}")
    finish = datetime.fromisoformat(f"{day} {end}")
    result = []
    while current <= finish:
        result.append(
            row(
                When=current.isoformat(sep=" "),
                Symbol=f"COVERAGE_{current.strftime('%H%M%S')}",
                DayChangePct="0",
                DayChangeRank="201",
                PreviousDayChangeRank="201",
                RankImprovement="0",
                DayChangeDeltaPct="0",
                TradedValue="0",
            )
        )
        current += timedelta(seconds=30)
    return result


def write_rows(path: Path, rows, *, trailing_newline=True):
    rows = list(rows)
    if rows and max(str(item.get("When") or "")[11:19] for item in rows) >= "09:30:00":
        day = min(str(item.get("When") or "") for item in rows)[:10]
        rows = opening_scaffold(day) + rows
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    if not trailing_newline:
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    if rows:
        write_coverage(path.parent, rows[0]["When"], full_session_eligible=True)


def write_coverage(source_dir: Path, first_recorded_at: str, *, full_session_eligible=True):
    day = str(first_recorded_at)[:10]
    payload = {
        "status": "recording_full_universe_rank",
        "date": day,
        "first_recorded_at": first_recorded_at,
        "generation_started_at": first_recorded_at,
        "full_session_eligible": full_session_eligible,
        "recorder_generation": 6,
        "interval_seconds": 15.0,
        "leader_pool_size": 200,
        "leader_pool_recall_pct": 100.0,
    }
    (source_dir / "full_universe_rank_status.json").write_text(json.dumps(payload))


def append_rows(path: Path, rows):
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, lineterminator="\n")
        writer.writerows(rows)


def write_raw_rows(path: Path, rows, *, trailing_newline=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    if not trailing_newline:
        path.write_bytes(path.read_bytes().rstrip(b"\n"))


class SequenceTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.messages = []

    def post(self, message):
        self.messages.append(message)
        return self.results.pop(0)


class RaisingTransport:
    def __init__(self, exception):
        self.exception = exception
        self.messages = []

    def post(self, message):
        self.messages.append(message)
        raise self.exception


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 9, 9, 10, 40, tzinfo=IST)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def journal_rows(state_dir: Path, *, include_operational=False):
    result = []
    for path in sorted((state_dir / "evidence").glob("*.jsonl")):
        result.extend(json.loads(line) for line in path.read_text().splitlines())
    if not include_operational:
        result = [item for item in result if item.get("event_kind") != "operational"]
    return result


def operational_rows(state_dir: Path):
    return [
        item
        for item in journal_rows(state_dir, include_operational=True)
        if item.get("event_kind") == "operational"
    ]


def test_exact_frozen_boundaries_qualify_and_context_fields_do_not_gate():
    result = evaluate_p5(row(), set())
    assert result.qualifies is True
    assert result.inputs["policy_version"] == POLICY_VERSION
    assert result.inputs["time_bucket"] == "10:30–12:00"
    # Deliberately poor display-only context must not affect P5 eligibility.
    assert result.inputs["buy_sell_ratio"] == 0.1
    assert result.inputs["spread_bps"] == 500
    assert result.inputs["pullback_from_hod_pct"] == 9


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("When", "2026-09-09 09:29:59", "outside"),
        ("When", "2026-09-09 15:00:00", "outside"),
        ("DayChangePct", "0.999", "day change"),
        ("DayChangePct", "3.501", "day change"),
        ("LastPrice", "49.99", "price"),
        ("TradedValue", "29999999.99", "turnover"),
        ("DayChangeRank", "201", "rank above"),
        ("RankImprovement", "24", "rank improvement"),
        ("DayChangeDeltaPct", "0.499", "move delta"),
        ("Exchange", "BSE", "exchange"),
    ],
)
def test_thresholds_reject_only_outside_frozen_boundaries(field, value, reason):
    result = evaluate_p5(row(**{field: value}), set())
    assert result.qualifies is False
    assert reason in result.reason


def test_upper_day_boundary_and_0930_boundary_are_inclusive():
    result = evaluate_p5(
        row(When="2026-09-09 09:30:00", DayChangePct="3.5"), set()
    )
    assert result.qualifies is True
    assert result.inputs["time_bucket"] == "09:30–10:30"


def test_opening_leader_exclusion():
    result = evaluate_p5(row(Symbol="OPENLEAD"), {"OPENLEAD"})
    assert result.qualifies is False
    assert result.reason == "major opening leader"


def test_one_alert_per_symbol_session_duplicate_rows_and_restart_dedupe(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    candidate = row(Symbol="ONCE")
    write_rows(source, [candidate, candidate])
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 1
    assert len(journal_rows(state)) == 1
    restarted = NGP5EarlyWatchPublisher(source.parent, state)
    assert restarted.process_feature_file(source) == 0
    assert len(journal_rows(state)) == 1
    assert list(restarted.state["sessions"]["2026-09-09"]["observed"]) == ["ONCE"]


def test_opening_leader_is_learned_from_source_before_later_row(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    opening = row(
        When="2026-09-09 09:20:00", Symbol="OPENLEAD", DayChangePct="0.5",
        DayChangeRank="200", TradedValue="1000", LastPrice="50",
    )
    later = row(When="2026-09-09 10:00:00", Symbol="OPENLEAD")
    write_rows(source, [opening, later])
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 0
    assert publisher.state["sessions"]["2026-09-09"]["opening_leaders"] == ["OPENLEAD"]


def test_partial_and_malformed_source_rows_do_not_emit_until_complete(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_rows(source, [row(Symbol="PARTIAL")], trailing_newline=False)
    state = tmp_path / "publisher"
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 0
    with source.open("ab") as handle:
        handle.write(b"\nmalformed,row\n")
    assert publisher.process_feature_file(source) == 1
    assert len(journal_rows(state)) == 1


def test_observed_evidence_contains_frozen_inputs_and_delivery_contract(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_rows(source, [row(Symbol="EVIDENCE")])
    state = tmp_path / "publisher"
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 1
    observed = journal_rows(state)[0]
    assert observed["delivery_state"] == "OBSERVED"
    assert observed["delivery_attempts"] == 0
    assert observed["http_status"] is None
    assert observed["event"]["qualification_result"] is True
    assert observed["event"]["dedupe_version"] == DEDUPE_VERSION
    assert observed["event"]["source_record"]["row_sha256"]
    assert observed["event"]["policy_inputs"]["move_delta_pct"] == 0.5


def _queued_publisher(tmp_path, transport, *, clock=None, max_attempts=3, backoff=0):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_rows(source, [row(Symbol="DELIVERY")])
    publisher = NGP5EarlyWatchPublisher(
        source.parent,
        tmp_path / "publisher",
        transport=transport,
        max_attempts=max_attempts,
        retry_backoff_seconds=backoff,
        clock=clock,
    )
    assert publisher.process_feature_file(source) == 1
    return publisher


def test_discord_500_is_retried_then_acknowledged(tmp_path):
    transport = SequenceTransport(
        DeliveryResult(False, 500, True, error="http_500"),
        DeliveryResult(True, 204, False),
    )
    publisher = _queued_publisher(tmp_path, transport)
    assert publisher.deliver_pending("2026-09-09") == 0
    assert publisher.state["pending"]
    assert publisher.deliver_pending("2026-09-09") == 1
    assert not publisher.state["pending"]
    states = [item["delivery_state"] for item in journal_rows(tmp_path / "publisher")]
    assert states == ["OBSERVED", "ATTEMPTED", "FAILED", "ATTEMPTED", "DELIVERED"]


def test_discord_429_honors_retry_after(tmp_path):
    clock = MutableClock()
    transport = SequenceTransport(
        DeliveryResult(False, 429, True, retry_after_seconds=7, error="http_429"),
        DeliveryResult(True, 204, False),
    )
    publisher = _queued_publisher(tmp_path, transport, clock=clock)
    assert publisher.deliver_pending("2026-09-09") == 0
    assert publisher.deliver_pending("2026-09-09") == 0
    assert len(transport.messages) == 1
    clock.advance(7)
    assert publisher.deliver_pending("2026-09-09") == 1


def test_network_failure_is_bounded(tmp_path):
    transport = SequenceTransport(
        DeliveryResult(False, None, True, error="URLError"),
        DeliveryResult(False, None, True, error="URLError"),
    )
    publisher = _queued_publisher(tmp_path, transport, max_attempts=2)
    assert publisher.deliver_pending("2026-09-09") == 0
    assert publisher.deliver_pending("2026-09-09") == 0
    assert not publisher.state["pending"]
    assert journal_rows(tmp_path / "publisher")[-1]["terminal"] is True


def test_permanent_4xx_is_not_retried(tmp_path):
    transport = SequenceTransport(DeliveryResult(False, 400, False, error="http_400"))
    publisher = _queued_publisher(tmp_path, transport)
    assert publisher.deliver_pending("2026-09-09") == 0
    assert not publisher.state["pending"]
    assert len(transport.messages) == 1
    failed = journal_rows(tmp_path / "publisher")[-1]
    assert failed["delivery_state"] == "FAILED"
    assert failed["http_status"] == 400
    assert failed["terminal"] is True


def test_publisher_restart_resumes_undelivered_queue(tmp_path):
    first_transport = SequenceTransport(
        DeliveryResult(False, 500, True, error="http_500")
    )
    first = _queued_publisher(tmp_path, first_transport)
    assert first.deliver_pending("2026-09-09") == 0
    second_transport = SequenceTransport(DeliveryResult(True, 204, False))
    restarted = NGP5EarlyWatchPublisher(
        tmp_path / "source", tmp_path / "publisher", transport=second_transport,
        retry_backoff_seconds=0,
    )
    assert restarted.deliver_pending("2026-09-09") == 1
    assert not restarted.state["pending"]


def test_candidate_upgrade_reads_existing_qualification_without_new_rule(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    upgrade_dir = tmp_path / "timing"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="UPGRADE", When="2026-09-09 10:37:08")])
    upgrade_dir.mkdir()
    timing = {
        "event_type": "SOURCE_QUALIFIED",
        "source": "buy_now_board",
        "symbol": "UPGRADE",
        "trading_date": "2026-09-09",
        "event_timestamp": "2026-09-09T11:07:08+05:30",
        "price": 53,
        "day_change_pct": 6,
    }
    (upgrade_dir / "2026-09-09.jsonl").write_text(json.dumps(timing) + "\n")
    publisher = NGP5EarlyWatchPublisher(source.parent, state, upgrade_source_dir=upgrade_dir)
    result = publisher.process_session("2026-09-09")
    assert result["observed"] == 1
    assert result["upgrades"] == 1
    pending = list(publisher.state["pending"].values())
    upgrade = next(item["event"] for item in pending if item["event"]["kind"] == "upgrade")
    assert upgrade["current_state"] == "Buy Now"
    assert upgrade["elapsed_minutes"] == 30
    assert upgrade["original_p5"]["day_change_pct"] == 1


def test_message_is_human_review_only():
    evaluation = evaluate_p5(row(Symbol="SAFE"), set())
    event = {
        "event_id": "a" * 64,
        "policy_inputs": evaluation.inputs,
    }
    message = build_early_watch_message(event)
    assert "NOT A BUY SIGNAL" in message
    assert "PAPER or SKIP" in message
    assert "NSE%3ASAFE" in message
    for forbidden in ("Target:", "Stop:", "Quantity:", "Expected return"):
        assert forbidden not in message


def test_only_publisher_local_state_is_written(tmp_path):
    data = tmp_path / "results" / "Data"
    source_dir = data / "live_feature_recorder"
    source = source_dir / "live_features_2026-09-09.csv"
    write_rows(source, [row(Symbol="ISOLATED")])
    protected = {}
    for name in ("experiment_1", "experiment_2", "experiment_3", "paper_trade", "goal_selector", "scanner"):
        path = data / name / "sentinel.txt"
        path.parent.mkdir(parents=True)
        path.write_text(name)
        protected[path] = path.read_bytes()
    source_before = source.read_bytes()
    state = data / "ng_p5_early_watch"
    publisher = NGP5EarlyWatchPublisher(source_dir, state)
    assert publisher.process_feature_file(source) == 1
    assert source.read_bytes() == source_before
    assert all(path.read_bytes() == content for path, content in protected.items())
    changed_roots = {path.relative_to(data).parts[0] for path in data.rglob("*") if path.is_file()}
    assert changed_roots == {
        "live_feature_recorder", "ng_p5_early_watch", "experiment_1",
        "experiment_2", "experiment_3", "paper_trade", "goal_selector", "scanner",
    }


def test_historical_research_time_boundaries_are_exactly_left_inclusive():
    assert "inclusive=left" in RESEARCH_PROVENANCE
    assert evaluate_p5(row(When="2026-09-09 09:29:59.999999"), set()).qualifies is False
    assert evaluate_p5(row(When="2026-09-09 09:30:00"), set()).qualifies is True
    assert evaluate_p5(row(When="2026-09-09 14:59:59.999999"), set()).qualifies is True
    assert evaluate_p5(row(When="2026-09-09 15:00:00"), set()).qualifies is False
    # 04:00 UTC is exactly 09:30 IST.
    assert evaluate_p5(row(When="2026-09-09T04:00:00+00:00"), set()).qualifies is True


def test_stale_prior_session_pending_expires_without_http(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="STALE")])
    first = NGP5EarlyWatchPublisher(source.parent, state)
    assert first.process_session("2026-09-09")["observed"] == 1
    transport = SequenceTransport(DeliveryResult(True, 204, False))
    next_day = NGP5EarlyWatchPublisher(source.parent, state, transport=transport)
    result = next_day.process_session("2026-09-10")
    assert result["expired"] == 1
    assert transport.messages == []
    expired = [
        item
        for item in journal_rows(state, include_operational=True)
        if item.get("delivery_state") == EXPIRED_STALE_SESSION
    ]
    assert len(expired) == 1
    assert expired[0]["original_session"] == "2026-09-09"
    assert expired[0]["observed_timestamp"].startswith("2026-09-09")
    assert expired[0]["expiry_timestamp"]
    assert expired[0]["previous_delivery_state"] == "OBSERVED"
    assert expired[0]["terminal"] is True
    restarted = NGP5EarlyWatchPublisher(source.parent, state, transport=transport)
    assert restarted.process_session("2026-09-10")["expired"] == 0
    assert transport.messages == []


@pytest.mark.parametrize("active_session", ["2026-09-12", "2026-09-13"])
def test_weekend_or_no_source_run_never_sends_stale(active_session, tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-11.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(When="2026-09-11 10:37:08", Symbol="FRIDAY")])
    first = NGP5EarlyWatchPublisher(source.parent, state)
    assert first.process_session("2026-09-11")["observed"] == 1
    transport = SequenceTransport(DeliveryResult(True, 204, False))
    weekend = NGP5EarlyWatchPublisher(source.parent, state, transport=transport)
    assert weekend.process_session(active_session)["expired"] == 1
    assert transport.messages == []


def test_same_session_pending_restart_still_delivers(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="SAME_DAY")])
    assert NGP5EarlyWatchPublisher(source.parent, state).process_session("2026-09-09")[
        "observed"
    ] == 1
    transport = SequenceTransport(DeliveryResult(True, 204, False))
    restarted = NGP5EarlyWatchPublisher(source.parent, state, transport=transport)
    assert restarted.deliver_pending("2026-09-09") == 1
    assert len(transport.messages) == 1


def test_start_before_market_waits_for_authoritative_coverage(tmp_path):
    source_dir = tmp_path / "source"
    state = tmp_path / "publisher"
    publisher = NGP5EarlyWatchPublisher(source_dir, state)
    assert publisher.process_session("2026-09-09")["observed"] == 0
    assert operational_rows(state)[-1]["delivery_state"] == OPENING_COVERAGE_UNKNOWN
    source = source_dir / "live_features_2026-09-09.csv"
    write_rows(source, [row(Symbol="AFTER_COVERAGE")])
    assert publisher.process_session("2026-09-09")["observed"] == 1
    assert publisher.state["sessions"]["2026-09-09"]["opening_coverage"]["state"] == OPENING_COVERAGE_VALID


def test_start_at_0925_then_append_after_0930(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_rows(
        source,
        opening_scaffold("2026-09-09", end="09:25:00")
        + [
            row(When="2026-09-09 09:25:00", Symbol="OPEN", DayChangePct="0.5"),
        ],
    )
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 0
    append_rows(
        source,
        opening_scaffold("2026-09-09", start="09:25:30")
        + [row(When="2026-09-09 09:30:00", Symbol="FRESH")],
    )
    assert publisher.process_feature_file(source) == 1
    assert "OPEN" in publisher.state["sessions"]["2026-09-09"]["opening_leaders"]


@pytest.mark.parametrize("late_time", ["09:30:00", "10:00:00"])
def test_late_start_replays_complete_opening_rows(late_time, tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_rows(
        source,
        [
            row(When="2026-09-09 09:15:00", Symbol="SENTINEL", DayChangePct="0", DayChangeRank="201"),
            row(When="2026-09-09 09:20:00", Symbol="LEADER", DayChangePct="0.5"),
            row(When=f"2026-09-09 {late_time}", Symbol="LEADER"),
            row(When=f"2026-09-09 {late_time}", Symbol="FRESH"),
        ],
    )
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 1
    assert set(publisher.state["sessions"]["2026-09-09"]["observed"]) == {"FRESH"}


def test_midday_restart_preserves_opening_exclusion(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(
        source,
        opening_scaffold("2026-09-09")
        + [
            row(When="2026-09-09 09:20:00", Symbol="LEADER", DayChangePct="0.5"),
        ],
    )
    assert NGP5EarlyWatchPublisher(source.parent, state).process_feature_file(source) == 0
    append_rows(
        source,
        [row(When="2026-09-09 12:15:00", Symbol="LEADER"), row(When="2026-09-09 12:15:00", Symbol="NEW")],
    )
    restarted = NGP5EarlyWatchPublisher(source.parent, state)
    assert restarted.process_feature_file(source) == 1
    assert set(restarted.state["sessions"]["2026-09-09"]["observed"]) == {"NEW"}


@pytest.mark.parametrize(
    ("first_time", "metadata_present", "full_session", "expected_state"),
    [
        ("09:22:00", True, True, OPENING_COVERAGE_INVALID),
        ("10:00:00", True, True, OPENING_COVERAGE_INVALID),
        ("09:15:00", False, True, OPENING_COVERAGE_UNKNOWN),
        ("09:15:00", True, False, OPENING_COVERAGE_INVALID),
    ],
)
def test_opening_coverage_fails_closed(
    first_time, metadata_present, full_session, expected_state, tmp_path
):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    first = row(When=f"2026-09-09 {first_time}", Symbol="AMBIGUOUS")
    write_raw_rows(source, [first])
    if metadata_present:
        write_coverage(source.parent, first["When"], full_session_eligible=full_session)
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 0
    assert publisher.state["sessions"]["2026-09-09"]["observed"] == {}
    assert publisher.state["sessions"]["2026-09-09"]["opening_coverage"]["state"] == expected_state


def test_truncated_opening_prefix_fails_closed_against_metadata(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_raw_rows(source, [row(When="2026-09-09 10:00:00", Symbol="AMBIGUOUS")])
    write_coverage(source.parent, "2026-09-09 09:15:00", full_session_eligible=True)
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 0
    coverage = publisher.state["sessions"]["2026-09-09"]["opening_coverage"]
    assert coverage["state"] == OPENING_COVERAGE_INVALID
    assert "prefix" in coverage["reason"]


def test_orphan_partial_quarantines_without_advancing_or_losing_silently(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="BROKEN")], trailing_newline=False)
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 0
    cursor = publisher.state["sources"][str(source.resolve())]
    ambiguous_offset = cursor["offset"]
    append_rows(source, [row(Symbol="WOULD_HAVE_BEEN_LOST"), row(Symbol="AFTER")])
    assert publisher.process_feature_file(source) == 0
    assert cursor["offset"] == ambiguous_offset
    quarantine = [
        item for item in operational_rows(state) if item["delivery_state"] == "SOURCE_QUARANTINED"
    ]
    assert quarantine
    assert quarantine[-1]["byte_offset"] == ambiguous_offset
    assert publisher.state["sessions"]["2026-09-09"]["observed"] == {}


def test_malformed_row_stops_before_it_and_blocks_later_candidate(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_rows(source, [row(Symbol="FIRST")])
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 1
    safe_offset = publisher.state["sources"][str(source.resolve())]["offset"]
    with source.open("ab") as handle:
        handle.write(b"malformed,row\n")
    append_rows(source, [row(Symbol="BLOCKED")])
    assert publisher.process_feature_file(source) == 0
    assert publisher.state["sources"][str(source.resolve())]["offset"] == safe_offset
    assert "BLOCKED" not in publisher.state["sessions"]["2026-09-09"]["observed"]


def test_missing_required_header_fails_closed_without_cursor_advance(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    source.parent.mkdir(parents=True)
    source.write_text("When,Symbol\n2026-09-09 09:15:00,X\n")
    write_coverage(source.parent, "2026-09-09 09:15:00")
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 0
    assert publisher.state["sessions"]["2026-09-09"]["opening_coverage"]["state"] == OPENING_COVERAGE_INVALID
    assert publisher.state["sources"] == {}


def test_inode_replacement_replays_safely_and_dedupes(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="OLD")])
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 1
    replacement = tmp_path / "replacement.csv"
    write_raw_rows(
        replacement,
        opening_scaffold("2026-09-09")
        + [
            row(Symbol="OLD"),
            row(Symbol="NEW"),
        ],
    )
    os.replace(replacement, source)
    assert publisher.process_feature_file(source) == 1
    assert set(publisher.state["sessions"]["2026-09-09"]["observed"]) == {"OLD", "NEW"}


def test_truncate_below_cursor_replays_safely(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="OLD")])
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 1
    inode = source.stat().st_ino
    write_raw_rows(
        source,
        opening_scaffold("2026-09-09"),
    )
    assert source.stat().st_ino == inode
    assert publisher.process_feature_file(source) == 0
    assert publisher.state["sources"][str(source.resolve())]["offset"] == source.stat().st_size
    assert any(item["delivery_state"] == "SOURCE_RESET_SAFE_REPLAY" for item in operational_rows(state))


def test_same_inode_truncate_regrow_beyond_cursor_detected(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol=f"OLD{i}") for i in range(4)])
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 4
    old_offset = publisher.state["sources"][str(source.resolve())]["offset"]
    inode = source.stat().st_ino
    write_raw_rows(
        source,
        opening_scaffold("2026-09-09")
        + [
            row(Symbol="RECOVERED"),
            *[row(Symbol=f"FILL{i}") for i in range(12)],
        ],
    )
    assert source.stat().st_ino == inode and source.stat().st_size > old_offset
    assert publisher.process_feature_file(source) == 13
    assert "RECOVERED" in publisher.state["sessions"]["2026-09-09"]["observed"]
    assert any("truncate/regrow" in item["reason"] for item in operational_rows(state))


def test_same_inode_earlier_rewrite_preserving_tail_checkpoint_is_detected_on_restart(
    tmp_path,
):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    rows = (
        opening_scaffold("2026-09-09")
        + [row(Symbol="CAND0001", DayChangePct="0.0")]
        + [row(Symbol=f"FILL{i:04d}", DayChangePct="0.0") for i in range(80)]
    )
    write_raw_rows(source, rows)
    write_coverage(source.parent, "2026-09-09 09:15:00")
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 0
    old_inode = source.stat().st_ino
    old_cursor = publisher.state["sources"][str(source.resolve())]["offset"]
    assert old_cursor > 5_000
    old_checkpoint = source.read_bytes()[old_cursor - 512 : old_cursor]

    changed = list(rows)
    changed[len(opening_scaffold("2026-09-09"))] = row(
        Symbol="CAND0001", DayChangePct="1.0"
    )
    changed.append(row(Symbol="APPENDED", DayChangePct="0.0"))
    write_raw_rows(source, changed)
    assert source.stat().st_ino == old_inode
    assert source.stat().st_size > old_cursor
    assert source.read_bytes()[old_cursor - 512 : old_cursor] == old_checkpoint

    restarted = NGP5EarlyWatchPublisher(source.parent, state)
    assert restarted.process_feature_file(source) == 1
    assert "CAND0001" in restarted.state["sessions"]["2026-09-09"]["observed"]
    resets = [
        item
        for item in operational_rows(state)
        if item["delivery_state"] == "SOURCE_RESET_SAFE_REPLAY"
    ]
    assert resets
    assert "prefix integrity changed" in resets[-1]["reason"]


def test_normal_append_at_eof_is_incremental(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_rows(source, [row(Symbol="ONE")])
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 1
    old_offset = publisher.state["sources"][str(source.resolve())]["offset"]
    append_rows(source, [row(Symbol="TWO")])
    assert publisher.process_feature_file(source) == 1
    assert publisher.state["sources"][str(source.resolve())]["offset"] > old_offset


def test_sparse_opening_window_is_invalid_even_when_live_metadata_says_full(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_raw_rows(
        source,
        [
            row(When="2026-09-09 09:15:00", Symbol="ONLY_OPENING", DayChangePct="0"),
            row(When="2026-09-09 10:00:00", Symbol="GAP_CANDIDATE"),
        ],
    )
    write_coverage(source.parent, "2026-09-09 09:15:00")
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 0
    coverage = publisher.state["sessions"]["2026-09-09"]["opening_coverage"]
    assert coverage["state"] == OPENING_COVERAGE_INVALID
    assert coverage["metadata"]["max_gap_seconds"] == 2700
    assert coverage["metadata"]["opening_snapshot_count"] == 2


def test_opening_gap_between_0918_and_0927_is_invalid(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    rows = (
        opening_scaffold("2026-09-09", end="09:18:00")
        + opening_scaffold("2026-09-09", start="09:27:00")
        + [row(Symbol="BLOCKED")]
    )
    write_raw_rows(source, rows)
    write_coverage(source.parent, "2026-09-09 09:15:00")
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 0
    coverage = publisher.state["sessions"]["2026-09-09"]["opening_coverage"]
    assert coverage["state"] == OPENING_COVERAGE_INVALID
    assert coverage["metadata"]["max_gap_seconds"] == 540


def test_0920_seconds_match_authoritative_minute_inclusivity(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    rows = opening_scaffold(
        "2026-09-09", start="09:20:59", end="09:30:29"
    ) + [row(When="2026-09-09 10:00:00", Symbol="MINUTE_EXACT")]
    write_raw_rows(source, rows)
    write_coverage(source.parent, "2026-09-09 09:20:59")
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 1
    assert publisher.state["sessions"]["2026-09-09"]["opening_coverage"]["state"] == OPENING_COVERAGE_VALID


def test_historical_coverage_fallback_requires_authoritative_continuity(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_raw_rows(source, opening_scaffold("2026-09-09") + [row(Symbol="HISTORICAL")])
    history = tmp_path / "full_universe_rank_strategy" / "session_coverage.csv"
    history.parent.mkdir()
    fields = [
        "date", "first_snapshot", "snapshots", "max_gap_seconds",
        "required_max_gap_seconds", "required_min_snapshots", "continuity_ok",
        "recorder_generation", "full_session_eligible",
    ]
    with history.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "date": "2026-09-09", "first_snapshot": "2026-09-09 09:15:00",
                "snapshots": "1300", "max_gap_seconds": "30",
                "required_max_gap_seconds": "45", "required_min_snapshots": "1000",
                "continuity_ok": "True", "recorder_generation": "6",
                "full_session_eligible": "True",
            }
        )
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 1
    assert publisher.state["sessions"]["2026-09-09"]["opening_coverage"]["state"] == OPENING_COVERAGE_VALID


def test_daily_rotation_and_same_symbol_new_session(tmp_path):
    source9 = tmp_path / "source" / "live_features_2026-09-09.csv"
    source10 = tmp_path / "source" / "live_features_2026-09-10.csv"
    state = tmp_path / "publisher"
    write_rows(source9, [row(Symbol="REPEAT")])
    publisher = NGP5EarlyWatchPublisher(source9.parent, state)
    assert publisher.process_feature_file(source9) == 1
    write_rows(source10, [row(When="2026-09-10 10:37:08", Symbol="REPEAT")])
    assert publisher.process_feature_file(source10) == 1
    first = publisher.state["sessions"]["2026-09-09"]["observed"]["REPEAT"]
    second = publisher.state["sessions"]["2026-09-10"]["observed"]["REPEAT"]
    assert first["event_id"] != second["event_id"]


def test_schema_widening_via_replacement_preserves_required_contract(tmp_path):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="BEFORE_WIDEN")])
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    assert publisher.process_feature_file(source) == 1
    replacement = tmp_path / "replacement.csv"
    widened = HEADER + ["FutureContextField"]
    rows = opening_scaffold("2026-09-09") + [row(Symbol="AFTER_WIDEN")]
    with replacement.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=widened, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{**item, "FutureContextField": "ignored"} for item in rows])
    os.replace(replacement, source)
    assert publisher.process_feature_file(source) == 1
    assert set(publisher.state["sessions"]["2026-09-09"]["observed"]) == {
        "BEFORE_WIDEN", "AFTER_WIDEN"
    }


def test_midnight_row_cannot_cross_session_filename_boundary(tmp_path):
    assert evaluate_p5(row(When="2026-09-10 00:00:00"), set()).qualifies is False
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    write_raw_rows(
        source,
        opening_scaffold("2026-09-09")
        + [row(When="2026-09-10 00:00:00", Symbol="WRONG_SESSION")],
    )
    write_coverage(source.parent, "2026-09-09 09:15:00")
    publisher = NGP5EarlyWatchPublisher(source.parent, tmp_path / "publisher")
    assert publisher.process_feature_file(source) == 0
    quarantine = [
        item for item in operational_rows(tmp_path / "publisher")
        if item["delivery_state"] == "SOURCE_QUARANTINED"
    ]
    assert quarantine and "does not match source session" in quarantine[-1]["reason"]


def test_default_cli_configuration_never_scans_upgrade_source(tmp_path, monkeypatch):
    assert _parser().parse_args([]).enable_upgrades is False
    monkeypatch.delenv("NG_EARLY_WATCH_DISCORD_WEBHOOK_URL", raising=False)
    called = []
    monkeypatch.setattr(
        NGP5EarlyWatchPublisher,
        "process_upgrade_file",
        lambda self, path: called.append(path) or 0,
    )
    assert cli_main(
        [
            "--once",
            "--session-date", "2026-09-09",
            "--source-dir", str(tmp_path / "source"),
            "--state-dir", str(tmp_path / "state"),
        ]
    ) == 0
    assert called == []


@pytest.mark.parametrize("bad_url", ["not-a-url", "ftp://example.test/x", "https:///missing-host"])
def test_malformed_webhook_url_is_rejected_at_startup(bad_url, tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        validate_webhook_url(bad_url)
    monkeypatch.setenv("NG_EARLY_WATCH_DISCORD_WEBHOOK_URL", bad_url)
    assert cli_main(["--once", "--state-dir", str(tmp_path / "state")]) == 2


@pytest.mark.parametrize("exception", [ValueError("bad client"), Exception("unexpected"), TimeoutError("timeout"), urllib.error.URLError("dns")])
def test_unexpected_and_network_exceptions_are_bounded(exception, tmp_path):
    transport = RaisingTransport(exception)
    publisher = _queued_publisher(tmp_path, transport, max_attempts=2)
    assert publisher.deliver_pending("2026-09-09") == 0
    assert publisher.deliver_pending("2026-09-09") == 0
    assert publisher.deliver_pending("2026-09-09") == 0
    assert len(transport.messages) == 2
    assert publisher.state["pending"] == {}
    final = journal_rows(tmp_path / "publisher")[-1]
    assert final["delivery_state"] == "FAILED" and final["terminal"] is True
    assert final["delivery_attempts"] == 2


@pytest.mark.parametrize("status", [400, 401, 403])
def test_permanent_http_4xx_variants_are_terminal(status, tmp_path):
    transport = SequenceTransport(DeliveryResult(False, status, False, error=f"http_{status}"))
    publisher = _queued_publisher(tmp_path, transport)
    assert publisher.deliver_pending("2026-09-09") == 0
    assert len(transport.messages) == 1
    assert publisher.state["pending"] == {}


@pytest.mark.parametrize(
    ("status", "retryable"), [(500, True), (400, False), (401, False), (403, False)]
)
def test_actual_http_adapter_classifies_http_errors(status, retryable, monkeypatch):
    error = urllib.error.HTTPError(
        "https://discord.test/webhook", status, "error", {}, io.BytesIO(b"{}")
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    result = DiscordWebhookTransport("https://discord.test/webhook").post("message")
    assert result.delivered is False
    assert result.http_status == status
    assert result.retryable is retryable
    assert result.error == f"http_{status}"


def test_actual_http_adapter_parses_429_retry_after_header(monkeypatch):
    error = urllib.error.HTTPError(
        "https://discord.test/webhook",
        429,
        "rate limited",
        {"Retry-After": "2.5"},
        io.BytesIO(b"{}"),
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    result = DiscordWebhookTransport("https://discord.test/webhook").post("message")
    assert result.http_status == 429
    assert result.retryable is True
    assert result.retry_after_seconds == 2.5


def test_actual_http_adapter_uses_discord_wait_and_stable_user_agent(monkeypatch):
    captured = {}

    class CreatedMessageResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"id":"123456789"}'

    def fake_urlopen(request, **kwargs):
        captured["request"] = request
        captured["timeout"] = kwargs.get("timeout")
        return CreatedMessageResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = DiscordWebhookTransport(
        "https://discord.test/webhook?existing=value",
        timeout_seconds=7,
    ).post("TEST ONLY")
    request = captured["request"]
    assert request.get_method() == "POST"
    assert "existing=value" in request.full_url
    assert "wait=true" in request.full_url
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("User-agent") == "NG-Quality-Watch-Publisher/1.0"
    assert json.loads(request.data.decode("utf-8")) == {"content": "TEST ONLY"}
    assert captured["timeout"] == 7
    assert result.delivered is True
    assert result.http_status == 200
    assert result.message_id == "123456789"


def test_malformed_http_adapter_response_is_contained_by_delivery_state_machine(
    tmp_path, monkeypatch
):
    class MalformedResponse:
        status = "not-an-integer"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_args, **_kwargs: MalformedResponse()
    )
    publisher = _queued_publisher(
        tmp_path,
        DiscordWebhookTransport("https://discord.test/webhook"),
        max_attempts=1,
    )
    assert publisher.deliver_pending("2026-09-09") == 0
    final = journal_rows(tmp_path / "publisher")[-1]
    assert final["delivery_state"] == "FAILED"
    assert final["failure"] == "unexpected_transport_exception:ValueError"
    assert final["terminal"] is True


def test_attempted_before_transport_crash_is_restart_recoverable(tmp_path):
    class ProcessDeath(BaseException):
        pass

    transport = RaisingTransport(ProcessDeath())
    publisher = _queued_publisher(tmp_path, transport, backoff=0)
    with pytest.raises(ProcessDeath):
        publisher.deliver_pending("2026-09-09")
    assert journal_rows(tmp_path / "publisher")[-1]["delivery_state"] == "ATTEMPTED"
    success = SequenceTransport(DeliveryResult(True, 204, False))
    restarted = NGP5EarlyWatchPublisher(
        tmp_path / "source", tmp_path / "publisher", transport=success, retry_backoff_seconds=0
    )
    assert restarted.deliver_pending("2026-09-09") == 1


def test_crash_before_observed_does_not_advance_cursor_and_restart_recovers(
    tmp_path, monkeypatch
):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="BEFORE_OBSERVED")])
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    original_append = publisher._append_evidence

    def crash_before_write(session_date, payload):
        if payload.get("delivery_state") == "OBSERVED":
            raise KeyboardInterrupt("crash before OBSERVED")
        return original_append(session_date, payload)

    monkeypatch.setattr(publisher, "_append_evidence", crash_before_write)
    with pytest.raises(KeyboardInterrupt):
        publisher.process_feature_file(source)
    restarted = NGP5EarlyWatchPublisher(source.parent, state)
    assert restarted.process_feature_file(source) == 1
    observed = [item for item in journal_rows(state) if item["delivery_state"] == "OBSERVED"]
    assert len(observed) == 1


def test_crash_after_observed_before_state_save_reconciles_without_duplicate(
    tmp_path, monkeypatch
):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="AFTER_OBSERVED")])
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    original_append = publisher._append_evidence

    def write_then_crash(session_date, payload):
        original_append(session_date, payload)
        if payload.get("delivery_state") == "OBSERVED":
            raise KeyboardInterrupt("crash after OBSERVED")

    monkeypatch.setattr(publisher, "_append_evidence", write_then_crash)
    with pytest.raises(KeyboardInterrupt):
        publisher.process_feature_file(source)
    restarted = NGP5EarlyWatchPublisher(source.parent, state)
    assert "AFTER_OBSERVED" in restarted.state["sessions"]["2026-09-09"]["observed"]
    assert len(restarted.state["pending"]) == 1
    assert restarted.process_feature_file(source) == 0
    observed = [item for item in journal_rows(state) if item["delivery_state"] == "OBSERVED"]
    assert len(observed) == 1


def test_crash_after_state_save_keeps_observation_and_cursor_recoverable(
    tmp_path, monkeypatch
):
    source = tmp_path / "source" / "live_features_2026-09-09.csv"
    state = tmp_path / "publisher"
    write_rows(source, [row(Symbol="AFTER_STATE")])
    publisher = NGP5EarlyWatchPublisher(source.parent, state)
    original_save = publisher._save_state

    def save_then_crash():
        original_save()
        raise KeyboardInterrupt("crash after state save")

    monkeypatch.setattr(publisher, "_save_state", save_then_crash)
    with pytest.raises(KeyboardInterrupt):
        publisher.process_feature_file(source)
    restarted = NGP5EarlyWatchPublisher(source.parent, state)
    assert "AFTER_STATE" in restarted.state["sessions"]["2026-09-09"]["observed"]
    assert len(restarted.state["pending"]) == 1
    assert restarted.process_feature_file(source) == 0
    observed = [item for item in journal_rows(state) if item["delivery_state"] == "OBSERVED"]
    assert len(observed) == 1


def test_http_accepted_before_delivered_persistence_retries_duplicate(tmp_path, monkeypatch):
    transport = SequenceTransport(DeliveryResult(True, 204, False))
    publisher = _queued_publisher(tmp_path, transport)
    original = publisher._append_evidence

    def crash_before_delivered(day, payload):
        if payload.get("delivery_state") == "DELIVERED":
            raise RuntimeError("crash before DELIVERED persistence")
        return original(day, payload)

    monkeypatch.setattr(publisher, "_append_evidence", crash_before_delivered)
    with pytest.raises(RuntimeError):
        publisher.deliver_pending("2026-09-09")
    duplicate = SequenceTransport(DeliveryResult(True, 204, False))
    restarted = NGP5EarlyWatchPublisher(
        tmp_path / "source", tmp_path / "publisher", transport=duplicate, retry_backoff_seconds=0
    )
    assert restarted.deliver_pending("2026-09-09") == 1
    assert len(transport.messages) == len(duplicate.messages) == 1


def test_delivered_persisted_before_queue_removal_does_not_retry(tmp_path, monkeypatch):
    transport = SequenceTransport(DeliveryResult(True, 204, False))
    publisher = _queued_publisher(tmp_path, transport)
    original = publisher._append_evidence

    def crash_after_delivered(day, payload):
        result = original(day, payload)
        if payload.get("delivery_state") == "DELIVERED":
            raise RuntimeError("crash after DELIVERED persistence")
        return result

    monkeypatch.setattr(publisher, "_append_evidence", crash_after_delivered)
    with pytest.raises(RuntimeError):
        publisher.deliver_pending("2026-09-09")
    no_duplicate = SequenceTransport(DeliveryResult(True, 204, False))
    restarted = NGP5EarlyWatchPublisher(
        tmp_path / "source", tmp_path / "publisher", transport=no_duplicate
    )
    assert restarted.deliver_pending("2026-09-09") == 0
    assert no_duplicate.messages == []

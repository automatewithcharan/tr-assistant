import json
from datetime import datetime, timedelta, timezone

from pkscreener.classes.MarginQualifiedLeverageObserver import (
    MarginQualifiedLeverageObserver,
)


IST = timezone(timedelta(hours=5, minutes=30))


def _candidate(source="stop"):
    return {
        "event": "entry",
        "EventId": f"base:{source}:AAA",
        "Date": "2026-07-30",
        "EntryAt": "2026-07-30T09:20:00+05:30",
        "EntryTs": datetime(2026, 7, 30, 9, 20, tzinfo=IST).timestamp(),
        "RecorderGeneration": 3,
        "Exchange": "NSE",
        "Symbol": "AAA",
        "Cohort": "score_ge_0.78",
        "Source": source,
        "EntryAsk": 100.0,
        "EntryBid": 99.9,
        "ExpectedReturnPct": 0.9,
        "MoveBand": "1-2",
        "ModelTrainedThrough": "2026-07-29",
        "ModelSourceSha256": "abc123",
    }


def _quote(quoted_at):
    return {
        "QuotedAt": quoted_at.isoformat(timespec="seconds"),
        "Symbol": "AAA",
        "Price": 100.0,
        "MarginPct": 20.0,
        "EffectiveLeverage": 5.0,
        "Status": "quoted",
    }


def _tick():
    return {
        "depth": {
            "sell": [
                {"price": 100.0, "quantity": 5000, "orders": 10},
                {"price": 100.1, "quantity": 5000, "orders": 10},
            ],
            "buy": [
                {"price": 99.9, "quantity": 5000, "orders": 10},
            ],
        }
    }


def test_leverage_entry_uses_only_depth_after_margin_quote(tmp_path):
    now = datetime(2026, 7, 30, 9, 20, tzinfo=IST)
    observer = MarginQualifiedLeverageObserver(
        tmp_path,
        lane_capital=250_000,
    )

    same_tick = observer.process(
        [_candidate()],
        [_quote(now)],
        {"AAA": _tick()},
        now=now,
    )
    later = observer.process(
        [],
        [],
        {"AAA": _tick()},
        now=now + timedelta(seconds=5),
    )

    assert same_tick == []
    assert len(later) == 2
    assert {row["NotionalMultiplier"] for row in later} == {1.25, 1.5}
    assert all(row["event"] == "entry" for row in later)
    assert all(row["EntryAt"] > row["MarginQuotedAt"] for row in later)
    assert all(row["RequiredMarginEstimate"] <= 250_000 for row in later)
    assert all(row["ExecutionSchemaVersion"] == 2 for row in later)
    assert all("post_margin" in row["EventId"] for row in later)

    status = json.loads(
        (
            tmp_path / "margin_qualified_leverage" / "status.json"
        ).read_text()
    )
    assert status["paper_only"] is True
    assert status["alerts_allowed"] is False
    assert status["real_orders_allowed"] is False


def test_leverage_observer_rejects_margin_insufficient_variant(tmp_path):
    now = datetime(2026, 7, 30, 9, 20, tzinfo=IST)
    observer = MarginQualifiedLeverageObserver(
        tmp_path,
        lane_capital=100.0,
        multipliers=(1.25,),
    )
    high_margin = _quote(now)
    high_margin["MarginPct"] = 110.0

    observer.process(
        [_candidate()],
        [high_margin],
        {"AAA": _tick()},
        now=now,
    )
    rows = observer.process(
        [],
        [],
        {"AAA": _tick()},
        now=now + timedelta(seconds=5),
    )

    assert rows == []


def test_leverage_observer_restores_armed_candidate_without_lookahead(
    tmp_path,
):
    now = datetime(2026, 7, 30, 9, 20, tzinfo=IST)
    first = MarginQualifiedLeverageObserver(tmp_path)
    first.process(
        [_candidate()],
        [_quote(now)],
        {"AAA": _tick()},
        now=now,
    )

    restarted = MarginQualifiedLeverageObserver(tmp_path)
    rows = restarted.process(
        [],
        [],
        {"AAA": _tick()},
        now=now + timedelta(seconds=5),
    )

    assert len(rows) == 2
    assert all(row["EntryAt"] > row["MarginQuotedAt"] for row in rows)

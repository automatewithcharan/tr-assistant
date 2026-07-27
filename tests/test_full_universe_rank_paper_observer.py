import json
import csv
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from pkscreener.classes.FullUniverseRankPaperObserver import (
    FullUniverseRankPaperObserver,
)


def _tick(bid=100.0, ask=100.1, price=100.05):
    return {
        "last_price": price,
        "depth": {
            "buy": [{"price": bid, "quantity": 1000}],
            "sell": [{"price": ask, "quantity": 1000}],
        },
    }


def _rows(**overrides):
    row = {
        "Symbol": "AAA",
        "RecorderGeneration": 5,
        "LastPrice": 100.05,
        "DayChangePct": 1.2,
        "DayChangeRank": 12,
        "PreviousDayChangeRank": 80,
        "RankImprovement": 68,
        "RankVelocityPerMinute": 68.0,
        "DayChangeDeltaPct": 0.4,
        "TradedValue": 50000000,
        "BuyerSellerRatio": 2.0,
        "SectorBreadthPct": 60.0,
        "MarketBreadthPct": 52.0,
        "SpreadOk": True,
        "RecordReason": "full_universe_leader_pool|rapid_rank_climber",
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_registers_only_buyable_rank_cohort_at_observed_ask(tmp_path):
    observer = FullUniverseRankPaperObserver(str(tmp_path))
    now = datetime(2026, 7, 22, 9, 20)

    events = observer.process(_rows(), {"AAA": _tick()}, now=now)

    assert len(events) == 1
    assert events[0]["event"] == "entry"
    assert events[0]["EntryAsk"] == 100.1
    assert events[0]["EntryBid"] == 100.0
    assert events[0]["RecorderGeneration"] == 5
    assert ":g5:" in events[0]["EventId"]
    assert events[0]["Cohort"] == "rapid_rank_climber:r11_25:m1_2"
    assert events[0]["Quantity"] == int(500000 // 100.1)
    assert events[0]["EntryAskQty"] == 1000
    assert events[0]["EntryTopLevelFillable"] is False
    assert json.loads(observer.status_path.read_text())["real_orders_allowed"] is False

    rejected = _rows(Symbol="CHEAP", LastPrice=20.0)
    wide = _rows(Symbol="WIDE")
    events = observer.process(
        pd.concat([rejected, wide], ignore_index=True),
        {"CHEAP": _tick(price=20.0, bid=19.9, ask=20.1), "WIDE": _tick(bid=99.0, ask=101.0)},
        now=now + timedelta(minutes=1),
    )
    assert events == []


def test_cohorts_keep_discovery_and_outside_pool_ranks_distinct(tmp_path):
    observer = FullUniverseRankPaperObserver(str(tmp_path))

    assert observer._cohort(
        "leader_pool", {"DayChangeRank": 85, "DayChangePct": 0.8}
    ) == "leader_pool:r76_100:m0_5_1"
    assert observer._cohort(
        "leader_pool", {"DayChangeRank": 150, "DayChangePct": 1.2}
    ) == "leader_pool:r101_200:m1_2"
    assert observer._cohort(
        "rapid_rank_climber",
        {"DayChangeRank": 634, "DayChangePct": 0.8},
    ) == "rapid_rank_climber:r201_plus:m0_5_1"


def test_target_exit_uses_observed_bid_and_applies_stress(tmp_path):
    observer = FullUniverseRankPaperObserver(str(tmp_path))
    start = datetime(2026, 7, 22, 9, 20)
    observer.process(_rows(), {"AAA": _tick()}, now=start)

    events = observer.process(
        pd.DataFrame(), {"AAA": _tick(bid=101.7, ask=101.8)}, now=start + timedelta(minutes=5)
    )

    assert events[0]["ExitBidQty"] == 1000
    assert events[0]["ExitTopLevelFillable"] is False

    assert len(events) == 1
    outcome = events[0]
    assert outcome["ExitReason"] == "target"
    assert outcome["ExitBid"] == 101.7
    assert outcome["NetReturnPct"] == pytest.approx(
        (101.7 - 100.1) / 100.1 * 100 - 0.1, abs=1e-5
    )
    assert outcome["NetPnl"] < outcome["GrossPnl"]
    assert not observer.active


def test_stop_and_timeout_use_current_bid(tmp_path):
    stop_observer = FullUniverseRankPaperObserver(str(tmp_path / "stop"))
    start = datetime(2026, 7, 22, 9, 20)
    stop_observer.process(_rows(), {"AAA": _tick()}, now=start)
    stopped = stop_observer.process(
        pd.DataFrame(), {"AAA": _tick(bid=99.0, ask=99.1)}, now=start + timedelta(minutes=3)
    )
    assert stopped[0]["ExitReason"] == "stop"
    assert stopped[0]["ExitBid"] == 99.0

    timeout_observer = FullUniverseRankPaperObserver(str(tmp_path / "timeout"))
    timeout_observer.process(_rows(), {"AAA": _tick()}, now=start)
    timed_out = timeout_observer.process(
        pd.DataFrame(), {"AAA": _tick(bid=100.4, ask=100.5)}, now=start + timedelta(minutes=61)
    )
    assert timed_out[0]["ExitReason"] == "timeout"
    assert timed_out[0]["ExitBid"] == 100.4


def test_state_reload_preserves_active_and_prevents_duplicate(tmp_path):
    start = datetime(2026, 7, 22, 9, 20)
    first = FullUniverseRankPaperObserver(str(tmp_path))
    first.process(_rows(), {"AAA": _tick()}, now=start)

    restarted = FullUniverseRankPaperObserver(str(tmp_path))
    duplicate = restarted.process(
        _rows(), {"AAA": _tick()}, now=start + timedelta(minutes=1)
    )

    assert duplicate == []
    assert len(restarted.active) == 1
    entries = pd.read_csv(restarted._entries_path(start))
    assert len(entries) == 1


def test_entry_ledger_widens_old_same_day_schema_atomically(tmp_path):
    observer = FullUniverseRankPaperObserver(str(tmp_path))
    now = datetime(2026, 7, 22, 9, 20)
    path = observer._entries_path(now)
    path.parent.mkdir(parents=True)
    old_columns = [
        column for column in observer.ENTRY_COLUMNS
        if column not in {"Sector", "UniverseSize", "OpenChangePct"}
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(old_columns)
        writer.writerow(["OLD" if column == "Symbol" else "" for column in old_columns])

    observer.process(_rows(), {"AAA": _tick()}, now=now)

    with path.open("r", encoding="utf-8", newline="") as handle:
        saved_rows = list(csv.reader(handle))
    assert saved_rows[0] == observer.ENTRY_COLUMNS
    assert {len(row) for row in saved_rows} == {len(observer.ENTRY_COLUMNS)}
    saved = pd.read_csv(path)
    assert saved["Symbol"].tolist() == ["OLD", "AAA"]


def test_eod_closes_open_event_and_component_has_no_broker_path(tmp_path):
    observer = FullUniverseRankPaperObserver(str(tmp_path))
    start = datetime(2026, 7, 22, 15, 20)
    observer.process(_rows(), {"AAA": _tick()}, now=start)
    events = observer.process(
        pd.DataFrame(), {"AAA": _tick(bid=100.2, ask=100.3)}, now=datetime(2026, 7, 22, 15, 25)
    )
    assert events[0]["ExitReason"] == "eod"

    source = Path(
        "pkscreener/classes/FullUniverseRankPaperObserver.py"
    ).read_text(encoding="utf-8")
    assert "KiteOrderManager" not in source
    assert "send_discord" not in source.lower()

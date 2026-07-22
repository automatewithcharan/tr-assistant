import json
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
    assert events[0]["Cohort"] == "rapid_rank_climber:r11_25:m1_2"
    assert events[0]["Quantity"] == int(500000 // 100.1)
    assert json.loads(observer.status_path.read_text())["real_orders_allowed"] is False

    rejected = _rows(Symbol="CHEAP", LastPrice=20.0)
    wide = _rows(Symbol="WIDE")
    events = observer.process(
        pd.concat([rejected, wide], ignore_index=True),
        {"CHEAP": _tick(price=20.0, bid=19.9, ask=20.1), "WIDE": _tick(bid=99.0, ask=101.0)},
        now=now + timedelta(minutes=1),
    )
    assert events == []


def test_target_exit_uses_observed_bid_and_applies_stress(tmp_path):
    observer = FullUniverseRankPaperObserver(str(tmp_path))
    start = datetime(2026, 7, 22, 9, 20)
    observer.process(_rows(), {"AAA": _tick()}, now=start)

    events = observer.process(
        pd.DataFrame(), {"AAA": _tick(bid=101.7, ask=101.8)}, now=start + timedelta(minutes=5)
    )

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

from datetime import datetime, timedelta
import json
from pathlib import Path

import pandas as pd

from pkscreener.classes.FullUniverseRankEntryTimingObserver import (
    ENTRY_VARIANTS,
    FullUniverseRankEntryTimingObserver,
)


def _tick(bid, ask, quantity=10000):
    return {
        "depth": {
            "buy": [{"price": bid, "quantity": quantity}],
            "sell": [{"price": ask, "quantity": quantity}],
        }
    }


def _entry(now):
    return {
        "event": "entry",
        "EventId": f"{now:%Y-%m-%d}:AAA:cohort",
        "Date": now.strftime("%Y-%m-%d"),
        "EntryAt": now.strftime("%Y-%m-%d %H:%M:%S"),
        "EntryTs": now.timestamp(),
        "RecorderGeneration": 5,
        "Symbol": "AAA",
        "Cohort": "rapid:r1_10:m1_2",
        "Source": "rapid_rank_climber",
        "EntryAsk": 100.0,
    }


def test_default_timing_matrix_registers_all_variants(tmp_path):
    observer = FullUniverseRankEntryTimingObserver(str(tmp_path))
    now = datetime(2026, 7, 22, 9, 20)

    events = observer.process([_entry(now)], {"AAA": _tick(99.9, 100.0)}, now=now)

    assert events == []
    assert len(observer.waiting) == len(ENTRY_VARIANTS)
    status = pd.read_json(observer.status_path, typ="series")
    assert status["variants"] == len(ENTRY_VARIANTS)
    assert status["real_orders_allowed"] is False
    registrations = pd.read_csv(
        observer.root / "registrations_2026-07-22.csv"
    )
    assert set(registrations["RecorderGeneration"].tolist()) == {5}


def test_dip_uses_conservative_limit_and_confirmation_uses_observed_ask(tmp_path):
    variants = {
        "dip": {"mode": "dip", "offset_pct": 0.5, "entry_window_minutes": 30},
        "confirm": {"mode": "confirm", "offset_pct": 0.25, "entry_window_minutes": 15},
    }
    observer = FullUniverseRankEntryTimingObserver(str(tmp_path), variants=variants)
    now = datetime(2026, 7, 22, 9, 20)
    observer.process([_entry(now)], {"AAA": _tick(99.9, 100.0)}, now=now)

    dip = observer.process([], {"AAA": _tick(99.35, 99.4)}, now=now + timedelta(minutes=2))
    confirm = observer.process([], {"AAA": _tick(100.25, 100.3)}, now=now + timedelta(minutes=4))

    assert dip[0]["event"] == "entry_filled"
    assert dip[0]["Variant"] == "dip"
    assert dip[0]["EntryAsk"] == 99.5
    assert dip[0]["RecorderGeneration"] == 5
    assert confirm[0]["Variant"] == "confirm"
    assert confirm[0]["EntryAsk"] == 100.3
    assert confirm[0]["RecorderGeneration"] == 5


def test_trigger_waits_until_displayed_ask_quantity_can_fill_order(tmp_path):
    variants = {
        "confirm": {
            "mode": "confirm",
            "offset_pct": 0.25,
            "entry_window_minutes": 15,
        },
    }
    observer = FullUniverseRankEntryTimingObserver(
        str(tmp_path), variants=variants
    )
    now = datetime(2026, 7, 22, 9, 20)
    observer.process(
        [_entry(now)],
        {"AAA": _tick(99.9, 100.0)},
        now=now,
    )

    thin = observer.process(
        [],
        {"AAA": _tick(100.25, 100.3, quantity=100)},
        now=now + timedelta(minutes=2),
    )
    fillable = observer.process(
        [],
        {"AAA": _tick(100.25, 100.3, quantity=10000)},
        now=now + timedelta(minutes=3),
    )

    assert thin == []
    assert fillable[0]["event"] == "entry_filled"
    assert fillable[0]["EntryAskQty"] == 10000
    assert fillable[0]["RequestedQuantity"] == 4985
    assert fillable[0]["EntryTopLevelFillable"] is True
    assert fillable[0]["ExecutionSchemaVersion"] == 2
    assert fillable[0]["EntryDepthFillable"] is True
    assert len(json.loads(fillable[0]["EntryDepthJson"])) == 1


def test_pullback_reclaim_requires_two_distinct_phases(tmp_path):
    variants = {
        "reclaim": {
            "mode": "pullback_reclaim",
            "offset_pct": 0.5,
            "reclaim_pct": 0.25,
            "entry_window_minutes": 30,
        }
    }
    observer = FullUniverseRankEntryTimingObserver(str(tmp_path), variants=variants)
    now = datetime(2026, 7, 22, 9, 20)
    observer.process([_entry(now)], {"AAA": _tick(99.9, 100.0)}, now=now)
    pullback = observer.process([], {"AAA": _tick(99.35, 99.4)}, now=now + timedelta(minutes=2))
    still_low = observer.process([], {"AAA": _tick(99.25, 99.3)}, now=now + timedelta(minutes=3))
    reclaimed = observer.process([], {"AAA": _tick(99.55, 99.6)}, now=now + timedelta(minutes=4))

    assert pullback == []
    assert still_low == []
    assert reclaimed[0]["event"] == "entry_filled"
    assert reclaimed[0]["EntryAsk"] == 99.6


def test_filled_variant_exits_at_bid_and_unfilled_variant_expires(tmp_path):
    variants = {
        "dip": {"mode": "dip", "offset_pct": 0.5, "entry_window_minutes": 30},
        "confirm": {"mode": "confirm", "offset_pct": 1.0, "entry_window_minutes": 15},
    }
    observer = FullUniverseRankEntryTimingObserver(str(tmp_path), variants=variants)
    now = datetime(2026, 7, 22, 9, 20)
    observer.process([_entry(now)], {"AAA": _tick(99.9, 100.0)}, now=now)
    observer.process([], {"AAA": _tick(99.35, 99.4)}, now=now + timedelta(minutes=2))
    expired = observer.process([], {"AAA": _tick(99.8, 99.9)}, now=now + timedelta(minutes=16))
    exited = observer.process([], {"AAA": _tick(101.1, 101.2)}, now=now + timedelta(minutes=20))

    assert expired[0]["event"] == "entry_unfilled"
    assert expired[0]["Variant"] == "confirm"
    assert exited[0]["event"] == "timing_exit"
    assert exited[0]["ExitReason"] == "target"
    assert exited[0]["ExitBid"] == 101.1
    assert exited[0]["ExitDepthFillable"] is True
    assert len(json.loads(exited[0]["ExitDepthJson"])) == 1
    assert exited[0]["RecorderGeneration"] == 5


def test_legacy_waiting_and_position_without_generation_still_resolve(tmp_path):
    variants = {
        "dip": {
            "mode": "dip",
            "offset_pct": 0.5,
            "entry_window_minutes": 30,
        },
        "confirm": {
            "mode": "confirm",
            "offset_pct": 1.0,
            "entry_window_minutes": 15,
        },
    }
    observer = FullUniverseRankEntryTimingObserver(
        str(tmp_path), variants=variants
    )
    now = datetime(2026, 7, 22, 9, 20)
    observer.process([_entry(now)], {"AAA": _tick(99.9, 100.0)}, now=now)
    for candidate in observer.waiting.values():
        candidate.pop("RecorderGeneration")
    filled = observer.process(
        [], {"AAA": _tick(99.35, 99.4)}, now=now + timedelta(minutes=2)
    )
    assert filled[0]["RecorderGeneration"] == 0

    expired = observer.process(
        [], {"AAA": _tick(99.8, 99.9)}, now=now + timedelta(minutes=16)
    )
    assert expired[0]["RecorderGeneration"] == 0

    position = next(iter(observer.positions.values()))
    position.pop("RecorderGeneration", None)
    exited = observer.process(
        [], {"AAA": _tick(101.1, 101.2)}, now=now + timedelta(minutes=20)
    )
    assert exited[0]["RecorderGeneration"] == 0


def test_restart_preserves_waiting_positions_and_prevents_duplicate(tmp_path):
    now = datetime(2026, 7, 22, 9, 20)
    first = FullUniverseRankEntryTimingObserver(str(tmp_path))
    first.process([_entry(now)], {"AAA": _tick(99.9, 100.0)}, now=now)

    restarted = FullUniverseRankEntryTimingObserver(str(tmp_path))
    restarted.process([_entry(now)], {"AAA": _tick(99.9, 100.0)}, now=now + timedelta(minutes=1))

    assert len(restarted.waiting) == len(ENTRY_VARIANTS)
    registrations = pd.read_csv(restarted.root / "registrations_2026-07-22.csv")
    assert len(registrations) == len(ENTRY_VARIANTS)


def test_component_has_no_broker_or_alert_path():
    source = Path(
        "pkscreener/classes/FullUniverseRankEntryTimingObserver.py"
    ).read_text(encoding="utf-8")
    assert "KiteOrderManager" not in source
    assert "send_discord" not in source.lower()

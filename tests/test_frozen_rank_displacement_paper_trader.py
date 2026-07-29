import json
from datetime import datetime, timedelta, timezone

from pkscreener.classes.FrozenRankDisplacementPaperTrader import (
    FrozenRankDisplacementPaperTrader,
)
from pkscreener.classes.OrderBookExecution import depth_json


IST = timezone(timedelta(hours=5, minutes=30))


def _tick(bid=100.0, ask=100.1, quantity=10_000):
    return {
        "depth": {
            "buy": [
                {"price": bid, "quantity": quantity, "orders": 10},
                {"price": bid - 0.05, "quantity": quantity, "orders": 10},
            ],
            "sell": [
                {"price": ask, "quantity": quantity, "orders": 10},
                {"price": ask + 0.05, "quantity": quantity, "orders": 10},
            ],
        }
    }


def _candidate(symbol, score, now, price=100.0, quantity=5_000):
    levels = [
        {"price": price, "quantity": quantity, "orders": 10},
        {"price": price + 0.05, "quantity": quantity, "orders": 10},
    ]
    return {
        "event": "entry",
        "EventId": f"{now.date()}:{symbol}:1-2:model",
        "Date": str(now.date()),
        "EntryAt": now.isoformat(timespec="seconds"),
        "Symbol": symbol,
        "RecorderGeneration": 6,
        "Exchange": "NSE",
        "Source": "prior_session_rank_migration_stop_avoidance",
        "ModelTrainedThrough": "2026-07-29",
        "ModelSourceSha256": "model-hash",
        "ExpectedReturnPct": score,
        "Quantity": quantity,
        "EntryDepthJson": depth_json(levels),
        "EntryDepthFillable": True,
        "EntryDepthVwap": price,
    }


def test_displaces_only_after_fixed_hold_and_score_advantage(tmp_path):
    trader = FrozenRankDisplacementPaperTrader(tmp_path, persist=False)
    start = datetime(2026, 7, 30, 9, 20, tzinfo=IST)

    entered = trader.process(
        [_candidate("AAA", 0.80, start)],
        {"AAA": _tick()},
        now=start,
    )
    too_soon = trader.process(
        [_candidate("BBB", 0.90, start + timedelta(minutes=2))],
        {"AAA": _tick(bid=100.2), "BBB": _tick()},
        now=start + timedelta(minutes=2),
    )
    displaced = trader.process(
        [_candidate("BBB", 0.86, start + timedelta(minutes=4))],
        {"AAA": _tick(bid=100.2), "BBB": _tick()},
        now=start + timedelta(minutes=4),
    )

    assert [row["event"] for row in entered] == ["entry"]
    assert too_soon == []
    assert [row["event"] for row in displaced] == ["exit", "entry"]
    assert displaced[0]["reason"] == "rank_displaced"
    assert displaced[0]["symbol"] == "AAA"
    assert displaced[0]["other_symbol"] == "BBB"
    assert trader.trades[0]["exit_depth_fillable"] is True
    assert trader.trades[0]["net_pnl"] > 0
    assert trader.trades[1]["symbol"] == "BBB"
    assert trader.trades[1]["closed"] is False


def test_does_not_displace_incumbent_already_above_progress_ceiling(tmp_path):
    trader = FrozenRankDisplacementPaperTrader(tmp_path, persist=False)
    start = datetime(2026, 7, 30, 9, 20, tzinfo=IST)
    trader.process(
        [_candidate("AAA", 0.80, start)],
        {"AAA": _tick()},
        now=start,
    )

    events = trader.process(
        [_candidate("BBB", 0.95, start + timedelta(minutes=4))],
        {"AAA": _tick(bid=100.6), "BBB": _tick()},
        now=start + timedelta(minutes=4),
    )

    assert events == []
    assert trader.trades[0]["closed"] is False


def test_rejects_incomplete_depth_and_small_capital_utilization(tmp_path):
    trader = FrozenRankDisplacementPaperTrader(tmp_path, persist=False)
    now = datetime(2026, 7, 30, 9, 20, tzinfo=IST)
    incomplete = _candidate("AAA", 0.90, now)
    incomplete["EntryDepthFillable"] = False
    too_small = _candidate("BBB", 0.90, now, quantity=100)

    events = trader.process(
        [incomplete, too_small],
        {"AAA": _tick(), "BBB": _tick()},
        now=now,
    )

    assert events == []
    assert trader.trades == []


def test_persists_restart_safe_paper_only_audit(tmp_path):
    start = datetime(2026, 7, 30, 9, 20, tzinfo=IST)
    first = FrozenRankDisplacementPaperTrader(tmp_path)
    first.process(
        [_candidate("AAA", 0.80, start)],
        {"AAA": _tick()},
        now=start,
    )

    restarted = FrozenRankDisplacementPaperTrader(tmp_path)
    events = restarted.process(
        [_candidate("BBB", 0.86, start + timedelta(minutes=4))],
        {"AAA": _tick(bid=100.2), "BBB": _tick()},
        now=start + timedelta(minutes=4),
    )

    assert [row["event"] for row in events] == ["exit", "entry"]
    status = json.loads(
        (
            tmp_path
            / "frozen_stop_displacement_paper"
            / "status.json"
        ).read_text(encoding="utf-8")
    )
    assert status["displacements"] == 1
    assert status["requires_five_level_fillability"] is True
    assert status["paper_only"] is True
    assert status["alerts_allowed"] is False
    assert status["real_orders_allowed"] is False
    assert not hasattr(restarted, "place_order")

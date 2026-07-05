from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from pkscreener.classes.LiveFeatureRecorder import LiveFeatureRecorder


def _tick(
    price: float,
    close: float = 100.0,
    day_open: float = 100.0,
    high: float = 0.0,
    volume: int = 100000,
    buy_qty: int = 300000,
    sell_qty: int = 100000,
    bid: float = 0.0,
    ask: float = 0.0,
):
    high = high or price
    bid = bid or round(price - 0.05, 2)
    ask = ask or round(price + 0.05, 2)
    return {
        "last_price": price,
        "close": close,
        "open": day_open,
        "high": high,
        "volume_traded": volume,
        "total_buy_quantity": buy_qty,
        "total_sell_quantity": sell_qty,
        "depth": {
            "buy": [{"price": bid, "quantity": 1000}],
            "sell": [{"price": ask, "quantity": 1000}],
        },
    }


def test_best_bid_ask_and_spread_from_depth(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=0.5)
    now = datetime(2026, 7, 3, 9, 20)

    rows = recorder.build_rows(
        {"AAA": _tick(101.0, close=100.0, bid=100.90, ask=101.10)},
        alerted_symbols={"AAA"},
        now=now,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["BestBid"] == 100.90
    assert row["BestAsk"] == 101.10
    assert row["SpreadBps"] == pytest.approx(19.802, abs=0.002)
    assert row["SpreadOk"] is True


def test_order_book_stability_requires_multiple_samples(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path),
        interval_seconds=0,
        top_gainer_pct=20.0,
        stability_window=3,
        min_stable_bs_ratio=2.0,
    )
    start = datetime(2026, 7, 3, 9, 20)

    first = recorder.build_rows(
        {"AAA": _tick(101.0, buy_qty=300000, sell_qty=100000)},
        alerted_symbols={"AAA"},
        now=start,
    )[0]
    second = recorder.build_rows(
        {"AAA": _tick(101.2, buy_qty=280000, sell_qty=100000)},
        alerted_symbols={"AAA"},
        now=start + timedelta(seconds=20),
    )[0]
    third = recorder.build_rows(
        {"AAA": _tick(101.3, buy_qty=350000, sell_qty=100000)},
        alerted_symbols={"AAA"},
        now=start + timedelta(seconds=40),
    )[0]

    assert first["OrderBookStable"] is False
    assert second["OrderBookStable"] is False
    assert third["OrderBookStable"] is True
    assert third["BuyerSellerMinWindow"] == pytest.approx(2.8)
    assert third["BuyerSellerMedianWindow"] == pytest.approx(3.0)


def test_sector_context_and_missed_runner_label(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=5.0)
    now = datetime(2026, 7, 3, 9, 40)
    ticks = {
        "AAA": _tick(106.0, close=100.0),
        "BBB": _tick(103.0, close=100.0),
        "CCC": _tick(99.0, close=100.0),
        "DDD": _tick(101.0, close=100.0),
    }

    def sector_for(symbol):
        return "AUTO" if symbol in {"AAA", "BBB", "CCC"} else "PHARMA"

    with patch("pkscreener.classes.LiveFeatureRecorder.get_sector", side_effect=sector_for):
        rows = recorder.build_rows(ticks, alerted_symbols=set(), now=now)

    aaa = next(row for row in rows if row["Symbol"] == "AAA")
    assert aaa["MissedRunnerCandidate"] is True
    assert aaa["IsTopGainerNow"] is True
    assert aaa["Sector"] == "AUTO"
    assert aaa["SectorCount"] == 3
    assert aaa["SectorTop2Count"] == 2
    assert aaa["SectorBreadthPct"] == pytest.approx(66.667, abs=0.01)
    assert "missed_runner" in aaa["RecordReason"]


def test_record_persists_and_throttles(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=60, top_gainer_pct=5.0)
    now = datetime(2026, 7, 3, 9, 20)
    ticks = {"AAA": _tick(106.0, close=100.0)}

    first = recorder.record(ticks, alerted_symbols=set(), now=now)
    second = recorder.record(ticks, alerted_symbols=set(), now=now + timedelta(seconds=10))

    assert len(first) == 1
    assert second.empty
    path = tmp_path / "live_feature_recorder" / "live_features_2026-07-03.csv"
    assert path.exists()

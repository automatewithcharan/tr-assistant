import csv
from datetime import datetime, timedelta
import json
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pkscreener.classes.NGTop100GainersWatch import NGTop100GainersWatch, rank_live_gainers


IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 18, 9, 20, tzinfo=IST)


def files(tmp_path, now=NOW, positives=110):
    universe = tmp_path / "EQUITY_L.csv"
    with universe.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["SYMBOL", "SERIES"])
        writer.writeheader()
        writer.writerows({"SYMBOL": f"S{i:04}", "SERIES": "EQ"} for i in range(1001))
    ticks = tmp_path / "ticks.csv"
    with ticks.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "exchange", "last_price",
                                                    "close", "volume_traded", "last_trade_time",
                                                    "received_at"])
        writer.writeheader()
        for i in range(positives):
            writer.writerow({"symbol": f"S{i:04}", "exchange": "NSE",
                             "last_price": 200 - i / 10, "close": 100,
                             "volume_traded": 10_000 + i,
                             "last_trade_time": f"{now.date()} 09:19:00",
                             "received_at": now.isoformat()})
        for i in range(positives, 600):
            writer.writerow({"symbol": f"S{i:04}", "exchange": "NSE",
                             "last_price": 99, "close": 100,
                             "volume_traded": 1000,
                             "last_trade_time": f"{now.date()} 09:19:00",
                             "received_at": now.isoformat()})
        writer.writerow({"symbol": "S0999", "exchange": "BSE", "last_price": 1000,
                         "close": 100, "volume_traded": 1000,
                         "last_trade_time": f"{now.date()} 09:19:00",
                         "received_at": now.isoformat()})
    os.utime(ticks, (now.timestamp(), now.timestamp()))
    return ticks, universe


class Transport:
    def __init__(self, outcomes=None):
        self.outcomes = iter(outcomes or [])
        self.messages = []

    def post(self, message):
        self.messages.append(message)
        return next(self.outcomes, SimpleNamespace(delivered=True, http_status=200,
                                                   retry_after_seconds=None))


def test_rank_is_full_market_top100_and_ignores_wrong_exchange(tmp_path):
    ticks, universe = files(tmp_path)
    rows = rank_live_gainers(ticks, universe, NOW)
    assert len(rows) == 100
    assert rows[0]["symbol"] == "S0000"
    assert rows[-1]["symbol"] == "S0099"
    assert all(row["symbol"] != "S0999" for row in rows)


def test_low_share_count_and_low_traded_value_do_not_rank(tmp_path):
    ticks, universe = files(tmp_path)
    with ticks.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0])
    rows[0]["volume_traded"] = "9"
    rows[1]["volume_traded"] = "3000"  # Above share floor, below ₹10L value floor.
    with ticks.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.utime(ticks, (NOW.timestamp(), NOW.timestamp()))
    ranked = rank_live_gainers(ticks, universe, NOW)
    assert len(ranked) == 100
    assert ranked[0]["symbol"] == "S0002"
    assert ranked[-1]["symbol"] == "S0101"


def test_weak_day_reports_available_positive_names(tmp_path):
    ticks, universe = files(tmp_path, positives=80)
    rows = rank_live_gainers(ticks, universe, NOW)
    assert len(rows) == 80
    assert rows[-1]["symbol"] == "S0079"


def test_stale_or_prior_day_ticks_cannot_send(tmp_path):
    ticks, universe = files(tmp_path)
    with pytest.raises(ValueError, match="stale"):
        rank_live_gainers(ticks, universe, NOW + timedelta(minutes=3))
    os.utime(ticks, ((NOW + timedelta(days=1)).timestamp(),
                     (NOW + timedelta(days=1)).timestamp()))
    with pytest.raises(ValueError, match="Only 0 fresh"):
        rank_live_gainers(ticks, universe, NOW + timedelta(days=1))


def test_skips_official_top20_batches_rest_and_dedupes(tmp_path):
    ticks, universe = files(tmp_path)
    root = tmp_path / "state"
    root.mkdir()
    (root / "2026-09-18.json").write_text(json.dumps({
        "sent_symbols": [f"S{i:04}" for i in range(20)]}))
    transport = Transport()
    watch = NGTop100GainersWatch(root / "kite_top100", ticks, universe, root,
                                 transport, lambda: NOW)
    result = watch.process()
    assert result["status"] == "delivered"
    assert len(result["symbols"]) == 80
    assert len(transport.messages) == 3
    assert "**S0020**" in transport.messages[0]
    assert "Vol 10,020" in transport.messages[0]
    assert all("**S0000**" not in message for message in transport.messages)
    assert all(len(message) <= 2000 for message in transport.messages)
    assert watch.process()["status"] == "up_to_date"


def test_failed_batch_retries_without_resending_delivered_batch(tmp_path):
    ticks, universe = files(tmp_path)
    root = tmp_path / "state"
    fail = SimpleNamespace(delivered=False, http_status=429, retry_after_seconds=30)
    transport = Transport([SimpleNamespace(delivered=True, http_status=200,
                                          retry_after_seconds=None), fail])
    current = [NOW]
    watch = NGTop100GainersWatch(root / "kite_top100", ticks, universe, root,
                                 transport, lambda: current[0])
    assert watch.process()["status"] == "delivery_failed"
    state = json.loads((root / "kite_top100" / "2026-09-18.json").read_text())
    assert len(state["sent_symbols"]) == 30
    assert watch.process()["status"] == "retry_wait"
    current[0] = NOW + timedelta(seconds=31)
    os.utime(ticks, (current[0].timestamp(), current[0].timestamp()))
    assert len(watch.process()["symbols"]) == 70
    assert len(transport.messages) == 5

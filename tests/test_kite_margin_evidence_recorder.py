import csv
import json
from datetime import datetime

from pkscreener.classes.KiteMarginEvidenceRecorder import (
    KiteMarginEvidenceRecorder,
)


class FakeMarginClient:
    def __init__(self):
        self.calls = []

    def basket_order_margins(
        self,
        orders,
        *,
        consider_positions=True,
        mode=None,
    ):
        self.calls.append(
            {
                "orders": orders,
                "consider_positions": consider_positions,
                "mode": mode,
            }
        )
        return {
            "orders": [
                {"tradingsymbol": order["tradingsymbol"], "total": 20.0}
                for order in orders
            ]
        }


class FailingMarginClient:
    def __init__(self):
        self.calls = 0

    def basket_order_margins(self, orders, **kwargs):
        self.calls += 1
        raise RuntimeError("margin endpoint unavailable")


def _event(symbol, source):
    return {
        "Exchange": "NSE",
        "Symbol": symbol,
        "Source": source,
        "EntryAsk": 100.0,
    }


def test_margin_recorder_is_calculation_only_and_persists_quote(tmp_path):
    client = FakeMarginClient()
    recorder = KiteMarginEvidenceRecorder(
        tmp_path,
        client_factory=lambda: client,
        min_interval_seconds=1,
    )
    now = datetime(2026, 7, 30, 9, 20)

    rows = recorder.process(
        [
            _event("AAA", "stop"),
            _event("AAA", "runner"),
            _event("BBB", "runner"),
        ],
        now=now,
        force=True,
    )

    assert len(client.calls) == 1
    assert client.calls[0]["consider_positions"] is False
    assert client.calls[0]["mode"] is None
    assert {
        order["tradingsymbol"] for order in client.calls[0]["orders"]
    } == {"AAA", "BBB"}
    assert all(order["product"] == "MIS" for order in client.calls[0]["orders"])
    assert all(order["quantity"] == 1 for order in client.calls[0]["orders"])
    assert len(rows) == 2
    assert all(row["Status"] == "quoted" for row in rows)
    assert all(row["MarginPct"] == 20.0 for row in rows)
    assert all(row["EffectiveLeverage"] == 5.0 for row in rows)

    quotes = list(
        csv.DictReader(
            (
                tmp_path
                / "kite_margin_evidence"
                / "margin_quotes_2026-07-30.csv"
            ).open()
        )
    )
    assert len(quotes) == 2
    assert not hasattr(recorder, "place_order")
    status = json.loads(
        (
            tmp_path / "kite_margin_evidence" / "status.json"
        ).read_text()
    )
    assert status["calculation_only"] is True
    assert status["response_mode"] == "full_per_order_breakdown"
    assert status["alerts_allowed"] is False
    assert status["real_orders_allowed"] is False


def test_margin_recorder_does_not_requote_successful_symbol(tmp_path):
    client = FakeMarginClient()
    recorder = KiteMarginEvidenceRecorder(
        tmp_path,
        client_factory=lambda: client,
        min_interval_seconds=1,
    )
    now = datetime(2026, 7, 30, 9, 20)
    recorder.process([_event("AAA", "stop")], now=now, force=True)

    rows = recorder.process([_event("AAA", "runner")], now=now, force=True)

    assert rows == []
    assert len(client.calls) == 1


def test_margin_recorder_stops_retrying_and_restores_retry_cap(tmp_path):
    client = FailingMarginClient()
    now = datetime(2026, 7, 30, 9, 20)
    recorder = KiteMarginEvidenceRecorder(
        tmp_path,
        client_factory=lambda: client,
        min_interval_seconds=1,
        max_attempts_per_symbol=2,
    )

    first = recorder.process([_event("AAA", "stop")], now=now, force=True)
    second = recorder.process([_event("AAA", "stop")], now=now, force=True)
    third = recorder.process([_event("AAA", "stop")], now=now, force=True)

    assert first[0]["Status"] == "failed"
    assert first[0]["Attempt"] == 1
    assert second[0]["Status"] == "failed_final"
    assert second[0]["Attempt"] == 2
    assert third == []
    assert client.calls == 2

    restarted_client = FailingMarginClient()
    restarted = KiteMarginEvidenceRecorder(
        tmp_path,
        client_factory=lambda: restarted_client,
        min_interval_seconds=1,
        max_attempts_per_symbol=2,
    )
    rows = restarted.process(
        [_event("AAA", "runner")],
        now=now,
        force=True,
    )

    assert rows == []
    assert restarted_client.calls == 0
    status = json.loads(
        (
            tmp_path / "kite_margin_evidence" / "status.json"
        ).read_text()
    )
    assert status["exhausted_symbols"] == 1
    assert status["max_attempts_per_symbol"] == 2

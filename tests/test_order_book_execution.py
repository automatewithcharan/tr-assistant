import pytest

from pkscreener.classes.KiteTickerStream import KiteTickerStreamRunner
from pkscreener.classes.OrderBookExecution import (
    affordable_market_fill,
    depth_json,
    depth_levels,
    market_fill,
    parse_depth_json,
)


def _tick():
    return {
        "depth": {
            "sell": [
                {"price": 100.0, "quantity": 10, "orders": 1},
                {"price": 100.1, "quantity": 20, "orders": 2},
                {"price": 100.2, "quantity": 30, "orders": 3},
            ],
            "buy": [
                {"price": 99.9, "quantity": 15, "orders": 1},
                {"price": 99.8, "quantity": 25, "orders": 2},
            ],
        }
    }


def test_market_fill_walks_visible_levels_and_reports_vwap():
    levels = depth_levels(_tick(), "sell")
    fill = market_fill(levels, 25)

    assert fill["fillable"] is True
    assert fill["filled_quantity"] == 25
    assert fill["levels_used"] == 2
    assert fill["vwap"] == pytest.approx((10 * 100.0 + 15 * 100.1) / 25)
    assert fill["worst_price"] == 100.1


def test_market_fill_fails_closed_when_visible_depth_is_insufficient():
    fill = market_fill(depth_levels(_tick(), "buy"), 50)

    assert fill["fillable"] is False
    assert fill["filled_quantity"] == 40
    assert fill["unfilled_quantity"] == 10


def test_depth_json_round_trip_is_compact_and_deterministic():
    levels = depth_levels(_tick(), "sell")
    encoded = depth_json(levels)

    assert " " not in encoded
    assert parse_depth_json(encoded) == levels


def test_affordable_fill_respects_capital_across_levels():
    fill = affordable_market_fill(depth_levels(_tick(), "sell"), 2501.0)

    assert fill["quantity"] == 24
    assert fill["filled_quantity"] == 24
    assert fill["notional"] == pytest.approx(2401.4)


def test_shared_kite_tick_preserves_five_depth_levels_per_side():
    runner = KiteTickerStreamRunner.__new__(KiteTickerStreamRunner)
    runner._token_to_exchange = {101: "NSE"}
    tick = {
        "instrument_token": 101,
        "last_price": 100.0,
        "ohlc": {
            "open": 99.0,
            "high": 101.0,
            "low": 98.0,
            "close": 99.5,
        },
        "depth": {
            "buy": [
                {
                    "price": 99.9 - level * 0.05,
                    "quantity": 450 + level,
                    "orders": 2 + level,
                }
                for level in range(6)
            ],
            "sell": [
                {
                    "price": 100.1 + level * 0.05,
                    "quantity": 375 + level,
                    "orders": 3 + level,
                }
                for level in range(6)
            ],
        },
    }

    compact = runner._compact_tick("TEST", tick, "2026-07-29T10:00:00")

    assert len(depth_levels(compact, "buy")) == 5
    assert len(depth_levels(compact, "sell")) == 5
    assert compact["best_bid_quantity"] == 450
    assert compact["best_ask_quantity"] == 375

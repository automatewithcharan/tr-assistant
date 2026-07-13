from datetime import datetime, timedelta

import pandas as pd

from pkscreener.classes.RankedConfirmationPaperTrader import (
    RankedConfirmationPaperTrader,
    RankedPaperConfig,
)


def _config(**overrides):
    values = {
        "budget_per_trade": 50_000.0,
        "max_open_positions": 1,
        "max_trades_per_day": 5,
        "max_daily_loss_amount": 1_000.0,
        "max_consecutive_losses": 2,
        "entry_window_seconds": 900,
        "min_observation_seconds": 60,
        "min_observations": 4,
        "min_confirm_pct": 0.15,
        "max_entry_extension_pct": 0.45,
        "min_open_move_pct": 0.5,
        "max_open_move_pct": 3.0,
        "min_day_change_pct": 0.5,
        "max_day_change_pct": 5.0,
        "max_pullback_pct": 0.75,
        "min_volume_growth_pct": 0.5,
        "min_current_bs_ratio": 1.5,
        "min_median_bs_ratio": 1.3,
        "min_bs_ratio": 0.9,
        "max_spread_bps": 20.0,
        "target_pct": 1.0,
        "stop_pct": 0.75,
        "fast_failure_age_seconds": 180,
        "fast_failure_drop_pct": 0.35,
        "fast_failure_sell_ratio": 1.3,
        "fee_pct_per_side": 0.05,
        "slippage_bps_per_side": 5.0,
        "eod_exit_minutes": 15 * 60 + 10,
    }
    values.update(overrides)
    return RankedPaperConfig(**values)


def _alert(symbol="TEST", strength=65.0, price=100.0):
    return pd.DataFrame(
        [{
            "Symbol": symbol,
            "NowAction": "⚡ Grade-B Anomaly",
            "PriceNow": price,
            "SignalStrengthPct": strength,
            "Checks": "Q:65/100 [B] | Vol 6.0x | B/S 2.0x",
        }]
    )


def _tick(price, volume, open_price=99.0, prev_close=99.0, bs=2.0, vwap=100.0):
    sell = 100_000.0
    return {
        "last_price": price,
        "best_bid": price - 0.01,
        "best_ask": price + 0.01,
        "open": open_price,
        "close": prev_close,
        "high": price * 1.001,
        "volume_traded": volume,
        "total_buy_quantity": sell * bs,
        "total_sell_quantity": sell,
        "average_traded_price": vwap,
    }


def _advance_to_entry(
    trader,
    start,
    symbol="TEST",
    strength=65.0,
    open_price=99.0,
    prev_close=99.0,
):
    prices = [100.00, 100.16, 100.18, 100.20]
    volumes = [100_000, 100_400, 100_800, 101_200]
    events = []
    for index, (price, volume) in enumerate(zip(prices, volumes)):
        now = start + timedelta(seconds=[0, 20, 40, 65][index])
        alerts = _alert(symbol, strength) if index == 0 else pd.DataFrame()
        events.extend(
            trader.process(
                alerts,
                {
                    symbol: _tick(
                        price,
                        volume,
                        open_price=open_price,
                        prev_close=prev_close,
                    )
                },
                now=now,
            )
        )
    return events


def test_sustained_confirmation_waits_then_enters(tmp_path):
    start = datetime(2026, 7, 13, 9, 30, 0)
    trader = RankedConfirmationPaperTrader(str(tmp_path), _config(), now_fn=lambda: start)

    events = _advance_to_entry(trader, start)

    entries = [event for event in events if event["event"] == "paper_entry"]
    assert len(entries) == 1
    assert entries[0]["symbol"] == "TEST"
    assert trader.trades["TEST"].entry_time == "2026-07-13T09:31:05"
    assert trader.trades["TEST"].entry_price > 100.20


def test_extended_mover_is_not_entered(tmp_path):
    start = datetime(2026, 7, 13, 9, 30, 0)
    trader = RankedConfirmationPaperTrader(str(tmp_path), _config(), now_fn=lambda: start)

    events = _advance_to_entry(trader, start, open_price=96.0)

    assert not [event for event in events if event["event"] == "paper_entry"]
    assert trader.candidates["TEST"].status_reason == "open_move_outside_early_window"
    candidates = pd.read_csv(
        tmp_path / "ranked_confirmation_paper" / "candidates_2026-07-13.csv"
    )
    assert candidates.iloc[0]["status_reason"] == "open_move_outside_early_window"
    assert candidates.iloc[0]["observation_count"] == 4


def test_gap_down_rebound_is_not_a_positive_day_continuation(tmp_path):
    start = datetime(2026, 7, 13, 9, 30, 0)
    trader = RankedConfirmationPaperTrader(str(tmp_path), _config(), now_fn=lambda: start)

    _advance_to_entry(trader, start, open_price=99.0, prev_close=102.0)

    # Price is +1.2% from the open but still below the prior close.
    assert "TEST" not in trader.trades
    assert trader.candidates["TEST"].status_reason == "day_move_below_positive_floor"


def test_unstable_buy_pressure_is_not_confirmation(tmp_path):
    start = datetime(2026, 7, 13, 9, 30, 0)
    trader = RankedConfirmationPaperTrader(str(tmp_path), _config(), now_fn=lambda: start)
    ratios = [2.0, 0.7, 2.0, 2.0]
    prices = [100.00, 100.16, 100.18, 100.20]
    for index, ratio in enumerate(ratios):
        now = start + timedelta(seconds=[0, 20, 40, 65][index])
        trader.process(
            _alert() if index == 0 else pd.DataFrame(),
            {"TEST": _tick(prices[index], 100_000 + index * 500, bs=ratio)},
            now=now,
        )

    assert "TEST" not in trader.trades
    assert trader.candidates["TEST"].status_reason == "buy_pressure_not_stable"


def test_ranking_selects_stronger_candidate_when_one_slot_exists(tmp_path):
    start = datetime(2026, 7, 13, 9, 30, 0)
    trader = RankedConfirmationPaperTrader(str(tmp_path), _config(max_open_positions=1), now_fn=lambda: start)
    alerts = pd.concat([_alert("LOW", 60.0), _alert("HIGH", 75.0)], ignore_index=True)
    for index in range(4):
        now = start + timedelta(seconds=[0, 20, 40, 65][index])
        current_alerts = alerts if index == 0 else pd.DataFrame()
        price = [100.00, 100.16, 100.18, 100.20][index]
        volume = 100_000 + index * 500
        trader.process(
            current_alerts,
            {"LOW": _tick(price, volume), "HIGH": _tick(price, volume)},
            now=now,
        )

    assert set(trader.trades) == {"HIGH"}
    assert trader.candidates["LOW"].status == "observing"


def test_target_exit_records_cost_adjusted_pnl(tmp_path):
    start = datetime(2026, 7, 13, 9, 30, 0)
    trader = RankedConfirmationPaperTrader(str(tmp_path), _config(), now_fn=lambda: start)
    _advance_to_entry(trader, start)
    trade = trader.trades["TEST"]

    events = trader.process(
        pd.DataFrame(),
        {"TEST": _tick(trade.target_price + 0.10, 103_000, vwap=100.2)},
        now=start + timedelta(seconds=90),
    )

    exits = [event for event in events if event["event"] == "paper_exit"]
    assert len(exits) == 1
    assert exits[0]["reason"] == "target_hit"
    assert exits[0]["gross_pnl"] > exits[0]["net_pnl"] > 0
    assert trader.trades["TEST"].closed
    assert (tmp_path / "ranked_confirmation_paper" / "trades_2026-07-13.csv").exists()
    tape = pd.read_csv(
        tmp_path / "ranked_confirmation_paper" / "observations_2026-07-13.csv"
    )
    assert len(tape) == 5
    assert set(["best_bid", "best_ask", "buy_qty", "sell_qty", "vwap"]).issubset(tape.columns)
    assert tape.iloc[-1]["candidate_status"] == "entered"


def test_consecutive_loss_guard_blocks_next_entry(tmp_path):
    start = datetime(2026, 7, 13, 9, 30, 0)
    trader = RankedConfirmationPaperTrader(
        str(tmp_path),
        _config(max_consecutive_losses=1),
        now_fn=lambda: start,
    )
    _advance_to_entry(trader, start, symbol="LOSS")
    loss = trader.trades["LOSS"]
    trader.process(
        pd.DataFrame(),
        {"LOSS": _tick(loss.stop_price - 0.10, 103_000, vwap=100.2)},
        now=start + timedelta(seconds=90),
    )

    events = _advance_to_entry(trader, start + timedelta(minutes=3), symbol="NEXT")

    assert not [event for event in events if event["event"] == "paper_entry" and event["symbol"] == "NEXT"]
    assert "NEXT" not in trader.trades
    assert trader._guardrail_reason() == "consecutive_loss_limit_reached"

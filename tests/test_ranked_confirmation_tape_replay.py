from datetime import datetime, timedelta

import pandas as pd

from pkscreener.classes.RankedConfirmationPaperTrader import (
    RankedConfirmationPaperTrader,
    RankedPaperConfig,
)
from scripts.replay_ranked_confirmation_tape import load_tapes, replay_tapes


def _config():
    return RankedPaperConfig(max_open_positions=1)


def _tick(price, volume, vwap=100.0):
    return {
        "last_price": price,
        "best_bid": price - 0.01,
        "best_ask": price + 0.01,
        "open": 99.0,
        "close": 99.0,
        "high": price * 1.001,
        "volume_traded": volume,
        "total_buy_quantity": 200_000,
        "total_sell_quantity": 100_000,
        "average_traded_price": vwap,
    }


def _advance_to_entry(trader, start):
    prices = [100.00, 100.16, 100.18, 100.20]
    for index, price in enumerate(prices):
        now = start + timedelta(seconds=[0, 20, 40, 65][index])
        alerts = pd.DataFrame(
            [{
                "Symbol": "TEST",
                "NowAction": "Grade-B Anomaly",
                "PriceNow": 100.0,
                "SignalStrengthPct": 65.0,
                "Checks": "Q:65/100 [B] | Vol 6.0x | B/S 2.0x",
            }]
        ) if index == 0 else pd.DataFrame()
        trader.process(alerts, {"TEST": _tick(price, 100_000 + index * 400)}, now=now)


def test_exact_tape_can_replay_a_target_trade(tmp_path):
    start = datetime(2026, 7, 13, 9, 30, 0)
    source = tmp_path / "source"
    trader = RankedConfirmationPaperTrader(str(source), _config(), now_fn=lambda: start)
    _advance_to_entry(trader, start)
    trade = trader.trades["TEST"]
    trader.process(
        pd.DataFrame(),
        {"TEST": _tick(trade.target_price + 0.10, 103_000, vwap=100.2)},
        now=start + timedelta(seconds=90),
    )
    # Add a final market-time row so the replay is promotion-complete.
    trader.process(
        pd.DataFrame(),
        {"TEST": _tick(trade.target_price + 0.10, 104_000, vwap=100.2)},
        now=datetime(2026, 7, 13, 15, 10, 0),
    )

    pattern = str(source / "ranked_confirmation_paper" / "observations_*.csv")
    tapes = load_tapes(pattern)
    trades, daily, summary = replay_tapes(
        tapes,
        output_dir=str(tmp_path / "replay"),
        config=_config(),
    )

    assert len(trades) == 1
    assert trades.iloc[0]["exit_reason"] == "target_hit"
    assert trades.iloc[0]["net_pnl"] > 0
    assert bool(daily.iloc[0]["tape_complete"])
    assert summary.iloc[0]["evidence_type"] == "exact_recorded_tick_tape"


def test_replay_uses_in_memory_trader(tmp_path, monkeypatch):
    start = datetime(2026, 7, 13, 9, 30, 0)
    source = tmp_path / "source"
    trader = RankedConfirmationPaperTrader(str(source), _config(), now_fn=lambda: start)
    _advance_to_entry(trader, start)
    trade = trader.trades["TEST"]
    trader.process(
        pd.DataFrame(),
        {"TEST": _tick(trade.target_price + 0.10, 103_000, vwap=100.2)},
        now=start + timedelta(seconds=90),
    )
    tapes = load_tapes(str(source / "ranked_confirmation_paper" / "observations_*.csv"))

    def fail_write(*args, **kwargs):
        raise AssertionError("exact replay must not write engine state per tick")

    monkeypatch.setattr(RankedConfirmationPaperTrader, "_save_state", fail_write)
    monkeypatch.setattr(RankedConfirmationPaperTrader, "_write_trades", fail_write)
    monkeypatch.setattr(RankedConfirmationPaperTrader, "_write_candidates", fail_write)
    trades, daily, _ = replay_tapes(
        tapes,
        output_dir=str(tmp_path / "replay"),
        config=_config(),
    )

    assert len(trades) == 1
    assert not daily.empty


def test_load_tapes_rejects_incomplete_schema(tmp_path):
    path = tmp_path / "observations_2026-07-13.csv"
    pd.DataFrame([{"date": "2026-07-13", "symbol": "TEST"}]).to_csv(path, index=False)

    try:
        load_tapes(str(path))
    except ValueError as exc:
        assert "missing replay columns" in str(exc)
    else:
        raise AssertionError("Incomplete tape must be rejected")

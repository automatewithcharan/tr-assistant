from pathlib import Path

import pandas as pd

import pkscreener.classes.KiteTickerStream as ticker_module
from pkscreener.classes.KiteTickerStream import KiteTickerStreamRunner


def test_ticker_updates_and_persists_minute_candles(tmp_path, monkeypatch):
    monkeypatch.setattr(ticker_module, "_output_dir", lambda: str(tmp_path))
    runner = KiteTickerStreamRunner(config_manager=None, universe="all", save_interval_seconds=1)

    runner._update_minute_candle(
        "TEST",
        {
            "last_price": 100.0,
            "volume_traded": 10,
            "exchange_timestamp": "2026-06-12 09:25:12",
            "received_at": "2026-06-12T09:25:13",
        },
    )
    runner._update_minute_candle(
        "TEST",
        {
            "last_price": 101.5,
            "volume_traded": 18,
            "exchange_timestamp": "2026-06-12 09:25:42",
            "received_at": "2026-06-12T09:25:43",
        },
    )

    runner._persist_minute_candles()

    path = Path(tmp_path) / "kiteticker_candles" / "1minute" / "kiteticker_all_1minute_2026-06-12.csv"
    assert path.exists()
    df = pd.read_csv(path)
    row = df.iloc[0]
    assert row["symbol"] == "TEST"
    assert row["open"] == 100.0
    assert row["high"] == 101.5
    assert row["low"] == 100.0
    assert row["close"] == 101.5
    assert row["volume_traded"] == 18

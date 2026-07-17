from pathlib import Path

from pkscreener.classes.IntradayEventShadowRecorder import IntradayEventShadowRecorder


def _tick(price, volume, timestamp):
    return {
        "last_price": price,
        "volume_traded": volume,
        "exchange_timestamp": timestamp,
    }


def test_aggregates_completed_five_minute_bar_and_persists(tmp_path):
    recorder = IntradayEventShadowRecorder(tmp_path)
    recorder.update({"AAA": _tick(100, 1000, "2026-01-02 09:15:01")})
    recorder.update({"AAA": _tick(101, 1100, "2026-01-02 09:16:01")})
    completed = recorder.update({"AAA": _tick(102, 1300, "2026-01-02 09:20:01")})

    assert len(completed) == 1
    assert completed[0]["open"] == 100
    assert completed[0]["high"] == 101
    assert completed[0]["close"] == 101
    assert completed[0]["volume"] == 100
    assert (Path(tmp_path) / "intraday_event_shadow" / "bars_2026-01-02.csv").exists()


def test_requires_completed_benchmark_bars_before_detecting_events(tmp_path):
    recorder = IntradayEventShadowRecorder(tmp_path)
    recorder._bars["AAA"] = [
        {"symbol": "AAA", "datetime": f"2026-01-02 09:{minute:02d}:00", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000, "observations": 2}
        for minute in (15, 20, 25, 30)
    ]

    assert recorder.detect_new_events(["AAA"]) == []


def test_timestamp_converts_timezone_to_ist():
    timestamp = IntradayEventShadowRecorder._timestamp(
        {"exchange_timestamp": "2026-01-02T03:45:00+00:00"}
    )

    assert str(timestamp) == "2026-01-02 09:15:00"


def test_timestamp_treats_naive_kite_exchange_time_as_utc():
    timestamp = IntradayEventShadowRecorder._timestamp(
        {"exchange_timestamp": "2026-01-02 03:45:00"}
    )

    assert str(timestamp) == "2026-01-02 09:15:00"


def test_timestamp_preserves_naive_ist_fallback_clock():
    timestamp = IntradayEventShadowRecorder._timestamp({}, fallback="2026-01-02 09:15:00")

    assert str(timestamp) == "2026-01-02 09:15:00"


def test_restores_completed_bars_for_today(tmp_path):
    import pandas as pd

    recorder = IntradayEventShadowRecorder(tmp_path)
    today = str(pd.Timestamp.now(tz="Asia/Kolkata").date())
    recorder.update({"AAA": _tick(100, 1000, f"{today} 03:45:01")})
    recorder.update({"AAA": _tick(101, 1100, f"{today} 03:46:01")})
    recorder.update({"AAA": _tick(102, 1300, f"{today} 03:50:01")})

    restored = IntradayEventShadowRecorder(tmp_path)

    assert len(restored._bars["AAA"]) == 1
    assert float(restored._bars["AAA"][0]["close"]) == 101

import pandas as pd

from scripts.rebuild_intraday_event_shadow_evidence import normalize_archived_bars


def _bars(times):
    return pd.DataFrame(
        {
            "symbol": ["AAA"] * len(times),
            "datetime": times,
            "open": [100.0] * len(times),
            "high": [101.0] * len(times),
            "low": [99.0] * len(times),
            "close": [100.5] * len(times),
            "volume": [1000.0] * len(times),
        }
    )


def test_normalize_archived_bars_repairs_utc_and_drops_premarket():
    frame = _bars(
        [
            "2026-07-16 02:30:00",  # 08:00 IST, stale pre-market
            "2026-07-16 03:45:00",  # 09:15 IST
            "2026-07-16 09:50:00",  # 15:20 IST
        ]
    )

    repaired, mode = normalize_archived_bars(frame, source_timezone="utc")

    assert mode == "utc_naive_plus_0530"
    assert repaired["datetime"].dt.strftime("%H:%M").tolist() == ["09:15", "15:20"]


def test_normalize_archived_bars_does_not_shift_fixed_ist_archive():
    frame = _bars(["2026-07-16 09:15:00", "2026-07-16 15:20:00"])

    repaired, mode = normalize_archived_bars(frame, source_timezone="auto")

    assert mode == "ist_naive_unchanged"
    assert repaired["datetime"].dt.strftime("%H:%M").tolist() == ["09:15", "15:20"]


def test_normalize_archived_bars_repairs_mixed_restart_archive():
    frame = _bars(
        [
            "2026-07-16 03:45:00",  # old UTC-naive 09:15 IST
            "2026-07-16 06:25:00",  # old UTC-naive 11:55 IST
            "2026-07-16 12:00:00",  # post-restart IST
            "2026-07-16 15:20:00",  # post-restart IST
        ]
    )

    repaired, mode = normalize_archived_bars(frame, source_timezone="auto")

    assert mode == "mixed_utc_preopen_shifted_ist_preserved"
    assert repaired["datetime"].dt.strftime("%H:%M").tolist() == [
        "09:15", "11:55", "12:00", "15:20"
    ]

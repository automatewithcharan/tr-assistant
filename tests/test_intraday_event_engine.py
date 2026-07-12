from types import SimpleNamespace

import pandas as pd
import pytest

from pkscreener.classes.IntradayEventEngine import detect_intraday_events, prepare_intraday_day


def _config(**overrides):
    values = {
        "start_time": "09:20:00",
        "latest_signal_time": "11:30:00",
        "min_move_pct": 0.3,
        "max_move_pct": 3.0,
        "min_vol_ratio": 1.2,
        "min_vol_ratio_5": 1.4,
        "min_relative_strength_pct": 0.0,
        "min_breakout_pct": 0.0,
        "max_breakout_pct": 1.25,
        "min_vwap_dist_pct": 0.0,
        "max_vwap_dist_pct": 2.5,
        "max_range_used_pct": 5.0,
        "min_candle_body_pct": 0.05,
        "retest_tolerance_pct": 0.25,
        "max_events_per_type_per_day": 1,
        "min_event_gap_bars": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _bars():
    times = pd.date_range("2026-01-02 09:15:00", periods=5, freq="5min")
    return pd.DataFrame(
        {
            "datetime": times,
            "open": [100.0, 100.1, 100.2, 100.3, 100.5],
            "high": [100.3, 100.4, 100.5, 100.7, 101.2],
            "low": [99.9, 100.0, 100.1, 100.2, 100.4],
            "close": [100.1, 100.2, 100.3, 100.6, 101.1],
            "volume": [1000, 1000, 1000, 2500, 4000],
        }
    )


def test_prepare_intraday_day_uses_only_bars_available_at_each_row():
    bars = _bars()
    original = prepare_intraday_day(bars)
    changed = bars.copy()
    changed.loc[4, ["high", "close", "volume"]] = [150.0, 140.0, 999999]
    changed_result = prepare_intraday_day(changed)

    columns = ["vwap", "prior_high", "high_so_far", "vol_ratio", "range_pct_so_far"]
    pd.testing.assert_series_equal(original.loc[3, columns], changed_result.loc[3, columns])


def test_detects_or15_breakout_from_shared_features():
    prepared = prepare_intraday_day(_bars())

    events = detect_intraday_events(prepared, _config())

    assert ("or15_breakout", 3, 1) in events


def test_prepare_rejects_invalid_bars():
    with pytest.raises(ValueError, match="missing columns"):
        prepare_intraday_day(pd.DataFrame({"datetime": []}))

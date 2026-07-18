import pandas as pd

from scripts.kite_intraday_backfill import audit_intraday


def _candles(days: pd.DatetimeIndex, bars_per_day: int = 75) -> pd.DataFrame:
    rows = []
    for day in days:
        for offset in range(bars_per_day):
            rows.append(
                {
                    "datetime": day + pd.Timedelta(hours=9, minutes=15 + offset * 5),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1000,
                }
            )
    return pd.DataFrame(rows)


def test_recent_listing_is_complete_from_first_available_session():
    expected = pd.bdate_range("2024-01-01", "2024-06-28")
    listed_days = expected[expected >= pd.Timestamp("2024-04-01")]

    result = audit_intraday(
        "NEWIPO",
        _candles(listed_days),
        "5minute",
        "2024-01-01",
        "2024-06-28",
        set(expected),
    )

    assert result["status"] == "listing_limited"
    assert result["history_scope"] == "listing_limited"
    assert result["day_coverage_pct"] < 95.0
    assert result["available_day_coverage_pct"] == 100.0


def test_late_start_does_not_hide_stale_or_sparse_history():
    expected = pd.bdate_range("2024-01-01", "2024-06-28")
    sparse_days = expected[expected >= pd.Timestamp("2024-04-01")][::2]

    result = audit_intraday(
        "BROKEN",
        _candles(sparse_days),
        "5minute",
        "2024-01-01",
        "2024-06-28",
        set(expected),
    )

    assert result["status"] == "incomplete"
    assert result["history_scope"] == "requested_range"


def test_short_new_listing_is_not_promoted_to_research_complete():
    expected = pd.bdate_range("2024-01-01", "2024-06-28")
    recent_days = expected[-10:]

    result = audit_intraday(
        "TOONEW",
        _candles(recent_days),
        "5minute",
        "2024-01-01",
        "2024-06-28",
        set(expected),
    )

    assert result["status"] == "incomplete"

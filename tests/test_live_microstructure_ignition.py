import pandas as pd

from scripts.assess_live_microstructure_ignition import build_ignition_candidates


def _row(when, change, bs, *, stable=False):
    timestamp = pd.Timestamp(when)
    return {
        "When": timestamp,
        "Date": str(timestamp.date()),
        "Symbol": "AAA",
        "DayChangePct": change,
        "BuyerSellerRatio": bs,
        "OrderBookStable": stable,
        "SpreadOk": True,
        "SpreadBps": 5.0,
        "LastPrice": 100.0,
        "TradedValue": 100_000_000.0,
        "PullbackFromHighPct": 0.1,
    }


def test_ignition_requires_two_prior_snapshots_and_current_acceleration():
    tape = pd.DataFrame(
        [
            _row("2026-07-13 09:30:00", 2.0, 1.0),
            _row("2026-07-13 09:31:00", 2.1, 1.2),
            _row("2026-07-13 09:32:00", 2.4, 1.8),
        ]
    )

    candidates = build_ignition_candidates(tape)

    assert not candidates.empty
    assert set(candidates["When"]) == {pd.Timestamp("2026-07-13 09:32:00")}
    assert "accel_depth" in set(candidates["Rule"])
    assert candidates["Selected"].all()


def test_ignition_rejects_wide_spread_even_with_acceleration():
    tape = pd.DataFrame(
        [
            _row("2026-07-13 09:30:00", 2.0, 1.0),
            _row("2026-07-13 09:31:00", 2.1, 1.2),
            _row("2026-07-13 09:32:00", 2.6, 2.0),
        ]
    )
    tape.loc[tape.index[-1], "SpreadBps"] = 25.0

    assert build_ignition_candidates(tape).empty

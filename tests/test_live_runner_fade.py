import pandas as pd

from scripts.assess_live_runner_fade import build_fade_candidates, summarize


def _row(when, symbol="AAA", pullback=0.0, bs=2.0, median=2.0):
    return {
        "When": pd.Timestamp(when),
        "Date": str(pd.Timestamp(when).date()),
        "Symbol": symbol,
        "MissedRunnerCandidate": True,
        "SpreadOk": True,
        "SpreadBps": 5.0,
        "LastPrice": 100.0,
        "TradedValue": 100_000_000.0,
        "DayChangePct": 7.0,
        "PullbackFromHighPct": pullback,
        "BuyerSellerRatio": bs,
        "BuyerSellerMedianWindow": median,
        "SectorBreadthPct": 40.0,
    }


def test_fade_candidate_requires_prior_watch_and_failure_confirmation():
    tape = pd.DataFrame(
        [
            _row("2026-07-13 09:30:00"),
            _row("2026-07-13 09:30:30", pullback=1.5, bs=0.5, median=0.8),
            _row("2026-07-13 09:31:05", pullback=1.5, bs=0.5, median=0.8),
        ]
    )

    candidates = build_fade_candidates(tape)

    assert not candidates.empty
    assert candidates["When"].min() == pd.Timestamp("2026-07-13 09:31:05")
    assert candidates["Selected"].all()


def test_fade_summary_includes_cash_days_and_never_enables_real_orders():
    outcomes = pd.DataFrame(
        [
            {
                "Date": "2026-07-01", "Rule": "failure_pb1_bs1",
                "Contract": "t0.5_s0.5", "OutcomeAvailable": True,
                "NetReturnPct": 0.4,
            },
            {
                "Date": "2026-07-02", "Rule": "failure_pb1_bs1",
                "Contract": "t0.5_s0.5", "OutcomeAvailable": True,
                "NetReturnPct": -0.6,
            },
        ]
    )
    dates = [f"2026-07-{day:02d}" for day in range(1, 10)]

    row = summarize(outcomes, dates).iloc[0]

    assert row["Sessions"] == 9
    assert not row["SampleGatePassed"]
    assert not row["ShadowCandidate"]
    assert not row["PaperPromotionAllowed"]
    assert not row["RealOrdersAllowed"]

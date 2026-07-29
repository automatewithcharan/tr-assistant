import pandas as pd

from scripts.assess_fixed_rank_first_forward import (
    HYPOTHESIS,
    evaluate_fixed_hypothesis,
)


def _event(
    date: str,
    net_return: float = 2.1,
    *,
    index: int = 0,
) -> dict:
    entry = pd.Timestamp(f"{date} 09:20:00") + pd.Timedelta(
        minutes=index * 10
    )
    return {
        "date": date,
        "Contract": HYPOTHESIS["contract"],
        "Symbol": f"S{date}{index}",
        "Cohort": "leader_pool",
        "entry_time": entry,
        "exit_time": entry + pd.Timedelta(minutes=5),
        "ExitReason": "target" if net_return > 0 else "stop",
        "EntryAsk": 100.0,
        "ExitBid": 102.1 if net_return > 0 else 99.0,
        "DayChangeRank": 1,
        "EntrySpreadBps": 5.0,
        "NetReturnPct": net_return,
    }


def test_fixed_hypothesis_excludes_selection_cutoff_from_forward():
    events = pd.DataFrame(
        [
            _event("2026-07-27", 2.1),
            _event("2026-07-28", -1.1),
        ]
    )

    status, selection_daily, _, forward_daily, _ = (
        evaluate_fixed_hypothesis(
            events,
            ["2026-07-27", "2026-07-28"],
        )
    )

    assert selection_daily["date"].tolist() == ["2026-07-27"]
    assert forward_daily["date"].tolist() == ["2026-07-28"]
    assert status["selection_excluded_from_forward_evidence"]
    assert status["forward_dates"] == ["2026-07-28"]
    assert not status["accepted"]
    assert not status["real_orders_allowed"]


def test_invalidated_hypothesis_cannot_accept_even_after_strong_forward_days():
    dates = [
        stamp.strftime("%Y-%m-%d")
        for stamp in pd.bdate_range("2026-07-28", periods=20)
    ]
    events = pd.DataFrame(
        [
            _event(date, 0.7, index=index)
            for date in dates
            for index in range(3)
        ]
    )

    status, _, _, forward_daily, forward_trades = (
        evaluate_fixed_hypothesis(events, dates)
    )

    assert len(forward_daily) == 20
    assert len(forward_trades) == 60
    assert status["forward"]["avg_daily_pnl"] == 10_500.0
    assert status["status"] == "invalidated_non_executable_selection"
    assert not status["selection_data_executable"]
    assert not status["accepted"]
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]


def test_future_results_cannot_change_frozen_hypothesis_definition():
    before = dict(HYPOTHESIS)
    dates = [
        stamp.strftime("%Y-%m-%d")
        for stamp in pd.bdate_range("2026-07-28", periods=20)
    ]

    status, *_ = evaluate_fixed_hypothesis(
        pd.DataFrame([_event(date, -1.1) for date in dates]),
        dates,
    )

    assert HYPOTHESIS == before
    assert status["hypothesis"] == before
    assert status["status"] == "invalidated_non_executable_selection"
    assert not status["paper_promotion_allowed"]
    assert not status["real_orders_allowed"]

import pandas as pd

from scripts.simulate_causal_intraday_capital_reuse import (
    simulate_calendar,
    simulate_day,
)


def _candidate(symbol, entry, exit_time, return_pct, score=1.0):
    return {
        "date": "2026-01-02",
        "symbol": symbol,
        "entry_time": entry,
        "exit_time__test": exit_time,
        "return__test": return_pct,
        "selection_score": score,
    }


def test_full_capital_reuses_only_after_exact_prior_exit_bar():
    candidates = pd.DataFrame(
        [
            _candidate("AAA", "2026-01-02 09:35", "2026-01-02 09:40", 1.0),
            _candidate("BBB", "2026-01-02 09:50", "2026-01-02 10:00", 1.5),
        ]
    )
    summary, trades = simulate_day(
        candidates, 400_000.0, 1, "return__test", "exit_time__test"
    )
    assert summary["executed_trades"] == 2
    assert summary["pnl"] == 10_000.0
    assert summary["capital_reuse"] == "exact_exit_bar_unleveraged"
    assert trades["executed"].all()


def test_same_candle_exit_cannot_fund_new_entry():
    candidates = pd.DataFrame(
        [
            _candidate("AAA", "2026-01-02 09:35", "2026-01-02 09:50", 1.0),
            _candidate("BBB", "2026-01-02 09:50", "2026-01-02 10:00", 1.5),
        ]
    )
    summary, trades = simulate_day(
        candidates, 400_000.0, 1, "return__test", "exit_time__test"
    )
    assert summary["executed_trades"] == 1
    assert summary["pnl"] == 4_000.0
    assert trades.iloc[1]["skip_reason"] == "all_slots_busy"


def test_calendar_includes_cash_days_and_goal_result():
    candidates = pd.DataFrame(
        [_candidate("AAA", "2026-01-02 09:35", "2026-01-02 09:40", 3.0)]
    )
    daily, _ = simulate_calendar(
        candidates,
        ["2026-01-02", "2026-01-05"],
        400_000.0,
        1,
        "return__test",
        "exit_time__test",
        10_000.0,
        extra_cost_pct=0.1,
    )
    assert daily.iloc[0]["pnl"] == 11_600.0
    assert bool(daily.iloc[0]["daily_goal_hit"])
    assert daily.iloc[1]["pnl"] == 0.0
    assert not bool(daily.iloc[1]["daily_goal_hit"])


def test_calendar_preserves_float_regression_selection_labels():
    candidates = pd.DataFrame(
        [_candidate("AAA", "2026-01-02 09:35", "2026-01-02 09:40", 1.0)]
    )
    candidates["selection_label"] = 0.375

    _, trades = simulate_calendar(
        candidates,
        ["2026-01-02"],
        400_000.0,
        1,
        "return__test",
        "exit_time__test",
        10_000.0,
    )

    assert trades["selection_label"].tolist() == [0.375]


def test_daily_trade_cap_blocks_eleventh_reused_slot():
    candidates = pd.DataFrame(
        [
            _candidate(
                f"S{index:02d}",
                f"2026-01-02 {9 + (35 + index * 10) // 60:02d}:"
                f"{(35 + index * 10) % 60:02d}",
                f"2026-01-02 {9 + (40 + index * 10) // 60:02d}:"
                f"{(40 + index * 10) % 60:02d}",
                0.5,
            )
            for index in range(11)
        ]
    )

    summary, trades = simulate_day(
        candidates,
        500_000.0,
        1,
        "return__test",
        "exit_time__test",
    )

    assert summary["executed_trades"] == 10
    assert summary["max_trades_per_day"] == 10
    assert trades.iloc[-1]["skip_reason"] == "daily_trade_cap"

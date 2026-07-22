import pandas as pd

from scripts.assess_live_rank_migration import (
    FEATURES,
    add_causal_rank_features,
    replay,
    train_frozen_model,
)


def _row(symbol, when, change, value=1_000_000, sector="TECH"):
    return {
        "date": "2026-07-21",
        "symbol": symbol,
        "signal_time": pd.Timestamp(when),
        "Sector": sector,
        "DayChangePct": change,
        "TradedValue": value,
    }


def test_rank_features_include_same_timestamp_batch_and_exclude_future_rows():
    rows = [
        _row("A", "2026-07-21 09:20:00", 1.0),
        _row("B", "2026-07-21 09:20:00", 2.0),
        _row("C", "2026-07-21 09:20:00", 3.0),
        _row("D", "2026-07-21 09:20:00", 4.0),
        _row("E", "2026-07-21 09:20:00", 5.0),
        _row("Z", "2026-07-21 09:21:00", 20.0),
    ]

    enriched = add_causal_rank_features(pd.DataFrame(rows))
    first = enriched[enriched["signal_time"].eq(pd.Timestamp("2026-07-21 09:20:00"))]

    assert set(first["RecentCandidateCount"]) == {5.0}
    assert first.set_index("symbol").loc["A", "CandidateRankPct15m"] == 0.2
    assert first.set_index("symbol").loc["E", "CandidateRankPct15m"] == 1.0
    assert first["CandidateRankPct15m"].max() == 1.0


def test_rank_migration_uses_only_previous_observation_of_same_symbol():
    rows = [
        _row("A", "2026-07-21 09:20:00", 1.0),
        _row("B", "2026-07-21 09:20:00", 2.0),
        _row("C", "2026-07-21 09:20:00", 3.0),
        _row("D", "2026-07-21 09:20:00", 4.0),
        _row("E", "2026-07-21 09:20:00", 5.0),
        _row("A", "2026-07-21 09:25:00", 6.0),
    ]

    enriched = add_causal_rank_features(pd.DataFrame(rows))
    moved = enriched[
        enriched["signal_time"].eq(pd.Timestamp("2026-07-21 09:25:00"))
    ].iloc[0]

    assert moved["CandidateRankPct15m"] == 1.0
    assert moved["CandidateRankDelta"] == 0.8
    assert round(moved["CandidateRankVelocityPerMinute"], 4) == 0.16
    assert moved["RepeatDayChangeVelocity"] == 1.0
    assert moved["EnteredLeaderQuintile"] == 1.0


def test_stale_candidates_leave_the_rank_window():
    rows = [
        _row("A", "2026-07-21 09:20:00", 1.0),
        _row("B", "2026-07-21 09:20:00", 2.0),
        _row("C", "2026-07-21 09:20:00", 3.0),
        _row("D", "2026-07-21 09:20:00", 4.0),
        _row("E", "2026-07-21 09:20:00", 5.0),
        _row("Z", "2026-07-21 09:36:00", 6.0),
    ]

    enriched = add_causal_rank_features(pd.DataFrame(rows))
    last = enriched.iloc[-1]

    assert last["RecentCandidateCount"] == 1.0
    assert pd.isna(last["CandidateRankPct15m"])


def test_frozen_rank_model_records_cutoff_threshold_and_paper_only_contract():
    rows = []
    for day in range(1, 9):
        date = f"2026-07-{day:02d}"
        for index in range(70):
            row = {feature: float(index % 7) for feature in FEATURES}
            row.update(
                {
                    "date": date,
                    "symbol": f"S{index:03d}",
                    "signal_time": pd.Timestamp(f"{date} 10:00:00"),
                    "trade_return_pct": (index % 5 - 2) / 10.0,
                }
            )
            rows.append(row)

    _, metadata = train_frozen_model(
        pd.DataFrame(rows),
        frozen_through="2026-07-08",
        threshold=0.33,
    )

    assert metadata["trained_through"] == "2026-07-08"
    assert metadata["valid_from"] == "2026-07-09"
    assert metadata["training_sessions"] == 8
    assert metadata["training_rows"] == 560
    assert metadata["threshold"] == 0.33
    assert metadata["paper_only"] is True
    assert metadata["real_orders_allowed"] is False


def test_empty_forward_replay_keeps_zero_trade_calendar_days():
    daily, trades = replay(
        pd.DataFrame(),
        ["2026-07-22", "2026-07-23"],
        threshold=0.2,
        capital=500_000,
        max_trades_per_day=10,
        extra_cost_pct=0.1,
    )

    assert daily["date"].tolist() == ["2026-07-22", "2026-07-23"]
    assert daily["trades"].sum() == 0
    assert trades.empty

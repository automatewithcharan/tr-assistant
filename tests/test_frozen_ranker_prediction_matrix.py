import pandas as pd

from pkscreener.classes.FullUniverseRankContractMatrixObserver import (
    CONTRACTS,
)
from scripts.assess_frozen_ranker_prediction_matrix import (
    build_assessment,
    replay_policy,
    valid_session_data,
)


def _evidence(session_count=32, winning_return=3.0):
    predictions = []
    registrations = []
    outcomes = []
    coverage = []
    winning_contract = next(iter(CONTRACTS))
    for index in range(session_count):
        date = (
            pd.Timestamp("2026-07-28") + pd.offsets.BDay(index)
        ).strftime("%Y-%m-%d")
        symbol = f"S{index:03d}"
        when = pd.Timestamp(f"{date} 09:30:00")
        model_date = (
            pd.Timestamp(date) - pd.offsets.BDay(1)
        ).strftime("%Y-%m-%d")
        model_hash = f"{index:064x}"
        event_id = f"{date}:{symbol}:1-2:{model_hash[:16]}"
        predictions.append(
            {
                "date": date,
                "when": when,
                "symbol": symbol,
                "move_band": "1-2",
                "expected_return_pct": 0.3,
                "selected": False,
                "model_trained_through": model_date,
                "model_source_sha256": model_hash,
            }
        )
        registrations.append(
            {
                "EventId": event_id,
                "Date": date,
                "EntryAt": when,
                "Symbol": symbol,
                "EntryAsk": 100.0,
                "ExpectedReturnPct": 0.3,
                "MoveBand": "1-2",
                "ModelTrainedThrough": model_date,
                "ModelSourceSha256": model_hash,
            }
        )
        for contract in CONTRACTS:
            outcomes.append(
                {
                    "EventId": event_id,
                    "Contract": contract,
                    "Date": date,
                    "EntryAt": when,
                    "ExitAt": when + pd.Timedelta(minutes=10),
                    "Symbol": symbol,
                    "NetReturnPct": (
                        winning_return
                        if contract == winning_contract
                        else 0.0
                    ),
                    "ExpectedReturnPct": 0.3,
                    "ModelTrainedThrough": model_date,
                    "ModelSourceSha256": model_hash,
                }
            )
        coverage.append(
            {"date": date, "full_session_eligible": True}
        )
    return tuple(
        pd.DataFrame(rows)
        for rows in (
            predictions,
            registrations,
            outcomes,
            coverage,
        )
    )


def test_matrix_selects_on_first_twelve_and_accepts_only_strong_forward():
    predictions, registrations, outcomes, coverage = _evidence()

    status, summary, daily, trades = build_assessment(
        predictions,
        registrations,
        outcomes,
        coverage,
        threshold=0.2,
        capital=500_000,
        daily_goal=10_000,
        extra_cost_pct=0.1,
        selection_sessions=12,
        minimum_forward_sessions=20,
        minimum_forward_trade_sessions=10,
        minimum_forward_trades=20,
        max_trades_per_day=10,
    )

    assert len(summary) == len(CONTRACTS) * 3
    assert status["selection_sessions"] == 12
    assert status["forward"]["sessions"] == 20
    assert status["forward"]["trades"] == 20
    assert status["selected_policy"]["slots"] == 1
    assert status["accepted"] is True
    assert status["paper_promotion_allowed"] is False
    assert status["real_orders_allowed"] is False
    assert set(daily["period"]) == {"selection", "forward"}
    assert len(trades) == 32


def test_matrix_cannot_accept_before_untouched_forward_sample_exists():
    predictions, registrations, outcomes, coverage = _evidence(
        session_count=15
    )

    status, *_ = build_assessment(
        predictions,
        registrations,
        outcomes,
        coverage,
        threshold=0.2,
        capital=500_000,
        daily_goal=10_000,
        extra_cost_pct=0.1,
        selection_sessions=12,
        minimum_forward_sessions=20,
        minimum_forward_trade_sessions=10,
        minimum_forward_trades=20,
        max_trades_per_day=10,
    )

    assert status["selection_sessions"] == 12
    assert status["forward"]["sessions"] == 3
    assert status["accepted"] is False
    assert status["status"] == "collecting_untouched_forward_sessions"


def test_incomplete_contract_set_invalidates_session():
    predictions, registrations, outcomes, _ = _evidence(
        session_count=1
    )
    outcomes = outcomes[
        ~outcomes["Contract"].eq(next(iter(CONTRACTS)))
    ]

    valid, issues, _ = valid_session_data(
        "2026-07-28",
        predictions,
        registrations,
        outcomes,
        threshold=0.2,
        expected_contracts=set(CONTRACTS),
    )

    assert valid is False
    assert any("contract set mismatch" in issue for issue in issues)


def test_replay_enforces_slot_occupancy_and_same_time_score_order():
    date = "2026-07-28"
    registrations = pd.DataFrame(
        [
            {
                "EventId": "A",
                "Date": date,
                "EntryAt": pd.Timestamp(f"{date} 09:30:00"),
                "Symbol": "AAA",
                "ExpectedReturnPct": 0.3,
            },
            {
                "EventId": "B",
                "Date": date,
                "EntryAt": pd.Timestamp(f"{date} 09:30:00"),
                "Symbol": "BBB",
                "ExpectedReturnPct": 0.4,
            },
            {
                "EventId": "C",
                "Date": date,
                "EntryAt": pd.Timestamp(f"{date} 09:41:00"),
                "Symbol": "CCC",
                "ExpectedReturnPct": 0.2,
            },
        ]
    )
    outcomes = pd.DataFrame(
        [
            {
                "EventId": event,
                "Contract": "fast",
                "ExitAt": pd.Timestamp(f"{date} {exit_time}"),
                "NetReturnPct": 1.0,
            }
            for event, exit_time in (
                ("A", "09:40:00"),
                ("B", "09:40:00"),
                ("C", "09:50:00"),
            )
        ]
    )

    daily, trades = replay_policy(
        [date],
        registrations,
        outcomes,
        contract="fast",
        slots=1,
        capital=500_000,
        extra_cost_pct=0.1,
        max_trades_per_day=10,
    )

    assert trades["symbol"].tolist() == ["BBB", "CCC"]
    assert daily.iloc[0]["trades"] == 2
    assert daily.iloc[0]["pnl"] == 9000.0

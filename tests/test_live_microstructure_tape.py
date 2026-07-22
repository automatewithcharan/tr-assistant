import pandas as pd

from scripts.evaluate_live_microstructure_tape import (
    candidate_rules,
    executable_outcome,
    minute_candle_outcome,
    minute_candle_short_outcome,
    summarize,
)


def test_executable_outcome_buys_ask_and_sells_future_bid():
    group = pd.DataFrame(
        {
            "RowId": [1, 2, 3],
            "When": pd.to_datetime(["2026-01-02 09:30", "2026-01-02 09:31", "2026-01-02 09:32"]),
            "LastPrice": [100.0, 100.4, 100.8],
            "BestAsk": [100.1, 100.5, 100.9],
            "BestBid": [99.9, 100.3, 100.7],
        }
    )
    outcome = executable_outcome(group, 1, target_pct=0.5, stop_pct=0.5, horizon_minutes=30, fee_pct=0.05)
    assert outcome["ExitReason"] == "target"
    assert outcome["EntryAsk"] == 100.1
    assert outcome["ExitWhen"] == "2026-01-02 09:32:00"
    assert outcome["NetReturnPct"] == 0.45


def test_executable_outcome_uses_observed_stop_slippage():
    group = pd.DataFrame(
        {
            "RowId": [1, 2],
            "When": pd.to_datetime(["2026-01-02 09:30", "2026-01-02 09:31"]),
            "LastPrice": [100.0, 99.0],
            "BestAsk": [100.1, 99.1],
            "BestBid": [99.9, 98.9],
        }
    )
    outcome = executable_outcome(group, 1, target_pct=1.0, stop_pct=0.5, horizon_minutes=30, fee_pct=0.05)
    assert outcome["ExitReason"] == "stop"
    assert outcome["NetReturnPct"] < -1.0


def test_executable_outcome_does_not_use_last_price_without_future_bid():
    group = pd.DataFrame(
        {
            "RowId": [1, 2],
            "When": pd.to_datetime(["2026-01-02 09:30", "2026-01-02 09:31"]),
            "LastPrice": [100.0, 101.0],
            "BestAsk": [100.1, 0.0],
            "BestBid": [99.9, 0.0],
        }
    )
    outcome = executable_outcome(group, 1, 0.5, 0.5, 30, 0.10)
    assert outcome == {"OutcomeAvailable": False}


def test_candidate_rules_require_executable_two_sided_quote():
    tape = pd.DataFrame(
        {
            "When": pd.to_datetime(["2026-01-02 09:30"]),
            "LastPrice": [100.0],
            "BestBid": [0.0],
            "BestAsk": [0.0],
            "SpreadOk": [True],
            "SpreadBps": [0.0],
            "TradedValue": [50_000_000.0],
            "DayChangePct": [1.0],
            "PullbackFromHighPct": [0.1],
            "OrderBookStable": [True],
            "BuyerSellerRatio": [3.0],
            "BuyerSellerMedianWindow": [2.0],
            "BuyerSellerMinWindow": [1.5],
            "SectorBreadthPct": [75.0],
            "SectorTop2Count": [3],
            "IsTopGainerNow": [True],
            "MissedRunnerCandidate": [True],
        }
    )
    assert not any(mask.iloc[0] for mask in candidate_rules(tape).values())


def test_summary_marks_contract_from_training_only():
    rows = []
    for target, returns in [(0.5, [0.4, 0.4, -1.0]), (1.0, [0.2, 0.2, 1.0])]:
        for date, net in zip(["2026-01-01", "2026-01-02", "2026-01-03"], returns):
            rows.append(
                {
                    "Rule": "stable_depth",
                    "TargetPct": target,
                    "StopPct": 0.5,
                    "Date": date,
                    "Symbol": "AAA",
                    "NetReturnPct": net,
                    "OutcomeAvailable": True,
                    "FirstBatchSelected": True,
                }
            )
    result = summarize(pd.DataFrame(rows), ["2026-01-01", "2026-01-02", "2026-01-03"], validation_days=1)
    selected = result[result["TrainSelected"]].iloc[0]
    assert selected["TargetPct"] == 0.5
    assert selected["ValidationAvgDailyPct"] == -1.0


def test_minute_outcome_enters_only_on_next_full_minute():
    candles = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                ["2026-01-02 09:30", "2026-01-02 09:31", "2026-01-02 09:32"]
            ),
            "open": [99.0, 100.0, 100.3],
            "high": [110.0, 100.2, 101.2],
            "low": [90.0, 99.9, 100.2],
            "close": [100.0, 100.1, 101.0],
        }
    )
    outcome = minute_candle_outcome(
        candles,
        pd.Timestamp("2026-01-02 09:30:30"),
        target_pct=0.5,
        stop_pct=0.5,
        horizon_minutes=30,
        fee_pct=0.10,
        slippage_bps=0.0,
    )
    assert outcome["EntryAsk"] == 100.0
    assert outcome["ExitReason"] == "target"
    assert outcome["NetReturnPct"] == 0.4


def test_minute_outcome_resolves_ambiguous_bar_as_stop_first():
    candles = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-01-02 09:31"]),
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
        }
    )
    outcome = minute_candle_outcome(
        candles,
        pd.Timestamp("2026-01-02 09:30:10"),
        target_pct=0.5,
        stop_pct=0.5,
        horizon_minutes=30,
        fee_pct=0.10,
        slippage_bps=5.0,
    )
    assert outcome["ExitReason"] == "stop_first_same_bar"
    assert outcome["NetReturnPct"] < -0.64


def test_minute_short_enters_next_minute_and_hits_target():
    candles = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                ["2026-01-02 09:30", "2026-01-02 09:31", "2026-01-02 09:32"]
            ),
            "open": [110.0, 100.0, 99.7],
            "high": [120.0, 100.2, 99.8],
            "low": [90.0, 99.8, 99.0],
            "close": [100.0, 99.9, 99.2],
        }
    )
    outcome = minute_candle_short_outcome(
        candles,
        pd.Timestamp("2026-01-02 09:30:30"),
        target_pct=0.5,
        stop_pct=0.5,
        horizon_minutes=30,
        fee_pct=0.10,
        slippage_bps=0.0,
    )
    assert outcome["EntryBid"] == 100.0
    assert outcome["ExitReason"] == "target"
    assert outcome["NetReturnPct"] == 0.4


def test_minute_short_resolves_ambiguous_bar_as_stop_first():
    candles = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-01-02 09:31"]),
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [99.5],
        }
    )
    outcome = minute_candle_short_outcome(
        candles,
        pd.Timestamp("2026-01-02 09:30:10"),
        target_pct=0.5,
        stop_pct=0.5,
        horizon_minutes=30,
        fee_pct=0.10,
        slippage_bps=5.0,
    )
    assert outcome["ExitReason"] == "stop_first_same_bar"
    assert outcome["NetReturnPct"] < -0.64

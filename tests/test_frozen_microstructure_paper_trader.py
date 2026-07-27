from datetime import datetime, timedelta

import pandas as pd
from sklearn.dummy import DummyRegressor

from pkscreener.classes.FrozenMicrostructureModel import (
    CONTRACT,
    MODEL_FEATURES,
    save_model_artifact,
)
from pkscreener.classes.FrozenMicrostructurePaperTrader import (
    FrozenMicrostructurePaperTrader,
)


def _artifact(tmp_path, trained_through="2026-07-21"):
    model = DummyRegressor(strategy="constant", constant=0.30)
    model.fit(pd.DataFrame([[0.0] * len(MODEL_FEATURES)], columns=MODEL_FEATURES), [0.3])
    metadata = {
        "artifact_type": "prior_session_frozen_microstructure_ranker",
        "contract": CONTRACT,
        "trained_through": trained_through,
        "valid_from": "2026-07-22",
        "training_sessions": 12,
        "training_rows": 1000,
        "training_start": "2026-07-01",
        "features": MODEL_FEATURES,
        "min_samples_leaf": 30,
        "equal_session_weights": True,
        "source_sha256": "test-hash",
        "paper_only": True,
        "real_orders_allowed": False,
        "threshold": 0.20,
    }
    path = tmp_path / "model.joblib"
    save_model_artifact(path, model, metadata)
    return path


def _row(when, move, bs=2.0):
    return {
        "When": when.strftime("%Y-%m-%d %H:%M:%S"),
        "Symbol": "AAA",
        "LastPrice": 100.0,
        "DayChangePct": move,
        "OpenChangePct": move,
        "PullbackFromHighPct": 0.1,
        "TradedValue": 50_000_000.0,
        "BuyerSellerRatio": bs,
        "BuyerSellerMedianWindow": bs,
        "BuyerSellerMinWindow": bs,
        "OrderBookStable": True,
        "BestBid": 99.9,
        "BestAsk": 100.1,
        "BestBidQty": 10000,
        "BestAskQty": 10000,
        "SpreadBps": 19.99,
        "SectorAvgChangePct": 0.2,
        "SectorBreadthPct": 60.0,
        "SectorTop2Count": 2,
        "SectorTop5Count": 0,
        "MarketBreadthPct": 55.0,
        "IsTopGainerNow": False,
        "MissedRunnerCandidate": False,
    }


def test_live_paper_prediction_precedes_executable_trade_and_exit(tmp_path):
    trader = FrozenMicrostructurePaperTrader(
        output_dir=str(tmp_path), model_path=str(_artifact(tmp_path)), persist=True
    )
    start = datetime(2026, 7, 22, 9, 18)
    ticks = {
        "AAA": {
            "best_bid": 99.9,
            "best_bid_quantity": 10000,
        }
    }

    assert trader.process(pd.DataFrame([_row(start, 0.55)]), ticks, now=start) == []
    assert trader.process(
        pd.DataFrame([_row(start + timedelta(minutes=1), 0.65)]),
        ticks,
        now=start + timedelta(minutes=1),
    ) == []
    events = trader.process(
        pd.DataFrame([_row(start + timedelta(minutes=2), 0.85)]),
        ticks,
        now=start + timedelta(minutes=2),
    )

    assert [event["event"] for event in events] == ["paper_entry"]
    assert events[0]["model_trained_through"] == "2026-07-21"
    assert events[0]["entry_price"] == 100.1
    assert events[0]["entry_top_level_fillable"] is True
    predictions = pd.read_csv(
        tmp_path / "frozen_microstructure_paper" / "predictions_2026-07-22.csv"
    )
    assert predictions.iloc[0]["selected"] in (True, "True")
    assert predictions.iloc[0]["expected_return_pct"] == 0.3
    contract_entries = trader.contract_entry_events()
    assert len(contract_entries) == 1
    assert contract_entries[0]["EntryAsk"] == 100.1
    assert contract_entries[0]["ExpectedReturnPct"] == 0.3
    assert contract_entries[0]["ModelTrainedThrough"] == "2026-07-21"
    assert contract_entries[0]["Source"] == "prior_session_frozen_ranker"

    exits = trader.process(
        pd.DataFrame(),
        {
            "AAA": {
                "best_bid": 101.7,
                "best_bid_quantity": 10000,
            }
        },
        now=start + timedelta(minutes=3),
    )
    assert [event["event"] for event in exits] == ["paper_exit"]
    assert exits[0]["exit_reason"] == "target"
    assert exits[0]["exit_top_level_fillable"] is True
    assert exits[0]["net_pnl"] > 0


def test_same_day_model_cannot_trade(tmp_path):
    trader = FrozenMicrostructurePaperTrader(
        output_dir=str(tmp_path),
        model_path=str(_artifact(tmp_path, trained_through="2026-07-22")),
        persist=False,
    )
    start = datetime(2026, 7, 22, 9, 18)
    for offset, move in enumerate((0.55, 0.65, 0.85)):
        events = trader.process(
            pd.DataFrame([_row(start + timedelta(minutes=offset), move)]),
            {
                "AAA": {
                    "best_bid": 99.9,
                    "best_bid_quantity": 10000,
                }
            },
            now=start + timedelta(minutes=offset),
        )
    assert events == []
    assert trader.trades == []


def test_high_frequency_standalone_rows_keep_model_history_near_two_minutes(
    tmp_path,
):
    trader = FrozenMicrostructurePaperTrader(
        output_dir=str(tmp_path),
        model_path=str(_artifact(tmp_path)),
        persist=False,
    )
    start = datetime(2026, 7, 22, 9, 18)
    ticks = {
        "AAA": {
            "best_bid": 99.9,
            "best_bid_quantity": 10000,
        }
    }
    entries = []
    for offset in range(0, 121, 15):
        now = start + timedelta(seconds=offset)
        entries.extend(
            trader.process(
                pd.DataFrame([_row(now, 0.55 + offset / 1000.0)]),
                ticks,
                now=now,
            )
        )

    assert len(entries) == 1
    assert entries[0]["entry_time"].endswith("09:20:00")
    assert trader.latest_predictions[0]["SnapshotSpanMinutes"] == 2.0

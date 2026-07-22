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
    ticks = {"AAA": {"best_bid": 99.9}}

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
    predictions = pd.read_csv(
        tmp_path / "frozen_microstructure_paper" / "predictions_2026-07-22.csv"
    )
    assert predictions.iloc[0]["selected"] in (True, "True")
    assert predictions.iloc[0]["expected_return_pct"] == 0.3

    exits = trader.process(
        pd.DataFrame(), {"AAA": {"best_bid": 101.7}},
        now=start + timedelta(minutes=3),
    )
    assert [event["event"] for event in exits] == ["paper_exit"]
    assert exits[0]["exit_reason"] == "target"
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
            {"AAA": {"best_bid": 99.9}},
            now=start + timedelta(minutes=offset),
        )
    assert events == []
    assert trader.trades == []

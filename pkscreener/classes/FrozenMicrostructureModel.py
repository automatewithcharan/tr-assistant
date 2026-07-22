"""Shared model contract for the frozen live microstructure ranker."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline


CONTRACT = "t1.5_s1.0"
MODEL_FEATURES = [
    "signal_minute",
    "DayChangePct",
    "OpenChangePct",
    "PullbackFromHighPct",
    "log_traded_value",
    "BuyerSellerRatio",
    "BuyerSellerMedianWindow",
    "BuyerSellerMinWindow",
    "OrderBookStable",
    "SpreadBps",
    "SectorAvgChangePct",
    "SectorBreadthPct",
    "SectorTop2Count",
    "SectorTop5Count",
    "MarketBreadthPct",
    "IsTopGainerNow",
    "MissedRunnerCandidate",
    "SnapshotSpanMinutes",
    "MoveAccelerationPct",
    "MoveVelocityPctPerMinute",
    "BuyerSellerSurge",
    "SectorRelativePct",
    "band_0_5_1",
    "band_1_2",
    "band_2_3",
    "band_3_5",
]


def build_model(min_samples_leaf: int = 30) -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            (
                "model",
                HistGradientBoostingRegressor(
                    loss="squared_error",
                    max_iter=80,
                    learning_rate=0.04,
                    max_leaf_nodes=10,
                    min_samples_leaf=int(min_samples_leaf),
                    l2_regularization=3.0,
                    random_state=42,
                ),
            ),
        ]
    )


def next_session_date(value: str) -> str:
    current = date.fromisoformat(str(value))
    current += timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current.isoformat()


def train_prior_session_model(
    frame: pd.DataFrame,
    *,
    trained_through: str,
    contract: str = CONTRACT,
    min_train_days: int = 8,
    min_train_rows: int = 500,
    min_samples_leaf: int = 30,
) -> Tuple[Pipeline, Dict[str, Any]]:
    train = frame[frame["date"].astype(str).le(str(trained_through))].copy()
    sessions = sorted(train["date"].astype(str).unique().tolist())
    if len(sessions) < int(min_train_days):
        raise ValueError(
            f"need {min_train_days} completed sessions, found {len(sessions)}"
        )
    if len(train) < int(min_train_rows):
        raise ValueError(f"need {min_train_rows} training rows, found {len(train)}")
    day_counts = train.groupby("date")["symbol"].transform("count").clip(lower=1)
    weights = 1.0 / day_counts.to_numpy(dtype=float)
    model = build_model(min_samples_leaf=min_samples_leaf)
    model.fit(
        train[MODEL_FEATURES],
        train["trade_return_pct"],
        model__sample_weight=weights,
    )
    identity = train[["date", "symbol", "signal_time", "trade_return_pct"]].copy()
    source_hash = hashlib.sha256(
        identity.to_csv(index=False).encode("utf-8")
    ).hexdigest()
    metadata = {
        "artifact_type": "prior_session_frozen_microstructure_ranker",
        "contract": str(contract),
        "trained_through": str(trained_through),
        "valid_from": next_session_date(str(trained_through)),
        "training_sessions": len(sessions),
        "training_rows": int(len(train)),
        "training_start": sessions[0],
        "features": list(MODEL_FEATURES),
        "min_samples_leaf": int(min_samples_leaf),
        "equal_session_weights": True,
        "source_sha256": source_hash,
        "paper_only": True,
        "real_orders_allowed": False,
    }
    return model, metadata


def save_model_artifact(
    path: str | Path,
    model: Pipeline,
    metadata: Dict[str, Any],
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": model, "metadata": dict(metadata)}
    joblib.dump(payload, destination)
    destination.with_suffix(".json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )
    return destination


def load_model_artifact(path: str | Path) -> Tuple[Pipeline, Dict[str, Any]]:
    payload = joblib.load(Path(path))
    if not isinstance(payload, dict) or "model" not in payload:
        raise ValueError("invalid frozen microstructure model artifact")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("frozen model artifact has no metadata")
    if metadata.get("contract") != CONTRACT:
        raise ValueError(f"unexpected model contract: {metadata.get('contract')}")
    if metadata.get("features") != MODEL_FEATURES:
        raise ValueError("frozen model feature contract mismatch")
    if metadata.get("real_orders_allowed") is not False:
        raise ValueError("frozen model artifact is not marked paper-only")
    return payload["model"], metadata

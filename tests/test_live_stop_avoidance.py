from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from pkscreener.classes.FrozenMicrostructurePaperTrader import (
    RANK_MIGRATION_MODEL_FEATURES,
)
from pkscreener.classes.StopAvoidanceMicrostructureModel import (
    StopAvoidanceProbabilityModel,
)
from scripts.assess_live_stop_avoidance import (
    ARTIFACT_TYPE,
    train_frozen_model,
)


def test_stop_avoidance_model_predicts_probability():
    size = 120
    features = pd.DataFrame(
        {
            name: np.linspace(0.0, 1.0, size)
            for name in RANK_MIGRATION_MODEL_FEATURES
        }
    )
    labels = (features[RANK_MIGRATION_MODEL_FEATURES[0]] > 0.5).astype(int)
    model = StopAvoidanceProbabilityModel(min_samples_leaf=5).fit(
        features,
        labels,
    )

    probabilities = model.predict(features.iloc[[0, -1]])

    assert probabilities.shape == (2,)
    assert np.all((probabilities >= 0.0) & (probabilities <= 1.0))
    assert probabilities[1] > probabilities[0]


def test_frozen_stop_model_excludes_post_cutoff_rows_and_stays_paper_only():
    rows = []
    start = datetime(2026, 7, 16, 9, 20)
    for day_index in range(10):
        current = start + timedelta(days=day_index)
        for row_index in range(60):
            row = {
                "date": current.strftime("%Y-%m-%d"),
                "symbol": f"S{row_index:03d}",
                "signal_time": current + timedelta(seconds=row_index),
                "trade_return_pct": 1.5 if row_index % 2 else -1.0,
                "avoid_stop": row_index % 2,
            }
            row.update(
                {
                    feature: float(row_index + day_index) / 100.0
                    for feature in RANK_MIGRATION_MODEL_FEATURES
                }
            )
            rows.append(row)
    future = dict(rows[-1])
    future.update(
        {
            "date": "2026-07-30",
            "symbol": "FUTURE",
            "signal_time": datetime(2026, 7, 30, 9, 20),
        }
    )
    frame = pd.DataFrame(rows + [future])

    model, metadata = train_frozen_model(
        frame,
        selection_cutoff="2026-07-29",
    )

    assert isinstance(model, StopAvoidanceProbabilityModel)
    assert metadata["artifact_type"] == ARTIFACT_TYPE
    assert metadata["trained_through"] <= "2026-07-29"
    assert metadata["training_rows"] == len(rows)
    assert metadata["valid_from"] == "2026-07-30"
    assert metadata["paper_only"] is True
    assert metadata["alerts_allowed"] is False
    assert metadata["real_orders_allowed"] is False

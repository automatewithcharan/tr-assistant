"""Paper-only probability model for avoiding a fixed intraday stop."""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline


class StopAvoidanceProbabilityModel:
    """Expose classifier probability through the paper trader's predict API."""

    def __init__(self, min_samples_leaf: int = 30) -> None:
        self.pipeline = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                (
                    "model",
                    HistGradientBoostingClassifier(
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

    def fit(
        self,
        features: Any,
        labels: Any,
        *,
        sample_weight: Any = None,
    ) -> "StopAvoidanceProbabilityModel":
        kwargs = (
            {"model__sample_weight": sample_weight}
            if sample_weight is not None
            else {}
        )
        self.pipeline.fit(features, labels, **kwargs)
        return self

    def predict(self, features: Any) -> np.ndarray:
        """Return P(no stop hit), not a hard class label."""
        probabilities = self.pipeline.predict_proba(features)
        classes = list(self.pipeline.named_steps["model"].classes_)
        if 1 not in classes:
            return np.zeros(len(features), dtype=float)
        return probabilities[:, classes.index(1)]


class RunnerQualifiedStopAvoidanceModel:
    """Rank continuation probability only after the stop-safety floor passes."""

    def __init__(
        self,
        *,
        stop_probability_floor: float = 0.78,
        min_samples_leaf: int = 30,
    ) -> None:
        self.stop_probability_floor = float(stop_probability_floor)
        self.stop_model = StopAvoidanceProbabilityModel(
            min_samples_leaf=min_samples_leaf
        )
        self.runner_model = StopAvoidanceProbabilityModel(
            min_samples_leaf=min_samples_leaf
        )

    def fit(
        self,
        features: Any,
        avoid_stop_labels: Any,
        runner_labels: Any,
        *,
        sample_weight: Any = None,
    ) -> "RunnerQualifiedStopAvoidanceModel":
        self.stop_model.fit(
            features,
            avoid_stop_labels,
            sample_weight=sample_weight,
        )
        self.runner_model.fit(
            features,
            runner_labels,
            sample_weight=sample_weight,
        )
        return self

    def predict(self, features: Any) -> np.ndarray:
        """Return runner score for safe rows and -1 for rejected rows."""
        stop_probability = self.stop_model.predict(features)
        runner_probability = self.runner_model.predict(features)
        return np.where(
            stop_probability >= self.stop_probability_floor,
            runner_probability,
            -1.0,
        )

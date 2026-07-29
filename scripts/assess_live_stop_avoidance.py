#!/usr/bin/env python3
"""Freeze and audit a paper-only stop-avoidance microstructure selector."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import joblib
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.FrozenMicrostructureModel import CONTRACT  # noqa: E402
from pkscreener.classes.FrozenMicrostructurePaperTrader import (  # noqa: E402
    RANK_MIGRATION_MODEL_FEATURES,
)
from pkscreener.classes.StopAvoidanceMicrostructureModel import (  # noqa: E402
    RunnerQualifiedStopAvoidanceModel,
    StopAvoidanceProbabilityModel,
)
from scripts.walk_forward_live_microstructure_ranker import (  # noqa: E402
    metrics,
    replay_exact_capital,
)


ARTIFACT_TYPE = "prior_session_rank_migration_stop_avoidance"
CHALLENGER_ARTIFACT_TYPE = (
    "prior_session_runner_qualified_stop_avoidance"
)
SELECTION_CUTOFF = "2026-07-29"
FORWARD_START = "2026-07-30"
THRESHOLD = 0.78
MAX_TRADES_PER_DAY = 5
CHALLENGER_THRESHOLD = 0.0
CHALLENGER_MAX_TRADES_PER_DAY = 10
BALANCED_STOP_WEIGHT = 0.50
MIN_TRAIN_DAYS = 8
MIN_TRAIN_ROWS = 500


def load_features(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path, low_memory=False)
    required = {
        "date",
        "symbol",
        "signal_time",
        "exit_time",
        "trade_return_pct",
        "ExitReason",
        "MfePct",
        *RANK_MIGRATION_MODEL_FEATURES,
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "stop-avoidance input missing columns: "
            + ", ".join(sorted(missing))
        )
    frame["date"] = frame["date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["signal_time"] = pd.to_datetime(
        frame["signal_time"], errors="coerce"
    )
    frame["exit_time"] = pd.to_datetime(
        frame["exit_time"], errors="coerce"
    )
    frame["trade_return_pct"] = pd.to_numeric(
        frame["trade_return_pct"], errors="coerce"
    )
    frame["MfePct"] = pd.to_numeric(frame["MfePct"], errors="coerce")
    frame["avoid_stop"] = (
        frame["ExitReason"].fillna("").astype(str).str.lower().ne("stop")
    ).astype(int)
    for feature in RANK_MIGRATION_MODEL_FEATURES:
        frame[feature] = pd.to_numeric(frame[feature], errors="coerce")
    frame = frame.dropna(
        subset=[
            "signal_time",
            "exit_time",
            "trade_return_pct",
            "DayChangePct",
            "MfePct",
        ]
    )
    future_peak_day_change = (
        (1.0 + frame["DayChangePct"] / 100.0)
        * (1.0 + frame["MfePct"].clip(lower=0.0) / 100.0)
        - 1.0
    ) * 100.0
    frame["reaches_five_pct_leader_state"] = (
        future_peak_day_change >= 5.0
    ).astype(int)
    return frame[
        frame["exit_time"].gt(frame["signal_time"])
    ].sort_values(["date", "signal_time", "symbol"]).reset_index(drop=True)


def _fit(train: pd.DataFrame) -> StopAvoidanceProbabilityModel:
    day_counts = train.groupby("date")["symbol"].transform("count").clip(
        lower=1
    )
    model = StopAvoidanceProbabilityModel(min_samples_leaf=30)
    return model.fit(
        train[RANK_MIGRATION_MODEL_FEATURES],
        train["avoid_stop"],
        sample_weight=1.0 / day_counts.to_numpy(dtype=float),
    )


def _fit_challenger(
    train: pd.DataFrame,
) -> RunnerQualifiedStopAvoidanceModel:
    day_counts = train.groupby("date")["symbol"].transform("count").clip(
        lower=1
    )
    model = RunnerQualifiedStopAvoidanceModel(
        stop_probability_floor=THRESHOLD,
        min_samples_leaf=30,
    )
    return model.fit(
        train[RANK_MIGRATION_MODEL_FEATURES],
        train["avoid_stop"],
        train["reaches_five_pct_leader_state"],
        sample_weight=1.0 / day_counts.to_numpy(dtype=float),
    )


def score_walk_forward(frame: pd.DataFrame) -> pd.DataFrame:
    dates = sorted(frame["date"].unique().tolist())
    groups = {
        date: day.copy() for date, day in frame.groupby("date", sort=False)
    }
    scored = []
    for index, date in enumerate(dates):
        prior_dates = dates[max(0, index - 40):index]
        if len(prior_dates) < MIN_TRAIN_DAYS:
            continue
        train = pd.concat(
            [groups[value] for value in prior_dates], ignore_index=True
        )
        if len(train) < MIN_TRAIN_ROWS:
            continue
        day = groups[date].copy()
        model = _fit(train)
        day["expected_return_pct"] = model.predict(
            day[RANK_MIGRATION_MODEL_FEATURES]
        )
        day["model_trained_through"] = prior_dates[-1]
        scored.append(day)
    return (
        pd.concat(scored, ignore_index=True)
        if scored
        else pd.DataFrame()
    )


def score_challenger_walk_forward(frame: pd.DataFrame) -> pd.DataFrame:
    dates = sorted(frame["date"].unique().tolist())
    groups = {
        date: day.copy() for date, day in frame.groupby("date", sort=False)
    }
    scored = []
    for index, date in enumerate(dates):
        prior_dates = dates[max(0, index - 40):index]
        if len(prior_dates) < MIN_TRAIN_DAYS:
            continue
        train = pd.concat(
            [groups[value] for value in prior_dates], ignore_index=True
        )
        if len(train) < MIN_TRAIN_ROWS:
            continue
        day = groups[date].copy()
        model = _fit_challenger(train)
        day["expected_return_pct"] = model.predict(
            day[RANK_MIGRATION_MODEL_FEATURES]
        )
        day["model_trained_through"] = prior_dates[-1]
        scored.append(day)
    return (
        pd.concat(scored, ignore_index=True)
        if scored
        else pd.DataFrame()
    )


def train_frozen_model(
    frame: pd.DataFrame,
    *,
    selection_cutoff: str = SELECTION_CUTOFF,
) -> tuple[StopAvoidanceProbabilityModel, dict[str, Any]]:
    train = frame[frame["date"].le(str(selection_cutoff))].copy()
    dates = sorted(train["date"].unique().tolist())
    if len(dates) < MIN_TRAIN_DAYS:
        raise ValueError(
            f"need {MIN_TRAIN_DAYS} training sessions, found {len(dates)}"
        )
    if len(train) < MIN_TRAIN_ROWS:
        raise ValueError(
            f"need {MIN_TRAIN_ROWS} training rows, found {len(train)}"
        )
    model = _fit(train)
    identity = train[
        ["date", "symbol", "signal_time", "trade_return_pct", "avoid_stop"]
    ]
    metadata = {
        "artifact_type": ARTIFACT_TYPE,
        "contract": CONTRACT,
        "objective": "probability_exit_reason_is_not_stop",
        "trained_through": dates[-1],
        "selection_cutoff": str(selection_cutoff),
        "valid_from": FORWARD_START,
        "training_sessions": len(dates),
        "training_rows": int(len(train)),
        "features": list(RANK_MIGRATION_MODEL_FEATURES),
        "threshold": THRESHOLD,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "target_pct": 1.5,
        "stop_pct": 1.0,
        "horizon_minutes": 60,
        "equal_session_weights": True,
        "source_sha256": hashlib.sha256(
            identity.to_csv(index=False).encode("utf-8")
        ).hexdigest(),
        "paper_only": True,
        "alerts_allowed": False,
        "real_orders_allowed": False,
    }
    return model, metadata


def train_frozen_challenger_model(
    frame: pd.DataFrame,
    *,
    selection_cutoff: str = SELECTION_CUTOFF,
) -> tuple[RunnerQualifiedStopAvoidanceModel, dict[str, Any]]:
    train = frame[frame["date"].le(str(selection_cutoff))].copy()
    dates = sorted(train["date"].unique().tolist())
    if len(dates) < MIN_TRAIN_DAYS:
        raise ValueError(
            f"need {MIN_TRAIN_DAYS} training sessions, found {len(dates)}"
        )
    if len(train) < MIN_TRAIN_ROWS:
        raise ValueError(
            f"need {MIN_TRAIN_ROWS} training rows, found {len(train)}"
        )
    model = _fit_challenger(train)
    identity = train[
        [
            "date",
            "symbol",
            "signal_time",
            "trade_return_pct",
            "avoid_stop",
            "reaches_five_pct_leader_state",
        ]
    ]
    metadata = {
        "artifact_type": CHALLENGER_ARTIFACT_TYPE,
        "contract": CONTRACT,
        "objective": (
            "rank_five_pct_continuation_after_stop_avoidance_floor"
        ),
        "trained_through": dates[-1],
        "selection_cutoff": str(selection_cutoff),
        "valid_from": FORWARD_START,
        "training_sessions": len(dates),
        "training_rows": int(len(train)),
        "features": list(RANK_MIGRATION_MODEL_FEATURES),
        "stop_probability_floor": THRESHOLD,
        "threshold": CHALLENGER_THRESHOLD,
        "max_trades_per_day": CHALLENGER_MAX_TRADES_PER_DAY,
        "target_pct": 1.5,
        "stop_pct": 1.0,
        "horizon_minutes": 60,
        "equal_session_weights": True,
        "source_sha256": hashlib.sha256(
            identity.to_csv(index=False).encode("utf-8")
        ).hexdigest(),
        "paper_only": True,
        "alerts_allowed": False,
        "real_orders_allowed": False,
    }
    return model, metadata


def run(
    input_path: str,
    output_dir: Path,
    *,
    capital: float = 500_000.0,
    daily_goal: float = 10_000.0,
    stress_cost_pct: float = 0.10,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frame = load_features(input_path)
    selection = frame[frame["date"].le(SELECTION_CUTOFF)].copy()
    scored = score_walk_forward(selection)
    dates = sorted(scored["date"].unique().tolist()) if not scored.empty else []
    daily, trades = replay_exact_capital(
        scored,
        dates,
        threshold=THRESHOLD,
        slots=1,
        max_trades_per_day=MAX_TRADES_PER_DAY,
        extra_cost_pct=stress_cost_pct,
        capital=capital,
    )
    selection_metrics = metrics(daily, daily_goal)
    challenger_scored = score_challenger_walk_forward(selection)
    challenger_dates = (
        sorted(challenger_scored["date"].unique().tolist())
        if not challenger_scored.empty
        else []
    )
    challenger_daily, challenger_trades = replay_exact_capital(
        challenger_scored,
        challenger_dates,
        threshold=CHALLENGER_THRESHOLD,
        slots=1,
        max_trades_per_day=CHALLENGER_MAX_TRADES_PER_DAY,
        extra_cost_pct=stress_cost_pct,
        capital=capital,
    )
    challenger_metrics = metrics(challenger_daily, daily_goal)
    balanced_daily = daily.merge(
        challenger_daily,
        on="date",
        how="inner",
        suffixes=("_stop", "_runner"),
    )
    if not balanced_daily.empty:
        balanced_daily["trades"] = (
            pd.to_numeric(
                balanced_daily["trades_stop"], errors="coerce"
            ).fillna(0)
            + pd.to_numeric(
                balanced_daily["trades_runner"], errors="coerce"
            ).fillna(0)
        ).astype(int)
        balanced_daily["pnl"] = (
            BALANCED_STOP_WEIGHT
            * pd.to_numeric(
                balanced_daily["pnl_stop"], errors="coerce"
            ).fillna(0.0)
            + (1.0 - BALANCED_STOP_WEIGHT)
            * pd.to_numeric(
                balanced_daily["pnl_runner"], errors="coerce"
            ).fillna(0.0)
        )
        balanced_daily["daily_return_pct"] = (
            balanced_daily["pnl"] / float(capital) * 100.0
        )
        balanced_daily = balanced_daily[
            ["date", "trades", "pnl", "daily_return_pct"]
        ].copy()
    balanced_metrics = metrics(balanced_daily, daily_goal)
    model, metadata = train_frozen_model(
        selection, selection_cutoff=SELECTION_CUTOFF
    )
    challenger_model, challenger_metadata = (
        train_frozen_challenger_model(
            selection,
            selection_cutoff=SELECTION_CUTOFF,
        )
    )
    joblib.dump(
        {"model": model, "metadata": metadata},
        output_dir / "frozen_stop_avoidance_model.joblib",
    )
    (output_dir / "frozen_stop_avoidance_model.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )
    joblib.dump(
        {"model": challenger_model, "metadata": challenger_metadata},
        output_dir / "frozen_runner_qualified_stop_model.joblib",
    )
    (
        output_dir / "frozen_runner_qualified_stop_model.json"
    ).write_text(
        json.dumps(challenger_metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    scored.to_csv(output_dir / "selection_predictions.csv", index=False)
    daily.to_csv(output_dir / "selection_stress_daily.csv", index=False)
    trades.to_csv(output_dir / "selection_stress_trades.csv", index=False)
    challenger_scored.to_csv(
        output_dir / "challenger_selection_predictions.csv", index=False
    )
    challenger_daily.to_csv(
        output_dir / "challenger_selection_stress_daily.csv", index=False
    )
    challenger_trades.to_csv(
        output_dir / "challenger_selection_stress_trades.csv", index=False
    )
    balanced_daily.to_csv(
        output_dir / "balanced_selection_stress_daily.csv", index=False
    )
    promising = bool(
        selection_metrics["sessions"] >= 8
        and selection_metrics["trades"] >= 30
        and selection_metrics["avg_daily_pnl"] > 0.0
        and selection_metrics["lower_95_pct"] is not None
        and float(selection_metrics["lower_95_pct"]) > 0.0
        and selection_metrics["block_floor_pct"] is not None
        and float(selection_metrics["block_floor_pct"]) > 0.0
        and float(selection_metrics["worst_session_pct"]) > -2.5
    )
    challenger_diagnostic = bool(
        challenger_metrics["sessions"] >= 8
        and challenger_metrics["trades"] >= 50
        and challenger_metrics["avg_daily_pnl"] > 0.0
        and challenger_metrics["lower_95_pct"] is not None
        and float(challenger_metrics["lower_95_pct"]) > 0.0
        and float(challenger_metrics["worst_session_pct"]) > -2.5
    )
    challenger_block_robust = bool(
        challenger_metrics["block_floor_pct"] is not None
        and float(challenger_metrics["block_floor_pct"]) > 0.0
    )
    status = {
        "evidence": "stop_avoidance_selection_diagnostic",
        "status": (
            "frozen_paper_forward_required"
            if promising
            else "selection_rejected"
        ),
        "objective": metadata["objective"],
        "contract": CONTRACT,
        "selection_cutoff": SELECTION_CUTOFF,
        "forward_start": FORWARD_START,
        "threshold": THRESHOLD,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "stress_extra_cost_pct": float(stress_cost_pct),
        "selection": selection_metrics,
        "selection_promising": promising,
        "selection_is_not_forward_evidence": True,
        "model": metadata,
        "challenger": {
            "objective": challenger_metadata["objective"],
            "selection": challenger_metrics,
            "model": challenger_metadata,
            "selection_diagnostic_positive": challenger_diagnostic,
            "selection_block_robust": challenger_block_robust,
            "diagnostic_forward_collection_allowed": (
                challenger_diagnostic
            ),
            "paper_promotion_allowed": False,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        },
        "balanced_portfolio": {
            "objective": (
                "fixed_half_capital_per_frozen_selector_to_reduce_"
                "correlated_drawdown"
            ),
            "stop_weight": BALANCED_STOP_WEIGHT,
            "runner_weight": 1.0 - BALANCED_STOP_WEIGHT,
            "selection": balanced_metrics,
            "selection_is_not_forward_evidence": True,
            "diagnostic_forward_collection_allowed": True,
            "paper_promotion_allowed": False,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        },
        "paper_collection_allowed": promising,
        "paper_promotion_allowed": False,
        "alerts_allowed": False,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument(
        "--output-dir",
        default="results/Data/live_stop_avoidance",
    )
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    args = parser.parse_args()
    try:
        status = run(
            args.input,
            Path(args.output_dir),
            capital=args.capital,
            daily_goal=args.daily_goal,
            stress_cost_pct=args.stress_cost_pct,
        )
    except ValueError as exc:
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        status = {
            "status": "insufficient_or_invalid_selection_data",
            "reason": str(exc),
            "paper_collection_allowed": False,
            "paper_promotion_allowed": False,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        (output / "status.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8"
        )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

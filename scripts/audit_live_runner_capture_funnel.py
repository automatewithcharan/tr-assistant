#!/usr/bin/env python3
"""Explain where eventual runners disappear from the live paper pipeline.

The runner label and threshold-crossing time are known only after the fact.
This audit is diagnostic: it can identify detection, execution, model, and
selection bottlenecks, but it can never promote a strategy or place an order.
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


DEFAULT_RECALL = "results/Data/live_top_gainer_recorder_recall/events.csv"
DEFAULT_PREDICTIONS = (
    "results/Data/frozen_microstructure_paper/predictions_20*.csv"
)
DEFAULT_REGISTRATIONS = (
    "results/Data/frozen_ranker_prediction_contract_matrix/"
    "registrations_20*.csv"
)
DEFAULT_BROAD_REGISTRATIONS = (
    "results/Data/full_universe_rank_paper/entries_20*.csv"
)
DEFAULT_OUTPUT = "results/Data/live_runner_capture_funnel"


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {
        "1", "1.0", "true", "yes", "y", "on"
    }


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {
            path
            for pattern in patterns
            for path in glob.glob(str(pattern), recursive=True)
        }
    )


def _market_naive(values: pd.Series) -> pd.Series:
    """Normalize mixed ISO timestamps to timezone-naive India market time."""
    def normalize(value):
        try:
            parsed = pd.Timestamp(value)
        except (TypeError, ValueError):
            return pd.NaT
        if pd.isna(parsed):
            return pd.NaT
        if parsed.tzinfo is not None:
            return parsed.tz_convert("Asia/Kolkata").tz_localize(None)
        return parsed

    return pd.to_datetime(values.map(normalize), errors="coerce")


def load_predictions(patterns: Iterable[str]) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {
            "date", "when", "symbol", "expected_return_pct", "selected",
        }
        if not required.issubset(frame.columns):
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["date"] = data["date"].astype(str).str[:10]
    data["when"] = _market_naive(data["when"])
    data["symbol"] = data["symbol"].astype(str).str.strip().str.upper()
    data["expected_return_pct"] = pd.to_numeric(
        data["expected_return_pct"], errors="coerce"
    )
    data["selected"] = data["selected"].map(_truthy)
    return data.dropna(
        subset=["date", "when", "symbol", "expected_return_pct"]
    ).drop_duplicates(["date", "when", "symbol"], keep="last")


def load_registrations(patterns: Iterable[str]) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {"Date", "EntryAt", "Symbol", "ExpectedReturnPct"}
        if not required.issubset(frame.columns):
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["Date"] = data["Date"].astype(str).str[:10]
    data["EntryAt"] = _market_naive(data["EntryAt"])
    data["Symbol"] = data["Symbol"].astype(str).str.strip().str.upper()
    data["ExpectedReturnPct"] = pd.to_numeric(
        data["ExpectedReturnPct"], errors="coerce"
    )
    if "EntryTopLevelFillable" in data.columns:
        data["EntryTopLevelFillable"] = data[
            "EntryTopLevelFillable"
        ].map(_truthy)
    else:
        data["EntryTopLevelFillable"] = pd.NA
    return data.dropna(
        subset=["Date", "EntryAt", "Symbol", "ExpectedReturnPct"]
    ).drop_duplicates(["Date", "EntryAt", "Symbol"], keep="last")


def load_broad_registrations(patterns: Iterable[str]) -> pd.DataFrame:
    """Load exact first-observation labels from the independent broad lane."""
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {"Date", "EntryAt", "Symbol"}
        if not required.issubset(frame.columns):
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["Date"] = data["Date"].astype(str).str[:10]
    data["EntryAt"] = _market_naive(data["EntryAt"])
    data["Symbol"] = data["Symbol"].astype(str).str.strip().str.upper()
    if "EntryTopLevelFillable" in data.columns:
        data["EntryTopLevelFillable"] = data[
            "EntryTopLevelFillable"
        ].map(_truthy)
    else:
        data["EntryTopLevelFillable"] = pd.NA
    return data.dropna(
        subset=["Date", "EntryAt", "Symbol"]
    ).drop_duplicates(["Date", "EntryAt", "Symbol"], keep="last")


def _safe_bool(row: pd.Series, column: str) -> bool:
    return _truthy(row.get(column, False))


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator * 100.0 / denominator, 2) if denominator else 0.0


def build_capture_funnel(
    recall: pd.DataFrame,
    predictions: pd.DataFrame,
    registrations: pd.DataFrame,
    broad_registrations: pd.DataFrame | None = None,
    *,
    score_threshold: float = 0.20,
) -> pd.DataFrame:
    """Classify each hindsight runner by its first failed causal stage."""
    if recall is None or recall.empty:
        return pd.DataFrame()

    events = recall.copy()
    events["Date"] = events["Date"].astype(str).str[:10]
    events["Symbol"] = events["Symbol"].astype(str).str.strip().str.upper()
    events["FirstCrossTime"] = pd.to_datetime(
        events["FirstCrossTime"], errors="coerce"
    )
    events["FullRankRecorderStart"] = pd.to_datetime(
        events.get("FullRankRecorderStart"), errors="coerce"
    )
    events["RunnerThresholdPct"] = pd.to_numeric(
        events["RunnerThresholdPct"], errors="coerce"
    )

    prediction_groups = {
        (str(date), str(symbol)): group.sort_values("when")
        for (date, symbol), group in predictions.groupby(
            ["date", "symbol"], sort=False
        )
    } if predictions is not None and not predictions.empty else {}
    registration_groups = {
        (str(date), str(symbol)): group.sort_values("EntryAt")
        for (date, symbol), group in registrations.groupby(
            ["Date", "Symbol"], sort=False
        )
    } if registrations is not None and not registrations.empty else {}
    broad_registration_groups = {
        (str(date), str(symbol)): group.sort_values("EntryAt")
        for (date, symbol), group in broad_registrations.groupby(
            ["Date", "Symbol"], sort=False
        )
    } if (
        broad_registrations is not None
        and not broad_registrations.empty
    ) else {}

    rows = []
    for event in events.itertuples(index=False):
        row = pd.Series(event._asdict())
        date = str(row["Date"])
        symbol = str(row["Symbol"])
        cross_time = pd.Timestamp(row["FirstCrossTime"])
        recorder_start = pd.to_datetime(
            row.get("FullRankRecorderStart"), errors="coerce"
        )
        session_eligible = _safe_bool(row, "FullRankSessionEligible")
        catchable = bool(
            session_eligible
            and pd.notna(recorder_start)
            and pd.notna(cross_time)
            and pd.Timestamp(recorder_start) < cross_time
        )
        seen = _safe_bool(row, "FullRankFeatureSeenBeforeCross")
        buyable = bool(
            seen and _safe_bool(row, "FullRankBuyableSeenBeforeCross")
        )
        early_buyable = bool(
            buyable
            and _safe_bool(row, "FullRankBuyableBand0_5To3Seen")
        )

        before_predictions = prediction_groups.get(
            (date, symbol), pd.DataFrame()
        )
        if not before_predictions.empty:
            before_predictions = before_predictions[
                before_predictions["when"] < cross_time
            ].copy()
        scored = not before_predictions.empty
        best_score = (
            float(before_predictions["expected_return_pct"].max())
            if scored
            else np.nan
        )
        score_qualified = bool(
            scored and best_score >= float(score_threshold)
        )
        selected = bool(
            scored and before_predictions["selected"].astype(bool).any()
        )
        first_prediction = (
            pd.Timestamp(before_predictions.iloc[0]["when"])
            if scored
            else pd.NaT
        )

        before_registrations = registration_groups.get(
            (date, symbol), pd.DataFrame()
        )
        if not before_registrations.empty:
            before_registrations = before_registrations[
                before_registrations["EntryAt"] < cross_time
            ].copy()
        registered = not before_registrations.empty
        fillability_known = bool(
            registered
            and before_registrations["EntryTopLevelFillable"].notna().any()
        )
        quantity_fillable = bool(
            fillability_known
            and before_registrations[
                "EntryTopLevelFillable"
            ].fillna(False).astype(bool).any()
        )
        before_broad = broad_registration_groups.get(
            (date, symbol), pd.DataFrame()
        )
        if not before_broad.empty:
            before_broad = before_broad[
                before_broad["EntryAt"] < cross_time
            ].copy()
        broad_registered = not before_broad.empty
        broad_fillability_known = bool(
            broad_registered
            and before_broad["EntryTopLevelFillable"].notna().any()
        )
        broad_quantity_fillable = bool(
            broad_fillability_known
            and before_broad["EntryTopLevelFillable"]
            .fillna(False)
            .astype(bool)
            .any()
        )

        if not catchable:
            stage = "uncatchable_before_recorder"
        elif not seen:
            stage = "recorder_visibility_miss"
        elif not buyable:
            stage = "not_contemporaneously_buyable"
        elif not early_buyable:
            stage = "buyable_only_after_early_band"
        elif not scored and broad_registered:
            stage = "broad_lane_only_awaiting_selector"
        elif not scored:
            stage = "frozen_model_not_scored"
        elif not score_qualified:
            stage = "frozen_score_below_threshold"
        elif fillability_known and not quantity_fillable:
            stage = "top_level_quantity_unfillable"
        elif not registered:
            stage = "score_qualified_no_exact_registration"
        elif not selected:
            stage = "qualified_not_selected"
        else:
            stage = "selected_before_runner_cross"

        rows.append(
            {
                "Date": date,
                "Symbol": symbol,
                "RunnerThresholdPct": float(row["RunnerThresholdPct"]),
                "FirstCrossTime": cross_time,
                "CatchableAfterRecorderStart": catchable,
                "RecorderSeenBeforeCross": seen,
                "BuyableSeenBeforeCross": buyable,
                "BuyableEarlyBandSeen": early_buyable,
                "FrozenModelScoredBeforeCross": scored,
                "BestExpectedReturnPctBeforeCross": (
                    round(best_score, 6) if scored else np.nan
                ),
                "ScoreThreshold": float(score_threshold),
                "ScoreQualifiedBeforeCross": score_qualified,
                "FirstPredictionTime": first_prediction,
                "ExactRegistrationBeforeCross": registered,
                "QuantityFillabilityKnown": fillability_known,
                "TopLevelQuantityFillableBeforeCross": quantity_fillable,
                "SelectedBeforeCross": selected,
                "BroadExactRegistrationBeforeCross": broad_registered,
                "BroadQuantityFillabilityKnown": (
                    broad_fillability_known
                ),
                "BroadTopLevelQuantityFillableBeforeCross": (
                    broad_quantity_fillable
                ),
                "FirstFailedStage": stage,
                "UsesFutureRunnerLabel": True,
                "DiagnosticOnly": True,
                "CanPromoteStrategy": False,
                "PaperPromotionAllowed": False,
                "RealOrdersAllowed": False,
            }
        )
    return pd.DataFrame(rows)


def summarize_funnel(funnel: pd.DataFrame) -> pd.DataFrame:
    if funnel is None or funnel.empty:
        return pd.DataFrame()
    rows = []
    for threshold, group in funnel.groupby(
        "RunnerThresholdPct", sort=True
    ):
        catchable = group[
            group["CatchableAfterRecorderStart"].astype(bool)
        ]
        denominator = int(len(catchable))
        stage_counts = group["FirstFailedStage"].value_counts().to_dict()
        rows.append(
            {
                "RunnerThresholdPct": float(threshold),
                "RunnerEvents": int(len(group)),
                "CatchableRunnerEvents": denominator,
                "RecorderSeenPctOfCatchable": _rate(
                    int(catchable["RecorderSeenBeforeCross"].sum()),
                    denominator,
                ),
                "BuyablePctOfCatchable": _rate(
                    int(catchable["BuyableSeenBeforeCross"].sum()),
                    denominator,
                ),
                "BuyableEarlyBandPctOfCatchable": _rate(
                    int(catchable["BuyableEarlyBandSeen"].sum()),
                    denominator,
                ),
                "FrozenModelScoredPctOfCatchable": _rate(
                    int(catchable["FrozenModelScoredBeforeCross"].sum()),
                    denominator,
                ),
                "BroadExactCandidatePctOfCatchable": _rate(
                    int(
                        catchable[
                            "BroadExactRegistrationBeforeCross"
                        ].sum()
                    ),
                    denominator,
                ),
                "ScoreQualifiedPctOfCatchable": _rate(
                    int(catchable["ScoreQualifiedBeforeCross"].sum()),
                    denominator,
                ),
                "SelectedPctOfCatchable": _rate(
                    int(catchable["SelectedBeforeCross"].sum()),
                    denominator,
                ),
                "PrimaryFailedStage": (
                    str(catchable["FirstFailedStage"].value_counts().index[0])
                    if not catchable.empty
                    else "none"
                ),
                "PrimaryFailedStageCount": (
                    int(catchable["FirstFailedStage"].value_counts().iloc[0])
                    if not catchable.empty
                    else 0
                ),
                "StageCountsJson": json.dumps(
                    stage_counts, sort_keys=True
                ),
                "UsesFutureRunnerLabel": True,
                "DiagnosticOnly": True,
                "CanPromoteStrategy": False,
                "PaperPromotionAllowed": False,
                "RealOrdersAllowed": False,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recall", default=DEFAULT_RECALL)
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS)
    parser.add_argument(
        "--registrations", default=DEFAULT_REGISTRATIONS
    )
    parser.add_argument(
        "--broad-registrations", default=DEFAULT_BROAD_REGISTRATIONS
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    parser.add_argument("--score-threshold", type=float, default=0.20)
    args = parser.parse_args()

    recall = pd.read_csv(args.recall, low_memory=False)
    predictions = load_predictions([args.predictions])
    registrations = load_registrations([args.registrations])
    broad_registrations = load_broad_registrations(
        [args.broad_registrations]
    )
    funnel = build_capture_funnel(
        recall,
        predictions,
        registrations,
        broad_registrations,
        score_threshold=args.score_threshold,
    )
    summary = summarize_funnel(funnel)

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    funnel.to_csv(output / "events.csv", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    status = {
        "evidence_type": "diagnostic_eventual_runner_capture_funnel",
        "sessions": (
            int(funnel["Date"].nunique()) if not funnel.empty else 0
        ),
        "runner_events": int(len(funnel)),
        "score_threshold": float(args.score_threshold),
        "uses_future_runner_labels": True,
        "diagnostic_only": True,
        "can_promote_strategy": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "summary": summary.to_dict(orient="records"),
    }
    (output / "status.json").write_text(
        json.dumps(status, indent=2, default=str), encoding="utf-8"
    )
    if not summary.empty:
        print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Frozen evidence judge for full-universe intraday rank migration states."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
from pathlib import Path
import sys
from typing import Iterable

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.FrozenMicrostructureModel import (  # noqa: E402
    build_model,
    next_session_date,
)
from scripts.walk_forward_live_microstructure_ranker import (  # noqa: E402
    accepted,
    metrics,
    replay_exact_capital,
)


CONTRACT = "t1_5_s1_0"
THRESHOLD = 0.20
FEATURES = [
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
    "UniverseSize",
    "DayChangeRank",
    "DayChangeRankPct",
    "PreviousDayChangeRank",
    "RankImprovement",
    "RankVelocityPerMinute",
    "DayChangeDeltaPct",
    "EnteredTop100",
    "EnteredTop50",
    "RapidRankClimber",
    "SectorRelativePct",
    "rank_band_001_010",
    "rank_band_011_025",
    "rank_band_026_050",
    "rank_band_051_075",
    "move_band_0_5_1",
    "move_band_1_2",
    "move_band_2_3",
    "move_band_3_5",
]

REQUIRED_RECORDER_GENERATION = 5
REQUIRED_GENERATION_MIN_SNAPSHOTS = 1000
REQUIRED_GENERATION_MAX_GAP_SECONDS = 45.0


def _truthy(values: pd.Series) -> pd.Series:
    return values.fillna("").astype(str).str.strip().str.lower().isin(
        {"1", "1.0", "true", "yes", "y", "on"}
    )


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {
            path
            for pattern in patterns
            for path in glob.glob(str(pattern), recursive=True)
        }
    )


def raw_session_coverage(
    patterns: Iterable[str],
    *,
    min_snapshots: int = 1000,
    latest_minute: int = 15 * 60 + 10,
    max_gap_seconds: float = 150.0,
) -> pd.DataFrame:
    rows = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {"When", "DayChangeRank"}
        if not required.issubset(frame.columns):
            continue
        frame["when"] = pd.to_datetime(frame["When"], errors="coerce")
        frame["rank"] = pd.to_numeric(frame["DayChangeRank"], errors="coerce")
        frame = frame[frame["when"].notna() & frame["rank"].gt(0)].copy()
        if frame.empty:
            continue
        recorder_generation = 0
        if "RecorderGeneration" in frame.columns:
            generations = pd.to_numeric(
                frame["RecorderGeneration"], errors="coerce"
            )
            if generations.notna().any():
                recorder_generation = int(generations.max())
                frame = frame[generations.eq(recorder_generation)].copy()
                if frame.empty:
                    continue
        frame["date"] = frame["when"].dt.date.astype(str)
        for date, day in frame.groupby("date", sort=True):
            snapshots = sorted(day["when"].dropna().unique().tolist())
            first = pd.Timestamp(snapshots[0])
            last = pd.Timestamp(snapshots[-1])
            gaps = pd.Series(pd.to_datetime(snapshots)).diff().dt.total_seconds()
            largest_gap = float(gaps.max()) if gaps.notna().any() else 0.0
            effective_max_gap = (
                min(
                    float(max_gap_seconds),
                    REQUIRED_GENERATION_MAX_GAP_SECONDS,
                )
                if recorder_generation == REQUIRED_RECORDER_GENERATION
                else float(max_gap_seconds)
            )
            effective_min_snapshots = (
                max(
                    int(min_snapshots),
                    REQUIRED_GENERATION_MIN_SNAPSHOTS,
                )
                if recorder_generation == REQUIRED_RECORDER_GENERATION
                else int(min_snapshots)
            )
            continuity_ok = largest_gap <= effective_max_gap
            first_minute = first.hour * 60 + first.minute
            last_minute = last.hour * 60 + last.minute
            rows.append(
                {
                    "date": str(date),
                    "source": str(path),
                    "snapshots": len(snapshots),
                    "first_snapshot": str(first),
                    "last_snapshot": str(last),
                    "max_gap_seconds": round(largest_gap, 3),
                    "required_max_gap_seconds": round(
                        effective_max_gap, 3
                    ),
                    "required_min_snapshots": effective_min_snapshots,
                    "continuity_ok": bool(continuity_ok),
                    "recorder_generation": int(recorder_generation),
                    "full_session_eligible": bool(
                        recorder_generation == REQUIRED_RECORDER_GENERATION
                        and
                        first_minute <= 9 * 60 + 20
                        and last_minute >= int(latest_minute)
                        and len(snapshots) >= effective_min_snapshots
                        and continuity_ok
                    ),
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=[
                "date", "source", "snapshots", "first_snapshot",
                "last_snapshot", "max_gap_seconds", "continuity_ok",
                "required_max_gap_seconds", "required_min_snapshots",
                "recorder_generation", "full_session_eligible",
            ]
        )
    coverage = pd.DataFrame(rows).sort_values(["date", "source"])
    # A date is eligible if any recorder file for that date proves full coverage.
    coverage.sort_values(
        ["date", "full_session_eligible", "snapshots"],
        ascending=[True, False, False],
        inplace=True,
    )
    return coverage.drop_duplicates("date", keep="first").reset_index(drop=True)


def load_rank_outcomes(
    patterns: Iterable[str], eligible_dates: set[str]
) -> pd.DataFrame:
    frames = []
    required = {
        "Date", "When", "Symbol", "OutcomeAvailable", "EntryPrice",
        "LastPrice", "BestBid", "BestAsk", "SpreadBps", "SpreadOk",
        "TradedValue", "DayChangePct", "DayChangeRank",
        "RankEvidenceBucket", f"{CONTRACT}_return_pct",
        f"{CONTRACT}_exit_time",
    }
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if not required.issubset(frame.columns):
            continue
        frame = frame[frame["Date"].astype(str).isin(eligible_dates)].copy()
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()

    data = pd.concat(frames, ignore_index=True)
    data["date"] = data["Date"].astype(str)
    data["symbol"] = data["Symbol"].astype(str).str.strip().str.upper()
    data["signal_time"] = pd.to_datetime(data["When"], errors="coerce")
    data["exit_time"] = pd.to_datetime(
        data[f"{CONTRACT}_exit_time"], errors="coerce"
    )
    data["trade_return_pct"] = pd.to_numeric(
        data[f"{CONTRACT}_return_pct"], errors="coerce"
    )
    numeric_columns = [
        "EntryPrice", "LastPrice", "BestBid", "BestAsk", "SpreadBps",
        "TradedValue", "DayChangePct", "OpenChangePct",
        "PullbackFromHighPct", "BuyerSellerRatio",
        "BuyerSellerMedianWindow", "BuyerSellerMinWindow",
        "SectorAvgChangePct", "SectorBreadthPct", "SectorTop2Count",
        "SectorTop5Count", "MarketBreadthPct", "UniverseSize",
        "DayChangeRank", "DayChangeRankPct", "PreviousDayChangeRank",
        "RankImprovement", "RankVelocityPerMinute", "DayChangeDeltaPct",
    ]
    for column in numeric_columns:
        if column not in data.columns:
            data[column] = np.nan
        data[column] = pd.to_numeric(data[column], errors="coerce")
    for column in (
        "OutcomeAvailable", "SpreadOk", "OrderBookStable", "EnteredTop100",
        "EnteredTop50", "RapidRankClimber",
    ):
        values = data.get(column, pd.Series(False, index=data.index))
        data[column] = _truthy(values).astype(float)

    data["signal_minute"] = (
        data["signal_time"].dt.hour * 60 + data["signal_time"].dt.minute
    )
    data["log_traded_value"] = np.log1p(data["TradedValue"].clip(lower=0))
    data["SectorRelativePct"] = (
        data["DayChangePct"] - data["SectorAvgChangePct"]
    )
    rank_bucket = data["RankEvidenceBucket"].fillna("").astype(str)
    data["rank_band_001_010"] = rank_bucket.str.contains("r001_010", regex=False).astype(float)
    data["rank_band_011_025"] = rank_bucket.str.contains("r011_025", regex=False).astype(float)
    data["rank_band_026_050"] = rank_bucket.str.contains("r026_050", regex=False).astype(float)
    data["rank_band_051_075"] = rank_bucket.str.contains("r051_075", regex=False).astype(float)
    data["move_band_0_5_1"] = rank_bucket.str.contains("m0_5_1", regex=False).astype(float)
    data["move_band_1_2"] = rank_bucket.str.contains("m1_2", regex=False).astype(float)
    data["move_band_2_3"] = rank_bucket.str.contains("m2_3", regex=False).astype(float)
    data["move_band_3_5"] = rank_bucket.str.contains("m3_5", regex=False).astype(float)

    executable = (
        data["OutcomeAvailable"].gt(0)
        & data["signal_time"].notna()
        & data["exit_time"].notna()
        & data["trade_return_pct"].notna()
        & data["EntryPrice"].ge(50.0)
        & data["TradedValue"].ge(30_000_000.0)
        & data["SpreadOk"].gt(0)
        & data["BestBid"].gt(0)
        & data["BestAsk"].gt(data["BestBid"])
        & data["SpreadBps"].gt(0)
        & data["SpreadBps"].le(20.0)
        & data["DayChangePct"].between(0.5, 5.0, inclusive="both")
        & data["RankEvidenceBucket"].fillna("").astype(str).ne("no_rank_state")
    )
    data = data[executable].copy()
    data = data[data["exit_time"] > data["signal_time"]].copy()
    for feature in FEATURES:
        if feature not in data.columns:
            data[feature] = np.nan
        data[feature] = pd.to_numeric(data[feature], errors="coerce")
    return data.drop_duplicates(
        ["date", "symbol", "signal_time", "RankEvidenceBucket"], keep="last"
    ).sort_values(["date", "signal_time", "symbol"]).reset_index(drop=True)


def rank_outcome_coverage(
    patterns: Iterable[str], raw_full_dates: set[str]
) -> pd.DataFrame:
    """Require labeled rank rows without dropping legitimate zero-trade days."""
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {
            "Date", "RankEvidenceBucket", "OutcomeAvailable", "EntryPrice",
            "BestBid", "BestAsk", "SpreadBps", "SpreadOk", "TradedValue",
            "DayChangePct",
        }
        if not required.issubset(frame.columns):
            continue
        frame = frame[frame["Date"].astype(str).isin(raw_full_dates)].copy()
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(
            columns=[
                "date", "labeled_rows", "candidate_rows", "available_rows",
                "coverage_pct", "outcome_ready",
            ]
        )
    data = pd.concat(frames, ignore_index=True)
    for column in (
        "EntryPrice", "BestBid", "BestAsk", "SpreadBps", "TradedValue",
        "DayChangePct",
    ):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["available"] = _truthy(data["OutcomeAvailable"])
    data["spread_ok"] = _truthy(data["SpreadOk"])
    data["rank_state"] = data["RankEvidenceBucket"].fillna("").astype(str)
    data["candidate"] = (
        data["rank_state"].ne("no_rank_state")
        & data["EntryPrice"].ge(50.0)
        & data["TradedValue"].ge(30_000_000.0)
        & data["spread_ok"]
        & data["BestBid"].gt(0)
        & data["BestAsk"].gt(data["BestBid"])
        & data["SpreadBps"].gt(0)
        & data["SpreadBps"].le(20.0)
        & data["DayChangePct"].between(0.5, 5.0, inclusive="both")
    )
    rows = []
    for date, day in data.groupby(data["Date"].astype(str), sort=True):
        candidate_rows = int(day["candidate"].sum())
        available_rows = int((day["candidate"] & day["available"]).sum())
        coverage_pct = (
            available_rows / candidate_rows * 100.0 if candidate_rows else 100.0
        )
        rows.append(
            {
                "date": str(date),
                "labeled_rows": int(len(day)),
                "candidate_rows": candidate_rows,
                "available_rows": available_rows,
                "coverage_pct": round(coverage_pct, 2),
                "outcome_ready": bool(coverage_pct >= 95.0),
            }
        )
    return pd.DataFrame(rows)


def score_walk_forward(
    frame: pd.DataFrame,
    *,
    min_train_days: int = 8,
    min_train_rows: int = 500,
    min_samples_leaf: int = 30,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    dates = sorted(frame["date"].unique().tolist())
    scored = []
    for index, date in enumerate(dates):
        prior_dates = dates[:index]
        if len(prior_dates) < int(min_train_days):
            continue
        train = frame[frame["date"].isin(prior_dates)].copy()
        if len(train) < int(min_train_rows):
            continue
        day = frame[frame["date"].eq(date)].copy()
        counts = train.groupby("date")["symbol"].transform("count").clip(lower=1)
        model = build_model(min_samples_leaf=min_samples_leaf)
        model.fit(
            train[FEATURES],
            train["trade_return_pct"],
            model__sample_weight=1.0 / counts.to_numpy(dtype=float),
        )
        day["expected_return_pct"] = model.predict(day[FEATURES])
        day["model_trained_through"] = prior_dates[-1]
        scored.append(day)
    return pd.concat(scored, ignore_index=True) if scored else pd.DataFrame()


def replay(
    scored: pd.DataFrame,
    dates: list[str],
    *,
    capital: float,
    max_trades_per_day: int,
    extra_cost_pct: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if scored is None or scored.empty:
        daily = pd.DataFrame(
            {
                "date": dates,
                "trades": [0] * len(dates),
                "pnl": [0.0] * len(dates),
                "daily_return_pct": [0.0] * len(dates),
            }
        )
        return daily, pd.DataFrame()
    return replay_exact_capital(
        scored,
        dates,
        threshold=THRESHOLD,
        slots=1,
        max_trades_per_day=max_trades_per_day,
        extra_cost_pct=extra_cost_pct,
        capital=capital,
    )


def train_frozen_model(frame: pd.DataFrame, selection_dates: list[str]):
    train = frame[frame["date"].isin(selection_dates)].copy()
    counts = train.groupby("date")["symbol"].transform("count").clip(lower=1)
    model = build_model(min_samples_leaf=30)
    model.fit(
        train[FEATURES],
        train["trade_return_pct"],
        model__sample_weight=1.0 / counts.to_numpy(dtype=float),
    )
    identity = train[["date", "symbol", "signal_time", "trade_return_pct"]]
    metadata = {
        "artifact_type": "frozen_full_universe_rank_strategy",
        "contract": CONTRACT,
        "threshold": THRESHOLD,
        "selection_start": selection_dates[0],
        "selection_end": selection_dates[-1],
        "forward_start": next_session_date(selection_dates[-1]),
        "selection_sessions": len(selection_dates),
        "training_rows": int(len(train)),
        "features": FEATURES,
        "source_sha256": hashlib.sha256(
            identity.to_csv(index=False).encode("utf-8")
        ).hexdigest(),
        "paper_only": True,
        "real_orders_allowed": False,
    }
    return model, metadata


def run_audit(
    *,
    outcome_patterns: list[str],
    raw_patterns: list[str],
    output_dir: Path,
    capital: float,
    daily_goal: float,
    stress_cost_pct: float,
    max_trades_per_day: int,
    selection_sessions_required: int,
    min_snapshots: int,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage = raw_session_coverage(
        raw_patterns, min_snapshots=min_snapshots
    )
    coverage.to_csv(output_dir / "session_coverage.csv", index=False)
    raw_eligible_dates = sorted(
        coverage.loc[
            coverage["full_session_eligible"].astype(bool), "date"
        ].astype(str).tolist()
    ) if not coverage.empty else []
    outcome_coverage = rank_outcome_coverage(
        outcome_patterns, set(raw_eligible_dates)
    )
    outcome_coverage.to_csv(output_dir / "outcome_coverage.csv", index=False)
    outcome_ready_dates = set(
        outcome_coverage.loc[
            outcome_coverage["outcome_ready"].astype(bool), "date"
        ].astype(str).tolist()
    ) if not outcome_coverage.empty else set()
    eligible_dates = sorted(set(raw_eligible_dates) & outcome_ready_dates)
    evidence = load_rank_outcomes(outcome_patterns, set(eligible_dates))
    evidence.to_csv(output_dir / "executable_rank_states.csv", index=False)

    base_status = {
        "evidence": "full_universe_rank_migration_strategy",
        "contract": CONTRACT,
        "threshold": THRESHOLD,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "stress_extra_cost_pct": float(stress_cost_pct),
        "max_trades_per_day": int(max_trades_per_day),
        "selection_sessions_required": int(selection_sessions_required),
        "coverage_dates": int(len(coverage)),
        "raw_full_sessions": int(len(raw_eligible_dates)),
        "outcome_ready_sessions": int(len(outcome_ready_dates)),
        "eligible_full_sessions": int(len(eligible_dates)),
        "partial_sessions": int(
            len(coverage) - int(coverage["full_session_eligible"].astype(bool).sum())
        ) if not coverage.empty else 0,
        "executable_rows": int(len(evidence)),
        "paper_only": True,
        "real_orders_allowed": False,
    }
    selection_path = output_dir / "frozen_selection.json"
    artifact_path = output_dir / "frozen_model.joblib"
    frozen = {}
    if selection_path.exists():
        try:
            frozen = json.loads(selection_path.read_text(encoding="utf-8"))
        except Exception:
            frozen = {}

    if not frozen and len(eligible_dates) < int(selection_sessions_required):
        status = {
            **base_status,
            "status": "collecting_full_session_selection_evidence",
            "selection_sessions_remaining": max(
                0, int(selection_sessions_required) - len(eligible_dates)
            ),
            "accepted": False,
            "paper_promotion_allowed": False,
        }
        (output_dir / "status.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8"
        )
        return status

    if not frozen:
        selection_dates = eligible_dates[: int(selection_sessions_required)]
        selected_evidence = evidence[evidence["date"].isin(selection_dates)].copy()
        diagnostic = score_walk_forward(selected_evidence)
        diagnostic_dates = sorted(
            diagnostic["date"].unique().tolist()
        ) if not diagnostic.empty else []
        raw_daily, raw_trades = replay(
            diagnostic,
            diagnostic_dates,
            capital=capital,
            max_trades_per_day=max_trades_per_day,
            extra_cost_pct=0.0,
        )
        stress_daily, stress_trades = replay(
            diagnostic,
            diagnostic_dates,
            capital=capital,
            max_trades_per_day=max_trades_per_day,
            extra_cost_pct=stress_cost_pct,
        )
        raw_summary = metrics(raw_daily, daily_goal)
        stress_summary = metrics(stress_daily, daily_goal)
        selection_passed = bool(
            stress_summary["sessions"] >= 4
            and stress_summary["trade_sessions"] >= 3
            and stress_summary["trades"] >= 10
            and stress_summary["avg_daily_pnl"] >= float(daily_goal)
            and stress_summary["lower_95_pct"] is not None
            and stress_summary["lower_95_pct"] > 0
            and stress_summary["block_floor_pct"] is not None
            and stress_summary["block_floor_pct"] > 0
            and stress_summary["goal_hit_pct"] >= 50.0
            and stress_summary["worst_session_pct"] >= -2.5
        )
        diagnostic.to_csv(output_dir / "selection_predictions.csv", index=False)
        raw_daily.to_csv(output_dir / "selection_daily.csv", index=False)
        raw_trades.to_csv(output_dir / "selection_trades.csv", index=False)
        stress_daily.to_csv(output_dir / "selection_stress_daily.csv", index=False)
        stress_trades.to_csv(output_dir / "selection_stress_trades.csv", index=False)
        frozen = {
            "selection_status": "passed" if selection_passed else "rejected",
            "selection_start": selection_dates[0],
            "selection_end": selection_dates[-1],
            "forward_start": next_session_date(selection_dates[-1]),
            "selection_dates": selection_dates,
            "selection": raw_summary,
            "selection_stress": stress_summary,
            "contract": CONTRACT,
            "threshold": THRESHOLD,
            "real_orders_allowed": False,
        }
        if selection_passed:
            model, metadata = train_frozen_model(selected_evidence, selection_dates)
            joblib.dump({"model": model, "metadata": metadata}, artifact_path)
            frozen["model_metadata"] = metadata
        selection_path.write_text(
            json.dumps(frozen, indent=2), encoding="utf-8"
        )

    if frozen.get("selection_status") != "passed":
        status = {
            **base_status,
            "status": "selection_rejected",
            "selection": frozen,
            "accepted": False,
            "paper_promotion_allowed": False,
        }
        (output_dir / "status.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8"
        )
        return status

    payload = joblib.load(artifact_path)
    model = payload["model"]
    forward_start = str(frozen["forward_start"])
    forward_dates = [date for date in eligible_dates if date >= forward_start]
    forward = evidence[evidence["date"].isin(forward_dates)].copy()
    if not forward.empty:
        forward["expected_return_pct"] = model.predict(forward[FEATURES])
        forward["model_trained_through"] = str(frozen["selection_end"])
    raw_daily, raw_trades = replay(
        forward,
        forward_dates,
        capital=capital,
        max_trades_per_day=max_trades_per_day,
        extra_cost_pct=0.0,
    )
    stress_daily, stress_trades = replay(
        forward,
        forward_dates,
        capital=capital,
        max_trades_per_day=max_trades_per_day,
        extra_cost_pct=stress_cost_pct,
    )
    forward_summary = metrics(raw_daily, daily_goal)
    forward_stress = metrics(stress_daily, daily_goal)
    is_accepted = accepted(
        forward_summary,
        forward_stress,
        min_forward_sessions=20,
        min_trade_sessions=15,
        min_trades=30,
        daily_goal=daily_goal,
    )
    forward.to_csv(output_dir / "forward_predictions.csv", index=False)
    raw_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    raw_trades.to_csv(output_dir / "forward_trades.csv", index=False)
    stress_daily.to_csv(output_dir / "forward_stress_daily.csv", index=False)
    stress_trades.to_csv(output_dir / "forward_stress_trades.csv", index=False)
    status = {
        **base_status,
        "status": (
            "accepted" if is_accepted
            else "rejected_forward" if len(forward_dates) >= 20
            else "collecting_untouched_forward_evidence"
        ),
        "selection": frozen,
        "forward": forward_summary,
        "forward_stress": forward_stress,
        "accepted": is_accepted,
        "paper_promotion_allowed": is_accepted,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outcome-glob", action="append", default=[])
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument(
        "--output-dir", default="results/Data/full_universe_rank_strategy"
    )
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    parser.add_argument("--max-trades-per-day", type=int, default=10)
    parser.add_argument("--selection-sessions", type=int, default=12)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()
    status = run_audit(
        outcome_patterns=args.outcome_glob
        or ["results/Data/live_feature_recorder/eod/20*/live_features_rows.csv"],
        raw_patterns=args.raw_glob
        or ["results/Data/live_feature_recorder/live_features_20*.csv"],
        output_dir=Path(args.output_dir),
        capital=args.capital,
        daily_goal=args.daily_goal,
        stress_cost_pct=args.stress_cost_pct,
        max_trades_per_day=args.max_trades_per_day,
        selection_sessions_required=args.selection_sessions,
        min_snapshots=args.min_snapshots,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Audit early leadership emergence from causal candidate-rank migration."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.FrozenMicrostructureModel import (  # noqa: E402
    CONTRACT,
    MODEL_FEATURES,
    build_model,
    next_session_date,
)
from scripts.walk_forward_live_microstructure_ranker import (  # noqa: E402
    accepted,
    load_outcomes,
    metrics,
    replay_exact_capital,
)


RANK_FEATURES = [
    "CandidateRankPct15m",
    "SectorCandidateRankPct15m",
    "TradedValueRankPct15m",
    "CandidateRankDelta",
    "CandidateRankVelocityPerMinute",
    "RepeatDayChangeVelocity",
    "EnteredLeaderQuintile",
    "RecentCandidateCount",
    "RecentSectorCandidateCount",
]
FEATURES = MODEL_FEATURES + RANK_FEATURES
DEFAULT_FROZEN_THROUGH = "2026-07-21"
DEFAULT_FORWARD_START = "2026-07-22"
DEFAULT_THRESHOLD = 0.20


def _percentile(values: list[float], current: float) -> float:
    clean = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if not np.isfinite(current) or clean.size == 0:
        return np.nan
    return float(np.mean(clean <= float(current)))


def add_causal_rank_features(
    frame: pd.DataFrame,
    *,
    lookback_minutes: int = 15,
    min_recent_candidates: int = 5,
) -> pd.DataFrame:
    """Add candidate-relative ranks using only rows visible by each timestamp.

    The recorder is a candidate tape, not a full-universe synchronized book.
    These fields therefore describe rank among recently observed candidates and
    are deliberately named that way. Rows sharing a timestamp see the complete
    same-timestamp batch; no later timestamp can affect an earlier row.
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(frame.columns) + RANK_FEATURES)

    required = {
        "date", "symbol", "signal_time", "Sector", "DayChangePct",
        "TradedValue",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "rank migration input missing columns: " + ", ".join(sorted(missing))
        )

    parts = []
    lookback = pd.Timedelta(minutes=max(1, int(lookback_minutes)))
    minimum = max(1, int(min_recent_candidates))
    for _, day in frame.groupby("date", sort=True):
        day = day.sort_values(["signal_time", "symbol"]).copy()
        latest: dict[str, dict[str, Any]] = {}
        prior_symbol: dict[str, dict[str, Any]] = {}
        enriched_batches = []
        for timestamp, batch in day.groupby("signal_time", sort=True):
            now = pd.Timestamp(timestamp)
            cutoff = now - lookback
            latest = {
                symbol: state
                for symbol, state in latest.items()
                if pd.Timestamp(state["when"]) >= cutoff
            }

            batch = batch.copy()
            for _, row in batch.iterrows():
                latest[str(row["symbol"])] = {
                    "when": now,
                    "sector": str(row.get("Sector", "") or ""),
                    "change": float(pd.to_numeric(row.get("DayChangePct"), errors="coerce")),
                    "traded_value": float(pd.to_numeric(row.get("TradedValue"), errors="coerce")),
                }

            all_changes = [state["change"] for state in latest.values()]
            all_values = [state["traded_value"] for state in latest.values()]
            output_rows = []
            for _, row in batch.iterrows():
                record = row.to_dict()
                symbol = str(row["symbol"])
                sector = str(row.get("Sector", "") or "")
                change = float(pd.to_numeric(row.get("DayChangePct"), errors="coerce"))
                traded_value = float(
                    pd.to_numeric(row.get("TradedValue"), errors="coerce")
                )
                sector_states = [
                    state for state in latest.values() if state["sector"] == sector
                ]
                recent_count = len(latest)
                sector_count = len(sector_states)
                rank = (
                    _percentile(all_changes, change)
                    if recent_count >= minimum
                    else np.nan
                )
                value_rank = (
                    _percentile(all_values, traded_value)
                    if recent_count >= minimum
                    else np.nan
                )
                sector_rank = (
                    _percentile(
                        [state["change"] for state in sector_states], change
                    )
                    if sector_count >= 2
                    else np.nan
                )
                previous = prior_symbol.get(symbol)
                rank_delta = rank_velocity = repeat_velocity = np.nan
                entered_leader = 0.0
                if previous is not None:
                    elapsed = max(
                        (now - pd.Timestamp(previous["when"])).total_seconds() / 60.0,
                        1e-6,
                    )
                    previous_rank = float(previous.get("rank", np.nan))
                    if np.isfinite(rank) and np.isfinite(previous_rank):
                        rank_delta = rank - previous_rank
                        rank_velocity = rank_delta / elapsed
                        entered_leader = float(rank >= 0.80 and previous_rank < 0.80)
                    previous_change = float(previous.get("change", np.nan))
                    if np.isfinite(change) and np.isfinite(previous_change):
                        repeat_velocity = (change - previous_change) / elapsed

                record.update(
                    {
                        "CandidateRankPct15m": rank,
                        "SectorCandidateRankPct15m": sector_rank,
                        "TradedValueRankPct15m": value_rank,
                        "CandidateRankDelta": rank_delta,
                        "CandidateRankVelocityPerMinute": rank_velocity,
                        "RepeatDayChangeVelocity": repeat_velocity,
                        "EnteredLeaderQuintile": entered_leader,
                        "RecentCandidateCount": float(recent_count),
                        "RecentSectorCandidateCount": float(sector_count),
                    }
                )
                output_rows.append(record)
                prior_symbol[symbol] = {
                    "when": now,
                    "rank": rank,
                    "change": change,
                }
            enriched_batches.append(pd.DataFrame(output_rows))
        if enriched_batches:
            parts.append(pd.concat(enriched_batches, ignore_index=True))
    if not parts:
        return pd.DataFrame(columns=list(frame.columns) + RANK_FEATURES)
    return pd.concat(parts, ignore_index=True).sort_values(
        ["date", "signal_time", "symbol"]
    ).reset_index(drop=True)


def score_prior_session_walk_forward(
    frame: pd.DataFrame,
    *,
    min_train_days: int = 8,
    min_train_rows: int = 500,
    lookback_days: int = 40,
    min_samples_leaf: int = 30,
) -> pd.DataFrame:
    dates = sorted(frame["date"].astype(str).unique().tolist())
    scored = []
    for index, date in enumerate(dates):
        prior_dates = dates[max(0, index - int(lookback_days)):index]
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


def train_frozen_model(
    frame: pd.DataFrame,
    *,
    frozen_through: str,
    min_train_days: int = 8,
    min_train_rows: int = 500,
    min_samples_leaf: int = 30,
    threshold: float = DEFAULT_THRESHOLD,
):
    train = frame[frame["date"].astype(str).le(str(frozen_through))].copy()
    sessions = sorted(train["date"].astype(str).unique().tolist())
    if len(sessions) < int(min_train_days):
        raise ValueError(f"need {min_train_days} training sessions, found {len(sessions)}")
    if len(train) < int(min_train_rows):
        raise ValueError(f"need {min_train_rows} training rows, found {len(train)}")
    counts = train.groupby("date")["symbol"].transform("count").clip(lower=1)
    model = build_model(min_samples_leaf=min_samples_leaf)
    model.fit(
        train[FEATURES],
        train["trade_return_pct"],
        model__sample_weight=1.0 / counts.to_numpy(dtype=float),
    )
    identity = train[["date", "symbol", "signal_time", "trade_return_pct"]]
    metadata = {
        "artifact_type": "prior_session_candidate_rank_migration",
        "contract": CONTRACT,
        "trained_through": str(frozen_through),
        "valid_from": next_session_date(str(frozen_through)),
        "training_sessions": len(sessions),
        "training_rows": int(len(train)),
        "features": list(FEATURES),
        "candidate_rank_window_minutes": 15,
        "candidate_rank_min_recent": 5,
        "threshold": float(threshold),
        "equal_session_weights": True,
        "source_sha256": hashlib.sha256(
            identity.to_csv(index=False).encode("utf-8")
        ).hexdigest(),
        "paper_only": True,
        "real_orders_allowed": False,
    }
    return model, metadata


def _empty_replay_dates(dates: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        pd.DataFrame(
            {
                "date": dates,
                "trades": [0] * len(dates),
                "pnl": [0.0] * len(dates),
                "daily_return_pct": [0.0] * len(dates),
            }
        ),
        pd.DataFrame(),
    )


def replay(
    scored: pd.DataFrame,
    dates: list[str],
    *,
    threshold: float,
    capital: float,
    max_trades_per_day: int,
    extra_cost_pct: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if scored is None or scored.empty:
        return _empty_replay_dates(dates)
    return replay_exact_capital(
        scored,
        dates,
        threshold=threshold,
        slots=1,
        max_trades_per_day=max_trades_per_day,
        extra_cost_pct=extra_cost_pct,
        capital=capital,
    )


def run_audit(
    *,
    input_path: str,
    output_dir: Path,
    frozen_through: str,
    forward_start: str,
    threshold: float,
    capital: float,
    daily_goal: float,
    stress_cost_pct: float,
    max_trades_per_day: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = load_outcomes(input_path, contract=CONTRACT)
    enriched = add_causal_rank_features(base)
    enriched.to_csv(output_dir / "rank_migration_features.csv", index=False)
    all_dates = sorted(enriched["date"].astype(str).unique().tolist())
    retrospective_dates = [date for date in all_dates if date <= frozen_through]
    forward_dates = [date for date in all_dates if date >= forward_start]

    try:
        model, metadata = train_frozen_model(
            enriched, frozen_through=frozen_through, threshold=threshold
        )
    except ValueError as exc:
        status = {
            "evidence": "causal_candidate_rank_migration",
            "status": "insufficient_training_evidence",
            "message": str(exc),
            "frozen_through": str(frozen_through),
            "forward_start": str(forward_start),
            "sessions": len(all_dates),
            "rows": int(len(enriched)),
            "accepted": False,
            "paper_promotion_allowed": False,
            "real_orders_allowed": False,
        }
        (output_dir / "status.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8"
        )
        return status

    artifact = output_dir / "frozen_rank_migration_model.joblib"
    joblib.dump({"model": model, "metadata": metadata}, artifact)
    artifact.with_suffix(".json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )

    diagnostic = score_prior_session_walk_forward(
        enriched[enriched["date"].le(frozen_through)].copy()
    )
    diagnostic_dates = sorted(
        diagnostic["date"].astype(str).unique().tolist()
    ) if not diagnostic.empty else []
    diagnostic_daily, diagnostic_trades = replay(
        diagnostic,
        diagnostic_dates,
        threshold=threshold,
        capital=capital,
        max_trades_per_day=max_trades_per_day,
        extra_cost_pct=0.0,
    )
    diagnostic_stress_daily, diagnostic_stress_trades = replay(
        diagnostic,
        diagnostic_dates,
        threshold=threshold,
        capital=capital,
        max_trades_per_day=max_trades_per_day,
        extra_cost_pct=stress_cost_pct,
    )

    forward = enriched[enriched["date"].ge(forward_start)].copy()
    if not forward.empty:
        forward["expected_return_pct"] = model.predict(forward[FEATURES])
        forward["model_trained_through"] = str(frozen_through)
    forward_daily, forward_trades = replay(
        forward,
        forward_dates,
        threshold=threshold,
        capital=capital,
        max_trades_per_day=max_trades_per_day,
        extra_cost_pct=0.0,
    )
    forward_stress_daily, forward_stress_trades = replay(
        forward,
        forward_dates,
        threshold=threshold,
        capital=capital,
        max_trades_per_day=max_trades_per_day,
        extra_cost_pct=stress_cost_pct,
    )

    diagnostic.to_csv(output_dir / "diagnostic_predictions.csv", index=False)
    diagnostic_daily.to_csv(output_dir / "diagnostic_daily.csv", index=False)
    diagnostic_trades.to_csv(output_dir / "diagnostic_trades.csv", index=False)
    diagnostic_stress_daily.to_csv(
        output_dir / "diagnostic_stress_daily.csv", index=False
    )
    diagnostic_stress_trades.to_csv(
        output_dir / "diagnostic_stress_trades.csv", index=False
    )
    forward.to_csv(output_dir / "forward_predictions.csv", index=False)
    forward_daily.to_csv(output_dir / "forward_daily.csv", index=False)
    forward_trades.to_csv(output_dir / "forward_trades.csv", index=False)
    forward_stress_daily.to_csv(
        output_dir / "forward_stress_daily.csv", index=False
    )
    forward_stress_trades.to_csv(
        output_dir / "forward_stress_trades.csv", index=False
    )

    diagnostic_summary = metrics(diagnostic_daily, daily_goal)
    diagnostic_stress_summary = metrics(diagnostic_stress_daily, daily_goal)
    forward_summary = metrics(forward_daily, daily_goal)
    forward_stress_summary = metrics(forward_stress_daily, daily_goal)
    is_accepted = accepted(
        forward_summary,
        forward_stress_summary,
        min_forward_sessions=20,
        min_trade_sessions=15,
        min_trades=30,
        daily_goal=daily_goal,
    )
    enough_forward = forward_summary["sessions"] >= 20
    status = {
        "evidence": "causal_candidate_rank_migration",
        "status": (
            "accepted" if is_accepted
            else "rejected_forward" if enough_forward
            else "collecting_forward_evidence"
        ),
        "description": (
            "candidate-relative rank migration among states observed in the "
            "previous 15 minutes; not synchronized full-universe rank"
        ),
        "contract": CONTRACT,
        "threshold": float(threshold),
        "frozen_through": str(frozen_through),
        "forward_start": str(forward_start),
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "stress_extra_cost_pct": float(stress_cost_pct),
        "max_trades_per_day": int(max_trades_per_day),
        "sessions": len(all_dates),
        "rows": int(len(enriched)),
        "retrospective_sessions": len(retrospective_dates),
        "forward_candidate_rows": int(len(forward)),
        "diagnostic": diagnostic_summary,
        "diagnostic_stress": diagnostic_stress_summary,
        "forward": forward_summary,
        "forward_stress": forward_stress_summary,
        "model_artifact": str(artifact),
        "accepted": is_accepted,
        "paper_promotion_allowed": is_accepted,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="results/Data/live_leader_continuation/snapshot_outcomes.csv",
    )
    parser.add_argument(
        "--output-dir", default="results/Data/live_rank_migration"
    )
    parser.add_argument("--frozen-through", default=DEFAULT_FROZEN_THROUGH)
    parser.add_argument("--forward-start", default=DEFAULT_FORWARD_START)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    parser.add_argument("--max-trades-per-day", type=int, default=10)
    args = parser.parse_args()
    status = run_audit(
        input_path=args.input,
        output_dir=Path(args.output_dir),
        frozen_through=args.frozen_through,
        forward_start=args.forward_start,
        threshold=args.threshold,
        capital=args.capital,
        daily_goal=args.daily_goal,
        stress_cost_pct=args.stress_cost_pct,
        max_trades_per_day=args.max_trades_per_day,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

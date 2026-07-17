#!/usr/bin/env python3
"""Train on historical causal events and evaluate repaired future live sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import joblib
from scipy.stats import t as student_t


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.IntradayEventEngine import CAUSAL_RICH_EVENT_FEATURES, EVENT_TYPES  # noqa: E402
from scripts.walk_forward_live_event_ranker import _model  # noqa: E402


DEFAULT_HISTORICAL = "results/Data/causal_grid_family_target_pipeline_input.csv"
DEFAULT_LIVE_GLOB = (
    "/home/charan_sai/tr-assistant/results/Data/intraday_event_shadow_rebuilt/"
    "intraday_event_shadow/outcomes_*.csv"
)
DEFAULT_OUTPUT = "results/Data/historical_event_model_live_holdout"


def _truthy(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes", "y"})


def _prepare_features(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    missing = set(CAUSAL_RICH_EVENT_FEATURES) - set(frame.columns)
    if missing:
        raise ValueError(f"{source} missing causal features: {', '.join(sorted(missing))}")
    prepared = frame.copy()
    for feature in CAUSAL_RICH_EVENT_FEATURES:
        prepared[feature] = pd.to_numeric(prepared[feature], errors="coerce")
    prepared["date"] = prepared["date"].astype(str).str[:10]
    prepared["symbol"] = prepared["symbol"].astype(str).str.strip().str.upper()
    prepared["event_type"] = prepared["event_type"].astype(str).str.strip()
    return prepared[prepared["event_type"].isin(EVENT_TYPES)].copy()


def load_historical(path: str, lookback_days: int) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"date", "symbol", "event_type", "exit_reason", *CAUSAL_RICH_EVENT_FEATURES}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
    frame = _prepare_features(frame, path)
    frame["label"] = frame["exit_reason"].astype(str).str.contains("target", case=False, na=False).astype(int)
    dates = sorted(frame["date"].unique().tolist())
    selected_dates = dates[-max(1, int(lookback_days)) :]
    return frame[frame["date"].isin(selected_dates)].reset_index(drop=True)


def load_live(paths: Iterable[Path]) -> tuple[pd.DataFrame, list[str]]:
    parts = []
    session_dates = []
    for path in sorted(paths):
        frame = pd.read_csv(path)
        required = {
            "date",
            "symbol",
            "event_type",
            "signal_time",
            "net_return_pct",
            "complete_session",
            *CAUSAL_RICH_EVENT_FEATURES,
        }
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
        frame = _prepare_features(frame, str(path))
        frame = frame[_truthy(frame["complete_session"])].copy()
        if "side" in frame.columns:
            frame = frame[frame["side"].astype(str).str.lower() == "long"].copy()
        if frame.empty:
            continue
        frame["source_path"] = str(path)
        parts.append(frame)
        session_dates.extend(frame["date"].unique().tolist())
    if not parts:
        raise ValueError("no complete live holdout sessions found")
    return pd.concat(parts, ignore_index=True), sorted(set(session_dates))


def validate_temporal_boundary(historical: pd.DataFrame, live: pd.DataFrame) -> dict:
    historical_end = str(historical["date"].max())
    live_start = str(live["date"].min())
    if historical_end >= live_start:
        raise ValueError(
            f"historical/live date overlap: historical_end={historical_end}, live_start={live_start}"
        )
    return {
        "historical_start": str(historical["date"].min()),
        "historical_end": historical_end,
        "live_start": live_start,
        "live_end": str(live["date"].max()),
    }


def fit_family_models(historical: pd.DataFrame, min_family_rows: int) -> tuple[dict, pd.DataFrame]:
    models = {}
    rows = []
    for event_type in EVENT_TYPES:
        family = historical[historical["event_type"] == event_type].copy()
        usable = len(family) >= int(min_family_rows) and family["label"].nunique() >= 2
        rows.append(
            {
                "event_type": event_type,
                "train_rows": int(len(family)),
                "train_days": int(family["date"].nunique()),
                "positive_label_pct": round(float(family["label"].mean() * 100.0), 4) if len(family) else 0.0,
                "model_fitted": bool(usable),
            }
        )
        if not usable:
            continue
        model = _model()
        model.fit(family[CAUSAL_RICH_EVENT_FEATURES], family["label"])
        models[event_type] = model
    if not models:
        raise ValueError("no event family had enough historical rows and both label classes")
    return models, pd.DataFrame(rows)


def save_model_bundle(path: str, models: dict, families: pd.DataFrame, historical: pd.DataFrame, lookback_days: int) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "models": models,
            "families": families,
            "features": list(CAUSAL_RICH_EVENT_FEATURES),
            "lookback_days": int(lookback_days),
            "historical_start": str(historical["date"].min()),
            "historical_end": str(historical["date"].max()),
            "historical_rows": int(len(historical)),
            "historical_sessions": int(historical["date"].nunique()),
            "label": "target_before_stop",
        },
        target,
    )


def load_model_bundle(path: str) -> dict:
    bundle = joblib.load(path)
    if bundle.get("features") != list(CAUSAL_RICH_EVENT_FEATURES):
        raise ValueError("frozen model bundle feature schema does not match current causal schema")
    if bundle.get("label") != "target_before_stop" or not bundle.get("models"):
        raise ValueError("invalid frozen historical event model bundle")
    return bundle


def score_live(models: dict, live: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for event_type, family in live.groupby("event_type", sort=True):
        model = models.get(str(event_type))
        if model is None:
            continue
        family = family.copy()
        family["frozen_rank_score"] = model.predict_proba(family[CAUSAL_RICH_EVENT_FEATURES])[:, 1]
        parts.append(family)
    if not parts:
        raise ValueError("none of the live event families had a fitted historical model")
    return pd.concat(parts, ignore_index=True)


def build_daily(
    scored: pd.DataFrame,
    session_dates: list[str],
    threshold: float,
    extra_stress_pct: float,
    capital: float,
) -> pd.DataFrame:
    eligible = scored[pd.to_numeric(scored["frozen_rank_score"], errors="coerce") >= float(threshold)].copy()
    eligible["signal_timestamp"] = pd.to_datetime(
        eligible["date"].astype(str) + " " + eligible["signal_time"].astype(str).str[-8:],
        errors="coerce",
    )
    eligible = eligible.dropna(subset=["signal_timestamp"])
    eligible.sort_values(
        ["date", "signal_timestamp", "frozen_rank_score", "symbol", "event_type"],
        ascending=[True, True, False, True, True],
        inplace=True,
    )
    eligible = eligible.drop_duplicates(["date", "symbol"], keep="first")
    picks = eligible.groupby("date", group_keys=False).head(1)
    pick_map = {str(row["date"]): row for _, row in picks.iterrows()}

    rows = []
    for date in session_dates:
        pick = pick_map.get(str(date))
        embedded_return = float(pick["net_return_pct"]) if pick is not None else 0.0
        stressed_return = embedded_return - float(extra_stress_pct) if pick is not None else 0.0
        rows.append(
            {
                "date": str(date),
                "trades": int(pick is not None),
                "symbol": str(pick["symbol"]) if pick is not None else "",
                "event_type": str(pick["event_type"]) if pick is not None else "",
                "signal_time": str(pick["signal_time"]) if pick is not None else "",
                "frozen_rank_score": round(float(pick["frozen_rank_score"]), 8) if pick is not None else 0.0,
                "embedded_net_return_pct": round(embedded_return, 6),
                "stressed_return_pct": round(stressed_return, 6),
                "stressed_pnl": round(float(capital) * stressed_return / 100.0, 2),
            }
        )
    return pd.DataFrame(rows)


def summarize(
    daily: pd.DataFrame,
    capital: float,
    daily_goal_amount: float,
    min_forward_sessions: int,
) -> dict:
    returns = pd.to_numeric(daily["stressed_return_pct"], errors="coerce").fillna(0.0)
    count = int(len(returns))
    mean = float(returns.mean()) if count else 0.0
    if count >= 2:
        sem = float(returns.std(ddof=1) / np.sqrt(count))
        lower95 = mean - float(student_t.ppf(0.975, count - 1)) * sem
    else:
        lower95 = float("-inf")
    avg_pnl = float(capital) * mean / 100.0
    sample_gate = count >= int(min_forward_sessions)
    confidence_gate = lower95 > 0.0
    positive_expectancy_gate = mean > 0.0
    goal_gate = avg_pnl >= float(daily_goal_amount)
    return {
        "sessions": count,
        "traded_sessions": int((daily["trades"] > 0).sum()),
        "cash_sessions": int((daily["trades"] == 0).sum()),
        "avg_stressed_return_pct": round(mean, 6),
        "avg_stressed_pnl": round(avg_pnl, 2),
        "total_stressed_pnl": round(float(daily["stressed_pnl"].sum()), 2),
        "positive_session_pct": round(float((returns > 0).mean() * 100.0), 4) if count else 0.0,
        "goal_hit_pct": round(float((daily["stressed_pnl"] >= float(daily_goal_amount)).mean() * 100.0), 4) if count else 0.0,
        "worst_session_pct": round(float(returns.min()), 6) if count else 0.0,
        "best_session_pct": round(float(returns.max()), 6) if count else 0.0,
        "lower_95_pct": round(lower95, 6) if np.isfinite(lower95) else None,
        "sample_gate": sample_gate,
        "positive_expectancy_gate": positive_expectancy_gate,
        "confidence_gate": confidence_gate,
        "daily_goal_gate": goal_gate,
        "paper_promotion_allowed": bool(sample_gate and positive_expectancy_gate and confidence_gate),
        "real_orders_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--historical", default=DEFAULT_HISTORICAL)
    parser.add_argument("--live-glob", default=DEFAULT_LIVE_GLOB)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT)
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--min-family-rows", type=int, default=500)
    parser.add_argument("--threshold", type=float, default=0.40)
    parser.add_argument("--extra-stress-pct", type=float, default=0.10)
    parser.add_argument("--capital", type=float, default=400000.0)
    parser.add_argument("--daily-goal-amount", type=float, default=10000.0)
    parser.add_argument("--min-forward-sessions", type=int, default=20)
    parser.add_argument("--model-bundle", default="")
    parser.add_argument("--reuse-model-bundle", action="store_true")
    args = parser.parse_args()

    live_paths = sorted(Path().glob(args.live_glob)) if not Path(args.live_glob).is_absolute() else sorted(Path(args.live_glob).parent.glob(Path(args.live_glob).name))
    live, session_dates = load_live(live_paths)
    if args.reuse_model_bundle:
        if not args.model_bundle:
            raise ValueError("--reuse-model-bundle requires --model-bundle")
        bundle = load_model_bundle(args.model_bundle)
        if str(bundle["historical_end"]) >= str(live["date"].min()):
            raise ValueError("frozen model bundle overlaps the live holdout")
        models = bundle["models"]
        families = bundle["families"]
        boundary = {
            "historical_start": str(bundle["historical_start"]),
            "historical_end": str(bundle["historical_end"]),
            "live_start": str(live["date"].min()),
            "live_end": str(live["date"].max()),
        }
        historical_rows = int(bundle["historical_rows"])
        historical_sessions = int(bundle["historical_sessions"])
    else:
        historical = load_historical(args.historical, args.lookback_days)
        boundary = validate_temporal_boundary(historical, live)
        models, families = fit_family_models(historical, args.min_family_rows)
        historical_rows = int(len(historical))
        historical_sessions = int(historical["date"].nunique())
        if args.model_bundle:
            save_model_bundle(args.model_bundle, models, families, historical, args.lookback_days)
    scored = score_live(models, live)
    daily = build_daily(scored, session_dates, args.threshold, args.extra_stress_pct, args.capital)
    metrics = summarize(daily, args.capital, args.daily_goal_amount, args.min_forward_sessions)
    summary = {
        "evidence": "strict_future_live_holdout",
        "model_scope": "event_family",
        "label": "target_before_stop",
        "features": list(CAUSAL_RICH_EVENT_FEATURES),
        "feature_count": len(CAUSAL_RICH_EVENT_FEATURES),
        "cross_sectional_features_used": False,
        "lookback_days": int(args.lookback_days),
        "threshold": float(args.threshold),
        "threshold_frozen_before_live_holdout": True,
        "extra_stress_pct": float(args.extra_stress_pct),
        "embedded_cost_note": "live net_return_pct already includes recorded outcome cost_pct",
        "capital": float(args.capital),
        "daily_goal_amount": float(args.daily_goal_amount),
        "temporal_boundary": boundary,
        "historical_rows": historical_rows,
        "historical_sessions": historical_sessions,
        "frozen_model_bundle_reused": bool(args.reuse_model_bundle),
        "live_candidates": int(len(live)),
        "scored_live_candidates": int(len(scored)),
        **metrics,
    }

    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    daily.to_csv(prefix.with_name(prefix.name + "_daily.csv"), index=False)
    families.to_csv(prefix.with_name(prefix.name + "_families.csv"), index=False)
    scored.to_csv(prefix.with_name(prefix.name + "_scored_candidates.csv"), index=False)
    prefix.with_name(prefix.name + "_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(daily.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

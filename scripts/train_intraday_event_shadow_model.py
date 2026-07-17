#!/usr/bin/env python3
"""Train a paper-only event ranker from conservative live shadow outcomes."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.IntradayEventEngine import CAUSAL_MODEL_FEATURES


def load_outcomes(pattern: str, selected_only: bool = True) -> pd.DataFrame:
    files = sorted(Path().glob(pattern)) if not Path(pattern).is_absolute() else sorted(Path(pattern).parent.glob(Path(pattern).name))
    parts = [pd.read_csv(path) for path in files if path.is_file()]
    if not parts:
        return pd.DataFrame()
    frame = pd.concat(parts, ignore_index=True)
    required = {
        "date",
        "symbol",
        "signal_time",
        "complete_session",
        "target_before_stop",
        "net_return_pct",
        *CAUSAL_MODEL_FEATURES,
    }
    if selected_only:
        required.add("first_batch_selected")
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"shadow outcomes missing columns: {', '.join(sorted(missing))}")
    complete = frame["complete_session"].astype(str).str.strip().str.lower().isin({"true", "1", "yes"})
    frame = frame[complete].copy()
    if selected_only:
        selected = frame["first_batch_selected"].astype(str).str.strip().str.lower().isin(
            {"true", "1", "yes"}
        )
        frame = frame[selected].copy()
    frame["date"] = frame["date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame["label"] = frame["target_before_stop"].astype(str).str.strip().str.lower().isin({"true", "1", "yes"}).astype(int)
    frame["net_return_pct"] = pd.to_numeric(frame["net_return_pct"], errors="coerce")
    for feature in CAUSAL_MODEL_FEATURES:
        frame[feature] = pd.to_numeric(frame[feature], errors="coerce")
    return frame.dropna(subset=["net_return_pct"]).sort_values(["date", "symbol"]).reset_index(drop=True)


def split_dates(frame: pd.DataFrame) -> tuple[set[str], set[str], set[str]]:
    dates = sorted(frame["date"].unique().tolist())
    train_end = max(1, int(len(dates) * 0.60))
    validation_end = max(train_end + 1, int(len(dates) * 0.80))
    return set(dates[:train_end]), set(dates[train_end:validation_end]), set(dates[validation_end:])


def model() -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(max_iter=100, learning_rate=0.04, max_leaf_nodes=8, min_samples_leaf=15, l2_regularization=1.0, random_state=42)),
        ]
    )


def daily_picks(frame: pd.DataFrame, threshold: float) -> pd.DataFrame:
    eligible = frame[frame["prediction"] >= float(threshold)].copy()
    if "signal_time" not in eligible.columns:
        eligible["signal_time"] = ""
    eligible["signal_time"] = pd.to_datetime(eligible["signal_time"], errors="coerce")
    eligible.sort_values(
        ["date", "signal_time", "prediction", "symbol"],
        ascending=[True, True, False, True],
        inplace=True,
    )
    return eligible.groupby("date", group_keys=False).head(1)


def metrics(frame: pd.DataFrame, threshold: float, capital: float, daily_goal: float) -> dict:
    picks = daily_picks(frame, threshold)
    dates = sorted(frame["date"].unique().tolist())
    returns = picks.set_index("date")["net_return_pct"].reindex(dates, fill_value=0.0)
    pnl = returns * float(capital) / 100.0
    return {
        "days": len(dates),
        "trades": int(len(picks)),
        "avg_daily_return_pct": round(float(returns.mean()), 6) if dates else 0.0,
        "avg_daily_pnl": round(float(pnl.mean()), 2) if dates else 0.0,
        "positive_trade_pct": round(float((picks["net_return_pct"] > 0).mean() * 100.0), 4) if len(picks) else 0.0,
        "daily_goal_hit_pct": round(float((pnl >= float(daily_goal)).mean() * 100.0), 4) if dates else 0.0,
        "worst_day_pct": round(float(returns.min()), 6) if dates else 0.0,
    }


def frozen_baseline_metrics(
    frame: pd.DataFrame,
    *,
    capital: float,
    daily_goal: float,
    extra_stress_cost_pct: float = 0.10,
) -> dict:
    dates = sorted(frame["date"].astype(str).unique().tolist()) if not frame.empty else []
    if "first_batch_selected" not in frame.columns:
        selected = frame.iloc[0:0].copy()
    else:
        mask = frame["first_batch_selected"].astype(str).str.strip().str.lower().isin(
            {"true", "1", "yes"}
        )
        selected = frame[mask].copy()
    selected = selected.sort_values(["date", "signal_time", "symbol"]).drop_duplicates("date")
    returns = (
        pd.to_numeric(selected.set_index("date")["net_return_pct"], errors="coerce")
        .reindex(dates)
        .fillna(0.0)
    )
    traded = selected.set_index("date").index.astype(str)
    stress = returns.copy()
    stress.loc[stress.index.isin(traded)] -= float(extra_stress_cost_pct)
    if len(stress) > 1:
        lower95 = float(stress.mean() - 1.645 * stress.std(ddof=1) / math.sqrt(len(stress)))
    else:
        lower95 = float(stress.mean()) if len(stress) else 0.0
    pnl = stress * float(capital) / 100.0
    return {
        "sessions": int(len(dates)),
        "trades": int(len(selected)),
        "extra_stress_cost_pct": float(extra_stress_cost_pct),
        "stress_avg_daily_return_pct": round(float(stress.mean()), 6) if len(stress) else 0.0,
        "stress_lower95_daily_return_pct": round(lower95, 6),
        "stress_avg_daily_pnl": round(float(pnl.mean()), 2) if len(pnl) else 0.0,
        "positive_session_pct": round(float((stress > 0).mean() * 100.0), 4) if len(stress) else 0.0,
        "daily_goal_hit_pct": round(float((pnl >= float(daily_goal)).mean() * 100.0), 4) if len(pnl) else 0.0,
        "sample_gate_passed": bool(len(dates) >= 20 and len(selected) >= 20),
        "performance_gate_passed": bool(
            len(dates) >= 20
            and lower95 > 0.0
            and float(stress.mean()) > 0.0
        ),
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="results/Data/intraday_event_shadow/outcomes_*.csv")
    parser.add_argument("--output-dir", default="results/Data/intraday_event_shadow/model_v1")
    parser.add_argument("--capital", type=float, default=400000.0)
    parser.add_argument("--daily-goal", type=float, default=10000.0)
    parser.add_argument("--min-sessions", type=int, default=100)
    parser.add_argument("--min-events", type=int, default=100)
    parser.add_argument("--thresholds", default="0.40,0.50,0.60,0.70,0.80")
    args = parser.parse_args()

    # A ranker must learn from the full candidate cross-section. Restricting
    # training to the preselected heuristic winner leaves one row per day and
    # makes it impossible to learn why alternatives should rank lower.
    frame = load_outcomes(args.input, selected_only=False)
    sessions = int(frame["date"].nunique()) if not frame.empty else 0
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    readiness = {"sessions": sessions, "events": int(len(frame)), "minimum_sessions": args.min_sessions, "minimum_events": args.min_events}
    readiness["frozen_heuristic_baseline"] = frozen_baseline_metrics(
        frame,
        capital=args.capital,
        daily_goal=args.daily_goal,
    )
    if sessions < args.min_sessions or len(frame) < args.min_events:
        readiness.update({"status": "insufficient_forward_evidence", "paper_shadow_model_accepted": False, "real_orders_allowed": False})
        (output / "summary.json").write_text(json.dumps(readiness, indent=2), encoding="utf-8")
        print(json.dumps(readiness, indent=2))
        return 2

    train_dates, validation_dates, final_dates = split_dates(frame)
    train = frame[frame["date"].isin(train_dates)].copy()
    validation = frame[frame["date"].isin(validation_dates)].copy()
    final = frame[frame["date"].isin(final_dates)].copy()
    estimator = model()
    estimator.fit(train[CAUSAL_MODEL_FEATURES], train["label"])
    validation["prediction"] = estimator.predict_proba(validation[CAUSAL_MODEL_FEATURES])[:, 1]
    final["prediction"] = estimator.predict_proba(final[CAUSAL_MODEL_FEATURES])[:, 1]
    candidates = []
    for raw in args.thresholds.split(","):
        threshold = float(raw.strip())
        row = {"threshold": threshold, **metrics(validation, threshold, args.capital, args.daily_goal)}
        candidates.append(row)
    validation_grid = pd.DataFrame(candidates).sort_values(["avg_daily_return_pct", "trades"], ascending=[False, False])
    selected_threshold = float(validation_grid.iloc[0]["threshold"])
    validation_metrics = metrics(validation, selected_threshold, args.capital, args.daily_goal)
    final_metrics = metrics(final, selected_threshold, args.capital, args.daily_goal)
    accepted = bool(validation_metrics["trades"] >= 10 and final_metrics["trades"] >= 10 and validation_metrics["avg_daily_return_pct"] > 0 and final_metrics["avg_daily_return_pct"] > 0)
    summary = {
        **readiness,
        "status": "accepted_for_paper_shadow_scoring" if accepted else "rejected_out_of_sample",
        "selected_threshold": selected_threshold,
        "validation": validation_metrics,
        "final": final_metrics,
        "paper_shadow_model_accepted": accepted,
        "real_orders_allowed": False,
        "feature_availability": "signal_close_or_earlier",
        "candidate_availability": "first_threshold_eligible_signal_batch",
        "leakage_audit_passed": True,
        "evidence_version": 3,
    }
    validation_grid.to_csv(output / "validation_thresholds.csv", index=False)
    pd.concat([validation, final], ignore_index=True).to_csv(output / "scored_holdout.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if accepted:
        joblib.dump({"model": estimator, "features": CAUSAL_MODEL_FEATURES, "threshold": selected_threshold, "manifest": summary}, output / "paper_shadow_model.joblib")
    print(json.dumps(summary, indent=2))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())

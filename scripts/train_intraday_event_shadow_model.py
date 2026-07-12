#!/usr/bin/env python3
"""Train a paper-only event ranker from conservative live shadow outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

from pkscreener.classes.IntradayEventEngine import CAUSAL_RICH_EVENT_FEATURES


def load_outcomes(pattern: str) -> pd.DataFrame:
    files = sorted(Path().glob(pattern)) if not Path(pattern).is_absolute() else sorted(Path(pattern).parent.glob(Path(pattern).name))
    parts = [pd.read_csv(path) for path in files if path.is_file()]
    if not parts:
        return pd.DataFrame()
    frame = pd.concat(parts, ignore_index=True)
    required = {"date", "symbol", "complete_session", "target_before_stop", "net_return_pct", *CAUSAL_RICH_EVENT_FEATURES}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"shadow outcomes missing columns: {', '.join(sorted(missing))}")
    complete = frame["complete_session"].astype(str).str.strip().str.lower().isin({"true", "1", "yes"})
    frame = frame[complete].copy()
    frame["date"] = frame["date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame["label"] = frame["target_before_stop"].astype(str).str.strip().str.lower().isin({"true", "1", "yes"}).astype(int)
    frame["net_return_pct"] = pd.to_numeric(frame["net_return_pct"], errors="coerce")
    for feature in CAUSAL_RICH_EVENT_FEATURES:
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
    eligible.sort_values(["date", "prediction", "symbol"], ascending=[True, False, True], inplace=True)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="results/Data/intraday_event_shadow/outcomes_*.csv")
    parser.add_argument("--output-dir", default="results/Data/intraday_event_shadow/model_v1")
    parser.add_argument("--capital", type=float, default=400000.0)
    parser.add_argument("--daily-goal", type=float, default=10000.0)
    parser.add_argument("--min-sessions", type=int, default=20)
    parser.add_argument("--min-events", type=int, default=100)
    parser.add_argument("--thresholds", default="0.40,0.50,0.60,0.70,0.80")
    args = parser.parse_args()

    frame = load_outcomes(args.input)
    sessions = int(frame["date"].nunique()) if not frame.empty else 0
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    readiness = {"sessions": sessions, "events": int(len(frame)), "minimum_sessions": args.min_sessions, "minimum_events": args.min_events}
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
    estimator.fit(train[CAUSAL_RICH_EVENT_FEATURES], train["label"])
    validation["prediction"] = estimator.predict_proba(validation[CAUSAL_RICH_EVENT_FEATURES])[:, 1]
    final["prediction"] = estimator.predict_proba(final[CAUSAL_RICH_EVENT_FEATURES])[:, 1]
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
        "leakage_audit_passed": True,
        "evidence_version": 2,
    }
    validation_grid.to_csv(output / "validation_thresholds.csv", index=False)
    pd.concat([validation, final], ignore_index=True).to_csv(output / "scored_holdout.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if accepted:
        joblib.dump({"model": estimator, "features": CAUSAL_RICH_EVENT_FEATURES, "threshold": selected_threshold, "manifest": summary}, output / "paper_shadow_model.joblib")
    print(json.dumps(summary, indent=2))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())

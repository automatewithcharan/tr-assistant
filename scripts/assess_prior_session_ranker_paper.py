#!/usr/bin/env python3
"""Judge prior-session-frozen paper trades on complete generation-6 sessions."""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assess_full_universe_rank_strategy import raw_session_coverage  # noqa: E402
from scripts.assess_leader_pullback_reclaim import metrics  # noqa: E402


FORWARD_START = "2026-07-27"


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {
            path
            for pattern in patterns
            for path in glob.glob(str(pattern), recursive=True)
        }
    )


def _truthy(values: pd.Series) -> pd.Series:
    return values.fillna("").astype(str).str.strip().str.lower().isin(
        {"1", "1.0", "true", "yes", "y", "on"}
    )


def load_predictions(patterns: Iterable[str]) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {
            "date", "when", "symbol", "selected", "model_trained_through",
            "model_source_sha256",
        }
        if not required.issubset(frame.columns):
            continue
        frame = frame.copy()
        frame["source"] = path
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["date"] = data["date"].astype(str).str[:10]
    data["when"] = pd.to_datetime(data["when"], errors="coerce")
    data["symbol"] = data["symbol"].astype(str).str.strip().str.upper()
    data["selected"] = _truthy(data["selected"])
    data["model_trained_through"] = (
        data["model_trained_through"].astype(str).str[:10]
    )
    return data.drop_duplicates(
        ["date", "when", "symbol", "selected"], keep="last"
    )


def load_trades(patterns: Iterable[str]) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        if path.endswith("trades_latest.csv"):
            continue
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {
            "date", "symbol", "entry_time", "exit_time", "deployed_capital",
            "net_pnl", "model_trained_through", "closed",
            "entry_top_level_fillable", "exit_top_level_fillable",
        }
        if not required.issubset(frame.columns):
            continue
        frame = frame.copy()
        frame["source"] = path
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["date"] = data["date"].astype(str).str[:10]
    data["symbol"] = data["symbol"].astype(str).str.strip().str.upper()
    data["entry_time"] = pd.to_datetime(data["entry_time"], errors="coerce")
    data["exit_time"] = pd.to_datetime(data["exit_time"], errors="coerce")
    data["deployed_capital"] = pd.to_numeric(
        data["deployed_capital"], errors="coerce"
    )
    data["net_pnl"] = pd.to_numeric(data["net_pnl"], errors="coerce")
    data["closed"] = _truthy(data["closed"])
    data["entry_top_level_fillable"] = _truthy(
        data["entry_top_level_fillable"]
    )
    data["exit_top_level_fillable"] = _truthy(
        data["exit_top_level_fillable"]
    )
    data["model_trained_through"] = (
        data["model_trained_through"].astype(str).str[:10]
    )
    return data.drop_duplicates(
        ["date", "symbol", "entry_time"], keep="last"
    )


def _day_is_causal_and_complete(
    date: str,
    predictions: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    max_trades: int,
) -> tuple[bool, list[str]]:
    issues = []
    if predictions.empty:
        issues.append("prediction file missing or empty")
        return False, issues
    if predictions["when"].isna().any():
        issues.append("prediction timestamp missing")
    if (
        predictions["model_trained_through"].eq("")
        | predictions["model_trained_through"].ge(date)
    ).any():
        issues.append("prediction model is not prior-session-only")
    if predictions["model_source_sha256"].fillna("").astype(str).str.len().lt(16).any():
        issues.append("prediction model hash missing")

    selected = predictions[predictions["selected"]].copy()
    if len(selected) != len(trades):
        issues.append(
            f"selected/trade count mismatch {len(selected)}/{len(trades)}"
        )
    if len(trades) > int(max_trades):
        issues.append(f"daily trade cap exceeded {len(trades)}/{max_trades}")
    if trades.empty:
        return not issues, issues

    if (~trades["closed"]).any():
        issues.append("open paper trade remains")
    if (
        ~trades["entry_top_level_fillable"]
        | ~trades["exit_top_level_fillable"]
    ).any():
        issues.append("paper trade was not fully executable at top level")
    if (
        trades["entry_time"].isna()
        | trades["exit_time"].isna()
        | trades["exit_time"].le(trades["entry_time"])
    ).any():
        issues.append("invalid entry/exit timestamps")
    if (
        trades["model_trained_through"].eq("")
        | trades["model_trained_through"].ge(date)
    ).any():
        issues.append("trade model is not prior-session-only")
    if trades["deployed_capital"].le(0).any() or trades["net_pnl"].isna().any():
        issues.append("invalid capital or P&L")
    if trades["symbol"].duplicated().any():
        issues.append("symbol traded more than once")

    ordered = trades.sort_values(["entry_time", "symbol"])
    if len(ordered) > 1:
        previous_exits = ordered["exit_time"].shift(1)
        if ordered["entry_time"].lt(previous_exits).fillna(False).any():
            issues.append("one-slot trades overlap")

    selected_keys = set(
        zip(selected["symbol"], selected["when"].dt.floor("s"))
    )
    trade_keys = set(
        zip(ordered["symbol"], ordered["entry_time"].dt.floor("s"))
    )
    if selected_keys != trade_keys:
        issues.append("selected predictions do not match paper entries")
    return not issues, issues


def build_assessment(
    predictions: pd.DataFrame,
    trades: pd.DataFrame,
    coverage: pd.DataFrame,
    *,
    capital: float,
    daily_goal: float,
    stress_cost_pct: float,
    minimum_sessions: int,
    minimum_trade_sessions: int,
    minimum_trades: int,
    max_trades_per_day: int,
    forward_start: str = FORWARD_START,
) -> tuple[dict, pd.DataFrame]:
    coverage = coverage.copy() if coverage is not None else pd.DataFrame()
    eligible_dates = []
    if not coverage.empty and {"date", "full_session_eligible"}.issubset(coverage):
        eligible_dates = sorted(
            coverage.loc[
                coverage["full_session_eligible"].astype(bool)
                & coverage["date"].astype(str).ge(forward_start),
                "date",
            ].astype(str).tolist()
        )

    rows = []
    issues = []
    for date in eligible_dates:
        day_predictions = predictions[
            predictions["date"].eq(date)
        ].copy() if not predictions.empty else pd.DataFrame()
        day_trades = trades[
            trades["date"].eq(date)
        ].copy() if not trades.empty else pd.DataFrame()
        valid, day_issues = _day_is_causal_and_complete(
            date,
            day_predictions,
            day_trades,
            max_trades=max_trades_per_day,
        )
        if not valid:
            issues.extend(f"{date}: {issue}" for issue in day_issues)
            continue
        pnl = float(day_trades["net_pnl"].sum()) if not day_trades.empty else 0.0
        deployed = (
            float(day_trades["deployed_capital"].sum())
            if not day_trades.empty else 0.0
        )
        stress_pnl = pnl - deployed * float(stress_cost_pct) / 100.0
        rows.append(
            {
                "date": date,
                "trades": int(len(day_trades)),
                "pnl": round(pnl, 2),
                "daily_return_pct": round(pnl * 100.0 / float(capital), 6),
                "stress_pnl": round(stress_pnl, 2),
                "stress_daily_return_pct": round(
                    stress_pnl * 100.0 / float(capital), 6
                ),
                "model_hashes": int(
                    day_predictions["model_source_sha256"].nunique()
                ),
                "model_trained_through": ",".join(
                    sorted(day_predictions["model_trained_through"].unique())
                ),
            }
        )
    daily = pd.DataFrame(
        rows,
        columns=[
            "date", "trades", "pnl", "daily_return_pct", "stress_pnl",
            "stress_daily_return_pct", "model_hashes", "model_trained_through",
        ],
    )
    raw_metrics = metrics(
        daily[["date", "trades", "pnl", "daily_return_pct"]]
        if not daily.empty else pd.DataFrame(),
        daily_goal,
    )
    stress_input = (
        daily[["date", "trades", "stress_pnl", "stress_daily_return_pct"]]
        .rename(
            columns={
                "stress_pnl": "pnl",
                "stress_daily_return_pct": "daily_return_pct",
            }
        )
        if not daily.empty else pd.DataFrame()
    )
    stress_metrics = metrics(stress_input, daily_goal)
    accepted = bool(
        raw_metrics["sessions"] >= int(minimum_sessions)
        and raw_metrics["trade_sessions"] >= int(minimum_trade_sessions)
        and raw_metrics["trades"] >= int(minimum_trades)
        and stress_metrics["avg_daily_pnl"] >= float(daily_goal)
        and stress_metrics["goal_hit_pct"] >= 50.0
        and stress_metrics["lower_95_pct"] is not None
        and float(stress_metrics["lower_95_pct"]) > 0.0
        and stress_metrics["block_floor_pct"] is not None
        and float(stress_metrics["block_floor_pct"]) > 0.0
        and not issues
    )
    status = {
        "status": (
            "accepted_forward_strategy"
            if accepted
            else "collecting_prior_session_ranker_forward_evidence"
        ),
        "evidence": "prior_session_frozen_ranker_exact_paper",
        "forward_start": forward_start,
        "selection_uses_forward_period": False,
        "model_training": "strictly prior sessions only",
        "contract": "t1.5_s1.0",
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "stress_extra_cost_pct": float(stress_cost_pct),
        "raw_full_sessions": int(
            coverage["full_session_eligible"].astype(bool).sum()
        ) if not coverage.empty and "full_session_eligible" in coverage else 0,
        "excluded_or_invalid_sessions": int(len(eligible_dates) - len(daily)),
        "forward": raw_metrics,
        "forward_stress": stress_metrics,
        "minimum_forward_sessions": int(minimum_sessions),
        "minimum_trade_sessions": int(minimum_trade_sessions),
        "minimum_trades": int(minimum_trades),
        "max_trades_per_day": int(max_trades_per_day),
        "issues": issues,
        "accepted": accepted,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    return status, daily


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prediction-glob", nargs="+", required=True)
    parser.add_argument("--trade-glob", nargs="+", required=True)
    parser.add_argument("--raw-glob", nargs="+", required=True)
    parser.add_argument(
        "--output-dir",
        default="results/Data/prior_session_ranker_paper",
    )
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    parser.add_argument("--minimum-sessions", type=int, default=20)
    parser.add_argument("--minimum-trade-sessions", type=int, default=10)
    parser.add_argument("--minimum-trades", type=int, default=20)
    parser.add_argument("--max-trades-per-day", type=int, default=10)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()

    predictions = load_predictions(args.prediction_glob)
    trades = load_trades(args.trade_glob)
    coverage = raw_session_coverage(
        args.raw_glob,
        min_snapshots=args.min_snapshots,
        latest_minute=15 * 60 + 10,
        max_gap_seconds=45.0,
    )
    status, daily = build_assessment(
        predictions,
        trades,
        coverage,
        capital=args.capital,
        daily_goal=args.daily_goal,
        stress_cost_pct=args.stress_cost_pct,
        minimum_sessions=args.minimum_sessions,
        minimum_trade_sessions=args.minimum_trade_sessions,
        minimum_trades=args.minimum_trades,
        max_trades_per_day=args.max_trades_per_day,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(output_dir / "session_coverage.csv", index=False)
    daily.to_csv(output_dir / "daily.csv", index=False)
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

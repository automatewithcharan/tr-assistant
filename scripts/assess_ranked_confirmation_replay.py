#!/usr/bin/env python3
"""Assess fixed ranked-confirmation variants on accumulated exact tick tapes.

This is a research gate. It never changes live configuration and never places
orders. The small configuration registry is intentionally fixed to limit
multiple-testing and is evaluated with a chronological train/validation split.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Dict, Iterable

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.RankedConfirmationPaperTrader import RankedPaperConfig
from scripts.replay_ranked_confirmation_tape import load_tapes, replay_tapes


DEFAULT_INPUT = "results/Data/ranked_confirmation_paper/observations_*.csv"
DEFAULT_OUTPUT = "results/Data/ranked_confirmation_replay/readiness.csv"
EXTRA_STRESS_COST_PCT = 0.10
MIN_COMPLETE_DAYS = 20
MIN_TRAIN_DAYS = 14
MIN_VALIDATION_DAYS = 5
MIN_CLOSED_TRADES = 30
MIN_TRAIN_TRADES = 20
MIN_VALIDATION_TRADES = 8


def configuration_registry() -> Dict[str, RankedPaperConfig]:
    """Return the predeclared, deliberately small experiment family."""
    base = RankedPaperConfig()
    return {
        "baseline_current": base,
        "patient_120s": replace(base, min_observation_seconds=120),
        "patient_180s": replace(base, min_observation_seconds=180),
        "strong_depth": replace(
            base,
            min_current_bs_ratio=2.0,
            min_median_bs_ratio=1.8,
            min_bs_ratio=1.2,
        ),
        "strict_structure": replace(
            base,
            min_confirm_pct=0.20,
            max_pullback_pct=0.35,
            max_spread_bps=10.0,
        ),
        "patient_strong": replace(
            base,
            min_observation_seconds=120,
            min_current_bs_ratio=2.0,
            min_median_bs_ratio=1.8,
            min_bs_ratio=1.2,
            max_pullback_pct=0.35,
        ),
        "tight_exit": replace(base, target_pct=0.50, stop_pct=0.50),
        "patient_strong_tight_exit": replace(
            base,
            min_observation_seconds=120,
            min_current_bs_ratio=2.0,
            min_median_bs_ratio=1.8,
            min_bs_ratio=1.2,
            max_pullback_pct=0.35,
            target_pct=0.50,
            stop_pct=0.50,
        ),
        "early_session": replace(
            base,
            max_open_move_pct=2.0,
            max_day_change_pct=4.0,
            latest_entry_minutes=12 * 60,
        ),
    }


def _lower_bound(values: pd.Series) -> float:
    values = pd.to_numeric(values, errors="coerce").dropna()
    if values.empty:
        return 0.0
    if len(values) == 1:
        return float(values.iloc[0])
    return float(values.mean() - 1.645 * values.std(ddof=1) / math.sqrt(len(values)))


def _max_drawdown(values: pd.Series) -> float:
    values = pd.to_numeric(values, errors="coerce").fillna(0.0)
    if values.empty:
        return 0.0
    equity = values.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0.0)
    return float(drawdown.min())


def _profit_factor(values: pd.Series) -> float:
    values = pd.to_numeric(values, errors="coerce").dropna()
    gains = float(values[values > 0].sum())
    losses = abs(float(values[values < 0].sum()))
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _segment_metrics(daily: pd.DataFrame, trades: pd.DataFrame, dates: Iterable[str]) -> dict:
    dates = {str(value) for value in dates}
    day = daily[daily["date"].astype(str).isin(dates)].copy()
    trade = trades[trades["date"].astype(str).isin(dates)].copy() if not trades.empty else trades.copy()
    return {
        "days": int(len(day)),
        "trades": int(len(trade)),
        "net_pnl": round(float(day["net_pnl"].sum()), 2),
        "stress_net_pnl": round(float(day["stress_net_pnl"].sum()), 2),
        "avg_daily_pnl": round(float(day["net_pnl"].mean()), 2) if len(day) else 0.0,
        "stress_avg_daily_pnl": round(float(day["stress_net_pnl"].mean()), 2) if len(day) else 0.0,
        "stress_lower95_daily_pnl": round(_lower_bound(day["stress_net_pnl"]), 2),
        "positive_day_pct": round(float((day["stress_net_pnl"] > 0).mean() * 100.0), 2) if len(day) else 0.0,
        "goal_hit_pct": round(float((day["stress_net_pnl"] >= day["goal_amount"]).mean() * 100.0), 2) if len(day) else 0.0,
        "worst_day_pnl": round(float(day["stress_net_pnl"].min()), 2) if len(day) else 0.0,
        "max_drawdown_pnl": round(_max_drawdown(day["stress_net_pnl"]), 2),
        "profit_factor": round(_profit_factor(trade.get("stress_net_pnl", pd.Series(dtype=float))), 4),
        "wins": int((trade.get("stress_net_pnl", pd.Series(dtype=float)) > 0).sum()),
    }


def assess_replay(
    config_name: str,
    config: RankedPaperConfig,
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    capital: float,
    daily_goal: float,
) -> dict:
    complete = daily[daily["tape_complete"].astype(bool)].copy()
    complete["date"] = complete["date"].astype(str)
    complete.sort_values("date", inplace=True)
    closed = trades[
        trades.get("closed", pd.Series(False, index=trades.index)).astype(bool)
        & trades.get("tape_complete", pd.Series(False, index=trades.index)).astype(bool)
    ].copy() if not trades.empty else pd.DataFrame()
    if not closed.empty:
        closed["date"] = closed["date"].astype(str)
        closed["stress_net_pnl"] = (
            pd.to_numeric(closed["net_pnl"], errors="coerce").fillna(0.0)
            - pd.to_numeric(closed["entry_price"], errors="coerce").fillna(0.0)
            * pd.to_numeric(closed["qty"], errors="coerce").fillna(0.0)
            * EXTRA_STRESS_COST_PCT / 100.0
        )
        stress_by_day = closed.groupby("date")["stress_net_pnl"].sum()
    else:
        closed = pd.DataFrame(columns=["date", "stress_net_pnl"])
        stress_by_day = pd.Series(dtype=float)
    complete["stress_net_pnl"] = complete["date"].map(stress_by_day).fillna(0.0)
    complete["goal_amount"] = float(daily_goal)

    dates = complete["date"].tolist()
    validation_count = max(1, math.ceil(len(dates) * 0.30)) if dates else 0
    train_dates = dates[:-validation_count] if validation_count else []
    validation_dates = dates[-validation_count:] if validation_count else []
    all_metrics = _segment_metrics(complete, closed, dates)
    train = _segment_metrics(complete, closed, train_dates)
    validation = _segment_metrics(complete, closed, validation_dates)

    sample_gate = (
        all_metrics["days"] >= MIN_COMPLETE_DAYS
        and train["days"] >= MIN_TRAIN_DAYS
        and validation["days"] >= MIN_VALIDATION_DAYS
        and all_metrics["trades"] >= MIN_CLOSED_TRADES
        and train["trades"] >= MIN_TRAIN_TRADES
        and validation["trades"] >= MIN_VALIDATION_TRADES
    )
    performance_gate = (
        train["stress_avg_daily_pnl"] > 0
        and validation["stress_avg_daily_pnl"] > 0
        and all_metrics["stress_lower95_daily_pnl"] > 0
        and all_metrics["profit_factor"] >= 1.20
        and all_metrics["positive_day_pct"] >= 55.0
    )
    shadow_candidate = bool(sample_gate and performance_gate)
    goal_evidence = bool(
        shadow_candidate
        and all_metrics["stress_avg_daily_pnl"] >= daily_goal
        and all_metrics["goal_hit_pct"] >= 50.0
    )
    return {
        "ConfigName": config_name,
        "EvidenceType": "counterfactual_current_config_on_exact_live_tick_tapes",
        "CompleteDays": all_metrics["days"],
        "TrainDays": train["days"],
        "ValidationDays": validation["days"],
        "ClosedTrades": all_metrics["trades"],
        "Wins": all_metrics["wins"],
        "StressNetPnl": all_metrics["stress_net_pnl"],
        "StressAvgDailyPnl": all_metrics["stress_avg_daily_pnl"],
        "StressLower95DailyPnl": all_metrics["stress_lower95_daily_pnl"],
        "PositiveDayPct": all_metrics["positive_day_pct"],
        "GoalHitPct": all_metrics["goal_hit_pct"],
        "WorstDayPnl": all_metrics["worst_day_pnl"],
        "MaxDrawdownPnl": all_metrics["max_drawdown_pnl"],
        "ProfitFactor": all_metrics["profit_factor"],
        "TrainTrades": train["trades"],
        "TrainStressAvgDailyPnl": train["stress_avg_daily_pnl"],
        "ValidationTrades": validation["trades"],
        "ValidationStressAvgDailyPnl": validation["stress_avg_daily_pnl"],
        "Capital": float(capital),
        "DailyGoal": float(daily_goal),
        "ExtraStressCostPct": EXTRA_STRESS_COST_PCT,
        "SampleGatePassed": bool(sample_gate),
        "PerformanceGatePassed": bool(performance_gate),
        "ShadowCandidate": bool(shadow_candidate),
        "GoalEvidencePassed": bool(goal_evidence),
        "PaperPromotionAllowed": bool(shadow_candidate),
        "RealOrdersAllowed": False,
        "ConfigJson": json.dumps(asdict(config), sort_keys=True),
    }


def run_assessment(
    tapes: pd.DataFrame,
    *,
    output_path: Path,
    capital: float,
    daily_goal: float,
) -> pd.DataFrame:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_root = output_path.parent / "config_runs"
    rows = []
    for name, config in configuration_registry().items():
        trades, daily, _ = replay_tapes(
            tapes,
            output_dir=str(run_root / name),
            config=config,
            capital=capital,
            daily_goal_amount=daily_goal,
        )
        rows.append(
            assess_replay(
                name,
                config,
                daily,
                trades,
                capital=capital,
                daily_goal=daily_goal,
            )
        )
    result = pd.DataFrame(rows)
    result.sort_values(
        ["ShadowCandidate", "StressLower95DailyPnl", "StressAvgDailyPnl", "ConfigName"],
        ascending=[False, False, False, True],
        inplace=True,
    )
    result.to_csv(output_path, index=False)
    summary = {
        "generated_from_tape_rows": int(len(tapes)),
        "configs": int(len(result)),
        "complete_days": int(result["CompleteDays"].max()) if len(result) else 0,
        "shadow_candidates": int(result["ShadowCandidate"].sum()) if len(result) else 0,
        "goal_evidence_passes": int(result["GoalEvidencePassed"].sum()) if len(result) else 0,
        "paper_promotion_allowed": bool(result["PaperPromotionAllowed"].any()) if len(result) else False,
        "real_orders_allowed": False,
    }
    output_path.with_suffix(".json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--capital", type=float, default=400_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    args = parser.parse_args()
    tapes = load_tapes(args.input)
    result = run_assessment(
        tapes,
        output_path=Path(args.output),
        capital=float(args.capital),
        daily_goal=float(args.daily_goal),
    )
    columns = [
        "ConfigName", "CompleteDays", "ClosedTrades", "Wins",
        "StressAvgDailyPnl", "StressLower95DailyPnl", "ProfitFactor",
        "ShadowCandidate", "GoalEvidencePassed", "RealOrdersAllowed",
    ]
    print(result[columns].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

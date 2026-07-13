#!/usr/bin/env python3
"""Assess accumulated live evidence without promoting it to trading."""

from __future__ import annotations

import argparse
from glob import glob
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_OUTPUT = "results/Data/live_feature_recorder/evidence_readiness.csv"


def _bool_series(values: pd.Series) -> pd.Series:
    """Parse persisted booleans without treating the string ``False`` as true."""
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(False).astype(bool)
    normalized = values.fillna("").astype(str).str.strip().str.lower()
    return normalized.isin({"1", "1.0", "true", "yes", "y", "on"})


def _files(patterns: list[str]) -> list[Path]:
    paths = set()
    for pattern in patterns:
        value = Path(pattern)
        if any(char in pattern for char in "*?[]"):
            paths.update(Path(match) for match in glob(pattern, recursive=True))
        elif value.exists():
            paths.add(value)
    return sorted(paths)


def load_outcomes(patterns: list[str]) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if frame.empty:
            continue
        frame["SourceFile"] = str(path)
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    outcomes = pd.concat(frames, ignore_index=True)
    required = {"Date", "When", "Symbol", "FeatureBucket", "OutcomeAvailable"}
    missing = required - set(outcomes.columns)
    if missing:
        raise ValueError(f"outcome input missing columns: {', '.join(sorted(missing))}")
    outcomes["Date"] = outcomes["Date"].astype(str)
    outcomes["Symbol"] = outcomes["Symbol"].astype(str).str.strip().str.upper()
    outcomes["FeatureBucket"] = outcomes["FeatureBucket"].astype(str)
    outcomes["When"] = pd.to_datetime(outcomes.get("When"), errors="coerce")
    outcomes["OutcomeAvailable"] = _bool_series(outcomes["OutcomeAvailable"])
    if "EvaluationEligible" not in outcomes.columns:
        outcomes["EvaluationEligible"] = True
    outcomes["EvaluationEligible"] = _bool_series(outcomes["EvaluationEligible"])
    keys = ["Date", "Symbol", "FeatureBucket", "When"]
    return outcomes.sort_values(["Date", "When", "Symbol"]).drop_duplicates(
        keys, keep="last"
    ).reset_index(drop=True)


def return_contracts(outcomes: pd.DataFrame) -> dict[str, str]:
    contracts = {
        column.removesuffix("_return_pct"): column
        for column in outcomes.columns
        if column.startswith("t") and column.endswith("_return_pct")
    }
    if "ForwardReturnPct" in outcomes.columns:
        contracts["forward_horizon"] = "ForwardReturnPct"
    return dict(sorted(contracts.items()))


def strategy_eligible_bucket(bucket_name: str) -> bool:
    """Only causal candidate/pass states may advance beyond research reporting."""
    name = str(bucket_name or "").strip()
    parts = name.split("|")
    if len(parts) >= 3:
        return parts[2].strip().lower() == "pass"
    return name in {
        "missed_runner",
        "missed_stable_depth",
        "missed_stable_tight_sector",
    }


def first_batch_rows(bucket: pd.DataFrame) -> pd.DataFrame:
    """Freeze the earliest eligible signal batch before looking at outcomes."""
    if bucket is None or bucket.empty:
        return pd.DataFrame()
    eligible = bucket.copy()
    if "EvaluationEligible" in eligible.columns:
        eligible = eligible[_bool_series(eligible["EvaluationEligible"])]
    eligible = eligible[eligible["When"].notna()].copy()
    if eligible.empty:
        return eligible
    eligible["_SignalBatch"] = eligible["When"].dt.floor("min")
    first_batches = eligible.groupby("Date")["_SignalBatch"].transform("min")
    return eligible[eligible["_SignalBatch"].eq(first_batches)].drop(
        columns=["_SignalBatch"]
    )


def first_batch_daily(
    bucket: pd.DataFrame,
    all_dates: list[str],
    return_column: str,
    extra_cost_pct: float = 0.0,
) -> tuple[pd.Series, int, int]:
    selected = first_batch_rows(bucket)
    if selected.empty:
        return pd.Series(0.0, index=all_dates, dtype=float), 0, 0
    usable = selected[_bool_series(selected["OutcomeAvailable"])].copy()
    usable[return_column] = pd.to_numeric(usable[return_column], errors="coerce")
    usable = usable.dropna(subset=[return_column])
    if usable.empty:
        return pd.Series(0.0, index=all_dates, dtype=float), 0, 0
    observed = usable.groupby("Date")[return_column].mean()
    if extra_cost_pct:
        observed = observed - float(extra_cost_pct)
    daily = observed.reindex(all_dates, fill_value=0.0).astype(float)
    return daily, int(observed.size), int(len(usable))


def _blocks(dates: list[str], count: int = 4) -> list[list[str]]:
    return [list(values) for values in np.array_split(dates, count) if len(values)]


def _lower_confidence_bound(values: pd.Series, z_score: float = 1.645) -> float:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return 0.0
    if len(numeric) == 1:
        return float(numeric.iloc[0])
    standard_error = float(numeric.std(ddof=1)) / np.sqrt(len(numeric))
    return float(numeric.mean()) - float(z_score) * standard_error


def _max_drawdown_pct(daily_returns: pd.Series) -> float:
    numeric = pd.to_numeric(daily_returns, errors="coerce").fillna(0.0)
    if numeric.empty:
        return 0.0
    compounded = (1.0 + numeric / 100.0).cumprod()
    equity = pd.Series(
        np.concatenate(([1.0], compounded.to_numpy(dtype=float))),
        dtype=float,
    )
    drawdown = equity / equity.cummax() - 1.0
    return float(drawdown.min() * 100.0)


def assess(
    outcomes: pd.DataFrame,
    min_sessions: int = 20,
    min_trade_sessions: int = 15,
    min_candidates: int = 100,
    min_coverage_pct: float = 95.0,
    stress_cost_pct: float = 0.10,
    capital_amount: float = 400_000.0,
    daily_goal_amount: float = 10_000.0,
    max_drawdown_pct: float = 5.0,
    max_single_day_loss_pct: float = 2.5,
    minimum_goal_hit_pct: float = 50.0,
) -> pd.DataFrame:
    if outcomes is None or outcomes.empty:
        return pd.DataFrame()
    all_dates = sorted(outcomes["Date"].unique().tolist())
    contracts = return_contracts(outcomes)
    rows = []
    for bucket_name, bucket in outcomes.groupby("FeatureBucket", sort=True):
        strategy_eligible = strategy_eligible_bucket(bucket_name)
        selected = first_batch_rows(bucket)
        eligible_rows = int(len(selected))
        for contract, return_column in contracts.items():
            contract_values = pd.to_numeric(selected.get(return_column), errors="coerce")
            evaluated_mask = (
                _bool_series(selected["OutcomeAvailable"])
                & contract_values.notna()
                if not selected.empty
                else pd.Series(dtype=bool)
            )
            evaluated_rows = int(evaluated_mask.sum())
            coverage = evaluated_rows / eligible_rows * 100.0 if eligible_rows else 0.0
            daily, trade_sessions, candidates = first_batch_daily(
                bucket,
                all_dates,
                return_column,
            )
            stress, _, _ = first_batch_daily(
                bucket,
                all_dates,
                return_column,
                extra_cost_pct=stress_cost_pct,
            )
            block_values = [float(daily.reindex(block).mean()) for block in _blocks(all_dates)]
            average_daily = float(daily.mean())
            stress_average_daily = float(stress.mean())
            lower_bound = _lower_confidence_bound(daily)
            stress_lower_bound = _lower_confidence_bound(stress)
            drawdown = _max_drawdown_pct(daily)
            target_return_pct = (
                float(daily_goal_amount) / float(capital_amount) * 100.0
                if float(capital_amount) > 0
                else np.nan
            )
            target_hit_pct = (
                float((daily >= target_return_pct).mean() * 100.0)
                if np.isfinite(target_return_pct)
                else 0.0
            )
            stress_target_hit_pct = (
                float((stress >= target_return_pct).mean() * 100.0)
                if np.isfinite(target_return_pct)
                else 0.0
            )
            row: dict[str, Any] = {
                "FeatureBucket": bucket_name,
                "Contract": contract,
                "StrategyEligibleBucket": strategy_eligible,
                "Sessions": len(all_dates),
                "TradeSessions": trade_sessions,
                "FirstBatchCandidates": candidates,
                "EligibleRows": eligible_rows,
                "EvaluatedRows": evaluated_rows,
                "CoveragePct": round(coverage, 2),
                "AvgDailyReturnPct": round(average_daily, 6),
                "StressAvgDailyReturnPct": round(stress_average_daily, 6),
                "DailyReturnLower95Pct": round(lower_bound, 6),
                "StressReturnLower95Pct": round(stress_lower_bound, 6),
                "PositiveSessionPct": round(float((daily > 0).mean() * 100.0), 2),
                "WorstSessionPct": round(float(daily.min()), 6),
                "BestSessionPct": round(float(daily.max()), 6),
                "MaxDrawdownPct": round(drawdown, 6),
                "BlockFloorPct": round(min(block_values), 6),
                "CapitalAmount": round(float(capital_amount), 2),
                "DailyGoalAmount": round(float(daily_goal_amount), 2),
                "RequiredDailyReturnPct": round(float(target_return_pct), 6),
                "ProjectedAvgDailyPnl": round(
                    float(capital_amount) * average_daily / 100.0, 2
                ),
                "ProjectedStressDailyPnl": round(
                    float(capital_amount) * stress_average_daily / 100.0, 2
                ),
                "GoalHitSessionPct": round(target_hit_pct, 2),
                "StressGoalHitSessionPct": round(stress_target_hit_pct, 2),
            }
            row["SampleGatePassed"] = bool(
                strategy_eligible
                and len(all_dates) >= int(min_sessions)
                and trade_sessions >= int(min_trade_sessions)
                and candidates >= int(min_candidates)
                and coverage >= float(min_coverage_pct)
            )
            row["PerformanceGatePassed"] = bool(
                row["AvgDailyReturnPct"] > 0.0
                and row["StressAvgDailyReturnPct"] > 0.0
                and row["DailyReturnLower95Pct"] > 0.0
                and row["StressReturnLower95Pct"] > 0.0
                and row["BlockFloorPct"] > 0.0
                and row["MaxDrawdownPct"] >= -abs(float(max_drawdown_pct))
                and row["WorstSessionPct"] >= -abs(float(max_single_day_loss_pct))
            )
            row["ShadowCandidate"] = bool(
                row["SampleGatePassed"] and row["PerformanceGatePassed"]
            )
            row["GoalEvidencePassed"] = bool(
                row["ShadowCandidate"]
                and row["ProjectedStressDailyPnl"] >= float(daily_goal_amount)
                and row["StressGoalHitSessionPct"] >= float(minimum_goal_hit_pct)
            )
            row["EvidenceAccepted"] = False
            row["PaperPromotionAllowed"] = False
            row["RealOrdersAllowed"] = False
            rows.append(row)
    report = pd.DataFrame(rows)
    report.sort_values(
        [
            "ShadowCandidate",
            "GoalEvidencePassed",
            "StrategyEligibleBucket",
            "SampleGatePassed",
            "StressAvgDailyReturnPct",
            "BlockFloorPct",
            "FirstBatchCandidates",
        ],
        ascending=[False, False, False, False, False, False, False],
        inplace=True,
    )
    return report.reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--min-sessions", type=int, default=20)
    parser.add_argument("--min-trade-sessions", type=int, default=15)
    parser.add_argument("--min-candidates", type=int, default=100)
    parser.add_argument("--min-coverage-pct", type=float, default=95.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    parser.add_argument("--capital-amount", type=float, default=400000.0)
    parser.add_argument("--daily-goal-amount", type=float, default=10000.0)
    parser.add_argument("--max-drawdown-pct", type=float, default=5.0)
    parser.add_argument("--max-single-day-loss-pct", type=float, default=2.5)
    parser.add_argument("--minimum-goal-hit-pct", type=float, default=50.0)
    args = parser.parse_args()

    outcomes = load_outcomes(args.inputs)
    if outcomes.empty:
        raise RuntimeError("no live evidence outcomes found")
    report = assess(
        outcomes,
        min_sessions=args.min_sessions,
        min_trade_sessions=args.min_trade_sessions,
        min_candidates=args.min_candidates,
        min_coverage_pct=args.min_coverage_pct,
        stress_cost_pct=args.stress_cost_pct,
        capital_amount=args.capital_amount,
        daily_goal_amount=args.daily_goal_amount,
        max_drawdown_pct=args.max_drawdown_pct,
        max_single_day_loss_pct=args.max_single_day_loss_pct,
        minimum_goal_hit_pct=args.minimum_goal_hit_pct,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output, index=False)
    print(
        f"rows={len(outcomes)} sessions={outcomes['Date'].nunique()} "
        f"buckets={outcomes['FeatureBucket'].nunique()} "
        f"shadow_candidates={int(report['ShadowCandidate'].sum())} "
        f"goal_evidence_passed={int(report['GoalEvidencePassed'].sum())}"
    )
    print(report.head(30).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

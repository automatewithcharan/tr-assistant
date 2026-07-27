#!/usr/bin/env python3
"""Capital growth and daily-goal feasibility from validated forward evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd


DEFAULT_STRATEGIES = {
    "exact_rank": "results/Data/full_universe_rank_exact_strategy/status.json",
    "exit_contract": "results/Data/full_universe_rank_contract_strategy/status.json",
    "entry_timing": "results/Data/full_universe_rank_entry_strategy/status.json",
    "feature_gate": "results/Data/full_universe_rank_feature_strategy/status.json",
    "capital_allocation": "results/Data/full_universe_rank_capital_allocation/status.json",
    "integrated_policy": "results/Data/full_universe_rank_integrated_policies/status.json",
    "full_rank_model": "results/Data/full_universe_rank_strategy/status.json",
    "prior_session_ranker": "results/Data/prior_session_ranker_paper/status.json",
}
SCENARIO_DAILY_RETURNS = (0.0, 0.25, 0.50, 1.0, 2.0)


def _number(payload: Dict, key: str, default: float = 0.0) -> float:
    try:
        return float(payload.get(key, default) or default)
    except Exception:
        return float(default)


def load_validated_strategy(
    paths: Dict[str, str], minimum_forward_sessions: int = 20
) -> Optional[Dict]:
    """Choose the strongest accepted strategy; ignore all paper-only diagnostics."""
    accepted = []
    for name, raw_path in paths.items():
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        forward = payload.get("forward", {}) or payload.get("forward_stress", {}) or {}
        sessions = int(_number(forward, "sessions"))
        avg_return = _number(forward, "avg_daily_return_pct")
        lower = forward.get("lower_95_pct")
        try:
            lower_value = float(lower) if lower is not None else None
        except Exception:
            lower_value = None
        if not (
            bool(payload.get("accepted", False))
            and sessions >= int(minimum_forward_sessions)
            and avg_return > 0.0
            and lower_value is not None
            and lower_value > 0.0
            and not bool(payload.get("real_orders_allowed", False))
        ):
            continue
        accepted.append(
            {
                "name": name,
                "path": str(path),
                "status": str(payload.get("status", "")),
                "forward_sessions": sessions,
                "forward_trades": int(_number(forward, "trades")),
                "avg_daily_return_pct": avg_return,
                "lower_95_pct": lower_value,
                "avg_daily_pnl": _number(forward, "avg_daily_pnl"),
            }
        )
    return max(accepted, key=lambda row: row["avg_daily_return_pct"], default=None)


def project_months(
    *,
    initial_capital: float,
    monthly_contribution: float,
    daily_goal: float,
    daily_return_pct: float,
    months: int,
    trading_days_per_month: int,
    scenario: str,
) -> pd.DataFrame:
    rows = []
    capital = float(initial_capital)
    for month in range(1, int(months) + 1):
        start = capital
        required_return = float(daily_goal) * 100.0 / start if start > 0 else 0.0
        projected_daily_pnl = start * float(daily_return_pct) / 100.0
        growth_factor = (1.0 + float(daily_return_pct) / 100.0) ** int(
            trading_days_per_month
        )
        trading_profit = start * (growth_factor - 1.0)
        before_contribution = start + trading_profit
        capital = before_contribution + float(monthly_contribution)
        rows.append(
            {
                "scenario": scenario,
                "month": month,
                "start_capital": round(start, 2),
                "daily_return_pct": round(float(daily_return_pct), 6),
                "required_return_for_goal_pct": round(required_return, 6),
                "projected_daily_pnl_at_month_start": round(projected_daily_pnl, 2),
                "trading_profit": round(trading_profit, 2),
                "monthly_contribution": round(float(monthly_contribution), 2),
                "end_capital": round(capital, 2),
                "daily_goal_met_at_month_start": bool(projected_daily_pnl >= float(daily_goal)),
            }
        )
    return pd.DataFrame(rows)


def required_capital_table(daily_goal: float, returns: Iterable[float]) -> pd.DataFrame:
    rows = []
    for daily_return in returns:
        required = (
            float(daily_goal) / (float(daily_return) / 100.0)
            if float(daily_return) > 0 else None
        )
        rows.append(
            {
                "daily_return_pct": float(daily_return),
                "capital_required_for_daily_goal": round(required, 2) if required else None,
            }
        )
    return pd.DataFrame(rows)


def run_projection(
    *,
    strategy_paths: Dict[str, str],
    output_dir: Path,
    initial_capital: float = 500_000.0,
    monthly_contribution: float = 200_000.0,
    daily_goal: float = 10_000.0,
    months: int = 12,
    trading_days_per_month: int = 20,
    minimum_forward_sessions: int = 20,
) -> Dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    validated = load_validated_strategy(
        strategy_paths, minimum_forward_sessions=minimum_forward_sessions
    )
    projections = []
    for daily_return in SCENARIO_DAILY_RETURNS:
        projections.append(
            project_months(
                initial_capital=initial_capital,
                monthly_contribution=monthly_contribution,
                daily_goal=daily_goal,
                daily_return_pct=daily_return,
                months=months,
                trading_days_per_month=trading_days_per_month,
                scenario=f"mathematical_{daily_return:.2f}pct",
            )
        )
    if validated:
        projections.append(
            project_months(
                initial_capital=initial_capital,
                monthly_contribution=monthly_contribution,
                daily_goal=daily_goal,
                daily_return_pct=validated["avg_daily_return_pct"],
                months=months,
                trading_days_per_month=trading_days_per_month,
                scenario=f"validated_{validated['name']}",
            )
        )
    projection = pd.concat(projections, ignore_index=True)
    projection.to_csv(output_dir / "monthly_scenarios.csv", index=False)
    required = required_capital_table(daily_goal, SCENARIO_DAILY_RETURNS)
    required.to_csv(output_dir / "required_capital.csv", index=False)
    contribution_only = projection[
        projection["scenario"].eq("mathematical_0.00pct")
    ].copy()
    first_contribution_month = contribution_only.loc[
        contribution_only["required_return_for_goal_pct"].le(1.0), "month"
    ]
    status = {
        "status": (
            "validated_forward_strategy_available"
            if validated else "awaiting_validated_forward_strategy"
        ),
        "initial_capital": float(initial_capital),
        "monthly_contribution": float(monthly_contribution),
        "daily_goal": float(daily_goal),
        "initial_required_daily_return_pct": round(
            float(daily_goal) * 100.0 / float(initial_capital), 6
        ),
        "months": int(months),
        "trading_days_per_month": int(trading_days_per_month),
        "minimum_forward_sessions": int(minimum_forward_sessions),
        "validated_strategy": validated or {},
        "first_month_required_return_at_or_below_1pct_without_trading_profit": (
            int(first_contribution_month.iloc[0])
            if not first_contribution_month.empty else None
        ),
        "mathematical_scenarios_are_predictions": False,
        "capital_projection_allowed": bool(validated),
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="results/Data/intraday_capital_growth")
    parser.add_argument("--initial-capital", type=float, default=500_000.0)
    parser.add_argument("--monthly-contribution", type=float, default=200_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--trading-days", type=int, default=20)
    parser.add_argument("--minimum-forward-sessions", type=int, default=20)
    args = parser.parse_args()
    status = run_projection(
        strategy_paths=DEFAULT_STRATEGIES,
        output_dir=Path(args.output_dir),
        initial_capital=args.initial_capital,
        monthly_contribution=args.monthly_contribution,
        daily_goal=args.daily_goal,
        months=args.months,
        trading_days_per_month=args.trading_days,
        minimum_forward_sessions=args.minimum_forward_sessions,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

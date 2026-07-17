#!/usr/bin/env python3
"""Track one frozen live microstructure selector on untouched sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assess_live_evidence_readiness import first_batch_rows, load_outcomes  # noqa: E402

SELECTOR = "missed_stable_tight_sector"
CONTRACT = "forward_horizon"
RETURN_COLUMN = "ForwardReturnPct"
FROZEN_THROUGH = "2026-07-16"
FORWARD_START = "2026-07-17"


def daily_returns(outcomes: pd.DataFrame, dates: list[str], extra_cost_pct: float = 0.0) -> tuple[pd.DataFrame, int]:
    bucket = outcomes[outcomes["FeatureBucket"].eq(SELECTOR)].copy()
    selected = first_batch_rows(bucket)
    selected[RETURN_COLUMN] = pd.to_numeric(selected.get(RETURN_COLUMN), errors="coerce")
    selected = selected[selected["OutcomeAvailable"] & selected[RETURN_COLUMN].notna()].copy()
    observed = selected.groupby("Date")[RETURN_COLUMN].mean() if not selected.empty else pd.Series(dtype=float)
    if extra_cost_pct and not observed.empty:
        observed = observed - float(extra_cost_pct)
    rows = []
    for date in dates:
        rows.append(
            {
                "date": date,
                "trades": int((selected["Date"] == date).sum()),
                "daily_return_pct": float(observed.get(date, 0.0)),
            }
        )
    return pd.DataFrame(rows, columns=["date", "trades", "daily_return_pct"]), int(len(selected))


def metrics(daily: pd.DataFrame, capital: float, goal: float) -> dict:
    values = pd.to_numeric(daily["daily_return_pct"], errors="coerce").fillna(0.0)
    count = int(len(values))
    mean = float(values.mean()) if count else 0.0
    lower = float("-inf")
    if count >= 2:
        lower = mean - float(student_t.ppf(0.975, count - 1)) * float(values.std(ddof=1) / np.sqrt(count))
    required = float(goal) / float(capital) * 100.0 if capital > 0 else float("inf")
    blocks = [part for part in np.array_split(values.to_numpy(dtype=float), 4) if len(part)]
    return {
        "sessions": count,
        "trade_sessions": int((pd.to_numeric(daily["trades"], errors="coerce").fillna(0) > 0).sum()),
        "candidates": int(pd.to_numeric(daily["trades"], errors="coerce").fillna(0).sum()),
        "avg_daily_return_pct": round(mean, 6),
        "avg_daily_pnl": round(float(capital) * mean / 100.0, 2),
        "lower_95_pct": round(lower, 6) if np.isfinite(lower) else None,
        "positive_session_pct": round(float((values > 0).mean() * 100.0), 2) if count else 0.0,
        "goal_hit_pct": round(float((values >= required).mean() * 100.0), 2) if count else 0.0,
        "worst_session_pct": round(float(values.min()), 6) if count else 0.0,
        "block_floor_pct": round(min(float(np.mean(block)) for block in blocks), 6) if blocks else 0.0,
    }


def accepted(forward: dict, stress: dict, min_sessions: int, min_trade_sessions: int, min_candidates: int, goal: float) -> bool:
    return bool(
        forward["sessions"] >= int(min_sessions)
        and forward["trade_sessions"] >= int(min_trade_sessions)
        and forward["candidates"] >= int(min_candidates)
        and float(forward["avg_daily_return_pct"]) > 0.0
        and float(stress["avg_daily_return_pct"]) > 0.0
        and forward["lower_95_pct"] is not None
        and stress["lower_95_pct"] is not None
        and float(forward["lower_95_pct"]) > 0.0
        and float(stress["lower_95_pct"]) > 0.0
        and float(forward["block_floor_pct"]) > 0.0
        and float(stress["block_floor_pct"]) > 0.0
        and float(stress["avg_daily_pnl"]) >= float(goal)
        and float(stress["goal_hit_pct"]) >= 50.0
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output-prefix", default="results/Data/live_feature_recorder/frozen_microstructure_selector")
    parser.add_argument("--capital", type=float, default=400000.0)
    parser.add_argument("--daily-goal", type=float, default=10000.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    parser.add_argument("--min-forward-sessions", type=int, default=20)
    parser.add_argument("--min-trade-sessions", type=int, default=15)
    parser.add_argument("--min-candidates", type=int, default=30)
    args = parser.parse_args()

    outcomes = load_outcomes(args.inputs)
    if outcomes.empty:
        raise RuntimeError("no live evidence outcomes found")
    dates = sorted(outcomes["Date"].unique().tolist())
    selection_dates = [date for date in dates if date <= FROZEN_THROUGH]
    forward_dates = [date for date in dates if date >= FORWARD_START]
    selection_daily, selection_candidates = daily_returns(outcomes, selection_dates)
    forward_daily, _ = daily_returns(outcomes, forward_dates)
    stress_daily, _ = daily_returns(outcomes, forward_dates, extra_cost_pct=args.stress_cost_pct)
    selection = metrics(selection_daily, args.capital, args.daily_goal)
    selection["candidates"] = selection_candidates
    forward = metrics(forward_daily, args.capital, args.daily_goal)
    stress = metrics(stress_daily, args.capital, args.daily_goal)
    is_accepted = accepted(
        forward,
        stress,
        args.min_forward_sessions,
        args.min_trade_sessions,
        args.min_candidates,
        args.daily_goal,
    )
    payload = {
        "evidence": "frozen_live_microstructure_selector",
        "selector": SELECTOR,
        "contract": CONTRACT,
        "frozen_through": FROZEN_THROUGH,
        "forward_start": FORWARD_START,
        "selection_uses_forward_period": False,
        "selection": selection,
        "forward": forward,
        "forward_stress": stress,
        "minimum_forward_sessions": args.min_forward_sessions,
        "minimum_trade_sessions": args.min_trade_sessions,
        "minimum_candidates": args.min_candidates,
        "accepted": is_accepted,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    daily_parts = [selection_daily.assign(period="selection")]
    if not forward_daily.empty:
        daily_parts.append(forward_daily.assign(period="forward"))
    if not stress_daily.empty:
        daily_parts.append(stress_daily.assign(period="forward_stress"))
    pd.concat(daily_parts, ignore_index=True).to_csv(
        prefix.with_name(prefix.name + "_daily.csv"), index=False
    )
    prefix.with_name(prefix.name + "_status.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

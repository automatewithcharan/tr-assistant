#!/usr/bin/env python3
"""Audit the existing leader pullback/reclaim lane on executable EOD outcomes."""

from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from scipy.stats import t as student_t


CONTRACTS = (
    "t0_5_s0_5",
    "t0_7_s0_5",
    "t1_0_s0_5",
    "t1_5_s1_0",
)
GOAL_CONTRACT = "t1_5_s1_0"
DEFAULT_PATTERNS = (
    "results/Data/live_feature_recorder/eod/20*/lane_decisions_rows.csv",
    "results/Data/live_feature_recorder/bootstrap/**/20*/lane_decisions_rows.csv",
)


def _truthy(values: pd.Series) -> pd.Series:
    return values.fillna("").astype(str).str.strip().str.lower().isin(
        {"1", "1.0", "true", "yes", "y", "on"}
    )


def load_reclaim_outcomes(patterns: Iterable[str]) -> tuple[pd.DataFrame, list[str]]:
    files = sorted(
        {
            path
            for pattern in patterns
            for path in glob.glob(str(pattern), recursive=True)
        }
    )
    frames = []
    calendar_dates = set()
    for file in files:
        try:
            frame = pd.read_csv(file, low_memory=False)
        except (OSError, pd.errors.EmptyDataError):
            continue
        if "Date" in frame.columns:
            calendar_dates.update(frame["Date"].dropna().astype(str).tolist())
        required = {
            "Date", "When", "Symbol", "Lane", "Stage", "Decision",
            "SignalStrengthPct", "EntryPrice", "OutcomeAvailable",
            *[f"{contract}_return_pct" for contract in CONTRACTS],
            *[f"{contract}_exit_time" for contract in CONTRACTS],
        }
        if not required.issubset(frame.columns):
            continue
        reclaim = frame[
            frame["Lane"].astype(str).eq("leader_ignition")
            & frame["Stage"].astype(str).eq("pullback_reclaim")
            & frame["Decision"].astype(str).str.lower().eq("pass")
            & _truthy(frame["OutcomeAvailable"])
        ].copy()
        if reclaim.empty:
            continue
        frames.append(reclaim)
    if not frames:
        return pd.DataFrame(), sorted(calendar_dates)
    data = pd.concat(frames, ignore_index=True)
    data["date"] = data["Date"].astype(str)
    data["symbol"] = data["Symbol"].astype(str).str.strip().str.upper()
    data["signal_time"] = pd.to_datetime(data["When"], errors="coerce")
    data["entry_time"] = data["signal_time"].dt.floor("min") + pd.Timedelta(minutes=1)
    data["rank_score"] = pd.to_numeric(data["SignalStrengthPct"], errors="coerce")
    data["entry_price"] = pd.to_numeric(data["EntryPrice"], errors="coerce")
    for contract in CONTRACTS:
        data[f"{contract}_return_pct"] = pd.to_numeric(
            data[f"{contract}_return_pct"], errors="coerce"
        )
        data[f"{contract}_exit_time"] = pd.to_datetime(
            data[f"{contract}_exit_time"], errors="coerce"
        )
    data = data.dropna(
        subset=["signal_time", "entry_time", "rank_score", "entry_price"]
    ).drop_duplicates(["date", "symbol", "signal_time"], keep="last")
    return data.sort_values(["date", "signal_time", "symbol"]), sorted(calendar_dates)


def replay_one_slot(
    candidates: pd.DataFrame,
    calendar_dates: Iterable[str],
    *,
    contract: str,
    capital: float = 500_000.0,
    max_trades_per_day: int = 10,
    extra_cost_pct: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    return_column = f"{contract}_return_pct"
    exit_column = f"{contract}_exit_time"
    trades = []
    daily_rows = []
    for date in sorted({str(value) for value in calendar_dates}):
        day = candidates[candidates["date"].eq(date)].copy()
        day = day.dropna(subset=[return_column, exit_column])
        day["signal_batch"] = day["signal_time"].dt.floor("min")
        day.sort_values(
            ["signal_batch", "rank_score", "symbol"],
            ascending=[True, False, True],
            inplace=True,
        )
        active_exit = None
        traded_symbols = set()
        day_pnl = 0.0
        day_trades = 0
        for batch, visible in day.groupby("signal_batch", sort=True):
            if active_exit is not None and pd.Timestamp(active_exit) > pd.Timestamp(batch):
                continue
            if day_trades >= int(max_trades_per_day):
                break
            visible = visible[~visible["symbol"].isin(traded_symbols)]
            if visible.empty:
                continue
            pick = visible.iloc[0]
            raw_return = float(pick[return_column])
            net_return = raw_return - float(extra_cost_pct)
            pnl = float(capital) * net_return / 100.0
            trades.append(
                {
                    "date": date,
                    "symbol": str(pick["symbol"]),
                    "contract": contract,
                    "signal_time": pick["signal_time"],
                    "entry_time": pick["entry_time"],
                    "exit_time": pick[exit_column],
                    "rank_score": round(float(pick["rank_score"]), 4),
                    "raw_return_pct": round(raw_return, 6),
                    "extra_cost_pct": round(float(extra_cost_pct), 6),
                    "net_return_pct": round(net_return, 6),
                    "pnl": round(pnl, 2),
                }
            )
            active_exit = pick[exit_column]
            traded_symbols.add(str(pick["symbol"]))
            day_trades += 1
            day_pnl += pnl
        daily_rows.append(
            {
                "date": date,
                "trades": day_trades,
                "pnl": round(day_pnl, 2),
                "daily_return_pct": round(day_pnl / float(capital) * 100.0, 6),
            }
        )
    return pd.DataFrame(daily_rows), pd.DataFrame(trades)


def metrics(daily: pd.DataFrame, daily_goal: float) -> dict:
    if daily is None or daily.empty:
        return {
            "sessions": 0, "trade_sessions": 0, "trades": 0,
            "avg_daily_return_pct": 0.0, "avg_daily_pnl": 0.0,
            "lower_95_pct": None, "positive_session_pct": 0.0,
            "goal_hit_pct": 0.0, "worst_session_pct": 0.0,
            "block_floor_pct": None,
        }
    returns = pd.to_numeric(daily["daily_return_pct"], errors="coerce").fillna(0.0)
    pnls = pd.to_numeric(daily["pnl"], errors="coerce").fillna(0.0)
    count = len(returns)
    lower = None
    if count >= 2:
        lower = float(returns.mean()) - float(student_t.ppf(0.975, count - 1)) * float(
            returns.std(ddof=1) / math.sqrt(count)
        )
    blocks = [part for part in np.array_split(returns.to_numpy(), 4) if len(part)]
    return {
        "sessions": int(count),
        "trade_sessions": int((daily["trades"] > 0).sum()),
        "trades": int(daily["trades"].sum()),
        "avg_daily_return_pct": round(float(returns.mean()), 6),
        "avg_daily_pnl": round(float(pnls.mean()), 2),
        "lower_95_pct": round(lower, 6) if lower is not None else None,
        "positive_session_pct": round(float((returns > 0).mean() * 100.0), 2),
        "goal_hit_pct": round(float((pnls >= float(daily_goal)).mean() * 100.0), 2),
        "worst_session_pct": round(float(returns.min()), 6),
        "block_floor_pct": round(
            min(float(np.mean(part)) for part in blocks), 6
        ) if blocks else None,
    }


def audit(
    candidates: pd.DataFrame,
    calendar_dates: list[str],
    *,
    output_dir: Path,
    capital: float,
    daily_goal: float,
    stress_cost_pct: float,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(output_dir / "candidates.csv", index=False)
    contract_rows = []
    goal_raw = goal_stress = None
    for contract in CONTRACTS:
        daily, trades = replay_one_slot(
            candidates, calendar_dates, contract=contract, capital=capital
        )
        stress_daily, stress_trades = replay_one_slot(
            candidates,
            calendar_dates,
            contract=contract,
            capital=capital,
            extra_cost_pct=stress_cost_pct,
        )
        raw_metrics = metrics(daily, daily_goal)
        stress_metrics = metrics(stress_daily, daily_goal)
        contract_rows.append(
            {"contract": contract, **raw_metrics, **{
                f"stress_{key}": value for key, value in stress_metrics.items()
            }}
        )
        if contract == GOAL_CONTRACT:
            goal_raw, goal_stress = raw_metrics, stress_metrics
            daily.to_csv(output_dir / "goal_daily.csv", index=False)
            trades.to_csv(output_dir / "goal_trades.csv", index=False)
            stress_daily.to_csv(output_dir / "goal_stress_daily.csv", index=False)
            stress_trades.to_csv(output_dir / "goal_stress_trades.csv", index=False)
    pd.DataFrame(contract_rows).to_csv(output_dir / "contracts.csv", index=False)
    goal_raw = goal_raw or metrics(pd.DataFrame(), daily_goal)
    goal_stress = goal_stress or metrics(pd.DataFrame(), daily_goal)
    accepted = bool(
        goal_stress["sessions"] >= 20
        and goal_stress["trade_sessions"] >= 15
        and goal_stress["trades"] >= 30
        and goal_stress["avg_daily_pnl"] >= float(daily_goal)
        and goal_stress["lower_95_pct"] is not None
        and goal_stress["lower_95_pct"] > 0.0
        and goal_stress["block_floor_pct"] is not None
        and goal_stress["block_floor_pct"] > 0.0
        and goal_stress["goal_hit_pct"] >= 50.0
        and goal_stress["worst_session_pct"] >= -2.5
    )
    status = {
        "evidence": "leader_pullback_reclaim_exact_capital_audit",
        "status": "accepted" if accepted else "rejected_current_rule",
        "strategy": "existing_leader_ignition_pullback_then_reclaim",
        "ranking": "highest_existing_signal_score_per_visible_minute",
        "entry": "next_observed_minute_open_with_existing_slippage_and_fees",
        "capital_reuse": "single_unleveraged_slot_released_at_recorded_exit",
        "goal_contract": GOAL_CONTRACT,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "stress_extra_cost_pct": float(stress_cost_pct),
        "candidate_rows": int(len(candidates)),
        "calendar_sessions": len(calendar_dates),
        "goal": goal_raw,
        "goal_stress": goal_stress,
        "accepted": accepted,
        "paper_promotion_allowed": accepted,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-glob", action="append", default=[])
    parser.add_argument(
        "--output-dir", default="results/Data/leader_pullback_reclaim_audit"
    )
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    args = parser.parse_args()
    patterns = args.input_glob or list(DEFAULT_PATTERNS)
    candidates, dates = load_reclaim_outcomes(patterns)
    status = audit(
        candidates,
        dates,
        output_dir=Path(args.output_dir),
        capital=args.capital,
        daily_goal=args.daily_goal,
        stress_cost_pct=args.stress_cost_pct,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

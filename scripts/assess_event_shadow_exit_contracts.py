#!/usr/bin/env python3
"""Evaluate a frozen exit-contract registry on repaired live event tapes."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import t as student_t


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.IntradayEventEngine import score_intraday_event  # noqa: E402
from pkscreener.classes.IntradayEventShadowOutcomes import _simulate  # noqa: E402


FROZEN_TARGETS = (0.8, 1.0, 1.5, 2.5, 3.0, 5.0)
FROZEN_STOPS = (0.5, 1.0, 1.5, 2.5)
FROZEN_POSITION_COUNTS = (1, 2)
FROZEN_THROUGH = "2026-07-16"


def registry() -> list[dict]:
    return [
        {
            "config": f"t{target:g}_s{stop:g}_n{positions}",
            "target_pct": float(target),
            "stop_pct": float(stop),
            "positions": int(positions),
        }
        for target, stop, positions in itertools.product(
            FROZEN_TARGETS, FROZEN_STOPS, FROZEN_POSITION_COUNTS
        )
    ]


def _complete(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def load_session(root: Path, date: str, required_last_bar: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    bars_path = root / f"bars_{date}.csv"
    events_path = root / f"events_{date}.csv"
    if not bars_path.exists() or not events_path.exists():
        return pd.DataFrame(), pd.DataFrame()
    bars = pd.read_csv(bars_path, parse_dates=["datetime"])
    events = pd.read_csv(events_path, parse_dates=["signal_time"])
    if bars.empty or events.empty:
        return pd.DataFrame(), pd.DataFrame()
    bars["symbol"] = bars["symbol"].astype(str).str.strip().str.upper()
    events["symbol"] = events["symbol"].astype(str).str.strip().str.upper()
    required = pd.Timestamp(f"{date} {required_last_bar}")
    complete_symbols = set(
        bars.groupby("symbol")["datetime"].max().loc[lambda values: values >= required].index
    )
    events = events[events["symbol"].isin(complete_symbols)].copy()
    if events.empty:
        return bars, events
    if "event_score" not in events.columns:
        events["event_score"] = np.nan
    events["event_score"] = pd.to_numeric(events["event_score"], errors="coerce")
    missing = events["event_score"].isna()
    if missing.any():
        events.loc[missing, "event_score"] = [
            score_intraday_event(str(row.get("event_type", "")), row)
            for row in events.loc[missing].to_dict(orient="records")
        ]
    earliest = events["signal_time"].min()
    first_batch = events[events["signal_time"].eq(earliest)].copy()
    first_batch.sort_values(
        ["event_score", "symbol", "event_type"],
        ascending=[False, True, True],
        inplace=True,
    )
    first_batch = first_batch.drop_duplicates("symbol", keep="first")
    return bars.sort_values(["symbol", "datetime"]), first_batch


def evaluate_registry(
    root: Path,
    dates: list[str],
    cost_pct: float,
    extra_stress_pct: float,
    required_last_bar: str = "15:20:00",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    sessions = {}
    for date in dates:
        bars, first_batch = load_session(root, date, required_last_bar)
        sessions[date] = (bars, first_batch)

    trade_rows = []
    daily_rows = []
    for contract in registry():
        for date in dates:
            bars, first_batch = sessions[date]
            selected = first_batch.head(contract["positions"])
            returns = []
            for event in selected.to_dict(orient="records"):
                symbol = str(event["symbol"])
                signal_time = pd.Timestamp(event["signal_time"])
                future = bars[(bars["symbol"] == symbol) & (bars["datetime"] > signal_time)].copy()
                if future.empty:
                    continue
                entry_price = float(future.iloc[0]["open"])
                if entry_price <= 0:
                    continue
                gross, reason, hit = _simulate(
                    future,
                    entry_price,
                    contract["target_pct"],
                    contract["stop_pct"],
                    side="long",
                )
                net = float(gross) - float(cost_pct) - float(extra_stress_pct)
                returns.append(net)
                trade_rows.append(
                    {
                        **contract,
                        "date": date,
                        "evidence_split": "exploratory" if date <= FROZEN_THROUGH else "forward",
                        "symbol": symbol,
                        "event_type": str(event.get("event_type", "")),
                        "signal_time": signal_time.isoformat(),
                        "entry_price": round(entry_price, 4),
                        "event_score": round(float(event.get("event_score", 0.0)), 8),
                        "gross_return_pct": round(float(gross), 6),
                        "stressed_return_pct": round(net, 6),
                        "exit_reason": reason,
                        "target_before_stop": bool(hit),
                    }
                )
            daily_return = float(np.mean(returns)) if returns else 0.0
            daily_rows.append(
                {
                    **contract,
                    "date": date,
                    "evidence_split": "exploratory" if date <= FROZEN_THROUGH else "forward",
                    "trades": int(len(returns)),
                    "daily_return_pct": round(daily_return, 6),
                }
            )
    return pd.DataFrame(trade_rows), pd.DataFrame(daily_rows)


def _metrics(group: pd.DataFrame, capital: float, daily_goal: float) -> dict:
    returns = pd.to_numeric(group["daily_return_pct"], errors="coerce").fillna(0.0)
    count = int(len(returns))
    mean = float(returns.mean()) if count else 0.0
    if count >= 2:
        sem = float(returns.std(ddof=1) / np.sqrt(count))
        lower = mean - float(student_t.ppf(0.975, count - 1)) * sem
    else:
        lower = float("-inf")
    return {
        "sessions": count,
        "traded_sessions": int((group["trades"] > 0).sum()),
        "avg_daily_return_pct": round(mean, 6),
        "avg_daily_pnl": round(float(capital) * mean / 100.0, 2),
        "total_pnl": round(float(capital) * float(returns.sum()) / 100.0, 2),
        "positive_session_pct": round(float((returns > 0).mean() * 100.0), 4) if count else 0.0,
        "goal_hit_pct": round(float((returns * float(capital) / 100.0 >= float(daily_goal)).mean() * 100.0), 4) if count else 0.0,
        "worst_session_pct": round(float(returns.min()), 6) if count else 0.0,
        "lower_95_pct": round(lower, 6) if np.isfinite(lower) else None,
    }


def summarize(daily: pd.DataFrame, capital: float, daily_goal: float, min_forward_sessions: int) -> pd.DataFrame:
    rows = []
    for config, group in daily.groupby("config", sort=True):
        first = group.iloc[0]
        exploratory = _metrics(group[group["evidence_split"] == "exploratory"], capital, daily_goal)
        forward = _metrics(group[group["evidence_split"] == "forward"], capital, daily_goal)
        sample_gate = forward["sessions"] >= int(min_forward_sessions)
        confidence_gate = forward["lower_95_pct"] is not None and forward["lower_95_pct"] > 0.0
        expectancy_gate = forward["avg_daily_return_pct"] > 0.0
        rows.append(
            {
                "config": config,
                "target_pct": float(first["target_pct"]),
                "stop_pct": float(first["stop_pct"]),
                "positions": int(first["positions"]),
                **{f"exploratory_{key}": value for key, value in exploratory.items()},
                **{f"forward_{key}": value for key, value in forward.items()},
                "sample_gate": bool(sample_gate),
                "confidence_gate": bool(confidence_gate),
                "expectancy_gate": bool(expectancy_gate),
                "paper_candidate": bool(sample_gate and confidence_gate and expectancy_gate),
                "real_orders_allowed": False,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="results/Data/intraday_event_shadow_rebuilt/intraday_event_shadow")
    parser.add_argument("--output-dir", default="results/Data/intraday_event_shadow_rebuilt/exit_contracts")
    parser.add_argument("--cost-pct", type=float, default=0.15)
    parser.add_argument("--extra-stress-pct", type=float, default=0.10)
    parser.add_argument("--capital", type=float, default=400000.0)
    parser.add_argument("--daily-goal", type=float, default=10000.0)
    parser.add_argument("--min-forward-sessions", type=int, default=20)
    args = parser.parse_args()

    root = Path(args.root)
    dates = sorted(path.stem.replace("bars_", "") for path in root.glob("bars_*.csv"))
    if not dates:
        raise ValueError(f"no repaired event-shadow bars found under {root}")
    trades, daily = evaluate_registry(root, dates, args.cost_pct, args.extra_stress_pct)
    summary = summarize(daily, args.capital, args.daily_goal, args.min_forward_sessions)
    summary.sort_values(
        ["paper_candidate", "forward_lower_95_pct", "forward_avg_daily_pnl", "config"],
        ascending=[False, False, False, True],
        inplace=True,
        na_position="last",
    )
    status = {
        "evidence": "frozen_event_shadow_exit_contract_registry",
        "frozen_through": FROZEN_THROUGH,
        "targets": list(FROZEN_TARGETS),
        "stops": list(FROZEN_STOPS),
        "position_counts": list(FROZEN_POSITION_COUNTS),
        "config_count": int(len(summary)),
        "sessions": int(len(dates)),
        "forward_sessions": int(sum(date > FROZEN_THROUGH for date in dates)),
        "cost_pct": float(args.cost_pct),
        "extra_stress_pct": float(args.extra_stress_pct),
        "paper_candidates": int(summary["paper_candidate"].sum()) if not summary.empty else 0,
        "real_orders_allowed": False,
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    trades.to_csv(output / "trades.csv", index=False)
    daily.to_csv(output / "daily.csv", index=False)
    summary.to_csv(output / "readiness.csv", index=False)
    (output / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))
    print(summary.head(12).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

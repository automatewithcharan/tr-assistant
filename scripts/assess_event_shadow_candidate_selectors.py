#!/usr/bin/env python3
"""Forward-track frozen same-timestamp candidate selectors for early leaders."""

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

from pkscreener.classes.IntradayEventShadowOutcomes import _simulate  # noqa: E402
from scripts.assess_event_shadow_exit_contracts import FROZEN_THROUGH, load_session  # noqa: E402


SELECTORS = (
    "event_score",
    "move_leader",
    "volume_leader",
    "liquid_leader",
    "balanced_leader",
    "event_liquidity",
)
TARGETS = (1.5, 3.0)
STOP_PCT = 1.0


def selector_registry() -> list[dict]:
    return [
        {"config": f"{selector}_t{target:g}_s{STOP_PCT:g}", "selector": selector, "target_pct": target, "stop_pct": STOP_PCT}
        for selector, target in itertools.product(SELECTORS, TARGETS)
    ]


def score_batch(batch: pd.DataFrame, selector: str) -> pd.Series:
    def col(name: str) -> pd.Series:
        return pd.to_numeric(batch.get(name, 0.0), errors="coerce").fillna(0.0)

    if selector == "event_score":
        return col("event_score")
    if selector == "move_leader":
        return col("cs_move_rank_pct")
    if selector == "volume_leader":
        return col("cs_vol_ratio_rank_pct")
    if selector == "liquid_leader":
        return col("cs_signal_value_rank_pct")
    if selector == "balanced_leader":
        return (
            col("cs_move_rank_pct")
            + col("cs_vol_ratio_rank_pct")
            + col("cs_relative_strength_rank_pct")
            + col("cs_signal_value_rank_pct")
            + (100.0 - col("cs_pullback_rank_pct"))
        ) / 5.0
    if selector == "event_liquidity":
        event_rank = col("event_score").rank(method="average", pct=True) * 100.0
        return (
            event_rank
            + col("cs_move_rank_pct")
            + col("cs_vol_ratio_rank_pct")
            + col("cs_signal_value_rank_pct")
        ) / 4.0
    raise ValueError(f"unknown selector: {selector}")


def pick_candidate(batch: pd.DataFrame, selector: str) -> pd.Series | None:
    if batch is None or batch.empty:
        return None
    scored = batch.copy()
    scored["selector_score"] = score_batch(scored, selector)
    scored.sort_values(
        ["selector_score", "event_score", "symbol", "event_type"],
        ascending=[False, False, True, True],
        inplace=True,
    )
    scored = scored.drop_duplicates("symbol", keep="first")
    return scored.iloc[0] if not scored.empty else None


def evaluate(root: Path, dates: list[str], cost_pct: float, stress_pct: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    sessions = {date: load_session(root, date, "15:20:00") for date in dates}
    trades = []
    daily = []
    for config in selector_registry():
        for date in dates:
            bars, batch = sessions[date]
            pick = pick_candidate(batch, config["selector"])
            net = 0.0
            traded = False
            if pick is not None:
                symbol = str(pick["symbol"])
                signal_time = pd.Timestamp(pick["signal_time"])
                future = bars[(bars["symbol"] == symbol) & (bars["datetime"] > signal_time)].copy()
                if not future.empty:
                    entry = float(future.iloc[0]["open"])
                    gross, reason, hit = _simulate(
                        future, entry, config["target_pct"], config["stop_pct"], side="long"
                    )
                    net = float(gross) - float(cost_pct) - float(stress_pct)
                    traded = True
                    trades.append(
                        {
                            **config,
                            "date": date,
                            "evidence_split": "exploratory" if date <= FROZEN_THROUGH else "forward",
                            "symbol": symbol,
                            "event_type": str(pick["event_type"]),
                            "signal_time": signal_time.isoformat(),
                            "selector_score": round(float(pick["selector_score"]), 8),
                            "entry_price": round(entry, 4),
                            "gross_return_pct": round(float(gross), 6),
                            "stressed_return_pct": round(net, 6),
                            "exit_reason": reason,
                            "target_before_stop": bool(hit),
                        }
                    )
            daily.append(
                {
                    **config,
                    "date": date,
                    "evidence_split": "exploratory" if date <= FROZEN_THROUGH else "forward",
                    "trades": int(traded),
                    "daily_return_pct": round(net, 6),
                }
            )
    return pd.DataFrame(trades), pd.DataFrame(daily)


def _metrics(frame: pd.DataFrame, capital: float, goal: float) -> dict:
    returns = pd.to_numeric(frame["daily_return_pct"], errors="coerce").fillna(0.0)
    count = len(returns)
    mean = float(returns.mean()) if count else 0.0
    if count >= 2:
        lower = mean - float(student_t.ppf(0.975, count - 1)) * float(returns.std(ddof=1) / np.sqrt(count))
    else:
        lower = float("-inf")
    return {
        "sessions": int(count),
        "avg_daily_return_pct": round(mean, 6),
        "avg_daily_pnl": round(float(capital) * mean / 100.0, 2),
        "positive_session_pct": round(float((returns > 0).mean() * 100.0), 4) if count else 0.0,
        "goal_hit_pct": round(float((returns * capital / 100.0 >= goal).mean() * 100.0), 4) if count else 0.0,
        "lower_95_pct": round(lower, 6) if np.isfinite(lower) else None,
    }


def summarize(daily: pd.DataFrame, capital: float, goal: float, min_forward_sessions: int) -> pd.DataFrame:
    rows = []
    for config, group in daily.groupby("config", sort=True):
        first = group.iloc[0]
        exploratory = _metrics(group[group["evidence_split"] == "exploratory"], capital, goal)
        forward = _metrics(group[group["evidence_split"] == "forward"], capital, goal)
        sample = forward["sessions"] >= min_forward_sessions
        confidence = forward["lower_95_pct"] is not None and forward["lower_95_pct"] > 0.0
        rows.append(
            {
                "config": config,
                "selector": first["selector"],
                "target_pct": float(first["target_pct"]),
                "stop_pct": float(first["stop_pct"]),
                **{f"exploratory_{key}": value for key, value in exploratory.items()},
                **{f"forward_{key}": value for key, value in forward.items()},
                "sample_gate": bool(sample),
                "confidence_gate": bool(confidence),
                "paper_candidate": bool(sample and confidence and forward["avg_daily_return_pct"] > 0.0),
                "real_orders_allowed": False,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="results/Data/intraday_event_shadow_rebuilt/intraday_event_shadow")
    parser.add_argument("--output-dir", default="results/Data/intraday_event_shadow_rebuilt/candidate_selectors")
    parser.add_argument("--cost-pct", type=float, default=0.15)
    parser.add_argument("--extra-stress-pct", type=float, default=0.10)
    parser.add_argument("--capital", type=float, default=400000.0)
    parser.add_argument("--daily-goal", type=float, default=10000.0)
    parser.add_argument("--min-forward-sessions", type=int, default=20)
    args = parser.parse_args()

    root = Path(args.root)
    dates = sorted(path.stem.replace("bars_", "") for path in root.glob("bars_*.csv"))
    if not dates:
        raise ValueError(f"no repaired sessions found under {root}")
    trades, daily = evaluate(root, dates, args.cost_pct, args.extra_stress_pct)
    readiness = summarize(daily, args.capital, args.daily_goal, args.min_forward_sessions)
    readiness.sort_values(
        ["paper_candidate", "forward_lower_95_pct", "forward_avg_daily_pnl", "config"],
        ascending=[False, False, False, True],
        inplace=True,
        na_position="last",
    )
    status = {
        "evidence": "frozen_same_timestamp_candidate_selector_registry",
        "frozen_through": FROZEN_THROUGH,
        "selectors": list(SELECTORS),
        "targets": list(TARGETS),
        "stop_pct": STOP_PCT,
        "config_count": int(len(readiness)),
        "sessions": int(len(dates)),
        "forward_sessions": int(sum(date > FROZEN_THROUGH for date in dates)),
        "paper_candidates": int(readiness["paper_candidate"].sum()),
        "real_orders_allowed": False,
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    trades.to_csv(output / "trades.csv", index=False)
    daily.to_csv(output / "daily.csv", index=False)
    readiness.to_csv(output / "readiness.csv", index=False)
    (output / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))
    print(readiness.head(12).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

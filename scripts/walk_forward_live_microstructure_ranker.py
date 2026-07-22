#!/usr/bin/env python3
"""Frozen prior-session-only ranker for executable live leader snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.FrozenMicrostructureModel import (
    CONTRACT,
    MODEL_FEATURES,
    build_model,
    save_model_artifact,
    train_prior_session_model,
)


FROZEN_THROUGH = "2026-07-21"
FORWARD_START = "2026-07-22"
def _bool(values: pd.Series) -> pd.Series:
    return values.fillna("").astype(str).str.strip().str.lower().isin(
        {"1", "1.0", "true", "yes", "y", "on"}
    )


def load_outcomes(path: str, contract: str = CONTRACT) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "Date", "When", "ExitWhen", "Symbol", "Contract", "OutcomeAvailable",
        "NetReturnPct", "MoveBand",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
    frame = frame[frame["Contract"].astype(str).eq(str(contract))].copy()
    frame = frame[_bool(frame["OutcomeAvailable"])].copy()
    frame["date"] = frame["Date"].astype(str)
    frame["symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
    frame["signal_time"] = pd.to_datetime(frame["When"], errors="coerce")
    frame["exit_time"] = pd.to_datetime(frame["ExitWhen"], errors="coerce")
    frame["trade_return_pct"] = pd.to_numeric(
        frame["NetReturnPct"], errors="coerce"
    )
    frame["signal_minute"] = (
        frame["signal_time"].dt.hour * 60 + frame["signal_time"].dt.minute
    )
    traded_value = pd.to_numeric(
        frame.get("TradedValue", pd.Series(0.0, index=frame.index)), errors="coerce"
    ).fillna(0.0)
    frame["log_traded_value"] = np.log1p(traded_value.clip(lower=0.0))
    for column in ("OrderBookStable", "IsTopGainerNow", "MissedRunnerCandidate"):
        values = frame.get(column, pd.Series(False, index=frame.index))
        frame[column] = _bool(values).astype(float)
    bands = frame["MoveBand"].fillna("").astype(str)
    for band, column in {
        "0.5-1": "band_0_5_1",
        "1-2": "band_1_2",
        "2-3": "band_2_3",
        "3-5": "band_3_5",
    }.items():
        frame[column] = bands.eq(band).astype(float)
    for feature in MODEL_FEATURES:
        if feature not in frame.columns:
            frame[feature] = 0.0
        frame[feature] = pd.to_numeric(frame[feature], errors="coerce")
    frame = frame.dropna(
        subset=["signal_time", "exit_time", "trade_return_pct"]
    ).copy()
    frame = frame[frame["exit_time"] > frame["signal_time"]].copy()
    return frame.sort_values(["date", "signal_time", "symbol"]).reset_index(drop=True)


def score_walk_forward(
    frame: pd.DataFrame,
    min_train_days: int = 8,
    min_train_rows: int = 500,
    lookback_days: int = 40,
    min_samples_leaf: int = 30,
) -> pd.DataFrame:
    dates = sorted(frame["date"].unique().tolist())
    groups = {date: day.copy() for date, day in frame.groupby("date", sort=False)}
    scored = []
    for index, date in enumerate(dates):
        prior_dates = dates[max(0, index - int(lookback_days)):index]
        if len(prior_dates) < int(min_train_days):
            continue
        train = pd.concat([groups[value] for value in prior_dates], ignore_index=True)
        if len(train) < int(min_train_rows):
            continue
        day_counts = train.groupby("date")["symbol"].transform("count").clip(lower=1)
        weights = 1.0 / day_counts.to_numpy(dtype=float)
        model = build_model(min_samples_leaf=min_samples_leaf)
        model.fit(
            train[MODEL_FEATURES],
            train["trade_return_pct"],
            model__sample_weight=weights,
        )
        day = groups[date].copy()
        day["expected_return_pct"] = model.predict(day[MODEL_FEATURES])
        day["model_trained_through"] = prior_dates[-1]
        scored.append(day)
    return pd.concat(scored, ignore_index=True) if scored else pd.DataFrame()


def replay_exact_capital(
    scored: pd.DataFrame,
    dates: list[str],
    threshold: float = 0.20,
    slots: int = 1,
    max_trades_per_day: int = 10,
    extra_cost_pct: float = 0.0,
    capital: float = 500_000.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    slots = max(1, int(slots))
    ticket = float(capital) / slots
    trades = []
    daily_rows = []
    for date in dates:
        day = scored[
            scored["date"].eq(str(date))
            & (scored["expected_return_pct"] >= float(threshold))
        ].copy()
        day["signal_batch"] = day["signal_time"].dt.floor("min")
        day.sort_values(
            ["signal_batch", "expected_return_pct", "symbol"],
            ascending=[True, False, True],
            inplace=True,
        )
        active_exits: list[pd.Timestamp] = []
        traded_symbols: set[str] = set()
        day_trades = 0
        day_pnl = 0.0
        for batch, visible in day.groupby("signal_batch", sort=True):
            active_exits = [value for value in active_exits if value > pd.Timestamp(batch)]
            available = slots - len(active_exits)
            if available <= 0 or day_trades >= int(max_trades_per_day):
                continue
            visible = visible[~visible["symbol"].isin(traded_symbols)].head(
                min(available, int(max_trades_per_day) - day_trades)
            )
            for _, pick in visible.iterrows():
                net_return = float(pick["trade_return_pct"]) - float(extra_cost_pct)
                pnl = ticket * net_return / 100.0
                trades.append(
                    {
                        "date": str(date),
                        "symbol": str(pick["symbol"]),
                        "signal_time": pick["signal_time"],
                        "exit_time": pick["exit_time"],
                        "expected_return_pct": round(
                            float(pick["expected_return_pct"]), 6
                        ),
                        "net_return_pct": round(net_return, 6),
                        "pnl": round(pnl, 2),
                        "model_trained_through": str(pick["model_trained_through"]),
                    }
                )
                active_exits.append(pd.Timestamp(pick["exit_time"]))
                traded_symbols.add(str(pick["symbol"]))
                day_trades += 1
                day_pnl += pnl
        daily_rows.append(
            {
                "date": str(date),
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
    mean = float(returns.mean())
    lower = None
    if count >= 2:
        lower = mean - float(student_t.ppf(0.975, count - 1)) * float(
            returns.std(ddof=1) / np.sqrt(count)
        )
    blocks = [part for part in np.array_split(returns.to_numpy(dtype=float), 4) if len(part)]
    return {
        "sessions": int(count),
        "trade_sessions": int((daily["trades"] > 0).sum()),
        "trades": int(daily["trades"].sum()),
        "avg_daily_return_pct": round(mean, 6),
        "avg_daily_pnl": round(float(pnls.mean()), 2),
        "lower_95_pct": round(float(lower), 6) if lower is not None else None,
        "positive_session_pct": round(float((returns > 0).mean() * 100.0), 2),
        "goal_hit_pct": round(float((pnls >= float(daily_goal)).mean() * 100.0), 2),
        "worst_session_pct": round(float(returns.min()), 6),
        "block_floor_pct": round(
            min(float(np.mean(block)) for block in blocks), 6
        ) if blocks else None,
    }


def accepted(
    forward: dict,
    stress: dict,
    min_forward_sessions: int,
    min_trade_sessions: int,
    min_trades: int,
    daily_goal: float,
) -> bool:
    return bool(
        forward["sessions"] >= int(min_forward_sessions)
        and forward["trade_sessions"] >= int(min_trade_sessions)
        and forward["trades"] >= int(min_trades)
        and float(stress["avg_daily_pnl"]) >= float(daily_goal)
        and stress["lower_95_pct"] is not None
        and float(stress["lower_95_pct"]) > 0.0
        and stress["block_floor_pct"] is not None
        and float(stress["block_floor_pct"]) > 0.0
        and float(stress["goal_hit_pct"]) >= 50.0
        and float(stress["worst_session_pct"]) >= -2.5
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--contract", default=CONTRACT)
    parser.add_argument("--frozen-through", default=FROZEN_THROUGH)
    parser.add_argument("--forward-start", default=FORWARD_START)
    parser.add_argument("--threshold", type=float, default=0.20)
    parser.add_argument("--slots", type=int, default=1)
    parser.add_argument("--max-trades-per-day", type=int, default=10)
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--stress-cost-pct", type=float, default=0.10)
    parser.add_argument("--min-train-days", type=int, default=8)
    parser.add_argument("--min-train-rows", type=int, default=500)
    parser.add_argument("--min-forward-sessions", type=int, default=20)
    parser.add_argument("--min-trade-sessions", type=int, default=15)
    parser.add_argument("--min-trades", type=int, default=30)
    parser.add_argument(
        "--model-output",
        default="",
        help="Optional paper-only model artifact trained through the latest completed session",
    )
    args = parser.parse_args()

    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    try:
        outcomes = load_outcomes(args.input, contract=args.contract)
    except ValueError as exc:
        status = {
            "evidence": "frozen_live_microstructure_expected_return_ranker",
            "status": "needs_exit_timestamp_rebuild",
            "reason": str(exc),
            "paper_promotion_allowed": False,
            "real_orders_allowed": False,
        }
        prefix.with_name(prefix.name + "_status.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8"
        )
        print(json.dumps(status, indent=2))
        return 0

    scored = score_walk_forward(
        outcomes,
        min_train_days=args.min_train_days,
        min_train_rows=args.min_train_rows,
    )
    scored.to_csv(prefix.with_name(prefix.name + "_scored_candidates.csv"), index=False)
    scored_dates = sorted(scored["date"].unique().tolist()) if not scored.empty else []
    forward_dates = [date for date in scored_dates if date >= str(args.forward_start)]
    forward_scored = scored[scored["date"].isin(forward_dates)].copy()
    daily, trades = replay_exact_capital(
        forward_scored,
        forward_dates,
        threshold=args.threshold,
        slots=args.slots,
        max_trades_per_day=args.max_trades_per_day,
        capital=args.capital,
    )
    stress_daily, stress_trades = replay_exact_capital(
        forward_scored,
        forward_dates,
        threshold=args.threshold,
        slots=args.slots,
        max_trades_per_day=args.max_trades_per_day,
        extra_cost_pct=args.stress_cost_pct,
        capital=args.capital,
    )
    daily.to_csv(prefix.with_name(prefix.name + "_forward_daily.csv"), index=False)
    trades.to_csv(prefix.with_name(prefix.name + "_forward_trades.csv"), index=False)
    stress_daily.to_csv(
        prefix.with_name(prefix.name + "_forward_stress_daily.csv"), index=False
    )
    stress_trades.to_csv(
        prefix.with_name(prefix.name + "_forward_stress_trades.csv"), index=False
    )
    forward_metrics = metrics(daily, args.daily_goal)
    stress_metrics = metrics(stress_daily, args.daily_goal)
    passed = accepted(
        forward_metrics,
        stress_metrics,
        args.min_forward_sessions,
        args.min_trade_sessions,
        args.min_trades,
        args.daily_goal,
    )
    model_metadata = None
    if args.model_output and not outcomes.empty:
        trained_through = str(outcomes["date"].max())
        model, model_metadata = train_prior_session_model(
            outcomes,
            trained_through=trained_through,
            contract=args.contract,
            min_train_days=args.min_train_days,
            min_train_rows=args.min_train_rows,
        )
        model_metadata.update(
            {
                "threshold": float(args.threshold),
                "capital": float(args.capital),
                "slots": int(args.slots),
                "max_trades_per_day": int(args.max_trades_per_day),
                "target_pct": 1.5,
                "stop_pct": 1.0,
                "fee_pct": 0.10,
            }
        )
        save_model_artifact(args.model_output, model, model_metadata)
    status = {
        "evidence": "frozen_live_microstructure_expected_return_ranker",
        "status": "accepted" if passed else "collecting_forward_evidence",
        "contract": str(args.contract),
        "frozen_through": str(args.frozen_through),
        "forward_start": str(args.forward_start),
        "selection_uses_forward_period": False,
        "model_features": MODEL_FEATURES,
        "candidate_availability": "same_minute_batch_then_exact_exit_capital_reuse",
        "threshold": float(args.threshold),
        "slots": int(args.slots),
        "max_trades_per_day": int(args.max_trades_per_day),
        "capital": float(args.capital),
        "daily_goal": float(args.daily_goal),
        "stress_extra_cost_pct": float(args.stress_cost_pct),
        "source_sessions": int(outcomes["date"].nunique()),
        "source_candidates": int(len(outcomes)),
        "scored_sessions": len(scored_dates),
        "forward": forward_metrics,
        "forward_stress": stress_metrics,
        "minimum_forward_sessions": int(args.min_forward_sessions),
        "minimum_trade_sessions": int(args.min_trade_sessions),
        "minimum_trades": int(args.min_trades),
        "accepted": passed,
        "paper_promotion_allowed": passed,
        "real_orders_allowed": False,
        "live_model": model_metadata,
    }
    prefix.with_name(prefix.name + "_status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

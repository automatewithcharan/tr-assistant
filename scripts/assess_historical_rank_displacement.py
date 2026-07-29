#!/usr/bin/env python3
"""Reproduce the post-hoc top-book rank-displacement hypothesis audit.

This uses walk-forward scores and contemporaneous recorded bids, but the old
tape does not contain complete five-level depth.  It can justify a frozen
forward observer; it can never promote alerts or orders.
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.FrozenRankDisplacementPaperTrader import (  # noqa: E402
    FROZEN_POLICY,
    POLICY_SHA256,
    POLICY_VERSION,
)
from scripts.assess_leader_pullback_reclaim import metrics  # noqa: E402


SELECTION_CUTOFF = "2026-07-29"
STRESS_EXTRA_COST_PCT = 0.10


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {
            path
            for pattern in patterns
            for path in glob.glob(str(pattern), recursive=True)
        }
    )


def load_predictions(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path, low_memory=False)
    required = {
        "date", "symbol", "signal_time", "exit_time", "EntryAsk",
        "trade_return_pct", "expected_return_pct", "model_trained_through",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "rank-displacement prediction input missing: "
            + ", ".join(sorted(missing))
        )
    frame["date"] = frame["date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["signal_time"] = pd.to_datetime(
        frame["signal_time"], errors="coerce"
    )
    frame["exit_time"] = pd.to_datetime(
        frame["exit_time"], errors="coerce"
    )
    for column in (
        "EntryAsk", "trade_return_pct", "expected_return_pct",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame[
        frame["date"].le(SELECTION_CUTOFF)
        & frame["signal_time"].notna()
        & frame["exit_time"].notna()
        & frame["EntryAsk"].gt(0)
        & frame["trade_return_pct"].notna()
        & frame["expected_return_pct"].ge(
            float(FROZEN_POLICY["score_threshold"])
        )
        & frame["model_trained_through"].astype(str).lt(frame["date"])
    ].sort_values(
        ["date", "signal_time", "symbol"]
    ).reset_index(drop=True)


def load_top_book_tape(
    patterns: Iterable[str],
    symbols_by_date: dict[str, set[str]],
) -> dict[str, dict[str, pd.DataFrame]]:
    tapes: dict[str, dict[str, pd.DataFrame]] = {}
    required = {"When", "Symbol", "BestBid"}
    for path in _files(patterns):
        try:
            header = pd.read_csv(path, nrows=0).columns
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if not required.issubset(header):
            continue
        columns = [
            column
            for column in (
                "When", "Symbol", "BestBid", "BestBidQty",
                "RecorderGeneration",
            )
            if column in header
        ]
        try:
            frame = pd.read_csv(path, usecols=columns, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        frame["when"] = pd.to_datetime(frame["When"], errors="coerce")
        frame["date"] = frame["when"].dt.date.astype(str)
        frame["symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
        frame["bid"] = pd.to_numeric(frame["BestBid"], errors="coerce")
        for date, day in frame.groupby("date", sort=False):
            wanted = symbols_by_date.get(str(date), set())
            if not wanted:
                continue
            day = day[
                day["symbol"].isin(wanted)
                & day["when"].notna()
                & day["bid"].gt(0)
            ].copy()
            for symbol, symbol_rows in day.groupby("symbol", sort=False):
                tapes.setdefault(str(date), {})[str(symbol)] = (
                    symbol_rows.sort_values("when").reset_index(drop=True)
                )
    return tapes


def quote_at_or_before(
    tapes: dict[str, dict[str, pd.DataFrame]],
    date: str,
    symbol: str,
    at: pd.Timestamp,
) -> Optional[dict[str, Any]]:
    tape = tapes.get(str(date), {}).get(str(symbol))
    if tape is None or tape.empty:
        return None
    index = tape["when"].searchsorted(at, side="right") - 1
    if index < 0:
        return None
    row = tape.iloc[int(index)]
    age = (at - pd.Timestamp(row["when"])).total_seconds()
    if not 0.0 <= age <= float(
        FROZEN_POLICY["maximum_candidate_age_seconds"]
    ):
        return None
    return {
        "bid": float(row["bid"]),
        "age_seconds": float(age),
        "recorder_generation": int(
            pd.to_numeric(
                pd.Series([row.get("RecorderGeneration", 0)]),
                errors="coerce",
            ).fillna(0).iloc[0]
        ),
    }


def replay(
    predictions: pd.DataFrame,
    tapes: dict[str, dict[str, pd.DataFrame]],
    *,
    displacement: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    capital = float(FROZEN_POLICY["capital"])
    max_trades = int(FROZEN_POLICY["max_trades_per_day"])
    trade_rows = []
    daily_rows = []
    for date, day in predictions.groupby("date", sort=True):
        day = day.copy()
        day["signal_batch"] = day["signal_time"].dt.floor("min")
        day.sort_values(
            ["signal_batch", "expected_return_pct", "symbol"],
            ascending=[True, False, True],
            inplace=True,
        )
        active: Optional[dict[str, Any]] = None
        traded_symbols: set[str] = set()
        day_pnl = 0.0
        trades_started = 0
        switches = 0
        for batch, visible in day.groupby("signal_batch", sort=True):
            at = pd.Timestamp(batch)
            if active is not None and pd.Timestamp(active["exit_time"]) <= at:
                net_return = (
                    float(active["trade_return_pct"])
                    - STRESS_EXTRA_COST_PCT
                )
                pnl = capital * net_return / 100.0
                trade_rows.append(
                    {
                        **active,
                        "actual_exit_time": active["exit_time"],
                        "actual_exit_reason": "natural_contract_exit",
                        "net_return_pct": round(net_return, 6),
                        "pnl": round(pnl, 2),
                    }
                )
                day_pnl += pnl
                active = None
            visible = visible[
                ~visible["symbol"].isin(traded_symbols)
            ]
            if visible.empty or trades_started >= max_trades:
                continue
            challenger = visible.iloc[0]
            if active is None:
                active = challenger.to_dict()
                traded_symbols.add(str(challenger["symbol"]))
                trades_started += 1
                continue
            if not displacement:
                continue
            held = (
                at - pd.Timestamp(active["signal_time"])
            ).total_seconds() / 60.0
            if held < float(FROZEN_POLICY["minimum_hold_minutes"]):
                continue
            if float(challenger["expected_return_pct"]) < float(
                active["expected_return_pct"]
            ) + float(FROZEN_POLICY["score_advantage"]):
                continue
            quote = quote_at_or_before(
                tapes, str(date), str(active["symbol"]), at
            )
            if quote is None:
                continue
            gross_return = (
                float(quote["bid"]) / float(active["EntryAsk"]) - 1.0
            ) * 100.0
            if gross_return > float(
                FROZEN_POLICY["maximum_incumbent_gross_return_pct"]
            ):
                continue
            net_return = (
                gross_return
                - float(FROZEN_POLICY["fee_pct"])
                - STRESS_EXTRA_COST_PCT
            )
            pnl = capital * net_return / 100.0
            trade_rows.append(
                {
                    **active,
                    "actual_exit_time": at,
                    "actual_exit_reason": "rank_displaced",
                    "replacement_symbol": str(challenger["symbol"]),
                    "replacement_score": float(
                        challenger["expected_return_pct"]
                    ),
                    "exit_bid": float(quote["bid"]),
                    "exit_quote_age_seconds": float(
                        quote["age_seconds"]
                    ),
                    "exit_recorder_generation": int(
                        quote["recorder_generation"]
                    ),
                    "net_return_pct": round(net_return, 6),
                    "pnl": round(pnl, 2),
                }
            )
            day_pnl += pnl
            active = challenger.to_dict()
            traded_symbols.add(str(challenger["symbol"]))
            trades_started += 1
            switches += 1
        if active is not None:
            net_return = (
                float(active["trade_return_pct"]) - STRESS_EXTRA_COST_PCT
            )
            pnl = capital * net_return / 100.0
            trade_rows.append(
                {
                    **active,
                    "actual_exit_time": active["exit_time"],
                    "actual_exit_reason": "natural_contract_exit",
                    "net_return_pct": round(net_return, 6),
                    "pnl": round(pnl, 2),
                }
            )
            day_pnl += pnl
        daily_rows.append(
            {
                "date": str(date),
                "trades": trades_started,
                "displacements": switches,
                "pnl": round(day_pnl, 2),
                "daily_return_pct": round(
                    day_pnl / capital * 100.0, 6
                ),
            }
        )
    return pd.DataFrame(daily_rows), pd.DataFrame(trade_rows)


def assess(
    prediction_path: str | Path,
    raw_patterns: Iterable[str],
    output_dir: Path,
    *,
    daily_goal: float = 10_000.0,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    predictions = load_predictions(prediction_path)
    symbols_by_date = {
        str(date): set(day["symbol"].astype(str))
        for date, day in predictions.groupby("date", sort=False)
    }
    tapes = load_top_book_tape(raw_patterns, symbols_by_date)
    baseline_daily, baseline_trades = replay(
        predictions, tapes, displacement=False
    )
    displaced_daily, displaced_trades = replay(
        predictions, tapes, displacement=True
    )
    baseline = metrics(baseline_daily, daily_goal)
    displaced = metrics(displaced_daily, daily_goal)
    switches = int(
        displaced_trades.get(
            "actual_exit_reason", pd.Series(dtype=str)
        ).astype(str).eq("rank_displaced").sum()
    )
    incremental = round(
        float(displaced_daily["pnl"].sum())
        - float(baseline_daily["pnl"].sum()),
        2,
    )
    supports_forward = bool(
        switches >= 2
        and incremental > 0.0
        and displaced["avg_daily_pnl"] > baseline["avg_daily_pnl"]
    )
    baseline_daily.to_csv(output_dir / "baseline_daily.csv", index=False)
    baseline_trades.to_csv(output_dir / "baseline_trades.csv", index=False)
    displaced_daily.to_csv(
        output_dir / "displacement_daily.csv", index=False
    )
    displaced_trades.to_csv(
        output_dir / "displacement_trades.csv", index=False
    )
    status = {
        "evidence": "post_hoc_top_book_rank_displacement_diagnostic",
        "status": (
            "supports_frozen_forward_collection"
            if supports_forward
            else "historical_support_insufficient"
        ),
        "selection_cutoff": SELECTION_CUTOFF,
        "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256,
        "policy": FROZEN_POLICY,
        "stress_extra_cost_pct": STRESS_EXTRA_COST_PCT,
        "baseline": baseline,
        "displacement": displaced,
        "historical_displacements": switches,
        "incremental_historical_pnl": incremental,
        "supports_forward_collection": supports_forward,
        "uses_future_contract_outcomes_for_natural_exits": True,
        "historical_depth_complete": False,
        "post_hoc_hypothesis_only": True,
        "can_promote_strategy": False,
        "paper_promotion_allowed": False,
        "alerts_allowed": False,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2) + "\n", encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument(
        "--output-dir",
        default="results/Data/historical_rank_displacement",
    )
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    args = parser.parse_args()
    status = assess(
        args.predictions,
        args.raw_glob
        or ["results/Data/live_feature_recorder/live_features_20*.csv"],
        Path(args.output_dir),
        daily_goal=args.daily_goal,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

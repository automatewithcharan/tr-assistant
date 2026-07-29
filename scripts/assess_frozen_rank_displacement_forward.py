#!/usr/bin/env python3
"""Judge untouched five-level rank-displacement paper evidence."""

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

from pkscreener.classes.FrozenRankDisplacementPaperTrader import (  # noqa: E402
    FROZEN_POLICY,
    POLICY_SHA256,
    POLICY_VERSION,
)
from pkscreener.classes.OrderBookExecution import (  # noqa: E402
    EXECUTION_SCHEMA_VERSION,
    parse_depth_json,
)
from scripts.assess_leader_pullback_reclaim import metrics  # noqa: E402


FORWARD_START = "2026-07-30"
MINIMUM_SESSIONS = 20
MINIMUM_TRADES = 40
MINIMUM_HEARTBEATS = 1_000
MAXIMUM_HEARTBEAT_GAP_SECONDS = 45.0


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


def load_heartbeats(patterns: Iterable[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    detail = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {
            "date", "when", "tick_symbols", "paper_only",
            "alerts_allowed", "real_orders_allowed", "policy_version",
            "policy_sha256",
        }
        if not required.issubset(frame.columns):
            continue
        frame["date"] = frame["date"].astype(str)
        frame["when"] = pd.to_datetime(frame["when"], errors="coerce")
        frame = frame[
            frame["date"].ge(FORWARD_START) & frame["when"].notna()
        ].copy()
        if frame.empty:
            continue
        detail.append(frame)
        for date, day in frame.groupby("date", sort=True):
            day = day.sort_values("when").drop_duplicates("when")
            gaps = day["when"].diff().dt.total_seconds()
            first = day["when"].iloc[0]
            last = day["when"].iloc[-1]
            policy_valid = bool(
                day["policy_version"].astype(str).eq(POLICY_VERSION).all()
                and day["policy_sha256"].astype(str).eq(POLICY_SHA256).all()
            )
            safety_valid = bool(
                _truthy(day["paper_only"]).all()
                and not _truthy(day["alerts_allowed"]).any()
                and not _truthy(day["real_orders_allowed"]).any()
            )
            rows.append(
                {
                    "date": str(date),
                    "heartbeats": int(len(day)),
                    "first_heartbeat": first,
                    "last_heartbeat": last,
                    "max_gap_seconds": round(
                        float(gaps.max()) if gaps.notna().any() else 0.0, 3
                    ),
                    "minimum_tick_symbols": int(
                        pd.to_numeric(
                            day["tick_symbols"], errors="coerce"
                        ).fillna(0).min()
                    ),
                    "policy_valid": policy_valid,
                    "safety_valid": safety_valid,
                    "complete_session": bool(
                        len(day) >= MINIMUM_HEARTBEATS
                        and first.hour * 60 + first.minute <= 9 * 60 + 20
                        and last.hour * 60 + last.minute >= 15 * 60 + 10
                        and (
                            not gaps.notna().any()
                            or float(gaps.max())
                            <= MAXIMUM_HEARTBEAT_GAP_SECONDS
                        )
                        and policy_valid
                        and safety_valid
                    ),
                }
            )
    coverage = pd.DataFrame(rows)
    if not coverage.empty:
        coverage.sort_values(
            ["date", "complete_session", "heartbeats"],
            ascending=[True, False, False],
            inplace=True,
        )
        coverage = coverage.drop_duplicates("date", keep="first")
    return coverage, (
        pd.concat(detail, ignore_index=True) if detail else pd.DataFrame()
    )


def load_trades(
    patterns: Iterable[str],
    eligible_dates: set[str],
) -> tuple[pd.DataFrame, list[str]]:
    frames = []
    invalid_files: list[str] = []
    for path in _files(patterns):
        if path.endswith("trades_latest.csv"):
            continue
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            invalid_files.append(path)
            continue
        required = {
            "date", "symbol", "entry_time", "exit_time", "quantity",
            "deployed_capital", "net_pnl", "net_return_pct", "closed",
            "recorder_generation", "exchange", "model_trained_through",
            "model_source_sha256",
            "execution_schema_version", "entry_depth_json", "exit_depth_json",
            "entry_depth_fillable", "exit_depth_fillable", "paper_only",
            "alerts_allowed", "real_orders_allowed", "policy_version",
            "policy_sha256",
        }
        if not required.issubset(frame.columns):
            invalid_files.append(path)
            continue
        frame["date"] = frame["date"].astype(str)
        frame = frame[frame["date"].isin(eligible_dates)].copy()
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(), invalid_files
    trades = pd.concat(frames, ignore_index=True)
    trades["entry_time"] = pd.to_datetime(
        trades["entry_time"], errors="coerce"
    )
    trades["exit_time"] = pd.to_datetime(
        trades["exit_time"], errors="coerce"
    )
    for column in (
        "quantity", "deployed_capital", "net_pnl", "net_return_pct",
        "execution_schema_version", "recorder_generation",
    ):
        trades[column] = pd.to_numeric(trades[column], errors="coerce")
    trades["depth_valid"] = trades.apply(
        lambda row: bool(
            parse_depth_json(row["entry_depth_json"])
            and parse_depth_json(row["exit_depth_json"])
        ),
        axis=1,
    )
    trades["model_provenance_valid"] = trades.apply(
        lambda row: bool(
            str(row["model_trained_through"])
            and str(row["model_trained_through"]) < str(row["date"])
            and str(row["model_source_sha256"]).strip()
        ),
        axis=1,
    )
    trades["row_valid"] = (
        trades["entry_time"].notna()
        & trades["exit_time"].notna()
        & trades["exit_time"].gt(trades["entry_time"])
        & trades["quantity"].gt(0)
        & trades["deployed_capital"].gt(0)
        & trades["net_pnl"].notna()
        & trades["execution_schema_version"].eq(EXECUTION_SCHEMA_VERSION)
        & trades["recorder_generation"].eq(
            int(FROZEN_POLICY["required_recorder_generation"])
        )
        & trades["exchange"].astype(str).str.strip().str.upper().eq("NSE")
        & trades["model_provenance_valid"]
        & _truthy(trades["closed"])
        & _truthy(trades["entry_depth_fillable"])
        & _truthy(trades["exit_depth_fillable"])
        & trades["depth_valid"]
        & _truthy(trades["paper_only"])
        & ~_truthy(trades["alerts_allowed"])
        & ~_truthy(trades["real_orders_allowed"])
        & trades["policy_version"].astype(str).eq(POLICY_VERSION)
        & trades["policy_sha256"].astype(str).eq(POLICY_SHA256)
    )
    return trades.sort_values(
        ["date", "entry_time", "symbol"]
    ).reset_index(drop=True), invalid_files


def assess(
    heartbeat_patterns: Iterable[str],
    trade_patterns: Iterable[str],
    output_dir: Path,
    *,
    daily_goal: float = 10_000.0,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage, _ = load_heartbeats(heartbeat_patterns)
    eligible_dates = set(
        coverage.loc[
            coverage.get("complete_session", False).astype(bool), "date"
        ].astype(str)
    ) if not coverage.empty else set()
    trades, invalid_files = load_trades(trade_patterns, eligible_dates)

    trade_dates = set(trades["date"].astype(str)) if not trades.empty else set()
    invalid_trade_dates = set(
        trades.loc[~trades["row_valid"], "date"].astype(str)
    ) if not trades.empty else set()
    complete_dates = sorted(
        date for date in eligible_dates if date not in invalid_trade_dates
    )
    daily_rows = []
    for date in complete_dates:
        day = trades[trades["date"].eq(date)]
        daily_rows.append(
            {
                "date": date,
                "trades": int(len(day)),
                "displacements": int(
                    day.get("exit_reason", pd.Series(dtype=str))
                    .astype(str)
                    .eq("rank_displaced")
                    .sum()
                ),
                "pnl": round(
                    float(pd.to_numeric(day["net_pnl"], errors="coerce").sum()),
                    2,
                ),
                "daily_return_pct": round(
                    float(
                        pd.to_numeric(day["net_pnl"], errors="coerce").sum()
                    )
                    / float(FROZEN_POLICY["capital"])
                    * 100.0,
                    6,
                ),
            }
        )
    daily = pd.DataFrame(daily_rows)
    result = metrics(daily, daily_goal)
    enough = bool(
        result["sessions"] >= MINIMUM_SESSIONS
        and result["trades"] >= MINIMUM_TRADES
    )
    accepted = bool(
        enough
        and result["avg_daily_pnl"] >= float(daily_goal)
        and result["lower_95_pct"] is not None
        and float(result["lower_95_pct"]) > 0.0
        and result["block_floor_pct"] is not None
        and float(result["block_floor_pct"]) > 0.0
        and result["positive_session_pct"] >= 70.0
        and result["goal_hit_pct"] >= 50.0
        and result["worst_session_pct"] >= -2.5
        and not invalid_files
        and not invalid_trade_dates
    )
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    trades.to_csv(output_dir / "forward_trades.csv", index=False)
    daily.to_csv(output_dir / "forward_daily.csv", index=False)
    status = {
        "evidence": "untouched_five_level_rank_displacement_forward",
        "status": (
            "accepted"
            if accepted
            else (
                "rejected_after_minimum_forward_sample"
                if enough
                else "collecting_untouched_forward_sessions"
            )
        ),
        "forward_start": FORWARD_START,
        "minimum_sessions": MINIMUM_SESSIONS,
        "minimum_trades": MINIMUM_TRADES,
        "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256,
        "policy": FROZEN_POLICY,
        "capital": float(FROZEN_POLICY["capital"]),
        "daily_goal": float(daily_goal),
        "execution_schema_version": EXECUTION_SCHEMA_VERSION,
        "eligible_complete_sessions": len(eligible_dates),
        "complete_evaluable_sessions": len(complete_dates),
        "sessions_with_trades": len(trade_dates),
        "invalid_trade_dates": sorted(invalid_trade_dates),
        "invalid_files": invalid_files,
        "forward": result,
        "accepted": accepted,
        "paper_promotion_allowed": accepted,
        "paper_only": True,
        "alerts_allowed": False,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2) + "\n", encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat-glob", action="append", default=[])
    parser.add_argument("--trade-glob", action="append", default=[])
    parser.add_argument(
        "--output-dir",
        default="results/Data/frozen_rank_displacement_forward",
    )
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    args = parser.parse_args()
    base = Path("results/Data/frozen_stop_displacement_paper")
    status = assess(
        args.heartbeat_glob or [str(base / "heartbeats_*.csv")],
        args.trade_glob or [str(base / "trades_*.csv")],
        Path(args.output_dir),
        daily_goal=args.daily_goal,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

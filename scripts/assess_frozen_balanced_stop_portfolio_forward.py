#!/usr/bin/env python3
"""Judge a frozen 50/50 portfolio of the two stop-avoidance selectors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.assess_frozen_rank_migration_depth_forward as judge  # noqa: E402
from scripts.assess_depth_executable_rank_strategy import (  # noqa: E402
    execution_metrics,
    passes_depth_gate,
)


EVIDENCE_NAME = "balanced_stop_portfolio_five_level_depth_forward"
CONTRACT = "t1_5_s1_0_h60"
SELECTION_CUTOFF = "2026-07-29"
FORWARD_START = "2026-07-30"
MODEL_TRAINED_THROUGH = "2026-07-29"
FORWARD_SESSIONS_REQUIRED = 20
TOTAL_CAPITAL = 500_000.0
LANE_CAPITAL = TOTAL_CAPITAL / 2.0

STOP_LANE = {
    "name": "stop_half",
    "source": "prior_session_rank_migration_stop_avoidance",
    "evidence": "balanced_portfolio_stop_half",
    "threshold": 0.78,
    "max_trades": 5,
}
RUNNER_LANE = {
    "name": "runner_half",
    "source": "prior_session_runner_qualified_stop_avoidance",
    "evidence": "balanced_portfolio_runner_half",
    "threshold": 0.0,
    "max_trades": 10,
}


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, low_memory=False)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


def _freeze_portfolio(
    output_dir: Path,
    stop_metadata: dict[str, Any],
    runner_metadata: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    expected = {
        "evidence": EVIDENCE_NAME,
        "contract": CONTRACT,
        "capital": TOTAL_CAPITAL,
        "allocation": {
            STOP_LANE["source"]: LANE_CAPITAL,
            RUNNER_LANE["source"]: LANE_CAPITAL,
        },
        "selection_cutoff": SELECTION_CUTOFF,
        "forward_start": FORWARD_START,
        "model_trained_through": MODEL_TRAINED_THROUGH,
        "stop_model_source_sha256": str(
            stop_metadata.get("source_sha256", "") or ""
        ),
        "runner_model_source_sha256": str(
            runner_metadata.get("source_sha256", "") or ""
        ),
        "execution_schema_version": judge.EXECUTION_SCHEMA_VERSION,
        "paper_only": True,
        "alerts_allowed": False,
        "real_orders_allowed": False,
    }
    path = output_dir / "frozen_hypothesis.json"
    if path.exists():
        try:
            frozen = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}, False
        valid = bool(
            frozen == expected
            and expected["stop_model_source_sha256"]
            and expected["runner_model_source_sha256"]
        )
        return frozen, valid
    path.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    valid = bool(
        expected["stop_model_source_sha256"]
        and expected["runner_model_source_sha256"]
    )
    return expected, valid


def _run_half(
    config: dict[str, Any],
    *,
    registration_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    lane_status_patterns: Iterable[str],
    model_path: Path,
    output_dir: Path,
    forward_sessions_required: int,
    min_snapshots: int,
) -> dict[str, Any]:
    return judge.run_audit(
        registration_patterns=registration_patterns,
        outcome_patterns=outcome_patterns,
        raw_patterns=raw_patterns,
        lane_status_patterns=lane_status_patterns,
        model_path=model_path,
        output_dir=output_dir,
        capital=LANE_CAPITAL,
        daily_goal=5_000.0,
        forward_sessions_required=forward_sessions_required,
        min_snapshots=min_snapshots,
        lane_source=str(config["source"]),
        evidence_name=str(config["evidence"]),
        contract=CONTRACT,
        score_threshold=float(config["threshold"]),
        selection_cutoff=SELECTION_CUTOFF,
        forward_start=FORWARD_START,
        model_trained_through=MODEL_TRAINED_THROUGH,
        max_trades_per_day=int(config["max_trades"]),
    )


def _combine_coverage(
    stop_coverage: pd.DataFrame,
    runner_coverage: pd.DataFrame,
) -> pd.DataFrame:
    stop = stop_coverage.add_prefix("stop_").rename(
        columns={"stop_date": "date"}
    )
    runner = runner_coverage.add_prefix("runner_").rename(
        columns={"runner_date": "date"}
    )
    if stop.empty and runner.empty:
        return pd.DataFrame(
            columns=["date", "stop_eligible", "runner_eligible", "eligible"]
        )
    combined = stop.merge(runner, on="date", how="outer")
    for column in ("stop_eligible", "runner_eligible"):
        if column not in combined.columns:
            combined[column] = False
        combined[column] = combined[column].fillna(False).astype(bool)
    combined["eligible"] = (
        combined["stop_eligible"] & combined["runner_eligible"]
    )
    return combined.sort_values("date").reset_index(drop=True)


def _combine_daily(
    stop_daily: pd.DataFrame,
    runner_daily: pd.DataFrame,
    dates: Iterable[str],
) -> pd.DataFrame:
    stop_map = {
        str(row["date"]): row
        for row in stop_daily.to_dict(orient="records")
    }
    runner_map = {
        str(row["date"]): row
        for row in runner_daily.to_dict(orient="records")
    }
    rows = []
    for date in sorted({str(value) for value in dates}):
        stop = stop_map.get(date, {})
        runner = runner_map.get(date, {})
        complete = bool(
            stop
            and runner
            and stop.get("execution_complete")
            and runner.get("execution_complete")
            and pd.notna(stop.get("pnl"))
            and pd.notna(runner.get("pnl"))
        )
        pnl = (
            float(stop["pnl"]) + float(runner["pnl"])
            if complete
            else pd.NA
        )
        rows.append(
            {
                "date": date,
                "trades": int(stop.get("trades", 0) or 0)
                + int(runner.get("trades", 0) or 0),
                "executable_trades": int(
                    stop.get("executable_trades", 0) or 0
                )
                + int(runner.get("executable_trades", 0) or 0),
                "exit_depth_failures": int(
                    stop.get("exit_depth_failures", 0) or 0
                )
                + int(runner.get("exit_depth_failures", 0) or 0),
                "execution_complete": complete,
                "pnl": round(float(pnl), 2) if complete else pd.NA,
                "daily_return_pct": (
                    round(float(pnl) / TOTAL_CAPITAL * 100.0, 6)
                    if complete
                    else pd.NA
                ),
            }
        )
    return pd.DataFrame(rows)


def run_audit(
    *,
    stop_registration_patterns: Iterable[str],
    stop_outcome_patterns: Iterable[str],
    stop_status_patterns: Iterable[str],
    stop_model_path: Path,
    runner_registration_patterns: Iterable[str],
    runner_outcome_patterns: Iterable[str],
    runner_status_patterns: Iterable[str],
    runner_model_path: Path,
    raw_patterns: Iterable[str],
    output_dir: Path,
    daily_goal: float = 10_000.0,
    forward_sessions_required: int = FORWARD_SESSIONS_REQUIRED,
    min_snapshots: int = 1000,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stop_metadata = judge.model_metadata(stop_model_path)
    runner_metadata = judge.model_metadata(runner_model_path)
    frozen, hypothesis_valid = _freeze_portfolio(
        output_dir, stop_metadata, runner_metadata
    )

    stop_status = _run_half(
        STOP_LANE,
        registration_patterns=stop_registration_patterns,
        outcome_patterns=stop_outcome_patterns,
        raw_patterns=raw_patterns,
        lane_status_patterns=stop_status_patterns,
        model_path=stop_model_path,
        output_dir=output_dir / "stop_half",
        forward_sessions_required=forward_sessions_required,
        min_snapshots=min_snapshots,
    )
    runner_status = _run_half(
        RUNNER_LANE,
        registration_patterns=runner_registration_patterns,
        outcome_patterns=runner_outcome_patterns,
        raw_patterns=raw_patterns,
        lane_status_patterns=runner_status_patterns,
        model_path=runner_model_path,
        output_dir=output_dir / "runner_half",
        forward_sessions_required=forward_sessions_required,
        min_snapshots=min_snapshots,
    )

    coverage = _combine_coverage(
        _read_csv(output_dir / "stop_half" / "coverage.csv"),
        _read_csv(output_dir / "runner_half" / "coverage.csv"),
    )
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = (
        coverage.loc[coverage["eligible"], "date"].astype(str).tolist()
        if not coverage.empty
        else []
    )
    stop_daily = _read_csv(output_dir / "stop_half" / "forward_daily.csv")
    runner_daily = _read_csv(
        output_dir / "runner_half" / "forward_daily.csv"
    )
    daily = _combine_daily(stop_daily, runner_daily, eligible_dates)
    daily.to_csv(output_dir / "forward_daily.csv", index=False)

    stop_trades = _read_csv(output_dir / "stop_half" / "forward_trades.csv")
    runner_trades = _read_csv(
        output_dir / "runner_half" / "forward_trades.csv"
    )
    if not stop_trades.empty:
        stop_trades.insert(0, "portfolio_lane", "stop_half")
    if not runner_trades.empty:
        runner_trades.insert(0, "portfolio_lane", "runner_half")
    trades = pd.concat(
        [frame for frame in (stop_trades, runner_trades) if not frame.empty],
        ignore_index=True,
    ) if not stop_trades.empty or not runner_trades.empty else pd.DataFrame()
    trades.to_csv(output_dir / "forward_trades.csv", index=False)

    result = execution_metrics(daily, daily_goal, len(eligible_dates))
    component_hypotheses_valid = bool(
        stop_status.get("hypothesis_valid")
        and runner_status.get("hypothesis_valid")
    )
    accepted = bool(
        hypothesis_valid
        and component_hypotheses_valid
        and len(eligible_dates) >= int(forward_sessions_required)
        and passes_depth_gate(
            result, daily_goal, forward_sessions_required
        )
    )
    if not hypothesis_valid or not component_hypotheses_valid:
        status_name = "invalid_or_changed_frozen_hypothesis"
    elif len(eligible_dates) < int(forward_sessions_required):
        status_name = "collecting_untouched_depth_forward_sessions"
    else:
        status_name = (
            "accepted_untouched_depth_forward"
            if accepted
            else "rejected_untouched_depth_forward"
        )
    status = {
        "evidence": EVIDENCE_NAME,
        "status": status_name,
        "contract": CONTRACT,
        "selection_cutoff": SELECTION_CUTOFF,
        "forward_start": FORWARD_START,
        "execution_schema_version": judge.EXECUTION_SCHEMA_VERSION,
        "capital": TOTAL_CAPITAL,
        "daily_goal": float(daily_goal),
        "allocation_per_lane": LANE_CAPITAL,
        "forward_sessions_required": int(forward_sessions_required),
        "eligible_sessions": len(eligible_dates),
        "forward_dates": eligible_dates,
        "frozen_hypothesis": frozen,
        "hypothesis_valid": hypothesis_valid,
        "component_hypotheses_valid": component_hypotheses_valid,
        "forward": result,
        "accepted": accepted,
        "paper_promotion_allowed": accepted,
        "alerts_allowed": False,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop-registration-glob", action="append", default=[])
    parser.add_argument("--stop-outcome-glob", action="append", default=[])
    parser.add_argument("--stop-status-glob", action="append", default=[])
    parser.add_argument(
        "--stop-model",
        default=(
            "results/Data/live_stop_avoidance/"
            "frozen_stop_avoidance_model.joblib"
        ),
    )
    parser.add_argument(
        "--runner-registration-glob", action="append", default=[]
    )
    parser.add_argument("--runner-outcome-glob", action="append", default=[])
    parser.add_argument("--runner-status-glob", action="append", default=[])
    parser.add_argument(
        "--runner-model",
        default=(
            "results/Data/live_stop_avoidance/"
            "frozen_runner_qualified_stop_model.joblib"
        ),
    )
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument(
        "--output-dir",
        default="results/Data/frozen_balanced_stop_portfolio_forward",
    )
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()
    status = run_audit(
        stop_registration_patterns=args.stop_registration_glob or [
            "results/Data/frozen_stop_avoidance_contract_matrix/"
            "registrations_20*.csv"
        ],
        stop_outcome_patterns=args.stop_outcome_glob or [
            "results/Data/frozen_stop_avoidance_contract_matrix/"
            "outcomes_20*.csv"
        ],
        stop_status_patterns=args.stop_status_glob or [
            "results/Data/frozen_stop_avoidance_contract_matrix/"
            "status_20*.json"
        ],
        stop_model_path=Path(args.stop_model),
        runner_registration_patterns=args.runner_registration_glob or [
            "results/Data/frozen_runner_qualified_stop_contract_matrix/"
            "registrations_20*.csv"
        ],
        runner_outcome_patterns=args.runner_outcome_glob or [
            "results/Data/frozen_runner_qualified_stop_contract_matrix/"
            "outcomes_20*.csv"
        ],
        runner_status_patterns=args.runner_status_glob or [
            "results/Data/frozen_runner_qualified_stop_contract_matrix/"
            "status_20*.json"
        ],
        runner_model_path=Path(args.runner_model),
        raw_patterns=args.raw_glob or [
            "results/Data/live_feature_recorder/live_features_20*.csv"
        ],
        output_dir=Path(args.output_dir),
        daily_goal=args.daily_goal,
        forward_sessions_required=args.forward_sessions,
        min_snapshots=args.min_snapshots,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

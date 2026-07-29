#!/usr/bin/env python3
"""Judge fixed post-margin MIS variants of the frozen balanced portfolio."""

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
from scripts.assess_frozen_balanced_stop_portfolio_forward import (  # noqa: E402
    CONTRACT,
    FORWARD_SESSIONS_REQUIRED,
    FORWARD_START,
    LANE_CAPITAL,
    MODEL_TRAINED_THROUGH,
    RUNNER_LANE,
    SELECTION_CUTOFF,
    STOP_LANE,
    TOTAL_CAPITAL,
    _combine_daily,
    _read_csv,
)


EVIDENCE_NAME = "balanced_stop_portfolio_post_margin_forward"
PRIMARY_MULTIPLIER = 1.50
DIAGNOSTIC_MULTIPLIER = 1.25
MULTIPLIERS = (DIAGNOSTIC_MULTIPLIER, PRIMARY_MULTIPLIER)


def _suffix(multiplier: float) -> str:
    return f"mis_{multiplier:.2f}x".replace(".", "_")


def _leveraged_source(source: str, multiplier: float) -> str:
    return f"{source}:{_suffix(multiplier)}"


def _freeze_matrix(
    output_dir: Path,
    stop_metadata: dict[str, Any],
    runner_metadata: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    expected = {
        "evidence": EVIDENCE_NAME,
        "contract": CONTRACT,
        "execution_schema_version": judge.EXECUTION_SCHEMA_VERSION,
        "capital": TOTAL_CAPITAL,
        "lane_capital": LANE_CAPITAL,
        "primary_multiplier": PRIMARY_MULTIPLIER,
        "diagnostic_multipliers": [DIAGNOSTIC_MULTIPLIER],
        "all_multipliers": list(MULTIPLIERS),
        "selection_cutoff": SELECTION_CUTOFF,
        "forward_start": FORWARD_START,
        "model_trained_through": MODEL_TRAINED_THROUGH,
        "stop_model_source_sha256": str(
            stop_metadata.get("source_sha256", "") or ""
        ),
        "runner_model_source_sha256": str(
            runner_metadata.get("source_sha256", "") or ""
        ),
        "entry_timing": "strictly_after_causal_kite_margin_quote",
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
        return frozen, bool(
            frozen == expected
            and expected["stop_model_source_sha256"]
            and expected["runner_model_source_sha256"]
        )
    path.write_text(
        json.dumps(expected, indent=2) + "\n", encoding="utf-8"
    )
    return expected, bool(
        expected["stop_model_source_sha256"]
        and expected["runner_model_source_sha256"]
    )


def _candidate_complete_coverage(
    base: pd.DataFrame,
    *,
    original_registration_patterns: Iterable[str],
    original_status_patterns: Iterable[str],
    leverage_registration_patterns: Iterable[str],
    original_source: str,
    leverage_source: str,
    score_threshold: float,
) -> pd.DataFrame:
    coverage = base.copy()
    if coverage.empty:
        return coverage
    originals = judge.load_registration_ledger(
        original_registration_patterns,
        lane_source=original_source,
        forward_start=FORWARD_START,
        score_threshold=score_threshold,
    )
    leveraged = judge.load_registration_ledger(
        leverage_registration_patterns,
        lane_source=leverage_source,
        forward_start=FORWARD_START,
        score_threshold=score_threshold,
    )
    original_status = judge.load_lane_status(
        original_status_patterns,
        forward_start=FORWARD_START,
    )
    status_map = {
        str(row["date"]): bool(row["lane_heartbeat_complete"])
        for row in original_status.to_dict(orient="records")
    }
    rows = []
    for row in coverage.to_dict(orient="records"):
        date = str(row.get("date", "") or "")
        expected = set(
            originals.loc[
                originals["Date"].astype(str).eq(date), "EventId"
            ].astype(str)
        ) if not originals.empty else set()
        observed = set()
        if (
            not leveraged.empty
            and "OriginalEventId" in leveraged.columns
        ):
            observed = set(
                leveraged.loc[
                    leveraged["Date"].astype(str).eq(date),
                    "OriginalEventId",
                ].fillna("").astype(str)
            )
            observed.discard("")
        missing = expected - observed
        unexpected = observed - expected
        original_heartbeat = status_map.get(date, False)
        row.update(
            {
                "original_lane_heartbeat_complete": original_heartbeat,
                "original_score_qualified_candidates": len(expected),
                "post_margin_entries": len(observed),
                "missing_post_margin_entries": len(missing),
                "unexpected_post_margin_entries": len(unexpected),
                "eligible": bool(
                    row.get("eligible")
                    and original_heartbeat
                    and not missing
                    and not unexpected
                ),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def _component(
    lane: dict[str, Any],
    multiplier: float,
    *,
    original_registration_patterns: Iterable[str],
    original_status_patterns: Iterable[str],
    leverage_registration_patterns: Iterable[str],
    leverage_outcome_patterns: Iterable[str],
    leverage_status_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    model_path: Path,
    output_dir: Path,
    forward_sessions_required: int,
    min_snapshots: int,
) -> tuple[dict[str, Any], pd.DataFrame]:
    source = _leveraged_source(str(lane["source"]), multiplier)
    status = judge.run_audit(
        registration_patterns=leverage_registration_patterns,
        outcome_patterns=leverage_outcome_patterns,
        raw_patterns=raw_patterns,
        lane_status_patterns=leverage_status_patterns,
        model_path=model_path,
        output_dir=output_dir,
        capital=LANE_CAPITAL,
        daily_goal=5_000.0,
        forward_sessions_required=forward_sessions_required,
        min_snapshots=min_snapshots,
        lane_source=source,
        evidence_name=f"{lane['evidence']}_{_suffix(multiplier)}",
        contract=CONTRACT,
        score_threshold=float(lane["threshold"]),
        selection_cutoff=SELECTION_CUTOFF,
        forward_start=FORWARD_START,
        model_trained_through=MODEL_TRAINED_THROUGH,
        max_trades_per_day=int(lane["max_trades"]),
        notional_multiplier=multiplier,
    )
    coverage = _candidate_complete_coverage(
        _read_csv(output_dir / "coverage.csv"),
        original_registration_patterns=original_registration_patterns,
        original_status_patterns=original_status_patterns,
        leverage_registration_patterns=leverage_registration_patterns,
        original_source=str(lane["source"]),
        leverage_source=source,
        score_threshold=float(lane["threshold"]),
    )
    coverage.to_csv(
        output_dir / "coverage_with_candidate_completeness.csv",
        index=False,
    )
    return status, coverage


def _combine_coverage(
    stop: pd.DataFrame,
    runner: pd.DataFrame,
) -> pd.DataFrame:
    stop_prefixed = stop.add_prefix("stop_").rename(
        columns={"stop_date": "date"}
    )
    runner_prefixed = runner.add_prefix("runner_").rename(
        columns={"runner_date": "date"}
    )
    combined = stop_prefixed.merge(
        runner_prefixed, on="date", how="outer"
    )
    for column in ("stop_eligible", "runner_eligible"):
        if column not in combined:
            combined[column] = False
        combined[column] = combined[column].fillna(False).astype(bool)
    combined["eligible"] = (
        combined["stop_eligible"] & combined["runner_eligible"]
    )
    return combined.sort_values("date").reset_index(drop=True)


def _run_variant(
    multiplier: float,
    *,
    stop_registration_patterns: Iterable[str],
    stop_status_patterns: Iterable[str],
    stop_model_path: Path,
    runner_registration_patterns: Iterable[str],
    runner_status_patterns: Iterable[str],
    runner_model_path: Path,
    leverage_registration_patterns: Iterable[str],
    leverage_outcome_patterns: Iterable[str],
    leverage_status_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    output_dir: Path,
    daily_goal: float,
    forward_sessions_required: int,
    min_snapshots: int,
) -> dict[str, Any]:
    stop_status, stop_coverage = _component(
        STOP_LANE,
        multiplier,
        original_registration_patterns=stop_registration_patterns,
        original_status_patterns=stop_status_patterns,
        leverage_registration_patterns=leverage_registration_patterns,
        leverage_outcome_patterns=leverage_outcome_patterns,
        leverage_status_patterns=leverage_status_patterns,
        raw_patterns=raw_patterns,
        model_path=stop_model_path,
        output_dir=output_dir / "stop_half",
        forward_sessions_required=forward_sessions_required,
        min_snapshots=min_snapshots,
    )
    runner_status, runner_coverage = _component(
        RUNNER_LANE,
        multiplier,
        original_registration_patterns=runner_registration_patterns,
        original_status_patterns=runner_status_patterns,
        leverage_registration_patterns=leverage_registration_patterns,
        leverage_outcome_patterns=leverage_outcome_patterns,
        leverage_status_patterns=leverage_status_patterns,
        raw_patterns=raw_patterns,
        model_path=runner_model_path,
        output_dir=output_dir / "runner_half",
        forward_sessions_required=forward_sessions_required,
        min_snapshots=min_snapshots,
    )
    coverage = _combine_coverage(stop_coverage, runner_coverage)
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = (
        coverage.loc[coverage["eligible"], "date"].astype(str).tolist()
        if not coverage.empty
        else []
    )
    stop_daily = _read_csv(
        output_dir / "stop_half" / "forward_daily.csv"
    )
    runner_daily = _read_csv(
        output_dir / "runner_half" / "forward_daily.csv"
    )
    daily = _combine_daily(stop_daily, runner_daily, eligible_dates)
    daily.to_csv(output_dir / "forward_daily.csv", index=False)

    trade_frames = []
    for lane_name in ("stop_half", "runner_half"):
        frame = _read_csv(
            output_dir / lane_name / "forward_trades.csv"
        )
        if not frame.empty:
            frame.insert(0, "portfolio_lane", lane_name)
            trade_frames.append(frame)
    trades = (
        pd.concat(trade_frames, ignore_index=True)
        if trade_frames else pd.DataFrame()
    )
    trades.to_csv(output_dir / "forward_trades.csv", index=False)

    result = execution_metrics(daily, daily_goal, len(eligible_dates))
    hypotheses_valid = bool(
        stop_status.get("hypothesis_valid")
        and runner_status.get("hypothesis_valid")
    )
    accepted = bool(
        hypotheses_valid
        and len(eligible_dates) >= int(forward_sessions_required)
        and passes_depth_gate(
            result, daily_goal, forward_sessions_required
        )
    )
    status = {
        "multiplier": multiplier,
        "role": (
            "primary_goal_test"
            if multiplier == PRIMARY_MULTIPLIER
            else "safety_diagnostic_not_promotion_eligible"
        ),
        "capital": TOTAL_CAPITAL,
        "execution_notional": TOTAL_CAPITAL * multiplier,
        "eligible_sessions": len(eligible_dates),
        "forward_dates": eligible_dates,
        "component_hypotheses_valid": hypotheses_valid,
        "forward": result,
        "accepted": accepted,
        "paper_promotion_allowed": bool(
            accepted and multiplier == PRIMARY_MULTIPLIER
        ),
        "alerts_allowed": False,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2) + "\n", encoding="utf-8"
    )
    return status


def run_audit(
    *,
    stop_registration_patterns: Iterable[str],
    stop_status_patterns: Iterable[str],
    stop_model_path: Path,
    runner_registration_patterns: Iterable[str],
    runner_status_patterns: Iterable[str],
    runner_model_path: Path,
    leverage_registration_patterns: Iterable[str],
    leverage_outcome_patterns: Iterable[str],
    leverage_status_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    output_dir: Path,
    daily_goal: float = 10_000.0,
    forward_sessions_required: int = FORWARD_SESSIONS_REQUIRED,
    min_snapshots: int = 1000,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frozen, hypothesis_valid = _freeze_matrix(
        output_dir,
        judge.model_metadata(stop_model_path),
        judge.model_metadata(runner_model_path),
    )
    variants: dict[str, dict[str, Any]] = {}
    for multiplier in MULTIPLIERS:
        key = f"{multiplier:.2f}x"
        variants[key] = _run_variant(
            multiplier,
            stop_registration_patterns=stop_registration_patterns,
            stop_status_patterns=stop_status_patterns,
            stop_model_path=stop_model_path,
            runner_registration_patterns=runner_registration_patterns,
            runner_status_patterns=runner_status_patterns,
            runner_model_path=runner_model_path,
            leverage_registration_patterns=leverage_registration_patterns,
            leverage_outcome_patterns=leverage_outcome_patterns,
            leverage_status_patterns=leverage_status_patterns,
            raw_patterns=raw_patterns,
            output_dir=output_dir / _suffix(multiplier),
            daily_goal=daily_goal,
            forward_sessions_required=forward_sessions_required,
            min_snapshots=min_snapshots,
        )
    primary = variants[f"{PRIMARY_MULTIPLIER:.2f}x"]
    primary_ready = (
        int(primary["eligible_sessions"]) >= int(forward_sessions_required)
    )
    accepted = bool(hypothesis_valid and primary.get("accepted"))
    if not hypothesis_valid:
        status_name = "invalid_or_changed_frozen_hypothesis"
    elif not primary_ready:
        status_name = "collecting_untouched_post_margin_sessions"
    else:
        status_name = (
            "accepted_primary_1_50x_post_margin_forward"
            if accepted
            else "rejected_primary_1_50x_post_margin_forward"
        )
    status = {
        "evidence": EVIDENCE_NAME,
        "status": status_name,
        "contract": CONTRACT,
        "execution_schema_version": judge.EXECUTION_SCHEMA_VERSION,
        "selection_cutoff": SELECTION_CUTOFF,
        "forward_start": FORWARD_START,
        "capital": TOTAL_CAPITAL,
        "daily_goal": float(daily_goal),
        "primary_multiplier": PRIMARY_MULTIPLIER,
        "forward_sessions_required": int(forward_sessions_required),
        "frozen_hypothesis": frozen,
        "hypothesis_valid": hypothesis_valid,
        "variants": variants,
        "forward": primary.get("forward", {}),
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
    parser.add_argument("--stop-registration-glob", action="append", default=[])
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
    parser.add_argument("--runner-status-glob", action="append", default=[])
    parser.add_argument(
        "--runner-model",
        default=(
            "results/Data/live_stop_avoidance/"
            "frozen_runner_qualified_stop_model.joblib"
        ),
    )
    parser.add_argument(
        "--leverage-registration-glob", action="append", default=[]
    )
    parser.add_argument(
        "--leverage-outcome-glob", action="append", default=[]
    )
    parser.add_argument(
        "--leverage-status-glob", action="append", default=[]
    )
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument(
        "--output-dir",
        default="results/Data/frozen_balanced_stop_margin_forward",
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
        stop_status_patterns=args.stop_status_glob or [
            "results/Data/frozen_stop_avoidance_contract_matrix/"
            "status_20*.json"
        ],
        stop_model_path=Path(args.stop_model),
        runner_registration_patterns=args.runner_registration_glob or [
            "results/Data/frozen_runner_qualified_stop_contract_matrix/"
            "registrations_20*.csv"
        ],
        runner_status_patterns=args.runner_status_glob or [
            "results/Data/frozen_runner_qualified_stop_contract_matrix/"
            "status_20*.json"
        ],
        runner_model_path=Path(args.runner_model),
        leverage_registration_patterns=(
            args.leverage_registration_glob or [
                "results/Data/frozen_margin_leverage_contract_matrix/"
                "registrations_20*.csv"
            ]
        ),
        leverage_outcome_patterns=args.leverage_outcome_glob or [
            "results/Data/frozen_margin_leverage_contract_matrix/"
            "outcomes_20*.csv"
        ],
        leverage_status_patterns=args.leverage_status_glob or [
            "results/Data/frozen_margin_leverage_contract_matrix/"
            "status_20*.json"
        ],
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

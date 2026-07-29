#!/usr/bin/env python3
"""Build one honest scoreboard across heterogeneous intraday strategy lanes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd


SCOREBOARD_COLUMNS = [
    "Lane",
    "Variant",
    "EvidenceClass",
    "Status",
    "Sessions",
    "Trades",
    "StressAvgDailyPnl",
    "StressLower95DailyPnl",
    "PositiveSessionPct",
    "GoalHitPct",
    "WorstSessionPnl",
    "Accepted",
    "PaperPromotionAllowed",
    "RealOrdersAllowed",
    "MeasuredEvidence",
    "CapitalAllocationComparable",
    "DiagnosticOnly",
    "Source",
]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "1.0", "true", "yes", "y", "on"}


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _best_csv_row(path: Path, avg_field: str, lower_field: str) -> Dict[str, Any]:
    try:
        frame = pd.read_csv(path)
    except Exception:
        return {}
    if frame.empty:
        return {}
    for field in (avg_field, lower_field):
        frame[field] = pd.to_numeric(frame.get(field), errors="coerce").fillna(0.0)
    accepted = frame.get("ShadowCandidate", pd.Series(False, index=frame.index)).apply(_truthy)
    frame = frame.assign(_accepted_sort=accepted)
    frame.sort_values(
        ["_accepted_sort", lower_field, avg_field],
        ascending=[False, False, False],
        inplace=True,
    )
    return frame.iloc[0].to_dict()


def _row(
    *,
    lane: str,
    variant: str,
    evidence_class: str,
    status: str,
    sessions: Any,
    trades: Any,
    avg: Any,
    lower: Any,
    positive: Any,
    goal: Any,
    worst: Any,
    accepted: Any,
    paper: Any,
    real: Any,
    measured: bool,
    comparable: bool,
    diagnostic: bool,
    source: str,
) -> Dict[str, Any]:
    return {
        "Lane": lane,
        "Variant": variant,
        "EvidenceClass": evidence_class,
        "Status": status,
        "Sessions": int(_number(sessions)),
        "Trades": int(_number(trades)),
        "StressAvgDailyPnl": round(_number(avg), 2),
        "StressLower95DailyPnl": round(_number(lower), 2),
        "PositiveSessionPct": round(_number(positive), 2),
        "GoalHitPct": round(_number(goal), 2),
        "WorstSessionPnl": round(_number(worst), 2),
        "Accepted": bool(accepted),
        "PaperPromotionAllowed": bool(paper),
        "RealOrdersAllowed": bool(real),
        "MeasuredEvidence": bool(measured),
        "CapitalAllocationComparable": bool(comparable),
        "DiagnosticOnly": bool(diagnostic),
        "Source": source,
    }


def _csv_lane(
    data_dir: Path,
    *,
    lane: str,
    relative_path: str,
    variant_fields: Iterable[str],
    evidence_class: str,
    avg_field: str,
    lower_field: str,
    sessions_field: str,
    trades_field: str,
    positive_field: str,
    goal_field: str,
    worst_field: str,
) -> Dict[str, Any]:
    path = data_dir / relative_path
    best = _best_csv_row(path, avg_field, lower_field)
    variant = " | ".join(str(best.get(field, "")) for field in variant_fields if str(best.get(field, "")))
    accepted = _truthy(best.get("ShadowCandidate")) or _truthy(best.get("EvidenceAccepted"))
    paper = _truthy(best.get("PaperPromotionAllowed"))
    real = _truthy(best.get("RealOrdersAllowed"))
    return _row(
        lane=lane,
        variant=variant,
        evidence_class=evidence_class,
        status="accepted" if accepted else ("collecting" if best else "missing"),
        sessions=best.get(sessions_field),
        trades=best.get(trades_field),
        avg=best.get(avg_field),
        lower=best.get(lower_field),
        positive=best.get(positive_field),
        goal=best.get(goal_field),
        worst=best.get(worst_field),
        accepted=accepted,
        paper=paper,
        real=real,
        measured=bool(best) and int(_number(best.get(trades_field))) > 0,
        comparable=False,
        diagnostic=True,
        source=relative_path,
    )


def _forward_status_lane(
    data_dir: Path,
    *,
    lane: str,
    relative_path: str,
    evidence_class: str,
) -> Dict[str, Any]:
    path = data_dir / relative_path
    status = _load_json(path)
    forward = status.get("forward_stress") or status.get("forward") or {}
    frozen = status.get("frozen_selection") or status.get("selected_policy") or {}
    variant = " | ".join(
        str(value)
        for value in (
            status.get("contract"),
            frozen.get("policy") if isinstance(frozen, dict) else "",
            frozen.get("selector") if isinstance(frozen, dict) else "",
        )
        if value
    )
    accepted = bool(status.get("accepted", False))
    five_level = (
        evidence_class == "five_level_depth_executable_forward"
        and int(_number(status.get("execution_schema_version"))) >= 2
    )
    comparable = bool(five_level and accepted)
    return _row(
        lane=lane,
        variant=variant,
        evidence_class=evidence_class,
        status=str(status.get("status") or ("missing" if not status else "collecting")),
        sessions=forward.get("sessions", status.get("eligible_sessions", 0)),
        trades=forward.get("trades", 0),
        avg=forward.get("avg_daily_pnl", 0),
        lower=(
            _number(forward.get("lower_95_pct"), 0.0)
            * _number(status.get("capital"), 0.0)
            / 100.0
        ),
        positive=forward.get("positive_session_pct", 0),
        goal=forward.get("goal_hit_pct", 0),
        worst=(
            _number(forward.get("worst_session_pct"), 0.0)
            * _number(status.get("capital"), 0.0)
            / 100.0
        ),
        accepted=accepted,
        paper=status.get("paper_promotion_allowed", False),
        real=status.get("real_orders_allowed", False),
        measured=int(_number(forward.get("trades", 0))) > 0,
        comparable=comparable,
        diagnostic=not comparable,
        source=relative_path,
    )


def _partial_capital_diagnostic(data_dir: Path) -> Dict[str, Any]:
    relative = "full_universe_rank_capital_allocation/status.json"
    status = _load_json(data_dir / relative)
    best = status.get("best_partial_diagnostic") or {}
    capital = _number(status.get("capital"), 0.0)
    return _row(
        lane="Full-universe capital allocation",
        variant=" | ".join(str(best.get(key, "")) for key in ("selector", "contract", "slots") if best.get(key, "") != ""),
        evidence_class="top_book_partial_diagnostic",
        status=str(status.get("status") or ("missing" if not status else "collecting")),
        sessions=best.get("sessions", 0),
        trades=best.get("trades", 0),
        avg=best.get("avg_daily_pnl", 0),
        lower=_number(best.get("lower_95_pct"), 0.0) * capital / 100.0,
        positive=best.get("positive_session_pct", 0),
        goal=best.get("goal_hit_pct", 0),
        worst=_number(best.get("worst_session_pct"), 0.0) * capital / 100.0,
        accepted=False,
        paper=False,
        real=False,
        measured=int(_number(best.get("trades", 0))) > 0,
        comparable=False,
        diagnostic=True,
        source=relative,
    )


def _goal_status_lane(
    data_dir: Path,
    *,
    lane: str,
    relative_path: str,
    evidence_class: str,
) -> Dict[str, Any]:
    status = _load_json(data_dir / relative_path)
    result = status.get("goal_stress") or {}
    capital = _number(status.get("capital"), 0.0)
    accepted = bool(status.get("accepted", False))
    return _row(
        lane=lane,
        variant=str(status.get("goal_contract") or status.get("strategy") or ""),
        evidence_class=evidence_class,
        status=str(status.get("status") or ("missing" if not status else "collecting")),
        sessions=result.get("sessions", status.get("calendar_sessions", 0)),
        trades=result.get("trades", 0),
        avg=result.get("avg_daily_pnl", 0),
        lower=_number(result.get("lower_95_pct"), 0.0) * capital / 100.0,
        positive=result.get("positive_session_pct", 0),
        goal=result.get("goal_hit_pct", 0),
        worst=_number(result.get("worst_session_pct"), 0.0) * capital / 100.0,
        accepted=accepted,
        paper=status.get("paper_promotion_allowed", False),
        real=status.get("real_orders_allowed", False),
        measured=int(_number(result.get("trades", 0))) > 0,
        comparable=False,
        diagnostic=True,
        source=relative_path,
    )


def build_scoreboard(data_dir: Path) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = [
        _csv_lane(
            data_dir,
            lane="Ranked Grade-B confirmation",
            relative_path="ranked_confirmation_replay/readiness.csv",
            variant_fields=("ConfigName",),
            evidence_class="exact_tick_top_book_counterfactual",
            avg_field="StressAvgDailyPnl",
            lower_field="StressLower95DailyPnl",
            sessions_field="CompleteDays",
            trades_field="ClosedTrades",
            positive_field="PositiveDayPct",
            goal_field="GoalHitPct",
            worst_field="WorstDayPnl",
        ),
        _csv_lane(
            data_dir,
            lane="Live ignition",
            relative_path="live_microstructure_ignition/readiness.csv",
            variant_fields=("Rule", "Contract"),
            evidence_class="live_feature_minute_outcome_counterfactual",
            avg_field="StressAvgDailyPnl",
            lower_field="StressLower95DailyPnl",
            sessions_field="Sessions",
            trades_field="Evaluated",
            positive_field="PositiveSessionPct",
            goal_field="GoalEvidencePassed",
            worst_field="WorstSessionPnl",
        ),
        _csv_lane(
            data_dir,
            lane="Live runner fade",
            relative_path="live_runner_fade/readiness.csv",
            variant_fields=("Rule", "Contract"),
            evidence_class="live_feature_minute_outcome_counterfactual",
            avg_field="StressAvgDailyPnl",
            lower_field="StressLower95DailyPnl",
            sessions_field="Sessions",
            trades_field="Evaluated",
            positive_field="PositiveSessionPct",
            goal_field="GoalEvidencePassed",
            worst_field="WorstSessionPnl",
        ),
        _forward_status_lane(
            data_dir,
            lane="Frozen leader continuation",
            relative_path="live_leader_continuation/frozen_ranker_status.json",
            evidence_class="live_forward_top_book_contract",
        ),
        _forward_status_lane(
            data_dir,
            lane="Frozen rank migration",
            relative_path="live_rank_migration/status.json",
            evidence_class="live_forward_top_book_contract",
        ),
        _forward_status_lane(
            data_dir,
            lane="Prior-session ranker",
            relative_path="prior_session_ranker_paper/status.json",
            evidence_class="live_forward_top_book_contract",
        ),
        _forward_status_lane(
            data_dir,
            lane="Five-level full-universe rank",
            relative_path="full_universe_rank_depth_execution/status.json",
            evidence_class="five_level_depth_executable_forward",
        ),
        _forward_status_lane(
            data_dir,
            lane="Five-level frozen rank migration",
            relative_path="frozen_rank_migration_depth_forward/status.json",
            evidence_class="five_level_depth_executable_forward",
        ),
        _forward_status_lane(
            data_dir,
            lane="Five-level stop avoidance",
            relative_path="frozen_stop_avoidance_depth_forward/status.json",
            evidence_class="five_level_depth_executable_forward",
        ),
        _forward_status_lane(
            data_dir,
            lane="Five-level runner-qualified stop",
            relative_path=(
                "frozen_runner_qualified_stop_depth_forward/status.json"
            ),
            evidence_class="five_level_depth_executable_forward",
        ),
        _partial_capital_diagnostic(data_dir),
        _goal_status_lane(
            data_dir,
            lane="Leader pullback reclaim",
            relative_path="leader_pullback_reclaim_audit/status.json",
            evidence_class="live_feature_minute_outcome_counterfactual",
        ),
    ]
    scoreboard = pd.DataFrame(rows, columns=SCOREBOARD_COLUMNS)
    scoreboard.sort_values(
        [
            "CapitalAllocationComparable",
            "Accepted",
            "MeasuredEvidence",
            "StressLower95DailyPnl",
            "StressAvgDailyPnl",
            "Lane",
        ],
        ascending=[False, False, False, False, False, True],
        inplace=True,
    )
    return scoreboard.reset_index(drop=True)


def write_scoreboard(data_dir: Path, output_dir: Path) -> pd.DataFrame:
    output_dir.mkdir(parents=True, exist_ok=True)
    scoreboard = build_scoreboard(data_dir)
    scoreboard.to_csv(output_dir / "scoreboard.csv", index=False)
    comparable = scoreboard[scoreboard["CapitalAllocationComparable"].astype(bool)]
    summary = {
        "lanes": int(len(scoreboard)),
        "accepted_lanes": int(scoreboard["Accepted"].astype(bool).sum()),
        "capital_allocation_comparable_lanes": int(len(comparable)),
        "goal_evidence_passes": int(
            (
                scoreboard["CapitalAllocationComparable"].astype(bool)
                & (scoreboard["StressAvgDailyPnl"] >= 10_000.0)
                & (scoreboard["StressLower95DailyPnl"] > 0.0)
            ).sum()
        ),
        "best_comparable_lane": (
            str(comparable.iloc[0]["Lane"]) if not comparable.empty else ""
        ),
        "real_orders_allowed": False,
        "status": (
            "comparable_executable_forward_available"
            if not comparable.empty
            else "collecting_executable_forward_evidence"
        ),
    }
    (output_dir / "status.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    return scoreboard


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="results/Data")
    parser.add_argument(
        "--output-dir",
        default="results/Data/intraday_strategy_evidence_scoreboard",
    )
    args = parser.parse_args()
    result = write_scoreboard(Path(args.data_dir), Path(args.output_dir))
    print(result.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

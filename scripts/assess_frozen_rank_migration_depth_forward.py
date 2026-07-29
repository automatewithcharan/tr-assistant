#!/usr/bin/env python3
"""Untouched five-level forward judge for the frozen rank-migration model."""

from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime
from pathlib import Path
import sys
from typing import Any, Iterable

import joblib
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.FullUniverseRankContractMatrixObserver import CONTRACTS
from pkscreener.classes.OrderBookExecution import (
    EXECUTION_SCHEMA_VERSION,
    affordable_market_fill,
    market_fill,
    parse_depth_json,
)
from scripts.assess_depth_executable_rank_strategy import (
    MIN_SLOT_DEPLOYMENT_PCT,
    execution_metrics,
    load_depth_events,
    passes_depth_gate,
)
from scripts.assess_full_universe_rank_strategy import raw_session_coverage


LANE_SOURCE = "prior_session_candidate_rank_migration"
EVIDENCE_NAME = "frozen_rank_migration_five_level_depth_forward"
CONTRACT = "t1_5_s1_0_h60"
SCORE_THRESHOLD = 0.25
SELECTION_CUTOFF = "2026-07-29"
FORWARD_START = "2026-07-30"
MODEL_TRAINED_THROUGH = "2026-07-21"
FORWARD_SESSIONS_REQUIRED = 20
MAX_TRADES_PER_DAY = 10
SLOTS = 1
LANE_STATUS_MINUTE = 15 * 60 + 10


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {path for pattern in patterns for path in glob.glob(str(pattern))}
    )


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "1", "1.0", "true", "yes", "y", "on",
    }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def model_metadata(path: Path) -> dict[str, Any]:
    try:
        payload = joblib.load(path)
    except Exception:
        return {}
    metadata = payload.get("metadata") if isinstance(payload, dict) else {}
    return metadata if isinstance(metadata, dict) else {}


def freeze_hypothesis(
    output_dir: Path,
    metadata: dict[str, Any],
    *,
    lane_source: str | None = None,
    contract: str | None = None,
    score_threshold: float | None = None,
    slots: int | None = None,
    capital: float = 500_000.0,
    max_trades_per_day: int | None = None,
    selection_cutoff: str | None = None,
    forward_start: str | None = None,
    model_trained_through: str | None = None,
    notional_multiplier: float = 1.0,
) -> tuple[dict, bool]:
    lane_source = LANE_SOURCE if lane_source is None else lane_source
    contract = CONTRACT if contract is None else contract
    score_threshold = (
        SCORE_THRESHOLD if score_threshold is None else score_threshold
    )
    slots = SLOTS if slots is None else slots
    max_trades_per_day = (
        MAX_TRADES_PER_DAY
        if max_trades_per_day is None
        else max_trades_per_day
    )
    selection_cutoff = (
        SELECTION_CUTOFF
        if selection_cutoff is None
        else selection_cutoff
    )
    forward_start = (
        FORWARD_START if forward_start is None else forward_start
    )
    model_trained_through = (
        MODEL_TRAINED_THROUGH
        if model_trained_through is None
        else model_trained_through
    )
    expected = {
        "lane_source": lane_source,
        "contract": contract,
        "score_threshold": score_threshold,
        "slots": slots,
        "capital": float(capital),
        "max_trades_per_day": max_trades_per_day,
        "minimum_slot_deployment_pct": MIN_SLOT_DEPLOYMENT_PCT,
        "selection_cutoff": selection_cutoff,
        "forward_start": forward_start,
        "model_trained_through": model_trained_through,
        "model_source_sha256": str(metadata.get("source_sha256", "") or ""),
        "execution_schema_version": EXECUTION_SCHEMA_VERSION,
        "paper_only": True,
        "real_orders_allowed": False,
    }
    if float(notional_multiplier) != 1.0:
        expected.update(
            {
                "notional_multiplier": float(notional_multiplier),
                "execution_capital": round(
                    float(capital) * float(notional_multiplier), 2
                ),
                "causal_margin_quote_required": True,
            }
        )
    metadata_valid = bool(
        str(metadata.get("trained_through", "") or "")
        == str(model_trained_through)
        and str(metadata.get("valid_from", "") or "")
        and str(metadata.get("valid_from", "") or "")
        <= str(forward_start)
        and _truthy(metadata.get("paper_only"))
        and not _truthy(metadata.get("real_orders_allowed"))
    )
    path = output_dir / "frozen_hypothesis.json"
    if path.exists():
        try:
            frozen = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}, False
        return frozen, bool(
            frozen == expected
            and expected["model_source_sha256"]
            and metadata_valid
        )
    path.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return expected, bool(expected["model_source_sha256"] and metadata_valid)


def load_registration_ledger(
    patterns: Iterable[str],
    *,
    lane_source: str | None = None,
    forward_start: str | None = None,
    score_threshold: float | None = None,
) -> pd.DataFrame:
    lane_source = LANE_SOURCE if lane_source is None else lane_source
    forward_start = FORWARD_START if forward_start is None else forward_start
    score_threshold = (
        SCORE_THRESHOLD if score_threshold is None else score_threshold
    )
    required = {
        "EventId", "Date", "RecorderGeneration", "Exchange", "Source",
        "ExecutionSchemaVersion", "ModelTrainedThrough",
        "ModelSourceSha256", "ExpectedReturnPct",
    }
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if required.issubset(frame.columns):
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    ledger = pd.concat(frames, ignore_index=True)
    ledger["Date"] = ledger["Date"].astype(str)
    ledger["Source"] = ledger["Source"].fillna("").astype(str)
    ledger["ModelTrainedThrough"] = (
        ledger["ModelTrainedThrough"].fillna("").astype(str)
    )
    ledger["ModelSourceSha256"] = (
        ledger["ModelSourceSha256"].fillna("").astype(str)
    )
    ledger["ExpectedReturnPct"] = pd.to_numeric(
        ledger["ExpectedReturnPct"], errors="coerce"
    ).fillna(-999.0)
    return ledger[
        ledger["Date"].ge(forward_start)
        & ledger["Source"].eq(lane_source)
        & ledger["ExpectedReturnPct"].ge(score_threshold)
    ].drop_duplicates("EventId", keep="first").copy()


def load_lane_status(
    patterns: Iterable[str],
    *,
    forward_start: str | None = None,
) -> pd.DataFrame:
    forward_start = FORWARD_START if forward_start is None else forward_start
    rows = []
    for path in _files(patterns):
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        date = str(payload.get("date", "") or "")
        updated = pd.to_datetime(payload.get("updated_at"), errors="coerce")
        minute = (
            int(updated.hour) * 60 + int(updated.minute)
            if not pd.isna(updated)
            else -1
        )
        rows.append(
            {
                "date": date,
                "lane_heartbeat_complete": bool(
                    date >= forward_start
                    and str(payload.get("status", ""))
                    == "recording_exact_exit_contract_matrix"
                    and int(_number(payload.get("execution_schema_version")))
                    >= EXECUTION_SCHEMA_VERSION
                    and int(_number(payload.get("contracts")))
                    == len(CONTRACTS)
                    and _truthy(payload.get("paper_only"))
                    and not _truthy(payload.get("alerts_allowed"))
                    and not _truthy(payload.get("real_orders_allowed"))
                    and minute >= LANE_STATUS_MINUTE
                ),
                "lane_updated_at": str(payload.get("updated_at", "") or ""),
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=["date", "lane_heartbeat_complete", "lane_updated_at"]
        )
    return (
        pd.DataFrame(rows)
        .sort_values(["date", "lane_updated_at"])
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


def forward_coverage(
    raw: pd.DataFrame,
    lane_status: pd.DataFrame,
    ledger: pd.DataFrame,
    entries: pd.DataFrame,
    events: pd.DataFrame,
    *,
    forward_start: str | None = None,
    contract: str | None = None,
) -> pd.DataFrame:
    forward_start = FORWARD_START if forward_start is None else forward_start
    contract = CONTRACT if contract is None else contract
    columns = [
        "date", "raw_full_session", "lane_heartbeat_complete",
        "score_qualified_entries", "schema_v2_entries",
        "invalid_entry_evidence", "missing_contract_outcomes", "eligible",
    ]
    statuses = {
        str(row["date"]): bool(row["lane_heartbeat_complete"])
        for row in lane_status.to_dict(orient="records")
    }
    rows = []
    for record in raw.to_dict(orient="records"):
        date = str(record.get("date", ""))
        if date < forward_start:
            continue
        observed_ids = set(
            ledger.loc[ledger["Date"].eq(date), "EventId"].astype(str)
        ) if not ledger.empty else set()
        valid_ids = set(
            entries.loc[entries["date"].eq(date), "EventId"].astype(str)
        ) if not entries.empty else set()
        closed_ids = set(
            events.loc[
                events["date"].eq(date)
                & events["Contract"].astype(str).eq(contract),
                "EventId",
            ].astype(str)
        ) if not events.empty else set()
        invalid_entries = observed_ids - valid_ids
        missing_outcomes = valid_ids - closed_ids
        raw_full = bool(record.get("full_session_eligible", False))
        heartbeat = statuses.get(date, False)
        rows.append(
            {
                "date": date,
                "raw_full_session": raw_full,
                "lane_heartbeat_complete": heartbeat,
                "score_qualified_entries": len(observed_ids),
                "schema_v2_entries": len(valid_ids),
                "invalid_entry_evidence": len(invalid_entries),
                "missing_contract_outcomes": len(missing_outcomes),
                "eligible": bool(
                    raw_full
                    and heartbeat
                    and not invalid_entries
                    and not missing_outcomes
                ),
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return (
        pd.DataFrame(rows, columns=columns)
        .sort_values("date")
        .reset_index(drop=True)
    )


def replay_forward(
    events: pd.DataFrame,
    dates: Iterable[str],
    *,
    capital: float,
    max_trades_per_day: int = MAX_TRADES_PER_DAY,
    min_slot_deployment_pct: float = MIN_SLOT_DEPLOYMENT_PCT,
    contract: str | None = None,
    score_threshold: float | None = None,
    execution_capital: float | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    contract = CONTRACT if contract is None else contract
    score_threshold = (
        SCORE_THRESHOLD if score_threshold is None else score_threshold
    )
    execution_capital = (
        float(capital)
        if execution_capital is None
        else float(execution_capital)
    )
    daily_rows = []
    trade_rows = []
    for date in sorted({str(value) for value in dates}):
        day = events[
            events["date"].eq(date)
            & events["Contract"].astype(str).eq(contract)
        ].copy() if not events.empty else pd.DataFrame()
        if not day.empty:
            day["entry_batch"] = day["entry_time"].dt.floor("min")
            day["ExpectedReturnPct"] = pd.to_numeric(
                day["ExpectedReturnPct"], errors="coerce"
            ).fillna(-999.0)
            day = day[
                day["ExpectedReturnPct"].ge(score_threshold)
            ].copy()
            day.sort_values(
                ["entry_batch", "ExpectedReturnPct", "Symbol"],
                ascending=[True, False, True],
                inplace=True,
            )

        active_exit: pd.Timestamp | None = None
        traded_symbols: set[str] = set()
        selected = executable = exit_failures = 0
        day_pnl = 0.0
        if not day.empty:
            for batch, visible in day.groupby("entry_batch", sort=True):
                if active_exit is not None and active_exit <= pd.Timestamp(batch):
                    active_exit = None
                if active_exit is not None or selected >= int(max_trades_per_day):
                    continue
                for _, pick in visible.iterrows():
                    symbol = str(pick["Symbol"])
                    if symbol in traded_symbols:
                        continue
                    entry_fill = affordable_market_fill(
                        parse_depth_json(pick.get("EntryDepthJson")),
                        execution_capital,
                    )
                    quantity = int(entry_fill["filled_quantity"])
                    deployed = float(entry_fill["notional"])
                    deployment_pct = (
                        deployed / execution_capital * 100.0
                        if execution_capital > 0 else 0.0
                    )
                    if (
                        quantity < 1
                        or deployment_pct < float(min_slot_deployment_pct)
                    ):
                        continue
                    selected += 1
                    traded_symbols.add(symbol)
                    active_exit = pd.Timestamp(pick["exit_time"])
                    exit_fill = market_fill(
                        parse_depth_json(pick.get("ExitDepthJson")),
                        quantity,
                    )
                    complete = bool(exit_fill["fillable"])
                    pnl: float | Any = pd.NA
                    if complete:
                        gross = (
                            float(exit_fill["vwap"])
                            - float(entry_fill["vwap"])
                        ) * quantity
                        stress_pct = _number(pick.get("StressPct"), 0.10)
                        pnl = gross - deployed * stress_pct / 100.0
                        day_pnl += float(pnl)
                        executable += 1
                    else:
                        exit_failures += 1
                    trade_rows.append(
                        {
                            "date": date,
                            "event_id": str(pick.get("EventId", "")),
                            "symbol": symbol,
                            "expected_return_pct": _number(
                                pick.get("ExpectedReturnPct")
                            ),
                            "entry_time": pick["entry_time"],
                            "exit_time": pick["exit_time"],
                            "exit_reason": str(pick.get("ExitReason", "")),
                            "deployed_capital": round(deployed, 2),
                            "base_capital": round(float(capital), 2),
                            "execution_capital": round(
                                execution_capital, 2
                            ),
                            "deployment_pct": round(deployment_pct, 4),
                            "quantity": quantity,
                            "entry_vwap": float(entry_fill["vwap"]),
                            "exit_vwap": (
                                float(exit_fill["vwap"])
                                if complete else pd.NA
                            ),
                            "execution_complete": complete,
                            "pnl": round(float(pnl), 2)
                            if complete else pd.NA,
                        }
                    )
                    break
        complete_session = exit_failures == 0
        daily_rows.append(
            {
                "date": date,
                "trades": selected,
                "executable_trades": executable,
                "exit_depth_failures": exit_failures,
                "execution_complete": complete_session,
                "pnl": round(day_pnl, 2) if complete_session else pd.NA,
                "daily_return_pct": (
                    round(day_pnl / float(capital) * 100.0, 6)
                    if complete_session else pd.NA
                ),
            }
        )
    return pd.DataFrame(daily_rows), pd.DataFrame(trade_rows)


def run_audit(
    *,
    registration_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    lane_status_patterns: Iterable[str],
    model_path: Path,
    output_dir: Path,
    capital: float = 500_000.0,
    daily_goal: float = 10_000.0,
    forward_sessions_required: int = FORWARD_SESSIONS_REQUIRED,
    min_snapshots: int = 1000,
    lane_source: str | None = None,
    evidence_name: str | None = None,
    contract: str | None = None,
    score_threshold: float | None = None,
    selection_cutoff: str | None = None,
    forward_start: str | None = None,
    model_trained_through: str | None = None,
    max_trades_per_day: int | None = None,
    notional_multiplier: float = 1.0,
) -> dict[str, Any]:
    lane_source = LANE_SOURCE if lane_source is None else lane_source
    evidence_name = EVIDENCE_NAME if evidence_name is None else evidence_name
    contract = CONTRACT if contract is None else contract
    score_threshold = (
        SCORE_THRESHOLD if score_threshold is None else score_threshold
    )
    selection_cutoff = (
        SELECTION_CUTOFF
        if selection_cutoff is None
        else selection_cutoff
    )
    forward_start = (
        FORWARD_START if forward_start is None else forward_start
    )
    model_trained_through = (
        MODEL_TRAINED_THROUGH
        if model_trained_through is None
        else model_trained_through
    )
    max_trades_per_day = (
        MAX_TRADES_PER_DAY
        if max_trades_per_day is None
        else max_trades_per_day
    )
    notional_multiplier = max(1.0, float(notional_multiplier))
    execution_capital = float(capital) * notional_multiplier
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = model_metadata(model_path)
    frozen, hypothesis_valid = freeze_hypothesis(
        output_dir,
        metadata,
        lane_source=lane_source,
        contract=contract,
        score_threshold=score_threshold,
        capital=capital,
        max_trades_per_day=max_trades_per_day,
        selection_cutoff=selection_cutoff,
        forward_start=forward_start,
        model_trained_through=model_trained_through,
        notional_multiplier=notional_multiplier,
    )
    expected_hash = str(frozen.get("model_source_sha256", "") or "")

    ledger = load_registration_ledger(
        registration_patterns,
        lane_source=lane_source,
        forward_start=forward_start,
        score_threshold=score_threshold,
    )
    entries, events = load_depth_events(
        registration_patterns, outcome_patterns
    )
    if not entries.empty:
        for column in (
            "Source", "ModelTrainedThrough", "ModelSourceSha256",
            "ExpectedReturnPct",
        ):
            if column not in entries.columns:
                entries[column] = ""
        entries = entries[
            entries["date"].ge(forward_start)
            & entries["Source"].astype(str).eq(lane_source)
            & entries["ModelTrainedThrough"].astype(str).eq(
                model_trained_through
            )
            & entries["ModelSourceSha256"].astype(str).eq(expected_hash)
            & pd.to_numeric(
                entries["ExpectedReturnPct"], errors="coerce"
            ).ge(score_threshold)
        ].copy()
        if notional_multiplier > 1.0:
            required_margin_columns = {
                "OriginalEventId",
                "MarginQuotedAt",
                "MarginPct",
                "RequiredMarginEstimate",
                "NotionalMultiplier",
                "BaseLaneCapital",
            }
            if not required_margin_columns.issubset(entries.columns):
                entries = entries.iloc[0:0].copy()
            else:
                entry_at = pd.to_datetime(
                    entries["EntryAt"], errors="coerce"
                )
                quoted_at = pd.to_datetime(
                    entries["MarginQuotedAt"], errors="coerce"
                )
                multiplier = pd.to_numeric(
                    entries["NotionalMultiplier"], errors="coerce"
                )
                base_capital = pd.to_numeric(
                    entries["BaseLaneCapital"], errors="coerce"
                )
                margin_pct = pd.to_numeric(
                    entries["MarginPct"], errors="coerce"
                )
                required_margin = pd.to_numeric(
                    entries["RequiredMarginEstimate"], errors="coerce"
                )
                entries = entries[
                    entry_at.notna()
                    & quoted_at.notna()
                    & entry_at.gt(quoted_at)
                    & multiplier.sub(notional_multiplier).abs().lt(1e-9)
                    & base_capital.sub(float(capital)).abs().lt(0.01)
                    & margin_pct.gt(0.0)
                    & margin_pct.le(100.0)
                    & required_margin.gt(0.0)
                    & required_margin.le(float(capital))
                    & entries["OriginalEventId"].fillna("").astype(str).ne("")
                ].copy()
    valid_ids = set(entries["EventId"].astype(str)) if not entries.empty else set()
    events = events[
        events["EventId"].astype(str).isin(valid_ids)
        & events["Contract"].astype(str).eq(contract)
    ].copy() if not events.empty else events

    raw = raw_session_coverage(raw_patterns, min_snapshots=min_snapshots)
    lane_status = load_lane_status(
        lane_status_patterns,
        forward_start=forward_start,
    )
    coverage = forward_coverage(
        raw,
        lane_status,
        ledger,
        entries,
        events,
        forward_start=forward_start,
        contract=contract,
    )
    coverage.to_csv(output_dir / "coverage.csv", index=False)
    eligible_dates = (
        coverage.loc[coverage["eligible"], "date"].astype(str).tolist()
        if not coverage.empty else []
    )
    daily, trades = replay_forward(
        events,
        eligible_dates,
        capital=capital,
        max_trades_per_day=max_trades_per_day,
        contract=contract,
        score_threshold=score_threshold,
        execution_capital=execution_capital,
    )
    daily.to_csv(output_dir / "forward_daily.csv", index=False)
    trades.to_csv(output_dir / "forward_trades.csv", index=False)
    result = execution_metrics(daily, daily_goal, len(eligible_dates))
    accepted = bool(
        hypothesis_valid
        and len(eligible_dates) >= int(forward_sessions_required)
        and passes_depth_gate(result, daily_goal, forward_sessions_required)
    )
    if not hypothesis_valid:
        status_name = "invalid_or_changed_frozen_hypothesis"
    elif len(eligible_dates) < int(forward_sessions_required):
        status_name = "collecting_untouched_depth_forward_sessions"
    else:
        status_name = (
            "accepted_untouched_depth_forward"
            if accepted else "rejected_untouched_depth_forward"
        )
    status = {
        "evidence": evidence_name,
        "status": status_name,
        "contract": contract,
        "score_threshold": score_threshold,
        "selection_cutoff": selection_cutoff,
        "forward_start": forward_start,
        "execution_schema_version": EXECUTION_SCHEMA_VERSION,
        "capital": float(capital),
        "notional_multiplier": notional_multiplier,
        "execution_capital": execution_capital,
        "causal_margin_quote_required": notional_multiplier > 1.0,
        "daily_goal": float(daily_goal),
        "slots": SLOTS,
        "max_trades_per_day": max_trades_per_day,
        "minimum_slot_deployment_pct": MIN_SLOT_DEPLOYMENT_PCT,
        "forward_sessions_required": int(forward_sessions_required),
        "eligible_sessions": len(eligible_dates),
        "forward_dates": eligible_dates,
        "frozen_hypothesis": frozen,
        "hypothesis_valid": hypothesis_valid,
        "forward": result,
        "accepted": accepted,
        "paper_promotion_allowed": accepted,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration-glob", action="append", default=[])
    parser.add_argument("--outcome-glob", action="append", default=[])
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument("--lane-status-glob", action="append", default=[])
    parser.add_argument(
        "--model",
        default="results/Data/live_rank_migration/frozen_rank_migration_model.joblib",
    )
    parser.add_argument(
        "--output-dir",
        default="results/Data/frozen_rank_migration_depth_forward",
    )
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()
    status = run_audit(
        registration_patterns=args.registration_glob or [
            "results/Data/frozen_rank_migration_contract_matrix/registrations_20*.csv"
        ],
        outcome_patterns=args.outcome_glob or [
            "results/Data/frozen_rank_migration_contract_matrix/outcomes_20*.csv"
        ],
        raw_patterns=args.raw_glob or [
            "results/Data/live_feature_recorder/live_features_20*.csv"
        ],
        lane_status_patterns=args.lane_status_glob or [
            "results/Data/frozen_rank_migration_contract_matrix/status_20*.json"
        ],
        model_path=Path(args.model),
        output_dir=Path(args.output_dir),
        capital=args.capital,
        daily_goal=args.daily_goal,
        forward_sessions_required=args.forward_sessions,
        min_snapshots=args.min_snapshots,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

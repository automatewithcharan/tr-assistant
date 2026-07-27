#!/usr/bin/env python3
"""Select and forward-test exact contracts for frozen-ranker predictions."""

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

from scripts.assess_full_universe_rank_strategy import raw_session_coverage  # noqa: E402
from scripts.assess_leader_pullback_reclaim import metrics  # noqa: E402
from scripts.assess_prior_session_ranker_paper import load_predictions  # noqa: E402
from pkscreener.classes.FullUniverseRankContractMatrixObserver import (  # noqa: E402
    CONTRACTS,
)


FORWARD_START = "2026-07-28"
SLOT_COUNTS = (1, 2, 3)
SCORE_THRESHOLDS = (0.20, 0.25, 0.30, 0.40)


def _files(patterns: Iterable[str]) -> list[str]:
    return sorted(
        {
            path
            for pattern in patterns
            for path in glob.glob(str(pattern), recursive=True)
        }
    )


def load_registrations(patterns: Iterable[str]) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {
            "EventId", "Date", "EntryAt", "Symbol", "EntryAsk",
            "ExpectedReturnPct", "MoveBand", "ModelTrainedThrough",
            "ModelSourceSha256",
        }
        if not required.issubset(frame.columns):
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["Date"] = data["Date"].astype(str).str[:10]
    data["EntryAt"] = pd.to_datetime(data["EntryAt"], errors="coerce")
    data["Symbol"] = data["Symbol"].astype(str).str.strip().str.upper()
    for column in ("EntryAsk", "ExpectedReturnPct"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["ModelTrainedThrough"] = (
        data["ModelTrainedThrough"].astype(str).str[:10]
    )
    return data.drop_duplicates(["EventId"], keep="last")


def load_outcomes(patterns: Iterable[str]) -> pd.DataFrame:
    frames = []
    for path in _files(patterns):
        try:
            frame = pd.read_csv(path, low_memory=False)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        required = {
            "EventId", "Contract", "Date", "EntryAt", "ExitAt", "Symbol",
            "NetReturnPct", "ExpectedReturnPct", "ModelTrainedThrough",
            "ModelSourceSha256",
        }
        if not required.issubset(frame.columns):
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["Date"] = data["Date"].astype(str).str[:10]
    data["EntryAt"] = pd.to_datetime(data["EntryAt"], errors="coerce")
    data["ExitAt"] = pd.to_datetime(data["ExitAt"], errors="coerce")
    data["Symbol"] = data["Symbol"].astype(str).str.strip().str.upper()
    for column in ("NetReturnPct", "ExpectedReturnPct"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.drop_duplicates(["EventId", "Contract"], keep="last")


def _expected_event_ids(
    predictions: pd.DataFrame,
    threshold: float,
) -> set[str]:
    eligible = predictions[
        pd.to_numeric(
            predictions["expected_return_pct"], errors="coerce"
        ).fillna(float("-inf")).ge(float(threshold))
    ]
    return {
        (
            f"{row.date}:{row.symbol}:{row.move_band}:"
            f"{str(row.model_source_sha256)[:16] or 'unknown-model'}"
        )
        for row in eligible.itertuples(index=False)
    }


def valid_session_data(
    date: str,
    predictions: pd.DataFrame,
    registrations: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    threshold: float,
    expected_contracts: set[str] | None = None,
) -> tuple[bool, list[str], list[str]]:
    issues = []
    if predictions.empty:
        return False, ["prediction file missing or empty"], []
    if predictions["when"].isna().any():
        issues.append("prediction timestamp missing")
    if (
        predictions["model_trained_through"].eq("")
        | predictions["model_trained_through"].ge(date)
    ).any():
        issues.append("prediction model is not prior-session-only")
    if (
        predictions["model_source_sha256"]
        .fillna("")
        .astype(str)
        .str.len()
        .lt(16)
        .any()
    ):
        issues.append("prediction model hash missing")

    expected_ids = _expected_event_ids(predictions, threshold)
    actual_ids = set(registrations.get("EventId", pd.Series(dtype=str)))
    if expected_ids != actual_ids:
        issues.append(
            "prediction/registration mismatch "
            f"{len(expected_ids)}/{len(actual_ids)}"
        )
    if registrations.empty:
        return not issues, issues, []
    if (
        registrations["EntryAt"].isna()
        | registrations["EntryAsk"].le(0)
    ).any():
        issues.append("invalid registration entry")
    if (
        registrations["ModelTrainedThrough"].eq("")
        | registrations["ModelTrainedThrough"].ge(date)
    ).any():
        issues.append("registration model is not prior-session-only")
    if (
        registrations["ModelSourceSha256"]
        .fillna("")
        .astype(str)
        .str.len()
        .lt(16)
        .any()
    ):
        issues.append("registration model hash missing")

    contracts = sorted(outcomes["Contract"].dropna().astype(str).unique())
    required_contracts = set(expected_contracts or CONTRACTS)
    if set(contracts) != required_contracts:
        issues.append(
            "contract set mismatch "
            f"{len(contracts)}/{len(required_contracts)}"
        )
    expected_outcomes = {
        (event_id, contract)
        for event_id in actual_ids
        for contract in contracts
    }
    actual_outcomes = set(
        zip(outcomes.get("EventId", []), outcomes.get("Contract", []))
    )
    if not contracts:
        issues.append("contract outcomes missing")
    elif expected_outcomes != actual_outcomes:
        issues.append(
            "registration/outcome matrix incomplete "
            f"{len(actual_outcomes)}/{len(expected_outcomes)}"
        )
    if not outcomes.empty and (
        outcomes["EntryAt"].isna()
        | outcomes["ExitAt"].isna()
        | outcomes["ExitAt"].le(outcomes["EntryAt"])
        | outcomes["NetReturnPct"].isna()
    ).any():
        issues.append("invalid contract outcome")
    if not outcomes.empty and (
        outcomes["ModelTrainedThrough"].astype(str).str[:10].ge(date)
        | outcomes["ModelSourceSha256"]
        .fillna("")
        .astype(str)
        .str.len()
        .lt(16)
    ).any():
        issues.append("outcome model provenance invalid")
    return not issues, issues, contracts


def replay_policy(
    dates: list[str],
    registrations: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    contract: str,
    slots: int,
    score_threshold: float,
    capital: float,
    extra_cost_pct: float,
    max_trades_per_day: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = []
    daily_rows = []
    slot_capital = float(capital) / int(slots)
    outcome_map = {
        str(row.EventId): row
        for row in outcomes[
            outcomes["Contract"].astype(str).eq(str(contract))
        ].itertuples(index=False)
    }
    for date in dates:
        candidates = registrations[
            registrations["Date"].eq(date)
            & pd.to_numeric(
                registrations["ExpectedReturnPct"], errors="coerce"
            ).ge(float(score_threshold))
        ].sort_values(
            ["EntryAt", "ExpectedReturnPct", "Symbol"],
            ascending=[True, False, True],
        )
        open_exits: list[pd.Timestamp] = []
        traded_symbols: set[str] = set()
        day_trades = []
        for entry_time, batch in candidates.groupby(
            "EntryAt", sort=True, dropna=False
        ):
            open_exits = [
                exit_time
                for exit_time in open_exits
                if exit_time > entry_time
            ]
            available = int(slots) - len(open_exits)
            if available <= 0 or len(day_trades) >= int(max_trades_per_day):
                continue
            ranked = batch.sort_values(
                ["ExpectedReturnPct", "Symbol"],
                ascending=[False, True],
            )
            for row in ranked.itertuples(index=False):
                if (
                    available <= 0
                    or len(day_trades) >= int(max_trades_per_day)
                ):
                    break
                if row.Symbol in traded_symbols:
                    continue
                outcome = outcome_map.get(str(row.EventId))
                if outcome is None:
                    continue
                net_return = (
                    float(outcome.NetReturnPct) - float(extra_cost_pct)
                )
                pnl = slot_capital * net_return / 100.0
                trade = {
                    "date": date,
                    "contract": str(contract),
                    "slots": int(slots),
                    "score_threshold": float(score_threshold),
                    "event_id": str(row.EventId),
                    "symbol": str(row.Symbol),
                    "entry_at": row.EntryAt,
                    "exit_at": outcome.ExitAt,
                    "expected_return_pct": float(
                        row.ExpectedReturnPct
                    ),
                    "observed_net_return_pct": float(
                        outcome.NetReturnPct
                    ),
                    "extra_cost_pct": float(extra_cost_pct),
                    "stress_return_pct": round(net_return, 6),
                    "allocated_capital": round(slot_capital, 2),
                    "pnl": round(pnl, 2),
                }
                day_trades.append(trade)
                trades.append(trade)
                traded_symbols.add(str(row.Symbol))
                open_exits.append(outcome.ExitAt)
                available -= 1
        pnl = sum(float(row["pnl"]) for row in day_trades)
        daily_rows.append(
            {
                "date": date,
                "trades": len(day_trades),
                "pnl": round(pnl, 2),
                "daily_return_pct": round(
                    pnl * 100.0 / float(capital), 6
                ),
            }
        )
    return pd.DataFrame(daily_rows), pd.DataFrame(trades)


def build_assessment(
    predictions: pd.DataFrame,
    registrations: pd.DataFrame,
    outcomes: pd.DataFrame,
    coverage: pd.DataFrame,
    *,
    threshold: float,
    candidate_thresholds: Iterable[float] | None = None,
    capital: float,
    daily_goal: float,
    extra_cost_pct: float,
    selection_sessions: int,
    minimum_forward_sessions: int,
    minimum_forward_trade_sessions: int,
    minimum_forward_trades: int,
    max_trades_per_day: int,
    minimum_selection_trade_sessions: int = 6,
    minimum_selection_trades: int = 6,
    forward_start: str = FORWARD_START,
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    selection_thresholds = sorted(
        {
            float(value)
            for value in (candidate_thresholds or SCORE_THRESHOLDS)
            if float(value) >= float(threshold)
        }
    )
    if not selection_thresholds:
        raise ValueError(
            "candidate thresholds must include at least one value at or above "
            "the recording threshold"
        )
    eligible_dates = []
    if not coverage.empty and {
        "date", "full_session_eligible"
    }.issubset(coverage):
        eligible_dates = sorted(
            coverage.loc[
                coverage["full_session_eligible"].astype(bool)
                & coverage["date"].astype(str).ge(forward_start),
                "date",
            ].astype(str).tolist()
        )

    valid_dates = []
    contracts: set[str] = set()
    issues = []
    for date in eligible_dates:
        day_predictions = predictions[
            predictions["date"].eq(date)
        ].copy() if not predictions.empty else pd.DataFrame()
        day_registrations = registrations[
            registrations["Date"].eq(date)
        ].copy() if not registrations.empty else pd.DataFrame()
        day_outcomes = outcomes[
            outcomes["Date"].eq(date)
        ].copy() if not outcomes.empty else pd.DataFrame()
        valid, day_issues, day_contracts = valid_session_data(
            date,
            day_predictions,
            day_registrations,
            day_outcomes,
            threshold=threshold,
            expected_contracts=set(CONTRACTS),
        )
        if valid:
            valid_dates.append(date)
            contracts.update(day_contracts)
        else:
            issues.extend(f"{date}: {issue}" for issue in day_issues)

    selection_dates = valid_dates[: int(selection_sessions)]
    forward_dates = valid_dates[int(selection_sessions):]
    summary_rows = []
    replay_cache = {}
    for contract in sorted(contracts):
        for slots in SLOT_COUNTS:
            for score_threshold in selection_thresholds:
                selection_daily, selection_trades = replay_policy(
                    selection_dates,
                    registrations,
                    outcomes,
                    contract=contract,
                    slots=slots,
                    score_threshold=score_threshold,
                    capital=capital,
                    extra_cost_pct=extra_cost_pct,
                    max_trades_per_day=max_trades_per_day,
                )
                selection_metrics = metrics(selection_daily, daily_goal)
                key = (contract, slots, score_threshold)
                replay_cache[key] = (selection_daily, selection_trades)
                summary_rows.append(
                    {
                        "contract": contract,
                        "slots": slots,
                        "score_threshold": score_threshold,
                        **{
                            f"selection_{name}": value
                            for name, value in selection_metrics.items()
                        },
                    }
                )
    summary = pd.DataFrame(summary_rows)
    chosen = {}
    selection_daily = pd.DataFrame()
    selection_trades = pd.DataFrame()
    forward_daily = pd.DataFrame()
    forward_trades = pd.DataFrame()
    if (
        len(selection_dates) == int(selection_sessions)
        and not summary.empty
    ):
        eligible_summary = summary[
            pd.to_numeric(
                summary["selection_trade_sessions"], errors="coerce"
            ).fillna(0).ge(int(minimum_selection_trade_sessions))
            & pd.to_numeric(
                summary["selection_trades"], errors="coerce"
            ).fillna(0).ge(int(minimum_selection_trades))
        ]
        ranked = eligible_summary.sort_values(
            [
                "selection_avg_daily_pnl",
                "selection_lower_95_pct",
                "selection_block_floor_pct",
                "selection_trades",
                "contract",
                "slots",
                "score_threshold",
            ],
            ascending=[False, False, False, False, True, True, False],
            na_position="last",
        )
        if not ranked.empty:
            chosen = {
                key: (
                    value.item()
                    if hasattr(value, "item")
                    else value
                )
                for key, value in ranked.iloc[0].to_dict().items()
            }
            key = (
                str(chosen["contract"]),
                int(chosen["slots"]),
                float(chosen["score_threshold"]),
            )
            selection_daily, selection_trades = replay_cache[key]
            forward_daily, forward_trades = replay_policy(
                forward_dates,
                registrations,
                outcomes,
                contract=key[0],
                slots=key[1],
                score_threshold=key[2],
                capital=capital,
                extra_cost_pct=extra_cost_pct,
                max_trades_per_day=max_trades_per_day,
            )

    forward_metrics = metrics(forward_daily, daily_goal)
    accepted = bool(
        len(selection_dates) == int(selection_sessions)
        and forward_metrics["sessions"] >= int(minimum_forward_sessions)
        and forward_metrics["trade_sessions"]
        >= int(minimum_forward_trade_sessions)
        and forward_metrics["trades"] >= int(minimum_forward_trades)
        and forward_metrics["avg_daily_pnl"] >= float(daily_goal)
        and forward_metrics["goal_hit_pct"] >= 50.0
        and forward_metrics["lower_95_pct"] is not None
        and float(forward_metrics["lower_95_pct"]) > 0.0
        and forward_metrics["block_floor_pct"] is not None
        and float(forward_metrics["block_floor_pct"]) > 0.0
        and not issues
    )
    if len(selection_dates) < int(selection_sessions):
        status_name = "collecting_contract_selection_sessions"
    elif not chosen:
        status_name = "selection_rejected_insufficient_activity"
    elif accepted:
        status_name = "accepted_forward_strategy"
    else:
        status_name = "collecting_untouched_forward_sessions"
    status = {
        "status": status_name,
        "evidence": "prior_session_ranker_all_predictions_exact_contracts",
        "forward_start": forward_start,
        "selection_uses_forward_period": False,
        "threshold": float(threshold),
        "recording_floor": float(threshold),
        "candidate_thresholds": selection_thresholds,
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "extra_cost_pct": float(extra_cost_pct),
        "contracts": int(len(contracts)),
        "slot_counts": list(SLOT_COUNTS),
        "valid_sessions": int(len(valid_dates)),
        "selection_sessions": int(len(selection_dates)),
        "selection_sessions_required": int(selection_sessions),
        "minimum_selection_trade_sessions": int(
            minimum_selection_trade_sessions
        ),
        "minimum_selection_trades": int(minimum_selection_trades),
        "forward": forward_metrics,
        "minimum_forward_sessions": int(minimum_forward_sessions),
        "minimum_forward_trade_sessions": int(
            minimum_forward_trade_sessions
        ),
        "minimum_forward_trades": int(minimum_forward_trades),
        "max_trades_per_day": int(max_trades_per_day),
        "selected_policy": chosen,
        "issues": issues,
        "accepted": accepted,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    combined_daily = pd.concat(
        [
            selection_daily.assign(period="selection"),
            forward_daily.assign(period="forward"),
        ],
        ignore_index=True,
    )
    combined_trades = pd.concat(
        [
            selection_trades.assign(period="selection"),
            forward_trades.assign(period="forward"),
        ],
        ignore_index=True,
    )
    return status, summary, combined_daily, combined_trades


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prediction-glob", nargs="+", required=True)
    parser.add_argument("--registration-glob", nargs="+", required=True)
    parser.add_argument("--outcome-glob", nargs="+", required=True)
    parser.add_argument("--raw-glob", nargs="+", required=True)
    parser.add_argument(
        "--output-dir",
        default="results/Data/frozen_ranker_prediction_strategy",
    )
    parser.add_argument("--threshold", type=float, default=0.20)
    parser.add_argument(
        "--candidate-thresholds",
        nargs="+",
        type=float,
        default=list(SCORE_THRESHOLDS),
    )
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--extra-cost-pct", type=float, default=0.10)
    parser.add_argument("--selection-sessions", type=int, default=12)
    parser.add_argument(
        "--minimum-selection-trade-sessions", type=int, default=6
    )
    parser.add_argument("--minimum-selection-trades", type=int, default=6)
    parser.add_argument("--minimum-forward-sessions", type=int, default=20)
    parser.add_argument(
        "--minimum-forward-trade-sessions", type=int, default=10
    )
    parser.add_argument("--minimum-forward-trades", type=int, default=30)
    parser.add_argument("--max-trades-per-day", type=int, default=10)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()

    predictions = load_predictions(args.prediction_glob)
    registrations = load_registrations(args.registration_glob)
    outcomes = load_outcomes(args.outcome_glob)
    coverage = raw_session_coverage(
        args.raw_glob,
        min_snapshots=args.min_snapshots,
        latest_minute=15 * 60 + 10,
        max_gap_seconds=45.0,
    )
    status, summary, daily, trades = build_assessment(
        predictions,
        registrations,
        outcomes,
        coverage,
        threshold=args.threshold,
        candidate_thresholds=args.candidate_thresholds,
        capital=args.capital,
        daily_goal=args.daily_goal,
        extra_cost_pct=args.extra_cost_pct,
        selection_sessions=args.selection_sessions,
        minimum_selection_trade_sessions=(
            args.minimum_selection_trade_sessions
        ),
        minimum_selection_trades=args.minimum_selection_trades,
        minimum_forward_sessions=args.minimum_forward_sessions,
        minimum_forward_trade_sessions=(
            args.minimum_forward_trade_sessions
        ),
        minimum_forward_trades=args.minimum_forward_trades,
        max_trades_per_day=args.max_trades_per_day,
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(output / "session_coverage.csv", index=False)
    summary.to_csv(output / "selection_summary.csv", index=False)
    daily.to_csv(output / "daily.csv", index=False)
    trades.to_csv(output / "trades.csv", index=False)
    (output / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

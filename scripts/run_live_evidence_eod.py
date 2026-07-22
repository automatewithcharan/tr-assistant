#!/usr/bin/env python3
"""Backfill and label one day of live feature evidence after market close.

This job is read-only with respect to the broker. It fetches historical minute
candles only for symbols present in today's live recorder, labels first-state
evidence with next-minute executable outcomes, and writes an explicit coverage
gate. It never creates alerts or orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_live_feature_outcomes import (
    boolean_series,
    dedupe_first_evidence_state,
    evaluate_rows_from_minutes,
    load_lane_decisions,
    load_live_features,
    summarize,
)
from scripts.evaluate_live_microstructure_tape import load_minute_candles
from scripts.kite_intraday_backfill import (
    build_client,
    cache_path,
    fetch_symbol,
    kite_session_status,
    mapped_records,
    read_cache,
    save_cache,
)


DEFAULT_RECORDER_DIR = "results/Data/live_feature_recorder"
DEFAULT_OUTPUT_DIR = "results/Data/live_feature_recorder/eod"
PROMOTION_FEATURE_BUCKETS = {
    "missed_stable_executable_known_sector",
}


def _local_naive(values: pd.Series) -> pd.Series:
    raw = values.astype(str).str.replace(r"(Z|[+-]\d{2}:?\d{2})$", "", regex=True)
    return pd.to_datetime(raw, errors="coerce")


def minute_day_coverage(frame: pd.DataFrame, date: str) -> dict[str, Any]:
    if frame is None or frame.empty or "datetime" not in frame.columns:
        return {
            "rows": 0,
            "first_bar": "",
            "last_bar": "",
            "complete": False,
        }
    timestamps = _local_naive(frame["datetime"]).dropna().sort_values()
    timestamps = timestamps[timestamps.dt.date.astype(str).eq(str(date))]
    if timestamps.empty:
        return {
            "rows": 0,
            "first_bar": "",
            "last_bar": "",
            "complete": False,
        }
    first = pd.Timestamp(timestamps.iloc[0])
    last = pd.Timestamp(timestamps.iloc[-1])
    complete = bool(
        len(timestamps) >= 360
        and (first.hour * 60 + first.minute) <= 9 * 60 + 16
        and (last.hour * 60 + last.minute) >= 15 * 60 + 28
    )
    return {
        "rows": int(len(timestamps)),
        "first_bar": str(first),
        "last_bar": str(last),
        "complete": complete,
    }


def load_daily_evidence(
    recorder_dir: str,
    date: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = Path(recorder_dir)
    features = load_live_features(str(root / f"live_features_{date}.csv"))
    decisions = load_lane_decisions(str(root / f"lane_decisions_{date}.csv"))
    if not features.empty:
        features = features[features["Date"].eq(str(date))].reset_index(drop=True)
    if not decisions.empty:
        decisions = decisions[decisions["Date"].eq(str(date))].reset_index(drop=True)
    return features, decisions


def evidence_symbols(features: pd.DataFrame, decisions: pd.DataFrame) -> list[str]:
    symbols = set()
    for frame in (features, decisions):
        if frame is not None and not frame.empty and "Symbol" in frame.columns:
            symbols.update(frame["Symbol"].astype(str).str.strip().str.upper().tolist())
    return sorted(symbol for symbol in symbols if symbol and symbol != "NAN")


def backfill_evidence_minutes(
    symbols: list[str],
    date: str,
    sleep_seconds: float = 0.45,
    force: bool = False,
    client=None,
) -> tuple[pd.DataFrame, Path]:
    minute_dir = cache_path("__probe__", "minute", date, date).parent
    if not symbols:
        return pd.DataFrame(), minute_dir
    client = client or build_client()
    records = mapped_records(symbols, refresh_instruments=False, client=client)
    record_map = {
        str(record.get("symbol", "")).strip().upper(): record
        for record in records
    }
    audit_rows = []
    for index, symbol in enumerate(symbols, start=1):
        record = record_map.get(symbol, {})
        if record.get("status") != "mapped" or not record.get("instrument_token"):
            audit_rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "status": "unmapped",
                    "rows": 0,
                    "first_bar": "",
                    "last_bar": "",
                    "complete": False,
                    "error": str(record.get("reason", "instrument not mapped")),
                }
            )
            continue
        existing = read_cache(symbol, "minute", date, date)
        coverage = minute_day_coverage(existing, date)
        fetch_error = ""
        if force or not coverage["complete"]:
            fetched, fetch_error = fetch_symbol(
                client,
                int(record["instrument_token"]),
                symbol,
                "minute",
                date,
                date,
                chunk_days=1,
                sleep=float(sleep_seconds),
                max_chunks=1,
                existing=existing,
            )
            if not fetched.empty:
                save_cache(fetched, symbol, "minute", date, date)
                existing = fetched
            coverage = minute_day_coverage(existing, date)
        status = "complete" if coverage["complete"] else (
            "fetch_error" if fetch_error else "incomplete"
        )
        audit_rows.append(
            {
                "date": date,
                "symbol": symbol,
                "status": status,
                **coverage,
                "error": fetch_error,
            }
        )
        if index % 25 == 0:
            complete = sum(bool(row["complete"]) for row in audit_rows)
            print(
                f"minute_backfill={index}/{len(symbols)} complete={complete}",
                flush=True,
            )
    return pd.DataFrame(audit_rows), minute_dir


def evaluate_stream(
    frame: pd.DataFrame,
    minute_dir: str | Path,
    output_prefix: str | Path,
    horizon_minutes: int,
    fee_pct: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prefix = Path(output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    if frame is None or frame.empty:
        outcomes = pd.DataFrame()
        summary = pd.DataFrame()
    else:
        frozen = dedupe_first_evidence_state(frame)
        groups = load_minute_candles(
            str(minute_dir),
            frozen["Symbol"].astype(str).unique().tolist(),
        )
        outcomes = evaluate_rows_from_minutes(
            frozen,
            groups,
            horizon_minutes=horizon_minutes,
            fee_pct=fee_pct,
            slippage_bps=slippage_bps,
        )
        summary = summarize(outcomes)
    outcomes.to_csv(prefix.with_name(prefix.name + "_rows.csv"), index=False)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    return outcomes, summary


def _coverage(outcomes: pd.DataFrame) -> tuple[int, int, int, float]:
    total = int(len(outcomes))
    eligible = (
        int(boolean_series(outcomes["EvaluationEligible"]).sum())
        if total and "EvaluationEligible" in outcomes.columns
        else total
    )
    evaluated = (
        int(boolean_series(outcomes["OutcomeAvailable"]).sum())
        if total and "OutcomeAvailable" in outcomes.columns
        else 0
    )
    pct = evaluated / eligible * 100.0 if eligible else 0.0
    return total, eligible, evaluated, round(pct, 2)


def _promotion_coverage(outcomes: pd.DataFrame) -> tuple[int, int, float]:
    """Measure outcome coverage only for states that could reach promotion.

    Broad feature snapshots and rejected lane decisions remain in the overall
    diagnostic denominator. Sparse, untradeable symbols in those diagnostics
    must not make a fully evaluated candidate day look operationally broken.
    """
    if outcomes is None or outcomes.empty:
        return 0, 0, 0.0
    eligible = (
        boolean_series(outcomes["EvaluationEligible"])
        if "EvaluationEligible" in outcomes.columns
        else pd.Series(True, index=outcomes.index, dtype=bool)
    )
    buckets = outcomes.get(
        "FeatureBucket", pd.Series("", index=outcomes.index, dtype=str)
    ).fillna("").astype(str)
    sources = outcomes.get(
        "EvaluationSource", pd.Series("", index=outcomes.index, dtype=str)
    ).fillna("").astype(str).str.lower()
    decisions = outcomes.get(
        "Decision", pd.Series("", index=outcomes.index, dtype=str)
    ).fillna("").astype(str).str.lower()
    bucket_decisions = buckets.str.split("|").str[2].fillna("").str.lower()
    candidate = (
        (sources.eq("live_feature") & buckets.isin(PROMOTION_FEATURE_BUCKETS))
        | (
            sources.eq("lane_decision")
            & (decisions.eq("pass") | bucket_decisions.eq("pass"))
        )
    )
    candidate_eligible = eligible & candidate
    candidate_rows = int(candidate_eligible.sum())
    available = (
        boolean_series(outcomes["OutcomeAvailable"])
        if "OutcomeAvailable" in outcomes.columns
        else pd.Series(False, index=outcomes.index, dtype=bool)
    )
    evaluated = int((candidate_eligible & available).sum())
    coverage = evaluated / candidate_rows * 100.0 if candidate_rows else 0.0
    return candidate_rows, evaluated, round(coverage, 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        default=datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"),
    )
    parser.add_argument("--recorder-dir", default=DEFAULT_RECORDER_DIR)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--minute-data-dir", default="")
    parser.add_argument("--skip-backfill", action="store_true")
    parser.add_argument("--force-backfill", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.45)
    parser.add_argument("--horizon-minutes", type=int, default=60)
    parser.add_argument("--fee-pct", type=float, default=0.10)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--min-coverage-pct", type=float, default=95.0)
    args = parser.parse_args()

    date = str(args.date)
    output_root = Path(args.output_dir) / date
    output_root.mkdir(parents=True, exist_ok=True)
    features, decisions = load_daily_evidence(args.recorder_dir, date)
    symbols = evidence_symbols(features, decisions)
    if not symbols:
        status = {
            "date": date,
            "status": "no_evidence_rows",
            "symbols": 0,
            "evidence_accepted": False,
            "real_orders_allowed": False,
        }
        (output_root / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        print(f"No live evidence rows for {date}")
        return 0

    audit = pd.DataFrame()
    if args.skip_backfill:
        if not args.minute_data_dir:
            raise ValueError("--minute-data-dir is required with --skip-backfill")
        minute_dir = Path(args.minute_data_dir)
    else:
        kite_ok, kite_reason = kite_session_status()
        if not kite_ok:
            status = {
                "date": date,
                "status": "kite_unavailable",
                "reason": kite_reason,
                "symbols": len(symbols),
                "evidence_accepted": False,
                "real_orders_allowed": False,
            }
            (output_root / "status.json").write_text(
                json.dumps(status, indent=2), encoding="utf-8"
            )
            print(f"Kite unavailable: {kite_reason}")
            return 2
        audit, minute_dir = backfill_evidence_minutes(
            symbols,
            date,
            sleep_seconds=args.sleep_seconds,
            force=args.force_backfill,
        )
        audit.to_csv(output_root / "minute_coverage.csv", index=False)

    feature_outcomes, feature_summary = evaluate_stream(
        features,
        minute_dir,
        output_root / "live_features",
        args.horizon_minutes,
        args.fee_pct,
        args.slippage_bps,
    )
    decision_outcomes, decision_summary = evaluate_stream(
        decisions,
        minute_dir,
        output_root / "lane_decisions",
        args.horizon_minutes,
        args.fee_pct,
        args.slippage_bps,
    )
    feature_total, feature_eligible, feature_evaluated, feature_coverage = _coverage(feature_outcomes)
    decision_total, decision_eligible, decision_evaluated, decision_coverage = _coverage(decision_outcomes)
    combined_total = feature_eligible + decision_eligible
    combined_evaluated = feature_evaluated + decision_evaluated
    combined_coverage = round(
        combined_evaluated / combined_total * 100.0 if combined_total else 0.0,
        2,
    )
    feature_promotion_eligible, feature_promotion_evaluated, _ = _promotion_coverage(
        feature_outcomes
    )
    decision_promotion_eligible, decision_promotion_evaluated, _ = _promotion_coverage(
        decision_outcomes
    )
    promotion_eligible = feature_promotion_eligible + decision_promotion_eligible
    promotion_evaluated = feature_promotion_evaluated + decision_promotion_evaluated
    promotion_coverage = round(
        promotion_evaluated / promotion_eligible * 100.0
        if promotion_eligible
        else 0.0,
        2,
    )
    gate_passed = bool(
        promotion_eligible == 0
        or promotion_coverage >= float(args.min_coverage_pct)
    )
    status_name = (
        "complete_no_promotion_candidates"
        if promotion_eligible == 0
        else ("complete" if gate_passed else "coverage_gate_failed")
    )
    status = {
        "date": date,
        "status": status_name,
        "raw_feature_rows": int(len(features)),
        "raw_decision_rows": int(len(decisions)),
        "symbols": len(symbols),
        "minute_complete_symbols": (
            int(boolean_series(audit["complete"]).sum())
            if not audit.empty and "complete" in audit.columns
            else None
        ),
        "feature_rows": feature_total,
        "feature_eligible": feature_eligible,
        "feature_evaluated": feature_evaluated,
        "feature_coverage_pct": feature_coverage,
        "decision_rows": decision_total,
        "decision_eligible": decision_eligible,
        "decision_evaluated": decision_evaluated,
        "decision_coverage_pct": decision_coverage,
        "combined_coverage_pct": combined_coverage,
        "coverage_gate_scope": "promotion_eligible_candidates",
        "promotion_eligible": promotion_eligible,
        "promotion_evaluated": promotion_evaluated,
        "promotion_coverage_pct": promotion_coverage,
        "coverage_gate_passed": gate_passed,
        "horizon_minutes": int(args.horizon_minutes),
        "fee_pct": float(args.fee_pct),
        "slippage_bps": float(args.slippage_bps),
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    (output_root / "status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    print(json.dumps(status, indent=2))
    if not feature_summary.empty:
        print("LIVE FEATURE SUMMARY")
        print(feature_summary.head(10).to_string(index=False))
    if not decision_summary.empty:
        print("LANE DECISION SUMMARY")
        print(decision_summary.head(10).to_string(index=False))
    return 0 if gate_passed else 3


if __name__ == "__main__":
    raise SystemExit(main())

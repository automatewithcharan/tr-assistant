#!/usr/bin/env python3
"""Bootstrap several recorded live-evidence sessions with one Kite fetch/symbol."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_live_feature_outcomes import summarize
from scripts.run_live_evidence_eod import (
    evidence_symbols,
    evaluate_stream,
    load_daily_evidence,
    minute_day_coverage,
)
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
DEFAULT_OUTPUT_DIR = "results/Data/live_feature_recorder/bootstrap"
DATE_PATTERN = re.compile(r"(?:live_features|lane_decisions)_(\d{4}-\d{2}-\d{2})\.csv$")


def discover_evidence_dates(
    recorder_dir: str,
    start_date: str,
    end_date: str,
) -> list[str]:
    dates = set()
    for path in Path(recorder_dir).glob("*.csv"):
        match = DATE_PATTERN.search(path.name)
        if not match:
            continue
        date = match.group(1)
        if str(start_date) <= date <= str(end_date):
            dates.add(date)
    return sorted(dates)


def load_evidence_range(
    recorder_dir: str,
    dates: list[str],
) -> tuple[dict[str, tuple[pd.DataFrame, pd.DataFrame]], dict[str, set[str]]]:
    frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    required_dates: dict[str, set[str]] = defaultdict(set)
    for date in dates:
        features, decisions = load_daily_evidence(recorder_dir, date)
        frames[date] = (features, decisions)
        for symbol in evidence_symbols(features, decisions):
            required_dates[symbol].add(date)
    return frames, dict(required_dates)


def backfill_range_minutes(
    required_dates: dict[str, set[str]],
    start_date: str,
    end_date: str,
    sleep_seconds: float = 0.45,
    force: bool = False,
    client=None,
) -> tuple[pd.DataFrame, Path]:
    minute_dir = cache_path("__probe__", "minute", start_date, end_date).parent
    symbols = sorted(required_dates)
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
        dates = sorted(required_dates[symbol])
        if record.get("status") != "mapped" or not record.get("instrument_token"):
            for date in dates:
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

        existing = read_cache(symbol, "minute", start_date, end_date)
        coverage_by_date = {
            date: minute_day_coverage(existing, date) for date in dates
        }
        fetch_error = ""
        if force or not all(value["complete"] for value in coverage_by_date.values()):
            fetched, fetch_error = fetch_symbol(
                client,
                int(record["instrument_token"]),
                symbol,
                "minute",
                start_date,
                end_date,
                chunk_days=max(1, (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days + 1),
                sleep=float(sleep_seconds),
                max_chunks=1,
                existing=existing,
            )
            if not fetched.empty:
                save_cache(fetched, symbol, "minute", start_date, end_date)
                existing = fetched
            coverage_by_date = {
                date: minute_day_coverage(existing, date) for date in dates
            }
        for date, coverage in coverage_by_date.items():
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
                f"range_minute_backfill={index}/{len(symbols)} "
                f"complete_symbol_dates={complete}",
                flush=True,
            )
    return pd.DataFrame(audit_rows), minute_dir


def combine_outputs(
    paths: list[Path],
    rows_path: Path,
    summary_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frames = []
    for path in paths:
        if path.exists() and path.stat().st_size > 0:
            try:
                frame = pd.read_csv(path)
            except Exception:
                continue
            if not frame.empty:
                frames.append(frame)
    outcomes = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    summary = summarize(outcomes) if not outcomes.empty else pd.DataFrame()
    outcomes.to_csv(rows_path, index=False)
    summary.to_csv(summary_path, index=False)
    return outcomes, summary


def _coverage(outcomes: pd.DataFrame) -> tuple[int, int, int, float]:
    total = int(len(outcomes))
    eligible = (
        int(outcomes["EvaluationEligible"].fillna(False).astype(bool).sum())
        if total and "EvaluationEligible" in outcomes.columns
        else total
    )
    evaluated = (
        int(outcomes["OutcomeAvailable"].fillna(False).astype(bool).sum())
        if total and "OutcomeAvailable" in outcomes.columns
        else 0
    )
    coverage = evaluated / eligible * 100.0 if eligible else 0.0
    return total, eligible, evaluated, round(coverage, 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
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

    dates = discover_evidence_dates(
        args.recorder_dir,
        args.start_date,
        args.end_date,
    )
    output_root = Path(args.output_dir) / f"{args.start_date}_{args.end_date}"
    output_root.mkdir(parents=True, exist_ok=True)
    if not dates:
        raise RuntimeError("no evidence dates found in requested range")
    frames, required_dates = load_evidence_range(args.recorder_dir, dates)
    if not required_dates:
        raise RuntimeError("no evidence symbols found in requested range")

    audit = pd.DataFrame()
    if args.skip_backfill:
        if not args.minute_data_dir:
            raise ValueError("--minute-data-dir is required with --skip-backfill")
        minute_dir = Path(args.minute_data_dir)
    else:
        kite_ok, kite_reason = kite_session_status()
        if not kite_ok:
            raise RuntimeError(f"Kite unavailable: {kite_reason}")
        audit, minute_dir = backfill_range_minutes(
            required_dates,
            args.start_date,
            args.end_date,
            sleep_seconds=args.sleep_seconds,
            force=args.force_backfill,
        )
        audit.to_csv(output_root / "minute_coverage.csv", index=False)

    feature_paths = []
    decision_paths = []
    for date in dates:
        features, decisions = frames[date]
        date_root = output_root / date
        feature_outcomes, _ = evaluate_stream(
            features,
            minute_dir,
            date_root / "live_features",
            args.horizon_minutes,
            args.fee_pct,
            args.slippage_bps,
        )
        decision_outcomes, _ = evaluate_stream(
            decisions,
            minute_dir,
            date_root / "lane_decisions",
            args.horizon_minutes,
            args.fee_pct,
            args.slippage_bps,
        )
        if not feature_outcomes.empty:
            feature_paths.append(date_root / "live_features_rows.csv")
        if not decision_outcomes.empty:
            decision_paths.append(date_root / "lane_decisions_rows.csv")

    feature_outcomes, feature_summary = combine_outputs(
        feature_paths,
        output_root / "live_features_rows.csv",
        output_root / "live_features_summary.csv",
    )
    decision_outcomes, decision_summary = combine_outputs(
        decision_paths,
        output_root / "lane_decisions_rows.csv",
        output_root / "lane_decisions_summary.csv",
    )
    feature_total, feature_eligible, feature_evaluated, feature_coverage = _coverage(
        feature_outcomes
    )
    decision_total, decision_eligible, decision_evaluated, decision_coverage = _coverage(
        decision_outcomes
    )
    combined_eligible = feature_eligible + decision_eligible
    combined_evaluated = feature_evaluated + decision_evaluated
    combined_coverage = round(
        combined_evaluated / combined_eligible * 100.0 if combined_eligible else 0.0,
        2,
    )
    gate_passed = bool(
        combined_eligible > 0 and combined_coverage >= float(args.min_coverage_pct)
    )
    status = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "evidence_dates": dates,
        "symbols": len(required_dates),
        "complete_symbol_dates": (
            int(audit["complete"].fillna(False).astype(bool).sum())
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
        print("COMBINED LIVE FEATURE SUMMARY")
        print(feature_summary.head(20).to_string(index=False))
    if not decision_summary.empty:
        print("COMBINED LANE DECISION SUMMARY")
        print(decision_summary.head(20).to_string(index=False))
    return 0 if gate_passed else 3


if __name__ == "__main__":
    raise SystemExit(main())

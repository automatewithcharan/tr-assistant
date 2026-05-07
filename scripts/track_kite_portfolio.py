#!/usr/bin/env python3
"""Snapshot and reconcile the live Kite portfolio.

This script is meant for a manual-Kite workflow:
- verify the official Kite session is usable
- pull current broker-side holdings/positions
- optionally reconcile missing sells back into the local tracker
- export a fresh CSV/JSON snapshot for review
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from PKDevTools.classes import Archiver

from pkscreener.classes.KiteConnectManager import (
    KiteConnectSessionManager,
    format_connection_report,
)
from pkscreener.classes.KiteOrderManager import KiteOrderManager


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rows_from_exposure(exposure: Dict[str, Dict[str, object]]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for symbol, payload in sorted(exposure.items()):
        qty = int(payload.get("qty", 0) or 0)
        avg_price = float(payload.get("avg_price", 0) or 0)
        last_price = float(payload.get("last_price", 0) or 0)
        pnl = float(payload.get("pnl", 0) or 0)
        value = round(last_price * qty, 2) if qty and last_price else 0.0
        rows.append(
            {
                "CapturedAt": _now_str(),
                "Symbol": symbol,
                "Qty": qty,
                "AvgPrice": round(avg_price, 2),
                "LastPrice": round(last_price, 2),
                "MarketValue": value,
                "PnL": round(pnl, 2),
                "Product": str(payload.get("product", "") or ""),
                "Source": str(payload.get("source", "") or ""),
            }
        )
    return rows


def _write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    fieldnames = [
        "CapturedAt",
        "Symbol",
        "Qty",
        "AvgPrice",
        "LastPrice",
        "MarketValue",
        "PnL",
        "Product",
        "Source",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Track and reconcile the live Kite portfolio.")
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Reconcile broker holdings/positions against the local tracked Kite portfolio.",
    )
    parser.add_argument(
        "--print-report",
        action="store_true",
        help="Print the broker snapshot rows in a simple table.",
    )
    args = parser.parse_args()

    session_mgr = KiteConnectSessionManager()
    health = session_mgr.health_report()
    print(format_connection_report(health))
    if health.get("error"):
        return 1

    output_dir = Path(Archiver.get_user_outputs_dir())
    tracker = KiteOrderManager(output_dir=str(output_dir))
    exposure = tracker.get_broker_exposure()
    rows = _rows_from_exposure(exposure)

    latest_csv = output_dir / "kite_broker_portfolio_latest.csv"
    latest_json = output_dir / "kite_broker_portfolio_latest.json"
    dated_stub = datetime.now().strftime("%Y%m%d_%H%M%S")
    dated_csv = output_dir / f"kite_broker_portfolio_{dated_stub}.csv"
    dated_json = output_dir / f"kite_broker_portfolio_{dated_stub}.json"

    _write_csv(latest_csv, rows)
    _write_csv(dated_csv, rows)
    latest_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    dated_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    print(f"\nBroker positions captured: {len(rows)}")
    print(f"CSV:  {latest_csv}")
    print(f"JSON: {latest_json}")

    if args.print_report and rows:
        print("")
        for row in rows:
            print(
                f"{row['Symbol']:12} qty={row['Qty']:>4} "
                f"avg=₹{row['AvgPrice']:<8} last=₹{row['LastPrice']:<8} "
                f"pnl=₹{row['PnL']:<10} {row['Product']}/{row['Source']}"
            )

    if args.reconcile:
        report = tracker.reconcile_with_broker()
        print("\nReconcile report:")
        print(json.dumps(report, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Audit historical V2 Anomaly hits against the 3% target.

Reads every signalmonitor_all_alerts_*.csv snapshot under results/Data/,
keeps the first-detection per (symbol, date) for any row whose NowAction
contains "Anomaly" or "Runner", then cross-references the intraday tick
file's day-high to compute whether the anomaly would have hit +3% from
the detection price before EOD.

Outputs a clean per-detection CSV and a one-line summary.
"""

from __future__ import annotations

import csv
import glob
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "results", "Data"
)
DATA_DIR = os.path.abspath(DATA_DIR)
OUT_PATH = os.path.join(DATA_DIR, "anomaly_3pct_audit.csv")
DAY_CACHE_DIR = os.path.join(DATA_DIR, "kite_history_cache", "day")


def load_day_bar(symbol: str, date: str) -> Optional[Dict[str, float]]:
    """Return {open, high, low, close} for the given symbol on the given date,
    or None if not found. date format: YYYY-MM-DD."""
    fp = os.path.join(DAY_CACHE_DIR, f"{symbol}.csv")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp) as f:
            for row in csv.DictReader(f):
                dt = (row.get("datetime") or "")[:10]
                if dt == date:
                    try:
                        return {
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                        }
                    except (KeyError, ValueError):
                        return None
    except Exception:
        return None
    return None


def parse_anomaly_rows(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    try:
        with open(path) as f:
            for r in csv.DictReader(f):
                action = (r.get("NowAction") or "")
                if "Anomaly" in action or "Runner" in action or "Pre-Move" in action:
                    rows.append(r)
    except Exception:
        pass
    return rows


def main() -> int:
    files = sorted(glob.glob(os.path.join(DATA_DIR, "signalmonitor_all_alerts_*.csv")))
    # Keep first detection per (symbol, date)
    seen: Dict[Tuple[str, str], Dict[str, str]] = {}
    for fp in files:
        for r in parse_anomaly_rows(fp):
            when = (r.get("When") or "").strip()
            sym = (r.get("Symbol") or "").strip().upper()
            if not when or not sym:
                continue
            date = when[:10]
            key = (sym, date)
            if key not in seen:
                seen[key] = r

    if not seen:
        print("[!] No V2 Anomaly detections found in alert history.")
        return 1

    print(f"[i] Aggregated {len(seen)} unique anomaly detections from {len(files)} alert files.")
    print(f"[i] Resolving same-day OHLC from {DAY_CACHE_DIR}")

    # Write per-detection audit
    fieldnames = [
        "Date", "When", "Symbol", "Tier", "Action",
        "DetectionPrice", "TargetPrice_3pct",
        "DayOpen", "DayHigh", "DayLow", "DayClose",
        "MaxFavExcursionPct", "MaxAdvExcursionPct",
        "Hit3pct", "WouldHave8pct", "EODReturnPct", "ChangePct", "Checks",
    ]
    counts = {"total": 0, "with_ohlc": 0, "hit3": 0, "hit8": 0}
    eod_returns: List[float] = []
    mfe_returns: List[float] = []

    with open(OUT_PATH, "w", newline="") as out:
        w = csv.DictWriter(out, fieldnames=fieldnames)
        w.writeheader()
        for (sym, date), r in sorted(seen.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            try:
                price = float(r.get("PriceNow") or 0)
            except ValueError:
                price = 0.0
            action = r.get("NowAction") or ""
            tier = "?"
            m = re.search(r"Grade-([AB])", action)
            if m:
                tier = m.group(1)
            elif "Pre-Move" in action:
                tier = "C"

            row_out = {
                "Date": date,
                "When": r.get("When") or "",
                "Symbol": sym,
                "Tier": tier,
                "Action": action,
                "DetectionPrice": price,
                "TargetPrice_3pct": round(price * 1.03, 2) if price else "",
                "ChangePct": r.get("ChangePct") or "",
                "Checks": r.get("Checks") or "",
                "DayOpen": "", "DayHigh": "", "DayLow": "", "DayClose": "",
                "MaxFavExcursionPct": "", "MaxAdvExcursionPct": "",
                "Hit3pct": "UNKNOWN", "WouldHave8pct": "UNKNOWN", "EODReturnPct": "",
            }

            counts["total"] += 1
            data = load_day_bar(sym, date) if price > 0 else None
            if data and price > 0:
                counts["with_ohlc"] += 1
                high = data["high"]; low = data["low"]; close = data["close"]; o = data["open"]
                mfe_pct = (high - price) / price * 100.0
                mae_pct = (low - price) / price * 100.0
                eod_pct = (close - price) / price * 100.0
                hit3 = mfe_pct >= 3.0
                hit8 = mfe_pct >= 8.0
                if hit3:
                    counts["hit3"] += 1
                if hit8:
                    counts["hit8"] += 1
                eod_returns.append(eod_pct)
                mfe_returns.append(mfe_pct)
                row_out.update({
                    "DayOpen": o, "DayHigh": high, "DayLow": low, "DayClose": close,
                    "MaxFavExcursionPct": round(mfe_pct, 2),
                    "MaxAdvExcursionPct": round(mae_pct, 2),
                    "Hit3pct": "YES" if hit3 else "NO",
                    "WouldHave8pct": "YES" if hit8 else "NO",
                    "EODReturnPct": round(eod_pct, 2),
                })
            w.writerow(row_out)

    # Summary
    print(f"\n[OUT] {OUT_PATH}")
    print(f"\n=== ANOMALY 3% AUDIT SUMMARY ===")
    print(f"Total unique detections: {counts['total']}")
    print(f"With same-day OHLC available: {counts['with_ohlc']}")
    if counts["with_ohlc"]:
        n = counts["with_ohlc"]
        print(f"Hit +3% before EOD:   {counts['hit3']:3d} / {n} = {counts['hit3']/n*100:.1f}%")
        print(f"Hit +8% before EOD:   {counts['hit8']:3d} / {n} = {counts['hit8']/n*100:.1f}%")
        print(f"Avg max-fav-excursion: {sum(mfe_returns)/n:+.2f}%")
        print(f"Avg EOD return:        {sum(eod_returns)/n:+.2f}%")
        print(f"Median MFE:            {sorted(mfe_returns)[n//2]:+.2f}%")
        print(f"Median EOD:            {sorted(eod_returns)[n//2]:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

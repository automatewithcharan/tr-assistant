"""One-time seed: build anomaly_history.csv from existing alert snapshots.

Reads every signalmonitor_all_alerts_*.csv, keeps the first detection per
(symbol, date) where NowAction contains 'Anomaly'/'Runner'/'Pre-Move', and
writes results/Data/anomaly_history.csv with the same schema the live
SignalMonitor persistence path now uses.

Idempotent — re-running merges with any rows already in the file.
"""

from __future__ import annotations

import csv
import glob
import os
from typing import Dict, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "results", "Data")
OUT = os.path.join(DATA_DIR, "anomaly_history.csv")

FIELDS = [
    "Date", "When", "Symbol", "Tier", "Action", "PriceNow",
    "ChangePct", "BuyAbove", "SellIfBelow", "TakeProfit1", "TakeProfit2",
    "SuggestedQty", "QualityScore", "Regime", "Checks", "Why",
]


def tier_from_action(action: str) -> str:
    if "Grade-A" in action: return "A"
    if "Grade-B" in action: return "B"
    if "Pre-Move" in action: return "C"
    return "?"


def main() -> int:
    seen: Dict[Tuple[str, str], dict] = {}

    if os.path.exists(OUT):
        with open(OUT) as f:
            for r in csv.DictReader(f):
                key = ((r.get("Symbol") or "").strip().upper(), (r.get("Date") or "")[:10])
                if all(key):
                    seen[key] = r

    files = sorted(glob.glob(os.path.join(DATA_DIR, "signalmonitor_all_alerts_*.csv")))
    new_count = 0
    for fp in files:
        try:
            with open(fp) as f:
                for r in csv.DictReader(f):
                    action = r.get("NowAction") or ""
                    if not any(t in action for t in ("Anomaly", "Runner", "Pre-Move")):
                        continue
                    when = (r.get("When") or "").strip()
                    sym = (r.get("Symbol") or "").strip().upper()
                    date = when[:10]
                    if not sym or not date:
                        continue
                    key = (sym, date)
                    if key in seen:
                        continue
                    seen[key] = {
                        "Date": date, "When": when, "Symbol": sym,
                        "Tier": tier_from_action(action),
                        "Action": action,
                        "PriceNow": r.get("PriceNow", ""),
                        "ChangePct": r.get("ChangePct", ""),
                        "BuyAbove": r.get("BuyAbove", ""),
                        "SellIfBelow": r.get("SellIfBelow", ""),
                        "TakeProfit1": r.get("TakeProfit1", ""),
                        "TakeProfit2": r.get("TakeProfit2", ""),
                        "SuggestedQty": r.get("SuggestedQty", ""),
                        "QualityScore": r.get("SignalStrengthPct", ""),
                        "Regime": "",
                        "Checks": r.get("Checks", ""),
                        "Why": r.get("Why", ""),
                    }
                    new_count += 1
        except Exception as exc:
            print(f"[skip] {fp}: {exc}")

    rows = sorted(seen.values(), key=lambda r: (r["Date"], r["Symbol"]))
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})

    print(f"[+] wrote {len(rows)} unique detections to {OUT}")
    print(f"    ({new_count} added this run)")
    by_tier = {}
    for r in rows:
        by_tier[r["Tier"]] = by_tier.get(r["Tier"], 0) + 1
    print(f"    by tier: {by_tier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

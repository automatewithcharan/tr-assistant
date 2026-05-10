"""Enrich anomaly_history.csv with same-day outcome columns.

For each row in anomaly_history.csv, looks up that date's daily OHLC bar
from kite_history_cache/day/<SYMBOL>.csv and computes:

  DayOpen, DayHigh, DayLow, DayClose
  MaxFavExcursionPct  = (DayHigh - PriceNow) / PriceNow * 100
  MaxAdvExcursionPct  = (DayLow  - PriceNow) / PriceNow * 100
  EODReturnPct        = (DayClose - PriceNow) / PriceNow * 100
  Hit3pct             = YES if MaxFavExcursionPct >= 3 else NO
  Hit8pct             = YES if MaxFavExcursionPct >= 8 else NO

Designed to be safe to re-run after EOD — it re-writes the CSV with the
new columns appended, preserving all original detection columns.

If a row's Symbol/Date is not in the day cache yet, outcome columns are
left blank so the next run (after Kite refreshes the cache) fills them.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "results", "Data")
HISTORY = os.path.join(DATA_DIR, "anomaly_history.csv")
DAY_CACHE = os.path.join(DATA_DIR, "kite_history_cache", "day")

OUTCOME_FIELDS = [
    "DayOpen", "DayHigh", "DayLow", "DayClose",
    "MaxFavExcursionPct", "MaxAdvExcursionPct", "EODReturnPct",
    "Hit3pct", "Hit8pct",
]


def load_day_bar(symbol: str, date: str) -> Optional[Dict[str, float]]:
    fp = os.path.join(DAY_CACHE, f"{symbol}.csv")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp) as f:
            for row in csv.DictReader(f):
                if (row.get("datetime") or "")[:10] == date:
                    return {
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    }
    except Exception:
        return None
    return None


def main() -> int:
    if not os.path.exists(HISTORY):
        print(f"[!] {HISTORY} not found — run seed_anomaly_history.py first.")
        return 1

    with open(HISTORY) as f:
        reader = csv.DictReader(f)
        base_fields = list(reader.fieldnames or [])
        rows = list(reader)

    fieldnames = base_fields + [f for f in OUTCOME_FIELDS if f not in base_fields]

    enriched = 0
    skipped = 0
    for r in rows:
        # Skip if already enriched with valid numbers
        already = bool(r.get("DayHigh")) and bool(r.get("MaxFavExcursionPct"))
        if already:
            continue
        sym = (r.get("Symbol") or "").strip().upper()
        date = (r.get("Date") or "")[:10]
        try:
            price = float(r.get("PriceNow") or 0)
        except ValueError:
            price = 0.0
        if not sym or not date or price <= 0:
            skipped += 1
            continue
        bar = load_day_bar(sym, date)
        if bar is None:
            skipped += 1
            continue
        mfe = (bar["high"] - price) / price * 100.0
        mae = (bar["low"] - price) / price * 100.0
        eod = (bar["close"] - price) / price * 100.0
        r["DayOpen"] = bar["open"]
        r["DayHigh"] = bar["high"]
        r["DayLow"] = bar["low"]
        r["DayClose"] = bar["close"]
        r["MaxFavExcursionPct"] = round(mfe, 2)
        r["MaxAdvExcursionPct"] = round(mae, 2)
        r["EODReturnPct"] = round(eod, 2)
        r["Hit3pct"] = "YES" if mfe >= 3.0 else "NO"
        r["Hit8pct"] = "YES" if mfe >= 8.0 else "NO"
        enriched += 1

    with open(HISTORY, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"[+] enriched {enriched} rows, {skipped} still pending OHLC")

    # Summary
    matched = [r for r in rows if r.get("Hit3pct") in ("YES", "NO")]
    if matched:
        from collections import defaultdict
        by_tier = defaultdict(list)
        for r in matched:
            by_tier[r.get("Tier", "?")].append(r)

        print("\n=== Outcome summary by tier ===")
        for t in sorted(by_tier):
            rs = by_tier[t]
            n = len(rs)
            hit3 = sum(1 for r in rs if r["Hit3pct"] == "YES")
            hit8 = sum(1 for r in rs if r["Hit8pct"] == "YES")
            mfes = [float(r["MaxFavExcursionPct"]) for r in rs]
            maes = [float(r["MaxAdvExcursionPct"]) for r in rs]
            eods = [float(r["EODReturnPct"]) for r in rs]
            print(f"  Tier {t}: n={n:3d}  hit3%={hit3}/{n}={hit3/n*100:5.1f}%  hit8%={hit8}/{n}={hit8/n*100:5.1f}%  "
                  f"avgMFE={sum(mfes)/n:+5.2f}%  avgMAE={sum(maes)/n:+5.2f}%  avgEOD={sum(eods)/n:+5.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

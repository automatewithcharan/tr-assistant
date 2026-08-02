"""Enrich anomaly_history.csv with same-day forward outcome columns.

For each row in anomaly_history.csv, looks up same-day 5-minute bars from the
alert timestamp onward and computes:

  DayOpen, DayHigh, DayLow, DayClose
  MaxFavExcursionPct  = (post-alert high - PriceNow) / PriceNow * 100
  MaxAdvExcursionPct  = (post-alert low  - PriceNow) / PriceNow * 100
  EODReturnPct        = (post-alert close - PriceNow) / PriceNow * 100
  Hit3pct             = YES if MaxFavExcursionPct >= 3 else NO
  Hit8pct             = YES if MaxFavExcursionPct >= 8 else NO

Designed to be safe to re-run after EOD — it re-writes the CSV with the
new columns appended, preserving all original detection columns.

If intraday data is not available it can fall back to daily OHLC, but that
fallback is less accurate for stop/target testing because it cannot know
whether the day's high/low occurred before or after the alert.
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime
from functools import lru_cache
from glob import glob
from typing import Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "results", "Data")
HISTORY = os.path.join(DATA_DIR, "anomaly_history.csv")
DAY_CACHE = os.path.join(DATA_DIR, "kite_history_cache", "day")
INTRADAY_CACHE = os.path.join(DATA_DIR, "kite_history_backfill", "5minute")
LIVE_INTRADAY_CACHE = os.path.join(DATA_DIR, "kite_history_backfill", "minute")

OUTCOME_FIELDS = [
    "DayOpen", "DayHigh", "DayLow", "DayClose",
    "MaxFavExcursionPct", "MaxAdvExcursionPct", "EODReturnPct",
    "Hit3pct", "Hit8pct",
    "OutcomeSource", "OutcomeStart",
]


def parse_dt(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


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


@lru_cache(maxsize=None)
def intraday_files(symbol: str, intraday_cache: str) -> tuple[str, ...]:
    return tuple(
        sorted(glob(os.path.join(intraday_cache, "*", f"{symbol}.csv")), reverse=True)
    )


def load_forward_intraday_bar(symbol: str, alert_when: str, intraday_cache: str) -> Optional[Dict[str, float]]:
    alert_dt = parse_dt(alert_when)
    if alert_dt is None:
        return None
    date = alert_dt.date().isoformat()
    for fp in intraday_files(symbol, intraday_cache):
        try:
            rows = []
            with open(fp) as f:
                for row in csv.DictReader(f):
                    row_dt = parse_dt(row.get("datetime", ""))
                    if row_dt is None or row_dt.date().isoformat() != date or row_dt < alert_dt:
                        continue
                    rows.append(
                        {
                            "datetime": row_dt,
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                        }
                    )
            if not rows:
                continue
            return {
                "open": rows[0]["open"],
                "high": max(r["high"] for r in rows),
                "low": min(r["low"] for r in rows),
                "close": rows[-1]["close"],
                "source": os.path.relpath(fp, DATA_DIR),
                "start": rows[0]["datetime"].isoformat(sep=" "),
            }
        except Exception:
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich anomaly_history.csv with post-alert intraday outcomes")
    parser.add_argument("--input", default=HISTORY)
    parser.add_argument(
        "--intraday-cache",
        action="append",
        default=[],
        help=(
            "intraday cache root; may be repeated. Defaults to the live minute "
            "cache followed by the historical 5-minute cache"
        ),
    )
    parser.add_argument(
        "--output",
        default="",
        help="write enriched rows here instead of replacing --input",
    )
    parser.add_argument("--force", action="store_true", help="recompute existing outcome columns")
    parser.add_argument("--no-daily-fallback", action="store_true", help="leave rows blank when post-alert intraday bars are missing")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"[!] {args.input} not found — run seed_anomaly_history.py first.")
        return 1

    with open(args.input) as f:
        reader = csv.DictReader(f)
        base_fields = list(reader.fieldnames or [])
        rows = list(reader)

    fieldnames = base_fields + [f for f in OUTCOME_FIELDS if f not in base_fields]

    enriched = 0
    skipped = 0
    intraday = 0
    daily_fallback = 0
    for r in rows:
        # Skip if already enriched with valid numbers
        already = bool(r.get("DayHigh")) and bool(r.get("MaxFavExcursionPct"))
        if already and not args.force:
            continue
        sym = (r.get("Symbol") or "").strip().upper()
        date = (r.get("Date") or "")[:10]
        when = (r.get("When") or "").strip()
        try:
            price = float(r.get("PriceNow") or 0)
        except ValueError:
            price = 0.0
        if not sym or not date or price <= 0:
            skipped += 1
            continue
        cache_roots = args.intraday_cache or [LIVE_INTRADAY_CACHE, INTRADAY_CACHE]
        bar = None
        for cache_root in cache_roots:
            bar = load_forward_intraday_bar(sym, when, cache_root)
            if bar is not None:
                break
        if bar is not None:
            intraday += 1
        elif not args.no_daily_fallback:
            bar = load_day_bar(sym, date)
            if bar is not None:
                bar["source"] = "kite_history_cache/day"
                bar["start"] = date
                daily_fallback += 1
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
        r["OutcomeSource"] = bar.get("source", "")
        r["OutcomeStart"] = bar.get("start", "")
        enriched += 1

    output_path = args.output or args.input
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(
        f"[+] enriched {enriched} rows "
        f"({intraday} post-alert intraday, {daily_fallback} daily fallback), "
        f"{skipped} still pending OHLC; saved {output_path}"
    )

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

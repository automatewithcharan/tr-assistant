"""Backfill daily OHLC for symbols flagged by V2 Anomaly but missing from
the local kite_history_cache/day/ folder.

Reads results/Data/anomaly_3pct_audit.csv (or anomaly_history.csv if present),
collects every (Symbol, Date) pair, and uses KiteHistoricalDataProvider to
fetch+cache 1 year of daily candles for each unique symbol that's missing.

Run after a fresh Kite session is connected. Idempotent — symbols already
cached are skipped.
"""

from __future__ import annotations

import csv
import os
import sys
from typing import Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider  # noqa: E402

DATA_DIR = os.path.join(ROOT, "results", "Data")
DAY_CACHE = os.path.join(DATA_DIR, "kite_history_cache", "day")
AUDIT_CSV = os.path.join(DATA_DIR, "anomaly_3pct_audit.csv")
HISTORY_CSV = os.path.join(DATA_DIR, "anomaly_history.csv")


def collect_symbols() -> Set[str]:
    syms: Set[str] = set()
    for path in (HISTORY_CSV, AUDIT_CSV):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for r in csv.DictReader(f):
                s = (r.get("Symbol") or "").strip().upper()
                if s:
                    syms.add(s)
    return syms


def main() -> int:
    symbols = collect_symbols()
    if not symbols:
        print("[!] No anomaly symbols found in audit/history files.")
        return 1

    missing = [s for s in sorted(symbols) if not os.path.exists(os.path.join(DAY_CACHE, f"{s}.csv"))]
    print(f"[i] {len(symbols)} unique symbols across audit + history")
    print(f"[i] Already cached: {len(symbols) - len(missing)}")
    print(f"[i] Need fetch:     {len(missing)}")
    if not missing:
        print("[+] Nothing to backfill.")
        return 0

    print(f"[i] Missing: {', '.join(missing)}")
    provider = KiteHistoricalDataProvider()

    ok, fail = 0, 0
    for sym in missing:
        try:
            df = provider.get_stock_data(sym, interval="day", count=365)
            if df is not None and not df.empty:
                ok += 1
                print(f"  [+] {sym}: cached {len(df)} bars")
            else:
                fail += 1
                print(f"  [-] {sym}: no data returned")
        except Exception as exc:
            fail += 1
            print(f"  [-] {sym}: {exc}")

    print(f"\n[done] fetched={ok}  failed={fail}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

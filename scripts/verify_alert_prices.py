"""Verify alert prices against Kite 1-minute bars.

Reads the Discord alerts export, extracts alerts in a time window, fetches
the 1-minute OHLC bar from Kite for each, and reports whether the alert
price falls inside that minute's high-low range.

Usage:
    python3 scripts/verify_alert_prices.py 12:00 14:30
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider
import pandas as pd
from datetime import datetime as _dt, timedelta as _td


class FreshFetcher:
    """Bypass the cache — pull fresh minute candles from Kite each time."""
    def __init__(self):
        self._inner = KiteHistoricalDataProvider()
        self._symbol_cache = {}

    def get(self, symbol):
        sym = symbol.strip().upper()
        if sym in self._symbol_cache:
            return self._symbol_cache[sym]
        token = self._inner._get_instrument_token(sym)
        if token is None:
            self._symbol_cache[sym] = None
            return None
        client = self._inner._get_client()
        try:
            today_open = _dt.now().replace(hour=9, minute=0, second=0, microsecond=0)
            today_close = _dt.now().replace(hour=15, minute=45, second=0, microsecond=0)
            candles = client.historical_data(
                token,
                from_date=today_open,
                to_date=today_close,
                interval="minute",
            )
        except Exception as exc:
            print(f"  fetch failed for {sym}: {exc}")
            self._symbol_cache[sym] = None
            return None
        if not candles:
            self._symbol_cache[sym] = None
            return None
        df = pd.DataFrame(candles)
        df["datetime"] = pd.to_datetime(df["date"])
        df = df.set_index("datetime").sort_index()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        self._symbol_cache[sym] = df
        return df

ALERTS_FILE = REPO_ROOT / "Screener Alerts - Text Channels - general [1488777695750914060].txt"
TODAY = "2026-05-07"

# Each alert block has a "Time `YYYY-MM-DD HH:MM:SS`" line plus per-symbol blocks like:
#   🟢 **Buy Now** `KROSS` — Strength `54.9%`
#   Price `220.05` ...
ALERT_TIME_RE = re.compile(r"Time `(" + re.escape(TODAY) + r" \d{2}:\d{2}:\d{2})`")
SYMBOL_RE = re.compile(r"\*\*[^*]+\*\*\s*`([A-Z0-9_&-]+)`")
PRICE_RE = re.compile(r"Price\s*`([\d.]+)`")


def parse_alerts(start_hhmm: str, end_hhmm: str):
    text = ALERTS_FILE.read_text(encoding="utf-8", errors="replace")
    blocks = text.split("[07-05-2026")  # split on Discord per-message markers
    start = datetime.strptime(f"{TODAY} {start_hhmm}:00", "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(f"{TODAY} {end_hhmm}:00", "%Y-%m-%d %H:%M:%S")
    rows = []
    for block in blocks:
        time_match = ALERT_TIME_RE.search(block)
        if not time_match:
            continue
        ts = datetime.strptime(time_match.group(1), "%Y-%m-%d %H:%M:%S")
        if not (start <= ts <= end):
            continue
        # split into per-symbol mini-blocks separated by blank line
        for chunk in block.split("\n\n"):
            sym_match = SYMBOL_RE.search(chunk)
            price_match = PRICE_RE.search(chunk)
            if not sym_match or not price_match:
                continue
            rows.append({
                "alert_time": ts,
                "symbol": sym_match.group(1),
                "alert_price": float(price_match.group(1)),
            })
    return rows


def verify(rows):
    provider = FreshFetcher()
    print(f"\nVerifying {len(rows)} alerts against Kite 1-minute bars...\n")
    print(f"{'Time':<10} {'Symbol':<14} {'Alert ₹':>9}  {'Bar Low':>9}  {'Bar High':>9}  {'Bar Close':>9}  Verdict")
    print("-" * 95)
    inside = outside = no_data = 0
    drift_pct = []
    for r in rows:
        ts = r["alert_time"]
        sym = r["symbol"]
        ap = r["alert_price"]
        try:
            df = provider.get(sym)
        except Exception as exc:
            print(f"{ts.strftime('%H:%M:%S')} {sym:<14} {ap:>9.2f}  ERROR fetch: {exc}")
            no_data += 1
            continue
        if df is None or df.empty:
            print(f"{ts.strftime('%H:%M:%S')} {sym:<14} {ap:>9.2f}  NO DATA")
            no_data += 1
            continue
        df = df.copy()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        # Find the bar whose minute matches the alert time (truncate alert to minute).
        bar_time = ts.replace(second=0, microsecond=0)
        matches = df[df.index == bar_time]
        if matches.empty:
            # closest within +/- 1 min
            window = df[(df.index >= bar_time - timedelta(minutes=1)) &
                        (df.index <= bar_time + timedelta(minutes=1))]
            if window.empty:
                print(f"{ts.strftime('%H:%M:%S')} {sym:<14} {ap:>9.2f}  NO BAR @ {bar_time}")
                no_data += 1
                continue
            bar = window.iloc[len(window) // 2]
        else:
            bar = matches.iloc[0]
        lo, hi, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        if lo <= ap <= hi:
            verdict = "INSIDE BAR (fresh)"
            inside += 1
        else:
            distance = (ap - hi) if ap > hi else (lo - ap)
            pct = distance / ((hi + lo) / 2) * 100
            drift_pct.append(pct)
            verdict = f"OUTSIDE BAR by {pct:.2f}%"
            outside += 1
        print(f"{ts.strftime('%H:%M:%S')} {sym:<14} {ap:>9.2f}  {lo:>9.2f}  {hi:>9.2f}  {close:>9.2f}  {verdict}")

    print("-" * 95)
    total = inside + outside + no_data
    print(f"\nTotal: {total} | Inside (fresh): {inside} | Outside (stale): {outside} | No data: {no_data}")
    if drift_pct:
        avg = sum(drift_pct) / len(drift_pct)
        worst = max(drift_pct)
        print(f"Drift on stale alerts — avg: {avg:.2f}%, worst: {worst:.2f}%")


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "12:00"
    e = sys.argv[2] if len(sys.argv) > 2 else "14:30"
    rows = parse_alerts(s, e)
    print(f"Found {len(rows)} alerts in window {s}–{e} on {TODAY}")
    verify(rows)

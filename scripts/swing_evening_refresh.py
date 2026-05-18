#!/usr/bin/env python3
"""Light evening refresh — fetches today's daily bar for the 200-symbol
swing research universe (plus NIFTY 50) and appends to the live cache.

Runs at 4:00 PM IST Mon-Fri, after market close (3:30 PM) and before
the swing paper EOD cron at 4:30 PM. Without this, the EOD job would
have to wait until next morning's full prewarm to see today's bar.

Idempotent: skips a symbol if today's date is already in its CSV.
Read-only: only fetches and appends. No order placement.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager   # noqa: E402

RESULTS_DIR     = REPO_ROOT / "results" / "Data"
LIVE_CACHE_DIR  = RESULTS_DIR / "kite_history_cache" / "day"
UNIVERSE_REF_DIR = RESULTS_DIR / "kite_history_backfill" / "day" / "2024-05-13_2026-05-12"
INST_MAP_NSE    = RESULTS_DIR / "kite_instruments_nse_latest.csv"
INST_MAP_BSE    = RESULTS_DIR / "kite_instruments_bse_latest.csv"

NIFTY_SYMBOL    = "NIFTY 50"
NIFTY_TOKEN     = 256265   # canonical Kite token for NIFTY 50

KITE_RATE_SLEEP = 0.37     # ~2.7 req/sec, well under 3/sec limit


def ist_today() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(tz=timezone(timedelta(hours=5, minutes=30))).date())


def load_universe() -> list[str]:
    return sorted(f.stem for f in UNIVERSE_REF_DIR.glob("*.csv") if f.stem != NIFTY_SYMBOL)


def load_instrument_map() -> dict[str, int]:
    """symbol -> instrument_token, NSE-preferred."""
    parts = []
    for p in (INST_MAP_NSE, INST_MAP_BSE):
        if p.exists():
            df = pd.read_csv(p)
            df["exchange"] = df.get("exchange", p.stem.split("_")[-2].upper())
            parts.append(df)
    if not parts:
        return {}
    inst = pd.concat(parts, ignore_index=True)
    inst["tradingsymbol_norm"] = inst["tradingsymbol"].astype(str).str.strip().str.upper()
    # Prefer NSE EQ
    inst["_rank"] = (
        (inst["exchange"].astype(str).str.upper() == "NSE").astype(int) * 10
        + (inst.get("instrument_type", "").astype(str).str.upper() == "EQ").astype(int) * 5
    )
    inst = inst.sort_values(["tradingsymbol_norm", "_rank"], ascending=[True, False])
    inst = inst.drop_duplicates("tradingsymbol_norm", keep="first")
    return dict(zip(inst["tradingsymbol_norm"], inst["instrument_token"].astype(int)))


def already_has_today(csv_path: Path, today: pd.Timestamp) -> bool:
    if not csv_path.exists():
        return False
    try:
        df = pd.read_csv(csv_path)
        if df.empty or "datetime" not in df.columns:
            return False
        latest = pd.to_datetime(df["datetime"]).dt.normalize().max()
        return latest >= today
    except Exception:
        return False


def fetch_today_bar(client, token: int, today: pd.Timestamp, retries: int = 3) -> pd.DataFrame:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            candles = client.historical_data(
                token,
                from_date=today.to_pydatetime(),
                to_date=today.to_pydatetime(),
                interval="day",
            )
            return pd.DataFrame(candles or [])
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if "too many requests" in msg or "timed out" in msg or "connection" in msg:
                wait = min(20.0, 2.0 * (attempt + 1))
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    return pd.DataFrame()


def append_bar(csv_path: Path, candles: pd.DataFrame) -> bool:
    if candles is None or candles.empty:
        return False
    if "date" in candles.columns:
        candles = candles.rename(columns={"date": "datetime"})
    candles["datetime"] = pd.to_datetime(candles["datetime"])
    new_row = candles[["datetime", "open", "high", "low", "close", "volume"]].copy()
    # Format datetime to match live-cache format: "YYYY-MM-DD 00:00:00+05:30"
    new_row["datetime"] = new_row["datetime"].dt.tz_convert("Asia/Kolkata") \
        if new_row["datetime"].dt.tz is not None \
        else new_row["datetime"].dt.tz_localize("Asia/Kolkata")
    new_row["datetime"] = new_row["datetime"].dt.strftime("%Y-%m-%d 00:00:00+05:30")

    if csv_path.exists():
        existing = pd.read_csv(csv_path)
        combined = pd.concat([existing, new_row], ignore_index=True)
        combined["_dt_norm"] = pd.to_datetime(combined["datetime"]).dt.normalize()
        combined = combined.drop_duplicates("_dt_norm", keep="last").drop(columns=["_dt_norm"])
        combined = combined.sort_values("datetime").reset_index(drop=True)
        combined.to_csv(csv_path, index=False)
    else:
        new_row.to_csv(csv_path, index=False)
    return True


def main() -> int:
    today = ist_today()
    print(f"[swing_evening_refresh] today = {today.date()}", flush=True)

    universe = load_universe()
    print(f"  Universe size: {len(universe)} symbols + NIFTY", flush=True)

    inst_map = load_instrument_map()
    if not inst_map:
        print("ERROR: no instrument map cached. Run kite_historical_backfill first.", file=sys.stderr)
        return 1

    try:
        manager = KiteConnectSessionManager()
        client  = manager._build_client()
    except Exception as exc:
        print(f"ERROR: Kite session unavailable: {exc}", file=sys.stderr)
        return 2

    fetched, skipped, missing_token, errors = 0, 0, 0, 0
    targets = universe + [NIFTY_SYMBOL]

    for i, sym in enumerate(targets, 1):
        csv_path = LIVE_CACHE_DIR / f"{sym}.csv"
        if already_has_today(csv_path, today):
            skipped += 1
            continue

        token = NIFTY_TOKEN if sym == NIFTY_SYMBOL else inst_map.get(sym.upper())
        if token is None:
            print(f"  [{i:3d}/{len(targets)}] {sym}: no instrument token", flush=True)
            missing_token += 1
            continue

        try:
            candles = fetch_today_bar(client, token, today)
            if candles.empty:
                print(f"  [{i:3d}/{len(targets)}] {sym}: no candles for today", flush=True)
                errors += 1
            else:
                append_bar(csv_path, candles)
                fetched += 1
            time.sleep(KITE_RATE_SLEEP)
        except Exception as exc:
            print(f"  [{i:3d}/{len(targets)}] {sym}: ERROR {exc}", flush=True)
            errors += 1

    print(f"\nSummary: fetched={fetched} skipped(already_present)={skipped} "
          f"missing_token={missing_token} errors={errors}", flush=True)
    return 0 if (fetched > 0 or skipped > 0) else 3


if __name__ == "__main__":
    sys.exit(main())

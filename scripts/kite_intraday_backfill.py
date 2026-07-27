#!/usr/bin/env python3
"""Backfill and audit Kite intraday candles for the clean research universe."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.kite_historical_backfill import (  # noqa: E402
    BACKFILL_ROOT,
    INDEX_TOKENS,
    build_client,
    kite_session_status,
    load_instruments,
    map_symbols,
)

RESULTS_DIR = REPO_ROOT / "results" / "Data"
DEFAULT_UNIVERSE = RESULTS_DIR / "kite_backfill_2y_eligible_kite_complete_universe.csv"
INTRADAY_ROOT = BACKFILL_ROOT
SESSION_START = "09:15"
SESSION_END = "15:30"


def _normalise_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def load_backfill_universe(path: Path, limit: int = 0) -> list[str]:
    """Load the exact ordered live universe; zero means no limit."""
    frame = pd.read_csv(path)
    column = next((name for name in ("Symbol", "symbol", "SYMBOL", "Stock") if name in frame.columns), None)
    if column is None:
        column = frame.columns[0]
    symbols = []
    seen = set()
    for value in frame[column]:
        symbol = _normalise_symbol(value)
        if not symbol or symbol == "NAN" or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
    return symbols[: int(limit)] if int(limit or 0) > 0 else symbols


def _date_str(value: str | pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _to_local_naive(values) -> pd.Series:
    raw = pd.Series(values).astype(str).str.replace(r"(Z|[+-]\d{2}:?\d{2})$", "", regex=True)
    return pd.to_datetime(raw, errors="coerce")


def interval_minutes(interval: str) -> int:
    mapping = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}
    if interval not in mapping:
        raise ValueError(f"Unsupported intraday interval: {interval}")
    return mapping[interval]


def bars_per_day(interval: str) -> int:
    minutes = interval_minutes(interval)
    return int(pd.Timedelta("6h15min").total_seconds() // 60 // minutes)


def cache_path(symbol: str, interval: str, start: str, end: str) -> Path:
    path = INTRADAY_ROOT / interval / f"{_date_str(start)}_{_date_str(end)}"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{_normalise_symbol(symbol)}.csv"


def chunk_ranges(start: str, end: str, chunk_days: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    out = []
    cursor = pd.Timestamp(start).normalize()
    final = pd.Timestamp(end).normalize()
    while cursor <= final:
        chunk_end = min(final, cursor + pd.Timedelta(days=max(1, int(chunk_days)) - 1))
        out.append((cursor, chunk_end))
        cursor = chunk_end + pd.Timedelta(days=1)
    return out


def normalise_candles(candles, symbol: str) -> pd.DataFrame:
    df = pd.DataFrame(candles or [])
    if df.empty:
        return df
    if "date" in df.columns:
        df = df.rename(columns={"date": "datetime"})
    if "datetime" not in df.columns:
        return pd.DataFrame()
    df["datetime"] = _to_local_naive(df["datetime"])
    keep = ["datetime", "open", "high", "low", "close", "volume"]
    if not set(keep).issubset(df.columns):
        return pd.DataFrame()
    df = df[keep].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).sort_values("datetime")
    df["symbol"] = _normalise_symbol(symbol)
    return df.reset_index(drop=True)


def read_cache(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    path = cache_path(symbol, interval, start, end)
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if "date" in df.columns and "datetime" not in df.columns:
            df = df.rename(columns={"date": "datetime"})
        df["datetime"] = _to_local_naive(df["datetime"])
        if "symbol" not in df.columns:
            df["symbol"] = _normalise_symbol(symbol)
        return df.dropna(subset=["datetime"]).drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def save_cache(df: pd.DataFrame, symbol: str, interval: str, start: str, end: str) -> None:
    if df.empty:
        return
    out = df.copy()
    out["symbol"] = _normalise_symbol(symbol)
    out = out.drop_duplicates(subset=["datetime"]).sort_values("datetime")
    path = cache_path(symbol, interval, start, end)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        out.to_csv(temporary, index=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_chunk(client, token: int, start: pd.Timestamp, end: pd.Timestamp, interval: str, retries: int = 5) -> pd.DataFrame:
    last_exc: Optional[Exception] = None
    for attempt in range(int(retries) + 1):
        try:
            candles = client.historical_data(
                token,
                from_date=start.to_pydatetime(),
                to_date=(end + pd.Timedelta(hours=23, minutes=59)).to_pydatetime(),
                interval=interval,
            )
            return normalise_candles(candles, "")
        except Exception as exc:
            last_exc = exc
            message = str(exc).lower()
            retryable = "too many requests" in message or "timed out" in message or "connection" in message
            if attempt >= int(retries) or not retryable:
                raise
            wait = min(60.0, 2.0 * (attempt + 1) ** 2)
            print(f"  Kite intraday fetch throttled; retrying in {wait:.1f}s ({attempt + 1}/{retries})", flush=True)
            time.sleep(wait)
    if last_exc:
        raise last_exc
    return pd.DataFrame()


def chunk_is_complete(existing: pd.DataFrame, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> bool:
    if existing is None or existing.empty:
        return False
    work = existing.copy()
    work["datetime"] = _to_local_naive(work["datetime"])
    mask = (work["datetime"] >= start) & (work["datetime"] < end + pd.Timedelta(days=1))
    chunk = work[mask].copy()
    if chunk.empty:
        return False
    days = set(chunk["datetime"].dt.normalize())
    if not days:
        return False
    expected_rows = len(days) * bars_per_day(interval)
    return len(chunk) >= expected_rows * 0.85


def fetch_symbol(
    client,
    token: int,
    symbol: str,
    interval: str,
    start: str,
    end: str,
    chunk_days: int,
    sleep: float,
    max_chunks: int,
    existing: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, str]:
    frames = [existing] if existing is not None and not existing.empty else []
    errors = []
    chunks = chunk_ranges(start, end, chunk_days)
    if max_chunks > 0:
        chunks = chunks[-int(max_chunks):]
    for chunk_start, chunk_end in chunks:
        if chunk_is_complete(existing, interval, chunk_start, chunk_end):
            continue
        try:
            df = fetch_chunk(client, token, chunk_start, chunk_end, interval)
            if not df.empty:
                df["symbol"] = _normalise_symbol(symbol)
                frames.append(df)
        except Exception as exc:
            errors.append(f"{_date_str(chunk_start)}:{exc}")
        if sleep > 0:
            time.sleep(float(sleep))
    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not merged.empty:
        merged = merged.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    return merged, "; ".join(errors[:5])


COMPLETE_AUDIT_STATUSES = {"complete", "warning", "listing_limited"}


def audit_intraday(symbol: str, df: pd.DataFrame, interval: str, start: str, end: str, expected_days: Optional[set[pd.Timestamp]], fetch_error: str = "") -> dict:
    if df is None or df.empty:
        return {
            "symbol": symbol,
            "interval": interval,
            "status": "missing" if not fetch_error else "fetch_error",
            "rows": 0,
            "start": "",
            "end": "",
            "day_coverage_pct": 0.0,
            "bar_coverage_pct": 0.0,
            "available_day_coverage_pct": 0.0,
            "available_bar_coverage_pct": 0.0,
            "history_scope": "missing",
            "missing_days": 0,
            "duplicates": 0,
            "bad_ohlc": 0,
            "zero_volume_bars": 0,
            "issues": fetch_error or "no candles",
        }
    work = df.copy()
    work["datetime"] = _to_local_naive(work["datetime"])
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    work = work[(work["datetime"] >= pd.Timestamp(start)) & (work["datetime"] < pd.Timestamp(end) + pd.Timedelta(days=1))]
    work["date"] = work["datetime"].dt.normalize()
    dates = set(work["date"])
    required = {d for d in (expected_days or dates) if pd.Timestamp(start) <= d <= pd.Timestamp(end)}
    missing = sorted(required - dates) if required else []
    day_coverage = len(dates & required) / len(required) * 100.0 if required else 0.0
    expected_rows = max(1, len(required) * bars_per_day(interval))
    bar_coverage = len(work) / expected_rows * 100.0 if required else 0.0
    first_date = work["datetime"].min().normalize()
    available_required = {d for d in required if d >= first_date}
    available_missing = sorted(available_required - dates) if available_required else []
    available_day_coverage = (
        len(dates & available_required) / len(available_required) * 100.0
        if available_required
        else 0.0
    )
    available_expected_rows = max(1, len(available_required) * bars_per_day(interval))
    available_bar_coverage = len(work) / available_expected_rows * 100.0 if available_required else 0.0
    duplicates = int(work["datetime"].duplicated().sum())
    bad_ohlc = int(((work["high"] < work[["open", "close", "low"]].max(axis=1)) | (work["low"] > work[["open", "close", "high"]].min(axis=1))).sum())
    zero_volume = int((work["volume"].fillna(0) <= 0).sum())
    issues = []
    late_start = first_date > pd.Timestamp(start) + pd.Timedelta(days=10)
    early_end = work["datetime"].max().normalize() < pd.Timestamp(end) - pd.Timedelta(days=10)
    if late_start:
        issues.append("late start")
    if early_end:
        issues.append("early end")
    if missing:
        issues.append(f"missing_days={len(missing)}")
    if duplicates:
        issues.append(f"duplicates={duplicates}")
    if bad_ohlc:
        issues.append(f"bad_ohlc={bad_ohlc}")
    if zero_volume > max(10, len(work) * 0.10) and symbol not in INDEX_TOKENS:
        issues.append(f"zero_volume_bars={zero_volume}")
    if fetch_error:
        issues.append(f"fetch_error={fetch_error}")
    status = "complete" if not issues else "warning"
    if day_coverage < 95.0 or bar_coverage < 85.0 or "late start" in issues or "early end" in issues or fetch_error:
        status = "incomplete" if not fetch_error else "fetch_error"
    listing_limited = (
        late_start
        and not early_end
        and not fetch_error
        and len(available_required) >= 20
        and available_day_coverage >= 95.0
        and available_bar_coverage >= 85.0
        and len(available_missing) <= max(2, int(len(available_required) * 0.05))
    )
    if listing_limited:
        status = "listing_limited"
        issues.append(f"listing_limited_start={first_date.date()}")
    return {
        "symbol": symbol,
        "interval": interval,
        "status": status,
        "rows": int(len(work)),
        "start": str(work["datetime"].min()),
        "end": str(work["datetime"].max()),
        "day_coverage_pct": round(day_coverage, 2),
        "bar_coverage_pct": round(bar_coverage, 2),
        "available_day_coverage_pct": round(available_day_coverage, 2),
        "available_bar_coverage_pct": round(available_bar_coverage, 2),
        "history_scope": "listing_limited" if listing_limited else "requested_range",
        "missing_days": len(missing),
        "duplicates": duplicates,
        "bad_ohlc": bad_ohlc,
        "zero_volume_bars": zero_volume,
        "issues": "; ".join(issues),
    }


def mapped_records(symbols: Iterable[str], refresh_instruments: bool, client=None) -> list[dict]:
    instruments = load_instruments(client=client, refresh=refresh_instruments)
    mapping = map_symbols(["NIFTY 50", *symbols], instruments)
    return mapping.to_dict(orient="records")


def select_record_batch(records: list[dict], batch_size: int, batch_index: int) -> list[dict]:
    """Select a stable zero-based stock batch while always retaining NIFTY."""
    size = max(0, int(batch_size or 0))
    index = max(0, int(batch_index or 0))
    if size <= 0:
        return records
    benchmark = [rec for rec in records if _normalise_symbol(rec.get("symbol")) == "NIFTY 50"]
    stocks = [rec for rec in records if _normalise_symbol(rec.get("symbol")) != "NIFTY 50"]
    start = index * size
    return benchmark + stocks[start : start + size]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Kite 5m/15m intraday data and audit completeness")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--limit", type=int, default=178)
    parser.add_argument("--batch-size", type=int, default=0, help="Stable stock batch size; 0 processes the full selected universe.")
    parser.add_argument("--batch-index", type=int, default=0, help="Zero-based batch index used with --batch-size.")
    parser.add_argument(
        "--batch-output-index",
        type=int,
        default=None,
        help="Optional audit/output batch label independent of the selected input slice.",
    )
    parser.add_argument("--start", default="2024-05-13")
    parser.add_argument("--end", default="2026-05-12")
    parser.add_argument("--intervals", nargs="+", default=["5minute", "15minute"])
    parser.add_argument("--chunk-days", type=int, default=60)
    parser.add_argument("--max-chunks-per-symbol", type=int, default=0, help="0 means full requested period; positive value fetches only latest N chunks.")
    parser.add_argument("--min-symbol-complete-pct", type=float, default=95.0)
    parser.add_argument("--sleep", type=float, default=0.7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--refresh-instruments", action="store_true")
    parser.add_argument("--out-prefix", default=str(RESULTS_DIR / "kite_intraday_2y"))
    args = parser.parse_args()

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    kite_ok, kite_reason = kite_session_status()
    if not kite_ok and not args.dry_run:
        print(f"Kite unavailable: {kite_reason}")
        return 2
    client = build_client() if kite_ok and not args.dry_run else None
    symbols = load_backfill_universe(Path(args.universe), args.limit)
    records = mapped_records(symbols, bool(args.refresh_instruments), client=client)
    full_record_count = len(records)
    records = select_record_batch(records, args.batch_size, args.batch_index)
    if args.batch_size > 0 and len(records) <= 1:
        print(
            f"Requested batch {args.batch_index} is empty for {max(0, full_record_count - 1)} stocks "
            f"with batch size {args.batch_size}."
        )
        return 0
    output_batch_index = (
        args.batch_index
        if args.batch_output_index is None
        else max(0, int(args.batch_output_index))
    )
    batch_suffix = f"_batch_{output_batch_index:04d}" if args.batch_size > 0 else ""
    batch_prefix = out_prefix.with_name(out_prefix.name + batch_suffix)
    pd.DataFrame(records).to_csv(batch_prefix.with_name(batch_prefix.name + "_instrument_map.csv"), index=False)

    interval_gates = {}
    all_audits = []
    for interval in args.intervals:
        print(f"Auditing/backfilling {interval}", flush=True)
        expected_days = None
        interval_rows = []
        for idx, rec in enumerate(records, 1):
            symbol = _normalise_symbol(rec["symbol"])
            if rec.get("status") != "mapped" or not rec.get("instrument_token"):
                audit = audit_intraday(symbol, pd.DataFrame(), interval, args.start, args.end, expected_days, fetch_error=rec.get("reason", "unmapped"))
                interval_rows.append(audit)
                all_audits.append(audit)
                continue
            cached = read_cache(symbol, interval, args.start, args.end)
            pre_audit = audit_intraday(symbol, cached, interval, args.start, args.end, expected_days)
            should_fetch = not args.dry_run and (args.force or pre_audit["status"] not in COMPLETE_AUDIT_STATUSES)
            fetch_error = ""
            if should_fetch:
                fetched, fetch_error = fetch_symbol(
                    client,
                    int(rec["instrument_token"]),
                    symbol,
                    interval,
                    args.start,
                    args.end,
                    args.chunk_days,
                    args.sleep,
                    args.max_chunks_per_symbol,
                    existing=cached,
                )
                if not fetched.empty:
                    save_cache(fetched, symbol, interval, args.start, args.end)
                    cached = fetched
            audit = audit_intraday(symbol, cached, interval, args.start, args.end, expected_days, fetch_error=fetch_error)
            if symbol == "NIFTY 50" and not cached.empty:
                expected_days = set(_to_local_naive(cached["datetime"]).dt.normalize())
                audit = audit_intraday(symbol, cached, interval, args.start, args.end, expected_days, fetch_error=fetch_error)
            interval_rows.append(audit)
            all_audits.append(audit)
            if idx % 10 == 0:
                stock_rows = [r for r in interval_rows if r["symbol"] != "NIFTY 50"]
                complete = sum(1 for r in stock_rows if r["status"] in COMPLETE_AUDIT_STATUSES)
                print(f"  {interval}: audited {idx}/{len(records)}; complete-ish stocks={complete}", flush=True)

        interval_df = pd.DataFrame(interval_rows)
        interval_df.to_csv(batch_prefix.with_name(batch_prefix.name + f"_{interval}_audit.csv"), index=False)
        stock_df = interval_df[~interval_df["symbol"].eq("NIFTY 50")]
        complete_count = int(stock_df["status"].isin(COMPLETE_AUDIT_STATUSES).sum())
        complete_pct = complete_count / max(1, len(stock_df)) * 100.0
        nifty_complete = bool(interval_df[interval_df["symbol"].eq("NIFTY 50")]["status"].isin(COMPLETE_AUDIT_STATUSES).any())
        gate_pass = complete_pct >= float(args.min_symbol_complete_pct) and nifty_complete
        eligible = stock_df[stock_df["status"].isin(COMPLETE_AUDIT_STATUSES)]["symbol"].tolist()
        pd.DataFrame({"symbol": eligible}).to_csv(batch_prefix.with_name(batch_prefix.name + f"_{interval}_complete_universe.csv"), index=False)
        interval_gates[interval] = {
            "interval": interval,
            "start": _date_str(args.start),
            "end": _date_str(args.end),
            "selected_symbols": int(len(stock_df)),
            "complete_symbols": complete_count,
            "complete_pct": round(complete_pct, 2),
            "nifty_complete": nifty_complete,
            "gate_pass": gate_pass,
            "min_symbol_complete_pct": float(args.min_symbol_complete_pct),
            "listing_limited_symbols": stock_df[stock_df["status"].eq("listing_limited")]["symbol"].tolist(),
            "incomplete_symbols": stock_df[~stock_df["status"].isin(COMPLETE_AUDIT_STATUSES)]["symbol"].tolist(),
        }

    gate = {
        "kite_session_available": kite_ok,
        "kite_session_reason": kite_reason,
        "requested_start": _date_str(args.start),
        "requested_end": _date_str(args.end),
        "batch_size": int(args.batch_size),
        "batch_index": int(args.batch_index),
        "batch_output_index": int(output_batch_index),
        "full_selected_symbols": max(0, full_record_count - 1),
        "intervals": interval_gates,
        "all_gates_pass": all(v["gate_pass"] for v in interval_gates.values()),
    }
    pd.DataFrame(all_audits).to_csv(batch_prefix.with_name(batch_prefix.name + "_audit.csv"), index=False)
    batch_prefix.with_name(batch_prefix.name + "_quality_gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
    print(json.dumps(gate, indent=2))
    return 0 if gate["all_gates_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

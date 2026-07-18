#!/usr/bin/env python3
"""Backfill and audit Kite historical candles for strategy research.

The script is intentionally data-layer only. It maps symbols to Kite
instrument tokens, downloads missing ranges, caches by interval/date-range, and
produces a quality gate file. Strategy research should run in Kite-only mode
only after this gate passes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PKDevTools.classes import Archiver  # noqa: E402
from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager  # noqa: E402
from scripts.research_intraday_strategies_2y import DEFAULT_UNIVERSE, load_universe  # noqa: E402

RESULTS_DIR = REPO_ROOT / "results" / "Data"
BACKFILL_ROOT = RESULTS_DIR / "kite_history_backfill"
LEGACY_CACHE_ROOT = RESULTS_DIR / "kite_history_cache"

INDEX_TOKENS = {
    "NIFTY": 256265,
    "NIFTY 50": 256265,
    "^NSEI": 256265,
}


@dataclass(frozen=True)
class InstrumentMatch:
    symbol: str
    instrument_token: Optional[int]
    exchange: str
    status: str
    reason: str
    tradingsymbol: str = ""
    name: str = ""


def _normalise_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _date_str(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _to_naive_datetime(values) -> pd.Series:
    raw = pd.Series(values).astype(str).str.replace(r"(Z|[+-]\d{2}:?\d{2})$", "", regex=True)
    return pd.to_datetime(raw, errors="coerce")


def _normalise_daily_dates(values) -> pd.Series:
    dates = _to_naive_datetime(values)
    shifted = dates.dt.hour.eq(18) & dates.dt.minute.eq(30)
    if shifted.mean() > 0.8:
        dates = dates + pd.Timedelta(days=1)
    return dates.dt.normalize()


def _range_dir(interval: str, start: str, end: str) -> Path:
    path = BACKFILL_ROOT / interval / f"{_date_str(start)}_{_date_str(end)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(symbol: str, interval: str, start: str, end: str) -> Path:
    return _range_dir(interval, start, end) / f"{_normalise_symbol(symbol)}.csv"


def _legacy_cache_path(symbol: str, interval: str) -> Path:
    path = LEGACY_CACHE_ROOT / interval
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{_normalise_symbol(symbol)}.csv"


def kite_session_status() -> tuple[bool, str]:
    try:
        manager = KiteConnectSessionManager()
        token = manager._get_saved_access_token()
        if not token or not manager.api_key:
            return False, "Kite API key/access token not available"
        client = manager._build_client()
        try:
            profile = client.profile()
        except Exception as exc:
            return False, f"Kite access token rejected: {exc}"
        user_id = profile.get("user_id") if isinstance(profile, dict) else ""
        suffix = f" for {user_id}" if user_id else ""
        return True, f"Kite session validated via {manager.session_file_path}{suffix}"
    except Exception as exc:
        return False, f"Kite session unavailable: {exc}"


def build_client():
    manager = KiteConnectSessionManager()
    return manager._build_client()


def load_instruments(client=None, refresh: bool = False) -> pd.DataFrame:
    rows = []
    for exchange in ["NSE", "BSE"]:
        cache_path = RESULTS_DIR / f"kite_instruments_{exchange.lower()}_latest.csv"
        df = pd.DataFrame()
        if cache_path.exists() and not refresh:
            try:
                df = pd.read_csv(cache_path)
            except Exception:
                df = pd.DataFrame()
        if df.empty and client is not None:
            instruments = client.instruments(exchange)
            df = pd.DataFrame(instruments or [])
            if not df.empty:
                df.to_csv(cache_path, index=False)
        if not df.empty:
            rows.append(df)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def map_symbols(symbols: Iterable[str], instruments: pd.DataFrame) -> pd.DataFrame:
    matches = []
    if instruments is None or instruments.empty:
        for symbol in symbols:
            matches.append(InstrumentMatch(_normalise_symbol(symbol), None, "", "unmapped", "instrument dump unavailable"))
        return pd.DataFrame([m.__dict__ for m in matches])

    inst = instruments.copy()
    inst["tradingsymbol_norm"] = inst["tradingsymbol"].astype(str).str.strip().str.upper()
    inst["exchange_norm"] = inst.get("exchange", "").astype(str).str.strip().str.upper()
    inst["instrument_type_norm"] = inst.get("instrument_type", "").astype(str).str.strip().str.upper()
    inst["segment_norm"] = inst.get("segment", "").astype(str).str.strip().str.upper()

    for raw_symbol in symbols:
        symbol = _normalise_symbol(raw_symbol)
        if symbol in INDEX_TOKENS:
            matches.append(InstrumentMatch(symbol, INDEX_TOKENS[symbol], "NSE", "mapped", "known Kite index token", symbol, "NIFTY 50"))
            continue

        exact = inst[
            (inst["tradingsymbol_norm"] == symbol)
            & (inst["exchange_norm"] == "NSE")
            & (inst["instrument_type_norm"].isin(["EQ", ""]))
        ].copy()
        if exact.empty:
            exact = inst[(inst["tradingsymbol_norm"] == symbol) & (inst["exchange_norm"] == "NSE")].copy()
        if exact.empty:
            exact = inst[(inst["tradingsymbol_norm"] == symbol) & (inst["exchange_norm"] == "BSE")].copy()

        if exact.empty:
            matches.append(InstrumentMatch(symbol, None, "", "unmapped", "symbol not found in Kite instruments"))
            continue

        if len(exact) > 1:
            # Prefer NSE EQ when duplicates exist.
            exact["_rank"] = (
                (exact["exchange_norm"] == "NSE").astype(int) * 10
                + (exact["instrument_type_norm"] == "EQ").astype(int) * 5
                + (exact["segment_norm"] == "NSE").astype(int)
            )
            exact = exact.sort_values("_rank", ascending=False)

        rec = exact.iloc[0]
        matches.append(
            InstrumentMatch(
                symbol=symbol,
                instrument_token=int(rec["instrument_token"]),
                exchange=str(rec.get("exchange", "")),
                status="mapped",
                reason="exact tradingsymbol match",
                tradingsymbol=str(rec.get("tradingsymbol", "")),
                name=str(rec.get("name", "")),
            )
        )
    return pd.DataFrame([m.__dict__ for m in matches])


def normalise_kite_candles(candles, symbol: str) -> pd.DataFrame:
    df = pd.DataFrame(candles or [])
    if df.empty:
        return df
    if "date" in df.columns:
        df["datetime"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.drop(columns=["date"])
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    else:
        return pd.DataFrame()
    df["datetime"] = _normalise_daily_dates(df["datetime"])
    keep = ["datetime", "open", "high", "low", "close", "volume"]
    if not set(keep).issubset(df.columns):
        return pd.DataFrame()
    df = df[keep].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).sort_values("datetime")
    df["symbol"] = symbol
    return df.reset_index(drop=True)


def read_cache(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    for path in [_cache_path(symbol, interval, start, end), _legacy_cache_path(symbol, interval)]:
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
            if "datetime" not in df.columns and "date" in df.columns:
                df = df.rename(columns={"date": "datetime"})
            df["datetime"] = _normalise_daily_dates(df["datetime"]) if interval == "day" else _to_naive_datetime(df["datetime"])
            if "symbol" not in df.columns:
                df["symbol"] = _normalise_symbol(symbol)
            return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        except Exception:
            continue
    return pd.DataFrame()


def is_complete_enough(df: pd.DataFrame, start: str, end: str, expected_days: Optional[set[pd.Timestamp]] = None, min_coverage: float = 0.98) -> bool:
    if df is None or df.empty:
        return False
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    dates = set(pd.to_datetime(df["datetime"]).dt.normalize())
    if min(dates) > start_ts + pd.Timedelta(days=10):
        return False
    if max(dates) < end_ts - pd.Timedelta(days=10):
        return False
    if expected_days:
        required = {d for d in expected_days if start_ts <= d <= end_ts}
        if required:
            return len(dates & required) / len(required) >= min_coverage
    return len(df) >= 450


def fetch_range(client, token: int, start: str, end: str, interval: str, retries: int = 5) -> pd.DataFrame:
    last_exc: Optional[Exception] = None
    for attempt in range(int(retries) + 1):
        try:
            candles = client.historical_data(
                token,
                from_date=pd.Timestamp(start).to_pydatetime(),
                to_date=pd.Timestamp(end).to_pydatetime(),
                interval=interval,
            )
            return normalise_kite_candles(candles, "")
        except Exception as exc:
            last_exc = exc
            message = str(exc).lower()
            retryable = "too many requests" in message or "timed out" in message or "connection" in message
            if attempt >= int(retries) or not retryable:
                raise
            wait = min(45.0, 2.0 * (attempt + 1) ** 2)
            print(f"  Kite fetch throttled; retrying in {wait:.1f}s ({attempt + 1}/{retries})", flush=True)
            time.sleep(wait)
    if last_exc:
        raise last_exc
    return pd.DataFrame()


def audit_candles(symbol: str, df: pd.DataFrame, start: str, end: str, expected_days: Optional[set[pd.Timestamp]]) -> dict:
    if df is None or df.empty:
        return {
            "symbol": symbol,
            "status": "missing",
            "rows": 0,
            "start": "",
            "end": "",
            "coverage_pct": 0.0,
            "missing_days": 0,
            "duplicates": 0,
            "bad_ohlc": 0,
            "zero_volume": 0,
            "issues": "no candles",
        }

    work = df.copy()
    work["datetime"] = _normalise_daily_dates(work["datetime"])
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    dates = set(work["datetime"].dt.normalize())
    required = {d for d in (expected_days or set()) if pd.Timestamp(start) <= d <= pd.Timestamp(end)}
    missing = sorted(required - dates) if required else []
    if required:
        coverage = len(dates & required) / len(required) * 100.0
    else:
        coverage = 100.0 if len(dates) >= 450 else len(dates) / 450.0 * 100.0
    duplicates = int(work["datetime"].duplicated().sum())
    bad_ohlc = int(((work["high"] < work[["open", "close", "low"]].max(axis=1)) | (work["low"] > work[["open", "close", "high"]].min(axis=1))).sum())
    zero_volume = int((work["volume"].fillna(0) <= 0).sum())
    issues = []
    if work["datetime"].min() > pd.Timestamp(start) + pd.Timedelta(days=10):
        issues.append("late start")
    if work["datetime"].max() < pd.Timestamp(end) - pd.Timedelta(days=10):
        issues.append("early end")
    if missing:
        issues.append(f"missing trading days={len(missing)}")
    if duplicates:
        issues.append(f"duplicates={duplicates}")
    if bad_ohlc:
        issues.append(f"bad_ohlc={bad_ohlc}")
    if zero_volume > max(5, len(work) * 0.05) and symbol not in INDEX_TOKENS:
        issues.append(f"zero_volume={zero_volume}")
    jumps = work["close"].pct_change().abs()
    if int((jumps > 0.35).sum()) > 0:
        issues.append("possible corporate action jump")
    status = "complete" if not issues else "warning"
    if coverage < 98.0 or "late start" in issues or "early end" in issues:
        status = "incomplete"
    return {
        "symbol": symbol,
        "status": status,
        "rows": int(len(work)),
        "start": str(work["datetime"].min().date()),
        "end": str(work["datetime"].max().date()),
        "coverage_pct": round(coverage, 2),
        "missing_days": len(missing),
        "duplicates": duplicates,
        "bad_ohlc": bad_ohlc,
        "zero_volume": zero_volume,
        "issues": "; ".join(issues),
    }


def save_cache(df: pd.DataFrame, symbol: str, interval: str, start: str, end: str) -> None:
    if df is None or df.empty:
        return
    out = df.copy()
    out["symbol"] = _normalise_symbol(symbol)
    out = out.drop_duplicates(subset=["datetime"]).sort_values("datetime")
    range_path = _cache_path(symbol, interval, start, end)
    legacy_path = _legacy_cache_path(symbol, interval)
    out.to_csv(range_path, index=False)
    out.to_csv(legacy_path, index=False)


def backfill_one(client, match: dict, interval: str, start: str, end: str, expected_days: Optional[set[pd.Timestamp]], force: bool, dry_run: bool) -> tuple[pd.DataFrame, dict]:
    symbol = _normalise_symbol(match["symbol"])
    if match.get("status") != "mapped" or not match.get("instrument_token"):
        return pd.DataFrame(), {
            "symbol": symbol,
            "status": "unmapped",
            "rows": 0,
            "start": "",
            "end": "",
            "coverage_pct": 0.0,
            "missing_days": 0,
            "duplicates": 0,
            "bad_ohlc": 0,
            "zero_volume": 0,
            "issues": match.get("reason", "unmapped"),
        }

    cached = read_cache(symbol, interval, start, end)
    if not force and is_complete_enough(cached, start, end, expected_days):
        return cached, audit_candles(symbol, cached, start, end, expected_days)

    if dry_run:
        audit = audit_candles(symbol, cached, start, end, expected_days)
        audit["status"] = "needs_fetch" if audit["status"] != "complete" else "complete"
        return cached, audit

    try:
        fetched = fetch_range(client, int(match["instrument_token"]), start, end, interval)
        if not fetched.empty:
            fetched["symbol"] = symbol
            save_cache(fetched, symbol, interval, start, end)
            return fetched, audit_candles(symbol, fetched, start, end, expected_days)
    except Exception as exc:
        audit = audit_candles(symbol, cached, start, end, expected_days)
        audit["status"] = "fetch_error"
        audit["issues"] = f"{audit.get('issues', '')}; fetch_error={exc}".strip("; ")
        return cached, audit
    return cached, audit_candles(symbol, cached, start, end, expected_days)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Kite historical candles and produce quality gate")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--start", default="2024-05-13")
    parser.add_argument("--end", default="2026-05-12")
    parser.add_argument("--interval", default="day")
    parser.add_argument("--include-intraday", action="store_true")
    parser.add_argument("--intraday-interval", default="5minute")
    parser.add_argument("--intraday-days", type=int, default=60)
    parser.add_argument("--min-symbol-complete-pct", type=float, default=95.0)
    parser.add_argument("--sleep", type=float, default=0.37)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-instruments", action="store_true")
    parser.add_argument("--out-prefix", default=str(RESULTS_DIR / "kite_backfill_2y"))
    args = parser.parse_args()

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    kite_ok, kite_reason = kite_session_status()
    if not kite_ok and not args.dry_run:
        print(f"Kite unavailable: {kite_reason}")
        return 2

    client = build_client() if kite_ok and not args.dry_run else None
    instruments = load_instruments(client=client, refresh=bool(args.refresh_instruments))
    symbols = load_universe(Path(args.universe), args.limit)
    all_symbols = ["NIFTY 50", *symbols]
    mapping = map_symbols(all_symbols, instruments)
    mapping.to_csv(out_prefix.with_name(out_prefix.name + "_instrument_map.csv"), index=False)

    expected_days = None
    nifty_match = mapping[mapping["symbol"].eq("NIFTY 50")]
    nifty_df = pd.DataFrame()
    if not nifty_match.empty:
        nifty_df, nifty_audit = backfill_one(
            client,
            nifty_match.iloc[0].to_dict(),
            args.interval,
            args.start,
            args.end,
            expected_days=None,
            force=bool(args.force),
            dry_run=bool(args.dry_run),
        )
        if not nifty_df.empty:
            expected_days = set(pd.to_datetime(nifty_df["datetime"]).dt.normalize())
    else:
        nifty_audit = {"symbol": "NIFTY 50", "status": "unmapped", "issues": "NIFTY token unavailable"}

    audit_rows = [nifty_audit]
    stock_matches = mapping[~mapping["symbol"].eq("NIFTY 50")].copy()
    for idx, rec in enumerate(stock_matches.to_dict(orient="records"), 1):
        df, audit = backfill_one(
            client,
            rec,
            args.interval,
            args.start,
            args.end,
            expected_days=expected_days,
            force=bool(args.force),
            dry_run=bool(args.dry_run),
        )
        audit_rows.append(audit)
        if idx % 10 == 0:
            complete = sum(1 for row in audit_rows[1:] if row.get("status") in {"complete", "warning"})
            print(f"  backfilled/audited {idx}/{len(stock_matches)} stocks; complete-ish={complete}", flush=True)
        if not args.dry_run and args.sleep > 0:
            time.sleep(float(args.sleep))

    audit_df = pd.DataFrame(audit_rows)
    audit_df.to_csv(out_prefix.with_name(out_prefix.name + "_daily_audit.csv"), index=False)
    stock_audit = audit_df[~audit_df["symbol"].eq("NIFTY 50")].copy()
    eligible_symbols = stock_audit[stock_audit["status"].isin(["complete", "warning"])]["symbol"].astype(str).tolist()
    out_prefix.with_name(out_prefix.name + "_kite_complete_universe.txt").write_text(
        "\n".join(eligible_symbols) + ("\n" if eligible_symbols else ""),
        encoding="utf-8",
    )
    pd.DataFrame({"symbol": eligible_symbols}).to_csv(
        out_prefix.with_name(out_prefix.name + "_kite_complete_universe.csv"),
        index=False,
    )
    mapped_count = int((stock_matches["status"] == "mapped").sum())
    complete_count = int(stock_audit["status"].isin(["complete", "warning"]).sum())
    total_selected = int(len(stock_audit))
    complete_pct = complete_count / max(total_selected, 1) * 100.0
    nifty_complete = str(audit_df[audit_df["symbol"].eq("NIFTY 50")]["status"].iloc[0]) in {"complete", "warning"}
    gate_pass = complete_pct >= float(args.min_symbol_complete_pct) and nifty_complete
    gate = {
        "kite_session_available": kite_ok,
        "kite_session_reason": kite_reason,
        "start": _date_str(args.start),
        "end": _date_str(args.end),
        "interval": args.interval,
        "selected_symbols": total_selected,
        "mapped_symbols": mapped_count,
        "complete_symbols": complete_count,
        "complete_pct": round(complete_pct, 2),
        "nifty_complete": bool(nifty_complete),
        "gate_pass": bool(gate_pass),
        "min_symbol_complete_pct": float(args.min_symbol_complete_pct),
        "unmapped_symbols": stock_matches[stock_matches["status"] != "mapped"]["symbol"].tolist(),
        "incomplete_symbols": stock_audit[~stock_audit["status"].isin(["complete", "warning"])]["symbol"].tolist(),
    }
    out_prefix.with_name(out_prefix.name + "_quality_gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")

    if args.include_intraday and not args.dry_run and gate_pass:
        intraday_end = pd.Timestamp(args.end)
        intraday_start = intraday_end - pd.Timedelta(days=max(1, int(args.intraday_days)))
        intraday_rows = []
        for idx, rec in enumerate(stock_matches[stock_matches["status"].eq("mapped")].to_dict(orient="records"), 1):
            df, audit = backfill_one(
                client,
                rec,
                args.intraday_interval,
                str(intraday_start.date()),
                str(intraday_end.date()),
                expected_days=None,
                force=bool(args.force),
                dry_run=False,
            )
            intraday_rows.append(audit)
            if idx % 10 == 0:
                print(f"  intraday backfilled {idx}/{mapped_count}", flush=True)
            if args.sleep > 0:
                time.sleep(float(args.sleep))
        pd.DataFrame(intraday_rows).to_csv(out_prefix.with_name(out_prefix.name + "_intraday_audit.csv"), index=False)

    print(json.dumps(gate, indent=2))
    if not gate_pass:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

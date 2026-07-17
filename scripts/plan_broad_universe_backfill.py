#!/usr/bin/env python3
"""Plan a resumable broad-universe Kite intraday history backfill.

This command performs no broker requests. It maps the current live universe,
audits exact-range cache files, estimates storage/API work, and emits stable
batches for the existing Kite downloader.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.kite_historical_backfill import load_instruments, map_symbols  # noqa: E402
from scripts.kite_intraday_backfill import cache_path, chunk_ranges, load_backfill_universe  # noqa: E402

RESULTS_DIR = REPO_ROOT / "results" / "Data"
DEFAULT_UNIVERSE = REPO_ROOT / "results" / "Indices" / "NSE_BSE_COMBINED.csv"


def existing_file_stats(directory: Path) -> tuple[int, int]:
    sizes = [path.stat().st_size for path in directory.glob("*.csv") if path.is_file()]
    return len(sizes), sum(sizes)


def retry_symbols_from_audits(prefix: Path, interval: str) -> set[str]:
    """Return symbols whose latest batch audit recorded a fetch error."""
    latest: dict[str, tuple[float, str]] = {}
    pattern = f"{prefix.name}_batch_*_{interval}_audit.csv"
    for path in prefix.parent.glob(pattern):
        try:
            modified = path.stat().st_mtime
            frame = pd.read_csv(path, usecols=["symbol", "status"])
        except Exception:
            continue
        for row in frame.to_dict(orient="records"):
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol or symbol == "NIFTY 50":
                continue
            if symbol not in latest or modified >= latest[symbol][0]:
                latest[symbol] = (modified, str(row.get("status", "")).strip().lower())
    return {symbol for symbol, (_, status) in latest.items() if status == "fetch_error"}


def build_plan(
    symbols: list[str],
    instruments: pd.DataFrame,
    interval: str,
    start: str,
    end: str,
    batch_size: int,
    chunk_days: int,
    retry_symbols: set[str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    mapping = map_symbols(symbols, instruments)
    range_dir = cache_path("__PLAN__", interval, start, end).parent
    placeholder = range_dir / "__PLAN__.csv"
    if placeholder.exists():
        placeholder.unlink()
    existing_count, existing_bytes = existing_file_stats(range_dir)
    known_sizes = [path.stat().st_size for path in range_dir.glob("*.csv") if path.is_file()]
    avg_bytes = int(sum(known_sizes) / len(known_sizes)) if known_sizes else 2_100_000
    size = max(1, int(batch_size))
    rows = []
    retry_symbols = retry_symbols or set()
    for position, rec in enumerate(mapping.to_dict(orient="records")):
        symbol = str(rec.get("symbol", "")).strip().upper()
        path = range_dir / f"{symbol}.csv"
        present = path.exists() and path.stat().st_size > 0
        rows.append(
            {
                **rec,
                "position": position,
                "batch_index": position // size,
                "cache_present": present,
                "cache_bytes": path.stat().st_size if present else 0,
                "needs_fetch": rec.get("status") == "mapped" and (not present or symbol in retry_symbols),
                "cache_path": str(path),
            }
        )
    manifest = pd.DataFrame(rows)
    mapped = int(manifest["status"].eq("mapped").sum())
    needs_fetch = int(manifest["needs_fetch"].sum())
    chunks = len(chunk_ranges(start, end, chunk_days))
    summary = {
        "universe_symbols": len(symbols),
        "mapped_symbols": mapped,
        "unmapped_symbols": int(len(symbols) - mapped),
        "exact_range_cache_files": int(manifest["cache_present"].sum()),
        "needs_fetch_symbols": needs_fetch,
        "batch_size": size,
        "batches": int(math.ceil(len(symbols) / size)),
        "chunks_per_missing_symbol": chunks,
        "estimated_api_calls": needs_fetch * chunks,
        "existing_range_bytes": existing_bytes,
        "average_cached_symbol_bytes": avg_bytes,
        "estimated_additional_bytes": needs_fetch * avg_bytes,
        "estimated_total_range_bytes": mapped * avg_bytes,
        "interval": interval,
        "start": pd.Timestamp(start).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(end).strftime("%Y-%m-%d"),
        "warning": "Current live-universe membership is not point-in-time membership; delisted historical names remain absent.",
    }
    return manifest, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan broad live-universe Kite intraday backfill")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--interval", default="5minute")
    parser.add_argument("--start", default="2024-05-30")
    parser.add_argument("--end", default="2026-05-29")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--chunk-days", type=int, default=60)
    parser.add_argument("--out-prefix", default=str(RESULTS_DIR / "broad_universe_5minute_backfill"))
    args = parser.parse_args()

    symbols = load_backfill_universe(Path(args.universe))
    instruments = load_instruments(client=None, refresh=False)
    prefix = Path(args.out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    retry_symbols = retry_symbols_from_audits(prefix, args.interval)
    manifest, summary = build_plan(
        symbols,
        instruments,
        args.interval,
        args.start,
        args.end,
        args.batch_size,
        args.chunk_days,
        retry_symbols=retry_symbols,
    )
    summary["fetch_error_retry_symbols"] = len(retry_symbols)
    manifest.to_csv(prefix.with_name(prefix.name + "_manifest.csv"), index=False)
    prefix.with_name(prefix.name + "_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    commands = []
    for batch_index in range(int(summary["batches"])):
        batch = manifest[manifest["batch_index"].eq(batch_index)]
        if not bool(batch["needs_fetch"].any()):
            continue
        commands.append(
            ".venv/bin/python scripts/kite_intraday_backfill.py "
            f"--universe {args.universe} --limit 0 --start {args.start} --end {args.end} "
            f"--intervals {args.interval} --chunk-days {args.chunk_days} "
            f"--batch-size {args.batch_size} --batch-index {batch_index} "
            f"--out-prefix {args.out_prefix}"
        )
    prefix.with_name(prefix.name + "_pending_commands.sh").write_text("\n".join(commands) + ("\n" if commands else ""), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

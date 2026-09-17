#!/usr/bin/env python3
"""Refresh the official NSE equity list and Kite instrument map before live scans."""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import requests


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
NSE_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
MAX_LIVE_SYMBOLS = 2990


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".csv", dir=path.parent, delete=False, encoding="utf-8", newline=""
    ) as handle:
        frame.to_csv(handle, index=False)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _official_equities(content: bytes) -> pd.DataFrame:
    frame = pd.read_csv(io.BytesIO(content), dtype=str).fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]
    if "SYMBOL" not in frame or "SERIES" not in frame:
        raise ValueError("NSE equity list has no SYMBOL/SERIES columns")
    frame["SYMBOL"] = frame["SYMBOL"].astype(str).str.strip().str.upper()
    frame["SERIES"] = frame["SERIES"].astype(str).str.strip().str.upper()
    frame = frame[frame["SYMBOL"].ne("")].drop_duplicates("SYMBOL")
    if not 1500 <= len(frame) <= 5000 or frame["SERIES"].eq("EQ").sum() < 1500:
        raise ValueError(f"NSE equity list failed size validation: {len(frame)} symbols")
    return frame


def _kite_nse_instruments(client) -> pd.DataFrame:
    frame = pd.DataFrame(client.instruments("NSE") or [])
    required = {"tradingsymbol", "instrument_token", "segment", "exchange", "instrument_type"}
    if not required.issubset(frame.columns):
        raise ValueError("Kite NSE instrument dump is missing required columns")
    frame = frame[frame["segment"].astype(str).str.upper().eq("NSE")].copy()
    if len(frame) < 5000:
        raise ValueError(f"Kite NSE instrument dump is unexpectedly small: {len(frame)}")
    frame["tradingsymbol"] = frame["tradingsymbol"].astype(str).str.strip().str.upper()
    return frame


def refresh(root: Path, *, content: bytes, instruments: pd.DataFrame) -> dict[str, int]:
    official = _official_equities(content)
    nse = instruments.copy()
    required = {"tradingsymbol", "instrument_token", "segment", "exchange", "instrument_type"}
    if not required.issubset(nse.columns):
        raise ValueError("Kite NSE instrument dump is missing required columns")
    nse = nse[nse["segment"].astype(str).str.upper().eq("NSE")].copy()
    if len(nse) < 5000:
        raise ValueError("Kite NSE instrument dump is unexpectedly small")
    nse["tradingsymbol"] = nse["tradingsymbol"].astype(str).str.strip().str.upper()
    official_eq = set(official.loc[official["SERIES"].eq("EQ"), "SYMBOL"])
    kite_eq = set(nse.loc[nse["instrument_type"].astype(str).str.upper().eq("EQ"), "tradingsymbol"])
    overlap = len(official_eq & kite_eq)
    if overlap < len(official_eq) * 0.95:
        raise ValueError(f"NSE/Kite EQ lists disagree: {overlap}/{len(official_eq)}")

    indices = root / "results" / "Indices"
    data = root / "results" / "Data"
    old_nse_path = indices / "EQUITY_L.csv"
    combined_path = indices / "NSE_BSE_COMBINED.csv"
    bse_path = indices / "BSE_TRADEABLE.csv"
    old_nse = set()
    if old_nse_path.is_file():
        old_frame = pd.read_csv(old_nse_path, usecols=["SYMBOL"], dtype=str)
        old_nse = set(old_frame["SYMBOL"].str.strip().str.upper())
    bse_symbols = set()
    if bse_path.is_file():
        bse_frame = pd.read_csv(bse_path, usecols=["Symbol"], dtype=str)
        bse_symbols = set(bse_frame["Symbol"].str.strip().str.upper())
    previous_order = []
    if combined_path.is_file():
        previous = pd.read_csv(combined_path, usecols=["Symbol"], dtype=str)
        previous_order = previous["Symbol"].str.strip().str.upper().tolist()

    nse_symbols = official["SYMBOL"].tolist()
    nse_set = set(nse_symbols)
    # Retain the previous liquidity order for existing symbols. New listings
    # join the NSE portion before BSE, rather than disappearing behind its cap.
    ordered_nse = [symbol for symbol in previous_order if symbol in nse_set]
    ordered_nse.extend(sorted(nse_set.difference(ordered_nse)))
    ordered_bse = [
        symbol for symbol in previous_order
        if symbol in bse_symbols and symbol not in nse_set and symbol not in old_nse
    ]
    combined = (ordered_nse + ordered_bse)[:MAX_LIVE_SYMBOLS]

    _atomic_csv(nse, data / "kite_instruments_nse_latest.csv")
    _atomic_csv(official, old_nse_path)
    _atomic_csv(pd.DataFrame({"Symbol": combined}), combined_path)
    return {
        "official_nse": len(official),
        "kite_eq_matched": overlap,
        "new_nse_symbols": len(nse_set - old_nse),
        "live_universe": len(combined),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO)
    args = parser.parse_args()
    response = requests.get(
        NSE_LIST_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager

    os.chdir(args.root)
    client = KiteConnectSessionManager()._build_client()
    instruments = _kite_nse_instruments(client)
    summary = refresh(args.root, content=response.content, instruments=instruments)
    print("NSE universe refreshed: " + ", ".join(f"{key}={value}" for key, value in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

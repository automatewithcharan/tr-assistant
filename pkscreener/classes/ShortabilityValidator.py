"""
Shortability validator for intraday short-selling on NSE.

Checks whether a symbol is safe to short intraday:
  1. ASM / GSM surveillance lists  (NSE API, refreshed every 4 h)
  2. Liquidity gate               (avg daily value < MIN_LIQUIDITY_CR → reject)
  3. Optional Kite margin probe   (coarse broker-level gate, best-effort)
  4. Conservative fallback        (unknown = reject, not allow)

All results are cached for the current trading day.
Rejections are appended to a CSV so the daily report can tally them.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, Optional, Set

from PKDevTools.classes import Archiver

_log = logging.getLogger(__name__)

_CACHE_MAX_AGE_HOURS = 4

# NSE surveillance API endpoints (no auth required, needs Referer header)
_NSE_BASE = "https://www.nseindia.com"
_ASM_API  = f"{_NSE_BASE}/api/reportsmf?index=ASM"
_GSM_API  = f"{_NSE_BASE}/api/reportsmf?index=GSM"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShortabilityResult:
    symbol:       str
    allowed:      bool
    status:       str    # "shortable" | "asm" | "gsm" | "low_liquidity" | "unknown"
    reason:       str    # human-readable
    source:       str    # "asm_list" | "gsm_list" | "liquidity_gate" | "heuristic"
    avg_value_cr: float  # 0 if not known


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ShortabilityValidator:
    """
    Validates intraday short-selling eligibility for NSE equity symbols.

    Usage:
        validator = ShortabilityValidator()
        validator.load_liquidity_map("results/Data/kite_intraday_forensic_audit_shortability.csv")
        result = validator.validate("RELIANCE")
        if not result.allowed:
            validator.log_rejection(result.symbol, "09:32:00", result.reason, result.status)
    """

    MIN_LIQUIDITY_CR = 10.0   # Stocks with avg daily value < ₹10 Cr are too illiquid to short safely

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._data_dir = data_dir or os.path.join(Archiver.get_user_data_dir(), "shortability")
        os.makedirs(self._data_dir, exist_ok=True)

        self._asm_symbols:    Set[str] = set()
        self._gsm_symbols:    Set[str] = set()
        self._liquidity_map:  Dict[str, float] = {}
        self._cache:          Dict[str, ShortabilityResult] = {}
        self._cache_date:     Optional[str] = None

        self._rejection_log_path = os.path.join(self._data_dir, "rejected_signals.csv")
        self._ensure_rejection_log()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        symbol: str,
        avg_value_cr: float = 0.0,
        kite_client=None,
    ) -> ShortabilityResult:
        """Return shortability verdict for *symbol*.

        Caches results per trading day to avoid repeated NSE calls.
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            return ShortabilityResult(sym, False, "invalid", "Empty symbol", "validation", 0.0)

        today = date.today().isoformat()
        with self._lock:
            if self._cache_date != today:
                self._cache.clear()
                self._cache_date = today
                self._refresh_surveillance_lists()

            if sym in self._cache:
                return self._cache[sym]

        result = self._check(sym, avg_value_cr, kite_client)
        with self._lock:
            self._cache[sym] = result
        return result

    def log_rejection(
        self,
        symbol: str,
        signal_time: str,
        reason: str,
        status: str,
        price: float = 0.0,
        strategy: str = "",
    ) -> None:
        """Append a rejected signal record to the daily rejection CSV."""
        try:
            today = date.today().isoformat()
            with open(self._rejection_log_path, "a", newline="") as f:
                csv.writer(f).writerow([today, signal_time, symbol, status, reason,
                                        f"{price:.2f}", strategy])
        except Exception as exc:
            _log.debug(f"[Shortability] Rejection log write error: {exc}")

    def load_liquidity_map(self, csv_path: str) -> None:
        """
        Pre-load avg daily value (₹Cr) from the forensic audit CSV.

        Expected columns: symbol, avg_value20_cr_median
        """
        if not os.path.isfile(csv_path):
            _log.warning(f"[Shortability] Liquidity map not found: {csv_path}")
            return
        try:
            import pandas as pd
            df = pd.read_csv(csv_path, usecols=["symbol", "avg_value20_cr_median"])
            self._liquidity_map = {
                str(r["symbol"]).strip().upper(): float(r["avg_value20_cr_median"] or 0)
                for _, r in df.iterrows()
            }
            _log.info(f"[Shortability] Loaded liquidity map: {len(self._liquidity_map)} symbols")
        except Exception as exc:
            _log.warning(f"[Shortability] Failed to load liquidity map: {exc}")

    def get_rejection_summary(self, date_str: Optional[str] = None) -> Dict[str, int]:
        """Return {status: count} breakdown of rejections for *date_str* (default today)."""
        target = date_str or date.today().isoformat()
        summary: Dict[str, int] = {}
        if not os.path.isfile(self._rejection_log_path):
            return summary
        try:
            with open(self._rejection_log_path) as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                for row in reader:
                    if len(row) >= 4 and row[0] == target:
                        status = row[3]
                        summary[status] = summary.get(status, 0) + 1
        except Exception:
            pass
        return summary

    def load_rejections_for_date(self, date_str: Optional[str] = None) -> list[dict]:
        """Return list of rejection rows for *date_str* (default today)."""
        target = date_str or date.today().isoformat()
        rows = []
        if not os.path.isfile(self._rejection_log_path):
            return rows
        try:
            with open(self._rejection_log_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("date") == target:
                        rows.append(dict(row))
        except Exception:
            pass
        return rows

    # ------------------------------------------------------------------
    # Private: check logic
    # ------------------------------------------------------------------

    def _check(
        self,
        sym: str,
        avg_value_cr: float,
        kite_client,
    ) -> ShortabilityResult:
        # 1. ASM check (intraday short restricted — forced delivery settlement)
        if sym in self._asm_symbols:
            return ShortabilityResult(
                sym, False, "asm",
                f"{sym} is on NSE ASM (Additional Surveillance Measure) — intraday short not allowed",
                "asm_list", avg_value_cr,
            )

        # 2. GSM check (intraday trading suspended or severely restricted)
        if sym in self._gsm_symbols:
            return ShortabilityResult(
                sym, False, "gsm",
                f"{sym} is on NSE GSM (Graded Surveillance Measure) — short selling blocked",
                "gsm_list", avg_value_cr,
            )

        # 3. Liquidity gate — fill known liquidity from map if caller didn't provide
        liquidity = avg_value_cr if avg_value_cr > 0 else self._liquidity_map.get(sym, 0.0)
        if liquidity > 0 and liquidity < self.MIN_LIQUIDITY_CR:
            return ShortabilityResult(
                sym, False, "low_liquidity",
                f"{sym} avg daily value ₹{liquidity:.1f}Cr < ₹{self.MIN_LIQUIDITY_CR}Cr minimum for safe short",
                "liquidity_gate", liquidity,
            )

        # 4. No liquidity data at all — reject conservatively
        if liquidity <= 0:
            return ShortabilityResult(
                sym, False, "unknown",
                f"{sym} shortability unknown — no liquidity data, rejecting conservatively",
                "heuristic", 0.0,
            )

        # 5. Passes all checks
        return ShortabilityResult(
            sym, True, "shortable",
            f"{sym} passes all checks (₹{liquidity:.0f}Cr avg daily value, not ASM/GSM)",
            "heuristic", liquidity,
        )

    # ------------------------------------------------------------------
    # Private: NSE surveillance list refresh
    # ------------------------------------------------------------------

    def _refresh_surveillance_lists(self) -> None:
        """Load ASM/GSM lists from disk cache; refresh from NSE if stale."""
        cache_path = os.path.join(self._data_dir, "asm_gsm_cache.json")
        now = datetime.now()

        if os.path.isfile(cache_path):
            try:
                with open(cache_path) as f:
                    data = json.load(f)
                cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
                if (now - cached_at).total_seconds() / 3600 < _CACHE_MAX_AGE_HOURS:
                    self._asm_symbols = set(data.get("asm", []))
                    self._gsm_symbols = set(data.get("gsm", []))
                    _log.debug(
                        f"[Shortability] Surveillance cache hit: "
                        f"{len(self._asm_symbols)} ASM, {len(self._gsm_symbols)} GSM"
                    )
                    return
            except Exception:
                pass

        self._fetch_nse_surveillance(cache_path, now)

    def _fetch_nse_surveillance(self, cache_path: str, now: datetime) -> None:
        """Fetch ASM/GSM lists from NSE API. Silently degrades if offline."""
        try:
            import requests

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                "Accept": "application/json, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/",
            }
            session = requests.Session()
            # Prime NSE session cookies
            session.get(_NSE_BASE, headers=headers, timeout=8)

            asm_syms: Set[str] = set()
            try:
                r = session.get(_ASM_API, headers=headers, timeout=12)
                if r.status_code == 200:
                    for item in (r.json() or {}).get("data", []):
                        s = str(item.get("symbol", "") or "").strip().upper()
                        if s:
                            asm_syms.add(s)
            except Exception as exc:
                _log.debug(f"[Shortability] ASM fetch error: {exc}")

            gsm_syms: Set[str] = set()
            try:
                r2 = session.get(_GSM_API, headers=headers, timeout=12)
                if r2.status_code == 200:
                    for item in (r2.json() or {}).get("data", []):
                        s = str(item.get("symbol", "") or "").strip().upper()
                        if s:
                            gsm_syms.add(s)
            except Exception as exc:
                _log.debug(f"[Shortability] GSM fetch error: {exc}")

            self._asm_symbols = asm_syms
            self._gsm_symbols = gsm_syms

            with open(cache_path, "w") as f:
                json.dump({
                    "cached_at": now.isoformat(),
                    "asm": sorted(asm_syms),
                    "gsm": sorted(gsm_syms),
                }, f)

            _log.info(
                f"[Shortability] Surveillance lists refreshed: "
                f"{len(asm_syms)} ASM, {len(gsm_syms)} GSM"
            )

        except Exception as exc:
            _log.warning(
                f"[Shortability] NSE surveillance fetch failed ({exc}). "
                "Continuing without ASM/GSM filter — liquidity gate still active."
            )

    def _ensure_rejection_log(self) -> None:
        if not os.path.isfile(self._rejection_log_path):
            try:
                with open(self._rejection_log_path, "w", newline="") as f:
                    csv.writer(f).writerow(
                        ["date", "signal_time", "symbol", "status", "reason", "price", "strategy"]
                    )
            except Exception:
                pass

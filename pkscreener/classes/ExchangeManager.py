"""
Exchange-aware symbol management for NSE + BSE dual-listed stocks.

Handles:
- Exchange detection (NSE vs BSE)
- Duplicate listing dedup (prefer NSE when dual-listed, with liquidity override)
- Exchange suffix mapping (.NS for NSE, .BO for BSE)
- BSE universe loading from CSV files
- Liquidity-based venue selection for dual-listed stocks
"""

import os
import logging
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

logger = logging.getLogger("pkscreener")

# Exchange suffixes used by data providers
EXCHANGE_SUFFIX_NSE = ".NS"
EXCHANGE_SUFFIX_BSE = ".BO"

# BSE universe files (mirror structure of NSE files)
BSE_UNIVERSE_FILES = {
    "bse_allstocks": os.path.join("results", "Indices", "BSE_EQUITY_L.csv"),
    "bse500": os.path.join("results", "Indices", "BSE500.csv"),
    "bse_midcap": os.path.join("results", "Indices", "BSE_MIDCAP.csv"),
    "bse_smallcap": os.path.join("results", "Indices", "BSE_SMALLCAP.csv"),
}


class ExchangeManager:
    """Manage exchange-aware symbol resolution and dedup for NSE + BSE."""

    def __init__(self, prefer_exchange: str = "NSE"):
        self.prefer_exchange = prefer_exchange.upper()
        self._nse_symbols: Set[str] = set()
        self._bse_symbols: Set[str] = set()
        self._dual_listed: Set[str] = set()
        # Maps: clean symbol -> (exchange, full_ticker)
        self._symbol_exchange_map: Dict[str, Tuple[str, str]] = {}
        # Liquidity overrides: symbol -> preferred exchange based on volume
        self._liquidity_overrides: Dict[str, str] = {}

    def load_nse_symbols(self, symbols: List[str]) -> None:
        """Register NSE symbols."""
        self._nse_symbols = {str(s).strip().upper() for s in symbols if s}

    def load_bse_symbols(self, symbols: List[str]) -> None:
        """Register BSE symbols."""
        self._bse_symbols = {str(s).strip().upper() for s in symbols if s}

    def compute_dedup_universe(self) -> List[Tuple[str, str, str]]:
        """
        Compute a deduplicated universe of symbols.

        Returns a list of (clean_symbol, exchange, suffix) tuples.
        When a symbol is dual-listed, the preferred exchange wins
        unless a liquidity override is set.
        """
        self._dual_listed = self._nse_symbols & self._bse_symbols
        result: List[Tuple[str, str, str]] = []
        seen: Set[str] = set()

        # Add preferred exchange first
        preferred_set = self._nse_symbols if self.prefer_exchange == "NSE" else self._bse_symbols
        preferred_suffix = EXCHANGE_SUFFIX_NSE if self.prefer_exchange == "NSE" else EXCHANGE_SUFFIX_BSE
        for sym in sorted(preferred_set):
            if sym not in seen:
                # Check for liquidity override on dual-listed
                if sym in self._dual_listed and sym in self._liquidity_overrides:
                    override_exchange = self._liquidity_overrides[sym]
                    override_suffix = EXCHANGE_SUFFIX_NSE if override_exchange == "NSE" else EXCHANGE_SUFFIX_BSE
                    result.append((sym, override_exchange, override_suffix))
                else:
                    result.append((sym, self.prefer_exchange, preferred_suffix))
                seen.add(sym)

        # Add non-preferred exchange symbols that aren't already covered
        other_set = self._bse_symbols if self.prefer_exchange == "NSE" else self._nse_symbols
        other_exchange = "BSE" if self.prefer_exchange == "NSE" else "NSE"
        other_suffix = EXCHANGE_SUFFIX_BSE if self.prefer_exchange == "NSE" else EXCHANGE_SUFFIX_NSE
        for sym in sorted(other_set):
            if sym not in seen:
                result.append((sym, other_exchange, other_suffix))
                seen.add(sym)

        return result

    def set_liquidity_preferences(
        self,
        volume_data: Dict[str, Dict[str, float]],
        min_volume_ratio: float = 1.5,
    ) -> int:
        """
        Set exchange preferences for dual-listed stocks based on volume data.

        Args:
            volume_data: {symbol: {"NSE": avg_volume, "BSE": avg_volume}}
            min_volume_ratio: Minimum ratio to override default preference.
                If BSE volume > NSE volume * ratio, prefer BSE (and vice versa).

        Returns:
            Number of overrides set.
        """
        overrides_set = 0
        for sym in self._dual_listed:
            sym_data = volume_data.get(sym)
            if not sym_data:
                continue
            nse_vol = float(sym_data.get("NSE", 0))
            bse_vol = float(sym_data.get("BSE", 0))
            if nse_vol <= 0 and bse_vol <= 0:
                continue

            # Pick the exchange with clearly higher liquidity
            if nse_vol > 0 and bse_vol > nse_vol * min_volume_ratio:
                self._liquidity_overrides[sym] = "BSE"
                overrides_set += 1
            elif bse_vol > 0 and nse_vol > bse_vol * min_volume_ratio:
                self._liquidity_overrides[sym] = "NSE"
                overrides_set += 1
            # If volumes are close, keep default preference

        return overrides_set

    def get_exchange_suffix(self, symbol: str) -> str:
        """Return the exchange suffix for a symbol."""
        sym = str(symbol).strip().upper()
        # Check liquidity override first
        if sym in self._liquidity_overrides:
            return EXCHANGE_SUFFIX_NSE if self._liquidity_overrides[sym] == "NSE" else EXCHANGE_SUFFIX_BSE
        if sym in self._nse_symbols and self.prefer_exchange == "NSE":
            return EXCHANGE_SUFFIX_NSE
        if sym in self._bse_symbols:
            return EXCHANGE_SUFFIX_BSE
        return EXCHANGE_SUFFIX_NSE  # default to NSE

    def get_exchange(self, symbol: str) -> str:
        """Return the exchange name for a symbol ('NSE' or 'BSE')."""
        sym = str(symbol).strip().upper()
        if sym in self._liquidity_overrides:
            return self._liquidity_overrides[sym]
        if sym in self._nse_symbols and self.prefer_exchange == "NSE":
            return "NSE"
        if sym in self._bse_symbols and sym not in self._nse_symbols:
            return "BSE"
        return self.prefer_exchange

    def is_dual_listed(self, symbol: str) -> bool:
        """Check if a symbol is listed on both NSE and BSE."""
        return str(symbol).strip().upper() in self._dual_listed

    @property
    def dual_listed_count(self) -> int:
        return len(self._dual_listed)

    @property
    def nse_only_count(self) -> int:
        return len(self._nse_symbols - self._bse_symbols)

    @property
    def bse_only_count(self) -> int:
        return len(self._bse_symbols - self._nse_symbols)

    @property
    def liquidity_override_count(self) -> int:
        return len(self._liquidity_overrides)

    def summary(self) -> str:
        """Return a human-readable summary of exchange coverage."""
        total = len(self._nse_symbols | self._bse_symbols)
        parts = [
            f"Universe: {total} unique symbols",
            f"NSE-only: {self.nse_only_count}",
            f"BSE-only: {self.bse_only_count}",
            f"Dual-listed: {self.dual_listed_count} (prefer {self.prefer_exchange})",
        ]
        if self._liquidity_overrides:
            parts.append(f"Liquidity overrides: {self.liquidity_override_count}")
        return " | ".join(parts)


def load_bse_universe(universe_key: str) -> List[str]:
    """Load BSE symbols from a universe CSV file."""
    csv_path = BSE_UNIVERSE_FILES.get(universe_key)
    if not csv_path or not os.path.exists(csv_path):
        logger.debug(f"BSE universe file not found: {csv_path}")
        return []

    try:
        df = pd.read_csv(csv_path)
        for col in ["Symbol", "SYMBOL", "symbol", "Security Code"]:
            if col in df.columns:
                return [str(s).strip().upper() for s in df[col].tolist() if str(s).strip()]
    except Exception as exc:
        logger.debug(f"Failed loading BSE universe: {exc}", exc_info=True)
    return []

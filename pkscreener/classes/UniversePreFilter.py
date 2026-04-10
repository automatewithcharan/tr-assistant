"""
Universe pre-filtering: fast liquidity and quality gates applied BEFORE
expensive per-symbol analysis to cut the scan universe early.

This module filters out stocks that are obviously unfit for trading
(too illiquid, too cheap, no volume) before burning CPU on indicators.

Performance features:
- Concurrent filtering via ThreadPoolExecutor for large universes
- Dynamic "active universe" cache to skip re-filtering stable symbols
- Batch-friendly: filter_symbols() processes all symbols in one call
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

logger = logging.getLogger("pkscreener")


class UniversePreFilter:
    """
    Apply fast pre-screening gates to eliminate obviously untradeable stocks.

    These checks use only OHLCV summary data (no indicators needed),
    so they are O(1) per symbol and can be run on the full universe
    before the expensive signal computation.
    """

    def __init__(
        self,
        min_price: float = 10.0,
        max_price: float = 50000.0,
        min_avg_volume: int = 50000,
        min_avg_trade_value: float = 5_000_000.0,
        min_candles: int = 60,
        max_spread_pct: float = 5.0,
    ):
        self.min_price = min_price
        self.max_price = max_price
        self.min_avg_volume = min_avg_volume
        self.min_avg_trade_value = min_avg_trade_value
        self.min_candles = min_candles
        self.max_spread_pct = max_spread_pct

        # Dynamic universe cache: symbol -> (eligible: bool, reason: str, timestamp)
        self._cache: Dict[str, Tuple[bool, str, float]] = {}
        self._cache_lock = threading.Lock()
        self._cache_ttl: float = 300.0  # 5 minutes default

    def set_cache_ttl(self, ttl_seconds: float) -> None:
        """Set the cache TTL for pre-filter results."""
        self._cache_ttl = max(0.0, ttl_seconds)

    def clear_cache(self) -> None:
        """Clear the pre-filter cache."""
        with self._cache_lock:
            self._cache.clear()

    def is_eligible(self, df: pd.DataFrame) -> Tuple[bool, str]:
        """
        Check if a stock's OHLCV data passes pre-filter gates.

        Returns (eligible, reason) where reason explains rejection.
        """
        if df is None or df.empty:
            return False, "no data"

        n = len(df)
        if n < self.min_candles:
            return False, f"insufficient history ({n} < {self.min_candles})"

        # Use last row for price check
        last = df.iloc[-1]
        close = float(last.get("close", 0) or 0)
        if close < self.min_price:
            return False, f"price too low ({close:.2f} < {self.min_price})"
        if close > self.max_price:
            return False, f"price too high ({close:.2f} > {self.max_price})"

        # Average volume over recent 20 bars
        vol_col = "volume" if "volume" in df.columns else "Volume"
        if vol_col in df.columns:
            recent = df.tail(20)
            avg_vol = recent[vol_col].mean()
            if avg_vol < self.min_avg_volume:
                return False, f"avg volume too low ({avg_vol:.0f} < {self.min_avg_volume})"

            # Average trade value = avg_vol * avg_close
            avg_close = recent["close"].mean() if "close" in recent.columns else close
            avg_trade_value = avg_vol * avg_close
            if avg_trade_value < self.min_avg_trade_value:
                return False, f"avg trade value too low (₹{avg_trade_value:,.0f})"

        # Spread check: reject stocks with extreme high-low range (penny/circuit stocks)
        high = float(last.get("high", 0) or 0)
        low = float(last.get("low", 0) or 0)
        if low > 0:
            spread_pct = (high - low) / low * 100.0
            if spread_pct > self.max_spread_pct * 3:  # 3x the max = likely circuit hit
                return False, f"extreme spread ({spread_pct:.1f}%)"

        return True, "passed"

    def filter_symbols(
        self,
        symbols: List[str],
        data_loader: Callable,
        show_progress: bool = False,
        max_workers: int = 4,
        use_cache: bool = True,
    ) -> Tuple[List[str], List[Tuple[str, str]]]:
        """
        Filter a list of symbols, returning (eligible, rejected_with_reasons).

        Uses concurrent execution and caching for fast filtering of large universes.

        Args:
            symbols: List of stock symbols to filter
            data_loader: callable(symbol) -> pd.DataFrame (OHLCV data)
            show_progress: Log progress every 100 symbols
            max_workers: Number of concurrent workers (1 = sequential)
            use_cache: Use dynamic cache to skip recently-checked symbols
        """
        eligible: List[str] = []
        rejected: List[Tuple[str, str]] = []
        to_check: List[str] = []
        now = time.monotonic()

        # Check cache first
        if use_cache:
            with self._cache_lock:
                for symbol in symbols:
                    key = str(symbol).strip().upper()
                    cached = self._cache.get(key)
                    if cached and (now - cached[2]) < self._cache_ttl:
                        if cached[0]:
                            eligible.append(symbol)
                        else:
                            rejected.append((symbol, cached[1]))
                    else:
                        to_check.append(symbol)
        else:
            to_check = list(symbols)

        if not to_check:
            return eligible, rejected

        # Filter uncached symbols
        if max_workers <= 1 or len(to_check) <= 10:
            self._filter_sequential(to_check, data_loader, eligible, rejected, show_progress, now)
        else:
            self._filter_concurrent(to_check, data_loader, eligible, rejected,
                                    show_progress, max_workers, now)

        return eligible, rejected

    def _filter_sequential(self, symbols, data_loader, eligible, rejected,
                           show_progress, timestamp):
        """Filter symbols sequentially (for small universes)."""
        for i, symbol in enumerate(symbols):
            ok, reason = self._check_one(symbol, data_loader, timestamp)
            if ok:
                eligible.append(symbol)
            else:
                rejected.append((symbol, reason))

            if show_progress and ((i + 1) % 100 == 0 or i + 1 == len(symbols)):
                logger.debug(f"Pre-filter: {i+1}/{len(symbols)} checked, {len(eligible)} eligible")

    def _filter_concurrent(self, symbols, data_loader, eligible, rejected,
                           show_progress, max_workers, timestamp):
        """Filter symbols concurrently via ThreadPoolExecutor."""
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self._check_one, sym, data_loader, timestamp): sym
                for sym in symbols
            }
            for future in as_completed(future_map):
                completed += 1
                sym = future_map[future]
                try:
                    ok, reason = future.result()
                    if ok:
                        eligible.append(sym)
                    else:
                        rejected.append((sym, reason))
                except Exception as exc:
                    rejected.append((sym, f"error: {exc}"))

                if show_progress and (completed % 100 == 0 or completed == len(symbols)):
                    logger.debug(f"Pre-filter: {completed}/{len(symbols)} checked, {len(eligible)} eligible")

    def _check_one(self, symbol: str, data_loader: Callable, timestamp: float) -> Tuple[bool, str]:
        """Check one symbol and update the cache."""
        key = str(symbol).strip().upper()
        try:
            df = data_loader(symbol)
            ok, reason = self.is_eligible(df)
        except Exception as exc:
            ok, reason = False, f"error: {exc}"

        with self._cache_lock:
            self._cache[key] = (ok, reason, timestamp)

        return ok, reason

    def get_cached_eligible(self) -> List[str]:
        """Return the current set of cached eligible symbols."""
        now = time.monotonic()
        with self._cache_lock:
            return [
                sym for sym, (ok, _, ts) in self._cache.items()
                if ok and (now - ts) < self._cache_ttl
            ]

    def get_cache_stats(self) -> Dict[str, int]:
        """Return cache statistics."""
        now = time.monotonic()
        with self._cache_lock:
            valid = [(ok, reason) for ok, reason, ts in self._cache.values()
                     if (now - ts) < self._cache_ttl]
            return {
                "total_cached": len(valid),
                "eligible": sum(1 for ok, _ in valid if ok),
                "rejected": sum(1 for ok, _ in valid if not ok),
                "expired": len(self._cache) - len(valid),
            }

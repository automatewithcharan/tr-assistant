"""
Concurrent batch data fetcher for high-speed multi-symbol data retrieval.

Instead of fetching OHLCV data one symbol at a time, this module fetches
data for multiple symbols concurrently using a thread pool, with:
- Configurable concurrency (thread pool size)
- Per-symbol timeout protection
- Result caching with TTL
- Progress callbacks
- Error isolation (one symbol failure doesn't block others)

Usage:
    fetcher = BatchDataFetcher(data_loader=my_loader, max_workers=8)
    results = fetcher.fetch_batch(["RELIANCE", "TCS", "INFY"])
    # results = {"RELIANCE": df, "TCS": df, "INFY": df}
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("pkscreener")


class BatchDataFetcher:
    """
    Fetch OHLCV data for multiple symbols concurrently.

    Wraps any single-symbol data loader callable and adds:
    - Thread pool concurrency
    - In-memory result cache with TTL
    - Per-symbol timeout
    - Error isolation and reporting
    """

    def __init__(
        self,
        data_loader: Callable[[str], pd.DataFrame],
        max_workers: int = 8,
        cache_ttl: float = 300.0,
        per_symbol_timeout: float = 30.0,
    ):
        """
        Args:
            data_loader: Callable that takes a symbol string and returns a DataFrame
            max_workers: Maximum concurrent fetch threads
            cache_ttl: Cache time-to-live in seconds (0 = no caching)
            per_symbol_timeout: Maximum seconds to wait per symbol fetch
        """
        self.data_loader = data_loader
        self.max_workers = max(1, max_workers)
        self.cache_ttl = max(0.0, cache_ttl)
        self.per_symbol_timeout = max(1.0, per_symbol_timeout)

        self._cache: Dict[str, Tuple[pd.DataFrame, float]] = {}
        self._cache_lock = threading.Lock()
        self._stats = {"fetched": 0, "cached": 0, "errors": 0, "total_time": 0.0}
        self._stats_lock = threading.Lock()

    def fetch_batch(
        self,
        symbols: List[str],
        use_cache: bool = True,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch data for multiple symbols concurrently.

        Args:
            symbols: List of symbol strings
            use_cache: Whether to use cached results
            progress_callback: Optional callback(completed, total) for progress

        Returns:
            Dict mapping symbol -> DataFrame (only successful fetches)
        """
        if not symbols:
            return {}

        results: Dict[str, pd.DataFrame] = {}
        to_fetch: List[str] = []
        now = time.monotonic()

        # Check cache first
        if use_cache and self.cache_ttl > 0:
            with self._cache_lock:
                for symbol in symbols:
                    key = str(symbol).strip().upper()
                    cached = self._cache.get(key)
                    if cached and (now - cached[1]) < self.cache_ttl:
                        results[symbol] = cached[0]
                        with self._stats_lock:
                            self._stats["cached"] += 1
                    else:
                        to_fetch.append(symbol)
        else:
            to_fetch = list(symbols)

        if not to_fetch:
            if progress_callback:
                progress_callback(len(symbols), len(symbols))
            return results

        start_time = time.monotonic()

        # Concurrent fetch
        if self.max_workers <= 1 or len(to_fetch) <= 2:
            self._fetch_sequential(to_fetch, results, progress_callback, len(symbols))
        else:
            self._fetch_concurrent(to_fetch, results, progress_callback, len(symbols))

        elapsed = time.monotonic() - start_time
        with self._stats_lock:
            self._stats["total_time"] += elapsed

        return results

    def _fetch_sequential(self, symbols, results, progress_callback, total):
        """Fetch symbols one at a time."""
        cached_count = total - len(symbols)
        for i, symbol in enumerate(symbols):
            df = self._fetch_one(symbol)
            if df is not None:
                results[symbol] = df
            if progress_callback:
                progress_callback(cached_count + i + 1, total)

    def _fetch_concurrent(self, symbols, results, progress_callback, total):
        """Fetch symbols concurrently via ThreadPoolExecutor."""
        cached_count = total - len(symbols)
        completed = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_map = {
                executor.submit(self._fetch_one, sym): sym
                for sym in symbols
            }
            for future in as_completed(future_map):
                completed += 1
                sym = future_map[future]
                try:
                    df = future.result(timeout=self.per_symbol_timeout)
                    if df is not None:
                        results[sym] = df
                except TimeoutError:
                    logger.debug(f"Timeout fetching {sym}")
                    with self._stats_lock:
                        self._stats["errors"] += 1
                except Exception as exc:
                    logger.debug(f"Error fetching {sym}: {exc}")
                    with self._stats_lock:
                        self._stats["errors"] += 1

                if progress_callback:
                    progress_callback(cached_count + completed, total)

    def _fetch_one(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch data for a single symbol and cache the result."""
        key = str(symbol).strip().upper()
        try:
            df = self.data_loader(symbol)
            if df is not None and not df.empty:
                if self.cache_ttl > 0:
                    with self._cache_lock:
                        self._cache[key] = (df, time.monotonic())
                with self._stats_lock:
                    self._stats["fetched"] += 1
                return df
        except Exception as exc:
            logger.debug(f"Failed to fetch {symbol}: {exc}")
            with self._stats_lock:
                self._stats["errors"] += 1
        return None

    def invalidate(self, symbol: str) -> None:
        """Remove a symbol from the cache."""
        key = str(symbol).strip().upper()
        with self._cache_lock:
            self._cache.pop(key, None)

    def clear_cache(self) -> None:
        """Clear all cached data."""
        with self._cache_lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, float]:
        """Return fetch statistics."""
        with self._stats_lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        """Reset fetch statistics."""
        with self._stats_lock:
            self._stats = {"fetched": 0, "cached": 0, "errors": 0, "total_time": 0.0}

    @property
    def cache_size(self) -> int:
        """Return the number of cached entries."""
        with self._cache_lock:
            return len(self._cache)

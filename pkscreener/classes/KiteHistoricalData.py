"""
Official Kite Connect historical-data cache for PKScreener.

This is intentionally lightweight and aimed at small monitored universes first.
It caches candle history locally so the live-tick layer only has to update the
latest candle instead of rebuilding full history on every monitor cycle.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.log import default_logger

from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager


class KiteHistoricalDataProvider:
    """Fetch and cache official Kite historical candles by symbol."""

    def __init__(self) -> None:
        self.session_manager = KiteConnectSessionManager()
        self._client = None
        self._instrument_lock = threading.Lock()
        self._instrument_map: Optional[Dict[str, int]] = None

    def get_stock_data(self, symbol: str, interval: str = "day", count: int = 365) -> Optional[pd.DataFrame]:
        """Return cached or freshly fetched historical OHLCV data."""
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None

        normalized_interval = self._normalize_interval(interval)
        cache_path = self._cache_path(normalized_symbol, normalized_interval)
        if self._is_cache_usable(cache_path, normalized_interval):
            try:
                df = pd.read_csv(cache_path, parse_dates=["datetime"]).set_index("datetime")
                return df.tail(count)
            except Exception as exc:
                default_logger().debug(f"Could not read Kite history cache for {normalized_symbol}: {exc}")

        instrument_token = self._get_instrument_token(normalized_symbol)
        if instrument_token is None:
            return None

        client = self._get_client()
        lookback_days = self._estimate_lookback_days(normalized_interval, count)
        from_date = datetime.now() - timedelta(days=lookback_days)
        to_date = datetime.now()

        try:
            candles = client.historical_data(
                instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=normalized_interval,
            )
        except Exception as exc:
            default_logger().debug(f"Kite historical_data failed for {normalized_symbol}: {exc}", exc_info=True)
            return None

        if not candles:
            return None

        df = pd.DataFrame(candles)
        if df.empty:
            return None

        if "date" in df.columns:
            df["datetime"] = pd.to_datetime(df["date"])
            df = df.drop(columns=["date"])
        if "datetime" not in df.columns:
            df["datetime"] = pd.Timestamp.now()

        rename_map = {
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        }
        df = df.rename(columns=rename_map)
        keep_columns = [column for column in ["datetime", "open", "high", "low", "close", "volume"] if column in df.columns]
        df = df[keep_columns].copy()
        df = df.set_index("datetime").sort_index()
        df.to_csv(cache_path)
        return df.tail(count)

    def get_cached_stock_data(
        self,
        symbol: str,
        interval: str = "day",
        count: int = 365,
    ) -> Optional[pd.DataFrame]:
        """Return only locally cached OHLCV data without making any network calls."""
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None

        normalized_interval = self._normalize_interval(interval)
        cache_path = self._cache_path(normalized_symbol, normalized_interval)
        if not self._is_cache_usable(cache_path, normalized_interval):
            return None

        try:
            df = pd.read_csv(cache_path, parse_dates=["datetime"]).set_index("datetime")
            return df.tail(count)
        except Exception as exc:
            default_logger().debug(
                f"Could not read cached Kite history for {normalized_symbol}: {exc}",
                exc_info=True,
            )
            return None

    def get_history_range(
        self,
        symbol: str,
        interval: str = "5minute",
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> Optional[pd.DataFrame]:
        """Fetch official Kite historical candles for an explicit date range."""
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None

        instrument_token = self._get_instrument_token(normalized_symbol)
        if instrument_token is None:
            return None

        normalized_interval = self._normalize_interval(interval)
        client = self._get_client()
        from_date = from_date or (datetime.now() - timedelta(days=30))
        to_date = to_date or datetime.now()

        try:
            candles = client.historical_data(
                instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=normalized_interval,
            )
        except Exception as exc:
            default_logger().debug(
                f"Kite ranged historical_data failed for {normalized_symbol}: {exc}",
                exc_info=True,
            )
            return None

        if not candles:
            return None

        df = pd.DataFrame(candles)
        if df.empty:
            return None

        if "date" in df.columns:
            df["datetime"] = pd.to_datetime(df["date"])
            df = df.drop(columns=["date"])
        if "datetime" not in df.columns:
            df["datetime"] = pd.Timestamp.now()

        keep_columns = [column for column in ["datetime", "open", "high", "low", "close", "volume"] if column in df.columns]
        df = df[keep_columns].copy()
        return df.set_index("datetime").sort_index()

    def prewarm_symbols(
        self,
        symbols: List[str],
        interval: str = "day",
        count: int = 365,
        max_workers: int = 4,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetch and cache history for a symbol list, returning a compact summary."""
        normalized_symbols = [
            str(symbol).strip().upper()
            for symbol in (symbols or [])
            if str(symbol).strip()
        ]
        if not normalized_symbols:
            return pd.DataFrame(
                columns=["Symbol", "Status", "Rows", "Source", "CachePath", "CheckedAt"]
            )

        normalized_interval = self._normalize_interval(interval)
        workers = max(1, int(max_workers or 1))
        rows = []

        if workers <= 1 or len(normalized_symbols) <= 1:
            for symbol in normalized_symbols:
                rows.append(
                    self._prewarm_one(symbol, normalized_interval, count, force_refresh=force_refresh)
                )
            return pd.DataFrame(rows)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(
                    self._prewarm_one,
                    symbol,
                    normalized_interval,
                    count,
                    force_refresh,
                ): symbol
                for symbol in normalized_symbols
            }
            for future in as_completed(future_map):
                try:
                    rows.append(future.result())
                except Exception as exc:
                    symbol = future_map.get(future, "UNKNOWN")
                    rows.append(
                        {
                            "Symbol": symbol,
                            "Status": f"error: {exc}",
                            "Rows": 0,
                            "Source": "error",
                            "CachePath": self._cache_path(symbol, normalized_interval),
                            "CheckedAt": datetime.now().isoformat(timespec="seconds"),
                        }
                    )

        summary_df = pd.DataFrame(rows)
        if not summary_df.empty and "Symbol" in summary_df.columns:
            summary_df.sort_values(["Status", "Symbol"], ascending=[True, True], inplace=True)
        return summary_df

    def _get_client(self):
        if self._client is None:
            self._client = self.session_manager._build_client()
        return self._client

    def _prewarm_one(
        self,
        symbol: str,
        normalized_interval: str,
        count: int,
        force_refresh: bool = False,
    ) -> Dict[str, object]:
        """Fetch/cache one symbol and report the outcome."""
        cache_path = self._cache_path(symbol, normalized_interval)
        checked_at = datetime.now().isoformat(timespec="seconds")

        if force_refresh:
            try:
                if os.path.exists(cache_path):
                    os.remove(cache_path)
            except Exception:
                pass

        source = "cache"
        if not self._is_cache_usable(cache_path, normalized_interval):
            source = "kite"

        df = self.get_stock_data(symbol, interval=normalized_interval, count=count)
        row_count = len(df) if df is not None else 0
        status = "ok" if df is not None and row_count > 0 else "missing"
        return {
            "Symbol": symbol,
            "Status": status,
            "Rows": int(row_count),
            "Source": source if status == "ok" else "none",
            "CachePath": cache_path,
            "CheckedAt": checked_at,
        }

    def _get_instrument_token(self, symbol: str) -> Optional[int]:
        instrument_map = self._load_instrument_map()
        return instrument_map.get(symbol)

    def _load_instrument_map(self) -> Dict[str, int]:
        if self._instrument_map is not None:
            return self._instrument_map

        with self._instrument_lock:
            if self._instrument_map is not None:
                return self._instrument_map

            cache_path = os.path.join(Archiver.get_user_data_dir(), "kite_instruments_nse_latest.csv")
            try:
                if os.path.exists(cache_path):
                    df = pd.read_csv(cache_path)
                    if not df.empty and {"tradingsymbol", "instrument_token"}.issubset(df.columns):
                        self._instrument_map = {
                            str(row["tradingsymbol"]).strip().upper(): int(row["instrument_token"])
                            for _, row in df.iterrows()
                        }
                        return self._instrument_map
            except Exception as exc:
                default_logger().debug(f"Could not read cached Kite instruments: {exc}")

            client = self._get_client()
            instruments = client.instruments("NSE")
            instruments_df = pd.DataFrame(instruments or [])
            if instruments_df.empty:
                self._instrument_map = {}
                return self._instrument_map

            filtered = instruments_df[instruments_df["segment"].astype(str).str.upper().eq("NSE")].copy()
            try:
                filtered.to_csv(cache_path, index=False)
            except Exception:
                pass
            self._instrument_map = {
                str(row["tradingsymbol"]).strip().upper(): int(row["instrument_token"])
                for _, row in filtered.iterrows()
            }
            return self._instrument_map

    def _cache_path(self, symbol: str, interval: str) -> str:
        cache_dir = os.path.join(Archiver.get_user_data_dir(), "kite_history_cache", interval)
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, f"{symbol}.csv")

    def cache_dir_for_interval(self, interval: str) -> str:
        """Return the cache directory for a normalized interval."""
        normalized_interval = self._normalize_interval(interval)
        cache_dir = os.path.join(Archiver.get_user_data_dir(), "kite_history_cache", normalized_interval)
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    def _is_cache_usable(self, path: str, interval: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            modified_at = datetime.fromtimestamp(os.path.getmtime(path))
            if interval == "day":
                return modified_at.date() >= datetime.now().date()
            return (datetime.now() - modified_at).total_seconds() <= 1800
        except Exception:
            return False

    def _normalize_interval(self, interval: str) -> str:
        mapping = {
            "1m": "minute",
            "3m": "3minute",
            "5m": "5minute",
            "10m": "10minute",
            "15m": "15minute",
            "30m": "30minute",
            "60m": "60minute",
            "1h": "60minute",
            "1d": "day",
            "day": "day",
        }
        return mapping.get(str(interval or "1d").strip().lower(), "day")

    def _estimate_lookback_days(self, interval: str, count: int) -> int:
        if interval == "day":
            return max(count + 30, 400)
        if interval in ["60minute", "30minute", "15minute", "10minute", "5minute", "3minute", "minute"]:
            return max(30, int(count / 20) + 10)
        return max(60, count)

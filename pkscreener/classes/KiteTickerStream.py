"""
Official KiteTicker live snapshot stream for PKScreener.

This module connects to Zerodha's official Kite WebSocket feed using the saved
official Kite Connect access token, subscribes to a chosen symbol universe, and
persists the latest full ticks locally for other PKScreener modes to reuse.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

try:
    import orjson as _json_fast
    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.log import default_logger

from pkscreener.classes.Fetcher import screenerStockDataFetcher
from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager


UNIVERSE_FILES = {
    "allstocks": os.path.join("results", "Indices", "EQUITY_L.csv"),
    "nifty50": os.path.join("results", "Indices", "ind_nifty50list.csv"),
    "next50": os.path.join("results", "Indices", "ind_niftynext50list.csv"),
    "nifty100": os.path.join("results", "Indices", "ind_nifty100list.csv"),
    "nifty200": os.path.join("results", "Indices", "ind_nifty200list.csv"),
    "nifty500": os.path.join("results", "Indices", "ind_nifty500list.csv"),
    "midcap50": os.path.join("results", "Indices", "ind_niftymidcap50list.csv"),
    "midcap100": os.path.join("results", "Indices", "ind_niftymidcap100list.csv"),
    "midcap150": os.path.join("results", "Indices", "ind_niftymidcap150list.csv"),
    "smallcap50": os.path.join("results", "Indices", "ind_niftysmallcap50list.csv"),
    "smallcap100": os.path.join("results", "Indices", "ind_niftysmallcap100list.csv"),
    "smallcap250": os.path.join("results", "Indices", "ind_niftysmallcap250list.csv"),
    # BSE universes
    "bse_allstocks": os.path.join("results", "Indices", "BSE_EQUITY_L.csv"),
    "bse_tradeable": os.path.join("results", "Indices", "BSE_TRADEABLE.csv"),
    "bse_uncached": os.path.join("results", "Indices", "BSE_UNCACHED.csv"),
    # Combined NSE + BSE (deduplicated)
    "all": os.path.join("results", "Indices", "NSE_BSE_COMBINED.csv"),
}

PERSISTED_DEPTH_LEVELS = 5


def _output_dir() -> str:
    return Archiver.get_user_data_dir()


def _latest_ticks_json_path(universe: str) -> str:
    return os.path.join(_output_dir(), f"kiteticker_{universe}_ticks_latest.json")


def _latest_ticks_csv_path(universe: str) -> str:
    return os.path.join(_output_dir(), f"kiteticker_{universe}_ticks_latest.csv")


def _latest_status_path(universe: str) -> str:
    return os.path.join(_output_dir(), f"kiteticker_{universe}_status_latest.json")


def load_latest_live_ticks(universe: str) -> Dict[str, Dict]:
    """Load the latest live tick snapshot by symbol for a universe."""
    path = _latest_ticks_json_path(str(universe or "").strip().lower())
    if not os.path.exists(path):
        return {}
    try:
        if _HAS_ORJSON:
            with open(path, "rb") as handle:
                payload = _json_fast.loads(handle.read())
        else:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        default_logger().debug(f"Could not read latest Kite live ticks: {exc}", exc_info=True)
        return {}


def _load_universe_symbols(config_manager, universe: str) -> List[str]:
    normalized = str(universe or "").strip().lower()
    if normalized == "watchlist":
        fetcher = screenerStockDataFetcher(config_manager)
        symbols = fetcher.fetchWatchlist()
        return [str(s).strip().upper() for s in symbols if str(s).strip()] if symbols else []

    csv_path = UNIVERSE_FILES.get(normalized)
    if not csv_path or not os.path.exists(csv_path):
        return []

    try:
        df = pd.read_csv(csv_path)
        symbol_column = next(
            (candidate for candidate in ["Symbol", "SYMBOL", "symbol"] if candidate in df.columns),
            None,
        )
        if symbol_column is None:
            return []
        return [
            str(symbol).strip().upper()
            for symbol in df[symbol_column].tolist()
            if str(symbol).strip()
        ]
    except Exception as exc:
        default_logger().debug(f"Failed to load universe {normalized}: {exc}", exc_info=True)
        return []


class KiteTickerStreamRunner:
    """Run an official KiteTicker stream and persist live full-tick snapshots."""

    def __init__(
        self,
        config_manager,
        universe: str = "nifty50",
        limit: Optional[int] = None,
        save_interval_seconds: float = 0.5,
        run_seconds: Optional[int] = None,
    ) -> None:
        self.config_manager = config_manager
        self.universe = str(universe or "nifty50").strip().lower()
        self.limit = int(limit) if limit not in [None, ""] else None
        self.save_interval_seconds = max(0.2, float(save_interval_seconds or 0.5))
        self.run_seconds = int(run_seconds) if run_seconds not in [None, ""] else None
        self.session_manager = KiteConnectSessionManager()

        self._symbols: List[str] = []
        self._token_to_symbol: Dict[int, str] = {}
        self._token_to_exchange: Dict[int, str] = {}
        self._ticks_by_symbol: Dict[str, Dict] = {}
        self._status: Dict[str, object] = {}
        self._connected_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._save_thread: Optional[threading.Thread] = None
        self._ticker = None
        self._started_at = datetime.now()

    def run(self) -> None:
        """Start the WebSocket stream until interrupted or timed out."""
        report = self.session_manager.get_connection_report()
        if report.get("error"):
            raise RuntimeError(
                "Official Kite session is not usable right now. Run --kiteconnectdoctor first "
                f"and fix: {report.get('error')}"
            )

        self._symbols = _load_universe_symbols(self.config_manager, self.universe)
        if self.limit and self.limit > 0:
            self._symbols = self._symbols[: self.limit]

        if not self._symbols:
            raise RuntimeError(f"No symbols available for KiteTicker universe: {self.universe}")

        tokens, missing_symbols = self._resolve_tokens(self._symbols)
        if not tokens:
            raise RuntimeError(
                f"Could not map any symbols in universe {self.universe} to official Kite instrument tokens."
            )

        self._status = {
            "universe": self.universe,
            "requested_symbols": len(self._symbols),
            "subscribed_symbols": len(tokens),
            "missing_symbols": missing_symbols,
            "missing_symbol_count": len(missing_symbols),
            "connected": False,
            "last_tick_at": "",
            "last_save_at": "",
            "ticks_seen": 0,
            "stream_mode": "full",
            "persisted_depth_levels_per_side": PERSISTED_DEPTH_LEVELS,
            "api_user_id": report.get("saved_user_id", ""),
            "started_at": self._started_at.isoformat(timespec="seconds"),
        }
        self._persist_status()

        from kiteconnect import KiteTicker

        access_token = self.session_manager._get_saved_access_token()
        self._ticker = KiteTicker(
            self.session_manager.api_key,
            access_token,
            reconnect=True,
            reconnect_max_tries=50,
            reconnect_max_delay=60,
            connect_timeout=30,
        )

        self._ticker.on_connect = self._on_connect
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_reconnect = self._on_reconnect
        self._ticker.on_noreconnect = self._on_noreconnect

        self._save_thread = threading.Thread(target=self._save_loop, daemon=True)
        self._save_thread.start()

        OutputControls().printOutput(
            f"  [+] Starting official KiteTicker stream for {len(tokens)} symbols from {self.universe}..."
        )
        self._ticker.connect(threaded=True)

        if not self._connected_event.wait(timeout=30):
            raise RuntimeError("KiteTicker did not confirm a connection within 30 seconds.")

        started = time.time()
        try:
            while not self._stop_event.is_set():
                if self.run_seconds and (time.time() - started) >= self.run_seconds:
                    OutputControls().printOutput(
                        f"  [+] KiteTicker timed run reached {self.run_seconds} seconds. Stopping..."
                    )
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            OutputControls().printOutput("  [+] Stopping official KiteTicker stream...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the ticker and persist final outputs."""
        self._stop_event.set()
        try:
            if self._ticker is not None:
                try:
                    self._ticker.close()
                except Exception:
                    pass
        finally:
            if self._save_thread and self._save_thread.is_alive():
                self._save_thread.join(timeout=max(2, self.save_interval_seconds + 1))
            self._status["stopped_at"] = datetime.now().isoformat(timespec="seconds")
            self._persist_snapshots()
            self._persist_status()

    def _resolve_tokens(self, symbols: List[str]) -> Tuple[List[int], List[str]]:
        """Map symbol list to official NSE and BSE instrument tokens."""
        self.session_manager.ensure_fresh_session()
        client = self.session_manager._build_client()
        symbol_to_instrument: Dict[str, Tuple[int, str]] = {}

        for exchange in ("NSE", "BSE"):
            try:
                instruments = client.instruments(exchange)
                instruments_df = pd.DataFrame(instruments or [])
                if instruments_df.empty:
                    continue
                if "tradingsymbol" in instruments_df.columns and "instrument_token" in instruments_df.columns:
                    filtered = instruments_df[
                        instruments_df["segment"].astype(str).str.upper().eq(exchange)
                    ]
                    for _, row in filtered.iterrows():
                        try:
                            sym = str(row["tradingsymbol"]).strip().upper()
                            # NSE tokens take priority over BSE for dual-listed
                            if sym not in symbol_to_instrument or exchange == "NSE":
                                symbol_to_instrument[sym] = (
                                    int(row["instrument_token"]),
                                    exchange,
                                )
                        except Exception:
                            continue
            except Exception as exc:
                default_logger().debug(f"Failed to fetch {exchange} instruments for ticker: {exc}")

        if not symbol_to_instrument:
            return [], list(symbols)

        tokens: List[int] = []
        missing: List[str] = []
        for symbol in symbols:
            instrument = symbol_to_instrument.get(str(symbol).strip().upper())
            if instrument is None:
                missing.append(str(symbol).strip().upper())
                continue
            token, exchange = instrument
            tokens.append(token)
            self._token_to_symbol[int(token)] = str(symbol).strip().upper()
            self._token_to_exchange[int(token)] = str(exchange).strip().upper()

        # The shared intraday event lane needs same-bar benchmark context.
        # NIFTY 50's Kite instrument token is stable and index-only; it cannot
        # be routed to an equity order. Keep this subscription configurable.
        benchmark_enabled = str(os.environ.get("KITE_TICKER_INCLUDE_BENCHMARK", "1") or "1").strip().lower() in {
            "1", "true", "yes", "y", "on"
        }
        if benchmark_enabled:
            benchmark_token = int(os.environ.get("KITE_TICKER_BENCHMARK_TOKEN", "256265") or 256265)
            benchmark_symbol = str(os.environ.get("KITE_TICKER_BENCHMARK_SYMBOL", "NIFTY 50") or "NIFTY 50").strip().upper()
            if benchmark_token not in tokens:
                tokens.append(benchmark_token)
            self._token_to_symbol[benchmark_token] = benchmark_symbol
            self._token_to_exchange[benchmark_token] = "NSE"

        # Persist the latest mapping for inspection/debugging.
        mapping_rows = [
            {
                "Symbol": symbol,
                "InstrumentToken": token,
                "Exchange": self._token_to_exchange.get(token, ""),
            }
            for token, symbol in self._token_to_symbol.items()
        ]
        if mapping_rows:
            pd.DataFrame(mapping_rows).sort_values("Symbol").to_csv(
                os.path.join(_output_dir(), f"kiteticker_{self.universe}_tokens_latest.csv"),
                index=False,
            )

        return tokens, missing

    def _on_connect(self, ws, response) -> None:
        tokens = list(self._token_to_symbol.keys())
        chunk_size = 500
        for index in range(0, len(tokens), chunk_size):
            chunk = tokens[index : index + chunk_size]
            ws.subscribe(chunk)
            ws.set_mode(ws.MODE_FULL, chunk)

        self._status["connected"] = True
        self._status["connected_at"] = datetime.now().isoformat(timespec="seconds")
        self._persist_status()
        self._connected_event.set()
        OutputControls().printOutput(
            f"  [+] Official KiteTicker connected. Subscribed to {len(tokens)} symbols in full mode."
        )

    def _on_ticks(self, ws, ticks) -> None:
        received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            for tick in ticks or []:
                try:
                    token = int(tick.get("instrument_token"))
                except Exception:
                    continue
                symbol = self._token_to_symbol.get(token)
                if not symbol:
                    continue
                self._ticks_by_symbol[symbol] = self._compact_tick(symbol, tick, received_at)
                self._status["ticks_seen"] = int(self._status.get("ticks_seen", 0) or 0) + 1
                self._status["last_tick_at"] = received_at

    def _compact_tick(self, symbol: str, tick: Dict, received_at: str) -> Dict:
        ohlc = tick.get("ohlc", {}) or {}
        depth = tick.get("depth", {}) or {}
        compact_depth = {
            side: [
                {
                    "price": level.get("price"),
                    "quantity": level.get("quantity"),
                    "orders": level.get("orders"),
                }
                for level in list(depth.get(side) or [])[
                    :PERSISTED_DEPTH_LEVELS
                ]
                if isinstance(level, dict)
            ]
            for side in ("buy", "sell")
        }
        best_bid_level = (compact_depth["buy"] or [{}])[0] or {}
        best_ask_level = (compact_depth["sell"] or [{}])[0] or {}
        best_bid = best_bid_level.get("price")
        best_ask = best_ask_level.get("price")
        return {
            "symbol": symbol,
            "instrument_token": tick.get("instrument_token"),
            "exchange": self._token_to_exchange.get(
                int(tick.get("instrument_token") or 0), ""
            ),
            "mode": tick.get("mode", ""),
            "last_price": tick.get("last_price"),
            "change": tick.get("change"),
            "volume_traded": tick.get("volume_traded"),
            "average_traded_price": tick.get("average_traded_price"),
            "last_traded_quantity": tick.get("last_traded_quantity"),
            "total_buy_quantity": tick.get("total_buy_quantity"),
            "total_sell_quantity": tick.get("total_sell_quantity"),
            "open": ohlc.get("open"),
            "high": ohlc.get("high"),
            "low": ohlc.get("low"),
            "close": ohlc.get("close"),
            "last_trade_time": str(tick.get("last_trade_time") or ""),
            "exchange_timestamp": str(tick.get("exchange_timestamp") or ""),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "best_bid_quantity": best_bid_level.get("quantity"),
            "best_ask_quantity": best_ask_level.get("quantity"),
            "depth": compact_depth,
            "received_at": received_at,
        }

    def _on_close(self, ws, code, reason) -> None:
        self._status["connected"] = False
        self._status["closed_at"] = datetime.now().isoformat(timespec="seconds")
        self._status["close_reason"] = f"{code}: {reason}"
        self._persist_status()
        if not self._stop_event.is_set():
            OutputControls().printOutput(f"  [+] Official KiteTicker closed: {code} {reason}")

    def _on_error(self, ws, code, reason) -> None:
        self._status["last_error"] = f"{code}: {reason}"
        self._persist_status()
        OutputControls().printOutput(f"  [+] Official KiteTicker error: {code} {reason}")

    def _on_reconnect(self, ws, attempts_count) -> None:
        self._status["reconnect_attempt"] = int(attempts_count)
        self._persist_status()
        OutputControls().printOutput(f"  [+] Official KiteTicker reconnect attempt {attempts_count}...")

    def _on_noreconnect(self, ws) -> None:
        self._status["connected"] = False
        self._status["last_error"] = "No more automatic reconnect attempts left."
        self._persist_status()
        OutputControls().printOutput("  [+] Official KiteTicker exhausted reconnect attempts.")
        self._stop_event.set()

    def _save_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(self.save_interval_seconds)
            self._persist_snapshots()
            self._persist_status()

    def _persist_snapshots(self) -> None:
        with self._lock:
            snapshot = dict(self._ticks_by_symbol)  # fast shallow copy, skip sort

        os.makedirs(_output_dir(), exist_ok=True)
        dest = _latest_ticks_json_path(self.universe)

        # Use orjson (C extension) for ~20x faster serialization vs json.dump+indent.
        # Atomic write via tempfile → rename to prevent readers seeing partial data.
        if _HAS_ORJSON:
            data_bytes = _json_fast.dumps(snapshot, option=_json_fast.OPT_SORT_KEYS)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".tmp")
            try:
                os.write(fd, data_bytes)
                os.close(fd)
                fd = -1
                os.replace(tmp, dest)  # atomic on POSIX
            except Exception:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        else:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(snapshot, handle, separators=(",", ":"), default=str)
                os.replace(tmp, dest)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

        # CSV is slower — save less frequently (every 5th cycle)
        self._csv_counter = getattr(self, "_csv_counter", 0) + 1
        if snapshot and self._csv_counter % 10 == 0:
            rows = list(snapshot.values())
            pd.DataFrame(rows).sort_values("symbol").to_csv(
                _latest_ticks_csv_path(self.universe),
                index=False,
            )
        self._status["last_save_at"] = datetime.now().isoformat(timespec="seconds")
        self._status["snapshot_symbols"] = len(snapshot)

    def _persist_status(self) -> None:
        os.makedirs(_output_dir(), exist_ok=True)
        with open(_latest_status_path(self.universe), "w", encoding="utf-8") as handle:
            json.dump(self._status, handle, indent=2, sort_keys=True, default=str)


def read_kiteticker_status(universe: str) -> Dict:
    """Read the latest saved KiteTicker status JSON."""
    path = _latest_status_path(str(universe or "").strip().lower())
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

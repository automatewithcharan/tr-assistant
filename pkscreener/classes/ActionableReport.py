"""
Actionable trade-plan report generation for local PKScreener usage.

This module bypasses the legacy menu flow and uses the existing multi-indicator
signal engine to produce a practical BUY / SELL / HOLD report with simple
entry, stop-loss, and target levels.
"""

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger

from pkscreener.classes.Fetcher import screenerStockDataFetcher
from pkscreener.classes.IncrementalIndicators import IncrementalStateRegistry
from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider
from pkscreener.classes.KiteTickerStream import load_latest_live_ticks
from pkscreener.classes.Pktalib import pktalib
from pkscreener.classes.screening.signals import SignalStrength, TradingSignals
from pkscreener.classes.ExchangeManager import (
    ExchangeManager, load_bse_universe, BSE_UNIVERSE_FILES,
    EXCHANGE_SUFFIX_NSE, EXCHANGE_SUFFIX_BSE,
)
from pkscreener.classes.UniversePreFilter import UniversePreFilter
from pkscreener.classes.RankingEngine import RankingEngine
from pkscreener.classes.AdvancedFilters import SignalQualityFilter, GARCHVolatility


class ActionableReportRunner:
    """Generate an actionable local report from PKScreener signals."""

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
        "fo": os.path.join("results", "Indices", "NSE_FO_SosScheme.csv"),
        # BSE universes
        "bse_allstocks": os.path.join("results", "Indices", "BSE_EQUITY_L.csv"),
        "bse_tradeable": os.path.join("results", "Indices", "BSE_TRADEABLE.csv"),
        "bse_uncached": os.path.join("results", "Indices", "BSE_UNCACHED.csv"),
        "bse500": os.path.join("results", "Indices", "BSE500.csv"),
        "bse_midcap": os.path.join("results", "Indices", "BSE_MIDCAP.csv"),
        "bse_smallcap": os.path.join("results", "Indices", "BSE_SMALLCAP.csv"),
        # Combined NSE + BSE (deduplicated)
        "all": os.path.join("results", "Indices", "NSE_BSE_COMBINED.csv"),
    }

    def __init__(
        self,
        config_manager,
        universe: str = "nifty50",
        limit: int = 50,
        min_confidence: float = 10.0,
        budget_amount: Optional[float] = None,
        risk_percent: float = 1.0,
        min_trade_value: float = 10000000.0,
        min_volume_ratio: float = 1.0,
        max_workers: int = 0,  # 0 = auto, tunable through ACTIONABLE_MAX_WORKERS
        use_live_kite: bool = False,
        strong_buy_only: bool = False,
    ):
        self.config_manager = config_manager
        self.universe = universe
        self.limit = limit
        self.min_confidence = min_confidence
        self.budget_amount = float(budget_amount) if budget_amount not in [None, ""] else None
        self.risk_percent = float(risk_percent)
        self.min_trade_value = float(min_trade_value)
        self.min_volume_ratio = float(min_volume_ratio)
        auto_worker_cap = int(os.environ.get("ACTIONABLE_MAX_WORKERS", "16") or 16)
        auto_workers = min(max(1, auto_worker_cap), max(4, (os.cpu_count() or 2) * 2))
        self.max_workers = max(1, int(max_workers or 0)) if max_workers else auto_workers
        self.use_live_kite = bool(use_live_kite)
        self.strong_buy_only = bool(strong_buy_only)
        self.buy_ready_max_gap_pct = 0.60
        self.strong_buy_max_gap_pct = 0.35
        self.momentum_buy_min_score = 75.0
        self.momentum_watch_min_score = 60.0
        self.buy_now_min_volume_ratio = 1.3
        self.strong_buy_min_confidence = max(float(self.min_confidence) + 7.5, 22.5)
        self.strong_buy_min_volume_ratio = max(float(self.min_volume_ratio) + 0.35, 1.35)
        self.strong_buy_min_trade_value = max(float(self.min_trade_value) * 1.5, 15000000.0)
        self.fetcher = screenerStockDataFetcher(config_manager)
        self.signals = TradingSignals(config_manager)
        self._thread_local = threading.local()
        self._prepared_cache: Dict[str, pd.DataFrame] = {}
        self._prepared_cache_lock = threading.Lock()
        self._prepared_cache_date = PKDateUtilities.currentDateTime().date()
        self._analysis_cache: Dict[str, Tuple[float, Dict]] = {}  # symbol -> (timestamp, row)
        self._analysis_cache_lock = threading.Lock()
        self._analysis_cache_ttl = 120  # seconds – skip recompute if data fingerprint unchanged (2x the ~66s cycle)
        self._incr_registry = IncrementalStateRegistry()  # per-symbol incremental indicator state
        self.live_ticks: Dict[str, Dict] = {}
        self.kite_history_provider = KiteHistoricalDataProvider() if self.use_live_kite else None
        self._pre_filter = UniversePreFilter(
            min_price=10.0,
            max_price=50000.0,
            min_avg_volume=50000,
            min_avg_trade_value=float(self.min_trade_value) * 0.5,
            min_candles=60,
        )
        self._signal_quality_filter = SignalQualityFilter()
        self._garch = GARCHVolatility()

    def get_universe_symbols(self) -> List[str]:
        """Return symbols for the selected universe."""
        return self._load_universe_symbols()

    def preload_symbols(
        self,
        symbols: List[str],
        cache_only: bool = True,
        show_progress: bool = False,
    ) -> Tuple[int, int]:
        """Preload prepared OHLCV frames into memory for faster live monitoring."""
        if not symbols:
            return 0, 0

        total = len(symbols)
        loaded = 0
        missing = 0

        for index, symbol in enumerate(symbols, start=1):
            try:
                prepared = None
                if cache_only and self.kite_history_provider is not None:
                    history_count = max(self._estimate_history_count(), 120)
                    cached_df = self.kite_history_provider.get_cached_stock_data(
                        symbol,
                        interval=self.config_manager.duration,
                        count=history_count,
                    )
                    prepared = self._normalize_if_ohlcv(cached_df)
                else:
                    prepared = self._load_prepared_base_data(symbol, self.fetcher)

                if prepared is not None and not prepared.empty and len(prepared) >= 60:
                    self._store_cached_prepared_data(symbol, prepared)
                    loaded += 1
                else:
                    missing += 1
            except Exception as exc:
                default_logger().debug(
                    f"Preload failed for {symbol}: {exc}",
                    exc_info=True,
                )
                missing += 1

            if show_progress and (index % 100 == 0 or index == total):
                OutputControls().printOutput(
                    f"  [+] Preloaded {index}/{total} symbols into live memory...", end="\r"
                )

        if show_progress:
            OutputControls().printOutput("")
        return loaded, missing

    def build_report(
        self,
        symbols: Optional[List[str]] = None,
        show_progress: bool = False,
        progress_label: str = "Processed",
        progress_callback: Optional[Callable[[List[Dict], int, int], None]] = None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """Build report data without printing or saving side effects."""
        symbols = symbols or self._load_universe_symbols()
        if not symbols:
            return None, None
        self._refresh_live_ticks()

        rows = self._collect_rows(
            symbols=symbols[: len(symbols)],
            show_progress=show_progress,
            progress_label=progress_label,
            progress_callback=progress_callback,
        )

        return self._build_from_rows(rows)

    def _build_from_rows(self, rows: List[Dict]) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """Finalize raw rows into sorted full and actionable report frames."""
        if not rows:
            return None, None
        report_df = pd.DataFrame(rows)

        # Compute composite rank using multi-factor ranking engine
        ranking_engine = RankingEngine()
        report_df = ranking_engine.rank_dataframe(report_df)

        report_df["ActionRank"] = report_df["Action"].map(
            {
                "BUY": 0,
                "WATCH BUY": 1,
                "HOLD": 2,
                "WATCH SELL": 3,
                "SELL/EXIT": 4,
            }
        ).fillna(9)
        report_df["NowActionRank"] = report_df["NowAction"].map(
            {
                "Strong Buy Now": 0,
                "Buy Now": 1,
                "Sell Now": 2,
                "Watch for Buy": 3,
                "Watch for Sell": 4,
                "Hold / Do Nothing": 5,
            }
        ).fillna(9)
        report_df.sort_values(
            by=["NowActionRank", "ActionRank", "CompositeRank", "ConfidencePct", "Symbol"],
            ascending=[True, True, False, False, True],
            inplace=True,
        )

        actionable_df = report_df[
            (report_df["Action"] != "HOLD")
            & (report_df["ConfidencePct"] >= self.min_confidence)
        ].copy()
        return report_df, actionable_df

    def run(self) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """Generate and save actionable reports."""
        symbols = self._load_universe_symbols()
        if not symbols:
            OutputControls().printOutput(
                "  [+] No symbols found for actionable report generation."
            )
            return None, None
        self._refresh_live_ticks()

        total = min(len(symbols), self.limit)
        OutputControls().printOutput(
            f"  [+] Building actionable report for {total} symbols from {self.universe}..."
        )

        scanned_symbols = symbols[:total]
        rows = self._collect_rows(
            symbols=scanned_symbols,
            show_progress=True,
            progress_label="Processed",
        )

        if not rows:
            OutputControls().printOutput(
                "  [+] No actionable rows could be generated from the selected universe."
            )
            return None, None

        report_df, actionable_df = self._build_from_rows(rows)
        if report_df is None or actionable_df is None:
            OutputControls().printOutput(
                "  [+] No actionable rows could be generated from the selected universe."
            )
            return None, None

        self._save_reports(report_df, actionable_df)
        self._print_summary(report_df, actionable_df)

        report_df.drop(columns=["ActionRank"], inplace=True, errors="ignore")
        report_df.drop(columns=["NowActionRank"], inplace=True, errors="ignore")
        actionable_df.drop(columns=["ActionRank"], inplace=True, errors="ignore")
        actionable_df.drop(columns=["NowActionRank"], inplace=True, errors="ignore")
        return report_df, actionable_df

    def _load_universe_symbols(self) -> List[str]:
        """Load stock symbols from the selected universe."""
        if self.universe == "watchlist":
            symbols = self.fetcher.fetchWatchlist()
            return [str(s).strip().upper() for s in symbols] if symbols else []

        csv_path = self.UNIVERSE_FILES.get(self.universe)
        if not csv_path or not os.path.exists(csv_path):
            default_logger().warning(
                f"Actionable universe file not found for {self.universe}: {csv_path}"
            )
            return []

        try:
            universe_df = pd.read_csv(csv_path)
            symbol_column = None
            for candidate in ["Symbol", "SYMBOL", "symbol"]:
                if candidate in universe_df.columns:
                    symbol_column = candidate
                    break
            if symbol_column is None:
                return []
            return [
                str(symbol).strip().upper()
                for symbol in universe_df[symbol_column].tolist()
                if str(symbol).strip()
            ]
        except Exception as exc:
            default_logger().debug(f"Failed loading actionable universe: {exc}", exc_info=True)
            return []

    def _refresh_live_ticks(self) -> None:
        """Load the latest official Kite live ticks for this universe if requested."""
        self._reset_prepared_cache_if_day_changed()
        if not self.use_live_kite:
            self.live_ticks = {}
            return
        self.live_ticks = load_latest_live_ticks(self.universe)

    def _reset_prepared_cache_if_day_changed(self) -> None:
        """Clear the in-memory OHLCV cache once the trading date changes."""
        current_date = PKDateUtilities.currentDateTime().date()
        if current_date == self._prepared_cache_date:
            return
        with self._prepared_cache_lock:
            self._prepared_cache.clear()
            self._prepared_cache_date = current_date
        with self._analysis_cache_lock:
            self._analysis_cache.clear()

    def _get_cached_prepared_data(self, symbol: str) -> Optional[pd.DataFrame]:
        """Return cached in-memory prepared OHLCV data when available (shallow copy for speed)."""
        normalized_symbol = str(symbol or "").strip().upper()
        with self._prepared_cache_lock:
            cached = self._prepared_cache.get(normalized_symbol)
            if cached is None:
                return None
            return cached.copy(deep=False)

    def _store_cached_prepared_data(self, symbol: str, df: pd.DataFrame) -> None:
        """Persist a clean base OHLCV frame for reuse across live monitor cycles."""
        if df is None or df.empty:
            return
        normalized_symbol = str(symbol or "").strip().upper()
        with self._prepared_cache_lock:
            self._prepared_cache[normalized_symbol] = df

    def _load_prepared_base_data(self, symbol: str, fetcher) -> pd.DataFrame:
        """Load a reusable OHLCV frame, preferring local caches before expensive fetches."""
        cached = self._get_cached_prepared_data(symbol)
        if cached is not None:
            return cached

        history_count = max(self._estimate_history_count(), 120)
        data = None

        if self.use_live_kite and self.kite_history_provider is not None:
            try:
                data = self.kite_history_provider.get_cached_stock_data(
                    symbol,
                    interval=self.config_manager.duration,
                    count=history_count,
                )
            except Exception as exc:
                default_logger().debug(
                    f"Kite history cache lookup failed for {symbol}: {exc}",
                    exc_info=True,
                )

        if data is None:
            # Use BSE suffix for BSE-only universes, NSE otherwise
            _suffix = EXCHANGE_SUFFIX_BSE if self.universe.startswith("bse") else EXCHANGE_SUFFIX_NSE
            if self.universe == "watchlist":
                _suffix = ""
            data = fetcher.fetchStockData(
                symbol,
                period=self.config_manager.period,
                duration=self.config_manager.duration,
                exchangeSuffix=_suffix,
            )

        prepared = self._prepare_ohlcv_data(symbol, data)
        if prepared is not None and not prepared.empty:
            self._store_cached_prepared_data(symbol, prepared.copy())
            return prepared
        return pd.DataFrame()

    def _collect_rows(
        self,
        symbols: List[str],
        show_progress: bool = False,
        progress_label: str = "Processed",
        progress_callback: Optional[Callable[[List[Dict], int, int], None]] = None,
    ) -> List[Dict]:
        """Collect analyzed rows, optionally in parallel, with lightweight progress updates."""
        if not symbols:
            return []

        total = len(symbols)
        if self.max_workers <= 1 or total <= 1:
            rows: List[Dict] = []
            for index, symbol in enumerate(symbols, start=1):
                row = self._analyze_symbol(symbol)
                if row is not None:
                    rows.append(row)
                if show_progress and (index % 25 == 0 or index == total):
                    OutputControls().printOutput(
                        f"  [+] {progress_label} {index}/{total} symbols...", end="\r"
                    )
                if progress_callback and (index % 50 == 0 or index == total):
                    progress_callback(list(rows), index, total)
            if show_progress:
                OutputControls().printOutput("")
            return rows

        rows = []
        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_map = {
                executor.submit(self._analyze_symbol, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(future_map):
                completed += 1
                try:
                    row = future.result()
                    if row is not None:
                        rows.append(row)
                except Exception as exc:
                    symbol = future_map.get(future, "UNKNOWN")
                    default_logger().debug(
                        f"Actionable analysis failed for {symbol}: {exc}",
                        exc_info=True,
                    )
                if show_progress and (completed % 25 == 0 or completed == total):
                    OutputControls().printOutput(
                        f"  [+] {progress_label} {completed}/{total} symbols...", end="\r"
                    )
                if progress_callback and (completed % 50 == 0 or completed == total):
                    progress_callback(list(rows), completed, total)
        if show_progress:
            OutputControls().printOutput("")
        return rows

    def _get_thread_components(self):
        """Create per-thread fetcher/signal helpers for safe concurrent analysis."""
        if not hasattr(self._thread_local, "fetcher"):
            self._thread_local.fetcher = screenerStockDataFetcher(self.config_manager)
        if not hasattr(self._thread_local, "signals"):
            self._thread_local.signals = TradingSignals(self.config_manager)
        return self._thread_local.fetcher, self._thread_local.signals

    def _analyze_symbol(self, symbol: str) -> Optional[Dict]:
        """Analyze a single symbol and return an actionable row."""
        try:
            fetcher, signals = self._get_thread_components()
            data = self._load_prepared_base_data(symbol, fetcher)
            if len(data) < 60:
                return None

            # Pre-filter: fast liquidity/quality gates before expensive analysis
            eligible, reason = self._pre_filter.is_eligible(data)
            if not eligible:
                default_logger().debug(f"Pre-filter rejected {symbol}: {reason}")
                return None

            if self.use_live_kite and self.live_ticks:
                data = self._apply_live_tick(symbol, data)
                if data is None or len(data) < 60:
                    return None

            # Fast-path: return cached analysis if data fingerprint unchanged
            fingerprint = self._data_fingerprint(symbol, data)
            cached_row = self._get_cached_analysis(symbol, fingerprint)
            if cached_row is not None:
                return cached_row

            # Incremental indicator state: warm up once, update O(1) on each tick
            incr_indicators = self._get_incremental_indicators(symbol, data)

            row = self.analyze_prepared_data(
                symbol=symbol,
                data=data,
                signals=signals,
                live_tick_used=bool(
                    self.use_live_kite and str(symbol).strip().upper() in self.live_ticks
                ),
                incr_indicators=incr_indicators,
            )
            if row is not None:
                self._store_cached_analysis(symbol, fingerprint, row)
            return row
        except Exception as exc:
            default_logger().debug(f"Actionable analysis failed for {symbol}: {exc}", exc_info=True)
            return None

    def _get_incremental_indicators(self, symbol: str, data: pd.DataFrame) -> Optional[Dict]:
        """Return pre-computed indicator values from incremental state, or None on first call."""
        try:
            state = self._incr_registry.get_or_create(symbol)
            n = len(data)
            if not state._warmed_up:
                # First time: warm up from full history — O(n), but only once per session
                state.warm_up(data)
                return state.get_indicators() if state.is_ready else None
            elif state.candles_processed < n:
                # New candle(s) since last cycle — update incrementally O(1) per new row
                new_rows = n - state.candles_processed
                for i in range(new_rows):
                    row = data.iloc[-(new_rows - i)]
                    state.update(
                        float(row["close"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row.get("volume") or 0.0),
                    )
            return state.get_indicators() if state.is_ready else None
        except Exception as exc:
            default_logger().debug(f"Incremental state update failed for {symbol}: {exc}", exc_info=True)
            return None

    @staticmethod
    def _data_fingerprint(symbol: str, data: pd.DataFrame) -> str:
        """Quick hash of the last few rows to detect meaningful changes."""
        tail = data.tail(3)
        raw = f"{symbol}|{tail['close'].values.tobytes()}|{tail['volume'].values.tobytes()}"
        return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()

    def _get_cached_analysis(self, symbol: str, fingerprint: str) -> Optional[Dict]:
        norm = str(symbol).strip().upper()
        with self._analysis_cache_lock:
            entry = self._analysis_cache.get(norm)
            if entry is None:
                return None
            ts, fp, row = entry
            if fp != fingerprint:
                return None
            if time.time() - ts > self._analysis_cache_ttl:
                return None
            return dict(row)  # return a copy

    def _store_cached_analysis(self, symbol: str, fingerprint: str, row: Dict) -> None:
        norm = str(symbol).strip().upper()
        with self._analysis_cache_lock:
            self._analysis_cache[norm] = (time.time(), fingerprint, dict(row))

    def analyze_prepared_data(
        self,
        symbol: str,
        data: pd.DataFrame,
        signals=None,
        run_at_text: Optional[str] = None,
        live_tick_used: bool = False,
        incr_indicators: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Analyze an already prepared OHLCV frame using the same live-signal logic."""
        if data is None or len(data) < 30:
            return None

        signals = signals or self.signals
        result = signals.analyze(data, incr_indicators=incr_indicators)
        close = float(data["close"].iloc[-1])
        prev_close = float(data["close"].iloc[-2]) if len(data) >= 2 else close
        volume = float(data["volume"].iloc[-1]) if "volume" in data.columns else 0.0
        change_pct = ((close - prev_close) * 100 / prev_close) if prev_close else 0.0
        avg_volume_20 = float(data["volume"].tail(20).mean()) if "volume" in data.columns else 0.0
        volume_ratio = (volume / avg_volume_20) if avg_volume_20 else 0.0
        avg_trade_value_20 = float((data["close"].tail(20) * data["volume"].tail(20)).mean())
        atr = self._compute_atr(data, close)
        action = self._signal_to_action(result.signal)

        # --- Advanced quality gate: Kalman trend + GARCH volatility ---
        close_arr = data["close"].values.astype(float)
        sq = self._signal_quality_filter.evaluate(close_arr)
        kalman_quality = sq.get("quality_score", 50.0)
        kalman_trend = sq.get("kalman_trend", "flat")
        vol_regime = sq.get("vol_regime", "normal")

        # GARCH-adaptive ATR multiplier for stop-loss sizing
        returns_arr = data["close"].pct_change().dropna().values
        garch_atr_mult = self._garch.adaptive_atr_multiplier(returns_arr, base_multiplier=2.0)

        # Keep the anti-junk protections, but don't demote strong live movers as
        # aggressively as the late-April tuning did.
        if action in ["BUY", "WATCH BUY"]:
            if kalman_trend == "down" and kalman_quality < 30:
                action = "HOLD"  # Kalman says downtrend despite noisy price spike
            elif sq.get("noise_spike", False) and kalman_quality < 40:
                action = "WATCH BUY"  # noise spike, demote from BUY

        entry, stop_loss, target_1, target_2, risk_pct = self._build_trade_plan(
            close, atr, action, garch_atr_mult=garch_atr_mult
        )
        risk_per_share = round(max(abs(entry - stop_loss), 0.01), 2)
        entry_gap_pct = (
            round((max(entry - close, 0.0) * 100.0 / close), 2)
            if close > 0 and entry > 0
            else 0.0
        )
        trigger_ready = close >= entry if entry > 0 else False
        liquidity_ok = avg_trade_value_20 >= self.min_trade_value
        volume_ok = volume_ratio >= self.min_volume_ratio
        strong_liquidity_ok = avg_trade_value_20 >= self.strong_buy_min_trade_value
        strong_volume_ok = volume_ratio >= self.strong_buy_min_volume_ratio
        now_action, quality_checks = self._determine_now_action(
            action=action,
            confidence=float(result.confidence),
            close=close,
            entry=entry,
            stop_loss=stop_loss,
            trigger_ready=trigger_ready,
            entry_gap_pct=entry_gap_pct,
            liquidity_ok=liquidity_ok,
            volume_ok=volume_ok,
            strong_liquidity_ok=strong_liquidity_ok,
            strong_volume_ok=strong_volume_ok,
            reason_text=" | ".join(result.reasons[:5]) if result.reasons else "",
            kalman_quality=kalman_quality,
            kalman_trend=kalman_trend,
            vol_regime=vol_regime,
        )
        momentum_score, momentum_setup, momentum_reason = self._score_momentum_setup(
            action=action,
            now_action=now_action,
            confidence=float(result.confidence),
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            entry_gap_pct=entry_gap_pct,
            trigger_ready=trigger_ready,
            liquidity_ok=liquidity_ok,
            volume_ok=volume_ok,
            reason_text=" | ".join(result.reasons[:3]) if result.reasons else "",
        )
        if (
            not self.strong_buy_only
            and now_action == "Watch for Buy"
            and momentum_setup == "Momentum Buy"
            and float(result.confidence) >= self.min_confidence
            and volume_ratio >= self.buy_now_min_volume_ratio
            and kalman_quality >= 50
            and kalman_trend != "down"
        ):
            now_action = "Buy Now"
            quality_checks = f"{quality_checks}, momentum promotion".strip(", ")

        return {
            "Symbol": symbol,
            "Action": action,
            "NowAction": now_action,
            "Signal": result.signal.name.replace("_", " "),
            "ConfidencePct": round(float(result.confidence), 1),
            "LTP": round(close, 2),
            "PrevClose": round(prev_close, 2),
            "ChangePct": round(change_pct, 2),
            "Volume": int(volume) if volume == volume else 0,
            "AvgVolume20": int(avg_volume_20) if avg_volume_20 == avg_volume_20 else 0,
            "VolumeRatio": round(volume_ratio, 2),
            "AvgTradeValue20": round(avg_trade_value_20, 2),
            "TriggerReady": trigger_ready,
            "EntryGapPct": entry_gap_pct,
            "LiquidityOk": liquidity_ok,
            "VolumeOk": volume_ok,
            "StrongLiquidityOk": strong_liquidity_ok,
            "StrongVolumeOk": strong_volume_ok,
            "MomentumScore": momentum_score,
            "MomentumSetup": momentum_setup,
            "MomentumReason": momentum_reason,
            "LiveTickUsed": bool(live_tick_used),
            "Entry": entry,
            "StopLoss": stop_loss,
            "Target1": target_1,
            "Target2": target_2,
            "RiskPct": risk_pct,
            "RiskPerShare": risk_per_share,
            "QualityChecks": quality_checks,
            "Reason": " | ".join(result.reasons[:3]) if result.reasons else "No strong reason",
            "RSI": round(float(result.indicators.get("RSI", 50)), 1),
            "CCI": round(float(result.indicators.get("CCI", 0)), 1),
            "MFI": round(float(result.indicators.get("MFI", 50)), 1),
            "KalmanQuality": round(kalman_quality, 1),
            "KalmanTrend": kalman_trend,
            "VolRegime": vol_regime,
            "GARCHAtrMult": garch_atr_mult,
            "RunAt": run_at_text or PKDateUtilities.currentDateTime().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _score_momentum_setup(
        self,
        action: str,
        now_action: str,
        confidence: float,
        change_pct: float,
        volume_ratio: float,
        entry_gap_pct: float,
        trigger_ready: bool,
        liquidity_ok: bool,
        volume_ok: bool,
        reason_text: str,
    ) -> Tuple[float, str, str]:
        """Score breakout-style momentum so strong watchlist names can be promoted."""
        normalized_action = str(action or "").strip().upper()
        normalized_now_action = str(now_action or "").strip().upper()
        reason_lower = str(reason_text or "").strip().lower()
        if normalized_action not in ["BUY", "WATCH BUY"] or normalized_now_action in ["SELL NOW", "SELL / EXIT"]:
            return 0.0, "", ""

        score = 0.0
        reasons: List[str] = []
        if normalized_action == "BUY":
            score += 18.0
            reasons.append("buy trend")
        else:
            score += 10.0
            reasons.append("watchlist trend")

        score += min(max(confidence, 0.0), 30.0) * 1.6
        score += min(max(volume_ratio, 0.0), 4.0) * 8.0

        if change_pct > 0:
            score += 6.0
            reasons.append("green day")
        if change_pct >= 2.0:
            score += 8.0
            reasons.append("strong day")
        if change_pct >= 4.0:
            score += 10.0
            reasons.append("breakout day")

        if trigger_ready:
            score += 10.0
            reasons.append("trigger ready")
        elif entry_gap_pct <= 0.30:
            score += 8.0
            reasons.append("very near trigger")
        elif entry_gap_pct <= 0.60:
            score += 5.0
            reasons.append("near trigger")
        elif entry_gap_pct <= 1.00:
            score += 2.0
        else:
            score -= 8.0

        if liquidity_ok:
            score += 6.0
        if volume_ok:
            score += 6.0
        if "above average volume" in reason_lower:
            score += 4.0
            reasons.append("volume expansion")

        if "below all major mas" in reason_lower or "bearish alignment" in reason_lower:
            score -= 18.0
            reasons.append("ma contradiction")
        if change_pct < 0:
            score -= 10.0
        if volume_ratio < 1.0:
            score -= 8.0

        score = round(max(0.0, min(score, 100.0)), 1)
        setup = ""
        if (
            score >= self.momentum_buy_min_score
            and change_pct > 0
            and volume_ratio >= 1.5
            and entry_gap_pct <= 0.80
            and "ma contradiction" not in reasons
        ):
            setup = "Momentum Buy"
        elif score >= self.momentum_watch_min_score:
            setup = "Momentum Watch"
        return score, setup, ", ".join(reasons)

    def _prepare_ohlcv_data(self, symbol: str, data: Optional[pd.DataFrame]) -> pd.DataFrame:
        """Normalize the fetched frame and, if needed, fall back to official Kite history."""
        prepared = self._normalize_if_ohlcv(data)
        if prepared is None and self.use_live_kite and self.kite_history_provider is not None:
            try:
                history_count = max(self._estimate_history_count(), 120)
                prepared = self.kite_history_provider.get_stock_data(
                    symbol,
                    interval=self.config_manager.duration,
                    count=history_count,
                )
                prepared = self._normalize_if_ohlcv(prepared)
            except Exception as exc:
                default_logger().debug(f"Kite history fallback failed for {symbol}: {exc}", exc_info=True)
                prepared = None
        return prepared if prepared is not None else pd.DataFrame()

    def _normalize_if_ohlcv(self, data: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        """Return a cleaned OHLCV frame only when the input really contains candle columns."""
        if data is None or not hasattr(data, "columns"):
            return None
        normalized = self._normalize_ohlcv_columns(data.copy()).dropna()
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(set(normalized.columns)):
            return None
        return normalized

    def _estimate_history_count(self) -> int:
        """Estimate how many candles we need from history based on config period/duration."""
        period_days = {
            "1d": 1,
            "5d": 5,
            "1wk": 7,
            "1mo": 30,
            "3mo": 90,
            "6mo": 180,
            "1y": 365,
            "2y": 730,
            "5y": 1825,
            "10y": 3650,
            "max": 5000,
        }
        interval_minutes = {
            "minute": 1,
            "1m": 1,
            "3minute": 3,
            "3m": 3,
            "5minute": 5,
            "5m": 5,
            "10minute": 10,
            "10m": 10,
            "15minute": 15,
            "15m": 15,
            "30minute": 30,
            "30m": 30,
            "60minute": 60,
            "60m": 60,
            "1h": 60,
            "day": 1440,
            "1d": 1440,
        }
        days = period_days.get(str(self.config_manager.period or "1y").strip().lower(), 365)
        interval = str(self.config_manager.duration or "1d").strip().lower()
        interval_mins = interval_minutes.get(interval, 1440)
        if interval_mins >= 1440:
            return days
        return min(int((days * 375) / interval_mins), 5000)

    def _normalize_ohlcv_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize OHLCV columns to the lowercase format expected by the signal engine."""
        rename_map = {}
        for column in df.columns:
            lowered = str(column).strip().lower()
            if lowered in ["open", "high", "low", "close", "volume"]:
                rename_map[column] = lowered
        return df.rename(columns=rename_map)

    def _apply_live_tick(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """Overlay the latest official Kite tick as the newest candle in the series."""
        tick = self.live_ticks.get(str(symbol).strip().upper())
        if not tick:
            return df

        last_price = self._to_float(tick.get("last_price"))
        if last_price <= 0:
            return df

        open_price = self._first_positive(
            self._to_float(tick.get("open")),
            self._to_float(tick.get("close")),
            last_price,
        )
        high_price = self._first_positive(
            self._to_float(tick.get("high")),
            last_price,
            open_price,
        )
        low_price = self._first_positive(
            self._to_float(tick.get("low")),
            last_price,
            open_price,
        )
        volume = max(self._to_float(tick.get("volume_traded")), 0.0)

        timestamp_text = str(tick.get("exchange_timestamp") or tick.get("received_at") or "").strip()
        try:
            tick_timestamp = pd.Timestamp(timestamp_text) if timestamp_text else pd.Timestamp.now()
        except Exception:
            tick_timestamp = pd.Timestamp.now()

        # Align tick_timestamp timezone to match the DataFrame index to avoid
        # TypeError: "Cannot compare tz-naive and tz-aware timestamps" during sort_index()
        if isinstance(df.index, pd.DatetimeIndex) and len(df.index) > 0:
            df_tz = df.index.tzinfo
            if df_tz is not None and tick_timestamp.tzinfo is None:
                tick_timestamp = tick_timestamp.tz_localize(df_tz)
            elif df_tz is None and tick_timestamp.tzinfo is not None:
                tick_timestamp = tick_timestamp.tz_localize(None)

        updated_row = df.iloc[-1].copy()
        updated_row["open"] = open_price
        updated_row["high"] = max(high_price, last_price, open_price)
        updated_row["low"] = min(low_price, last_price, open_price)
        updated_row["close"] = last_price
        updated_row["volume"] = volume

        if isinstance(df.index, pd.DatetimeIndex) and len(df.index) > 0:
            last_ts = pd.Timestamp(df.index[-1])
            if last_ts.date() == tick_timestamp.date():
                df.loc[df.index[-1], ["open", "high", "low", "close", "volume"]] = [
                    updated_row["open"],
                    updated_row["high"],
                    updated_row["low"],
                    updated_row["close"],
                    updated_row["volume"],
                ]
                return df

        updated_df = pd.concat([df, pd.DataFrame([updated_row], index=[tick_timestamp])])
        updated_df = updated_df[~updated_df.index.duplicated(keep="last")]
        return updated_df.sort_index()

    def _compute_atr(self, df: pd.DataFrame, close: float) -> float:
        """Compute ATR with a safe fallback."""
        try:
            atr_series = pktalib.ATR(df["high"], df["low"], df["close"], timeperiod=14)
            atr_value = atr_series.iloc[-1] if hasattr(atr_series, "iloc") else atr_series[-1]
            if atr_value is not None and atr_value == atr_value and atr_value > 0:
                return float(atr_value)
        except Exception as exc:
            default_logger().debug(f"ATR computation failed: {exc}")
        return max(close * 0.02, 0.5)

    def _to_float(self, value) -> float:
        """Best-effort float conversion helper."""
        try:
            if value in [None, "", "None"]:
                return 0.0
            return float(value)
        except Exception:
            return 0.0

    def _first_positive(self, *values: float) -> float:
        """Return the first positive numeric candidate, else 0."""
        for value in values:
            numeric = self._to_float(value)
            if numeric > 0:
                return numeric
        return 0.0

    def _signal_to_action(self, signal: SignalStrength) -> str:
        """Map signal strength to a practical action label."""
        if signal in [SignalStrength.STRONG_BUY, SignalStrength.BUY]:
            return "BUY"
        if signal == SignalStrength.WEAK_BUY:
            return "WATCH BUY"
        if signal in [SignalStrength.STRONG_SELL, SignalStrength.SELL]:
            return "SELL/EXIT"
        if signal == SignalStrength.WEAK_SELL:
            return "WATCH SELL"
        return "HOLD"

    def _build_trade_plan(self, close: float, atr: float, action: str, garch_atr_mult: float = 2.0) -> Tuple[float, float, float, float, float]:
        """Build a simple entry / stop / target plan with GARCH-adaptive sizing."""
        buffer_amt = max(close * 0.002, atr * 0.10)
        # GARCH-adaptive risk: scale ATR multiplier based on volatility regime
        risk_mult = max(garch_atr_mult * 0.75, 1.0)  # floor at 1.0x ATR
        risk_amt = max(close * 0.015, atr * risk_mult)

        if action in ["BUY", "WATCH BUY"]:
            entry = round(close + buffer_amt, 2)
            stop_loss = round(max(entry - risk_amt, 0.01), 2)
            risk = max(entry - stop_loss, 0.01)
            target_1 = round(entry + (risk * 1.5), 2)
            target_2 = round(entry + (risk * 2.5), 2)
        elif action in ["SELL/EXIT", "WATCH SELL"]:
            entry = round(max(close - buffer_amt, 0.01), 2)
            stop_loss = round(entry + risk_amt, 2)
            risk = max(stop_loss - entry, 0.01)
            target_1 = round(max(entry - (risk * 1.5), 0.01), 2)
            target_2 = round(max(entry - (risk * 2.5), 0.01), 2)
        else:
            return round(close, 2), 0.0, 0.0, 0.0, 0.0

        risk_pct = round((risk * 100 / entry), 2) if entry else 0.0
        return entry, stop_loss, target_1, target_2, risk_pct

    def _determine_now_action(
        self,
        action: str,
        confidence: float,
        close: float,
        entry: float,
        stop_loss: float,
        trigger_ready: bool,
        entry_gap_pct: float,
        liquidity_ok: bool,
        volume_ok: bool,
        strong_liquidity_ok: bool,
        strong_volume_ok: bool,
        reason_text: str = "",
        kalman_quality: float = 50.0,
        kalman_trend: str = "flat",
        vol_regime: str = "normal",
    ) -> Tuple[str, str]:
        """Turn a raw signal into a stricter real-time decision label."""
        checks = []
        if confidence >= self.min_confidence:
            checks.append("confidence ok")
        else:
            checks.append("confidence low")
        near_trigger = bool(trigger_ready or entry_gap_pct <= self.buy_ready_max_gap_pct)
        strong_trigger = bool(trigger_ready or entry_gap_pct <= self.strong_buy_max_gap_pct)
        checks.append(
            "trigger hit"
            if trigger_ready
            else ("near trigger" if near_trigger else "waiting for trigger")
        )
        checks.append("liquidity ok" if liquidity_ok else "low liquidity")
        checks.append("volume ok" if volume_ok else "low volume")
        strong_confidence_ok = confidence >= self.strong_buy_min_confidence
        if strong_confidence_ok:
            checks.append("strong confidence")
        if strong_liquidity_ok:
            checks.append("strong liquidity")
        if strong_volume_ok:
            checks.append("strong volume")

        # Kalman quality gate
        if kalman_quality >= 60:
            checks.append("kalman quality ok")
        elif kalman_quality >= 40:
            checks.append("kalman marginal")
        else:
            checks.append("kalman weak")
        if kalman_trend == "down":
            checks.append("kalman downtrend")
        if vol_regime in ("elevated", "extreme"):
            checks.append(f"vol {vol_regime}")

        # Trend safety: block Buy Now for stocks in confirmed downtrends
        reason_lower = str(reason_text).lower()
        bearish_trend = (
            "bearish alignment" in reason_lower
            or "below all major ma" in reason_lower
            or "death cross" in reason_lower
        )
        if bearish_trend:
            checks.append("BEARISH TREND")

        normalized = str(action).strip().upper()
        if normalized in ["BUY", "WATCH BUY"]:
            # Never promote to Buy Now if stock is in a bearish trend
            if bearish_trend:
                return "Watch for Buy", ", ".join(checks)
            # Kalman quality gate: block Buy Now for weak signals
            if kalman_quality < 30 or (kalman_trend == "down" and kalman_quality < 35):
                return "Watch for Buy", ", ".join(checks)
            # Vol regime gate: block in extreme volatility
            if vol_regime == "extreme":
                return "Watch for Buy", ", ".join(checks)
            if (
                normalized == "BUY"
                and strong_confidence_ok
                and strong_trigger
                and strong_liquidity_ok
                and strong_volume_ok
                and kalman_quality >= 50
            ):
                return "Strong Buy Now", ", ".join(checks)
            if (
                not self.strong_buy_only
                and confidence >= self.min_confidence
                and near_trigger
                and liquidity_ok
                and volume_ok
                and kalman_quality >= 35
            ):
                return "Buy Now", ", ".join(checks)
            return "Watch for Buy", ", ".join(checks)
        if normalized in ["SELL/EXIT", "WATCH SELL"]:
            if close <= stop_loss or normalized == "SELL/EXIT":
                return "Sell Now", ", ".join(checks)
            return "Watch for Sell", ", ".join(checks)
        return "Hold / Do Nothing", ", ".join(checks)

    def _save_reports(self, full_df: pd.DataFrame, actionable_df: pd.DataFrame) -> None:
        """Save dated and latest report snapshots."""
        output_dir = Archiver.get_user_data_dir()
        timestamp = PKDateUtilities.currentDateTime().strftime("%Y%m%d_%H%M%S")

        full_latest = os.path.join(output_dir, f"actionable_{self.universe}_latest.csv")
        full_dated = os.path.join(output_dir, f"actionable_{self.universe}_{timestamp}.csv")
        actionable_latest = os.path.join(output_dir, f"actionable_{self.universe}_only_latest.csv")
        actionable_dated = os.path.join(output_dir, f"actionable_{self.universe}_only_{timestamp}.csv")
        simple_latest = os.path.join(output_dir, f"actionable_{self.universe}_simple_latest.csv")
        simple_dated = os.path.join(output_dir, f"actionable_{self.universe}_simple_{timestamp}.csv")
        actionable_simple_latest = os.path.join(output_dir, f"actionable_{self.universe}_only_simple_latest.csv")
        actionable_simple_dated = os.path.join(output_dir, f"actionable_{self.universe}_only_simple_{timestamp}.csv")

        full_df.drop(columns=["ActionRank"], inplace=True, errors="ignore")
        full_df.drop(columns=["NowActionRank"], inplace=True, errors="ignore")
        actionable_df.drop(columns=["ActionRank"], inplace=True, errors="ignore")
        actionable_df.drop(columns=["NowActionRank"], inplace=True, errors="ignore")

        full_df.to_csv(full_latest, index=False)
        full_df.to_csv(full_dated, index=False)
        actionable_df.to_csv(actionable_latest, index=False)
        actionable_df.to_csv(actionable_dated, index=False)
        self._build_simple_view(full_df).to_csv(simple_latest, index=False)
        self._build_simple_view(full_df).to_csv(simple_dated, index=False)
        self._build_simple_view(actionable_df).to_csv(actionable_simple_latest, index=False)
        self._build_simple_view(actionable_df).to_csv(actionable_simple_dated, index=False)

        OutputControls().printOutput(f"  [+] Saved full report: {full_latest}")
        OutputControls().printOutput(f"  [+] Saved actionable report: {actionable_latest}")
        OutputControls().printOutput(f"  [+] Saved simple report: {actionable_simple_latest}")

    def _print_summary(self, full_df: pd.DataFrame, actionable_df: pd.DataFrame) -> None:
        """Print a compact console summary."""
        action_counts = full_df["NowAction"].value_counts().to_dict()
        OutputControls().printOutput(
            f"  [+] Decision summary: {action_counts}"
        )

        if actionable_df is None or actionable_df.empty:
            OutputControls().printOutput(
                "  [+] No rows met the actionable confidence threshold in this run."
            )
            return

        preview_df = self._build_simple_view(actionable_df).head(12).copy()
        OutputControls().printOutput(
            preview_df.to_string(index=False),
            enableMultipleLineOutput=True
        )

    def _build_simple_view(self, df: pd.DataFrame) -> pd.DataFrame:
        """Create a plain-English report view for easier manual use."""
        if df is None or df.empty:
            return pd.DataFrame()

        run_at_series = df["RunAt"] if "RunAt" in df.columns else pd.Series([""] * len(df))
        quality_checks_series = df["QualityChecks"] if "QualityChecks" in df.columns else pd.Series([""] * len(df))
        entry_gap_series = df["EntryGapPct"] if "EntryGapPct" in df.columns else pd.Series([0.0] * len(df))
        momentum_score_series = df["MomentumScore"] if "MomentumScore" in df.columns else pd.Series([0.0] * len(df))
        momentum_setup_series = df["MomentumSetup"] if "MomentumSetup" in df.columns else pd.Series([""] * len(df))
        change_pct_series = df["ChangePct"] if "ChangePct" in df.columns else pd.Series([0.0] * len(df))
        risk_pct_series = df["RiskPct"] if "RiskPct" in df.columns else pd.Series([0.0] * len(df))
        volume_ratio_series = df["VolumeRatio"] if "VolumeRatio" in df.columns else pd.Series([0.0] * len(df))
        avg_trade_value_series = df["AvgTradeValue20"] if "AvgTradeValue20" in df.columns else pd.Series([0.0] * len(df))
        reason_series = df["Reason"] if "Reason" in df.columns else pd.Series([""] * len(df))
        what_to_do = [
            self._human_action_label(action, price_now, buy_above)
            for action, price_now, buy_above in zip(df["NowAction"], df["LTP"], df["Entry"])
        ]
        simple_df = pd.DataFrame(
            {
                "Stock": df["Symbol"],
                "What To Do": what_to_do,
                "Signal Strength %": df["ConfidencePct"],
                "Momentum Score": momentum_score_series,
                "Momentum Setup": momentum_setup_series,
                "Price Now": df["LTP"],
                "PrevClose": df["PrevClose"] if "PrevClose" in df.columns else df["LTP"],
                "Change %": change_pct_series,
                "Buy Above": df["Entry"],
                "Entry Gap %": entry_gap_series,
                "Sell If Below": df["StopLoss"],
                "Take Profit 1": df["Target1"],
                "Take Profit 2": df["Target2"],
                "Risk %": risk_pct_series,
                "Volume vs Avg": volume_ratio_series,
                "Avg Daily Value": avg_trade_value_series.apply(self._format_rupee_amount),
                "RSI": df["RSI"] if "RSI" in df.columns else 50.0,
                "CCI": df["CCI"] if "CCI" in df.columns else 0.0,
                "MFI": df["MFI"] if "MFI" in df.columns else 50.0,
                "Checks": quality_checks_series,
                "Why": reason_series,
                "Checked At": run_at_series,
            }
        )
        if self.budget_amount is not None and self.budget_amount > 0:
            risk_capital = round(self.budget_amount * self.risk_percent / 100.0, 2)
            if "RiskPerShare" in df.columns:
                risk_qty = df.apply(
                    lambda row: int(risk_capital // float(row["RiskPerShare"])) if float(row["RiskPerShare"]) > 0 else 0,
                    axis=1,
                )
            else:
                risk_qty = pd.Series([0] * len(df))
            qty_series = df["Entry"].apply(
                lambda value: int(self.budget_amount // float(value)) if float(value) > 0 else 0
            )
            suggested_qty = pd.concat([qty_series, risk_qty], axis=1).min(axis=1).astype(int)
            money_used = (suggested_qty * df["Entry"]).round(2)
            money_left = (self.budget_amount - money_used).round(2)
            insert_at = 5
            simple_df.insert(insert_at, "Qty By Budget", qty_series)
            simple_df.insert(insert_at + 1, "Qty By Risk", risk_qty)
            simple_df.insert(insert_at + 2, "Suggested Qty", suggested_qty)
            simple_df.insert(insert_at + 3, "Risk Money", risk_capital)
            simple_df.insert(insert_at + 4, "Money Used", money_used)
            simple_df.insert(insert_at + 5, "Money Left", money_left)
        return simple_df

    def build_simple_view(self, df: pd.DataFrame) -> pd.DataFrame:
        """Public wrapper for generating the plain-English report view."""
        return self._build_simple_view(df)

    def _human_action_label(self, action: str, price_now: float = 0.0, buy_above: float = 0.0) -> str:
        """Translate terse action codes into plain English."""
        normalized = str(action).strip().upper()
        price_now = float(price_now or 0.0)
        buy_above = float(buy_above or 0.0)

        if normalized == "STRONG BUY NOW":
            return "Strong Buy Now"
        if normalized in ["BUY NOW", "WATCH FOR BUY"]:
            return "Buy Now" if normalized == "BUY NOW" else "Watch for Buy"
        if normalized in ["BUY", "WATCH BUY"]:
            return "Buy Now" if buy_above > 0 and price_now >= buy_above else "Watch for Buy"
        if normalized == "HOLD":
            return "Hold / Do Nothing"
        if normalized in ["WATCH SELL", "WATCH FOR SELL"]:
            return "Watch for Sell"
        if normalized in ["SELL/EXIT", "SELL NOW", "SELL / EXIT"]:
            return "Sell / Exit"
        return str(action)

    def _format_rupee_amount(self, value: float) -> str:
        """Format large rupee values compactly for the simple view."""
        amount = float(value or 0.0)
        if amount >= 10000000:
            return f"{amount / 10000000:.2f} Cr"
        if amount >= 100000:
            return f"{amount / 100000:.2f} L"
        return f"{amount:.0f}"

"""
Market-Neutral Scanner — end-to-end pairs-trading opportunity scanner.

Orchestrates the full pipeline from data loading to actionable report:

1. Load OHLCV data for a configurable universe of NSE/BSE symbols.
2. Discover cointegrated pairs (``PairDiscoveryEngine``).
3. Compute live spread z-scores (``SpreadMonitor``).
4. Generate ENTRY / EXIT / STOP signals (``MarketNeutralSignalGenerator``).
5. Size dollar-neutral positions for a daily profit target (``MarketNeutralSizer``).
6. Format a human-readable DataFrame report with risk/reward metrics.

Designed for integration with the live screener runtime, paper-trading
loop, or standalone one-shot scans via ``run_market_neutral_scan()``.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import pandas as pd

from PKDevTools.classes.log import default_logger
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.PKDateUtilities import PKDateUtilities

# ---------------------------------------------------------------------------
# Heavy / optional imports are deferred to method bodies so the module can be
# imported even when statsmodels or scipy are not installed.
# ---------------------------------------------------------------------------

# Universe CSV paths — mirrors ActionableReportRunner.UNIVERSE_FILES
_UNIVERSE_FILES = {
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
    "bse500": os.path.join("results", "Indices", "BSE500.csv"),
}


# ===================================================================
# MarketNeutralScanner
# ===================================================================

class MarketNeutralScanner:
    """End-to-end scanner for market-neutral (pairs trading) opportunities.

    Orchestrates:
    1. Load price data for a universe of symbols
    2. Discover cointegrated pairs (via PairDiscoveryEngine)
    3. Monitor spreads in real-time (via SpreadMonitor)
    4. Generate actionable signals (via MarketNeutralSignalGenerator)
    5. Size positions for a target daily P&L (via MarketNeutralSizer)
    6. Format output as a DataFrame report
    """

    MAX_FETCH_WORKERS = 8       # cap on parallel data-fetch threads
    PROGRESS_UPDATE_INTERVAL = 50  # print progress every N fetches

    def __init__(
        self,
        config_manager,
        universe: str = "nifty200",
        capital: float = 500_000.0,
        daily_target: float = 15_000.0,
        max_pairs: int = 5,
        min_history: int = 60,
        use_live_kite: bool = False,
    ):
        self.config_manager = config_manager
        self.universe = universe.lower().strip()
        self.capital = float(capital)
        self.daily_target = float(daily_target)
        self.max_pairs = int(max_pairs)
        self.min_history = int(min_history)
        self.use_live_kite = bool(use_live_kite)

        self._lock = threading.Lock()
        self._last_scan_time: Optional[float] = None
        self._last_pairs: List = []
        self._last_trades: List = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(
        self,
        show_progress: bool = True,
    ) -> Tuple[Optional[pd.DataFrame], List]:
        """Run the full scan pipeline.

        Returns:
            ``(report_df, sized_trades)`` where *report_df* is a
            human-readable DataFrame and *sized_trades* is the list of
            ``SizedPairTrade`` objects.  Returns ``(None, [])`` on
            failure.
        """
        start_ts = time.monotonic()
        oc = OutputControls()

        if show_progress:
            oc.printOutput(
                "\n╔══════════════════════════════════════════════════════════╗"
            )
            oc.printOutput(
                "║   Market-Neutral Pairs Trading Scanner                 ║"
            )
            oc.printOutput(
                "╚══════════════════════════════════════════════════════════╝"
            )

        # Step 1 — load symbols & price data
        if show_progress:
            oc.printOutput(f"\n  [1/5] Loading price data (universe={self.universe}) …")

        price_data = self._load_price_data(show_progress=show_progress)
        if not price_data:
            if show_progress:
                oc.printOutput("  [!] No price data loaded — aborting scan.")
            return None, []

        if show_progress:
            oc.printOutput(f"        ✓ Loaded {len(price_data)} symbols")

        return self.scan_with_data(price_data, show_progress=show_progress)

    def scan_with_data(
        self,
        price_data: Dict[str, pd.DataFrame],
        show_progress: bool = True,
    ) -> Tuple[Optional[pd.DataFrame], List]:
        """Run the pipeline with pre-loaded price data.

        Useful when the caller (e.g. the live monitor) already holds
        OHLCV frames in memory.

        Returns:
            ``(report_df, sized_trades)``
        """
        from pkscreener.classes.MarketNeutralStrategy import (
            PairDiscoveryEngine,
            SpreadMonitor,
            MarketNeutralSignalGenerator,
            MarketNeutralSizer,
        )

        oc = OutputControls()
        start_ts = time.monotonic()

        if not price_data:
            return None, []

        # Step 2 — discover cointegrated pairs
        if show_progress:
            oc.printOutput("  [2/5] Discovering cointegrated pairs …")

        discovery = PairDiscoveryEngine()
        pairs = discovery.discover_pairs(price_data, min_history=self.min_history)

        if not pairs:
            if show_progress:
                oc.printOutput("  [!] No cointegrated pairs found.")
            return None, []

        if show_progress:
            oc.printOutput(f"        ✓ Found {len(pairs)} cointegrated pairs")

        # Step 3 — compute spread z-scores
        if show_progress:
            oc.printOutput("  [3/5] Computing spread z-scores …")

        monitor = SpreadMonitor()
        spreads = monitor.batch_compute_spreads(pairs, price_data)

        if not spreads:
            if show_progress:
                oc.printOutput("  [!] Could not compute spreads.")
            return None, []

        if show_progress:
            oc.printOutput(f"        ✓ Computed spreads for {len(spreads)} pairs")

        # Step 4 — generate signals
        if show_progress:
            oc.printOutput("  [4/5] Generating trade signals …")

        generator = MarketNeutralSignalGenerator()
        signals = generator.generate_signals(
            spreads,
            capital=self.capital,
            daily_target=self.daily_target,
        )

        entry_signals = [s for s in signals if s.signal_type.startswith("ENTRY")]
        if show_progress:
            oc.printOutput(
                f"        ✓ {len(signals)} total signals "
                f"({len(entry_signals)} entries)"
            )

        # Step 5 — size positions
        if show_progress:
            oc.printOutput("  [5/5] Sizing positions …")

        sizer = MarketNeutralSizer()
        trades = sizer.size_positions(
            signals,
            capital=self.capital,
            daily_target=self.daily_target,
            max_pairs=self.max_pairs,
        )

        if show_progress:
            elapsed = time.monotonic() - start_ts
            oc.printOutput(f"        ✓ Sized {len(trades)} pair trades ({elapsed:.1f}s)")

        # Cache results
        with self._lock:
            self._last_scan_time = time.time()
            self._last_pairs = pairs
            self._last_trades = trades

        if not trades:
            if show_progress:
                oc.printOutput("  [!] No actionable pair trades after sizing.")
            return None, []

        report_df = self.format_report(trades)
        return report_df, trades

    # ------------------------------------------------------------------
    # Report formatting
    # ------------------------------------------------------------------

    def format_report(self, trades: List) -> pd.DataFrame:
        """Format sized trades into a human-readable DataFrame.

        Columns:
            PairName, Sector, Action, LongSymbol, ShortSymbol,
            LongPrice, ShortPrice, LongQty, ShortQty, CapitalUsed,
            ExpectedProfit, MaxLoss, RiskReward, ZScore, Confidence,
            HalfLife, Reason
        """
        rows = []
        for t in trades:
            sig = t.signal
            pair = sig.pair
            rows.append(
                {
                    "PairName": f"{pair.symbol_a}/{pair.symbol_b}",
                    "Sector": pair.sector,
                    "Action": sig.signal_type,
                    "LongSymbol": sig.long_symbol,
                    "ShortSymbol": sig.short_symbol,
                    "LongPrice": sig.long_price,
                    "ShortPrice": sig.short_price,
                    "LongQty": t.long_quantity,
                    "ShortQty": t.short_quantity,
                    "CapitalUsed": t.total_capital,
                    "ExpectedProfit": t.expected_profit,
                    "MaxLoss": t.max_loss,
                    "RiskReward": t.risk_reward_ratio,
                    "ZScore": sig.z_score,
                    "Confidence": sig.confidence,
                    "HalfLife": sig.spread_state.half_life,
                    "Reason": sig.reason,
                }
            )

        df = pd.DataFrame(rows)
        if not df.empty:
            df.sort_values("Confidence", ascending=False, inplace=True)
            df.reset_index(drop=True, inplace=True)
        return df

    def print_report(self, df: pd.DataFrame) -> None:
        """Pretty-print the market-neutral report to console."""
        oc = OutputControls()

        # Header
        regime_str = self._detect_regime_label()
        timestamp = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d %H:%M:%S")

        oc.printOutput("")
        oc.printOutput("=" * 72)
        oc.printOutput("  MARKET-NEUTRAL PAIRS TRADING REPORT")
        oc.printOutput(f"  Generated: {timestamp}")
        oc.printOutput(f"  Universe : {self.universe.upper()}")
        oc.printOutput(
            f"  Capital  : ₹{self.capital:,.0f}  |  "
            f"Daily Target: ₹{self.daily_target:,.0f}"
        )
        if regime_str:
            oc.printOutput(f"  Regime   : {regime_str}")
        oc.printOutput("=" * 72)

        if df is None or df.empty:
            oc.printOutput("  No actionable pair trades found.\n")
            return

        stats = self.summary_stats(self._last_trades)
        oc.printOutput(
            f"  Pairs: {stats.get('pair_count', 0)}  |  "
            f"Capital Used: ₹{stats.get('total_capital_used', 0):,.0f}  |  "
            f"Expected P&L: ₹{stats.get('total_expected_profit', 0):,.0f}  |  "
            f"Avg RR: {stats.get('avg_risk_reward', 0):.2f}"
        )
        oc.printOutput("-" * 72)

        # Print each trade
        for idx, row in df.iterrows():
            oc.printOutput(
                f"\n  #{idx + 1}  {row['PairName']}  ({row['Sector']})  "
                f"[{row['Action']}]"
            )
            oc.printOutput(
                f"      LONG  {row['LongSymbol']:>12s}  "
                f"@ ₹{row['LongPrice']:>10,.2f}  x {row['LongQty']:>5d}"
            )
            oc.printOutput(
                f"      SHORT {row['ShortSymbol']:>12s}  "
                f"@ ₹{row['ShortPrice']:>10,.2f}  x {row['ShortQty']:>5d}"
            )
            oc.printOutput(
                f"      Capital: ₹{row['CapitalUsed']:>10,.0f}  |  "
                f"Exp.Profit: ₹{row['ExpectedProfit']:>8,.0f}  |  "
                f"MaxLoss: ₹{row['MaxLoss']:>8,.0f}  |  "
                f"RR: {row['RiskReward']:.2f}"
            )
            oc.printOutput(
                f"      z={row['ZScore']:+.2f}  "
                f"conf={row['Confidence']:.0f}  "
                f"HL={row['HalfLife']:.0f}d"
            )
            oc.printOutput(f"      {row['Reason']}")

        oc.printOutput("\n" + "=" * 72 + "\n")

    def summary_stats(self, trades: List) -> Dict:
        """Compute aggregate statistics for a list of sized trades.

        Returns:
            dict with keys: pair_count, total_capital_used,
            total_expected_profit, total_max_loss, avg_risk_reward,
            avg_confidence, avg_half_life
        """
        if not trades:
            return {
                "pair_count": 0,
                "total_capital_used": 0.0,
                "total_expected_profit": 0.0,
                "total_max_loss": 0.0,
                "avg_risk_reward": 0.0,
                "avg_confidence": 0.0,
                "avg_half_life": 0.0,
            }

        n = len(trades)
        total_cap = sum(t.total_capital for t in trades)
        total_profit = sum(t.expected_profit for t in trades)
        total_loss = sum(t.max_loss for t in trades)
        avg_rr = sum(t.risk_reward_ratio for t in trades) / n
        avg_conf = sum(t.signal.confidence for t in trades) / n
        avg_hl = sum(t.signal.spread_state.half_life for t in trades) / n

        return {
            "pair_count": n,
            "total_capital_used": round(total_cap, 2),
            "total_expected_profit": round(total_profit, 2),
            "total_max_loss": round(total_loss, 2),
            "avg_risk_reward": round(avg_rr, 4),
            "avg_confidence": round(avg_conf, 2),
            "avg_half_life": round(avg_hl, 2),
        }

    # ------------------------------------------------------------------
    # Data loading (private)
    # ------------------------------------------------------------------

    def _load_price_data(
        self,
        show_progress: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """Load OHLCV data for every symbol in the selected universe.

        Uses the same CSV universe files as ``ActionableReportRunner``.
        Data is fetched via ``screenerStockDataFetcher`` with batched
        ``ThreadPoolExecutor`` parallelism.
        """
        symbols = self._load_universe_symbols()
        if not symbols:
            default_logger().warning(
                f"MarketNeutralScanner: no symbols for universe '{self.universe}'"
            )
            return {}

        oc = OutputControls()
        if show_progress:
            oc.printOutput(f"        Loading {len(symbols)} symbols …")

        try:
            from pkscreener.classes.Fetcher import screenerStockDataFetcher
            from pkscreener.classes.ExchangeManager import (
                EXCHANGE_SUFFIX_NSE,
                EXCHANGE_SUFFIX_BSE,
            )
        except ImportError as exc:
            default_logger().debug(
                f"MarketNeutralScanner: cannot import fetcher: {exc}"
            )
            return {}

        fetcher = screenerStockDataFetcher(self.config_manager)
        suffix = (
            EXCHANGE_SUFFIX_BSE
            if self.universe.startswith("bse")
            else EXCHANGE_SUFFIX_NSE
        )
        period = getattr(self.config_manager, "period", "1y")
        duration = getattr(self.config_manager, "duration", "1d")

        price_data: Dict[str, pd.DataFrame] = {}
        data_lock = threading.Lock()
        max_workers = min(self.MAX_FETCH_WORKERS, max(1, os.cpu_count() or 2))
        total = len(symbols)
        loaded_count = 0
        failed_count = 0

        def _fetch_one(sym: str) -> Tuple[str, Optional[pd.DataFrame]]:
            try:
                df = fetcher.fetchStockData(
                    sym,
                    period=period,
                    duration=duration,
                    exchangeSuffix=suffix,
                )
                if df is not None and not df.empty and len(df) >= self.min_history:
                    return sym, df
            except Exception as exc:
                default_logger().debug(
                    f"MarketNeutralScanner: fetch failed for {sym}: {exc}"
                )
            return sym, None

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_fetch_one, sym): sym for sym in symbols
            }
            for future in as_completed(futures):
                sym, df = future.result()
                if df is not None:
                    with data_lock:
                        price_data[sym] = df
                        loaded_count += 1
                else:
                    failed_count += 1

                done = loaded_count + failed_count
                if show_progress and (done % self.PROGRESS_UPDATE_INTERVAL == 0 or done == total):
                    oc.printOutput(
                        f"        Fetched {done}/{total} "
                        f"({loaded_count} ok, {failed_count} failed) …",
                        end="\r",
                    )

        if show_progress:
            oc.printOutput("")

        default_logger().info(
            f"MarketNeutralScanner: loaded {loaded_count}/{total} symbols "
            f"({failed_count} failed) for universe '{self.universe}'"
        )
        return price_data

    def _load_universe_symbols(self) -> List[str]:
        """Load stock symbols from the universe CSV file."""
        csv_path = _UNIVERSE_FILES.get(self.universe)
        if not csv_path or not os.path.exists(csv_path):
            default_logger().warning(
                f"MarketNeutralScanner: universe file not found for "
                f"'{self.universe}': {csv_path}"
            )
            return []

        try:
            universe_df = pd.read_csv(csv_path)
            symbol_col = None
            for candidate in ("Symbol", "SYMBOL", "symbol"):
                if candidate in universe_df.columns:
                    symbol_col = candidate
                    break
            if symbol_col is None:
                default_logger().warning(
                    f"MarketNeutralScanner: no symbol column in {csv_path}"
                )
                return []

            symbols = [
                str(s).strip().upper()
                for s in universe_df[symbol_col].tolist()
                if str(s).strip()
            ]
            return symbols
        except Exception as exc:
            default_logger().debug(
                f"MarketNeutralScanner: failed loading universe: {exc}",
                exc_info=True,
            )
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_regime_label() -> str:
        """Best-effort market regime label from MarketContext."""
        try:
            from pkscreener.classes.MarketContext import MarketContextEngine

            engine = MarketContextEngine()
            regime = engine.regime
            return f"{regime.regime} (score={regime.regime_score})"
        except Exception:
            return ""


# ===================================================================
# Convenience function
# ===================================================================

def run_market_neutral_scan(
    universe: str = "nifty200",
    capital: float = 500_000.0,
    daily_target: float = 15_000.0,
    max_pairs: int = 5,
) -> Optional[pd.DataFrame]:
    """Run a one-shot market-neutral pairs trading scan.

    Creates a default ``tools`` config manager, runs the scanner, prints
    the report, and returns the report DataFrame.

    Args:
        universe: stock universe key (e.g. "nifty200", "nifty500").
        capital: total trading capital in ₹.
        daily_target: target daily profit in ₹.
        max_pairs: maximum number of pair positions.

    Returns:
        Report DataFrame or ``None`` if no trades found.
    """
    try:
        from pkscreener.classes.ConfigManager import tools as ConfigTools

        config_manager = ConfigTools()
    except Exception as exc:
        default_logger().debug(
            f"run_market_neutral_scan: config init failed: {exc}"
        )
        # Minimal fallback — duck-typed config with period/duration
        config_manager = type(
            "_MinimalConfig", (), {"period": "1y", "duration": "1d"}
        )()

    scanner = MarketNeutralScanner(
        config_manager=config_manager,
        universe=universe,
        capital=capital,
        daily_target=daily_target,
        max_pairs=max_pairs,
    )

    report_df, trades = scanner.scan(show_progress=True)

    if report_df is not None and not report_df.empty:
        scanner.print_report(report_df)

    return report_df

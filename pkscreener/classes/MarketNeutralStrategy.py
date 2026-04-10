"""
Market-Neutral / Pairs Trading Strategy Engine for Indian Equities (NSE/BSE).

Discovers cointegrated stock pairs within the same sector, monitors spread
dynamics, generates dollar-neutral trade signals, and sizes positions to
target a configurable daily profit while capping risk.

Pipeline:
1. PairDiscoveryEngine  — Engle-Granger cointegration scan across sector peers
2. SpreadMonitor        — rolling z-score & half-life of each pair spread
3. MarketNeutralSignalGenerator — ENTRY / EXIT / STOP signals from z-score
4. MarketNeutralSizer   — dollar-neutral, risk-capped position sizing

Used by the live assistant runtime to surface market-neutral opportunities
that are independent of broad market direction.
"""

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from PKDevTools.classes.log import default_logger
from PKDevTools.classes.OutputControls import OutputControls

from pkscreener.classes.MarketContext import (
    get_sector,
    get_sector_peers,
    _SECTOR_MAP,
    _SECTOR_STOCKS,
    _load_sector_map,
)

# ---------------------------------------------------------------------------
# Optional dependency: statsmodels for cointegration testing
# ---------------------------------------------------------------------------
try:
    from statsmodels.tsa.stattools import coint as _sm_coint
    _COINT_AVAILABLE = True
except ImportError:
    _COINT_AVAILABLE = False

try:
    from scipy.stats import pearsonr as _pearsonr
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False


# ===================================================================
# Data classes
# ===================================================================

@dataclass
class PairCandidate:
    """A cointegrated stock pair discovered within a single sector."""
    symbol_a: str
    symbol_b: str
    sector: str
    correlation: float
    coint_pvalue: float
    hedge_ratio: float
    half_life: float        # mean-reversion half-life in trading days
    spread_std: float
    discovered_at: str      # ISO-8601 timestamp


@dataclass
class SpreadState:
    """Live spread snapshot for a pair."""
    pair: PairCandidate
    current_spread: float
    spread_mean: float
    spread_std: float
    z_score: float
    half_life: float
    price_a: float          # current price of symbol_a
    price_b: float          # current price of symbol_b


@dataclass
class PairTradeSignal:
    """Actionable trade signal for a pair."""
    pair: PairCandidate
    spread_state: SpreadState
    signal_type: str        # "ENTRY_LONG_A", "ENTRY_LONG_B", "EXIT", "STOP"
    long_symbol: str
    short_symbol: str
    long_price: float
    short_price: float
    z_score: float
    expected_return_pct: float   # estimated % return when spread reverts
    stop_z: float                # z-score at which to stop out
    target_z: float              # z-score target for profit taking
    confidence: float            # 0-100
    reason: str


@dataclass
class SizedPairTrade:
    """Fully sized pair trade ready for execution."""
    signal: PairTradeSignal
    long_quantity: int
    short_quantity: int
    long_capital: float
    short_capital: float
    total_capital: float
    expected_profit: float
    max_loss: float
    risk_reward_ratio: float


# ===================================================================
# Pair Discovery Engine
# ===================================================================

class PairDiscoveryEngine:
    """Scan sector peers for cointegrated pairs using Engle-Granger test.

    Workflow:
    1. Group symbols by sector (via MarketContext).
    2. Within each sector, pre-filter by Pearson correlation > *min_corr*.
    3. Run Engle-Granger cointegration test on surviving pairs.
    4. Compute hedge ratio & half-life for accepted pairs.
    5. Cache results for *cache_seconds* before re-scanning.
    """

    # --- configuration thresholds ---
    MIN_CORRELATION = 0.7
    MAX_COINT_PVALUE = 0.05
    DEFAULT_CACHE_SECONDS = 86400  # 1 day

    def __init__(
        self,
        min_corr: float = MIN_CORRELATION,
        max_pvalue: float = MAX_COINT_PVALUE,
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
    ):
        self._min_corr = min_corr
        self._max_pvalue = max_pvalue
        self._cache_seconds = cache_seconds
        self._cached_pairs: List[PairCandidate] = []
        self._cache_time: Optional[datetime] = None
        self._lock = threading.Lock()
        _load_sector_map()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover_pairs(
        self,
        price_data: Dict[str, pd.DataFrame],
        min_history: int = 60,
    ) -> List[PairCandidate]:
        """Discover cointegrated pairs from historical closing prices.

        Args:
            price_data: ``{symbol: DataFrame}`` where each DataFrame has a
                        ``'Close'`` column indexed by date.
            min_history: minimum number of trading days required per symbol.

        Returns:
            List of *PairCandidate* objects sorted by cointegration p-value
            (best first).
        """
        if not _COINT_AVAILABLE:
            default_logger().debug(
                "MarketNeutralStrategy: statsmodels not available — "
                "cointegration test skipped"
            )
            return []

        if not _SCIPY_AVAILABLE:
            default_logger().debug(
                "MarketNeutralStrategy: scipy not available — "
                "correlation pre-filter skipped"
            )
            return []

        # Check cache freshness
        if self._is_cache_valid():
            return self.get_cached_pairs()

        # Build a universe of symbols with enough history
        closes = self._extract_close_series(price_data, min_history)
        if len(closes) < 2:
            default_logger().debug(
                "MarketNeutralStrategy: fewer than 2 symbols with "
                f"min_history={min_history} — nothing to scan"
            )
            return []

        # Group by sector
        sector_groups: Dict[str, List[str]] = defaultdict(list)
        for sym in closes:
            sector = get_sector(sym)
            if sector not in ("UNKNOWN", "OTHER"):
                sector_groups[sector].append(sym)

        # Scan each sector
        pairs: List[PairCandidate] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for sector, symbols in sector_groups.items():
            if len(symbols) < 2:
                continue
            sector_pairs = self._scan_sector(symbols, closes, sector, now_iso)
            pairs.extend(sector_pairs)

        # Sort by cointegration p-value (best first)
        pairs.sort(key=lambda p: p.coint_pvalue)

        with self._lock:
            self._cached_pairs = pairs
            self._cache_time = datetime.now(timezone.utc)

        default_logger().info(
            f"MarketNeutralStrategy: discovered {len(pairs)} cointegrated pairs "
            f"across {len(sector_groups)} sectors"
        )
        return pairs

    def get_cached_pairs(self) -> List[PairCandidate]:
        """Return the most recently discovered pairs (thread-safe)."""
        with self._lock:
            return list(self._cached_pairs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_cache_valid(self) -> bool:
        with self._lock:
            if not self._cache_time or not self._cached_pairs:
                return False
            elapsed = (datetime.now(timezone.utc) - self._cache_time).total_seconds()
            return elapsed < self._cache_seconds

    @staticmethod
    def _extract_close_series(
        price_data: Dict[str, pd.DataFrame],
        min_history: int,
    ) -> Dict[str, pd.Series]:
        """Extract clean closing-price series with at least *min_history* bars."""
        closes: Dict[str, pd.Series] = {}
        for sym, df in price_data.items():
            if df is None or df.empty:
                continue
            col = None
            for candidate in ("Close", "close", "Adj Close"):
                if candidate in df.columns:
                    col = candidate
                    break
            if col is None:
                continue
            series = df[col].dropna()
            if len(series) >= min_history:
                closes[sym.strip().upper()] = series
        return closes

    def _scan_sector(
        self,
        symbols: List[str],
        closes: Dict[str, pd.Series],
        sector: str,
        now_iso: str,
    ) -> List[PairCandidate]:
        """Brute-force scan all unique pairs within a sector."""
        pairs: List[PairCandidate] = []
        n = len(symbols)
        for i in range(n):
            for j in range(i + 1, n):
                sym_a, sym_b = symbols[i], symbols[j]
                s_a, s_b = closes.get(sym_a), closes.get(sym_b)
                if s_a is None or s_b is None:
                    continue

                # Align on common dates
                aligned = pd.concat([s_a, s_b], axis=1, join="inner").dropna()
                if len(aligned) < 30:
                    continue
                arr_a = aligned.iloc[:, 0].values.astype(np.float64)
                arr_b = aligned.iloc[:, 1].values.astype(np.float64)

                # 1. Correlation pre-filter
                corr, _ = _pearsonr(arr_a, arr_b)
                if abs(corr) < self._min_corr:
                    continue

                # 2. Engle-Granger cointegration test
                try:
                    coint_stat, pvalue, _ = _sm_coint(arr_a, arr_b)
                except Exception as exc:
                    default_logger().debug(
                        f"MarketNeutralStrategy: coint failed for "
                        f"{sym_a}/{sym_b}: {exc}"
                    )
                    continue

                if pvalue > self._max_pvalue:
                    continue

                # 3. Hedge ratio via OLS: price_a = hedge_ratio * price_b + c
                hedge_ratio = self._ols_hedge_ratio(arr_a, arr_b)

                # 4. Spread & half-life
                spread = arr_a - hedge_ratio * arr_b
                spread_std = float(np.std(spread))
                half_life = self._ou_half_life(spread)

                pairs.append(
                    PairCandidate(
                        symbol_a=sym_a,
                        symbol_b=sym_b,
                        sector=sector,
                        correlation=round(float(corr), 4),
                        coint_pvalue=round(float(pvalue), 6),
                        hedge_ratio=round(float(hedge_ratio), 6),
                        half_life=round(float(half_life), 2),
                        spread_std=round(float(spread_std), 4),
                        discovered_at=now_iso,
                    )
                )
        return pairs

    @staticmethod
    def _ols_hedge_ratio(y: np.ndarray, x: np.ndarray) -> float:
        """Compute OLS hedge ratio: y = beta * x + alpha."""
        x_with_const = np.column_stack([x, np.ones(len(x))])
        # np.linalg.lstsq is fast and handles rank-deficient data
        beta, _, _, _ = np.linalg.lstsq(x_with_const, y, rcond=None)
        return float(beta[0])

    @staticmethod
    def _ou_half_life(spread: np.ndarray) -> float:
        """Estimate mean-reversion half-life using Ornstein-Uhlenbeck fit.

        Regresses Δspread on lagged spread:
            Δs_t = θ * s_{t-1} + ε
        Half-life = -ln(2) / θ
        """
        if len(spread) < 3:
            return 999.0
        lag = spread[:-1]
        delta = np.diff(spread)
        # OLS: delta = theta * lag
        lag_with_const = np.column_stack([lag, np.ones(len(lag))])
        beta, _, _, _ = np.linalg.lstsq(lag_with_const, delta, rcond=None)
        theta = float(beta[0])
        if theta >= 0:
            return 999.0  # not mean-reverting
        half_life = -np.log(2) / theta
        return max(1.0, float(half_life))


# ===================================================================
# Spread Monitor
# ===================================================================

class SpreadMonitor:
    """Compute rolling z-scores and half-lives for discovered pairs.

    All heavy lifting is vectorized with numpy to support batch
    processing of many pairs per tick cycle.
    """

    # --- configuration ---
    LOOKBACK = 20  # rolling window for z-score computation

    def __init__(self, lookback: int = LOOKBACK):
        self._lookback = lookback

    def compute_spread(
        self,
        pair: PairCandidate,
        prices_a: pd.Series,
        prices_b: pd.Series,
    ) -> SpreadState:
        """Compute current spread state for a single pair.

        Args:
            pair: the PairCandidate describing the relationship.
            prices_a: recent closing prices for symbol_a (newest last).
            prices_b: recent closing prices for symbol_b (newest last).

        Returns:
            A SpreadState snapshot.
        """
        aligned = pd.concat([prices_a, prices_b], axis=1, join="inner").dropna()
        if aligned.empty:
            return self._empty_state(pair)

        arr_a = aligned.iloc[:, 0].values.astype(np.float64)
        arr_b = aligned.iloc[:, 1].values.astype(np.float64)

        spread_full = arr_a - pair.hedge_ratio * arr_b

        window = min(self._lookback, len(spread_full))
        recent = spread_full[-window:]

        spread_mean = float(np.mean(recent))
        spread_std = float(np.std(recent)) if window > 1 else pair.spread_std

        current_spread = float(spread_full[-1])
        z_score = (
            (current_spread - spread_mean) / spread_std
            if spread_std > 1e-9
            else 0.0
        )

        half_life = PairDiscoveryEngine._ou_half_life(spread_full)

        return SpreadState(
            pair=pair,
            current_spread=round(current_spread, 4),
            spread_mean=round(spread_mean, 4),
            spread_std=round(spread_std, 4),
            z_score=round(z_score, 4),
            half_life=round(half_life, 2),
            price_a=round(float(arr_a[-1]), 2),
            price_b=round(float(arr_b[-1]), 2),
        )

    def batch_compute_spreads(
        self,
        pairs: List[PairCandidate],
        price_data: Dict[str, pd.DataFrame],
    ) -> List[SpreadState]:
        """Compute spread states for many pairs at once.

        Args:
            pairs: list of PairCandidate to evaluate.
            price_data: ``{symbol: DataFrame}`` with a ``'Close'`` column.

        Returns:
            List of SpreadState, one per pair that had valid data.
        """
        results: List[SpreadState] = []
        close_cache: Dict[str, pd.Series] = {}

        for pair in pairs:
            s_a = self._get_close(pair.symbol_a, price_data, close_cache)
            s_b = self._get_close(pair.symbol_b, price_data, close_cache)
            if s_a is None or s_b is None:
                continue
            state = self.compute_spread(pair, s_a, s_b)
            results.append(state)

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_close(
        symbol: str,
        price_data: Dict[str, pd.DataFrame],
        cache: Dict[str, pd.Series],
    ) -> Optional[pd.Series]:
        """Retrieve and cache Close series for *symbol*."""
        key = symbol.strip().upper()
        if key in cache:
            return cache[key]
        df = price_data.get(key)
        if df is None:
            df = price_data.get(symbol)
        if df is None or df.empty:
            return None
        for col in ("Close", "close", "Adj Close"):
            if col in df.columns:
                series = df[col].dropna()
                cache[key] = series
                return series
        return None

    @staticmethod
    def _empty_state(pair: PairCandidate) -> SpreadState:
        return SpreadState(
            pair=pair,
            current_spread=0.0,
            spread_mean=0.0,
            spread_std=pair.spread_std,
            z_score=0.0,
            half_life=pair.half_life,
            price_a=0.0,
            price_b=0.0,
        )


# ===================================================================
# Signal Generator
# ===================================================================

class MarketNeutralSignalGenerator:
    """Generate ENTRY / EXIT / STOP signals from pair spread z-scores.

    Thresholds (configurable):
        ENTRY  — |z| > 2.0  (spread is stretched, expect mean reversion)
        EXIT   — |z| < 0.5  (spread reverted, take profit)
        STOP   — |z| > 3.5  (divergence too wide, cut loss)
    """

    # --- z-score thresholds ---
    Z_ENTRY = 2.0
    Z_EXIT = 0.5
    Z_STOP = 3.5

    # --- confidence scoring weights ---
    _W_COINT = 30.0    # lower p-value → higher score
    _W_HL = 30.0       # shorter half-life → higher score
    _W_Z = 20.0        # z-score proximity to entry zone
    _W_CORR = 20.0     # higher correlation → higher score

    def __init__(
        self,
        z_entry: float = Z_ENTRY,
        z_exit: float = Z_EXIT,
        z_stop: float = Z_STOP,
    ):
        self._z_entry = z_entry
        self._z_exit = z_exit
        self._z_stop = z_stop

    def generate_signals(
        self,
        spreads: List[SpreadState],
        capital: float = 500000.0,
        daily_target: float = 15000.0,
    ) -> List[PairTradeSignal]:
        """Scan spread states and emit trade signals.

        Args:
            spreads: current SpreadState for each monitored pair.
            capital: total trading capital (₹).
            daily_target: desired daily P&L (₹), used for expected-return
                          filtering.

        Returns:
            List of PairTradeSignal sorted by confidence (highest first).
        """
        signals: List[PairTradeSignal] = []

        for ss in spreads:
            z = ss.z_score
            abs_z = abs(z)

            # --- STOP: spread diverged too far ---
            if abs_z > self._z_stop:
                sig = self._build_signal(
                    ss,
                    signal_type="STOP",
                    reason=f"Spread z-score {z:.2f} exceeds stop threshold "
                           f"±{self._z_stop}",
                )
                signals.append(sig)
                continue

            # --- ENTRY: spread is stretched (mean-reversion opportunity) ---
            if abs_z > self._z_entry:
                direction = "ENTRY_LONG_B" if z > 0 else "ENTRY_LONG_A"
                reason = (
                    f"z={z:.2f} — spread stretched. "
                    f"Expect reversion (half-life ≈{ss.half_life:.0f}d)"
                )
                sig = self._build_signal(ss, signal_type=direction, reason=reason)
                signals.append(sig)
                continue

            # --- EXIT: spread has reverted ---
            if abs_z < self._z_exit:
                sig = self._build_signal(
                    ss,
                    signal_type="EXIT",
                    reason=f"z={z:.2f} — spread reverted to mean zone ±{self._z_exit}",
                )
                signals.append(sig)

        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_signal(
        self,
        ss: SpreadState,
        signal_type: str,
        reason: str,
    ) -> PairTradeSignal:
        """Construct a PairTradeSignal from a SpreadState."""
        pair = ss.pair
        z = ss.z_score

        # Direction: when z > 0 spread is too high → long B, short A
        #            when z < 0 spread is too low  → long A, short B
        if signal_type == "ENTRY_LONG_B" or (signal_type == "STOP" and z > 0):
            long_sym, short_sym = pair.symbol_b, pair.symbol_a
            long_price, short_price = ss.price_b, ss.price_a
        else:
            long_sym, short_sym = pair.symbol_a, pair.symbol_b
            long_price, short_price = ss.price_a, ss.price_b

        # Expected return: distance from current z to exit z, scaled by spread_std
        # as a percentage of the mean spread capital deployed
        mean_capital = (ss.price_a + pair.hedge_ratio * ss.price_b) / 2.0
        if mean_capital > 0 and ss.spread_std > 0:
            z_distance = abs(z) - self._z_exit
            expected_move = z_distance * ss.spread_std
            expected_return_pct = (expected_move / mean_capital) * 100.0
        else:
            expected_return_pct = 0.0

        confidence = self._score_confidence(ss)

        return PairTradeSignal(
            pair=pair,
            spread_state=ss,
            signal_type=signal_type,
            long_symbol=long_sym,
            short_symbol=short_sym,
            long_price=round(long_price, 2),
            short_price=round(short_price, 2),
            z_score=round(z, 4),
            expected_return_pct=round(expected_return_pct, 4),
            stop_z=round(self._z_stop, 2),
            target_z=round(self._z_exit, 2),
            confidence=round(confidence, 2),
            reason=reason,
        )

    def _score_confidence(self, ss: SpreadState) -> float:
        """Score 0-100 based on cointegration quality, half-life, z, and corr."""
        pair = ss.pair

        # Cointegration quality: lower p-value = better (0.00 → 30, 0.05 → 0)
        coint_score = self._W_COINT * max(
            0.0, 1.0 - pair.coint_pvalue / self._max_pvalue_for_scoring
        )

        # Half-life: shorter = better (1d → 30, 60d → 0)
        hl = max(1.0, pair.half_life)
        hl_score = self._W_HL * max(0.0, 1.0 - hl / 60.0)

        # Z-score: closer to entry zone = better for entries
        abs_z = abs(ss.z_score)
        z_score_component = self._W_Z * min(1.0, abs_z / self._z_entry) if abs_z <= self._z_stop else 0.0

        # Correlation: higher = better
        corr_score = self._W_CORR * min(1.0, abs(pair.correlation))

        total = coint_score + hl_score + z_score_component + corr_score
        return max(0.0, min(100.0, total))

    @property
    def _max_pvalue_for_scoring(self) -> float:
        return 0.05


# ===================================================================
# Position Sizer
# ===================================================================

class MarketNeutralSizer:
    """Dollar-neutral, risk-capped position sizing for pair trades.

    Sizing rules:
    1. Dollar-neutral: equal ₹ value on long and short legs.
    2. Risk cap: max 2% of capital at risk per pair trade.
    3. Daily target allocation: distribute capital across top-N pairs
       weighted by inverse half-life (faster reversion → bigger size).
    4. Max pairs: limit concurrent positions to *max_pairs*.
    """

    MAX_RISK_PCT = 0.02          # 2% of capital per trade
    DEFAULT_DAILY_TARGET = 15000.0  # ₹15,000

    def size_positions(
        self,
        signals: List[PairTradeSignal],
        capital: float,
        daily_target: float = DEFAULT_DAILY_TARGET,
        max_pairs: int = 5,
    ) -> List[SizedPairTrade]:
        """Size each signal into a dollar-neutral position.

        Args:
            signals: trade signals (already sorted by confidence).
            capital: total available capital (₹).
            daily_target: target daily P&L (₹).
            max_pairs: maximum concurrent pair positions.

        Returns:
            List of SizedPairTrade ready for execution.
        """
        # Only size entry signals
        entry_signals = [
            s for s in signals
            if s.signal_type.startswith("ENTRY") and s.long_price > 0 and s.short_price > 0
        ]
        if not entry_signals:
            return []

        # Take top-N by confidence
        top = entry_signals[:max_pairs]

        # Allocate capital weighted by inverse half-life
        weights = self._half_life_weights(top)
        per_pair_capital = self._allocate_capital(capital, weights)

        sized: List[SizedPairTrade] = []
        for sig, alloc in zip(top, per_pair_capital):
            trade = self._size_one(sig, alloc, capital)
            if trade is not None:
                sized.append(trade)

        return sized

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _half_life_weights(signals: List[PairTradeSignal]) -> np.ndarray:
        """Inverse half-life weights: shorter half-life → higher allocation."""
        hl = np.array(
            [max(1.0, s.spread_state.half_life) for s in signals],
            dtype=np.float64,
        )
        inv_hl = 1.0 / hl
        total = inv_hl.sum()
        if total <= 0:
            return np.ones(len(signals)) / max(len(signals), 1)
        return inv_hl / total

    @staticmethod
    def _allocate_capital(
        capital: float,
        weights: np.ndarray,
    ) -> np.ndarray:
        """Distribute capital according to weights, respecting 2% risk cap."""
        max_per_pair = capital * MarketNeutralSizer.MAX_RISK_PCT / 0.05
        # max_per_pair = capital that would result in 2% loss if spread moves 5%
        raw = capital * weights
        capped = np.minimum(raw, max_per_pair)
        return capped

    @staticmethod
    def _size_one(
        signal: PairTradeSignal,
        pair_capital: float,
        total_capital: float,
    ) -> Optional[SizedPairTrade]:
        """Size a single pair trade (dollar-neutral)."""
        half_capital = pair_capital / 2.0
        if half_capital <= 0:
            return None

        long_qty = int(half_capital / signal.long_price) if signal.long_price > 0 else 0
        short_qty = int(half_capital / signal.short_price) if signal.short_price > 0 else 0

        if long_qty < 1 or short_qty < 1:
            return None

        long_capital = long_qty * signal.long_price
        short_capital = short_qty * signal.short_price
        total_cap = long_capital + short_capital

        # Expected profit: expected_return_pct applied to the smaller leg
        expected_profit = min(long_capital, short_capital) * (
            signal.expected_return_pct / 100.0
        )

        # Max loss: assume spread moves to stop z (from entry z)
        z_entry = abs(signal.z_score)
        z_stop = signal.stop_z
        spread_std = signal.spread_state.spread_std
        max_spread_move = (z_stop - z_entry) * spread_std if spread_std > 0 else 0.0
        # Approximate loss: spread move * quantity of the beta-weighted leg
        max_loss = abs(max_spread_move) * min(long_qty, short_qty)

        risk_reward = (
            expected_profit / max_loss if max_loss > 0 else 0.0
        )

        return SizedPairTrade(
            signal=signal,
            long_quantity=long_qty,
            short_quantity=short_qty,
            long_capital=round(long_capital, 2),
            short_capital=round(short_capital, 2),
            total_capital=round(total_cap, 2),
            expected_profit=round(expected_profit, 2),
            max_loss=round(max_loss, 2),
            risk_reward_ratio=round(risk_reward, 4),
        )

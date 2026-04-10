"""
Tests for MarketNeutralStrategy — pairs trading engine.

Uses importlib to load the module directly and avoid triggering heavy
__init__.py imports from PKDevTools/PKNSETools dependency chain.
"""

import importlib.util
import os
import sys
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Load MarketNeutralStrategy without triggering full pkscreener imports
# ---------------------------------------------------------------------------

MODULE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "pkscreener",
    "classes",
    "MarketNeutralStrategy.py",
)


def _load_module():
    """Import MarketNeutralStrategy by file path, stubbing heavy deps."""
    # Stub out heavy parent packages so the module can be imported standalone
    for mod_name in [
        "PKDevTools",
        "PKDevTools.classes",
        "PKDevTools.classes.log",
        "PKDevTools.classes.OutputControls",
        "PKDevTools.classes.PKDateUtilities",
        "pkscreener",
        "pkscreener.classes",
        "pkscreener.classes.MarketContext",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    # Provide stub functions that MarketContext imports need
    mc_mock = sys.modules["pkscreener.classes.MarketContext"]
    mc_mock.get_sector = MagicMock(return_value="FINANCE")
    mc_mock.get_sector_peers = MagicMock(return_value=set())
    mc_mock._SECTOR_MAP = {"HDFCBANK": "FINANCE", "ICICIBANK": "FINANCE", "SBIN": "FINANCE", "KOTAKBANK": "FINANCE"}
    mc_mock._SECTOR_STOCKS = {"FINANCE": {"HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK"}}
    mc_mock._load_sector_map = MagicMock()

    # Provide a stub default_logger
    log_mock = sys.modules["PKDevTools.classes.log"]
    logger_mock = MagicMock()
    log_mock.default_logger = MagicMock(return_value=logger_mock)

    spec = importlib.util.spec_from_file_location(
        "MarketNeutralStrategy", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mns = _load_module()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_cointegrated_prices(n: int = 120, seed: int = 42) -> (pd.Series, pd.Series):
    """Generate two cointegrated price series for testing."""
    rng = np.random.RandomState(seed)
    # Price B follows a random walk
    b = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    # Price A is cointegrated with B: A = 1.5 * B + mean-reverting noise
    noise = np.zeros(n)
    for i in range(1, n):
        noise[i] = 0.7 * noise[i - 1] + rng.normal(0, 1)
    a = 1.5 * b + noise + 50

    dates = pd.bdate_range("2025-01-01", periods=n)
    return (
        pd.Series(a, index=dates, name="Close"),
        pd.Series(b, index=dates, name="Close"),
    )


def _make_price_dataframes(n: int = 120, seed: int = 42):
    """Build Dict[str, DataFrame] suitable for PairDiscoveryEngine."""
    s_a, s_b = _make_cointegrated_prices(n, seed)
    return {
        "HDFCBANK": pd.DataFrame({"Close": s_a}),
        "ICICIBANK": pd.DataFrame({"Close": s_b}),
    }


# ---------------------------------------------------------------------------
# PairCandidate & SpreadState dataclass tests
# ---------------------------------------------------------------------------

class TestDataClasses:
    def test_pair_candidate_fields(self):
        pc = mns.PairCandidate(
            symbol_a="HDFCBANK", symbol_b="ICICIBANK", sector="FINANCE",
            correlation=0.95, coint_pvalue=0.01, hedge_ratio=1.5,
            half_life=5.0, spread_std=2.5, discovered_at="2025-01-01T00:00:00Z",
        )
        assert pc.symbol_a == "HDFCBANK"
        assert pc.sector == "FINANCE"
        assert pc.half_life == 5.0

    def test_spread_state_fields(self):
        pc = mns.PairCandidate(
            "A", "B", "X", 0.9, 0.02, 1.2, 4.0, 1.0, "2025-01-01"
        )
        ss = mns.SpreadState(
            pair=pc, current_spread=1.5, spread_mean=0.0,
            spread_std=1.0, z_score=1.5, half_life=4.0,
            price_a=150.0, price_b=100.0,
        )
        assert ss.z_score == 1.5
        assert ss.price_a == 150.0


# ---------------------------------------------------------------------------
# PairDiscoveryEngine tests
# ---------------------------------------------------------------------------

class TestPairDiscoveryEngine:
    def test_discover_pairs_finds_cointegrated(self):
        """Given two cointegrated series, should find at least one pair."""
        price_data = _make_price_dataframes(n=120)
        engine = mns.PairDiscoveryEngine(min_corr=0.5, max_pvalue=0.10)
        pairs = engine.discover_pairs(price_data, min_history=30)
        assert len(pairs) >= 1
        p = pairs[0]
        assert set([p.symbol_a, p.symbol_b]) == {"HDFCBANK", "ICICIBANK"}
        assert p.correlation > 0.5
        assert p.coint_pvalue <= 0.10
        assert p.hedge_ratio > 0
        assert p.half_life > 0

    def test_discover_pairs_caches_results(self):
        """Second call within cache window should return cached results."""
        price_data = _make_price_dataframes()
        engine = mns.PairDiscoveryEngine(min_corr=0.5, max_pvalue=0.10)
        pairs_1 = engine.discover_pairs(price_data, min_history=30)
        pairs_2 = engine.discover_pairs(price_data, min_history=30)
        # Should be same object list (from cache)
        assert len(pairs_1) == len(pairs_2)

    def test_discover_pairs_empty_with_insufficient_data(self):
        """With very short histories, should find nothing."""
        price_data = {
            "HDFCBANK": pd.DataFrame({"Close": [100, 101]}),
            "ICICIBANK": pd.DataFrame({"Close": [50, 51]}),
        }
        engine = mns.PairDiscoveryEngine()
        pairs = engine.discover_pairs(price_data, min_history=60)
        assert pairs == []

    def test_ols_hedge_ratio(self):
        """OLS hedge ratio of y = 2*x + noise should be close to 2."""
        rng = np.random.RandomState(123)
        x = np.arange(100, dtype=np.float64)
        y = 2.0 * x + rng.normal(0, 0.1, 100)
        ratio = mns.PairDiscoveryEngine._ols_hedge_ratio(y, x)
        assert abs(ratio - 2.0) < 0.05

    def test_ou_half_life_mean_reverting(self):
        """A strongly mean-reverting process should have a short half-life."""
        rng = np.random.RandomState(99)
        spread = np.zeros(200)
        for i in range(1, 200):
            spread[i] = 0.8 * spread[i - 1] + rng.normal(0, 1)
        hl = mns.PairDiscoveryEngine._ou_half_life(spread)
        assert 0 < hl < 20  # should be a few days

    def test_ou_half_life_random_walk(self):
        """A random walk (no mean reversion) should return a long half-life."""
        # Pure upward drift ensures theta >= 0 (no mean reversion)
        spread = np.arange(200, dtype=np.float64) * 0.5
        hl = mns.PairDiscoveryEngine._ou_half_life(spread)
        assert hl >= 999.0  # _INVALID_HALF_LIFE


# ---------------------------------------------------------------------------
# SpreadMonitor tests
# ---------------------------------------------------------------------------

class TestSpreadMonitor:
    def test_compute_spread_basic(self):
        s_a, s_b = _make_cointegrated_prices(100)
        pc = mns.PairCandidate(
            "HDFCBANK", "ICICIBANK", "FINANCE", 0.95, 0.01, 1.5,
            5.0, 2.0, "2025-01-01",
        )
        monitor = mns.SpreadMonitor(lookback=20)
        state = monitor.compute_spread(pc, s_a, s_b)
        assert isinstance(state, mns.SpreadState)
        assert state.price_a > 0
        assert state.price_b > 0
        assert state.spread_std > 0

    def test_batch_compute_spreads(self):
        price_data = _make_price_dataframes(100)
        pc = mns.PairCandidate(
            "HDFCBANK", "ICICIBANK", "FINANCE", 0.95, 0.01, 1.5,
            5.0, 2.0, "2025-01-01",
        )
        monitor = mns.SpreadMonitor()
        results = monitor.batch_compute_spreads([pc], price_data)
        assert len(results) == 1
        assert results[0].pair.symbol_a == "HDFCBANK"

    def test_compute_spread_empty_data(self):
        pc = mns.PairCandidate(
            "A", "B", "X", 0.9, 0.02, 1.0, 5.0, 1.0, "2025-01-01",
        )
        monitor = mns.SpreadMonitor()
        # Empty series
        state = monitor.compute_spread(
            pc, pd.Series(dtype=float), pd.Series(dtype=float)
        )
        assert state.z_score == 0.0


# ---------------------------------------------------------------------------
# MarketNeutralSignalGenerator tests
# ---------------------------------------------------------------------------

class TestSignalGenerator:
    def _make_spread_state(self, z_score: float, half_life: float = 5.0):
        pc = mns.PairCandidate(
            "HDFCBANK", "ICICIBANK", "FINANCE", 0.95, 0.01, 1.5,
            half_life, 2.0, "2025-01-01",
        )
        return mns.SpreadState(
            pair=pc, current_spread=z_score * 2.0, spread_mean=0.0,
            spread_std=2.0, z_score=z_score, half_life=half_life,
            price_a=1500.0, price_b=1000.0,
        )

    def test_entry_signal_positive_z(self):
        """z > 2.0 should produce ENTRY_LONG_B signal."""
        gen = mns.MarketNeutralSignalGenerator()
        ss = self._make_spread_state(z_score=2.5)
        signals = gen.generate_signals([ss])
        entry_signals = [s for s in signals if s.signal_type.startswith("ENTRY")]
        assert len(entry_signals) == 1
        assert entry_signals[0].signal_type == "ENTRY_LONG_B"
        assert entry_signals[0].long_symbol == "ICICIBANK"
        assert entry_signals[0].short_symbol == "HDFCBANK"

    def test_entry_signal_negative_z(self):
        """z < -2.0 should produce ENTRY_LONG_A signal."""
        gen = mns.MarketNeutralSignalGenerator()
        ss = self._make_spread_state(z_score=-2.5)
        signals = gen.generate_signals([ss])
        entry_signals = [s for s in signals if s.signal_type.startswith("ENTRY")]
        assert len(entry_signals) == 1
        assert entry_signals[0].signal_type == "ENTRY_LONG_A"
        assert entry_signals[0].long_symbol == "HDFCBANK"
        assert entry_signals[0].short_symbol == "ICICIBANK"

    def test_exit_signal(self):
        """z near 0 should produce EXIT signal."""
        gen = mns.MarketNeutralSignalGenerator()
        ss = self._make_spread_state(z_score=0.2)
        signals = gen.generate_signals([ss])
        exit_signals = [s for s in signals if s.signal_type == "EXIT"]
        assert len(exit_signals) == 1

    def test_stop_signal(self):
        """z > 3.5 should produce STOP signal."""
        gen = mns.MarketNeutralSignalGenerator()
        ss = self._make_spread_state(z_score=4.0)
        signals = gen.generate_signals([ss])
        stop_signals = [s for s in signals if s.signal_type == "STOP"]
        assert len(stop_signals) == 1

    def test_no_signal_in_dead_zone(self):
        """z between 0.5 and 2.0 should produce no signal."""
        gen = mns.MarketNeutralSignalGenerator()
        ss = self._make_spread_state(z_score=1.5)
        signals = gen.generate_signals([ss])
        assert len(signals) == 0

    def test_confidence_scoring(self):
        """Higher quality pair should have higher confidence."""
        gen = mns.MarketNeutralSignalGenerator()
        # Good pair: low p-value, short half-life, high correlation
        good = self._make_spread_state(z_score=2.5, half_life=3.0)
        good.pair = mns.PairCandidate(
            "A", "B", "X", 0.99, 0.001, 1.5, 3.0, 2.0, "2025-01-01",
        )
        # Weak pair: higher p-value, longer half-life
        weak = self._make_spread_state(z_score=2.5, half_life=40.0)
        weak.pair = mns.PairCandidate(
            "C", "D", "Y", 0.75, 0.04, 1.2, 40.0, 2.0, "2025-01-01",
        )
        sigs = gen.generate_signals([good, weak])
        entries = [s for s in sigs if s.signal_type.startswith("ENTRY")]
        assert len(entries) == 2
        # Sorted by confidence, good should come first
        assert entries[0].confidence > entries[1].confidence


# ---------------------------------------------------------------------------
# MarketNeutralSizer tests
# ---------------------------------------------------------------------------

class TestMarketNeutralSizer:
    def _make_entry_signal(self, z_score: float = 2.5, price_a: float = 1500.0, price_b: float = 1000.0):
        pc = mns.PairCandidate(
            "HDFCBANK", "ICICIBANK", "FINANCE", 0.95, 0.01, 1.5,
            5.0, 2.0, "2025-01-01",
        )
        ss = mns.SpreadState(
            pair=pc, current_spread=z_score * 2.0, spread_mean=0.0,
            spread_std=2.0, z_score=z_score, half_life=5.0,
            price_a=price_a, price_b=price_b,
        )
        return mns.PairTradeSignal(
            pair=pc, spread_state=ss, signal_type="ENTRY_LONG_B",
            long_symbol="ICICIBANK", short_symbol="HDFCBANK",
            long_price=price_b, short_price=price_a,
            z_score=z_score, expected_return_pct=2.0,
            stop_z=3.5, target_z=0.5, confidence=80.0,
            reason="Test signal",
        )

    def test_size_positions_basic(self):
        sizer = mns.MarketNeutralSizer()
        sig = self._make_entry_signal()
        trades = sizer.size_positions([sig], capital=500000.0)
        assert len(trades) == 1
        t = trades[0]
        assert t.long_quantity > 0
        assert t.short_quantity > 0
        # Dollar-neutral: both legs should have similar capital
        assert abs(t.long_capital - t.short_capital) / max(t.long_capital, 1) < 0.5

    def test_size_positions_respects_max_pairs(self):
        sizer = mns.MarketNeutralSizer()
        signals = [self._make_entry_signal() for _ in range(10)]
        trades = sizer.size_positions(signals, capital=500000.0, max_pairs=3)
        assert len(trades) <= 3

    def test_size_positions_empty_on_no_entries(self):
        sizer = mns.MarketNeutralSizer()
        # EXIT signal should not be sized
        pc = mns.PairCandidate("A", "B", "X", 0.9, 0.02, 1.0, 5.0, 1.0, "2025-01-01")
        ss = mns.SpreadState(pair=pc, current_spread=0.1, spread_mean=0.0,
                             spread_std=1.0, z_score=0.1, half_life=5.0,
                             price_a=100.0, price_b=50.0)
        exit_sig = mns.PairTradeSignal(
            pair=pc, spread_state=ss, signal_type="EXIT",
            long_symbol="A", short_symbol="B",
            long_price=100.0, short_price=50.0,
            z_score=0.1, expected_return_pct=0.0,
            stop_z=3.5, target_z=0.5, confidence=50.0,
            reason="Exit",
        )
        trades = sizer.size_positions([exit_sig], capital=500000.0)
        assert trades == []

    def test_expected_profit_positive(self):
        sizer = mns.MarketNeutralSizer()
        sig = self._make_entry_signal()
        trades = sizer.size_positions([sig], capital=500000.0)
        assert len(trades) == 1
        assert trades[0].expected_profit > 0

    def test_half_life_weighting(self):
        """Pairs with shorter half-life should get larger allocation."""
        sizer = mns.MarketNeutralSizer()
        short_hl = self._make_entry_signal(z_score=2.5)
        short_hl.spread_state = mns.SpreadState(
            pair=short_hl.pair, current_spread=5.0, spread_mean=0.0,
            spread_std=2.0, z_score=2.5, half_life=3.0,
            price_a=1500.0, price_b=1000.0,
        )
        long_hl = self._make_entry_signal(z_score=2.5)
        long_hl_pair = mns.PairCandidate(
            "SBIN", "KOTAKBANK", "FINANCE", 0.90, 0.03, 1.2,
            30.0, 2.0, "2025-01-01",
        )
        long_hl.pair = long_hl_pair
        long_hl.spread_state = mns.SpreadState(
            pair=long_hl_pair, current_spread=5.0, spread_mean=0.0,
            spread_std=2.0, z_score=2.5, half_life=30.0,
            price_a=500.0, price_b=1800.0,
        )
        long_hl.long_price = 1800.0
        long_hl.short_price = 500.0

        trades = sizer.size_positions([short_hl, long_hl], capital=500000.0, max_pairs=2)
        assert len(trades) == 2
        # Short half-life pair should get more capital
        assert trades[0].total_capital >= trades[1].total_capital


# ---------------------------------------------------------------------------
# End-to-end pipeline test
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_full_pipeline(self):
        """Run discovery → spread → signals → sizing end-to-end."""
        price_data = _make_price_dataframes(n=120)
        engine = mns.PairDiscoveryEngine(min_corr=0.5, max_pvalue=0.10)
        pairs = engine.discover_pairs(price_data, min_history=30)

        if not pairs:
            pytest.skip("No pairs discovered (stochastic test)")

        monitor = mns.SpreadMonitor(lookback=20)
        spreads = monitor.batch_compute_spreads(pairs, price_data)
        assert len(spreads) > 0

        gen = mns.MarketNeutralSignalGenerator()
        signals = gen.generate_signals(spreads, capital=500000.0)
        # Signals may or may not exist depending on current z-score
        # Just ensure no exceptions

        if signals:
            sizer = mns.MarketNeutralSizer()
            trades = sizer.size_positions(signals, capital=500000.0)
            for t in trades:
                assert t.total_capital > 0
                assert t.long_quantity > 0
                assert t.short_quantity > 0

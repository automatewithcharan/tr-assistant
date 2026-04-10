"""Unit tests for screener optimization modules.

Tests cover:
- RankingEngine: vectorized ranking, new scoring factors, setup classification
- UniversePreFilter: eligibility, concurrent filtering, caching
- IncrementalIndicators: warm-up, incremental updates, state consistency
- ExchangeManager: dedup, liquidity preferences
- BatchDataFetcher: concurrent fetching, caching, error handling
- BacktestEvaluator: signal evaluation, report generation

These modules are self-contained (no PKDevTools/PKNSETools dependency).
We use importlib to load them directly, bypassing heavy pkscreener.__init__ imports.
"""

import importlib
import importlib.util
import os
import sys
import threading
import time

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Direct module loading (bypasses pkscreener/classes/__init__.py which
# transitively pulls in PKNSETools, PKBrokers, etc.)
# ---------------------------------------------------------------------------

_CLASSES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pkscreener", "classes",
)


def _load_module(name: str):
    """Load a module from pkscreener/classes/ by filename without triggering __init__.py."""
    path = os.path.join(_CLASSES_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RankingEngine_mod = _load_module("RankingEngine")
RankingEngine = RankingEngine_mod.RankingEngine

UniversePreFilter_mod = _load_module("UniversePreFilter")
UniversePreFilter = UniversePreFilter_mod.UniversePreFilter

IncrementalIndicators_mod = _load_module("IncrementalIndicators")
SymbolIncrementalState = IncrementalIndicators_mod.SymbolIncrementalState
IncrementalStateRegistry = IncrementalIndicators_mod.IncrementalStateRegistry

ExchangeManager_mod = _load_module("ExchangeManager")
ExchangeManager = ExchangeManager_mod.ExchangeManager

BatchDataFetcher_mod = _load_module("BatchDataFetcher")
BatchDataFetcher = BatchDataFetcher_mod.BatchDataFetcher

BacktestEvaluator_mod = _load_module("BacktestEvaluator")
BacktestEvaluator = BacktestEvaluator_mod.BacktestEvaluator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 100, start_close: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame for testing."""
    rng = np.random.RandomState(seed)
    closes = start_close + np.cumsum(rng.randn(n) * 0.5)
    closes = np.maximum(closes, 1.0)  # no negative prices
    highs = closes + rng.uniform(0.2, 1.5, n)
    lows = closes - rng.uniform(0.2, 1.5, n)
    lows = np.maximum(lows, 0.5)
    opens = closes + rng.uniform(-0.5, 0.5, n)
    volumes = rng.randint(50000, 500000, n).astype(float)

    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def _make_report_df(n: int = 20, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic report DataFrame for RankingEngine.rank_dataframe()."""
    rng = np.random.RandomState(seed)
    return pd.DataFrame({
        "Symbol": [f"SYM{i}" for i in range(n)],
        "RSI": rng.uniform(20, 80, n),
        "CCI": rng.uniform(-150, 200, n),
        "MFI": rng.uniform(15, 85, n),
        "VolumeRatio": rng.uniform(0.3, 5.0, n),
        "ChangePct": rng.uniform(-5, 8, n),
        "ConfidencePct": rng.uniform(0, 100, n),
        "PriceNow": rng.uniform(50, 5000, n),
        "EntryGapPct": rng.uniform(0, 3, n),
        "ATRPct": rng.uniform(0.5, 4.0, n),
        "AvgTradeValue20": rng.uniform(1e6, 2e8, n),
    })


# ---------------------------------------------------------------------------
# RankingEngine Tests
# ---------------------------------------------------------------------------

class TestRankingEngine:
    """Tests for the RankingEngine class."""

    def test_rank_single_returns_all_factor_scores(self):
        engine = RankingEngine()
        indicators = {
            "rsi": 55, "cci": 80, "mfi": 60,
            "volume_ratio": 2.0, "change_pct": 3.5,
            "confidence": 70, "close": 500, "entry_gap_pct": 0.2,
            "ema20": 490, "ema50": 480, "sma200": 450,
        }
        scores = engine.rank_single(indicators)

        # All factor scores should be present
        assert "momentum_score" in scores
        assert "breakout_score" in scores
        assert "volume_score" in scores
        assert "trend_score" in scores
        assert "rs_score" in scores
        assert "quality_score" in scores
        assert "volatility_expansion_score" in scores
        assert "setup_quality_score" in scores
        assert "CompositeRank" in scores
        assert "setup_type" in scores
        assert "trigger_level" in scores
        assert "invalidation_level" in scores
        assert "why_qualified" in scores

    def test_rank_single_composite_in_range(self):
        engine = RankingEngine()
        indicators = {"rsi": 55, "cci": 80, "mfi": 60, "volume_ratio": 2.0, "change_pct": 3.5}
        scores = engine.rank_single(indicators)
        assert 0 <= scores["CompositeRank"] <= 100

    def test_rank_single_with_ohlcv(self):
        engine = RankingEngine()
        df = _make_ohlcv(250)
        indicators = {"rsi": 60, "close": float(df["close"].iloc[-1]), "volume_ratio": 1.8}
        scores = engine.rank_single(indicators, ohlcv_df=df)
        assert 0 <= scores["CompositeRank"] <= 100
        assert scores["breakout_score"] >= 0

    def test_rank_dataframe_vectorized(self):
        engine = RankingEngine()
        report = _make_report_df(50)
        ranked = engine.rank_dataframe(report)

        assert "CompositeRank" in ranked.columns
        assert "MomentumScore" in ranked.columns
        assert "BreakoutScore" in ranked.columns
        assert "VolumeScore" in ranked.columns
        assert "TrendScore" in ranked.columns
        assert "RSScore" in ranked.columns
        assert "QualityScore" in ranked.columns
        assert "VolExpansionScore" in ranked.columns
        assert "SetupQualityScore" in ranked.columns

        # All ranks in valid range
        assert ranked["CompositeRank"].min() >= 0
        assert ranked["CompositeRank"].max() <= 100

        # Should be sorted descending by CompositeRank
        ranks = ranked["CompositeRank"].tolist()
        assert ranks == sorted(ranks, reverse=True)

    def test_rank_dataframe_empty(self):
        engine = RankingEngine()
        assert engine.rank_dataframe(None) is None
        assert engine.rank_dataframe(pd.DataFrame()).empty

    def test_rank_dataframe_missing_columns(self):
        """rank_dataframe should handle missing columns gracefully with defaults."""
        engine = RankingEngine()
        df = pd.DataFrame({"RSI": [55, 60], "CCI": [80, -50]})
        ranked = engine.rank_dataframe(df)
        assert "CompositeRank" in ranked.columns
        assert len(ranked) == 2

    def test_custom_weights_normalization(self):
        engine = RankingEngine(weights={"momentum": 50, "breakout": 50})
        assert abs(sum(engine.weights.values()) - 100) < 0.01

    def test_setup_classification(self):
        engine = RankingEngine()

        # Breakout setup
        scores_breakout = {"breakout_score": 75, "volume_score": 60, "momentum_score": 40}
        assert engine._classify_setup(scores_breakout, {}) == "breakout"

        # Momentum setup
        scores_momentum = {"breakout_score": 30, "volume_score": 40, "momentum_score": 75}
        assert engine._classify_setup(scores_momentum, {}) == "momentum"

    def test_volatility_expansion_with_ohlcv(self):
        engine = RankingEngine()
        df = _make_ohlcv(30)
        indicators = {"close": float(df["close"].iloc[-1]), "volume_ratio": 2.5, "change_pct": 4.0}
        score = engine._score_volatility_expansion(indicators, df)
        assert 0 <= score <= 100


# ---------------------------------------------------------------------------
# UniversePreFilter Tests
# ---------------------------------------------------------------------------

class TestUniversePreFilter:
    """Tests for the UniversePreFilter class."""

    def test_eligible_stock(self):
        pf = UniversePreFilter(min_price=10, min_avg_volume=1000, min_avg_trade_value=10000, min_candles=10)
        df = _make_ohlcv(100, start_close=500.0)
        ok, reason = pf.is_eligible(df)
        assert ok is True
        assert reason == "passed"

    def test_reject_low_price(self):
        pf = UniversePreFilter(min_price=100, min_candles=5)
        df = _make_ohlcv(20, start_close=5.0)
        ok, reason = pf.is_eligible(df)
        assert ok is False
        assert "price too low" in reason

    def test_reject_insufficient_history(self):
        pf = UniversePreFilter(min_candles=100)
        df = _make_ohlcv(10)
        ok, reason = pf.is_eligible(df)
        assert ok is False
        assert "insufficient history" in reason

    def test_reject_empty_data(self):
        pf = UniversePreFilter()
        ok, reason = pf.is_eligible(None)
        assert ok is False
        assert reason == "no data"

    def test_filter_symbols_sequential(self):
        pf = UniversePreFilter(min_price=10, min_avg_volume=1000, min_avg_trade_value=10000, min_candles=10)
        symbols = ["GOOD1", "GOOD2", "BAD1"]
        data = {
            "GOOD1": _make_ohlcv(100, start_close=500),
            "GOOD2": _make_ohlcv(100, start_close=300),
            "BAD1": _make_ohlcv(5, start_close=500),
        }
        eligible, rejected = pf.filter_symbols(symbols, lambda s: data[s], max_workers=1, use_cache=False)
        assert "GOOD1" in eligible
        assert "GOOD2" in eligible
        assert len(rejected) == 1
        assert rejected[0][0] == "BAD1"

    def test_filter_symbols_concurrent(self):
        pf = UniversePreFilter(min_price=10, min_avg_volume=1000, min_avg_trade_value=10000, min_candles=10)
        symbols = [f"SYM{i}" for i in range(20)]
        data = {s: _make_ohlcv(100, start_close=500 + i * 10) for i, s in enumerate(symbols)}
        eligible, rejected = pf.filter_symbols(symbols, lambda s: data[s], max_workers=4, use_cache=False)
        assert len(eligible) + len(rejected) == 20

    def test_cache_hit(self):
        pf = UniversePreFilter(min_price=10, min_avg_volume=1000, min_avg_trade_value=10000, min_candles=10)
        pf.set_cache_ttl(60.0)
        data = {"SYM1": _make_ohlcv(100, start_close=500)}

        # First call populates cache
        eligible1, _ = pf.filter_symbols(["SYM1"], lambda s: data[s], max_workers=1, use_cache=True)
        assert "SYM1" in eligible1

        # Second call should use cache (even with a broken loader)
        eligible2, _ = pf.filter_symbols(["SYM1"], lambda s: (_ for _ in ()).throw(RuntimeError("should not be called")), max_workers=1, use_cache=True)
        assert "SYM1" in eligible2

    def test_cache_stats(self):
        pf = UniversePreFilter(min_price=10, min_avg_volume=1000, min_avg_trade_value=10000, min_candles=10)
        pf.set_cache_ttl(60.0)
        data = {"SYM1": _make_ohlcv(100, start_close=500)}
        pf.filter_symbols(["SYM1"], lambda s: data[s], max_workers=1, use_cache=True)
        stats = pf.get_cache_stats()
        assert stats["eligible"] >= 1


# ---------------------------------------------------------------------------
# IncrementalIndicators Tests
# ---------------------------------------------------------------------------

class TestIncrementalIndicators:
    """Tests for the IncrementalIndicators module."""

    def test_warm_up_and_ready(self):
        state = SymbolIncrementalState()
        df = _make_ohlcv(200)
        state.warm_up(df)
        assert state.is_ready is True
        assert state.candles_processed == 200

    def test_warm_up_with_uppercase_columns(self):
        """warm_up should handle both 'close'/'Close' column naming."""
        state = SymbolIncrementalState()
        df = _make_ohlcv(100)
        df.columns = ["Open", "High", "Low", "Close", "Volume"]
        state.warm_up(df)
        assert state.candles_processed == 100

    def test_incremental_update_consistency(self):
        df = _make_ohlcv(200)

        # Warm up with first 190 candles, then update with last 10
        state = SymbolIncrementalState()
        state.warm_up(df.iloc[:190])
        for i in range(190, 200):
            row = df.iloc[i]
            state.update(float(row["close"]), float(row["high"]), float(row["low"]), float(row["volume"]))

        ind = state.get_indicators()
        assert "rsi" in ind
        assert "macd_hist" in ind
        assert "atr_value" in ind
        assert "ema20" in ind
        assert "cci" in ind
        assert "mfi" in ind

    def test_rsi_in_valid_range(self):
        state = SymbolIncrementalState()
        df = _make_ohlcv(200)
        state.warm_up(df)
        ind = state.get_indicators()
        assert 0 <= ind["rsi"] <= 100

    def test_registry(self):
        registry = IncrementalStateRegistry()
        state1 = registry.get_or_create("RELIANCE")
        state2 = registry.get_or_create("reliance")  # case-insensitive
        assert state1 is state2

        registry.reset("RELIANCE")
        state3 = registry.get_or_create("RELIANCE")
        assert state3 is not state1


# ---------------------------------------------------------------------------
# ExchangeManager Tests
# ---------------------------------------------------------------------------

class TestExchangeManager:
    """Tests for the ExchangeManager class."""

    def test_dedup_prefers_nse(self):
        mgr = ExchangeManager(prefer_exchange="NSE")
        mgr.load_nse_symbols(["RELIANCE", "TCS", "INFY"])
        mgr.load_bse_symbols(["RELIANCE", "TCS", "WIPRO"])

        universe = mgr.compute_dedup_universe()
        symbols = {s[0] for s in universe}
        assert "RELIANCE" in symbols
        assert "TCS" in symbols
        assert "INFY" in symbols
        assert "WIPRO" in symbols
        assert len(universe) == 4  # no duplicates

        # Dual-listed should prefer NSE
        reliance = [u for u in universe if u[0] == "RELIANCE"][0]
        assert reliance[1] == "NSE"
        assert reliance[2] == ".NS"

    def test_dual_listed_detection(self):
        mgr = ExchangeManager()
        mgr.load_nse_symbols(["RELIANCE", "TCS"])
        mgr.load_bse_symbols(["RELIANCE", "WIPRO"])
        mgr.compute_dedup_universe()

        assert mgr.is_dual_listed("RELIANCE") is True
        assert mgr.is_dual_listed("TCS") is False
        assert mgr.is_dual_listed("WIPRO") is False

    def test_liquidity_preferences(self):
        mgr = ExchangeManager(prefer_exchange="NSE")
        mgr.load_nse_symbols(["RELIANCE", "SMALLCAP1"])
        mgr.load_bse_symbols(["RELIANCE", "SMALLCAP1"])
        mgr.compute_dedup_universe()

        # SMALLCAP1 has much higher BSE volume
        volume_data = {
            "RELIANCE": {"NSE": 5000000, "BSE": 500000},  # NSE clearly better
            "SMALLCAP1": {"NSE": 100000, "BSE": 300000},   # BSE clearly better (3x)
        }
        overrides = mgr.set_liquidity_preferences(volume_data, min_volume_ratio=1.5)
        assert overrides >= 1

        # SMALLCAP1 should now prefer BSE
        universe = mgr.compute_dedup_universe()
        sc1 = [u for u in universe if u[0] == "SMALLCAP1"][0]
        assert sc1[1] == "BSE"

    def test_get_exchange(self):
        mgr = ExchangeManager(prefer_exchange="NSE")
        mgr.load_nse_symbols(["TCS"])
        mgr.load_bse_symbols(["WIPRO"])
        assert mgr.get_exchange("TCS") == "NSE"
        assert mgr.get_exchange("WIPRO") == "BSE"

    def test_summary(self):
        mgr = ExchangeManager()
        mgr.load_nse_symbols(["A", "B", "C"])
        mgr.load_bse_symbols(["B", "C", "D"])
        mgr.compute_dedup_universe()
        summary = mgr.summary()
        assert "4 unique symbols" in summary
        assert "Dual-listed: 2" in summary


# ---------------------------------------------------------------------------
# BatchDataFetcher Tests
# ---------------------------------------------------------------------------

class TestBatchDataFetcher:
    """Tests for the BatchDataFetcher class."""

    def test_fetch_batch_sequential(self):
        data = {f"SYM{i}": _make_ohlcv(50) for i in range(5)}
        fetcher = BatchDataFetcher(data_loader=lambda s: data[s], max_workers=1, cache_ttl=0)
        results = fetcher.fetch_batch(list(data.keys()))
        assert len(results) == 5

    def test_fetch_batch_concurrent(self):
        data = {f"SYM{i}": _make_ohlcv(50) for i in range(20)}
        fetcher = BatchDataFetcher(data_loader=lambda s: data[s], max_workers=4, cache_ttl=0)
        results = fetcher.fetch_batch(list(data.keys()))
        assert len(results) == 20

    def test_cache_hit(self):
        call_count = {"n": 0}
        data = {"SYM1": _make_ohlcv(50)}

        def counting_loader(s):
            call_count["n"] += 1
            return data[s]

        fetcher = BatchDataFetcher(data_loader=counting_loader, max_workers=1, cache_ttl=60)
        fetcher.fetch_batch(["SYM1"])
        assert call_count["n"] == 1

        # Second fetch should use cache
        fetcher.fetch_batch(["SYM1"])
        assert call_count["n"] == 1  # not called again

    def test_error_isolation(self):

        def flaky_loader(s):
            if s == "BAD":
                raise RuntimeError("fetch failed")
            return _make_ohlcv(50)

        fetcher = BatchDataFetcher(data_loader=flaky_loader, max_workers=2, cache_ttl=0)
        results = fetcher.fetch_batch(["GOOD1", "BAD", "GOOD2"])
        assert "GOOD1" in results
        assert "GOOD2" in results
        assert "BAD" not in results

    def test_progress_callback(self):
        data = {f"SYM{i}": _make_ohlcv(50) for i in range(5)}
        progress_calls = []

        fetcher = BatchDataFetcher(data_loader=lambda s: data[s], max_workers=1, cache_ttl=0)
        fetcher.fetch_batch(list(data.keys()), progress_callback=lambda c, t: progress_calls.append((c, t)))
        assert len(progress_calls) == 5
        assert progress_calls[-1] == (5, 5)

    def test_stats(self):
        data = {f"SYM{i}": _make_ohlcv(50) for i in range(3)}
        fetcher = BatchDataFetcher(data_loader=lambda s: data[s], max_workers=1, cache_ttl=0)
        fetcher.fetch_batch(list(data.keys()))
        stats = fetcher.get_stats()
        assert stats["fetched"] == 3
        assert stats["errors"] == 0

    def test_empty_batch(self):
        fetcher = BatchDataFetcher(data_loader=lambda s: None, max_workers=1)
        results = fetcher.fetch_batch([])
        assert results == {}


# ---------------------------------------------------------------------------
# BacktestEvaluator Tests
# ---------------------------------------------------------------------------

class TestBacktestEvaluator:
    """Tests for the BacktestEvaluator class."""

    def test_evaluate_signal_basic(self):
        evaluator = BacktestEvaluator()

        # Forward data: price goes up
        forward_data = pd.DataFrame({
            "open": [101, 103, 105, 107, 109],
            "high": [102, 104, 106, 108, 110],
            "low": [100, 102, 104, 106, 108],
            "close": [101.5, 103.5, 105.5, 107.5, 109.5],
            "volume": [100000] * 5,
        })

        outcome = evaluator.evaluate_signal(
            symbol="TCS", exchange="NSE", signal_date="2025-01-01",
            setup_type="breakout", composite_rank=75.0,
            entry_price=100.0, stop_level=95.0,
            forward_data=forward_data,
        )

        assert outcome is not None
        assert outcome.symbol == "TCS"
        assert outcome.triggered is True
        assert outcome.return_1d is not None and outcome.return_1d > 0
        assert outcome.return_5d is not None and outcome.return_5d > 0
        assert outcome.score_bucket == "B (60-80)"

    def test_evaluate_signal_losing(self):
        evaluator = BacktestEvaluator()

        # Forward data: price goes down
        forward_data = pd.DataFrame({
            "open": [99, 97, 95, 93, 91],
            "high": [100, 98, 96, 94, 92],
            "low": [98, 96, 94, 92, 90],
            "close": [98.5, 96.5, 94.5, 92.5, 90.5],
            "volume": [100000] * 5,
        })

        outcome = evaluator.evaluate_signal(
            symbol="LOSS", exchange="BSE", signal_date="2025-01-01",
            setup_type="momentum", composite_rank=45.0,
            entry_price=100.0, stop_level=95.0,
            forward_data=forward_data,
        )

        assert outcome is not None
        assert outcome.return_5d is not None and outcome.return_5d < 0

    def test_generate_report(self):
        evaluator = BacktestEvaluator()

        # Add several signals
        for i in range(10):
            price_move = 2.0 if i < 7 else -3.0  # 70% winners
            forward = pd.DataFrame({
                "open": [100 + price_move],
                "high": [100 + abs(price_move) + 1],
                "low": [100 - abs(price_move)],
                "close": [100 + price_move],
                "volume": [100000],
            })
            evaluator.evaluate_signal(
                symbol=f"SYM{i}", exchange="NSE" if i % 2 == 0 else "BSE",
                signal_date="2025-01-01", setup_type="breakout" if i < 5 else "momentum",
                composite_rank=70 + i, entry_price=100.0, stop_level=95.0,
                forward_data=forward,
            )

        report = evaluator.generate_report()
        assert report.total_signals == 10
        assert report.trigger_rate > 0
        assert "NSE" in report.by_exchange
        assert "breakout" in report.by_setup_type

    def test_to_dataframe(self):
        evaluator = BacktestEvaluator()
        forward = pd.DataFrame({
            "open": [101], "high": [102], "low": [99], "close": [101.5], "volume": [100000],
        })
        evaluator.evaluate_signal(
            symbol="TEST", exchange="NSE", signal_date="2025-01-01",
            setup_type="test", composite_rank=60.0, entry_price=100.0,
            stop_level=95.0, forward_data=forward,
        )
        df = evaluator.to_dataframe()
        assert len(df) == 1
        assert "Symbol" in df.columns
        assert "CompositeRank" in df.columns

    def test_summary_text(self):
        evaluator = BacktestEvaluator()
        forward = pd.DataFrame({
            "open": [101], "high": [102], "low": [99], "close": [101.5], "volume": [100000],
        })
        evaluator.evaluate_signal(
            symbol="TEST", exchange="NSE", signal_date="2025-01-01",
            setup_type="test", composite_rank=60.0, entry_price=100.0,
            stop_level=95.0, forward_data=forward,
        )
        summary = evaluator.summary_text()
        assert "Backtest Evaluation Summary" in summary
        assert "Total signals: 1" in summary

    def test_empty_evaluator(self):
        evaluator = BacktestEvaluator()
        report = evaluator.generate_report()
        assert report.total_signals == 0
        df = evaluator.to_dataframe()
        assert df.empty

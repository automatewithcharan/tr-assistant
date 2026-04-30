"""Validation suite for the focused strategy changes (A), (B), (C).

Covers six required scenarios without running real orders or hitting Kite:

1. Bad market regime (crash) blocks early-watch auto-buy.
2. Neutral / supportive regime allows the existing logic to run.
3. Strong mover with no fresh base / no rest stays MISSED.
4. Strong mover with pullback + VWAP reclaim + renewed buyer pressure
   becomes a continuation candidate.
5. Intraday position that loses VWAP soon after entry triggers the
   FAILED_BREAKOUT fast exit.
6. Missing market context / VWAP / depth data does not crash any path.

Run from the repo root:

    python3 -m unittest scripts.validate_strategy_changes -v
or
    python3 scripts/validate_strategy_changes.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from typing import Dict, List
from unittest import mock

# Make the repo root importable when run as `python3 scripts/...`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _install_dependency_stubs() -> None:
    """Pre-populate ``sys.modules`` with minimal stubs so the modules under
    test can be imported in environments without the full dependency tree
    (PKDevTools, the ``pkscreener.classes`` heavy ``__init__`` chain, etc.).

    These stubs only satisfy import-time wiring; every test below either
    bypasses the stubbed code or rebinds the relevant attribute on the real
    instance. Running the validator on a fully-installed environment is also
    safe — installed packages take precedence after the stubs have served
    their purpose because we register stubs only if the real module is
    missing.
    """
    import types

    def _stub(name: str, attrs: dict | None = None):
        if name in sys.modules:
            return sys.modules[name]
        mod = types.ModuleType(name)
        for k, v in (attrs or {}).items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    # PKDevTools chain.
    try:
        import PKDevTools  # noqa: F401
    except ImportError:
        _stub("PKDevTools")
        _stub("PKDevTools.classes")
        archiver = _stub("PKDevTools.classes.Archiver")
        archiver.get_user_outputs_dir = lambda: "."
        archiver.get_user_data_dir = lambda: "."

        class _OutputControls:
            def __init__(self, *_a, **_k):
                pass

            def printOutput(self, *_a, **_k):
                pass
        _stub("PKDevTools.classes.OutputControls", {"OutputControls": _OutputControls})

        class _PKDateUtilities:
            @staticmethod
            def currentDateTime():
                return datetime.now()
        _stub(
            "PKDevTools.classes.PKDateUtilities",
            {"PKDateUtilities": _PKDateUtilities},
        )

        class _Logger:
            def info(self, *_a, **_k):
                pass

            def debug(self, *_a, **_k):
                pass

            def warning(self, *_a, **_k):
                pass

            def error(self, *_a, **_k):
                pass
        _stub("PKDevTools.classes.log", {"default_logger": lambda: _Logger()})

        class _ColorText:
            @staticmethod
            def reset():
                return ""
        _stub("PKDevTools.classes.ColorText", {"colorText": _ColorText})

    # pkscreener heavy package init — replace with empty namespace package
    # so absolute imports like ``from pkscreener.classes.X import Y`` work
    # without dragging in the full menu/CLI/GCS chain.
    if "pkscreener" not in sys.modules:
        _stub("pkscreener")
    if "pkscreener.classes" not in sys.modules:
        pkg = types.ModuleType("pkscreener.classes")
        pkg.__path__ = [os.path.join(_REPO_ROOT, "pkscreener", "classes")]
        sys.modules["pkscreener.classes"] = pkg

    # Sibling modules imported at top of AutoPilotInvestor/KiteOrderManager
    # that would otherwise pull more weight than these tests need.
    class _PaperTradeTracker:
        def __init__(self, *_a, **_k):
            pass

        def log_entry(self, *_a, **_k):
            pass

        def log_fade(self, *_a, **_k):
            pass
    _stub(
        "pkscreener.classes.PaperTradeTracker",
        {"PaperTradeTracker": _PaperTradeTracker},
    )

    class _PreOpenAnalyzer:
        def __init__(self, *_a, **_k):
            pass

        @staticmethod
        def is_preopen_window():
            return False

        @staticmethod
        def _current_phase():
            return ""

        def get_top_picks(self, *_a, **_k):
            return []

        def format_alert(self, *_a, **_k):
            return ""
    _stub(
        "pkscreener.classes.PreOpenAnalyzer",
        {"PreOpenAnalyzer": _PreOpenAnalyzer},
    )

    class _LiveTradingGuardrails:
        def __init__(self, *_a, **_k):
            pass
    _stub(
        "pkscreener.classes.RiskGuardrails",
        {"LiveTradingGuardrails": _LiveTradingGuardrails},
    )


_install_dependency_stubs()


class _StubRegime:
    def __init__(self, regime: str):
        self.regime = regime


class _StubMarketContext:
    """Quacks like ``MarketContextEngine`` for the gating tests."""

    def __init__(self, regime_label: str):
        self._regime = _StubRegime(regime_label)
        self.update_calls = 0

    def update(self, ticks):
        self.update_calls += 1
        return self._regime

    @property
    def regime(self):
        return self._regime


def _make_investor():
    """Build an ``AutoPilotInvestor`` instance without running ``__init__``.

    The full constructor wires Kite, GCS, Discord, etc. We do not need any
    of that — we are only testing pure decision logic. Re-bind only the
    attributes used by the methods under test.
    """
    from pkscreener.classes.AutoPilotInvestor import AutoPilotInvestor

    inv = AutoPilotInvestor.__new__(AutoPilotInvestor)
    # Constants pulled from the real defaults (kept in sync via tests).
    inv._EARLY_RUNNER_AUTO_MAX_CHANGE = 5.5
    inv._EARLY_RUNNER_AUTO_MIN_CHANGE = 1.2
    inv._EARLY_RUNNER_AUTO_MIN_TURNOVER_CR = 0.75
    inv._EARLY_RUNNER_AUTO_MIN_IMBALANCE = 1.4
    inv._EARLY_RUNNER_AUTO_MIN_RR = 1.5
    inv._EARLY_RUNNER_AUTO_MAX_PER_DAY = 4
    inv._EARLY_RUNNER_AUTO_MAX_PULLBACK_PCT = 0.3
    inv._EARLY_RUNNER_AUTO_MAX_TICK_STALE_DROP_PCT = 0.7
    inv._EARLY_RUNNER_AUTO_BUY_ENABLED = True
    inv._EARLY_WATCH_MAX_SIZE = 40
    inv._EARLY_WATCH_WEAK_IMBALANCE = 1.0
    inv._EARLY_WATCH_PEAK_PULLBACK_PCT = 0.6
    inv._SECOND_LEG_MAX_CHANGE = 8.0
    inv._SECOND_LEG_MIN_PULLBACK_PCT = 1.0
    inv._SECOND_LEG_MIN_IMBALANCE = 2.5
    inv._SECOND_LEG_MIN_TURNOVER_CR = 1.5
    inv._SECOND_LEG_MIN_REST_SEC = 300
    inv._SECOND_LEG_MAX_VWAP_GAP_PCT = 0.5
    inv._MARKET_REGIME_REFRESH_SEC = 30
    inv._MARKET_REGIME_BUY_OK = {"strong_bull", "bull", "neutral"}
    inv._market_context = None
    inv._market_context_init_failed = False
    inv._market_regime_last_refresh = 0.0
    inv._nifty_crash_pause = False
    inv._INTRADAY_AUTO_BUY_CUTOFF_MIN = 11 * 60 + 30
    inv._BUY_ALERT_CUTOFF_MIN = 15 * 60 + 15
    inv._AUTO_BUY_TOP_N = 0
    inv._tick_confirmed_auto_buy_count = 0
    inv._daily_no_buy_reasons = __import__("collections").Counter()
    inv._daily_internal_watch = set()
    inv._daily_internal_avoid = set()
    inv._early_watch_list = {}
    inv._early_watch_date = datetime.now().strftime("%Y-%m-%d")
    inv._early_runner_auto_attempted = set()
    inv._early_runner_auto_bought = set()
    inv._early_runner_auto_date = datetime.now().strftime("%Y-%m-%d")
    inv._early_runner_seen = {}

    # Side-effect-free stubs.
    inv._log = lambda *_a, **_k: None
    inv._record_no_buy_reason = lambda reason: inv._daily_no_buy_reasons.update([reason])
    inv._record_internal_stage = (
        lambda stage, sym, reason="":
        inv._daily_internal_avoid.add(sym)
        if str(stage).upper() in {"MISSED", "AVOID"}
        else inv._daily_internal_watch.add(sym)
    )
    inv._past_buy_cutoff = lambda: False
    inv._ist_now_minute = lambda: 10 * 60 + 0  # 10:00 IST — well before cutoff
    inv._load_runner_radar_ticks = lambda: {}

    # Fake order manager — never touched by these tests but the methods
    # check `is None`.
    inv._order_manager = mock.MagicMock()
    inv._order_manager._latest_tick_price.return_value = 100.0
    return inv


class MarketRegimeGateTests(unittest.TestCase):
    def test_crash_regime_blocks_auto_buy(self):
        inv = _make_investor()
        inv._market_context = _StubMarketContext("crash")
        ok, reason = inv._is_safe_to_buy()
        self.assertFalse(ok)
        self.assertIn("crash", reason)

    def test_bear_regime_blocks_auto_buy(self):
        inv = _make_investor()
        inv._market_context = _StubMarketContext("bear")
        ok, reason = inv._is_safe_to_buy()
        self.assertFalse(ok)

    def test_neutral_regime_allows_auto_buy(self):
        inv = _make_investor()
        inv._market_context = _StubMarketContext("neutral")
        ok, reason = inv._is_safe_to_buy()
        self.assertTrue(ok)

    def test_bull_regime_allows_auto_buy(self):
        inv = _make_investor()
        inv._market_context = _StubMarketContext("bull")
        ok, _ = inv._is_safe_to_buy()
        self.assertTrue(ok)

    def test_missing_engine_fails_safe_open(self):
        inv = _make_investor()
        inv._market_context = None
        ok, reason = inv._is_safe_to_buy()
        # Fail-safe: do NOT block when the engine is unavailable; downstream
        # checks (cash, dup, SL/target) still run.
        self.assertTrue(ok)
        self.assertIn("unavailable", reason)

    def test_engine_init_failure_fails_safe(self):
        inv = _make_investor()
        inv._market_context = None
        # Force the import to fail.
        with mock.patch.dict(sys.modules, {"pkscreener.classes.MarketContext": None}):
            inv._refresh_market_context(ticks={"FAKE": {"last_price": 100, "close": 100}})
        self.assertTrue(inv._market_context_init_failed)
        ok, _ = inv._is_safe_to_buy()
        self.assertTrue(ok, "engine init failure must not crash or lock the bot")

    def test_nifty_crash_pause_blocks_even_with_bull_regime(self):
        inv = _make_investor()
        inv._market_context = _StubMarketContext("bull")
        inv._nifty_crash_pause = True
        ok, reason = inv._is_safe_to_buy()
        self.assertFalse(ok)
        self.assertIn("crash pause", reason)


class SecondLegContinuationTests(unittest.TestCase):
    def setUp(self):
        self.inv = _make_investor()

    def _qualifies(self, **overrides):
        defaults = dict(
            change=6.5,
            peak_change=7.5,
            peak_change_ts=time.time() - 600,  # rested 10 min
            now_ts=time.time(),
            price=101.0,
            avg_px=100.6,
            above_vwap=True,
            imbalance=3.0,
            turnover_cr=2.5,
            near_high=True,
        )
        defaults.update(overrides)
        return self.inv._qualifies_as_second_leg_continuation(**defaults)

    def test_strong_mover_without_rest_stays_missed(self):
        # Peak just hit 1 second ago → no rest → not a continuation.
        self.assertFalse(self._qualifies(peak_change_ts=time.time() - 1))

    def test_strong_mover_without_pullback_stays_missed(self):
        self.assertFalse(self._qualifies(change=7.4, peak_change=7.5))

    def test_strong_mover_below_vwap_stays_missed(self):
        self.assertFalse(self._qualifies(above_vwap=False))

    def test_strong_mover_with_weak_demand_stays_missed(self):
        self.assertFalse(self._qualifies(imbalance=1.5))

    def test_strong_mover_too_far_above_vwap_stays_missed(self):
        # Stretched far above VWAP — blow-off top guard.
        self.assertFalse(self._qualifies(price=110.0, avg_px=100.0))

    def test_strong_mover_above_hard_ceiling_stays_missed(self):
        self.assertFalse(self._qualifies(change=9.5, peak_change=9.7))

    def test_clean_continuation_qualifies(self):
        # Rested 10 min, pulled back 1%+, reclaimed VWAP, demand back, near high.
        self.assertTrue(self._qualifies())

    def test_missing_vwap_does_not_crash(self):
        # avg_px=0 → vwap_gap check skipped, but above_vwap=False below.
        self.assertFalse(self._qualifies(avg_px=0.0, above_vwap=False))


class FailedBreakoutFastExitTests(unittest.TestCase):
    """Drives ``KiteOrderManager.check_sell_signals`` directly with mocked ticks."""

    def _make_manager(self):
        from pkscreener.classes.KiteOrderManager import (
            KiteOrderManager,
            TrackedPosition,
        )
        # Bypass the real constructor (Kite client, env, files).
        mgr = KiteOrderManager.__new__(KiteOrderManager)
        mgr._positions: Dict = {}
        mgr._lock = __import__("threading").Lock()
        mgr.confirm_stop_loss_with_broker = False
        mgr.trailing_stop_pct = 3.0
        mgr.trailing_trail_pct = 1.0
        mgr.swing_trail_activate_pct = 5.0
        mgr.swing_trail_dist_pct = 4.0
        mgr.max_hold_days = 10
        mgr.product_type = "MIS"
        mgr.failed_breakout_min_age_sec = 180.0
        mgr.failed_breakout_max_age_sec = 1800.0
        mgr.failed_breakout_min_drop_pct = 0.4
        mgr.failed_breakout_sell_imbalance = 1.3
        mgr._save_positions = lambda: None
        mgr._load_ticks = lambda: {}
        mgr._broker_last_price = lambda sym: 0.0
        return mgr, TrackedPosition

    def _open_intraday_position(self, mgr, TrackedPosition, age_sec: float, *, t1_booked=False):
        from pkscreener.classes.KiteOrderManager import _ist_now
        pos = TrackedPosition(
            symbol="ACME",
            qty=10,
            buy_price=100.0,
            stop_loss=97.0,
            target1=104.0,
            target2=107.0,
            product_type="MIS",
            holding_horizon="intraday",
        )
        # Override buy_time to control age. Must be timezone-aware to match
        # ``_ist_now()`` used inside ``check_sell_signals``.
        pos.buy_time = (_ist_now() - timedelta(seconds=age_sec)).isoformat(timespec="seconds")
        pos.t1_booked = t1_booked
        pos.highest_price = 101.5
        mgr._positions[pos.symbol] = pos
        return pos

    def _force_market_open(self):
        return mock.patch(
            "pkscreener.classes.KiteOrderManager._is_market_open", return_value=True
        )

    def test_lost_vwap_with_sell_pressure_triggers_failed_breakout(self):
        mgr, TrackedPosition = self._make_manager()
        self._open_intraday_position(mgr, TrackedPosition, age_sec=600)
        ticks = {
            "ACME": {
                "last_price": 99.0,                 # under entry
                "average_traded_price": 99.6,       # LTP < VWAP
                "total_buy_quantity": 10_000,
                "total_sell_quantity": 18_000,      # 1.8x sell pressure
                "ohlc": {"high": 101.5, "low": 98.5},
            }
        }
        with self._force_market_open():
            signals = mgr.check_sell_signals(ticks)
        self.assertEqual(len(signals), 1)
        sym, reason, _price, qty = signals[0]
        from pkscreener.classes.KiteOrderManager import SellReason
        self.assertEqual(sym, "ACME")
        self.assertEqual(reason, SellReason.FAILED_BREAKOUT)
        self.assertEqual(qty, 0)  # full exit

    def test_one_noisy_tick_without_sell_pressure_does_not_exit(self):
        mgr, TrackedPosition = self._make_manager()
        self._open_intraday_position(mgr, TrackedPosition, age_sec=600)
        ticks = {
            "ACME": {
                "last_price": 99.0,                 # under entry
                "average_traded_price": 99.6,       # LTP < VWAP
                "total_buy_quantity": 18_000,       # buy-side dominates → not failed
                "total_sell_quantity": 10_000,
                "ohlc": {"high": 101.5, "low": 98.5},
            }
        }
        with self._force_market_open():
            signals = mgr.check_sell_signals(ticks)
        # Stop-loss is 97 and price is 99, so no sell signal at all is fine.
        self.assertEqual(signals, [])

    def test_too_young_position_does_not_trigger_failed_breakout(self):
        mgr, TrackedPosition = self._make_manager()
        self._open_intraday_position(mgr, TrackedPosition, age_sec=60)  # < min_age
        ticks = {
            "ACME": {
                "last_price": 99.0,
                "average_traded_price": 99.6,
                "total_buy_quantity": 10_000,
                "total_sell_quantity": 18_000,
                "ohlc": {"high": 101.5, "low": 98.5},
            }
        }
        with self._force_market_open():
            signals = mgr.check_sell_signals(ticks)
        self.assertEqual(signals, [])

    def test_t1_already_booked_skips_failed_breakout(self):
        mgr, TrackedPosition = self._make_manager()
        self._open_intraday_position(mgr, TrackedPosition, age_sec=600, t1_booked=True)
        ticks = {
            "ACME": {
                "last_price": 99.0,
                "average_traded_price": 99.6,
                "total_buy_quantity": 10_000,
                "total_sell_quantity": 18_000,
                "ohlc": {"high": 101.5, "low": 98.5},
            }
        }
        with self._force_market_open():
            signals = mgr.check_sell_signals(ticks)
        # Trade already in profit-protection lane — defer to trailing/SL.
        for _sym, reason, *_rest in signals:
            self.assertNotIn("failed_breakout", reason)

    def test_missing_vwap_does_not_crash_or_falsely_trigger(self):
        mgr, TrackedPosition = self._make_manager()
        self._open_intraday_position(mgr, TrackedPosition, age_sec=600)
        ticks = {
            "ACME": {
                "last_price": 99.0,
                # average_traded_price MISSING
                "total_buy_quantity": 10_000,
                "total_sell_quantity": 18_000,
                # ohlc MISSING
            }
        }
        with self._force_market_open():
            signals = mgr.check_sell_signals(ticks)  # must not raise
        for _sym, reason, *_rest in signals:
            self.assertNotIn("failed_breakout", reason)


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(MarketRegimeGateTests),
        loader.loadTestsFromTestCase(SecondLegContinuationTests),
        loader.loadTestsFromTestCase(FailedBreakoutFastExitTests),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()

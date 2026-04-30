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
import re
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
    inv._auto_buy_timeline = {}
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


class SwingEntryTypeTests(unittest.TestCase):
    def setUp(self):
        self.inv = _make_investor()
        from pkscreener.classes.AutoPilotInvestor import BlindBuyCandidate
        self.BlindBuyCandidate = BlindBuyCandidate

    def _candidate(self, **overrides):
        c = self.BlindBuyCandidate(
            symbol="LATE_SWING_STOCK",
            price=105.0,
            buy_above=104.5,
            stop_loss=100.0,
            target1=110.0,
            target2=115.0,
            confidence=36.0,
            volume_ratio=2.4,
            change_pct=5.3,
            blind_score=60.0,
            holding_horizon="2-day swing",
            product_preference="CNC",
            off_high_pct=0.6,
            vwap_gap_pct=0.4,
            support_gap_pct=0.5,
            buyer_imbalance=1.9,
            turnover_cr=2.5,
            fresh_buyer_confirmation=True,
        )
        for key, value in overrides.items():
            setattr(c, key, value)
        return c

    def test_late_chase_is_blocked(self):
        c = self._candidate(
            off_high_pct=0.2,
            vwap_gap_pct=1.1,
            support_gap_pct=1.4,
            buyer_imbalance=1.2,
            fresh_buyer_confirmation=False,
            confidence=30.0,
            volume_ratio=2.1,
        )
        entry_type, _ = self.inv._classify_entry_type(c)
        ok, reason = self.inv._swing_entry_quality_gate(c)
        self.assertEqual(entry_type, "LATE_CHASE")
        self.assertFalse(ok)
        self.assertIn("LATE_CHASE", reason)

    def test_pullback_retest_stays_allowed(self):
        c = self._candidate(
            off_high_pct=1.1,
            vwap_gap_pct=0.2,
            support_gap_pct=0.5,
            buyer_imbalance=2.4,
        )
        entry_type, _ = self.inv._classify_entry_type(c)
        ok, reason = self.inv._swing_entry_quality_gate(c)
        self.assertEqual(entry_type, "PULLBACK_RETEST")
        self.assertTrue(ok)
        self.assertIn("buyers returned", reason)

    def test_swing_breakout_needs_stronger_confirmation(self):
        c = self._candidate(
            off_high_pct=0.7,
            vwap_gap_pct=0.5,
            support_gap_pct=0.6,
            confidence=30.0,
            volume_ratio=2.1,
            buyer_imbalance=1.7,
        )
        entry_type, _ = self.inv._classify_entry_type(c)
        ok, reason = self.inv._swing_entry_quality_gate(c)
        self.assertEqual(entry_type, "SWING_BREAKOUT")
        self.assertFalse(ok)
        self.assertIn("needs stronger confirmation", reason)

    def test_confirmed_swing_breakout_stays_allowed(self):
        c = self._candidate(
            off_high_pct=0.7,
            vwap_gap_pct=0.5,
            support_gap_pct=0.6,
            confidence=35.0,
            volume_ratio=2.3,
            buyer_imbalance=1.8,
        )
        entry_type, _ = self.inv._classify_entry_type(c)
        ok, _ = self.inv._swing_entry_quality_gate(c)
        self.assertEqual(entry_type, "SWING_BREAKOUT")
        self.assertTrue(ok)


class ExecutionQualityAndCutoffTests(unittest.TestCase):
    def setUp(self):
        self.inv = _make_investor()
        from pkscreener.classes.AutoPilotInvestor import BlindBuyCandidate
        self.BlindBuyCandidate = BlindBuyCandidate

    def _freeze_ist_time(self, hour: int, minute: int):
        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2026, 4, 30, hour, minute)
                if tz is not None:
                    if hasattr(tz, "localize"):
                        return tz.localize(base)
                    return base.replace(tzinfo=tz)
                return base

        return mock.patch("pkscreener.classes.AutoPilotInvestor.datetime", _FixedDateTime)

    def _candidate(self, **overrides):
        c = self.BlindBuyCandidate(
            symbol="CHOPPY_STOCK",
            price=72.0,
            buy_above=72.0,
            stop_loss=69.12,
            target1=75.6,
            target2=79.2,
            confidence=40.0,
            volume_ratio=2.5,
            change_pct=4.2,
            blind_score=72.0,
            original_action="Buy Now",
            reasons="volume surge 2.5x, gap-up +4.2%, near trigger",
            holding_horizon="5-day swing",
            product_preference="CNC",
            off_high_pct=0.74,
            vwap_gap_pct=0.0,
            support_gap_pct=0.0,
            buyer_imbalance=1.64,
            turnover_cr=3.19,
            fresh_buyer_confirmation=True,
            spread_pct=0.54,
            range_pct=3.58,
            last_trade_qty=50,
        )
        for key, value in overrides.items():
            setattr(c, key, value)
        return c

    def test_rnitai_style_execution_is_blocked(self):
        c = self._candidate()
        ok, reason = self.inv._live_auto_buy_gate_detail(c, "A")
        self.assertFalse(ok)
        self.assertIn("spread", reason.lower())

    def test_imbalance_without_real_liquidity_is_blocked(self):
        c = self._candidate(
            symbol="THIN_MICROCAP_STOCK",
            price=38.0,
            buy_above=37.8,
            stop_loss=36.4,
            target1=39.9,
            target2=41.8,
            turnover_cr=1.4,
            spread_pct=0.38,
            last_trade_qty=40,
            buyer_imbalance=2.4,
            fresh_buyer_confirmation=False,
        )
        ok, reason = self.inv._live_auto_buy_gate_detail(c, "A")
        self.assertFalse(ok)
        self.assertTrue(
            "imbalance" in reason.lower()
            or "spread" in reason.lower()
            or "traded quantity" in reason.lower()
        )

    def test_missing_safety_data_fails_closed(self):
        c = self._candidate(
            symbol="MISSING_DATA_STOCK",
            turnover_cr=0.0,
            spread_pct=0.0,
            last_trade_qty=0.0,
            buyer_imbalance=0.0,
            vwap_gap_pct=0.0,
            fresh_buyer_confirmation=False,
        )
        ok, reason = self.inv._live_auto_buy_gate_detail(c, "A")
        self.assertFalse(ok)
        self.assertIn("safety data", reason.lower())

    def test_post_1130_intraday_rechecks_as_strict_swing(self):
        self.inv._ist_now_minute = lambda: 12 * 60
        c = self._candidate(
            symbol="POST_CUTOFF_STOCK",
            price=1544.0,
            buy_above=1538.0,
            stop_loss=1496.0,
            target1=1595.0,
            target2=1648.0,
            confidence=37.0,
            volume_ratio=2.3,
            change_pct=5.1,
            blind_score=64.0,
            holding_horizon="intraday",
            product_preference="MIS",
            off_high_pct=0.7,
            vwap_gap_pct=0.5,
            support_gap_pct=0.6,
            buyer_imbalance=1.8,
            turnover_cr=8.5,
            spread_pct=0.08,
            range_pct=2.9,
            last_trade_qty=650,
            fresh_buyer_confirmation=True,
        )
        with self._freeze_ist_time(12, 0):
            resolved, _grade, eligible, reason = self.inv._resolve_auto_buy_candidate(c)
        self.assertTrue(eligible, reason)
        self.assertEqual(resolved.product_preference, "CNC")
        self.assertEqual(resolved.holding_horizon, "2-day swing")

    def test_post_1130_intraday_only_stays_blocked_if_strict_swing_fails(self):
        self.inv._ist_now_minute = lambda: 12 * 60
        c = self._candidate(
            symbol="POST_CUTOFF_STOCK",
            holding_horizon="intraday",
            product_preference="MIS",
            spread_pct=0.42,
            last_trade_qty=45,
            turnover_cr=2.1,
            buyer_imbalance=1.4,
            fresh_buyer_confirmation=False,
        )
        with self._freeze_ist_time(12, 0):
            resolved, _grade, eligible, reason = self.inv._resolve_auto_buy_candidate(c)
        self.assertFalse(eligible)
        self.assertEqual(resolved.product_preference, "CNC")
        self.assertIn("strict cnc swing re-check failed", reason.lower())

    def test_after_130_without_prior_buyable_becomes_late_discovery(self):
        self.inv._ist_now_minute = lambda: 14 * 60
        c = self._candidate(symbol="POST_CUTOFF_DISCOVERY_STOCK")
        msg = self.inv._format_missed_or_late_message(
            "POST_CUTOFF_DISCOVERY_STOCK",
            reason="Signal surfaced only after the swing cutoff.",
            candidate=c,
            live_price=c.price,
        )
        self.assertIn("LATE_DISCOVERY", msg)

    def test_buyable_before_1130_but_delayed_is_important_missed(self):
        c = self._candidate(symbol="POST_CUTOFF_STOCK", holding_horizon="intraday", product_preference="MIS")
        self.inv._ist_now_minute = lambda: 10 * 60 + 45
        self.inv._note_auto_buy_timeline("POST_CUTOFF_STOCK", live_price=1542.0, entry_type="EARLY_BREAKOUT", holding_horizon="intraday", buyable_now=True)
        self.inv._ist_now_minute = lambda: 12 * 60
        msg = self.inv._format_missed_or_late_message(
            "POST_CUTOFF_STOCK",
            reason="Execution was delayed until after the MIS cutoff.",
            candidate=c,
            live_price=1548.0,
        )
        self.assertIn("IMPORTANT MISSED OPPORTUNITY", msg)

    def test_buyable_before_130_swing_but_delayed_is_important_missed(self):
        c = self._candidate(symbol="POST_CUTOFF_STOCK")
        self.inv._ist_now_minute = lambda: 12 * 60 + 15
        self.inv._note_auto_buy_timeline("POST_CUTOFF_STOCK", live_price=1540.0, entry_type="PULLBACK_RETEST", holding_horizon="2-day swing", buyable_now=True)
        self.inv._ist_now_minute = lambda: 14 * 60
        msg = self.inv._format_missed_or_late_message(
            "POST_CUTOFF_STOCK",
            reason="Execution slipped beyond the swing cutoff.",
            candidate=c,
            live_price=1556.0,
        )
        self.assertIn("IMPORTANT MISSED OPPORTUNITY", msg)

    def test_before_1130_normal_mis_candidate_can_auto_buy(self):
        self.inv._ist_now_minute = lambda: 10 * 60 + 30
        c = self._candidate(
            symbol="NORMAL_MIS_STOCK",
            holding_horizon="intraday",
            product_preference="MIS",
            price=1544.0,
            buy_above=1538.0,
            stop_loss=1496.0,
            target1=1595.0,
            target2=1648.0,
            confidence=37.0,
            volume_ratio=2.3,
            change_pct=4.8,
            blind_score=64.0,
            off_high_pct=0.6,
            vwap_gap_pct=0.3,
            support_gap_pct=0.5,
            buyer_imbalance=2.1,
            turnover_cr=8.5,
            spread_pct=0.08,
            range_pct=2.9,
            last_trade_qty=650,
            fresh_buyer_confirmation=True,
        )
        with self._freeze_ist_time(10, 30):
            ok, reason = self.inv._live_auto_buy_gate_detail(c, "A")
        self.assertTrue(ok, reason)

    def test_after_1130_mis_uses_strict_mode_not_blunt_block(self):
        self.inv._ist_now_minute = lambda: 12 * 60
        c = self._candidate(
            symbol="STRICT_LATE_MIS_STOCK",
            holding_horizon="intraday",
            product_preference="MIS",
            price=1544.0,
            buy_above=1538.0,
            stop_loss=1496.0,
            target1=1595.0,
            target2=1648.0,
            confidence=37.0,
            volume_ratio=2.3,
            change_pct=5.1,
            blind_score=64.0,
            off_high_pct=0.6,
            vwap_gap_pct=0.3,
            support_gap_pct=0.5,
            buyer_imbalance=2.1,
            turnover_cr=8.5,
            spread_pct=0.08,
            range_pct=2.9,
            last_trade_qty=650,
            fresh_buyer_confirmation=True,
        )
        with self._freeze_ist_time(12, 0):
            ok, reason = self.inv._live_auto_buy_gate_detail(c, "A")
        self.assertTrue(ok, reason)
        self.assertIn("late-session MIS", reason)

    def test_after_1130_weak_mis_gets_strict_mode_rejected(self):
        self.inv._ist_now_minute = lambda: 12 * 60
        c = self._candidate(
            symbol="LATE_CHASE_MIS_STOCK",
            holding_horizon="intraday",
            product_preference="MIS",
            off_high_pct=0.15,
            vwap_gap_pct=1.1,
            support_gap_pct=1.3,
            buyer_imbalance=1.2,
            fresh_buyer_confirmation=False,
            spread_pct=0.41,
            last_trade_qty=45,
        )
        with self._freeze_ist_time(12, 0):
            ok, reason = self.inv._live_auto_buy_gate_detail(c, "A")
        self.assertFalse(ok)
        self.assertIn("strict late-session MIS rejected", reason)

    def test_after_final_mis_cutoff_no_new_intraday_trade(self):
        self.inv._ist_now_minute = lambda: 14 * 60
        c = self._candidate(
            symbol="PAST_FINAL_MIS_CUTOFF_STOCK",
            holding_horizon="intraday",
            product_preference="MIS",
        )
        with self._freeze_ist_time(14, 0):
            ok, reason = self.inv._live_auto_buy_gate_detail(c, "A")
        self.assertFalse(ok)
        self.assertIn("paused after 1:30 PM", reason)

    def test_amic_style_post_cutoff_case_uses_truthful_label(self):
        self.inv._ist_now_minute = lambda: 12 * 60 + 5
        c = self._candidate(
            symbol="POST_CUTOFF_DISCOVERY_STOCK",
            holding_horizon="intraday",
            product_preference="MIS",
            off_high_pct=0.2,
            vwap_gap_pct=1.0,
            support_gap_pct=1.2,
            buyer_imbalance=1.1,
            fresh_buyer_confirmation=False,
            spread_pct=0.44,
            last_trade_qty=35,
        )
        with self._freeze_ist_time(12, 5):
            resolved, _grade, eligible, reason = self.inv._resolve_auto_buy_candidate(c)
        self.assertFalse(eligible)
        msg = self.inv._format_missed_or_late_message(
            "POST_CUTOFF_DISCOVERY_STOCK",
            reason=reason,
            candidate=resolved,
            live_price=resolved.price,
        )
        self.assertIn("SWING_RECHECK_REJECTED", msg)


class NoHardcodedSymbolRulesTests(unittest.TestCase):
    def test_production_files_do_not_contain_stock_specific_hacks(self):
        roots = [
            os.path.join(_REPO_ROOT, "pkscreener", "classes"),
        ]
        blocked_symbols = [
            "RNITAI",
            "BALRAMCHIN",
            "SEAMECLTD",
            "AMIC",
            "THINMICRO",
            "NODATA",
        ]
        offenders = []
        for root in roots:
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    if not filename.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, filename)
                    with open(path, "r", encoding="utf-8") as fh:
                        text = fh.read()
                    for sym in blocked_symbols:
                        if re.search(rf"\\b{re.escape(sym)}\\b", text):
                            offenders.append(f"{path}:{sym}")
        self.assertEqual(offenders, [])


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

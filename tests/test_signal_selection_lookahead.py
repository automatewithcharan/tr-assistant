"""
Regression tests for chronological signal selection and lookahead prevention.

These tests encode the fix applied on 2026-05-14 for the critical lookahead bug
discovered in best_signal_fast() and signal_candidates().

The bug: argmax(score) selected the bar with the highest intraday score (e.g.,
ret_from_open_pct) — for late_day_reversal|short, this picked the intraday price
peak using up to 90 minutes of future data. Corrected behaviour: first qualifying
bar in chronological order.

INVARIANTS TESTED:
  1. signal_candidates() always returns rows sorted by datetime ascending
  2. best_signal_fast() always selects the first chronological qualifying bar
  3. For late_day_reversal, argmax(score) != first_bar when later bars score higher
     (proves the bug would fire on real data if not for the fix)
  4. detect_lookahead_trap() correctly identifies score-time correlation
  5. StageValidator blocks advancement when lookahead_clean is False
  6. Paper simulation records lookahead_clean=False when tier0 flags say so
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.research_kite_intraday import (
    IntradayConfig,
    IntradayDay,
    best_signal_fast,
    detect_lookahead_trap,
    signal_candidates,
)
from pkscreener.classes.StageValidator import StageValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    family: str = "late_day_reversal",
    side: str = "short",
    interval: str = "5minute",
    params: dict | None = None,
    target: float = 1.1,
    stop: float = 0.4,
) -> IntradayConfig:
    if params is None:
        params = {
            "opening_minutes": 30,
            "late_minutes": 270,
            "move_pct": 1.2,
            "min_price": 40.0,
            "min_value_cr": 25.0,
        }
    return IntradayConfig(family, side, interval, params, target, stop, 5, 2.5, "equal")


def _make_intraday_day(
    n_bars: int = 75,
    day_open: float = 100.0,
    avg_value20_cr: float = 50.0,
    ret_profile: list[float] | None = None,
) -> IntradayDay:
    """Build a synthetic IntradayDay for testing.

    ret_profile: list of ret_from_open_pct per bar. If None, linearly increasing
    from 0 to +2.5% (simulates a stock drifting up all day — classic late_day
    reversal candidate where argmax would pick the last bar).
    """
    if ret_profile is None:
        ret_profile = [i * 2.5 / (n_bars - 1) for i in range(n_bars)]

    base_dt = pd.Timestamp("2026-05-14 09:15:00")
    datetimes = np.array([
        (base_dt + pd.Timedelta(minutes=5 * i)).value
        for i in range(n_bars)
    ], dtype=np.int64)

    closes = np.array([day_open * (1 + r / 100.0) for r in ret_profile], dtype=float)
    opens = np.full(n_bars, day_open, dtype=float)
    highs = closes * 1.002
    lows = closes * 0.998
    volumes = np.full(n_bars, 10000, dtype=float)
    bar_index = np.arange(n_bars, dtype=float)
    vwap = closes.copy()
    roll_high3 = pd.Series(highs).rolling(3).max().fillna(highs[0]).values
    roll_low3 = pd.Series(lows).rolling(3).min().fillna(lows[0]).values
    rel_strength_pct = np.zeros(n_bars, dtype=float)
    nifty_ret = np.zeros(n_bars, dtype=float)
    prev_close = day_open
    gap_pct = 0.0

    return IntradayDay(
        date=pd.Timestamp("2026-05-14"),
        symbol="TEST",
        open=opens,
        high=highs,
        low=lows,
        close=closes,
        volume=volumes,
        bar_index=bar_index,
        vwap=vwap,
        day_open=day_open,
        prev_close=prev_close,
        gap_pct=gap_pct,
        avg_value20_cr=avg_value20_cr,
        ret_from_open_pct=np.array(ret_profile, dtype=float),
        rel_strength_pct=rel_strength_pct,
        nifty_ret_from_open_pct=nifty_ret,
        roll_high3=roll_high3,
        roll_low3=roll_low3,
        datetime=datetimes,
        orig_index=np.arange(n_bars, dtype=int),
        rolling_cache={},
    )


def _make_pandas_day(intraday_day: IntradayDay) -> pd.DataFrame:
    """Convert IntradayDay to the pandas DataFrame format used by signal_candidates()."""
    n = len(intraday_day.close)
    return pd.DataFrame({
        "datetime": pd.to_datetime(intraday_day.datetime),
        "open": intraday_day.open,
        "high": intraday_day.high,
        "low": intraday_day.low,
        "close": intraday_day.close,
        "volume": intraday_day.volume,
        "bar_index": intraday_day.bar_index,
        "vwap": intraday_day.vwap,
        "day_open": intraday_day.day_open,
        "prev_close": intraday_day.prev_close,
        "gap_pct": intraday_day.gap_pct,
        "avg_value20_cr": intraday_day.avg_value20_cr,
        "ret_from_open_pct": intraday_day.ret_from_open_pct,
        "rel_strength_pct": intraday_day.rel_strength_pct,
        "nifty_ret_from_open_pct": intraday_day.nifty_ret_from_open_pct,
        "symbol": intraday_day.symbol,
        "date": intraday_day.date,
    })


# ---------------------------------------------------------------------------
# Test 1: signal_candidates() always returns datetime-ascending order
# ---------------------------------------------------------------------------

class TestSignalCandidatesChronological:
    """signal_candidates() must return rows sorted by datetime, earliest first."""

    def test_late_day_reversal_short_sorted_by_datetime(self):
        """Candidate rows must be ordered by time, not by score descending."""
        cfg = _make_config()
        day = _make_intraday_day()  # ret linearly increasing: later bars score higher
        df = _make_pandas_day(day)
        result = signal_candidates(df, cfg)
        if result.empty:
            pytest.skip("No qualifying bars — increase ret_profile")
        dt_values = result["datetime"].values
        assert all(dt_values[i] <= dt_values[i + 1] for i in range(len(dt_values) - 1)), (
            "signal_candidates() returned rows NOT sorted by datetime. "
            "This is the lookahead bug: earlier rows should always come first."
        )

    def test_first_row_is_earliest_qualifying_bar(self):
        """First row of signal_candidates() must be the earliest qualifying bar."""
        cfg = _make_config()
        day = _make_intraday_day()
        df = _make_pandas_day(day)
        result = signal_candidates(df, cfg)
        if result.empty:
            pytest.skip("No qualifying bars")
        # The first qualifying bar is the one with bar_index >= 54 (late_minutes=270/5=54)
        # AND ret_from_open >= 1.2%
        expected_first_idx = result["datetime"].idxmin()
        actual_first_idx = result.index[0]
        assert actual_first_idx == expected_first_idx, (
            f"First row is not the earliest bar: got index {actual_first_idx}, "
            f"expected {expected_first_idx}."
        )

    def test_score_not_sorted_descending(self):
        """Scores of returned rows should NOT be in descending order when bars have
        monotonically increasing ret_from_open (which was the buggy sort order)."""
        cfg = _make_config()
        day = _make_intraday_day()  # ret linearly increasing, so later = higher score
        df = _make_pandas_day(day)
        result = signal_candidates(df, cfg)
        if len(result) < 2:
            pytest.skip("Need at least 2 qualifying bars to test sort order")
        scores = result["signal_score"].values
        # If sorted correctly by datetime (not score desc), earlier bars come first.
        # Since score = ret_from_open increases over time, chronological order means
        # scores should be ASCENDING (or at least not strictly descending).
        is_strictly_descending = all(scores[i] > scores[i + 1] for i in range(len(scores) - 1))
        assert not is_strictly_descending, (
            "signal_candidates() returned scores in strictly descending order. "
            "This indicates the old buggy sort_values(signal_score, ascending=False) "
            "is still in effect."
        )

    def test_vwap_mean_reversion_also_chronological(self):
        """vwap_mean_reversion|short must also return chronological candidates.

        For vwap_mean_reversion|short: stock must be ABOVE VWAP (close > vwap*(1+dev/100))
        AND ret_from_open > dev. This is the mean-reversion short setup: stock ran up,
        is extended above VWAP, you short expecting reversion back to VWAP.
        Old argmax: picks bar with highest close/vwap deviation = peak extension (lookahead).
        Fix: picks first qualifying bar chronologically.
        """
        cfg = _make_config(
            family="vwap_mean_reversion",
            side="short",
            params={"opening_minutes": 15, "dev_pct": 0.45, "min_price": 40.0, "min_value_cr": 25.0},
        )
        # Stock rising: ret_from_open increases from 0 to +3%
        # VWAP set slightly below close so close > vwap*(1+dev/100) throughout
        ret_profile = [0.0] * 3 + [0.5 + i * 2.5 / 72 for i in range(72)]
        day = _make_intraday_day(ret_profile=ret_profile)
        df = _make_pandas_day(day)
        # Set VWAP below close by a fixed amount so close is always above VWAP*(1+0.45/100)
        # close is around 100 * (1 + ret/100), VWAP should be ~99% of close
        df["vwap"] = df["close"] * 0.995  # close is ~0.5% above VWAP
        result = signal_candidates(df, cfg)
        assert not result.empty, (
            "Expected qualifying bars for vwap_mean_reversion|short with rising stock above VWAP. "
            "Adjust ret_profile or vwap so close > vwap*(1+dev/100) and ret_from_open > dev."
        )
        dt_values = result["datetime"].values
        assert all(dt_values[i] <= dt_values[i + 1] for i in range(len(dt_values) - 1)), (
            "vwap_mean_reversion signal_candidates() not chronological"
        )


# ---------------------------------------------------------------------------
# Test 2: best_signal_fast() selects first qualifying bar, not argmax
# ---------------------------------------------------------------------------

class TestBestSignalFastChronological:
    """best_signal_fast() must return the first chronological qualifying bar."""

    def test_returns_first_qualifying_bar_not_peak(self):
        """Given multiple qualifying late-day bars with increasing ret_from_open,
        best_signal_fast must return the first (earliest), not the last (highest score)."""
        cfg = _make_config()
        # ret_from_open: flat at 0 until bar 55, then increases from 1.5% to 3.0%
        ret_profile = [0.0] * 55 + [1.5 + (i * 1.5 / 19) for i in range(20)]
        day = _make_intraday_day(n_bars=75, ret_profile=ret_profile)

        result = best_signal_fast(day, cfg)
        assert result is not None, "Expected a signal but got None"
        selected_pos, selected_score = result

        # First qualifying bar: bar_index >= 54 AND ret_from_open >= 1.2
        # Bar 55 has ret = 1.5% >= 1.2% — this is the first qualifying bar
        assert selected_pos == 55, (
            f"Expected bar 55 (first qualifying), got bar {selected_pos}. "
            f"argmax would have picked bar {74} (highest ret_from_open). "
            f"This is the lookahead bug."
        )

    def test_buggy_argmax_would_pick_different_bar(self):
        """Prove that argmax WOULD have picked a different (later) bar — confirming
        the bug existed and the fix is meaningful, not trivially true."""
        ret_profile = [0.0] * 55 + [1.5 + (i * 1.5 / 19) for i in range(20)]
        day = _make_intraday_day(n_bars=75, ret_profile=ret_profile)
        cfg = _make_config()

        late_bar = 54  # late_minutes=270 / 5minute = 54
        move = 1.2
        candidates = (day.bar_index >= late_bar) & (day.ret_from_open_pct >= move) & (day.close < day.roll_high3)
        score = day.ret_from_open_pct
        candidates = candidates & ~np.isnan(score)

        qualifying = np.flatnonzero(candidates)
        assert len(qualifying) > 1, "Need multiple qualifying bars for this test"

        first_chrono = int(qualifying[0])
        argmax_pick = int(qualifying[np.argmax(score[qualifying])])

        assert first_chrono != argmax_pick, (
            "argmax and chronological picked the same bar — test data does not exercise the bug. "
            "Update ret_profile so later bars have higher ret_from_open_pct."
        )
        assert argmax_pick > first_chrono, (
            f"argmax picked bar {argmax_pick}, chronological picked {first_chrono}. "
            "Expected argmax to pick a LATER bar (higher score = later in day)."
        )

    def test_single_qualifying_bar_returns_that_bar(self):
        """When exactly one bar qualifies, both methods agree — regression safety."""
        cfg = _make_config()
        # Only bar 60 qualifies: exactly at move_pct threshold, all others below
        ret_profile = [0.0] * 60 + [1.2] + [0.0] * 14  # only bar 60 = 1.2%
        # But: bar must also pass close < roll_high3. Use flat close to ensure.
        day = _make_intraday_day(n_bars=75, ret_profile=ret_profile)
        result = best_signal_fast(day, cfg)
        # Whether signal fires depends on roll_high3 filter, but if it does:
        if result is not None:
            selected_pos, _ = result
            assert selected_pos == 60, (
                f"Expected bar 60 (only qualifying bar), got bar {selected_pos}"
            )


# ---------------------------------------------------------------------------
# Test 3: detect_lookahead_trap() correctly identifies score-time correlation
# ---------------------------------------------------------------------------

class TestLookaheadTrapDetector:
    """detect_lookahead_trap() must correctly identify whether argmax differs from first."""

    def test_monotonic_increasing_score_flagged(self):
        """If score increases monotonically with bar position, argmax picks last bar.
        This is the lookahead trap pattern."""
        n = 20
        candidates = np.ones(n, dtype=bool)
        score = np.arange(n, dtype=float)  # 0, 1, 2, ..., 19

        result = detect_lookahead_trap("late_day_reversal", "short", candidates, score)
        assert not result["argmax_is_first"], (
            "argmax_is_first should be False: monotonically increasing score means "
            "argmax picks the last bar, not the first."
        )
        assert result["score_time_corr"] > 0.8, (
            f"score_time_corr should be near 1.0 for monotonically increasing score, "
            f"got {result['score_time_corr']}"
        )

    def test_constant_score_first_qualifies(self):
        """If all qualifying bars have the same score, argmax is ambiguous but
        the fix ensures we pick the first bar."""
        n = 10
        candidates = np.ones(n, dtype=bool)
        score = np.ones(n, dtype=float)  # all equal

        result = detect_lookahead_trap("late_day_reversal", "short", candidates, score)
        # With all equal scores, argmax returns the first (np.argmax returns lowest index on tie)
        assert result["argmax_is_first"] is True, (
            "With equal scores, argmax should return the first bar (numpy behaviour)."
        )

    def test_decreasing_score_flagged_as_anti_lookahead(self):
        """If score decreases with time, argmax picks the first bar — no divergence.
        But score_time_corr will be strongly negative."""
        n = 20
        candidates = np.ones(n, dtype=bool)
        score = np.arange(n, dtype=float)[::-1]  # 19, 18, ..., 0

        result = detect_lookahead_trap("late_day_reversal", "short", candidates, score)
        assert result["argmax_is_first"] is True, (
            "Decreasing score: argmax picks the first (highest-scored) bar, which also "
            "happens to be the chronologically first bar — they agree."
        )
        assert result["score_time_corr"] < -0.8, (
            f"score_time_corr should be near -1.0, got {result['score_time_corr']}"
        )

    def test_no_candidates_returns_safe_result(self):
        """Empty candidates should return safe defaults without crashing."""
        candidates = np.zeros(10, dtype=bool)
        score = np.zeros(10, dtype=float)
        result = detect_lookahead_trap("late_day_reversal", "short", candidates, score)
        assert result["argmax_is_first"] is True
        assert result["n_qualifying"] == 0

    def test_real_late_day_data_profile_is_flagged(self):
        """Simulate the actual ANANTRAJ-style data that triggered the original bug.
        A late-day window with monotonically increasing ret_from_open must be flagged."""
        # Bars 54-74: ret increases from 1.5% to 3.0% (qualifying throughout)
        n = 20
        candidates = np.ones(n, dtype=bool)
        score = np.linspace(1.5, 3.0, n)  # ret_from_open_pct increasing across window

        result = detect_lookahead_trap("late_day_reversal", "short", candidates, score)
        assert not result["argmax_is_first"], (
            "Real late-day data profile with increasing score should flag divergence."
        )
        assert result["score_time_corr"] > 0.9


# ---------------------------------------------------------------------------
# Test 4: StageValidator blocks when lookahead_clean is False
# ---------------------------------------------------------------------------

class TestStageValidatorLookaheadBlock:
    """StageValidator must block Stage 1 advancement when lookahead_clean is False."""

    def test_lookahead_false_tier0_check_fails(self):
        """Setting lookahead_clean=False in tier0_flags must produce a failed Tier 0 check
        for lookahead_clean specifically, regardless of paper-trading data availability."""
        sv = StageValidator()
        result = sv.evaluate(
            "late_day_reversal",  # real strategy with paper data
            days=20,
            target_stage=1,
            tier0_flags={"lookahead_clean": False},
        )
        # Find the lookahead_clean check result
        lookahead_check = next(
            (t for t in result.tier0_results if t.check == "lookahead_clean"), None
        )
        assert lookahead_check is not None, "lookahead_clean check must be present in tier0_results"
        assert not lookahead_check.passed, (
            f"lookahead_clean Tier 0 check must fail when set to False. "
            f"Detail: {lookahead_check.detail}"
        )
        assert "FAILED" in lookahead_check.detail, (
            f"lookahead_clean detail must say FAILED, got: {lookahead_check.detail}"
        )

    def test_lookahead_unverified_tier0_check_fails(self):
        """UNVERIFIED lookahead_clean (not in tier0_flags) must produce a failed check.
        Only explicit True passes."""
        sv = StageValidator()
        result = sv.evaluate(
            "late_day_reversal",
            days=20,
            target_stage=1,
            tier0_flags={},  # lookahead_clean not set = UNVERIFIED
        )
        lookahead_check = next(
            (t for t in result.tier0_results if t.check == "lookahead_clean"), None
        )
        assert lookahead_check is not None
        assert not lookahead_check.passed, (
            "UNVERIFIED lookahead_clean must not pass Tier 0"
        )
        assert "UNVERIFIED" in lookahead_check.detail, (
            f"Detail should say UNVERIFIED, got: {lookahead_check.detail}"
        )

    def test_lookahead_true_check_passes(self):
        """When lookahead_clean=True, that specific Tier 0 check must pass."""
        sv = StageValidator()
        result = sv.evaluate(
            "late_day_reversal",
            days=20,
            target_stage=1,
            tier0_flags={"lookahead_clean": True},
        )
        lookahead_check = next(
            (t for t in result.tier0_results if t.check == "lookahead_clean"), None
        )
        assert lookahead_check is not None
        assert lookahead_check.passed, (
            f"lookahead_clean Tier 0 check must pass when set to True. "
            f"Detail: {lookahead_check.detail}"
        )

    def test_lookahead_false_blocks_overall_readiness(self):
        """When lookahead_clean=False, strategy must not be ready (result.ready=False)."""
        sv = StageValidator()
        result = sv.evaluate(
            "late_day_reversal",
            days=20,
            target_stage=1,
            tier0_flags={"lookahead_clean": False},
        )
        assert not result.ready, (
            "Strategy must not be ready when lookahead_clean=False"
        )
        assert not result.stage_passed, (
            "Stage must not pass when lookahead_clean=False"
        )

    def test_paper_simulation_report_shows_failed_lookahead(self):
        """Stage report must show explicit FAILED for lookahead_clean when set to False."""
        sv = StageValidator()
        result = sv.evaluate(
            "late_day_reversal",
            days=20,
            target_stage=1,
            tier0_flags={"lookahead_clean": False},
        )
        report = StageValidator.format_report(result)
        assert "FAILED" in report, (
            "Stage report must show FAILED when lookahead_clean=False"
        )
        assert "lookahead" in report.lower(), (
            "Stage report must mention 'lookahead' when lookahead_clean=False"
        )


# ---------------------------------------------------------------------------
# Test 5: No strategy can be created with score that peaks at end of window
# (structural/documentation test — documents the invariant)
# ---------------------------------------------------------------------------

class TestSignalSelectionInvariant:
    """Document the core invariant: signal bar is always the first qualifying bar."""

    def test_invariant_documentation(self):
        """This test documents the expected behaviour as a living spec.

        THE INVARIANT:
          For any strategy family and side, the selected signal bar must be the
          first qualifying bar in chronological order.

        WHY:
          In live trading, a trader can only act on information available AT THE
          CURRENT BAR. The first qualifying bar is the first moment in time at
          which the entry conditions are satisfied. Choosing any later bar uses
          future price data (even if only implicitly through score maximisation).

        CONSEQUENCE:
          Any scoring function that correlates positively with future price moves
          (e.g., ret_from_open_pct for an intraday trend strategy) will select
          a bar near the intraday extreme when argmax is used. This fabricates
          edge that does not exist.

        CORRECT IMPLEMENTATION:
          signal_candidates() must return rows sorted by datetime ascending.
          best_signal_fast() must use flatnonzero(candidates)[0], not argmax(score).
        """
        cfg = _make_config()
        day = _make_intraday_day()
        result = best_signal_fast(day, cfg)
        df = _make_pandas_day(day)
        candidates_df = signal_candidates(df, cfg)

        if result is not None:
            pos, _ = result
            if not candidates_df.empty:
                # Both paths agree on the first qualifying bar
                first_dt_fast = pd.Timestamp(day.datetime[pos])
                first_dt_legacy = candidates_df["datetime"].iloc[0]
                assert first_dt_fast == first_dt_legacy, (
                    f"Fast path ({first_dt_fast}) and legacy path ({first_dt_legacy}) "
                    f"selected different bars — implementations are inconsistent."
                )

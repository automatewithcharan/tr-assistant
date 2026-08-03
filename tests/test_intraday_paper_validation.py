"""
Tests for the intraday short paper trading validation layer.

Covers:
  - ShortabilityValidator: ASM/GSM blocking, liquidity gate, rejection log
  - IntradayPaperTrader: signal acceptance, fill simulation, circuit breakers
  - IntradayPaperReport: daily report, approval criteria
"""

import os
import sys
import tempfile
import csv
import json
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from pkscreener.classes.ShortabilityValidator import ShortabilityValidator, ShortabilityResult
from pkscreener.classes.IntradayPaperTrader import (
    IntradayPaperTrader,
    IntradayShortSignal,
    SymbolCircuitBreaker,
    DailyCircuitBreaker,
    BAD_FILL_THRESHOLD_PCT,
    MAX_BAD_FILL_STRIKES,
    MAX_DAILY_LOSS_PCT,
)


# ---------------------------------------------------------------------------
# ShortabilityValidator tests
# ---------------------------------------------------------------------------

class InMemorySurveillanceProvider:
    def __init__(self, asm=None, gsm=None, error=None):
        self.asm = {"data": []} if asm is None else asm
        self.gsm = {"data": []} if gsm is None else gsm
        self.error = error
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.asm, self.gsm


class TestShortabilityValidator:

    def _make_validator(self, tmpdir, provider=None, clock=datetime.now) -> ShortabilityValidator:
        v = ShortabilityValidator(
            data_dir=tmpdir,
            surveillance_provider=provider or InMemorySurveillanceProvider(),
            clock=clock,
        )
        return v

    def test_rejects_unknown_liquidity(self, tmp_path):
        v = self._make_validator(str(tmp_path))
        result = v.validate("UNKNOWN_SYM")
        assert not result.allowed
        assert result.status == "unknown"

    def test_rejects_low_liquidity(self, tmp_path):
        v = self._make_validator(str(tmp_path))
        result = v.validate("LOWLIQ", avg_value_cr=2.0)
        assert not result.allowed
        assert result.status == "low_liquidity"
        assert "2.0Cr" in result.reason

    def test_allows_high_liquidity(self, tmp_path):
        v = self._make_validator(str(tmp_path))
        result = v.validate("RELIANCE", avg_value_cr=500.0)
        assert result.allowed
        assert result.status == "shortable"

    def test_blocks_asm_symbol(self, tmp_path):
        v = self._make_validator(str(tmp_path))
        # Prime cache_date so _refresh_surveillance_lists() is skipped
        v._cache_date = date.today().isoformat()
        v._asm_symbols = {"ASMSTOCK"}
        v._surveillance_health = "healthy_cache"
        result = v.validate("ASMSTOCK", avg_value_cr=100.0)
        assert not result.allowed
        assert result.status == "asm"

    def test_blocks_gsm_symbol(self, tmp_path):
        v = self._make_validator(str(tmp_path))
        v._cache_date = date.today().isoformat()
        v._gsm_symbols = {"GSMSTOCK"}
        v._surveillance_health = "healthy_cache"
        result = v.validate("GSMSTOCK", avg_value_cr=100.0)
        assert not result.allowed
        assert result.status == "gsm"

    def test_asm_takes_priority_over_liquidity(self, tmp_path):
        v = self._make_validator(str(tmp_path))
        v._cache_date = date.today().isoformat()
        v._asm_symbols = {"BIGSTOCK"}
        v._surveillance_health = "healthy_cache"
        result = v.validate("BIGSTOCK", avg_value_cr=1000.0)
        assert not result.allowed
        assert result.status == "asm"

    def test_rejection_log_written(self, tmp_path):
        v = self._make_validator(str(tmp_path))
        v.log_rejection("BADSTOCK", "09:32:00", "GSM restricted", "gsm", price=100.0, strategy="test")
        rows = v.load_rejections_for_date(date.today().isoformat())
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BADSTOCK"
        assert rows[0]["status"] == "gsm"

    def test_rejection_summary_counts(self, tmp_path):
        v = self._make_validator(str(tmp_path))
        v.log_rejection("A", "09:30:00", "ASM", "asm")
        v.log_rejection("B", "09:31:00", "GSM", "gsm")
        v.log_rejection("C", "09:32:00", "Low liq", "low_liquidity")
        summary = v.get_rejection_summary()
        assert summary.get("asm") == 1
        assert summary.get("gsm") == 1
        assert summary.get("low_liquidity") == 1

    def test_liquidity_map_loaded(self, tmp_path):
        # Write a temp CSV
        csv_path = str(tmp_path / "liq.csv")
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "avg_value20_cr_median"])
            w.writerow(["TESTSTOCK", "50.5"])
        v = self._make_validator(str(tmp_path))
        v.load_liquidity_map(csv_path)
        result = v.validate("TESTSTOCK")
        assert result.allowed
        assert result.avg_value_cr == pytest.approx(50.5)

    def test_cache_prevents_double_check(self, tmp_path):
        provider = InMemorySurveillanceProvider({"data": [{"symbol": "CACHED"}]})
        v = self._make_validator(str(tmp_path), provider=provider)
        r1 = v.validate("CACHED", avg_value_cr=100.0)
        # Remove from the loaded set — cached result must not call provider again.
        v._asm_symbols.clear()
        r2 = v.validate("CACHED", avg_value_cr=100.0)
        assert r1.allowed == r2.allowed
        assert r1.status  == r2.status
        assert provider.calls == 1

    def test_empty_and_malformed_payloads_are_deterministic(self, tmp_path):
        empty = InMemorySurveillanceProvider({"data": []}, {"data": []})
        malformed = InMemorySurveillanceProvider({"data": "bad"}, [])
        assert self._make_validator(str(tmp_path / "empty"), empty).validate("SAFE", 20).allowed
        malformed_result = self._make_validator(str(tmp_path / "malformed"), malformed).validate("SAFE", 20)
        assert not malformed_result.allowed
        assert malformed_result.status == "surveillance_unavailable"

    def test_provider_exception_fails_closed(self, tmp_path):
        provider = InMemorySurveillanceProvider(error=RuntimeError("fixture failure"))
        result = self._make_validator(str(tmp_path), provider).validate("SAFE", 20)
        assert not result.allowed and result.status == "surveillance_unavailable" and provider.calls == 1

    def test_duplicate_symbols_and_normalization_block_once(self, tmp_path):
        provider = InMemorySurveillanceProvider({"data": [{"symbol": " asmstock "}, {"symbol": "ASMSTOCK"}]})
        result = self._make_validator(str(tmp_path), provider).validate(" asmstock ", 100)
        assert not result.allowed and result.status == "asm" and provider.calls == 1

    def test_stale_cache_refreshes_with_injected_clock(self, tmp_path):
        now = datetime(2026, 1, 2, 10, 0)
        provider = InMemorySurveillanceProvider({"data": [{"symbol": "FRESH"}]})
        cache = tmp_path / "asm_gsm_cache.json"
        cache.write_text(json.dumps({"cached_at": (now - timedelta(hours=5)).isoformat(), "asm": ["OLD"], "gsm": []}), encoding="utf-8")
        result = self._make_validator(str(tmp_path), provider, clock=lambda: now).validate("FRESH", 100)
        assert not result.allowed and provider.calls == 1

    def test_valid_cache_reuses_without_provider(self, tmp_path):
        now = datetime(2026, 1, 2, 10, 0)
        provider = InMemorySurveillanceProvider(error=AssertionError("provider should not run"))
        (tmp_path / "asm_gsm_cache.json").write_text(json.dumps({"cached_at": now.isoformat(), "asm": ["CACHED"], "gsm": []}), encoding="utf-8")
        result = self._make_validator(str(tmp_path), provider, clock=lambda: now).validate("cached", 100)
        assert not result.allowed and result.status == "asm" and provider.calls == 0

    def test_offline_mode_remains_deterministic_with_injected_provider(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PKSCREENER_TEST_OFFLINE", "1")
        provider = InMemorySurveillanceProvider({"data": [{"symbol": "OFFLINE"}]})
        result = self._make_validator(str(tmp_path), provider).validate("offline", 100)
        assert not result.allowed and provider.calls == 1


# ---------------------------------------------------------------------------
# SymbolCircuitBreaker tests
# ---------------------------------------------------------------------------

class TestSymbolCircuitBreaker:

    def test_no_disable_on_good_fill(self):
        cb = SymbolCircuitBreaker()
        for _ in range(10):
            cb.record_fill("GOOD", 0.05)  # below threshold
        disabled, _ = cb.is_disabled("GOOD")
        assert not disabled

    def test_disables_after_max_strikes(self):
        cb = SymbolCircuitBreaker()
        for _ in range(MAX_BAD_FILL_STRIKES):
            cb.record_fill("BAD", BAD_FILL_THRESHOLD_PCT + 0.1)
        disabled, reason = cb.is_disabled("BAD")
        assert disabled
        assert "bad fills" in reason.lower()

    def test_strikes_reset_on_good_fill(self):
        cb = SymbolCircuitBreaker()
        # 2 bad fills, then 1 good fill, then check: should NOT be disabled yet
        cb.record_fill("PARTIAL", BAD_FILL_THRESHOLD_PCT + 0.1)
        cb.record_fill("PARTIAL", BAD_FILL_THRESHOLD_PCT + 0.1)
        cb.record_fill("PARTIAL", 0.0)  # good fill resets
        disabled, _ = cb.is_disabled("PARTIAL")
        assert not disabled

    def test_reset_clears_disable(self):
        cb = SymbolCircuitBreaker()
        for _ in range(MAX_BAD_FILL_STRIKES):
            cb.record_fill("X", BAD_FILL_THRESHOLD_PCT + 0.5)
        cb.reset("X")
        disabled, _ = cb.is_disabled("X")
        assert not disabled


# ---------------------------------------------------------------------------
# DailyCircuitBreaker tests
# ---------------------------------------------------------------------------

class TestDailyCircuitBreaker:

    def test_not_halted_initially(self):
        cb = DailyCircuitBreaker()
        halted, _ = cb.is_halted()
        assert not halted

    def test_halts_on_max_loss(self):
        cb = DailyCircuitBreaker()
        cb.record_trade(-MAX_DAILY_LOSS_PCT - 0.01)
        halted, reason = cb.is_halted()
        assert halted
        assert "Daily loss" in reason

    def test_does_not_halt_before_loss_breach(self):
        cb = DailyCircuitBreaker()
        cb.record_trade(-1.0)
        cb.record_trade(-1.0)
        halted, _ = cb.is_halted()
        assert not halted

    def test_running_pnl_accumulates(self):
        cb = DailyCircuitBreaker()
        cb.record_trade(-1.5)
        cb.record_trade(0.8)
        assert cb.running_pnl == pytest.approx(-0.7)

    def test_trade_count_increments(self):
        cb = DailyCircuitBreaker()
        cb.record_trade(-0.1)
        cb.record_trade(0.2)
        assert cb.trade_count == 2


# ---------------------------------------------------------------------------
# IntradayPaperTrader tests
# ---------------------------------------------------------------------------

class TestIntradayPaperTrader:

    def _make_trader(self, tmpdir) -> IntradayPaperTrader:
        t = IntradayPaperTrader(data_dir=tmpdir)
        return t

    def _signal(self, symbol: str = "TESTCO", price: float = 100.0) -> IntradayShortSignal:
        return IntradayShortSignal(
            symbol=symbol,
            signal_time="09:35:00",
            intended_entry=price,
            stop_loss=price * 1.015,
            target=price * 0.975,
            strategy="gap_fade_short",
            avg_value_cr=50.0,
            spread=0.20,
            volume_ratio=2.5,
        )

    def test_accept_valid_signal(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        accepted, reason = t.accept_signal(self._signal(), shortability_allowed=True)
        assert accepted
        assert reason == "accepted"

    def test_reject_shortability_blocked(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        accepted, reason = t.accept_signal(
            self._signal(), shortability_allowed=False, shortability_reason="ASM restricted"
        )
        assert not accepted
        assert "ASM" in reason

    def test_reject_duplicate_signal_same_day(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        t.accept_signal(self._signal(), shortability_allowed=True)
        accepted, reason = t.accept_signal(self._signal(), shortability_allowed=True)
        assert not accepted
        assert "already has an open" in reason

    def test_fill_simulation_records_slippage(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        t.accept_signal(self._signal("FILL", 200.0), shortability_allowed=True)
        t.update_fill("FILL", next_bar_open=200.5, spread=0.20)
        trade = t._trades["FILL"]
        # fill = 200.5 + 0.10 (half spread) = 200.60
        assert trade.simulated_fill == pytest.approx(200.60, abs=0.01)
        # slippage = (200.60 - 200.0) / 200.0 * 100 = 0.30 %
        assert trade.fill_slippage_pct == pytest.approx(0.30, abs=0.01)

    def test_exit_records_pnl_for_short(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        t.accept_signal(self._signal("EXIT", 100.0), shortability_allowed=True)
        t.update_fill("EXIT", next_bar_open=100.0, spread=0.0)
        # Short exits at 97.5 (target hit) — profit
        t.update_exit("EXIT", 97.5, "10:00:00", "target_hit", day_high=100.5, day_low=97.2)
        trade = t._trades["EXIT"]
        assert trade.closed
        assert trade.pnl_pct > 0     # short profit when price falls
        assert trade.pnl_after_costs  # also positive after costs
        assert trade.target_outcome == "hit"

    def test_stop_hit_records_loss(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        t.accept_signal(self._signal("STOP", 100.0), shortability_allowed=True)
        t.update_fill("STOP", next_bar_open=100.0, spread=0.0)
        # Stop at 101.5 — loss for short
        t.update_exit("STOP", 101.5, "10:05:00", "stop_hit", day_high=101.5, day_low=99.0)
        trade = t._trades["STOP"]
        assert trade.closed
        assert trade.pnl_pct < 0
        assert trade.stop_outcome == "hit"

    def test_close_day_eod_exit(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        t.accept_signal(self._signal("EOD", 100.0), shortability_allowed=True)
        t.update_fill("EOD", next_bar_open=100.0, spread=0.0)
        summary = t.close_day({"EOD": 99.0})
        assert summary
        trade = t._trades["EOD"]
        assert trade.closed
        assert trade.exit_reason == "eod"

    def test_daily_circuit_breaker_halts_new_signals(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        # Force halt via daily circuit breaker
        t._daily_cb.record_trade(-MAX_DAILY_LOSS_PCT - 1.0)
        accepted, reason = t.accept_signal(self._signal("AFTER_HALT"), shortability_allowed=True)
        assert not accepted
        assert "circuit breaker" in reason.lower()

    def test_symbol_circuit_breaker_disables_symbol(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        for _ in range(MAX_BAD_FILL_STRIKES):
            t._symbol_cb.record_fill("BADFILLSYM", BAD_FILL_THRESHOLD_PCT + 0.5)
        accepted, reason = t.accept_signal(
            self._signal("BADFILLSYM"), shortability_allowed=True
        )
        assert not accepted
        assert "circuit breaker" in reason.lower()

    def test_slippage_alert_triggered(self, tmp_path):
        t = self._make_trader(str(tmp_path))
        t.accept_signal(self._signal("SLIPALERT", 100.0), shortability_allowed=True)
        # next bar open is much higher — causes large slippage for short entry
        t.update_fill("SLIPALERT", next_bar_open=103.0, spread=0.0)
        alerts = t.get_slippage_alerts()
        assert len(alerts) >= 1
        assert "SLIPPAGE ALERT" in alerts[0]

    def test_persist_and_reload(self, tmp_path):
        t1 = self._make_trader(str(tmp_path))
        t1.accept_signal(self._signal("PERSIST", 50.0), shortability_allowed=True)
        t1.update_fill("PERSIST", next_bar_open=50.1, spread=0.10)

        # Re-create trader — should load from disk
        t2 = self._make_trader(str(tmp_path))
        assert "PERSIST" in t2._trades
        assert t2._trades["PERSIST"].simulated_fill > 0


# ---------------------------------------------------------------------------
# IntradayPaperReport tests
# ---------------------------------------------------------------------------

class TestIntradayPaperReport:

    def test_daily_report_empty(self, tmp_path):
        from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader
        from pkscreener.classes.ShortabilityValidator import ShortabilityValidator
        from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator

        trader    = IntradayPaperTrader(data_dir=str(tmp_path / "paper"))
        validator = ShortabilityValidator(data_dir=str(tmp_path / "short"))
        reporter  = IntradayPaperReportGenerator(trader, validator)

        report = reporter.daily_report("2099-01-01")  # no data
        assert "Daily Report" in report
        assert "0" in report  # all zeros

    def test_approval_criteria_fails_no_data(self, tmp_path):
        from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader
        from pkscreener.classes.ShortabilityValidator import ShortabilityValidator
        from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator

        trader    = IntradayPaperTrader(data_dir=str(tmp_path / "paper"))
        validator = ShortabilityValidator(data_dir=str(tmp_path / "short"))
        reporter  = IntradayPaperReportGenerator(trader, validator)

        report = reporter.check_approval_criteria(days=20)
        assert "NOT YET APPROVED" in report
        assert "FAIL" in report

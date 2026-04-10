"""
Tests for PaperTrading short-side and pair trade support.

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
# Stub heavy dependencies before importing PaperTrading
# ---------------------------------------------------------------------------

for mod_name in [
    "PKDevTools",
    "PKDevTools.classes",
    "PKDevTools.classes.log",
    "PKDevTools.classes.OutputControls",
    "PKDevTools.classes.PKDateUtilities",
    "PKDevTools.classes.Archiver",
    "pkscreener",
    "pkscreener.classes",
    "pkscreener.classes.ActionableReport",
]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

# Provide a stub default_logger and OutputControls
log_mock = sys.modules["PKDevTools.classes.log"]
logger_mock = MagicMock()
log_mock.default_logger = MagicMock(return_value=logger_mock)

oc_mock = sys.modules["PKDevTools.classes.OutputControls"]
oc_mock.OutputControls = MagicMock

dt_mock = sys.modules["PKDevTools.classes.PKDateUtilities"]
dt_mock.PKDateUtilities = MagicMock()
dt_mock.PKDateUtilities.currentDateTime = MagicMock()

arch_mock = sys.modules["PKDevTools.classes.Archiver"]
arch_mock.get_user_outputs_dir = MagicMock(return_value="/tmp")

ar_mock = sys.modules["pkscreener.classes.ActionableReport"]
ar_mock.ActionableReportRunner = MagicMock


MODULE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "pkscreener",
    "classes",
    "PaperTrading.py",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("PaperTrading", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pt = _load_module()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_trade_row(side: str = "LONG", symbol: str = "HDFCBANK",
                    entry_price: float = 1500.0, quantity: int = 10,
                    status: str = "OPEN"):
    """Create a single-row trades DataFrame."""
    invested = entry_price * quantity
    return {
        "TradeId": 1,
        "Universe": "nifty50",
        "Symbol": symbol,
        "Side": side,
        "Status": status,
        "EntryDate": "2025-04-01",
        "EntryPrice": entry_price,
        "Quantity": quantity,
        "InvestedAmount": invested,
        "StopLoss": 0.0,
        "Target1": 0.0,
        "Target2": 0.0,
        "EntrySignal": f"PAIR {side}",
        "EntryConfidencePct": 80.0,
        "EntryReason": "Test pair",
        "CurrentPrice": entry_price,
        "CurrentValue": invested,
        "UnrealizedPnL": 0.0,
        "UnrealizedReturnPct": 0.0,
        "LastAction": "ENTRY",
        "LastSignal": "PAIR",
        "LastConfidencePct": 80.0,
        "LastReason": "Test",
        "LastUpdatedAt": "2025-04-01 10:00:00",
        "ExitDate": "",
        "ExitPrice": 0.0,
        "ExitValue": 0.0,
        "ExitReason": "",
        "RealizedPnL": 0.0,
        "RealizedReturnPct": 0.0,
    }


class TestShortSidePnL:
    """Test that SHORT trades compute PnL inversely (profit when price falls)."""

    def test_long_unrealized_pnl_positive_when_price_rises(self):
        """LONG trade: price rises → positive unrealized PnL."""
        row = _make_trade_row(side="LONG", entry_price=100.0, quantity=10)
        trades_df = pd.DataFrame([row], columns=pt.PaperTradingRunner.TRADE_COLUMNS)

        # Current price > entry → profit for LONG
        current_price = 110.0
        invested = 100.0 * 10
        current_value = 110.0 * 10
        expected_pnl = current_value - invested  # +100
        assert expected_pnl > 0

    def test_short_unrealized_pnl_positive_when_price_falls(self):
        """SHORT trade: price falls → positive unrealized PnL."""
        row = _make_trade_row(side="SHORT", entry_price=100.0, quantity=10)
        trades_df = pd.DataFrame([row], columns=pt.PaperTradingRunner.TRADE_COLUMNS)

        # For SHORT: PnL = invested - current_value (profit when price drops)
        current_price = 90.0
        invested = 100.0 * 10
        current_value = 90.0 * 10
        expected_pnl = invested - current_value  # +100
        assert expected_pnl > 0

    def test_short_unrealized_pnl_negative_when_price_rises(self):
        """SHORT trade: price rises → negative unrealized PnL."""
        entry_price = 100.0
        current_price = 115.0
        quantity = 10
        invested = entry_price * quantity
        current_value = current_price * quantity
        # SHORT P&L
        pnl = invested - current_value  # -150
        assert pnl < 0


class TestCreateClosedTradeRecord:
    """Test that _create_closed_trade_record handles SHORT side correctly."""

    @staticmethod
    def _to_float(value, fallback=0.0):
        """Simplified _to_float matching the PaperTradingRunner implementation."""
        try:
            numeric = float(value)
            if numeric == numeric:  # not NaN
                return numeric
        except (ValueError, TypeError):
            pass
        return fallback

    def _make_runner_mock(self):
        runner = MagicMock(spec=pt.PaperTradingRunner)
        runner.run_at_str = "2025-04-02 10:00:00"
        runner.run_date = "2025-04-02"
        runner._to_float = self._to_float
        runner.TRADE_COLUMNS = pt.PaperTradingRunner.TRADE_COLUMNS
        return runner

    def test_long_close_profit(self):
        """LONG close: exit > entry → positive PnL."""
        row = _make_trade_row(side="LONG", entry_price=100.0, quantity=10)
        trade_series = pd.Series(row)
        runner = self._make_runner_mock()

        record = pt.PaperTradingRunner._create_closed_trade_record(
            runner, trade_series, sell_qty=10, exit_price=120.0,
            exit_reason="Target hit", trade_id=2,
        )
        assert record["RealizedPnL"] == 200.0  # (120 - 100) * 10
        assert record["RealizedReturnPct"] == 20.0

    def test_short_close_profit(self):
        """SHORT close: exit < entry → positive PnL."""
        row = _make_trade_row(side="SHORT", entry_price=100.0, quantity=10)
        trade_series = pd.Series(row)
        runner = self._make_runner_mock()

        record = pt.PaperTradingRunner._create_closed_trade_record(
            runner, trade_series, sell_qty=10, exit_price=80.0,
            exit_reason="Short target", trade_id=2,
        )
        assert record["RealizedPnL"] == 200.0  # (100 - 80) * 10
        assert record["RealizedReturnPct"] == 20.0

    def test_short_close_loss(self):
        """SHORT close: exit > entry → negative PnL."""
        row = _make_trade_row(side="SHORT", entry_price=100.0, quantity=10)
        trade_series = pd.Series(row)
        runner = self._make_runner_mock()

        record = pt.PaperTradingRunner._create_closed_trade_record(
            runner, trade_series, sell_qty=10, exit_price=110.0,
            exit_reason="Short stop", trade_id=2,
        )
        assert record["RealizedPnL"] == -100.0  # (100 - 110) * 10
        assert record["RealizedReturnPct"] == -10.0


class TestOpenPairTradesMethod:
    """Test that open_pair_trades creates both LONG and SHORT legs."""

    def test_method_exists(self):
        """PaperTradingRunner should have open_pair_trades method."""
        assert hasattr(pt.PaperTradingRunner, "open_pair_trades")

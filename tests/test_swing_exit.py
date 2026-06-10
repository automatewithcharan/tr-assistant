"""
Swing exit management tests.

Covers:
 1. Partial profit (50%) booked at T1; runner (50%) remains open
 2. GTT qty flag set after T1 partial (CNC only, not intraday)
 3. Trailing stop does NOT activate before threshold; activates after
 4. Trailing stop floor never goes below entry price
 5. Stop updates upward on new highs, never downward
 6. Swing failed-structure exits on same-day deep reversal
 7. Swing failed-structure skips when T1 already booked
 8. Swing failed-structure skips when trade is too young
 9. Swing failed-structure does NOT fire on intraday (MIS) positions
10. Strong winner trails instead of exiting at fixed target
11. Duplicate GTT flag: gtt_needs_reissue is cleared after reissue
12. Override logging: log_override and get_override_stats round-trip
13. Missing VWAP / ATR / tick data does not crash check_sell_signals
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import pytest

from pkscreener.classes.KiteOrderManager import (
    KiteOrderManager,
    SellReason,
    TrackedPosition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ist_now_str(delta_minutes: int = 0) -> str:
    """Return an ISO timestamp offset from now (for buy_time injection)."""
    from pkscreener.classes.KiteOrderManager import _ist_now
    t = _ist_now() - timedelta(minutes=abs(delta_minutes)) if delta_minutes >= 0 else _ist_now()
    return t.isoformat(timespec="seconds")


def _make_manager(dry_run: bool = True) -> KiteOrderManager:
    mgr = KiteOrderManager.__new__(KiteOrderManager)
    mgr.dry_run = dry_run
    mgr.auto_trade = False
    mgr.output_dir = "/tmp/test_kom"
    mgr._positions_file = MagicMock()
    mgr._positions_file.exists.return_value = False
    mgr._orders_log_file = MagicMock()
    mgr._pending_buys_file = MagicMock()
    mgr._pending_buys_file.exists.return_value = False
    mgr._positions = {}
    mgr._pending_buys = {}
    mgr._lock = threading.Lock()
    mgr.product_type = "MIS"
    mgr.trailing_stop_pct = 3.0
    mgr.trailing_trail_pct = 1.0
    mgr.swing_trail_activate_pct = 5.0
    mgr.swing_trail_dist_pct = 4.0
    mgr.swing_failed_min_age_sec = 900.0
    mgr.swing_failed_min_drop_pct = 1.5
    mgr.failed_breakout_min_age_sec = 180.0
    mgr.failed_breakout_max_age_sec = 1800.0
    mgr.failed_breakout_min_drop_pct = 0.4
    mgr.failed_breakout_sell_imbalance = 1.3
    mgr.max_hold_days = 10
    mgr.confirm_stop_loss_with_broker = False
    mgr._save_positions = MagicMock()
    mgr._load_ticks = MagicMock(return_value={})
    mgr._broker_last_price = MagicMock(return_value=0.0)
    return mgr


def _cnc_pos(
    buy_price: float = 100.0,
    qty: int = 10,
    sl: float = 94.0,
    t1: float = 107.0,
    t2: float = 118.0,
    age_minutes: int = 60,
    gtt_id: str = "GTT-TEST",
) -> TrackedPosition:
    pos = TrackedPosition(
        symbol="TSTOCK",
        qty=qty,
        buy_price=buy_price,
        stop_loss=sl,
        target1=t1,
        target2=t2,
        product_type="CNC",
        holding_horizon="swing",
    )
    pos.buy_time = _ist_now_str(age_minutes)
    pos.gtt_trigger_id = gtt_id
    pos.initial_qty = qty
    return pos


# ---------------------------------------------------------------------------
# Test 1: Partial profit booked at T1; runner remains
# ---------------------------------------------------------------------------

def test_partial_profit_booked_at_t1_runner_remains():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=100.0, qty=10, t1=107.0, t2=118.0)
    mgr._positions["TSTOCK"] = pos

    signals = mgr.check_sell_signals({"TSTOCK": {"last_price": 108.0}})

    partial_signals = [s for s in signals if s[1] == SellReason.PARTIAL_T1]
    assert len(partial_signals) == 1, "Expected exactly one PARTIAL_T1 signal"
    _, _, price, qty_sold = partial_signals[0]
    assert qty_sold == 5, f"Expected 5 shares sold (50%), got {qty_sold}"
    assert pos.t1_booked is True
    # Full T2 exit must NOT have been triggered
    full_exits = [s for s in signals if s[1] == SellReason.TARGET_2]
    assert len(full_exits) == 0, "T2 exit should not fire when only T1 was hit"


# ---------------------------------------------------------------------------
# Test 2: gtt_needs_reissue set for CNC after T1, NOT for MIS
# ---------------------------------------------------------------------------

def test_gtt_needs_reissue_set_for_cnc_only():
    mgr = _make_manager()
    cnc = _cnc_pos(buy_price=100.0, qty=10, t1=107.0, t2=118.0)
    mgr._positions["TSTOCK"] = cnc
    mgr.check_sell_signals({"TSTOCK": {"last_price": 108.0}})
    assert cnc.gtt_needs_reissue is True, "CNC position should set gtt_needs_reissue after T1"

    # MIS position must NOT set the flag
    mgr2 = _make_manager()
    mis_pos = TrackedPosition(
        symbol="MSTOCK", qty=10, buy_price=100.0,
        stop_loss=95.0, target1=105.0, target2=110.0,
        product_type="MIS", holding_horizon="intraday",
    )
    mis_pos.buy_time = _ist_now_str(30)
    mgr2._positions["MSTOCK"] = mis_pos
    mgr2.check_sell_signals({"MSTOCK": {"last_price": 106.0}})
    assert not mis_pos.gtt_needs_reissue, "MIS position must not set gtt_needs_reissue"


# ---------------------------------------------------------------------------
# Test 3: Trailing stop does NOT activate below threshold
# ---------------------------------------------------------------------------

def test_trailing_stop_not_activated_below_threshold():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=100.0, qty=10, t1=120.0, t2=130.0)
    mgr._positions["TSTOCK"] = pos

    # +3% gain — below swing threshold of 5%
    mgr.check_sell_signals({"TSTOCK": {"last_price": 103.0}})
    assert pos.trailing_stop == 0.0, "Trailing stop must not activate below threshold"


def test_trailing_stop_activates_at_threshold():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=100.0, qty=10, t1=120.0, t2=130.0)
    mgr._positions["TSTOCK"] = pos

    # +5% gain — exactly at swing threshold
    mgr.check_sell_signals({"TSTOCK": {"last_price": 105.0}})
    assert pos.trailing_stop > 0.0, "Trailing stop must activate at threshold"
    # Trail = 105 * (1 - 0.04) = 100.8
    assert pos.trailing_stop >= pos.buy_price, "Trailing stop must not go below entry"


# ---------------------------------------------------------------------------
# Test 4: Trailing stop floor never below entry
# ---------------------------------------------------------------------------

def test_trailing_stop_never_below_entry():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=100.0, qty=10, t1=120.0, t2=130.0)
    mgr._positions["TSTOCK"] = pos

    # Price +5.1% — just enough to activate trailing, which would be 105.1 * 0.96 = 100.9
    mgr.check_sell_signals({"TSTOCK": {"last_price": 105.1}})
    assert pos.trailing_stop >= pos.buy_price


# ---------------------------------------------------------------------------
# Test 5: Stop moves up on new high, never down
# ---------------------------------------------------------------------------

def test_trailing_stop_only_moves_up():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=100.0, qty=10, t1=120.0, t2=130.0)
    mgr._positions["TSTOCK"] = pos

    mgr.check_sell_signals({"TSTOCK": {"last_price": 110.0}})
    stop_after_high = pos.trailing_stop

    # Price drops back to 106 — trailing stop must not decrease
    mgr.check_sell_signals({"TSTOCK": {"last_price": 106.0}})
    assert pos.trailing_stop >= stop_after_high, "Trailing stop must never move down"


# ---------------------------------------------------------------------------
# Test 6: Swing failed-structure exits on same-day reversal
# ---------------------------------------------------------------------------

def test_swing_failed_structure_exits_same_day():
    mgr = _make_manager()
    # Trade is 16 minutes old (> 15 min threshold), -2% (> 1.5% threshold)
    pos = _cnc_pos(buy_price=100.0, qty=10, sl=94.0, t1=107.0, t2=118.0, age_minutes=16)
    mgr._positions["TSTOCK"] = pos

    signals = mgr.check_sell_signals({"TSTOCK": {"last_price": 97.8}})  # -2.2%
    reasons = [s[1] for s in signals]
    assert SellReason.SWING_FAILED_STRUCTURE in reasons, \
        f"Expected SWING_FAILED_STRUCTURE, got: {reasons}"


# ---------------------------------------------------------------------------
# Test 7: Swing failed-structure skips after T1 booked
# ---------------------------------------------------------------------------

def test_swing_failed_structure_skips_after_t1():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=100.0, qty=5, sl=94.0, t1=107.0, t2=118.0, age_minutes=16)
    pos.t1_booked = True  # T1 already hit
    mgr._positions["TSTOCK"] = pos

    signals = mgr.check_sell_signals({"TSTOCK": {"last_price": 97.8}})
    reasons = [s[1] for s in signals]
    assert SellReason.SWING_FAILED_STRUCTURE not in reasons, \
        "SWING_FAILED_STRUCTURE must not fire after T1 booked"


# ---------------------------------------------------------------------------
# Test 8: Swing failed-structure skips when trade is too young
# ---------------------------------------------------------------------------

def test_swing_failed_structure_too_young():
    mgr = _make_manager()
    # Only 5 minutes old — below the 15-minute minimum
    pos = _cnc_pos(buy_price=100.0, qty=10, sl=94.0, t1=107.0, t2=118.0, age_minutes=5)
    mgr._positions["TSTOCK"] = pos

    signals = mgr.check_sell_signals({"TSTOCK": {"last_price": 97.0}})
    reasons = [s[1] for s in signals]
    assert SellReason.SWING_FAILED_STRUCTURE not in reasons, \
        "SWING_FAILED_STRUCTURE must not fire before min age"


# ---------------------------------------------------------------------------
# Test 9: Swing failed-structure does NOT fire for intraday (MIS) positions
# ---------------------------------------------------------------------------

def test_swing_failed_structure_not_for_intraday():
    mgr = _make_manager()
    pos = TrackedPosition(
        symbol="MSTOCK", qty=10, buy_price=100.0,
        stop_loss=94.0, target1=107.0, target2=118.0,
        product_type="MIS", holding_horizon="intraday",
    )
    pos.buy_time = _ist_now_str(16)
    mgr._positions["MSTOCK"] = pos

    signals = mgr.check_sell_signals({"MSTOCK": {"last_price": 97.0}})
    reasons = [s[1] for s in signals]
    assert SellReason.SWING_FAILED_STRUCTURE not in reasons, \
        "SWING_FAILED_STRUCTURE must not fire for MIS positions"


# ---------------------------------------------------------------------------
# Test 10: Strong winner trails instead of capping at fixed T2
# ---------------------------------------------------------------------------

def test_strong_winner_uses_trailing_not_fixed_exit():
    mgr = _make_manager()
    # Position with t2 = 118, but price at 112 (between T1 and T2)
    # Trailing stop should be set, position stays open
    pos = _cnc_pos(buy_price=100.0, qty=5, t1=107.0, t2=118.0, age_minutes=120)
    pos.t1_booked = True  # already booked partial
    pos.trailing_stop = 106.0  # stop above entry
    mgr._positions["TSTOCK"] = pos

    signals = mgr.check_sell_signals({"TSTOCK": {"last_price": 112.0}})
    # No T2 hit (112 < 118), no trailing stop hit (112 > 106)
    assert len(signals) == 0, "Strong winner between T1 and T2 with valid trail should stay open"
    # Trailing stop should have moved up from 106
    assert pos.trailing_stop > 106.0, "Trailing stop should advance on new high"


# ---------------------------------------------------------------------------
# Test 11: gtt_needs_reissue is cleared after reissue in audit
# ---------------------------------------------------------------------------

def test_gtt_reissue_clears_flag():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=100.0, qty=5, sl=94.0, t1=107.0, t2=118.0)
    pos.gtt_needs_reissue = True
    mgr._positions["TSTOCK"] = pos

    # Mock the broker-side calls
    mgr._get_broker_exposure = MagicMock(return_value={"TSTOCK": {"qty": 5}})
    mgr.get_gtts = MagicMock(return_value=[])
    mgr.delete_gtt = MagicMock(return_value=True)
    mgr.place_gtt_oco = MagicMock(return_value={
        "success": True, "trigger_id": "GTT-NEW-001", "message": "ok"
    })

    actions = mgr.audit_open_position_gtts()
    reissued = [a for a in actions if a["action"] == "reissued_after_partial"]
    assert len(reissued) == 1, f"Expected one reissued_after_partial action, got: {actions}"
    assert pos.gtt_needs_reissue is False, "Flag must be cleared after successful reissue"
    assert pos.gtt_trigger_id == "GTT-NEW-001", "New trigger ID must be recorded"


# ---------------------------------------------------------------------------
# Test 12: Override logging and stats round-trip
# ---------------------------------------------------------------------------

def test_override_logging_round_trip():
    from pkscreener.classes.LivePortfolio import LivePortfolioStore

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    store = LivePortfolioStore(db_path)
    rid = store.log_override(
        symbol="RELIANCE",
        manual_action="MANUAL_SELL at 2450.00",
        system_recommendation="HOLD (stage=CONFIRMED, trail=2390.00)",
        entry_price=2400.0,
        override_price=2450.0,
    )
    assert rid > 0, "log_override must return a positive row id"

    stats = store.get_override_stats()
    assert stats["total"] == 1
    assert stats["pending"] == 1  # outcome not yet recorded
    assert stats["helped"] == 0

    store.update_override_outcome(rid, outcome_pct=-3.5)  # override hurt (sold before rally)
    stats2 = store.get_override_stats()
    assert stats2["pending"] == 0
    assert stats2["hurt"] == 1
    assert stats2["helped"] == 0
    assert stats2["help_rate_pct"] == 0.0


# ---------------------------------------------------------------------------
# Test 13: Missing tick data does not crash check_sell_signals
# ---------------------------------------------------------------------------

def test_missing_tick_data_does_not_crash():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=100.0, qty=10)
    mgr._positions["TSTOCK"] = pos

    # Empty tick dict — no last_price, no VWAP, no depth
    try:
        signals = mgr.check_sell_signals({"TSTOCK": {}})
    except Exception as exc:
        pytest.fail(f"check_sell_signals crashed on missing tick data: {exc}")
    # Position with no live price is skipped — no signals
    assert all(s[0] != "TSTOCK" for s in signals), \
        "Position with no live price should produce no signals"


def test_load_ticks_prefers_newer_all_universe_tick_over_stale_allstocks(tmp_path):
    mgr = KiteOrderManager.__new__(KiteOrderManager)
    mgr.output_dir = str(tmp_path)
    stale = {
        "JNKINDIA": {
            "last_price": 368.0,
            "received_at": "2026-04-23T10:29:48",
            "symbol": "JNKINDIA",
        }
    }
    fresh = {
        "JNKINDIA": {
            "last_price": 500.0,
            "received_at": datetime.now().isoformat(timespec="seconds"),
            "symbol": "JNKINDIA",
        }
    }
    (tmp_path / "kiteticker_allstocks_ticks_latest.json").write_text(json.dumps(stale))
    (tmp_path / "kiteticker_all_ticks_latest.json").write_text(json.dumps(fresh))

    ticks = mgr._load_ticks()

    assert ticks["JNKINDIA"]["last_price"] == 500.0


def test_check_sell_signals_ignores_stale_stop_loss_tick():
    mgr = _make_manager()
    pos = _cnc_pos(buy_price=496.0, qty=99, sl=471.2, t1=525.76, t2=545.6)
    pos.symbol = "JNKINDIA"
    mgr._positions["JNKINDIA"] = pos
    mgr._broker_last_price = MagicMock(return_value=500.0)

    signals = mgr.check_sell_signals({
        "JNKINDIA": {
            "last_price": 368.0,
            "received_at": "2026-04-23T10:29:48",
        }
    })

    assert signals == []

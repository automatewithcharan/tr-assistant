from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from pkscreener.classes.SignalMonitor import SignalMonitorRunner


def _runner(ticks, cap=1):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.use_live_kite = True
    runner.streaming_mode = True
    runner.actionable_runner = SimpleNamespace(
        live_ticks=ticks, _refresh_live_ticks=lambda: None
    )
    runner.run_at = datetime(2026, 9, 17, 9, 20)
    runner._cycle_number = 0
    runner._previous_live_ticks_by_symbol = {}
    runner._pending_refresh_symbols = {}
    runner._refresh_queue_date = "2026-09-17"
    runner.MAX_CHANGED_SYMBOLS_PER_CYCLE = cap
    runner.FULL_REFRESH_EVERY_CYCLES = 200
    runner._refresh_hot_sectors = lambda _ticks: None
    return runner


def test_unchanged_symbols_waiting_behind_cycle_cap_are_eventually_scanned():
    ticks = {
        "EARLY": {"last_price": 102, "change": 2.0},
        "LATE": {"last_price": 120, "change": 20.0},
        "QUIET": {"last_price": 100, "change": 0.0},
    }
    runner = _runner(ticks)
    symbols = list(ticks)
    empty = pd.DataFrame()

    first = runner._select_symbols_for_cycle(symbols, empty)
    assert first == ["EARLY"]
    assert set(runner._pending_refresh_symbols) == {"LATE", "QUIET"}

    runner._cycle_number = 1
    second = runner._select_symbols_for_cycle(symbols, empty)
    runner._cycle_number = 2
    third = runner._select_symbols_for_cycle(symbols, empty)
    runner._cycle_number = 3
    fourth = runner._select_symbols_for_cycle(symbols, empty)

    assert {second[0], third[0]} == {"LATE", "QUIET"}
    assert fourth == []


def test_new_early_mover_is_prioritized_while_backlog_is_retained():
    ticks = {
        "A": {"last_price": 100, "change": 0.0},
        "B": {"last_price": 100, "change": 0.0},
        "C": {"last_price": 100, "change": 0.0},
    }
    runner = _runner(ticks)
    runner._select_symbols_for_cycle(list(ticks), pd.DataFrame())
    runner._cycle_number = 1
    ticks["NEW"] = {"last_price": 102, "change": 2.0}

    selected = runner._select_symbols_for_cycle(list(ticks), pd.DataFrame())

    assert selected == ["NEW"]
    assert set(runner._pending_refresh_symbols) == {"B", "C"}

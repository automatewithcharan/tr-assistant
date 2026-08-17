from datetime import datetime

import pandas as pd
import pytest
from types import SimpleNamespace

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.SignalMonitor import SignalMonitorRunner


def _runner_for_boundary_test():
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._cycle_number = 0
    runner.universe = "all"
    runner.limit = 0
    runner.streaming_mode = False
    runner.use_live_kite = False
    runner._preload_done = True
    runner.performance_tracker = None
    runner.ALERT_COLUMNS = SignalMonitorRunner.ALERT_COLUMNS
    runner.close_calls = 0
    runner.shadow_outcome_calls = 0
    runner._close_paper_trades_if_eod = lambda: setattr(runner, "close_calls", runner.close_calls + 1)
    runner._evaluate_event_shadow_outcomes_if_eod = lambda: setattr(
        runner, "shadow_outcome_calls", runner.shadow_outcome_calls + 1
    )
    return runner


@pytest.mark.parametrize(
    ("clock", "allowed"),
    [
        ("09:14:59", False),
        ("09:15:00", True),
        ("15:29:59", True),
        ("15:30:00", False),
        ("15:50:00", False),
    ],
)
def test_intraday_candidate_window_is_half_open(monkeypatch, clock, allowed):
    now = datetime.fromisoformat(f"2026-08-17T{clock}")
    monkeypatch.setattr(signal_monitor_module.PKDateUtilities, "currentDateTime", staticmethod(lambda: now))
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "isHoliday",
        staticmethod(lambda _date: (False, "")),
    )

    runner = _runner_for_boundary_test()
    assert runner._is_intraday_candidate_window() is allowed


@pytest.mark.parametrize("clock", ["09:14:59", "15:30:00", "15:50:00"])
def test_candidate_boundary_blocks_all_lanes_but_keeps_post_market_bookkeeping(monkeypatch, clock):
    now = datetime.fromisoformat(f"2026-08-17T{clock}")
    monkeypatch.setattr(signal_monitor_module.PKDateUtilities, "currentDateTime", staticmethod(lambda: now))
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "isHoliday",
        staticmethod(lambda _date: (False, "")),
    )
    runner = _runner_for_boundary_test()

    def unexpected_candidate_generation(*_args, **_kwargs):
        raise AssertionError("candidate/lifecycle generation ran outside the intraday window")

    runner.actionable_runner = SimpleNamespace(get_universe_symbols=lambda: ["ABC"])
    for method in (
        "_build_alerts",
        "_build_early_breakout_alerts",
        "_build_top_gainer_radar_alerts",
        "_record_lifecycle_alerts",
    ):
        monkeypatch.setattr(runner, method, unexpected_candidate_generation)

    monkeypatch.setattr(runner, "_load_previous_state", lambda: pd.DataFrame())
    monkeypatch.setattr(runner, "_load_previous_simple", lambda: pd.DataFrame())
    monkeypatch.setattr(runner, "_run_leader_ignition_lane", lambda: pd.DataFrame())
    monkeypatch.setattr(runner, "_send_leader_ignition_shadow_alerts", lambda frame: None)
    monkeypatch.setattr(runner, "_record_live_feature_snapshot", lambda frame: None)
    monkeypatch.setattr(
        runner,
        "_build_summary_from_simple",
        lambda previous, total, alerts: pd.DataFrame([{"ScannedSymbols": total}]),
    )
    monkeypatch.setattr(runner, "_annotate_summary_timing", lambda summary, started, scanned: summary)
    monkeypatch.setattr(runner, "_save_outputs", lambda *args: None)
    monkeypatch.setattr(runner, "_print_summary", lambda *args: None)

    result = runner.run()

    assert all(isinstance(frame, pd.DataFrame) for frame in result)
    assert all(frame.empty for frame in result[:3])
    assert runner.close_calls == 1
    assert runner.shadow_outcome_calls == 1
    assert runner._cycle_number == 1


def test_candidate_boundary_does_not_change_downstream_inclusive_market_gate(monkeypatch):
    now = datetime.fromisoformat("2026-08-17T15:30:00")
    monkeypatch.setattr(signal_monitor_module.PKDateUtilities, "currentDateTime", staticmethod(lambda: now))
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "isHoliday",
        staticmethod(lambda _date: (False, "")),
    )
    runner = _runner_for_boundary_test()

    assert runner._is_regular_market_hours() is True
    assert runner._is_intraday_candidate_window() is False


def test_candidate_boundary_allows_normal_in_session_cycle(monkeypatch):
    now = datetime.fromisoformat("2026-08-17T09:15:00")
    monkeypatch.setattr(signal_monitor_module.PKDateUtilities, "currentDateTime", staticmethod(lambda: now))
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "isHoliday",
        staticmethod(lambda _date: (False, "")),
    )

    runner = _runner_for_boundary_test()
    assert runner._is_intraday_candidate_window() is True

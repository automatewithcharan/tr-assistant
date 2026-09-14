from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.LiveFeatureRecorder import LiveFeatureRecorder
from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from pkscreener.classes.TopGainerLifecycleRecorder import TopGainerLifecycleRecorder


def _runner(tmp_path, monkeypatch, hour=9, minute=45):
    now = datetime(2026, 9, 7, hour, minute)
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: now),
    )
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    runner.universe = "all"
    runner.run_at = now
    runner.run_at_str = now.strftime("%Y-%m-%d %H:%M:%S")
    runner.enable_early_breakout_alerts = True
    runner._early_breakout_alerted_date = ""
    runner._early_breakout_alerted_today = set()
    runner.goal_stop_pct = 1.0
    runner.goal_target1_pct = 1.5
    runner.goal_target2_pct = 3.0
    runner.use_live_kite = True
    runner._is_regular_market_hours = lambda: True
    runner.enable_live_feature_recorder = True
    runner._live_feature_recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path), interval_seconds=60
    )
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(str(tmp_path))
    runner.enable_experiment3_shadow_context = False
    runner.actionable_runner = SimpleNamespace(live_ticks={"TEST": _tick()})
    return runner


def _tick(**overrides):
    tick = {
        "last_price": 100.0,
        "high": 100.5,
        "volume_traded": 100000,
        "total_buy_quantity": 200000,
        "total_sell_quantity": 100000,
        "depth": {
            "buy": [{"price": 99.95, "quantity": 1000}],
            "sell": [{"price": 100.05, "quantity": 900}],
        },
    }
    tick.update(overrides)
    return tick


def _row(**overrides):
    row = {
        "Stock": "TEST",
        "What To Do": "Buy Now",
        "Momentum Setup": "",
        "Price Now": 100.0,
        "Change %": 2.0,
        "Open Change %": 1.5,
        "Volume vs Avg": 1.5,
        "Signal Strength %": 40.0,
        "RSI": 60.0,
        "Suggested Qty": 10,
        "Week52High": 150.0,
        "Checks": "confidence ok",
        "Why": "price above trend",
    }
    row.update(overrides)
    return row


def _decision(runner):
    path = (
        f"{runner.output_dir}/live_feature_recorder/"
        "lane_decisions_2026-09-07.csv"
    )
    frame = pd.read_csv(path)
    assert list(frame["Lane"].unique()) == ["early_breakout"]
    return frame.iloc[-1]


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"Change %": 0.9}, "change below early-breakout band"),
        ({"Change %": 3.6}, "change above early-breakout band"),
        ({"RSI": 70.0}, "RSI overbought"),
        ({"Week52High": 101.0}, "too close to 52-week high"),
        ({"Suggested Qty": 0}, "quantity below one"),
        ({"What To Do": "Watch for Buy"}, "source action not buy or momentum"),
        ({"Price Now": 0}, "price unavailable or non-positive"),
    ],
)
def test_early_breakout_first_failure_is_journaled(
    tmp_path, monkeypatch, overrides, reason
):
    runner = _runner(tmp_path, monkeypatch)

    alerts = runner._build_early_breakout_alerts(pd.DataFrame([_row(**overrides)]))

    assert alerts.empty
    decision = _decision(runner)
    assert decision["Decision"] == "reject"
    assert decision["Reason"] == reason


@pytest.mark.parametrize("change_pct", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_change_is_rejected_without_lifecycle_or_delivery(
    tmp_path, monkeypatch, change_pct
):
    runner = _runner(tmp_path, monkeypatch)
    runner._send_tradingview_candidate_alerts = lambda *_args, **_kwargs: pytest.fail(
        "Discord delivery called"
    )

    alerts = runner._build_early_breakout_alerts(
        pd.DataFrame([_row(**{"Change %": change_pct})])
    )

    assert alerts.empty
    decision = _decision(runner)
    assert decision["Decision"] == "reject"
    assert decision["Reason"] == "change unavailable or non-finite"
    assert runner._top_gainer_lifecycle_recorder.replay() == []


@pytest.mark.parametrize(
    "change_pct",
    [SignalMonitorRunner.EARLY_BREAKOUT_MIN_PCT, SignalMonitorRunner.EARLY_BREAKOUT_MAX_PCT],
)
def test_change_band_boundaries_remain_inclusive(tmp_path, monkeypatch, change_pct):
    runner = _runner(tmp_path, monkeypatch)

    alerts = runner._build_early_breakout_alerts(
        pd.DataFrame([_row(**{"Change %": change_pct})])
    )

    assert list(alerts["Symbol"]) == ["TEST"]
    assert _decision(runner)["Decision"] == "pass"
    events = runner._top_gainer_lifecycle_recorder.replay()
    assert [event["event_type"] for event in events] == ["candidate_observed"]


@pytest.mark.parametrize(
    "hour,minute,strength,reason",
    [
        (9, 45, 31.9, "strength below opening-window floor"),
        (10, 30, 37.9, "strength below normal-session floor"),
    ],
)
def test_early_breakout_strength_floor_reason_tracks_session(
    tmp_path, monkeypatch, hour, minute, strength, reason
):
    runner = _runner(tmp_path, monkeypatch, hour=hour, minute=minute)

    alerts = runner._build_early_breakout_alerts(
        pd.DataFrame([_row(**{"Signal Strength %": strength})])
    )

    assert alerts.empty
    assert _decision(runner)["Reason"] == reason


def test_early_breakout_records_only_the_first_failed_gate(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)

    alerts = runner._build_early_breakout_alerts(
        pd.DataFrame(
            [_row(**{"Signal Strength %": 25.0, "RSI": 76.0, "Volume vs Avg": 0.5})]
        )
    )

    assert alerts.empty
    journal = pd.read_csv(
        f"{runner.output_dir}/live_feature_recorder/lane_decisions_2026-09-07.csv"
    )
    assert len(journal) == 1
    assert journal.iloc[0]["Reason"] == "strength below opening-window floor"


def test_low_volume_and_buy_sell_confirmation_is_journaled(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    runner.actionable_runner.live_ticks["TEST"] = _tick(
        total_buy_quantity=100000, total_sell_quantity=100000
    )

    alerts = runner._build_early_breakout_alerts(
        pd.DataFrame([_row(**{"Volume vs Avg": 1.2})])
    )

    assert alerts.empty
    assert _decision(runner)["Reason"] == "volume and buyer/seller confirmation below floor"


def test_buy_sell_confirmation_preserves_existing_volume_or_rule(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    runner.actionable_runner.live_ticks["TEST"] = _tick(
        total_buy_quantity=150000, total_sell_quantity=100000
    )

    alerts = runner._build_early_breakout_alerts(
        pd.DataFrame([_row(**{"Volume vs Avg": 1.2})])
    )

    assert list(alerts["Symbol"]) == ["TEST"]
    assert _decision(runner)["Decision"] == "pass"


def test_pullback_rejection_is_journaled(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    runner.actionable_runner.live_ticks["TEST"] = _tick(high=102.0)

    alerts = runner._build_early_breakout_alerts(pd.DataFrame([_row()]))

    assert alerts.empty
    decision = _decision(runner)
    assert decision["Reason"] == "pulled back too far from day high"
    assert decision["PullbackPct"] == pytest.approx(2.0)


def test_valid_candidate_keeps_alert_and_lifecycle_and_adds_pass_row(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)

    alerts = runner._build_early_breakout_alerts(pd.DataFrame([_row()]))

    assert list(alerts["Symbol"]) == ["TEST"]
    assert alerts.iloc[0]["NowAction"] == "🌱 Early Breakout"
    observation_id = alerts.iloc[0]["LifecycleObservationId"]
    lifecycle = runner._top_gainer_lifecycle_recorder.replay()
    assert [row["lifecycle_id"] for row in lifecycle] == [observation_id]
    decision = _decision(runner)
    assert decision["Decision"] == "pass"
    assert decision["Reason"] == "early breakout qualification passed"


def test_lane_recorder_failure_is_non_blocking_for_valid_candidate(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)

    class FailingRecorder:
        def record_decisions(self, *args, **kwargs):
            raise OSError("journal unavailable")

    runner._live_feature_recorder = FailingRecorder()

    alerts = runner._build_early_breakout_alerts(pd.DataFrame([_row()]))

    assert list(alerts["Symbol"]) == ["TEST"]
    assert alerts.iloc[0]["LifecycleObservationId"]


def test_rejection_telemetry_has_no_delivery_paper_or_broker_side_effect(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path, monkeypatch)
    runner._send_discord_alert = lambda *a, **k: pytest.fail("Discord called")
    runner._run_ranked_confirmation_paper_lane = lambda *a, **k: pytest.fail("paper called")
    runner._run_autotrade_lane = lambda *a, **k: pytest.fail("broker lane called")

    alerts = runner._build_early_breakout_alerts(
        pd.DataFrame([_row(**{"Change %": 0.5})])
    )

    assert alerts.empty
    assert _decision(runner)["Decision"] == "reject"

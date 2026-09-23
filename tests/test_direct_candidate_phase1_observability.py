from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from pkscreener.classes.TopGainerLifecycleRecorder import TopGainerLifecycleRecorder
from pkscreener.classes.TradingViewCandidateDelivery import load_delivery_records


IST = ZoneInfo("Asia/Kolkata")


def _time(second: int, microsecond: int = 0) -> datetime:
    return datetime(2026, 9, 22, 9, 30, second, microsecond, tzinfo=IST)


def _runner(tmp_path) -> SignalMonitorRunner:
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    runner.universe = "all"
    runner.run_at = _time(0)
    runner.run_at_str = "2026-09-22 09:30:00"
    runner.alert_channels = set()
    runner.enable_early_breakout_alerts = True
    runner._early_breakout_alerted_date = "2026-09-22"
    runner._early_breakout_alerted_today = set()
    runner._top_gainer_radar_date = "2026-09-22"
    runner._top_gainer_radar_today = set()
    runner._direct_causal_by_symbol = {}
    runner._direct_lane_timing_states = {}
    runner._direct_candidate_lanes_by_symbol = {}
    runner._direct_candidate_lanes_date = "2026-09-22"
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(str(tmp_path))
    runner.enable_live_feature_recorder = False
    runner._live_feature_recorder = None
    runner.use_live_kite = True
    runner.streaming_mode = True
    runner.min_trade_value = 50_000_000.0
    runner.budget_amount = 50_000.0
    runner.goal_stop_pct = 1.0
    runner.goal_target1_pct = 1.5
    runner.goal_target2_pct = 3.0
    runner._record_lane_decisions = lambda *_args, **_kwargs: None
    runner._pending_refresh_symbols = {}
    runner._refresh_queue_date = "2026-09-22"
    runner._previous_live_ticks_by_symbol = {}
    runner._cycle_number = 0
    runner.MAX_CHANGED_SYMBOLS_PER_CYCLE = 1
    runner.FULL_REFRESH_EVERY_CYCLES = 200
    runner._refresh_hot_sectors = lambda _ticks: None
    runner.actionable_runner = SimpleNamespace(live_ticks={}, _refresh_live_ticks=lambda: None)
    return runner


def _simple_row(symbol="CAUSAL", **overrides):
    row = {
        "Stock": symbol,
        "What To Do": "Buy Now",
        "Momentum Setup": "",
        "Price Now": 101.0,
        "Change %": 2.0,
        "Volume vs Avg": 1.6,
        "Signal Strength %": 45.0,
        "RSI": 60.0,
        "Entry Gap %": 0.1,
        "Buy Above": 101.0,
        "Sell If Below": 99.99,
        "Take Profit 1": 102.52,
        "Take Profit 2": 104.03,
        "Suggested Qty": 10,
        "Avg Daily Value": "8.50 Cr",
        "Traded Value": 85_000_000.0,
        "VWAP Distance": 0.35,
        "Market Breadth %": 1.2,
        "Sector Avg Change %": 1.8,
        "Checks": "confidence ok, low volume",
        "Why": "Price above ATR trailing stop",
        "Week52High": 150.0,
    }
    row.update(overrides)
    return row


def _tick(price=101.0):
    return {
        "last_price": price,
        "change": 2.0,
        "high": 101.5,
        "volume_traded": 150_000,
        "total_buy_quantity": 200_000,
        "total_sell_quantity": 100_000,
        "exchange_timestamp": "2026-09-22T09:30:00.500000+05:30",
        "received_at": "2026-09-22T09:30:00.700000+05:30",
    }


def test_thresholds_and_candidate_population_remain_unchanged(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner.actionable_runner.live_ticks = {"PASS": _tick(), "REJECT": _tick()}
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: _time(5)),
    )
    runner._causal_clock = lambda: _time(5)

    alerts = runner._build_early_breakout_alerts(
        pd.DataFrame(
            [
                _simple_row("PASS"),
                _simple_row("REJECT", **{"Change %": 0.99}),
            ]
        )
    )

    assert runner.EARLY_BREAKOUT_MIN_PCT == 1.0
    assert runner.EARLY_BREAKOUT_MAX_PCT == 3.5
    assert runner.EARLY_BREAKOUT_MIN_VOLUME_RATIO == 1.3
    assert runner.TOP_GAINER_RADAR_MIN_PCT == 1.5
    assert runner.TOP_GAINER_RADAR_MAX_PCT == 3.5
    assert list(alerts["Symbol"]) == ["PASS"]


def test_stage_timestamps_use_actual_boundaries_and_preserve_tick_times(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner.actionable_runner.live_ticks = {"CAUSAL": _tick()}
    queue_times = iter([_time(1), _time(2)])
    runner._causal_clock = lambda: next(queue_times)

    selected = runner._select_symbols_for_cycle(["CAUSAL"], pd.DataFrame())
    simple = runner._annotate_analysis_completed(
        pd.DataFrame([_simple_row()]), selected, _time(4)
    )
    runner._causal_clock = lambda: _time(5)
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: _time(5)),
    )

    alerts = runner._build_early_breakout_alerts(simple)
    row = alerts.iloc[0]

    assert row["FirstEligibleTime"] == _time(1).isoformat(timespec="microseconds")
    assert row["QueueSelectedTime"] == _time(2).isoformat(timespec="microseconds")
    assert row["AnalysisCompletedTime"] == _time(4).isoformat(timespec="microseconds")
    assert row["QualifiedTime"] == _time(5).isoformat(timespec="microseconds")
    assert row["QualifiedTime"] != runner.run_at.isoformat(timespec="microseconds")
    assert row["QualifiedPrice"] == 101.0
    assert row["SourceExchangeTimestamp"] == "2026-09-22T09:30:00.500000+05:30"
    assert row["TickTimestamp"] == "2026-09-22T09:30:00.700000+05:30"

    observation = runner._top_gainer_lifecycle_recorder.replay()[0]
    fields = observation["signal_time_fields"]
    assert fields["first_eligible_time"] == row["FirstEligibleTime"]
    assert fields["qualified_time"] == row["QualifiedTime"]
    assert fields["qualified_price"] == 101.0


def test_delivery_snapshots_send_price_and_writes_all_timing_deltas(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    runner.alert_channels = {"discord"}
    runner._tradingview_delivery_available = True
    runner._manual_discord_row_is_eligible = lambda *_args, **_kwargs: True
    runner.tradingview_discord_max_attempts = 1
    runner.tradingview_discord_retry_backoff_seconds = 0
    runner.tradingview_candidate_retry_ttl_seconds = 600
    runner.tradingview_candidate_max_total_attempts = 1
    runner.actionable_runner.live_ticks = {"CAUSAL": _tick(price=102.0)}
    delivery_times = iter([_time(5, 500_000), _time(6), _time(6, 400_000)])
    runner._tradingview_delivery_now = lambda: next(delivery_times)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    posts = []
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *_args, **kwargs: posts.append(kwargs["json"]["content"])
        or SimpleNamespace(status_code=204, text="", headers={}),
    )
    candidate = pd.DataFrame(
        [
            {
                "Lane": "early_breakout",
                "LifecycleObservationId": "phase1-observation",
                "LifecycleTimestamp": _time(5).isoformat(timespec="microseconds"),
                "QualifiedTime": _time(5).isoformat(timespec="microseconds"),
                "QualifiedPrice": 101.0,
                "FirstEligibleTime": _time(1).isoformat(timespec="microseconds"),
                "FirstEligiblePrice": 100.0,
                "QueueSelectedTime": _time(2).isoformat(timespec="microseconds"),
                "QueueSelectedPrice": 100.25,
                "AnalysisCompletedTime": _time(4).isoformat(timespec="microseconds"),
                "SourceExchangeTimestamp": "2026-09-22T09:30:00.500000+05:30",
                "TickTimestamp": "2026-09-22T09:30:00.700000+05:30",
                "Symbol": "CAUSAL",
                "PriceNow": 101.0,
                "ChangePct": 2.0,
                "SignalStrengthPct": 45.0,
                "RSI": 60.0,
                "VolumeRatio": 1.6,
                "TradedValue": 85_000_000.0,
                "BuyerSellerRatio": 2.0,
                "PullbackPct": 0.5,
                "VWAPDistance": 0.35,
                "BenchmarkStrength": 1.2,
                "SectorStrength": 1.8,
                "WarningFlags": "low volume",
                "Checks": "low volume | vol 1.6x | B/S 2.0x",
                "Why": "qualified",
            }
        ]
    )

    runner._send_tradingview_candidate_alerts(candidate)

    delivered = load_delivery_records(tmp_path, "2026-09-22")[-1]
    assert delivered["discord_attempt_price"] == 102.0
    assert delivered["first_seen_to_qualified_seconds"] == 4.0
    assert delivered["qualified_to_discord_attempt_seconds"] == 1.0
    assert delivered["discord_attempt_to_success_seconds"] == 0.4
    assert delivered["discord_success_at"] == _time(6, 400_000).isoformat(timespec="microseconds")
    assert "first seen" in posts[0]
    assert "send" in posts[0]
    assert "₹102.00" in posts[0]
    assert "RVOL `1.60x`" in posts[0]
    assert "Warnings: low volume" in posts[0]


def test_cross_lane_candidate_is_annotated_but_both_are_delivered(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner.alert_channels = {"discord"}
    runner._tradingview_delivery_available = True
    runner._manual_discord_row_is_eligible = lambda *_args, **_kwargs: True
    runner.tradingview_discord_max_attempts = 1
    runner.tradingview_discord_retry_backoff_seconds = 0
    runner.tradingview_candidate_retry_ttl_seconds = 600
    runner.tradingview_candidate_max_total_attempts = 1
    runner.actionable_runner.live_ticks = {"BOTH": _tick()}
    runner._tradingview_delivery_now = lambda: _time(6)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    posts = []
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *_args, **kwargs: posts.append(kwargs["json"]["content"])
        or SimpleNamespace(status_code=204, text="", headers={}),
    )

    def frame(lane, observation):
        return pd.DataFrame(
            [{
                "Lane": lane,
                "LifecycleObservationId": observation,
                "LifecycleTimestamp": _time(5).isoformat(timespec="microseconds"),
                "QualifiedTime": _time(5).isoformat(timespec="microseconds"),
                "QualifiedPrice": 101.0,
                "Symbol": "BOTH",
                "PriceNow": 101.0,
                "ChangePct": 2.0,
                "SignalStrengthPct": 45.0,
                "Checks": "vol 1.6x",
                "Why": "qualified",
            }]
        )

    runner._send_tradingview_candidate_alerts(frame("early_breakout", "obs-early"))
    runner._send_tradingview_candidate_alerts(frame("top_gainer_radar", "obs-radar"))

    assert len(posts) == 2
    assert "MULTI-LANE" not in posts[0]
    assert "MULTI-LANE" in posts[1]
    delivered = [
        row for row in load_delivery_records(tmp_path, "2026-09-22")
        if row["delivery_status"] == "delivered"
    ]
    assert len(delivered) == 2
    assert delivered[1]["multi_lane"] is True
    assert delivered[1]["multi_lane_sources"] == "early_breakout,top_gainer_radar"


def test_payload_enrichment_failure_and_missing_optional_fields_are_fail_open(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    minimal = {"Symbol": "MINIMAL", "Lane": "early_breakout", "PriceNow": 100.0}
    message = runner._build_tradingview_candidate_message(minimal)
    assert "MINIMAL" in message
    assert "Not an approved trade" in message

    runner.alert_channels = {"discord"}
    runner._tradingview_delivery_available = True
    runner._manual_discord_row_is_eligible = lambda *_args, **_kwargs: True
    runner.tradingview_discord_max_attempts = 1
    runner.tradingview_discord_retry_backoff_seconds = 0
    runner.tradingview_candidate_retry_ttl_seconds = 600
    runner.tradingview_candidate_max_total_attempts = 1
    runner._tradingview_delivery_now = lambda: _time(6)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    posts = []
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *_args, **kwargs: posts.append(kwargs["json"]["content"])
        or SimpleNamespace(status_code=204, text="", headers={}),
    )
    monkeypatch.setattr(
        runner,
        "_build_tradingview_candidate_message",
        lambda _row: (_ for _ in ()).throw(ValueError("optional formatting failed")),
    )
    candidate = pd.DataFrame(
        [{
            **minimal,
            "LifecycleObservationId": "minimal-observation",
            "LifecycleTimestamp": _time(5).isoformat(timespec="microseconds"),
        }]
    )

    runner._send_tradingview_candidate_alerts(candidate)

    assert len(posts) == 1
    assert "MINIMAL" in posts[0]
    assert load_delivery_records(tmp_path, "2026-09-22")[-1]["delivery_status"] == "delivered"


def test_direct_timing_evidence_is_one_row_per_decision_transition(tmp_path):
    runner = _runner(tmp_path)
    timing_events = []
    row = {"Symbol": "TRANSITION", "PriceNow": 100.0}

    runner._append_direct_transition_timing_event(
        "early_breakout", row, "reject", "below band", timing_events
    )
    runner._append_direct_transition_timing_event(
        "early_breakout", row, "reject", "below band", timing_events
    )
    runner._append_direct_transition_timing_event(
        "early_breakout", row, "pass", "qualified", timing_events
    )

    assert [event["event_type"] for event in timing_events] == [
        "SOURCE_REJECTED",
        "SOURCE_QUALIFIED",
    ]

import inspect
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from pkscreener.classes.TopGainerLifecycleRecorder import TopGainerLifecycleRecorder
from pkscreener.classes.TradingViewCandidateDelivery import (
    load_delivery_records,
    reconcile_tradingview_candidates,
)


def _runner(output_dir):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(output_dir)
    runner.universe = "all"
    runner.run_at = datetime(2026, 9, 7, 10, 15)
    runner.run_at_str = "2026-09-07 10:15:00"
    runner.alert_channels = {"discord"}
    runner.tradingview_discord_max_attempts = 3
    runner.tradingview_discord_retry_backoff_seconds = 0
    runner.tradingview_candidate_retry_ttl_seconds = 600
    runner.tradingview_candidate_max_total_attempts = 6
    runner._tradingview_delivery_now = lambda: datetime(
        2026, 9, 7, 10, 16, tzinfo=ZoneInfo("Asia/Kolkata")
    )
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(str(output_dir))
    runner.enable_early_breakout_alerts = True
    runner._early_breakout_alerted_date = "2026-09-07"
    runner._early_breakout_alerted_today = set()
    runner.enable_live_feature_recorder = False
    runner._live_feature_recorder = None
    runner.use_live_kite = False
    runner.actionable_runner = SimpleNamespace(live_ticks={})
    return runner


def _direct_frame(runner, lane, symbols):
    rows = []
    for symbol in symbols:
        observation_id = runner._record_direct_lane_observation(
            lane,
            {
                "symbol": symbol,
                "feature_available_at": runner.run_at,
                "current_price": 100.0,
                "return_from_previous_close": 2.0,
            },
            f"{lane} qualification passed",
            observation_timestamp=runner.run_at,
        )
        rows.append(
            {
                "Lane": lane,
                "LifecycleObservationId": observation_id,
                "LifecycleTimestamp": runner.run_at.isoformat(timespec="microseconds"),
                "When": runner.run_at_str,
                "Symbol": symbol,
                "PreviousAction": "Hold",
                "NowAction": "🌱 Early Breakout" if lane == "early_breakout" else "📡 Top Gainer Radar",
                "PriceNow": 100.0,
                "ChangePct": 2.0,
                "EntryGapPct": 0.2,
                "RSI": 60.0,
                "BuyAbove": 100.0,
                "SellIfBelow": 99.0,
                "TakeProfit1": 101.5,
                "TakeProfit2": 103.0,
                "SuggestedQty": 10,
                "SignalStrengthPct": 50.0,
                "Checks": "vol 2.0x | B/S 2.0x",
                "Why": "qualified",
            }
        )
    return pd.DataFrame(rows)


def _core_row(symbol):
    return {
        "When": "2026-09-07 10:15:00",
        "Symbol": symbol,
        "PreviousAction": "Hold",
        "NowAction": "Buy Now",
        "PriceNow": 100.0,
        "ChangePct": 2.0,
        "EntryGapPct": 0.2,
        "RSI": 60.0,
        "BuyAbove": 100.0,
        "SellIfBelow": 99.0,
        "TakeProfit1": 101.5,
        "TakeProfit2": 103.0,
        "SuggestedQty": 10,
        "SignalStrengthPct": 50.0,
        "Checks": "core",
        "Why": "core candidate",
    }


def _simulate(output_dir, deliver):
    runner = _runner(output_dir)
    rejected = runner._build_early_breakout_alerts(
        pd.DataFrame(
            [
                {
                    "Stock": "ORDINARY_REJECT",
                    "What To Do": "Buy Now",
                    "Price Now": 100.0,
                    "Change %": 0.5,
                }
            ]
        )
    )
    assert rejected.empty
    early = _direct_frame(runner, "early_breakout", ["EARLY_KEEP", "EARLY_DROP"])
    radar = _direct_frame(runner, "top_gainer_radar", ["RADAR_KEEP", "RADAR_DROP"])
    direct_ids = {
        row.Symbol: row.LifecycleObservationId
        for row in pd.concat([early, radar]).itertuples()
    }

    if deliver:
        runner._send_tradingview_candidate_alerts(early)
        runner._send_tradingview_candidate_alerts(radar)

    alerts = pd.DataFrame([_core_row("EARLY_DROP"), _core_row("RADAR_DROP")])
    for direct in (early, radar):
        existing = set(alerts["Symbol"].astype(str).str.upper())
        surviving = direct[
            ~direct["Symbol"].astype(str).str.upper().isin(existing)
        ]
        alerts = pd.concat([alerts, surviving], ignore_index=True)
    runner._record_lifecycle_alerts(alerts)
    return runner._top_gainer_lifecycle_recorder.replay(), direct_ids


def test_delivery_preserves_canonical_lifecycle_and_same_symbol_aggregation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *args, **kwargs: type(
            "Response", (), {"status_code": 204, "text": "", "headers": {}}
        )(),
    )

    baseline_events, baseline_ids = _simulate(tmp_path / "baseline", deliver=False)
    patched_events, patched_ids = _simulate(tmp_path / "patched", deliver=True)

    assert patched_events == baseline_events
    assert patched_ids == baseline_ids
    assert [event["event_type"] for event in patched_events].count("candidate_observed") == 6
    assert [event["event_type"] for event in patched_events].count("alert_emitted") == 4

    emitted_ids = {
        event.get("observation_id")
        for event in patched_events
        if event.get("event_type") == "alert_emitted"
    }
    assert patched_ids["EARLY_KEEP"] in emitted_ids
    assert patched_ids["RADAR_KEEP"] in emitted_ids
    assert patched_ids["EARLY_DROP"] not in emitted_ids
    assert patched_ids["RADAR_DROP"] not in emitted_ids

    deliveries = load_delivery_records(tmp_path / "patched", "2026-09-07")
    assert {
        row["lifecycle_observation_id"]
        for row in deliveries
        if row["delivery_status"] == "delivered"
    } == set(patched_ids.values())

    baseline_report = reconcile_tradingview_candidates(
        tmp_path / "baseline",
        "2026-09-07",
        now=datetime(2026, 9, 7, 10, 16, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    patched_report = reconcile_tradingview_candidates(
        tmp_path / "patched",
        "2026-09-07",
        now=datetime(2026, 9, 7, 10, 16, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    assert baseline_report["lanes"]["early_breakout"]["detected"] == 2
    assert baseline_report["lanes"]["early_breakout"]["missing"] == 2
    assert baseline_report["lanes"]["top_gainer_radar"]["detected"] == 2
    assert baseline_report["lanes"]["top_gainer_radar"]["missing"] == 2
    assert patched_report["lanes"]["early_breakout"]["delivered"] == 2
    assert patched_report["lanes"]["top_gainer_radar"]["delivered"] == 2


def test_run_has_no_preaggregation_canonical_direct_lane_recording():
    source = inspect.getsource(SignalMonitorRunner.run)
    assert "_record_lifecycle_alerts(early_breakout_alerts_df)" not in source
    assert "_record_lifecycle_alerts(top_gainer_radar_df)" not in source
    assert source.count("_record_lifecycle_alerts(alerts_df)") == 1


def test_nan_change_matches_baseline_with_no_lifecycle_alert_or_delivery(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    runner.actionable_runner.live_ticks = {
        "NAN_CASE": {
            "last_price": 100.0,
            "high": 100.5,
            "total_buy_quantity": 200000,
            "total_sell_quantity": 100000,
        }
    }
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: runner.run_at),
    )
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *args, **kwargs: posts.append(kwargs["json"]),
    )
    change_pct = float("nan")
    baseline_in_change_band = (
        runner.EARLY_BREAKOUT_MIN_PCT
        <= change_pct
        <= runner.EARLY_BREAKOUT_MAX_PCT
    )
    baseline_alerts = pd.DataFrame(columns=runner.ALERT_COLUMNS)
    frame = pd.DataFrame(
        [
            {
                "Stock": "NAN_CASE",
                "What To Do": "Buy Now",
                "Price Now": 100.0,
                "Change %": change_pct,
                "Signal Strength %": 50.0,
                "RSI": 60.0,
                "Volume vs Avg": 1.5,
                "Suggested Qty": 10,
                "Week52High": 150.0,
            }
        ]
    )

    patched_alerts = runner._build_early_breakout_alerts(frame)

    assert baseline_in_change_band is False
    pd.testing.assert_frame_equal(patched_alerts, baseline_alerts)
    events = runner._top_gainer_lifecycle_recorder.replay()
    assert [event["event_type"] for event in events] == []
    assert posts == []
    assert load_delivery_records(tmp_path, "2026-09-07") == []


def test_qualified_observations_receive_delivery_state_before_cycle_cap(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    decision_rows = []
    runner._record_lane_decisions = (
        lambda lane, rows: decision_rows.extend(dict(row) for row in rows)
    )
    runner.EARLY_BREAKOUT_MAX_PER_CYCLE = 1
    runner.actionable_runner.live_ticks = {
        symbol: {
            "last_price": 100.0,
            "high": 100.5,
            "total_buy_quantity": 200000,
            "total_sell_quantity": 100000,
        }
        for symbol in ("CAP_A", "CAP_B")
    }
    monkeypatch.setattr(
        signal_monitor_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: runner.run_at),
    )
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *args, **kwargs: type(
            "Response", (), {"status_code": 204, "text": "", "headers": {}}
        )(),
    )
    frame = pd.DataFrame(
        [
            {
                "Stock": symbol,
                "What To Do": "Buy Now",
                "Price Now": 100.0,
                "Change %": 2.0,
                "Signal Strength %": strength,
                "RSI": 60.0,
                "Volume vs Avg": 1.5,
                "Suggested Qty": 10,
                "Week52High": 150.0,
            }
            for symbol, strength in (("CAP_A", 50.0), ("CAP_B", 45.0))
        ]
    )

    alerts = runner._build_early_breakout_alerts(frame)

    assert len(alerts) == 1
    observations = [
        event
        for event in runner._top_gainer_lifecycle_recorder.replay()
        if event["event_type"] == "candidate_observed"
    ]
    assert len(observations) == 2
    assert not any(
        event["event_type"] == "alert_emitted"
        for event in runner._top_gainer_lifecycle_recorder.replay()
    )
    deliveries = load_delivery_records(tmp_path, "2026-09-07")
    assert sum(row["delivery_status"] == "delivered" for row in deliveries) == 2
    qualified = [row for row in decision_rows if row.get("Decision") == "pass"]
    assert len(qualified) == 2
    assert {row["Reason"] for row in qualified} == {
        "early breakout qualification passed"
    }
    assert not any(
        row.get("Reason") == "cycle early-breakout slot not selected"
        for row in decision_rows
    )

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import inspect
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.DiscordShadowRanking import (
    SCHEMA_VERSION,
    append_shadow_priority_record,
    calculate_shadow_priority,
    load_shadow_priority_records,
)
from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from pkscreener.classes.TradingViewCandidateDelivery import load_delivery_records
from scripts.evaluate_discord_shadow_ranking import attach_future_outcomes, summarize_group


IST = ZoneInfo("Asia/Kolkata")


def ts(second: int) -> datetime:
    return datetime(2026, 9, 22, 9, 30, tzinfo=IST) + timedelta(seconds=second)


def candidate(**overrides):
    row = {
        "Symbol": "RANKED",
        "Lane": "early_breakout",
        "LifecycleObservationId": "candidate-1",
        "FirstEligibleTime": ts(1).isoformat(),
        "FirstEligiblePrice": 100.0,
        "QueueSelectedTime": ts(2).isoformat(),
        "QueueSelectedPrice": 100.1,
        "AnalysisCompletedTime": ts(3).isoformat(),
        "QualifiedTime": ts(5).isoformat(),
        "QualifiedPrice": 100.2,
        "DiscordAttemptTime": ts(6).isoformat(),
        "DiscordAttemptPrice": 100.3,
        "SourceExchangeTimestamp": ts(5).isoformat(),
        "TickTimestamp": ts(5).isoformat(),
        "TradedValue": 80_000_000.0,
        "AvgDailyValue": "12.0 Cr",
        "SpreadBps": 4.0,
        "VolumeRatio": 3.0,
        "BuyerSellerRatio": 2.2,
        "NiftyRelativeStrength": 0.8,
        "BenchmarkStrength": 1.0,
        "SectorStrength": 0.5,
        "PullbackPct": 0.4,
        "VWAPDistance": 0.6,
        "ChangePct": 2.0,
        "Distance52WeekHighPct": 8.0,
        "SignalStrengthPct": 60.0,
        "MultiLane": True,
        "WarningFlags": "",
        "Checks": "qualified",
        "Why": "causal direct candidate",
    }
    row.update(overrides)
    return row


def runner(tmp_path) -> SignalMonitorRunner:
    value = SignalMonitorRunner.__new__(SignalMonitorRunner)
    value.output_dir = str(tmp_path)
    value.run_at = ts(0)
    value.run_at_str = ts(0).isoformat()
    value.alert_channels = {"discord"}
    value._tradingview_delivery_available = True
    value._manual_discord_row_is_eligible = lambda *_args, **_kwargs: True
    value.tradingview_discord_max_attempts = 1
    value.tradingview_discord_retry_backoff_seconds = 0
    value.tradingview_candidate_retry_ttl_seconds = 600
    value.tradingview_candidate_max_total_attempts = 1
    value._direct_candidate_lanes_by_symbol = {}
    value._direct_candidate_lanes_date = "2026-09-22"
    value.actionable_runner = SimpleNamespace(
        live_ticks={
            "RANKED": {
                "last_price": 100.3,
                "exchange_timestamp": ts(5).isoformat(),
                "received_at": ts(5).isoformat(),
            }
        }
    )
    value._tradingview_delivery_now = iter([ts(6), ts(6), ts(7)]).__next__
    return value


def test_priority_components_are_transparent_and_provisional() -> None:
    high = calculate_shadow_priority(candidate(), ts(6))
    medium = calculate_shadow_priority(
        candidate(
            MultiLane=False, VolumeRatio=1.6, BuyerSellerRatio=1.3,
            NiftyRelativeStrength=None, BenchmarkStrength=None, SectorStrength=None,
            PullbackPct=None, VWAPDistance=None, SignalStrengthPct=45.0,
            TradedValue=20_000_000.0, AvgDailyValue=None,
        ),
        ts(6),
    )
    low = calculate_shadow_priority(
        candidate(
            MultiLane=False, TradedValue=2_000_000.0, AvgDailyValue=None,
            VolumeRatio=0.8, BuyerSellerRatio=0.7, NiftyRelativeStrength=-1.0,
            BenchmarkStrength=-1.0, SectorStrength=-0.5, PullbackPct=2.0,
            VWAPDistance=2.8, SignalStrengthPct=35.0,
            WarningFlags="low liquidity, low volume, stale tick",
            SourceExchangeTimestamp=ts(0).isoformat(),
        ),
        ts(30),
    )

    assert [high["priority"], medium["priority"], low["priority"]] == ["A", "B", "C"]
    assert high["hypothesis_only"] is True
    assert high["priority_score"] == sum(high["priority_components"].values())
    assert high["schema_version"] == SCHEMA_VERSION
    assert low["priority_components"]["liquidity"] == -2
    assert low["priority_components"]["rvol"] == -2


def test_missing_values_remain_missing_and_future_fields_are_never_read() -> None:
    minimal = {
        "Symbol": "MISSING", "Lane": "top_gainer_radar",
        "LifecycleObservationId": "missing-1", "QualifiedTime": ts(5).isoformat(),
        "QualifiedPrice": 100.0, "DiscordAttemptTime": ts(6).isoformat(),
        "DiscordAttemptPrice": 100.0,
    }
    baseline = calculate_shadow_priority(minimal, ts(6))
    contaminated = calculate_shadow_priority(
        {**minimal, "FutureHigh": 99999, "MFE30": 900, "PineBuyOutcome": True}, ts(6)
    )
    assert baseline == contaminated
    assert baseline["features"]["rvol"] is None
    assert "rvol" in baseline["missing_fields"]
    assert "missing_data" in baseline["warnings"]


def test_priority_calculation_does_not_mutate_or_filter_candidate_population() -> None:
    rows = [candidate(Symbol="ONE"), candidate(Symbol="TWO", LifecycleObservationId="candidate-2")]
    original = deepcopy(rows)
    ranked = [calculate_shadow_priority(row, ts(6)) for row in rows]
    assert rows == original
    assert [record["symbol"] for record in ranked] == ["ONE", "TWO"]
    assert len(ranked) == len(rows)


def test_message_keeps_existing_content_and_labels_priority_as_shadow(tmp_path) -> None:
    value = runner(tmp_path)
    for expected, overrides in (
        ("A", {}),
        ("B", {"MultiLane": False, "VolumeRatio": 1.6, "BuyerSellerRatio": 1.3, "NiftyRelativeStrength": None, "BenchmarkStrength": None, "SectorStrength": None, "PullbackPct": None, "VWAPDistance": None, "SignalStrengthPct": 45.0, "TradedValue": 20_000_000.0, "AvgDailyValue": None}),
        ("C", {"MultiLane": False, "TradedValue": 2_000_000.0, "AvgDailyValue": None, "VolumeRatio": 0.8, "BuyerSellerRatio": 0.7, "NiftyRelativeStrength": -1.0, "BenchmarkStrength": -1.0, "SectorStrength": -0.5, "PullbackPct": 2.0, "VWAPDistance": 2.8, "SignalStrengthPct": 35.0, "WarningFlags": "low liquidity, low volume, stale tick", "SourceExchangeTimestamp": ts(0).isoformat()}),
    ):
        row = candidate(**overrides)
        row["ShadowRanking"] = calculate_shadow_priority(row, ts(6))
        message = value._build_tradingview_candidate_message(row)
        assert f"REVIEW PRIORITY: {expected} [SHADOW]" in message
        assert "Manual TradingView review required. Not an approved trade." in message
        assert "HIGH-CONFIDENCE TRADE" not in message
        assert "STRONG BUY" not in message


def test_persistence_is_append_only_new_schema(tmp_path) -> None:
    record = calculate_shadow_priority(candidate(), ts(6))
    assert append_shadow_priority_record(tmp_path, record)
    path = tmp_path / "research" / "discord_shadow_ranking" / "2026-09-22.jsonl"
    assert path.exists()
    loaded = load_shadow_priority_records(path)
    assert len(loaded) == 1
    assert loaded[0]["candidate_id"] == "candidate-1"
    assert loaded[0]["qualified_at"] == ts(5).isoformat()
    assert loaded[0]["discord_attempt_price"] == 100.3


def test_ranking_and_persistence_failures_cannot_suppress_delivery(tmp_path, monkeypatch) -> None:
    for failure_target in ("calculate", "persist"):
        case_dir = tmp_path / failure_target
        value = runner(case_dir)
        posts = []
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
        monkeypatch.setattr(
            signal_monitor_module.requests,
            "post",
            lambda *_args, **kwargs: posts.append(kwargs["json"]["content"])
            or SimpleNamespace(status_code=204, text="", headers={}),
        )
        if failure_target == "calculate":
            monkeypatch.setattr(
                signal_monitor_module,
                "calculate_shadow_priority",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("research failed")),
            )
        else:
            monkeypatch.setattr(signal_monitor_module, "calculate_shadow_priority", calculate_shadow_priority)
            monkeypatch.setattr(
                signal_monitor_module,
                "append_shadow_priority_record",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk failed")),
            )
        value._send_tradingview_candidate_alerts(pd.DataFrame([candidate()]))
        assert len(posts) == 1
        delivered = [row for row in load_delivery_records(case_dir, "2026-09-22") if row["delivery_status"] == "delivered"]
        assert len(delivered) == 1


def test_successful_delivery_persists_one_rank_with_success_latency(tmp_path, monkeypatch) -> None:
    value = runner(tmp_path)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    posts = []
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *_args, **kwargs: posts.append(kwargs["json"]["content"])
        or SimpleNamespace(status_code=204, text="", headers={}),
    )
    value._send_tradingview_candidate_alerts(pd.DataFrame([candidate()]))
    records = load_shadow_priority_records(
        tmp_path / "research" / "discord_shadow_ranking"
    )
    assert len(posts) == 1
    assert len(records) == 1
    assert records[0]["delivery_status"] == "delivered"
    assert records[0]["discord_success_at"] == ts(7).isoformat(timespec="microseconds")
    assert records[0]["stage_latencies_seconds"]["discord_attempt_to_success_seconds"] == 1.0


def test_phase1_timestamps_and_latency_report_are_preserved() -> None:
    record = calculate_shadow_priority(candidate(), ts(6))
    assert record["first_eligible_at"] == ts(1).isoformat()
    assert record["queue_selected_at"] == ts(2).isoformat()
    assert record["analysis_completed_at"] == ts(3).isoformat()
    assert record["qualified_at"] == ts(5).isoformat()
    assert record["discord_attempt_at"] == ts(6).isoformat(timespec="microseconds")
    assert record["stage_latencies_seconds"] == {
        "first_eligible_to_queue_selected_seconds": 1.0,
        "queue_selected_to_analysis_completed_seconds": 1.0,
        "analysis_completed_to_qualified_seconds": 2.0,
        "first_eligible_to_qualified_seconds": 4.0,
        "qualified_to_discord_attempt_seconds": 1.0,
    }
    assert record["largest_latency_stage"] == "analysis_completed_to_qualified_seconds"
    assert record["stage_price_drifts_pct"]["qualified_to_discord_attempt_pct"] > 0


def test_offline_evaluator_attaches_future_outcomes_without_changing_priority() -> None:
    record = calculate_shadow_priority(candidate(), ts(6))
    bars = pd.DataFrame(
        {
            "symbol": ["RANKED"] * 4,
            "bar_time": [ts(7), ts(11), ts(21), ts(61)],
            "high": [100.5, 101.0, 102.0, 103.0],
            "low": [100.0, 99.8, 99.5, 99.0],
            "close": [100.4, 100.8, 101.5, 102.0],
        }
    )
    pine = pd.DataFrame(
        [{"symbol": "RANKED", "pine_buy_timestamp": ts(21), "pine_buy_price": 101.0}]
    )
    evaluated = attach_future_outcomes([record], bars, pine)
    assert evaluated.iloc[0]["priority"] == record["priority"]
    assert evaluated.iloc[0]["mfe_5m_pct"] > 0
    assert evaluated.iloc[0]["mae_60m_pct"] < 0
    assert bool(evaluated.iloc[0]["pine_buy_generated"]) is True
    summary = summarize_group(evaluated, "priority")
    assert summary.iloc[0]["candidate_count"] == 1
    assert "prob_0.5pct_15m" in summary.columns


def test_detector_caps_thresholds_and_delivery_contract_remain_frozen() -> None:
    assert SignalMonitorRunner.EARLY_BREAKOUT_MIN_PCT == 1.0
    assert SignalMonitorRunner.EARLY_BREAKOUT_MAX_PCT == 3.5
    assert SignalMonitorRunner.EARLY_BREAKOUT_MIN_VOLUME_RATIO == 1.3
    assert SignalMonitorRunner.EARLY_BREAKOUT_MAX_PER_CYCLE == 4
    assert SignalMonitorRunner.TOP_GAINER_RADAR_MIN_PCT == 1.5
    assert SignalMonitorRunner.TOP_GAINER_RADAR_MAX_PCT == 3.5
    assert SignalMonitorRunner.TOP_GAINER_RADAR_MAX_PER_CYCLE == 1
    detector_source = inspect.getsource(SignalMonitorRunner._build_early_breakout_alerts)
    detector_source += inspect.getsource(SignalMonitorRunner._build_top_gainer_radar_alerts)
    assert "calculate_shadow_priority" not in detector_source
    assert "append_shadow_priority_record" not in detector_source

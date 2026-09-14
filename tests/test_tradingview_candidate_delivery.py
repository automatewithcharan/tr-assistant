import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.SignalMonitor import SignalMonitorRunner
from pkscreener.classes.TopGainerLifecycleRecorder import TopGainerLifecycleRecorder
from pkscreener.classes.TradingViewCandidateDelivery import (
    append_delivery_record,
    deterministic_fallback_candidate_key,
    journal_path,
    load_delivery_records,
    reconstruct_delivery_states,
    reconcile_tradingview_candidates,
)


def _runner(tmp_path) -> SignalMonitorRunner:
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    runner.universe = "all"
    runner.run_at = datetime(2026, 9, 7, 10, 15)
    runner.run_at_str = "2026-09-07 10:15:00"
    runner.alert_channels = {"discord"}
    runner.enable_intraday_goal_mode = True
    runner.goal_selector_validated = True
    runner.goal_max_trades_per_day = 50
    runner.goal_anomaly_only = True
    runner.goal_allow_direct_buy = False
    runner.radar_max_alerts_per_day = 3
    runner.anomaly_alert_only = False
    runner.anomaly_alert_only_max_per_day = 20
    runner._last_goal_suppressed_reasons = []
    runner.tradingview_discord_max_attempts = 3
    runner.tradingview_discord_retry_backoff_seconds = 0
    runner.tradingview_candidate_retry_ttl_seconds = 600
    runner.tradingview_candidate_max_total_attempts = 6
    runner._tradingview_delivery_now = lambda: datetime(
        2026, 9, 7, 10, 16, tzinfo=ZoneInfo("Asia/Kolkata")
    )
    runner._top_gainer_lifecycle_recorder = TopGainerLifecycleRecorder(str(tmp_path))
    return runner


def _candidate(lane: str, observation_id: str, symbol: str = "ABC") -> pd.DataFrame:
    action = "🌱 Early Breakout" if lane == "early_breakout" else "📡 Top Gainer Radar"
    return pd.DataFrame(
        [
            {
                "Lane": lane,
                "LifecycleObservationId": observation_id,
                "LifecycleTimestamp": "2026-09-07T10:15:00.000000+05:30",
                "When": "2026-09-07 10:15:00",
                "Symbol": symbol,
                "PreviousAction": "Hold / Watch",
                "NowAction": action,
                "PriceNow": 101.25,
                "ChangePct": 2.25,
                "EntryGapPct": 0.2,
                "RSI": 61.0,
                "BuyAbove": 101.25,
                "SellIfBelow": 100.0,
                "TakeProfit1": 103.0,
                "TakeProfit2": 104.0,
                "SuggestedQty": 10,
                "SignalStrengthPct": 58.0,
                "Checks": "vol 2.4x | B/S 1.8x",
                "Why": "direct detector qualification passed",
            }
        ]
    )


def _response(status: int, text: str = "", retry_after: str | None = None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return SimpleNamespace(status_code=status, text=text, headers=headers)


@pytest.mark.parametrize("lane", ["early_breakout", "top_gainer_radar"])
def test_direct_candidate_is_delivered_even_when_goal_mode_rejects(
    tmp_path, monkeypatch, lane
):
    runner = _runner(tmp_path)
    frame = _candidate(lane, f"obs-{lane}")
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *args, **kwargs: posts.append(kwargs["json"]) or _response(204),
    )

    assert runner._apply_intraday_goal_mode(frame).empty
    runner._send_tradingview_candidate_alerts(frame)

    assert len(posts) == 1
    assert "TRADINGVIEW CANDIDATE — MANUAL REVIEW" in posts[0]["content"]
    assert "Not an approved trade" in posts[0]["content"]


def test_successful_delivery_writes_structured_delivered_record(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(signal_monitor_module.requests, "post", lambda *a, **k: _response(204))

    runner._send_tradingview_candidate_alerts(_candidate("early_breakout", "obs-1"))

    record = load_delivery_records(tmp_path, "2026-09-07")[-1]
    assert record["lifecycle_observation_id"] == "obs-1"
    assert record["delivery_status"] == "delivered"
    assert record["http_status"] == 204
    assert record["attempt_count"] == 1
    assert record["delivered_at"]
    assert "webhook" not in json.dumps(record).lower()


def test_permanent_http_failure_is_failed_and_not_delivered(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(400, "bad request"),
    )

    frame = _candidate("early_breakout", "obs-2")
    runner._send_tradingview_candidate_alerts(frame)
    runner._send_tradingview_candidate_alerts(frame)

    records = load_delivery_records(tmp_path, "2026-09-07")
    assert posts == [1]
    assert len(records) == 2
    assert records[0]["delivery_status"] == "attempt_reserved"
    assert records[-1]["delivery_status"] == "terminal_failed"
    assert records[-1]["failure_class"] == "permanent_http"
    assert records[-1]["http_status"] == 400
    assert records[-1]["delivered_at"] is None


def test_transient_failure_retries_then_records_attempt_two_success(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    responses = iter([_response(500, "temporary"), _response(204)])
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(signal_monitor_module.requests, "post", lambda *a, **k: next(responses))

    runner._send_tradingview_candidate_alerts(_candidate("top_gainer_radar", "obs-3"))

    records = load_delivery_records(tmp_path, "2026-09-07")
    assert [row["delivery_status"] for row in records] == [
        "attempt_reserved",
        "retrying",
        "attempt_reserved",
        "delivered",
    ]
    assert records[-1]["attempt_count"] == 2


@pytest.mark.parametrize(
    "responses",
    [
        [_response(500), _response(502), _response(503)],
        [TimeoutError("timeout"), ConnectionError("down"), TimeoutError("timeout")],
    ],
)
def test_transient_immediate_exhaustion_remains_retryable(tmp_path, monkeypatch, responses):
    runner = _runner(tmp_path)
    outcomes = iter(responses)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")

    def post(*args, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(signal_monitor_module.requests, "post", post)
    runner._send_tradingview_candidate_alerts(_candidate("early_breakout", "obs-transient"))

    state = reconstruct_delivery_states(tmp_path, "2026-09-07")["obs-transient"]
    assert state["delivery_status"] == "retryable_failed"
    assert state["total_attempts"] == 3


def test_later_invocation_retries_transient_failure_and_then_deduplicates(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    outcomes = iter([_response(500), _response(500), _response(500), _response(204)])
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")

    def post(*args, **kwargs):
        posts.append(1)
        return next(outcomes)

    monkeypatch.setattr(signal_monitor_module.requests, "post", post)
    frame = _candidate("early_breakout", "obs-later")
    runner._send_tradingview_candidate_alerts(frame)
    runner._send_tradingview_candidate_alerts(frame)
    runner._send_tradingview_candidate_alerts(frame)

    state = reconstruct_delivery_states(tmp_path, "2026-09-07")["obs-later"]
    assert len(posts) == 4
    assert state["delivery_status"] == "delivered"
    assert state["total_attempts"] == 4
    assert sum(row["delivery_status"] == "delivered" for row in load_delivery_records(tmp_path, "2026-09-07")) == 1


def test_http_429_is_retryable_and_honors_retry_after(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    outcomes = iter([_response(429, retry_after="2.5"), _response(204)])
    sleeps = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(signal_monitor_module.requests, "post", lambda *a, **k: next(outcomes))
    monkeypatch.setattr(signal_monitor_module.time, "sleep", sleeps.append)

    runner._send_tradingview_candidate_alerts(_candidate("top_gainer_radar", "obs-429"))

    assert sleeps == [2.5]
    records = load_delivery_records(tmp_path, "2026-09-07")
    retry_record = next(row for row in records if row["delivery_status"] == "retrying")
    assert retry_record["retryable"] is True
    assert records[-1]["delivery_status"] == "delivered"


def test_global_attempt_limit_survives_invocations(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner.tradingview_discord_max_attempts = 2
    runner.tradingview_candidate_max_total_attempts = 5
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(503),
    )
    frame = _candidate("early_breakout", "obs-max")

    for _ in range(4):
        runner._send_tradingview_candidate_alerts(frame)

    state = reconstruct_delivery_states(tmp_path, "2026-09-07")["obs-max"]
    assert len(posts) == 5
    assert state["delivery_status"] == "terminal_failed"
    assert state["failure_class"] == "max_total_attempts_exhausted"
    assert state["total_attempts"] == 5


def test_retry_ttl_expiration_becomes_terminal_without_post(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner.tradingview_discord_max_attempts = 1
    clock = [datetime(2026, 9, 7, 10, 16, tzinfo=ZoneInfo("Asia/Kolkata"))]
    runner._tradingview_delivery_now = lambda: clock[0]
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(500),
    )
    frame = _candidate("early_breakout", "obs-ttl")
    runner._send_tradingview_candidate_alerts(frame)
    clock[0] = datetime(2026, 9, 7, 10, 26, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
    runner._send_tradingview_candidate_alerts(frame)
    runner._send_tradingview_candidate_alerts(frame)

    state = reconstruct_delivery_states(tmp_path, "2026-09-07")["obs-ttl"]
    assert len(posts) == 1
    assert state["delivery_status"] == "terminal_failed"
    assert state["failure_class"] == "retry_ttl_expired"


def test_restart_resumes_timeout_500_timeout_then_delivers_once(tmp_path, monkeypatch):
    first = _runner(tmp_path)
    outcomes = iter([TimeoutError("timeout"), _response(500), TimeoutError("timeout")])
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")

    def fail_post(*args, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(signal_monitor_module.requests, "post", fail_post)
    frame = _candidate("top_gainer_radar", "obs-restart")
    first._send_tradingview_candidate_alerts(frame)

    restarted = _runner(tmp_path)
    posts_after_restart = []
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts_after_restart.append(1) or _response(204),
    )
    restarted._retry_pending_tradingview_candidates()
    restarted._send_tradingview_candidate_alerts(frame)

    state = reconstruct_delivery_states(tmp_path, "2026-09-07")["obs-restart"]
    assert posts_after_restart == [1]
    assert state["delivery_status"] == "delivered"
    assert state["total_attempts"] == 4


def test_same_observation_is_not_delivered_twice(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(204),
    )
    frame = _candidate("early_breakout", "obs-4")

    runner._send_tradingview_candidate_alerts(frame)
    runner._send_tradingview_candidate_alerts(frame)

    assert len(posts) == 1


def test_corrupt_journal_disables_delivery_without_stopping_scanner(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    path = journal_path(tmp_path, "2026-09-07")
    path.parent.mkdir(parents=True)
    path.write_text("{not-json}\n", encoding="utf-8")
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(204),
    )

    runner._send_tradingview_candidate_alerts(_candidate("early_breakout", "corrupt"))
    runner._retry_pending_tradingview_candidates()

    assert posts == []
    assert runner._tradingview_delivery_available is False


def test_unwritable_journal_prevents_http_and_is_contained(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module,
        "append_delivery_record",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("read only")),
    )
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(204),
    )

    runner._send_tradingview_candidate_alerts(_candidate("early_breakout", "unwritable"))

    assert posts == []
    assert runner._tradingview_delivery_available is False


def test_http_success_then_outcome_persistence_failure_is_not_retried(
    tmp_path, monkeypatch
):
    first = _runner(tmp_path)
    real_append = signal_monitor_module.append_delivery_record
    append_calls = []

    def fail_second_append(output_dir, record):
        append_calls.append(record["delivery_status"])
        if len(append_calls) == 2:
            raise OSError("fsync failed")
        return real_append(output_dir, record)

    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(signal_monitor_module, "append_delivery_record", fail_second_append)
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(204),
    )
    frame = _candidate("early_breakout", "uncertain-after-success")

    first._send_tradingview_candidate_alerts(frame)
    assert posts == [1]
    assert first._tradingview_delivery_available is False

    monkeypatch.setattr(signal_monitor_module, "append_delivery_record", real_append)
    restarted = _runner(tmp_path)
    restarted._send_tradingview_candidate_alerts(frame)

    assert posts == [1]
    state = reconstruct_delivery_states(tmp_path, "2026-09-07")[
        "uncertain-after-success"
    ]
    assert state["delivery_status"] == "delivery_uncertain"
    assert state["total_attempts"] == 1


def test_missing_lifecycle_id_uses_durable_restart_fallback(tmp_path, monkeypatch):
    first = _runner(tmp_path)
    frame = _candidate("early_breakout", "")
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(204),
    )

    first._send_tradingview_candidate_alerts(frame)
    restarted = _runner(tmp_path)
    restarted.run_at_str = "2026-09-07 10:45:00"
    changed_timestamp = frame.copy()
    changed_timestamp.loc[0, "LifecycleTimestamp"] = "2026-09-07T10:45:00+05:30"
    restarted._send_tradingview_candidate_alerts(changed_timestamp)

    assert posts == [1]
    fallback_id = deterministic_fallback_candidate_key(
        "2026-09-07", "early_breakout", "ABC"
    )
    state = reconstruct_delivery_states(tmp_path, "2026-09-07")[fallback_id]
    assert state["delivery_status"] == "delivered"
    assert state["identity_kind"] == "causal_fallback"


def test_tradingview_helper_initialization_failure_disables_only_delivery(
    tmp_path, monkeypatch
):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    monkeypatch.setattr(signal_monitor_module, "reconstruct_delivery_states", None)

    runner._initialize_runtime_helpers()

    assert runner._tradingview_delivery_available is False
    assert runner._alert_timing_observer is not None


def test_distinct_observation_ids_are_not_merged(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    posts = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(
        signal_monitor_module.requests,
        "post",
        lambda *a, **k: posts.append(1) or _response(204),
    )

    runner._send_tradingview_candidate_alerts(_candidate("early_breakout", "obs-5"))
    runner._send_tradingview_candidate_alerts(_candidate("early_breakout", "obs-6"))

    assert len(posts) == 2


def test_candidate_delivery_does_not_touch_broker_execution(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner._autotrade_order_manager = SimpleNamespace(
        place_order=lambda *a, **k: pytest.fail("broker execution must not be called")
    )
    runner._ranked_confirmation_paper_trader = SimpleNamespace(
        register_candidates=lambda *a, **k: pytest.fail("paper entry must not be called")
    )
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(signal_monitor_module.requests, "post", lambda *a, **k: _response(204))

    runner._send_tradingview_candidate_alerts(_candidate("early_breakout", "obs-7"))


def test_reconciliation_distinguishes_every_delivery_state(tmp_path):
    recorder = TopGainerLifecycleRecorder(str(tmp_path))
    timestamp = "2026-09-07T10:15:00+05:30"
    identities = {}
    for symbol in ("DONE", "RETRY", "TERMINAL", "MISS"):
        observation_id = recorder.record_observation(
            {"symbol": symbol, "feature_available_at": timestamp},
            "early_breakout",
            timestamp,
        )
        recorder.record_event(observation_id, "alert_emitted", timestamp, "early_breakout")
        identities[symbol] = observation_id
    for symbol, status, http_status in (
        ("DONE", "delivered", 204),
        ("RETRY", "retryable_failed", 503),
        ("TERMINAL", "terminal_failed", 403),
    ):
        append_delivery_record(
            tmp_path,
            {
                "lifecycle_observation_id": identities[symbol],
                "trading_date": "2026-09-07",
                "symbol": symbol,
                "source_lane": "early_breakout",
                "detected_at": timestamp,
                "send_attempt_at": timestamp,
                "delivery_status": status,
                "http_status": http_status,
                "error": None if status == "delivered" else status,
                "delivered_at": timestamp if status == "delivered" else None,
                "attempt_count": 1,
            },
        )

    report = reconcile_tradingview_candidates(
        tmp_path,
        "2026-09-07",
        now=datetime(2026, 9, 7, 10, 16, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    lane = report["lanes"]["early_breakout"]
    assert lane["detected"] == 4
    assert lane["delivered"] == 1
    assert lane["retryable_failed"] == 1
    assert lane["terminal_failed"] == 1
    assert lane["missing"] == 1
    assert lane["missing_observations"] == [
        {"lifecycle_observation_id": identities["MISS"], "symbol": "MISS"}
    ]

from __future__ import annotations

import datetime as dt
import hashlib
import ast
import sqlite3
from pathlib import Path

import pytest

from pkscreener.classes.EmergingLeaderAlertPublisher import (
    EmergingLeaderAlertPublisher,
    IST,
    open_radar_read_only,
)
from pkscreener.classes.NGP5EarlyWatchPublisher import DeliveryResult
from pkscreener.classes.EmergingLeaderShadowPlan import (
    LIVE_PARTIAL_1M,
    ShadowPlanInputs,
    calculate_shadow_plan,
)
from pkscreener.classes.EmergingLeaderDiscordActionability import (
    DEFAULT_MIN_CUM_VOLUME,
    DEFAULT_MIN_TURNOVER,
    DiscordActionabilityThresholds,
    evaluate_discord_actionability,
)
from pkscreener.classes.EmergingLeaderDiscordQuality import (
    DEFAULT_MIN_DISTANCE_FROM_DAY_HIGH_PCT,
    DEFAULT_MIN_PRICE_VS_VWAP_PCT,
    DEFAULT_MIN_RANK_ACCELERATION,
    DEFAULT_MIN_RANK_IMPROVEMENT_5M,
    DEFAULT_MIN_RANK_VELOCITY_5M,
    DEFAULT_MIN_RECENT_CAPACITY_5PCT,
    DEFAULT_MIN_RECENT_TRADED_VALUE_RATE,
    DEFAULT_NOT_BEFORE_IST,
    DiscordQualityThresholds,
    evaluate_discord_quality,
)


class Clock:
    def __init__(self, epoch_ms: int = 1_800_000_000_000):
        self.epoch_ms = epoch_ms

    def __call__(self):
        return dt.datetime.fromtimestamp(self.epoch_ms / 1000, tz=IST)


class Transport:
    def __init__(self, *results):
        self.results = list(results)
        self.messages = []

    def post(self, message):
        self.messages.append(message)
        result = self.results.pop(0) if self.results else DeliveryResult(True, 200, False)
        if isinstance(result, BaseException):
            raise result
        return result


def make_radar(tmp_path: Path, session_date="2027-01-15"):
    root = tmp_path / "radar"
    path = root / session_date / "emerging_leader.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session_metadata(session_id INTEGER PRIMARY KEY, session_date TEXT);
            CREATE TABLE symbols(symbol_id INTEGER PRIMARY KEY, symbol TEXT UNIQUE);
            CREATE TABLE rank_events(
                session_id INTEGER, symbol_id INTEGER, timestamp_epoch_ms INTEGER,
                milestone INTEGER, broad_rank INTEGER, pct_change REAL,
                rank_universe_size INTEGER, expected_universe_size INTEGER,
                coverage_pct REAL, last_price REAL, cumulative_volume REAL,
                approx_turnover REAL,
                PRIMARY KEY(session_id,symbol_id,milestone)
            );
            CREATE TABLE snapshot_times(
                snapshot_id INTEGER PRIMARY KEY, session_id INTEGER,
                timestamp_epoch_ms INTEGER
            );
            CREATE TABLE features(
                snapshot_id INTEGER, symbol_id INTEGER, rank_5m_ago REAL,
                rank_improvement_5m REAL, rank_velocity_5m REAL,
                rank_acceleration REAL, rs_vs_nifty REAL, rs_change_5m REAL,
                turnover_acceleration REAL, volume_rate_5m REAL,
                distance_from_day_high_pct REAL, price_vs_vwap_pct REAL
            );
            INSERT INTO session_metadata VALUES(1, '2027-01-15');
            """
        )
    return root, path


def add_event(
    path: Path,
    symbol: str,
    timestamp: int,
    milestone: int,
    rank: int,
    features=True,
    cumulative_volume=800_000,
    approx_turnover=100_000_000,
    volume_rate_5m=40_000,
    rank_improvement_5m=152,
    rank_velocity_5m=30.4,
    rank_acceleration=2.2,
    distance_from_day_high_pct=-0.35,
    price_vs_vwap_pct=0.62,
):
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT symbol_id FROM symbols WHERE symbol=?", (symbol,)).fetchone()
        if row:
            symbol_id = row[0]
        else:
            symbol_id = connection.execute(
                "INSERT INTO symbols(symbol) VALUES(?) RETURNING symbol_id", (symbol,)
            ).fetchone()[0]
        connection.execute(
            "INSERT OR REPLACE INTO rank_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1, symbol_id, timestamp, milestone, rank, 2.41, 2180, 2337,
                93.28, 125.5, cumulative_volume, approx_turnover,
            ),
        )
        if features:
            snapshot_id = connection.execute(
                "INSERT INTO snapshot_times(session_id,timestamp_epoch_ms) VALUES(?,?) RETURNING snapshot_id",
                (1, timestamp),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO features VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    symbol_id,
                    None if features == "liquidity_only" else 428,
                    None if features == "liquidity_only" else rank_improvement_5m,
                    None if features == "liquidity_only" else rank_velocity_5m,
                    None if features == "liquidity_only" else rank_acceleration,
                    None if features == "liquidity_only" else 1.83,
                    None if features == "liquidity_only" else 0.4,
                    None if features == "liquidity_only" else 12_000_000,
                    volume_rate_5m,
                    None if features == "liquidity_only" else distance_from_day_high_pct,
                    None if features == "liquidity_only" else price_vs_vwap_pct,
                ),
            )


def publisher(
    tmp_path,
    transport=None,
    clock=None,
    shadow_plan_provider=None,
    quality_thresholds=None,
):
    root, radar = make_radar(tmp_path)
    value = EmergingLeaderAlertPublisher(
        root,
        tmp_path / "state" / "publisher.sqlite",
        transport,
        clock or Clock(),
        retry_base_seconds=0.1,
        shadow_plan_provider=shadow_plan_provider,
        actionability_thresholds=DiscordActionabilityThresholds(),
        quality_thresholds=quality_thresholds or DiscordQualityThresholds.permissive(),
    )
    return value, radar


def outbox_rows(value):
    with sqlite3.connect(value.state_db) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM outbox ORDER BY outbox_id").fetchall()


def actionability_rows(value):
    with sqlite3.connect(value.state_db) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM discord_actionability_audit ORDER BY source_event_id"
        ).fetchall()


def quality_rows(value):
    with sqlite3.connect(value.state_db) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM discord_quality_audit ORDER BY source_event_id"
        ).fetchall()


def test_radar_connection_is_read_only_and_source_is_unchanged(tmp_path):
    value, radar = publisher(tmp_path)
    before = hashlib.sha256(radar.read_bytes()).hexdigest()
    with open_radar_read_only(radar) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM rank_events")
    value.initialize_session("2027-01-15")
    assert hashlib.sha256(radar.read_bytes()).hexdigest() == before


def test_startup_cursor_prevents_historical_flood(tmp_path):
    value, radar = publisher(tmp_path, Transport())
    add_event(radar, "OLD", 1000, 300, 276)
    result = value.process_once("2027-01-15")
    assert result.initialized is True
    assert outbox_rows(value) == []
    assert value.process_once("2027-01-15").ingested == 0


def test_top300_delivery_and_duplicate_dedupe_survive_restart(tmp_path):
    transport = Transport(DeliveryResult(True, 200, False, message_id="one"))
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 2000, 300, 276)
    result = value.process_once("2027-01-15")
    assert result.delivered == 1
    assert len(transport.messages) == 1
    row = outbox_rows(value)[0]
    assert (row["stage"], row["delivery_state"], row["discord_message_id"]) == ("TOP300", "delivered", "one")
    restarted = EmergingLeaderAlertPublisher(value.radar_root, value.state_db, transport, Clock())
    assert restarted.process_once("2027-01-15").delivered == 0
    assert len(transport.messages) == 1


def test_progression_stages_require_delivered_emerging_and_graduate(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 2000, 300, 276)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 3000, 200, 188)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 4000, 125, 119)
    value.process_once("2027-01-15")
    add_event(radar, "ABC", 5000, 100, 96)
    value.process_once("2027-01-15")
    assert [row["stage"] for row in outbox_rows(value)] == [
        "TOP300", "TOP200", "TOP125", "TOP100_GRADUATED"
    ]
    assert len(transport.messages) == 4


@pytest.mark.parametrize("milestone,rank", [(200, 188), (125, 119), (100, 96)])
def test_generic_symbol_without_emerging_does_not_alert(tmp_path, milestone, rank):
    value, radar = publisher(tmp_path, Transport())
    value.process_once("2027-01-15")
    add_event(radar, "GENERIC", 2000, milestone, rank)
    value.process_once("2027-01-15")
    assert outbox_rows(value) == []


def test_top300_must_remain_outside_top100(tmp_path):
    value, radar = publisher(tmp_path, Transport())
    value.process_once("2027-01-15")
    add_event(radar, "JUMP", 2000, 300, 90)
    value.process_once("2027-01-15")
    assert outbox_rows(value) == []


def test_failed_delivery_stays_pending_and_retries(tmp_path):
    clock = Clock()
    transport = Transport(
        DeliveryResult(False, 503, True, error="http_503"),
        DeliveryResult(True, 200, False, message_id="retry-ok"),
    )
    value, radar = publisher(tmp_path, transport, clock)
    value.process_once("2027-01-15")
    add_event(radar, "RETRY", 2000, 300, 250)
    first = value.process_once("2027-01-15")
    assert first.pending == 1
    assert outbox_rows(value)[0]["delivery_attempts"] == 1
    clock.epoch_ms += 1000
    second = value.process_once("2027-01-15")
    assert second.delivered == 1 and second.pending == 0
    assert outbox_rows(value)[0]["discord_message_id"] == "retry-ok"


def test_missing_feature_enrichment_is_omitted(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(radar, "SPARSE", 2000, 300, 250, features="liquidity_only")
    value.process_once("2027-01-15")
    message = transport.messages[0]
    assert "EMERGING LEADER — SPARSE" in message
    assert "Rank 5m ago" not in message
    assert "Research watch only — not a buy signal" in message


def test_transport_exception_is_isolated_and_pending(tmp_path):
    value, radar = publisher(tmp_path, Transport(RuntimeError("discord down")))
    value.process_once("2027-01-15")
    add_event(radar, "SAFE", 2000, 300, 250)
    result = value.process_once("2027-01-15")
    assert result.pending == 1
    row = outbox_rows(value)[0]
    assert row["delivery_state"] == "pending"
    assert row["last_error"] == "transport_exception:RuntimeError"


def causal_inputs(entry=100.0, atr=2.0, low=99.5, price_age_seconds=1.0, mode=LIVE_PARTIAL_1M):
    candidate = dt.datetime(2027, 1, 15, 10, 0, tzinfo=IST)
    return ShadowPlanInputs(
        candidate_detected_at=candidate,
        reference_entry=entry,
        price_observed_at=candidate - dt.timedelta(seconds=price_age_seconds),
        atr14=atr,
        atr_input_at=candidate,
        signal_low=low,
        signal_low_at=candidate,
        stop_input_mode=mode,
    )


def test_valid_causal_shadow_plan_formula_and_max_quantity():
    plan = calculate_shadow_plan(causal_inputs())
    assert plan.shadow_plan_status == "VALID"
    assert plan.initial_stop == pytest.approx(99.0)
    assert plan.risk_per_share == pytest.approx(1.0)
    assert plan.final_qty == 500
    assert plan.target_1r == pytest.approx(101.0)
    assert plan.target_2r == pytest.approx(102.0)
    assert plan.planned_rupee_risk == pytest.approx(500.0)
    assert plan.position_exposure == pytest.approx(50_000.0)


def test_shadow_plan_risk_cap():
    plan = calculate_shadow_plan(causal_inputs(entry=100.0, atr=2.6, low=99.5))
    assert plan.shadow_plan_status == "VALID"
    assert plan.planned_rupee_risk <= 500.0 + 1e-6


def test_shadow_plan_exposure_cap():
    plan = calculate_shadow_plan(causal_inputs(entry=1000.0, atr=1.0, low=999.8))
    assert plan.shadow_plan_status == "VALID"
    assert plan.exposure_qty == 300
    assert plan.final_qty == 300
    assert plan.position_exposure <= 300_000.0 + 1e-6


@pytest.mark.parametrize(
    "inputs,reason",
    [
        (causal_inputs(atr=None), "causal ATR14"),
        (causal_inputs(low=None), "causal current-minute low"),
        (causal_inputs(price_age_seconds=121), "reference price stale"),
        (causal_inputs(mode="DAY_LOW"), "LIVE_PARTIAL_1M provenance"),
    ],
)
def test_invalid_or_noncausal_inputs_make_plan_unavailable(inputs, reason):
    plan = calculate_shadow_plan(inputs)
    assert plan.shadow_plan_status == "INPUTS_UNAVAILABLE"
    assert plan.final_qty is None
    assert reason in plan.missing_reason


def test_missing_plan_inputs_do_not_suppress_candidate_alert(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(radar, "NOINPUTS", 2000, 300, 250)
    result = value.process_once("2027-01-15")
    assert result.delivered == 1
    assert "Plan: INPUTS_UNAVAILABLE" in transport.messages[0]
    with sqlite3.connect(value.state_db) as connection:
        audit = connection.execute(
            "SELECT shadow_plan_status,price_observed_at,atr14,signal_low,final_qty "
            "FROM shadow_plan_audit"
        ).fetchone()
    assert audit == ("INPUTS_UNAVAILABLE", None, None, None, None)


@pytest.mark.parametrize("case", ["missing_atr", "missing_low", "stale_price"])
def test_each_unavailable_input_case_still_delivers_candidate(tmp_path, case):
    def provider(event):
        candidate = dt.datetime.fromtimestamp(event["event_epoch_ms"] / 1000, tz=IST)
        return ShadowPlanInputs(
            candidate_detected_at=candidate,
            reference_entry=event["last_price"],
            price_observed_at=candidate - dt.timedelta(seconds=121 if case == "stale_price" else 1),
            atr14=None if case == "missing_atr" else 2.0,
            atr_input_at=None if case == "missing_atr" else candidate,
            signal_low=None if case == "missing_low" else event["last_price"] - 0.5,
            signal_low_at=None if case == "missing_low" else candidate,
            stop_input_mode=LIVE_PARTIAL_1M,
        )

    transport = Transport()
    value, radar = publisher(tmp_path, transport, shadow_plan_provider=provider)
    value.process_once("2027-01-15")
    add_event(radar, "UNAVAILABLE", 2000, 300, 250)
    result = value.process_once("2027-01-15")
    assert result.delivered == 1
    assert len(transport.messages) == 1
    assert "Plan: INPUTS_UNAVAILABLE" in transport.messages[0]


def test_formatter_failure_cannot_suppress_alert(tmp_path):
    def broken_provider(_event):
        raise RuntimeError("format broke")

    transport = Transport()
    value, radar = publisher(tmp_path, transport, shadow_plan_provider=broken_provider)
    value.process_once("2027-01-15")
    add_event(radar, "STILLALERTS", 2000, 300, 250)
    result = value.process_once("2027-01-15")
    assert result.delivered == 1
    assert "Plan: PLAN_ERROR" in transport.messages[0]
    assert "EMERGING LEADER — STILLALERTS" in transport.messages[0]


def test_shadow_plan_does_not_change_candidate_selection(tmp_path):
    def valid_provider(event):
        candidate = dt.datetime.fromtimestamp(event["event_epoch_ms"] / 1000, tz=IST)
        return ShadowPlanInputs(
            candidate_detected_at=candidate,
            reference_entry=event["last_price"],
            price_observed_at=candidate,
            atr14=2.0,
            atr_input_at=candidate,
            signal_low=event["last_price"] - 0.5,
            signal_low_at=candidate,
            stop_input_mode=LIVE_PARTIAL_1M,
        )

    normal_dir = tmp_path / "normal"
    error_dir = tmp_path / "error"
    normal, normal_radar = publisher(normal_dir, Transport(), shadow_plan_provider=valid_provider)
    broken, broken_radar = publisher(
        error_dir,
        Transport(),
        shadow_plan_provider=lambda _event: (_ for _ in ()).throw(RuntimeError("no plan")),
    )
    for value, radar in ((normal, normal_radar), (broken, broken_radar)):
        value.process_once("2027-01-15")
        add_event(radar, "SAME", 2000, 300, 250)
        value.process_once("2027-01-15")
    normal_keys = [(row["symbol"], row["stage"]) for row in outbox_rows(normal)]
    broken_keys = [(row["symbol"], row["stage"]) for row in outbox_rows(broken)]
    assert normal_keys == broken_keys == [("SAME", "TOP300")]
    assert outbox_rows(normal)[0]["event_epoch_ms"] == outbox_rows(broken)[0]["event_epoch_ms"] == 2000


def test_valid_plan_observability_is_persisted(tmp_path):
    def provider(event):
        candidate = dt.datetime.fromtimestamp(event["event_epoch_ms"] / 1000, tz=IST)
        return ShadowPlanInputs(
            candidate_detected_at=candidate,
            reference_entry=100.0,
            price_observed_at=candidate - dt.timedelta(seconds=1),
            atr14=2.0,
            atr_input_at=candidate,
            signal_low=99.5,
            signal_low_at=candidate,
            stop_input_mode=LIVE_PARTIAL_1M,
        )

    transport = Transport()
    value, radar = publisher(tmp_path, transport, shadow_plan_provider=provider)
    value.process_once("2027-01-15")
    add_event(radar, "AUDITED", 2000, 300, 250)
    value.process_once("2027-01-15")
    with sqlite3.connect(value.state_db) as connection:
        connection.row_factory = sqlite3.Row
        audit = dict(connection.execute("SELECT * FROM shadow_plan_audit").fetchone())
    assert audit["shadow_plan_status"] == "VALID"
    assert audit["reference_entry"] == 100.0
    assert audit["atr14"] == 2.0
    assert audit["signal_low"] == 99.5
    assert audit["initial_stop"] == 99.0
    assert audit["risk_per_share"] == 1.0
    assert audit["risk_qty"] == 500
    assert audit["exposure_qty"] == 3000
    assert audit["final_qty"] == 500
    assert audit["target_1r"] == 101.0
    assert audit["target_2r"] == 102.0
    assert audit["planned_rupee_risk"] == 500.0
    assert audit["position_exposure"] == 50_000.0


def test_actionability_environment_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("NG_EMERGING_DISCORD_MIN_CUM_VOLUME", raising=False)
    monkeypatch.delenv("NG_EMERGING_DISCORD_MIN_TURNOVER", raising=False)
    defaults = DiscordActionabilityThresholds.from_environment()
    assert defaults.min_cumulative_volume == DEFAULT_MIN_CUM_VOLUME == 1_000.0
    assert defaults.min_turnover == DEFAULT_MIN_TURNOVER == 1_000_000.0
    monkeypatch.setenv("NG_EMERGING_DISCORD_MIN_CUM_VOLUME", "2500")
    monkeypatch.setenv("NG_EMERGING_DISCORD_MIN_TURNOVER", "3000000")
    overridden = DiscordActionabilityThresholds.from_environment()
    assert overridden == DiscordActionabilityThresholds(2_500.0, 3_000_000.0)


@pytest.mark.parametrize(
    "volume,turnover,rate,expected_reasons",
    [
        (10, 8_000, 1, ("LOW_CUM_VOLUME", "LOW_TURNOVER")),
        (10_000, 900_000, 500, ("LOW_TURNOVER",)),
        (999, 2_000_000, 500, ("LOW_CUM_VOLUME",)),
        (10_000, 2_000_000, None, ("NO_RECENT_VOLUME_RATE",)),
        (10_000, 2_000_000, 0, ("NONPOSITIVE_RECENT_VOLUME_RATE",)),
        (10_000, 2_000_000, -1, ("NONPOSITIVE_RECENT_VOLUME_RATE",)),
    ],
)
def test_actionability_suppression_reasons(volume, turnover, rate, expected_reasons):
    result = evaluate_discord_actionability(
        volume, turnover, rate, DiscordActionabilityThresholds()
    )
    assert result.discord_actionability_status == "SUPPRESSED_LOW_LIQUIDITY"
    assert result.reasons == expected_reasons


def test_liquid_candidate_passes_and_capacity_proxy_is_descriptive(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(
        radar, "LIQUID", 2000, 300, 250,
        cumulative_volume=10_000, approx_turnover=2_000_000, volume_rate_5m=500,
    )
    result = value.process_once("2027-01-15")
    assert result.delivered == 1
    audit = actionability_rows(value)[0]
    assert audit["discord_actionability_status"] == "PASS"
    assert audit["recent_5m_participation_capacity_5pct"] == 25
    message = transport.messages[0]
    assert "💧 ACTIONABILITY" in message
    assert "Liquidity gate: PASS" in message
    assert "5% recent-volume capacity proxy: 25 shares/min" in message
    assert message.count("Volume: 10,000") == 1
    assert message.count("Turnover: ₹0.20 Cr") == 1


def test_low_liquidity_is_retained_and_audited_but_not_sent(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(
        radar, "ILLIQUID", 2000, 300, 250,
        cumulative_volume=10, approx_turnover=8_000, volume_rate_5m=1,
    )
    result = value.process_once("2027-01-15")
    assert result.delivered == 0
    assert transport.messages == []
    assert outbox_rows(value) == []
    with sqlite3.connect(value.state_db) as connection:
        retained = connection.execute(
            "SELECT symbol,applied,cumulative_volume,approx_turnover,volume_rate_5m "
            "FROM source_events"
        ).fetchone()
    assert retained == ("ILLIQUID", 1, 10.0, 8_000.0, 1.0)
    audit = actionability_rows(value)[0]
    assert audit["discord_actionability_status"] == "SUPPRESSED_LOW_LIQUIDITY"
    assert audit["reasons"] == "LOW_CUM_VOLUME;LOW_TURNOVER"
    assert audit["min_cumulative_volume"] == 1_000.0
    assert audit["min_turnover"] == 1_000_000.0
    assert audit["recent_5m_participation_capacity_5pct"] == 0
    with sqlite3.connect(radar) as connection:
        assert connection.execute("SELECT count(*) FROM rank_events").fetchone()[0] == 1


def test_suppressed_top300_can_alert_at_liquid_top200_and_then_graduate(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(
        radar, "RECOVERS", 2000, 300, 276,
        cumulative_volume=400, approx_turnover=2_000_000, volume_rate_5m=100,
    )
    value.process_once("2027-01-15")
    assert outbox_rows(value) == []
    add_event(
        radar, "RECOVERS", 3000, 200, 188,
        cumulative_volume=5_000, approx_turnover=2_000_000, volume_rate_5m=500,
    )
    value.process_once("2027-01-15")
    assert [row["stage"] for row in outbox_rows(value)] == ["TOP200"]
    add_event(
        radar, "RECOVERS", 4000, 100, 96,
        cumulative_volume=8_000, approx_turnover=3_000_000, volume_rate_5m=600,
    )
    value.process_once("2027-01-15")
    assert [row["stage"] for row in outbox_rows(value)] == [
        "TOP200", "TOP100_GRADUATED"
    ]
    assert [row["discord_actionability_status"] for row in actionability_rows(value)] == [
        "SUPPRESSED_LOW_LIQUIDITY", "PASS", "PASS"
    ]


def test_suppressed_top300_can_alert_directly_at_liquid_top125(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(
        radar, "LATE125", 2000, 300, 276,
        cumulative_volume=400, approx_turnover=200_000, volume_rate_5m=10,
    )
    value.process_once("2027-01-15")
    add_event(
        radar, "LATE125", 3000, 125, 119,
        cumulative_volume=8_000, approx_turnover=3_000_000, volume_rate_5m=600,
    )
    value.process_once("2027-01-15")
    assert [row["stage"] for row in outbox_rows(value)] == ["TOP125"]
    assert [row["discord_actionability_status"] for row in actionability_rows(value)] == [
        "SUPPRESSED_LOW_LIQUIDITY", "PASS"
    ]


def test_suppressed_only_path_does_not_fake_graduation_delivery(tmp_path):
    transport = Transport()
    value, radar = publisher(tmp_path, transport)
    value.process_once("2027-01-15")
    add_event(
        radar, "NEVERSENT", 2000, 300, 276,
        cumulative_volume=10, approx_turnover=8_000, volume_rate_5m=1,
    )
    value.process_once("2027-01-15")
    add_event(
        radar, "NEVERSENT", 3000, 100, 96,
        cumulative_volume=10_000, approx_turnover=2_000_000, volume_rate_5m=500,
    )
    value.process_once("2027-01-15")
    assert outbox_rows(value) == []


def test_quality_environment_defaults(monkeypatch):
    names = (
        "NG_EMERGING_DISCORD_NOT_BEFORE_IST",
        "NG_EMERGING_DISCORD_MIN_RANK_IMPROVEMENT_5M",
        "NG_EMERGING_DISCORD_MIN_RANK_VELOCITY_5M",
        "NG_EMERGING_DISCORD_MIN_RANK_ACCELERATION",
        "NG_EMERGING_DISCORD_MIN_PRICE_VS_VWAP_PCT",
        "NG_EMERGING_DISCORD_MIN_DISTANCE_FROM_DAY_HIGH_PCT",
        "NG_EMERGING_DISCORD_MIN_RECENT_CAPACITY_5PCT",
        "NG_EMERGING_DISCORD_MIN_RECENT_TRADED_VALUE_RATE",
        "NG_EMERGING_DISCORD_REQUIRE_COMPLETE_QUALITY_FEATURES",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert DiscordQualityThresholds.from_environment() == DiscordQualityThresholds(
        not_before_ist=DEFAULT_NOT_BEFORE_IST,
        min_rank_improvement_5m=DEFAULT_MIN_RANK_IMPROVEMENT_5M,
        min_rank_velocity_5m=DEFAULT_MIN_RANK_VELOCITY_5M,
        min_rank_acceleration=DEFAULT_MIN_RANK_ACCELERATION,
        min_price_vs_vwap_pct=DEFAULT_MIN_PRICE_VS_VWAP_PCT,
        min_distance_from_day_high_pct=DEFAULT_MIN_DISTANCE_FROM_DAY_HIGH_PCT,
        min_recent_capacity_5pct=DEFAULT_MIN_RECENT_CAPACITY_5PCT,
        min_recent_traded_value_rate=DEFAULT_MIN_RECENT_TRADED_VALUE_RATE,
    )


def test_quality_gate_boundary_pass_and_multiple_failure_reasons():
    thresholds = DiscordQualityThresholds()
    passing = evaluate_discord_quality(
        dt.datetime(2027, 1, 15, 9, 30, tzinfo=IST),
        100, 20, 0, 0.75, -0.50, 500, 1_000, 50, thresholds,
    )
    assert passing.discord_quality_status == "PASS"
    assert passing.recent_traded_value_rate == 500_000
    failing = evaluate_discord_quality(
        dt.datetime(2027, 1, 15, 9, 20, tzinfo=IST),
        -5, -1, -2, -0.5, -2, 100, 5, 0, thresholds,
    )
    assert failing.discord_quality_status == "SUPPRESSED_LOW_QUALITY"
    assert set(failing.reasons) == {
        "BEFORE_ALERT_WINDOW",
        "LOW_RANK_IMPROVEMENT_5M",
        "LOW_RANK_VELOCITY_5M",
        "LOW_RANK_ACCELERATION",
        "BELOW_VWAP_BUFFER",
        "TOO_FAR_FROM_DAY_HIGH",
        "LOW_RECENT_SHARE_CAPACITY",
        "LOW_RECENT_TRADED_VALUE_RATE",
    }


def test_quality_suppression_is_audited_and_later_strong_stage_can_alert(tmp_path):
    transport = Transport()
    value, radar = publisher(
        tmp_path, transport, quality_thresholds=DiscordQualityThresholds()
    )
    value.process_once("2027-01-15")
    first = int(dt.datetime(2027, 1, 15, 9, 31, tzinfo=IST).timestamp() * 1000)
    add_event(
        radar, "QUALITYRECOVER", first, 300, 250,
        rank_improvement_5m=20, rank_velocity_5m=4,
        rank_acceleration=-1, price_vs_vwap_pct=0.2,
    )
    assert value.process_once("2027-01-15").delivered == 0
    audit = quality_rows(value)[0]
    assert audit["discord_quality_status"] == "SUPPRESSED_LOW_QUALITY"
    assert "LOW_RANK_IMPROVEMENT_5M" in audit["reasons"]
    with sqlite3.connect(value.state_db) as connection:
        assert connection.execute(
            "SELECT applied FROM source_events WHERE symbol='QUALITYRECOVER'"
        ).fetchone()[0] == 1

    add_event(
        radar, "QUALITYRECOVER", first + 5 * 60_000, 200, 180,
        rank_improvement_5m=150, rank_velocity_5m=30,
        rank_acceleration=4, price_vs_vwap_pct=1.2,
        distance_from_day_high_pct=-0.2,
    )
    result = value.process_once("2027-01-15")
    assert result.delivered == 1
    assert [row["stage"] for row in outbox_rows(value)] == ["TOP200"]
    assert [row["discord_quality_status"] for row in quality_rows(value)] == [
        "SUPPRESSED_LOW_QUALITY", "PASS"
    ]
    assert "Quality gate: PASS" in transport.messages[0]


def test_same_event_milestones_collapse_to_deepest_stage(tmp_path):
    transport = Transport()
    value, radar = publisher(
        tmp_path, transport, quality_thresholds=DiscordQualityThresholds()
    )
    value.process_once("2027-01-15")
    timestamp = int(dt.datetime(2027, 1, 15, 9, 35, tzinfo=IST).timestamp() * 1000)
    common = dict(
        rank_improvement_5m=180,
        rank_velocity_5m=36,
        rank_acceleration=5,
        price_vs_vwap_pct=1.5,
        distance_from_day_high_pct=-0.1,
    )
    add_event(radar, "FASTJUMP", timestamp, 300, 120, **common)
    add_event(radar, "FASTJUMP", timestamp, 200, 120, **common)
    add_event(radar, "FASTJUMP", timestamp, 125, 120, **common)
    result = value.process_once("2027-01-15")
    assert result.delivered == 1
    assert [row["stage"] for row in outbox_rows(value)] == ["TOP125"]
    audits = quality_rows(value)
    assert [row["stage"] for row in audits] == ["TOP300", "TOP200", "TOP125"]
    assert [row["discord_quality_status"] for row in audits] == [
        "SUPPRESSED_SUPERSEDED_STAGE", "SUPPRESSED_SUPERSEDED_STAGE", "PASS"
    ]
    assert [row["reasons"] for row in audits[:2]] == [
        "SUPERSEDED_SAME_EVENT", "SUPERSEDED_SAME_EVENT"
    ]
    with sqlite3.connect(value.state_db) as connection:
        assert connection.execute("SELECT count(*) FROM source_events").fetchone()[0] == 3
        assert connection.execute("SELECT sum(applied) FROM source_events").fetchone()[0] == 3


def test_shadow_plan_modules_have_no_broker_or_order_dependencies():
    root = Path(__file__).parents[1]
    paths = [
        root / "pkscreener/classes/EmergingLeaderDiscordActionability.py",
        root / "pkscreener/classes/EmergingLeaderDiscordQuality.py",
        root / "pkscreener/classes/EmergingLeaderShadowPlan.py",
        root / "pkscreener/classes/EmergingLeaderAlertPublisher.py",
    ]
    imported = set()
    combined = ""
    for path in paths:
        source = path.read_text()
        combined += source.lower()
        tree = ast.parse(source)
        imported.update(
            alias.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        )
    assert not any("broker" in name or "order" in name for name in imported)
    assert "place_order(" not in combined
    assert "kiteconnect" not in combined


def test_systemd_flags_keep_legacy_alerts_disabled():
    unit = (Path(__file__).parents[1] / "deploy/systemd/ng-emerging-leader-alerts.service").read_text()
    assert "Environment=NG_EMERGING_LEADER_DISCORD_ENABLED=1" in unit
    assert "Environment=NG_TOP100_WATCH_DISCORD_ENABLED=0" in unit
    assert "Environment=NG_NIFTY50_WATCH_DISCORD_ENABLED=0" in unit

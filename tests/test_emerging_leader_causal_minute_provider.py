import datetime as dt
import sqlite3

from pkscreener.classes.EmergingLeaderCausalMinuteProvider import (
    EmergingLeaderCausalMinuteProvider,
    IST,
)
from pkscreener.classes.EmergingLeaderShadowPlan import calculate_shadow_plan


def at(hour, minute, second=0):
    return dt.datetime(2027, 1, 15, hour, minute, second, tzinfo=IST)


def tick(symbol, price, observed_at, day_low=1.0):
    return {
        symbol: {
            "symbol": symbol,
            "last_price": price,
            "received_at": observed_at.isoformat(),
            # This is the session low and must never enter the minute provider.
            "low": day_low,
        }
    }


def event(symbol, price, detected_at):
    return {
        "symbol": symbol,
        "last_price": price,
        "event_epoch_ms": int(detected_at.timestamp() * 1000),
    }


def warm_provider(provider, symbol="LIQUID"):
    # Sixteen completed observed bars give the provider a prior close plus at
    # least fourteen causal true ranges for Wilder ATR(14).
    for offset in range(16):
        minute = 15 + offset
        base = 100.0 + offset * 0.25
        for second, price in ((1, base), (20, base - 0.4), (50, base + 0.3)):
            observed = at(9, minute, second)
            provider.observe_ticks(tick(symbol, price, observed), observed)
    provider.observe_ticks({}, at(9, 31, 0))


def test_provider_builds_valid_causal_plan_and_ignores_day_low_and_future_tick(tmp_path):
    provider = EmergingLeaderCausalMinuteProvider(
        tmp_path / "ticks.json",
        tmp_path / "publisher.sqlite",
        clock=lambda: at(9, 14, 59),
    )
    warm_provider(provider)
    provider.observe_ticks(tick("LIQUID", 110.0, at(9, 31, 5), day_low=1.0), at(9, 31, 5))
    provider.observe_ticks(tick("LIQUID", 109.0, at(9, 31, 20), day_low=1.0), at(9, 31, 20))
    provider.observe_ticks(tick("LIQUID", 109.5, at(9, 31, 25), day_low=1.0), at(9, 31, 25))
    # The provider may have observed this by processing time, but it happened
    # after candidate detection and must not affect the earlier plan.
    provider.observe_ticks(tick("LIQUID", 50.0, at(9, 31, 40), day_low=1.0), at(9, 31, 40))

    inputs = provider(event("LIQUID", 109.5, at(9, 31, 30)))
    assert inputs.signal_low == 109.0
    assert inputs.signal_low_at == at(9, 31, 20)
    assert inputs.price_observed_at == at(9, 31, 25)
    assert inputs.atr14 is not None and inputs.atr14 > 0
    assert inputs.atr_input_at == at(9, 31, 0)
    plan = calculate_shadow_plan(inputs)
    assert plan.shadow_plan_status == "VALID"
    assert plan.final_qty is not None and plan.final_qty > 0
    assert plan.initial_stop < plan.reference_entry
    assert plan.target_1r > plan.reference_entry
    assert plan.target_2r > plan.target_1r


def test_provider_fails_closed_for_incomplete_startup_minute(tmp_path):
    provider = EmergingLeaderCausalMinuteProvider(
        tmp_path / "ticks.json",
        tmp_path / "publisher.sqlite",
        clock=lambda: at(9, 15, 20),
    )
    provider.observe_ticks(tick("STARTUP", 100.0, at(9, 15, 25)), at(9, 15, 25))
    inputs = provider(event("STARTUP", 100.0, at(9, 15, 30)))
    plan = calculate_shadow_plan(inputs)
    assert plan.shadow_plan_status == "INPUTS_UNAVAILABLE"
    assert inputs.signal_low is None
    assert inputs.atr14 is None


def test_provider_rejects_unmatched_event_price_timestamp(tmp_path):
    provider = EmergingLeaderCausalMinuteProvider(
        tmp_path / "ticks.json",
        tmp_path / "publisher.sqlite",
        clock=lambda: at(9, 14, 59),
    )
    warm_provider(provider)
    provider.observe_ticks(tick("LIQUID", 109.0, at(9, 31, 20)), at(9, 31, 20))
    inputs = provider(event("LIQUID", 109.5, at(9, 31, 30)))
    assert inputs.price_observed_at is None
    plan = calculate_shadow_plan(inputs)
    assert plan.shadow_plan_status == "INPUTS_UNAVAILABLE"
    assert "price_observed_at" in plan.missing_reason


def test_provider_requires_consecutive_completed_minutes_for_atr(tmp_path):
    provider = EmergingLeaderCausalMinuteProvider(
        tmp_path / "ticks.json",
        tmp_path / "publisher.sqlite",
        clock=lambda: at(9, 14, 59),
    )
    for offset in range(17):
        if offset == 8:
            continue
        observed = at(9, 15 + offset, 10)
        provider.observe_ticks(tick("GAPPED", 100 + offset, observed), observed)
    provider.observe_ticks({}, at(9, 32, 0))
    provider.observe_ticks(tick("GAPPED", 120.0, at(9, 32, 10)), at(9, 32, 10))
    inputs = provider(event("GAPPED", 120.0, at(9, 32, 20)))
    assert inputs.signal_low == 120.0
    assert inputs.atr14 is None
    assert calculate_shadow_plan(inputs).shadow_plan_status == "INPUTS_UNAVAILABLE"


def test_completed_bars_are_persisted_only_in_sidecar_state(tmp_path):
    state_db = tmp_path / "publisher.sqlite"
    provider = EmergingLeaderCausalMinuteProvider(
        tmp_path / "ticks.json", state_db, clock=lambda: at(9, 14, 59)
    )
    warm_provider(provider, "PERSISTED")
    with sqlite3.connect(state_db) as connection:
        count, sources = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT source_mode) FROM shadow_observed_minute_bars"
        ).fetchone()
        source = connection.execute(
            "SELECT source_mode FROM shadow_observed_minute_bars LIMIT 1"
        ).fetchone()[0]
    assert count == 16
    assert sources == 1
    assert source == "KITE_LATEST_SNAPSHOT_OBSERVED_1M"


def test_module_has_no_broker_or_order_dependency():
    import inspect
    import pkscreener.classes.EmergingLeaderCausalMinuteProvider as module

    source = inspect.getsource(module).lower()
    assert "kiteconnect" not in source
    assert "place_order" not in source
    assert "signalmonitor" not in source

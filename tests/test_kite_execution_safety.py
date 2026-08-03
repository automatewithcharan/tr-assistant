import ast
import json
import multiprocessing
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pkscreener.classes.AutoPilotInvestor import AutoPilotConfig, AutoPilotInvestor
from pkscreener.classes.DiscordTradeBot import _strict_discord_authorization
from pkscreener.classes.KiteOrderManager import (
    LIVE_ACKNOWLEDGEMENT_TOKEN,
    BrokerMutationError,
    KiteOrderManager,
    TrackedPosition,
    _drain_test_notification_events,
    _send_discord_alert,
)
from conftest import OfflineNetworkAttempt, SANITIZED_ENVIRONMENT_NAMES
from pkscreener.classes.RiskGuardrails import LiveTradingGuardrails


SAFETY_ENV = (
    "KITE_EXECUTION_MODE", "KITE_DRY_RUN", "KITE_AUTO_TRADE",
    "KITE_LIVE_ACKNOWLEDGEMENT", "KITE_LIVE_CALLER_ALLOWLIST",
    "KITE_LIVE_STRATEGY_ALLOWLIST", "KITE_LIVE_EXECUTION_ALLOWLIST",
    "ASSISTANT_MAX_DAILY_LOSS", "ASSISTANT_MAX_CONSECUTIVE_LOSSES",
)


class Broker:
    GTT_TYPE_OCO = "two-leg"
    def __init__(self):
        self.place_calls = 0
        self.cancel_calls = 0
        self.gtt_place_calls = 0
        self.gtt_delete_calls = 0
        self.order_rows = []
        self.gtt_rows = []
        self.position_rows = []
        self.holding_rows = []

    def positions(self):
        return {"net": list(self.position_rows)}

    def holdings(self):
        return list(self.holding_rows)

    def orders(self):
        return list(self.order_rows)

    def get_gtts(self):
        return list(self.gtt_rows)

    def place_order(self, **kwargs):
        self.place_calls += 1
        order_id = f"OID-{self.place_calls}"
        self.order_rows.append({
            "order_id": order_id, "tag": kwargs["tag"], "status": "OPEN",
            "tradingsymbol": kwargs["tradingsymbol"],
            "transaction_type": kwargs["transaction_type"],
            "quantity": kwargs["quantity"], "product": kwargs.get("product", "MIS"),
        })
        return order_id

    def cancel_order(self, **kwargs):
        self.cancel_calls += 1
        for row in self.order_rows:
            if row["order_id"] == kwargs["order_id"]:
                row["status"] = "CANCELLED"
        return kwargs["order_id"]

    def place_gtt(self, **kwargs):
        self.gtt_place_calls += 1
        trigger_id = f"GTT-{self.gtt_place_calls}"
        self.gtt_rows.append({
            "id": trigger_id, "status": "active",
            "condition": {
                "tradingsymbol": kwargs["tradingsymbol"],
                "trigger_values": kwargs["trigger_values"],
            },
            "orders": kwargs["orders"],
        })
        return {"trigger_id": trigger_id}

    def delete_gtt(self, trigger_id):
        self.gtt_delete_calls += 1
        self.gtt_rows = [row for row in self.gtt_rows if row["id"] != trigger_id]


@pytest.fixture
def manager(tmp_path, monkeypatch):
    for name in SAFETY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KITE_LIVE_NAMESPACE_ROOT", str(tmp_path / "live-namespace"))
    return KiteOrderManager(output_dir=str(tmp_path))


def enable_live(monkeypatch, manager, callers="test_caller", strategies="test_strategy"):
    monkeypatch.setenv("KITE_EXECUTION_MODE", "live")
    monkeypatch.setenv("KITE_DRY_RUN", "0")
    monkeypatch.setenv("KITE_AUTO_TRADE", "1")
    monkeypatch.setenv("KITE_LIVE_ACKNOWLEDGEMENT", LIVE_ACKNOWLEDGEMENT_TOKEN)
    monkeypatch.setenv("KITE_LIVE_CALLER_ALLOWLIST", callers)
    monkeypatch.setenv("KITE_LIVE_STRATEGY_ALLOWLIST", strategies)
    monkeypatch.setenv("ASSISTANT_MAX_DAILY_LOSS", "1000")
    monkeypatch.setenv("ASSISTANT_MAX_CONSECUTIVE_LOSSES", "2")
    manager.dry_run = False
    manager.auto_trade = True


def write_entry_evidence(manager, symbol="TEST"):
    guard = LiveTradingGuardrails(
        account_name="main", evidence_path=str(Path(manager.output_dir) / "risk_evidence_main.json")
    )
    guard.write_evidence_contract(
        daily_realized_pnl=0, consecutive_losses=0, sequence=0,
        session_state="open", zero_trade=True,
    )
    (Path(manager.output_dir) / "kiteticker_all_ticks_latest.json").write_text(
        json.dumps({symbol: {"last_price": 100.0}}), encoding="utf-8"
    )


@pytest.mark.parametrize("caller,strategy,callers,strategies", [
    ("", "test_strategy", "test_caller", "test_strategy"),
    ("test_caller", "", "test_caller", "test_strategy"),
    ("test_caller", "test_strategy", "", "test_strategy"),
    ("test_caller", "test_strategy", "test_caller", ""),
    ("wrong", "test_strategy", "test_caller", "test_strategy"),
    ("test_caller", "wrong", "test_caller", "test_strategy"),
    ("test_caller", "test_strategy", " test_caller", "test_strategy"),
    ("test_caller", "test_strategy", "Test_caller", "test_strategy"),
    ("test_caller", "test_strategy", "test_caller,test_caller", "test_strategy"),
    ("test_caller", "test_strategy", "test_caller,", "test_strategy"),
])
def test_strict_independent_identity_matrix(
    manager, monkeypatch, caller, strategy, callers, strategies
):
    enable_live(monkeypatch, manager, callers, strategies)
    assert not manager.authorize_broker_mutation(caller=caller, strategy=strategy)["authorized"]


def test_both_exact_identities_are_required_and_grade_b_is_paper_only(manager, monkeypatch):
    enable_live(
        monkeypatch, manager, "test_caller,signalmonitor_autotrade",
        "test_strategy,grade_b_confirmation",
    )
    assert manager.authorize_broker_mutation(
        caller="test_caller", strategy="test_strategy"
    )["authorized"]
    denied = manager.authorize_broker_mutation(
        caller="signalmonitor_autotrade", strategy="grade_b_confirmation"
    )
    assert not denied["authorized"]
    assert any("paper-only" in reason for reason in denied["reasons"])


def test_acknowledgement_and_mode_values_are_exact(manager, monkeypatch):
    enable_live(monkeypatch, manager)
    monkeypatch.setenv("KITE_LIVE_ACKNOWLEDGEMENT", f" {LIVE_ACKNOWLEDGEMENT_TOKEN}")
    assert not manager.authorize_broker_mutation(
        caller="test_caller", strategy="test_strategy"
    )["authorized"]
    monkeypatch.setenv("KITE_LIVE_ACKNOWLEDGEMENT", LIVE_ACKNOWLEDGEMENT_TOKEN)
    monkeypatch.setenv("KITE_EXECUTION_MODE", "live ")
    assert not manager.authorize_broker_mutation(
        caller="test_caller", strategy="test_strategy"
    )["authorized"]


def test_private_submit_requires_complete_entry_context(manager, monkeypatch):
    enable_live(monkeypatch, manager)
    broker = Broker()
    with pytest.raises(BrokerMutationError):
        manager._submit_order_once(
            broker, {
                "tradingsymbol": "TEST", "transaction_type": "BUY",
                "quantity": 1, "product": "MIS",
            },
            caller="test_caller", strategy="test_strategy", kind="entry",
        )
    assert broker.place_calls == 0

    write_entry_evidence(manager)
    order_id = manager._submit_order_once(
        broker, {
            "tradingsymbol": "TEST", "transaction_type": "BUY",
            "quantity": 1, "product": "MIS",
        },
        caller="test_caller", strategy="test_strategy", kind="entry",
    )
    assert order_id == "OID-1"
    assert broker.place_calls == 1


def test_authoritative_journal_failure_is_fail_closed_before_submission(manager, monkeypatch):
    enable_live(monkeypatch, manager)
    write_entry_evidence(manager)
    broker = Broker()
    monkeypatch.setattr(
        manager, "_append_authoritative_transition",
        lambda payload: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    with pytest.raises(BrokerMutationError, match="journal"):
        manager._submit_order_once(
            broker, {"tradingsymbol": "TEST", "transaction_type": "BUY", "quantity": 1, "product": "MIS"},
            caller="test_caller", strategy="test_strategy", kind="entry",
        )
    assert broker.place_calls == 0


def test_post_submission_journal_failure_enters_durable_recovery(manager, monkeypatch):
    enable_live(monkeypatch, manager)
    write_entry_evidence(manager)
    broker = Broker()
    original = manager._append_authoritative_transition

    def fail_ack(payload):
        if payload.get("event") == "mutation_transition" and payload.get("state") == "acknowledged":
            raise OSError("disk unavailable after broker response")
        return original(payload)

    monkeypatch.setattr(manager, "_append_authoritative_transition", fail_ack)
    with pytest.raises(OSError):
        manager._submit_order_once(
            broker, {"tradingsymbol": "TEST", "transaction_type": "BUY", "quantity": 1, "product": "MIS"},
            caller="test_caller", strategy="test_strategy", kind="entry",
        )
    assert broker.place_calls == 1
    assert any(item["state"] == "recovery_required" for item in manager._order_intents.values())
    restarted = KiteOrderManager(output_dir=manager.output_dir)
    assert any(item["state"] == "recovery_required" for item in restarted._order_intents.values())


def test_all_mutation_adapters_make_zero_calls_without_authorization(manager):
    broker = Broker()
    manager._client = broker
    pos = TrackedPosition(
        "TEST", 1, 100, stop_loss=95, target2=110, product_type="MIS",
        bot_owned=True, auto_exit_allowed=True,
    )
    manager._positions["TEST"] = pos
    assert not manager._cancel_order_best_effort(broker, "OID-X")
    assert not manager._place_protective_stop_order(pos, broker)
    assert not manager.delete_gtt("GTT-X")
    result = manager.place_gtt_oco("TEST", 1, 100, 95, 110)
    assert result["success"]  # paper simulation is allowed; broker side effects are not
    assert (broker.place_calls, broker.cancel_calls, broker.gtt_place_calls, broker.gtt_delete_calls) == (0, 0, 0, 0)


def test_every_nonentry_adapter_denies_when_reconciliation_is_invalid(manager, monkeypatch):
    enable_live(
        monkeypatch, manager,
        "test_caller,protective_stop,gtt_repair",
        "risk_reducing_exit,position_protection",
    )
    manager._resolve_exchange_and_tick_size = lambda symbol: ("NSE", 0.05)
    broker = Broker()
    manager._client = broker
    pos = TrackedPosition(
        "TEST", 1, 100, stop_loss=95, target2=110, product_type="MIS",
        bot_owned=True, auto_exit_allowed=True,
    )
    pos.gtt_trigger_id = "GTT-X"
    manager._positions["TEST"] = pos
    assert not manager._cancel_order_best_effort(
        broker, "OID-X", caller="test_caller", strategy="risk_reducing_exit"
    )
    assert not manager._place_protective_stop_order(pos, broker)
    assert not manager.place_gtt_oco(
        "TEST", 1, 100, 95, 110,
        caller="gtt_repair", strategy="position_protection",
    )["success"]
    assert not manager.delete_gtt(
        "GTT-X", caller="gtt_repair", strategy="position_protection"
    )
    with pytest.raises(BrokerMutationError):
        manager._submit_order_once(
            broker, {"tradingsymbol": "TEST", "transaction_type": "SELL", "quantity": 1, "product": "MIS"},
            caller="test_caller", strategy="risk_reducing_exit", kind="exit",
            reconciliation_policy="exit", position=pos,
        )
    assert (broker.place_calls, broker.cancel_calls, broker.gtt_place_calls, broker.gtt_delete_calls) == (0, 0, 0, 0)


def test_unknown_open_order_blocks_entry(manager, monkeypatch):
    enable_live(monkeypatch, manager)
    write_entry_evidence(manager)
    broker = Broker()
    broker.order_rows = [{
        "order_id": "FOREIGN", "tag": "", "status": "OPEN",
        "tradingsymbol": "OTHER", "quantity": 1,
    }]
    with pytest.raises(BrokerMutationError, match="unknown active"):
        manager._submit_order_once(
            broker, {"tradingsymbol": "TEST", "transaction_type": "BUY", "quantity": 1, "product": "MIS"},
            caller="test_caller", strategy="test_strategy", kind="entry",
        )
    assert broker.place_calls == 0


def test_ambiguous_order_uses_bounded_visibility_and_never_duplicates(manager, monkeypatch):
    enable_live(monkeypatch, manager)
    write_entry_evidence(manager)
    manager._sleep = lambda _: None
    manager._reconcile_attempts = 3

    class Delayed(Broker):
        def __init__(self):
            super().__init__()
            self.saved = None
            self.after_submit_queries = 0
        def place_order(self, **kwargs):
            self.place_calls += 1
            self.saved = kwargs
            raise TimeoutError("accepted but response lost")
        def orders(self):
            if self.saved is None:
                return []
            self.after_submit_queries += 1
            if self.after_submit_queries < 2:
                return []
            return [{
                "order_id": "DELAYED", "tag": self.saved["tag"], "status": "OPEN",
                "tradingsymbol": "TEST", "quantity": 1,
            }]

    broker = Delayed()
    assert manager._submit_order_once(
        broker, {"tradingsymbol": "TEST", "transaction_type": "BUY", "quantity": 1, "product": "MIS"},
        caller="test_caller", strategy="test_strategy", kind="entry",
    ) == "DELAYED"
    assert broker.place_calls == 1


def protected_position(manager, broker, *, product="CNC"):
    pos = TrackedPosition(
        "TEST", 2, 100, stop_loss=95, target2=110, product_type=product,
        bot_owned=True, auto_exit_allowed=True,
    )
    manager._positions["TEST"] = pos
    if product == "MIS":
        broker.position_rows = [{"tradingsymbol": "TEST", "quantity": 2, "product": "MIS"}]
    else:
        broker.holding_rows = [{"tradingsymbol": "TEST", "quantity": 2, "t1_quantity": 0}]
    return pos


def test_delayed_protective_sl_m_never_creates_fallback_duplicate(manager, monkeypatch):
    enable_live(monkeypatch, manager, "protective_stop", "position_protection")
    manager._sleep = lambda _: None
    manager._resolve_exchange_and_tick_size = lambda symbol: ("NSE", 0.05)

    class DelayedStop(Broker):
        def __init__(self):
            super().__init__()
            self.saved = None
            self.visibility = 0
        def place_order(self, **kwargs):
            self.place_calls += 1
            self.saved = kwargs
            raise TimeoutError("accepted, response lost")
        def orders(self):
            if self.saved is None:
                return []
            self.visibility += 1
            if self.visibility < 2:
                return []
            return [{
                "order_id": "SLM-1", "tag": self.saved["tag"], "status": "TRIGGER PENDING",
                "tradingsymbol": "TEST", "quantity": 2, "transaction_type": "SELL",
                "product": "MIS", "variety": "regular", "order_type": "SL-M",
                "trigger_price": 95.0,
            }]
        def order_history(self, order_id):
            return [{"order_id": order_id, "status": "TRIGGER PENDING"}]

    broker = DelayedStop()
    pos = protected_position(manager, broker, product="MIS")
    assert manager._place_protective_stop_order(pos, broker)
    assert broker.place_calls == 1


def test_gtt_delete_lost_response_is_confirmed_before_success(manager, monkeypatch):
    enable_live(monkeypatch, manager, "gtt_repair", "position_protection")
    manager._sleep = lambda _: None

    class LostDelete(Broker):
        def delete_gtt(self, trigger_id):
            self.gtt_delete_calls += 1
            self.gtt_rows = []
            raise TimeoutError("response lost")

    broker = LostDelete()
    pos = protected_position(manager, broker)
    pos.gtt_trigger_id = "GTT-OLD"
    broker.gtt_rows = [{
        "id": "GTT-OLD", "status": "active",
        "condition": {"tradingsymbol": "TEST", "trigger_values": [95, 110]},
        "orders": [{"quantity": 2}, {"quantity": 2}],
    }]
    manager._client = broker
    assert manager.delete_gtt(
        "GTT-OLD", caller="gtt_repair", strategy="position_protection"
    )
    assert broker.gtt_delete_calls == 1


def test_failed_or_unconfirmed_gtt_delete_blocks_replacement(manager, monkeypatch):
    enable_live(monkeypatch, manager, "gtt_repair", "position_protection")
    manager._sleep = lambda _: None

    class FailedDelete(Broker):
        def delete_gtt(self, trigger_id):
            self.gtt_delete_calls += 1
            raise TimeoutError("not accepted")

    broker = FailedDelete()
    pos = protected_position(manager, broker)
    pos.gtt_trigger_id = "GTT-OLD"
    pos.gtt_needs_reissue = True
    broker.gtt_rows = [{
        "id": "GTT-OLD", "status": "active",
        "condition": {"tradingsymbol": "TEST", "trigger_values": [95, 110]},
        "orders": [{"quantity": 2}, {"quantity": 2}],
    }]
    manager._client = broker
    actions = manager.audit_open_position_gtts()
    assert actions[0]["action"] == "delete_unresolved"
    assert broker.gtt_place_calls == 0


def test_ambiguous_gtt_create_delayed_visibility_and_duplicate_detection(manager, monkeypatch):
    enable_live(monkeypatch, manager, "gtt_repair", "position_protection")
    manager._sleep = lambda _: None
    manager._resolve_exchange_and_tick_size = lambda symbol: ("NSE", 0.05)

    class DelayedGTT(Broker):
        def __init__(self, duplicate=False):
            super().__init__()
            self.polls = 0
            self.duplicate = duplicate
        def place_gtt(self, **kwargs):
            self.gtt_place_calls += 1
            self.saved = kwargs
            raise TimeoutError("response lost")
        def get_gtts(self):
            self.polls += 1
            # First call is pre-mutation reconciliation; visibility is delayed.
            if self.polls < 3:
                return []
            row = {
                "id": "GTT-VISIBLE", "status": "active",
                "condition": {"tradingsymbol": "TEST", "trigger_values": [95.0, 110.0]},
                "orders": [{"quantity": 2}, {"quantity": 2}],
            }
            return [row, {**row, "id": "GTT-DUP"}] if self.duplicate else [row]

    broker = DelayedGTT()
    protected_position(manager, broker)
    manager._client = broker
    result = manager.place_gtt_oco(
        "TEST", 2, 100, 95, 110,
        caller="gtt_repair", strategy="position_protection",
    )
    assert not result["success"] and "original trigger id" in result["message"].lower()
    assert broker.gtt_place_calls == 1

    monkeypatch.setenv("KITE_LIVE_NAMESPACE_ROOT", str(Path(manager.output_dir) / "duplicate-live"))
    other = KiteOrderManager(output_dir=str(Path(manager.output_dir) / "duplicate"))
    other.dry_run = False
    other.auto_trade = True
    other._sleep = lambda _: None
    other._resolve_exchange_and_tick_size = lambda symbol: ("NSE", 0.05)
    duplicate = DelayedGTT(duplicate=True)
    protected_position(other, duplicate)
    other._client = duplicate
    result = other.place_gtt_oco(
        "TEST", 2, 100, 95, 110,
        caller="gtt_repair", strategy="position_protection",
    )
    assert not result["success"] and "original trigger id" in result["message"].lower()
    assert duplicate.gtt_place_calls == 1


def test_unresolved_gtt_create_is_recovered_after_restart(manager, monkeypatch, tmp_path):
    enable_live(monkeypatch, manager, "gtt_repair", "position_protection")
    broker = Broker()
    protected_position(manager, broker)
    manager._save_positions()
    intent = manager._new_order_intent(
        kind="gtt_create", symbol="TEST", side="SELL", qty=2,
        caller="gtt_repair", strategy="position_protection",
        parameters={"trigger_values": [95.0, 110.0], "product": "CNC"},
    )
    manager._update_intent(intent, "submitted")
    manager._update_intent(intent, "unresolved", error="response lost")
    broker.gtt_rows = [{
        "id": "GTT-RECOVERED", "status": "active",
        "condition": {"tradingsymbol": "TEST", "trigger_values": [95.0, 110.0]},
        "orders": [{"quantity": 2}, {"quantity": 2}],
    }]

    restarted = KiteOrderManager(output_dir=str(tmp_path))
    restarted.dry_run = False
    restarted.auto_trade = True
    context = restarted._reconcile_for_mutation(
        broker, policy="gtt", symbol="TEST", qty=2, product="CNC"
    )
    assert not context["valid"]
    assert "ambiguous" in context["reason"].lower() or "unresolved" in context["reason"].lower()


def test_emergency_close_reconstructs_only_pk_tagged_bot_ownership(manager, monkeypatch):
    enable_live(monkeypatch, manager, "emergency_close", "risk_reducing_exit")
    monkeypatch.setattr("pkscreener.classes.KiteOrderManager._is_market_open", lambda: True)
    manager._resolve_exchange_and_tick_size = lambda symbol: ("NSE", 0.05)
    manager._is_at_circuit_limit = lambda symbol: (False, "")
    broker = Broker()
    broker.position_rows = [{"tradingsymbol": "TEST", "quantity": 2, "product": "MIS"}]
    broker.order_rows = [{
        "order_id": "BUY-1", "tag": "PKknown", "status": "COMPLETE",
        "tradingsymbol": "TEST", "transaction_type": "BUY", "quantity": 2,
        "average_price": 100.0, "product": "MIS",
    }]
    broker.order_history = lambda order_id: [{
        "order_id": order_id, "status": "COMPLETE", "filled_quantity": 1,
        "average_price": 99.0,
    }]
    manager._client = broker
    manager._order_intents["owned"] = {
        "account": manager.account_id, "correlation_id": "owned", "tag": "PKknown", "kind": "entry",
        "symbol": "TEST", "state": "filled", "order_id": "BUY-1",
    }
    manager._append_authoritative_transition({
        "event": "mutation_transition", "state": "reserved",
        "correlation_id": "owned", "intent": manager._order_intents["owned"],
    })
    manager._append_authoritative_transition({
        "event": "mutation_transition", "state": "submitted", "correlation_id": "owned",
    })
    manager._append_authoritative_transition({
        "event": "mutation_transition", "state": "filled", "correlation_id": "owned",
    })
    manager._persist_execution_state()
    result = manager.emergency_close_bot_position("TEST", qty=1)
    assert result.success
    assert broker.place_calls == 1
    assert manager._positions["TEST"].bot_owned

    other = manager.emergency_close_bot_position("UNOWNED", qty=1)
    assert not other.success
    assert broker.place_calls == 1


def test_unprotected_bot_position_blocks_new_entries_and_persists(manager, monkeypatch, tmp_path):
    enable_live(monkeypatch, manager)
    write_entry_evidence(manager)
    pos = TrackedPosition(
        "HELD", 2, 100, stop_loss=95, product_type="MIS",
        bot_owned=True, auto_exit_allowed=True,
    )
    manager._positions["HELD"] = pos
    broker = Broker()
    broker.position_rows = [{"tradingsymbol": "HELD", "quantity": 2, "product": "MIS"}]
    with pytest.raises(BrokerMutationError, match="lacks confirmed active protective"):
        manager._submit_order_once(
            broker, {"tradingsymbol": "TEST", "transaction_type": "BUY", "quantity": 1, "product": "MIS"},
            caller="test_caller", strategy="test_strategy", kind="entry",
        )
    assert broker.place_calls == 0
    restarted = KiteOrderManager(output_dir=str(tmp_path))
    assert "protection_or_exposure" in restarted._critical_states


def test_intent_state_machine_is_durable_at_each_boundary(manager, tmp_path):
    intent = manager._new_order_intent(
        kind="test_mutation", symbol="TEST", side="BUY", qty=1,
        caller="test_caller", strategy="test_strategy", parameters={"x": 1},
    )
    for state in ("reserved", "submitted", "acknowledged", "open", "partially_filled", "filled"):
        if state != "reserved":
            manager._update_intent(intent, state)
        restarted = KiteOrderManager(output_dir=str(tmp_path))
        assert restarted._order_intents[intent["correlation_id"]]["state"] == state
    with pytest.raises(RuntimeError, match="invalid mutation transition"):
        manager._update_intent(intent, "submitted")


def test_risk_reducing_exit_reconciles_but_ignores_entry_evidence(manager, monkeypatch):
    enable_live(monkeypatch, manager, "test_caller", "risk_reducing_exit")
    pos = TrackedPosition(
        "TEST", 2, 100, product_type="MIS", bot_owned=True, auto_exit_allowed=True,
    )
    manager._positions["TEST"] = pos
    broker = Broker()
    broker.position_rows = [{"tradingsymbol": "TEST", "quantity": 2, "product": "MIS"}]
    unrelated = TrackedPosition("OTHER", 1, 100, product_type="MIS", bot_owned=False)
    with pytest.raises(BrokerMutationError, match="bot-owned"):
        manager._submit_order_once(
            broker, {"tradingsymbol": "OTHER", "transaction_type": "SELL", "quantity": 1, "product": "MIS"},
            caller="test_caller", strategy="risk_reducing_exit", kind="exit",
            reconciliation_policy="exit", position=unrelated,
        )
    assert broker.place_calls == 0
    order_id = manager._submit_order_once(
        broker, {"tradingsymbol": "TEST", "transaction_type": "SELL", "quantity": 1, "product": "MIS"},
        caller="test_caller", strategy="risk_reducing_exit", kind="exit",
        reconciliation_policy="exit", position=pos,
    )
    assert order_id == "OID-1"
    assert broker.place_calls == 1


def contract(path, now, **updates):
    payload = {
        "schema_version": 1, "trading_date": now.date().isoformat(),
        "generated_at": now.isoformat(), "complete_through": now.isoformat(),
        "source": "live_portfolio", "account": "main", "sequence": 0,
        "session_state": "open", "zero_trade": True,
        "daily_realized_pnl": 0.0, "consecutive_losses": 0,
    }
    payload.update(updates)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_risk_contract_accepts_zero_trade_preopen_and_holiday(tmp_path, monkeypatch):
    now = datetime(2026, 8, 3, 8, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    path = tmp_path / "risk.json"
    guard = LiveTradingGuardrails(require_valid_evidence=True, evidence_path=str(path))
    monkeypatch.setattr(guard, "_now_ist", lambda: now)
    contract(path, now, session_state="preopen")
    assert guard.evaluate().evidence_valid
    contract(path, now, session_state="holiday", sequence=1)
    assert guard.evaluate().evidence_valid


@pytest.mark.parametrize("updates", [
    {"trading_date": "2026-08-02"},
    {"generated_at": "2026-08-03T10:00:00"},
    {"source": "wrong"},
    {"account": "wrong"},
    {"schema_version": 2},
    {"zero_trade": True, "daily_realized_pnl": -1},
])
def test_risk_contract_rejects_bad_identity_date_timezone_and_schema(tmp_path, monkeypatch, updates):
    now = datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    path = tmp_path / "risk.json"
    guard = LiveTradingGuardrails(require_valid_evidence=True, evidence_path=str(path))
    monkeypatch.setattr(guard, "_now_ist", lambda: now)
    contract(path, now, **updates)
    assert not guard.evaluate().evidence_valid


def test_risk_contract_rejects_stale_or_future_watermark(tmp_path, monkeypatch):
    now = datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    path = tmp_path / "risk.json"
    guard = LiveTradingGuardrails(
        require_valid_evidence=True, evidence_path=str(path), evidence_max_age_seconds=60
    )
    monkeypatch.setattr(guard, "_now_ist", lambda: now)
    contract(path, now, complete_through=(now - timedelta(minutes=2)).isoformat())
    assert not guard.evaluate().evidence_valid
    contract(path, now, generated_at=(now + timedelta(minutes=10)).isoformat())
    assert not guard.evaluate().evidence_valid


def test_grade_b_observation_runs_without_execution_lane(monkeypatch):
    monitor = object.__new__(__import__(
        "pkscreener.classes.SignalMonitor", fromlist=["SignalMonitor"]
    ).SignalMonitorRunner)
    monitor.autotrade_observation_enabled = True
    monitor.autotrade_enabled = False
    monitor.autotrade_real_orders = False
    monitor.autotrade_buy_enabled = False
    monitor.use_live_kite = True
    monitor._is_regular_market_hours = lambda: True
    calls = []
    monitor._register_autotrade_candidates = lambda frame: calls.append("register")
    monitor._process_autotrade_entries = lambda: calls.append("entries")
    monitor._process_autotrade_exits = lambda: calls.append("exits")
    monitor._save_autotrade_state = lambda: calls.append("save")
    monitor._run_autotrade_lane(object())
    assert calls == ["register", "entries", "exits", "save"]


def test_discord_autopilot_configuration_is_scan_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pkscreener.classes.AutoPilotInvestor.Archiver.get_user_data_dir",
        lambda: str(tmp_path),
    )
    investor = AutoPilotInvestor(AutoPilotConfig(
        alert_channels="none", broker_execution_enabled=False
    ))
    assert investor.config.broker_execution_enabled is False
    assert investor._order_manager is None


def test_discord_sensitive_command_authorization_is_strict():
    class User:
        id = 7
    class Interaction:
        user = User()
        channel_id = 11
    interaction = Interaction()
    assert not _strict_discord_authorization(interaction, None, 11)[0]
    assert not _strict_discord_authorization(interaction, 7, None)[0]
    assert not _strict_discord_authorization(interaction, 8, 11)[0]
    assert not _strict_discord_authorization(interaction, 7, 12)[0]
    assert _strict_discord_authorization(interaction, 7, 11)[0]


def test_grade_b_launcher_overrides_hostile_parent_environment(tmp_path):
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "captured.env"
    fake_bash = fake_bin / "bash"
    fake_bash.write_text(
        "#!/bin/sh\nenv | sort > \"$CAPTURE_ENV\"\n",
        encoding="utf-8",
    )
    fake_bash.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:/usr/bin:/bin", "CAPTURE_ENV": str(capture),
        "KITE_EXECUTION_MODE": "live", "KITE_DRY_RUN": "0",
        "KITE_AUTO_TRADE": "1", "KITE_AUTO_BUY": "1", "KITE_AUTO_SELL": "1",
        "SIGNALMONITOR_AUTOTRADE_ENABLED": "1",
        "SIGNALMONITOR_AUTOTRADE_REAL_ORDERS": "1",
        "KITE_LIVE_ACKNOWLEDGEMENT": LIVE_ACKNOWLEDGEMENT_TOKEN,
    })
    subprocess.run(
        ["/bin/bash", str(root / "scripts/start_grade_b_confirmation_autotrade.sh")],
        cwd=root, env=env, check=True, capture_output=True, text=True,
    )
    values = dict(
        line.split("=", 1) for line in capture.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert values["KITE_EXECUTION_MODE"] == "paper"
    assert values["KITE_DRY_RUN"] == "1"
    assert values["KITE_AUTO_TRADE"] == values["KITE_AUTO_BUY"] == values["KITE_AUTO_SELL"] == "0"
    assert values["SIGNALMONITOR_AUTOTRADE_ENABLED"] == "0"
    assert values["SIGNALMONITOR_AUTOTRADE_REAL_ORDERS"] == "0"
    assert values["SIGNALMONITOR_AUTOTRADE_OBSERVATION_ENABLED"] == "1"
    assert values["KITE_LIVE_ACKNOWLEDGEMENT"] == ""


def _concurrent_worker(output_dir, counter, ready, start):
    os.environ.update({
        "KITE_EXECUTION_MODE": "live", "KITE_DRY_RUN": "0", "KITE_AUTO_TRADE": "1",
        "KITE_LIVE_ACKNOWLEDGEMENT": LIVE_ACKNOWLEDGEMENT_TOKEN,
        "KITE_LIVE_CALLER_ALLOWLIST": "test_caller",
        "KITE_LIVE_STRATEGY_ALLOWLIST": "risk_reducing_exit",
        "KITE_LIVE_NAMESPACE_ROOT": str(Path(output_dir) / "live-namespace"),
    })
    manager = KiteOrderManager(output_dir=output_dir)
    manager.dry_run = False
    manager.auto_trade = True
    pos = TrackedPosition("TEST", 1, 100, product_type="MIS", bot_owned=True, auto_exit_allowed=True)
    manager._positions["TEST"] = pos

    class SharedBroker(Broker):
        def place_order(self, **kwargs):
            with counter.get_lock():
                counter.value += 1
            return "OID-SHARED"
    broker = SharedBroker()
    broker.position_rows = [{"tradingsymbol": "TEST", "quantity": 1, "product": "MIS"}]
    ready.set()
    start.wait(5)
    try:
        manager._submit_order_once(
            broker, {"tradingsymbol": "TEST", "transaction_type": "SELL", "quantity": 1, "product": "MIS"},
            caller="test_caller", strategy="risk_reducing_exit", kind="exit",
            reconciliation_policy="exit", position=pos,
        )
    except Exception:
        pass


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires POSIX fork")
def test_cross_process_lock_allows_one_logical_submission(tmp_path):
    ctx = multiprocessing.get_context("fork")
    counter = ctx.Value("i", 0)
    start = ctx.Event()
    ready = [ctx.Event(), ctx.Event()]
    procs = [
        ctx.Process(target=_concurrent_worker, args=(str(tmp_path), counter, ready[i], start))
        for i in range(2)
    ]
    for proc in procs:
        proc.start()
    for event in ready:
        assert event.wait(5)
    start.set()
    for proc in procs:
        proc.join(10)
        assert proc.exitcode == 0
    assert counter.value == 1


def test_direct_broker_mutations_exist_only_in_controlled_adapters():
    root = Path(__file__).parents[1]
    allowed = {
        ("KiteOrderManager.py", "_submit_order_once", "place_order"),
        ("KiteOrderManager.py", "_cancel_order_best_effort", "cancel_order"),
        ("KiteOrderManager.py", "place_gtt_oco", "place_gtt"),
        ("KiteOrderManager.py", "delete_gtt", "delete_gtt"),
    }
    found = set()
    for path in list((root / "pkscreener").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr not in {"place_order", "cancel_order", "place_gtt", "delete_gtt"}:
                    continue
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and node.func.attr == "delete_gtt"
                ):
                    continue
                parent = node
                while parent in parents and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parent = parents[parent]
                found.add((path.name, getattr(parent, "name", ""), node.func.attr))
    assert found == allowed


def test_same_account_uses_shared_namespace_and_different_accounts_are_isolated(tmp_path, monkeypatch):
    root = tmp_path / "live"
    monkeypatch.setenv("KITE_LIVE_NAMESPACE_ROOT", str(root))
    monkeypatch.setenv("KITE_RISK_ACCOUNT", "acct_a")
    a = KiteOrderManager(output_dir=str(tmp_path / "one"))
    monkeypatch.setenv("KITE_RISK_ACCOUNT", "acct_b")
    b = KiteOrderManager(output_dir=str(tmp_path / "two"))
    assert a._execution_lock_file != b._execution_lock_file
    assert a._execution_state_file != b._execution_state_file
    assert a._authoritative_execution_journal != b._authoritative_execution_journal


def test_corrupt_or_truncated_authoritative_journal_blocks_restart(manager):
    manager._new_order_intent(
        kind="order", symbol="TEST", side="BUY", qty=1,
        caller="test_caller", strategy="test_strategy", parameters={}, reserve_daily_quota=True,
    )
    path = manager._authoritative_execution_journal
    path.write_bytes(path.read_bytes().rstrip(b"\n") + b"{truncated")
    restarted = KiteOrderManager(output_dir=manager.output_dir)
    assert restarted._state_load_error


def test_risk_evidence_sequence_reuse_fails_closed(tmp_path, monkeypatch):
    guard = LiveTradingGuardrails(account_name="main", require_valid_evidence=True, evidence_path=str(tmp_path / "risk.json"))
    monkeypatch.setattr(guard, "_now_ist", lambda: datetime(2026, 8, 3, 8, 30, tzinfo=ZoneInfo("Asia/Kolkata")))
    guard.write_evidence_contract(daily_realized_pnl=0, consecutive_losses=0, sequence=2, session_state="preopen", zero_trade=True)
    assert guard.evaluate().evidence_valid
    guard.write_evidence_contract(daily_realized_pnl=-1, consecutive_losses=1, sequence=2, session_state="closed", zero_trade=False)
    status = guard.evaluate()
    assert not status.evidence_valid and any("reused" in reason for reason in status.evidence_reasons)


def test_duplicate_symbol_entry_has_no_broker_call(manager, monkeypatch):
    monkeypatch.setattr("pkscreener.classes.KiteOrderManager._is_market_open", lambda: True)
    manager.auto_trade = True
    manager._positions["TEST"] = TrackedPosition("TEST", 1, 100, product_type="MIS", bot_owned=True)
    result = manager.place_buy_order("TEST", 1, 100, caller="test_caller", strategy="test_strategy")
    assert not result.success and "one position" in result.message.lower()


def test_protective_failure_records_offline_alert_without_broker_mutation(manager, monkeypatch):
    import pkscreener.classes.KiteOrderManager as kite_module

    kite_module._discord_alerted_today.clear()
    _drain_test_notification_events()
    enable_live(monkeypatch, manager, "protective_stop", "position_protection")
    broker = Broker()
    pos = TrackedPosition("TEST", 1, 100, stop_loss=95, product_type="MIS", bot_owned=True, auto_exit_allowed=True)
    manager._positions["TEST"] = pos
    assert not manager._place_protective_stop_order(pos, broker)
    events = _drain_test_notification_events()
    assert len(events) == 1
    assert "PROTECTIVE STOP FAILED — TEST" in events[0]["message"]
    assert (broker.place_calls, broker.cancel_calls, broker.gtt_place_calls, broker.gtt_delete_calls) == (0, 0, 0, 0)


def test_parent_discord_environment_is_ignored_and_alert_stays_in_memory(monkeypatch):
    import pkscreener.classes.KiteOrderManager as kite_module

    kite_module._discord_alerted_today.clear()
    _drain_test_notification_events()
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://production.invalid/webhook")
    _send_discord_alert("test notification", dedup_key="offline-parent-env")
    assert _drain_test_notification_events() == [{
        "channel": "discord", "message": "test notification", "dedup_key": "offline-parent-env",
    }]


def test_production_notification_environment_is_sanitized_before_imports():
    from pkscreener.classes.KiteOrderManager import _get_secret

    assert "PKSCREENER_TEST_OFFLINE" in os.environ
    assert not any(name.startswith(("DISCORD_", "TELEGRAM_", "SLACK_", "SMTP_")) for name in os.environ)
    assert isinstance(SANITIZED_ENVIRONMENT_NAMES, tuple)
    assert _get_secret("DISCORD_WEBHOOK_URL", "") == ""


def test_socket_network_attempt_fails_immediately():
    import socket

    with pytest.raises(OfflineNetworkAttempt):
        socket.create_connection(("example.invalid", 443))


def test_repeated_protective_failures_only_record_expected_events(manager, monkeypatch):
    import pkscreener.classes.KiteOrderManager as kite_module

    kite_module._discord_alerted_today.clear()
    _drain_test_notification_events()
    enable_live(monkeypatch, manager, "protective_stop", "position_protection")
    broker = Broker()
    pos = TrackedPosition("TEST", 1, 100, stop_loss=95, product_type="MIS", bot_owned=True, auto_exit_allowed=True)
    manager._positions["TEST"] = pos
    assert not manager._place_protective_stop_order(pos, broker)
    assert not manager._place_protective_stop_order(pos, broker)
    events = _drain_test_notification_events()
    assert len(events) == 1 and "TEST" in events[0]["message"]


def test_offline_notifier_creates_no_background_worker():
    import threading

    before = {thread.ident for thread in threading.enumerate() if thread.is_alive()}
    _send_discord_alert("worker check", dedup_key="offline-worker-check")
    after = {thread.ident for thread in threading.enumerate() if thread.is_alive()}
    assert after == before
    _drain_test_notification_events()

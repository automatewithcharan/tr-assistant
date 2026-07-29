"""Paper-only portfolio that can replace a weak incumbent with a stronger rank.

The policy is deliberately frozen and causal.  It consumes score-qualified
entry events from the prior-session stop-avoidance model, values both legs from
the current five-level order book, and has no alert or broker-order surface.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from pkscreener.classes.OrderBookExecution import (
    EXECUTION_SCHEMA_VERSION,
    affordable_market_fill,
    depth_json,
    depth_levels,
    market_fill,
    parse_depth_json,
)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "1", "1.0", "true", "yes", "y", "on",
    }


def _timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return pd.NaT
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp


POLICY_VERSION = "rank_displacement_v1_20260729"
FROZEN_POLICY = {
    "source": "prior_session_rank_migration_stop_avoidance",
    "score_threshold": 0.78,
    "score_advantage": 0.05,
    "minimum_hold_minutes": 3.0,
    "maximum_incumbent_gross_return_pct": 0.50,
    "target_pct": 1.50,
    "stop_pct": 1.00,
    "horizon_minutes": 60.0,
    "fee_pct": 0.10,
    "max_trades_per_day": 5,
    "capital": 500_000.0,
    "minimum_capital_utilization_pct": 95.0,
        "maximum_candidate_age_seconds": 30.0,
        "required_recorder_generation": 6,
}
POLICY_SHA256 = hashlib.sha256(
    json.dumps(FROZEN_POLICY, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
).hexdigest()


class FrozenRankDisplacementPaperTrader:
    """Run one depth-executable slot with a fixed causal displacement rule."""

    POLICY = FROZEN_POLICY

    TRADE_COLUMNS = [
        "date", "trade_id", "symbol", "source_event_id", "entry_time",
        "exit_time", "recorder_generation", "exchange",
        "model_trained_through", "model_source_sha256",
        "entry_score", "replacement_score", "entry_price",
        "exit_price", "quantity", "deployed_capital", "capital_utilization_pct",
        "target_price", "stop_price", "exit_reason", "gross_pnl", "costs",
        "net_pnl", "net_return_pct", "hold_minutes", "replaced_by_symbol",
        "execution_schema_version", "entry_depth_json", "exit_depth_json",
        "entry_depth_fillable", "exit_depth_fillable", "entry_depth_vwap",
        "exit_depth_vwap", "closed", "paper_only", "alerts_allowed",
        "real_orders_allowed", "policy_version", "policy_sha256",
    ]

    EVENT_COLUMNS = [
        "date", "when", "event", "symbol", "other_symbol", "score",
        "other_score", "price", "gross_return_pct", "reason", "trade_id",
        "source_event_id", "paper_only", "alerts_allowed",
        "real_orders_allowed", "policy_version", "policy_sha256",
    ]

    def __init__(
        self,
        output_dir: str | Path,
        *,
        persist: bool = True,
        root_name: str = "frozen_stop_displacement_paper",
    ) -> None:
        self.output_dir = Path(output_dir) / root_name
        self.persist = bool(persist)
        self._date = ""
        self.trades: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self._traded_symbols: set[str] = set()
        self._seen_event_ids: set[str] = set()
        self._first_heartbeat_at = ""
        self._last_heartbeat_at = ""
        self._heartbeat_cycles = 0
        if self.persist:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def process(
        self,
        candidate_events: List[Dict[str, Any]],
        ticks: Dict[str, Dict[str, Any]],
        *,
        now: datetime,
    ) -> List[Dict[str, Any]]:
        """Advance the frozen portfolio and return entry/exit audit events."""
        today = now.strftime("%Y-%m-%d")
        self._roll_day(today)
        emitted = self._update_natural_exit(ticks, now)
        candidates = self._eligible_candidates(candidate_events, now)
        incumbent = self._open_trade()

        if incumbent is not None and candidates:
            replacement = candidates[0]
            displaced = self._try_displacement(
                incumbent, replacement, ticks, now
            )
            if displaced:
                emitted.extend(displaced)
                incumbent = self._open_trade()

        if incumbent is None and candidates and len(self.trades) < int(
            self.POLICY["max_trades_per_day"]
        ):
            entry = self._enter(candidates[0], now)
            if entry is not None:
                emitted.append(entry)

        self._record_heartbeat(
            now,
            tick_symbols=len(ticks or {}),
            candidate_events=len(candidate_events or []),
            eligible_candidates=len(candidates),
            emitted_events=len(emitted),
        )
        self._persist()
        return emitted

    def _eligible_candidates(
        self,
        candidate_events: List[Dict[str, Any]],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for raw in candidate_events or []:
            symbol = str(raw.get("Symbol", "") or "").strip().upper()
            event_id = str(raw.get("EventId", "") or "").strip()
            score = _float(raw.get("ExpectedReturnPct"))
            source = str(raw.get("Source", "") or "").strip()
            generation = int(_float(raw.get("RecorderGeneration")))
            exchange = str(raw.get("Exchange", "") or "").strip().upper()
            trained_through = str(
                raw.get("ModelTrainedThrough", "") or ""
            ).strip()
            model_hash = str(
                raw.get("ModelSourceSha256", "") or ""
            ).strip()
            entry_at = _timestamp(raw.get("EntryAt"))
            age_seconds = (
                (_timestamp(now) - entry_at).total_seconds()
                if not pd.isna(entry_at)
                else float("inf")
            )
            if (
                not symbol
                or not event_id
                or symbol in self._traded_symbols
                or event_id in self._seen_event_ids
                or source != self.POLICY["source"]
                or generation
                != int(self.POLICY["required_recorder_generation"])
                or exchange != "NSE"
                or not trained_through
                or trained_through >= self._date
                or not model_hash
                or score < float(self.POLICY["score_threshold"])
                or pd.isna(entry_at)
                or entry_at > _timestamp(now)
                or not _truthy(raw.get("EntryDepthFillable"))
                or age_seconds > float(
                    self.POLICY["maximum_candidate_age_seconds"]
                )
            ):
                continue
            entry_levels = parse_depth_json(raw.get("EntryDepthJson"))
            entry_fill = affordable_market_fill(
                entry_levels,
                float(self.POLICY["capital"]),
            )
            quantity = int(entry_fill["filled_quantity"])
            vwap = _float(entry_fill["vwap"])
            if (
                quantity < 1
                or vwap <= 0
                or not entry_fill["fillable"]
            ):
                continue
            deployed = quantity * vwap
            utilization = deployed / float(self.POLICY["capital"]) * 100.0
            if utilization < float(
                self.POLICY["minimum_capital_utilization_pct"]
            ):
                continue
            row = dict(raw)
            row["_symbol"] = symbol
            row["_event_id"] = event_id
            row["_score"] = score
            row["_quantity"] = quantity
            row["_entry_vwap"] = vwap
            row["_entry_depth_json"] = depth_json(entry_levels)
            row["_generation"] = generation
            row["_exchange"] = exchange
            row["_trained_through"] = trained_through
            row["_model_hash"] = model_hash
            row["_deployed"] = deployed
            row["_utilization"] = utilization
            rows.append(row)
        rows.sort(key=lambda row: (-row["_score"], row["_symbol"]))
        return rows

    def _try_displacement(
        self,
        incumbent: Dict[str, Any],
        replacement: Dict[str, Any],
        ticks: Dict[str, Dict[str, Any]],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        if len(self.trades) >= int(self.POLICY["max_trades_per_day"]):
            return []
        held = (
            _timestamp(now) - _timestamp(incumbent["entry_time"])
        ).total_seconds() / 60.0
        if held < float(self.POLICY["minimum_hold_minutes"]):
            return []
        incumbent_score = _float(incumbent.get("entry_score"))
        replacement_score = _float(replacement.get("_score"))
        if replacement_score < incumbent_score + float(
            self.POLICY["score_advantage"]
        ):
            return []
        exit_fill, exit_levels = self._exit_fill(incumbent, ticks)
        if not exit_fill["fillable"]:
            return []
        exit_price = _float(exit_fill["vwap"])
        gross_return = (
            exit_price / _float(incumbent["entry_price"]) - 1.0
        ) * 100.0
        if gross_return > float(
            self.POLICY["maximum_incumbent_gross_return_pct"]
        ):
            return []

        exit_event = self._close(
            incumbent,
            now=now,
            exit_price=exit_price,
            exit_reason="rank_displaced",
            exit_levels=exit_levels,
            replacement=replacement,
        )
        entry_event = self._enter(replacement, now)
        return [
            event for event in (exit_event, entry_event) if event is not None
        ]

    def _update_natural_exit(
        self,
        ticks: Dict[str, Dict[str, Any]],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        trade = self._open_trade()
        if trade is None:
            return []
        exit_fill, exit_levels = self._exit_fill(trade, ticks)
        if not exit_fill["fillable"]:
            return []
        exit_price = _float(exit_fill["vwap"])
        held = (
            _timestamp(now) - _timestamp(trade["entry_time"])
        ).total_seconds() / 60.0
        minute = now.hour * 60 + now.minute
        reason = ""
        if exit_price <= _float(trade["stop_price"]):
            reason = "stop"
        elif exit_price >= _float(trade["target_price"]):
            reason = "target"
        elif held >= float(self.POLICY["horizon_minutes"]):
            reason = "horizon"
        elif minute >= 15 * 60 + 15:
            reason = "eod"
        if not reason:
            return []
        event = self._close(
            trade,
            now=now,
            exit_price=exit_price,
            exit_reason=reason,
            exit_levels=exit_levels,
        )
        return [event] if event is not None else []

    @staticmethod
    def _exit_fill(
        trade: Dict[str, Any],
        ticks: Dict[str, Dict[str, Any]],
    ) -> tuple[Dict[str, Any], List[Dict[str, float]]]:
        symbol = str(trade.get("symbol", "") or "").strip().upper()
        tick = ticks.get(symbol, {}) if isinstance(ticks, dict) else {}
        levels = depth_levels(tick, "buy")
        return market_fill(levels, int(_float(trade.get("quantity")))), levels

    def _enter(
        self,
        candidate: Dict[str, Any],
        now: datetime,
    ) -> Optional[Dict[str, Any]]:
        if len(self.trades) >= int(self.POLICY["max_trades_per_day"]):
            return None
        symbol = str(candidate["_symbol"])
        event_id = str(candidate["_event_id"])
        quantity = int(candidate["_quantity"])
        price = float(candidate["_entry_vwap"])
        deployed = price * quantity
        trade_id = f"{self._date}:{len(self.trades) + 1}:{symbol}"
        trade = {
            "date": self._date,
            "trade_id": trade_id,
            "symbol": symbol,
            "source_event_id": event_id,
            "entry_time": now.isoformat(timespec="seconds"),
            "exit_time": "",
            "recorder_generation": int(candidate["_generation"]),
            "exchange": str(candidate["_exchange"]),
            "model_trained_through": str(candidate["_trained_through"]),
            "model_source_sha256": str(candidate["_model_hash"]),
            "entry_score": round(float(candidate["_score"]), 6),
            "replacement_score": 0.0,
            "entry_price": round(price, 6),
            "exit_price": 0.0,
            "quantity": quantity,
            "deployed_capital": round(deployed, 2),
            "capital_utilization_pct": round(
                float(candidate["_utilization"]), 4
            ),
            "target_price": round(
                price * (1.0 + float(self.POLICY["target_pct"]) / 100.0), 6
            ),
            "stop_price": round(
                price * (1.0 - float(self.POLICY["stop_pct"]) / 100.0), 6
            ),
            "exit_reason": "",
            "gross_pnl": 0.0,
            "costs": 0.0,
            "net_pnl": 0.0,
            "net_return_pct": 0.0,
            "hold_minutes": 0.0,
            "replaced_by_symbol": "",
            "execution_schema_version": EXECUTION_SCHEMA_VERSION,
            "entry_depth_json": str(candidate["_entry_depth_json"]),
            "exit_depth_json": "",
            "entry_depth_fillable": True,
            "exit_depth_fillable": False,
            "entry_depth_vwap": round(price, 6),
            "exit_depth_vwap": 0.0,
            "closed": False,
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
            "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA256,
        }
        self.trades.append(trade)
        self._traded_symbols.add(symbol)
        self._seen_event_ids.add(event_id)
        event = self._event(
            "entry", trade, now, price=price, reason="slot_available"
        )
        return event

    def _close(
        self,
        trade: Dict[str, Any],
        *,
        now: datetime,
        exit_price: float,
        exit_reason: str,
        exit_levels: List[Dict[str, float]],
        replacement: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if _truthy(trade.get("closed")):
            return None
        quantity = int(_float(trade.get("quantity")))
        deployed = _float(trade.get("deployed_capital"))
        gross = (float(exit_price) - _float(trade.get("entry_price"))) * quantity
        costs = deployed * float(self.POLICY["fee_pct"]) / 100.0
        net = gross - costs
        held = (
            _timestamp(now) - _timestamp(trade["entry_time"])
        ).total_seconds() / 60.0
        replacement_symbol = (
            str(replacement.get("_symbol", "")) if replacement else ""
        )
        replacement_score = (
            _float(replacement.get("_score")) if replacement else 0.0
        )
        trade.update(
            {
                "exit_time": now.isoformat(timespec="seconds"),
                "exit_price": round(float(exit_price), 6),
                "replacement_score": round(replacement_score, 6),
                "exit_reason": str(exit_reason),
                "gross_pnl": round(gross, 2),
                "costs": round(costs, 2),
                "net_pnl": round(net, 2),
                "net_return_pct": round(
                    net / deployed * 100.0 if deployed > 0 else 0.0, 6
                ),
                "hold_minutes": round(held, 3),
                "replaced_by_symbol": replacement_symbol,
                "exit_depth_json": depth_json(exit_levels),
                "exit_depth_fillable": True,
                "exit_depth_vwap": round(float(exit_price), 6),
                "closed": True,
            }
        )
        return self._event(
            "exit",
            trade,
            now,
            other_symbol=replacement_symbol,
            other_score=replacement_score,
            price=exit_price,
            reason=exit_reason,
        )

    def _event(
        self,
        event: str,
        trade: Dict[str, Any],
        now: datetime,
        *,
        other_symbol: str = "",
        other_score: float = 0.0,
        price: float = 0.0,
        reason: str = "",
    ) -> Dict[str, Any]:
        entry = _float(trade.get("entry_price"))
        gross_return = (float(price) / entry - 1.0) * 100.0 if entry else 0.0
        row = {
            "date": self._date,
            "when": now.isoformat(timespec="seconds"),
            "event": str(event),
            "symbol": str(trade.get("symbol", "")),
            "other_symbol": str(other_symbol),
            "score": _float(trade.get("entry_score")),
            "other_score": float(other_score),
            "price": round(float(price), 6),
            "gross_return_pct": round(gross_return, 6),
            "reason": str(reason),
            "trade_id": str(trade.get("trade_id", "")),
            "source_event_id": str(trade.get("source_event_id", "")),
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
            "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA256,
        }
        self.events.append(row)
        return row

    def _open_trade(self) -> Optional[Dict[str, Any]]:
        return next(
            (row for row in self.trades if not _truthy(row.get("closed"))),
            None,
        )

    def _roll_day(self, today: str) -> None:
        if self._date == today:
            return
        self._date = today
        self.trades = []
        self.events = []
        self._traded_symbols = set()
        self._seen_event_ids = set()
        self._first_heartbeat_at = ""
        self._last_heartbeat_at = ""
        self._heartbeat_cycles = 0
        if not self.persist:
            return
        trade_path = self.output_dir / f"trades_{today}.csv"
        event_path = self.output_dir / f"events_{today}.csv"
        try:
            if trade_path.exists():
                frame = pd.read_csv(trade_path).fillna("")
                self.trades = frame.to_dict(orient="records")
                self._traded_symbols = {
                    str(row.get("symbol", "")).strip().upper()
                    for row in self.trades
                    if str(row.get("symbol", "")).strip()
                }
                self._seen_event_ids = {
                    str(row.get("source_event_id", "")).strip()
                    for row in self.trades
                    if str(row.get("source_event_id", "")).strip()
                }
            if event_path.exists():
                self.events = pd.read_csv(event_path).fillna("").to_dict(
                    orient="records"
                )
        except (OSError, pd.errors.EmptyDataError):
            self.trades = []
            self.events = []
            self._traded_symbols = set()
            self._seen_event_ids = set()

    def _record_heartbeat(
        self,
        now: datetime,
        *,
        tick_symbols: int,
        candidate_events: int,
        eligible_candidates: int,
        emitted_events: int,
    ) -> None:
        when = now.isoformat(timespec="seconds")
        if when == self._last_heartbeat_at:
            return
        if not self._first_heartbeat_at:
            self._first_heartbeat_at = when
        self._last_heartbeat_at = when
        self._heartbeat_cycles += 1
        if not self.persist:
            return
        path = self.output_dir / f"heartbeats_{self._date}.csv"
        row = {
            "date": self._date,
            "when": when,
            "tick_symbols": int(tick_symbols),
            "candidate_events": int(candidate_events),
            "eligible_candidates": int(eligible_candidates),
            "emitted_events": int(emitted_events),
            "open_position": self._open_trade() is not None,
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
            "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA256,
        }
        pd.DataFrame([row]).to_csv(
            path,
            mode="a",
            header=not path.exists(),
            index=False,
        )

    def _persist(self) -> None:
        if not self.persist:
            return
        pd.DataFrame(self.trades, columns=self.TRADE_COLUMNS).to_csv(
            self.output_dir / f"trades_{self._date}.csv", index=False
        )
        pd.DataFrame(self.trades, columns=self.TRADE_COLUMNS).to_csv(
            self.output_dir / "trades_latest.csv", index=False
        )
        pd.DataFrame(self.events, columns=self.EVENT_COLUMNS).to_csv(
            self.output_dir / f"events_{self._date}.csv", index=False
        )
        status = {
            "status": "collecting_frozen_rank_displacement_paper",
            "date": self._date,
            "policy": self.POLICY,
            "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA256,
            "trades": len(self.trades),
            "closed_trades": sum(
                _truthy(row.get("closed")) for row in self.trades
            ),
            "displacements": sum(
                str(row.get("exit_reason", "")) == "rank_displaced"
                for row in self.trades
            ),
            "net_pnl": round(
                sum(_float(row.get("net_pnl")) for row in self.trades), 2
            ),
            "execution_schema_version": EXECUTION_SCHEMA_VERSION,
            "requires_five_level_fillability": True,
            "heartbeat_cycles": self._heartbeat_cycles,
            "first_heartbeat_at": self._first_heartbeat_at,
            "last_heartbeat_at": self._last_heartbeat_at,
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        (self.output_dir / "status.json").write_text(
            json.dumps(status, indent=2) + "\n", encoding="utf-8"
        )

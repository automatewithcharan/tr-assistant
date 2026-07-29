"""Paper-only post-margin entry evidence for fixed MIS exposure variants."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from pkscreener.classes.OrderBookExecution import (
    EXECUTION_SCHEMA_VERSION,
    affordable_market_fill,
    depth_json,
    depth_levels,
)


class MarginQualifiedLeverageObserver:
    """Arm frozen signals, then capture depth strictly after a margin quote."""

    DEFAULT_MULTIPLIERS = (1.25, 1.50)

    def __init__(
        self,
        output_dir: str | Path,
        *,
        lane_capital: float = 250_000.0,
        multipliers: Iterable[float] = DEFAULT_MULTIPLIERS,
        candidate_ttl_seconds: float = 180.0,
        quote_ttl_seconds: float = 300.0,
        persist: bool = True,
    ) -> None:
        self.root = Path(output_dir) / "margin_qualified_leverage"
        self.state_path = self.root / "state.json"
        self.status_path = self.root / "status.json"
        self.lane_capital = max(1.0, float(lane_capital))
        self.multipliers = tuple(
            sorted(
                {
                    round(float(value), 4)
                    for value in multipliers
                    if float(value) > 1.0
                }
            )
        )
        if not self.multipliers:
            raise ValueError("at least one leverage multiplier above 1 is required")
        self.candidate_ttl_seconds = max(
            1.0, float(candidate_ttl_seconds)
        )
        self.quote_ttl_seconds = max(1.0, float(quote_ttl_seconds))
        self.persist = bool(persist)
        self._date = ""
        self._pending: dict[str, dict[str, Any]] = {}
        self._quotes: dict[str, dict[str, Any]] = {}
        self._emitted: set[str] = set()
        self._expired = 0
        self._depth_waits = 0
        if self.persist:
            self.root.mkdir(parents=True, exist_ok=True)
            self._load_state()

    def process(
        self,
        candidate_events: Iterable[dict[str, Any]],
        margin_rows: Iterable[dict[str, Any]],
        ticks: dict[str, dict[str, Any]],
        *,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Return leverage entry events from a strictly later depth snapshot."""
        self._roll_date(now)
        self._register_candidates(candidate_events, now)
        self._register_quotes(margin_rows)
        output = self._capture_due_entries(ticks, now)
        self._save_state(now)
        self._write_status(now)
        return output

    def _roll_date(self, now: datetime) -> None:
        date = now.strftime("%Y-%m-%d")
        if date == self._date:
            return
        self._date = date
        self._pending = {}
        self._quotes = {}
        self._emitted = set()
        self._expired = 0
        self._depth_waits = 0

    def _register_candidates(
        self,
        events: Iterable[dict[str, Any]],
        now: datetime,
    ) -> None:
        for raw in events or []:
            if str(raw.get("event", "") or "") != "entry":
                continue
            event_id = str(raw.get("EventId", "") or "")
            symbol = str(raw.get("Symbol", "") or "").strip().upper()
            source = str(raw.get("Source", "") or "").strip()
            exchange = str(raw.get("Exchange", "") or "").strip().upper()
            if not event_id or not symbol or not source or exchange != "NSE":
                continue
            key = f"{source}|{event_id}"
            if key in self._pending:
                continue
            if all(
                self._variant_key(key, multiplier) in self._emitted
                for multiplier in self.multipliers
            ):
                continue
            payload = dict(raw)
            payload["_candidate_key"] = key
            payload["_observed_ts"] = now.timestamp()
            payload["_expires_ts"] = (
                now.timestamp() + self.candidate_ttl_seconds
            )
            self._pending[key] = payload

    def _register_quotes(
        self,
        rows: Iterable[dict[str, Any]],
    ) -> None:
        for row in rows or []:
            if str(row.get("Status", "") or "").lower() != "quoted":
                continue
            symbol = str(row.get("Symbol", "") or "").strip().upper()
            quoted_at = self._datetime(row.get("QuotedAt"))
            margin_pct = self._number(row.get("MarginPct"))
            if not symbol or quoted_at is None or margin_pct <= 0:
                continue
            self._quotes[symbol] = {
                "quoted_at": quoted_at.isoformat(timespec="seconds"),
                "quoted_ts": quoted_at.timestamp(),
                "margin_pct": margin_pct,
                "effective_leverage": self._number(
                    row.get("EffectiveLeverage")
                ),
                "quote_price": self._number(row.get("Price")),
            }

    def _capture_due_entries(
        self,
        ticks: dict[str, dict[str, Any]],
        now: datetime,
    ) -> list[dict[str, Any]]:
        normalized_ticks = {
            str(symbol).strip().upper(): tick
            for symbol, tick in (ticks or {}).items()
            if str(symbol).strip() and isinstance(tick, dict)
        }
        rows: list[dict[str, Any]] = []
        for key, candidate in list(self._pending.items()):
            if now.timestamp() > self._number(candidate.get("_expires_ts")):
                self._expired += 1
                self._pending.pop(key, None)
                continue
            symbol = str(candidate.get("Symbol", "") or "").strip().upper()
            quote = self._quotes.get(symbol)
            if not quote:
                continue
            quote_ts = self._number(quote.get("quoted_ts"))
            quote_age = now.timestamp() - quote_ts
            if quote_age <= 0 or quote_age > self.quote_ttl_seconds:
                continue
            entry_depth = depth_levels(
                normalized_ticks.get(symbol, {}),
                "sell",
            )
            if not entry_depth:
                self._depth_waits += 1
                continue
            emitted_for_candidate = 0
            for multiplier in self.multipliers:
                variant_key = self._variant_key(key, multiplier)
                if variant_key in self._emitted:
                    emitted_for_candidate += 1
                    continue
                notional_budget = self.lane_capital * multiplier
                fill = affordable_market_fill(
                    entry_depth,
                    notional_budget,
                )
                quantity = int(fill["filled_quantity"])
                deployed = float(fill["notional"])
                margin_required = (
                    deployed * self._number(quote.get("margin_pct")) / 100.0
                )
                if (
                    quantity < 1
                    or deployed <= 0
                    or margin_required > self.lane_capital
                ):
                    self._depth_waits += 1
                    continue
                rows.append(
                    self._entry_row(
                        candidate,
                        quote,
                        fill,
                        entry_depth,
                        multiplier,
                        margin_required,
                        now,
                    )
                )
                self._emitted.add(variant_key)
                emitted_for_candidate += 1
            if emitted_for_candidate == len(self.multipliers):
                self._pending.pop(key, None)
        return rows

    def _entry_row(
        self,
        candidate: dict[str, Any],
        quote: dict[str, Any],
        fill: dict[str, Any],
        entry_depth: list[dict[str, float]],
        multiplier: float,
        margin_required: float,
        now: datetime,
    ) -> dict[str, Any]:
        original_event_id = str(candidate.get("EventId", "") or "")
        source = str(candidate.get("Source", "") or "")
        suffix = f"mis_{multiplier:.2f}x".replace(".", "_")
        entry_vwap = float(fill["vwap"])
        quantity = int(fill["filled_quantity"])
        return {
            "event": "entry",
            "EventId": f"{original_event_id}|{suffix}|post_margin",
            "OriginalEventId": original_event_id,
            "Date": self._date,
            "EntryAt": now.isoformat(timespec="seconds"),
            "EntryTs": now.timestamp(),
            "RecorderGeneration": int(
                self._number(candidate.get("RecorderGeneration"))
            ),
            "Exchange": "NSE",
            "Symbol": str(candidate.get("Symbol", "") or "").upper(),
            "Cohort": str(candidate.get("Cohort", "") or ""),
            "Source": f"{source}:{suffix}",
            "EntryAsk": entry_vwap,
            "EntryBid": self._number(candidate.get("EntryBid")),
            "EntryAskQty": int(entry_depth[0]["quantity"]),
            "RequestedQuantity": quantity,
            "EntryTopLevelFillable": bool(
                int(entry_depth[0]["quantity"]) >= quantity
            ),
            "ExecutionSchemaVersion": EXECUTION_SCHEMA_VERSION,
            "EntryDepthJson": depth_json(entry_depth),
            "EntryDepthQty": int(fill["visible_quantity"]),
            "EntryDepthFillable": bool(fill["fillable"]),
            "EntryDepthVwap": entry_vwap,
            "Quantity": quantity,
            "Capital": round(float(fill["notional"]), 2),
            "DayChangePct": self._number(candidate.get("DayChangePct")),
            "DayChangeRank": self._number(candidate.get("DayChangeRank")),
            "RankImprovement": self._number(candidate.get("RankImprovement")),
            "RankVelocityPerMinute": self._number(
                candidate.get("RankVelocityPerMinute")
            ),
            "BuyerSellerRatio": self._number(
                candidate.get("BuyerSellerRatio")
            ),
            "EntrySpreadBps": self._number(
                candidate.get("EntrySpreadBps")
            ),
            "ExpectedReturnPct": self._number(
                candidate.get("ExpectedReturnPct")
            ),
            "MoveBand": str(candidate.get("MoveBand", "") or ""),
            "ModelTrainedThrough": str(
                candidate.get("ModelTrainedThrough", "") or ""
            ),
            "ModelSourceSha256": str(
                candidate.get("ModelSourceSha256", "") or ""
            ),
            "MarginQuotedAt": str(quote.get("quoted_at", "") or ""),
            "MarginPct": round(
                self._number(quote.get("margin_pct")), 6
            ),
            "RequiredMarginEstimate": round(margin_required, 2),
            "NotionalMultiplier": multiplier,
            "BaseLaneCapital": self.lane_capital,
        }

    @staticmethod
    def _variant_key(candidate_key: str, multiplier: float) -> str:
        return f"{candidate_key}|{multiplier:.4f}"

    def _save_state(self, now: datetime) -> None:
        if not self.persist:
            return
        payload = {
            "date": self._date,
            "updated_at": now.isoformat(timespec="seconds"),
            "pending": self._pending,
            "quotes": self._quotes,
            "emitted": sorted(self._emitted),
            "expired": self._expired,
            "depth_waits": self._depth_waits,
        }
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.state_path)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(
                self.state_path.read_text(encoding="utf-8")
            )
        except Exception:
            return
        self._date = str(payload.get("date", "") or "")
        self._pending = {
            str(key): value
            for key, value in (payload.get("pending", {}) or {}).items()
            if isinstance(value, dict)
        }
        self._quotes = {
            str(key): value
            for key, value in (payload.get("quotes", {}) or {}).items()
            if isinstance(value, dict)
        }
        self._emitted = set(
            str(value) for value in payload.get("emitted", [])
        )
        self._expired = int(self._number(payload.get("expired")))
        self._depth_waits = int(self._number(payload.get("depth_waits")))

    def _write_status(self, now: datetime) -> None:
        if not self.persist:
            return
        payload = {
            "status": "recording_post_margin_five_level_entries",
            "date": self._date,
            "updated_at": now.isoformat(timespec="seconds"),
            "lane_capital": self.lane_capital,
            "multipliers": list(self.multipliers),
            "candidate_ttl_seconds": self.candidate_ttl_seconds,
            "quote_ttl_seconds": self.quote_ttl_seconds,
            "pending_candidates": len(self._pending),
            "causal_quotes": len(self._quotes),
            "emitted_variants": len(self._emitted),
            "expired_candidates": self._expired,
            "depth_waits": self._depth_waits,
            "execution_schema_version": EXECUTION_SCHEMA_VERSION,
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        temporary = self.status_path.with_name(
            f".{self.status_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.status_path)

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value or ""))
        except (TypeError, ValueError):
            return None
        return parsed

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

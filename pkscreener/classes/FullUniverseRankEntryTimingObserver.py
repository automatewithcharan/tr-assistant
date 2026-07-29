"""Paper-only entry timing variants for full-universe rank candidates."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger
from pkscreener.classes.FullUniverseRankPaperObserver import (
    FullUniverseRankPaperObserver,
)
from pkscreener.classes.LiveFeatureRecorder import LiveFeatureRecorder


ENTRY_VARIANTS = {
    "dip_0_25": {"mode": "dip", "offset_pct": 0.25, "entry_window_minutes": 30},
    "dip_0_50": {"mode": "dip", "offset_pct": 0.50, "entry_window_minutes": 30},
    "dip_1_00": {"mode": "dip", "offset_pct": 1.00, "entry_window_minutes": 30},
    "confirm_0_25": {"mode": "confirm", "offset_pct": 0.25, "entry_window_minutes": 15},
    "confirm_0_50": {"mode": "confirm", "offset_pct": 0.50, "entry_window_minutes": 15},
    "pullback_0_50_reclaim_0_25": {
        "mode": "pullback_reclaim",
        "offset_pct": 0.50,
        "reclaim_pct": 0.25,
        "entry_window_minutes": 30,
    },
}


class FullUniverseRankEntryTimingObserver:
    """Track alternative exact entry triggers, then one fixed exit contract."""

    REGISTRATION_COLUMNS = [
        "TimingId", "EventId", "Variant", "Date", "SignalAt",
        "RecorderGeneration", "Exchange", "Symbol", "Cohort", "Source",
        "SignalAsk",
        "TriggerPrice", "ExpiresAt",
    ]
    FILL_COLUMNS = [
        "TimingId", "EventId", "Variant", "Date", "SignalAt", "EntryAt",
        "RecorderGeneration", "Exchange", "Symbol", "Cohort", "Source",
        "SignalAsk",
        "EntryAsk", "EntryBid", "EntryAskQty", "RequestedQuantity",
        "EntryTopLevelFillable", "Quantity", "Capital", "TargetPrice",
        "StopPrice", "EntryDelayMinutes",
    ]
    NONFILL_COLUMNS = [
        "TimingId", "EventId", "Variant", "Date", "SignalAt", "ResolvedAt",
        "RecorderGeneration", "Exchange", "Symbol", "SignalAsk", "Reason",
        "BestAskSeen", "WorstAskSeen", "MaxAskQtySeen",
    ]
    OUTCOME_COLUMNS = [
        "TimingId", "EventId", "Variant", "Date", "SignalAt", "EntryAt",
        "RecorderGeneration", "Exchange", "ExitAt", "Symbol", "Cohort", "Source",
        "SignalAsk", "EntryAsk", "ExitBid", "EntryAskQty",
        "RequestedQuantity", "EntryTopLevelFillable", "Quantity", "ExitReason",
        "EntryDelayMinutes", "HoldMinutes", "GrossReturnPct", "StressPct",
        "NetReturnPct", "GrossPnl", "StressCost", "NetPnl", "TargetPrice",
        "StopPrice",
    ]

    def __init__(
        self,
        output_dir: str,
        capital: float = 500_000.0,
        target_pct: float = 1.5,
        stop_pct: float = 1.0,
        horizon_minutes: float = 60.0,
        stress_pct: float = 0.10,
        max_confirmation_slippage_pct: float = 0.20,
        variants: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> None:
        self.root = Path(output_dir) / "full_universe_rank_entry_timing"
        self.state_path = self.root / "state.json"
        self.status_path = self.root / "status.json"
        self.capital = max(0.0, float(capital))
        self.target_pct = max(0.1, float(target_pct))
        self.stop_pct = max(0.1, float(stop_pct))
        self.horizon_minutes = max(1.0, float(horizon_minutes))
        self.stress_pct = max(0.0, float(stress_pct))
        self.max_confirmation_slippage_pct = max(
            0.0, float(max_confirmation_slippage_pct)
        )
        self.variants = variants or ENTRY_VARIANTS
        self.waiting: Dict[str, Dict[str, Any]] = {}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.seen_events: set = set()
        self.state_date = ""
        self._load_state()

    def process(
        self,
        paper_events: List[Dict[str, Any]],
        ticks: Dict[str, Dict[str, Any]],
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        now_dt = now or PKDateUtilities.currentDateTime()
        self._roll_day(now_dt.strftime("%Y-%m-%d"))
        normalized_ticks = {
            str(symbol).strip().upper(): tick
            for symbol, tick in (ticks or {}).items()
            if str(symbol).strip() and isinstance(tick, dict)
        }
        emitted = self._close_positions(normalized_ticks, now_dt)
        emitted.extend(self._process_waiting(normalized_ticks, now_dt))
        if now_dt.hour * 60 + now_dt.minute < 15 * 60 + 25:
            self._register(paper_events or [], now_dt)
        self._save_state(now_dt)
        self._write_status(now_dt)
        return emitted

    def _register(self, events: List[Dict[str, Any]], now: datetime) -> None:
        rows = []
        for event in events:
            if str(event.get("event", "")) != "entry":
                continue
            event_id = str(event.get("EventId", "") or "")
            if not event_id or event_id in self.seen_events:
                continue
            signal_ask = self._number(event.get("EntryAsk"))
            if signal_ask <= 0:
                continue
            self.seen_events.add(event_id)
            for name, variant in self.variants.items():
                timing_id = f"{event_id}|{name}"
                mode = str(variant["mode"])
                offset = float(variant["offset_pct"])
                trigger = signal_ask * (
                    1.0 - offset / 100.0 if mode != "confirm" else 1.0 + offset / 100.0
                )
                expires_ts = float(event.get("EntryTs", now.timestamp())) + float(
                    variant["entry_window_minutes"]
                ) * 60.0
                waiting = {
                    "TimingId": timing_id,
                    "EventId": event_id,
                    "Variant": name,
                    "Mode": mode,
                    "Date": str(event.get("Date", now.strftime("%Y-%m-%d"))),
                    "SignalAt": str(event.get("EntryAt", now.strftime("%Y-%m-%d %H:%M:%S"))),
                    "SignalTs": float(event.get("EntryTs", now.timestamp())),
                    "RecorderGeneration": int(
                        self._number(event.get("RecorderGeneration"))
                    ),
                    "Exchange": str(
                        event.get("Exchange", "") or ""
                    ).strip().upper(),
                    "ExpiresTs": expires_ts,
                    "Symbol": str(event.get("Symbol", "") or "").upper(),
                    "Cohort": str(event.get("Cohort", "") or ""),
                    "Source": str(event.get("Source", "") or ""),
                    "SignalAsk": signal_ask,
                    "TriggerPrice": round(trigger, 2),
                    "ReclaimPct": float(variant.get("reclaim_pct", 0.0)),
                    "Phase": "waiting_pullback" if mode == "pullback_reclaim" else "waiting_trigger",
                    "TroughAsk": signal_ask,
                    "BestAskSeen": signal_ask,
                    "WorstAskSeen": signal_ask,
                    "MaxAskQtySeen": 0,
                }
                self.waiting[timing_id] = waiting
                rows.append(
                    {
                        "TimingId": timing_id,
                        "EventId": event_id,
                        "Variant": name,
                        "Date": waiting["Date"],
                        "SignalAt": waiting["SignalAt"],
                        "RecorderGeneration": int(
                            self._number(waiting.get("RecorderGeneration"))
                        ),
                        "Exchange": str(
                            waiting.get("Exchange", "") or ""
                        ).strip().upper(),
                        "Symbol": waiting["Symbol"],
                        "Cohort": waiting["Cohort"],
                        "Source": waiting["Source"],
                        "SignalAsk": signal_ask,
                        "TriggerPrice": waiting["TriggerPrice"],
                        "ExpiresAt": datetime.fromtimestamp(expires_ts).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        self._append_rows(
            self.root / f"registrations_{now:%Y-%m-%d}.csv",
            self.REGISTRATION_COLUMNS,
            rows,
        )

    def _process_waiting(
        self, ticks: Dict[str, Dict[str, Any]], now: datetime
    ) -> List[Dict[str, Any]]:
        emitted = []
        fill_rows = []
        nonfill_rows = []
        eod = now.hour * 60 + now.minute >= 15 * 60 + 25
        for timing_id, candidate in list(self.waiting.items()):
            bid, ask = LiveFeatureRecorder.best_bid_ask(
                ticks.get(candidate["Symbol"], {})
            )
            if ask > 0:
                candidate["BestAskSeen"] = min(float(candidate["BestAskSeen"]), ask)
                candidate["WorstAskSeen"] = max(float(candidate["WorstAskSeen"]), ask)
            expired = now.timestamp() >= float(candidate["ExpiresTs"])
            if eod or expired:
                row = {
                    "TimingId": timing_id,
                    "EventId": candidate["EventId"],
                    "Variant": candidate["Variant"],
                    "Date": candidate["Date"],
                    "SignalAt": candidate["SignalAt"],
                    "ResolvedAt": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "RecorderGeneration": int(
                        self._number(candidate.get("RecorderGeneration"))
                    ),
                    "Exchange": str(
                        candidate.get("Exchange", "") or ""
                    ).strip().upper(),
                    "Symbol": candidate["Symbol"],
                    "SignalAsk": candidate["SignalAsk"],
                    "Reason": "eod_unfilled" if eod else "entry_window_expired",
                    "BestAskSeen": candidate["BestAskSeen"],
                    "WorstAskSeen": candidate["WorstAskSeen"],
                    "MaxAskQtySeen": int(
                        self._number(candidate.get("MaxAskQtySeen"))
                    ),
                }
                nonfill_rows.append(row)
                emitted.append({"event": "entry_unfilled", **row})
                del self.waiting[timing_id]
                continue
            if bid <= 0 or ask <= bid:
                continue
            _, ask_qty = LiveFeatureRecorder.best_bid_ask_quantities(
                ticks.get(candidate["Symbol"], {})
            )
            candidate["MaxAskQtySeen"] = max(
                int(self._number(candidate.get("MaxAskQtySeen"))),
                int(ask_qty),
            )
            entry_price = 0.0
            mode = candidate["Mode"]
            trigger = float(candidate["TriggerPrice"])
            if mode == "dip" and ask <= trigger:
                # A resting limit may fill anywhere below its limit. Use the
                # limit itself, not the better observed ask, to stay conservative.
                entry_price = trigger
            elif mode == "confirm" and ask >= trigger:
                maximum = trigger * (1.0 + self.max_confirmation_slippage_pct / 100.0)
                if ask <= maximum:
                    entry_price = ask
            elif mode == "pullback_reclaim":
                if candidate["Phase"] == "waiting_pullback" and ask <= trigger:
                    candidate["Phase"] = "waiting_reclaim"
                    candidate["TroughAsk"] = ask
                elif candidate["Phase"] == "waiting_reclaim":
                    candidate["TroughAsk"] = min(float(candidate["TroughAsk"]), ask)
                    reclaim = float(candidate["TroughAsk"]) * (
                        1.0 + float(candidate["ReclaimPct"]) / 100.0
                    )
                    if ask >= reclaim and ask <= float(candidate["SignalAsk"]):
                        entry_price = ask
            if entry_price <= 0:
                continue
            quantity = int(self.capital // entry_price)
            if quantity < 1:
                continue
            if ask_qty < quantity:
                continue
            delay = max(0.0, (now.timestamp() - float(candidate["SignalTs"])) / 60.0)
            position = {
                **candidate,
                "RecorderGeneration": int(
                    self._number(candidate.get("RecorderGeneration"))
                ),
                "Exchange": str(
                    candidate.get("Exchange", "") or ""
                ).strip().upper(),
                "EntryAt": now.strftime("%Y-%m-%d %H:%M:%S"),
                "EntryTs": now.timestamp(),
                "EntryAsk": round(entry_price, 2),
                "EntryBid": round(bid, 2),
                "EntryAskQty": int(ask_qty),
                "RequestedQuantity": quantity,
                "EntryTopLevelFillable": True,
                "Quantity": quantity,
                "Capital": round(quantity * entry_price, 2),
                "TargetPrice": round(entry_price * (1.0 + self.target_pct / 100.0), 2),
                "StopPrice": round(entry_price * (1.0 - self.stop_pct / 100.0), 2),
                "EntryDelayMinutes": round(delay, 3),
            }
            self.positions[timing_id] = position
            del self.waiting[timing_id]
            row = {column: position.get(column, "") for column in self.FILL_COLUMNS}
            fill_rows.append(row)
            emitted.append({"event": "entry_filled", **row})
        self._append_rows(
            self.root / f"fills_{now:%Y-%m-%d}.csv", self.FILL_COLUMNS, fill_rows
        )
        self._append_rows(
            self.root / f"nonfills_{now:%Y-%m-%d}.csv",
            self.NONFILL_COLUMNS,
            nonfill_rows,
        )
        return emitted

    def _close_positions(
        self, ticks: Dict[str, Dict[str, Any]], now: datetime
    ) -> List[Dict[str, Any]]:
        rows = []
        eod = now.hour * 60 + now.minute >= 15 * 60 + 25
        for timing_id, position in list(self.positions.items()):
            bid, _ = LiveFeatureRecorder.best_bid_ask(
                ticks.get(position["Symbol"], {})
            )
            if bid <= 0:
                continue
            hold = max(0.0, (now.timestamp() - float(position["EntryTs"])) / 60.0)
            reason = ""
            if bid >= float(position["TargetPrice"]):
                reason = "target"
            elif bid <= float(position["StopPrice"]):
                reason = "stop"
            elif eod:
                reason = "eod"
            elif hold >= self.horizon_minutes:
                reason = "timeout"
            if not reason:
                continue
            ask = float(position["EntryAsk"])
            quantity = int(position["Quantity"])
            gross_return = (bid - ask) * 100.0 / ask
            gross_pnl = (bid - ask) * quantity
            stress_cost = ask * quantity * self.stress_pct / 100.0
            row = {
                "TimingId": timing_id,
                "EventId": position["EventId"],
                "Variant": position["Variant"],
                "Date": position["Date"],
                "SignalAt": position["SignalAt"],
                "EntryAt": position["EntryAt"],
                "RecorderGeneration": int(
                    self._number(position.get("RecorderGeneration"))
                ),
                "Exchange": str(
                    position.get("Exchange", "") or ""
                ).strip().upper(),
                "ExitAt": now.strftime("%Y-%m-%d %H:%M:%S"),
                "Symbol": position["Symbol"],
                "Cohort": position["Cohort"],
                "Source": position["Source"],
                "SignalAsk": position["SignalAsk"],
                "EntryAsk": ask,
                "ExitBid": round(bid, 2),
                "EntryAskQty": int(
                    self._number(position.get("EntryAskQty"))
                ),
                "RequestedQuantity": int(
                    self._number(
                        position.get("RequestedQuantity", quantity)
                    )
                ),
                "EntryTopLevelFillable": bool(
                    position.get("EntryTopLevelFillable", False)
                ),
                "Quantity": quantity,
                "ExitReason": reason,
                "EntryDelayMinutes": position["EntryDelayMinutes"],
                "HoldMinutes": round(hold, 3),
                "GrossReturnPct": round(gross_return, 5),
                "StressPct": self.stress_pct,
                "NetReturnPct": round(gross_return - self.stress_pct, 5),
                "GrossPnl": round(gross_pnl, 2),
                "StressCost": round(stress_cost, 2),
                "NetPnl": round(gross_pnl - stress_cost, 2),
                "TargetPrice": position["TargetPrice"],
                "StopPrice": position["StopPrice"],
            }
            rows.append(row)
            del self.positions[timing_id]
        self._append_rows(
            self.root / f"outcomes_{now:%Y-%m-%d}.csv", self.OUTCOME_COLUMNS, rows
        )
        return [{"event": "timing_exit", **row} for row in rows]

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.state_date = str(payload.get("date", ""))
            self.waiting = {
                str(key): value for key, value in (payload.get("waiting", {}) or {}).items()
                if isinstance(value, dict)
            }
            self.positions = {
                str(key): value for key, value in (payload.get("positions", {}) or {}).items()
                if isinstance(value, dict)
            }
            self.seen_events = set(str(value) for value in payload.get("seen_events", []))
        except Exception as exc:
            default_logger().debug(f"Could not load entry-timing state: {exc}", exc_info=True)

    def _roll_day(self, day: str) -> None:
        if self.state_date == day:
            return
        self.state_date = day
        self.waiting = {
            key: value for key, value in self.waiting.items()
            if str(value.get("Date", "")) == day
        }
        self.positions = {
            key: value for key, value in self.positions.items()
            if str(value.get("Date", "")) == day
        }
        self.seen_events = {
            event_id for event_id in self.seen_events if event_id.startswith(day + ":")
        }

    def _save_state(self, now: datetime) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "waiting": self.waiting,
            "positions": self.positions,
            "seen_events": sorted(self.seen_events),
            "variants": self.variants,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _write_status(self, now: datetime) -> None:
        fills = self._read_frame(self.root / f"fills_{now:%Y-%m-%d}.csv")
        nonfills = self._read_frame(self.root / f"nonfills_{now:%Y-%m-%d}.csv")
        outcomes = self._read_frame(self.root / f"outcomes_{now:%Y-%m-%d}.csv")
        payload = {
            "status": "recording_exact_entry_timing_matrix",
            "date": now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "variants": len(self.variants),
            "entry_labels": len(self.seen_events),
            "waiting": len(self.waiting),
            "open_positions": len(self.positions),
            "fills": int(len(fills)),
            "nonfills": int(len(nonfills)),
            "closed_positions": int(len(outcomes)),
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        self.status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _append_rows(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        FullUniverseRankPaperObserver._ensure_csv_schema(path, columns)
        pd.DataFrame(rows, columns=columns).to_csv(
            path, mode="a", header=not path.exists(), index=False
        )

    @staticmethod
    def _read_frame(path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _number(value: Any) -> float:
        try:
            if value is None or pd.isna(value):
                return 0.0
            return float(value)
        except Exception:
            return 0.0

"""Paper-only exit contract matrix for full-universe rank entries."""

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


CONTRACTS = {
    "t0_5_s0_5_h15": {"target_pct": 0.5, "stop_pct": 0.5, "horizon_minutes": 15},
    "t0_8_s0_5_h30": {"target_pct": 0.8, "stop_pct": 0.5, "horizon_minutes": 30},
    "t1_0_s0_5_h30": {"target_pct": 1.0, "stop_pct": 0.5, "horizon_minutes": 30},
    "t1_0_s1_0_h60": {"target_pct": 1.0, "stop_pct": 1.0, "horizon_minutes": 60},
    "t1_5_s1_0_h60": {"target_pct": 1.5, "stop_pct": 1.0, "horizon_minutes": 60},
    "t2_0_s1_0_h60": {"target_pct": 2.0, "stop_pct": 1.0, "horizon_minutes": 60},
    "t1_5_s1_5_h60": {"target_pct": 1.5, "stop_pct": 1.5, "horizon_minutes": 60},
    "t3_0_s1_5_h90": {"target_pct": 3.0, "stop_pct": 1.5, "horizon_minutes": 90},
    "t5_0_s2_0_h120": {"target_pct": 5.0, "stop_pct": 2.0, "horizon_minutes": 120},
}


class FullUniverseRankContractMatrixObserver:
    """Track fixed exit contracts independently from the same paper entry."""

    REGISTRATION_COLUMNS = [
        "EventId", "Date", "EntryAt", "RecorderGeneration", "Exchange", "Symbol",
        "Cohort", "Source",
        "EntryAsk", "EntryBid", "EntryAskQty", "RequestedQuantity",
        "EntryTopLevelFillable", "Quantity", "Capital", "DayChangePct",
        "DayChangeRank", "RankImprovement", "RankVelocityPerMinute",
        "BuyerSellerRatio", "EntrySpreadBps",
    ]
    OUTCOME_COLUMNS = [
        "MatrixId", "EventId", "Contract", "Date", "EntryAt",
        "RecorderGeneration", "Exchange", "ExitAt", "Symbol", "Cohort", "Source",
        "EntryAsk", "ExitBid", "EntryAskQty", "ExitBidQty",
        "EntryTopLevelFillable", "ExitTopLevelFillable", "Quantity",
        "TargetPct", "StopPct", "HorizonMinutes", "ExitReason", "HoldMinutes",
        "GrossReturnPct", "StressPct", "NetReturnPct", "GrossPnl",
        "StressCost", "NetPnl",
    ]

    def __init__(
        self,
        output_dir: str,
        stress_pct: float = 0.10,
        contracts: Optional[Dict[str, Dict[str, float]]] = None,
        root_name: str = "full_universe_rank_contract_matrix",
        metadata_columns: Optional[List[str]] = None,
    ) -> None:
        self.root = Path(output_dir) / str(root_name)
        self.state_path = self.root / "state.json"
        self.status_path = self.root / "status.json"
        self.stress_pct = max(0.0, float(stress_pct))
        self.contracts = contracts or CONTRACTS
        self.metadata_columns = list(dict.fromkeys(metadata_columns or []))
        self.registration_columns = (
            self.REGISTRATION_COLUMNS + self.metadata_columns
        )
        self.outcome_columns = self.OUTCOME_COLUMNS + self.metadata_columns
        self.active: Dict[str, Dict[str, Any]] = {}
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
        day = now_dt.strftime("%Y-%m-%d")
        self._roll_day(day)
        normalized_ticks = {
            str(symbol).strip().upper(): tick
            for symbol, tick in (ticks or {}).items()
            if str(symbol).strip() and isinstance(tick, dict)
        }
        outcomes = self._close_eligible(normalized_ticks, now_dt)
        if now_dt.hour * 60 + now_dt.minute < 15 * 60 + 25:
            self._register_entries(paper_events or [], now_dt)
        self._save_state(now_dt)
        self._write_status(now_dt)
        return outcomes

    def _register_entries(self, paper_events: List[Dict[str, Any]], now: datetime) -> None:
        registrations = []
        for event in paper_events:
            if str(event.get("event", "")) != "entry":
                continue
            event_id = str(event.get("EventId", "") or "")
            if not event_id or event_id in self.seen_events:
                continue
            entry_ask = self._number(event.get("EntryAsk"))
            quantity = int(self._number(event.get("Quantity")))
            if entry_ask <= 0 or quantity < 1:
                continue
            registration = {
                column: event.get(column, "")
                for column in self.registration_columns
            }
            registrations.append(registration)
            self.seen_events.add(event_id)
            for name, contract in self.contracts.items():
                matrix_id = f"{event_id}|{name}"
                target_pct = float(contract["target_pct"])
                stop_pct = float(contract["stop_pct"])
                self.active[matrix_id] = {
                    "MatrixId": matrix_id,
                    "EventId": event_id,
                    "Contract": name,
                    "Date": str(event.get("Date", now.strftime("%Y-%m-%d"))),
                    "EntryAt": str(event.get("EntryAt", now.strftime("%Y-%m-%d %H:%M:%S"))),
                    "EntryTs": float(event.get("EntryTs", now.timestamp())),
                    "RecorderGeneration": int(
                        self._number(event.get("RecorderGeneration"))
                    ),
                    "Exchange": str(
                        event.get("Exchange", "") or ""
                    ).strip().upper(),
                    "Symbol": str(event.get("Symbol", "") or "").upper(),
                    "Cohort": str(event.get("Cohort", "") or ""),
                    "Source": str(event.get("Source", "") or ""),
                    "EntryAsk": entry_ask,
                    "EntryAskQty": int(
                        self._number(event.get("EntryAskQty"))
                    ),
                    "EntryTopLevelFillable": self._truthy(
                        event.get("EntryTopLevelFillable")
                    ),
                    "Quantity": quantity,
                    "TargetPct": target_pct,
                    "StopPct": stop_pct,
                    "HorizonMinutes": float(contract["horizon_minutes"]),
                    "TargetPrice": round(entry_ask * (1.0 + target_pct / 100.0), 2),
                    "StopPrice": round(entry_ask * (1.0 - stop_pct / 100.0), 2),
                }
                for column in self.metadata_columns:
                    self.active[matrix_id][column] = event.get(column, "")
        self._append_rows(
            self.root / f"registrations_{now:%Y-%m-%d}.csv",
            self.registration_columns,
            registrations,
        )

    def _close_eligible(
        self, ticks: Dict[str, Dict[str, Any]], now: datetime
    ) -> List[Dict[str, Any]]:
        rows = []
        eod = now.hour * 60 + now.minute >= 15 * 60 + 25
        for matrix_id, entry in list(self.active.items()):
            exit_tick = ticks.get(entry["Symbol"], {})
            bid, _ = LiveFeatureRecorder.best_bid_ask(exit_tick)
            bid_qty, _ = LiveFeatureRecorder.best_bid_ask_quantities(exit_tick)
            if bid <= 0:
                continue
            hold = max(0.0, (now.timestamp() - float(entry["EntryTs"])) / 60.0)
            reason = ""
            if bid >= float(entry["TargetPrice"]):
                reason = "target"
            elif bid <= float(entry["StopPrice"]):
                reason = "stop"
            elif eod:
                reason = "eod"
            elif hold >= float(entry["HorizonMinutes"]):
                reason = "timeout"
            if not reason:
                continue
            ask = float(entry["EntryAsk"])
            quantity = int(entry["Quantity"])
            gross_return = (bid - ask) * 100.0 / ask
            gross_pnl = (bid - ask) * quantity
            stress_cost = ask * quantity * self.stress_pct / 100.0
            row = {
                "MatrixId": matrix_id,
                "EventId": entry["EventId"],
                "Contract": entry["Contract"],
                "Date": entry["Date"],
                "EntryAt": entry["EntryAt"],
                "RecorderGeneration": int(
                    self._number(entry.get("RecorderGeneration"))
                ),
                "Exchange": str(
                    entry.get("Exchange", "") or ""
                ).strip().upper(),
                "ExitAt": now.strftime("%Y-%m-%d %H:%M:%S"),
                "Symbol": entry["Symbol"],
                "Cohort": entry["Cohort"],
                "Source": entry["Source"],
                "EntryAsk": ask,
                "ExitBid": round(bid, 2),
                "EntryAskQty": int(
                    self._number(entry.get("EntryAskQty"))
                ),
                "ExitBidQty": int(bid_qty),
                "EntryTopLevelFillable": self._truthy(
                    entry.get("EntryTopLevelFillable")
                ),
                "ExitTopLevelFillable": bool(
                    bid_qty > 0 and bid_qty >= quantity
                ),
                "Quantity": quantity,
                "TargetPct": entry["TargetPct"],
                "StopPct": entry["StopPct"],
                "HorizonMinutes": entry["HorizonMinutes"],
                "ExitReason": reason,
                "HoldMinutes": round(hold, 3),
                "GrossReturnPct": round(gross_return, 5),
                "StressPct": self.stress_pct,
                "NetReturnPct": round(gross_return - self.stress_pct, 5),
                "GrossPnl": round(gross_pnl, 2),
                "StressCost": round(stress_cost, 2),
                "NetPnl": round(gross_pnl - stress_cost, 2),
            }
            for column in self.metadata_columns:
                row[column] = entry.get(column, "")
            rows.append(row)
            del self.active[matrix_id]
        self._append_rows(
            self.root / f"outcomes_{now:%Y-%m-%d}.csv",
            self.outcome_columns,
            rows,
        )
        return [{"event": "contract_exit", **row} for row in rows]

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.state_date = str(payload.get("date", ""))
            self.active = {
                str(key): value for key, value in (payload.get("active", {}) or {}).items()
                if isinstance(value, dict)
            }
            self.seen_events = set(str(value) for value in payload.get("seen_events", []))
        except Exception as exc:
            default_logger().debug(f"Could not load contract-matrix state: {exc}", exc_info=True)

    def _roll_day(self, day: str) -> None:
        if self.state_date == day:
            return
        self.state_date = day
        self.active = {
            key: value for key, value in self.active.items()
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
            "active": self.active,
            "seen_events": sorted(self.seen_events),
            "contracts": self.contracts,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _write_status(self, now: datetime) -> None:
        outcomes = self._read_frame(self.root / f"outcomes_{now:%Y-%m-%d}.csv")
        payload = {
            "status": "recording_exact_exit_contract_matrix",
            "date": now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "contracts": len(self.contracts),
            "entry_labels": len(self.seen_events),
            "active_contracts": len(self.active),
            "closed_contracts": int(len(outcomes)),
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

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {
            "1", "true", "yes", "y"
        }

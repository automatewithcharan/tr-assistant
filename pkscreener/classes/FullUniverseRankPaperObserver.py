"""Executable paper outcomes for full-universe rank candidates.

This observer labels live rank cohorts using displayed ask-to-bid prices. It is
intentionally broker-free: it writes research evidence and cannot place orders
or send alerts.
"""

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger
from pkscreener.classes.LiveFeatureRecorder import LiveFeatureRecorder


class FullUniverseRankPaperObserver:
    """Persist ask-entry/bid-exit outcomes for predefined rank cohorts."""

    ENTRY_COLUMNS = [
        "EventId", "Date", "EntryAt", "RecorderGeneration", "Exchange",
        "InstrumentToken", "Symbol", "Sector",
        "Cohort", "Source",
        "EntryAsk", "EntryBid", "EntrySpreadBps", "Quantity", "Capital",
        "TargetPrice", "StopPrice", "TargetPct", "StopPct", "HorizonMinutes",
        "StressPct", "OpenChangePct", "PullbackFromHighPct", "DayChangePct",
        "UniverseSize", "DayChangeRank", "DayChangeRankPct", "PreviousDayChangeRank",
        "RankImprovement", "RankVelocityPerMinute", "DayChangeDeltaPct",
        "EnteredTop100", "EnteredTop50", "RapidRankClimber", "Volume",
        "TradedValue", "BuyQty", "SellQty", "BuyerSellerRatio",
        "BuyerSellerMedianWindow", "BuyerSellerMinWindow", "OrderBookStable",
        "SpreadOk", "SectorAvgChangePct", "SectorBreadthPct", "SectorTop2Count",
        "SectorTop5Count", "MarketBreadthPct", "RecordReason",
    ]
    OUTCOME_COLUMNS = [
        "EventId", "Date", "EntryAt", "ExitAt", "Symbol", "Cohort", "Source",
        "EntryAsk", "ExitBid", "Quantity", "ExitReason", "HoldMinutes",
        "GrossReturnPct", "StressPct", "NetReturnPct", "GrossPnl", "StressCost",
        "NetPnl", "TargetPrice", "StopPrice",
    ]

    def __init__(
        self,
        output_dir: str,
        capital: float = 500000.0,
        target_pct: float = 1.5,
        stop_pct: float = 1.0,
        horizon_minutes: float = 60.0,
        stress_pct: float = 0.10,
        min_price: float = 50.0,
        min_traded_value: float = 30000000.0,
        max_spread_bps: float = 20.0,
    ) -> None:
        self.root = Path(output_dir) / "full_universe_rank_paper"
        self.capital = max(0.0, float(capital))
        self.target_pct = max(0.1, float(target_pct))
        self.stop_pct = max(0.1, float(stop_pct))
        self.horizon_minutes = max(1.0, float(horizon_minutes))
        self.stress_pct = max(0.0, float(stress_pct))
        self.min_price = max(0.0, float(min_price))
        self.min_traded_value = max(0.0, float(min_traded_value))
        self.max_spread_bps = max(0.0, float(max_spread_bps))
        self.state_path = self.root / "state.json"
        self.status_path = self.root / "status.json"
        self.active: Dict[str, Dict[str, Any]] = {}
        self.seen: set = set()
        self.state_date = ""
        self._load_state()

    def process(
        self,
        feature_rows: Optional[pd.DataFrame],
        ticks: Dict[str, Dict[str, Any]],
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Observe exits on every tick cycle and entries on recorder snapshots."""
        now_dt = now or PKDateUtilities.currentDateTime()
        day = now_dt.strftime("%Y-%m-%d")
        self._roll_day(day)
        normalized_ticks = {
            str(symbol).strip().upper(): tick
            for symbol, tick in (ticks or {}).items()
            if str(symbol).strip() and isinstance(tick, dict)
        }
        events = self._close_eligible(normalized_ticks, now_dt)
        if now_dt.hour * 60 + now_dt.minute < 15 * 60 + 25:
            events.extend(self._register_entries(feature_rows, normalized_ticks, now_dt))
        self._save_state(now_dt)
        self._write_status(now_dt)
        return events

    def _register_entries(
        self,
        frame: Optional[pd.DataFrame],
        ticks: Dict[str, Dict[str, Any]],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        if frame is None or frame.empty:
            return []
        created: List[Dict[str, Any]] = []
        entry_rows: List[Dict[str, Any]] = []
        for row in frame.to_dict(orient="records"):
            source = self._source(row)
            if not source:
                continue
            symbol = str(row.get("Symbol", "") or "").strip().upper()
            tick = ticks.get(symbol, {})
            bid, ask = LiveFeatureRecorder.best_bid_ask(tick)
            spread_bps = self._spread_bps(bid, ask)
            price = self._number(row.get("LastPrice"))
            traded_value = self._number(row.get("TradedValue"))
            day_change = self._number(row.get("DayChangePct"))
            exchange = str(row.get("Exchange", "") or "").strip().upper()
            if not (
                symbol
                and exchange == "NSE"
                and price >= self.min_price
                and traded_value >= self.min_traded_value
                and 0.5 <= day_change <= 5.0
                and bid > 0.0
                and ask > bid
                and 0.0 < spread_bps <= self.max_spread_bps
                and self._truthy(row.get("SpreadOk"))
            ):
                continue
            cohort = self._cohort(source, row)
            recorder_generation = int(
                self._number(row.get("RecorderGeneration"))
            )
            event_id = (
                f"{now:%Y-%m-%d}:{symbol}:g{recorder_generation}:{cohort}"
            )
            if event_id in self.seen:
                continue
            quantity = int(self.capital // ask) if ask > 0 else 0
            if quantity < 1:
                continue
            entry = {
                "EventId": event_id,
                "Date": now.strftime("%Y-%m-%d"),
                "EntryAt": now.strftime("%Y-%m-%d %H:%M:%S"),
                "EntryTs": now.timestamp(),
                "RecorderGeneration": recorder_generation,
                "Exchange": exchange,
                "InstrumentToken": int(
                    self._number(row.get("InstrumentToken"))
                ),
                "Symbol": symbol,
                "Sector": str(row.get("Sector", "") or "UNKNOWN"),
                "Cohort": cohort,
                "Source": source,
                "EntryAsk": round(ask, 2),
                "EntryBid": round(bid, 2),
                "EntrySpreadBps": round(spread_bps, 3),
                "Quantity": quantity,
                "Capital": round(quantity * ask, 2),
                "TargetPrice": round(ask * (1.0 + self.target_pct / 100.0), 2),
                "StopPrice": round(ask * (1.0 - self.stop_pct / 100.0), 2),
                "TargetPct": self.target_pct,
                "StopPct": self.stop_pct,
                "HorizonMinutes": self.horizon_minutes,
                "StressPct": self.stress_pct,
                "OpenChangePct": round(self._number(row.get("OpenChangePct")), 3),
                "PullbackFromHighPct": round(self._number(row.get("PullbackFromHighPct")), 3),
                "DayChangePct": round(day_change, 3),
                "UniverseSize": int(self._number(row.get("UniverseSize"))),
                "DayChangeRank": int(self._number(row.get("DayChangeRank"))),
                "DayChangeRankPct": round(self._number(row.get("DayChangeRankPct")), 3),
                "PreviousDayChangeRank": int(self._number(row.get("PreviousDayChangeRank"))),
                "RankImprovement": int(self._number(row.get("RankImprovement"))),
                "RankVelocityPerMinute": round(self._number(row.get("RankVelocityPerMinute")), 3),
                "DayChangeDeltaPct": round(self._number(row.get("DayChangeDeltaPct")), 3),
                "EnteredTop100": self._truthy(row.get("EnteredTop100")),
                "EnteredTop50": self._truthy(row.get("EnteredTop50")),
                "RapidRankClimber": self._truthy(row.get("RapidRankClimber")),
                "Volume": int(self._number(row.get("Volume"))),
                "TradedValue": round(traded_value, 2),
                "BuyQty": int(self._number(row.get("BuyQty"))),
                "SellQty": int(self._number(row.get("SellQty"))),
                "BuyerSellerRatio": round(self._number(row.get("BuyerSellerRatio")), 3),
                "BuyerSellerMedianWindow": round(self._number(row.get("BuyerSellerMedianWindow")), 3),
                "BuyerSellerMinWindow": round(self._number(row.get("BuyerSellerMinWindow")), 3),
                "OrderBookStable": self._truthy(row.get("OrderBookStable")),
                "SpreadOk": self._truthy(row.get("SpreadOk")),
                "SectorAvgChangePct": round(self._number(row.get("SectorAvgChangePct")), 3),
                "SectorBreadthPct": round(self._number(row.get("SectorBreadthPct")), 3),
                "SectorTop2Count": int(self._number(row.get("SectorTop2Count"))),
                "SectorTop5Count": int(self._number(row.get("SectorTop5Count"))),
                "MarketBreadthPct": round(self._number(row.get("MarketBreadthPct")), 3),
                "RecordReason": str(row.get("RecordReason", "") or ""),
            }
            self.active[event_id] = entry
            self.seen.add(event_id)
            entry_rows.append({column: entry.get(column, "") for column in self.ENTRY_COLUMNS})
            created.append({"event": "entry", **entry})
        self._append_rows(self._entries_path(now), self.ENTRY_COLUMNS, entry_rows)
        return created

    def _close_eligible(
        self, ticks: Dict[str, Dict[str, Any]], now: datetime
    ) -> List[Dict[str, Any]]:
        closed: List[Dict[str, Any]] = []
        outcome_rows: List[Dict[str, Any]] = []
        eod = now.hour * 60 + now.minute >= 15 * 60 + 25
        for event_id, entry in list(self.active.items()):
            bid, _ = LiveFeatureRecorder.best_bid_ask(ticks.get(entry["Symbol"], {}))
            if bid <= 0:
                continue
            hold_minutes = max(0.0, (now.timestamp() - float(entry["EntryTs"])) / 60.0)
            reason = ""
            if bid >= float(entry["TargetPrice"]):
                reason = "target"
            elif bid <= float(entry["StopPrice"]):
                reason = "stop"
            elif eod:
                reason = "eod"
            elif hold_minutes >= self.horizon_minutes:
                reason = "timeout"
            if not reason:
                continue
            ask = float(entry["EntryAsk"])
            quantity = int(entry["Quantity"])
            gross_return = (bid - ask) * 100.0 / ask
            gross_pnl = (bid - ask) * quantity
            stress_cost = ask * quantity * self.stress_pct / 100.0
            outcome = {
                "EventId": event_id,
                "Date": entry["Date"],
                "EntryAt": entry["EntryAt"],
                "ExitAt": now.strftime("%Y-%m-%d %H:%M:%S"),
                "Symbol": entry["Symbol"],
                "Cohort": entry["Cohort"],
                "Source": entry["Source"],
                "EntryAsk": ask,
                "ExitBid": round(bid, 2),
                "Quantity": quantity,
                "ExitReason": reason,
                "HoldMinutes": round(hold_minutes, 3),
                "GrossReturnPct": round(gross_return, 5),
                "StressPct": self.stress_pct,
                "NetReturnPct": round(gross_return - self.stress_pct, 5),
                "GrossPnl": round(gross_pnl, 2),
                "StressCost": round(stress_cost, 2),
                "NetPnl": round(gross_pnl - stress_cost, 2),
                "TargetPrice": entry["TargetPrice"],
                "StopPrice": entry["StopPrice"],
            }
            outcome_rows.append(outcome)
            closed.append({"event": "exit", **outcome})
            del self.active[event_id]
        self._append_rows(self._outcomes_path(now), self.OUTCOME_COLUMNS, outcome_rows)
        return closed

    def _roll_day(self, day: str) -> None:
        if self.state_date == day:
            return
        self.state_date = day
        self.active = {
            key: value
            for key, value in self.active.items()
            if str(value.get("Date", "")) == day
        }
        self.seen = {key for key in self.seen if str(key).startswith(day + ":")}

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.state_date = str(payload.get("date", ""))
            self.active = {
                str(key): value
                for key, value in (payload.get("active", {}) or {}).items()
                if isinstance(value, dict)
            }
            self.seen = set(str(key) for key in (payload.get("seen", []) or []))
        except Exception as exc:
            default_logger().debug(f"Could not load full-rank paper state: {exc}", exc_info=True)

    def _save_state(self, now: datetime) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "active": self.active,
            "seen": sorted(self.seen),
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _write_status(self, now: datetime) -> None:
        entries = self._read_count(self._entries_path(now))
        outcomes = self._read_frame(self._outcomes_path(now))
        net_pnl = float(pd.to_numeric(outcomes.get("NetPnl", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum())
        wins = int((pd.to_numeric(outcomes.get("NetPnl", pd.Series(dtype=float)), errors="coerce").fillna(0.0) > 0).sum())
        payload = {
            "status": "recording_exact_ask_to_bid_paper_outcomes",
            "date": now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "entries": entries,
            "active": len(self.active),
            "closed": int(len(outcomes)),
            "wins": wins,
            "net_pnl": round(net_pnl, 2),
            "target_pct": self.target_pct,
            "stop_pct": self.stop_pct,
            "horizon_minutes": self.horizon_minutes,
            "stress_pct": self.stress_pct,
            "capital_per_event": self.capital,
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        self.status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _source(row: Dict[str, Any]) -> str:
        reason = str(row.get("RecordReason", "") or "")
        if "rapid_rank_climber" in reason:
            return "rapid_rank_climber"
        if "full_universe_leader_pool" in reason:
            return "leader_pool"
        return ""

    def _cohort(self, source: str, row: Dict[str, Any]) -> str:
        rank = int(self._number(row.get("DayChangeRank")))
        move = self._number(row.get("DayChangePct"))
        rank_band = (
            "r1_10"
            if rank <= 10
            else "r11_25"
            if rank <= 25
            else "r26_50"
            if rank <= 50
            else "r51_75"
            if rank <= 75
            else "r76_100"
            if rank <= 100
            else "r101_200"
            if rank <= 200
            else "r201_plus"
        )
        move_band = "m0_5_1" if move < 1.0 else "m1_2" if move < 2.0 else "m2_3" if move < 3.0 else "m3_5"
        return f"{source}:{rank_band}:{move_band}"

    @staticmethod
    def _spread_bps(bid: float, ask: float) -> float:
        midpoint = (bid + ask) / 2.0
        return (ask - bid) * 10000.0 / midpoint if bid > 0 and ask > bid and midpoint > 0 else 0.0

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
        return str(value or "").strip().lower() in {"1", "true", "yes", "y"}

    def _entries_path(self, now: datetime) -> Path:
        return self.root / f"entries_{now:%Y-%m-%d}.csv"

    def _outcomes_path(self, now: datetime) -> Path:
        return self.root / f"outcomes_{now:%Y-%m-%d}.csv"

    @staticmethod
    def _append_rows(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        FullUniverseRankPaperObserver._ensure_csv_schema(path, columns)
        pd.DataFrame(rows, columns=columns).to_csv(path, mode="a", header=not path.exists(), index=False)

    @staticmethod
    def _ensure_csv_schema(path: Path, columns: List[str]) -> None:
        """Atomically widen a same-day ledger after a code deployment."""
        if not path.exists() or path.stat().st_size <= 0:
            return
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            old_header = next(reader, [])
            if old_header == columns:
                return
            if not old_header or not set(old_header).issubset(set(columns)):
                raise ValueError(f"unsupported paper ledger schema in {path}")
            old_rows = list(reader)
        widened = []
        for values in old_rows:
            if len(values) == len(old_header):
                source = dict(zip(old_header, values))
            elif len(values) == len(columns):
                source = dict(zip(columns, values))
            else:
                raise ValueError(
                    f"unsupported paper ledger row width {len(values)} in {path}"
                )
            widened.append([source.get(column, "") for column in columns])
        temporary = path.with_suffix(path.suffix + ".schema_tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(widened)
        os.replace(temporary, path)

    @staticmethod
    def _read_count(path: Path) -> int:
        if not path.exists():
            return 0
        try:
            return len(pd.read_csv(path, usecols=["EventId"]))
        except Exception:
            return 0

    @staticmethod
    def _read_frame(path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()

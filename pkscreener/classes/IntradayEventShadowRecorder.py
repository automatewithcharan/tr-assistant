"""Paper-only live five-minute recorder for the shared intraday event engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable

import pandas as pd

from pkscreener.classes.IntradayEventEngine import detect_intraday_events, prepare_intraday_day


@dataclass(frozen=True)
class EventShadowConfig:
    benchmark_symbol: str = "NIFTY 50"
    start_time: str = "09:20:00"
    latest_signal_time: str = "11:30:00"
    min_move_pct: float = 0.3
    max_move_pct: float = 3.0
    min_vol_ratio: float = 1.2
    min_vol_ratio_5: float = 1.4
    min_relative_strength_pct: float = 0.0
    min_breakout_pct: float = 0.0
    max_breakout_pct: float = 1.25
    min_vwap_dist_pct: float = 0.0
    max_vwap_dist_pct: float = 2.5
    max_range_used_pct: float = 5.0
    min_candle_body_pct: float = 0.05
    retest_tolerance_pct: float = 0.25
    max_events_per_type_per_day: int = 1
    min_event_gap_bars: int = 3


class IntradayEventShadowRecorder:
    """Aggregate cumulative-volume ticks and persist completed bars/events."""

    def __init__(self, output_dir: str | Path, config: EventShadowConfig | None = None):
        self.output_dir = Path(output_dir)
        self.config = config or EventShadowConfig()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._last_cumulative_volume: Dict[str, float] = {}
        self._bars: Dict[str, list[dict]] = {}
        self._event_keys: set[tuple[str, str, str, int]] = set()
        self._restore_today()

    @staticmethod
    def _timestamp(tick: Dict[str, Any], fallback: Any = None) -> pd.Timestamp | None:
        for value in (tick.get("exchange_timestamp"), tick.get("last_trade_time"), fallback):
            if value in (None, "", "None"):
                continue
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                continue
            timestamp = pd.Timestamp(parsed)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.tz_convert("Asia/Kolkata").tz_localize(None)
            return timestamp
        return None

    def update(self, ticks: Dict[str, Dict[str, Any]], fallback_time: Any = None) -> list[dict]:
        completed: list[dict] = []
        for raw_symbol, tick in (ticks or {}).items():
            symbol = str(raw_symbol or "").strip().upper()
            price = self._number(tick.get("last_price"))
            timestamp = self._timestamp(tick, fallback=fallback_time)
            if not symbol or price <= 0 or timestamp is None:
                continue
            bucket = timestamp.floor("5min")
            cumulative = max(0.0, self._number(tick.get("volume_traded")))
            previous_cumulative = self._last_cumulative_volume.get(symbol)
            volume_delta = max(0.0, cumulative - previous_cumulative) if previous_cumulative is not None else 0.0
            self._last_cumulative_volume[symbol] = cumulative

            active = self._active.get(symbol)
            if active is not None and pd.Timestamp(active["datetime"]) != bucket:
                completed_bar = self._complete_active(symbol)
                if completed_bar is not None:
                    completed.append(completed_bar)
                active = None
            if active is None:
                self._active[symbol] = {
                    "datetime": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume_delta,
                    "observations": 1,
                }
                continue
            active["high"] = max(float(active["high"]), price)
            active["low"] = min(float(active["low"]), price)
            active["close"] = price
            active["volume"] = float(active["volume"]) + volume_delta
            active["observations"] = int(active["observations"]) + 1

        if completed:
            self._persist_completed(completed)
        return completed

    def _complete_active(self, symbol: str) -> dict | None:
        active = self._active.pop(symbol, None)
        if active is None or int(active.get("observations", 0)) < 2:
            return None
        row = {"symbol": symbol, **active}
        self._bars.setdefault(symbol, []).append(row)
        self._bars[symbol] = self._bars[symbol][-90:]
        return row

    def detect_new_events(self, completed_symbols: Iterable[str]) -> list[dict]:
        benchmark = self._benchmark_map()
        if not benchmark:
            return []
        events: list[dict] = []
        for symbol in {str(item).strip().upper() for item in completed_symbols}:
            if not symbol or symbol == self.config.benchmark_symbol.upper():
                continue
            bars = self._bars.get(symbol, [])
            if len(bars) < 4:
                continue
            raw = pd.DataFrame(bars).drop(columns=["symbol", "observations"], errors="ignore")
            prepared = prepare_intraday_day(raw, benchmark)
            for event_type, index, stage in detect_intraday_events(prepared, SimpleNamespace(**asdict(self.config))):
                signal = prepared.iloc[index]
                date = str(pd.Timestamp(signal["datetime"]).date())
                key = (date, symbol, event_type, int(stage))
                if key in self._event_keys:
                    continue
                self._event_keys.add(key)
                events.append(
                    {
                        "date": date,
                        "symbol": symbol,
                        "event_type": event_type,
                        "event_stage": int(stage),
                        "signal_time": pd.Timestamp(signal["datetime"]).isoformat(),
                        "price": round(float(signal["close"]), 4),
                        "move_from_open_pct": round(float(signal["move_from_open_pct"]), 6),
                        "vol_ratio": round(float(signal["vol_ratio"]), 6),
                        "vol_ratio_5": round(float(signal["vol_ratio_5"]), 6),
                        "vwap_dist_pct": round(float(signal["vwap_dist_pct"]), 6),
                        "relative_strength_open_pct": round(float(signal["relative_strength_open_pct"]), 6),
                        "range_pct_so_far": round(float(signal["range_pct_so_far"]), 6),
                        "benchmark_available": True,
                        "paper_only": True,
                    }
                )
        if events:
            self._append_csv(self._events_path(events[0]["date"]), events)
        return events

    def _benchmark_map(self) -> dict[pd.Timestamp, dict[str, float]]:
        symbol = self.config.benchmark_symbol.upper()
        bars = self._bars.get(symbol, [])
        if len(bars) < 2:
            return {}
        frame = pd.DataFrame(bars).sort_values("datetime").copy()
        day_open = float(frame.iloc[0]["open"])
        frame["benchmark_return_from_open_pct"] = (frame["close"] - day_open) / day_open * 100.0
        frame["benchmark_recent_return_3bar_pct"] = frame["close"].pct_change(3).fillna(0.0) * 100.0
        return {
            pd.Timestamp(row["datetime"]): {
                "benchmark_return_from_open_pct": float(row["benchmark_return_from_open_pct"]),
                "benchmark_recent_return_3bar_pct": float(row["benchmark_recent_return_3bar_pct"]),
            }
            for _, row in frame.iterrows()
        }

    def _persist_completed(self, rows: list[dict]) -> None:
        by_date: Dict[str, list[dict]] = {}
        for row in rows:
            date = str(pd.Timestamp(row["datetime"]).date())
            serializable = dict(row)
            serializable["datetime"] = pd.Timestamp(row["datetime"]).isoformat()
            by_date.setdefault(date, []).append(serializable)
        for date, date_rows in by_date.items():
            self._append_csv(self._bars_path(date), date_rows)

    def _append_csv(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(path, mode="a", header=not path.exists(), index=False)

    def _bars_path(self, date: str) -> Path:
        return self.output_dir / "intraday_event_shadow" / f"bars_{date}.csv"

    def _events_path(self, date: str) -> Path:
        return self.output_dir / "intraday_event_shadow" / f"events_{date}.csv"

    def _restore_today(self) -> None:
        today = str(pd.Timestamp.now(tz="Asia/Kolkata").date())
        bars_path = self._bars_path(today)
        if bars_path.exists():
            try:
                frame = pd.read_csv(bars_path, parse_dates=["datetime"])
                normalized = frame["symbol"].astype(str).str.strip().str.upper()
                for symbol, group in frame.groupby(normalized, sort=False):
                    self._bars[str(symbol)] = group.sort_values("datetime").tail(90).to_dict(orient="records")
            except Exception:
                self._bars = {}
        events_path = self._events_path(today)
        if events_path.exists():
            try:
                events = pd.read_csv(events_path)
                for row in events.to_dict(orient="records"):
                    self._event_keys.add(
                        (
                            str(row.get("date", today)),
                            str(row.get("symbol", "")).strip().upper(),
                            str(row.get("event_type", "")),
                            int(row.get("event_stage", 1)),
                        )
                    )
            except Exception:
                self._event_keys = set()

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

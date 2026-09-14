"""NSE cash-equity eligibility at the manual Discord delivery boundary.

This module deliberately does not participate in scanning, qualification,
research persistence, paper trading, or order routing.  It answers only
whether a symbol may be shown to a human as a manual Discord candidate.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Set, Tuple

import pandas as pd

from PKDevTools.classes.log import default_logger


@dataclass(frozen=True)
class ManualDiscordEligibility:
    eligible: bool
    reason: str
    symbol: str
    exchange: str = ""
    series: str = ""
    instrument_token: Optional[int] = None
    metadata_source: str = ""


class NSEEQManualDiscordGuard:
    """Fail closed unless existing instrument metadata proves NSE cash EQ."""

    def __init__(self, output_dir: str, instrument_path: Optional[str] = None) -> None:
        self.output_dir = Path(output_dir)
        self.instrument_path = Path(instrument_path) if instrument_path else (
            self.output_dir / "kite_instruments_nse_latest.csv"
        )
        self._loaded_mtime_ns: Optional[int] = None
        self._metadata_available = False
        self._nse_eq: Dict[str, ManualDiscordEligibility] = {}
        self._known_symbols: Set[str] = set()
        self._reported: Set[Tuple[str, str, str, str]] = set()

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        return str(value).strip().upper()

    @staticmethod
    def _token(value: Any) -> Optional[int]:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def _load(self) -> None:
        try:
            mtime_ns = self.instrument_path.stat().st_mtime_ns
        except OSError:
            self._metadata_available = False
            self._nse_eq = {}
            self._known_symbols = set()
            self._loaded_mtime_ns = None
            return
        if self._loaded_mtime_ns == mtime_ns:
            return

        self._loaded_mtime_ns = mtime_ns
        self._metadata_available = False
        self._nse_eq = {}
        self._known_symbols = set()
        try:
            frame = pd.read_csv(self.instrument_path)
            required = {"tradingsymbol", "exchange", "segment", "instrument_type"}
            if frame.empty or not required.issubset(frame.columns):
                return
            for row in frame.to_dict(orient="records"):
                symbol = self._text(row.get("tradingsymbol"))
                if not symbol:
                    continue
                self._known_symbols.add(symbol)
                exchange = self._text(row.get("exchange"))
                segment = self._text(row.get("segment"))
                series = self._text(row.get("instrument_type"))
                if exchange == "NSE" and segment == "NSE" and series == "EQ":
                    self._nse_eq[symbol] = ManualDiscordEligibility(
                        eligible=True,
                        reason="valid_nse_eq_instrument",
                        symbol=symbol,
                        exchange=exchange,
                        series=series,
                        instrument_token=self._token(row.get("instrument_token")),
                        metadata_source=str(self.instrument_path),
                    )
            self._metadata_available = True
        except Exception as exc:
            default_logger().warning(
                f"Manual Discord NSE-EQ metadata could not be loaded from "
                f"{self.instrument_path}: {exc}"
            )

    def check(
        self,
        symbol: str,
        runtime_metadata: Optional[Mapping[str, Any]] = None,
    ) -> ManualDiscordEligibility:
        """Return eligibility without changing any upstream candidate state."""
        normalized = self._text(symbol)
        if not normalized:
            return ManualDiscordEligibility(False, "missing_symbol", normalized)

        # A row carrying complete exchange/series identity is authoritative too.
        metadata = runtime_metadata or {}
        exchange = self._text(metadata.get("exchange", metadata.get("Exchange")))
        segment = self._text(metadata.get("segment", metadata.get("Segment")))
        series = self._text(
            metadata.get(
                "instrument_type",
                metadata.get("InstrumentType", metadata.get("Series")),
            )
        )
        if exchange == "NSE" and segment == "NSE" and series == "EQ":
            return ManualDiscordEligibility(
                True,
                "valid_nse_eq_instrument",
                normalized,
                exchange,
                series,
                self._token(metadata.get("instrument_token", metadata.get("InstrumentToken"))),
                "candidate_runtime_metadata",
            )

        self._load()
        eligible = self._nse_eq.get(normalized)
        if eligible is not None:
            return eligible
        reason = (
            "nse_instrument_metadata_unavailable"
            if not self._metadata_available
            else "no_valid_nse_eq_instrument"
        )
        return ManualDiscordEligibility(
            False,
            reason,
            normalized,
            exchange=exchange,
            series=series,
            instrument_token=self._token(
                metadata.get("instrument_token", metadata.get("InstrumentToken"))
            ),
            metadata_source=str(self.instrument_path) if self._metadata_available else "",
        )

    def record_suppression(
        self,
        decision: ManualDiscordEligibility,
        source: str,
        now: Optional[datetime] = None,
    ) -> None:
        """Append one compact suppression record per symbol/source/reason/day."""
        timestamp = now or datetime.now().astimezone()
        day = timestamp.date().isoformat()
        key = (day, str(source or "unknown"), decision.symbol, decision.reason)
        if key in self._reported:
            return
        self._reported.add(key)
        payload = {
            "event": "manual_discord_delivery_suppressed",
            "timestamp": timestamp.isoformat(timespec="seconds"),
            "source": str(source or "unknown"),
            **asdict(decision),
        }
        try:
            path = self.output_dir / "manual_discord_delivery_guard" / f"{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
        except Exception as exc:
            default_logger().warning(
                f"Could not persist manual Discord suppression for "
                f"{decision.symbol}: {exc}"
            )
        default_logger().info(
            f"Manual Discord delivery suppressed for {decision.symbol} "
            f"from {source}: {decision.reason}"
        )

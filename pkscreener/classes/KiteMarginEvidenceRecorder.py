"""Calculation-only Kite MIS margin evidence for paper strategy research."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
import time
from typing import Any, Callable, Iterable


class KiteMarginEvidenceRecorder:
    """Persist causal MIS margin quotes without exposing order placement."""

    COLUMNS = [
        "Date",
        "QuotedAt",
        "Exchange",
        "Symbol",
        "Product",
        "Quantity",
        "Price",
        "Notional",
        "RequiredMargin",
        "MarginPct",
        "EffectiveLeverage",
        "Sources",
        "Attempt",
        "Status",
        "Error",
    ]

    def __init__(
        self,
        output_dir: str | Path,
        *,
        client_factory: Callable[[], Any] | None = None,
        min_interval_seconds: float = 60.0,
        max_batch_size: int = 50,
        max_attempts_per_symbol: int = 3,
        persist: bool = True,
    ) -> None:
        self.output_dir = Path(output_dir) / "kite_margin_evidence"
        self.client_factory = client_factory or self._default_client
        self.min_interval_seconds = max(
            1.0, float(min_interval_seconds or 60.0)
        )
        self.max_batch_size = max(1, int(max_batch_size or 50))
        self.max_attempts_per_symbol = max(
            1, int(max_attempts_per_symbol or 3)
        )
        self.persist = bool(persist)
        self._client = None
        self._date = ""
        self._quoted: set[str] = set()
        self._exhausted: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._last_request_monotonic = 0.0
        self._api_calls = 0
        self._successful_quotes = 0
        self._failed_quotes = 0
        if self.persist:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_client():
        from pkscreener.classes.KiteConnectManager import (
            KiteConnectSessionManager,
        )

        return KiteConnectSessionManager()._build_client()

    def process(
        self,
        events: Iterable[dict[str, Any]],
        *,
        now: datetime,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Queue qualified symbols and persist any due margin calculation."""
        self._roll_date(now)
        for event in events or []:
            symbol = str(event.get("Symbol", "") or "").strip().upper()
            exchange = str(
                event.get("Exchange", "NSE") or "NSE"
            ).strip().upper()
            price = self._number(
                event.get("EntryDepthVwap")
                or event.get("EntryAsk")
                or event.get("Price")
            )
            if (
                not symbol
                or exchange != "NSE"
                or price <= 0
                or symbol in self._quoted
                or symbol in self._exhausted
            ):
                continue
            source = str(event.get("Source", "") or "").strip()
            pending = self._pending.setdefault(
                symbol,
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "price": price,
                    "sources": set(),
                },
            )
            pending["price"] = price
            if source:
                pending["sources"].add(source)

        elapsed = time.monotonic() - self._last_request_monotonic
        if not self._pending or (
            not force and elapsed < self.min_interval_seconds
        ):
            self._write_status(now)
            return []
        rows = self._quote_batch(now)
        self._append_rows(rows)
        self._write_status(now)
        return rows

    def _roll_date(self, now: datetime) -> None:
        date = now.strftime("%Y-%m-%d")
        if date == self._date:
            return
        self._date = date
        self._pending = {}
        self._quoted = set()
        self._exhausted = set()
        self._attempts = {}
        self._last_request_monotonic = 0.0
        self._api_calls = 0
        self._successful_quotes = 0
        self._failed_quotes = 0
        path = self._quotes_path()
        if not path.is_file():
            return
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    symbol = str(row.get("Symbol", "") or "").upper()
                    if not symbol:
                        continue
                    status = str(row.get("Status", "")).lower()
                    if status == "quoted":
                        self._quoted.add(symbol)
                    elif status in {"failed", "failed_final"}:
                        self._attempts[symbol] = (
                            self._attempts.get(symbol, 0) + 1
                        )
                        if (
                            status == "failed_final"
                            or self._attempts[symbol]
                            >= self.max_attempts_per_symbol
                        ):
                            self._exhausted.add(symbol)
        except OSError:
            return

    def _quote_batch(self, now: datetime) -> list[dict[str, Any]]:
        symbols = sorted(self._pending)[: self.max_batch_size]
        requests = []
        for symbol in symbols:
            pending = self._pending[symbol]
            requests.append(
                {
                    "exchange": pending["exchange"],
                    "tradingsymbol": symbol,
                    "transaction_type": "BUY",
                    "variety": "regular",
                    "product": "MIS",
                    "order_type": "LIMIT",
                    "quantity": 1,
                    "price": round(float(pending["price"]), 2),
                    "trigger_price": 0,
                }
            )
        self._last_request_monotonic = time.monotonic()
        self._api_calls += 1
        try:
            if self._client is None:
                self._client = self.client_factory()
            response = self._client.basket_order_margins(
                requests,
                consider_positions=False,
                mode=None,
            )
            margins = (
                response.get("orders", [])
                if isinstance(response, dict)
                else []
            )
            if len(margins) != len(requests):
                raise RuntimeError(
                    "Kite margin response did not match request batch"
                )
            margin_by_symbol = {
                str(item.get("tradingsymbol", "") or "").strip().upper(): item
                for item in margins
                if isinstance(item, dict)
                and str(item.get("tradingsymbol", "") or "").strip()
            }
            if set(margin_by_symbol) != set(symbols):
                raise RuntimeError(
                    "Kite margin response symbols did not match request batch"
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            return [
                self._failed_row(now, symbol, error)
                for symbol in symbols
            ]

        rows = []
        for symbol in symbols:
            margin = margin_by_symbol[symbol]
            required = self._number(
                (margin or {}).get("total")
                if isinstance(margin, dict)
                else 0.0
            )
            pending = self._pending[symbol]
            if required <= 0:
                rows.append(
                    self._failed_row(
                        now,
                        symbol,
                        "missing positive total margin",
                    )
                )
                continue
            rows.append(
                self._row(
                    now,
                    pending,
                    status="quoted",
                    required_margin=required,
                )
            )
            self._successful_quotes += 1
            self._quoted.add(symbol)
            self._pending.pop(symbol, None)
        return rows

    def _failed_row(
        self,
        now: datetime,
        symbol: str,
        error: str,
    ) -> dict[str, Any]:
        pending = self._pending[symbol]
        attempt = self._attempts.get(symbol, 0) + 1
        self._attempts[symbol] = attempt
        self._failed_quotes += 1
        final = attempt >= self.max_attempts_per_symbol
        if final:
            self._exhausted.add(symbol)
            self._pending.pop(symbol, None)
        return self._row(
            now,
            pending,
            status="failed_final" if final else "failed",
            error=error,
            attempt=attempt,
        )

    def _row(
        self,
        now: datetime,
        pending: dict[str, Any],
        *,
        status: str,
        required_margin: float = 0.0,
        error: str = "",
        attempt: int = 1,
    ) -> dict[str, Any]:
        price = float(pending["price"])
        margin_pct = required_margin / price * 100.0 if price > 0 else 0.0
        leverage = price / required_margin if required_margin > 0 else 0.0
        return {
            "Date": self._date,
            "QuotedAt": now.isoformat(timespec="seconds"),
            "Exchange": pending["exchange"],
            "Symbol": pending["symbol"],
            "Product": "MIS",
            "Quantity": 1,
            "Price": round(price, 2),
            "Notional": round(price, 2),
            "RequiredMargin": round(required_margin, 4),
            "MarginPct": round(margin_pct, 6),
            "EffectiveLeverage": round(leverage, 6),
            "Sources": ",".join(sorted(pending["sources"])),
            "Attempt": int(attempt),
            "Status": status,
            "Error": error,
        }

    def _append_rows(self, rows: list[dict[str, Any]]) -> None:
        if not self.persist or not rows:
            return
        path = self._quotes_path()
        write_header = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)

    def _write_status(self, now: datetime) -> None:
        if not self.persist:
            return
        payload = {
            "status": "recording_calculation_only_kite_margins",
            "date": self._date,
            "updated_at": now.isoformat(timespec="seconds"),
            "product": "MIS",
            "consider_positions": False,
            "response_mode": "full_per_order_breakdown",
            "quote_quantity": 1,
            "api_calls": self._api_calls,
            "successful_quotes": self._successful_quotes,
            "failed_quotes": self._failed_quotes,
            "pending_symbols": len(self._pending),
            "exhausted_symbols": len(self._exhausted),
            "max_attempts_per_symbol": self.max_attempts_per_symbol,
            "paper_only": True,
            "calculation_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        temporary = self.output_dir / f".status.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.output_dir / "status.json")

    def _quotes_path(self) -> Path:
        return self.output_dir / f"margin_quotes_{self._date}.csv"

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

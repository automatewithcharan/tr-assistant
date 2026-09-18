"""Report every new member of NSE's All Securities Top 20 Gainers board."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from pkscreener.classes.NGP5EarlyWatchPublisher import IST


PAGE = "https://www.nseindia.com/market-data/top-gainers-losers"
API = "https://www.nseindia.com/api/live-analysis-variations?index=gainers"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
    "Referer": PAGE,
}
SYMBOL = re.compile(r"^[A-Z0-9&_.-]+$")


def fetch_board(session: requests.Session, now: datetime) -> tuple[datetime, list[dict[str, Any]]]:
    """Fetch the same allSec board used by NSE's All Securities tab."""
    response = session.get(API, headers=HEADERS, timeout=12)
    if response.status_code in {401, 403}:
        session.get(PAGE, headers=HEADERS, timeout=12).raise_for_status()
        response = session.get(API, headers=HEADERS, timeout=12)
    response.raise_for_status()
    board = response.json().get("allSec")
    if not isinstance(board, dict):
        raise ValueError("NSE allSec board missing")
    snapshot = datetime.strptime(board["timestamp"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
    age = (now - snapshot).total_seconds()
    if snapshot.date() != now.date() or age < -300 or age > 900:
        raise ValueError(f"NSE board timestamp stale or invalid: {snapshot.isoformat()}")
    rows = board.get("data")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("NSE allSec board missing Top 20 rows")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("NSE allSec row malformed")
        symbol = str(row.get("symbol", "")).strip().upper()
        if not SYMBOL.fullmatch(symbol) or symbol in seen:
            raise ValueError(f"NSE allSec symbol malformed or repeated: {symbol!r}")
        seen.add(symbol)
        result.append(row)
    return snapshot, result


def format_alert(snapshot: datetime, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"📈 **NSE TOP 20 GAINERS — WATCH ONLY** | {snapshot:%H:%M} IST",
        f"{len(rows)} new symbol(s) on NSE's All Securities board today:",
    ]
    for row in rows:
        symbol = str(row["symbol"]).strip().upper()
        change = float(row["perChange"])
        price = float(row["ltp"])
        lines.append(f"• **{symbol}**  +{change:.2f}%  ₹{price:,.2f}")
    lines.extend(["Board appearance only; not a buy signal.", PAGE])
    message = "\n".join(lines)
    if len(message) > 2000:
        raise ValueError("NSE Top 20 alert exceeds Discord limit")
    return message


class NSETopGainersWatch:
    def __init__(self, state_root: Path, transport: Any,
                 clock: Callable[[], datetime], session: requests.Session | None = None) -> None:
        self.state_root = Path(state_root)
        self.transport = transport
        self.clock = clock
        self.session = session or requests.Session()

    def _save(self, path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         delete=False) as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = handle.name
        os.replace(temp_path, path)

    def process(self) -> dict[str, Any]:
        now = self.clock().astimezone(IST)
        minute = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not 555 <= minute < 930:
            return {"status": "outside_window"}
        path = self.state_root / f"{now.date().isoformat()}.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if float(state.get("retry_after_epoch", 0)) > now.timestamp():
            return {"status": "retry_wait"}
        try:
            snapshot, rows = fetch_board(self.session, now)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            return {"status": "source_failed", "error": str(exc)[:200]}
        sent = set(state.get("sent_symbols", []))
        broad_path = self.state_root / "kite_top100" / f"{now.date().isoformat()}.json"
        broad_state = json.loads(broad_path.read_text(encoding="utf-8")) if broad_path.exists() else {}
        already_alerted = sent | set(broad_state.get("sent_symbols", []))
        new_rows = [row for row in rows if row["symbol"].strip().upper() not in already_alerted]
        if not new_rows:
            return {"status": "up_to_date", "board_size": len(rows), "sent_today": len(sent)}
        if self.transport is None:
            return {"status": "no_transport", "pending": len(new_rows)}
        try:
            result = self.transport.post(format_alert(snapshot, new_rows))
        except Exception as exc:
            return {"status": "delivery_failed", "error": type(exc).__name__,
                    "pending": len(new_rows)}
        if not result.delivered:
            state["retry_after_epoch"] = now.timestamp() + max(
                30.0, float(result.retry_after_seconds or 30.0))
            self._save(path, state)
            return {"status": "delivery_failed", "http_status": result.http_status,
                    "pending": len(new_rows)}
        symbols = [str(row["symbol"]).strip().upper() for row in new_rows]
        state["sent_symbols"] = sorted(sent | set(symbols))
        state["last_delivery_at"] = now.isoformat()
        state["last_board_at"] = snapshot.isoformat()
        state.pop("retry_after_epoch", None)
        self._save(path, state)
        return {"status": "delivered", "symbols": symbols, "board_size": len(rows)}

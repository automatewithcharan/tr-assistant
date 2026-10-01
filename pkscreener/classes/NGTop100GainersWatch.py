"""Broad NSE EQ top-gainer coverage from the live Kite ticker snapshot."""

from __future__ import annotations

import csv
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any, Callable

from pkscreener.classes.NGP5EarlyWatchPublisher import IST
from pkscreener.classes.NSETopGainersWatch import NSETopGainersWatch


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def load_eq_universe(path: Path) -> set[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        symbols = {row["SYMBOL"].strip().upper() for row in csv.DictReader(handle)
                   if row.get("SERIES", "").strip().upper() == "EQ" and row.get("SYMBOL")}
    if len(symbols) < 1000:
        raise ValueError("NSE EQ universe too small or invalid")
    return symbols


def rank_live_gainers(ticks_path: Path, universe_path: Path, now: datetime,
                      top_n: int = 100, min_shares: int = 1000,
                      min_traded_value: float = 1_000_000.0) -> list[dict[str, Any]]:
    """Rank today's traded NSE EQ shares; reject stale or partial ticker data."""
    if (now.timestamp() - ticks_path.stat().st_mtime) > 120:
        raise ValueError("Kite ticker snapshot is stale")
    allowed = load_eq_universe(universe_path)
    rows: list[dict[str, Any]] = []
    fresh_count = 0
    with ticks_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol not in allowed or str(row.get("exchange") or "").upper() != "NSE":
                continue
            price = _number(row.get("last_price"))
            prior_close = _number(row.get("close"))
            volume = _number(row.get("volume_traded"))
            if price is None or prior_close is None or prior_close <= 0 or volume is None or volume <= 0:
                continue
            try:
                received_at = datetime.fromisoformat(str(row["received_at"]).replace("Z", "+00:00"))
                if received_at.tzinfo is None:
                    continue
                trade_date = datetime.fromisoformat(str(row["last_trade_time"])).date()
            except (KeyError, TypeError, ValueError):
                continue
            age = (now - received_at.astimezone(IST)).total_seconds()
            if trade_date != now.date() or age < -10 or age > 120:
                continue
            fresh_count += 1
            average_price = _number(row.get("average_traded_price"))
            traded_value = volume * (average_price if average_price is not None and average_price > 0 else price)
            if volume < min_shares or traded_value < min_traded_value:
                continue
            change = (price / prior_close - 1.0) * 100.0
            if change <= 0:
                continue
            rows.append({"symbol": symbol, "change": change, "price": price,
                         "volume": volume, "traded_value": traded_value,
                         "received_at": received_at.isoformat()})
    if fresh_count < 500:
        raise ValueError(f"Only {fresh_count} fresh NSE EQ trades; need 500 for a broad ranking")
    if not rows:
        raise ValueError("No fresh positive NSE EQ shares meeting liquidity minimums")
    rows.sort(key=lambda row: (-row["change"], -row["volume"], row["symbol"]))
    return rows[:top_n]


def build_message(now: datetime, rows: list[dict[str, Any]], top_n: int,
                  min_shares: int = 1000, min_traded_value: float = 1_000_000.0) -> str:
    lines = [f"📈 **NG NSE EQ TOP {top_n} — WATCH ONLY** | {now:%H:%M} IST",
             f"Live Kite rank; min {min_shares:,} shares and ₹{min_traded_value / 100_000:g}L traded; new today:"]
    for row in rows:
        lines.append(f"• #{row['rank']} **{row['symbol']}**  +{row['change']:.2f}%  ₹{row['price']:,.2f}  Vol {row['volume']:,.0f}")
    lines.append("Rank appearance only; not a BUY signal.")
    message = "\n".join(lines)
    if len(message) > 2000:
        raise ValueError("Top-gainers message exceeds Discord limit")
    return message


class NGTop100GainersWatch(NSETopGainersWatch):
    def __init__(self, state_root: Path, ticks_path: Path, universe_path: Path,
                 official_state_root: Path, transport: Any, clock: Callable[[], datetime],
                 top_n: int = 100, min_shares: int = 1000,
                 min_traded_value: float = 1_000_000.0, notifications_enabled: bool = False) -> None:
        super().__init__(state_root, transport, clock)
        if not 21 <= top_n <= 200:
            raise ValueError("Top-gainer coverage must be between 21 and 200")
        self.ticks_path = Path(ticks_path)
        self.universe_path = Path(universe_path)
        self.official_state_root = Path(official_state_root)
        self.top_n = top_n
        self.min_shares = min_shares
        self.min_traded_value = min_traded_value
        self.notifications_enabled = bool(notifications_enabled)

    def process(self) -> dict[str, Any]:
        now = self.clock().astimezone(IST)
        minute = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not 555 <= minute < 930:
            return {"status": "outside_window"}
        day = now.date().isoformat()
        path = self.state_root / f"{day}.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if float(state.get("retry_after_epoch", 0)) > now.timestamp():
            return {"status": "retry_wait"}
        try:
            ranked = rank_live_gainers(self.ticks_path, self.universe_path, now,
                                       self.top_n, self.min_shares, self.min_traded_value)
        except (OSError, ValueError, KeyError) as exc:
            return {"status": "source_failed", "error": str(exc)[:160]}
        if not self.notifications_enabled:
            return {"status": "notification_disabled", "board_size": len(ranked)}
        official_path = self.official_state_root / f"{day}.json"
        official = json.loads(official_path.read_text(encoding="utf-8")) if official_path.exists() else {}
        sent = set(state.get("sent_symbols", []))
        already = sent | set(official.get("sent_symbols", []))
        pending = []
        for rank, row in enumerate(ranked, 1):
            if row["symbol"] not in already:
                pending.append({**row, "rank": rank})
        if not pending:
            return {"status": "up_to_date", "board_size": len(ranked), "sent_today": len(sent)}
        if self.transport is None:
            return {"status": "no_transport", "pending": len(pending)}
        delivered = []
        for offset in range(0, len(pending), 30):
            batch = pending[offset:offset + 30]
            try:
                result = self.transport.post(build_message(now, batch, self.top_n,
                                                           self.min_shares, self.min_traded_value))
            except Exception as exc:
                return {"status": "delivery_failed", "error": type(exc).__name__,
                        "delivered": len(delivered), "pending": len(pending) - len(delivered)}
            if not result.delivered:
                state["retry_after_epoch"] = now.timestamp() + max(
                    30.0, float(result.retry_after_seconds or 30.0))
                self._save(path, state)
                return {"status": "delivery_failed", "http_status": result.http_status,
                        "delivered": len(delivered), "pending": len(pending) - len(delivered)}
            symbols = [row["symbol"] for row in batch]
            sent.update(symbols)
            delivered.extend(symbols)
            state["sent_symbols"] = sorted(sent)
            state["last_delivery_at"] = now.isoformat()
            state.pop("retry_after_epoch", None)
            self._save(path, state)
        return {"status": "delivered", "symbols": delivered, "board_size": len(ranked)}

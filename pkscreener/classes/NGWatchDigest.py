"""Capped, paper-only Discord review digest from archived P5 observations."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from pkscreener.classes.NGP5EarlyWatchPublisher import IST


MAX_SYMBOLS_PER_DAY = 5
MIN_TURNOVER = 50_000_000.0  # INR, review filter only
MAX_SPREAD_BPS = 20.0
MIN_BUY_SELL_RATIO = 1.2
MAX_FROM_OPEN_PCT = 3.0


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def eligible(event: Mapping[str, Any]) -> bool:
    """Operational attention filter; it makes no claim about trade returns."""
    if event.get("kind") != "p5":
        return False
    p = event.get("policy_inputs") or {}
    symbol = str(p.get("symbol") or "").strip().upper()
    turnover = _number(p.get("turnover"))
    spread = _number(p.get("spread_bps"))
    buyers = _number(p.get("buy_sell_ratio"))
    from_open = _number(p.get("open_change_pct"))
    return bool(
        symbol
        and turnover is not None and turnover >= MIN_TURNOVER
        and spread is not None and 0 <= spread <= MAX_SPREAD_BPS
        and buyers is not None and buyers >= MIN_BUY_SELL_RATIO
        and from_open is not None and 0 <= from_open <= MAX_FROM_OPEN_PCT
        and _timestamp(event.get("observation_timestamp")) is not None
    )


def priority(event: Mapping[str, Any]) -> tuple[float, str]:
    """Deterministic review order, deliberately not an entry or return model."""
    p = event["policy_inputs"]
    turnover_cr = (_number(p.get("turnover")) or 0) / 10_000_000
    score = (
        min(_number(p.get("rank_improvement")) or 0, 100) / 50
        + min(_number(p.get("buy_sell_ratio")) or 0, 3)
        + math.log1p(turnover_cr)
        + min(_number(p.get("move_delta_pct")) or 0, 2)
        - (_number(p.get("spread_bps")) or 0) / 20
        - (_number(p.get("open_change_pct")) or 0) / 3
    )
    return score, str(p["symbol"])


def load_observations(journal: Path, day: str) -> list[dict[str, Any]]:
    if not journal.is_file():
        return []
    events: dict[str, dict[str, Any]] = {}
    with journal.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a live writer may leave a partial final line
            if row.get("delivery_state") != "OBSERVED":
                continue
            event = row.get("event") or {}
            if event.get("session_date") == day and eligible(event):
                events[str(event["event_id"])] = event
    return list(events.values())


def select_candidates(
    events: list[dict[str, Any]], now: dt.datetime, sent_symbols: set[str]
) -> list[dict[str, Any]]:
    remaining = MAX_SYMBOLS_PER_DAY - len(sent_symbols)
    if remaining <= 0:
        return []
    fresh: dict[str, dict[str, Any]] = {}
    for event in events:
        if not eligible(event):
            continue
        stamp = _timestamp(event["observation_timestamp"])
        if stamp is None or stamp > now or now - stamp > dt.timedelta(minutes=30):
            continue
        symbol = str(event["policy_inputs"]["symbol"]).upper()
        if symbol in sent_symbols:
            continue
        if symbol not in fresh or priority(event) > priority(fresh[symbol]):
            fresh[symbol] = event
    return sorted(fresh.values(), key=lambda e: (-priority(e)[0], priority(e)[1]))[:remaining]


def current_slot(now: dt.datetime) -> str | None:
    """Digest windows start at 09:45 IST and repeat every 30 minutes."""
    now = now.astimezone(IST)
    minute = now.hour * 60 + now.minute
    first, end = 9 * 60 + 45, 15 * 60 + 15
    if minute < first or minute >= end:
        return None
    slot = first + (minute - first) // 30 * 30
    return f"{slot // 60:02d}:{slot % 60:02d}"


def build_message(events: list[dict[str, Any]], slot: str) -> str:
    lines = [
        f"🟡 **NG REVIEW SHORTLIST · {slot} IST**",
        "**NOT A BUY SIGNAL · PAPER / SKIP**",
        "At most five new symbols today. Check the chart before any decision.",
        "",
    ]
    for event in events:
        p = event["policy_inputs"]
        symbol = str(p["symbol"]).upper()
        lines.append(
            f"**{symbol}** · ₹{_number(p.get('last_price')) or 0:.2f} "
            f"· open +{_number(p.get('open_change_pct')) or 0:.2f}% "
            f"· rank {p.get('current_rank', '?')} "
            f"· B/S {_number(p.get('buy_sell_ratio')) or 0:.2f} "
            f"· spread {_number(p.get('spread_bps')) or 0:.1f} bps "
            f"· ₹{(_number(p.get('turnover')) or 0) / 10_000_000:.1f} Cr "
            f"· [chart](https://www.tradingview.com/chart/?symbol=NSE%3A{symbol})"
        )
    lines += ["", "Filtered from post-open P5 watches; ranking is unvalidated for profit."]
    return "\n".join(lines)


class NGWatchDigest:
    def __init__(self, p5_state_root: Path, state_root: Path, transport: Any, *, clock=None):
        self.p5_state_root = Path(p5_state_root)
        self.state_root = Path(state_root)
        self.transport = transport
        self.clock = clock or (lambda: dt.datetime.now(IST))

    def _state_path(self, day: str) -> Path:
        return self.state_root / day / "state.json"

    def _evidence_path(self, day: str) -> Path:
        return self.state_root / day / "events.jsonl"

    def _record(self, day: str, row: dict[str, Any]) -> None:
        path = self._evidence_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _save(self, day: str, state: dict[str, Any]) -> None:
        path = self._state_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            tmp = handle.name
        os.replace(tmp, path)

    def process(self) -> dict[str, Any]:
        now = self.clock().astimezone(IST)
        day = now.date().isoformat()
        slot = current_slot(now)
        if slot is None or now.weekday() >= 5:
            return {"status": "outside_window"}
        path = self._state_path(day)
        state = json.loads(path.read_text()) if path.is_file() else {"sent_symbols": [], "last_slot": None}
        if state.get("last_slot") == slot:
            return {"status": "already_processed", "slot": slot}
        if float(state.get("retry_after_epoch", 0)) > now.timestamp():
            return {"status": "retry_wait", "slot": slot}
        journal = self.p5_state_root / day / "evidence" / f"events_{day}.jsonl"
        selected = select_candidates(load_observations(journal, day), now, set(state["sent_symbols"]))
        if not selected:
            state["last_slot"] = slot
            self._save(day, state)
            return {"status": "no_candidates", "slot": slot}
        if self.transport is None:
            return {"status": "no_transport", "slot": slot, "candidates": len(selected)}
        symbols = [str(e["policy_inputs"]["symbol"]).upper() for e in selected]
        try:
            result = self.transport.post(build_message(selected, slot))
        except Exception as exc:
            self._record(day, {"slot": slot, "state": "FAILED", "symbols": symbols,
                               "error": type(exc).__name__, "at": now.isoformat()})
            state["retry_after_epoch"] = now.timestamp() + 30
            self._save(day, state)
            return {"status": "failed", "slot": slot}
        if not result.delivered:
            self._record(day, {"slot": slot, "state": "FAILED", "symbols": symbols,
                               "http_status": result.http_status, "at": now.isoformat()})
            if not result.retryable:
                state["last_slot"] = slot
            else:
                delay = result.retry_after_seconds if result.retry_after_seconds is not None else 30
                state["retry_after_epoch"] = now.timestamp() + max(1, float(delay))
            self._save(day, state)
            return {"status": "failed", "slot": slot}
        state["sent_symbols"] += symbols
        state["last_slot"] = slot
        state.pop("retry_after_epoch", None)
        self._save(day, state)
        self._record(day, {"slot": slot, "state": "DELIVERED", "symbols": symbols,
                           "event_ids": [e["event_id"] for e in selected],
                           "http_status": result.http_status, "at": now.isoformat()})
        return {"status": "delivered", "slot": slot, "symbols": symbols}

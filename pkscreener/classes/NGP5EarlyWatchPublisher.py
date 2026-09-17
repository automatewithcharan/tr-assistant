"""Standalone NG P5 EARLY WATCH publisher.

This module is deliberately isolated from SignalMonitor.  It only reads the
append-only full-universe live feature recorder (and, optionally, existing
alert-timing telemetry), while all of its state and evidence live beneath a
publisher-owned directory.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
POLICY_VERSION = "ng_p5_frozen_v1"
DEDUPE_VERSION = "one_p5_per_symbol_session_v1"
STATE_SCHEMA_VERSION = 3
EVIDENCE_SCHEMA_VERSION = 1

MARKET_OPEN_MINUTE = 9 * 60 + 15
P5_START_MINUTE = 9 * 60 + 30
P5_END_MINUTE = 15 * 60

P5_MIN_DAY_CHANGE_PCT = 1.0
P5_MAX_DAY_CHANGE_PCT = 3.5
P5_MIN_PRICE = 50.0
P5_MIN_TURNOVER = 30_000_000.0
P5_MAX_RANK = 200
P5_MIN_RANK_IMPROVEMENT = 25
P5_MIN_MOVE_DELTA_PCT = 0.5
CHECKPOINT_BYTES = 512
OPENING_SCAN_LIMIT_BYTES = 16 * 1024 * 1024
REQUIRED_RECORDER_GENERATION = 6
REQUIRED_OPENING_MAX_GAP_SECONDS = 45.0
# If recording starts at the latest accepted time (09:20), covering the ten
# minutes through 09:30 with gaps <=45 seconds requires at least 15 snapshots.
REQUIRED_OPENING_MIN_SNAPSHOTS = 15
PREFIX_CHAIN_ALGORITHM = "sha256-line-chain-v1"
PREFIX_CHAIN_SEED = hashlib.sha256(b"").hexdigest()

# Authoritative historical evaluator provenance (recovered from the research
# execution which produced P1-P5):
#   opening = minute.between(555, 570, inclusive="left")
#   post    = minute.between(570, 900, inclusive="left")
# Recorder timestamps are IST wall-clock values.  These constants therefore
# deliberately encode [09:15, 09:30) and [09:30, 15:00).
RESEARCH_PROVENANCE = "fresh_accel_extract.py:minute.between(inclusive=left)"

REQUIRED_COLUMNS = frozenset(
    {
        "When",
        "Exchange",
        "Symbol",
        "LastPrice",
        "DayChangePct",
        "DayChangeRank",
        "PreviousDayChangeRank",
        "RankImprovement",
        "DayChangeDeltaPct",
        "TradedValue",
    }
)

UPGRADE_STATES = {
    "buy_now_board": "Buy Now",
    "early_breakout": "Early Breakout qualified candidate",
    "top_gainer_radar": "Top Gainer Radar qualified candidate",
    "anomaly": "Anomaly qualified candidate",
    "pre_move": "Pre-Move qualified candidate",
}

OPENING_COVERAGE_VALID = "OPENING_COVERAGE_VALID"
OPENING_COVERAGE_INVALID = "OPENING_COVERAGE_INVALID"
OPENING_COVERAGE_UNKNOWN = "OPENING_COVERAGE_UNKNOWN"
EXPIRED_STALE_SESSION = "EXPIRED_STALE_SESSION"
_SESSION_FILE_RE = re.compile(r"live_features_(\d{4}-\d{2}-\d{2})\.csv$")


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_int(value: Any) -> Optional[int]:
    number = _finite_float(value)
    if number is None:
        return None
    return int(number)


def _strict_true(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {"true", "1", "yes"}


def _valid_session_date(value: str) -> str:
    text = str(value or "").strip()
    date.fromisoformat(text)
    return text


def _parse_ist_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _minute_of_day(value: datetime) -> float:
    local = value.astimezone(IST)
    return local.hour * 60 + local.minute + local.second / 60.0


def time_bucket(value: datetime) -> Optional[str]:
    minute = _minute_of_day(value)
    if 570 <= minute < 630:
        return "09:30–10:30"
    if 630 <= minute < 720:
        return "10:30–12:00"
    if 720 <= minute < 810:
        return "12:00–13:30"
    if 810 <= minute < 900:
        return "13:30–15:00"
    return None


def deterministic_event_id(session_date: str, symbol: str) -> str:
    raw = f"{POLICY_VERSION}|{session_date}|{symbol.strip().upper()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def source_row_id(raw_line: bytes) -> str:
    return hashlib.sha256(raw_line.rstrip(b"\r\n")).hexdigest()


def is_major_opening_leader(row: Mapping[str, Any]) -> bool:
    timestamp = _parse_ist_timestamp(row.get("When"))
    rank = _finite_int(row.get("DayChangeRank"))
    change = _finite_float(row.get("DayChangePct"))
    if timestamp is None or rank is None or change is None:
        return False
    minute = _minute_of_day(timestamp)
    return (
        MARKET_OPEN_MINUTE <= minute < P5_START_MINUTE
        and 1 <= rank <= P5_MAX_RANK
        and change >= 0.5
    )


@dataclass(frozen=True)
class P5Evaluation:
    qualifies: bool
    reason: str
    inputs: Dict[str, Any]


@dataclass(frozen=True)
class OpeningCoverage:
    state: str
    reason: str
    metadata: Dict[str, Any]


def evaluate_p5(row: Mapping[str, Any], opening_leaders: Iterable[str]) -> P5Evaluation:
    """Evaluate the exact frozen P5 contract from one causal recorder row."""
    timestamp = _parse_ist_timestamp(row.get("When"))
    symbol = str(row.get("Symbol") or "").strip().upper()
    exchange = str(row.get("Exchange") or "").strip().upper()
    price = _finite_float(row.get("LastPrice"))
    day_change = _finite_float(row.get("DayChangePct"))
    open_change = _finite_float(row.get("OpenChangePct"))
    rank = _finite_int(row.get("DayChangeRank"))
    previous_rank = _finite_int(row.get("PreviousDayChangeRank"))
    improvement = _finite_int(row.get("RankImprovement"))
    move_delta = _finite_float(row.get("DayChangeDeltaPct"))
    turnover = _finite_float(row.get("TradedValue"))
    volume = _finite_int(row.get("Volume"))
    buy_sell_ratio = _finite_float(row.get("BuyerSellerRatio"))
    spread_bps = _finite_float(row.get("SpreadBps"))
    pullback_pct = _finite_float(row.get("PullbackFromHighPct"))
    opening_set = {str(item).strip().upper() for item in opening_leaders}
    bucket = time_bucket(timestamp) if timestamp is not None else None
    inputs = {
        "policy_version": POLICY_VERSION,
        "observation_timestamp": timestamp.isoformat() if timestamp else None,
        "session_date": timestamp.date().isoformat() if timestamp else None,
        "time_bucket": bucket,
        "symbol": symbol or None,
        "exchange": exchange or None,
        "last_price": price,
        "day_change_pct": day_change,
        "open_change_pct": open_change,
        "current_rank": rank,
        "previous_rank": previous_rank,
        "rank_improvement": improvement,
        "rank_velocity_per_minute": _finite_float(row.get("RankVelocityPerMinute")),
        "move_delta_pct": move_delta,
        "turnover": turnover,
        "cumulative_volume": volume,
        "buy_sell_ratio": buy_sell_ratio,
        "spread_bps": spread_bps,
        "pullback_from_hod_pct": pullback_pct,
        "market_breadth_pct": _finite_float(row.get("MarketBreadthPct")),
        "sector": str(row.get("Sector") or "").strip() or None,
        "sector_avg_change_pct": _finite_float(row.get("SectorAvgChangePct")),
        "sector_breadth_pct": _finite_float(row.get("SectorBreadthPct")),
        "was_major_opening_leader": symbol in opening_set if symbol else None,
    }
    checks = (
        (timestamp is not None and bucket is not None, "outside 09:30–15:00 IST"),
        (bool(symbol), "missing symbol"),
        (exchange == "NSE", "exchange is not NSE"),
        (symbol not in opening_set, "major opening leader"),
        (price is not None and price >= P5_MIN_PRICE, "price below ₹50"),
        (
            day_change is not None
            and P5_MIN_DAY_CHANGE_PCT <= day_change <= P5_MAX_DAY_CHANGE_PCT,
            "day change outside +1.0% to +3.5%",
        ),
        (turnover is not None and turnover >= P5_MIN_TURNOVER, "turnover below ₹3 crore"),
        (rank is not None and 1 <= rank <= P5_MAX_RANK, "rank above 200 or invalid"),
        (
            improvement is not None and improvement >= P5_MIN_RANK_IMPROVEMENT,
            "rank improvement below 25",
        ),
        (
            move_delta is not None and move_delta >= P5_MIN_MOVE_DELTA_PCT,
            "move delta below +0.5 point",
        ),
    )
    for passed, reason in checks:
        if not passed:
            return P5Evaluation(False, reason, inputs)
    return P5Evaluation(True, "Fresh post-open acceleration / P5 qualification", inputs)


def tradingview_link(symbol: str) -> str:
    ticker = urllib.parse.quote(f"NSE:{symbol.strip().upper()}", safe="")
    return f"https://www.tradingview.com/chart/?symbol={ticker}"


def _display(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _finite_float(value)
    return "UNKNOWN" if number is None else f"{number:.{digits}f}{suffix}"


def build_early_watch_message(event: Mapping[str, Any]) -> str:
    i = event["policy_inputs"]
    symbol = str(i.get("symbol") or "UNKNOWN")
    context_parts = []
    if i.get("market_breadth_pct") is not None:
        context_parts.append(f"Market breadth {_display(i.get('market_breadth_pct'), 2, '%')}")
    sector = str(i.get("sector") or "").strip()
    if sector and sector.upper() != "UNKNOWN":
        context_parts.append(f"Sector {sector}")
    if i.get("sector_avg_change_pct") is not None:
        context_parts.append(f"sector avg {_display(i.get('sector_avg_change_pct'), 2, '%')}")
    if i.get("sector_breadth_pct") is not None:
        context_parts.append(f"sector breadth {_display(i.get('sector_breadth_pct'), 2, '%')}")
    context_line = f"Context: {' | '.join(context_parts)}\n" if context_parts else ""
    return (
        "🟡 **NG EARLY WATCH**\n"
        "**NOT A BUY SIGNAL**\n\n"
        f"**`{symbol}`** — {i.get('observation_timestamp') or 'UNKNOWN'} IST\n"
        f"LTP: ₹{_display(i.get('last_price'))} | Day: {_display(i.get('day_change_pct'), 3, '%')} "
        f"| From open: {_display(i.get('open_change_pct'), 3, '%')}\n"
        f"Rank: {i.get('current_rank') if i.get('current_rank') is not None else 'UNKNOWN'} "
        f"(previous {i.get('previous_rank') if i.get('previous_rank') is not None else 'UNKNOWN'}, "
        f"improvement {i.get('rank_improvement') if i.get('rank_improvement') is not None else 'UNKNOWN'})\n"
        f"Move delta: {_display(i.get('move_delta_pct'), 3, ' pt')} | "
        f"Turnover: ₹{_display((_finite_float(i.get('turnover')) or 0) / 10_000_000, 2, ' Cr')} | "
        f"Volume: {i.get('cumulative_volume') if i.get('cumulative_volume') is not None else 'UNKNOWN'}\n"
        f"B/S: {_display(i.get('buy_sell_ratio'), 3)} | Spread: {_display(i.get('spread_bps'), 2, ' bps')} | "
        f"Pullback from known HOD: {_display(i.get('pullback_from_hod_pct'), 3, '%')}\n"
        f"Time bucket: {i.get('time_bucket') or 'UNKNOWN'}\n\n"
        f"{context_line}"
        "**Why alerted:** Fresh post-open acceleration / P5 qualification\n"
        "**Review:** Open TradingView → inspect structure → **PAPER or SKIP**\n"
        f"[Open NSE:{symbol} in TradingView]({tradingview_link(symbol)})\n"
        f"Event: `{event['event_id'][:16]}`"
    )


def build_upgrade_message(upgrade: Mapping[str, Any]) -> str:
    original = upgrade["original_p5"]
    return (
        "🟢 **NG CANDIDATE UPGRADE**\n\n"
        f"**`{upgrade['symbol']}`** — {upgrade['current_state']}\n"
        f"Current: {upgrade['upgrade_timestamp']} IST | ₹{_display(upgrade.get('price'))} "
        f"| Day {_display(upgrade.get('day_change_pct'), 3, '%')}\n"
        f"Original P5: {original['observation_timestamp']} IST | "
        f"Day {_display(original.get('day_change_pct'), 3, '%')}\n"
        f"Elapsed: {_display(upgrade.get('elapsed_minutes'), 1, ' min')}\n\n"
        "Existing scanner qualification only; this message does not create a new rule.\n"
        f"Event: `{upgrade['event_id'][:16]}`"
    )


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    http_status: Optional[int]
    retryable: bool
    retry_after_seconds: Optional[float] = None
    error: Optional[str] = None
    message_id: Optional[str] = None


def validate_webhook_url(webhook_url: str) -> str:
    value = str(webhook_url or "").strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Discord webhook must be a valid HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("Discord webhook URL must not contain URL credentials")
    return value


class DiscordWebhookTransport:
    USER_AGENT = "NG-Quality-Watch-Publisher/1.0"

    def __init__(self, webhook_url: str, timeout_seconds: float = 10.0) -> None:
        self.webhook_url = validate_webhook_url(webhook_url)
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    def post(self, message: str) -> DeliveryResult:
        if not self.webhook_url:
            return DeliveryResult(False, None, True, error="webhook_not_configured")
        parsed = urllib.parse.urlparse(self.webhook_url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key.lower() != "wait"]
        query.append(("wait", "true"))
        post_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
        request = urllib.request.Request(
            post_url,
            data=json.dumps({"content": message}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                raw = response.read()
                message_id = None
                if 200 <= status < 300 and raw:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                        message_id = str(payload.get("id") or "") or None
                    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                        message_id = None
                return DeliveryResult(
                    200 <= status < 300,
                    status,
                    status == 429 or status >= 500,
                    message_id=message_id,
                )
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            retry_after = None
            if status == 429:
                raw = exc.headers.get("Retry-After") if exc.headers else None
                retry_after = _finite_float(raw)
                if retry_after is None:
                    try:
                        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                        retry_after = _finite_float(payload.get("retry_after"))
                    except Exception:
                        retry_after = None
            return DeliveryResult(
                False,
                status,
                status == 429 or 500 <= status <= 599,
                retry_after_seconds=retry_after,
                error=f"http_{status}",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return DeliveryResult(False, None, True, error=type(exc).__name__)


class NGP5EarlyWatchPublisher:
    def __init__(
        self,
        source_dir: Path | str,
        state_dir: Path | str,
        *,
        transport: Optional[Any] = None,
        upgrade_source_dir: Optional[Path | str] = None,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 5.0,
        clock: Optional[Callable[[], datetime]] = None,
        coverage_status_path: Optional[Path | str] = None,
        coverage_history_path: Optional[Path | str] = None,
        shadow_experiment_recorder: Optional[Any] = None,
        p5_rejection_observer: Optional[Any] = None,
    ) -> None:
        self.source_dir = Path(source_dir)
        self.state_dir = Path(state_dir)
        self.upgrade_source_dir = Path(upgrade_source_dir) if upgrade_source_dir else None
        self.transport = transport
        self.max_attempts = max(1, int(max_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.clock = clock or _now_ist
        # Experiment 4 is an observational sink only.  Its return value is never
        # consulted by qualification, queuing, delivery, or deduplication.
        self.shadow_experiment_recorder = shadow_experiment_recorder
        # This side channel cannot participate in policy, state, or delivery.
        self.p5_rejection_observer = p5_rejection_observer
        self.coverage_status_path = (
            Path(coverage_status_path)
            if coverage_status_path is not None
            else self.source_dir / "full_universe_rank_status.json"
        )
        self.coverage_history_path = (
            Path(coverage_history_path)
            if coverage_history_path is not None
            else self.source_dir.parent
            / "full_universe_rank_strategy"
            / "session_coverage.csv"
        )
        self.state_path = self.state_dir / "state.json"
        self._verified_source_prefixes: set[str] = set()
        self._validated_opening_sources: Dict[
            str, tuple[int, int, int, OpeningCoverage]
        ] = {}
        self._opening_scanned_this_cycle: set[str] = set()
        self.state = self._load_state()
        self._state_dirty = False
        self._reconcile_evidence()

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "dedupe_version": DEDUPE_VERSION,
            "sessions": {},
            "sources": {},
            "pending": {},
            "operational_events": {},
        }

    def _load_state(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (
                payload.get("schema_version") == STATE_SCHEMA_VERSION
                and payload.get("policy_version") == POLICY_VERSION
                and payload.get("dedupe_version") == DEDUPE_VERSION
            ):
                payload.setdefault("sessions", {})
                payload.setdefault("sources", {})
                payload.setdefault("pending", {})
                payload.setdefault("operational_events", {})
                return payload
        except Exception:
            pass
        return self._empty_state()

    def _save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.state_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self._state_dirty = False

    def _save_if_dirty(self) -> None:
        if self._state_dirty:
            self._save_state()

    def _journal_path(self, session_date: str) -> Path:
        return self.state_dir / "evidence" / f"events_{session_date}.jsonl"

    def _append_evidence(self, session_date: str, payload: Mapping[str, Any]) -> None:
        path = self._journal_path(session_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "recorded_at": self.clock().astimezone(IST).isoformat(),
            **dict(payload),
        }
        data = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _record_operational(
        self,
        session_date: str,
        operational_state: str,
        reason: str,
        *,
        source_path: Optional[Path | str] = None,
        byte_offset: Optional[int] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        identity = "|".join(
            (
                str(session_date),
                str(operational_state),
                str(reason),
                str(Path(source_path).resolve()) if source_path else "",
                str(byte_offset) if byte_offset is not None else "",
                json.dumps(dict(details or {}), sort_keys=True, default=str),
            )
        )
        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if event_id in self.state["operational_events"]:
            return False
        payload = {
            "event_id": event_id,
            "delivery_id": event_id,
            "event_kind": "operational",
            "delivery_state": operational_state,
            "delivery_attempts": 0,
            "http_status": None,
            "delivered_timestamp": None,
            "terminal": operational_state
            in {OPENING_COVERAGE_INVALID, "SOURCE_QUARANTINED", EXPIRED_STALE_SESSION},
            "session_date": session_date,
            "reason": reason,
            "source_path": str(Path(source_path).resolve()) if source_path else None,
            "byte_offset": byte_offset,
            "details": dict(details or {}),
        }
        self._append_evidence(session_date, payload)
        self.state["operational_events"][event_id] = payload
        self._state_dirty = True
        return True

    def _session(self, session_date: str) -> Dict[str, Any]:
        if session_date not in self.state["sessions"]:
            self.state["sessions"][session_date] = {
                "opening_leaders": [],
                "observed": {},
                "upgraded": [],
                "opening_coverage": None,
            }
            self._state_dirty = True
        session = self.state["sessions"][session_date]
        session.setdefault("opening_leaders", [])
        session.setdefault("observed", {})
        session.setdefault("upgraded", [])
        session.setdefault("opening_coverage", None)
        return session

    def _reconcile_evidence(self) -> None:
        if not (self.state_dir / "evidence").exists():
            return
        transitions: Dict[str, str] = {}
        observed_events: Dict[str, Dict[str, Any]] = {}
        for path in sorted((self.state_dir / "evidence").glob("events_*.jsonl")):
            try:
                handle = path.open("r", encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    event_id = str(row.get("event_id") or "")
                    delivery_id = str(row.get("delivery_id") or event_id)
                    state = str(row.get("delivery_state") or "")
                    if row.get("event_kind") == "operational" and event_id:
                        if event_id not in self.state["operational_events"]:
                            self.state["operational_events"][event_id] = row
                            self._state_dirty = True
                    if state == "OBSERVED" and row.get("event"):
                        observed_events[delivery_id] = dict(row["event"])
                        event = row["event"]
                        if event.get("kind") == "p5":
                            day = str(event.get("session_date") or "")
                            symbol = str(event.get("symbol") or "").upper()
                            if day and symbol:
                                if symbol not in self._session(day)["observed"]:
                                    self._session(day)["observed"][symbol] = event
                                    self._state_dirty = True
                        elif event.get("kind") == "upgrade":
                            day = str(event.get("session_date") or "")
                            symbol = str(event.get("symbol") or "").upper()
                            if day and symbol:
                                session = self._session(day)
                                session["upgraded"] = sorted(
                                    set(session.get("upgraded", [])) | {symbol}
                                )
                                self._state_dirty = True
                    if state in {"DELIVERED", "FAILED", EXPIRED_STALE_SESSION, "ARCHIVED_FOR_DIGEST"} and bool(
                        row.get("terminal")
                    ):
                        transitions[delivery_id] = state
        for delivery_id, event in observed_events.items():
            if delivery_id not in transitions and delivery_id not in self.state["pending"]:
                self.state["pending"][delivery_id] = {
                    "event": event,
                    "message": build_early_watch_message(event) if event.get("kind") == "p5" else build_upgrade_message(event),
                    "attempts": 0,
                    "next_attempt_at": 0.0,
                    "last_delivery_state": "OBSERVED",
                }
                self._state_dirty = True
        for delivery_id in transitions:
            if self.state["pending"].pop(delivery_id, None) is not None:
                self._state_dirty = True
        self._save_if_dirty()

    def _source_cursor(self, path: Path) -> Dict[str, Any]:
        key = str(path.resolve())
        if key not in self.state["sources"]:
            self.state["sources"][key] = {
                "device": None,
                "inode": None,
                "offset": 0,
                "header": None,
                "header_sha256": None,
                "checkpoint_length": 0,
                "checkpoint_sha256": None,
                "prefix_chain_algorithm": PREFIX_CHAIN_ALGORITHM,
                "prefix_chain_sha256": PREFIX_CHAIN_SEED,
            }
            self._state_dirty = True
        return self.state["sources"][key]

    def _coverage_metadata(self, session_date: str) -> tuple[Optional[Dict[str, Any]], str]:
        try:
            live = json.loads(self.coverage_status_path.read_text(encoding="utf-8"))
            if str(live.get("date") or "") == session_date:
                return dict(live), str(self.coverage_status_path.resolve())
        except (OSError, TypeError, json.JSONDecodeError):
            pass
        try:
            with self.coverage_history_path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get("date") or "") == session_date:
                        return dict(row), str(self.coverage_history_path.resolve())
        except (OSError, csv.Error):
            pass
        return None, ""

    @staticmethod
    def _opening_source_continuity(
        path: Path, session_date: str, expected_generation: int
    ) -> OpeningCoverage:
        if not path.exists():
            return OpeningCoverage(
                OPENING_COVERAGE_UNKNOWN, "source file is absent", {}
            )
        consumed = 0
        snapshots: set[datetime] = set()
        first_source: Optional[datetime] = None
        boundary_snapshot: Optional[datetime] = None
        try:
            with path.open("rb") as handle:
                header_raw = handle.readline()
                consumed += len(header_raw)
                if not header_raw.endswith(b"\n"):
                    return OpeningCoverage(
                        OPENING_COVERAGE_INVALID, "source header is incomplete", {}
                    )
                try:
                    header = next(csv.reader([header_raw.decode("utf-8").rstrip("\r\n")]))
                except (UnicodeDecodeError, csv.Error, StopIteration):
                    return OpeningCoverage(
                        OPENING_COVERAGE_INVALID, "source header is malformed", {}
                    )
                coverage_columns = set(REQUIRED_COLUMNS) | {"RecorderGeneration"}
                if not coverage_columns.issubset(set(header)) or len(header) != len(set(header)):
                    return OpeningCoverage(
                        OPENING_COVERAGE_INVALID,
                        "source header is missing required coverage columns",
                        {},
                    )
                while consumed <= OPENING_SCAN_LIMIT_BYTES:
                    raw = handle.readline()
                    if not raw:
                        break
                    consumed += len(raw)
                    if not raw.endswith(b"\n"):
                        break
                    try:
                        values = next(csv.reader([raw.decode("utf-8").rstrip("\r\n")]))
                    except (UnicodeDecodeError, csv.Error, StopIteration):
                        return OpeningCoverage(
                            OPENING_COVERAGE_INVALID,
                            "opening source row is malformed",
                            {"bytes_scanned": consumed},
                        )
                    if len(values) != len(header):
                        return OpeningCoverage(
                            OPENING_COVERAGE_INVALID,
                            "opening source row width does not match header",
                            {"bytes_scanned": consumed},
                        )
                    row = dict(zip(header, values))
                    timestamp = _parse_ist_timestamp(row.get("When"))
                    if timestamp is None:
                        return OpeningCoverage(
                            OPENING_COVERAGE_INVALID,
                            "opening source timestamp is malformed",
                            {"bytes_scanned": consumed},
                        )
                    if timestamp.date().isoformat() != session_date:
                        return OpeningCoverage(
                            OPENING_COVERAGE_INVALID,
                            "opening source row belongs to a different IST session",
                            {"bytes_scanned": consumed, "row_timestamp": timestamp.isoformat()},
                        )
                    if first_source is None:
                        first_source = timestamp
                    generation = _finite_int(row.get("RecorderGeneration"))
                    rank = _finite_float(row.get("DayChangeRank"))
                    if generation != expected_generation or rank is None or rank <= 0:
                        continue
                    snapshots.add(timestamp)
                    if _minute_of_day(timestamp) >= P5_START_MINUTE:
                        boundary_snapshot = timestamp
                        break
        except OSError as exc:
            return OpeningCoverage(
                OPENING_COVERAGE_UNKNOWN,
                f"source cannot be read: {type(exc).__name__}",
                {},
            )
        details: Dict[str, Any] = {
            "bytes_scanned": consumed,
            "first_source_timestamp": first_source.isoformat() if first_source else None,
            "boundary_snapshot": boundary_snapshot.isoformat() if boundary_snapshot else None,
            "opening_snapshot_count": len(snapshots),
            "required_recorder_generation": expected_generation,
            "required_max_gap_seconds": REQUIRED_OPENING_MAX_GAP_SECONDS,
            "required_min_snapshots": REQUIRED_OPENING_MIN_SNAPSHOTS,
        }
        if first_source is None:
            return OpeningCoverage(
                OPENING_COVERAGE_UNKNOWN, "source has no complete data row", details
            )
        if first_source.hour * 60 + first_source.minute > 9 * 60 + 20:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "source opening coverage starts after 09:20 IST",
                details,
            )
        if boundary_snapshot is None:
            reason = (
                "source opening prefix exceeds bounded validation scan"
                if consumed > OPENING_SCAN_LIMIT_BYTES
                else "source has not yet reached the 09:30 IST coverage boundary"
            )
            state = (
                OPENING_COVERAGE_INVALID
                if consumed > OPENING_SCAN_LIMIT_BYTES
                else OPENING_COVERAGE_UNKNOWN
            )
            return OpeningCoverage(state, reason, details)
        ordered = sorted(snapshots)
        opening = [item for item in ordered if _minute_of_day(item) < P5_START_MINUTE]
        if not opening:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "source prefix has no valid opening snapshots",
                details,
            )
        first = opening[0]
        details["first_valid_snapshot"] = first.isoformat()
        if first.hour * 60 + first.minute > 9 * 60 + 20:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "source opening coverage starts after 09:20 IST",
                details,
            )
        gaps = [
            (right - left).total_seconds()
            for left, right in zip(ordered, ordered[1:])
        ]
        max_gap = max(gaps, default=0.0)
        details["max_gap_seconds"] = max_gap
        if max_gap > REQUIRED_OPENING_MAX_GAP_SECONDS:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "source opening coverage exceeds the research maximum snapshot gap",
                details,
            )
        if len(ordered) < REQUIRED_OPENING_MIN_SNAPSHOTS:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "source opening coverage has insufficient snapshots",
                details,
            )
        return OpeningCoverage(
            OPENING_COVERAGE_VALID,
            "source proves continuous generation-6 opening coverage",
            details,
        )

    def opening_coverage(self, session_date: str, source_path: Path | str) -> OpeningCoverage:
        session_date = _valid_session_date(session_date)
        path = Path(source_path)
        metadata, metadata_source = self._coverage_metadata(session_date)
        if metadata is None:
            return OpeningCoverage(
                OPENING_COVERAGE_UNKNOWN,
                "authoritative full-session eligibility metadata is absent",
                {"metadata_source": None},
            )
        evidence = {"metadata_source": metadata_source, **metadata}
        if not _strict_true(metadata.get("full_session_eligible")):
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "authoritative full_session_eligible is false",
                evidence,
            )
        expected_text = str(
            metadata.get("first_recorded_at") or metadata.get("first_snapshot") or ""
        ).strip()
        expected = _parse_ist_timestamp(expected_text)
        if expected is None or expected.date().isoformat() != session_date:
            return OpeningCoverage(
                OPENING_COVERAGE_UNKNOWN,
                "authoritative first opening timestamp is absent or invalid",
                evidence,
            )
        expected_generation = _finite_int(metadata.get("recorder_generation"))
        if expected_generation != REQUIRED_RECORDER_GENERATION:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "authoritative recorder generation is not research-valid generation 6",
                evidence,
            )
        if metadata_source.endswith("session_coverage.csv"):
            continuity_ok = _strict_true(metadata.get("continuity_ok"))
            max_gap = _finite_float(metadata.get("max_gap_seconds"))
            allowed_gap = _finite_float(metadata.get("required_max_gap_seconds"))
            snapshots = _finite_int(metadata.get("snapshots"))
            required_snapshots = _finite_int(metadata.get("required_min_snapshots"))
            if (
                not continuity_ok
                or max_gap is None
                or allowed_gap is None
                or max_gap > allowed_gap
                or snapshots is None
                or required_snapshots is None
                or snapshots < required_snapshots
            ):
                return OpeningCoverage(
                    OPENING_COVERAGE_INVALID,
                    "historical coverage artifact fails its authoritative continuity contract",
                    evidence,
                )
        source_key = str(path.resolve())
        try:
            source_stat = path.stat()
        except OSError:
            source_stat = None
        cached = self._validated_opening_sources.get(source_key)
        if cached is not None and source_stat is not None:
            device, inode, proof_bytes, cached_coverage = cached
            cached_first = _parse_ist_timestamp(
                cached_coverage.metadata.get("first_source_timestamp")
            )
            if (
                (source_stat.st_dev, source_stat.st_ino) == (device, inode)
                and source_stat.st_size >= proof_bytes
                and cached_first == expected
            ):
                return cached_coverage
        source_coverage = self._opening_source_continuity(
            path, session_date, expected_generation
        )
        self._opening_scanned_this_cycle.add(source_key)
        source_evidence = {**evidence, **source_coverage.metadata}
        first_source = _parse_ist_timestamp(
            source_coverage.metadata.get("first_source_timestamp")
        )
        if first_source is not None and first_source != expected:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "source prefix does not match authoritative first opening timestamp",
                {
                    **source_evidence,
                    "expected_first_timestamp": expected.isoformat(),
                },
            )
        if source_coverage.state != OPENING_COVERAGE_VALID:
            return OpeningCoverage(
                source_coverage.state, source_coverage.reason, source_evidence
            )
        assert first_source is not None
        if first_source.date().isoformat() != session_date:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "source prefix belongs to a different IST session",
                source_evidence,
            )
        if expected.hour * 60 + expected.minute > 9 * 60 + 20:
            return OpeningCoverage(
                OPENING_COVERAGE_INVALID,
                "authoritative opening coverage starts after 09:20 IST",
                source_evidence,
            )
        result = OpeningCoverage(
            OPENING_COVERAGE_VALID,
            "authoritative eligibility and continuous opening source coverage agree",
            source_evidence,
        )
        if source_stat is not None:
            self._validated_opening_sources[source_key] = (
                source_stat.st_dev,
                source_stat.st_ino,
                int(source_evidence.get("bytes_scanned") or 0),
                result,
            )
        return result

    def _record_opening_coverage(
        self, session_date: str, source_path: Path, coverage: OpeningCoverage
    ) -> None:
        session = self._session(session_date)
        current = {
            "state": coverage.state,
            "reason": coverage.reason,
            "metadata": coverage.metadata,
        }
        if session.get("opening_coverage") == current:
            return
        self._record_operational(
            session_date,
            coverage.state,
            coverage.reason,
            source_path=source_path,
            details=coverage.metadata,
        )
        session["opening_coverage"] = current
        self._state_dirty = True

    @staticmethod
    def _checkpoint_before(handle: Any, offset: int) -> bytes:
        length = min(CHECKPOINT_BYTES, max(0, int(offset)))
        if length <= 0:
            return b""
        handle.seek(int(offset) - length)
        return handle.read(length)

    @staticmethod
    def _reset_cursor(cursor: Dict[str, Any], stat: os.stat_result) -> None:
        cursor.update(
            {
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "offset": 0,
                "header": None,
                "header_sha256": None,
                "checkpoint_length": 0,
                "checkpoint_sha256": None,
                "prefix_chain_algorithm": PREFIX_CHAIN_ALGORITHM,
                "prefix_chain_sha256": PREFIX_CHAIN_SEED,
            }
        )

    @staticmethod
    def _chain_update(chain_sha256: str, raw: bytes) -> str:
        return hashlib.sha256(bytes.fromhex(chain_sha256) + raw).hexdigest()

    def _verify_consumed_prefix(self, handle: Any, cursor: Mapping[str, Any]) -> bool:
        target = int(cursor.get("offset", 0))
        expected = str(cursor.get("prefix_chain_sha256") or "")
        if (
            target <= 0
            or cursor.get("prefix_chain_algorithm") != PREFIX_CHAIN_ALGORITHM
            or not expected
        ):
            return target <= 0
        chain = PREFIX_CHAIN_SEED
        handle.seek(0)
        while handle.tell() < target:
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n") or handle.tell() > target:
                return False
            chain = self._chain_update(chain, raw)
        return handle.tell() == target and chain == expected

    def _complete_lines(
        self, path: Path, session_date: str
    ) -> Iterable[tuple[int, int, bytes]]:
        """Yield unread complete lines and commit a cursor only after each resume.

        The caller must stop iteration on ambiguous input. Because cursor advance
        happens after the yielded row has been handled, that row will be replayed
        after any processing exception or process crash.
        """
        if not path.exists():
            return
        cursor = self._source_cursor(path)
        source_key = str(path.resolve())
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            previous_identity = (cursor.get("device"), cursor.get("inode"))
            current_identity = (stat.st_dev, stat.st_ino)
            reset_reason = None
            if previous_identity != (None, None) and previous_identity != current_identity:
                reset_reason = "source inode/device changed; replaying safely from byte zero"
            elif int(cursor.get("offset", 0)) > stat.st_size:
                reset_reason = "source size shrank below durable cursor; replaying safely from byte zero"
            elif source_key not in self._verified_source_prefixes and int(
                cursor.get("offset", 0)
            ) > 0:
                if not self._verify_consumed_prefix(handle, cursor):
                    reset_reason = (
                        "consumed source prefix integrity changed; "
                        "same-inode earlier rewrite detected on restart"
                    )
            elif int(cursor.get("offset", 0)) > 0 and cursor.get("checkpoint_sha256"):
                checkpoint = self._checkpoint_before(handle, int(cursor["offset"]))
                if (
                    len(checkpoint) != int(cursor.get("checkpoint_length", 0))
                    or hashlib.sha256(checkpoint).hexdigest()
                    != cursor.get("checkpoint_sha256")
                ):
                    reset_reason = (
                        "source content before durable cursor changed; "
                        "same-inode truncate/regrow detected"
                    )
            if previous_identity == (None, None) or reset_reason:
                self._reset_cursor(cursor, stat)
                self._state_dirty = True
                if reset_reason:
                    freshly_validated = source_key in self._opening_scanned_this_cycle
                    self._validated_opening_sources.pop(source_key, None)
                    session = self._session(session_date)
                    session["opening_leaders"] = []
                    self._record_operational(
                        session_date,
                        "SOURCE_RESET_SAFE_REPLAY",
                        reset_reason,
                        source_path=path,
                        byte_offset=0,
                        details={
                            "previous_device": previous_identity[0],
                            "previous_inode": previous_identity[1],
                            "current_device": stat.st_dev,
                            "current_inode": stat.st_ino,
                            "current_size": stat.st_size,
                        },
                    )
                    if not freshly_validated:
                        revalidated = self.opening_coverage(session_date, path)
                        self._record_opening_coverage(session_date, path, revalidated)
                        if revalidated.state != OPENING_COVERAGE_VALID:
                            self._opening_scanned_this_cycle.discard(source_key)
                            return
            self._opening_scanned_this_cycle.discard(source_key)
            self._verified_source_prefixes.add(source_key)
            offset = int(cursor.get("offset", 0))
            tail = self._checkpoint_before(handle, offset)
            chain = str(cursor.get("prefix_chain_sha256") or PREFIX_CHAIN_SEED)
            handle.seek(offset)
            while True:
                start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    handle.seek(start)
                    break
                end = handle.tell()
                yield start, end, raw
                # Reaching this point proves the caller safely handled the row.
                offset = end
                tail = (tail + raw)[-CHECKPOINT_BYTES:]
                chain = self._chain_update(chain, raw)
                cursor["offset"] = offset
                cursor["device"] = stat.st_dev
                cursor["inode"] = stat.st_ino
                cursor["checkpoint_length"] = len(tail)
                cursor["checkpoint_sha256"] = (
                    hashlib.sha256(tail).hexdigest() if tail else None
                )
                cursor["prefix_chain_algorithm"] = PREFIX_CHAIN_ALGORITHM
                cursor["prefix_chain_sha256"] = chain
                self._state_dirty = True

    def _quarantine_source_row(
        self,
        session_date: str,
        path: Path,
        start: int,
        raw: bytes,
        reason: str,
    ) -> None:
        self._record_operational(
            session_date,
            "SOURCE_QUARANTINED",
            reason,
            source_path=path,
            byte_offset=start,
            details={
                "row_sha256": source_row_id(raw),
                "row_bytes": len(raw),
            },
        )

    def process_feature_file(
        self, path: Path | str, session_date: Optional[str] = None
    ) -> int:
        path = Path(path)
        if session_date is None:
            match = _SESSION_FILE_RE.search(path.name)
            if match is None:
                raise ValueError("feature source filename must contain an ISO session date")
            session_date = match.group(1)
        session_date = _valid_session_date(session_date)
        coverage = self.opening_coverage(session_date, path)
        self._record_opening_coverage(session_date, path, coverage)
        if coverage.state != OPENING_COVERAGE_VALID:
            self._save_if_dirty()
            return 0

        cursor = self._source_cursor(path)
        emitted = 0
        for start, end, raw in self._complete_lines(path, session_date):
            try:
                parsed = next(csv.reader([raw.decode("utf-8").rstrip("\r\n")]))
            except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
                self._quarantine_source_row(
                    session_date, path, start, raw, f"malformed CSV: {type(exc).__name__}"
                )
                break
            if cursor.get("header") is None:
                if (
                    not REQUIRED_COLUMNS.issubset(set(parsed))
                    or len(parsed) != len(set(parsed))
                ):
                    self._quarantine_source_row(
                        session_date,
                        path,
                        start,
                        raw,
                        "required source schema is absent or materially incompatible",
                    )
                    break
                cursor["header"] = parsed
                cursor["header_sha256"] = source_row_id(raw)
                self._state_dirty = True
                continue
            header = list(cursor["header"])
            if len(parsed) != len(header):
                self._quarantine_source_row(
                    session_date,
                    path,
                    start,
                    raw,
                    f"CSV row width {len(parsed)} does not match header width {len(header)}",
                )
                break
            row = dict(zip(header, parsed))
            timestamp = _parse_ist_timestamp(row.get("When"))
            symbol = str(row.get("Symbol") or "").strip().upper()
            if timestamp is None or not symbol:
                self._quarantine_source_row(
                    session_date,
                    path,
                    start,
                    raw,
                    "required row identity (When/Symbol) is malformed",
                )
                break
            day = timestamp.date().isoformat()
            if day != session_date:
                self._quarantine_source_row(
                    session_date,
                    path,
                    start,
                    raw,
                    f"row IST session {day} does not match source session {session_date}",
                )
                break
            session = self._session(day)
            opening = set(session.get("opening_leaders", []))
            if is_major_opening_leader(row):
                if symbol not in opening:
                    opening.add(symbol)
                    session["opening_leaders"] = sorted(opening)
                    self._state_dirty = True
                continue
            evaluation = evaluate_p5(row, opening)
            if self.p5_rejection_observer is not None:
                try:
                    self.p5_rejection_observer.record(
                        evaluation_inputs=copy.deepcopy(evaluation.inputs),
                        qualification_result=bool(evaluation.qualifies),
                        qualification_reason=str(evaluation.reason),
                        source_record={
                            "path": str(path.resolve()),
                            "byte_offset_start": start,
                            "byte_offset_end": end,
                            "row_sha256": source_row_id(raw),
                        },
                    )
                except Exception:
                    # A telemetry failure must not alter frozen P5 behavior.
                    pass
            if self.shadow_experiment_recorder is not None:
                try:
                    self.shadow_experiment_recorder.observe_p5(
                        row,
                        opening,
                        evaluation=evaluation,
                        source_record={
                            "path": str(path.resolve()),
                            "byte_offset_start": start,
                            "byte_offset_end": end,
                            "row_sha256": source_row_id(raw),
                        },
                    )
                except Exception:
                    # A shadow recorder failure must not alter or interrupt the
                    # frozen production qualification and delivery path.
                    pass
            if not evaluation.qualifies or symbol in session["observed"]:
                continue
            event_id = deterministic_event_id(day, symbol)
            event = {
                "kind": "p5",
                "event_id": event_id,
                "delivery_id": event_id,
                "session_date": day,
                "symbol": symbol,
                "observation_timestamp": evaluation.inputs["observation_timestamp"],
                "policy_version": POLICY_VERSION,
                "dedupe_version": DEDUPE_VERSION,
                "qualification_result": True,
                "qualification_reason": evaluation.reason,
                "policy_inputs": evaluation.inputs,
                "source_record": {
                    "path": str(path.resolve()),
                    "byte_offset_start": start,
                    "byte_offset_end": end,
                    "row_sha256": source_row_id(raw),
                },
            }
            message = build_early_watch_message(event)
            self._append_evidence(
                day,
                {
                    "event_id": event_id,
                    "delivery_id": event_id,
                    "event_kind": "p5",
                    "delivery_state": "OBSERVED",
                    "delivery_attempts": 0,
                    "http_status": None,
                    "delivered_timestamp": None,
                    "terminal": False,
                    "event": event,
                },
            )
            session["observed"][symbol] = event
            self.state["pending"][event_id] = {
                "event": event,
                "message": message,
                "attempts": 0,
                "next_attempt_at": 0.0,
                "last_delivery_state": "OBSERVED",
            }
            self._state_dirty = True
            emitted += 1
        self._save_if_dirty()
        return emitted

    def process_upgrade_file(self, path: Path | str) -> int:
        path = Path(path)
        session_date = _valid_session_date(path.stem)
        emitted = 0
        for start, end, raw in self._complete_lines(path, session_date):
            try:
                row = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if str(row.get("event_type") or "").upper() != "SOURCE_QUALIFIED":
                continue
            source = str(row.get("source") or "").lower()
            current_state = UPGRADE_STATES.get(source)
            if current_state is None:
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            timestamp = _parse_ist_timestamp(row.get("event_timestamp"))
            day = str(row.get("trading_date") or (timestamp.date().isoformat() if timestamp else ""))
            if not symbol or timestamp is None or not day:
                continue
            session = self._session(day)
            original = session.get("observed", {}).get(symbol)
            if not original or symbol in set(session.get("upgraded", [])):
                continue
            original_time = _parse_ist_timestamp(original.get("observation_timestamp"))
            if original_time is None or timestamp <= original_time:
                continue
            upgrade_id = hashlib.sha256(
                f"ng_candidate_upgrade_v1|{original['event_id']}|{source}".encode("utf-8")
            ).hexdigest()
            upgrade = {
                "kind": "upgrade",
                "event_id": upgrade_id,
                "delivery_id": upgrade_id,
                "session_date": day,
                "symbol": symbol,
                "current_state": current_state,
                "source": source,
                "upgrade_timestamp": timestamp.isoformat(),
                "price": _finite_float(row.get("price")),
                "day_change_pct": _finite_float(row.get("day_change_pct")),
                "elapsed_minutes": round((timestamp - original_time).total_seconds() / 60.0, 3),
                "original_p5": original["policy_inputs"],
                "source_record": {
                    "path": str(path.resolve()),
                    "byte_offset_start": start,
                    "byte_offset_end": end,
                    "row_sha256": source_row_id(raw),
                },
            }
            self._append_evidence(
                day,
                {
                    "event_id": upgrade_id,
                    "delivery_id": upgrade_id,
                    "event_kind": "upgrade",
                    "delivery_state": "OBSERVED",
                    "delivery_attempts": 0,
                    "http_status": None,
                    "delivered_timestamp": None,
                    "terminal": False,
                    "event": upgrade,
                },
            )
            session["upgraded"] = sorted(set(session.get("upgraded", [])) | {symbol})
            self.state["pending"][upgrade_id] = {
                "event": upgrade,
                "message": build_upgrade_message(upgrade),
                "attempts": 0,
                "next_attempt_at": 0.0,
                "last_delivery_state": "OBSERVED",
            }
            self._state_dirty = True
            emitted += 1
        self._save_if_dirty()
        return emitted

    def expire_stale_pending(self, current_session: str) -> int:
        current_session = _valid_session_date(current_session)
        expired = 0
        expiry_timestamp = self.clock().astimezone(IST).isoformat()
        for delivery_id in list(self.state["pending"]):
            pending = self.state["pending"].get(delivery_id)
            if pending is None:
                continue
            event = pending.get("event") or {}
            original_session = str(event.get("session_date") or "")
            if not original_session or original_session >= current_session:
                continue
            previous_state = str(pending.get("last_delivery_state") or "OBSERVED")
            self._append_evidence(
                original_session,
                {
                    "event_id": event.get("event_id") or delivery_id,
                    "delivery_id": delivery_id,
                    "event_kind": event.get("kind") or "p5",
                    "delivery_state": EXPIRED_STALE_SESSION,
                    "delivery_attempts": int(pending.get("attempts", 0)),
                    "http_status": None,
                    "delivered_timestamp": None,
                    "terminal": True,
                    "original_session": original_session,
                    "observed_timestamp": event.get("observation_timestamp")
                    or event.get("upgrade_timestamp"),
                    "expiry_timestamp": expiry_timestamp,
                    "reason": "pending event belongs to a prior IST trading session",
                    "previous_delivery_state": previous_state,
                },
            )
            self.state["pending"].pop(delivery_id, None)
            self._state_dirty = True
            expired += 1
        self._save_if_dirty()
        return expired

    def deliver_pending(self, current_session: str) -> int:
        current_session = _valid_session_date(current_session)
        self.expire_stale_pending(current_session)
        if self.transport is None:
            return 0
        delivered = 0
        now = self.clock().astimezone(IST)
        now_epoch = now.timestamp()
        for delivery_id in list(self.state["pending"]):
            pending = self.state["pending"].get(delivery_id)
            if pending is None or float(pending.get("next_attempt_at", 0.0)) > now_epoch:
                continue
            event = pending["event"]
            day = str(event.get("session_date") or "")
            if day != current_session:
                continue
            if int(pending.get("attempts", 0)) >= self.max_attempts:
                self._append_evidence(
                    day,
                    {
                        "event_id": event["event_id"],
                        "delivery_id": delivery_id,
                        "event_kind": event["kind"],
                        "delivery_state": "FAILED",
                        "delivery_attempts": int(pending.get("attempts", 0)),
                        "http_status": None,
                        "delivered_timestamp": None,
                        "failure": "max_attempts_already_reached",
                        "failure_category": "retry_budget_exhausted",
                        "retryable": False,
                        "terminal": True,
                    },
                )
                self.state["pending"].pop(delivery_id, None)
                self._state_dirty = True
                self._save_state()
                continue
            attempt = int(pending.get("attempts", 0)) + 1
            pending["attempts"] = attempt
            pending["last_delivery_state"] = "ATTEMPTED"
            attempt_timestamp = self.clock().astimezone(IST).isoformat()
            self._state_dirty = True
            self._append_evidence(
                day,
                {
                    "event_id": event["event_id"],
                    "delivery_id": delivery_id,
                    "event_kind": event["kind"],
                    "delivery_state": "ATTEMPTED",
                    "delivery_attempts": attempt,
                    "attempt_number": attempt,
                    "attempt_timestamp": attempt_timestamp,
                    "http_status": None,
                    "delivered_timestamp": None,
                    "terminal": False,
                },
            )
            self._save_state()
            try:
                result = self.transport.post(str(pending["message"]))
                if not isinstance(result, DeliveryResult):
                    raise TypeError("transport returned a non-DeliveryResult")
            except Exception as exc:
                result = DeliveryResult(
                    False,
                    None,
                    True,
                    error=f"unexpected_transport_exception:{type(exc).__name__}",
                )
            if result.delivered:
                delivered_at = self.clock().astimezone(IST).isoformat()
                self._append_evidence(
                    day,
                    {
                        "event_id": event["event_id"],
                        "delivery_id": delivery_id,
                        "event_kind": event["kind"],
                        "delivery_state": "DELIVERED",
                        "delivery_attempts": attempt,
                        "attempt_number": attempt,
                        "http_status": result.http_status,
                        "delivered_timestamp": delivered_at,
                        "terminal": True,
                    },
                )
                self.state["pending"].pop(delivery_id, None)
                self._state_dirty = True
                delivered += 1
                self._save_state()
                continue
            terminal = (not result.retryable) or attempt >= self.max_attempts
            delay = (
                max(0.0, float(result.retry_after_seconds))
                if result.retry_after_seconds is not None
                else self.retry_backoff_seconds * (2 ** (attempt - 1))
            )
            self._append_evidence(
                day,
                {
                    "event_id": event["event_id"],
                    "delivery_id": delivery_id,
                    "event_kind": event["kind"],
                    "delivery_state": "FAILED",
                    "delivery_attempts": attempt,
                    "attempt_number": attempt,
                    "http_status": result.http_status,
                    "delivered_timestamp": None,
                    "failure": result.error,
                    "failure_category": (
                        str(result.error).split(":", 1)[0]
                        if result.error
                        else "transport_failure"
                    ),
                    "retryable": result.retryable,
                    "retry_after_seconds": delay if not terminal else None,
                    "terminal": terminal,
                },
            )
            if terminal:
                self.state["pending"].pop(delivery_id, None)
            else:
                pending["next_attempt_at"] = now_epoch + delay
                pending["last_delivery_state"] = "FAILED"
            self._state_dirty = True
            self._save_state()
        return delivered

    def archive_pending(self, current_session: str) -> int:
        """Keep P5 events as evidence while a digest handles notifications."""
        day = _valid_session_date(current_session)
        archived = 0
        for delivery_id in list(self.state["pending"]):
            pending = self.state["pending"][delivery_id]
            event = pending.get("event") or {}
            if event.get("session_date") != day:
                continue
            self._append_evidence(day, {
                "event_id": event["event_id"],
                "delivery_id": delivery_id,
                "event_kind": event.get("kind") or "p5",
                "delivery_state": "ARCHIVED_FOR_DIGEST",
                "delivery_attempts": int(pending.get("attempts", 0)),
                "http_status": None,
                "delivered_timestamp": None,
                "terminal": True,
            })
            self.state["pending"].pop(delivery_id, None)
            self._state_dirty = True
            archived += 1
        self._save_if_dirty()
        return archived

    def process_session(self, session_date: str) -> Dict[str, int]:
        session_date = _valid_session_date(session_date)
        expired = self.expire_stale_pending(session_date)
        observed = self.process_feature_file(
            self.source_dir / f"live_features_{session_date}.csv", session_date
        )
        upgrades = 0
        if self.upgrade_source_dir is not None:
            upgrades = self.process_upgrade_file(
                self.upgrade_source_dir / f"{session_date}.jsonl"
            )
        delivered = self.deliver_pending(session_date)
        return {
            "observed": observed,
            "upgrades": upgrades,
            "delivered": delivered,
            "expired": expired,
        }

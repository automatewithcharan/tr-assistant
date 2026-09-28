"""Authenticated, bounded HTTP ingestion for TradingView Pine lifecycle events.

This module is analytics-only.  It deliberately has no broker integration.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urlsplit

from pkscreener.classes.CandidateLifecycleTracker import (
    CandidateLifecycleTracker,
    normalize_symbol,
)


UTC = timezone.utc
SUPPORTED_EVENTS = {
    "BUY_READY", "ENTRY", "BUY", "LOW_LIQUIDITY_SKIP", "STOP_UPDATE", "EXIT",
}
NUMERIC_FIELDS = {
    "signal_price", "assumed_entry", "entry_price", "qty", "quantity",
    "initial_stop", "stop", "one_r", "one_r_level", "target", "risk_per_share",
    "stop_price", "old_stop", "new_stop", "exit_price", "theoretical_pnl",
    "intended_qty", "liquidity_capacity",
}
NONNEGATIVE_FIELDS = {"qty", "quantity", "intended_qty", "liquidity_capacity"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PineWebhookConfig:
    secret: str
    host: str = "127.0.0.1"
    port: int = 8787
    max_body_bytes: int = 16_384
    max_past_age_seconds: int = 86_400
    max_future_skew_seconds: int = 300
    rate_limit_per_minute: int = 120
    auth_failure_rate_limit_per_minute: int = 20
    max_rate_limit_clients: int = 2_048
    allow_path_secret: bool = False
    trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_environment(cls) -> "PineWebhookConfig":
        secret = str(os.environ.get("SIGNALMONITOR_PINE_WEBHOOK_SECRET", ""))
        if len(secret) < 16:
            raise ValueError("SIGNALMONITOR_PINE_WEBHOOK_SECRET must contain at least 16 characters")
        networks = []
        for item in str(os.environ.get("SIGNALMONITOR_PINE_WEBHOOK_TRUSTED_PROXY_IPS", "")).split(","):
            if item.strip():
                networks.append(ipaddress.ip_network(item.strip(), strict=False))
        return cls(
            secret=secret,
            host=str(os.environ.get("SIGNALMONITOR_PINE_WEBHOOK_HOST", "127.0.0.1")),
            port=_env_int("SIGNALMONITOR_PINE_WEBHOOK_PORT", 8787),
            max_body_bytes=_env_int("SIGNALMONITOR_PINE_WEBHOOK_MAX_BODY_BYTES", 16_384),
            max_past_age_seconds=_env_int(
                "SIGNALMONITOR_PINE_WEBHOOK_MAX_PAST_AGE_SECONDS", 86_400
            ),
            max_future_skew_seconds=_env_int(
                "SIGNALMONITOR_PINE_WEBHOOK_MAX_FUTURE_SKEW_SECONDS", 300, minimum=0
            ),
            rate_limit_per_minute=_env_int(
                "SIGNALMONITOR_PINE_WEBHOOK_RATE_LIMIT_PER_MINUTE", 120
            ),
            auth_failure_rate_limit_per_minute=_env_int(
                "SIGNALMONITOR_PINE_WEBHOOK_AUTH_FAILURE_RATE_LIMIT_PER_MINUTE", 20
            ),
            max_rate_limit_clients=_env_int(
                "SIGNALMONITOR_PINE_WEBHOOK_MAX_RATE_LIMIT_CLIENTS", 2_048
            ),
            allow_path_secret=_env_bool("SIGNALMONITOR_PINE_WEBHOOK_ALLOW_PATH_SECRET"),
            trusted_proxy_networks=tuple(networks),
        )


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    body: dict[str, Any]


class SlidingWindowRateLimiter:
    """Thread-safe one-minute global/per-client limiter with bounded client state."""

    def __init__(self, limit: int, max_clients: int) -> None:
        self.limit = limit
        self.max_clients = max_clients
        self._global: deque[float] = deque()
        self._clients: dict[str, deque[float]] = defaultdict(deque)
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _trim(bucket: deque[float], now: float) -> None:
        cutoff = now - 60.0
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def allow(self, client: str, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._trim(self._global, current)
            bucket = self._clients[client]
            self._trim(bucket, current)
            if len(self._global) >= self.limit or len(bucket) >= self.limit:
                return False
            self._global.append(current)
            bucket.append(current)
            self._last_seen[client] = current
            if len(self._clients) > self.max_clients:
                for old_client, _ in sorted(self._last_seen.items(), key=lambda item: item[1])[
                    : len(self._clients) - self.max_clients
                ]:
                    self._clients.pop(old_client, None)
                    self._last_seen.pop(old_client, None)
            return True


class PineWebhookApplication:
    ROUTE = "/webhooks/tradingview/pine"

    def __init__(
        self,
        tracker: CandidateLifecycleTracker,
        config: PineWebhookConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if len(config.secret) < 16:
            raise ValueError("webhook secret must contain at least 16 characters")
        self.tracker = tracker
        self.config = config
        self.logger = logger or logging.getLogger("pkscreener.pine_webhook")
        self.requests = SlidingWindowRateLimiter(
            config.rate_limit_per_minute, config.max_rate_limit_clients
        )
        self.auth_failures = SlidingWindowRateLimiter(
            config.auth_failure_rate_limit_per_minute, config.max_rate_limit_clients
        )

    def _log(self, level: int, action: str, **fields: Any) -> None:
        record = {"component": "pine_webhook", "action": action, **fields}
        self.logger.log(level, json.dumps(record, sort_keys=True, separators=(",", ":")))

    def _client_ip(self, peer_ip: str, headers: Mapping[str, str]) -> str:
        try:
            peer = ipaddress.ip_address(peer_ip)
        except ValueError:
            return "unknown"
        trusted = any(peer in network for network in self.config.trusted_proxy_networks)
        if trusted:
            forwarded = str(headers.get("X-Forwarded-For", "")).split(",", 1)[0].strip()
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return str(peer)

    def _authorized(self, path: str, headers: Mapping[str, str]) -> bool:
        authorization = str(headers.get("Authorization", ""))
        scheme, separator, supplied = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and hmac.compare_digest(
            supplied, self.config.secret
        ):
            return True
        prefix = self.ROUTE + "/"
        if self.config.allow_path_secret and path.startswith(prefix):
            candidate = unquote(path[len(prefix):])
            return "/" not in candidate and hmac.compare_digest(candidate, self.config.secret)
        return False

    @staticmethod
    def _parse_timestamp(value: Any, received: datetime) -> datetime:
        if value in (None, ""):
            return received
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise ValueError("timestamp must be valid ISO-8601") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return parsed.astimezone(UTC)

    def _validate(self, value: Any, received: datetime) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        payload = dict(value)
        event = str(payload.get("event") or payload.get("event_type") or "").strip().upper()
        if event not in SUPPORTED_EVENTS:
            raise ValueError(f"unsupported event: {event or 'missing'}")
        payload["event"] = event
        symbol = str(payload.get("symbol") or payload.get("ticker") or "").strip()
        candidate_id = str(payload.get("candidate_id") or "").strip()
        if not symbol and not candidate_id:
            raise ValueError("symbol or candidate_id is required")
        if len(symbol) > 100 or len(candidate_id) > 200:
            raise ValueError("symbol or candidate_id is too long")
        event_id = str(payload.get("event_id") or "").strip()
        if len(event_id) > 200:
            raise ValueError("event_id is too long")
        exchange = str(payload.get("exchange") or "NSE").strip().upper()
        if exchange != "NSE":
            raise ValueError("only NSE lifecycle events are supported")
        payload["exchange"] = exchange
        timestamp = self._parse_timestamp(payload.get("timestamp", payload.get("time")), received)
        if timestamp < received - timedelta(seconds=self.config.max_past_age_seconds):
            raise ValueError("timestamp is older than the configured tolerance")
        if timestamp > received + timedelta(seconds=self.config.max_future_skew_seconds):
            raise ValueError("timestamp is later than the configured tolerance")
        payload["timestamp"] = timestamp.isoformat().replace("+00:00", "Z")
        for field in NUMERIC_FIELDS:
            if field not in payload or payload[field] is None:
                continue
            if isinstance(payload[field], bool):
                raise ValueError(f"{field} must be a finite number")
            try:
                number = float(payload[field])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{field} must be a finite number") from error
            if not math.isfinite(number):
                raise ValueError(f"{field} must be a finite number")
            if field in NONNEGATIVE_FIELDS and number < 0:
                raise ValueError(f"{field} must not be negative")
            payload[field] = number
        return payload

    def process(
        self,
        method: str,
        raw_path: str,
        headers: Mapping[str, str],
        body: bytes,
        peer_ip: str,
        *,
        received_at: Optional[datetime] = None,
        monotonic_now: Optional[float] = None,
    ) -> WebhookResponse:
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:12]
        received = (received_at or datetime.now(UTC)).astimezone(UTC)
        path = urlsplit(raw_path).path
        client_ip = self._client_ip(peer_ip, headers)
        if method == "GET" and path == "/health":
            return WebhookResponse(200, {"status": "ok"})
        valid_route = path == self.ROUTE or (
            self.config.allow_path_secret and path.startswith(self.ROUTE + "/")
        )
        if method != "POST" or not valid_route:
            return WebhookResponse(404, {"status": "error", "error": "not_found"})
        self._log(
            logging.DEBUG, "request_received", request_id=request_id,
            client_ip=client_ip, method=method,
        )
        if not self.requests.allow(client_ip, monotonic_now):
            self._log(logging.WARNING, "rate_limited", request_id=request_id, client_ip=client_ip)
            return WebhookResponse(429, {"status": "error", "error": "rate_limited"})
        if not self._authorized(path, headers):
            allowed = self.auth_failures.allow(client_ip, monotonic_now)
            self._log(logging.WARNING, "authentication_failed", request_id=request_id, client_ip=client_ip)
            return WebhookResponse(
                401 if allowed else 429,
                {"status": "error", "error": "unauthorized" if allowed else "rate_limited"},
            )
        self._log(
            logging.DEBUG, "authentication_succeeded",
            request_id=request_id, client_ip=client_ip,
        )
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._log(
                logging.INFO, "validation_failed", request_id=request_id,
                client_ip=client_ip, error="json_required",
            )
            return WebhookResponse(415, {"status": "error", "error": "json_required"})
        if len(body) > self.config.max_body_bytes:
            self._log(
                logging.WARNING, "validation_failed", request_id=request_id,
                client_ip=client_ip, error="payload_too_large",
            )
            return WebhookResponse(413, {"status": "error", "error": "payload_too_large"})
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._log(
                logging.INFO, "validation_failed", request_id=request_id,
                client_ip=client_ip, error="invalid_json",
            )
            return WebhookResponse(400, {"status": "error", "error": "invalid_json"})
        try:
            payload = self._validate(parsed, received)
        except ValueError as error:
            self._log(
                logging.INFO, "validation_failed", request_id=request_id,
                client_ip=client_ip, error=str(error),
            )
            return WebhookResponse(400, {"status": "error", "error": str(error)})
        self._log(
            logging.DEBUG, "validation_succeeded", request_id=request_id,
            client_ip=client_ip, event_type=payload["event"],
            normalized_symbol=normalize_symbol(payload.get("symbol") or payload.get("ticker")),
        )
        try:
            result = self.tracker.ingest_pine_event(
                payload, ingestion_source="tradingview_webhook", received_at=received
            )
        except Exception:
            self._log(logging.ERROR, "tracking_failed", request_id=request_id, client_ip=client_ip)
            return WebhookResponse(500, {"status": "error", "error": "internal_error"})
        response = {
            "status": result["status"],
            "event_id": result["event_id"],
            "matched": bool(result.get("candidate_id")),
            "candidate_id": result.get("candidate_id"),
            "match_method": result.get("match_method"),
            "match_confidence": result.get("match_confidence"),
            "processing_status": result.get("processing_status"),
        }
        response = {key: value for key, value in response.items() if value is not None}
        self._log(
            logging.INFO, "processed", request_id=request_id, client_ip=client_ip,
            event_type=payload["event"], event_id=result["event_id"],
            status=result["status"], matched=bool(result.get("candidate_id")),
            match_method=result.get("match_method"),
            processing_status=result.get("processing_status"),
            latency_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return WebhookResponse(200 if result["status"] == "duplicate" else 201, response)


class PineWebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], application: PineWebhookApplication):
        self.application = application
        super().__init__(address, PineWebhookRequestHandler)


class PineWebhookRequestHandler(BaseHTTPRequestHandler):
    server_version = "PineWebhook/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        # BaseHTTPRequestHandler logs the raw path, which may contain the
        # optional credential. Structured application logs are emitted above.
        return

    def _send(self, response: WebhookResponse) -> None:
        encoded = json.dumps(response.body, separators=(",", ":")).encode("utf-8")
        self.send_response(response.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._send(self.server.application.process(  # type: ignore[attr-defined]
            "GET", self.path, self.headers, b"", self.client_address[0]
        ))

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("Transfer-Encoding"):
            self._send(WebhookResponse(400, {"status": "error", "error": "content_length_required"}))
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(WebhookResponse(411, {"status": "error", "error": "content_length_required"}))
            return
        maximum = self.server.application.config.max_body_bytes  # type: ignore[attr-defined]
        if length < 0 or length > maximum:
            self._send(WebhookResponse(413, {"status": "error", "error": "payload_too_large"}))
            return
        body = self.rfile.read(length)
        self._send(self.server.application.process(  # type: ignore[attr-defined]
            "POST", self.path, self.headers, body, self.client_address[0]
        ))

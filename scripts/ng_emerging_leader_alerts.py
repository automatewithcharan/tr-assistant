#!/usr/bin/env python3
"""Run the isolated read-only Emerging Leader Discord alert sidecar."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.EmergingLeaderAlertPublisher import (  # noqa: E402
    EmergingLeaderAlertPublisher,
    IST,
    RadarUnavailable,
)
from pkscreener.classes.EmergingLeaderCausalMinuteProvider import (  # noqa: E402
    EmergingLeaderCausalMinuteProvider,
)
from pkscreener.classes.NGP5EarlyWatchPublisher import (  # noqa: E402
    DiscordWebhookTransport,
    validate_webhook_url,
)


def _truthy(name: str) -> bool:
    return str(os.environ.get(name, "0")).strip().lower() in {"1", "true", "yes", "on"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--radar-root",
        type=Path,
        default=Path("/home/charan_sai/tr-assistant/results/Data/emerging_leader_radar"),
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=Path("/home/charan_sai/tr-assistant/results/Data/emerging_leader_alerts/publisher.sqlite"),
    )
    parser.add_argument(
        "--ticks-path",
        type=Path,
        default=Path("/home/charan_sai/tr-assistant/results/Data/kiteticker_all_ticks_latest.json"),
    )
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not _truthy("NG_EMERGING_LEADER_DISCORD_ENABLED"):
        print(json.dumps({"feed": "emerging_leader_alerts", "status": "notification_disabled"}))
        return 0
    webhook = os.environ.get("NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("Emerging Leader Discord webhook is not configured", file=sys.stderr)
        return 2
    try:
        transport = DiscordWebhookTransport(
            validate_webhook_url(webhook), timeout_seconds=args.http_timeout_seconds
        )
    except ValueError as exc:
        print(f"Invalid Emerging Leader Discord webhook: {exc}", file=sys.stderr)
        return 2
    if args.test_notification:
        result = transport.post(
            "📡 Emerging Leader Alerts transport test\n\n"
            "Read-only alert sidecar is connected. No market candidate was generated.\n\n"
            "Research watch only — not a buy signal"
        )
        print(
            json.dumps(
                {
                    "feed": "emerging_leader_alerts",
                    "status": "test_delivered" if result.delivered else "test_failed",
                    "http_status": result.http_status,
                    "error": result.error,
                },
                sort_keys=True,
            )
        )
        return 0 if result.delivered else 1

    args.state_db.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_db.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Emerging Leader alert publisher is already running", file=sys.stderr)
            return 2
        minute_provider = EmergingLeaderCausalMinuteProvider(
            args.ticks_path, args.state_db
        )
        publisher = EmergingLeaderAlertPublisher(
            args.radar_root,
            args.state_db,
            transport=transport,
            shadow_plan_provider=minute_provider,
        )
        print(
            json.dumps(
                {
                    "feed": "emerging_leader_alerts",
                    "shadow_plan_provider": minute_provider.health(),
                    "status": "causal_minute_provider_started",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        last_error = None
        last_minute_provider_error = None
        while not stop.is_set():
            session_date = publisher.clock().astimezone(IST).date().isoformat()
            try:
                minute_provider.observe_latest()
                last_minute_provider_error = None
            except Exception as exc:
                minute_provider_error = f"{type(exc).__name__}:{exc}"
                if minute_provider_error != last_minute_provider_error:
                    print(
                        json.dumps(
                            {
                                "feed": "emerging_leader_alerts",
                                "status": "shadow_plan_provider_unavailable",
                                "error": minute_provider_error,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                last_minute_provider_error = minute_provider_error
            try:
                result = publisher.process_once(session_date)
                payload = {
                    "feed": "emerging_leader_alerts",
                    "session_date": session_date,
                    "radar_db_mode": "read_only",
                    **result.__dict__,
                }
                if result.initialized or result.ingested or result.queued or result.delivered or result.pending:
                    print(json.dumps(payload, sort_keys=True), flush=True)
                last_error = None
            except RadarUnavailable as exc:
                error = f"{type(exc).__name__}:{exc}"
                if error != last_error:
                    print(
                        json.dumps(
                            {"feed": "emerging_leader_alerts", "status": "radar_unavailable", "error": error},
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                last_error = error
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "feed": "emerging_leader_alerts",
                            "status": "failed_open",
                            "error": f"{type(exc).__name__}:{exc}",
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            if args.once:
                break
            stop.wait(max(0.2, float(args.poll_seconds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

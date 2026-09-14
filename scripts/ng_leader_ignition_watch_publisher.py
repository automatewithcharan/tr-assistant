#!/usr/bin/env python3
"""Run the isolated NG QUALITY WATCH Leader Ignition subscriber."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.NGLeaderIgnitionWatchPublisher import (  # noqa: E402
    DiscordWebhookTransport,
    NGLeaderIgnitionWatchPublisher,
    PublisherLock,
    validate_webhook_url,
)
from pkscreener.classes.NGPublisherHealth import (  # noqa: E402
    write_publisher_health_snapshot,
)


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _now_ist() -> dt.datetime:
    return dt.datetime.now(tz=IST)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish existing Leader Ignition PASS rows as informational NG QUALITY WATCH events."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=ROOT / "results" / "Data" / "leader_ignition",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=ROOT / "results" / "Data" / "ng_leader_ignition_watch",
    )
    parser.add_argument("--session-date", help="YYYY-MM-DD; defaults to current IST date")
    parser.add_argument("--once", action="store_true", help="Process available live rows once")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=5.0)
    parser.add_argument(
        "--health-output-dir",
        type=Path,
        help="Publisher health snapshot directory; defaults beneath --state-dir",
    )
    parser.add_argument(
        "--webhook-env",
        default="NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL",
        help="Environment variable containing the dedicated human-review webhook",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    session_date = args.session_date or _now_ist().date().isoformat()
    try:
        session_date = dt.date.fromisoformat(session_date).isoformat()
    except ValueError:
        print("Invalid --session-date; expected YYYY-MM-DD", file=sys.stderr)
        return 2

    webhook = os.environ.get(args.webhook_env, "").strip()
    if webhook:
        try:
            webhook = validate_webhook_url(webhook)
        except ValueError as exc:
            print(f"Invalid {args.webhook_env}: {exc}", file=sys.stderr)
            return 2
    transport = (
        DiscordWebhookTransport(webhook, timeout_seconds=args.http_timeout_seconds)
        if webhook
        else None
    )
    if transport is None:
        print(
            f"[{_now_ist().isoformat()}] {args.webhook_env} is unset; live observations will queue without delivery",
            file=sys.stderr,
        )

    args.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock = PublisherLock(args.state_dir / ".publisher.lock")
        lock.__enter__()
    except BlockingIOError:
        print(f"Leader Watch publisher already running for state directory {args.state_dir}", file=sys.stderr)
        return 2

    try:
        publisher = NGLeaderIgnitionWatchPublisher(
            args.source_dir,
            args.state_dir,
            transport=transport,
            max_attempts=args.max_attempts,
            retry_backoff_seconds=args.retry_backoff_seconds,
        )
        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        while not stopping:
            result = publisher.process_session(session_date)
            try:
                health_dir = args.health_output_dir or args.state_dir / "health"
                write_publisher_health_snapshot(
                    "ng_leader_ignition_watch",
                    [args.state_dir / "evidence"],
                    session_date,
                    health_dir / f"health_{session_date}.json",
                )
            except Exception as exc:
                print(
                    f"[{_now_ist().isoformat()}] publisher health snapshot failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            if any(result.values()):
                print(json.dumps({"session_date": session_date, **result}, sort_keys=True), flush=True)
            if args.once:
                break
            time.sleep(max(0.1, float(args.poll_seconds)))
        return 0
    except Exception as exc:
        print(f"Leader Watch publisher stopped safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())

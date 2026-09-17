#!/usr/bin/env python3
"""Publish the capped NG P5 review digest to the quality-watch Discord channel."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.NGP5EarlyWatchPublisher import DiscordWebhookTransport, validate_webhook_url  # noqa: E402
from pkscreener.classes.NGWatchDigest import NGWatchDigest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p5-state-root", type=Path,
                        default=ROOT / "results/Data/ng_p5_early_watch_shadow")
    parser.add_argument("--leader-state-root", type=Path,
                        default=ROOT / "results/Data/ng_leader_ignition_watch")
    parser.add_argument("--state-root", type=Path,
                        default=ROOT / "results/Data/ng_watch_digest")
    parser.add_argument("--webhook-env", default="NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    webhook = os.environ.get(args.webhook_env, "").strip()
    try:
        webhook = validate_webhook_url(webhook) if webhook else ""
    except ValueError as exc:
        print(f"Invalid webhook: {exc}", file=sys.stderr)
        return 2
    args.state_root.mkdir(parents=True, exist_ok=True)
    lock = (args.state_root / ".publisher.lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("NG digest already running", file=sys.stderr)
        return 2
    transport = DiscordWebhookTransport(webhook) if webhook else None
    publisher = NGWatchDigest(args.p5_state_root, args.state_root, transport,
                              leader_state_root=args.leader_state_root)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        result = publisher.process()
        if result["status"] not in {"already_processed", "outside_window"}:
            print(json.dumps(result, sort_keys=True), flush=True)
        if args.once:
            break
        time.sleep(max(1.0, args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

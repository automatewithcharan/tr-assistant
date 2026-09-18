#!/usr/bin/env python3
"""Publish NSE Top 20 and live NSE EQ Top 100 gainers to Discord."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.NGP5EarlyWatchPublisher import DiscordWebhookTransport, IST, validate_webhook_url  # noqa: E402
from pkscreener.classes.NSETopGainersWatch import NSETopGainersWatch  # noqa: E402
from pkscreener.classes.NGTop100GainersWatch import NGTop100GainersWatch  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path,
                        default=ROOT / "results/Data/ng_nse_top_gainers_watch")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--ticks-path", type=Path,
                        default=Path("/home/charan_sai/tr-assistant/results/Data/kiteticker_all_ticks_latest.csv"))
    parser.add_argument("--universe-path", type=Path,
                        default=Path("/home/charan_sai/tr-assistant/results/Indices/EQUITY_L.csv"))
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    webhook = os.environ.get("NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL is required", file=sys.stderr)
        return 2
    try:
        webhook = validate_webhook_url(webhook)
    except ValueError as exc:
        print(f"Invalid webhook: {exc}", file=sys.stderr)
        return 2
    args.state_root.mkdir(parents=True, exist_ok=True)
    with (args.state_root / ".publisher.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("NSE Top 20 publisher already running", file=sys.stderr)
            return 2
        publisher = NSETopGainersWatch(args.state_root, DiscordWebhookTransport(webhook),
                                      lambda: datetime.now(IST))
        broad = NGTop100GainersWatch(args.state_root / "kite_top100", args.ticks_path,
                                    args.universe_path, args.state_root, publisher.transport,
                                    lambda: datetime.now(IST), top_n=args.top_n)
        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        last_status = None
        last_broad_status = None
        while not stopping:
            result = publisher.process()
            if result["status"] not in {"outside_window", "up_to_date", "retry_wait"} or result["status"] != last_status:
                print(json.dumps(result, sort_keys=True), flush=True)
            last_status = result["status"]
            broad_result = broad.process()
            if broad_result["status"] not in {"outside_window", "up_to_date", "retry_wait"} or broad_result["status"] != last_broad_status:
                print(json.dumps({"feed": "kite_top100", **broad_result}, sort_keys=True), flush=True)
            last_broad_status = broad_result["status"]
            if args.once:
                break
            time.sleep(max(10.0, args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

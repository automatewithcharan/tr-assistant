#!/usr/bin/env python3
"""Publish NSE Top 20 and live NSE EQ Top 100 gainers to Discord."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.NGP5EarlyWatchPublisher import DiscordWebhookTransport, IST, validate_webhook_url  # noqa: E402
from pkscreener.classes.NSETopGainersWatch import NSETopGainersWatch  # noqa: E402
from pkscreener.classes.NGTop100GainersWatch import NGTop100GainersWatch  # noqa: E402
from pkscreener.classes.MarketObservationLedger import MarketObservationLedger  # noqa: E402
from pkscreener.classes.Nifty50WatchPublisher import Nifty50WatchPublisher  # noqa: E402
from pkscreener.classes.EmergingLeaderRecorder import EmergingLeaderRecorder  # noqa: E402

def _truthy(name: str) -> bool:
    return str(os.environ.get(name, "0")).strip().lower() in {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path,
                        default=ROOT / "results/Data/ng_nse_top_gainers_watch")
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--ticks-path", type=Path,
                        default=Path("/home/charan_sai/tr-assistant/results/Data/kiteticker_all_ticks_latest.csv"))
    parser.add_argument("--universe-path", type=Path,
                        default=Path("/home/charan_sai/tr-assistant/results/Indices/EQUITY_L.csv"))
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--min-shares", type=int, default=1000)
    parser.add_argument("--min-traded-value", type=float, default=1_000_000.0,
                        help="Minimum cumulative traded rupee value")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--emerging-leader-root", type=Path, default=None,
                        help="Shadow-only research output root (requires feature flag)")
    args = parser.parse_args(argv)
    webhook = os.environ.get("NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL", "").strip()
    transport = None
    if webhook:
        try:
            transport = DiscordWebhookTransport(validate_webhook_url(webhook))
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
        if args.min_shares < 1 or args.min_traded_value < 0:
            print("Liquidity minimums must be nonnegative", file=sys.stderr)
            return 2
        publisher = NSETopGainersWatch(args.state_root, transport,
                                      lambda: datetime.now(IST), min_shares=args.min_shares,
                                      min_traded_value=args.min_traded_value)
        broad = NGTop100GainersWatch(args.state_root / "kite_top100", args.ticks_path,
                                    args.universe_path, args.state_root, publisher.transport,
                                    lambda: datetime.now(IST), top_n=args.top_n,
                                    min_shares=args.min_shares,
                                    min_traded_value=args.min_traded_value,
                                    notifications_enabled=_truthy("NG_TOP100_WATCH_DISCORD_ENABLED"))
        ledger = MarketObservationLedger(args.state_root / "observation_ledger", args.universe_path,
            ROOT / "results/Indices/ind_nifty50list.csv",
            Path("/home/charan_sai/tr-assistant/results/Data/kiteticker_all_tokens_latest.csv"), lambda: datetime.now(IST))
        nifty_watch = Nifty50WatchPublisher(args.state_root / "nifty50_watch", transport,
                                             _truthy("NG_NIFTY50_WATCH_DISCORD_ENABLED"))
        recorder = None
        if _truthy("NG_EMERGING_LEADER_RESEARCH_ENABLED"):
            try:
                sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
                recorder = EmergingLeaderRecorder(
                    args.emerging_leader_root or args.state_root / "emerging_leader_radar",
                    args.universe_path,
                    Path("/home/charan_sai/tr-assistant/results/Data/kiteticker_all_tokens_latest.csv"),
                    sha,
                    lambda: datetime.now(IST),
                )
            except Exception as exc:  # Research must never block observation/watch.
                print(json.dumps({"feed": "emerging_leader", "status": "initialization_failed", "error": str(exc)}), flush=True)
        stop_event = threading.Event()

        def stop(_signum: int, _frame: object) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        last_status = None
        last_broad_status = None
        last_official_at = None
        while not stop_event.is_set():
            observation = ledger.process(args.ticks_path)
            if recorder is not None:
                try:
                    result = recorder.process(args.ticks_path)
                    print(json.dumps({"feed": "emerging_leader", **result}, sort_keys=True), flush=True)
                except Exception as exc:  # Fail open: no production behaviour depends on this recorder.
                    print(json.dumps({"feed": "emerging_leader", "status": "failed_open", "error": str(exc)}), flush=True)
            nifty_result = nifty_watch.process(observation.pop("event_records", []))
            print(json.dumps({"feed": "observation", **observation}, sort_keys=True), flush=True)
            if nifty_result["status"] not in {"notification_disabled", "up_to_date"}:
                print(json.dumps({"feed": "nifty50_watch", **nifty_result}, sort_keys=True), flush=True)
            now_monotonic = datetime.now(IST)
            if last_official_at is None or (now_monotonic - last_official_at).total_seconds() >= 60:
                result = publisher.process()
                if result["status"] not in {"outside_window", "up_to_date", "retry_wait"} or result["status"] != last_status:
                    print(json.dumps(result, sort_keys=True), flush=True)
                last_status = result["status"]
                last_official_at = now_monotonic
            broad_result = broad.process()
            if broad_result["status"] not in {"outside_window", "up_to_date", "retry_wait"} or broad_result["status"] != last_broad_status:
                print(json.dumps({"feed": "kite_top100", **broad_result}, sort_keys=True), flush=True)
            last_broad_status = broad_result["status"]
            if args.once:
                break
            stop_event.wait(max(10.0, args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the isolated NG P5 EARLY WATCH Discord publisher."""

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

from pkscreener.classes.NGP5EarlyWatchPublisher import (  # noqa: E402
    DiscordWebhookTransport,
    IST,
    NGP5EarlyWatchPublisher,
    _now_ist,
    validate_webhook_url,
)
from pkscreener.classes.NGPublisherHealth import (  # noqa: E402
    write_publisher_health_snapshot,
)
from pkscreener.classes.NGTradedValueShadowExperiment import (  # noqa: E402
    NGTradedValueShadowRecorder,
)
from pkscreener.classes.P5RejectionObservability import (  # noqa: E402
    P5RejectionObserver,
)


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {
        "1", "true", "yes", "y", "on",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tail full-universe causal rows and publish frozen NG P5 EARLY WATCH events."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=ROOT / "results" / "Data" / "live_feature_recorder",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=ROOT / "results" / "Data" / "ng_p5_early_watch",
    )
    parser.add_argument(
        "--upgrade-source-dir",
        type=Path,
        default=ROOT / "results" / "Data" / "alert_timing_observability",
    )
    upgrades = parser.add_mutually_exclusive_group()
    upgrades.add_argument(
        "--enable-upgrades",
        action="store_true",
        help="EXPERIMENTAL: scan existing candidate telemetry for upgrades",
    )
    upgrades.add_argument(
        "--no-upgrades",
        action="store_false",
        dest="enable_upgrades",
        help="Explicitly retain the safe default: P5 only, no upgrade journal scan",
    )
    parser.set_defaults(enable_upgrades=False)
    parser.add_argument("--session-date", help="YYYY-MM-DD; defaults to current IST date")
    parser.add_argument("--once", action="store_true", help="Process available complete rows once")
    parser.add_argument("--archive-only", action="store_true", help="Record events without raw Discord messages")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=5.0)
    parser.add_argument(
        "--experiment-4-dir",
        type=Path,
        default=ROOT / "results" / "Data" / "experiment_4_traded_value_shadow",
        help="Isolated output directory for shadow-only traded-value observations",
    )
    parser.add_argument(
        "--disable-experiment-4",
        action="store_true",
        help="Disable only the Experiment 4 observation sink; P5 behavior is unchanged",
    )
    parser.add_argument(
        "--health-output-dir",
        type=Path,
        help="Publisher health snapshot directory; defaults beneath --state-dir",
    )
    parser.add_argument(
        "--webhook-env",
        default="NG_EARLY_WATCH_DISCORD_WEBHOOK_URL",
        help="Environment variable containing the publisher-specific Discord webhook",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_dir / ".publisher.lock"
    lock_handle = lock_path.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"NG P5 publisher already running for state directory {args.state_dir}", file=sys.stderr)
        return 2

    webhook = "" if args.archive_only else os.environ.get(args.webhook_env, "").strip()
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
    if transport is None and not args.archive_only:
        print(
            f"[{_now_ist().isoformat()}] {args.webhook_env} is unset; observations will queue without delivery",
            file=sys.stderr,
        )
    shadow_recorder = (
        None
        if args.disable_experiment_4
        else NGTradedValueShadowRecorder(args.experiment_4_dir)
    )
    publisher = NGP5EarlyWatchPublisher(
        args.source_dir,
        args.state_dir,
        transport=transport,
        upgrade_source_dir=args.upgrade_source_dir if args.enable_upgrades else None,
        max_attempts=args.max_attempts,
        retry_backoff_seconds=args.retry_backoff_seconds,
        shadow_experiment_recorder=shadow_recorder,
        p5_rejection_observer=(
            P5RejectionObserver(args.state_dir.parent)
            if _env_truthy("NG_P5_ENABLE_REJECTION_OBSERVABILITY")
            else None
        ),
    )
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    poll_seconds = max(0.1, float(args.poll_seconds))
    while not stopping:
        session_date = args.session_date or _now_ist().astimezone(IST).date().isoformat()
        try:
            result = publisher.process_session(session_date)
            if args.archive_only:
                result["archived"] = publisher.archive_pending(session_date)
            try:
                health_dir = args.health_output_dir or args.state_dir / "health"
                write_publisher_health_snapshot(
                    "ng_p5_early_watch",
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
        except Exception as exc:
            # This process is external to the scanner; errors are contained here.
            print(
                f"[{_now_ist().isoformat()}] publisher cycle failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        if args.once:
            break
        time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

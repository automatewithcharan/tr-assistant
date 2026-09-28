#!/usr/bin/env python3
"""Run the authenticated TradingView/Pine lifecycle webhook receiver."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from pkscreener.classes.CandidateLifecycleTracker import CandidateLifecycleTracker
from pkscreener.classes.EnvironmentLoader import load_repository_environment
from pkscreener.classes.PineWebhookReceiver import (
    PineWebhookApplication,
    PineWebhookConfig,
    PineWebhookHTTPServer,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Receive authenticated Pine lifecycle events; never places trades."
    )
    parser.add_argument("--output-dir", required=True, help="PKScreener user-data directory")
    parser.add_argument("--host", help="override SIGNALMONITOR_PINE_WEBHOOK_HOST")
    parser.add_argument("--port", type=int, help="override SIGNALMONITOR_PINE_WEBHOOK_PORT")
    parser.add_argument("--match-max-age-minutes", type=float, default=30.0)
    return parser


def main(argv=None) -> int:
    load_repository_environment(REPOSITORY_ROOT)
    args = build_parser().parse_args(argv)
    config = PineWebhookConfig.from_environment()
    if args.host or args.port:
        config = PineWebhookConfig(
            **{**config.__dict__, "host": args.host or config.host, "port": args.port or config.port}
        )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    tracker = CandidateLifecycleTracker(
        args.output_dir, pine_match_max_age_minutes=args.match_max_age_minutes
    )
    application = PineWebhookApplication(tracker, config)
    server = PineWebhookHTTPServer((config.host, config.port), application)
    logging.getLogger("pkscreener.pine_webhook").info(
        '{"action":"started","component":"pine_webhook","host":"%s","port":%d}',
        config.host, config.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

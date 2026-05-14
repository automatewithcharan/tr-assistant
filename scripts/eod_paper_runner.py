#!/usr/bin/env python3
"""
EOD paper validation runner — CLI entry point.

Runs after NSE market close (4:15 PM IST).
Designed to be called from cron or systemd daily at 16:15 IST.

Usage:
    # Automatic run for today (skip if already ran or not a trading day)
    python scripts/eod_paper_runner.py

    # Force re-run today even if lock exists
    python scripts/eod_paper_runner.py --force

    # Run for a specific past date (historical back-fill)
    python scripts/eod_paper_runner.py --date 2026-05-12

    # Dry-run: show what would run but don't write anything
    python scripts/eod_paper_runner.py --dry-run

    # Send Discord notification if DISCORD_WEBHOOK_URL env var is set
    python scripts/eod_paper_runner.py --notify

SAFETY: No real orders ever placed.  Read-only Kite access only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))


def send_discord(webhook_url: str, result) -> None:
    """Post a summary embed to Discord if webhook is configured."""
    import urllib.request

    severity_color = {
        "SHUTDOWN": 0xFF0000,   # red
        "WARN":     0xFFAA00,   # amber
        "INFO":     0x00AA44,   # green
    }

    shutdown_alerts = [a for a in result.alerts if a.severity == "SHUTDOWN"]
    warn_alerts     = [a for a in result.alerts if a.severity == "WARN"]
    color = (
        severity_color["SHUTDOWN"] if shutdown_alerts else
        severity_color["WARN"]     if warn_alerts     else
        severity_color["INFO"]
    )

    win_rate = (
        f"{result.wins_today / result.trades_today * 100:.0f}%"
        if result.trades_today else "—"
    )

    fields = [
        {"name": "Date",         "value": result.date_str,              "inline": True},
        {"name": "Trades",       "value": str(result.trades_today),     "inline": True},
        {"name": "Win rate",     "value": win_rate,                     "inline": True},
        {"name": "Net P&L",      "value": f"{result.net_pnl_today:+.3f}%", "inline": True},
        {"name": "Avg slip",     "value": f"{result.avg_slippage_today:+.4f}%", "inline": True},
        {"name": "Stage",        "value": result.stage_verdict,         "inline": True},
        {"name": "Cumul days",   "value": str(result.cumulative_days),  "inline": True},
        {"name": "Cumul trades", "value": str(result.cumulative_trades),"inline": True},
        {"name": "Avg daily",    "value": f"{result.cumul_avg_daily:+.3f}%", "inline": True},
        {"name": "Max DD",       "value": f"{result.cumul_drawdown:.2f}%",   "inline": True},
    ]

    alert_lines = "\n".join(f"• [{a.severity}] {a.code}: {a.message}" for a in result.alerts)
    if alert_lines:
        fields.append({"name": "⚠ Alerts", "value": alert_lines[:1024], "inline": False})

    payload = json.dumps({
        "username": "paper-trader",
        "embeds": [{
            "title":       f"Paper Trading EOD — {result.date_str}",
            "description": "Daily simulation completed.",
            "color":       color,
            "fields":      fields,
            "footer":      {"text": "This is a paper trading simulation. No real orders."},
        }],
    }).encode("utf-8")

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"  [WARN] Discord notification failed: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EOD paper trading validation runner (read-only, no real orders)"
    )
    parser.add_argument(
        "--date", "-d", type=str, default=None, metavar="YYYY-MM-DD",
        help="Date to run for (default: today in IST)"
    )
    parser.add_argument(
        "--force", "-f", action="store_true",
        help="Force re-run even if today already ran"
    )
    parser.add_argument(
        "--notify", "-n", action="store_true",
        help="Send Discord notification (requires DISCORD_WEBHOOK_URL env var)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would run but do not write any files"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress per-day verbose output"
    )
    parser.add_argument(
        "--strategy", "-s", type=str, default="late_day_reversal",
        help="Strategy name (default: late_day_reversal)"
    )
    parser.add_argument(
        "--lookback", "-l", type=int, default=60,
        help="Days of history for cumulative report (default: 60)"
    )
    args = parser.parse_args()

    # IST date if no date specified
    date_str = args.date or datetime.utcnow().replace(
        tzinfo=None   # add UTC→IST offset
    ).strftime("%Y-%m-%d")
    # Simple IST: UTC + 5:30
    if args.date is None:
        from datetime import timezone, timedelta as td
        ist_now  = datetime.now(tz=timezone(td(hours=5, minutes=30)))
        date_str = ist_now.strftime("%Y-%m-%d")

    if args.dry_run:
        print(f"  [DRY RUN] Would run EOD paper validation for {date_str}")
        print(f"  Strategy:  {args.strategy}")
        print(f"  Lookback:  {args.lookback} days")
        print(f"  Force:     {args.force}")
        return

    from pkscreener.classes.EODPaperRunner import EODPaperRunner

    runner = EODPaperRunner(
        strategy=args.strategy,
        lookback_days=args.lookback,
    )
    result = runner.run(
        date_str=date_str,
        force=args.force,
        verbose=not args.quiet,
    )

    # Handle outcomes
    if result.error:
        print(f"\n  [ERROR] EOD runner failed: {result.error}", file=sys.stderr)
        sys.exit(1)

    if result.skipped_reason:
        print(f"  [SKIP] {result.skipped_reason}")
        sys.exit(0)

    # Print report paths
    if not args.quiet and result.report_paths:
        print("\n  Reports saved:")
        for kind, path in result.report_paths.items():
            print(f"    {kind:12s} → {path}")

    # Discord notification
    if args.notify:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook:
            print("  Sending Discord notification …", end=" ", flush=True)
            send_discord(webhook, result)
            print("done")
        else:
            print("  [WARN] --notify passed but DISCORD_WEBHOOK_URL not set")

    # Exit code: non-zero if any SHUTDOWN alert
    shutdown_alerts = [a for a in result.alerts if a.severity == "SHUTDOWN"]
    if shutdown_alerts:
        print(f"\n  [SHUTDOWN] {len(shutdown_alerts)} shutdown condition(s) triggered.")
        for a in shutdown_alerts:
            print(f"    {a.code}: {a.message}")
        sys.exit(2)


if __name__ == "__main__":
    main()

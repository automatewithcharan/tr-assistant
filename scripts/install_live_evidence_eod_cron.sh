#!/usr/bin/env bash
# Install the independent 3:35 PM IST weekday live-evidence EOD job.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMAND="5 10 * * 1-5 $REPO/scripts/live_evidence_eod.sh >> /tmp/live_evidence_eod.log 2>&1"
MARKER="# Live microstructure evidence backfill/evaluation at 3:35 PM IST (10:05 UTC) Mon-Fri"
CURRENT="$(crontab -l 2>/dev/null || true)"

if printf '%s\n' "$CURRENT" | grep -Fq "$REPO/scripts/live_evidence_eod.sh"; then
    echo "Live evidence EOD cron already installed."
    exit 0
fi

{
    printf '%s\n' "$CURRENT"
    printf '\n%s\n%s\n' "$MARKER" "$COMMAND"
} | crontab -

echo "Installed live evidence EOD cron: $COMMAND"

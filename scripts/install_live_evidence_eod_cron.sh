#!/usr/bin/env bash
# Install the independent 4:25 PM IST weekday live-evidence EOD job.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMAND="55 10 * * 1-5 $REPO/scripts/live_evidence_eod.sh >> /tmp/live_evidence_eod.log 2>&1"
MARKER="# Live microstructure evidence backfill/evaluation at 4:25 PM IST (10:55 UTC) Mon-Fri"
CURRENT="$(crontab -l 2>/dev/null || true)"
FILTERED="$(
    printf '%s\n' "$CURRENT" |
        grep -Fv "$REPO/scripts/live_evidence_eod.sh" |
        grep -Fv "Live microstructure evidence backfill/evaluation" || true
)"

{
    printf '%s\n' "$FILTERED"
    printf '\n%s\n%s\n' "$MARKER" "$COMMAND"
} | crontab -

echo "Installed live evidence EOD cron: $COMMAND"

#!/usr/bin/env bash
# Daily NSE event-feed refresh.
# Pulls event calendar + corporate announcements and tags today's alert list.
# Suggested slot: before blind_buy_alert_paper_eod.sh so the trader sees fresh tags.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
PY="$REPO/scripts/runtime_python.sh"
LOG="$REPO/results/Data/nse_event_feed/cron.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }

echo "[$(ts)] start" >> "$LOG"
"$PY" scripts/nse_event_feed.py >> "$LOG" 2>&1 || {
    echo "[$(ts)] FAILED" >> "$LOG"
    exit 1
}
echo "[$(ts)] done" >> "$LOG"

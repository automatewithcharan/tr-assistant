#!/usr/bin/env bash
# Daily blind-buy alert paper trader — runs at 10:30 AM IST after prewarm.
# Fills pending entries at today's open, checks exits, sends Discord summary.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[ -f .env.dev ] && set -a && . .env.dev && set +a
PY="$REPO/scripts/runtime_python.sh"
LOG="$REPO/results/Data/blind_buy_alert_paper/eod_cron.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }

echo "[$(ts)] start" >> "$LOG"
OUT=$("$PY" scripts/blind_buy_paper_trader.py 2>&1)
echo "$OUT" >> "$LOG"

# Discord summary (built by a standalone script — picks + holdings + stats)
SUMMARY=$("$PY" scripts/blind_buy_discord_summary.py 2>&1) || true

if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    PAYLOAD=$(echo "$SUMMARY" | python3 -c 'import sys, json; print(json.dumps({"content": sys.stdin.read()}))')
    curl -s -X POST "$DISCORD_WEBHOOK_URL" \
        -H 'Content-Type: application/json' \
        --data-raw "$PAYLOAD" > /dev/null 2>&1 || true
fi

echo "[$(ts)] done — $SUMMARY" >> "$LOG"

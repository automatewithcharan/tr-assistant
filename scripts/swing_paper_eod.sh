#!/usr/bin/env bash
# Swing paper-trading EOD runner. Runs Mon-Fri at 4:30 PM IST (11:00 UTC) via cron.
#
# Behaviour:
#   - Sources .env.dev so DISCORD_WEBHOOK_URL is available.
#   - Picks the LATEST trading date present in the daily-refreshed Kite cache
#     (results/Data/kite_history_cache/day/, populated by trade-assistant-prewarm
#     timer @ 8:30 AM IST). The 1-day lag is intentional: prewarm fetches T-1's
#     bar, we paper-trade on it the next evening.
#   - Idempotent: skips if that date is already in the paper journal's daily_log.
#   - Holidays handled naturally — no new data → already processed → skip.
#   - Sends Discord on success and on failure.
#
# SAFETY: No real orders. Read-only Kite access via existing cache.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Load env
if [ -f "$REPO/.env.dev" ]; then
    set -a
    . "$REPO/.env.dev"
    set +a
fi

if [ -x "$REPO/.venv/bin/python3" ]; then
    PYTHON="$REPO/.venv/bin/python3"
else
    PYTHON="python3"
fi

LOG="$REPO/results/Data/swing_paper_trades/swing_eod_cron.log"
mkdir -p "$(dirname "$LOG")"

ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }

send_discord() {
    local content="$1"
    [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return
    curl -s -X POST "$DISCORD_WEBHOOK_URL" \
        -H 'Content-Type: application/json' \
        --data-raw "{\"content\": \"$content\"}" > /dev/null 2>&1 || true
}

echo "[$(ts)] swing_paper_eod: start" >> "$LOG"

# 1. Find latest cached trading date
LATEST_DATE=$("$PYTHON" -c "
import sys; sys.path.insert(0, '.')
from scripts.swing_paper_trader import load_features
_, dates = load_features()
print(dates[-1].strftime('%Y-%m-%d'))
" 2>>"$LOG") || {
    echo "[$(ts)] ERROR: could not load features" >> "$LOG"
    send_discord "❌ **Swing EOD failed** — feature load error. See $LOG"
    exit 1
}

echo "[$(ts)]   Latest cached trading date: $LATEST_DATE" >> "$LOG"

# 2. Idempotency check
ALREADY=$("$PYTHON" -c "
import json
from pathlib import Path
p = Path('results/Data/swing_paper_trades/paper_journal.json')
if not p.exists():
    print('no'); exit()
j = json.loads(p.read_text())
seen = {e.get('date') for e in j.get('daily_log', [])}
print('yes' if '$LATEST_DATE' in seen else 'no')
")

if [ "$ALREADY" = "yes" ]; then
    echo "[$(ts)]   Already processed $LATEST_DATE — skipping" >> "$LOG"
    exit 0
fi

# 3. Run EOD
echo "[$(ts)]   Running --eod --date $LATEST_DATE" >> "$LOG"
if "$PYTHON" scripts/swing_paper_trader.py --eod --date "$LATEST_DATE" >> "$LOG" 2>&1; then
    # Pull a quick summary line from the journal for the Discord ping
    SUMMARY=$("$PYTHON" -c "
import json
from pathlib import Path
j = json.loads(Path('results/Data/swing_paper_trades/paper_journal.json').read_text())
today = [e for e in j.get('daily_log', []) if e.get('date') == '$LATEST_DATE']
if today:
    e = today[-1]
    print(f\"signals={e.get('signals_fired',0)} entries={e.get('new_entries',0)} active={e.get('active_after',0)} closed_today={e.get('closed_today',0)}\")
else:
    print('no daily_log entry')
")
    send_discord "✅ **Swing EOD** $LATEST_DATE | $SUMMARY"
    echo "[$(ts)] done — $SUMMARY" >> "$LOG"
else
    echo "[$(ts)] ERROR: --eod failed" >> "$LOG"
    send_discord "❌ **Swing EOD failed** for $LATEST_DATE. See $LOG"
    exit 1
fi

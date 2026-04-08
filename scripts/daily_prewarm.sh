# Daily prewarm — refreshes Kite OHLCV history cache for all stocks.
# Runs at 2:30 AM UTC (8:00 AM IST) Mon-Fri, BEFORE the watchdog starts the live session.
# Uses --kiteprewarm (standalone, exits when done) NOT --assistantstart.
# --kiteprewarmforce forces re-download even if cache exists, ensuring no stale data.

set -e

REPO=/home/charan_sai/tr-assistant
PYTHON=$REPO/venv/bin/python3
LOG=/tmp/daily_prewarm.log
WEBHOOK=$(grep DISCORD_WEBHOOK_URL $REPO/.env.dev 2>/dev/null | cut -d= -f2- || echo '')

send_discord() {
    [ -z "$WEBHOOK" ] && return
    curl -s -X POST "$WEBHOOK"          -H 'Content-Type: application/json'          -d "{\"content\": \"$1\"}" > /dev/null 2>&1 || true
}

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: starting cache refresh for allstocks" >> $LOG
send_discord "🔄 **Daily prewarm started** — refreshing cache for ~2000 stocks (8 AM IST)"

# Check Kite session is valid before starting; re-login if token is expired
SESSION_FILE=$REPO/results/Data/kite_connect_session.json
if [ -f "$SESSION_FILE" ]; then
    MODIFIED_EPOCH=$(stat -c %Y "$SESSION_FILE")
    NOW_EPOCH=$(date +%s)
    AGE_HOURS=$(( (NOW_EPOCH - MODIFIED_EPOCH) / 3600 ))
    if [ $AGE_HOURS -gt 20 ]; then
        echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: Kite session is ${AGE_HOURS}h old — auto-login needed" >> $LOG
        send_discord "⚠️ **Kite session expired** (${AGE_HOURS}h old) — prewarm may fail. Re-login required."
    fi
fi

# Run standalone prewarm (exits when done — does NOT start a trading session)
TERM=xterm $PYTHON -m pkscreener.pkscreenercli     --kiteprewarm allstocks     --kiteprewarmforce     --kiteprewarmworkers 4     >> $LOG 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    REFRESHED=$(find $REPO/results/Data/kite_history_cache/day -name '*.csv' -newer $LOG 2>/dev/null | wc -l)
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: done (exit=0, ~${REFRESHED} files updated)" >> $LOG
    send_discord "✅ **Prewarm complete** — cache refreshed for ~${REFRESHED} stocks. Session will start at 8:30 AM IST."
else
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: FAILED (exit=$EXIT_CODE)" >> $LOG
    send_discord "🚨 **Prewarm FAILED** (exit=${EXIT_CODE}) — scanner may use stale data today. Check /tmp/daily_prewarm.log"
fi

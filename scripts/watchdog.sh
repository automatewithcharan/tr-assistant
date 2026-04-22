# Trade assistant watchdog — runs every 5 min during market hours (03:00–10:35 UTC Mon-Fri).
# Dual-check: (1) --assistantstatus DB says running, (2) supervisor PID is actually alive.
# If either fails, kills stale state and restarts cleanly.

REPO=/home/charan_sai/tr-assistant
if [ -x "$REPO/.venv/bin/python3" ]; then
    PYTHON="$REPO/.venv/bin/python3"
elif [ -x "$REPO/venv/bin/python3" ]; then
    PYTHON="$REPO/venv/bin/python3"
else
    PYTHON="python3"
fi
LOG=/tmp/trade_watchdog.log
WEBHOOK=$(grep DISCORD_WEBHOOK_URL $REPO/.env.dev 2>/dev/null | cut -d= -f2- || echo '')
CLEANUP_STAMP=/tmp/trade_data_cleanup_last_date

send_discord() {
    [ -z "$WEBHOOK" ] && return
    curl -s -X POST "$WEBHOOK"          -H 'Content-Type: application/json'          -d "{\"content\": \"$1\"}" > /dev/null 2>&1 || true
}

run_daily_data_cleanup() {
    TODAY=$(date +%Y-%m-%d)
    LAST_DONE=""
    [ -f "$CLEANUP_STAMP" ] && LAST_DONE=$(cat "$CLEANUP_STAMP" 2>/dev/null || echo "")

    # Run once per day only. Uses safe defaults:
    # - keep *_latest files
    # - keep portfolio history
    # - keep cache directories
    if [ "$LAST_DONE" = "$TODAY" ]; then
        return
    fi

    CLEANUP_OUT=$(cd "$REPO" && "$PYTHON" scripts/cleanup_results_data.py --apply --keep-days 14 --archive-instead-of-delete 2>&1)
    CLEANUP_RC=$?
    if [ "$CLEANUP_RC" -eq 0 ]; then
        DELETED_COUNT=$(echo "$CLEANUP_OUT" | grep 'Delete count:' | awk '{print $3}' | head -1)
        ARCHIVED_COUNT=$(echo "$CLEANUP_OUT" | grep 'Archived count:' | awk '{print $3}' | head -1)
        [ -z "$DELETED_COUNT" ] && DELETED_COUNT=0
        [ -z "$ARCHIVED_COUNT" ] && ARCHIVED_COUNT=0
        echo "$TODAY" > "$CLEANUP_STAMP"
        echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: data cleanup done (deleted=$DELETED_COUNT archived=$ARCHIVED_COUNT)" >> "$LOG"
        send_discord "🧹 **Data cleanup completed** at $(TZ='Asia/Kolkata' date +'%H:%M') IST — archived $ARCHIVED_COUNT and deleted $DELETED_COUNT old files"
    else
        echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: data cleanup failed" >> "$LOG"
        echo "$CLEANUP_OUT" >> "$LOG"
        send_discord "⚠️ **Data cleanup failed** at $(TZ='Asia/Kolkata' date +'%H:%M') IST — check /tmp/trade_watchdog.log"
    fi
}

cd $REPO

# --- Check 1: DB status says running ---
STATUS=$($PYTHON -m pkscreener.pkscreenercli --assistantstatus 2>/dev/null | head -20)
DB_RUNNING=$(echo "$STATUS" | grep -c 'Running now: yes' || true)

# --- Check 2: Supervisor PID is actually alive ---
SUPERVISOR_PID=$(echo "$STATUS" | grep 'Supervisor PID' | grep -o '[0-9]*' | head -1)
PID_ALIVE=0
if [ -n "$SUPERVISOR_PID" ] && kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    PID_ALIVE=1
fi

if [ "$DB_RUNNING" -gt 0 ] && [ "$PID_ALIVE" -eq 1 ]; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: healthy (pid=$SUPERVISOR_PID)" >> $LOG
    run_daily_data_cleanup
    exit 0
fi

# Dead — log why
if [ "$DB_RUNNING" -eq 0 ]; then
    REASON="DB shows not running"
else
    REASON="PID $SUPERVISOR_PID not alive (zombie in DB)"
fi
echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: supervisor down ($REASON), restarting..." >> $LOG

# Clean up stale state
$PYTHON -m pkscreener.pkscreenercli --assistantstop all >> $LOG 2>&1 || true
sleep 3

# Restart with combined NSE+BSE universe
TERM=xterm $PYTHON -m pkscreener.pkscreenercli     --assistantstart all     --assistantautotune     --assistantbudget 100000     --assistantinterval 60     --assistantalerts discord     --assistantautopilot >> $LOG 2>&1

if [ $? -eq 0 ]; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: restart succeeded" >> $LOG
    send_discord "⚠️ **Trade Assistant restarted** at $(TZ='Asia/Kolkata' date +'%H:%M') IST — was down ($REASON)"
else
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: restart FAILED" >> $LOG
    send_discord "🚨 **Trade Assistant restart FAILED** at $(TZ='Asia/Kolkata' date +'%H:%M') IST — manual action needed"
fi

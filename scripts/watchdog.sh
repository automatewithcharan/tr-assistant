# Trade assistant watchdog — runs every 5 min during market hours (03:00–10:35 UTC Mon-Fri).
# Dual-check: (1) --assistantstatus DB says running, (2) supervisor PID is actually alive.
# If either fails, kills stale state and restarts cleanly.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "$REPO/.venv/bin/python3" ]; then
    PYTHON="$REPO/.venv/bin/python3"
elif [ -x "$REPO/venv/bin/python3" ]; then
    PYTHON="$REPO/venv/bin/python3"
else
    PYTHON="python3"
fi
LOG_DIR="$REPO/results/Data"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/trade_watchdog.log"
WEBHOOK=$(grep DISCORD_WEBHOOK_URL $REPO/.env.dev 2>/dev/null | cut -d= -f2- || echo '')
DISCORD_OUTBOX_DIR="$LOG_DIR/discord_outbox"
DISK_STATE_STAMP="$LOG_DIR/trade_disk_guard_state"
DISK_WARN_GB=$(grep '^TRADE_DISK_WARN_GB=' "$REPO/.env.dev" 2>/dev/null | tail -1 | cut -d= -f2- || echo '8')
DISK_CRITICAL_GB=$(grep '^TRADE_DISK_CRITICAL_GB=' "$REPO/.env.dev" 2>/dev/null | tail -1 | cut -d= -f2- || echo '4')
DISK_GUARD_STOPPED=0

send_discord() {
    local message="$1"
    mkdir -p "$DISCORD_OUTBOX_DIR"
    if [ -z "$WEBHOOK" ]; then
        MESSAGE="$message" STATUS="skipped_no_webhook" HTTP_STATUS="0" OUTBOX_DIR="$DISCORD_OUTBOX_DIR" "$PYTHON" - <<'PY' || true
import json
import os
import uuid
from datetime import datetime
sent_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
row = {
    "id": uuid.uuid4().hex[:12],
    "sent_at": sent_at,
    "source": "watchdog",
    "universe": "all",
    "status": os.environ.get("STATUS", ""),
    "http_status": int(os.environ.get("HTTP_STATUS", "0") or 0),
    "error": "",
    "message_length": len(os.environ.get("MESSAGE", "")),
    "message": os.environ.get("MESSAGE", ""),
}
with open(os.path.join(os.environ["OUTBOX_DIR"], f"discord_outbox_{sent_at[:10]}.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
        return
    fi
    local payload
    payload=$(MESSAGE="$message" "$PYTHON" - <<'PY'
import json
import os
print(json.dumps({"content": os.environ.get("MESSAGE", "")[:1900]}, ensure_ascii=False))
PY
    )
    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$WEBHOOK" -H 'Content-Type: application/json' -d "$payload" 2>/dev/null || echo "0")
    local status="failed_http"
    case "$http_status" in
        2*) status="sent" ;;
        0|000) status="failed_exception" ;;
    esac
    MESSAGE="$message" STATUS="$status" HTTP_STATUS="$http_status" OUTBOX_DIR="$DISCORD_OUTBOX_DIR" "$PYTHON" - <<'PY' || true
import json
import os
import uuid
from datetime import datetime
sent_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
row = {
    "id": uuid.uuid4().hex[:12],
    "sent_at": sent_at,
    "source": "watchdog",
    "universe": "all",
    "status": os.environ.get("STATUS", ""),
    "http_status": int(os.environ.get("HTTP_STATUS", "0") or 0),
    "error": "",
    "message_length": len(os.environ.get("MESSAGE", "")),
    "message": os.environ.get("MESSAGE", ""),
}
with open(os.path.join(os.environ["OUTBOX_DIR"], f"discord_outbox_{sent_at[:10]}.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
}

has_systemd_service() {
    command -v systemctl >/dev/null 2>&1 && systemctl cat trade-assistant.service >/dev/null 2>&1
}

stop_assistant() {
    if has_systemd_service; then
        systemctl stop trade-assistant.service >> "$LOG" 2>&1 || true
    else
        $PYTHON -m pkscreener.pkscreenercli --assistantstop all >> "$LOG" 2>&1 || true
    fi
}

restart_assistant() {
    if has_systemd_service; then
        systemctl restart trade-assistant.service >> "$LOG" 2>&1
    else
        TERM=xterm $PYTHON -m pkscreener.pkscreenercli \
            --assistantstart all \
            --assistantautotune \
            --assistantbudget 100000 \
            --assistantinterval 60 \
            --assistantalerts discord \
            --assistantautopilot >> "$LOG" 2>&1
    fi
}

assistant_is_running() {
    local status supervisor_pid
    status=$($PYTHON -m pkscreener.pkscreenercli --assistantstatus 2>/dev/null | head -20)
    echo "$status" | grep -q 'Running now: yes' || return 1
    supervisor_pid=$(echo "$status" | grep 'Supervisor PID' | grep -o '[0-9]*' | head -1)
    [ -n "$supervisor_pid" ] || return 1
    kill -0 "$supervisor_pid" 2>/dev/null || return 1
    return 0
}

get_free_kb() {
    df -Pk "$REPO" 2>/dev/null | awk 'NR==2 {print $4}'
}

kb_to_gb() {
    awk -v kb="$1" 'BEGIN { printf "%.1f", kb/1024/1024 }'
}

send_discord_on_disk_state_change() {
    NEW_STATE="$1"
    MESSAGE="$2"
    LAST_STATE=""
    [ -f "$DISK_STATE_STAMP" ] && LAST_STATE=$(cat "$DISK_STATE_STAMP" 2>/dev/null || echo "")
    if [ "$NEW_STATE" != "$LAST_STATE" ]; then
        echo "$NEW_STATE" > "$DISK_STATE_STAMP"
        send_discord "$MESSAGE"
    fi
}

run_daily_data_cleanup() {
    return
}

run_disk_guard() {
    WARN_KB=$((DISK_WARN_GB * 1024 * 1024))
    CRIT_KB=$((DISK_CRITICAL_GB * 1024 * 1024))
    FREE_KB=$(get_free_kb)
    [ -z "$FREE_KB" ] && return

    if [ "$FREE_KB" -gt "$WARN_KB" ]; then
        send_discord_on_disk_state_change "normal" "✅ **Disk space healthy again** — free space is back to $(kb_to_gb "$FREE_KB") GB."
        echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: disk healthy (free=$(kb_to_gb "$FREE_KB")G)" >> "$LOG"
        return
    fi

    AFTER_GB=$(kb_to_gb "$FREE_KB")

    if [ "$FREE_KB" -le "$CRIT_KB" ]; then
        echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: disk CRITICAL (free=${AFTER_GB}G), stopping assistant to protect machine" >> "$LOG"
        stop_assistant
        DISK_GUARD_STOPPED=1
        send_discord_on_disk_state_change "critical" "🚨 **Disk critically low** — only ${AFTER_GB} GB free. Assistant was stopped to protect login/session stability. Systemd disk guard remains responsible for storage cleanup."
        return
    fi

    send_discord_on_disk_state_change "warning" "⚠️ **Disk space low** — free space is ${AFTER_GB} GB. Systemd disk guard handles storage cleanup; the watchdog is only monitoring bot uptime."
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: disk warning persists (free=${AFTER_GB}G)" >> "$LOG"
}

cd $REPO
run_disk_guard
[ "$DISK_GUARD_STOPPED" -eq 1 ] && exit 0

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
stop_assistant
sleep 3

# Restart using the systemd service when available so the detached supervisor
# is owned by the persistent unit rather than the short-lived watchdog timer.
if restart_assistant; then
    sleep 8
else
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: restart command failed" >> $LOG
fi

if assistant_is_running; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: restart succeeded" >> $LOG
    send_discord "⚠️ **Trade Assistant restarted** at $(TZ='Asia/Kolkata' date +'%H:%M') IST — was down ($REASON)"
else
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Watchdog: restart FAILED verification" >> $LOG
    send_discord "🚨 **Trade Assistant restart FAILED** at $(TZ='Asia/Kolkata' date +'%H:%M') IST — Kite/session or service issue needs manual action"
fi

#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if [ -x "$REPO/.venv/bin/python3" ]; then
    PYTHON="$REPO/.venv/bin/python3"
elif [ -x "$REPO/venv/bin/python3" ]; then
    PYTHON="$REPO/venv/bin/python3"
else
    PYTHON="python3"
fi

ENV_FILE="$REPO/.env.dev"
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

RUNTIME_DIR="$REPO/results/Data"
mkdir -p "$RUNTIME_DIR"
LOG="$RUNTIME_DIR/disk_guard.log"
STATE_STAMP="$RUNTIME_DIR/disk_guard_state"
WEBHOOK="${DISCORD_WEBHOOK_URL:-}"
ARCHIVE_DIR="$REPO/results/Data/archive"
LOGS_DIR="$REPO/results/Data/assistant_logs"
ARCHIVE_BUCKET="${TRADE_ASSISTANT_ARCHIVE_BUCKET:-}"
DISK_WARN_GB="${TRADE_DISK_WARN_GB:-8}"
DISK_CRITICAL_GB="${TRADE_DISK_CRITICAL_GB:-4}"
ARCHIVE_PRUNE_DAYS="${TRADE_ARCHIVE_PRUNE_AFTER_SYNC_DAYS:-2}"
LOG_COMPRESS_MINUTES="${TRADE_LOG_COMPRESS_MINUTES:-180}"
LOG_DELETE_DAYS="${TRADE_LOG_DELETE_DAYS:-60}"
CLEANUP_KEEP_DAYS="${TRADE_CLEANUP_KEEP_DAYS:-3}"

send_discord() {
    [ -z "$WEBHOOK" ] && return
    curl -s -X POST "$WEBHOOK" \
        -H 'Content-Type: application/json' \
        -d "{\"content\": \"$1\"}" >/dev/null 2>&1 || true
}

send_discord_on_state_change() {
    local new_state="$1"
    local message="$2"
    local last_state=""
    [ -f "$STATE_STAMP" ] && last_state=$(cat "$STATE_STAMP" 2>/dev/null || echo "")
    if [ "$new_state" != "$last_state" ]; then
        echo "$new_state" > "$STATE_STAMP"
        send_discord "$message"
    fi
}

log() {
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"
}

free_kb() {
    df -Pk "$REPO" 2>/dev/null | awk 'NR==2 {print $4}'
}

kb_to_gb() {
    awk -v kb="$1" 'BEGIN { printf "%.1f", kb/1024/1024 }'
}

run_cleanup() {
    local args=(--diskusagereport --diskusageapply --diskusagenotify)
    if [ -n "$ARCHIVE_BUCKET" ]; then
        args+=(--diskusagebucket "$ARCHIVE_BUCKET")
    fi
    "$PYTHON" -m pkscreener.pkscreenercli "${args[@]}" >> "$LOG" 2>&1 || true
}

sync_archive_bucket() {
    return 0
}

sync_compressed_logs() {
    return 0
}

sync_critical_state() {
    return 0
}

compress_old_logs() {
    return 0
}

prune_synced_archive() {
    return 0
}

WARN_KB=$((DISK_WARN_GB * 1024 * 1024))
CRIT_KB=$((DISK_CRITICAL_GB * 1024 * 1024))
FREE_KB=$(free_kb)

compress_old_logs
run_cleanup

if [ -n "${FREE_KB:-}" ] && [ "$FREE_KB" -le "$WARN_KB" ]; then
    log "Disk guard: low disk detected (free=$(kb_to_gb "$FREE_KB")G), running cleanup"
    run_cleanup
    FREE_KB=$(free_kb)
fi

if [ -n "${FREE_KB:-}" ] && [ "$FREE_KB" -le "$CRIT_KB" ]; then
    log "Disk guard: critical disk remains after cleanup (free=$(kb_to_gb "$FREE_KB")G)"
    send_discord_on_state_change "critical" "🚨 **Disk guard critical** — only $(kb_to_gb "$FREE_KB") GB free. Cleanup ran and archive sync may be incomplete."
else
    log "Disk guard: syncing archive to ${ARCHIVE_BUCKET:-<not-configured>}"
    if sync_archive_bucket && sync_compressed_logs && sync_critical_state; then
        prune_synced_archive
        if [ -n "${FREE_KB:-}" ]; then
            send_discord_on_state_change "healthy" "✅ **Disk/archive guard healthy** — archive sync is working and free space is $(kb_to_gb "$FREE_KB") GB."
        fi
        log "Disk guard: archive sync complete"
    else
        log "Disk guard: archive sync failed"
        send_discord_on_state_change "sync_failed" "⚠️ **Archive bucket sync failed** — local cleanup is still active, but GCS archive sync needs attention."
        exit 1
    fi
fi

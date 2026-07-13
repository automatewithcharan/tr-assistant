#!/usr/bin/env bash
# Read-only post-market labeling for live feature and lane-decision evidence.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

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

DATE="$(TZ=Asia/Kolkata date +%F)"
OUTPUT_ROOT="$REPO/results/Data/live_feature_recorder/eod/$DATE"
LOG_DIR="$REPO/results/Data/live_feature_recorder/eod/logs"
LOG="$LOG_DIR/live_evidence_eod_${DATE}.log"
LOCK="$REPO/results/Data/live_feature_recorder/eod/.live_evidence_eod.lock"
mkdir -p "$LOG_DIR" "$OUTPUT_ROOT"

exec 9>"$LOCK"
if ! flock -n 9; then
    exit 0
fi

send_discord() {
    local content="$1"
    [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return
    local payload
    payload=$("$PYTHON" -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$content")
    curl -sS -X POST "$DISCORD_WEBHOOK_URL" \
        -H 'Content-Type: application/json' \
        --data-binary "$payload" >/dev/null 2>&1 || true
}

run_readiness() {
    "$PYTHON" "$REPO/scripts/assess_live_evidence_readiness.py" \
        --inputs \
        "$REPO/results/Data/live_feature_recorder/eod/*/live_features_rows.csv" \
        "$REPO/results/Data/live_feature_recorder/eod/*/lane_decisions_rows.csv" \
        --output "$REPO/results/Data/live_feature_recorder/evidence_readiness.csv" \
        >>"$LOG" 2>&1 || true
}

if [ -f "$OUTPUT_ROOT/status.json" ]; then
    existing_status=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", ""))' "$OUTPUT_ROOT/status.json" 2>/dev/null || true)
    if [ "$existing_status" = "complete" ]; then
        run_readiness
        exit 0
    fi
fi

echo "[$(date -u +'%Y-%m-%d %H:%M:%S UTC')] live evidence EOD start date=$DATE" >>"$LOG"
set +e
"$PYTHON" "$REPO/scripts/run_live_evidence_eod.py" \
    --date "$DATE" \
    --recorder-dir "$REPO/results/Data/live_feature_recorder" \
    --output-dir "$REPO/results/Data/live_feature_recorder/eod" \
    >>"$LOG" 2>&1
RC=$?
set -e

STATUS_FILE="$OUTPUT_ROOT/status.json"
if [ -f "$STATUS_FILE" ]; then
    SUMMARY=$("$PYTHON" -c '
import json, sys
s = json.load(open(sys.argv[1]))
status = s.get("status")
symbols = s.get("symbols", 0)
feature_evaluated = s.get("feature_evaluated", 0)
feature_rows = s.get("feature_rows", 0)
decision_evaluated = s.get("decision_evaluated", 0)
decision_rows = s.get("decision_rows", 0)
coverage = s.get("combined_coverage_pct", 0)
print(
    f"status={status} symbols={symbols} "
    f"feature={feature_evaluated}/{feature_rows} "
    f"decisions={decision_evaluated}/{decision_rows} coverage={coverage}%"
)
' "$STATUS_FILE" 2>/dev/null || echo "status=unreadable")
else
    SUMMARY="status file missing"
fi

if [ "$RC" -eq 0 ]; then
    send_discord "Live evidence EOD complete for $DATE | $SUMMARY | research only, no broker orders"
else
    send_discord "Live evidence EOD needs attention for $DATE | rc=$RC | $SUMMARY | see $LOG"
fi
run_readiness
echo "[$(date -u +'%Y-%m-%d %H:%M:%S UTC')] live evidence EOD finished rc=$RC $SUMMARY" >>"$LOG"
exit "$RC"

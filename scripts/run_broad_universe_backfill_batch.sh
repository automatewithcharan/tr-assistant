#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PYTHON="$REPO/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$REPO/venv/bin/python3"
LOCK="${BROAD_UNIVERSE_BACKFILL_LOCK:-$REPO/results/Data/.broad_universe_backfill.lock}"
LOG="$REPO/results/Data/broad_universe_backfill.log"
PREFIX="$REPO/results/Data/broad_universe_5minute_backfill"
UNIVERSE="$REPO/results/Indices/NSE_BSE_COMBINED.csv"
PENDING_UNIVERSE="${PREFIX}_pending_batch.csv"
START="${BROAD_BACKFILL_START:-2024-05-30}"
END="${BROAD_BACKFILL_END:-2026-05-29}"
BATCH_SIZE="${BROAD_BACKFILL_BATCH_SIZE:-100}"
REQUEST_SLEEP="${BROAD_BACKFILL_REQUEST_SLEEP:-0.4}"
BUCKET="${TRADE_ASSISTANT_ARCHIVE_BUCKET:-}"

(umask 000; : >>"$LOCK")
chmod a+rw "$LOCK" 2>/dev/null || true
exec 9>>"$LOCK"
flock -n 9 || exit 0

if [ -f "$REPO/.env.dev" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO/.env.dev"
    set +a
    BUCKET="${TRADE_ASSISTANT_ARCHIVE_BUCKET:-$BUCKET}"
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

mkdir -p "$(dirname "$LOG")"
echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] planning broad backfill" >> "$LOG"
"$PYTHON" scripts/plan_broad_universe_backfill.py \
    --universe "$UNIVERSE" --interval 5minute --start "$START" --end "$END" \
    --batch-size "$BATCH_SIZE" --chunk-days 60 --out-prefix "$PREFIX" \
    --pending-batch-output "$PENDING_UNIVERSE" >> "$LOG" 2>&1

BATCH_COUNT=$("$PYTHON" - "$PENDING_UNIVERSE" <<'PY'
import sys
import pandas as pd
path = sys.argv[1]
frame = pd.read_csv(path)
print(len(frame))
PY
)

if [ "$BATCH_COUNT" -eq 0 ]; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] broad backfill already complete" >> "$LOG"
    exit 0
fi

BATCH_INDEX=$("$PYTHON" - "$PREFIX" <<'PY'
from pathlib import Path
import re
import sys
prefix = Path(sys.argv[1])
indices = []
pattern = re.compile(r"_batch_(\d+)_5minute_audit\.csv$")
for path in prefix.parent.glob(prefix.name + "_batch_*_5minute_audit.csv"):
    match = pattern.search(path.name)
    if match:
        indices.append(int(match.group(1)))
print(max(indices, default=-1) + 1)
PY
)

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] starting run $BATCH_INDEX with $BATCH_COUNT pending symbols" >> "$LOG"
set +e
"$PYTHON" scripts/kite_intraday_backfill.py \
    --universe "$PENDING_UNIVERSE" --limit 0 --start "$START" --end "$END" \
    --intervals 5minute --chunk-days 60 --batch-size "$BATCH_SIZE" \
    --batch-index 0 --batch-output-index "$BATCH_INDEX" \
    --sleep "$REQUEST_SLEEP" --out-prefix "$PREFIX" >> "$LOG" 2>&1
BATCH_RC=$?
set -e

SYNC_RC=0
if [ -n "$BUCKET" ] && command -v gsutil >/dev/null 2>&1; then
    RANGE_DIR="$REPO/results/Data/kite_history_backfill/5minute/${START}_${END}"
    set +e
    gsutil -q -m rsync -r "$RANGE_DIR" "$BUCKET/kite_history_backfill/5minute/${START}_${END}/" >> "$LOG" 2>&1
    SYNC_RC=$?
    set -e
fi

set +e
"$PYTHON" scripts/plan_broad_universe_backfill.py \
    --universe "$UNIVERSE" --interval 5minute --start "$START" --end "$END" \
    --batch-size "$BATCH_SIZE" --chunk-days 60 --out-prefix "$PREFIX" >> "$LOG" 2>&1
PLAN_RC=$?
set -e

AUDIT=$(printf '%s_batch_%04d_5minute_audit.csv' "$PREFIX" "$BATCH_INDEX")
STATUS="$REPO/results/Data/broad_universe_backfill_latest.json"
rm -f "$STATUS"
set +e
"$PYTHON" scripts/summarize_broad_backfill_batch.py \
    --summary "${PREFIX}_summary.json" --audit "$AUDIT" \
    --batch-index "$BATCH_INDEX" --batch-rc "$BATCH_RC" \
    --plan-rc "$PLAN_RC" --sync-rc "$SYNC_RC" --output "$STATUS" >> "$LOG" 2>&1
SUMMARY_RC=$?
set -e
MESSAGE=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("message", "Broad history status unreadable"))' "$STATUS" 2>/dev/null || echo "Broad history status missing")
send_discord "$MESSAGE"
echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] completed batch $BATCH_INDEX rc=$BATCH_RC plan_rc=$PLAN_RC sync_rc=$SYNC_RC summary_rc=$SUMMARY_RC" >> "$LOG"
exit "$SUMMARY_RC"

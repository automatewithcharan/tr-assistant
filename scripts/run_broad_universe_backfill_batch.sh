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
START="${BROAD_BACKFILL_START:-2024-05-30}"
END="${BROAD_BACKFILL_END:-2026-05-29}"
BATCH_SIZE="${BROAD_BACKFILL_BATCH_SIZE:-100}"
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
    --batch-size "$BATCH_SIZE" --chunk-days 60 --out-prefix "$PREFIX" >> "$LOG" 2>&1

BATCH_INDEX=$("$PYTHON" - "$PREFIX" <<'PY'
import pandas as pd
import sys
manifest = pd.read_csv(sys.argv[1] + "_manifest.csv")
pending = manifest[manifest["needs_fetch"].astype(str).str.lower().isin({"true", "1"})]
print(int(pending["batch_index"].min()) if not pending.empty else -1)
PY
)

if [ "$BATCH_INDEX" -lt 0 ]; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] broad backfill already complete" >> "$LOG"
    exit 0
fi

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] starting batch $BATCH_INDEX" >> "$LOG"
set +e
"$PYTHON" scripts/kite_intraday_backfill.py \
    --universe "$UNIVERSE" --limit 0 --start "$START" --end "$END" \
    --intervals 5minute --chunk-days 60 --batch-size "$BATCH_SIZE" \
    --batch-index "$BATCH_INDEX" --sleep 0.7 --out-prefix "$PREFIX" >> "$LOG" 2>&1
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

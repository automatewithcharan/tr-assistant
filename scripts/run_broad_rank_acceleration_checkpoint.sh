#!/usr/bin/env bash
# Run the causal rank-acceleration registry only at material history milestones.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PYTHON="$REPO/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$REPO/venv/bin/python3"
LOCK=/tmp/broad_rank_acceleration_checkpoint.lock
BACKFILL_LOCK=/tmp/broad_universe_backfill.lock
LOG="$REPO/results/Data/broad_rank_acceleration/checkpoint.log"
SUMMARY="$REPO/results/Data/broad_universe_5minute_backfill_summary.json"
DATA_DIR="$REPO/results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29"
CALENDAR="$DATA_DIR/NIFTY 50.csv"
WORKERS="${BROAD_RANK_WORKERS:-$(nproc)}"
case "$WORKERS" in
    ''|*[!0-9]*) WORKERS="$(nproc)" ;;
esac
if [ "$WORKERS" -lt 1 ]; then
    WORKERS=1
fi
if [ "$WORKERS" -gt 16 ]; then
    WORKERS=16
fi

exec 9>"$LOCK"
flock -n 9 || exit 0
exec 8>"$BACKFILL_LOCK"
flock -n 8 || exit 0
mkdir -p "$(dirname "$LOG")"

if [ ! -s "$SUMMARY" ] || [ ! -s "$CALENDAR" ]; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] waiting for summary/calendar" >> "$LOG"
    exit 0
fi

CACHED=$($PYTHON -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("exact_range_cache_files", 0)))' "$SUMMARY")
MAPPED=$($PYTHON -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("mapped_symbols", 0)))' "$SUMMARY")
MILESTONE=0
for value in 1000 2000 2900; do
    if [ "$CACHED" -ge "$value" ] && [ ! -s "$REPO/results/Data/broad_rank_acceleration/checkpoint_${value}/status.json" ]; then
        MILESTONE=$value
        break
    fi
done
if [ "$MILESTONE" -eq 0 ]; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] coverage $CACHED below first checkpoint" >> "$LOG"
    exit 0
fi

OUT="$REPO/results/Data/broad_rank_acceleration/checkpoint_${MILESTONE}"
STATUS="$OUT/status.json"
if [ -s "$STATUS" ]; then
    exit 0
fi
mkdir -p "$OUT"
printf '%s\n' "$CACHED" > "$OUT/coverage_at_start.txt"
printf '%s\n' "$MAPPED" > "$OUT/mapped_at_start.txt"
echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] starting checkpoint=$MILESTONE cached=$CACHED mapped=$MAPPED" >> "$LOG"

nice -n 10 "$PYTHON" scripts/backtest_fixed_time_leader_rank.py \
    --data-dir "$DATA_DIR" --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/fixed_time" \
    --signal-times 09:30,09:45,10:00,10:30 \
    --horizons 60 --targets 0.5,0.7,1.0,1.5 --stops 0.5,0.7,1.0,1.5 \
    --top-k 1,2 --workers "$WORKERS" --progress-every 100 --warmup-days 76 \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/audit_top_gainer_early_recall.py \
    --data-dir "$DATA_DIR" --candidates "$OUT/fixed_time_candidates.csv" \
    --output-prefix "$OUT/top_gainer_recall" --workers "$WORKERS" \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/fixed_time_candidates.csv" \
    --runner-events "$OUT/top_gainer_recall_events.csv" \
    --calendar-file "$CALENDAR" --output-prefix "$OUT/eventual_runner_classifier" \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/research_rank_acceleration_registry.py \
    --input "$OUT/fixed_time_candidates.csv" --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/long" >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/research_rank_acceleration_registry.py \
    --side short --input "$OUT/fixed_time_candidates.csv" \
    --calendar-file "$CALENDAR" --data-dir "$DATA_DIR" \
    --output-prefix "$OUT/short" --horizon-minutes 60 \
    >> "$LOG" 2>&1

"$PYTHON" scripts/build_broad_rank_checkpoint_status.py \
    --output-dir "$OUT" --status "$STATUS" --milestone "$MILESTONE" \
    --cached-symbols "$CACHED" --mapped-symbols "$MAPPED" >> "$LOG" 2>&1

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] completed checkpoint=$MILESTONE cached=$CACHED" >> "$LOG"

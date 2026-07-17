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
    --top-k 1,2 --workers 24 --progress-every 100 --warmup-days 76 \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/research_rank_acceleration_registry.py \
    --input "$OUT/fixed_time_candidates.csv" --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/long" >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/research_rank_acceleration_registry.py \
    --side short --input "$OUT/fixed_time_candidates.csv" \
    --calendar-file "$CALENDAR" --data-dir "$DATA_DIR" \
    --output-prefix "$OUT/short" --horizon-minutes 60 \
    >> "$LOG" 2>&1

"$PYTHON" - "$OUT" "$STATUS" "$MILESTONE" "$CACHED" "$MAPPED" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

output_dir, status_path, milestone, cached, mapped = sys.argv[1:]
output = Path(output_dir)
long_status = json.load(open(output / "long_status.json"))
short_status = json.load(open(output / "short_status.json"))
status = {
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "milestone": int(milestone),
    "cached_symbols": int(cached),
    "mapped_symbols": int(mapped),
    "long_selection_gate_passed": bool(long_status.get("selection_gate_passed", False)),
    "short_selection_gate_passed": bool(short_status.get("selection_gate_passed", False)),
    "long_final_goal_evidence_passed": bool(long_status.get("final_goal_evidence_passed", False)),
    "short_final_goal_evidence_passed": bool(short_status.get("final_goal_evidence_passed", False)),
    "evidence_accepted": False,
    "paper_promotion_allowed": False,
    "real_orders_allowed": False,
    "long_status": long_status,
    "short_status": short_status,
}
Path(status_path).write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
PY

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] completed checkpoint=$MILESTONE cached=$CACHED" >> "$LOG"

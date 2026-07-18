#!/usr/bin/env bash
# Run the causal rank-acceleration registry only at material history milestones.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PYTHON="$REPO/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$REPO/venv/bin/python3"
LOCK="${BROAD_RANK_CHECKPOINT_LOCK:-$REPO/results/Data/.broad_rank_acceleration_checkpoint.lock}"
BACKFILL_LOCK="${BROAD_UNIVERSE_BACKFILL_LOCK:-$REPO/results/Data/.broad_universe_backfill.lock}"
LOG="$REPO/results/Data/broad_rank_acceleration/checkpoint.log"
SUMMARY="$REPO/results/Data/broad_universe_5minute_backfill_summary.json"
DATA_DIR="$REPO/results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29"
CALENDAR="$DATA_DIR/NIFTY 50.csv"
QUALITY_UNIVERSE="$REPO/results/Data/broad_universe_5minute_backfill_quality_eligible_universe.csv"
WORKERS="${BROAD_RANK_WORKERS:-$(nproc)}"
CAPITAL="${BROAD_RANK_CAPITAL:-500000}"
DAILY_GOAL="${BROAD_RANK_DAILY_GOAL:-10000}"
STRESS_COST_PCT="${BROAD_RANK_STRESS_COST_PCT:-0.10}"
case "$WORKERS" in
    ''|*[!0-9]*) WORKERS="$(nproc)" ;;
esac
if [ "$WORKERS" -lt 1 ]; then
    WORKERS=1
fi
if [ "$WORKERS" -gt 16 ]; then
    WORKERS=16
fi

(umask 000; : >>"$LOCK")
chmod a+rw "$LOCK" 2>/dev/null || true
exec 9>>"$LOCK"
flock -n 9 || exit 0
(umask 000; : >>"$BACKFILL_LOCK")
chmod a+rw "$BACKFILL_LOCK" 2>/dev/null || true
exec 8>>"$BACKFILL_LOCK"
flock -n 8 || exit 0
mkdir -p "$(dirname "$LOG")"

if [ ! -s "$SUMMARY" ] || [ ! -s "$CALENDAR" ] || [ ! -s "$QUALITY_UNIVERSE" ]; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] waiting for summary/calendar" >> "$LOG"
    exit 0
fi

CACHED=$($PYTHON -c 'import json,sys; d=json.load(open(sys.argv[1])); print(int(d.get("quality_eligible_cache_files", d.get("exact_range_cache_files", 0))))' "$SUMMARY")
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
printf '%s\n' "$CAPITAL" > "$OUT/starting_capital.txt"
printf '%s\n' "$DAILY_GOAL" > "$OUT/daily_goal.txt"
printf '%s\n' "$STRESS_COST_PCT" > "$OUT/stress_cost_pct.txt"
git rev-parse HEAD > "$OUT/code_commit_at_start.txt"
sha256sum "$QUALITY_UNIVERSE" | awk '{print $1}' > "$OUT/quality_universe_sha256.txt"
sha256sum "$CALENDAR" | awk '{print $1}' > "$OUT/calendar_sha256.txt"
cp "$SUMMARY" "$OUT/backfill_summary_at_start.json"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$OUT/started_at_utc.txt"
echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] starting checkpoint=$MILESTONE cached=$CACHED mapped=$MAPPED" >> "$LOG"

nice -n 10 "$PYTHON" scripts/backtest_fixed_time_leader_rank.py \
    --data-dir "$DATA_DIR" --calendar-file "$CALENDAR" \
    --universe-file "$QUALITY_UNIVERSE" \
    --output-prefix "$OUT/fixed_time" \
    --signal-times 09:30,09:45,10:00,10:30 \
    --horizons 60 --targets 0.5,0.7,1.0,1.5 --stops 0.5,0.7,1.0,1.5 \
    --top-k 1,2 --workers "$WORKERS" --progress-every 100 --warmup-days 76 \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/audit_top_gainer_early_recall.py \
    --data-dir "$DATA_DIR" --candidates "$OUT/fixed_time_candidates.csv" \
    --output-prefix "$OUT/top_gainer_recall" --workers "$WORKERS" \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/audit_runner_filter_funnel.py \
    --data-dir "$DATA_DIR" --runner-events "$OUT/top_gainer_recall_events.csv" \
    --calendar-file "$CALENDAR" --universe-file "$QUALITY_UNIVERSE" \
    --output-prefix "$OUT/runner_filter_funnel" --workers "$WORKERS" \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/fixed_time_candidates.csv" \
    --runner-events "$OUT/top_gainer_recall_events.csv" \
    --calendar-file "$CALENDAR" --output-prefix "$OUT/eventual_runner_classifier" \
    --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" \
    --stress-cost-pct "$STRESS_COST_PCT" \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/fixed_time_candidates.csv" \
    --runner-events "$OUT/top_gainer_recall_events.csv" \
    --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/eventual_runner_temporal_classifier" \
    --feature-set temporal --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" \
    --stress-cost-pct "$STRESS_COST_PCT" >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/fixed_time_candidates.csv" \
    --runner-events "$OUT/top_gainer_recall_events.csv" \
    --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/eventual_runner_bar_path_classifier" \
    --feature-set bar_path --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" \
    --stress-cost-pct "$STRESS_COST_PCT" >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/fixed_time_candidates.csv" \
    --calendar-file "$CALENDAR" --output-prefix "$OUT/direct_target_classifier" \
    --label-mode target_before_stop --capital "$CAPITAL" \
    --daily-goal "$DAILY_GOAL" --stress-cost-pct "$STRESS_COST_PCT" \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/fixed_time_candidates.csv" \
    --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/direct_target_temporal_classifier" \
    --label-mode target_before_stop --feature-set temporal \
    --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" \
    --stress-cost-pct "$STRESS_COST_PCT" >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/fixed_time_candidates.csv" \
    --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/direct_target_bar_path_classifier" \
    --label-mode target_before_stop --feature-set bar_path \
    --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" \
    --stress-cost-pct "$STRESS_COST_PCT" >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/research_rank_acceleration_registry.py \
    --input "$OUT/fixed_time_candidates.csv" --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/long" --capital "$CAPITAL" \
    --daily-goal "$DAILY_GOAL" --stress-cost-pct "$STRESS_COST_PCT" \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/research_rank_acceleration_registry.py \
    --side short --input "$OUT/fixed_time_candidates.csv" \
    --calendar-file "$CALENDAR" --data-dir "$DATA_DIR" \
    --output-prefix "$OUT/short" --horizon-minutes 60 \
    --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" \
    --stress-cost-pct "$STRESS_COST_PCT" \
    >> "$LOG" 2>&1

for SIGNAL_TIME in 09:30 09:45 10:00 10:30; do
    SIGNAL_KEY="${SIGNAL_TIME/:/}"
    nice -n 10 "$PYTHON" scripts/walk_forward_fixed_time_leader_ranker.py \
        --input "$OUT/fixed_time_candidates.csv" --calendar-file "$CALENDAR" \
        --output-prefix "$OUT/expected_return_${SIGNAL_KEY}" \
        --signal-time "$SIGNAL_TIME" --contract h60_target_1.5_stop_1 \
        --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" \
        --stress-cost-pct "$STRESS_COST_PCT" >> "$LOG" 2>&1
done

"$PYTHON" scripts/select_expected_return_ranker_registry.py \
    --glob "$OUT/expected_return_*_selected.json" \
    --output-prefix "$OUT/expected_return_registry" >> "$LOG" 2>&1

"$PYTHON" scripts/build_broad_rank_checkpoint_status.py \
    --output-dir "$OUT" --status "$STATUS" --milestone "$MILESTONE" \
    --cached-symbols "$CACHED" --mapped-symbols "$MAPPED" >> "$LOG" 2>&1

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] completed checkpoint=$MILESTONE cached=$CACHED" >> "$LOG"

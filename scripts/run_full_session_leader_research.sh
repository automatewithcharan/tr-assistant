#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
DATA_DIR="${FULL_SESSION_DATA_DIR:-results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29}"
CALENDAR="${FULL_SESSION_CALENDAR:-$DATA_DIR/NIFTY 50.csv}"
UNIVERSE="${FULL_SESSION_UNIVERSE:-results/Data/broad_universe_5minute_backfill_quality_eligible_universe.csv}"
OUT="${FULL_SESSION_OUTPUT_DIR:-results/Data/broad_rank_acceleration/checkpoint_1000/full_session}"
CAPITAL="${FULL_SESSION_CAPITAL:-500000}"
DAILY_GOAL="${FULL_SESSION_DAILY_GOAL:-10000}"
WORKERS="${FULL_SESSION_WORKERS:-8}"
# Keep this aligned with the frozen broad-checkpoint schedule. The 14:15
# signal enters on the 14:20 bar and its 60-minute contract ends by 15:20.
SIGNAL_TIMES="${FULL_SESSION_SIGNAL_TIMES:-09:30,09:45,10:00,10:15,10:30,10:45,11:00,11:15,11:30,11:45,12:00,12:15,12:30,12:45,13:00,13:15,13:30,13:45,14:00,14:15}"

mkdir -p "$OUT"

nice -n 10 "$PYTHON" scripts/backtest_fixed_time_leader_rank.py \
    --data-dir "$DATA_DIR" --calendar-file "$CALENDAR" \
    --universe-file "$UNIVERSE" --output-prefix "$OUT/candidates" \
    --signal-times "$SIGNAL_TIMES" --horizons 60 \
    --targets 0.5,0.7,1.0,1.5 --stops 0.5,0.7,1.0,1.5 --top-k 1,2 \
    --workers "$WORKERS" --progress-every 100 \
    --turnover-mode projected_daily --min-signal-turnover 30000000 \
    --min-signal-turnover-floor 5000000 \
    --min-move-from-open-pct -0.5 --max-move-from-open-pct 5.0 \
    --min-day-change-pct -0.5 --max-day-change-pct 7.0

nice -n 10 "$PYTHON" scripts/audit_top_gainer_early_recall.py \
    --data-dir "$DATA_DIR" --candidates "$OUT/candidates_candidates.csv" \
    --output-prefix "$OUT/top_gainer_recall" --workers "$WORKERS" \
    --cutoffs "$SIGNAL_TIMES"

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/candidates_candidates.csv" \
    --calendar-file "$CALENDAR" --output-prefix "$OUT/expected_return_bar_path" \
    --label-mode expected_return --feature-set bar_path \
    --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" --stress-cost-pct 0.10

echo "Full-session research completed. Outputs: $OUT"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
DATA_DIR="${FULL_SESSION_DATA_DIR:-results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29}"
CALENDAR="${FULL_SESSION_CALENDAR:-$DATA_DIR/NIFTY 50.csv}"
UNIVERSE="${FULL_SESSION_UNIVERSE:-results/Data/broad_universe_5minute_backfill_quality_eligible_universe.csv}"
OUT="${FULL_SESSION_REGIME_OUTPUT_DIR:-results/Data/broad_rank_acceleration/checkpoint_1000/full_session_regime}"
WORKERS="${FULL_SESSION_WORKERS:-12}"
SIGNAL_TIMES="${FULL_SESSION_SIGNAL_TIMES:-09:30,09:45,10:00,10:15,10:30,10:45,11:00,11:15,11:30,11:45,12:00,12:15,12:30,12:45,13:00,13:15,13:30}"

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

FULL_SESSION_OUTPUT_DIR="$OUT" \
FULL_SESSION_CANDIDATES="$OUT/candidates_candidates.csv" \
FULL_SESSION_CONTRACT_OUTPUT_DIR="$OUT/contract_matrix" \
FULL_SESSION_FEATURE_SET=bar_path_regime \
FULL_SESSION_CONTRACTS="${FULL_SESSION_CONTRACTS:-h60_target_1.5_stop_1.5}" \
FULL_SESSION_CONTRACT_PARALLEL="${FULL_SESSION_CONTRACT_PARALLEL:-1}" \
FULL_SESSION_CONTRACT_THREADS="${FULL_SESSION_CONTRACT_THREADS:-8}" \
bash scripts/run_full_session_contract_matrix.sh

echo "Full-session regime-aware leader research completed. Outputs: $OUT"

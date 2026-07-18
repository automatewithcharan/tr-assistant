#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
BASE="${FULL_SESSION_OUTPUT_DIR:-results/Data/broad_rank_acceleration/checkpoint_1000/full_session}"
CANDIDATES="${FULL_SESSION_CANDIDATES:-$BASE/candidates_candidates.csv}"
CALENDAR="${FULL_SESSION_CALENDAR:-results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29/NIFTY 50.csv}"
OUT="${FULL_SESSION_CONTRACT_OUTPUT_DIR:-$BASE/contract_matrix}"
CAPITAL="${FULL_SESSION_CAPITAL:-500000}"
DAILY_GOAL="${FULL_SESSION_DAILY_GOAL:-10000}"
MAX_PARALLEL="${FULL_SESSION_CONTRACT_PARALLEL:-8}"
THREADS_PER_JOB="${FULL_SESSION_CONTRACT_THREADS:-4}"

mkdir -p "$OUT/logs"

contracts=()
for target in 0.5 0.7 1 1.5; do
    for stop in 0.5 0.7 1 1.5; do
        contracts+=("h60_target_${target}_stop_${stop}")
    done
done

run_contract() {
    local contract="$1"
    local prefix="$OUT/expected_return_${contract}"
    env OMP_NUM_THREADS="$THREADS_PER_JOB" MKL_NUM_THREADS=1 \
        OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
        nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
        --candidates "$CANDIDATES" --calendar-file "$CALENDAR" \
        --output-prefix "$prefix" --label-mode expected_return \
        --feature-set bar_path --return-column "return__${contract}" \
        --exit-time-column "exit_time__${contract}" \
        --exit-reason-column "exit__${contract}" \
        --capital "$CAPITAL" --daily-goal "$DAILY_GOAL" \
        --stress-cost-pct 0.10 >"$OUT/logs/${contract}.log" 2>&1
}

active=0
failed=0
for contract in "${contracts[@]}"; do
    run_contract "$contract" &
    active=$((active + 1))
    if [ "$active" -ge "$MAX_PARALLEL" ]; then
        if ! wait -n; then
            failed=1
        fi
        active=$((active - 1))
    fi
done

while [ "$active" -gt 0 ]; do
    if ! wait -n; then
        failed=1
    fi
    active=$((active - 1))
done

if [ "$failed" -ne 0 ]; then
    echo "One or more contract models failed; inspect $OUT/logs" >&2
    exit 1
fi

"$PYTHON" scripts/select_full_session_contract_registry.py \
    --glob "$OUT/expected_return_*_status.json" \
    --output-prefix "$OUT/contract_registry"

echo "Full-session contract matrix completed. Outputs: $OUT"

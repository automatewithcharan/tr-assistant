#!/usr/bin/env bash
# Rebuild the current cached-universe eventual-runner baseline after market.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PYTHON="$REPO/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$REPO/.venv/bin/python3"
LOCK=/tmp/broad_universe_backfill.lock
DATA_DIR="$REPO/results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29"
CALENDAR="$DATA_DIR/NIFTY 50.csv"
OUT="$REPO/results/Data/eventual_runner_current_505"
LOG="$OUT/run.log"
WORKERS="$(nproc)"
[ "$WORKERS" -gt 16 ] && WORKERS=16

if [ -f "$REPO/.env.dev" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO/.env.dev"
    set +a
fi

send_discord() {
    local content="$1"
    [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return
    local payload
    payload=$("$PYTHON" -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$content")
    curl -sS -X POST "$DISCORD_WEBHOOK_URL" \
        -H 'Content-Type: application/json' --data-binary "$payload" >/dev/null 2>&1 || true
}

mkdir -p "$OUT"
exec 9>"$LOCK"
flock -n 9 || exit 0
trap 'rc=$?; send_discord "Eventual-runner 505 research FAILED | rc=$rc | see $LOG | real orders remain OFF"; exit $rc' ERR

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] current eventual-runner baseline start" >> "$LOG"
nice -n 10 "$PYTHON" scripts/backtest_fixed_time_leader_rank.py \
    --data-dir "$DATA_DIR" --calendar-file "$CALENDAR" \
    --output-prefix "$OUT/fixed_time" \
    --signal-times 09:30,09:45,10:00,10:30 --horizons 60 \
    --targets 1.5 --stops 1.0 --top-k 1 --workers "$WORKERS" \
    --progress-every 100 --warmup-days 76 >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/audit_top_gainer_early_recall.py \
    --data-dir "$DATA_DIR" --candidates "$OUT/fixed_time_candidates.csv" \
    --output-prefix "$OUT/top_gainer_recall" --workers "$WORKERS" \
    >> "$LOG" 2>&1

nice -n 10 "$PYTHON" scripts/walk_forward_eventual_runner_classifier.py \
    --candidates "$OUT/fixed_time_candidates.csv" \
    --runner-events "$OUT/top_gainer_recall_events.csv" \
    --calendar-file "$CALENDAR" --output-prefix "$OUT/classifier" \
    >> "$LOG" 2>&1

MESSAGE=$("$PYTHON" - "$OUT/classifier_status.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
m = s["selected"]
print(
    "Eventual-runner 505 research complete | "
    f"selection={s.get('selected', {}).get('selection_gate_passed', False)} "
    f"goal={s.get('final_goal_contract_passed', False)} | "
    f"stress final avg Rs {float(m.get('stress_final_avg_daily_pnl', 0)):.0f}/day | "
    f"lower95 Rs {float(m.get('stress_final_mean_lower_95_pnl', 0)):.0f}/day | "
    f"goal days {float(m.get('stress_final_goal_hit_pct', 0)):.1f}% | "
    "research only, real orders OFF"
)
PY
)
send_discord "$MESSAGE"
echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] $MESSAGE" >> "$LOG"

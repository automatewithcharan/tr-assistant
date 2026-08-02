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
        "$REPO/results/Data/live_feature_recorder/bootstrap/*/live_features_rows.csv" \
        "$REPO/results/Data/live_feature_recorder/bootstrap/*/lane_decisions_rows.csv" \
        --output "$REPO/results/Data/live_feature_recorder/evidence_readiness.csv" \
        >>"$LOG" 2>&1 || true
    "$PYTHON" "$REPO/scripts/assess_frozen_microstructure_selector.py" \
        --inputs \
        "$REPO/results/Data/live_feature_recorder/eod/*/live_features_rows.csv" \
        "$REPO/results/Data/live_feature_recorder/bootstrap/*/live_features_rows.csv" \
        --output-prefix "$REPO/results/Data/live_feature_recorder/frozen_microstructure_selector" \
        >>"$LOG" 2>&1 || true
}

run_ranked_replay() {
    local pattern="$REPO/results/Data/ranked_confirmation_paper/observations_*.csv"
    if ! compgen -G "$pattern" >/dev/null; then
        return
    fi
    "$PYTHON" "$REPO/scripts/assess_ranked_confirmation_replay.py" \
        --input "$pattern" \
        --output "$REPO/results/Data/ranked_confirmation_replay/readiness.csv" \
        --capital 400000 \
        --daily-goal 10000 \
        >>"$LOG" 2>&1 || true
}

run_live_microstructure_research() {
    local feature_pattern="$REPO/results/Data/live_feature_recorder/live_features_20*.csv"
    local minute_pattern="$REPO/results/Data/kite_history_backfill/minute/*"
    if ! compgen -G "$feature_pattern" >/dev/null; then
        return
    fi
    "$PYTHON" "$REPO/scripts/assess_live_runner_fade.py" \
        --input "$feature_pattern" \
        --minute-cache-glob "$minute_pattern" \
        --output-dir "$REPO/results/Data/live_runner_fade" \
        --end-date "$DATE" \
        >>"$LOG" 2>&1 || true
    "$PYTHON" "$REPO/scripts/assess_live_microstructure_ignition.py" \
        --input "$feature_pattern" \
        --minute-cache-glob "$minute_pattern" \
        --output-dir "$REPO/results/Data/live_microstructure_ignition" \
        --end-date "$DATE" \
        >>"$LOG" 2>&1 || true
}

run_event_shadow_rebuild() {
    local bars="$REPO/results/Data/intraday_event_shadow/bars_${DATE}.csv"
    local rebuilt="$REPO/results/Data/intraday_event_shadow_rebuilt"
    if [ ! -s "$bars" ]; then
        return
    fi
    "$PYTHON" "$REPO/scripts/rebuild_intraday_event_shadow_evidence.py" \
        --source-root "$REPO/results/Data" \
        --output-root "$rebuilt" \
        --dates "$DATE" \
        --source-timezone auto \
        >>"$LOG" 2>&1 || true
    "$PYTHON" "$REPO/scripts/train_intraday_event_shadow_model.py" \
        --input "$rebuilt/intraday_event_shadow/outcomes_*.csv" \
        --output-dir "$rebuilt/model_v1" \
        >>"$LOG" 2>&1 || true
    "$PYTHON" "$REPO/scripts/audit_live_top_gainer_recorder_recall.py" \
        --features "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --bars "$rebuilt/intraday_event_shadow/bars_20*.csv" \
        --output-dir "$REPO/results/Data/live_top_gainer_recorder_recall" \
        >>"$LOG" 2>&1 || true
    "$PYTHON" "$REPO/scripts/audit_live_runner_capture_funnel.py" \
        --recall "$REPO/results/Data/live_top_gainer_recorder_recall/events.csv" \
        --predictions "$REPO/results/Data/frozen_microstructure_paper/predictions_20*.csv" \
        --registrations "$REPO/results/Data/frozen_ranker_prediction_contract_matrix/registrations_20*.csv" \
        --broad-registrations "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --output-dir "$REPO/results/Data/live_runner_capture_funnel" \
        --score-threshold 0.20 \
        >>"$LOG" 2>&1 || true
    "$PYTHON" "$REPO/scripts/assess_live_leader_continuation.py" \
        --features "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --bars "$rebuilt/intraday_event_shadow/bars_20*.csv" \
        --output-dir "$REPO/results/Data/live_leader_continuation" \
        >>"$LOG" 2>&1 || true
    "$PYTHON" "$REPO/scripts/walk_forward_live_microstructure_ranker.py" \
        --input "$REPO/results/Data/live_leader_continuation/snapshot_outcomes.csv" \
        --output-prefix "$REPO/results/Data/live_leader_continuation/frozen_ranker" \
        --contract t1.5_s1.0 --frozen-through 2026-07-21 \
        --forward-start 2026-07-22 --capital 500000 --daily-goal 10000 \
        --model-output "$REPO/results/Data/live_leader_continuation/frozen_ranker_live_model.joblib" \
        >>"$LOG" 2>&1 || true
    local frozen_bundle="$REPO/results/Data/research_models/historical_event_family_target.joblib"
    if [ -s "$frozen_bundle" ]; then
        "$PYTHON" "$REPO/scripts/evaluate_historical_event_model_live_holdout.py" \
            --live-glob "$rebuilt/intraday_event_shadow/outcomes_*.csv" \
            --output-prefix "$rebuilt/historical_transfer/holdout" \
            --model-bundle "$frozen_bundle" \
            --reuse-model-bundle \
            >>"$LOG" 2>&1 || true
    fi
    "$PYTHON" "$REPO/scripts/assess_event_shadow_exit_contracts.py" \
        --root "$rebuilt/intraday_event_shadow" \
        --output-dir "$rebuilt/exit_contracts" \
        >>"$LOG" 2>&1 || true
    "$PYTHON" "$REPO/scripts/assess_event_shadow_candidate_selectors.py" \
        --root "$rebuilt/intraday_event_shadow" \
        --output-dir "$rebuilt/candidate_selectors" \
        >>"$LOG" 2>&1 || true
}

run_rank_migration_audit() {
    local outcomes="$REPO/results/Data/live_leader_continuation/snapshot_outcomes.csv"
    if [ ! -s "$outcomes" ]; then
        return
    fi
    "$PYTHON" "$REPO/scripts/assess_live_rank_migration.py" \
        --input "$outcomes" \
        --output-dir "$REPO/results/Data/live_rank_migration" \
        --frozen-through 2026-07-21 --forward-start 2026-07-22 \
        --threshold 0.20 --capital 500000 --daily-goal 10000 \
        >>"$LOG" 2>&1 || true
}

run_stop_avoidance_audit() {
    local features="$REPO/results/Data/live_rank_migration/rank_migration_features.csv"
    if [ ! -s "$features" ]; then
        return
    fi
    "$PYTHON" "$REPO/scripts/assess_live_stop_avoidance.py" \
        --input "$features" \
        --output-dir "$REPO/results/Data/live_stop_avoidance" \
        --capital 500000 --daily-goal 10000 --stress-cost-pct 0.10 \
        >>"$LOG" 2>&1 || true
}

run_historical_rank_displacement_audit() {
    local predictions="$REPO/results/Data/live_stop_avoidance/selection_predictions.csv"
    [[ -f "$predictions" ]] || return 0
    "$PYTHON" "$REPO/scripts/assess_historical_rank_displacement.py" \
        --predictions "$predictions" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/historical_rank_displacement" \
        --daily-goal 10000 \
        >>"$LOG" 2>&1 || true
}

run_prior_session_ranker_paper() {
    "$PYTHON" "$REPO/scripts/assess_prior_session_ranker_paper.py" \
        --prediction-glob "$REPO/results/Data/frozen_microstructure_paper/predictions_20*.csv" \
        --trade-glob "$REPO/results/Data/frozen_microstructure_paper/trades_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/prior_session_ranker_paper" \
        --capital 500000 --daily-goal 10000 \
        --stress-cost-pct 0.10 --minimum-sessions 20 \
        --minimum-trade-sessions 10 --minimum-trades 20 \
        --max-trades-per-day 10 --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_frozen_ranker_prediction_matrix() {
    "$PYTHON" "$REPO/scripts/assess_frozen_ranker_prediction_matrix.py" \
        --prediction-glob "$REPO/results/Data/frozen_microstructure_paper/predictions_20*.csv" \
        --registration-glob "$REPO/results/Data/frozen_ranker_prediction_contract_matrix/registrations_20*.csv" \
        --outcome-glob "$REPO/results/Data/frozen_ranker_prediction_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/frozen_ranker_prediction_strategy" \
        --threshold 0.20 --capital 500000 --daily-goal 10000 \
        --extra-cost-pct 0.10 --selection-sessions 12 \
        --minimum-forward-sessions 20 \
        --minimum-forward-trade-sessions 10 \
        --minimum-forward-trades 30 --max-trades-per-day 10 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_full_universe_rank_strategy() {
    "$PYTHON" "$REPO/scripts/assess_full_universe_rank_strategy.py" \
        --outcome-glob "$REPO/results/Data/live_feature_recorder/eod/20*/live_features_rows.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_strategy" \
        --capital 500000 --daily-goal 10000 \
        --selection-sessions 12 --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_exact_full_rank_strategy() {
    "$PYTHON" "$REPO/scripts/assess_exact_full_rank_paper.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_paper/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_exact_strategy" \
        --capital 500000 --daily-goal 10000 \
        --max-trades 10 --selection-sessions 12 --forward-sessions 20 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_full_rank_contract_matrix() {
    "$PYTHON" "$REPO/scripts/assess_full_rank_contract_matrix.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_contract_strategy" \
        --capital 500000 --daily-goal 10000 \
        --max-trades 10 --selection-sessions 12 --forward-sessions 20 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_depth_executable_rank_strategy() {
    "$PYTHON" "$REPO/scripts/assess_depth_executable_rank_strategy.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_depth_execution" \
        --capital 500000 --daily-goal 10000 \
        --max-trades 10 --selection-sessions 12 --forward-sessions 20 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_predeclared_capacity_challenger() {
    "$PYTHON" "$REPO/scripts/assess_predeclared_capacity_challenger.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/predeclared_capacity_challenger" \
        --forward-start 2026-08-03 --daily-goal 10000 \
        >>"$LOG" 2>&1 || true
}

run_frozen_rank_migration_depth_forward() {
    "$PYTHON" "$REPO/scripts/assess_frozen_rank_migration_depth_forward.py" \
        --registration-glob "$REPO/results/Data/frozen_rank_migration_contract_matrix/registrations_20*.csv" \
        --outcome-glob "$REPO/results/Data/frozen_rank_migration_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --lane-status-glob "$REPO/results/Data/frozen_rank_migration_contract_matrix/status_20*.json" \
        --model "$REPO/results/Data/live_rank_migration/frozen_rank_migration_model.joblib" \
        --output-dir "$REPO/results/Data/frozen_rank_migration_depth_forward" \
        --capital 500000 --daily-goal 10000 \
        --forward-sessions 20 --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_frozen_stop_avoidance_depth_forward() {
    "$PYTHON" "$REPO/scripts/assess_frozen_stop_avoidance_depth_forward.py" \
        --registration-glob "$REPO/results/Data/frozen_stop_avoidance_contract_matrix/registrations_20*.csv" \
        --outcome-glob "$REPO/results/Data/frozen_stop_avoidance_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --lane-status-glob "$REPO/results/Data/frozen_stop_avoidance_contract_matrix/status_20*.json" \
        --model "$REPO/results/Data/live_stop_avoidance/frozen_stop_avoidance_model.joblib" \
        --output-dir "$REPO/results/Data/frozen_stop_avoidance_depth_forward" \
        --capital 500000 --daily-goal 10000 \
        --forward-sessions 20 --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_frozen_runner_qualified_stop_depth_forward() {
    "$PYTHON" "$REPO/scripts/assess_frozen_runner_qualified_stop_depth_forward.py" \
        --registration-glob "$REPO/results/Data/frozen_runner_qualified_stop_contract_matrix/registrations_20*.csv" \
        --outcome-glob "$REPO/results/Data/frozen_runner_qualified_stop_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --lane-status-glob "$REPO/results/Data/frozen_runner_qualified_stop_contract_matrix/status_20*.json" \
        --model "$REPO/results/Data/live_stop_avoidance/frozen_runner_qualified_stop_model.joblib" \
        --output-dir "$REPO/results/Data/frozen_runner_qualified_stop_depth_forward" \
        --capital 500000 --daily-goal 10000 \
        --forward-sessions 20 --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_frozen_balanced_stop_portfolio_forward() {
    "$PYTHON" "$REPO/scripts/assess_frozen_balanced_stop_portfolio_forward.py" \
        --stop-registration-glob "$REPO/results/Data/frozen_stop_avoidance_contract_matrix/registrations_20*.csv" \
        --stop-outcome-glob "$REPO/results/Data/frozen_stop_avoidance_contract_matrix/outcomes_20*.csv" \
        --stop-status-glob "$REPO/results/Data/frozen_stop_avoidance_contract_matrix/status_20*.json" \
        --stop-model "$REPO/results/Data/live_stop_avoidance/frozen_stop_avoidance_model.joblib" \
        --runner-registration-glob "$REPO/results/Data/frozen_runner_qualified_stop_contract_matrix/registrations_20*.csv" \
        --runner-outcome-glob "$REPO/results/Data/frozen_runner_qualified_stop_contract_matrix/outcomes_20*.csv" \
        --runner-status-glob "$REPO/results/Data/frozen_runner_qualified_stop_contract_matrix/status_20*.json" \
        --runner-model "$REPO/results/Data/live_stop_avoidance/frozen_runner_qualified_stop_model.joblib" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/frozen_balanced_stop_portfolio_forward" \
        --daily-goal 10000 --forward-sessions 20 --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_frozen_balanced_margin_forward() {
    "$PYTHON" "$REPO/scripts/assess_frozen_balanced_margin_forward.py" \
        --stop-registration-glob "$REPO/results/Data/frozen_stop_avoidance_contract_matrix/registrations_20*.csv" \
        --stop-status-glob "$REPO/results/Data/frozen_stop_avoidance_contract_matrix/status_20*.json" \
        --stop-model "$REPO/results/Data/live_stop_avoidance/frozen_stop_avoidance_model.joblib" \
        --runner-registration-glob "$REPO/results/Data/frozen_runner_qualified_stop_contract_matrix/registrations_20*.csv" \
        --runner-status-glob "$REPO/results/Data/frozen_runner_qualified_stop_contract_matrix/status_20*.json" \
        --runner-model "$REPO/results/Data/live_stop_avoidance/frozen_runner_qualified_stop_model.joblib" \
        --leverage-registration-glob "$REPO/results/Data/frozen_margin_leverage_contract_matrix/registrations_20*.csv" \
        --leverage-outcome-glob "$REPO/results/Data/frozen_margin_leverage_contract_matrix/outcomes_20*.csv" \
        --leverage-status-glob "$REPO/results/Data/frozen_margin_leverage_contract_matrix/status_20*.json" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/frozen_balanced_stop_margin_forward" \
        --daily-goal 10000 --forward-sessions 20 --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_frozen_rank_displacement_forward() {
    "$PYTHON" "$REPO/scripts/assess_frozen_rank_displacement_forward.py" \
        --heartbeat-glob "$REPO/results/Data/frozen_stop_displacement_paper/heartbeats_20*.csv" \
        --trade-glob "$REPO/results/Data/frozen_stop_displacement_paper/trades_20*.csv" \
        --output-dir "$REPO/results/Data/frozen_rank_displacement_forward" \
        --daily-goal 10000 \
        >>"$LOG" 2>&1 || true
}

run_fixed_rank_first_forward() {
    "$PYTHON" "$REPO/scripts/assess_fixed_rank_first_forward.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/fixed_rank_first_forward" \
        --capital 500000 --daily-goal 10000 \
        --max-trades 10 --forward-sessions 20 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_full_rank_entry_timing() {
    "$PYTHON" "$REPO/scripts/assess_full_rank_entry_timing.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_entry_timing/outcomes_20*.csv" \
        --nonfill-glob "$REPO/results/Data/full_universe_rank_entry_timing/nonfills_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_entry_strategy" \
        --capital 500000 --daily-goal 10000 \
        --max-trades 10 --selection-sessions 12 --forward-sessions 20 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_full_rank_feature_gates() {
    "$PYTHON" "$REPO/scripts/assess_full_rank_feature_gates.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_paper/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_feature_strategy" \
        --capital 500000 --daily-goal 10000 \
        --max-trades 10 --selection-sessions 12 --forward-sessions 20 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_full_rank_capital_allocation() {
    "$PYTHON" "$REPO/scripts/assess_full_rank_capital_allocation.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_capital_allocation" \
        --capital 500000 --daily-goal 10000 \
        --max-trades 10 --selection-sessions 12 --forward-sessions 20 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_full_rank_integrated_policies() {
    "$PYTHON" "$REPO/scripts/assess_full_rank_integrated_policies.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_integrated_policies" \
        --capital 500000 --daily-goal 10000 \
        --max-trades 10 --selection-sessions 12 --forward-sessions 20 \
        --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_full_rank_opportunity_ceiling() {
    "$PYTHON" "$REPO/scripts/assess_full_rank_opportunity_ceiling.py" \
        --entry-glob "$REPO/results/Data/full_universe_rank_paper/entries_20*.csv" \
        --outcome-glob "$REPO/results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv" \
        --raw-glob "$REPO/results/Data/live_feature_recorder/live_features_20*.csv" \
        --output-dir "$REPO/results/Data/full_universe_rank_opportunity_ceiling" \
        --capital 500000 --daily-goal 10000 --max-trades 10 --min-snapshots 1000 \
        >>"$LOG" 2>&1 || true
}

run_intraday_capital_growth() {
    "$PYTHON" "$REPO/scripts/project_intraday_capital_growth.py" \
        --output-dir "$REPO/results/Data/intraday_capital_growth" \
        --initial-capital 500000 --monthly-contribution 200000 \
        --daily-goal 10000 --months 12 --trading-days 20 \
        --minimum-forward-sessions 20 \
        >>"$LOG" 2>&1 || true
}

run_pullback_reclaim_audit() {
    "$PYTHON" "$REPO/scripts/assess_leader_pullback_reclaim.py" \
        --output-dir "$REPO/results/Data/leader_pullback_reclaim_audit" \
        --capital 500000 \
        --daily-goal 10000 \
        >>"$LOG" 2>&1 || true
}

run_strategy_evidence_scoreboard() {
    "$PYTHON" "$REPO/scripts/build_intraday_strategy_evidence_scoreboard.py" \
        --data-dir "$REPO/results/Data" \
        --output-dir "$REPO/results/Data/intraday_strategy_evidence_scoreboard" \
        >>"$LOG" 2>&1 || true
}

run_pipeline_readiness() {
    "$PYTHON" "$REPO/scripts/check_intraday_evidence_pipeline.py" --allow-issues \
        >>"$LOG" 2>&1 || true
}

run_eod_integrity() {
    "$PYTHON" "$REPO/scripts/check_intraday_eod_evidence.py" \
        --date "$DATE" --allow-issues >>"$LOG" 2>&1 || true
}

send_eod_integrity_summary() {
    local integrity="$REPO/results/Data/intraday_evidence_pipeline/eod_latest.json"
    if [ ! -s "$integrity" ]; then
        send_discord "Intraday EOD evidence needs attention for $DATE | integrity manifest missing | no orders enabled"
        return
    fi
    local report
    report=$("$PYTHON" -c '
import json, sys
s = json.load(open(sys.argv[1]))
print(
    f"status={s.get('"'"'status'"'"', '"'"'missing'"'"')} "
    f"artifacts={s.get('"'"'artifacts_refreshed'"'"', 0)}/{s.get('"'"'artifacts_required'"'"', 0)} "
    f"session_eligible={s.get('"'"'session_evidence_eligible'"'"', False)} "
    f"issues={len(s.get('"'"'issues'"'"', []))}"
)
' "$integrity" 2>/dev/null || echo "status=unreadable")
    if [[ "$report" == status=complete_* ]]; then
        send_discord "Intraday EOD evidence processed for $DATE | $report | research only, no broker orders"
    else
        send_discord "Intraday EOD evidence needs attention for $DATE | $report | see $LOG"
    fi
}

if [ -f "$OUTPUT_ROOT/status.json" ]; then
    existing_status=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", ""))' "$OUTPUT_ROOT/status.json" 2>/dev/null || true)
    if [[ "$existing_status" == complete* ]]; then
        run_readiness
        run_ranked_replay
        run_live_microstructure_research
        run_event_shadow_rebuild
        run_rank_migration_audit
        run_stop_avoidance_audit
        run_historical_rank_displacement_audit
        run_prior_session_ranker_paper
        run_frozen_ranker_prediction_matrix
        run_full_universe_rank_strategy
        run_exact_full_rank_strategy
        run_full_rank_contract_matrix
        run_depth_executable_rank_strategy
        run_predeclared_capacity_challenger
        run_frozen_rank_migration_depth_forward
        run_frozen_stop_avoidance_depth_forward
        run_frozen_runner_qualified_stop_depth_forward
        run_frozen_balanced_stop_portfolio_forward
        run_frozen_balanced_margin_forward
        run_frozen_rank_displacement_forward
        run_fixed_rank_first_forward
        run_full_rank_entry_timing
        run_full_rank_feature_gates
        run_full_rank_capital_allocation
        run_full_rank_integrated_policies
        run_full_rank_opportunity_ceiling
        run_intraday_capital_growth
        run_pullback_reclaim_audit
        run_strategy_evidence_scoreboard
        run_eod_integrity
        run_pipeline_readiness
        send_eod_integrity_summary
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
coverage = s.get("promotion_coverage_pct", s.get("combined_coverage_pct", 0))
print(
    f"status={status} symbols={symbols} "
    f"feature={feature_evaluated}/{feature_rows} "
    f"decisions={decision_evaluated}/{decision_rows} candidate_coverage={coverage}%"
)
' "$STATUS_FILE" 2>/dev/null || echo "status=unreadable")
else
    SUMMARY="status file missing"
fi

run_readiness
run_ranked_replay
run_live_microstructure_research
run_event_shadow_rebuild
run_rank_migration_audit
run_stop_avoidance_audit
run_historical_rank_displacement_audit
run_prior_session_ranker_paper
run_frozen_ranker_prediction_matrix
run_full_universe_rank_strategy
run_exact_full_rank_strategy
run_full_rank_contract_matrix
run_depth_executable_rank_strategy
run_predeclared_capacity_challenger
run_frozen_rank_migration_depth_forward
run_frozen_stop_avoidance_depth_forward
run_frozen_runner_qualified_stop_depth_forward
run_frozen_balanced_stop_portfolio_forward
run_frozen_balanced_margin_forward
run_frozen_rank_displacement_forward
run_fixed_rank_first_forward
run_full_rank_entry_timing
run_full_rank_feature_gates
run_full_rank_capital_allocation
run_full_rank_integrated_policies
run_full_rank_opportunity_ceiling
run_intraday_capital_growth
run_pullback_reclaim_audit
run_strategy_evidence_scoreboard
run_eod_integrity
run_pipeline_readiness
send_eod_integrity_summary
echo "[$(date -u +'%Y-%m-%d %H:%M:%S UTC')] live evidence EOD finished rc=$RC $SUMMARY" >>"$LOG"
exit "$RC"

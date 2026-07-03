#!/usr/bin/env bash
# Starts the live trade assistant.
# Reads config from .env.dev in the repo root.
# Run manually: bash scripts/start_live_assistant.sh
# Or via systemd (see trade-assistant.service).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Load env
if [[ -f .env.dev ]]; then
    set -a
    source .env.dev
    set +a
fi

# Pull latest code from GitHub so every morning's session uses the newest fixes.
# --ff-only: only fast-forward (never merge); fails silently if network is down.
echo "Pulling latest code from GitHub..."
git -C "$REPO_ROOT" pull --ff-only origin main 2>&1 || echo "git pull skipped (network/conflict) — using existing code"

PYTHON="$REPO_ROOT/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: venv not found at $REPO_ROOT/.venv — run: python3 -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

# Ensure indices exist
bash "$REPO_ROOT/scripts/ensure_required_indices.sh" || true

# Live-speed tuning: keep the alert path lean during market hours.
export SIGNALMONITOR_FAST_MODE=1
export SIGNALMONITOR_MAX_CHANGED_PER_CYCLE="${SIGNALMONITOR_MAX_CHANGED_PER_CYCLE:-40}"
export SIGNALMONITOR_STREAM_BOOTSTRAP_MAX_SYMBOLS="${SIGNALMONITOR_STREAM_BOOTSTRAP_MAX_SYMBOLS:-40}"
export SIGNALMONITOR_REPEAT_BUY_ALERT_SECONDS="${SIGNALMONITOR_REPEAT_BUY_ALERT_SECONDS:-60}"
export SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY="${SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY:-50}"
export SIGNALMONITOR_GOAL_TARGET1_PCT="${SIGNALMONITOR_GOAL_TARGET1_PCT:-1.5}"
export SIGNALMONITOR_GOAL_TARGET2_PCT="${SIGNALMONITOR_GOAL_TARGET2_PCT:-3.0}"
export SIGNALMONITOR_GOAL_ANOMALY_ONLY="${SIGNALMONITOR_GOAL_ANOMALY_ONLY:-1}"
export SIGNALMONITOR_GOAL_ALLOW_DIRECT_BUY="${SIGNALMONITOR_GOAL_ALLOW_DIRECT_BUY:-0}"
export SIGNALMONITOR_ALERT_SOURCE="${SIGNALMONITOR_ALERT_SOURCE:-intraday-goal-live}"
export KITE_ASSISTANT_INTERVAL="${KITE_ASSISTANT_INTERVAL:-3}"
export ENABLE_GRADE_B_AUTOTRADE="${ENABLE_GRADE_B_AUTOTRADE:-1}"

if [[ "$ENABLE_GRADE_B_AUTOTRADE" == "1" ]]; then
    export SIGNALMONITOR_AUTOTRADE_ENABLED="${SIGNALMONITOR_AUTOTRADE_ENABLED:-1}"
    export SIGNALMONITOR_AUTOTRADE_REAL_ORDERS="${SIGNALMONITOR_AUTOTRADE_REAL_ORDERS:-1}"
    export SIGNALMONITOR_AUTOTRADE_STRATEGY="${SIGNALMONITOR_AUTOTRADE_STRATEGY:-grade_b_confirmation}"
    export SIGNALMONITOR_AUTOTRADE_CONFIRM_PCT="${SIGNALMONITOR_AUTOTRADE_CONFIRM_PCT:-0.25}"
    export SIGNALMONITOR_AUTOTRADE_TARGET_PCT="${SIGNALMONITOR_AUTOTRADE_TARGET_PCT:-0.7}"
    export SIGNALMONITOR_AUTOTRADE_STOP_PCT="${SIGNALMONITOR_AUTOTRADE_STOP_PCT:-2.5}"
    export SIGNALMONITOR_AUTOTRADE_ENTRY_WINDOW_MINUTES="${SIGNALMONITOR_AUTOTRADE_ENTRY_WINDOW_MINUTES:-15}"
    export SIGNALMONITOR_AUTOTRADE_MAX_ENTRY_SLIPPAGE_PCT="${SIGNALMONITOR_AUTOTRADE_MAX_ENTRY_SLIPPAGE_PCT:-0.4}"
    export SIGNALMONITOR_AUTOTRADE_LIMIT_BUFFER_PCT="${SIGNALMONITOR_AUTOTRADE_LIMIT_BUFFER_PCT:-0.10}"

    export KITE_AUTO_TRADE=1
    export KITE_AUTO_BUY=1
    export KITE_AUTO_SELL=1
    export KITE_PRODUCT_TYPE="${AUTOTRADE_KITE_PRODUCT_TYPE:-MIS}"
    export KITE_INTRADAY_CAPITAL="${AUTOTRADE_KITE_INTRADAY_CAPITAL:-400000}"
    export KITE_INTRADAY_MAX_ORDER_VALUE="${AUTOTRADE_KITE_INTRADAY_MAX_ORDER_VALUE:-50000}"
    export KITE_INTRADAY_MAX_OPEN_POSITIONS="${AUTOTRADE_KITE_INTRADAY_MAX_OPEN_POSITIONS:-2}"
    export KITE_MAX_DAILY_ORDERS="${AUTOTRADE_KITE_MAX_DAILY_ORDERS:-10}"
    export KITE_ORDER_TYPE="${AUTOTRADE_KITE_ORDER_TYPE:-LIMIT}"
    export KITE_CANCEL_UNFILLED_LIMIT="${AUTOTRADE_KITE_CANCEL_UNFILLED_LIMIT:-1}"
    export KITE_ORDER_FILL_WAIT_SEC="${AUTOTRADE_KITE_ORDER_FILL_WAIT_SEC:-8}"
    export KITE_INTRADAY_PROTECTIVE_STOP="${AUTOTRADE_KITE_INTRADAY_PROTECTIVE_STOP:-1}"
    export KITE_INTRADAY_PROTECTIVE_STOP_ORDER_TYPE="${AUTOTRADE_KITE_INTRADAY_PROTECTIVE_STOP_ORDER_TYPE:-SL-M}"
    export ASSISTANT_MAX_DAILY_LOSS="${AUTOTRADE_MAX_DAILY_LOSS:-3000}"
    export ASSISTANT_MAX_CONSECUTIVE_LOSSES="${AUTOTRADE_MAX_CONSECUTIVE_LOSSES:-2}"
else
    export SIGNALMONITOR_AUTOTRADE_ENABLED=0
    export SIGNALMONITOR_AUTOTRADE_REAL_ORDERS=0
fi

echo "Starting trade assistant..."
echo "  Universe : all (NSE + BSE combined, ~2990 stocks)"
echo "  Liquidity filter: min avg daily traded value ₹5 crore"
echo "  Volume filter: min 2x average volume on signal day"
echo "  Early quality mode: Buy Now + Strong Buy Now"
echo "  Scan interval: ${KITE_ASSISTANT_INTERVAL}s"
echo "  Fast mode: on (lean live monitor, changed symbols cap ${SIGNALMONITOR_MAX_CHANGED_PER_CYCLE})"
echo "  Day-goal alerts: max ${SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY}; Target 1 ${SIGNALMONITOR_GOAL_TARGET1_PCT}%; Target 2 ${SIGNALMONITOR_GOAL_TARGET2_PCT}%"
echo "  Day-goal lane: Grade-A/B anomaly only (direct Buy Now disabled)"
echo "  Alert source label: ${SIGNALMONITOR_ALERT_SOURCE}"
if [[ "$ENABLE_GRADE_B_AUTOTRADE" == "1" ]]; then
    echo "  Mode: controlled real-order automation"
    echo "  Auto-trade lane: ${SIGNALMONITOR_AUTOTRADE_STRATEGY}; confirm +${SIGNALMONITOR_AUTOTRADE_CONFIRM_PCT}%; target +${SIGNALMONITOR_AUTOTRADE_TARGET_PCT}%; stop -${SIGNALMONITOR_AUTOTRADE_STOP_PCT}%"
    echo "  Kite product: ${KITE_PRODUCT_TYPE}; per-order max: ₹${KITE_INTRADAY_MAX_ORDER_VALUE}; max open: ${KITE_INTRADAY_MAX_OPEN_POSITIONS}; daily buy cap: ${KITE_MAX_DAILY_ORDERS}"
    echo "  Loss guardrails: daily ₹${ASSISTANT_MAX_DAILY_LOSS}; consecutive losses ${ASSISTANT_MAX_CONSECUTIVE_LOSSES}"
else
    echo "  Mode: alerts only (manual buys in Groww / broker app)"
    echo "  Kite auto-trade: disabled from this launcher"
fi
echo "  Swing capital: ₹${KITE_SWING_CAPITAL:-0}  per-order max: ₹${KITE_SWING_MAX_ORDER_VALUE:-0}"
echo ""

exec "$PYTHON" -m pkscreener.pkscreenercli \
    --assistantstart all \
    --assistantforeground \
    --assistantbudget "${KITE_SWING_MAX_ORDER_VALUE:-10000}" \
    --assistantworkers "${KITE_ASSISTANT_WORKERS:-24}" \
    --assistantinterval "${KITE_ASSISTANT_INTERVAL}" \
    --assistantalerts discord \
    --assistantminliquidity 50000000 \
    --assistantminvolratio 2.0

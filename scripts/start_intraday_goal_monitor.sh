#!/usr/bin/env bash
# Start validated intraday-goal Discord alerts for manual real-money trading.
#
# This launcher does not place broker orders. It runs SignalMonitor with live Kite
# data and sends Discord alerts; the human must place/skip orders and manage exits.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env.dev ]]; then
    set -a
    source .env.dev
    set +a
fi

export KITE_EXECUTION_MODE=paper
export KITE_DRY_RUN=1
export KITE_LIVE_ACKNOWLEDGEMENT=""
export KITE_LIVE_CALLER_ALLOWLIST=""
export KITE_LIVE_STRATEGY_ALLOWLIST=""
export SIGNALMONITOR_AUTOTRADE_ENABLED=0
export SIGNALMONITOR_AUTOTRADE_REAL_ORDERS=0
export KITE_AUTO_TRADE=0
export KITE_AUTO_BUY=0
export KITE_AUTO_SELL=0

PYTHON="$REPO_ROOT/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: venv not found at $REPO_ROOT/.venv"
    exit 1
fi

ACCEPTED_SELECTOR="$REPO_ROOT/results/Data/intraday_goal_selector_n500_mfe1_liquidity_replacement_vwap70_early_failure_validation.csv"
if [[ ! -s "$ACCEPTED_SELECTOR" ]]; then
    echo "ERROR: accepted selector report not found: $ACCEPTED_SELECTOR"
    exit 1
fi

export SIGNALMONITOR_FAST_MODE="${SIGNALMONITOR_FAST_MODE:-1}"
export SIGNALMONITOR_INTRADAY_GOAL_MODE="${SIGNALMONITOR_INTRADAY_GOAL_MODE:-1}"
export SIGNALMONITOR_GOAL_SELECTOR_FILE="${SIGNALMONITOR_GOAL_SELECTOR_FILE:-$ACCEPTED_SELECTOR}"
export SIGNALMONITOR_GOAL_DAILY_CAPITAL="${SIGNALMONITOR_GOAL_DAILY_CAPITAL:-200000}"
export SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY="${SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY:-20}"
export SIGNALMONITOR_GOAL_STOP_PCT="${SIGNALMONITOR_GOAL_STOP_PCT:-1.0}"
export SIGNALMONITOR_GOAL_TARGET1_PCT="${SIGNALMONITOR_GOAL_TARGET1_PCT:-1.5}"
export SIGNALMONITOR_GOAL_TARGET2_PCT="${SIGNALMONITOR_GOAL_TARGET2_PCT:-3.0}"
export SIGNALMONITOR_MAX_CHANGED_PER_CYCLE="${SIGNALMONITOR_MAX_CHANGED_PER_CYCLE:-40}"
export SIGNALMONITOR_STREAM_BOOTSTRAP_MAX_SYMBOLS="${SIGNALMONITOR_STREAM_BOOTSTRAP_MAX_SYMBOLS:-40}"
export SIGNALMONITOR_REPEAT_BUY_ALERT_SECONDS="${SIGNALMONITOR_REPEAT_BUY_ALERT_SECONDS:-120}"
export SIGNALMONITOR_ALERT_SOURCE="${SIGNALMONITOR_ALERT_SOURCE:-intraday-goal-live}"

REAL_BUDGET="${INTRADAY_REAL_BUDGET:-20000}"
MAX_DAILY_LOSS="${INTRADAY_REAL_MAX_DAILY_LOSS:-1000}"
MAX_CONSECUTIVE_LOSSES="${INTRADAY_REAL_MAX_CONSECUTIVE_LOSSES:-1}"
WORKERS="${INTRADAY_REAL_WORKERS:-24}"
INTERVAL="${INTRADAY_REAL_INTERVAL:-10}"

echo "Starting validated intraday-goal monitor..."
echo "  Mode: Discord alerts only; manual broker order entry"
echo "  Selector: $SIGNALMONITOR_GOAL_SELECTOR_FILE"
echo "  Alert source: $SIGNALMONITOR_ALERT_SOURCE"
echo "  Starter order budget: Rs $REAL_BUDGET"
echo "  Daily loss guardrail: Rs $MAX_DAILY_LOSS"
echo "  Max consecutive losses: $MAX_CONSECUTIVE_LOSSES"
echo "  Goal slots/day: $SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY"
echo "  Manual early-failure rule from research: exit if first 4 bars fall 0.14% before +0.15% favorable move"
echo ""

echo "Checking intraday-goal readiness..."
"$PYTHON" scripts/check_intraday_goal_readiness.py --enforce-time
echo ""

if [[ "${SKIP_DISCORD_PREFLIGHT:-0}" != "1" ]]; then
    echo "Checking Discord webhook before starting..."
    "$PYTHON" scripts/test_discord_webhook.py --message "Intraday goal monitor starting: Discord alerts are reachable."
    echo ""
fi

exec "$PYTHON" -m pkscreener.pkscreenercli \
    --signalmonitor all \
    --signalmonitorlivekite \
    --signalmonitoralerts discord \
    --signalmonitorbudget "$REAL_BUDGET" \
    --signalmonitorriskpct 1.0 \
    --signalmonitorminconfidence 15.0 \
    --signalmonitorminliquidity 50000000 \
    --signalmonitorminvolratio 2.0 \
    --signalmonitorworkers "$WORKERS" \
    --signalmonitorlimit 0 \
    --signalmonitoraccount main \
    --signalmonitormaxdailyloss "$MAX_DAILY_LOSS" \
    --signalmonitormaxconsecutivelosses "$MAX_CONSECUTIVE_LOSSES" \
    -c "$INTERVAL" \
    -a Y

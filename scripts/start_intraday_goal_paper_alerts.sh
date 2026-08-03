#!/usr/bin/env bash
# Start validated intraday-goal Discord alerts with paper tracking enabled.
#
# This launcher does not place broker orders. It sends the same live Discord
# alerts as the real-money starter and records paper entries for validation.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

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

export SIGNALMONITOR_ENABLE_PAPER_TRACKING=1
export SIGNALMONITOR_ALERT_SOURCE="${SIGNALMONITOR_ALERT_SOURCE:-intraday-goal-paper}"
export INTRADAY_REAL_BUDGET="${INTRADAY_PAPER_BUDGET:-200000}"
export INTRADAY_REAL_MAX_DAILY_LOSS="${INTRADAY_PAPER_MAX_DAILY_LOSS:-2000}"
export SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY="${SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY:-20}"

echo "Starting intraday-goal PAPER alerts..."
echo "  Broker orders: disabled by design; this uses SignalMonitor alerts + paper ledger"
echo "  Alert source: $SIGNALMONITOR_ALERT_SOURCE"
echo "  Paper capital used for sizing: Rs $INTRADAY_REAL_BUDGET"
echo ""

exec bash scripts/start_intraday_goal_monitor.sh

#!/usr/bin/env bash
# Start the Grade-B confirmation paper-evidence lane.
#
# This does not enable the old assistant autopilot. It runs the normal intraday
# goal monitor with Grade-B observations retained for paper evidence only.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export SIGNALMONITOR_AUTOTRADE_ENABLED=0
export SIGNALMONITOR_AUTOTRADE_OBSERVATION_ENABLED=1
export SIGNALMONITOR_AUTOTRADE_REAL_ORDERS=0
export SIGNALMONITOR_AUTOTRADE_STRATEGY="${SIGNALMONITOR_AUTOTRADE_STRATEGY:-grade_b_confirmation}"
export SIGNALMONITOR_AUTOTRADE_CONFIRM_PCT="${SIGNALMONITOR_AUTOTRADE_CONFIRM_PCT:-0.25}"
export SIGNALMONITOR_AUTOTRADE_TARGET_PCT="${SIGNALMONITOR_AUTOTRADE_TARGET_PCT:-0.7}"
export SIGNALMONITOR_AUTOTRADE_STOP_PCT="${SIGNALMONITOR_AUTOTRADE_STOP_PCT:-2.5}"
export SIGNALMONITOR_AUTOTRADE_ENTRY_WINDOW_MINUTES="${SIGNALMONITOR_AUTOTRADE_ENTRY_WINDOW_MINUTES:-15}"
export SIGNALMONITOR_AUTOTRADE_MAX_ENTRY_SLIPPAGE_PCT="${SIGNALMONITOR_AUTOTRADE_MAX_ENTRY_SLIPPAGE_PCT:-0.4}"

export KITE_EXECUTION_MODE=paper
export KITE_DRY_RUN=1
export KITE_LIVE_ACKNOWLEDGEMENT=""
export KITE_LIVE_CALLER_ALLOWLIST=""
export KITE_LIVE_STRATEGY_ALLOWLIST=""
export KITE_AUTO_TRADE=0
export KITE_AUTO_BUY=0
export KITE_AUTO_SELL=0
export KITE_PRODUCT_TYPE="${KITE_PRODUCT_TYPE:-MIS}"
export KITE_INTRADAY_CAPITAL="${KITE_INTRADAY_CAPITAL:-200000}"
export KITE_INTRADAY_MAX_ORDER_VALUE="${KITE_INTRADAY_MAX_ORDER_VALUE:-50000}"
export KITE_INTRADAY_MAX_OPEN_POSITIONS="${KITE_INTRADAY_MAX_OPEN_POSITIONS:-1}"
export KITE_MAX_DAILY_ORDERS="${KITE_MAX_DAILY_ORDERS:-3}"
export KITE_ORDER_TYPE="${KITE_ORDER_TYPE:-LIMIT}"
export KITE_CANCEL_UNFILLED_LIMIT="${KITE_CANCEL_UNFILLED_LIMIT:-1}"

echo "Starting Grade-B confirmation paper-evidence lane..."
echo "  Broker mutations: disabled; this strategy is not approved for live orders"
echo "  Entry: Grade-B alert +${SIGNALMONITOR_AUTOTRADE_CONFIRM_PCT}% confirmation"
echo "  Target: +${SIGNALMONITOR_AUTOTRADE_TARGET_PCT}% from executed entry"
echo "  Stop: -${SIGNALMONITOR_AUTOTRADE_STOP_PCT}% from executed entry"
echo "  Max entry chase: +${SIGNALMONITOR_AUTOTRADE_MAX_ENTRY_SLIPPAGE_PCT}% beyond trigger"
echo "  Kite product: ${KITE_PRODUCT_TYPE}"
echo "  Per-order cap: Rs ${KITE_INTRADAY_MAX_ORDER_VALUE}"
echo "  Max open positions: ${KITE_INTRADAY_MAX_OPEN_POSITIONS}"
echo "  Max daily orders: ${KITE_MAX_DAILY_ORDERS}"
echo ""

exec bash scripts/start_intraday_goal_monitor.sh

#!/usr/bin/env bash
# Starts the live trade assistant in alerts-only manual-buy mode.
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
timeout 20s git -C "$REPO_ROOT" -c core.sshCommand="ssh -o BatchMode=yes -o ConnectTimeout=10" pull --ff-only origin main 2>&1 || echo "git pull skipped (timeout/network/conflict) — using existing code"

PYTHON="$REPO_ROOT/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: venv not found at $REPO_ROOT/.venv — run: python3 -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

echo "Checking Kite session before live start..."
"$PYTHON" - <<'PY'
import sys

from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager

try:
    report = KiteConnectSessionManager().ensure_fresh_session()
except Exception as exc:
    print(f"ERROR: Kite session preflight failed: {exc}")
    print("ACTION: refresh Kite login manually with --kiteconnectloginurl and --kiteconnectsession, then restart trade-assistant.service.")
    sys.exit(1)

if report.get("error"):
    print(f"ERROR: Kite session preflight failed: {report['error']}")
    print("ACTION: refresh Kite login manually with --kiteconnectloginurl and --kiteconnectsession, then restart trade-assistant.service.")
    sys.exit(1)

print("Kite session preflight OK.")
PY

# Ensure indices exist
bash "$REPO_ROOT/scripts/ensure_required_indices.sh" || true

# Live-speed tuning: keep the alert path lean during market hours.
export SIGNALMONITOR_FAST_MODE=1
export SIGNALMONITOR_MAX_CHANGED_PER_CYCLE="${SIGNALMONITOR_MAX_CHANGED_PER_CYCLE:-40}"
export SIGNALMONITOR_STREAM_BOOTSTRAP_MAX_SYMBOLS="${SIGNALMONITOR_STREAM_BOOTSTRAP_MAX_SYMBOLS:-40}"
export SIGNALMONITOR_REPEAT_BUY_ALERT_SECONDS="${SIGNALMONITOR_REPEAT_BUY_ALERT_SECONDS:-60}"
export SIGNALMONITOR_ENABLE_ANOMALY="${SIGNALMONITOR_ENABLE_ANOMALY:-1}"
export SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY="${SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY:-50}"
export SIGNALMONITOR_GOAL_TARGET1_PCT="${SIGNALMONITOR_GOAL_TARGET1_PCT:-1.5}"
export SIGNALMONITOR_GOAL_TARGET2_PCT="${SIGNALMONITOR_GOAL_TARGET2_PCT:-3.0}"
# Real-money Discord lane: keep this forced to the vetted anomaly basket.
export SIGNALMONITOR_GOAL_ANOMALY_ONLY=1
export SIGNALMONITOR_GOAL_ALLOW_GRADE_C="${SIGNALMONITOR_GOAL_ALLOW_GRADE_C:-0}"
export SIGNALMONITOR_GOAL_ALLOW_DIRECT_BUY=0
export SIGNALMONITOR_HEARTBEAT_MINUTES="${SIGNALMONITOR_HEARTBEAT_MINUTES:-60}"
export SIGNALMONITOR_RADAR_MAX_ALERTS_PER_DAY="${SIGNALMONITOR_RADAR_MAX_ALERTS_PER_DAY:-15}"
export SIGNALMONITOR_RADAR_MIN_STRENGTH="${SIGNALMONITOR_RADAR_MIN_STRENGTH:-32}"
export SIGNALMONITOR_TOP_GAINER_RADAR_MIN_PCT="${SIGNALMONITOR_TOP_GAINER_RADAR_MIN_PCT:-1.2}"
export SIGNALMONITOR_TOP_GAINER_RADAR_MAX_PCT="${SIGNALMONITOR_TOP_GAINER_RADAR_MAX_PCT:-12.0}"
export SIGNALMONITOR_TOP_GAINER_RADAR_EARLY_MAX_PCT="${SIGNALMONITOR_TOP_GAINER_RADAR_EARLY_MAX_PCT:-3.5}"
export SIGNALMONITOR_TOP_GAINER_RADAR_HOT_MAX_PULLBACK_PCT="${SIGNALMONITOR_TOP_GAINER_RADAR_HOT_MAX_PULLBACK_PCT:-1.8}"
export SIGNALMONITOR_TOP_GAINER_RADAR_MAX_PER_CYCLE="${SIGNALMONITOR_TOP_GAINER_RADAR_MAX_PER_CYCLE:-3}"
export SIGNALMONITOR_TOP_GAINER_VISIBILITY_ENABLED="${SIGNALMONITOR_TOP_GAINER_VISIBILITY_ENABLED:-1}"
export SIGNALMONITOR_TOP_GAINER_VISIBILITY_MINUTES="${SIGNALMONITOR_TOP_GAINER_VISIBILITY_MINUTES:-15}"
export SIGNALMONITOR_TOP_GAINER_VISIBILITY_MIN_PCT="${SIGNALMONITOR_TOP_GAINER_VISIBILITY_MIN_PCT:-1.0}"
export SIGNALMONITOR_TOP_GAINER_VISIBILITY_MAX_LINES="${SIGNALMONITOR_TOP_GAINER_VISIBILITY_MAX_LINES:-10}"
export SIGNALMONITOR_ENGINE_WATCH_ENABLED=0
export SIGNALMONITOR_ENGINE_WATCH_MAX_PER_DAY="${SIGNALMONITOR_ENGINE_WATCH_MAX_PER_DAY:-8}"
export SIGNALMONITOR_GOAL_LAST_ENTRY_TIME="${SIGNALMONITOR_GOAL_LAST_ENTRY_TIME:-14:30}"
export SIGNALMONITOR_ALERT_SOURCE="${SIGNALMONITOR_ALERT_SOURCE:-intraday-goal-live}"
export KITE_ASSISTANT_INTERVAL="${KITE_ASSISTANT_INTERVAL:-3}"

echo "Starting trade assistant..."
echo "  Universe : all (NSE + BSE combined, ~2990 stocks)"
echo "  Liquidity filter: min avg daily traded value ₹5 crore"
echo "  Volume filter: min 2x average volume on signal day"
echo "  Early quality mode: Buy Now + Strong Buy Now"
echo "  Scan interval: ${KITE_ASSISTANT_INTERVAL}s"
echo "  Fast mode: on (lean live monitor, changed symbols cap ${SIGNALMONITOR_MAX_CHANGED_PER_CYCLE})"
echo "  Day-goal alerts: max ${SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY}; Target 1 ${SIGNALMONITOR_GOAL_TARGET1_PCT}%; Target 2 ${SIGNALMONITOR_GOAL_TARGET2_PCT}%"
echo "  Day-goal lane: hold-confirmed Grade-A/B anomaly only; Grade-C watch disabled by default"
echo "  Top-gainer watch: ${SIGNALMONITOR_RADAR_MAX_ALERTS_PER_DAY}/day, ${SIGNALMONITOR_TOP_GAINER_RADAR_MIN_PCT}% to ${SIGNALMONITOR_TOP_GAINER_RADAR_MAX_PCT}% live movers, ${SIGNALMONITOR_TOP_GAINER_RADAR_MAX_PER_CYCLE}/cycle"
echo "  Top-gainer visibility: every ${SIGNALMONITOR_TOP_GAINER_VISIBILITY_MINUTES} min, top ${SIGNALMONITOR_TOP_GAINER_VISIBILITY_MAX_LINES} movers above ${SIGNALMONITOR_TOP_GAINER_VISIBILITY_MIN_PCT}% (not buy alerts)"
echo "  Engine watch lane: disabled in live Discord by default"
echo "  Last fresh intraday entry: ${SIGNALMONITOR_GOAL_LAST_ENTRY_TIME} IST"
echo "  Heartbeat: every ${SIGNALMONITOR_HEARTBEAT_MINUTES} min (alerts still immediate)"
echo "  Alert source label: ${SIGNALMONITOR_ALERT_SOURCE}"
echo "  Mode: alerts only (manual buys in Groww / broker app)"
echo "  Kite auto-trade: disabled from this launcher"
echo "  Swing capital: ₹${KITE_SWING_CAPITAL:-0}  per-order max: ₹${KITE_SWING_MAX_ORDER_VALUE:-0}"
echo ""

exec "$PYTHON" -m pkscreener.pkscreenercli \
    --assistantstart all \
    --assistantforeground \
    --assistantautopilot \
    --assistantbudget "${KITE_SWING_MAX_ORDER_VALUE:-10000}" \
    --assistantworkers "${KITE_ASSISTANT_WORKERS:-24}" \
    --assistantinterval "${KITE_ASSISTANT_INTERVAL}" \
    --assistantalerts discord \
    --assistantminliquidity 50000000 \
    --assistantminvolratio 2.0

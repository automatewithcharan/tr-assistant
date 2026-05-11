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

echo "Starting trade assistant..."
echo "  Universe : all (NSE + BSE combined, ~2990 stocks)"
echo "  Liquidity filter: min avg daily traded value ₹5 crore"
echo "  Volume filter: min 2x average volume on signal day"
echo "  Early quality mode: Buy Now + Strong Buy Now"
echo "  Scan interval: 10s"
echo "  Fast mode: on (lean live monitor, changed symbols cap ${SIGNALMONITOR_MAX_CHANGED_PER_CYCLE})"
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
    --assistantinterval 10 \
    --assistantalerts discord \
    --assistantminliquidity 50000000 \
    --assistantminvolratio 2.0

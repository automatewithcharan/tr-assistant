#!/usr/bin/env bash
# Cron wrapper for high_value_alert_notifier.py — ensures correct working dir + env.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[ -f .env.dev ] && set -a && . .env.dev && set +a
exec "$REPO/.venv/bin/python3" "$REPO/scripts/high_value_alert_notifier.py"

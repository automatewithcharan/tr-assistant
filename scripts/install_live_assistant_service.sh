#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  cat <<'EOF'
Usage:
  sudo bash scripts/install_live_assistant_service.sh <linux-user> <repo-root> [universe] [budget] [interval]

Example:
  sudo bash scripts/install_live_assistant_service.sh charan_sai /home/charan_sai/tr-assistant nifty500 3000 30
EOF
  exit 1
fi

APP_USER="$1"
REPO_ROOT="$2"
UNIVERSE="${3:-nifty500}"
BUDGET="${4:-3000}"
INTERVAL="${5:-30}"

UNIT_PATH="/etc/systemd/system/trade-assistant.service"
ENV_PATH="/etc/default/trade-assistant"

cat > "$ENV_PATH" <<EOF
APP_USER=${APP_USER}
REPO_ROOT=${REPO_ROOT}
UNIVERSE=${UNIVERSE}
BUDGET=${BUDGET}
INTERVAL=${INTERVAL}
EOF

cat > "$UNIT_PATH" <<'EOF'
[Unit]
Description=Trade Assistant Live Supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%i
EnvironmentFile=/etc/default/trade-assistant
ExecStart=/bin/bash -lc 'cd "$REPO_ROOT" && source .venv/bin/activate && exec ./scripts/start_live_assistant.sh'
ExecStop=/bin/bash -lc 'cd "$REPO_ROOT" && source .venv/bin/activate && python3 -m pkscreener.pkscreenercli --assistantstop all || true'
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

# Replace the unsupported %i placeholder with the concrete user now.
sed -i "s|User=%i|User=${APP_USER}|" "$UNIT_PATH"
sed -i "s|User=${APP_USER}|User=${APP_USER}\nWorkingDirectory=${REPO_ROOT}|" "$UNIT_PATH"

systemctl daemon-reload
systemctl enable trade-assistant.service

cat <<EOF
Installed trade-assistant.service

Environment file:
  $ENV_PATH

Systemd unit:
  $UNIT_PATH

Start:
  sudo systemctl start trade-assistant

Status:
  sudo systemctl status trade-assistant

Stop:
  sudo systemctl stop trade-assistant

Logs:
  journalctl -u trade-assistant -f
EOF

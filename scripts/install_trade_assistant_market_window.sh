#!/usr/bin/env bash
set -euo pipefail

START_UTC="${1:-03:15}"
STOP_UTC="${2:-10:50}"

WATCHDOG_TIMES=(
  "03:20:00" "03:25:00" "03:30:00" "03:35:00" "03:40:00" "03:45:00"
  "03:50:00" "03:55:00" "04:00:00" "04:15:00" "04:30:00" "04:45:00"
  "05:00:00" "05:30:00" "06:00:00" "06:30:00" "07:00:00" "07:30:00"
  "08:00:00" "08:30:00" "09:00:00" "09:30:00" "10:00:00" "10:20:00"
  "10:35:00" "10:45:00"
)

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

cat > "$TMP_DIR/trade-assistant-stop.service" <<'EOF'
[Unit]
Description=Stop trade assistant session

[Service]
Type=oneshot
ExecStart=/bin/systemctl stop trade-assistant.service
EOF

cat > "$TMP_DIR/trade-assistant-watchdog.service" <<'EOF'
[Unit]
Description=Trade assistant watchdog
After=network-online.target trade-assistant.service
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/default/trade-assistant
ExecStart=/bin/bash -lc 'cd "$REPO_ROOT" && exec ./scripts/watchdog.sh'
EOF

cat > "$TMP_DIR/trade-assistant.timer" <<EOF
[Unit]
Description=Start trade assistant for pre-market window (8:45 AM IST = ${START_UTC} UTC)

[Timer]
OnCalendar=Mon..Fri ${START_UTC} UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > "$TMP_DIR/trade-assistant-stop.timer" <<EOF
[Unit]
Description=Stop trade assistant after post-market tasks (4:20 PM IST = ${STOP_UTC} UTC)

[Timer]
OnCalendar=Mon..Fri ${STOP_UTC} UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

{
  cat <<'EOF'
[Unit]
Description=Run trade assistant watchdog during pre-market, market, and post-close buffer

[Timer]
EOF
  for t in "${WATCHDOG_TIMES[@]}"; do
    echo "OnCalendar=Mon..Fri *-*-* ${t} UTC"
  done
  cat <<'EOF'
Persistent=false

[Install]
WantedBy=timers.target
EOF
} > "$TMP_DIR/trade-assistant-watchdog.timer"

sudo install -m 0644 "$TMP_DIR/trade-assistant.timer" /etc/systemd/system/trade-assistant.timer
sudo install -m 0644 "$TMP_DIR/trade-assistant-stop.service" /etc/systemd/system/trade-assistant-stop.service
sudo install -m 0644 "$TMP_DIR/trade-assistant-stop.timer" /etc/systemd/system/trade-assistant-stop.timer
sudo install -m 0644 "$TMP_DIR/trade-assistant-watchdog.service" /etc/systemd/system/trade-assistant-watchdog.service
sudo install -m 0644 "$TMP_DIR/trade-assistant-watchdog.timer" /etc/systemd/system/trade-assistant-watchdog.timer

sudo systemctl daemon-reload
sudo systemctl enable trade-assistant.timer trade-assistant-stop.timer trade-assistant-watchdog.timer >/dev/null
sudo systemctl restart trade-assistant.timer trade-assistant-stop.timer trade-assistant-watchdog.timer

echo "Installed updated trade assistant timers:"
echo "  start timer: ${START_UTC} UTC (8:45 AM IST)"
echo "  stop timer:  ${STOP_UTC} UTC (4:20 PM IST)"
echo "  watchdog:    through 10:45 UTC (4:15 PM IST)"

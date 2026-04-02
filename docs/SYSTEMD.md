# Systemd Setup

Use `systemd` on the GCP VM so the assistant keeps running even after you close the SSH browser tab.

## Install

```bash
cd ~/tr-assistant
sudo bash scripts/install_live_assistant_service.sh "$USER" "$HOME/tr-assistant" nifty500 3000 30
```

This creates:

- `/etc/default/trade-assistant`
- `/etc/systemd/system/trade-assistant.service`

## Commands

Start:

```bash
sudo systemctl start trade-assistant
```

Status:

```bash
sudo systemctl status trade-assistant
python3 -m pkscreener.pkscreenercli --assistantstatus
```

Stop:

```bash
sudo systemctl stop trade-assistant
```

Live logs:

```bash
journalctl -u trade-assistant -f
```

## Change Universe / Budget / Interval

Edit:

```bash
sudo nano /etc/default/trade-assistant
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart trade-assistant
```

## Notes

- This uses the existing assistant supervisor flow.
- It survives closing SSH/browser windows because `systemd` owns the process.
- The assistant still writes detailed runtime logs under `results/Data/assistant_logs/`.

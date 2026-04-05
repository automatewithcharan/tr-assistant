# Trade Assistant

Phone-first live trading assistant for NSE built on top of the PKScreener codebase, simplified around:

- Official Kite Connect auth and live ticks
- Strong-buy-only live monitoring
- Discord alerts and Discord trade logging
- SQLite-backed portfolio tracking
- Simple start / status / stop scripts

## What This Repo Keeps

- `pkscreener/` core runtime code
- `scripts/` live assistant launchers
- official Kite Connect integration
- Discord bot + alert flow
- live monitor, tuner, validator, and portfolio tracking

## What This Repo Drops

- huge upstream git history
- most upstream GitHub workflow clutter
- generated runtime data
- screenshots/docs/testing baggage that is not needed for running the assistant

## Quick Start

1. Create and activate a Python 3.12 virtual environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env.dev` and fill your secrets.
4. Complete official Kite login:

```powershell
python -m pkscreener.pkscreenercli --kiteconnectloginurl
python -m pkscreener.pkscreenercli --kiteconnectsession "FULL_REDIRECT_URL"
python -m pkscreener.pkscreenercli --kiteconnectdoctor
```

5. Start the live assistant:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_live_assistant.ps1 -Budget 3000 -IntervalSeconds 30
```

6. Check or stop it:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\status_live_assistant.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\stop_live_assistant.ps1
```

## Safer Live Mode

If you want near-blind operation, use hard loss brakes instead of blindly accepting every buy alert.

Example:

```powershell
python -m pkscreener.pkscreenercli --assistantstart nifty50 --assistantaccount main --assistantautotune --assistantstrongbuyonly --assistantmaxdailyloss 1500 --assistantmaxconsecutivelosses 2
```

This keeps sell-side alerts flowing, but suppresses fresh buy-side monitor ideas for the linked live account after:

- realized loss for the current day breaches your limit
- consecutive losing closed trades hit your limit

For Discord `/buynow`, the bot also shows whether the buy-side guardrail is clear or paused.

## Run On Linux VM

For GCP/Ubuntu, use `systemd` so the assistant survives SSH/browser closure:

```bash
cd ~/tr-assistant
sudo bash scripts/install_live_assistant_service.sh "$USER" "$HOME/tr-assistant" nifty500 3000 30
sudo systemctl start trade-assistant
sudo systemctl status trade-assistant
```

Full details:

- `docs/SYSTEMD.md`

## Discord Commands

- `/buynow`
- `/sellnow`
- `/portfolio`
- `/positions`
- `/pnl`
- `/history`
- `/addcash`
- `/buy`
- `/sell`

## Runtime Output

Generated files are written under `results/Data/`, including:

- `assistant_runtime.db`
- `assistant_portfolio.db`
- `assistant_logs/`
- live dashboards
- signal exports

These are runtime artifacts and are intentionally ignored by git.

## Notes

- This repo is a trimmed product-focused derivative of PKScreener.
- The original license is preserved in `LICENSE`.
- No trading tool can guarantee profits; this project is built to improve decision quality and workflow speed, not to promise certainty.

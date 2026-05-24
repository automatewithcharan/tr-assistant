#!/usr/bin/env bash
# Daily swing alert paper trader — runs at 10:30 AM IST after prewarm.
# Processes yesterday's alerts (fills entries at today's open), checks active
# position exits, sends Discord summary.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[ -f .env.dev ] && set -a && . .env.dev && set +a
PY="$REPO/.venv/bin/python3"
LOG="$REPO/results/Data/swing_alert_paper/eod_cron.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }

echo "[$(ts)] start" >> "$LOG"
OUT=$("$PY" scripts/swing_alert_paper_trader.py 2>&1)
echo "$OUT" >> "$LOG"

# Discord summary
SUMMARY=$("$PY" -c "
import json, pandas as pd
from pathlib import Path
j = json.loads(Path('results/Data/swing_alert_paper/journal.json').read_text())
trades = j['trades']
closed = [t for t in trades if t.get('status') == 'closed']
active = [t for t in trades if t.get('status') == 'active']
pending = [t for t in trades if t.get('status') == 'pending']
today = j['daily_log'][-1] if j['daily_log'] else {}
date = today.get('date', '?')

if not closed:
    print(f'📊 **Swing Alert Paper — {date}**')
    print(f'New: {len(pending)} pending | Active: {len(active)} | Closed: 0 yet')
    print('Strategy: Grade B+C alerts, Q≥50, +12%/-5%/10d hold')
else:
    df = pd.DataFrame(closed)
    n = len(df)
    wins = df[df['net_return_pct'] > 0]
    avg = df['net_return_pct'].mean()
    total = df['net_return_pct'].sum()
    wr = len(wins)/n*100
    cum_inr = total/100*50000
    closed_today_n = today.get('closed_today', 0)
    today_pl = sum(t['net_return_pct'] for t in trades if t.get('status')=='closed' and t.get('exit_date')==date)
    print(f'📊 **Swing Alert Paper — {date}**')
    print(f'Today: filled={today.get(\"filled_today\",0)}, closed={closed_today_n}, P&L {today_pl:+.2f}%')
    print(f'Pipeline: {len(pending)} pending | {len(active)} active')
    print(f'All-time: {n} closed, WR {wr:.0f}%, avg {avg:+.2f}%/trade, cum ₹{cum_inr:+,.0f}')
")

if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    PAYLOAD=$(echo "$SUMMARY" | python3 -c 'import sys, json; print(json.dumps({"content": sys.stdin.read()}))')
    curl -s -X POST "$DISCORD_WEBHOOK_URL" -H 'Content-Type: application/json' --data-raw "$PAYLOAD" > /dev/null 2>&1 || true
fi

echo "[$(ts)] done" >> "$LOG"

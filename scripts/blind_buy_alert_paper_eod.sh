#!/usr/bin/env bash
# Daily blind-buy alert paper trader — runs at 10:30 AM IST after prewarm.
# Fills pending entries at today's open, checks exits, sends Discord summary.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[ -f .env.dev ] && set -a && . .env.dev && set +a
PY="$REPO/scripts/runtime_python.sh"
LOG="$REPO/results/Data/blind_buy_alert_paper/eod_cron.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }

echo "[$(ts)] start" >> "$LOG"
OUT=$("$PY" scripts/blind_buy_paper_trader.py 2>&1)
echo "$OUT" >> "$LOG"

# Discord summary
SUMMARY=$("$PY" -c "
import json, pandas as pd
from pathlib import Path
jpath = Path('results/Data/blind_buy_alert_paper/journal.json')
if not jpath.exists():
    print('Blind-Buy Paper: no journal yet')
    raise SystemExit(0)
j = json.loads(jpath.read_text())
trades = j['trades']
closed  = [t for t in trades if t.get('status') == 'closed']
active  = [t for t in trades if t.get('status') == 'active']
pending = [t for t in trades if t.get('status') == 'pending']
today = j['daily_log'][-1] if j['daily_log'] else {}
date = today.get('date', '?')
ret_col = 'blended_net_pct' if closed and 'blended_net_pct' in closed[0] else 'net_return_pct'

if not closed:
    print(f'📈 **Blind-Buy Paper — {date}**')
    print(f'New: {today.get(\"new_alerts\",0)} | Filled: {today.get(\"filled_today\",0)} | Active: {len(active)}')
    print('No closed trades yet.')
else:
    df = pd.DataFrame(closed)
    df[ret_col] = pd.to_numeric(df[ret_col], errors='coerce')
    n = len(df)
    wins = df[df[ret_col] > 0]
    avg = df[ret_col].mean()
    wr = len(wins)/n*100
    # Real ₹ from per-trade position size; falls back to legacy ₹50K if missing.
    df['_pos'] = pd.to_numeric(df.get('position_inr'), errors='coerce').fillna(50000)
    cum_inr = (df['_pos'] * df[ret_col] / 100.0).sum()
    capital = j.get('config', {}).get('total_capital_inr') or 200000
    ret_on_cap = cum_inr / capital * 100
    closed_today_n = today.get('closed_today', 0)
    today_pl_inr = sum(
        float(t.get('position_inr', 50000)) * float(t.get(ret_col, 0)) / 100.0
        for t in trades
        if t.get('status')=='closed' and t.get('exit_date')==date
    )
    print(f'📈 **Blind-Buy Paper — {date}**')
    print(f'Today: filled={today.get(\"filled_today\",0)}, closed={closed_today_n}, P&L ₹{today_pl_inr:+,.0f}')
    print(f'Pipeline: {len(pending)} pending | {len(active)} active')
    print(f'All-time: {n} closed, WR {wr:.0f}%, avg {avg:+.2f}%/trade, cum ₹{cum_inr:+,.0f} ({ret_on_cap:+.2f}% on ₹{capital/100000:.0f}L)')
" 2>&1) || true

if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    PAYLOAD=$(echo "$SUMMARY" | python3 -c 'import sys, json; print(json.dumps({"content": sys.stdin.read()}))')
    curl -s -X POST "$DISCORD_WEBHOOK_URL" \
        -H 'Content-Type: application/json' \
        --data-raw "$PAYLOAD" > /dev/null 2>&1 || true
fi

echo "[$(ts)] done — $SUMMARY" >> "$LOG"

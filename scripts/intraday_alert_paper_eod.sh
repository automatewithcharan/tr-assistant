#!/usr/bin/env bash
# Run intraday alert paper trader EOD, send Discord summary.
# Cron: 30 11 * * 1-5 (5:00 PM IST Mon-Fri)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[ -f .env.dev ] && set -a && . .env.dev && set +a
PY="$REPO/.venv/bin/python3"

LOG="$REPO/results/Data/intraday_alert_paper/eod_cron.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }

echo "[$(ts)] eod start" >> "$LOG"

# Process today's alerts
OUT=$("$PY" scripts/intraday_alert_paper_trader.py 2>&1)
echo "$OUT" >> "$LOG"

# Build Discord message: today's trades + cumulative stats
SUMMARY=$("$PY" -c "
import json, pandas as pd
from pathlib import Path
j = json.loads(Path('results/Data/intraday_alert_paper/journal.json').read_text())
today = max((e['date'] for e in j['daily_log']), default=None)
today_log = next((e for e in j['daily_log'] if e['date']==today), {})
trades_df = pd.DataFrame([t for t in j['trades'] if not t.get('skipped')])
today_trades = trades_df[trades_df.get('date','') == today] if not trades_df.empty else pd.DataFrame()
total_n = len(trades_df)
avg_per_trade = trades_df['net_return_pct'].mean() if not trades_df.empty else 0
win_rate = (trades_df['net_return_pct']>0).mean()*100 if not trades_df.empty else 0
cum = trades_df['net_return_pct'].sum() if not trades_df.empty else 0
days = len({t['date'] for t in j['trades']}) if j['trades'] else 0
monthly_pace = cum / max(days,1) * 21
today_n = len(today_trades)
today_avg = today_trades['net_return_pct'].mean() if not today_trades.empty else 0
print(f\"📊 **Intraday Alert Paper — {today}**\\n\"
      f\"Today: {today_n} trades, avg {today_avg:+.2f}%\\n\"
      f\"Cumulative ({total_n} trades, {days} days): {cum:+.2f}%  |  avg {avg_per_trade:+.2f}%/trade  |  WR {win_rate:.0f}%\\n\"
      f\"Monthly pace: {monthly_pace:+.2f}%/mo (sequential 1-position)\")
")

if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    curl -s -X POST "$DISCORD_WEBHOOK_URL" \
        -H 'Content-Type: application/json' \
        --data-raw "$(printf '{"content": %s}' "$(echo "$SUMMARY" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')")" \
        > /dev/null 2>&1 || true
fi

echo "[$(ts)] eod done" >> "$LOG"

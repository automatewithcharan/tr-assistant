# Daily prewarm — refreshes Kite OHLCV history cache for all stocks.
# Runs at 2:30 AM UTC (8:00 AM IST) Mon-Fri, BEFORE the watchdog starts the live session.
# Uses --kiteprewarm (standalone, exits when done) NOT --assistantstart.
# --kiteprewarmforce forces re-download even if cache exists, ensuring no stale data.

set -e

REPO=/home/charan_sai/tr-assistant
PYTHON=$REPO/venv/bin/python3
LOG=/tmp/daily_prewarm.log
WEBHOOK=$(grep DISCORD_WEBHOOK_URL $REPO/.env.dev 2>/dev/null | cut -d= -f2- || echo '')

send_discord() {
    [ -z "$WEBHOOK" ] && return
    curl -s -X POST "$WEBHOOK"          -H 'Content-Type: application/json'          -d "{\"content\": \"$1\"}" > /dev/null 2>&1 || true
}

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: starting cache refresh for NSE+BSE" >> $LOG
send_discord "🔄 **Daily prewarm started** — refreshing cache for NSE+BSE stocks (8 AM IST)"

# Check Kite session is valid before starting; re-login if token is expired
SESSION_FILE=$REPO/results/Data/kite_connect_session.json
if [ -f "$SESSION_FILE" ]; then
    MODIFIED_EPOCH=$(stat -c %Y "$SESSION_FILE")
    NOW_EPOCH=$(date +%s)
    AGE_HOURS=$(( (NOW_EPOCH - MODIFIED_EPOCH) / 3600 ))
    if [ $AGE_HOURS -gt 20 ]; then
        echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: Kite session is ${AGE_HOURS}h old — auto-login needed" >> $LOG
        send_discord "⚠️ **Kite session expired** (${AGE_HOURS}h old) — prewarm may fail. Re-login required."
    fi
fi

# Run standalone prewarm (exits when done — does NOT start a trading session)
# 1. Refresh NSE allstocks
TERM=xterm $PYTHON -m pkscreener.pkscreenercli     --kiteprewarm allstocks     --kiteprewarmforce     --kiteprewarmworkers 8     >> $LOG 2>&1

# 2. Refresh BSE stocks (bonds already excluded in BSE_EQUITY_L.csv)
TERM=xterm $PYTHON -m pkscreener.pkscreenercli     --kiteprewarm bse_allstocks     --kiteprewarmworkers 8     >> $LOG 2>&1

# 3. Rebuild combined universe sorted by liquidity (for ticker subscription)
TERM=xterm $PYTHON -c "
import pandas as pd, os, csv
cache_dir = 'results/Data/kite_history_cache/day'
nse_syms = set(pd.read_csv('results/Indices/EQUITY_L.csv')['SYMBOL'].str.strip().str.upper())
results = []
for fname in os.listdir(cache_dir):
    if not fname.endswith('.csv'): continue
    sym = fname[:-4]
    try:
        candles = []
        with open(os.path.join(cache_dir, fname)) as f:
            for row in csv.DictReader(f):
                candles.append(float(row['close']) * int(float(row['volume'])))
        if len(candles) < 20: continue
        avg_to = sum(candles[-20:]) / 20
        results.append({'Symbol': sym, 'AvgTurnover': avg_to, 'Exchange': 'NSE' if sym in nse_syms else 'BSE'})
    except: continue
df = pd.DataFrame(results)
nse_all = df[df['Exchange']=='NSE']
bse_liquid = df[(df['Exchange']=='BSE') & (df['AvgTurnover'] >= 5_000_000)]
combined = pd.concat([nse_all, bse_liquid]).sort_values('AvgTurnover', ascending=False)
combined[['Symbol']].to_csv('results/Indices/NSE_BSE_COMBINED.csv', index=False)
print(f'Combined universe: {len(combined)} symbols (NSE={len(nse_all)}, BSE-liquid={len(bse_liquid)})')
" >> $LOG 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    REFRESHED=$(find $REPO/results/Data/kite_history_cache/day -name '*.csv' -newer $LOG 2>/dev/null | wc -l)
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: done (exit=0, ~${REFRESHED} files updated)" >> $LOG
    send_discord "✅ **Prewarm complete** — cache refreshed for ~${REFRESHED} stocks. Session will start at 8:30 AM IST."
else
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: FAILED (exit=$EXIT_CODE)" >> $LOG
    send_discord "🚨 **Prewarm FAILED** (exit=${EXIT_CODE}) — scanner may use stale data today. Check /tmp/daily_prewarm.log"
fi

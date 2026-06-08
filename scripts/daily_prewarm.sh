# Daily prewarm — refreshes Kite OHLCV history cache for all stocks.
# Runs at 2:30 AM UTC (8:00 AM IST) Mon-Fri, BEFORE the watchdog starts the live session.
# Uses --kiteprewarm (standalone, exits when done) NOT --assistantstart.
# --kiteprewarmforce forces re-download even if cache exists, ensuring no stale data.

set -e

REPO=/home/charan_sai/tr-assistant
PYTHON=$REPO/.venv/bin/python
if [ ! -x "$PYTHON" ]; then
    PYTHON=$REPO/venv/bin/python3
fi
LOG=/tmp/daily_prewarm.log
WEBHOOK=$(grep DISCORD_WEBHOOK_URL $REPO/.env.dev 2>/dev/null | cut -d= -f2- || echo '')

send_discord() {
    [ -z "$WEBHOOK" ] && return
    curl -s -X POST "$WEBHOOK"          -H 'Content-Type: application/json'          -d "{\"content\": \"$1\"}" > /dev/null 2>&1 || true
}

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: starting cache refresh for NSE+BSE" >> $LOG
send_discord "🔄 **Daily prewarm started** — refreshing cache for NSE+BSE stocks (8 AM IST)"

# Stamp a reference marker at run start so we can count files refreshed *during* this run.
# (Can't use $LOG as the reference because it gets appended to throughout the run, so its
#  mtime always ends up newer than the cache files → find -newer $LOG returns 0.)
REF_MARKER=/tmp/daily_prewarm.start_marker
touch $REF_MARKER

# Check Kite session is valid before starting; re-login if token is expired
SESSION_FILE=$REPO/results/Data/kite_connect_session.json
if [ -f "$SESSION_FILE" ]; then
    MODIFIED_EPOCH=$(stat -c %Y "$SESSION_FILE")
    NOW_EPOCH=$(date +%s)
    AGE_HOURS=$(( (NOW_EPOCH - MODIFIED_EPOCH) / 3600 ))
    if [ $AGE_HOURS -gt 20 ]; then
        echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: Kite session is ${AGE_HOURS}h old — auto-login needed" >> $LOG
        send_discord "⚠️ **Kite session expired** (${AGE_HOURS}h old) — trying auto-login before prewarm."
        TERM=xterm $PYTHON -m pkscreener.pkscreenercli --kiteautologin >> $LOG 2>&1 || {
            echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: auto-login failed; continuing with existing session" >> $LOG
            send_discord "🚨 **Kite auto-login failed before prewarm** — scanner may have weak anomaly coverage."
        }
    fi
fi

# Run standalone prewarm (exits when done — does NOT start a trading session)
# 1. Refresh NSE allstocks
TERM=xterm $PYTHON -m pkscreener.pkscreenercli     --kiteprewarm allstocks     --kiteprewarmforce     --kiteprewarmworkers 8     >> $LOG 2>&1

# 2. Refresh BSE stocks (bonds already excluded in BSE_EQUITY_L.csv)
TERM=xterm $PYTHON -m pkscreener.pkscreenercli     --kiteprewarm bse_allstocks     --kiteprewarmworkers 8     >> $LOG 2>&1

# 3. Rebuild combined universe sorted by liquidity (for ticker subscription)
TERM=xterm $PYTHON -c "
import pandas as pd, os, csv, re
cache_dir = 'results/Data/kite_history_cache/day'
nse_syms = set(pd.read_csv('results/Indices/EQUITY_L.csv')['SYMBOL'].str.strip().str.upper())
bse_tradeable = set(pd.read_csv('results/Indices/BSE_TRADEABLE.csv')['Symbol'].str.strip().str.upper())
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
nse_in_cache = df[df['Exchange']=='NSE']
# Build universe in priority order so the 2990-cap protects what matters:
#   1. ALL NSE equities (cached + uncached) — official listings always included.
#      Uncached entries (NUVAMA, MARICO, JIOFIN style) are flagged with priority
#      so they survive truncation alongside their cached peers.
#   2. BSE tradeable equities — fill remaining slots ranked by turnover.
nse_nocache = {s for s in nse_syms if s not in set(df['Symbol'])}
nse_extra = pd.DataFrame([{'Symbol': s, 'AvgTurnover': 0, 'Exchange': 'NSE'} for s in nse_nocache])
nse_all = pd.concat([nse_in_cache, nse_extra])
print(f'NSE pool: {len(nse_all)} ({len(nse_in_cache)} cached + {len(nse_nocache)} uncached)')

# Skip govt bonds/debentures (symbols starting with digits) on BSE side
is_equity = ~df['Symbol'].str.match(r'^\d')
bse_in_cache = df[(df['Exchange']=='BSE') & is_equity]
bse_nocache = {s for s in bse_tradeable if s not in set(df['Symbol']) and not re.match(r'^\d', s)}
bse_extra = pd.DataFrame([{'Symbol': s, 'AvgTurnover': 0, 'Exchange': 'BSE'} for s in bse_nocache])
bse_all = pd.concat([bse_in_cache, bse_extra]).drop_duplicates('Symbol')

# WebSocket cap = 2990 (3000 limit minus 10 buffer).
# Always preserve full NSE first; trim BSE if capacity is tight.
nse_dedup = nse_all.drop_duplicates('Symbol')
nse_take = nse_dedup.sort_values('AvgTurnover', ascending=False)
bse_capacity = max(0, 2990 - len(nse_take))
bse_take = bse_all.sort_values('AvgTurnover', ascending=False).head(bse_capacity)
combined = pd.concat([nse_take, bse_take]).drop_duplicates('Symbol').head(2990)
combined[['Symbol']].to_csv('results/Indices/NSE_BSE_COMBINED.csv', index=False)
print(f'Combined universe: {len(combined)} symbols (NSE={len(nse_take)}, BSE={len(bse_take)})')
" >> $LOG 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    REFRESHED=$(find $REPO/results/Data/kite_history_cache/day -name '*.csv' -newer $REF_MARKER 2>/dev/null | wc -l)
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: done (exit=0, ~${REFRESHED} files updated)" >> $LOG
    send_discord "✅ **Prewarm complete** — cache refreshed for ~${REFRESHED} stocks. Session will start at 8:30 AM IST."
else
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Prewarm: FAILED (exit=$EXIT_CODE)" >> $LOG
    send_discord "🚨 **Prewarm FAILED** (exit=${EXIT_CODE}) — scanner may use stale data today. Check /tmp/daily_prewarm.log"
fi

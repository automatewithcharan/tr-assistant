# UT_SYNC v2.7a A/B/C paper research

This additive package reconstructs completed-candle UT_SYNC v2.7a opportunities and compares three independent paper portfolios. It is not an order router. It has no broker mutation method, no production runtime import, and no HTTP/Discord sender. `live-paper` is deliberately disabled pending separate approval and review.

## Strategy labels

- A: `A_UT_V27A_BAR_CLOSE_RECONSTRUCTION` — fresh UT and controlled continuation with UT/PSAR/VWAP/Pine stop rules.
- B: A plus prior fully closed rising 5-minute EMA20 and bullish/rising Wilder DMI/ADX.
- C: B plus exact simplified QQC three-orange, falling CHOP14 below 40, same-minute RVOL above 1.2, aligned positive/rising NIFTY 50 RS, and `ORB_HOLD_2`.

Pine AUTO SIGNALS is realtime `varip` behavior. OHLC replay cannot establish exact TradingView server-alert parity, tick order, live drift, or fill prices. Signals are observable only at candle end and replay fills use the next bar open plus adverse cost proxies.

## Commands

Run from the isolated repository root with its existing virtual environment:

```bash
${REPO_ROOT}/.venv/bin/python -m paper_trading doctor
${REPO_ROOT}/.venv/bin/python -m paper_trading validate-config --config paper_trading/default_config.json
${REPO_ROOT}/.venv/bin/python -m paper_trading replay \
  --data-root ${REPO_ROOT}/results/Data/kite_history_backfill/minute \
  --symbols IFCI,RELIANCE,TCS \
  --benchmark 'NIFTY 50' --start 2026-09-15 --end 2026-10-09 \
  --config paper_trading/default_config.json \
  --journal artifacts/paper.sqlite --output-dir artifacts/replay
${REPO_ROOT}/.venv/bin/python -m paper_trading report \
  --journal artifacts/paper.sqlite --output artifacts/replay/report.md
```

Outputs include the WAL SQLite journal, `daily_report.md/.json`, normalized `trades.csv`, per-symbol parity CSVs, a local unsent Discord preview, and replay provenance hashes. Re-running identical source/config/data into the same DB is idempotent.

## Data and safety

Input CSVs must contain `datetime,open,high,low,close,volume,symbol`; timestamps are interpreted as IST candle starts. Invalid/off-session/duplicate data are rejected, gaps are reported, and missing C inputs fail closed. The loader never writes source caches.

Official research fee defaults checked 2026-10-10: Zerodha NSE equity intraday brokerage 0.03% capped at ₹20/order, sell STT 0.025% rounded to whole rupees, NSE charges 0.00307%, SEBI ₹10/crore, buy stamp 0.003%, GST 18%, and NSE IPFT ₹0.01/crore from 2026-03-01. No delivery DP fee is applied. Validate against an actual contract note before any operational use.

## Rollback

Nothing is installed or scheduled. Stop using the feature worktree; archive/delete only its generated `artifacts/` if desired, and remove the feature worktree/branch after review. Existing services need no restart or rollback because they are untouched.

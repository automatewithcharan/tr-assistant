# Phase 6E reproducibility summary

This is the compact companion to
[`PHASE6E_20_SESSION_ABC_BACKTEST_REPORT.md`](PHASE6E_20_SESSION_ABC_BACKTEST_REPORT.md).
It intentionally contains no raw candles, SQLite journals, trade ledger, credentials,
or operational paths.

## Immutable run

- Run ID: `b233add28d212c0008d626d82ff1da2b`
- Frozen cohort: 78 existing Phase 6A snapshot symbols
- Warm-up: 2026-07-20–2026-07-31; evaluation: 2026-08-03–2026-08-28
- CAS ledger: 1,540 CAS, 20 non-CAS, zero unresolved stock-sessions
- Policy: `FROZEN_ORIGINAL`; `ORB_RETEST=false`
- Manifest SHA-256: `bdccfd03f976fb004df07d15249c8365faa4eb89babc9464ee2480b5e32726c2`
- Normalized source SHA-256: `c48d4f9ee635d485e320143f170be0c281d1c964beace87d1cc2abc86783cf9b`
- Configuration canonical SHA-256: `5c5f60feeca74d7f5921328e3c09da0005cedc4d2e72f04be353460e9bcd66b0`

## Aggregate result

| Variant | Closed | Gross P&L | Costs | Net P&L | Avg net R |
|---|---:|---:|---:|---:|---:|
| A | 193 | ₹-26,432.20 | ₹11,933.43 | ₹-38,365.63 | -1.376 |
| B | 179 | ₹-25,826.40 | ₹11,845.70 | ₹-37,672.10 | -1.309 |
| C | 63 | ₹-9,368.85 | ₹5,289.69 | ₹-14,658.54 | -1.133 |

All variants were gross- and net-negative.  There were no incomplete positions;
the CAS-aware censor rule remains present for any unresolved EOD exit and does not
convert an auction price into a continuous-market fill.  These are OHLC replay
outputs, not broker fills or TradingView realtime-alert parity.

## Local-only evidence

The feature worktree retains the immutable manifest, normalized-source database,
replay journal, trades/events CSV exports, and aggregate-metrics JSON under
`paper_trading/research_artifacts/phase6e/`.  They are excluded from public review
because they include raw market data and detailed operational research artifacts.

# Phase 6A — Frozen A/B/C exploratory backtest (sanitized)

This public review document excludes local paths, credentials, raw market data, SQLite journals, detailed ledgers, and operational configuration.

Fixed cohort: 78 current-exact symbols from the 2026-04-22 NIFTY 100 snapshot; indicator-only warm-up July 13–24, 2026; evaluation July 27–31. The policy was `FROZEN_ORIGINAL`: historical closed-bar reconstruction with next-bar-open proxy, not verified broker execution. ORB retest was disabled.

| Variant | Closed | Win rate | Gross ₹ | Cost drag ₹ | Net ₹ | Avg net R | Profit factor | Max DD ₹ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 30 | 6.7% | -7,037.00 | 2,169.22 | -9,206.22 | -1.299 | 0.030 | 9,407.12 |
| B | 38 | 15.8% | -5,834.00 | 2,739.97 | -8,573.97 | -1.029 | 0.184 | 8,974.58 |
| C | 17 | 5.9% | -6,752.75 | 1,684.50 | -8,437.25 | -1.600 | 0.043 | 9,654.90 |

No variant was profitable before costs. There were no incomplete positions. The narrow, complete-data cohort has snapshot-membership and data-availability selection bias, covers only five sessions, and cannot establish Pine realtime-alert parity or a trading edge.

No API request, broker operation, Discord send, deployment, or production-service change occurred.

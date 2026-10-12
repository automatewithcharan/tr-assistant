# Phase 6A trade forensics (sanitized)

This review copy preserves aggregate calculations while excluding raw trades, event journals, local file locations, and operational details.

| Variant | Losing trades | Never-positive MFE | Positive MFE then loss | Hard/gap stops | PSAR exits | Trail exits |
|---|---:|---:|---:|---:|---:|---:|
| A | 28 / 30 | 11 | 17 | 17 / 4 | 3 | 6 |
| B | 32 / 38 | 13 | 19 | 17 / 4 | 9 | 8 |
| C | 16 / 17 | 9 | 7 | 11 / 0 | 5 | 1 |

Mean entry distance above VWAP was +0.440%/+0.399%/+0.900% for A/B/C; mean initial stop distance was 1.549x/1.751x/2.048x ATR. Gross P&L was negative before fees; repeated hard/gap stops and reversals explain average net R below -1.

B had 38 closed trades versus A’s 30 because variant portfolios, leg consumption, risk limits, and occupancy are independent. Twenty-four accepted candidates overlapped. C rejected realized A/B fills that were mostly losing in this sample, but this is observational only: its executable portfolio path would differ.

Losses occurred on all five sessions. The sample is too short and selected to support symbol-specific or profitability conclusions.

# Phase 6A detailed aggregate trading evidence (sanitized)

Source: the authoritative Phase 6A `verified2` closed-trade and event journal. This report publishes aggregate calculations only: no raw candles, individual trade ledger, local path, account information, or operational configuration. It is an exploratory, five-session closed-bar reconstruction—not a broker-executable or TradingView-alert-parity result.

## Reconciliation and entry type

All tables reconcile to the published closed-trade totals: A 30 / -₹9,206.22 / -38.96 net R; B 38 / -₹8,573.97 / -39.11 net R; C 17 / -₹8,437.25 / -27.20 net R.

| Variant | Entry type | Trades | Gross R | Net R | Win rate | Exit reasons |
|---|---|---:|---:|---:|---:|---|
| A | Fresh UT | 19 | -21.81 | -28.83 | 0.0% | 13 hard, 2 gap, 1 PSAR, 3 trail |
| A | Continuation | 11 | -6.37 | -10.13 | 18.2% | 4 hard, 2 gap, 2 PSAR, 3 trail |
| B | Fresh UT | 13 | -13.97 | -18.41 | 0.0% | 8 hard, 1 gap, 2 PSAR, 2 trail |
| B | Continuation | 25 | -11.16 | -20.70 | 24.0% | 9 hard, 3 gap, 7 PSAR, 6 trail |
| C | Fresh UT | 13 | -18.68 | -24.10 | 0.0% | 9 hard, 0 gap, 4 PSAR |
| C | Continuation | 4 | -2.31 | -3.10 | 25.0% | 2 hard, 0 gap, 1 PSAR, 1 trail |

## Intrabar MFE/MAE and duration

MFE/MAE are reconstructed from each 1-minute candle high/low and are **intrabar estimates**. A threshold hit is not proof that an order could have been causally submitted and filled there. In particular, +1R/+1.5R MFE is not an executable profit-taking outcome under the frozen trailing policy.

| Variant | ≥+0.25R MFE | ≥+0.5R | ≥+1R | ≥+1.5R | Never positive MFE | Positive MFE then net loss | Mean / median hold minutes |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 16/30 | 12/30 | 10/30 | 5/30 | 11 | 17 | 5.1 / 3 |
| B | 18/38 | 17/38 | 13/38 | 8/38 | 13 | 19 | 6.6 / 3 |
| C | 6/17 | 4/17 | 2/17 | 1/17 | 9 | 7 | 4.4 / 2 |

MFE-R quartiles (25/50/75%) were A 0.00/0.37/1.24, B 0.00/0.20/1.37, C 0.00/0.00/0.46. MAE-R quartiles were A -1.32/-1.14/-0.72, B -1.27/-0.99/-0.61, C -1.27/-1.06/-0.82. Gap-through fills explain MAE below -1R.

## Stop and entry-distance distributions

| Variant | Stop ATR min / median / max | Stop % min / median / max | VWAP distance % min / median / max | Confirmed EMA5 % min / median / max |
|---|---|---|---|---|
| A | 0.87 / 1.43 / 2.86 | 0.122 / 0.247 / 0.496 | +0.122 / +0.421 / +1.003 | -0.212 / +0.442 / +1.984 |
| B | 0.84 / 1.65 / 3.63 | 0.122 / 0.252 / 0.968 | +0.069 / +0.395 / +0.721 | +0.244 / +0.817 / +2.131 |
| C | 1.05 / 2.24 / 2.89 | 0.118 / 0.193 / 0.521 | +0.372 / +0.870 / +1.879 | +0.213 / +0.928 / +1.793 |

The journal supports these distribution summaries but does not have a precomputed bucket schema; no unrequested bucket boundary was optimized or inferred. The complete local forensic CSV remains private.

## Costs and exit outcomes

| Variant | Brokerage ₹ | STT ₹ | Transaction ₹ | SEBI ₹ | Stamp ₹ | GST ₹ | Total fees ₹ |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 988.82 | 706.00 | 173.59 | 5.64 | 84.95 | 210.22 | 2,169.22 |
| B | 1,294.73 | 854.00 | 209.80 | 6.81 | 102.60 | 272.03 | 2,739.97 |
| C | 627.60 | 665.00 | 163.32 | 5.32 | 79.90 | 143.36 | 1,684.50 |

Spread and slippage are embedded in the adverse simulated fill price. The journal does not store a separable ₹ spread/slippage attribution, so that measurement is unavailable rather than estimated.

| Variant | Exit | Wins / losses | Net ₹ |
|---|---|---:|---:|
| A | Hard / gap / PSAR / trail | 0/17; 0/4; 2/1; 0/6 | -6,908.93; -1,720.84; -172.46; -403.99 |
| B | Hard / gap / PSAR / trail | 0/17; 0/4; 3/6; 3/5 | -6,607.34; -1,529.61; -1,606.59; +1,169.57 |
| C | Hard / PSAR / trail | 0/11; 0/5; 1/0 | -6,155.54; -2,660.99; +379.28 |

## C funnel, daily concentration, and B-versus-A evidence

C produced 18,487 base candidates, accepted 17 and filled 17. Its primary rejections included UT reversal 4,685, base trend 4,010, entry-window 2,113, UT age 1,703, ADX low/rising 761/320, CHOP high/falling 314/51, QQC quantity/quality/volume 158/8/8, RVOL 67, ORB two-close hold 30, and relative-strength 15 positive plus 1 rising failure. ORB retest was disabled.

| Date | A net ₹ / trades | B net ₹ / trades | C net ₹ / trades |
|---|---:|---:|---:|
| Jul 27 | -1,687.99 / 7 | -1,759.29 / 13 | -1,295.00 / 5 |
| Jul 28 | -2,173.52 / 6 | -1,536.61 / 11 | -2,172.92 / 4 |
| Jul 29 | -1,812.11 / 5 | -1,730.04 / 5 | -1,564.65 / 2 |
| Jul 30 | -1,997.05 / 6 | -1,905.06 / 4 | -1,684.39 / 3 |
| Jul 31 | -1,535.55 / 6 | -1,642.97 / 5 | -1,720.29 / 3 |

Largest aggregate symbol losses were A: BHARTIARTL -₹1,442.55; B: NAUKRI -₹963.87; C: ETERNAL -₹1,892.81. Losses were present every day; five sessions are insufficient for a concentration conclusion.

B accepted 71 candidates and filled 38 versus A’s 56/30; 24 accepted candidates overlapped. B has independent positions, leg consumption, daily-loss state, and occupancy. B also had 588 decision-stage daily-loss rejections versus A’s 2,552, but 29 B entry attempts were then unfilled at the position limit. These causal ledger facts explain the count difference; no unexpected confirmation-gate bypass was found.

## Next predeclared cohort (not executed)

After point-in-time membership and CAS classification data are approved and repaired, use a fixed historical NSE EQ eligibility snapshot, at least 20 consecutive complete sessions with 10 prior valid RVOL sessions, a locked benchmark, immutable file hashes, and a chronological 10-session warm-up / 10-session evaluation split. Retain every incomplete position separately, require equal data access for A/B/C, and do not choose symbols/dates after results are known. This is a proposal only; no data retrieval or replay has been performed.

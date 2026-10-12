# Phase 6E — frozen 20-session A/B/C backtest

## Decision

All three frozen variants were negative **before** and after the configured costs in
this predeclared 20-session evaluation.  This is a research result, not evidence
of TradingView alert parity, live-fill performance, statistical significance, or
tradability.  No parameter was changed after seeing the result.

## Frozen scope and integrity

| Item | Fixed value |
|---|---|
| Cohort | 78-symbol `PHASE6A_NIFTY100_APRIL22_CURRENT_EXACT_COMPLETE_78` snapshot |
| Indicator-only warm-up | 2026-07-20 through 2026-07-31 (10 sessions; no trades or P&L) |
| Evaluation | 2026-08-03 through 2026-08-28 (20 sessions) |
| CAS calendar | 1,540 verified CAS stock-sessions; 20 verified non-CAS; zero unresolved |
| Execution | `FROZEN_ORIGINAL`, closed-bar reconstruction and next-observation bar-open proxy |
| ORB | Existing `ORB_HOLD_2`; `ORB_RETEST=false` |
| Benchmark | Existing validated NIFTY 50 one-minute cache, verified 375 candles/session |

Every frozen stock-session passed the dated expected-continuous-candle test: 360
09:15–15:14 candles for verified CAS securities and 375 09:15–15:29 candles for
the non-CAS security.  The NIFTY benchmark also passed 375 candles/session.  The
normalizer recorded 1,001 input-quality warnings; these are retained in the local
immutable source/replay artifacts and were not used to relax the session checks.

KOTAKBANK was accepted only through the Phase 6D dated mapping from old ISIN
`INE237A01028` to `INE237A01036`, effective 2026-01-14, plus the frozen token
crosswalk (Kite token 492033 / NSE FinInstrmId 1922) and dated CAS field.  The
local circular PDFs were absent; the split-circular facts remain user-supplied
official evidence, not locally byte-verified documents.

The immutable run ID is `b233add28d212c0008d626d82ff1da2b`; manifest SHA-256 is
`bdccfd03f976fb004df07d15249c8365faa4eb89babc9464ee2480b5e32726c2`.
The normalized-source SHA-256 is
`c48d4f9ee635d485e320143f170be0c281d1c964beace87d1cc2abc86783cf9b`, the
classification-ledger SHA-256 is
`ef3508cec38797cc6496b377d3bbcd11bb269feff0b340bff8ace2b7b13c46d5`, and the
canonical strategy-config SHA-256 is
`5c5f60feeca74d7f5921328e3c09da0005cedc4d2e72f04be353460e9bcd66b0`.

## Results

`Eligible` below means the 8,804 baseline-passing opportunities among 66,251
common base-candidate observations.  A variant decision can still be rejected by
its independent position, daily-loss, sizing, or portfolio controls.  A simulated
fill is never a broker fill.

| Variant | Accepted | Entry fills | Unfilled | Closed | Incomplete | Gross P&L | Costs | Net P&L | Avg gross R | Avg net R | Win rate | Gross PF | Max MTM DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A — original UT_SYNC | 828 | 193 | 635 | 193 | 0 | ₹-26,432.20 | ₹11,933.43 | ₹-38,365.63 | -0.974 | -1.376 | 8.3% | 0.265 | ₹38,468.70 |
| B — trend-confirmed | 336 | 179 | 157 | 179 | 0 | ₹-25,826.40 | ₹11,845.70 | ₹-37,672.10 | -0.911 | -1.309 | 8.9% | 0.249 | ₹37,672.10 |
| C — strict QQC/RS/ORB hold | 63 | 63 | 0 | 63 | 0 | ₹-9,368.85 | ₹5,289.69 | ₹-14,658.54 | -0.761 | -1.133 | 12.7% | 0.318 | ₹15,570.63 |

There were no incomplete positions.  The engine nevertheless now censors an
unresolved original-policy EOD exit on a verified CAS symbol as
`EOD_EXIT_UNVERIFIED_CAS_NO_NEXT_CONTINUOUS_OBSERVATION`, rather than inventing a
15:15/auction fill.  Thus the absence of censoring in this run does not validate
the original 15:15 convention; it only means no position reached that unresolved
state here.

### Fresh UT and controlled continuation

| Variant | Setup | Closed | Gross P&L | Net P&L | Avg net R | Wins / losses |
|---|---|---:|---:|---:|---:|---:|
| A | Fresh UT | 112 | ₹-18,231.40 | ₹-24,365.78 | -1.531 | 5 / 107 |
| A | Continuation | 81 | ₹-8,200.80 | ₹-13,999.85 | -1.162 | 11 / 70 |
| B | Fresh UT | 71 | ₹-10,457.25 | ₹-14,603.37 | -1.469 | 5 / 66 |
| B | Continuation | 108 | ₹-15,369.15 | ₹-23,068.73 | -1.204 | 11 / 97 |
| C | Fresh UT | 39 | ₹-6,775.15 | ₹-9,809.11 | -1.285 | 3 / 36 |
| C | Continuation | 24 | ₹-2,593.70 | ₹-4,849.43 | -0.887 | 5 / 19 |

Fresh UT was worse in each variant.  This is an observation from this fixed cohort,
not a warrant to change the frozen entry rule.

### Exit outcomes

| Variant | Gap stop | Hard stop | PSAR reversal | Trail/profit lock | UT reversal |
|---|---:|---:|---:|---:|---:|
| A closed / net P&L | 6 / ₹-257.16 | 121 / ₹-36,469.36 | 38 / ₹-4,079.63 | 26 / ₹3,048.87 | 2 / ₹-608.35 |
| B closed / net P&L | 12 / ₹-1,028.61 | 106 / ₹-34,563.69 | 35 / ₹-3,017.41 | 25 / ₹1,204.30 | 1 / ₹-266.69 |
| C closed / net P&L | 4 / ₹-966.69 | 31 / ₹-13,359.27 | 18 / ₹-519.06 | 9 / ₹510.85 | 1 / ₹-324.37 |

Hard stops were 258/435 resolved exits (59.3%) and every hard-stop trade was net
negative.  That concentration, plus predominantly non-positive early excursion,
is stronger evidence of an unfavorable entry/market fit than of a single exit-rule
problem; it does not isolate causality.

### Excursion and holding-time evidence

MFE/MAE use one-minute OHLC extrema after entry, expressed against actual initial
risk.  They are descriptive intrabar bounds, **not causally executable outcomes**
because the within-bar order is unknown.

| Variant | MFE ≥0.25R | ≥0.5R | ≥1R | ≥1.5R | Median MFE / MAE | Median hold | Range hold |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 68/193 (35.2%) | 54 (28.0%) | 35 (18.1%) | 27 (14.0%) | 0.00R / -1.08R | 4m | 0–35m |
| B | 68/179 (38.0%) | 58 (32.4%) | 40 (22.3%) | 21 (11.7%) | 0.00R / -1.04R | 4m | 0–25m |
| C | 31/63 (49.2%) | 25 (39.7%) | 18 (28.6%) | 10 (15.9%) | 0.00R / -1.00R | 5m | 0–32m |

The median MFE was zero in all variants while median MAE was about -1R.  That
supports the descriptive conclusion that many entries did not move favorably
before adverse movement; it is not evidence that a different stop would repair
them.

### Portfolio constraints and rejection funnel

Unfilled accepted entries were A: 572 max-open-position, 58 sizing/capital/risk/
liquidity, and 5 daily-loss-limit; B: 133, 22, and 2 respectively; C: zero.  The
portfolio maximum was three concurrent positions for all variants.

The following are per-gate failure counts, not a sequential funnel: a candidate
may fail several gates.  C accepted 63 of the 66,251 common observations.  Its
most frequent strict failures were ORB two-close hold (51,909), CHOP level
(49,436), RVOL (42,965), EMA rising (34,542), ADX rising (30,802), RS positive
(30,798), and CHOP falling (27,601).  Benchmark-alignment absence caused 2,009
RS rejects; they were fail-closed.  C had 13 accepted-candidate overlaps and 8
actual-fill overlaps with B (A/C: 10 and 2).  Consequently, this journal cannot
claim that C rejected losing or winning A/B trades: counterfactual portfolio fills
would require separate, predeclared causal research.

A/B had 216 accepted-candidate overlaps but only 34 filled-candidate overlaps
because their independent positions and risk constraints diverged.  B's modestly
less-negative aggregate (₹693.53 net and 0.067 average net R better than A) is
not a material superiority finding: the variants did not execute the same set of
trades, both are sharply negative, and the sample is not a significance test.

### Costs

| Component | A | B | C |
|---|---:|---:|---:|
| Brokerage | ₹5,542.01 | ₹5,393.85 | ₹2,219.00 |
| GST | ₹1,170.98 | ₹1,147.27 | ₹485.46 |
| STT | ₹3,801.00 | ₹3,861.00 | ₹1,881.00 |
| Transaction charge | ₹932.89 | ₹948.76 | ₹462.86 |
| Stamp | ₹456.27 | ₹463.99 | ₹226.30 |
| SEBI | ₹30.28 | ₹30.83 | ₹15.07 |
| Total reconciled cost drag | ₹11,933.43 | ₹11,845.70 | ₹5,289.69 |

The totals equal gross P&L minus net P&L for each variant.  The configured spread
and slippage are embedded in simulated entry/exit prices rather than this fee
table.  A was gross-negative by ₹26,432.20, B by ₹25,826.40, and C by ₹9,368.85;
costs therefore did not create the negative sign.

### Daily results (closed trades / net P&L)

| Date | A | B | C |
|---|---:|---:|---:|
| 2026-08-03 | 8 / ₹-1,977.92 | 12 / ₹-2,153.70 | 3 / ₹26.99 |
| 2026-08-04 | 7 / ₹-1,935.03 | 7 / ₹-1,780.96 | 3 / ₹-1,148.63 |
| 2026-08-05 | 12 / ₹-2,259.16 | 11 / ₹-2,297.61 | 5 / ₹-1,616.61 |
| 2026-08-06 | 9 / ₹-1,875.10 | 7 / ₹-2,195.35 | 4 / ₹-830.96 |
| 2026-08-07 | 9 / ₹-2,246.24 | 14 / ₹-1,551.62 | 8 / ₹-1,825.28 |
| 2026-08-10 | 5 / ₹-2,047.75 | 4 / ₹-1,526.93 | 4 / ₹-1,803.97 |
| 2026-08-11 | 3 / ₹-1,830.51 | 4 / ₹-2,061.65 | 2 / ₹-472.74 |
| 2026-08-12 | 15 / ₹-2,000.25 | 12 / ₹-1,505.49 | 2 / ₹1,226.41 |
| 2026-08-13 | 6 / ₹-2,129.82 | 7 / ₹-2,245.23 | 3 / ₹-1,156.59 |
| 2026-08-14 | 9 / ₹-1,869.88 | 8 / ₹-1,717.07 | 3 / ₹-700.11 |
| 2026-08-17 | 10 / ₹-1,592.83 | 5 / ₹-2,159.65 | 3 / ₹-1,160.39 |
| 2026-08-18 | 7 / ₹-1,727.74 | 6 / ₹-1,713.92 | 1 / ₹-297.27 |
| 2026-08-19 | 7 / ₹-2,071.81 | 9 / ₹-1,685.74 | 1 / ₹-325.30 |
| 2026-08-20 | 9 / ₹-1,759.33 | 9 / ₹-1,733.26 | 3 / ₹-1,249.92 |
| 2026-08-21 | 12 / ₹-1,568.03 | 17 / ₹-1,972.70 | 4 / ₹-530.09 |
| 2026-08-24 | 11 / ₹-1,763.18 | 5 / ₹-1,691.30 | 2 / ₹-226.24 |
| 2026-08-25 | 12 / ₹-1,892.14 | 5 / ₹-1,761.65 | 1 / ₹-196.89 |
| 2026-08-26 | 14 / ₹-1,744.75 | 13 / ₹-2,016.92 | 8 / ₹-1,580.22 |
| 2026-08-27 | 14 / ₹-1,844.25 | 5 / ₹-2,000.39 | 0 / ₹0.00 |
| 2026-08-28 | 14 / ₹-2,229.91 | 19 / ₹-1,900.96 | 3 / ₹-790.73 |

A and B were net-negative on every evaluation date.  C was net-positive only on
August 3 and August 12.  The three worst symbols were ETERNAL (A ₹-3,954.00;
B ₹-2,446.65), ADANIPOWER (A ₹-2,195.15), and TATASTEEL (C ₹-1,594.50), but
losses were dispersed: no single name or session explains the aggregate loss.

## Comparison with Phase 6A

The earlier five-session `verified2` experiment remains separate: A ₹-9,206 net
on 30 closed trades, B ₹-8,574 on 38, and C ₹-8,437 on 17.  It is not pooled with
this different 20-session evaluation.  Directionally, Phase 6E repeats the
negative result before and after costs, with more observations and fixed dated CAS
classification; it does not establish independent replication or statistical
significance.

## Limitations and next research boundary

- This is a closed-bar OHLC reconstruction using an optimistic-labelled
  next-observation `bar_open_zero_latency_proxy`, not a verified broker execution.
- Intrabar MFE/MAE and stop ordering are unknowable from one-minute OHLC alone.
- `BROKER_REALISTIC_SENSITIVITY` was not used because its timing issue remains
  unresolved.  CAS auction prices were never used as continuous-market exits.
- The current April snapshot cohort has survivorship/snapshot bias; it is not a
  point-in-time all-NSE eligibility universe.  Results cannot be generalized to
  all NSE EQ stocks.
- The KOTAK corporate-action circulars were not locally preserved, and no
  TradingView timestamped per-bar export/alert stream was supplied for parity.
- The local detailed SQLite journal, CSV trades/events, normalized source and
  immutable manifest are intentionally not published; this report contains
  reconciled aggregates only.

The evidence is enough to justify *separate predeclared research*, not a strategy
change: (1) leader-first selection using causal momentum/relative-strength/volume
data, and (2) an execution-model study of stop/profit handling that explicitly
models intrabar ambiguity.  Neither was run or optimized here.

## Verification and safety

- Focused CAS/calendar/dated-identity tests: **25 passed**.
- The Phase 6E runner refused to overwrite its immutable output and was executed
  once successfully after a pre-run path validation failure; no second replay was
  performed.
- Repository full offline suite: **passed** through `scripts/test_offline.sh -q`
  (the wrapper cleaned its isolated temporary test root, its success condition).
  A direct `pytest -q` attempt is not a valid substitute: it failed collection
  because the repository's offline guard requires that wrapper's environment.
- No Kite API request, order, modification, cancellation, GTT operation, Discord
  send, live-paper activation, production-service change, deployment, merge, or
  feature-branch push occurred.

# Phase 7 — leader-first momentum research design

## Decision

No leader-first replay, strategy implementation, market-data request, or production
change was performed in this phase.  This document pre-registers one independent
candidate and describes the minimum evidence required to test it fairly against
the frozen UT_SYNC A/B/C baselines.

The candidate is deliberately independent of UT_SYNC: UT signals neither create,
rank, veto, nor exit its positions.  UT_SYNC v2.7a, QQC CLEAR v6, A/B/C and the
existing Strict Emerging Leader service remain unchanged.

## What the existing evidence does and does not say

The authoritative Phase 6E journal (`b233add28d212c0008d626d82ff1da2b`) contains
435 resolved reconstructed trades over the predeclared August 3–28 window.  The
findings below are descriptive and were not used to tune the candidate rules.

| Evidence | A | B | C |
|---|---:|---:|---:|
| Closed trades | 193 | 179 | 63 |
| Fresh UT: net P&L / average net R | ₹-24,365.78 / -1.531 | ₹-14,603.37 / -1.469 | ₹-9,809.11 / -1.285 |
| Continuation: net P&L / average net R | ₹-13,999.85 / -1.162 | ₹-23,068.73 / -1.204 | ₹-4,849.43 / -0.887 |
| Trades with zero OHLC MFE | 106/193 (54.9%) | 95/179 (53.1%) | 32/63 (50.8%) |
| Hard-stop exits | 121/193 | 106/179 | 31/63 |
| Median MFE / MAE | 0.00R / -1.08R | 0.00R / -1.04R | 0.00R / -1.00R |

The repeated zero median MFE means that at least half of the closed reconstructed
trades never showed a positive one-minute high excursion above their simulated
entry before exit.  Together with 258 hard stops (59.3% of all closed trades),
this is consistent with entries arriving before sustained directional movement or
into failed/short-lived moves.  It does **not** prove that a different entry or
stop rule would have worked: one-minute intrabar order is unknown, and the fills
are a next-bar-open proxy rather than broker fills.

### Entry context, separated by setup

For actual Phase 6E fills, the table expresses entry distance from VWAP and the
prior breakout high in units of the actual initial stop distance.  These are
observations, not proposed cut-offs.

| Variant / setup | Median entry–VWAP | Median entry–prior-high | Median RVOL | Median RS % | Zero-MFE trades | Hard stops |
|---|---:|---:|---:|---:|---:|---:|
| A fresh UT (112) | 1.36R | 0.52R | 0.68 | 0.20 | 64 | 73 |
| A continuation (81) | 1.35R | 0.57R | 0.82 | 0.41 | 42 | 48 |
| B fresh UT (71) | 1.55R | 0.56R | 0.79 | 0.19 | 42 | 44 |
| B continuation (108) | 1.49R | 0.54R | 1.22 | 0.49 | 53 | 62 |
| C fresh UT (39) | 2.82R | 0.73R | 3.20 | 1.18 | 22 | 20 |
| C continuation (24) | 2.22R | 0.57R | 2.69 | 1.04 | 10 | 11 |

The stricter C sample had stronger recorded RVOL/RS but was also more extended
from VWAP.  C remained gross-negative.  Therefore neither high RVOL/RS alone nor
the observed extension pattern is evidence of an edge.  The Phase 6A five-session
forensics are directionally consistent: its mean entry distance above VWAP was
0.44% (A), 0.40% (B), and 0.90% (C), and hard/gap stops dominated low win rates.

### Acceptance, fills, and risk controls

Phase 6E had 66,251 common base observations and 8,804 baseline-passing
opportunities.  An accepted decision was not necessarily a fill: independent
portfolio state could prevent a pending entry from being executed.

| Variant | Accepted | Filled | Unfilled | Main unfilled reasons |
|---|---:|---:|---:|---|
| A | 828 | 193 | 635 | 572 max-open-positions; 58 sizing/capital/risk/liquidity; 5 daily-loss-limit |
| B | 336 | 179 | 157 | 133 max-open-positions; 22 sizing/capital/risk/liquidity; 2 daily-loss-limit |
| C | 63 | 63 | 0 | none |

The three-position cap was reached in each independent portfolio.  Decision-level
`DAILY_LOSS_LIMIT` rejections occurred after the portfolio guard activated (A on
all 20 sessions, B on all 20, C on four); the large counts are repeated later
candidate observations, not thousands of separate loss-limit events.  The journal
also records the small number of already-accepted pending entries that became
unfilled for that limit.  This is consistent with the intended fail-closed risk
behavior, but it is not a substitute for a dedicated risk-engine proof in a future
candidate implementation.

## Input-quality audit

The Phase 6E manifest's `input_quality_warning_count=1001` is an in-memory
reporting cap, not the full normalized-source count.  The source SQLite quality
table contains **11,261** retained warnings, all `ZERO_VOLUME_OBSERVATION`:

- 11,250 are NIFTY 50 index candles: 30 cached sessions × 375 minutes.  Index
  price candles legitimately have zero/unavailable volume and were explicitly
  accepted for benchmark-relative-strength calculations.
- 11 are isolated stock candles: SOLARINDS (3), HAVELLS (3), LODHA (2), and one
  each for INDHOTEL, PIDILITIND, and APOLLOHOSP.
- There were zero retained invalid-OHLCV, parse, off-session, out-of-order, gap,
  duplicate-identical, or duplicate-conflict warnings in the normalized source.

The 11 stock flags occur on complete sessions and retain valid flat OHLC prices;
they did not change the 360/375 session-completeness decision.  They can still
affect volume-derived measurements, so the future leader candidate must reject a
signal whose current bar has non-positive volume and must exclude invalid historical
same-minute volume observations from its RVOL denominator.  This phase did not
attribute Phase 6E P&L to those 11 observations and does not remove any trade.

## Pre-registered candidate: `LEADER_FIRST_V1`

The following fixed specification is selected before any leader-first outcome is
examined.  It is a research candidate, not a deployment specification.

### Daily universe and data eligibility

1. At the prior session close, form the point-in-time NSE EQ universe using that
   date's official CM-MII security master.  Require exact EQ series, active dated
   identity/ISIN mapping, and a valid cached instrument identity.  A current NSE
   list must not substitute for history.
2. Require 20 prior valid sessions of one-minute stock and NIFTY data.  Require at
   least 15 non-missing same-minute historical volume observations for RVOL.
3. Require prior-session close ≥ ₹50 and median daily traded value over the prior
   20 sessions ≥ ₹5 crore, calculated only from already-complete sessions.  A
   symbol failing either rule is excluded for the whole following session.
4. Require a dated CAS classification and a complete expected continuous session:
   360 09:15–15:14 candles for CAS and 375 09:15–15:29 for non-CAS.  Unknown
   classification, identity conflict, benchmark gap, invalid price, or stock gap
   fails closed.

These liquidity/price values are frozen engineering eligibility rules, not values
fit to Phase 6A/6E outcomes.

### Causal signal and ranking

The first eligible signal time is the close of the 09:25 candle; a decision is
observable one minute later and may fill only at the following observed bar open.
No current partial five-minute bar, future volume, or future price may enter a
calculation.

At each completed one-minute bar from 09:25 through 14:45 IST, a stock is a
leader candidate only if all conditions hold:

1. `RS_5m > 0` and `RS_5m > RS_5m[t-3]`, where RS is stock return from its 09:15
   open less aligned NIFTY 50 return from its 09:15 open.
2. Same-minute RVOL ≥ 1.50, using the mean of 15–20 prior valid sessions at that
   minute; current-day data and zero-volume historical entries are excluded.
3. Close is above session VWAP, above the most recently completed five-minute
   EMA20, and that confirmed EMA20 exceeds its prior completed five-minute value.
4. The completed candle is bullish; its close location is at least 0.70; and its
   range is at most 1.80× ATR14.  Current-bar volume must be positive.
5. A five-minute opening range is fixed from 09:15–09:19.  The candidate must be
   either: (a) the second consecutive completed close strictly above OR high; or
   (b) the first qualifying retest within 15 completed minutes after a confirmed
   breakout, with low no more than 0.15 ATR14 below OR high and close above OR
   high.  If both routes occur, the earlier observable route is used once.
6. Define the reference stop as the tick-floored minimum of the qualifying bar low
   and preceding completed bar low, minus 0.10× ATR14.  Require planned risk
   `(reference close - stop)/ATR14` in [0.35, 1.25], close no more than 0.75 ATR14
   above OR high, and close no more than 1.50 ATR14 above VWAP.  Otherwise reject
   as `NO_CHASE`.

Candidates at an identical observable timestamp are ranked by a fixed score:
50% cross-sectional percentile of `RS_5m`, 30% percentile of RVOL, and 20%
percentile of five-minute return from 09:15.  Higher score wins; ties break by
higher RS, then higher RVOL, then lexical NSE symbol.  Ranking is recomputed only
from eligible symbols at that timestamp.  The portfolio considers ranks in order;
at most three positions may be open and only one attempted entry is permitted per
symbol per session.  No UT signal is read anywhere in this pathway.

### Execution, exits, and risk

- Primary simulation: the same `FROZEN_ORIGINAL` next-observation bar-open proxy,
  ₹300,000 initial capital, ₹500 reference risk/trade, three-position cap, ₹1,500
  daily realized-plus-conservative-open-loss guard, participation cap, frozen fee
  schedule, 10 bps total spread proxy, and 5 bps/side slippage as A/B/C.
- Shares are recomputed from actual simulated entry and reference stop.  A missing
  next observation is `UNFILLED`, not a fill.
- Hard stop is active immediately; gaps and same-bar ordering use the existing
  adverse/conservative model.  At +1R MFE, lock at least +0.25R effective next
  bar; at +1.5R, trail peak minus 2× ATR14 effective next bar; stops never loosen.
- A completed close below VWAP **and** below the confirmed five-minute EMA20
  schedules an exit for the next observation.  Positions also follow the same
  frozen EOD convention and CAS censoring used in Phase 6E; auction prices are not
  ordinary fills.  `BROKER_REALISTIC_SENSITIVITY` remains excluded until repaired.
- No entry after 14:45 IST, no same-bar re-entry after exit, no overnight carry;
  a position without a credible same-session exit is reported incomplete/censored.

## Independent evaluation readiness

July and August are development evidence, not holdout data.  The earliest proposed
independent window is **2026-09-01 through 2026-09-28** (20 cached trading
sessions), with **2026-08-17 through 2026-08-28** as a ten-session indicator/RVOL
warm-up.  These dates are selected chronologically, not from leader-first results.

The period is **not ready to run**.  Although the existing stock cache has 2,468
CSV files on September 1 (and 2,495 on August 31) and the validated normalized
NIFTY cache extends through October 9, required historical classification and
eligibility evidence is absent locally.  The present Phase 6C master evidence ends
at August 28, and a broad point-in-time EQ universe/liquidity record has not been
frozen.

Minimum verification before any replay:

1. Acquire/validate the dated official CM-MII master for each of the 20 evaluation
   dates (September 1–11, 15–18, 21–25, and 28), using the same schema, hash,
   effective-date, EQ/ISIN/token and CAS-field controls as Phase 6C.
2. Build a dated CAS/identity ledger for every selected stock-session; retain
   unresolved rows rather than infer non-CAS from a 15:29 or 15:14 cutoff.
3. Freeze the September 1 universe from the prior-date master and calculate the
   20-day prior-session liquidity screen without using September outcomes.
4. Validate every selected stock session and every benchmark timestamp, including
   zero-volume index semantics, duplicate conflicts, 360/375 calendars, and the
   15 prior same-minute RVOL samples.
5. Hash the exact universe, master files, candles, benchmark, configuration and
   candidate-specification source before viewing outcomes.  If fewer than 20
   common calendar-valid sessions remain, stop rather than shorten the sample.

No acquisition is authorized by this report.

## Fair test protocol and failure criteria

When the prerequisites are separately approved and complete, run one immutable
streaming replay over the frozen September window.  Leader-first and frozen A/B/C
receive identical raw candles, benchmark, fees, fill proxy, capital and portfolio
limits; each has independent positions and accounting.  A/B/C parameters and ORB
behavior remain exactly frozen.

Predeclare reports for each strategy: candidate/accepted/filled/unfilled/closed/
censored counts; gross and net expectancy/R; win rate, profit factor and realized
drawdown; exit and stop reasons; MFE/MAE and holding time; symbol/day concentration;
missed leaders and failed/rejected breakouts; fresh/continuation results for A/B/C;
and all data exclusions.  Report a primary next-open result plus non-optimizing
execution sensitivities: one additional full-bar entry delay and an adverse-fill
case with twice the spread/slippage assumption.  Never select the better result.

The candidate fails the research gate if it lacks complete dated eligibility,
requires any lookahead or unverified CAS assumption, has material unresolved EOD
censoring, or is not gross-positive with profit factor above 1.0 on the frozen
holdout.  Passing those descriptive hurdles would still not prove profitability,
statistical significance, broker execution, or TradingView parity; it would only
justify a separately authorized replication on a later holdout.  Net results,
drawdown, adverse-delay performance, and concentration must all be disclosed even
if the primary result is favorable.

## Safety and publication

This phase performed only local read-only artifact inspection and documentation.
No Kite API request/order/GTT/modification/cancellation, Discord message, live
paper activation, production-service change, deployment, merge, or feature-branch
push occurred.  Raw candles, SQLite journals, and detailed trade/event ledgers are
not included in the public documentation export.

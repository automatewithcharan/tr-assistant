# Phase 7A — leader-first holdout evaluation readiness

## Decision

**NO — do not run a September leader-first or A/B/C profitability replay.**

The chronological calendar can be corrected without looking at outcomes, but the
isolated research worktree does not presently contain the required September stock
candles or the dated CM-MII evidence needed to create a point-in-time universe and
CAS calendar.  In addition, the current leader-first prototype is an opportunity
prototype, not an integrated streaming experiment with its predeclared exit policy.

This is an offline inspection only.  No price download, Kite call, order/GTT,
paper activation, Discord message, replay, deployment, merge, or production-service
change occurred.

## 1. Frozen calendar correction

The proposed independent chronological evaluation is the first twenty actual NSE
weekday sessions beginning in September, with no symbol or outcome selection:

| Role | Dates | Sessions |
|---|---|---:|
| Daily-liquidity/RVOL history | 2026-08-04, 05, 06, 07, 10–14, 17–21, 24–28, 31 | 20 |
| Evaluation | 2026-09-01–04, 07–11, 15–18, 21–25, 28–29 | 20 |

September 1–28 has only 19 trading sessions.  NSE's 2026 holiday circular lists
Monday 2026-09-14 (Ganesh Chaturthi) as a trading holiday; including the next
chronological session, Tuesday 2026-09-29, makes the window twenty sessions.
This replaces, rather than selectively shortens, the earlier September 1–28
proposal.  The official CM-MII filename convention is
`NSE_CM_security_ddmmyyyy.csv.gz` and is published daily by NSE.

Sources: [NSE 2026 holidays circular](https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf) and
[NSE CM-MII dissemination circular](https://nsearchives.nseindia.com/content/circulars/MSD60315.pdf).

The operational intraday warm-up is distinct from the twenty daily historical
sessions: the Phase 7 rules need at least 60 earlier in-session minute bars for E4
resistance, 14 bars for ATR, and 21 completed five-minute buckets for EMA20.  A
session is not eligible until those causal intraday observations exist.  The 20
prior sessions are required for liquidity/RVOL evidence, with at least 15 valid
same-minute volume observations.

## 2. Data and point-in-time readiness

### Locally retained inputs

| Input | Offline observation | Status |
|---|---|---|
| Normalized NIFTY 50 cache | 25,500 rows over 68 complete sessions, 2026-07-06–2026-10-09 | Available |
| NIFTY 50 on each proposed 20 evaluation day | 375 timestamps, 09:15–15:29 IST | Complete |
| NIFTY 50 on each proposed 20 history day | 375 timestamps, 09:15–15:29 IST | Complete |
| Phase 6E normalized source | 79 symbols, 865,650 rows, 2026-07-20–2026-08-28 | Retained August-only evidence |
| Phase 6A normalized source | 79 symbols, 444,375 rows, 2026-07-13–2026-07-31 | Retained July-only evidence |
| Isolated `results/Data/scalable_cache` | Directory exists but contains no stock CSVs | Empty |
| September stock candles in isolated worktree | 0 rows/files found | Missing |
| September CM-MII masters | None found locally | Missing |

The benchmark's zero volume is valid for relative-strength price calculations and
is not a rejection by itself.  It does not substitute for the missing stock-side
price/volume inputs.

The retained August SQLite source covers only the prior-history tail, and only a
previous 78-stock exploratory cohort.  It cannot establish an all-NSE-EQ
September universe, calculate each September stock's RVOL, or drive the proposed
evaluation.  Neither a current NSE list nor the available cache filenames may be
used as historical membership truth.

### Required pre-run eligibility ledger

For each stock and session, create and hash a ledger before the replay that joins:

1. the master applicable on the prior trading day, exact trading symbol, `EQ`
   series, ISIN, and reconciled cached instrument identity;
2. an authoritative `ElgbltyClsgAuctnSsn` value, resolved as 360 expected
   continuous minutes (CAS) or 375 (non-CAS), with exact timestamp-set—not merely
   count—validation;
3. prior-session close and trailing 20-session traded value, computed only from
   previously validated sessions, applying the frozen >= Rs50 close and >= Rs5
   crore median daily traded-value screens;
4. at least 15 valid historical same-minute volume observations from those twenty
   sessions; and
5. complete, aligned benchmark minutes.  Invalid prices, identity conflicts,
   zero-volume current fill bars, missing benchmark minutes, unknown CAS status,
   or incomplete stock sessions fail closed.

The cohort cannot yet be frozen because no September stock session can pass that
ledger.  This is a data-availability constraint, not an exclusion based on
trading results.  The eventual cohort remains subject to survivorship and
availability bias: it will be a dated-master-defined NSE EQ population further
conditioned on predeclared liquidity and complete cached data.  Every excluded
symbol-session, censored position, and unresolved identity must remain in the
run manifest and results denominator.

### Minimum public-reference acquisition manifest (not executed)

Existing Phase 6C evidence already locally validates the official CAS field's
schema/value semantics and the prior-trading-day application convention.  The
smallest additional **reference** set is therefore 21 exact public CM-MII HTTPS
GETs—not price downloads and not Kite requests.  Each must be fetched only from
`https://nsearchives.nseindia.com/content/cm/`, validate gzip magic/integrity,
filename date, 120-column schema, field-24 CAS domain `0/1`, immutable SHA-256,
and the previous-trading-day mapping below.  Reject redirects, identity ambiguity,
or a mapping/schema disagreement.

| ID | Master filename/date | Applies to |
|---|---|---|
| P7A-M01 | `NSE_CM_security_28082026.csv.gz` | 2026-08-31 warm-up |
| P7A-M02 | `NSE_CM_security_31082026.csv.gz` | 2026-09-01 evaluation |
| P7A-M03…M11 | `01092026`, `02092026`, `03092026`, `04092026`, `07092026`, `08092026`, `09092026`, `10092026`, `11092026` | 2026-09-02…11 |
| P7A-M12…M15 | `15092026`, `16092026`, `17092026`, `18092026` | 2026-09-16…18, 21 |
| P7A-M16…M20 | `21092026`, `22092026`, `23092026`, `24092026`, `25092026` | 2026-09-22…28 |
| P7A-M21 | `NSE_CM_security_28092026.csv.gz` | 2026-09-29 |

For rows M03–M21, the file date is the immediately preceding actual trading day;
the table compresses only the ID range, not the requirement.  The complete URLs
are mechanically `https://nsearchives.nseindia.com/content/cm/NSE_CM_security_<DDMMYYYY>.csv.gz`.
The already-present Phase 6C masters cover August 4–28 warm-up applicability;
M01 is needed to classify August 31.  After these references validate the
universe, a separate bounded stock-candle acquisition/restore manifest must be
derived from its exact eligible symbol-session set.  No stock-candle request is
authorized or implied by this report.

## 3. Prototype/specification audit

`paper_trading/leader_first.py` is deterministic, offline, and has no broker or
network import.  It accepts completed candles and supplied snapshots, and its
candidate adapter marks the result as a completed-bar reconstruction.  These are
positive safety properties, but the following must be resolved *before* a frozen
backtest; this audit makes no silent strategy change.

| Area | Finding | Consequence |
|---|---|---|
| ORB | The design's `LEADER_FIRST_V1` common gate requires `ORB_HOLD_2` or the specified retest.  Neither parent function applies ORB. | Material specification conflict; approve/encode one source of truth with tests. |
| Parent independence | Momentum Breakout + Pullback and RS + Market Pullback are separate functions and do not read UT signals. | Pass, subject to integration. |
| E1 | Uses the parent completed-bar timestamp as the next-boundary proxy. | Correctly labelled proxy, not a proven broker fill. |
| E2 | `confirmation_expiry_bars=3` is declared but unused.  Code tests only the immediately next bar; a missing next bar remains pending until the 15-minute parent expiry. | Timing/expiry differs from the published “next bar; reject after 3 bars” contract. |
| E3 | Searches only later supplied bars, up to the fixed 15-bar expiry, and is diagnostic/executable-plan separate. | Causal within a correctly session-bounded caller. |
| E4 | Uses fewer than the required 60 prior bars when fewer are supplied; empty history becomes infinite room. | Must fail closed until 60 same-session, completed prior bars exist. |
| Session boundaries | Parents do not verify that their 5/3 or 3/1 bars belong to one market date or have contiguous one-minute timestamps. | A caller could accidentally form a setup across a session/gap. |
| Snapshot provenance | `eligible`, RVOL sample quality/count, aligned benchmark return and confirmed EMA are supplied booleans/numbers; the module does not prove their dates/timestamps. | Required eligibility/indicator builder is absent. |
| Structural risk | `Portfolio.on_bar_open` recomputes quantity from the simulated fill and stop. | Shared paper sizing support exists. |
| Exit policy | `leader_exit_signal` is defined, but `Portfolio.on_bar_range_and_close` schedules only UT/PSAR/EOD exits. | Leader positions would receive the wrong exit policy today. |
| Diagnostics | 30-bar failed-breakout labels are retrospective and do not create trades. | Correct separation, but must remain out of causal entry flow. |

The ORB conflict is especially important: the older frozen `LEADER_FIRST_V1`
section predeclares a two-close ORB hold or bounded retest, whereas the later
prototype contract describes the two parents without applying any ORB condition.
Neither interpretation may be selected after inspecting September outcomes.

## 4. Paper-engine integration readiness

The existing engine provides independent A/B/C portfolios, deterministic
SQLite events/checkpoints, frozen fees/spread/slippage, next-observation
bar-open proxy fills, actual-fill sizing, risk/capital guards, and
`FROZEN_ORIGINAL` CAS-aware censored EOD accounting.  The known
`BROKER_REALISTIC` sensitivity remains excluded.

It does **not** provide a ready leader-first streaming driver.  `run_streaming_replay`
instantiates only A/B/C `StreamingSymbolState` objects and fixed A/B/C candidate
generation.  A leader integration must add, without changing A/B/C:

1. a pre-session universe/calendar/liquidity/RVOL builder and verified snapshot
   provenance;
2. a chronological leader state that evaluates parent opportunities and E1–E4
   plans only when their observations are available;
3. independent named experiment portfolios (and an explicit decision whether
   their capital/risk is separate per entry method or globally shared), with
   deterministic rank-first allocation at each timestamp;
4. the predeclared VWAP-and-confirmed-EMA exit hook, next-observation scheduling,
   CAS EOD censoring, and no session crossing; and
5. journal/checkpoint serialization plus batch/stream/restart equivalence tests
   for leader-specific state, pending plans, expiry, fills, exits, rejections and
   censored positions.

The phrase “independent experiment portfolios” in the Phase 7 design is compatible
with A/B/C-style separate accounting, but “global capital and risk controls” is
not yet implemented across E1–E4.  The implementation decision must be frozen
before outcomes: either a single shared leader portfolio with method-labelled
plans, or separate capital accounts clearly reported as non-competing experiments.

## 5. Verification performed

Focused offline regression suite:

```
87 passed in 11.73s
tests/paper_trading/test_leader_first.py
tests/paper_trading/test_paper_engine.py
tests/paper_trading/test_streaming_checkpoint.py
tests/paper_trading/test_phase5e_cas_execution.py
tests/paper_trading/test_cas_session_calendar.py
```

The tests establish existing prototype, engine, checkpoint and CAS regression
behaviour.  They do not establish the missing leader streaming integration,
September eligibility, stock cache completeness, or the identified E2/E4/ORB
discrepancies.

## 6. Shortest safe path to one frozen evaluation

1. Separately approve and validate the 21-reference manifest above; do not
   download prices in that step.
2. Restore/acquire stock candles only through a second exact manifest generated
   from the dated-master universe, covering August 4–September 29 as needed for
   every predeclared eligible symbol.  Validate identity, 360/375 timestamp sets,
   OHLC/duplicates and benchmark alignment.
3. Freeze and hash the complete eligibility ledger, symbols, exclusions, dates,
   data, code and configuration before opening any performance output.
4. Resolve the ORB specification conflict and repair the leader-only integration
   gaps above with deterministic tests.  Preserve A/B/C source, parameters,
   sizing, risk and frozen-original execution unchanged.
5. Only after an explicit separate approval, run exactly one chronological
   September 1–29, 20-session streaming replay and report every rejection,
   unfilled plan, incomplete/censored position, loss and exclusion.

## Protected-service status

The audit made no writes to production services or configurations.  It invoked no
broker/Kite method and made no market-data request.  No real Kite order,
modification, cancellation, GTT, Discord notification, deployment, merge or
trading activation occurred.

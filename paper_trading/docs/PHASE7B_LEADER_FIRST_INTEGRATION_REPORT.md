# Phase 7B — leader-first integration and September-readiness repairs

## Decision

**NO — the September profitability replay is not technically data-ready.**

The isolated leader-first implementation is now a paper-only streaming experiment
with deterministic E1–E4 portfolio integration.  However, an authoritative
September point-in-time universe/CAS ledger and a validated stock-session matrix
are still absent.  The external stock cache is present and read-accessible, but
its filenames alone cannot prove identity, CAS status, timestamp completeness,
liquidity eligibility, or valid RVOL inputs.

No profitability replay, Kite/API request, market-data download, broker action,
Discord send, production-service change, deployment, scheduling, merge, or live
paper activation occurred.

## 1. September source-cache inventory

Phase 7A inspected only the isolated worktree's ignored `scalable_cache` directory.
The earlier reported cache exists instead in the original repository's external
historical directory, `results/Data/kite_history_backfill/minute`.  It is
user-owned and read-accessible from the isolated worktree; this phase made no
writes and copied no data into Git.

| Period | Sessions | Per-session CSV range | Common filenames within period | Inventory bytes | Metadata-inventory SHA-256 |
|---|---:|---:|---:|---:|---|
| Prior-session history, 2026-08-04–31 | 20 | 2,423–2,510 | 1,824 | 805,930,516 | `2c93b5417b7da70940f4801cc8b0c7d0f4ac8cec02fc996eb47e6dc44a21e1c1` |
| Evaluation, 2026-09-01–29 | 20 | 2,456–2,811 | 1,865 | 846,437,294 | `c01b06627e17ef8ba05c6a77da645cdff6621d8f434bc8b598d7c53caf6b1257` |
| Both periods | 40 | — | **1,658** | 1,652,367,810 | `4e2c14498388ad053149efbab19a1fda37e4207d94f5764e5e2541d608dc02e5` |

The hashes are deterministic metadata inventories over sorted
`session|filename|byte-size` records, not a claim that every raw CSV body has
been byte-hashed.  The combined intersection contains 66,320 candidate
symbol-session files (1,658 × 40).  It is an availability inventory, **not an
eligible universe**.  `NIFTY 50` remains separately available in the normalized
research benchmark cache with 375 IST candles for each proposed warm-up and
evaluation date.

The exact frozen chronological calendar remains:

- history/RVOL/liquidity evidence: 2026-08-04, 05, 06, 07, 10–14, 17–21,
  24–28, 31;
- evaluation: 2026-09-01–04, 07–11, 15–18, 21–25, 28–29.

September 14 is the official NSE holiday; September 29 is the mechanical,
pre-outcome extension to twenty sessions.  The external cache therefore resolves
the **location/visibility** discrepancy, not the data-quality blocker.

### Remaining source-data gates

Before any historical evaluation, construct a read-only, hashable symbol × session
ledger that joins the external cache to dated masters and validates exact timestamp
sets, OHLC, duplicates, `EQ` identity/ISIN/token, CAS status, prior close/liquidity,
and historical same-minute RVOL samples.  A row is usable only if its dated master
classification gives an exact 360-minute CAS or 375-minute non-CAS continuous
session.  Unknown classifications, partial files, invalid prices, current
zero-volume fill bars, missing aligned benchmark minutes, or identity conflicts
remain exclusions; they must be retained in the denominator rather than silently
dropped.

## 2. Frozen-contract decisions and repairs

The Phase 7 design's common causal gate unambiguously applies opening-range logic
to every leader candidate.  Phase 7B therefore applies it to **both** Momentum
Breakout + Pullback and RS + Market Pullback parents: a parent requires either the
second completed close strictly above OR high or the predeclared bounded/reclaimed
retest.  This is a fidelity repair, not a threshold change.

| Contract element | Frozen interpretation | Repair / verification |
|---|---|---|
| Parent strategies | The two parents remain independent of UT_SYNC and of each other. | Retained separate parent functions and overlap/ranking semantics. |
| Common ORB gate | `ORB_HOLD_2` **or** qualifying 15-minute retest is mandatory for both parents. | Added causal OR fields to `LeaderSnapshot`; fail closed on missing ORH or neither route. |
| E1 | Parent completed signal; next-observation bar-open proxy only. | Unchanged and explicitly reconstruction-only. |
| E2 | Evaluate the immediately next completed, contiguous minute only.  If that observation is absent, retain `PENDING` through the fixed three-minute deadline, then record `CONFIRMATION_EXPIRED`; a present next bar below trigger is `CONFIRMATION_FAILED`. | Replaced accidental 15-minute parent-expiry behaviour; test covers absent-next-bar expiry. |
| E3 | Retest/reclaim only on a later completed bar within 15 minutes. | Retained; no future bar creates an entry. |
| E4 | Require exactly 60 **prior**, complete, contiguous, same-session minutes; no previous-session or short-history substitute. | Empty/short resistance history now rejects with `ROOM_OR_EXTENSION_FAIL`. |
| Parent windows | 5/3/recovery and 3-prior/recovery bars must be same-session and contiguous. | Added fail-closed continuity check. |
| Stops/no-chase | Structural stops, risk range, and extension limits are unchanged. | Retained; actual fill quantity still recomputes inside the portfolio. |
| Diagnostics | 30-bar failed-breakout labels are descriptive only. | Retained outside all causal entry paths. |

No September price outcome was inspected in resolving these implementation defects.

## 3. Paper-only streaming integration

Added `paper_trading/leader_streaming.py`, a standalone offline adapter with no
broker or network import.  It accepts completed stock candles and an explicitly
provided causal `LeaderSnapshot` provider.  The caller must provide validated
universe/CAS/liquidity/RVOL/benchmark evidence; the adapter never infers it from a
current list, candle shape, or future observations.

The adapter provides:

1. chronological leader state, parent generation, E1–E4 pending-plan expiry and
   deterministic shared parent ranking;
2. independent `E1_IMMEDIATE`, `E2_CONFIRMATION`, `E3_RETEST`, and `E4_CONTEXT`
   paper portfolios, each retaining its own capital/risk accounting exactly as an
   experiment portfolio (they are not a combined live-capital claim);
3. shared existing simulated-fill sizing, participation, fees, adverse
   spread/slippage, stop ordering and capital/risk controls;
4. candidate, rejected-plan, accepted-decision, unfilled, filled and incomplete
   trade journal records;
5. leader-specific close-below-VWAP-and-confirmed-EMA next-observation exit,
   while frozen A/B/C preserve their UT/PSAR exits unchanged; and
6. `FROZEN_ORIGINAL` CAS-aware incomplete-position handling plus checkpoint
   serialization/restoration.

`BROKER_REALISTIC` is not selectable by the leader runner.  This preserves the
known-invalid broker-realistic CAS sensitivity as disabled research code rather
than treating it as an executable path.

## 4. Deterministic verification

Focused leader/engine/CAS/checkpoint suite: **90 passed in 14.13s**.

Included coverage verifies common ORB routing, E2 expiry, E3 bounds, E4
same-session 60-bar rejection, structural-stop candidate adaptation, ranking,
leader exit scheduling, actual simulated-fill handling, CAS behaviour and an
uninterrupted-versus-checkpoint-resumed leader result equivalence test.  The
existing A/B/C regression tests pass unchanged in that focused suite.

The full approved offline repository wrapper, `scripts/test_offline.sh -q`, also
**passed**.  Its isolated temporary test root was removed by the wrapper's defined
success path.

## 5. Required public-reference manifest (not executed)

The Phase 6C locally retained CAS schema evidence remains valid.  The following
21 public NSE CM-MII references are the exact minimum additional dated masters;
each URL is
`https://nsearchives.nseindia.com/content/cm/NSE_CM_security_<DDMMYYYY>.csv.gz`.
They must be validated for gzip integrity, filename/date, schema, field-24 CAS
domain, source hash, prior-trading-day applicability and exact `EQ`/ISIN/token
join before they classify any cache row.

| ID | DDMMYYYY | Applicable session |
|---|---|---|
| P7A-M01 | 28082026 | 2026-08-31 |
| P7A-M02 | 31082026 | 2026-09-01 |
| P7A-M03 | 01092026 | 2026-09-02 |
| P7A-M04 | 02092026 | 2026-09-03 |
| P7A-M05 | 03092026 | 2026-09-04 |
| P7A-M06 | 04092026 | 2026-09-07 |
| P7A-M07 | 07092026 | 2026-09-08 |
| P7A-M08 | 08092026 | 2026-09-09 |
| P7A-M09 | 09092026 | 2026-09-10 |
| P7A-M10 | 10092026 | 2026-09-11 |
| P7A-M11 | 11092026 | 2026-09-15 |
| P7A-M12 | 15092026 | 2026-09-16 |
| P7A-M13 | 16092026 | 2026-09-17 |
| P7A-M14 | 17092026 | 2026-09-18 |
| P7A-M15 | 18092026 | 2026-09-21 |
| P7A-M16 | 21092026 | 2026-09-22 |
| P7A-M17 | 22092026 | 2026-09-23 |
| P7A-M18 | 23092026 | 2026-09-24 |
| P7A-M19 | 24092026 | 2026-09-25 |
| P7A-M20 | 25092026 | 2026-09-28 |
| P7A-M21 | 28092026 | 2026-09-29 |

No public file, stock candle, or Kite endpoint was downloaded in Phase 7B.  After
the master-only acquisition, the validated ledger—not current membership or
performance—will determine the smallest exact stock-cache repair manifest.

## 6. GitHub-publication reconciliation

The documentation remote was not divergent in the unsafe two-head sense: its
current head `20ebf5f` is an ancestor of the retained local sanitized-review head
`ec82de7`.  This indicates a stale/rolled-back remote branch, not a merge conflict.
Publication may therefore be restored only by a verified fast-forward from a fresh
worktree based on the remote head.  The publication contains sanitized Markdown
only; no cache, source code, raw journal, authentication material or production
details are eligible.

## 7. Final readiness

| Dimension | Status |
|---|---|
| Frozen A/B/C definitions | Unchanged |
| Leader candidate/exit engineering | Ready for a separately authorized offline run once data eligibility exists |
| September external stock cache | Present/read-accessible, but not yet historically validated |
| Dated September universe/CAS evidence | Missing; 21-master manifest pending separate authorization |
| September replay | **NO** |

The shortest defensible path is: validate the 21 masters; build the cache-backed
identity/CAS/completeness/liquidity/RVOL ledger; freeze and hash its cohort and
exclusions; then seek separate approval for exactly one chronological replay.

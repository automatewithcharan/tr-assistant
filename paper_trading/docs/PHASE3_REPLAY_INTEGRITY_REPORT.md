# Phase 3 replay integrity and scalability report

Date: 2026-10-10  
Worktree: `${PAPER_WORKTREE}`  
Branch: `codex/ut-sync-abc-paper-20261010`  
Parent commit: `6b442e2b687f38b0ef503899f151874ae53ed8b0`  
Scope: correctness and readiness only; no profitability replay or rule optimization.

## Outcome

The documented next-day EOD fill defect is fixed. A 15:14 candle-start closes at 15:15 and is the EOD **decision observation**, not an executable exit price. The earliest valid OHLC replay fill is the open of a liquid 15:15 candle-start in the same NSE session. If no such same-session execution observation exists, the position is censored as an `INCOMPLETE_TRADE`; no next-day price is used and the record is excluded from resolved-trade expectancy.

Valid-price zero-volume rows are now retained as genuine no-trade observations. They update price-only indicators, contribute exactly zero (never invented volume) to volume-aware indicators, and cannot fill entries, exits, or stops. Negative volume and invalid/non-positive OHLC remain rejected. Missing timestamps remain missing. Duplicates are independently identified and globally de-duplicated across overlapping files, with conflicting duplicates warned and the first deterministic source retained.

No A/B/C threshold or signal predicate was optimized. The only candidate-quality change removes a lookahead defect: a gap later in a session no longer retroactively invalidates earlier signals; eligibility fails closed from the first causally observed leading/internal gap onward.

## Root causes and fixes

| Defect | Root cause | Fix and audit evidence |
|---|---|---|
| Next-day EOD fill | `pending_exit_reason=EOD` survived the date change and `on_bar_open` accepted the next symbol bar regardless of date | Both the global session boundary and `on_bar_open` enforce the market date. An unresolved position is removed into SQLite `incomplete_trades` with reason `SESSION_BOUNDARY_NO_SAME_DAY_EXIT`. |
| Fabricated resolved expectancy | The old end-of-data path only emitted `EOD_EXIT_UNVERIFIED`, leaving the position open | Incomplete trades have no exit price, P&L, R, or `trades` row. Entry cash/fee accounting is reversed solely to keep subsequent portfolio capacity neutral; the original entry and censor event remain in the journal. |
| Stale entry fills | Fill delay counted the next available bar, even after a gap or date change | Pending entries now require the exact configured chronological minute in the same session; otherwise `NO_TIMELY_SAME_SESSION_OBSERVATION` or boundary expiry is emitted. |
| Zero volume treated as corrupt | CSV validation rejected every zero-volume equity row | Valid OHLC with volume zero is retained and tagged `ZERO_VOLUME_OBSERVATION`; the execution layer requires positive volume and minimum turnover. |
| Cross-file duplicates | De-duplication existed only inside one CSV | Merge uniqueness is enforced on `(symbol, bar_start)` with separate identical/conflicting warnings. |
| Date leakage | Directory-name filtering did not filter actual CSV rows | `load_csv`/`merge_csvs` now apply inclusive row-level start/end dates. |
| Gap lookahead | One later gap placed the entire market date in `bad_session_dates` | Candidate quality is causal: 09:15 must be present, and eligibility becomes false only after the observed discontinuity. |
| Timestamp ordering memory | All observation tuples were duplicated and globally sorted | A deterministic `(bar_end, symbol)` k-way heap merge now uses O(symbols) incremental observation-order memory. Same-timestamp processing is phased: opens/exits, range/close, all marks/equity, then candidates. |

## Session-quality matrix

The source evidence files were read completely and retained unchanged. Their SHA-256 values are:

- NIFTY 100 matrix: `9ef87ed1b797b210eb17fcb78cb806d63d5a313113d24c65a95c228f27fd8d3f` (6,868 rows).
- NIFTY 500 matrix: `51f3120d83dfe66be7ad87047024b1804b4175d65d6fdbbb4521c3dd10e7616b` (34,068 rows).
- Cohort/session summary: `94fe9f3508ca5c9a023bc3bbbd17853b9e1c524c49b0bd2eaaa82e90b01a9a3a` (136 rows).

After retaining valid zero-volume observations, engine timestamp completeness returns to raw valid-OHLC completeness:

| Cohort | Cells | Before full / tail / partial / missing | After full / tail / partial / missing | Zero-volume observations restored |
|---|---:|---:|---:|---:|
| NIFTY 100 | 6,868 | 1,589 / 4,465 / 286 / 528 | 1,677 / 4,663 / 0 / 528 | 3,860 |
| NIFTY 500 | 34,068 | 10,863 / 9,295 / 11,537 / 2,373 | 19,123 / 10,014 / 2,558 / 2,373 | 307,728 |

This is timestamp/price-indicator completeness, not execution completeness. A restored zero-volume minute is never a fill observation. The machine-readable summary is `evidence/phase3_quality_summary_2026-10-10.csv`.

Benchmark coverage is unchanged: only 10 of 68 observed dates have complete NIFTY 50 one-minute data. This yields 1,010 full / 5,858 missing benchmark cells for NIFTY 100 and 5,010 / 29,058 for NIFTY 500. Strategy C continues to fail closed when the exactly aligned benchmark minute is absent. Five-minute EMA confirmation continues to use only fully completed five-minute buckets.

## Zero-volume indicator semantics

- UT trail, ATR, PSAR, DMI/ADX and CHOP consume the valid OHLC record because it is a timestamped price observation; no future value is substituted.
- VWAP adds zero price-volume and zero volume. If earlier session volume exists, VWAP remains mathematically unchanged; before the first positive-volume minute it remains unavailable.
- RVOL uses observed zero as the current/prior minute value. A zero current minute produces RVOL zero once warm; it does not become missing or receive synthetic volume.
- QQC quantity/quality use OHLC. QQC's volume-weighted term receives exactly zero contribution for that row.
- Entries, next-observation exits, and stop fills require positive volume and configured minimum turnover. A zero-volume OHLC range cannot create a phantom execution.

These semantics classify valid-price/zero-volume rows as no-trade observations. The cache has no exchange trade-id field, so it cannot distinguish vendor-carried last price from every other no-trade representation; that provenance limitation remains explicit.

## EOD and regression cases

Deterministic tests cover: 15:14 close setting the EOD intent; liquid 15:15 same-session next-open fill; zero-volume 15:15 deferral; next-day open censorship; end-of-data censorship; exact-minute pending-entry expiry; restart idempotency; causal internal gaps; prior-session UT rejection; missing benchmark rejection; zero-volume VWAP/RVOL/QQC behavior; and broker mutation tripwires. A multi-day/holiday gap follows the same market-date boundary contract—elapsed wall-clock duration is irrelevant and no next-session fill is permitted.

Resolved and incomplete samples are deliberately separate SQLite tables. Reports must calculate expectancy from `trades` only and report `incomplete_trades` as a separate denominator and data-quality warning.

## Scalability and portfolio ordering

The new chronological merge keeps one heap item per stock and never materializes the global observation list. On a synthetic 500-symbol × 375-minute (187,500 observation) ordering benchmark measured with `tracemalloc`, incremental peak allocation was 57,052 bytes versus 17,488,304 bytes for materialize-and-sort: 306.5× lower for this layer. Streaming and materialized paths produced identical ordered observations and identical replay summaries on the deterministic complete-data fixture.

A/B/C still receive one common candidate object and maintain separate positions, cash, risk, daily-loss state, leg consumption, and trades. No partition independently allocates capital. Within each timestamp, all portfolio marks are updated as one phase before candidate allocation. Simultaneous candidates retain the declared lexicographic symbol tie-break; it is deterministic but economically arbitrary and must be frozen before any profitability study.

Important remaining scaling blocker: candle arrays, per-symbol indicator timelines, and candidate dictionaries are still resident together. The k-way merge removes one large duplicate object graph but does **not** yet establish bounded total memory for 11–12 million NIFTY 500 bars. A safe NIFTY 500 run therefore remains blocked until per-symbol rolling indicator state and candidates are staged/checkpointed by session (preferably SQLite partitions), and resume restores portfolio/checkpoint state rather than recomputing the run. Current event/trade IDs and SQLite uniqueness make reruns duplicate-safe; `SESSION_CHECKPOINT` events provide audit boundaries, but efficient mid-run state restoration is not yet implemented. No NIFTY 100/500 profitability replay was attempted.

## Membership, identities, corporate actions and warm-up

The April 22 files support only an explicitly labelled April-snapshot fixed cohort: 101 and 501 rows, including `DUMMYHDLVR` and `RELINFRA` BE. They do not prove point-in-time membership for every July–October session. NIFTY 100 has 99 current exact identities and two unresolved (`DUMMYHDLVR`, `LTIM`). NIFTY 500 has 492 current exact, six archived-token-only, and three unresolved (`AKZOINDIA`, `DUMMYHDLVR`, `LTIM`); `SCHNEIDER` also has a token conflict. Dynamic membership requires official effective-date snapshots, reconstitution/corporate-action circulars, dated security masters, archived instrument dumps, ISIN successor links, and point-in-time tick sizes.

July 13–24 provides only ten observed warm-up sessions; July 27 is the earliest provisional evaluation date. This is enough only for the configured minimum RVOL sample where exact minutes exist, not proof of converged recursive Pine state. A defensible study should acquire at least 20 complete prior sessions and exclude them from evaluation. Corporate-action adjustment policy remains unresolved and must fail closed at detected discontinuities.

## Minimal backfill manifest (not executed)

The machine-readable approval manifest is `evidence/phase3_minimal_backfill_manifest_2026-10-10.csv`.

1. D1: NIFTY 50 only, July 13–October 9, split into two ≤60-calendar-day GET windows: **2 requests**.
2. D2: targeted raw-incomplete windows for 99 token-resolved NIFTY 100 names: **195 stock requests**, or **197 including D1**. Two identities remain unresolved.
3. D3: targeted windows for 498 token-resolved NIFTY 500 names: **700 stock requests**, or **702 including D1**. Three identities remain unresolved.

The counts are estimates from the frozen matrices, assume archived tokens remain accepted, and exclude retries. No API/subscription validity check was made. Before any request, freeze instrument token, ISIN, series, tick size, date window, expected sessions and destination hash manifest. Validate D1 first, rebuild the matrix, then decide whether D2 is still minimal; do not authorize D3 until D2, parity, memory and restart gates pass.

## Pine parity limitations

Formula fixtures pass, but no user-supplied TradingView per-bar export was available for numeric comparison. Historical OHLC cannot reproduce Pine realtime `barstate.isrealtime`, `varip`, intrabar drift/peaks, alert creation state, or exact alert latency. Results remain labelled `A_UT_V27A_BAR_CLOSE_RECONSTRUCTION` / `FORMULA_RECONSTRUCTION_NO_TICK_ALERT_PARITY`. Per-instrument historical tick sizes, corporate-action adjustment, PSAR/ADX seed parity and anonymized contract-note fee parity remain approval blockers.

## Validation results

- Focused paper engine: `45 passed` in 1.35 seconds.
- Full repository under fail-closed offline harness: `1,290 passed`, 15 warnings in 85.92 seconds.
- The first full-suite collection attempt was invalid because `PKSCREENER_TEST_ROOT` was absent; it produced 91 collection errors and ran no tests. The corrected invocation set `PKSCREENER_TEST_ROOT=/tmp/codex-phase3-offline-root` and `PKSCREENER_TEST_OFFLINE=1`.
- Paper doctor: `paper_only=true`, zero broker mutation paths, no network sender, live paper disabled.
- Static scan found no direct Kite mutation calls in `paper_trading`.
- Warnings were existing deprecation/syntax warnings (Discord `audioop`, multiprocessing fork, and parsed escape sequences); no Phase 3 functional failures.

## Files changed in Phase 3

- `paper_trading/data.py`: zero-volume contract, row-date filtering and global duplicate handling.
- `paper_trading/strategy.py`: causal gap eligibility.
- `paper_trading/engine.py`: session boundary, censorship, liquidity gate, exact-minute intent expiry, phased timestamp processing and heap merge.
- `paper_trading/journal.py`: separate durable incomplete-trade ledger.
- `paper_trading/cli.py`: passes row-level date bounds.
- `tests/paper_trading/test_paper_engine.py`: deterministic integrity regressions.
- This report and the two Phase 3 evidence CSVs, plus the previously prepared data-readiness report and three matrices.

The verified feature commit is the commit containing this report on `codex/ut-sync-abc-paper-20261010`; its immutable SHA is reported by `git rev-parse HEAD` after commit (embedding a commit's own SHA inside itself is impossible).

## Safety and final status

Read-only process snapshots before and after showed the same production PIDs ${SERVICE_PIDS} for the emerging-leader/UT-SYNC watchers. No production source, configuration, auth/session file, scheduler, service or process was changed or restarted.

No Kite endpoint was called—historical, quote, account, order, GTT or otherwise. No data was downloaded. No real or paper Kite order operation occurred. No Discord message was sent. Nothing was merged, pushed, deployed, scheduled or activated.

Status: **integrity fixes implemented and tested in the feature worktree; EOD/zero-volume behavior verified; total-memory scalability and TradingView numeric parity not yet fully verified; no larger replay run; not deployed; zero Kite API calls and zero Kite orders.**

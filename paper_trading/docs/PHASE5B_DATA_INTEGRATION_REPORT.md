# Phase 5B benchmark integration and stock-data repair readiness

Date: 2026-10-11

Worktree: `${PAPER_WORKTREE}`

Branch: `codex/ut-sync-abc-paper-20261010`

Starting commit: `b2711784b63df3d70f0161b7ef7d8099064011bf`

Verified integration/evidence commit: `401837d2c488cdf9ba619dee00ed9369671ca939`

## Decision

The Phase 5A NIFTY 50 artifact is valid and is now an explicit, isolated input to both in-memory and streaming research replay. This removes the benchmark-availability blocker for the 68 observed sessions without changing A/B/C rules.

The stock universe is **not ready for a trustworthy broad profitability comparison**. The raw NIFTY 500 matrix still has 12,429 symbol-sessions missing at least one 15:15–15:29 timestamp and 185,874 absent tail candles. The minimum request plan remains 658 full-window historical requests across 439 symbols. Of these, 642 rows have a current exact identity and 16 rows are blocked by archived or unresolved identity evidence. No stock request was made.

This phase performed no profitability replay, parameter optimization, network request, broker/order/GTT operation, live-paper activation, Discord delivery, deployment, scheduling, merge or push.

## Reproducible evidence

| Artifact | Purpose | Rows | SHA-256 |
|---|---|---:|---|
| `evidence/phase5b_stock_tail_union_audit_2026-10-11.csv` | One row for every raw stock-session tail gap after unioning all relevant local sources | 12,429 | `c08422b6736b4c4272d8e9f8da6863ac8a0cae46bba36a36b7c930636c8ae0b9` |
| `evidence/phase5b_minimum_stock_requests_2026-10-11.csv` | Revised, non-executed stock request manifest | 658 | `d6af486954ec9b2fb456556802961ad0f9787096e79fc9de2d0becb7744a1d9d` |
| `evidence/phase5b_data_readiness_summary_2026-10-11.json` | Cache inventory and aggregate coverage/quality counts | JSON | `7cfd31a108d29d6dc0e01f32b639f0961da08d03d4fc5c7ab1f7481dd96f360b` |
| `evidence/phase5b_benchmark_eligibility_2026-10-11.json` | Nine-symbol, signal-gate-only before/after benchmark comparison | JSON | `290bd77de65bbd7c289b5bb65520d75c8ac2b1b2406b0e8c464bc0798fc9cf6a` |

The Phase 5 long matrices remain the source for every NIFTY 100/500 symbol-session, identity, warm-up and benchmark field. The Phase 5B scripts are deterministic offline scanners; they make no HTTP calls and do not instantiate a portfolio.

## 1. Benchmark integration verification

The immutable research input is:

`results/Data/ut_sync_phase5a_benchmark/normalized/NIFTY 50.csv`

Its SHA-256 remains `81ade633fabd530253c388df62b6cb53215723ad205adc1389c28c18ed938c68`. Validation confirms:

- 25,500 unique candles in 68 sessions, exactly 375 candles per session;
- timezone-aware IST starts from 09:15 through 15:29;
- token `256265`, symbol `NIFTY 50`, valid OHLC and zero duplicate timestamps;
- 25,500 zero-volume index observations, retained for price calculations and never executable as fills;
- all 3,750 candles on the ten formerly cached sessions reconcile exactly, with zero OHLCV conflicts;
- 58 recovered sessions and no missing or unexpected date in the 68-session stock calendar.

The replay CLI now accepts `--benchmark-file`. When supplied, it removes any dated fragments named `NIFTY 50.csv` from the discovered stock paths and adds exactly the explicit normalized artifact. A nonexistent path fails closed. Both CSV and SQLite streaming normalization retain the optional `instrument_token`; neither production data discovery nor any production service path changed.

Relative strength remains causal: stock and benchmark opens are established only from their 09:15 observations; the current calculation requires the benchmark candle with the exact same completed-bar timestamp; the rising test uses the stock sequence's three-prior observation. No forward-fill or future benchmark lookup exists. A regression changes a future benchmark candle and proves earlier RS output is unchanged; removing the exact aligned candle yields missing RS.

## 2. Stock-tail gap analysis

### Local inventory searched

| Local source | Files / rows | Size | Finding |
|---|---:|---:|---|
| Main `kite_history_backfill/minute` | 160,992 files; 31,187 NIFTY-500 files scanned | 2,703,021,824 B | Primary matrix source |
| Separate `intraday-trading` backfill | 56 files; 17 NIFTY-500 files scanned | 5,970,071 B | Duplicate/other-symbol coverage; no missing tail recovered |
| Main and intraday `kite_history_cache/minute` | 0 files | 0 B | No coverage |
| Live-worktree history cache | absent | 0 B | No coverage |
| `kiteticker_candles/1minute` | 11 files | 291,069,854 B | June/invalid-epoch capture, outside the 68-session window |
| UT_SYNC SQLite completed historical bars | 496,959 rows; 911 NIFTY-500 symbol-sessions | 298,045,440 B | Oct 5–9; already represented in primary coverage, no missing tail recovered |
| Phase 5A normalized cache | one benchmark file | 2,259,076 B | Benchmark only, not stock repair |

Canonical comparison found no conflicting or identical duplicate tail observation across the affected cells and no invalid local OHLC row. The secondary caches recover zero raw-absent tail timestamps. This is why the request count does not decrease.

The distinction between missing and no-trade is explicit. The NIFTY 500 matrix contains 307,728 valid zero-volume price observations. They remain on the indicator timeline, contribute zero volume to VWAP/RVOL/QQC volume terms, and are not eligible fill observations. The request audit uses `raw_missing_tail_1515_1529`, not the legacy loader-derived field that once classified zero-volume candles as absent. Among partial affected tails, 162 observed timestamps have zero volume; none is requested merely for having zero volume.

### Current stock-session coverage

| Cohort | Cells | Raw full 375 | Exact 15-bar tail loss | Other partial | Entirely missing | Fully usable for complete-session replay |
|---|---:|---:|---:|---:|---:|---:|
| April NIFTY 100 snapshot (101 identities) | 6,868 | 1,677 | 4,663 | 0 | 528 | 1,677 (24.42%) |
| April NIFTY 500 snapshot (501 identities) | 34,068 | 19,123 | 10,014 | 2,558 | 2,373 | 19,123 (56.13%) |

Across NIFTY 500, 12,387 affected sessions have no observed 15:15 execution candle; 42 partial-tail sessions do have 15:15 but still lack later audit coverage. Every position open at the 15:15 decision point in the former group must be censored as `INCOMPLETE_TRADE`, not filled at a later session. The entry cutoff is 14:50, so the missing 15:15–15:29 interval can create **zero new entry signals directly**. It can affect same-day EOD fills and, for partial tails, stop/trail/PSAR observations after entry.

The signal-only audit did not open positions, so it cannot truthfully report an exact broad-universe open-position count. Computing that count would be a new broad portfolio replay, which was prohibited. The historical nine-symbol artifacts had zero unresolved EOD/incomplete outcomes, but that does not clear the 12,429 broader stock-session gaps. The simulator preserves any future censored trade in candidate/event denominators and keeps it out of resolved-trade expectancy with an explicit reason; it never silently discards or assigns a favorable next-day price.

### Revised minimum manifest

The revised plan is 658 requests over 439 symbols, expected to recover 185,874 genuinely absent tail timestamps:

| Identity state | Request rows | Symbols | Expected absent candles | Disposition |
|---|---:|---:|---:|---|
| `CURRENT_EXACT` | 642 | 430 | 178,433 | Eligible only after separate approval and canary validation |
| `ARCHIVED_TOKEN_ONLY` | 10 | 6 | 4,381 | Block until dated token/ISIN continuity is proven |
| `UNRESOLVED` | 6 | 3 | 3,060 | Block |

Archived symbols are `GSPL`, `GUJGASLTD`, `HEG`, `HFCL`, `JBCHEPHARM`, and `RELINFRA`. Unresolved symbols are `AKZOINDIA`, `DUMMYHDLVR`, and `LTIM`. Request windows remain at most 60 calendar days and intentionally retrieve full windows for overlap validation; 185,874 is the expected newly recovered count, not the total response-row count.

## 3. Broad-universe readiness

### 100-stock engineering stage

The available April NIFTY 100 file has 101 identities because the dummy constituent is preserved. It is suitable for an engineering dry run only if labelled an April 22 fixed snapshot, after its official effective date is evidenced. Only 1,677/6,868 symbol-sessions are currently complete. It is not yet a valid profitability cohort.

### 500-stock engineering stage

The April NIFTY 500 file has 501 identities: 500 EQ plus `RELINFRA` in BE. It has 492 current-exact, six archived-token-only and three unresolved identities. It is suitable for deterministic throughput, exclusion-ledger and recovery testing, but only 19,123/34,068 sessions are fully usable before repair. `HEG` and `TRIVENI` retain unresolved discontinuity/corporate-action flags from Phase 5.

### Broader cached NSE EQ universe

Approximately 2,335 symbols match the October current EQ master and fit the tested engineering envelope. That list is **not** historical point-in-time truth for July–October and cannot select a research cohort without survivorship/lookahead bias. Existing volume supports prior-only liquidity calculations where sessions are complete, but the liquidity predicate and threshold must be frozen before outcomes are viewed. Current-session or future volume must never establish eligibility.

Required point-in-time evidence remains:

1. official NSE daily/effective-dated security masters, including listing, suspension, series, delisting, rename and successor events;
2. official NIFTY 100/500 effective constituent files and all ad-hoc change circulars for the study dates;
3. archived Kite instrument dumps mapping token, symbol, exchange, series, tick size and ISIN by identity epoch;
4. official trading calendars/special sessions and corporate-action effective dates/factors;
5. a frozen prior-session-only liquidity rule and a denominator row for every excluded or censored identity/session.

No missing, delisted, renamed, dummy or losing security may be replaced with an October survivor. Fixed-snapshot and daily point-in-time cohorts must remain separate studies.

## 4. A/B/C eligibility and Pine limitations

The offline comparison reused the original nine stock histories and generated signal gates only—no portfolio, fills, exits, fees or P&L:

| Measure | Before (10 benchmark sessions) | After (68 benchmark sessions) | Change |
|---|---:|---:|---:|
| Common candidates | 25,699 | 25,699 | 0 |
| Candidates with current and three-prior RS | 3,998 | 25,065 | +21,067 |
| `RS_BENCHMARK_MISSING` occurrences | 21,701 | 634 | -21,067 |
| A signal-gate accepted | 3,237 | 3,237 | 0 |
| B signal-gate accepted | 825 | 825 | 0 |
| C signal-gate accepted | 7 | 26 | +19 |

The remaining 634 missing-RS cases come from stock-side session/open/gap availability, not missing Phase 5A benchmark candles. The C increase demonstrates evaluability only; it is not an outcome or profitability claim. A and B are unchanged, confirming that the benchmark integration did not alter their frozen rules.

Internal batch/stream formula parity remains intact, but genuine TradingView parity is still unproven. UT v2.7a requires timestamped TradingView exports for ATR10, trail, UT events/age, PSAR, VWAP, ATR14, reference stop/quantity, completed 5-minute EMA20 seeds/buckets and emitted alert metadata. QQC CLEAR v6 requires per-bar count, signed-body quality and signed volume values. Realtime `varip`, tick path and server alert timing cannot be reconstructed from one-minute OHLC.

ADX/DMI, CHOP, same-minute RVOL, NIFTY RS and opening-range gates are frozen Python research definitions, not present in the supplied Pine scripts. External parity requires a separate timestamped reference Pine export implementing those exact definitions. ORB retest remains disabled; no rule was added. Historical OHLC cannot establish exact intrabar Pine exit parity.

## 5. Streaming scalability and portfolio accounting

The chronological k-way stream continues to process one timestamp batch at a time, updates all symbol states, then performs deterministic lexicographic allocation against shared portfolio risk. It does not load every candle into RAM. Rolling indicator/RVOL state is bounded by symbol count and lookback; journals/checkpoints are disk-backed.

The Phase 5 three-session engineering measurements remain applicable because this phase changed only benchmark path selection and token preservation:

| Stocks | Observations | Peak RSS | Compressed checkpoint | Recovery to first unseen batch |
|---:|---:|---:|---:|---:|
| 100 | 112,500 | 55.2 MiB | 38.3 KiB | 0.219 s |
| 500 | 562,500 | 130.2 MiB | 177.9 KiB | 1.127 s |
| 2,335 | 2,626,875 | 637.9 MiB | 816.7 KiB | 6.447 s |

Checkpoint serialization is the memory high-water. A timestamp-indexed SQLite resume seek and page/delta checkpoint could improve efficiency later, but neither is required for correctness and neither was changed here. Actual-data stages must still measure event/quality-ledger growth and restart equivalence before broad research.

## 6. Approval-ready bounded retrieval plan

No part of this plan is authorized by Phase 5B.

1. **Offline identity gate:** obtain/validate dated security masters and token/ISIN epochs. Keep all 16 archived/unresolved rows blocked.
2. **Canary approval:** if separately approved, execute only `EOD-0001` through `EOD-0010` from manifest SHA `d6af4869...` (ten current-exact requests across seven symbols), serialized at the conservative existing 0.7-second spacing.
3. **Canary validation:** preserve raw bytes and sanitized provenance; require IST 09:15–15:29 alignment, OHLC invariants, unique timestamps, nonnegative volume, exact overlap equality and identity match. Zero-volume rows are valid timeline observations but non-executable. Any 401/403/429 pattern, truncation, conflict, mixed identity or unexpected calendar session aborts the stage.
4. **Current-exact remainder:** after a written canary report and new approval, execute the remaining 632 `CURRENT_EXACT` manifest rows. The complete current-exact ceiling is 642 requests/178,433 missing candles. At 0.7 seconds between starts, throttle spacing alone is about 7.5 minutes; network, validation, backoff and retries are additional.
5. **Blocked identity stage:** do not request the remaining 16 rows/7,441 candles unless dated identity evidence is supplied and a new exact manifest is separately approved.
6. **Post-repair validation only:** rebuild coverage and conflict manifests, run 100 then 500 actual-data engineering validations, verify censorship/denominators/checkpoint equivalence, and request a separate decision before any profitability replay.

Only the historical-candle GET endpoint would be permitted in a future approved phase. No quote streaming, auth repair, order/GTT/position endpoint, Discord, service control, deployment or scheduling belongs in this plan.

## 7. Tests and files changed

Tests executed offline:

- `python -m pytest -q tests/paper_trading` — 65 passed.
- `python -m pytest -q` with a fresh `PKSCREENER_TEST_ROOT` — 1,310 passed, 15 pre-existing warnings, 96.58 seconds.
- After strengthening the real-cache streaming assertion: `python -m pytest -q tests/paper_trading/test_phase5b_benchmark_integration.py tests/paper_trading/test_streaming_checkpoint.py` — 18 passed.
- `python -m py_compile ...` for all changed Python files — passed.
- `git diff --check` — passed.

The first full-suite attempt was intentionally offline but lacked `PKSCREENER_TEST_ROOT` and stopped during collection with 91 guard errors. It was rerun correctly with an isolated `mktemp` root and passed; no network access was enabled.

Integration commit `401837d2c488cdf9ba619dee00ed9369671ca939` changes:

- `paper_trading/cli.py` — explicit isolated benchmark input;
- `paper_trading/data.py`, `paper_trading/streaming.py` — retain instrument token;
- `tests/paper_trading/test_phase5b_benchmark_integration.py` — cache contract, streaming, fail-closed path and causal RS regressions;
- `scripts/audit_phase5b_local_stock_gaps.py` and `scripts/audit_phase5b_benchmark_eligibility.py` — offline evidence generation;
- the four Phase 5B evidence artifacts listed above.

This report is added by the subsequent documentation commit reported in the handback.

## 8. Protected-service and safety status

Before and after work, the same production processes remained running with unchanged command lines and start time (2026-10-11 12:16:37 UTC): PID ${SERVICE_PID} top-gainers watcher, PID ${SERVICE_PID} emerging-leader alerts, and PID ${SERVICE_PID} UT_SYNC leader watch. They were not signalled, restarted, reconfigured or written by this worktree.

Final status: benchmark integration is regression-tested and isolated; stock repair is manifested but not authorized or executed; broad replay remains blocked; paper trading is not activated; no production service was modified; no Kite order or GTT operation was invoked; no stock data was downloaded; nothing was merged, deployed, scheduled or pushed.

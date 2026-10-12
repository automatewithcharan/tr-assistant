# Phase 5C controlled stock-history repair pilot

Date: 2026-10-11

Worktree: `${PAPER_WORKTREE}`

Starting commit: `06dd5840d87e00cedf645837edabfd755e71ed16`

## Result

The authorized pilot completed and stopped at exactly **20 historical-candle HTTP GET requests**. All 20 returned HTTP 200. The run retrieved 135,853 one-minute candles, of which 105,885 exactly matched existing local candles and 29,968 were new. There were zero overlap conflicts, duplicate response timestamps, off-session timestamps, invalid OHLCV rows, redirects, retries, quote calls or non-historical endpoints. The existing authentication/session file was byte-for-byte and metadata unchanged after execution.

The pilot proves that the endpoint can recover wholly missing sessions, but it **does not validate the existing tail-repair assumption for the remaining manifest**. The 11 five-day requests repaired their affected EOD tails. Five longer requests repaired only the five wholly missing July 6–10 sessions and returned the same missing 15:15–15:29 tails on their later affected dates. Four later-window requests returned only exact overlap and repaired no tail. Across 296 targeted symbol-sessions, 80 tails were repaired and 216 remain missing.

No further stock request should be authorized from the current plan until a separately approved, very small single-session diagnostic determines whether the 15:15–15:29 absence is range-dependent, source-side unavailable, or permanent. Repeating the existing 60/36-day windows would waste requests and produce known duplicates.

No profitability replay or A/B/C rule change was performed.

## Authorization and deterministic selection

The Phase 5B manifest remained byte-identical at SHA-256 `d6af486954ec9b2fb456556802961ad0f9787096e79fc9de2d0becb7744a1d9d`, with 642 `CURRENT_EXACT` rows. Selection used the first 20 eligible rows by ascending numeric stable request ID. The pre-execution artifact was written before authentication or any HTTP call:

- `evidence/phase5c_selected_requests_2026-10-11.json`
- SHA-256 `a2f4efefac32ae053612ec31b40348799b2cb58df6fc3820811e03c885bde761`
- selected IDs `EOD-0001` through `EOD-0020`
- 20/20 `CURRENT_EXACT`; zero archived-token or unresolved rows.

The sanitized post-run result artifact is `evidence/phase5c_pilot_results_2026-10-11.json`, SHA-256 `f8dc0e4ce4422374738e9519f3189726bd94805ef47ad2da4341b9935d118c56`. It binds to local provenance SHA-256 `34ea2b07e8622e4d1a2b9017563ef8fbf18f81b476511fd918e20e4390e64653`.

Each `(symbol, token)` was independently matched to the unchanged local NSE instrument master as an NSE EQ identity. The endpoint template was only:

`GET https://api.kite.trade/instruments/historical/{current_exact_token}/minute`

Parameters were `continuous=0`, `oi=0`, and the exact manifest `from`/`to` values. The script hard-limits selection to 20 and asserts the manifest hash, eligible population, selected ID sequence, current identity, NSE EQ mapping, canonical 09:15/15:29 boundaries and maximum 60-calendar-day window before execution.

## Exact requests and results

| Request | Symbol | Token | IST parameters (`from` → `to`) | Rows | New | Exact overlap | Tails repaired | Outcome |
|---|---|---:|---|---:|---:|---:|---:|---|
| `EOD-0001` | 360ONE | 3343617 | 2026-07-06 09:15:00 → 2026-09-03 15:29:00 | 16,140 | 1,875 | 14,265 | 5/29 | partial |
| `EOD-0002` | 360ONE | 3343617 | 2026-09-04 09:15:00 → 2026-10-09 15:29:00 | 8,640 | 0 | 8,640 | 0/24 | none |
| `EOD-0003` | 3MINDIA | 121345 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,851 | 1,851 | 0 | 5/5 | full purpose |
| `EOD-0004` | AADHARHFC | 6074625 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,874 | 1,874 | 0 | 5/5 | full purpose |
| `EOD-0005` | AAVAS | 1378561 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,875 | 1,875 | 0 | 5/5 | full purpose |
| `EOD-0006` | ABB | 3329 | 2026-07-06 09:15:00 → 2026-09-03 15:29:00 | 16,140 | 1,875 | 14,265 | 5/29 | partial |
| `EOD-0007` | ABB | 3329 | 2026-09-04 09:15:00 → 2026-10-09 15:29:00 | 8,640 | 0 | 8,640 | 0/24 | none |
| `EOD-0008` | ABBOTINDIA | 4583169 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,870 | 1,870 | 0 | 5/5 | full purpose |
| `EOD-0009` | ABCAPITAL | 5533185 | 2026-07-06 09:15:00 → 2026-09-03 15:29:00 | 16,140 | 1,875 | 14,265 | 5/29 | partial |
| `EOD-0010` | ABCAPITAL | 5533185 | 2026-09-04 09:15:00 → 2026-10-09 15:29:00 | 8,640 | 0 | 8,640 | 0/24 | none |
| `EOD-0011` | ABFRL | 7707649 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,875 | 1,875 | 0 | 5/5 | full purpose |
| `EOD-0012` | ABLBL | 193751809 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,873 | 1,873 | 0 | 5/5 | full purpose |
| `EOD-0013` | ABREL | 160001 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,875 | 1,875 | 0 | 5/5 | full purpose |
| `EOD-0014` | ABSLAMC | 1540609 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,875 | 1,875 | 0 | 5/5 | full purpose |
| `EOD-0015` | ACC | 5633 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,875 | 1,875 | 0 | 5/5 | full purpose |
| `EOD-0016` | ACE | 3478273 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,875 | 1,875 | 0 | 5/5 | full purpose |
| `EOD-0017` | ACMESOLAR | 6927617 | 2026-07-06 09:15:00 → 2026-07-10 15:29:00 | 1,875 | 1,875 | 0 | 5/5 | full purpose |
| `EOD-0018` | ADANIENSOL | 2615553 | 2026-07-06 09:15:00 → 2026-09-03 15:29:00 | 16,140 | 1,875 | 14,265 | 5/29 | partial |
| `EOD-0019` | ADANIENSOL | 2615553 | 2026-09-04 09:15:00 → 2026-10-09 15:29:00 | 8,640 | 0 | 8,640 | 0/24 | none |
| `EOD-0020` | ADANIENT | 6401 | 2026-07-06 09:15:00 → 2026-09-03 15:29:00 | 16,140 | 1,875 | 14,265 | 5/29 | partial |

All response SHA-256 values and sanitized headers are recorded in `evidence/phase5c_pilot_results_2026-10-11.json`; credentials and authorization headers are absent. Requests ran from 12:45:45 through 12:46:10 UTC. The minimum start-to-start interval was 0.977 seconds, above the configured conservative 0.7-second throttle. There were no retries.

## Raw and normalized preservation

The isolated research cache is `results/Data/ut_sync_phase5c_stock_pilot`:

- 20 immutable raw JSON responses, 8,003,142 bytes total;
- 20 atomically written per-request normalized CSVs;
- 16 atomically written per-symbol normalized CSVs;
- incremental sanitized provenance, 57 files and about 29 MiB total;
- no original cache file overwritten.

Each response was persisted before parsing. Validation required JSON success, timezone-aware IST, the approved date window, weekday 09:15–15:29 timestamps, sorted uniqueness, positive consistent OHLC, nonnegative volume and exact identity-bound endpoint. It was reconciled against 373,331 local observations from 1,008 files plus 7,256 exact SQLite duplicates. A conflict would have stopped the sequence before another request.

The research-data root is ignored by repository policy, so the 29 MiB raw/normalized cache is preserved locally but not force-added to Git. The committed selection and result artifacts contain hashes and provenance sufficient to audit it without committing broker responses.

## Coverage improvement

### Selected 16-symbol grid

| Metric across 16 symbols × 68 sessions | Before | After | Improvement |
|---|---:|---:|---:|
| Missing candles | 34,669 | 4,701 | 29,968 fewer |
| Missing tail candles | 4,800 | 3,600 | 1,200 fewer |
| Full 375-candle sessions | 608 | 679 | +71 |
| Sessions with a 15:15 observation | 768 | 848 | +80 |

The 80 recovered July 6–10 symbol-sessions consist of 71 full sessions and nine still-partial sessions. The nine partial sessions contain 32 absent intraday minutes returned as genuine omissions—not invalid rows or zero-volume placeholders. All 80 now include complete 15:15–15:29 tails.

### NIFTY 500 matrix impact

| Metric | Before | After pilot |
|---|---:|---:|
| Missing candles | 1,060,024 | 1,030,056 |
| Missing 15:15–15:29 candles | 185,874 | 184,674 |
| Full sessions | 19,123 | 19,194 |
| Incomplete sessions | 14,945 | 14,874 |
| Sessions lacking a 15:15 observation | 12,387 | 12,307 |

The endpoint returned the exact same 360-row 09:15–15:14 pattern for every tested affected date from August 3 onward. Therefore those responses are internally valid but do not repair the intended EOD evidence. The pilot's 29,968 new candles are dominated by the previously wholly missing July 6–10 sessions, not the later tails.

## A/B/C historical eligibility

An offline signal-gate audit covered only the 16 selected symbols. It used the recovered 68-session NIFTY benchmark, did not instantiate portfolios, and computed no fills, exits, fees or P&L.

| Signal-only measure | Before | After | Delta |
|---|---:|---:|---:|
| Common candidate observations | 48,074 | 52,023 | +3,949 |
| Candidate observations marked causal `DATA_GAP` | 6,082 | 6,384 | +302 |
| A gate-accepted IDs | 5,333 | 5,848 | +515 |
| B gate-accepted IDs | 1,278 | 1,376 | +98 net |
| C gate-accepted IDs | 56 | 60 | +4 net |

Every post-repair candidate ID increase comes from newly available historical bars. Absolute `DATA_GAP` occurrences rise because 3,949 new candidates now exist; the rate falls from 12.65% to 12.27%. A had 515 new and zero lost accepted IDs. B had 101 new and three lost; C had five new and one lost. The lost IDs reflect causal indicator/warm-up history changing when formerly absent prior bars become available, not a strategy-rule change. These eligibility counts are not profitability evidence.

Tail-only repairs do not create new entries after 15:15 because the frozen entry cutoff is 14:50. They matter for same-day EOD/stop/trail observability. Wholly recovered sessions can affect all causal indicators and signal gates, as the audit shows.

## Remaining requests and blockers

- 622 `CURRENT_EXACT` manifest rows were not executed and are not authorized.
- Nine of the 20 executed records still have at least one intended tail gap; their response data must remain in the cache, but they are not successful tail repairs.
- 16 blocked rows remain untouched: ten `ARCHIVED_TOKEN_ONLY`, six `UNRESOLVED`.
- No archived or unresolved token was requested.

The current broad request estimate should not be treated as 622 likely-successful repairs. A future plan should first isolate one failed symbol/date and, only with separate approval, compare a minimal single-session GET with the already preserved long-window response. Until then, the later tail data should be treated as unavailable and affected trades censored.

## Tests and protected-service status

The following checks passed:

- `python -m pytest -q tests/paper_trading/test_phase5c_stock_backfill_pilot.py tests/paper_trading/test_phase5b_benchmark_integration.py tests/paper_trading/test_streaming_checkpoint.py` — 20 passed;
- `python -m pytest -q tests/paper_trading` — 67 passed;
- full offline repository suite with a fresh `PKSCREENER_TEST_ROOT` — 1,312 passed, 15 pre-existing warnings, 92.65 seconds;
- `python -m py_compile` for both Phase 5C scripts and the new tests — passed;
- `git diff --check` — passed.

The tests cover deterministic selection, GET-only endpoint construction, blocked-identity exclusion, IST/window/OHLC/duplicate/conflict validation and the existing benchmark/streaming contracts. Eligibility generation contains no portfolio or P&L path. Repository data policy is respected; raw broker responses remain ignored/local.

Before execution and after retrieval, the same production processes remained running with their original 2026-10-11 12:16:37 UTC start time and unchanged command lines: PID ${SERVICE_PID} top-gainers watcher, PID ${SERVICE_PID} Strict Emerging Leader alerts and PID ${SERVICE_PID} UT_SYNC leader watch. They were not signalled, restarted, reconfigured or written by this worktree.

Final safety status: exactly 20 authorized historical GETs; zero additional requests; zero orders, modifications, cancellations, GTT or broker execution operations; zero quote/streaming calls; zero authentication changes or repairs; no live paper, Discord, deployment, scheduling, merge or push.

# Phase 5 historical-data repair, Pine-parity and full-universe readiness report

Date: 2026-10-10  
Worktree: `${PAPER_WORKTREE}`  
Branch: `codex/ut-sync-abc-paper-20261010`  
Starting commit: `d72c5171cb7e7d543dc2ec2e7d8ddbfe749a151a`  
Scope: data/parity/readiness engineering only; no profitability comparison.

## Decision and approval gate

The engine is **not ready for a broad historical A/B/C profitability replay**. Streaming and restart accounting scale to 2,335 symbols in a bounded engineering test, but the historical evidence does not yet support a valid broad cohort. The immediate blockers are 58 missing NIFTY 50 sessions, widespread missing stock-session tails, unresolved historical identities/corporate actions, an unsettled zero-volume policy, incomplete point-in-time NSE membership evidence, and absent TradingView numeric exports.

The smallest useful next data operation is exactly **two read-only NIFTY 50 historical GET requests**. They are specified below and remain `AWAITING_EXPLICIT_D1_APPROVAL`. The 658 stock-tail requests are deliberately deferred until the benchmark response and identity/data policies pass validation. No request was sent in Phase 5.

Phase 5 did not access credentials or session files, download data, run a profitability replay, alter A/B/C rules, place/modify/cancel an order or GTT, send Discord output, activate paper trading, or change/restart a production service.

## Reproducible evidence

| Artifact | Contents | Rows including header | SHA-256 |
|---|---|---:|---|
| `evidence/phase5_session_missing_manifest_2026-10-10.csv` | Every observed session from July 6 through October 9; benchmark and NIFTY 100/500 raw coverage | 69 | `15a13f49953af69309e40f859bede308a80960921be7878b3310ab23e6f75447` |
| `evidence/phase5_proposed_historical_requests_2026-10-10.csv` | Exact, non-executed benchmark and stock-tail request plan, including identity/approval status | 661 | `73c7fcb3e701796b38a6b4aa0f40edc9a6e229105988cc3c85c54f68858a1a42` |
| `evidence/phase5_ifci_20_timestamp_internal_parity_2026-10-09.csv` | Twenty timestamped IFCI batch-versus-stream observations with all indicator/candidate fields | 21 | `8c51fe1d891faf2b8877b0f19f166309f066052abfc53a675112dab764b778c1` |

The prior long-form NIFTY 100/500 matrices remain the stock-by-session source of truth:

- `evidence/nifty100_symbol_session_coverage_2026-10-10.csv`, SHA-256 `9ef87ed1b797b210eb17fcb78cb806d63d5a313113d24c65a95c228f27fd8d3f`;
- `evidence/nifty500_symbol_session_coverage_2026-10-10.csv`, SHA-256 `51f3120d83dfe66be7ad87047024b1804b4175d65d6fdbbb4521c3dd10e7616b`;
- `evidence/nifty_cohort_session_summary_2026-10-10.csv`, SHA-256 `94fe9f3508ca5c9a023bc3bbbd17853b9e1c524c49b0bd2eaaa82e90b01a9a3a`.

The Phase 5 manifest was generated from local files only. Its 68 observed sessions are:

`2026-07-06..10, 07-13..17, 07-20..24, 07-27..31; 08-03..07, 08-10..14, 08-17..21, 08-24..28, 08-31; 09-01..04, 09-07..11, 09-15..18, 09-21..25, 09-28..30; 10-01, 10-05..09`.

These are dates observed in the stock caches, not a substitute for an official exchange calendar. A benchmark response containing an additional valid exchange session must be retained and investigated, not filtered out to preserve the current date list.

## Historical data findings

### NIFTY 50 benchmark

All repository caches were rechecked. The only replayable one-minute benchmark history remains the ten files under `results/Data/kite_history_backfill/minute/*/NIFTY 50.csv` in the main repository data store. They contain 3,750 unique, aligned candles: exactly 375 rows from 09:15 through 15:29 on July 17, July 27, July 29, August 3, August 20, September 15, September 16, October 5, October 6 and October 9.

Thus only 10/68 observed sessions are complete and 58/68 are missing, totaling 21,750 expected one-minute observations on the observed-session calendar. NIFTY cannot be forward-filled, reconstructed from five-minute bars, or synthesized. Strategy C must fail closed without its exactly aligned benchmark bar.

The earlier content manifest hash was `702684511128e696cef78908ee0e0cf43faf8dd7ff6df929aa4bc9ebe177a2c7`. A fresh scanner implementation produced a different *manifest-method* digest (`ea9126...`); this does not establish a source-data change. The row counts, timestamps and per-file content findings are unchanged. Future validation must compare canonical `(timestamp, OHLCV)` row hashes rather than equating two scanner-specific manifest encodings.

### Stock-session completeness

Raw local-cache totals across the 68 observed sessions are unchanged:

| Cohort | Cells | Full 375 | Exact 15:15–15:29 loss | Other partial | Entirely missing | Zero-volume observations |
|---|---:|---:|---:|---:|---:|---:|
| April NIFTY 100 snapshot (101 rows) | 6,868 | 1,677 | 4,663 | 0 | 528 | 3,860 |
| April NIFTY 500 snapshot (501 rows) | 34,068 | 19,123 | 10,014 | 2,558 | 2,373 | 307,728 |

For NIFTY 100, 98 cached symbols exhibit the exact 360-row pattern ending at 15:14 on at least one day. For NIFTY 500, 212 symbols exhibit that exact tail loss and another 174 have another partial pattern. Six April-list names have no cached minute rows at all: `AKZOINDIA`, `DUMMYHDLVR`, `GSPL`, `GUJGASLTD`, `LTIM`, and `RELINFRA`.

The stock-tail request plan covers every NIFTY 500 symbol/session whose raw cache lacks at least one 15:15–15:29 candle. Greedy <=60-calendar-day windows produce **658 requests across 439 symbols**. Of the request rows, 642 have a current exact token, ten depend on an archived token that must be validated, and six have an unresolved identity. This is a planning upper bound, not current authorization.

If all incomplete minutes—not only EOD tails—were repaired, the estimate rises to **821 windows across 476 symbols** (805 current-exact request rows and 16 blocked rows). That broader acquisition is not recommended until the zero-volume and source-conflict policies are fixed.

### Why the 15:15–15:29 tail is material

The strategy declares EOD at 15:15. A 15:14 candle closes at 15:15 and creates a pending exit, but the simulator fills only on a subsequent observed bar. A 360-row session has no same-day execution observation. The Phase 3 audit showed that the old behavior could carry that pending exit to the following 09:15 open and label it merely `EOD`/`NEXT_OBSERVATION`, allowing an overnight gap and consuming next-day cash/risk capacity.

The Phase 4 session-boundary logic now expires/censors prior-session state rather than silently treating the next session as the same-day fill. Nevertheless, a historical study still cannot claim an executable same-day EOD exit without a post-15:15 observation. Every repaired session must contain the canonical 09:15–15:29 grid or be explicitly censored/ineligible according to the frozen policy. Missing tails must never be imputed.

### Timestamp, duplicate, zero-volume and identity checks

- Canonical timestamps are timezone-aware IST candle starts, unique by `(instrument identity epoch, timestamp)`, on the 09:15–15:29 grid. The fetcher must normalize API candle timestamps before comparing them with cache rows.
- Overlapping range files must be merged globally, not merely deduplicated within each file. Exact duplicates are harmless but logged; any OHLCV conflict aborts the stage and preserves both source records for audit.
- The current loader rejects zero-volume equity rows. This is conservative but unresolved: valid flat-OHLC no-trade candles may be genuine API observations. No acquisition or replay may silently convert such rows to positive volume or delete them without recording the resulting timeline gap. A representative raw-response review and authoritative API semantics are required before freezing the policy.
- Every normalized row must retain token, exchange, tradingsymbol, ISIN when available, series, tick size, identity effective range, raw-response hash and acquisition request ID. Symbol text alone is insufficient.
- Historical token reuse is guarded by effective-dated identity tables. An archived token may be requested only after its date-range symbol/ISIN mapping is proven.

### Corporate-action discontinuity screen

A fresh adjacent-observed-session scan covered 31,187 matched files and 495 April NIFTY 500 symbols with data. At an intentionally sensitive >=25% close-to-next-observed-open threshold it found two suspects:

| Symbol | Prior observed close | Next observed open | Change | Interpretation |
|---|---:|---:|---:|---|
| HEG | 725.40 on 2026-09-04 | 216.00 on 2026-09-17 | -70.2233% | suspect only; intervening sessions are absent |
| TRIVENI | 471.00 on 2026-07-21 | 255.00 on 2026-08-05 | -45.8599% | suspect only; intervening sessions are absent |

These are not proof of splits/demergers. Both identities must fail closed until official corporate-action records and intervening raw candles establish the cause and effective date. A broad replay must freeze either a raw-price policy with position/share adjustments at effective events, or a fully documented adjusted series. Mixing policies is forbidden.

## Exact proposed read-only requests — not executed

Known NIFTY 50 instrument token in the local mappings: `256265`. Proposed query parameters use `continuous=0` and `oi=0`.

```http
GET /instruments/historical/256265/minute?from=2026-07-06+09:15:00&to=2026-09-03+15:29:00&continuous=0&oi=0
GET /instruments/historical/256265/minute?from=2026-09-04+09:15:00&to=2026-10-09+15:29:00&continuous=0&oi=0
```

No host request, authorization header, token or credential is included in the artifact. The exact request rows are `BMARK-001` and `BMARK-002` in the proposal CSV. The first window spans 60 calendar days inclusive and the second spans 36. They intentionally retrieve complete windows so the ten existing sessions can serve as overlap controls.

The repository's prior official-document audit recorded a one-minute maximum window of 60 calendar days and a historical endpoint limit of 3 requests/second. The project will use the more conservative existing throttle of one request per 0.7 seconds (about 1.43 requests/second), serialize requests, honor rate-limit responses and back off. These external limits must be revalidated against the current official Kite documentation immediately before execution because Phase 5 intentionally made no external request.

Request estimates:

| Stage | Requests | Minimum throttle spacing | Authorization |
|---|---:|---:|---|
| D1 NIFTY benchmark | 2 | about 1.4 s plus network/validation | request explicit approval now |
| D2 NIFTY 500 stock EOD tails | 658 | about 7.7 min plus network/retry/validation | deferred; do not approve yet |
| D1 + planned D2 ceiling | 660 | about 7.7 min plus overhead | not a combined approval request |
| Alternative all-incomplete-stock repair | 821 stock requests | about 9.6 min plus overhead | estimate only; not manifested/approved |

### D1 validation and abort rules

1. Revalidate current API limits and Historical Data subscription entitlement without exposing credentials in logs.
2. Record request ID, token, exact parameters, HTTP status, response timestamp, raw-byte SHA-256 and row count; never log authorization material.
3. Stage responses immutably. Validate parseability, IST alignment, OHLC invariants, nonnegative volume, uniqueness and the official exchange-session grid before any merge.
4. Compare all ten overlapping cached sessions candle-by-candle. Exact equality is the acceptance target. Any conflicting timestamp aborts the merge and creates a source-conflict record; the existing file is not overwritten.
5. Confirm all 58 presently missing observed dates, while separately investigating API-returned sessions not in the current 68-date stock calendar.
6. Canonically merge on `(token, timestamp)`, rebuild the data SHA/manifests, then rerun coverage/parity tests only. Do not run profitability.
7. Re-estimate D2 from the rebuilt coverage and validate archived identities before requesting stock-data approval.

Retries must be idempotent. Partial responses, pagination-like truncation, unexpected first/last candles, mixed tokens, duplicate conflicts, entitlement failures, 401/403, or repeated 429/5xx responses fail the stage closed.

## Pine-to-Python parity audit

### Sources actually inspected

The requested UT source is present with a filesystem suffix as `UT_SYNC_v2_7a_ENTRY_DIAGNOSTICS (1).pine`; no unsuffixed duplicate was found. Its SHA-256 is `a59f373a1e7e8cd4e5df0f749efae617e15fdc47eabe31d84e9d6348555ecdc9`, exactly the frozen `source_sha` in `AppConfig`. It has 582 lines.

`QQC_CLEAR_UT_SYNC_COMPANION_v6.pine` has SHA-256 `7aeff8247a6879bffd02dc2862da34f9d2c163968ad664f3d087c3865532b577` and 106 lines. No claimed predecessor source was used as a substitute.

Frozen Python config SHA-256 at this audit is `5c5f60feeca74d7f5921328e3c09da0005cedc4d2e72f04be353460e9bcd66b0`; the Phase 5 package hash before the final documentation commit is `8c97c9f2a891b1c61df5d08409c6efdd60c28d2a19eacabce20b8472a3ef1af4`.

### Agreement status by feature

| Feature | Code-level finding | Internal numeric evidence | Genuine TradingView status |
|---|---|---|---|
| UT ATR10/trail/crosses | Same Wilder ATR intent, key 3 recursive classic UT trail, confirmed close crosses; state is not reset at sessions | Batch and stream equal | unverified without exported Pine plot values |
| Fresh UT qualification/age | Same intact leg, same signal day, age 0–2, PSAR/VWAP/trail trend, chase limits, 09:20–14:50 window, reference stop/quantity | Candidate IDs/reasons equal on internal fixtures | realtime `liveSafe` ticks cannot be reconstructed from OHLC |
| Controlled continuation | Same prior five bars excluding signal bar, age 3–30, bullish breakout, close location, range and ATR/VWAP extension limits | Batch and stream equal | Pine alert timestamps/export required |
| PSAR | Same 0.02/0.02/0.20 parameters; Python implements a conventional reconstruction | Batch and stream equal to each other | `ta.sar` seed/reversal values not independently established |
| Session VWAP | Same HLC3-volume accumulation and session reset intent | Batch and stream equal | zero-volume semantics/reference values required |
| Completed 5m EMA20 | Python maps only fully closed 09:15-aligned buckets; Pine uses `request.security(... ta.ema(close,20)[1]/[2], lookahead_on)` | Batch and stream equal | TradingView session/bucket/EMA seed export required |
| QQC 9-bar quantity | `upBars > downBars` | equal | formula-level agreement; values not TV-verified |
| QQC quality | signed sum of candle bodies > 0 | equal | formula-level agreement; values not TV-verified |
| QQC volume | sum of `(body/range)*volume` > 0 with zero contribution for zero-range bars | equal | formula-level agreement; values not TV-verified |
| EOD/stop/PSAR exits | Python preserves conservative next-observation fills, old-stop-before-new-trail and session censoring; Pine auto/manual managers contain realtime `varip` paths | restart/event tests equal | historical OHLC cannot prove intrabar Pine behavior |
| ADX/DMI14/14 | Python is causal Wilder DMI/ADX and internally consistent | batch and stream equal | not defined in either supplied Pine source; no Pine-parity claim |
| CHOP14 | standard TR-sum/range formula in Python | batch and stream equal | not defined in supplied Pine sources |
| same-minute RVOL | current volume divided by up to 20 prior-session values after 10 samples | batch and stream equal | research definition, not supplied Pine |
| NIFTY relative strength | stock return since open minus exactly aligned benchmark return; three-bars-prior comparison | batch and stream equal when benchmark exists | research definition, not supplied Pine; 58 sessions blocked |
| five-minute OR / two-close hold | 09:15–09:19 high, available after completion, consecutive closes above | batch and stream equal | research definition, not supplied Pine; ORB retest remains disabled |

The supplied Pine code does not contain ADX/DMI, CHOP, RVOL, NIFTY RS or the Python opening-range gates. These belong to the frozen B/C research definitions. Calling those features “Pine parity” would be false; only causal formula tests and an independently supplied TradingView reference indicator could validate them.

### Twenty timestamped comparisons

The Phase 5 IFCI artifact records the completed bars ending 09:16 through 09:35 IST on October 9. It was calculated from 23,625 IFCI bars (63 cached sessions) and 3,750 locally cached benchmark bars. Every recorded indicator, signal, age, setup, rejection reason and B/C input matched between the prior batch implementation and the Phase 4 streaming state for all twenty timestamps.

This is **internal batch/stream agreement only**. Every row says `reference_kind=LOCAL_KITE_OHLC_BATCH_VS_STREAM` and `pine_reference_available=NO`. Benchmark zero-volume observations generated expected loader warnings; they do not turn the comparison into a TradingView reference.

The existing anecdotal IFCI alert observations—approximately 09:21 `P BUY CONT` around ₹68.65/SL ₹68.28 and a 09:22:09 `SELL HARD SL` on October 9—lack a machine-readable TradingView bar-value export and alert metadata, so they cannot prove formula/timing equality.

### Evidence needed from the user for genuine TradingView parity

Supply an export captured with the exact two source hashes, inputs, symbol/exchange, chart timezone, 1-minute session settings and data-adjustment setting. For at least one full warm-up plus test session (IFCI October 9 is useful), provide per completed bar:

- timestamp, OHLCV and `syminfo.mintick`;
- ATR10, UT trail, UT BUY/SELL booleans, age and source UT time;
- PSAR, VWAP, ATR14, prior breakout high, reference stop/quantity and fresh/continuation booleans;
- completed 5m EMA20 and its prior value;
- QQC count/body/volume signed measures and three booleans;
- every emitted alert's server timestamp, bar time, alert name/message, chart symbol/timeframe, alert creation time and whether it was a historical, bar-close or realtime event.

For B/C-only indicators, provide a separate frozen reference Pine script/export implementing the exact Python definitions. TradingView screenshots alone are useful diagnostics but insufficient for numeric parity. Realtime `varip`, tick path, live drift and alert-server latency remain irreducible from historical OHLC and must retain the `BAR_CLOSE_RECONSTRUCTION_NEXT_OPEN` qualification.

## Multi-session scalability and recovery

The Phase 4 benchmark was extended so generated observations use distinct exchange dates and real session transitions. Each fresh-process Phase 5 run used three 375-minute sessions, interrupted after two complete sessions, saved a compressed full-state checkpoint, measured a recovery probe through the first unseen timestamp batch, then resumed through the third session. The source is explicitly `SYNTHETIC_ENGINEERING_ONLY`, contains constant liquid bars, and produced zero resolved trades. It exercises local engine state/accounting only and is not market or profitability evidence.

| Symbols | Stock observations | Peak RSS | Compressed checkpoint | SQLite footprint at checkpoint | Recovery to first unseen batch | Resume through remaining session | Total incl. recovery probe | Final allocated SQLite footprint |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 112,500 | 56,532 KiB (55.2 MiB) | 39,256 B (38.3 KiB) | 172,032 B | 0.219 s | 1.783 s | 5.044 s | 143,360 B |
| 500 | 562,500 | 133,364 KiB (130.2 MiB) | 182,138 B (177.9 KiB) | 450,560 B | 1.127 s | 11.636 s | 33.494 s | 425,984 B |
| 2,335 | 2,626,875 | 653,232 KiB (637.9 MiB) | 836,323 B (816.7 KiB) | 1,708,032 B | 6.447 s | 65.317 s | 183.582 s | 1,716,224 B |

The recovery measurement includes restoring/deserializing state, replay-source scanning past the committed timestamp and processing the first unseen batch; it is an operational recovery latency, not deserialization alone. “Total” includes the deliberate one-batch recovery probe and therefore is not a normal uninterrupted-runtime benchmark. SQLite reports allocated file/WAL footprint; deleting the completed checkpoint does not necessarily shrink already allocated pages.

Findings:

- State remains proportional to universe size and bounded rolling windows, not total replay duration. Same-minute RVOL grows only to 20 prior samples per minute.
- Three-session transition, indicator warm-up, deterministic lexicographic simultaneous allocation, global three-position/open-risk/daily-loss controls, journals and session checkpoints use the same tested replay path.
- Recovery equivalence is covered at multiple mid-session cuts and near EOD; a stale data/config/software checkpoint fails closed.
- Full checkpoint serialization remains the memory high-water event. At 2,335 symbols, peak RSS is about 638 MiB. This is acceptable for research on the audited machine but materially above steady-state Phase 4's approximately 222 MiB.
- The compressed on-disk checkpoint is under 1 MiB even at 2,335 symbols; Python pickle/compression copies, not storage size, cause the transient. Delta/page-oriented checkpointing could reduce this, but Phase 5 found no correctness justification to rewrite it before data validity is solved.
- The current source resume scans from the beginning and skips committed rows, explaining the 6.447-second broad-universe recovery probe. A timestamp-indexed source seek is a safe later performance improvement, not a rule change.
- These three-session synthetic results do not prove multi-month real-cache runtime or event-ledger growth. A staged, non-profitability NIFTY 100 then 500 dry validation must measure actual issue/event density after D1/D2.

## Point-in-time universe protocol

### April index lists

The April files are unchanged:

- NIFTY 100 snapshot: 101 unique symbols/ISINs, SHA-256 `05c25cc4889a9bacb47db8624d295292b0a47675198a185f807f7e64568e2c34`;
- NIFTY 500 snapshot: 501 unique symbols/ISINs, SHA-256 `e070407d4bed56fad89a59df5ec95433cbe91c022c32f0d253914ff10a49c43b`.

They can support an explicitly labelled **April 22 fixed snapshot cohort** only after their official effective date is evidenced. All 101/501 rows—including dummy, BE-series, renamed, suspended, merged, delisted and data-missing names—must stay in the denominator with reason codes. They cannot establish dynamic membership on July–October dates and cannot justify replacing failed identities with October survivors.

Required additional point-in-time evidence:

1. Official NIFTY 100/500 constituents and weights effective March 31, July 6/13 and September 30, plus every intervening/ad-hoc inclusion/exclusion circular.
2. NSE daily/event-dated security masters covering listing, delisting, suspension, series and ISIN, and official merger/demerger/rename successor links.
3. Archived Kite instrument dumps/token/tick-size mappings for each identity epoch.
4. Official exchange calendars and special-session records.
5. Corporate-action events with ex/effective dates and adjustment factors.

Current reconciliation still has 492 current-exact, six archived-token-only and three unresolved identities in the NIFTY 500 file. The blocked symbols are archived `GSPL`, `GUJGASLTD`, `HEG`, `HFCL`, `JBCHEPHARM`, `RELINFRA`, and unresolved `AKZOINDIA`, `DUMMYHDLVR`, `LTIM`. Similar current names or series are not evidence of identity continuity.

### Broad NSE EQ target outside NIFTY 500

The approximately 2,335 symbols matched to the October EQ master are an engineering-load universe only. They would introduce survivorship/lookahead selection if used for July research.

The proposed broad protocol is:

1. On each session `D`, start with the official NSE equity security master effective by the prior close; exclude non-EQ series only according to a rule frozen before results.
2. Resolve each security by stable ISIN plus dated symbol/token epochs. Never use a future master to repair an earlier identity silently.
3. Record an eligibility row for every security/date, including inactive, suspended, delisted, renamed, corporate-action pending, no-data and insufficient-warm-up cases.
4. Calculate liquidity solely from completed sessions before `D` (proposed 20-session turnover window). A missing/no-trade prior session remains visible; future volume and eventual index membership cannot affect eligibility.
5. Use July 6–31 as a 20-observed-session warm-up only if those sessions become complete. The earliest broad evaluation session would then be August 3. Newly listed names become eligible only after independently satisfying the frozen warm-up.
6. Freeze the exact universe predicate, minimum listing history, turnover statistic/threshold, identity/corporate-action policy, A/B/C config hash, fee version, tick-size source, fill/EOD conventions, simultaneous allocation rule and evaluation dates before viewing any profitability output.
7. Publish counts for the full starting security master, each exclusion reason, eligible denominator, missing-data denominator and completed outcomes. Never drop a name because it later failed, disappeared or lacked a convenient file.

A fixed-start April cohort and a daily point-in-time broad cohort answer different questions. They must have separate cohort labels and results; one must not be substituted for the other after outcomes are seen.

## Remaining correctness blockers before profitability

1. Execute and validate D1 benchmark backfill only after approval; Strategy C otherwise lacks 58 sessions.
2. Resolve zero-volume candle semantics and freeze timeline-versus-execution eligibility.
3. Obtain point-in-time constituents/security masters, dated token/tick-size identities and official corporate actions.
4. Repair or explicitly censor missing same-session EOD tails; never permit an unlabeled next-day fallback.
5. Add global token/timestamp duplicate/conflict enforcement to the acquisition merge.
6. Obtain genuine TradingView numeric/alert exports; specifically settle `ta.sar`, multi-timeframe EMA seeds/session mapping and realtime-only behavior.
7. Establish adjusted/raw corporate-action treatment for HEG/TRIVENI and all official events.
8. Freeze exact broad-universe causal liquidity parameters and holdout/evaluation dates without examining A/B/C profitability.
9. Validate historical per-instrument tick sizes; the current global ₹0.05 research default is not sufficient for a definitive broad study.
10. Run actual-data NIFTY 100 and NIFTY 500 non-profitability dry validations and measure source/journal growth before the broad cohort.

## Approval-ready Phase 6 staging plan

### D1 — minimum benchmark acquisition

Approval requested: the two NIFTY 50 GETs shown above, and only those GETs. This approval would not authorize stock requests, any order endpoint, profitability replay, service change, paper activation or deployment.

After acquisition: validate immutable responses, reconcile all overlaps, rebuild manifests, rerun data/parity tests and return a D1 acceptance report. Stop on any conflict.

### D2 — stock-tail pilot, separately approved later

After D1 and identity/zero-volume rules pass, recalculate the 658-request ceiling. Select a small, predeclared identity-clean pilot based on missingness—not trading outcome—covering full, exact-tail and other-partial cases. Verify same-day EOD accounting, duplicate merging and data semantics. Then seek separate approval for only the remaining necessary tails.

### V1/V2 — non-profitability cohort validation

- V1: April-fixed NIFTY 100, checking only coverage, warm-up, eligibility, parity sample, event determinism, checkpoint equivalence and accounting invariants.
- V2: April-fixed and point-in-time NIFTY 500 under separately labelled cohorts, same gates.
- No threshold changes and no comparison of returns during V1/V2.

### U1 — point-in-time broad engineering dry run

Build the daily NSE EQ denominator from dated masters, apply the frozen prior-only liquidity screen, preserve all exclusions, and run a journal/coverage/recovery validation up to the approximately 2,335-symbol engineering envelope. Confirm resource budget and hashes.

### R1 — later profitability study

Only after D1/D2/V1/V2/U1 sign-off: lock code/config/data/universe hashes and dates, execute one pre-registered historical A/B/C comparison, report all denominators and incomplete trades, and avoid post-result tuning. This requires another explicit user approval.

## Offline validation and changed files

- Focused command: `${REPO_ROOT}/.venv/bin/python -m pytest -q tests/paper_trading` — **59 passed in 5.71 seconds**.
- Full fail-closed offline command with a fresh `PKSCREENER_TEST_ROOT` — **1,304 passed, 15 warnings in 84.89 seconds**.
- Warnings are the existing Discord `audioop` deprecation, multiprocessing fork warning and parsed legacy escape-sequence warnings.
- The first full-suite invocation omitted `PKSCREENER_TEST_ROOT`; the offline guard correctly rejected subprocess creation and pytest stopped during collection with 91 errors and no valid full-suite run. The corrected isolated invocation above is the reportable result.
- `git diff --check` is clean.
- Broker safety tests passed, including direct-mutation inventory and cross-process submission locking. The test bootstrap removed secret-like environment variables, blocked network transports, forced dry-run/paper flags and isolated runtime paths.

Phase 5 changed only:

- `paper_trading/benchmark_streaming.py`: distinct multi-session stress data, interrupt/resume exercise and checkpoint/SQLite/recovery metrics;
- `tests/paper_trading/test_streaming_checkpoint.py`: regression that proves the stress source creates distinct exchange sessions and monotonic per-symbol indices;
- `paper_trading/docs/PHASE5_DATA_PARITY_REPORT.md`: this report;
- the three `paper_trading/docs/evidence/phase5_*.csv` audit artifacts listed above.

No production service source or configuration was modified.

## Safety and final status

Protected service snapshots before and after the read-only audit show the same processes and PIDs: `ng_nse_top_gainers_watch` PID ${SERVICE_PID}, `ng_emerging_leader_alerts` PID ${SERVICE_PID} and `ng_ut_sync_leader_watch` PID ${SERVICE_PID}. No production source/configuration, scheduler, environment/session file or process was changed or restarted.

Broker/order isolation remains fail-closed in the offline test suite. Phase 5 made zero Kite API calls and zero order/GTT operations. Nothing was pushed, merged, deployed, scheduled, messaged or activated.

Final readiness: **streaming/recovery engineering ready for staged data validation; historical data, point-in-time identity and independent TradingView parity not ready; profitability replay prohibited. Await explicit approval for D1's two read-only benchmark requests only.**

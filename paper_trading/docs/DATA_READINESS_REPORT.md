# UT_SYNC A/B/C historical-data readiness report

Date: 2026-10-10  
Audit worktree: `${PAPER_WORKTREE}`  
Audit commit before report: `6b442e2b687f38b0ef503899f151874ae53ed8b0`  
Market-data source inspected read-only: `${REPO_ROOT}/results/Data`  

## Decision

The system is **not ready for a larger profitability replay**. The limiting issues are not a lack of stock filenames. They are sparse NIFTY 50 one-minute coverage, incomplete stock sessions, unresolved point-in-time identities, a zero-volume validation policy that makes many otherwise timestamp-complete sessions unusable, non-scalable in-memory replay structures, arbitrary same-timestamp symbol ordering, and unresolved Pine numeric parity.

The smallest useful approved data action is a two-request NIFTY 50 backfill for the existing July 13 through October 9 window. Stock repair should wait until the data-validation and EOD semantics below are fixed and tested; otherwise the same data could be downloaded and then rejected or misused.

No data was downloaded during this audit. No A/B/C rule, replay result, service, scheduler, paper process, Discord integration, production configuration, or broker state was changed. No Kite API or order operation was invoked.

## Evidence artifacts

The full symbol-by-session evidence is kept outside the prose report because 501 symbols x 68 sessions produces 34,068 rows.

- `evidence/nifty500_symbol_session_coverage_2026-10-10.csv`: all 501 rows in the local NIFTY 500 file across 68 observed sessions; SHA-256 `51f3120d83dfe66be7ad87047024b1804b4175d65d6fdbbb4521c3dd10e7616b`.
- `evidence/nifty100_symbol_session_coverage_2026-10-10.csv`: the 101-symbol NIFTY 100 subset; SHA-256 `9ef87ed1b797b210eb17fcb78cb806d63d5a313113d24c65a95c228f27fd8d3f`.
- `evidence/nifty_cohort_session_summary_2026-10-10.csv`: compact per-cohort/per-session totals; SHA-256 `94fe9f3508ca5c9a023bc3bbbd17853b9e1c524c49b0bd2eaaa82e90b01a9a3a`.

Each long-form matrix row records membership identity, ISIN, series, token provenance, raw and current-loader candle counts, duplicates, invalid OHLC, zero-volume rejection, opening/closing timestamps, 15:15-15:29 tail loss, prior-session warm-up, and same-date NIFTY availability. `raw_session_status` describes timestamp completeness before the simulator's zero-volume rule. `session_status` describes what the current `paper_trading.data.load_csv` path can actually use.

Source fingerprints:

| Source | SHA-256 |
|---|---|
| April NIFTY 100 file | `05c25cc4889a9bacb47db8624d295292b0a47675198a185f807f7e64568e2c34` |
| April NIFTY 500 file | `e070407d4bed56fad89a59df5ec95433cbe91c022c32f0d253914ff10a49c43b` |
| October 10 Kite NSE instrument dump | `2143cd46f0c1141711d82434c7afdf19dc93e1a01eab53a173f52aff21a3837b` |
| May 4 NIFTY 500 token map | `3224fa58b107cc39b16d48179675af15dbbcb799bf2ea812a5221a6a42e9dfaf` |

## 1. NIFTY 50 one-minute inventory across repository caches

Only one repository location contains replayable NIFTY 50 one-minute candles:

`${REPO_ROOT}/results/Data/kite_history_backfill/minute/*/NIFTY 50.csv`

It contains ten complete sessions, 375 candles per session, with no duplicate timestamps, off-session rows, or invalid OHLC:

| Session | Bars | First/last IST candle starts |
|---|---:|---|
| 2026-07-17 | 375 | 09:15 / 15:29 |
| 2026-07-27 | 375 | 09:15 / 15:29 |
| 2026-07-29 | 375 | 09:15 / 15:29 |
| 2026-08-03 | 375 | 09:15 / 15:29 |
| 2026-08-20 | 375 | 09:15 / 15:29 |
| 2026-09-15 | 375 | 09:15 / 15:29 |
| 2026-09-16 | 375 | 09:15 / 15:29 |
| 2026-10-05 | 375 | 09:15 / 15:29 |
| 2026-10-06 | 375 | 09:15 / 15:29 |
| 2026-10-09 | 375 | 09:15 / 15:29 |

Total: 3,750 unique candles. The ten-file manifest SHA-256 is `702684511128e696cef78908ee0e0cf43faf8dd7ff6df929aa4bc9ebe177a2c7`.

Other inspected caches do not extend this history:

| Cache | Finding | Replay usability |
|---|---|---|
| `kite_history_cache/minute/` | Empty: zero files | None |
| `kiteticker_candles/1minute/` | Ten June capture files, but zero `NIFTY 50` rows | None for RS |
| `emerging_leader_alerts/ut_sync.sqlite` | 496,959 completed stock bars, 481 symbols, 2026-10-05 through 2026-10-09; no NIFTY symbol | None for benchmark |
| `scalable_cache/candles_latest.json` | Metadata claims `NIFTY 50`, source `pickle`, 250 records | Metadata only; no candles in the JSON |
| `candle_store.pkl` | 328 bytes and contains no `NIFTY` identifier | Does not substantiate the metadata claim |
| `kiteticker_all_ticks_latest.json` | One NIFTY quote snapshot at 2026-10-09 10:50:03 UTC | Quote, not history |
| `PKDevTools_cache.sqlite` | One response blob, no NIFTY marker | None found |
| daily/5-minute/15-minute backfills | NIFTY files exist at coarser intervals | Cannot reconstruct genuine one-minute candles |
| replay SQLite/parity artifacts | Derived RS/decision values, not raw benchmark candles | Must not be reverse-engineered into data |

Across the 68 observed stock dates, benchmark coverage is therefore 10/68 sessions, or 14.7%. Fifty-eight dates are missing. Strategy C must fail closed on all missing dates; NIFTY cannot be forward-filled from another date or synthesized from 5-minute bars.

## 2. Stock completeness and EOD consequences

### Raw timestamp coverage

| Cohort file | Symbols represented | Session cells | Full 375 | Exact 15-bar tail loss | Other partial | Entirely missing |
|---|---:|---:|---:|---:|---:|---:|
| NIFTY 100 | 101 | 6,868 | 1,677 | 4,663 | 0 | 528 |
| NIFTY 500 | 501 | 34,068 | 19,123 | 10,014 | 2,558 | 2,373 |

For NIFTY 100, 98 symbols have at least one session with the exact pattern 09:15-15:14: 360 candles and all 15 candles from 15:15 through 15:29 absent. Only IFCI avoids this pattern among symbols with data. The missing tails total 69,945 candles.

For NIFTY 500, 212 symbols have at least one exact tail-loss session and another 174 have some other raw partial-session pattern. Missing tail timestamps total 150,279 across nonempty symbol-sessions. Six names have no cached minute rows at all: `AKZOINDIA`, `DUMMYHDLVR`, `GSPL`, `GUJGASLTD`, `LTIM`, and `RELINFRA`.

### Current-loader usability

The current loader rejects every zero-volume equity candle. That changes the matrix materially:

| Cohort | Engine-full | Engine exact-tail | Engine other-partial | Engine-missing | Rejected zero-volume rows |
|---|---:|---:|---:|---:|---:|
| NIFTY 100 | 1,589 | 4,465 | 286 | 528 | 3,860 |
| NIFTY 500 | 10,863 | 9,295 | 11,537 | 2,373 | 307,728 |

`generate_candidates` marks a whole session `DATA_GAP` after any removed internal minute. This is conservative, but the premise is unresolved: a zero-volume Kite candle with valid flat OHLC may be a legitimate no-trade minute rather than corrupt data. Before more data are requested, the project needs an evidence-backed classification using Kite response semantics and representative raw rows. A larger replay must not quietly turn this into either an automatic rejection or an invented-volume candle.

### EOD behavior

The configured EOD cut is 15:15 IST. A 15:14 candle closes at 15:15, causing the simulator to set a pending EOD exit. The fill is intentionally deferred to the next observed bar open. If 15:15-15:29 is absent, there is no same-day fill.

The current engine does not stop at the session boundary. A focused diagnostic showed that a position marked `pending_exit_reason=EOD` after the August 3 15:14 bar was filled at the August 4 09:15 open and still labelled simply `EOD` / `NEXT_OBSERVATION`. It was not labelled `NEXT_DAY_FALLBACK`, stale, or overnight carry. `EOD_EXIT_UNVERIFIED` is emitted only if the position remains open at the end of the entire dataset.

This creates four pre-replay blockers:

1. Same-day EOD fills cannot be claimed on a 360-bar session.
2. Next-session fallback can absorb an overnight gap without a distinct reason code.
3. The overnight position consumes cash, open-risk, and a position slot in the following session.
4. An end-of-dataset `EOD_EXIT_UNVERIFIED` event is timestamped with the original entry time rather than the failed exit observation time.

The initial nine-stock artifacts happened to finish with zero open positions and no EOD trades or `EOD_EXIT_UNVERIFIED` events, so this defect did not alter their completed ledger. It remains material for a broader universe.

## 3. Can the April 22 lists define a fixed cohort?

Conditionally, yes—but only as an explicitly named **April 22 snapshot cohort**, not as proof of the exact NIFTY membership on every July-October session.

Observed files:

- NIFTY 100: modified 2026-04-22 04:16:59 UTC; 101 unique symbols and ISINs, all marked `EQ`.
- NIFTY 500: modified 2026-04-22 04:16:59 UTC; 501 unique symbols and ISINs: 500 `EQ` plus `RELINFRA` marked `BE`.
- `DUMMYHDLVR` is present in both, with dummy ISIN `DUM030A01027`.
- Every NIFTY 100 row is present in the NIFTY 500 file.

The extra rows are not safe to delete merely to force the labels to 100/500. Dummy securities and temporary series changes can arise from index/corporate-action maintenance. NSE Indices states that broad indices are reviewed semi-annually, normally effective on the last working day of March and September, and may also undergo ad-hoc changes for suspension, delisting, or schemes of arrangement. See the official [reconstitution calendar](https://www.niftyindices.com/resources/index-rebalancing-schedule) and [equity-index methodology](https://www.niftyindices.com/Methodology/Method_NIFTY_Equity_Indices.pdf).

The April files can avoid survivorship bias if all of the following are frozen before results are viewed:

1. The cohort selection date is April 22, not retroactively July or October.
2. All 101/501 rows remain in the denominator, including later missing, renamed, suspended, BE-series, merged, or dummy constituents.
3. No missing name is replaced by its successful successor without a dated corporate-action rule.
4. The evaluation begins after April 22; July 27 is the earliest sensible existing-data evaluation date after ten July 13-24 warm-up sessions.
5. The report calls this an April-snapshot fixed cohort, not a dynamic NIFTY 100/500 membership replay.

The filesystem modification time alone does not establish the list's official effective date or prove there were no ad-hoc changes before July. For a defensible cohort, obtain and hash:

- Official NIFTY 100/500 constituent snapshots effective March 31, July 6/13, September 30, and each evaluation date if a dynamic cohort is desired.
- NSE Indices constituent-change circulars, including demerger/dummy-security treatment.
- Daily or event-dated NSE security masters with symbol, ISIN, series, listing/delisting/suspension dates, and corporate-action successor links.
- Archived Kite instrument dumps/token maps for each identity epoch, plus point-in-time tick sizes.

Current identity reconciliation illustrates the need:

| Cohort | Current exact symbol/token | Archived-token only | Unresolved |
|---|---:|---:|---:|
| NIFTY 100 | 99 | 0 | 2: `DUMMYHDLVR`, `LTIM` |
| NIFTY 500 | 492 | 6 | 3: `AKZOINDIA`, `DUMMYHDLVR`, `LTIM` |

Archived-only names are `GSPL`, `GUJGASLTD`, `HEG`, `HFCL`, `JBCHEPHARM`, and `RELINFRA`. The October instrument dump exposes related current names such as `HEGAM`, `HFCL-BE`, and `RELINFRA-BE`, but symbol similarity is not sufficient evidence for a causal mapping. `SCHNEIDER` also has conflicting May and October tokens (`136739588` versus `7995905`). ISIN-aware, dated resolution is required.

## 4. Coverage and warm-up interpretation

The matrix spans 68 observed sessions from July 6 through October 9. The broad store begins substantially on July 13; July 6-10 contains only about 127 NIFTY 500 members per day. A clean fixed-window study should therefore use:

- Warm-up: July 13 through July 24 (ten sessions).
- Earliest evaluation date: July 27.
- End date: October 9.

This is a minimum, not proof of full indicator convergence. It enables the configured RVOL minimum of ten prior same-minute observations only where each required minute exists. Matrix field `rvol_10_prior_sessions_ready` is a session-level upper bound; the engine must still check the exact minute. `warmup_100_prior_bars_ready` covers the 20 completed five-minute EMA's nominal bar requirement but does not prove exact recursive UT/PSAR state parity. A production-quality historical study should obtain at least 20 complete prior sessions and exclude them from evaluation.

The ten benchmark dates create 1,010 NIFTY 100 and 5,010 NIFTY 500 symbol-session cells with full RS inputs. The other 5,858 and 29,058 cells respectively have no benchmark at all.

## 5. Scaling analysis

The present replay is O(total bars) in several simultaneous Python object graphs:

- all CSV candles are loaded into `dict[str, list[Candle]]`;
- full indicator timelines are retained for every symbol;
- all candidates are retained in a dictionary;
- all observations are materialized again as tuples and globally sorted;
- parity CSV is generated for every symbol after replay.

The nine-stock input held 210,615 stock bars. The current NIFTY 500 cache is roughly 11-12 million raw bars—about 55 times larger—before indicator snapshots, candidate values, observation tuples, journal payloads, and exports. A naive run is likely to require many gigabytes and can plausibly reach the tens-of-gigabytes range. It should not be attempted as the next validation step.

Required scalable design, without changing A/B/C predicates:

1. Normalize and validate source files into immutable symbol/session partitions with hashes.
2. Compute per-symbol indicators and common candidates in bounded batches, persisting compact candidate/quality streams rather than retaining every snapshot.
3. Replay one market session at a time through a k-way chronological merge. Retain only rolling indicator state, the current session, open positions, pending intents, three portfolio states, and marks.
4. At each timestamp, process a batch in fixed phases: pending exits/fills, bar range and confirmed-close exits, marks/equity, then all new candidate decisions.
5. Resolve simultaneous candidates with a predeclared allocation rule. Current lexicographic symbol ordering is deterministic but gives alphabetically earlier symbols first access to three position slots and risk capital.
6. Checkpoint only at completed session boundaries. Include source-partition hashes and the allocation-policy version in the run ID.
7. Batch SQLite commits and stream reports; make full per-bar parity export opt-in for a small audit sample.

This preserves chronological portfolio-level capital, open-risk, daily-loss, and concurrency accounting. Independent per-symbol replays followed by combining trades later would be invalid because they bypass shared portfolio constraints.

## 6. Simulator and Pine-parity blockers

These must be resolved before a larger replay, without optimizing the trading rules:

1. **Session-end transition:** detect missing same-day next observations; emit `EOD_EXIT_UNVERIFIED`, then separately apply and label a conservative `NEXT_DAY_FALLBACK` if configured.
2. **Complete-session gate:** leading/trailing gaps are not detected by the existing adjacent-row gap scan. A 360-bar session is currently eligible for A/B candidates until an EOD position encounters the missing tail.
3. **Zero-volume policy:** establish whether valid-OHLC zero-volume equity rows are no-trade candles, missing trades, or corrupt. Keep a full minute timeline and treat price/volume eligibility separately if official semantics support that conclusion.
4. **Global duplicate handling:** `load_csv` deduplicates only within one file; `merge_csvs` can retain overlapping timestamps from range files. Expansion/backfill merging needs token/symbol/timestamp uniqueness and conflict logging.
5. **Requested-date filtering:** CLI folder filtering uses the first date in a range-directory name and does not filter rows after loading. A July 6-10 file can cross calibration/validation boundaries.
6. **Per-instrument tick size:** Python uses one global ₹0.05 tick, while Pine uses `syminfo.mintick`. The current instrument dump contains varying tick sizes. Historical point-in-time tick size must enter stops, fills, risk, and provenance.
7. **Identity persistence:** replay candles/events do not carry instrument token or ISIN even though the specification requires instrument identity in the ledger.
8. **Same-timestamp portfolio ordering:** marks, conservative open loss, fills, and candidate acceptance are updated symbol by symbol. Results can depend on alphabetical symbol order.
9. **Missing-next-bar intent expiry:** fill delay counts the next available symbol bar, not necessarily the next chronological minute; a gap can produce a late fill without a stale/unfilled reason.
10. **Indicator seed/parity evidence:** ATR/RMA, PSAR, DMI/ADX, recursive UT, and especially the completed-five-minute EMA need numeric comparison against an exported TradingView series. Deterministic fixtures alone do not prove Pine parity.
11. **Realtime irreducibility:** Pine `barstate.isrealtime`, `varip`, live drift, tick-observed peaks, and alert-creation state remain unreconstructable from OHLC. Historical results must retain the bar-close reconstruction label.
12. **Fees:** current formula tests do not replace parity against an anonymized 2026 contract note's aggregation and rounding.
13. **Corporate actions:** split/demerger/rename handling and raw-versus-adjusted price policy are not yet attached to the data manifest.

These are correctness and data-contract changes, not A/B/C optimization. The frozen strategy thresholds should remain unchanged.

## 7. Smallest necessary backfill

Kite documents a 60-calendar-day maximum for a one-minute historical request and a historical-data limit of 3 requests/second. The repository's existing conservative throttle is 0.7 seconds between calls, about 1.43 requests/second. Official references: [historical API](https://kite.trade/docs/connect/v3/historical/), [rate limits](https://kite.trade/docs/connect/v3/exceptions/), and [60-day interval limit](https://kite.trade/forum/discussion/3105/historical-api-frequency-and-limit-on-number-of-candles-fetched).

Recommended order:

### Stage D0: no API, approval-independent correctness work

Resolve blockers 1-9 above, add session/calendar manifests, freeze point-in-time identity policy, and test a synthetic 100/500-symbol chronological stream. Do not compute profitability.

### Stage D1: benchmark only

After approval, fetch NIFTY 50 from July 13 through October 9 in two <=60-day requests. Merge by token/timestamp and preserve the ten existing files for comparison. Expected requests: **2**. At 0.7-second spacing, throttle time is about 1.4 seconds plus API/validation overhead.

This is the highest-value acquisition: it changes benchmark coverage from 10/63 broad sessions to the complete chosen window and unlocks causal Strategy C RS validation without selecting stocks.

### Stage D2: NIFTY 100 repair

Using the April fixed cohort and July 13 warm-up start, a greedy grouping of all raw-incomplete dates into <=60-day windows needs **195 stock requests** across the 99 token-resolved names. Add the two benchmark requests for **197 total**. Two unresolved names remain data-quality exclusions, not silently removed constituents.

For comparison, a simpler two-full-range refresh would take 198 stock calls plus two benchmark calls, or **200 total**. At the existing throttle, 197 calls require about 2 minutes 18 seconds of intentional spacing; the 3 requests/second theoretical floor is about 66 seconds, excluding response and retry time.

### Stage D3: NIFTY 500 repair

Using July 13 as warm-up start, the exact matrix's raw deficiencies can be covered by **700 stock request windows** for currently or historically token-resolved names. Add two benchmark calls for **702 total**. Three unresolved identities must remain explicit exclusions pending point-in-time mapping.

A simpler two-range refresh of all 498 token-resolved cohort rows would be 996 stock calls plus two benchmark calls, or **998 total**. At 0.7 seconds, targeted 702-call throttle time is about 8 minutes 11 seconds; actual elapsed time will be longer.

The targeted counts are planning estimates, not authorization to call Kite. They assume each archived token remains accepted for historical GETs and that no retry is needed. Subscription/session validity was not tested.

Do not request one candle per missing symbol-date: the endpoint is per instrument, so that approach would multiply calls. Use at most two bounded ranges per affected symbol and merge only previously missing/conflicting timestamps after validation.

## 8. Approval-ready validation plan

1. **Approve correctness-only implementation.** Add no rule changes. Fix date filtering, global deduplication, complete-session gating, EOD/fallback reason codes, per-instrument ticks, identity provenance, same-timestamp batching, and stale intent expiry.
2. **Approve data-contract tests.** Add fixtures for 360-bar tails, missing 09:15, one internal zero-volume candle, legitimate no-trade candles if verified, overlapping caches, cross-range date filters, token changes, 15:15 fill, absent 15:15, and next-day fallback.
3. **Approve streaming architecture.** Demonstrate identical event/trade hashes between old and streaming engines on small complete fixtures, then measure memory on synthetic 100- and 500-symbol no-trade streams. No profitability statistics.
4. **Approve D1 only.** Make two read-only NIFTY historical GETs. Validate 375 timestamps/session, timezone, gaps, duplicates, and hashes before accepting the cache.
5. **Rebuild the matrix.** Confirm benchmark coverage and determine whether any stock backfill is still necessary for the chosen evaluation window.
6. **Approve D2.** Backfill/repair the fixed April NIFTY 100 cohort, preserve unresolved names in the denominator, then run formula/parity and accounting validation only.
7. **Parity gate.** Compare a sample across price/tick/volume regimes against genuine TradingView per-bar exports, including UT trail, PSAR, VWAP, ATR, confirmed 5-minute EMA, ADX/DMI, QQC, CHOP, RVOL, RS, ORH, stops, and timestamps.
8. **Approve D3 only after D2 passes.** Repair NIFTY 500 and rerun data-readiness/memory/idempotency tests.
9. **Separate later approval for profitability.** Freeze cohort, windows, costs, allocation tie-breaker, and all A/B/C rules before any out-of-sample profitability replay.

## Approval gates

The next safe approval can be narrowly phrased as:

> Implement the data-readiness correctness and streaming changes in the isolated UT_SYNC worktree, without changing A/B/C predicates or running profitability. Add tests and return for review before any API use.

After that passes, a separate approval can authorize exactly two NIFTY 50 historical GET requests. No order, GTT, account mutation, paper activation, deployment, service change, merge, or Discord send is required for either gate.


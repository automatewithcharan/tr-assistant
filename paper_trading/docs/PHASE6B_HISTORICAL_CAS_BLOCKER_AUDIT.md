# Phase 6B — Historical CAS blocker audit and acquisition manifest (sanitized)

## Decision

**Readiness for a 20-session A/B/C replay today: NO.**

The price-data topology is sufficient for the fixed cohort, but 1,540 of 1,560 post-CAS cohort sessions lack dated, authoritative CAS/non-CAS classification. No profitability artifact was opened, no replay was run, and no strategy or execution setting was changed.

The earliest evaluation window is now frozen, before any outcome inspection:

- indicator/RVOL warm-up: the ten actual NSE sessions from **2026-07-20 through 2026-07-31**;
- evaluation: the first 20 actual NSE sessions after CAS go-live, **2026-08-03 through 2026-08-28**;
- universe: the existing 78-symbol `PHASE6A_NIFTY100_APRIL22_CURRENT_EXACT_COMPLETE_78` cohort, unchanged;
- execution: the previously frozen original closed-bar/next-observation model, not the defective broker-realistic sensitivity.

The classification blocker is bounded. Twenty dated CM-MII masters are the minimum data payload. The schema circular and go-live circular bring the approval package to 22 files/GETs. Retrieval and parsing require separate approval.

## Authoritative-source findings

NSE's [CAS page](https://www.nseindia.com/static/products-services/closing-auction-session) identifies circular `NSE/CMTR/73845`, dated 2026-04-22, as the security-master modification and records the CAS framework. NSE's [public All Reports catalog](https://www.nseindia.com/all-reports) exposes `CM - MII - Security File (.gz) (NSE Listed securities)` and supports dated historical reports. Circular [NSE/MSD/60315](https://nsearchives.nseindia.com/content/circulars/MSD60315.pdf) establishes the public filename convention `NSE_CM_security_ddmmyyyy.csv.gz` and daily website dissemination.

The official [go-live circular NSE/CMTR/75479](https://nsearchives.nseindia.com/content/circulars/CMTR75479.pdf) states that CAS became live on 2026-08-03 and explicitly names `NSE_CM_security_31072026.csv.gz` as the master to load for the August 1 mock and August 3 live session. This establishes the previous-trading-day applicability used below. Later files in the manifest are therefore mapped to the next trading session, not retrospectively to their filename date.

The original `NSE/CMTR/73845` package is publicly linked as [CMTR73845.zip](https://nsearchives.nseindia.com/content/circulars/CMTR73845.zip). A [secondary indexed rendering of that circular](https://rhnvrm.github.io/stock-market-circulars/circulars/nse/2026/nse-2026-04-22-6d4314ce0c300da0-modification-in-security-master-files-on-account-of-introduction-of-closing-auct/) says that the MII CSV's field 24 changes from RETDEBT eligibility to **Eligibility for Closing Auction Session**, with ISO tag `ElgbltyClsgAuctnSsn` and binary `0/1` values. Because the official package was not downloaded in this audit, those field details are treated as schema expectations to be verified against the signed circular and each acquired CSV—not as already locally proven facts.

The daily MII master is the correct evidence type: it is security-level, dated, includes stable identity fields, and carries the CAS eligibility indicator. In principle it can classify both CAS and non-CAS rows. It is not enough merely to find a symbol in a closing-auction transaction report, and absence from such a report is never interpreted as non-CAS.

## Frozen cohort and cache reconciliation

The reconciliation used the frozen cohort manifest and the Phase 5E NIFTY-100 coverage ledger, without reading trade outcomes:

| Item | Result |
|---|---:|
| Cohort symbols | 78 |
| Warm-up sessions | 10 |
| Warm-up symbol-sessions | 780 / 780 present, all 375 candles |
| Evaluation sessions | 20 |
| Evaluation symbol-sessions | 1,560 / 1,560 present |
| Instrument identity | 1,560 `CURRENT_EXACT`; 0 unresolved in this cohort/window |
| Dated-authoritative CAS today | 20 (`ADANIENSOL`, all 20 sessions) |
| Unknown without dated master | 1,540 |
| Unknown sessions with 360 candles, 09:15–15:14 | 1,520 |
| Unknown sessions with 375 candles, 09:15–15:29 | 20 (`BAJAJHFL`) |
| Benchmark warm-up | 3,750 candles / 10 complete sessions |
| Benchmark evaluation | 7,500 candles / 20 complete sessions |

The normalized NIFTY 50 cache contains 25,500 candles across 68 complete sessions from 2026-07-06 through 2026-10-09. Its SHA-256 remains `81ade633fabd530253c388df62b6cb53215723ad205adc1389c28c18ed938c68`. The older per-symbol Phase 5E matrix contains stale benchmark flags and must not override the directly validated normalized benchmark cache.

The 1,520 exact 360-candle shapes are evidence-consistent with CAS but are not classifications. If a dated master says `CAS=1`, an exact 09:15–15:14 sequence can be complete. If it says `CAS=0`, the absent 15:15–15:29 interval is a genuine 15-candle deficit. Conversely, `BAJAJHFL`'s 375-candle sessions remain unknown until the master says `CAS=0`; observed late candles alone do not prove the status. Any symbol/series/ISIN mismatch stays unresolved rather than being guessed.

The wider Phase 5E record is unchanged: 210 symbols/9,918 topology-inferred sessions remain unresolved, and the classification-independent repair floor remains 2,335 sessions/34,464 candles until dated masters are applied. The 16 previously blocked archived-token or unresolved-identity windows remain blocked. This audit does not relabel or remove any of them.

## Minimum acquisition manifest

### Reference package

| ID | Kind | Exact URL | Expected identifier | Purpose |
|---|---|---|---|---|
| `CAS6B-REF-01` | circular ZIP | `https://nsearchives.nseindia.com/content/circulars/CMTR73845.zip` | `NSE/CMTR/73845`, 2026-04-22 | Signed field-24 CAS schema and value semantics |
| `CAS6B-REF-02` | circular PDF | `https://nsearchives.nseindia.com/content/circulars/CMTR75479.pdf` | `NSE/CMTR/75479`, 2026-07-30 | Go-live date and July 31 master applicability |

### Twenty classification masters

All URLs are the NSE-listed-only CM master, not the broader NSE+BSE-exclusive variant.

| ID | Master date | Applies to evaluation session | Expected filename and exact URL |
|---|---|---|---|
| `CAS6B-M01` | 2026-07-31 | 2026-08-03 | [`NSE_CM_security_31072026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_31072026.csv.gz) |
| `CAS6B-M02` | 2026-08-03 | 2026-08-04 | [`NSE_CM_security_03082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_03082026.csv.gz) |
| `CAS6B-M03` | 2026-08-04 | 2026-08-05 | [`NSE_CM_security_04082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_04082026.csv.gz) |
| `CAS6B-M04` | 2026-08-05 | 2026-08-06 | [`NSE_CM_security_05082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_05082026.csv.gz) |
| `CAS6B-M05` | 2026-08-06 | 2026-08-07 | [`NSE_CM_security_06082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_06082026.csv.gz) |
| `CAS6B-M06` | 2026-08-07 | 2026-08-10 | [`NSE_CM_security_07082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_07082026.csv.gz) |
| `CAS6B-M07` | 2026-08-10 | 2026-08-11 | [`NSE_CM_security_10082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_10082026.csv.gz) |
| `CAS6B-M08` | 2026-08-11 | 2026-08-12 | [`NSE_CM_security_11082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_11082026.csv.gz) |
| `CAS6B-M09` | 2026-08-12 | 2026-08-13 | [`NSE_CM_security_12082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_12082026.csv.gz) |
| `CAS6B-M10` | 2026-08-13 | 2026-08-14 | [`NSE_CM_security_13082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_13082026.csv.gz) |
| `CAS6B-M11` | 2026-08-14 | 2026-08-17 | [`NSE_CM_security_14082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_14082026.csv.gz) |
| `CAS6B-M12` | 2026-08-17 | 2026-08-18 | [`NSE_CM_security_17082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_17082026.csv.gz) |
| `CAS6B-M13` | 2026-08-18 | 2026-08-19 | [`NSE_CM_security_18082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_18082026.csv.gz) |
| `CAS6B-M14` | 2026-08-19 | 2026-08-20 | [`NSE_CM_security_19082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_19082026.csv.gz) |
| `CAS6B-M15` | 2026-08-20 | 2026-08-21 | [`NSE_CM_security_20082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_20082026.csv.gz) |
| `CAS6B-M16` | 2026-08-21 | 2026-08-24 | [`NSE_CM_security_21082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_21082026.csv.gz) |
| `CAS6B-M17` | 2026-08-24 | 2026-08-25 | [`NSE_CM_security_24082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_24082026.csv.gz) |
| `CAS6B-M18` | 2026-08-25 | 2026-08-26 | [`NSE_CM_security_25082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_25082026.csv.gz) |
| `CAS6B-M19` | 2026-08-26 | 2026-08-27 | [`NSE_CM_security_26082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_26082026.csv.gz) |
| `CAS6B-M20` | 2026-08-27 | 2026-08-28 | [`NSE_CM_security_27082026.csv.gz`](https://nsearchives.nseindia.com/content/cm/NSE_CM_security_27082026.csv.gz) |

On 2026-10-12, a body-free HTTPS `HEAD` check returned `200 OK` and `Content-Type: application/x-gzip` for all 20 exact master URLs. Reported sizes ranged from 1,204,210 to 1,268,904 bytes. These checks establish public addressability, not file integrity or contents. No master body was downloaded.

## Acquisition and validation controls

If separately approved, the bounded acquisition should perform exactly 22 HTTPS `GET` requests: two reference files and 20 masters. It must use no Kite credentials and no broker API.

1. Allow only the exact URLs above; reject redirects outside `nseindia.com`/`nsearchives.nseindia.com` and reject any filename/date mismatch.
2. Save immutable raw bytes with request UTC time, final URL, status, content length/type, ETag/Last-Modified where present, and SHA-256. Do not use `Last-Modified` as the applicability date.
3. Enforce conservative size caps, verify PDF/ZIP/gzip magic, run ZIP path-traversal checks and `gzip -t`, and parse CSV without formula execution.
4. Verify the signed `73845` schema before interpreting data: MII CSV field 24, name `Eligibility for Closing Auction Session`, ISO tag `ElgbltyClsgAuctnSsn`, and allowed `0/1` domain. Stop on any disagreement.
5. Verify each master's internal batch/date metadata and the previous-trading-day applicability rule. If the file indicates same-day rather than next-session applicability, stop and rebuild the mapping before classification; do not shift silently.
6. Join cohort securities by exact symbol, `EQ` series and ISIN, then reconcile the recorded instrument token. Require one unambiguous equity row per symbol/session. Preserve renamed, missing, duplicate and ISIN-conflicting rows as `UNKNOWN`.
7. Classify `1` as dated-authoritative CAS and `0` as dated-authoritative non-CAS only after the schema check. Never use transaction-report absence as evidence.
8. Recompute expected minutes: 360 for verified CAS, 375 for verified non-CAS. Compare exact timestamp sets, not merely row counts. Preserve invalid OHLC, duplicates, internal gaps and true missing tails separately.
9. Require all 1,560 cohort-session classifications and all expected stock timestamps before declaring the frozen window data-ready. Any master-proven non-CAS 360-row session becomes a 15-candle repair blocker; any conflicting 375-row CAS session requires source investigation.
10. Regenerate and hash the classification ledger and coverage matrix before a replay approval. Do not inspect profitability while resolving data eligibility.

## Bias and readiness assessment

The 78 symbols come from a dated 2026-04-22 NIFTY-100 snapshot and were frozen before this proposed evaluation's outcomes. That avoids performance-based post-selection in this audit, but the cohort still has surviving-snapshot, current-token-continuity and future-data-completeness selection bias: it was reduced to names with usable cached data. A future run must remain labelled exploratory and cannot be generalized to all historical NSE EQ.

The existing cache can *conditionally* support the 20-session window: all 780 warm-up and 1,560 evaluation stock sessions are present, all identities are current-exact, and the benchmark is complete. It cannot support it **yet** under the project's evidence standard because 1,540 session calendars are unresolved.

Approval of this manifest would authorize evidence acquisition, not a replay. After acquisition, readiness becomes **YES only if** every cohort row maps unambiguously and every master-proven session has its full expected timestamp set. Otherwise the answer remains NO and the newly exposed genuine gaps form a separate, exact stock-candle repair manifest.

## Verification and safety record

- Offline cohort/matrix reconciliation: 78 symbols × 20 sessions = 1,560 rows, exact.
- Warm-up reconciliation: 78 symbols × 10 sessions = 780 complete pre-CAS rows.
- Normalized benchmark: 30/30 frozen warm-up/evaluation dates complete, 11,250 candles.
- Public availability check: 20/20 master URLs returned body-free `HEAD 200` with gzip media type.
- Master/circular downloads: **0**.
- Kite API calls: **0**.
- Profitability replays or outcome inspection: **0**.
- Strategy, simulator, production-service, Discord, deployment, merge and feature-branch changes: **0**.
- Real Kite order, GTT, modification or cancellation operations: **0**.

Source hashes used for the offline reconciliation:

| Artifact | SHA-256 |
|---|---|
| Frozen Phase 6A cohort manifest | `b42e07e071adc32e770f3667f155106c6a9f4e18cf7a5f377532e606d865667a` |
| Phase 5E NIFTY-100 CAS coverage ledger | `d6a0c1e78e4159ebdf8cfedb69c9086e30081c678f5a95a7088e8d08ba0d6098` |
| Normalized NIFTY 50 benchmark | `81ade633fabd530253c388df62b6cb53215723ad205adc1389c28c18ed938c68` |

Final status: **NO — not ready today.** The remaining blocker is exact and acquisition-ready: 20 dated public CM-MII masters plus two provenance circulars, followed by fail-closed validation. No data file was downloaded and no backtest was run.

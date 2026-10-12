# Phase 5E — CAS-aware calendar and realistic EOD execution readiness

Audit date: 2026-10-11

Finalized: 2026-10-12

Starting feature HEAD: `716b0aa50feb33174614894c767d8062347b5d41`

Implementation/evidence commit: `fb03fad2decd257f607e6fe3d7ca0ed51d6d5672`

Scope: offline data-readiness and execution-model work only

## Decision

The broad-NSE A/B/C profitability replay is **not ready**. The calendar and a separately labelled broker-realistic EOD sensitivity are now implemented and deterministically tested, but local data cannot resolve historical CAS membership for most post-2026-08-03 sessions.

The stricter Phase 5E evidence standard changes the Phase 5D “verified/composite” treatment:

- only `360ONE` and `ADANIENSOL` have dated authoritative derivative-eligibility evidence in the collected record; their 96 post-CAS sessions and 1,440 apparent tail cells are confirmed CAS;
- `ABB`, `ABCAPITAL`, and `ADANIENT` remain strongly CAS-consistent but move back to the inferred tier because no locally retained dated 2026-07-31 or daily master proves their status on each replay date;
- in total, 210 symbols / 9,918 sessions / 148,770 apparent tail cells remain topology-inferred and unresolved;
- no post-CAS session is labelled verified non-CAS from a modern list or from the presence of late candles alone.

The classification remains fail-closed. A session ending at 15:14 is not promoted merely because its shape resembles CAS.

## Safety boundary and protected services

- External downloads: **0**.
- Kite/API requests: **0**.
- Orders, modifications, cancellations, GTT, positions or broker execution calls: **0**.
- Authentication/session-file changes: **0**.
- Profitability replays or A/B/C parameter changes: **0**.
- Live-paper activation, Discord, deployment, scheduling, merge or push: **none**.
- Production-service writes or process-control operations: **0**.

The production processes were inspected read-only. On the 2026-10-11 pre-commit check, the UT-SYNC watcher was still PID ${SERVICE_PID} with its original 12:16:37 UTC start time and command line. After the environment/date rollover, the final 2026-10-12 check showed the corresponding top-gainers, alerts and UT-SYNC watchers running as PIDs ${SERVICE_PIDS}, all started at 03:05:48 UTC; other production CLI/ticker/monitor processes were also present. The PID/start-time change happened outside this work. This phase issued no signal, restart, process-control command or production-path write, so it does not claim uninterrupted service continuity across that external rollover. The implementation is confined to `paper_trading`, its offline tests, scripts and research evidence.

## 1. Historical CAS classification evidence

### Local inventory

A recursive inventory across repository caches under `${USER_HOME}` found:

- historically dated `NSE_CM_security_DDMMYYYY.csv.gz` files: **0**;
- historically dated `NSE_FO_contract_DDMMYYYY.csv.gz` files: **0**;
- three NSE Kite instrument files, all explicitly named `kite_instruments_nse_latest.csv`.

The latest-only snapshots are documented with absolute path, byte size, row count, modification time and SHA-256 in `evidence/phase5e_local_master_inventory_2026-10-11.json`. They are useful for current identity reconciliation but cannot establish historical CAS membership and were not applied retrospectively.

### Evidence tiers

| Phase 5E class | Symbols/sessions | Apparent tail cells | Completeness treatment |
|---|---:|---:|---|
| Confirmed CAS, dated authoritative | 2 / 96 | 1,440 | 360 expected; exact 09:15–15:14 session can be complete |
| Inferred CAS, topology/corroboration only | 210 / 9,918 | 148,770 | expected count unresolved; not complete |
| Pre-CAS regular deficit | — / 1,922 | 28,774 | 375 expected; repair required |
| Post-CAS non-topology/identity deficit | 16 / 413 | 5,690 | unresolved; repair/identity work required |
| Repaired/no current tail gap | — / 80 | 0 | no tail request |
| Verified post-CAS non-CAS | 0 / 0 | 0 | none can be proven from local dated evidence |

The 12,429-row decision ledger is `evidence/phase5e_cas_classification_2026-10-11.csv`. It preserves the Phase 5B/5C raw observations, tokens and request IDs alongside the stricter Phase 5E classification and finality.

### Nine Phase 5C post-CAS request findings

The nine responses still demonstrate that continuous candles ended at 15:14 with no source conflicts. Under the stricter point-in-time standard:

- `360ONE` and `ADANIENSOL` are confirmed CAS by dated eligibility evidence;
- `ABB`, `ABCAPITAL`, and `ADANIENT` remain inferred CAS pending the dated cash-security master.

Repeating those post-CAS tail requests is not justified. Their response topology is preserved as evidence, but topology alone is not membership truth.

## 2. Corrected completeness and backfill requirements

| Accounting view | Gap sessions | Missing/apparent tail cells |
|---|---:|---:|
| Phase 5B blanket-375 baseline | 12,429 | 185,874 |
| After Phase 5C local repairs | 12,349 | 184,674 |
| Phase 5E strict confirmed-CAS correction | 12,253 | 183,234 |
| Within that total: inferred CAS evidence hold | 9,918 | 148,770 |
| Classification-independent repair floor | 2,335 | 34,464 |

The repair floor remains **372 bounded request windows across 363 symbols**, covering 1,922 pre-CAS sessions plus 413 post-CAS non-topology sessions. It is in `evidence/phase5e_minimum_stock_requests_2026-10-11.csv` and is explicitly marked `NOT_AUTHORIZED_PHASE5E`. Sixteen identity-blocked windows remain subject to archived-token or unresolved-identity work; no identity was guessed.

The revised matrices are:

- `evidence/phase5e_nifty100_cas_coverage_2026-10-11.csv`: 6,868 symbol-session rows;
- `evidence/phase5e_nifty500_cas_coverage_2026-10-11.csv`: 34,068 symbol-session rows.

Each row retains the earlier raw/usable candle, warm-up, identity and benchmark columns and adds Phase 5E evidence, expected continuous count, corrected missing count and final/unresolved status. Post-CAS rows without dated evidence remain unresolved even when 360 or 375 candles are observed.

### Exact historical-reference acquisition plan — separate approval required

`evidence/phase5e_historical_master_acquisition_manifest_2026-10-11.csv` contains 51 exact records:

1. circular `NSE/CMTR/73845` for the CAS master schema;
2. circular `NSE/CMTR/75479` for go-live/effective-date instructions;
3. baseline `NSE_CM_security_31072026.csv.gz` for the 2026-08-03 transition;
4. one named `NSE_CM_security_DDMMYYYY.csv.gz` file for each of the 48 cached post-CAS trading dates from 2026-08-03 through 2026-10-09.

Every record names the official NSE catalog, purpose, expected filename/date, validation requirements and `NOT_AUTHORIZED_PHASE5E` status. A future authorized retrieval must preserve raw gzip/PDF bytes, provenance and SHA-256; validate schema, symbol-series-ISIN uniqueness and effective-date continuity; and stop on ambiguity. Daily files may later be reduced to a baseline plus authoritative change records only if those change records are complete and independently validated.

## 3. Instrument/date session calendar

`paper_trading/session_calendar.py` now provides immutable dated classification records and an `InstrumentSessionCalendar` with a stable content fingerprint.

| Date/classification | Continuous interval | Expected minutes | Broker cutoff start |
|---|---|---:|---|
| Pre-CAS normal trading day | 09:15–15:30 | 375 | 15:25 |
| Verified CAS on/after 2026-08-03 | 09:15–15:15 | 360 | 15:12 |
| Verified non-CAS on/after 2026-08-03 | 09:15–15:30 | 375 | 15:25 |
| Inferred/unknown after effective date | unresolved | unresolved | unresolved/fail closed |
| Explicit holiday | none | 0 | none |

Conflicting overlapping records resolve to `UNKNOWN`, never whichever row happens to load last. Opening-auction observations before 09:15 and CAS auction/post-continuous observations from 15:15 are outside the continuous execution window.

## 4. Broker-realistic EOD execution sensitivity

The original A/B/C path remains the default `FROZEN_ORIGINAL` policy with the frozen `eod_minute = 15:15`; signal definitions and parameter files are unchanged.

The new opt-in policy is explicitly named `BROKER_REALISTIC_SENSITIVITY`:

- verified CAS: after the candle observable at 15:11, submit a simulated intent for the next continuous observation, with a hard 15:12 deadline;
- verified non-CAS and pre-CAS regular sessions: corresponding decision at 15:24 and hard 15:25 deadline;
- a fill is accepted only at a liquid next-observation bar open strictly before the deadline, using the existing adverse spread/slippage function and unchanged fees, liquidity participation, exposure and risk controls;
- the cutoff is not treated as a fill. Zero liquidity, a missing next observation, reaching the cutoff with an open position, end of data, or session boundary creates an auditable `INCOMPLETE_TRADE` and reverses entry accounting rather than fabricating P&L;
- unknown EOD classification fails closed at the conservative CAS boundary instead of assuming either schedule;
- closing-auction and post-continuous bars cannot trigger entries, stops or ordinary next-bar fills;
- UT/PSAR exits retain precedence and use the same applicable cutoff deadline; gap-through and hard-stop handling retain their established ordering while a credible continuous window remains;
- no position can cross a session boundary.

Policy value and calendar fingerprint are included in both batch and streaming run IDs, journal metadata, summaries and checkpoint compatibility checks. A checkpoint created for one policy/calendar cannot resume under another. A/B/C still receive the same market observations and retain separate portfolio state and accounting.

This policy is an execution-model correction/sensitivity only. It is not a strategy optimization, and no return comparison was run.

## 5. Deterministic validation

Focused tests cover:

- last pre-CAS behavior, effective-date verified CAS, verified non-CAS, inferred/unknown and explicit holiday cases;
- 15:12 and 15:25 MIS boundaries, causal next-observation fills and missing/zero-liquidity final observations;
- opening/closing auction separation and prevention of post-cutoff stop fills;
- gap-through stop precedence and signal exits under the broker deadline;
- frozen-original path preservation;
- batch/streaming event and accounting equivalence under the broker policy;
- near-EOD checkpoint interruption/restoration with the same policy/calendar fingerprint;
- existing broker-mutation tripwire and package security tests, which preserve zero Kite order paths.

Verification results:

- focused Phase 5E/calendar/engine/streaming/Phase 5B/5C set: **86 passed**;
- complete offline repository suite: **1,333 passed**, 15 pre-existing warnings, 133.46 seconds;
- Python compilation for changed modules and generator: passed;
- deterministic evidence regeneration and `git diff --check`: passed.

The batch and streaming test outputs match after normalizing run IDs. Checkpoint restoration matches uninterrupted broker-policy output.

## 6. Broad-universe research protocol

No profitability run is authorized by this protocol. After evidence and data approval, the next study should use:

1. **Historically valid eligibility:** NSE EQ series, dated listing/trading status, dated symbol/ISIN/token continuity and point-in-time universe membership. Current lists may be reported only as explicitly biased engineering cohorts.
2. **Causal liquidity:** price floor and liquidity gates based only on information available before each decision; include renamed, suspended and delisted securities rather than selecting survivors.
3. **Equal market data:** identical stock and NIFTY benchmark observations, warm-up policy and data exclusions for A/B/C.
4. **Costs:** the same configured brokerage, taxes, spread, slippage, participation, exposure and risk limits across variants; disclose sensitivity assumptions without tuning signals.
5. **Two EOD labels:** frozen-original 15:15 research definition and the separately labelled broker-realistic policy. Never blend their trades or choose the better result post hoc.
6. **Missing data:** retain censored/incomplete trades in denominators and report their reason distribution. Do not fill missing minutes, convert auction prices into continuous bars, or discard adverse unresolved positions.
7. **Chronology:** locked development period followed by untouched chronological out-of-sample windows; no future membership, liquidity or benchmark values.
8. **Statistics:** report per-symbol, per-session and portfolio-level counts, exposure, turnover, resolved/incomplete trades, gross/net P&L, R, drawdown, concentration and bootstrap/temporal uncertainty. Report A/B/C independently and include data-eligibility attrition.
9. **Scale:** use the existing k-way chronological stream and disk-backed replay source so memory grows with symbols/state rather than total bars; checkpoint only deterministic state while keeping portfolio-level ordering and risk accounting chronological.

Staging should remain 100-stock engineering, 500-stock engineering, then all historically eligible/liquid NSE EQ. Those are validation sizes, not permanent universe restrictions.

## 7. Remaining TradingView/Pine limitations

CAS and EOD corrections do not establish external Pine parity. Still required are timestamped TradingView exports for UT_SYNC v2.7a ATR/trail/events, PSAR, VWAP, ATR14, completed 5-minute EMA buckets and alerts; QQC CLEAR v6 count/quality/volume values; and a reference for Python-only ADX/DMI, CHOP, same-minute RVOL, NIFTY relative strength and opening-range gates.

Minute OHLC cannot prove tick/`varip` alert ordering, broker queue timing or closing-auction equilibrium fills. A TradingView export crossing 15:15 must identify whether it represents continuous, transition or auction state. These remain external blockers, not reasons to alter the frozen formulas.

## 8. Files and evidence

Research implementation:

- `paper_trading/session_calendar.py`
- `paper_trading/engine.py`
- `paper_trading/streaming.py`
- `paper_trading/models.py`
- `tests/paper_trading/test_phase5e_cas_execution.py`
- `tests/paper_trading/test_streaming_checkpoint.py`
- `scripts/audit_phase5e_cas_readiness.py`

Generated offline evidence:

- `paper_trading/docs/evidence/phase5e_cas_classification_2026-10-11.csv`
- `paper_trading/docs/evidence/phase5e_cas_readiness_summary_2026-10-11.json`
- `paper_trading/docs/evidence/phase5e_historical_master_acquisition_manifest_2026-10-11.csv`
- `paper_trading/docs/evidence/phase5e_local_master_inventory_2026-10-11.json`
- `paper_trading/docs/evidence/phase5e_minimum_stock_requests_2026-10-11.csv`
- `paper_trading/docs/evidence/phase5e_nifty100_cas_coverage_2026-10-11.csv`
- `paper_trading/docs/evidence/phase5e_nifty500_cas_coverage_2026-10-11.csv`

The machine-readable summary contains artifact sizes and SHA-256 values. The implementation/evidence commit above contains the verified code, tests and generated artifacts; this report is committed immediately afterward so its own commit hash is reported in the final handback.

## Approval-ready next steps

1. Separately authorize only the 51-record NSE historical-reference manifest, or a reviewed subset that still proves every effective-date transition. Do not authorize stock candles at the same time.
2. Validate and archive those reference files offline; join by date, symbol, series and ISIN; regenerate the 12,429-row classification and both coverage matrices.
3. Promote only master-proven CAS/non-CAS rows. Move verified non-CAS gaps into the stock repair plan and remove verified CAS tails.
4. Resolve the 16 non-current-exact identities using dated instrument/token evidence.
5. Request separate approval for the regenerated, stable current-exact stock-candle manifest only.
6. Re-run integrity, batch/stream and checkpoint tests; obtain the required TradingView exports.
7. Only after those gates pass, seek approval for the staged 100/500/full-universe profitability protocol.

No acquisition or replay should begin from this report alone.

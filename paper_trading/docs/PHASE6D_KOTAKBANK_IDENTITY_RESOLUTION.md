# Phase 6D — KOTAKBANK identity resolution

## Decision

**Historical data and identity readiness for the frozen 78-stock, 20-session window: YES.**

The frozen April 2026 cohort snapshot remains unchanged with KOTAKBANK ISIN `INE237A01028`. Applying the dated 5-for-1 split transition supplied by the user resolves that stale snapshot identity to `INE237A01036` for all 20 evaluation sessions (2026-08-03 through 2026-08-28). KOTAKBANK is therefore classified as verified CAS for the window, not inferred CAS.

This is **not** a broker-realistic execution readiness decision. The separate CAS broker-exit sensitivity remains unsuitable for a profitability evaluation because its known 15:11 candle-close/next-observation timing issue is not repaired here. The next replay, if approved, must use the frozen original closed-bar/next-observation execution policy, not that sensitivity path.

No profitability replay was run and no A/B/C definition, parameter, risk limit, cohort member or execution assumption was changed.

## Official-transition evidence and local-availability result

The supplied authoritative references state:

| Circular | Dated | Stated transition evidence |
|---|---|---|
| `NSE/CML/72216` | 2026-01-08 | KOTAKBANK 5-for-1 split; old ISIN `INE237A01028`, new ISIN `INE237A01036` |
| `NCL/CMPT/72256` | 2026-01-12 | Effective trading date: 2026-01-14 |

The corresponding PDFs (`CML72216.pdf` and `CMPT72256.pdf`) were **not present locally** after a filename and content inventory of the isolated worktree and local repository caches. Per instruction, no external GET request was made. Thus their cited facts are recorded as **user-supplied official-source evidence**, not as locally byte/hash-verified files. The frozen snapshot itself is retained unchanged; the dated mapping is an explicit audit reconciliation, not a rewrite of history.

| Mapping | Value |
|---|---|
| Symbol / series | `KOTAKBANK` / `EQ` |
| Frozen snapshot ISIN | `INE237A01028` |
| Replacement ISIN | `INE237A01036` |
| Effective from | 2026-01-14 |
| Corporate action | 5-for-1 split |
| Mapping status for August window | effective and unambiguous |

## Token, master and cache reconciliation

The identity decision does not rely on the ISIN transition alone. The following independent fields agree:

| Evidence layer | Result |
|---|---|
| Current Kite instrument inventory | `instrument_token=492033`, `exchange_token=1922`, `tradingsymbol=KOTAKBANK`, `instrument_type=EQ`, `exchange=NSE` |
| Dated NSE CM-MII masters (`CAS6B-M01`…`M20`) | exactly one `KOTAKBANK` / `EQ` row per session, `ISIN=INE237A01036`, `FinInstrmId=1922` |
| Crosswalk | Kite `exchange_token=1922` equals dated-master `FinInstrmId=1922` |
| Isolated history-map provenance | `KOTAKBANK → Kite token 492033`, exact trading-symbol mapping |
| Normalized historical cache | 20 August source sessions under `KOTAKBANK`; the normalized candle table does not retain a token field, so cache-token provenance is not asserted from that table alone |
| Current NSE EQ list | `KOTAKBANK` / `EQ` / `INE237A01036` |

The cache is therefore connected through the frozen cohort symbol, the preserved source-map token, and the independent Kite exchange-token/master-instrument crosswalk. A cached candle row with a missing normalized `instrument_token` is not treated as proof of token continuity; it is merely non-contradictory provenance.

## CAS classification and candle completeness

Every dated CM-MII master row for KOTAKBANK in the frozen window has `ElgbltyClsgAuctnSsn=1`. With the effective-dated identity transition and exact `EQ`/instrument crosswalk, each is classified as **verified CAS**.

| Measure | Before Phase 6D | After Phase 6D |
|---|---:|---:|
| Verified CAS sessions | 1,520 | 1,540 |
| Verified non-CAS sessions | 20 | 20 |
| Unresolved sessions | 20 | 0 |
| Complete classified sessions | 1,540 | 1,560 |
| Genuine missing candles in classified sessions | 0 | 0 |

For all 20 KOTAKBANK sessions: expected count is 360; observed unique count is 360; timestamps run 09:15–15:14 IST; CAS flag is `1`; and duplicate, off-session, invalid-OHLC and zero-volume counts are all zero. The transition was not inferred from the 360-candle shape—the dated master determines CAS status.

## Fail-closed implementation and tests

`paper_trading/identity.py` adds an audit-only dated-identity resolver. It accepts either an exact ISIN match or one unambiguous transition whose effective date has passed. It fails closed for a missing, pre-effective, ambiguous, symbol-conflicting, series-conflicting, or Kite-exchange-token-conflicting identity. It is intentionally independent of A/B/C signal and execution code.

Focused verification passed:

- Phase 6D reconciliation assertions: **PASS** — 1,560 rows; 1,540 CAS; 20 non-CAS; zero unresolved; complete KOTAK timestamp coverage.
- Dated-identity, CAS calendar and EOD tests: **24 passed in 1.48s**.
- New tests cover effective-dated stale-snapshot resolution, pre-effective rejection, symbol/series conflict, missing-transition fail-closed behavior, and exchange-token conflict.
- Full repository suite: **PASS** under `scripts/test_offline.sh -q`; the wrapper completed cleanly and removed its isolated temporary test root. The initial direct suite invocation without that wrapper was intentionally invalid because the required offline root was absent and is not counted as a test result.

Relevant local evidence hashes:

| Artifact | SHA-256 |
|---|---|
| Phase 6C classification ledger | `ef3508cec38797cc6496b377d3bbcd11bb269feff0b340bff8ace2b7b13c46d5` |
| First dated CM-MII master | `5c3b3113e7c147a5be79a725f025b53b5d16f8f0826fc9ba5d8ed0198a6ef8d7` |
| Current NSE EQ list | `0328d2f1e3a0f15c46dcea028feb5cb8fda3fd89c21572790187f80c893a270d` |
| Kite NSE instrument inventory | `2143cd46f0c1141711d82434c7afdf19dc93e1a01eab53a173f52aff21a3837b` |
| Kite historical-map provenance | `2469774db4435603025cc60931d0eafe8cf426be42fa51d6a7d7b3bf9f4cd091` |

## Remaining limitations and approval-ready next step

1. The local environment does not retain the two split circular PDFs. Their contents should be acquired or supplied as immutable local evidence in a separately authorized provenance task if byte-level reproducibility is required; no network retrieval is authorized by this phase.
2. The frozen 78-stock cohort is still an exploratory, surviving-snapshot/current-cache cohort. It does not establish all-NSE-EQ representativeness or remove the already documented liquidity and point-in-time-universe limitations.
3. The CAS broker-realistic EOD sensitivity remains a separate unresolved execution-model issue. It must not be used as an alternative exit path in the frozen replay.

**Approval-ready next step:** authorize a single frozen-original, chronological 78-stock replay over the predeclared 2026-08-03 through 2026-08-28 evaluation window, with its predeclared warm-up, complete 1,560-session matrix, equal data access for A/B/C, and all censored/incomplete positions retained in outputs. This would be a new profitability study and requires separate explicit authorization.

## Safety record

- External GET requests: 0.
- Kite API calls, order/GTT/modify/cancel operations and live streaming: 0.
- Profitability replays: 0.
- Strategy, risk, cohort and frozen execution-policy modifications: 0.
- Production-service, Discord, deployment, merge and feature-branch push operations: 0.

Final status: **YES for historical data and identity readiness; NO decision is made for broker-realistic CAS execution readiness.**

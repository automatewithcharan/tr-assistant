# Phase 5D — NSE Closing Auction Session audit

Audit date: 2026-10-11  
Feature baseline: `f81a0756a8b625dc4c03f7553b1ab2f23b106792`  
Scope: offline data-readiness and execution-integrity audit only

## Decision

The old blanket 375-candle completeness rule is wrong after 2026-08-03 for CAS-eligible equities. A normal CAS continuous session has **360 one-minute candle starts, 09:15 through 15:14 IST**. The 15:15–15:35 closing auction is a separate execution regime and must not be represented as fifteen ordinary continuous-market candles.

The audit does not use candle shape alone as historical membership truth. It reaches three separate conclusions:

1. **Verified/composite CAS evidence:** five Phase 5C symbols—`360ONE`, `ABB`, `ABCAPITAL`, `ADANIENSOL`, and `ADANIENT`—have NSE F&O evidence plus exact post-effective-date Kite responses. Their 240 post-CAS session tails, or 3,600 formerly “missing” candles, are expected and complete for the continuous session.
2. **CAS-consistent but not historically verified:** another 207 symbols have 9,774 exact 09:15–15:14/360-candle sessions. Those 146,610 tail cells are withheld from retrieval, but remain unresolved rather than being silently marked complete until a dated exchange master proves the CAS flag.
3. **Genuine or unresolved deficit:** 34,464 tail candles remain in the bounded repair plan: 28,774 on pre-CAS dates and 5,690 in post-CAS sessions that are wholly missing or do not have the exact 360-candle shape.

No profitability replay was run and no A/B/C rule was changed.

## Safety and source boundary

- Kite historical-data requests in this phase: **0**.
- Other Kite/API requests, orders, modifications, cancellations, GTT, quotes or streaming calls: **0**.
- External data files downloaded: **0**. The supplied public HTML references and indexed exchange circular text were read for documentation; no security master or candle file was fetched.
- Authentication files changed: **0**.
- Production services signalled, stopped, restarted or reconfigured: **0**.
- Paper activation, Discord, deployment, scheduling, merge and push: **none**.

The audit read the committed Phase 5B/5C evidence and the ignored isolated Phase 5C raw/normalized research cache. It did not write to production market-data paths.

## Authoritative session contract

The [NSE CAS page](https://www.nse.in/static/products-services/closing-auction-session) states that Phase 1 applies to cash stocks with derivative contracts, CAS is separate from continuous trading, CAS securities transition at 15:15, non-CAS securities trade continuously until 15:30, and the auction runs through 15:35. NSE lists circular `NSE/CMTR/73845` for the CAS security-master changes and gives 2026-08-03 as the live effective date through its implementation circulars. The published go-live instructions identify `NSE_CM_security_31072026.csv.gz` as the master to load for the effective date; that file is not present locally.

The [Zerodha CAS notice](https://zerodha.com/z-connect/general/everything-you-need-to-know-about-closing-auction-session-cas) independently records the 2026-08-03 change and the broker's MIS auto-square-off times: 15:12 for CAS equities and 15:25 for non-CAS equities. The [Kite developer thread](https://kite.trade/forum/discussion/16171/historical-data-api-missing-minute-candles-after-15-14) is consistent with the observed historical endpoint ending at the 15:14 candle for CAS stocks.

The research calendar implemented in `paper_trading/session_calendar.py` is deliberately fail-closed:

| Instrument/date state | Continuous candle starts | Expected count | Closing auction |
|---|---|---:|---|
| Any ordinary trading day before 2026-08-03 | 09:15–15:29 | 375 | none in this model |
| Verified CAS, from 2026-08-03 | 09:15–15:14 | 360 | separate 15:15–15:35 regime |
| Verified non-CAS, from 2026-08-03 | 09:15–15:29 | 375 | none |
| Inferred/unknown, from 2026-08-03 | unresolved | unresolved | no completeness promotion |
| Exchange holiday/non-trading day | none | 0 | none |

Special sessions, an early market-wide circuit close, a suspension, or a security-specific trading-status change require an explicit dated override. They are not inferred from absent candles.

## Historical CAS classification evidence

### Verified/composite set

| Symbol | Evidence used | Audited post-CAS sessions | Result |
|---|---|---:|---|
| `360ONE` | NSE `NSE/FAOP/68181`, F&O effective 2025-06-27; Phase 5C `EOD-0001/0002` | 48 | 360 rows, last 15:14 |
| `ABB` | Official NSE derivatives-underlying listing; Phase 5C `EOD-0006/0007` | 48 | 360 rows, last 15:14 |
| `ABCAPITAL` | Official NSE derivatives-underlying listing; Phase 5C `EOD-0009/0010` | 48 | 360 rows, last 15:14 |
| `ADANIENSOL` | NSE `NSE/FAOP/65064`, F&O effective 2024-11-29; Phase 5C `EOD-0018/0019` | 48 | 360 rows, last 15:14 |
| `ADANIENT` | Official NSE derivatives-underlying listing; Phase 5C `EOD-0020` | 48 | 360 rows, last 15:14 |

“Composite” is intentional: only `360ONE` and `ADANIENSOL` have an explicit dated introduction circular in the collected evidence. For the other three, the official F&O listing plus the preserved historical endpoint behavior is strong enough to explain the nine failed pilot requests, but the dated 2026-07-31 cash security master remains the preferred final point-in-time record.

### Inferred and unknown set

- **207 symbols / 9,774 sessions / 146,610 candles** have the exact complete-prefix topology but no locally retained dated CAS master. They are labelled `INFERRED_CAS`, not complete.
- **16 symbols / 413 post-CAS sessions / 5,690 candles** do not have that topology. Of these, 375 sessions contain no candles at all and 38 contain a partial tail or other partial session. They remain data/identity deficits. The symbols are `AKZOINDIA`, `ASTRAZEN`, `BLUEDART`, `DBREALTY`, `DUMMYHDLVR`, `GSPL`, `GUJGASLTD`, `HEG`, `HFCL`, `HONAUT`, `JBCHEPHARM`, `LTIM`, `MAHSCOOTER`, `RELINFRA`, `TRIVENI`, and `VENTIVE`.
- No current NSE EQ list was treated as historical point-in-time truth.

The exact 12,429-row classification is in `evidence/phase5d_cas_gap_cross_reference_2026-10-11.csv`. Every row retains its identity status, token, raw range, original request ID, current missing count, evidence class and request disposition.

## Phase 5C nine-request finding

The nine requests previously described as unsuccessful were actually successful historical responses that could not produce nonexistent ordinary CTS candles:

| Request IDs | Symbol | Post-CAS target sessions | Observed result |
|---|---|---:|---|
| `EOD-0001`, `EOD-0002` | `360ONE` | 48 | all 48 had 360 rows through 15:14 |
| `EOD-0006`, `EOD-0007` | `ABB` | 48 | all 48 had 360 rows through 15:14 |
| `EOD-0009`, `EOD-0010` | `ABCAPITAL` | 48 | all 48 had 360 rows through 15:14 |
| `EOD-0018`, `EOD-0019` | `ADANIENSOL` | 48 | all 48 had 360 rows through 15:14 |
| `EOD-0020` | `ADANIENT` | 24 | all 24 had 360 rows through 15:14 |

There were zero response conflicts. The five first-window requests still legitimately recovered their five missing July 6–10 sessions; the post-August tails were never recoverable as continuous candles. Repeating these requests would not repair the apparent tail.

## Before/after completeness

| Accounting view | Affected sessions | Missing tail candles |
|---|---:|---:|
| Phase 5B blanket-375 baseline | 12,429 | 185,874 |
| After Phase 5C local repairs, before CAS correction | 12,349 | 184,674 |
| Conservative: remove only five-symbol verified/composite CAS cells | 12,109 | 181,074 |
| Classification hold: exact topology, not yet promoted | 9,774 | 146,610 |
| Definite repair set if all holds are later verified | 2,335 | 34,464 |

The 34,464 floor consists of 1,922 still-incomplete pre-CAS sessions plus 413 post-CAS non-topology sessions. It is not an assertion that all other sessions are complete today; 9,774 remain on an explicit evidence hold.

Session completeness must use the instrument/date expectation, not a global count. A verified CAS session with all 360 expected timestamps can be continuously complete even though no 15:15 bar exists. Auction price/order evidence, if later needed, belongs in a separate dataset and execution model.

## Rebuilt minimum backfill manifest

The former manifest had 658 rows. The CAS-aware plan produces:

- **21 proven unnecessary old requests**, `EOD-0001` through `EOD-0021`, because Phase 5C recovered the pre-CAS gaps and/or verified CAS explains the remaining tail;
- **265 old requests withheld**, not declared unnecessary, because their only remaining targets are topology-inferred CAS sessions awaiting dated classification;
- **372 definite repair requests across 363 symbols**, covering 2,335 sessions and 34,464 tail candles;
- within those 372 rows, **356 are current-exact** and **16 remain blocked** by archived-token or unresolved identity evidence.

The bounded plan is `evidence/phase5d_minimum_stock_requests_2026-10-11.csv`. It is **not authorized**. Its full-window GETs would also recover non-tail minutes in wholly missing sessions, so 34,464 is the tail-specific minimum, not the maximum number of returned candles. `evidence/phase5d_cas_classification_holds_2026-10-11.csv` separately preserves all 9,774 withheld sessions.

Approval order for a future phase:

1. obtain the dated `NSE_CM_security_31072026.csv.gz` master and effective-dated successors for any CAS/F&O membership change through 2026-10-09;
2. join by symbol/series/ISIN and effective date, then regenerate this audit;
3. remove sessions whose dated CAS flag is verified and move dated non-CAS sessions into the repair plan;
4. resolve the 16 blocked identity rows before any request;
5. only then seek separate approval for a stable list of the remaining current-exact historical GETs.

## EOD execution model

The frozen strategy uses `eod_minute = 15:15`. In the simulator, the 15:14 candle becomes observable at 15:15 and sets a pending `EOD` exit; the fill requires a later liquid observation. For a CAS equity there is no later ordinary continuous candle. The current same-session boundary rule therefore correctly censors an unresolved position rather than fabricating a 15:15 fill or carrying it overnight.

This is still not a realistic executable CAS exit rule:

- Zerodha's published MIS auto-square-off starts at **15:12**, before the frozen 15:15 decision.
- A 15:14 close does not prove that an order could have been placed or filled at 15:15.
- CAS order entry begins after the transition period and matching occurs under auction priority/equilibrium-price rules. It cannot be simulated as a next-bar market fill from one-minute CTS OHLC.

Accordingly, broad replay remains blocked from claiming resolved CAS EOD trades. A separately labelled sensitivity model may test an exit deadline no later than 15:11 with a fill completed before 15:12, or an explicitly modelled auction order/equilibrium fill. Either would be execution-model sensitivity research, not a change to or optimization of A/B/C. No such replay was run here.

## Pine parity and remaining blockers

CAS correction changes data eligibility and exit observability; it does not establish Pine parity. Still required:

- timestamped TradingView exports for UT v2.7a ATR10, trail, UT event/age, PSAR, VWAP, ATR14, stop/quantity, completed 5-minute EMA20 buckets and alert metadata;
- QQC CLEAR v6 per-bar count, signed-body quality and signed-volume values;
- an external reference implementation/export for the frozen Python-only ADX/DMI, CHOP, same-minute RVOL, NIFTY RS and opening-range gates;
- tick/`varip`/alert-timing evidence and an explicit CAS/auction execution reference if TradingView alerts cross 15:15.

Historical one-minute OHLC cannot prove intrabar alert ordering or auction fills. `HEG`, `TRIVENI`, renamed/delisted/dummy identities, special sessions and daily CAS membership changes remain fail-closed.

## Evidence artifacts

| Artifact | Rows | SHA-256 |
|---|---:|---|
| `phase5d_cas_gap_cross_reference_2026-10-11.csv` | 12,429 | `84af4f50676793e8d1d0fe8f9487bd8bb45f8c5b33e8c7910205ca97f23f2800` |
| `phase5d_minimum_stock_requests_2026-10-11.csv` | 372 | `d024a439a67534bfe47ce57262385fcf013bba18e895cb15020022faccc1ed49` |
| `phase5d_cas_classification_holds_2026-10-11.csv` | 9,774 | `ac39fe143278e64abe99f48b8d12939208aacaf398f08751350bcdd46a9632e4` |
| `phase5d_cas_audit_summary_2026-10-11.json` | summary | regenerated deterministically by the offline audit script |

## Tests and changed files

Deterministic CAS tests cover the last pre-CAS day, 2026-08-03 boundary, verified CAS, verified non-CAS, inferred/unknown fail-closed behavior, an explicit holiday, CTS-to-auction separation, the frozen 15:15 EOD boundary and the 15:12 MIS conflict. Existing paper-engine tests continue to cover same-session EOD fill versus next-session censorship.

Changed research-only files:

- `paper_trading/session_calendar.py`
- `scripts/audit_phase5d_cas_sessions.py`
- `tests/paper_trading/test_cas_session_calendar.py`
- the four Phase 5D evidence artifacts above
- this report

Offline verification:

- focused CAS/EOD/Phase 5C regressions: **54 passed**;
- complete `tests/paper_trading` suite: **74 passed**;
- complete repository suite with a fresh `PKSCREENER_TEST_ROOT`: **1,319 passed**, 15 pre-existing warnings, 113.31 seconds;
- `py_compile` for the calendar and audit generator: passed;
- `git diff --check`: passed.

Phase 5D implementation/evidence commit: `0c4fb281e5e893daf34f2845959386b74128fd56`. This report is added by the subsequent documentation commit reported in the handback.

## Protected-service status

The production watchers were inspected read-only before work and after the full suite. PIDs ${SERVICE_PIDS} remained present with their original 2026-10-11 12:16:37 UTC start time and unchanged command lines. No process-control operation or production-service write occurred.

Final status: the old tail deficit was materially overstated by CAS, but only 240 sessions are promoted using the collected verified/composite evidence. The 9,774 topology-only sessions remain explicit classification holds. No further stock-history request should be approved from the old 658-row manifest.

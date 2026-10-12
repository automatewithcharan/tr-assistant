# Phase 5A limited NIFTY 50 benchmark backfill report

Date: 2026-10-10  
Worktree: `${PAPER_WORKTREE}`  
Branch: `codex/ut-sync-abc-paper-20261010`  
Starting commit: `b34bcf07aaee3d07501134b5b1547359712136e5`  
Authorization: `BMARK-001` and `BMARK-002` only; read-only benchmark history.

## Outcome

Both and only the approved Kite historical-candle GET requests completed with HTTP 200. They returned 25,500 NIFTY 50 one-minute candles across all 68 observed sessions from July 6 through October 9, 2026. Every returned session has the complete 375-candle 09:15–15:29 IST grid.

Coverage improved from 10/68 to 68/68 observed sessions. All 58 previously missing benchmark sessions were recovered. No observed-session dates remain missing, and Kite returned no unexpected session date relative to the Phase 5 manifest.

All 3,750 candles from the ten previously cached sessions matched the new responses exactly across timestamp, OHLC and volume. There were no duplicate timestamps, invalid OHLC rows, incomplete sessions or source conflicts. All 25,500 index volume values are zero; they were retained as valid benchmark observations and were not treated as price-data failures.

No stock-tail request, instrument-list request, profile/holdings/positions request, quote/streaming request, login/session refresh or other Kite request was made.

## Preflight identity and scope verification

The Phase 5 proposal, local historical instrument maps, current ticker evidence and the existing backfill mapping all independently identify:

| Field | Verified value |
|---|---|
| Instrument | NIFTY 50 index |
| Exchange identity | NSE index |
| Kite instrument token | `256265` |
| Interval | `minute` |
| Host | `https://api.kite.trade` |
| SDK route | `/instruments/historical/{instrument_token}/{interval}` |
| Concrete endpoint | `/instruments/historical/256265/minute` |
| Continuous | `0` |
| Open interest | `0` |

`BMARK-001` spans 60 calendar days inclusive. `BMARK-002` spans 36 calendar days inclusive. Both comply with the previously audited one-minute maximum of 60 calendar days. Requests were serialized with 0.7 seconds of intentional spacing, approximately 1.43 requests/second and below the documented historical limit of 3 requests/second. There were no retries and redirects were disabled.

The executor hard-coded the approved token, endpoint, interval and windows and refused pre-existing output to prevent accidental repeats. Its sole network call site was HTTP GET. It contained no broker mutation, GTT, order, instruments, profile, holdings, positions, login or session-writing call.

## Actual requests

Authentication used the existing fresh canonical Kite session read-only. Authorization headers and credential values were neither logged nor persisted in provenance.

### BMARK-001

```http
GET https://api.kite.trade/instruments/historical/256265/minute?from=2026-07-06+09%3A15%3A00&to=2026-09-03+15%3A29%3A00&continuous=0&oi=0
```

| Result | Value |
|---|---|
| Started UTC | `2026-10-10T15:29:16.570800+00:00` |
| Completed UTC | `2026-10-10T15:29:17.231059+00:00` |
| HTTP status | 200 |
| Response bytes | 1,065,956 |
| Raw response SHA-256 | `c376ab0834b72486284d796f778edb01b357b6bb7b7b1552b035a431abacb659` |
| Candles | 16,500 |
| Complete sessions | 44 |
| Duplicate/incomplete sessions | 0 / 0 |

### BMARK-002

```http
GET https://api.kite.trade/instruments/historical/256265/minute?from=2026-09-04+09%3A15%3A00&to=2026-10-09+15%3A29%3A00&continuous=0&oi=0
```

| Result | Value |
|---|---|
| Started UTC | `2026-10-10T15:29:18.181294+00:00` |
| Completed UTC | `2026-10-10T15:29:19.436247+00:00` |
| HTTP status | 200 |
| Response bytes | 581,138 |
| Raw response SHA-256 | `1f1a0da8acf5fb8b4b7505d8a252005c66e87554ca43c4d666e2073c89bc2673` |
| Candles | 9,000 |
| Complete sessions | 24 |
| Duplicate/incomplete sessions | 0 / 0 |

The 0.950-second gap from completion of the first request to start of the second exceeds the configured 0.7-second minimum.

## Coverage before and after

| Measure | Before | After |
|---|---:|---:|
| Observed-session calendar | 68 | 68 |
| Complete NIFTY sessions | 10 | 68 |
| Missing NIFTY sessions | 58 | 0 |
| Unique NIFTY candles | 3,750 | 25,500 |
| Benchmark coverage | 14.7% | 100% |

The ten original complete sessions remain July 17, July 27, July 29, August 3, August 20, September 15, September 16, October 5, October 6 and October 9.

Recovered sessions, exactly:

- July: `06, 07, 08, 09, 10, 13, 14, 15, 16, 20, 21, 22, 23, 24, 28, 30, 31` (17);
- August: `04, 05, 06, 07, 10, 11, 12, 13, 14, 17, 18, 19, 21, 24, 25, 26, 27, 28, 31` (19);
- September: `01, 02, 03, 04, 07, 08, 09, 10, 11, 17, 18, 21, 22, 23, 24, 25, 28, 29, 30` (19);
- October: `01, 07, 08` (3).

The response session-date sequence exactly equals the 68-row Phase 5 observed-session manifest. Remaining missing dates: **none within that manifest**.

## Validation evidence

Validation performed before accepting the research cache:

1. Parsed every API timestamp as timezone-aware UTC+05:30 and normalized it to `Asia/Kolkata`.
2. Required every timestamp to remain inside its approved window.
3. Required positive OHLC, `high >= open/low/close`, `low <= open/high/close`, and nonnegative volume.
4. Required strictly ordered, globally unique timestamps across both responses.
5. Required exactly 375 consecutive timestamps from 09:15 through 15:29 for every returned date.
6. Compared the returned session-date list with the Phase 5 68-session manifest.
7. Compared all ten existing benchmark sessions candle-by-candle using exact decimal values.
8. Accepted zero index volume without weakening OHLC/timestamp validation.

Results:

| Check | Result |
|---|---:|
| IST timestamp failures | 0 |
| Out-of-window rows | 0 |
| Invalid OHLC/negative volume | 0 |
| Duplicate timestamps | 0 |
| Out-of-order timestamps | 0 |
| Incomplete session grids | 0 |
| Unexpected session dates | 0 |
| Existing overlap rows | 3,750 |
| Exact overlap matches | 3,750 |
| Overlap conflicts | 0 |
| Zero-volume index rows retained | 25,500 |

No validation failure or data inconsistency was found.

## Cache and provenance

The original main-repository files were read only and were not overwritten. Their ten-file Phase 5 source manifest remains the comparison baseline.

New isolated research cache:

- root: `${PAPER_WORKTREE}/results/Data/ut_sync_phase5a_benchmark`;
- raw response bodies: `raw/BMARK-001.response.json` and `raw/BMARK-002.response.json`;
- complete normalized cache: `normalized/NIFTY 50.csv`;
- local detailed provenance: `phase5a_provenance.json`;
- normalized file: 2,259,076 bytes, SHA-256 `81ade633fabd530253c388df62b6cb53215723ad205adc1389c28c18ed938c68`.

The raw and normalized market data remain in the ignored local research cache rather than Git. The committed sanitized request result is `paper_trading/docs/evidence/phase5a_benchmark_request_results_2026-10-10.json`. It contains no credentials.

The canonical session file SHA-256 before and after retrieval was unchanged at `48b3194ff09b82975cdd6851a04f9b43c2cce9c2184a008e30c067211a2819d7`; its size and modification timestamp were also unchanged. No authentication/session file was repaired, refreshed or written.

## Tests and safety

- Focused paper plus Kite execution-safety suite: **113 passed**, 13 warnings, 13.31 seconds.
- Full fail-closed offline repository suite: **1,306 passed**, 15 warnings, 88.84 seconds.
- `git diff --check`: clean.
- Existing warnings are the known Discord `audioop` deprecation, multiprocessing fork warning and legacy parsed escape-sequence warnings.

Protected services were unchanged before and after retrieval:

- PID ${SERVICE_PID}: `ng_nse_top_gainers_watch`;
- PID ${SERVICE_PID}: `ng_emerging_leader_alerts`;
- PID ${SERVICE_PID}: `ng_ut_sync_leader_watch`.

No broker order, modification, cancellation, GTT, execution, position management, live quote stream or Discord operation occurred. No A/B/C profitability replay was run and no strategy/configuration threshold changed. Nothing was pushed, merged, deployed, scheduled or activated.

## Remaining blockers and stop status

The NIFTY benchmark gap for the 68 observed sessions is resolved locally. This does not resolve missing stock-session tails, point-in-time universe identities, corporate actions, historical tick sizes, zero-volume equity semantics or genuine TradingView parity. The 658 stock-tail requests remain unauthorized and were not executed.

Phase 5A stops here after exactly two successful historical GETs. No additional market-data request or profitability replay is authorized or pending execution.

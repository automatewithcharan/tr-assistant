# Phase 6C — Historical NSE CAS classification results

## Decision

**Readiness for the frozen 78-stock, 20-session A/B/C replay: NO.**

The approved evidence acquisition resolved the historical session calendar for 77 stocks and 1,540 of 1,560 stock-sessions. All 1,540 resolved sessions have complete continuous-market timestamp sets under their authoritative CAS status. The remaining 20 sessions, all `KOTAKBANK`, are deliberately unresolved because the frozen cohort snapshot ISIN conflicts with every dated NSE master ISIN. No identity was silently substituted and no price was inferred from candle count.

No profitability replay was run. No A/B/C strategy, execution policy, production service or broker path was changed or invoked.

## Scope and acquisition result

Exactly the 22 HTTPS `GET` requests authorized from the Phase 6B manifest were made: `CAS6B-REF-01`, `CAS6B-REF-02`, and `CAS6B-M01` through `CAS6B-M20`.

| Control | Result |
|---|---:|
| Authorized GET requests | 22 |
| Actual GET requests | 22 |
| HTTP 200 responses | 22 |
| Unapproved requests | 0 |
| Redirected requests | 0 |
| Kite/API-authenticated requests | 0 |
| Profitability replays | 0 |

The two circulars and all 20 master files passed response identity, content-type, magic-byte and file-integrity checks. Raw bytes, response metadata, final URLs, retrieval times and SHA-256 hashes were retained in the isolated research-artifact area. Credentials, cookies and authorization headers were neither required nor recorded.

### Official references

| Request | File | Bytes | SHA-256 | Validation |
|---|---|---:|---|---|
| `CAS6B-REF-01` | `CMTR73845.zip` | 120,265 | `94f9288a24e815265feb03023f997ecc3a4993cbc1445b0b20383beb7c4c7239` | ZIP structure, traversal guard and members passed |
| `CAS6B-REF-02` | `CMTR75479.pdf` | 241,038 | `17e415f619be6a132f86a4edbc127412f44e90f87e838151d86484e2204022c1` | PDF identity and six-page structure passed |

`CMTR73845.zip` contains the signed circular PDF and `Annexure A.xlsx`. The annexure defines the NSE-listed CM MII file `NSE_CM_security_ddmmyyyy.csv.gz`; field 24 is `Eligibility for Closing Auction Session`, ISO tag `ElgbltyClsgAuctnSsn`, where `0` means not eligible and `1` means eligible. Every acquired master had 120 columns, placed this field at field 24, and contained only the allowed `0`/`1` domain.

### Dated CM-MII masters

| ID | Master date → session | Rows / CAS=1 | Bytes | SHA-256 |
|---|---|---:|---:|---|
| `CAS6B-M01` | 2026-07-31 → 2026-08-03 | 36,184 / 208 | 1,210,241 | `5c3b3113e7c147a5be79a725f025b53b5d16f8f0826fc9ba5d8ed0198a6ef8d7` |
| `CAS6B-M02` | 2026-08-03 → 2026-08-04 | 36,188 / 208 | 1,209,602 | `c61c505973befcf041acd8900339b6c3b595aae18283726b4725661c29be8359` |
| `CAS6B-M03` | 2026-08-04 → 2026-08-05 | 36,221 / 208 | 1,204,210 | `a733d8566880c42cd0742b7f52592037fac4e94cbec09f99e50784e80022d2f6` |
| `CAS6B-M04` | 2026-08-05 → 2026-08-06 | 36,243 / 208 | 1,213,427 | `c2ec4672ba080e33a889a29fc78320e318d85a99c193155c1f7cfd41af5c0d8d` |
| `CAS6B-M05` | 2026-08-06 → 2026-08-07 | 36,258 / 208 | 1,212,273 | `24dcf48fc8bccb6d87c6c8797be91a50f5ea244866c45358aa01a24aed12b579` |
| `CAS6B-M06` | 2026-08-07 → 2026-08-10 | 36,263 / 208 | 1,211,256 | `091bbde81933c3583f0d8e64c2061470103a3501d8a84375a653f9934833cc37` |
| `CAS6B-M07` | 2026-08-10 → 2026-08-11 | 36,265 / 208 | 1,212,708 | `164474fbcf2d90c854679428f4d3a052075939b7f4198be27c59f6b2e9b7aaf7` |
| `CAS6B-M08` | 2026-08-11 → 2026-08-12 | 36,285 / 208 | 1,213,148 | `3575488e66635aa8b081ec421e1557ee3ae58369b209d5de26ecd2c30e254995` |
| `CAS6B-M09` | 2026-08-12 → 2026-08-13 | 36,296 / 208 | 1,213,302 | `03da38dfa7a0d8e5104464d4df466a2541a6b1a8982388550180e9321460bdab` |
| `CAS6B-M10` | 2026-08-13 → 2026-08-14 | 36,331 / 208 | 1,208,871 | `296555b292bbad0877646abf0326cb88ba682587d5fbd7df95f0bc7b75fe1b4b` |
| `CAS6B-M11` | 2026-08-14 → 2026-08-17 | 37,042 / 208 | 1,268,904 | `8f57b6caa7f061b589d3a15a943323575effff8cab0a285a987ced54eba6ef06` |
| `CAS6B-M12` | 2026-08-17 → 2026-08-18 | 37,055 / 208 | 1,237,350 | `54405572ab8ff7443ed193fa1f7bc3498dcc5b8a13bebb2e80f2369204cc141f` |
| `CAS6B-M13` | 2026-08-18 → 2026-08-19 | 37,071 / 208 | 1,229,164 | `c620dd52c79b19164f190ed999b8e8085c383c91d5d7b7647d667287151f0afa` |
| `CAS6B-M14` | 2026-08-19 → 2026-08-20 | 37,091 / 208 | 1,240,205 | `87024191b048847874ef627d76b489ee2ca69d13f8657d6d03a434c21a59cbfa` |
| `CAS6B-M15` | 2026-08-20 → 2026-08-21 | 37,111 / 208 | 1,237,513 | `544059a874c3d1bbf9310eb3e0db7246aaa6e2de71cb6eae36be3d3ed99c0783` |
| `CAS6B-M16` | 2026-08-21 → 2026-08-24 | 37,127 / 208 | 1,239,276 | `5c5edcfa278d743ee03ccee208bed1abae70d96e61051127cc8094b8f7a54451` |
| `CAS6B-M17` | 2026-08-24 → 2026-08-25 | 37,141 / 208 | 1,240,192 | `b73f46ace50f000423bb52004a27a4e08e98eabeff00d666489e96e6d52153a7` |
| `CAS6B-M18` | 2026-08-25 → 2026-08-26 | 37,149 / 211 | 1,239,257 | `ee98d282388bf6925d20f9d06e39a31b036a4936d7851451b742a51947572c22` |
| `CAS6B-M19` | 2026-08-26 → 2026-08-27 | 37,154 / 211 | 1,240,362 | `d619d43120c650579492a2a2d15bb9b9395f3a3a87cc81ee2ea3be3ddc8fa868` |
| `CAS6B-M20` | 2026-08-27 → 2026-08-28 | 37,167 / 210 | 1,241,997 | `723ee3002eb9b57749fc31d380e1599f2cc430584d4e87c46d2bb9539a362794` |

All 20 gzip streams passed decompression checks. Each filename date matched the manifest. `NSE/CMTR/75479` explicitly maps the 2026-07-31 master to the 2026-08-03 go-live session. For subsequent masters, the established previous-trading-day rule was also consistent with each HTTP `Last-Modified` being after the source session close and before the mapped next session. `Last-Modified` was used only as a consistency check, not as the applicability definition.

## Classification method

For each of the 1,560 frozen rows, classification required exactly one dated-master row matching:

1. exact trading symbol;
2. `EQ` series;
3. the frozen 2026-04-22 snapshot ISIN; and
4. consistent available instrument identity evidence.

Only after that identity join was `ElgbltyClsgAuctnSsn=1` classified as CAS and `0` as non-CAS. Missing, duplicate or conflicting identities were retained as unresolved. Candle shape was never used to assign status.

### Results

| Classification | Symbols | Stock-sessions | Expected continuous candles/session | Complete | Incomplete |
|---|---:|---:|---:|---:|---:|
| Verified CAS | 76 | 1,520 | 360, 09:15–15:14 IST | 1,520 | 0 |
| Verified non-CAS | 1 | 20 | 375, 09:15–15:29 IST | 20 | 0 |
| Unresolved identity | 1 | 20 | not assigned | 0 | 20 unresolved |
| **Total** | **78** | **1,560** | — | **1,540** | **20 unresolved** |

The sole verified non-CAS symbol is `BAJAJHFL`. The 76 verified CAS symbols are:

`ABB`, `ADANIENSOL`, `ADANIENT`, `ADANIGREEN`, `ADANIPORTS`, `ADANIPOWER`, `APOLLOHOSP`, `AXISBANK`, `BAJAJ-AUTO`, `BAJAJFINSV`, `BAJFINANCE`, `BANKBARODA`, `BEL`, `BHARTIARTL`, `BPCL`, `CANBK`, `CGPOWER`, `CHOLAFIN`, `CIPLA`, `COALINDIA`, `DIVISLAB`, `DRREDDY`, `EICHERMOT`, `ETERNAL`, `GAIL`, `GRASIM`, `HAVELLS`, `HCLTECH`, `HDFCBANK`, `HDFCLIFE`, `HINDALCO`, `HINDUNILVR`, `HINDZINC`, `ICICIBANK`, `INDHOTEL`, `INDIGO`, `INFY`, `IOC`, `IRFC`, `ITC`, `JIOFIN`, `JSWSTEEL`, `LICI`, `LODHA`, `LT`, `M&M`, `MARUTI`, `MAXHEALTH`, `MOTHERSON`, `NAUKRI`, `NESTLEIND`, `NTPC`, `ONGC`, `PFC`, `PIDILITIND`, `PNB`, `POWERGRID`, `RECLTD`, `RELIANCE`, `SBILIFE`, `SBIN`, `SOLARINDS`, `SUNPHARMA`, `TATACONSUM`, `TATAPOWER`, `TATASTEEL`, `TCS`, `TECHM`, `TITAN`, `TMPV`, `TORNTPHARM`, `TRENT`, `ULTRACEMCO`, `VBL`, `VEDL`, `WIPRO`.

## Candle validation and genuine gaps

Every resolved CAS row has exactly the expected 360 unique minute timestamps, beginning 09:15 and ending 15:14 IST. Every resolved non-CAS row has exactly the expected 375 unique timestamps, beginning 09:15 and ending 15:29 IST.

| Check across 1,540 resolved sessions | Result |
|---|---:|
| Missing expected candles | 0 |
| Duplicate timestamps | 0 |
| Off-session timestamps | 0 |
| Invalid OHLC rows | 0 |
| Genuine incomplete sessions | 0 |
| Zero-volume price observations | 11 across 10 sessions |

The 11 zero-volume observations occur in otherwise complete price series: `SOLARINDS` (2026-08-06, 2026-08-10 and 2026-08-21), `HAVELLS` (2026-08-10 and twice on 2026-08-14), `LODHA` (2026-08-12 and 2026-08-14), `INDHOTEL` (2026-08-21), `PIDILITIND` (2026-08-21), and `APOLLOHOSP` (2026-08-26). Under the established replay data policy these remain valid price/indicator observations but are not fill-eligible; they are not manufactured or counted as missing candles.

No additional historical candle backfill is justified for the 77 resolved stocks in this evaluation window.

## Unresolved identity and readiness blocker

`KOTAKBANK` matches by symbol and `EQ` series but fails the required ISIN identity join on every evaluation date:

| Evidence | Value |
|---|---|
| Frozen 2026-04-22 snapshot ISIN | `INE237A01028` |
| All 20 dated-master ISINs | `INE237A01036` |
| Dated-master `FinInstrmId` | `1922` |
| Existing Kite instrument token | `492033` |
| Candidate master CAS flag | `1` |
| Cached candle shape | 360 candles, 09:15–15:14 IST on all 20 dates |

The candidate rows are consistent with CAS, but neither their flag nor the candle shape proves continuity from the frozen snapshot identity. The available current equity list also carries `INE237A01036`, but it is not date-specific corporate-action evidence. Consequently all 20 rows remain `UNRESOLVED_FAIL_CLOSED` and are not included among complete classified sessions.

The minimum remaining evidence is an authoritative, dated NSE corporate-action/security-identity record establishing the ISIN transition and its effective date, plus token continuity reconciliation. Obtaining any new external file requires a separate exact authorization. Until that is resolved—or a predeclared methodology explicitly excludes the identity-conflicted stock without using its outcomes—the frozen 78-stock replay is **not ready**.

## Provenance and reproducibility

The isolated research environment retains, without publication:

- the 22 immutable raw source files and sanitized response metadata;
- extracted circular/schema evidence;
- a 1,560-row classification ledger; and
- an aggregate classification summary.

| Local evidence artifact | SHA-256 |
|---|---|
| Acquisition provenance JSON | `0a5ad61839430cf377955cecdc3e41ec71f554dd18223e0e847309543fd52c0c` |
| Classification ledger CSV | `ef3508cec38797cc6496b377d3bbcd11bb269feff0b340bff8ace2b7b13c46d5` |
| Classification summary JSON | `21ab78098c636a37275aa42fe16b80d8d33efd5e1e2ecb48f11a7ead3de1477a` |

Input hashes retained from the frozen design are:

| Input | SHA-256 |
|---|---|
| Frozen Phase 6A cohort manifest | `b42e07e071adc32e770f3667f155106c6a9f4e18cf7a5f377532e606d865667a` |
| Phase 5E coverage ledger | `d6a0c1e78e4159ebdf8cfedb69c9086e30081c678f5a95a7088e8d08ba0d6098` |
| Current equity identity list | `0328d2f1e3a0f15c46dcea028feb5cb8fda3fd89c21572790187f80c893a270d` |

## Verification and safety record

- Deterministic Phase 6C evidence assertions: **PASS** (`22` provenance rows, `1,560` unique ledger rows, source-byte SHA-256 checks, 20 schemas, classification arithmetic and resolved-session quality).
- Focused offline regression tests: **19 passed in 1.49s** (`tests/paper_trading/test_cas_session_calendar.py` and `tests/paper_trading/test_phase5e_cas_execution.py`).
- Allowlist audit: 22/22 provenance rows have approved request IDs, exact manifest URLs, `GET`, HTTP 200 and matching final URLs.
- Reference integrity: PDF/ZIP signatures passed; ZIP member paths were safe; signed schema and field semantics matched expectations.
- Master integrity: 20/20 gzip streams, filenames, schemas, column positions and `0`/`1` domains passed.
- Matrix reconciliation: 78 symbols × 20 sessions = 1,560 unique rows; classifications sum exactly to 1,560.
- Identity reconciliation: 77 symbols exact across symbol, `EQ` and ISIN; one symbol retained unresolved.
- Timestamp validation: all 1,540 resolved sessions match their exact expected minute sets.
- No A/B/C performance outcome was computed or inspected.
- No Kite API request, order, GTT, modification, cancellation, live stream, paper activation, Discord send, deployment, merge or production-service action occurred.

Final status: **NO — the historical calendar and candles are complete for 77 stocks, but the frozen 78-stock cohort remains blocked by 20 unresolved KOTAKBANK identity rows.**

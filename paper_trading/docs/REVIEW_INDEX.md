# UT_SYNC A/B/C paper-trading documentation review index

This is a sanitized, documentation-only review export. It is not a release, deployment, live-paper activation, profitability conclusion, or authorization for market-data retrieval or broker execution. See the [redaction log](REDACTION_LOG.md) for exact security replacements and excluded material.

## Timeline

| Date / phase | Event | Evidence in this export |
|---|---|---|
| Pre-feature context | UT_SYNC v2.7a and QQC companion requirements, paper-first safety boundary and preliminary A/B/C proposal were recorded. | Master handoff |
| Initial implementation | Isolated A/B/C paper-replay package, baseline audit, safety/parity constraints and initial handoff were documented. | Baseline, README, fee/safety notes, handoff |
| Phase 3 | Replay integrity, complete-session handling, zero-volume semantics, chronological portfolio accounting and backfill planning were audited. | Phase 3 and data-readiness reports |
| Phase 4 | Disk-backed streaming, deterministic chronological ordering and checkpoint recovery were documented. | Phase 4 report |
| Phase 5 | Historical data, benchmark coverage and Pine-parity blockers were audited. | Phase 5 report |
| Phase 5A–5C | Approved benchmark retrieval, integration, and a bounded stock-history pilot were documented. | Phase 5A–5C reports |
| Phase 5D–5E | Closing Auction Session (CAS) data classification and a separate broker-realistic EOD sensitivity were documented. | Phase 5D–5E reports |
| Phase 6A | A fixed 78-symbol, five-session frozen A/B/C reconstruction and trade forensics were completed. | Phase 6A reports |
| Phase 6B | The proposed extended replay was stopped; the earliest 20-session post-CAS window was frozen and its exact historical CM-MII acquisition blocker was bounded. | Phase 6B reports |
| Phase 6C | The approved 22-file NSE reference acquisition classified 1,540 of 1,560 frozen cohort sessions; the replay remained blocked by one fail-closed security-identity conflict. | Phase 6C report |
| Phase 6D | User-supplied dated split evidence resolved the KOTAKBANK snapshot/master mismatch; historical data and identity readiness became affirmative, while broker-realistic CAS execution remained separate. | Phase 6D report |
| Phase 6E | One predeclared 78-symbol, 20-session frozen streaming replay completed with dated CAS calendar; all variants were gross- and net-negative. | Phase 6E report and reproducibility summary |
| Phase 7 | Two deterministic leader-first parent strategies and E1–E4 entry-quality plans were prototyped offline; no replay was run because dated September eligibility/CAS evidence is missing. | Phase 7 research design |
| Phase 7A | The independent September calendar was corrected to twenty sessions, and the leader-first prototype/cache readiness audit stopped the replay on data and integration blockers. | Phase 7A readiness |
| Phase 7B | The external September cache was located, frozen leader rules were repaired, and a paper-only leader streaming integration was tested; master-backed data eligibility remains blocked. | Phase 7B integration report |

The reports are historical research records. Their statements about service state, test totals, data coverage and authorization apply to their recorded dates and do not assert a current deployment state.

## Documents

| Phase | Document | Purpose | Original source commit | Excluded dependencies |
|---|---|---|---|---|
| Pre-feature | [UT_SYNC master handoff](../../UT_SYNC_NEW_CHAT_MASTER_CONTEXT.md) | Strategy context, A/B/C proposal and safety constraints. | `af111b5f130f1343028d899205dd113d63c00303` | Pine files and handoff ZIP are excluded. |
| Initial | [Paper-trading README](../README.md) | Package scope, A/B/C definitions and local replay usage. | `6b442e2b687f38b0ef503899f151874ae53ed8b0` | Code, configuration JSON and data roots are excluded. |
| Initial | [Baseline audit](BASELINE_AUDIT.md) | Initial repository, safety, data and parity assessment. | `6b442e2b687f38b0ef503899f151874ae53ed8b0` | Source tree, services, caches and Pine files are excluded. |
| Initial | [Fee assumptions](FEE_ASSUMPTIONS.md) | Research fee/slippage assumptions and sources. | `6b442e2b687f38b0ef503899f151874ae53ed8b0` | Configuration/code are excluded. |
| Initial | [Safety and parity](SAFETY_AND_PARITY.md) | Offline and no-order safety boundary; parity limitations. | `6b442e2b687f38b0ef503899f151874ae53ed8b0` | Source code and external TradingView exports are excluded. |
| Initial | [Implementation handoff](HANDOFF_2026-10-10.md) | Initial replay results, limitations and operational handoff. | `6b442e2b687f38b0ef503899f151874ae53ed8b0` | Raw inputs, ledgers, previews and code are excluded. |
| Data readiness | [Historical-data readiness](DATA_READINESS_REPORT.md) | Cache inventory, NIFTY 100/500 matrices, scalability and backfill plan. | `0aa4d8c6ce81e933158cded333466dbd7f5e951a` | CSV matrices, cache files and manifests are excluded. |
| Phase 3 | [Replay integrity](PHASE3_REPLAY_INTEGRITY_REPORT.md) | Integrity fixes, session/EOD behavior and no-lookahead safeguards. | `0aa4d8c6ce81e933158cded333466dbd7f5e951a` | Code, tests and evidence CSVs are excluded. |
| Phase 4 | [Streaming and checkpoints](PHASE4_STREAMING_CHECKPOINT_REPORT.md) | Bounded streaming replay, portfolio ordering and recovery validation. | `d72c5171cb7e7d543dc2ec2e7d8ddbfe749a151a` | Code, SQLite sources and benchmark outputs are excluded. |
| Phase 5 | [Data and parity](PHASE5_DATA_PARITY_REPORT.md) | Broad-universe data/parity blockers and bounded benchmark plan. | `b34bcf07aaee3d07501134b5b1547359712136e5` | Evidence CSV/JSON, source/Pine exports and data caches are excluded. |
| Phase 5A | [Benchmark backfill](PHASE5A_BENCHMARK_BACKFILL_REPORT.md) | Authorized NIFTY benchmark retrieval, validation and provenance. | `b2711784b63df3d70f0161b7ef7d8099064011bf` | Raw HTTP responses, normalized candles and provenance JSON are excluded. |
| Phase 5B | [Data integration](PHASE5B_DATA_INTEGRATION_REPORT.md) | Benchmark integration, stock-tail audit and broad-universe readiness. | `06dd5840d87e00cedf645837edabfd755e71ed16` | Evidence manifests, normalized cache and code are excluded. |
| Phase 5C | [Stock-backfill pilot](PHASE5C_STOCK_BACKFILL_PILOT_REPORT.md) | Bounded 20-request read-only repair pilot and validation. | `f81a0756a8b625dc4c03f7553b1ab2f23b106792` | Raw responses, pilot cache and sanitized JSON evidence are excluded. |
| Phase 5D | [CAS session audit](PHASE5D_CAS_SESSION_AUDIT.md) | CAS classification, tail-gap recalculation and EOD findings. | `716b0aa50feb33174614894c767d8062347b5d41` | Classification matrices, request manifests and code are excluded. |
| Phase 5E | [CAS execution readiness](PHASE5E_CAS_EXECUTION_READINESS_REPORT.md) | Dated CAS evidence limits and broker-realistic EOD sensitivity. | `e44b27d4074c81c064b58bda8f24c96d05d4a588` | Generated matrices/manifests, code and tests are excluded. |
| Phase 6A | [Exploratory backtest](PHASE6A_EXPLORATORY_BACKTEST_REPORT.md) | Fixed-cohort A/B/C results, costs, and limitations. | `0daf8a681214e2e3c44c060021c05268f275a766` | Raw candles, journals, trade/event exports, and detailed manifests are excluded. |
| Phase 6A | [Trade forensics](PHASE6A_TRADE_FORENSICS.md) | Aggregate loss, MFE/MAE, gate, and counterfactual analysis. | local research analysis, not committed on feature branch | Raw trades/events and detailed indicator rows are excluded. |
| Phase 6A | [Detailed aggregate trading evidence](PHASE6A_AGGREGATE_TRADING_EVIDENCE.md) | Entry-type, MFE/MAE, cost, exit, funnel, and concentration tables. | authoritative `verified2` local research artifacts | Raw trades/events, local artifact paths, and per-entry rows are excluded. |
| Phase 6B | [Extended-evaluation limitation and research design](PHASE6B_EXTENDED_EVALUATION_LIMITATION_AND_RESEARCH_DESIGN.md) | Why no 20-session replay was defensible; predeclared future research designs. | offline Phase 5E/6A evidence review | Raw classification matrices, artifacts, and implementation are excluded. |
| Phase 6B | [Historical CAS blocker audit](PHASE6B_HISTORICAL_CAS_BLOCKER_AUDIT.md) | Frozen 20-session window, official CM-MII schema findings, exact no-download acquisition manifest, cache reconciliation and readiness decision. | offline Phase 5D/5E/6B evidence and official public-source review | Dated CM-MII masters, circular packages, raw matrices and implementation are excluded. |
| Phase 6C | [CAS classification results](PHASE6C_CAS_CLASSIFICATION_RESULTS.md) | Approved public NSE acquisition, dated CAS/non-CAS classification, candle completeness and the remaining identity blocker. | offline acquisition and classification audit; feature branch unchanged | Raw circulars, dated masters, response metadata and the 1,560-row classification ledger are excluded. |
| Phase 6D | [KOTAKBANK identity resolution](PHASE6D_KOTAKBANK_IDENTITY_RESOLUTION.md) | Dated split mapping, identity reconciliation, CAS reclassification and readiness distinction. | `3425a014dd16f8fe832f958438de3a0163af7d12` | Circular PDFs, raw masters, local maps, cache rows and source code are excluded. |
| Phase 6E | [Frozen 20-session A/B/C backtest](PHASE6E_20_SESSION_ABC_BACKTEST_REPORT.md) | Predeclared 78-symbol streaming reconstruction, aggregate results, costs, exits, MFE/MAE and limitations. | `e4c78e931acd4615d6dbf09022ccf320fbabc4ee` | Immutable manifest, raw candles, normalized source, SQLite journal, trade/event CSVs and code are excluded. |
| Phase 6E | [Reproducibility summary](PHASE6E_REPRODUCIBILITY_SUMMARY.md) | Run identifiers, input hashes and reconciled aggregate results. | `e4c78e931acd4615d6dbf09022ccf320fbabc4ee` | Raw research artifacts and source implementation are excluded. |
| Phase 7 | [Leader-first momentum research design](PHASE7_LEADER_FIRST_RESEARCH_DESIGN.md) | Two pre-registered independent parent strategies, E1–E4 plans, deterministic prototype tests, later-holdout blocker and fair-test protocol. | `68dcb940e5c64676c2cd94224cb4e5eafa615996` | Raw artifact queries, market data, master files, journals and code are excluded. |
| Phase 7A | [Leader-first evaluation readiness](PHASE7A_LEADER_FIRST_EVALUATION_READINESS.md) | Calendar correction, cache/benchmark inspection, contract audit, data blockers and exact master-reference manifest. | `176bc79b1b2df21453613c575f2624d92ab132d2` | External cache contents, masters, journals and implementation are excluded. |
| Phase 7B | [Leader-first integration and readiness repairs](PHASE7B_LEADER_FIRST_INTEGRATION_REPORT.md) | External-cache inventory, frozen-contract repairs, paper-only streaming integration, tests and remaining September data blockers. | `50cba95cc374688f9abd0fdf3a136ce68797e94c` | Raw cache files, master files, journals, source code and operational detail are excluded. |
| Policy | [Public review policy](CODEX_PUBLIC_REVIEW_POLICY.md) | Required sanitization and documentation-only publication workflow. | this documentation commit | Implementation and production systems are out of scope. |

## Missing from this export

- The original unredacted documents and original feature branch.
- Raw market candles, API responses, local caches, SQLite ledgers, authentication/session material and service configuration.
- Pine sources, Python implementation, configuration JSON and test code.
- Machine-readable evidence files referenced by reports.
- TradingView timestamped exports required for external Pine parity.

Those omissions are intentional. They prevent this review branch from becoming a data, credentials, operational configuration or executable-strategy distribution channel.

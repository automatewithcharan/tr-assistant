# Redaction log — UT_SYNC documentation review export

This branch contains sanitized copies of Markdown from feature commit `e44b27d4074c81c064b58bda8f24c96d05d4a588`. The originals remain unchanged in the isolated feature worktree and are not published by this branch.

## Applied replacements

- Original absolute paper-worktree path → `${PAPER_WORKTREE}`.
- Original absolute repository-root path → `${REPO_ROOT}`.
- Other user-home absolute paths → `${LOCAL_PATH}`; a bare user-home directory → `${USER_HOME}`.
- Historical numeric process identifiers → `${SERVICE_PID}` or `${SERVICE_PIDS}`.
- The master handoff's historical operational health snapshot was replaced with a statement that protected services were not modified. Removed details include notification/Discord state, candidate and instrument counts, kill-switch state, and authentication/session-path repair details.
- References to session/auth paths in the master handoff were generalized to protected-service boundaries.

## Intentionally preserved

Research conclusions, limitations, A/B/C definitions, parameter values, performance figures, data-coverage findings, CAS classifications, backfill counts, test results, source-code-relative paths, commit hashes, public documentation links, and request-window descriptions are unchanged except where a local path placeholder was required.

No access token, API key, password, session cookie, authorization header, webhook URL, raw market data, SQLite database, HTTP response, trade ledger, or source code is included in this export.

## Phase 6A addition

- Published only aggregate research tables and conclusions from the exploratory replay and forensic analysis.
- Excluded local artifact paths, input manifests, raw candles, SQLite journals, full trade/event exports, unsent notification previews, and per-entry indicator rows.
- Preserved unfavorable gross/net results, fees, loss distributions, MFE/MAE counts, exit statistics, selection-bias limitations, and the no-broker/no-production conclusion.

## Phase 6A detailed aggregate evidence addition

- Preserved only aggregate distributions, fee components, candidate funnels, daily totals, and symbol-concentration summaries.
- Explicitly marked individual ₹ spread/slippage attribution unavailable because the journal embeds it in adverse proxy fills.
- Excluded raw trades, events, forensic CSV rows, local artifacts, and source-data references.

## Phase 6B addition

- Preserved aggregate CAS-evidence counts, the no-replay decision, and future-study design.
- Excluded local master-file inventory, request manifests, raw matrices, paths, and service details.

## Phase 6B historical CAS blocker audit addition

- Published the exact official public URLs, expected filenames, fixed evaluation dates, aggregate cache counts, source hashes and validation controls needed for independent review.
- Replaced local evidence locations with artifact descriptions; no username, absolute path, service detail, credential, cookie or account identifier is included.
- Excluded all raw market candles, local coverage CSV rows, HTTP response bodies, authentication material, trading journals and implementation source.
- Preserved the unfavorable `NO` readiness conclusion, the 1,540 unresolved cohort-session count and all stated selection-bias limitations.

## Phase 6C historical CAS classification addition

- Preserved the exact request IDs, official public filenames, file hashes, schema findings, classification counts, symbol classifications, candle-quality totals, unresolved identity evidence and unfavorable `NO` readiness conclusion.
- Published only aggregate and symbol-level research findings. Excluded the 22 raw source files, HTTP headers, extracted circular content, response bodies, local artifact paths and the 1,560-row classification ledger.
- The report was authored with repository-relative evidence descriptions; no personal path replacement was required.
- No credentials, cookies, authorization headers, account identifiers, raw market candles, broker data or operational service details are included.

## Phase 6D KOTAKBANK identity-resolution addition

- Preserved the user-supplied official circular identifiers, dated old/new ISIN mapping, token/master crosswalk, classification totals, test results and the separate broker-execution limitation.
- Explicitly retained the limitation that the two circular PDFs were not locally present or byte/hash-verified; no document was downloaded in Phase 6D.
- Excluded raw NSE masters, the original research ledger, local instrument maps, candle-cache rows, absolute paths and implementation source.

## Phase 6E frozen backtest addition

- Preserved the predeclared cohort/dates, CAS counts, immutable hashes, unfavorable gross/net results, cost components, exit and MFE/MAE aggregates, daily aggregates, symbol-concentration observations, test status and all execution limitations.
- Excluded the raw candle cache, normalized-source and replay SQLite files, detailed trade/event CSVs, full input file manifest, local artifact paths, credentials and implementation code.
- Published no broker fills, account data, service state, token, cookie, webhook or personal path. The report states that reconstructed bar-open fills are not verified broker fills.

## Review limitation

Evidence CSV/JSON files, raw research caches, databases, Pine source, configuration JSON, and implementation code are deliberately excluded. Reports identify many of those dependencies by relative path and/or SHA-256, but those references cannot be opened from this documentation-only branch.

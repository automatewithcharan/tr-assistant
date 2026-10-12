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

## Review limitation

Evidence CSV/JSON files, raw research caches, databases, Pine source, configuration JSON, and implementation code are deliberately excluded. Reports identify many of those dependencies by relative path and/or SHA-256, but those references cannot be opened from this documentation-only branch.

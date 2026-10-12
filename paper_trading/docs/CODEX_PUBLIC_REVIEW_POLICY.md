# Public review documentation policy

For each completed UT_SYNC paper-trading milestone, prepare a detailed local report, a sanitized Markdown review copy, and an updated `REVIEW_INDEX.md`. Before publication, inspect the complete documentation-only diff and scan for credentials, tokens, cookies, webhooks, personal paths, usernames, account identifiers, session locations, raw data, SQLite files, trade ledgers, and operational service details.

Only sanitized Markdown and the index/redaction log may be committed to `codex/ut-sync-paper-docs-review-20261012`. Do not push the implementation branch, merge into main, force-push, publish raw research artifacts, or alter production services. If scanning or branch-integrity review fails, stop publication and report the blocker.

After a permitted push, verify the GitHub raw URL and return it in the task handback. This policy authorizes documentation publication only; it never authorizes broker actions, API downloads, Discord sends, live-paper activation, deployment, or strategy changes.

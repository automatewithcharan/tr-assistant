# UT_SYNC paper-trading baseline audit

Observed 2026-10-10 UTC/IST context. This document is an implementation baseline, not evidence of profitability or TradingView alert parity.

## Repository and isolation

- Source repository: `${REPO_ROOT}`.
- Source checkout was on dirty branch `codex/ng-preopen-leader-radar-v1-phase15` at `0035aca76019fd791ef15a79a7974f0a8856a782`; its existing tracked and untracked changes were not touched.
- Fresh remote `origin/main`: `af111b5f130f1343028d899205dd113d63c00303` (`paper trading`, 2026-10-10T11:37:40+05:30).
- Isolated implementation worktree: `${PAPER_WORKTREE}`, branch `codex/ut-sync-abc-paper-20261010`, based on the fresh remote commit above.
- The requested `CODEX_UT_SYNC_ABC_PAPER_IMPLEMENTATION_2026-10-10.md` basename is absent from `origin/main`; the complete supplied specification was read from the task attachment `pasted-text-1.txt`.

## Authoritative sources

| Purpose | Actual location | SHA-256 | Observation |
|---|---|---|---|
| Current UT_SYNC | `UT_SYNC_v2_7a_ENTRY_DIAGNOSTICS (1).pine` | `a59f373a1e7e8cd4e5df0f749efae617e15fdc47eabe31d84e9d6348555ecdc9` | Name differs by ` (1)`; bytes equal bundled canonical basename. Pine v6 `indicator("UT_SYNC", ...)`. |
| QQC companion | `QQC_CLEAR_UT_SYNC_COMPANION_v6.pine` | `7aeff8247a6879bffd02dc2862da34f9d2c163968ad664f3d087c3865532b577` | Pine v6 simplified 9-bar QQC; not proprietary-original parity. |
| Master handoff | `UT_SYNC_NEW_CHAT_MASTER_CONTEXT.md` | `33790f639a1160fe277de98299de2dda568d28d15b3997cdb1594ec79e4977d1` | Read completely. |
| v2.7 predecessor | inside `UT_SYNC_NEW_CHAT_HANDOFF_BUNDLE.zip` | `41b55a2d312abe4090cacc567f5ed2aae928c40f4140cb2b56aab6ac55d38363` | Not present as a standalone tracked file. |
| Handoff bundle | `UT_SYNC_NEW_CHAT_HANDOFF_BUNDLE.zip` | `0c28d1113fe3bb79ceec5e11b56c4dbbf2a9bb07abf93cb675e42382abfc5410` | Its UT and QQC bytes equal the standalone tracked files. |

## Existing topology and frozen boundaries

- Existing Kite/auth/data modules: `pkscreener/classes/KiteConnectManager.py`, `KiteHistoricalData.py`, `KiteTickerStream.py`, and `KiteOrderManager.py`. The new package will not import the order manager or production runtime.
- Existing Discord modules: `NotificationService.py`, `DiscordTradeBot.py`, and `SignalMonitor.py`. The new notifier will be isolated, console/file by default, and will not read a production webhook.
- Existing paper/backtest code is spread across `pkscreener/classes/*Paper*.py`, `Backtest*.py`, and `scripts/*paper*`/`scripts/*backtest*`. The new implementation is additive and self-contained.
- Existing scheduling uses cron and systemd units under `deploy/systemd/`, `systemd/`, and host configuration. No schedule or unit will be modified or started.
- Protected running processes observed before implementation:
  - PID ${SERVICE_PID}: `ng_nse_top_gainers_watch.py` (read-only top-gainers/radar watch).
  - PID ${SERVICE_PID}: `ng_emerging_leader_alerts.py` (Strict Emerging Leader Discord publisher).
  - PID ${SERVICE_PID}: `ng_ut_sync_leader_watch.py` (shadow UT-SYNC leader watch).
- Active system units included `ng-emerging-leader-alerts.service` and `ng-ut-sync-leader-watch.service`. These processes, source trees, state DBs, canonical Kite auth/session files, configs, cron, and production Discord behavior are frozen boundaries.

## Data observations and limitations

- Genuine local Kite minute CSV caches exist read-only under `${REPO_ROOT}/results/Data/kite_history_backfill/minute/`, including daily directories from July through 2026-10-09 and a broad symbol universe.
- CSV timestamps are naive IST candle-start times with columns `datetime,open,high,low,close,volume,symbol`; the adapter must attach `Asia/Kolkata`, derive end time, validate completeness/order/session/values, and never mutate source data.
- `NIFTY 50.csv` is present only for some dates. Strategy C must fail closed with `RS_BENCHMARK_MISSING` where aligned benchmark data are unavailable.
- Time-of-day RVOL needs at least ten earlier valid sessions for the same symbol/minute; early samples must produce `RVOL_WARMUP`.
- No audited TradingView per-bar export was found. The IFCI alert anecdote is not enough for numeric formula parity, execution parity, or exact historical-alert parity.
- Pine realtime `varip`, tick-observed live drift, server alert creation state, spread/order book, and true tick order cannot be reconstructed from OHLC. Historical A is therefore labeled `A_UT_V27A_BAR_CLOSE_RECONSTRUCTION`.

## Source-to-Python parity matrix

| Pine construct | Python implementation requirement | Parity level |
|---|---|---|
| `ta.atr(10/14)` | Wilder RMA with Pine-like seed/warm-up | Formula reconstruction; deterministic fixtures |
| Recursive UT trail/crosses | Continuous across sessions; no midnight reset | Formula reconstruction |
| `ta.sar(.02,.02,.20)` | State-machine PSAR with Pine initialization semantics | Formula reconstruction; tolerance tests |
| `ta.vwap(hlc3)` | Volume-weighted HLC3 reset each exchange session | Formula reconstruction |
| Fresh/continuation rules | Exact completed-bar age, prior-high exclusion, time-close window, stop tick floor | Predicate reconstruction |
| `request.security` prior 5m EMA | Only fully closed 09:15-aligned 5m buckets; use previous and predecessor buckets | No-lookahead reconstruction |
| QQC 9-bar loops | Exact up/down, body sum, signed body/range volume; zero-range contributes zero | Formula reconstruction |
| Realtime AUTO SIGNALS | Separate live-observation semantics; never inferred from historical bars | Irreducible without raw tick/alert evidence |
| Grey labels / `NO BUY` | Diagnostics only | Excluded from trading predicates |

## Proposed additive implementation

- `paper_trading/`: immutable models/config, validated CSV replay and read-only Kite protocol adapter, indicators, common candidate generation, A/B/C filters, independent portfolios/execution/risk/fees, SQLite journal, local-only notifier/report/parity export, CLI (`doctor`, `validate-config`, `replay`, `report`, guarded `live-paper`).
- `tests/paper_trading/`: deterministic formula, session, filter, fill/risk/fee, idempotency, and broker-mutation tripwire tests.
- `paper_trading/fixtures/`, `paper_trading/docs/`, and `.env.paper.example`: fixtures, generated local examples, configuration/rollback guidance.

No real broker order path, dormant live-order toggle, production import, deployment, scheduler mutation, process restart, or Discord send will be added.

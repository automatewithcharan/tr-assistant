# Phase 4 full NSE equity streaming replay and checkpoint recovery report

Date: 2026-10-10  
Worktree: `${PAPER_WORKTREE}`  
Branch: `codex/ut-sync-abc-paper-20261010`  
Starting commit: `0aa4d8c6ce81e933158cded333466dbd7f5e951a`  
Scope: replay engineering and recovery verification only. No profitability study.

## Outcome

The paper engine now has a genuinely streaming replay path. It reads a disk-normalized SQLite candle stream in `(bar_end, symbol)` order, updates compact per-symbol indicator state one candle at a time, creates at most one current candidate per observation, processes one complete timestamp batch through the three portfolios, and writes events/trades directly to SQLite. It does not retain candle histories, indicator timelines, candidate histories, global observation lists, or resolved-trade objects in streaming mode.

Atomic compressed checkpoints restore indicator, candidate, execution and A/B/C portfolio state. Four mid-session interruption points plus a near-EOD interruption produced the same normalized events, trades, fees and summaries as uninterrupted runs. A checkpoint from different data, configuration or software fails closed.

The frozen A/B/C definitions and Phase 3 execution safeguards were preserved. Internal numerical equivalence is verified; independent TradingView parity is not.

## Architecture before and after

| Area | Phase 3 | Phase 4 streaming path |
|---|---|---|
| Source ingestion | Every CSV became an in-memory `list[Candle]` | Files are read one row at a time into a normalized, indexed SQLite source. Original files are read-only. One input file and one SQLite connection are active. |
| Ordering | Heap merge was bounded, but its input arrays were not | One SQLite ordered cursor supplies all symbols and the benchmark in deterministic `(bar_end, symbol)` order. Mapping fixtures use the existing heap merge. |
| Indicators | Full arrays for ATR, UT, PSAR, VWAP, EMA, DMI/ADX, CHOP, QQC, RVOL, RS and OR | Serializable recursive/rolling state per symbol: two RMA ATRs, DMI/ADX RMAs, UT state, two PSAR bars, five 1m bucket bars, 14 CHOP values, nine QQC values, 20 prior volumes for each of 375 minutes, and small session fields. |
| Candidates | Full candidate dictionary for all history | Candidate is derived causally from the current snapshot and discarded after the timestamp batch. Only current UT-leg/attempt state remains. |
| Benchmark | Full benchmark lookup | The benchmark candle is taken from the same current timestamp batch; it is not forward-filled or retained as a series. |
| Portfolio | Resolved `Trade` objects and session dictionaries grew indefinitely | Streaming portfolios write trades immediately and retain only totals. Prior-session leg, entry, realized and re-entry maps are pruned at session transition. Open positions/intents remain bounded by the universe/risk rules. |
| Audit data | SQLite events/trades | Same append-only ledger plus `replay_checkpoints`, `universe_eligibility`, full disk-based source quality issues and incomplete trades. |

Memory is bounded by the configured universe size and fixed rolling windows, not by replay duration. The normalized SQLite source and event ledger intentionally grow on disk with input/events. The CLI's deterministic path manifest remains proportional to the number of source partitions; it contains paths, not candles. This is the only history-length-dependent in-memory structure still documented, and is much smaller than market data. A future manifest table/iterator can remove it if partition counts become materially larger.

## Indicator and signal preservation

The streaming state was compared bar-for-bar against the previous batch engine over 960 deterministic candles spanning a deliberate storage split and multiple sessions. Every `IndicatorSnapshot` field matched (floating values to absolute tolerance `1e-10`), including:

- Wilder ATR10/ATR14, recursive UT(3) trail and BUY/SELL crosses;
- PSAR state and reversals;
- session VWAP, including zero-volume behavior;
- prior fully completed, 09:15-aligned 5-minute EMA20 and slope;
- Wilder DMI/ADX14/14 and CHOP14;
- QQC nine-bar quantity, body-quality and signed-volume conditions;
- same-minute RVOL using only prior observations;
- exact-timestamp NIFTY relative strength and three-minute prior value;
- five-minute opening range and two-close hold count.

Candidate IDs, reasons, values, UT leg ages, fresh/continuation classification and ordering also matched. Recursive states are not reset at file, SQLite partition or session boundaries. Only VWAP, OR, RS-open and causal session-quality state reset by exchange session as required.

On a two-symbol/two-session execution fixture, the previous and streaming engines produced identical A/B/C summaries and identical normalized candidate, decision, fill, stop, session-checkpoint, trade, fee and portfolio-summary records. Run/event IDs differ intentionally because the engine version is part of provenance.

This is internal implementation equivalence—not proof of TradingView server-alert parity. Pine realtime `varip`, tick paths, alert creation state and latency remain irreducible from historical OHLC. No TradingView export was supplied for independent numeric verification.

## Deterministic event and execution order

Each timestamp is one atomic unit:

1. identify the exactly aligned benchmark observation;
2. apply session transition and expire/censor prior-session intents/positions;
3. process all pending fills/exits in lexicographic symbol order;
4. update current indicators/candidates per symbol;
5. apply conservative ranges and confirmed-close exits;
6. update all marks and portfolio equity;
7. process common candidates in lexicographic symbol order for independent A/B/C portfolios;
8. write a compatible checkpoint and ledger commit together.

Lexicographic symbol priority is deterministic and uses no future information. It is economically arbitrary, remains explicitly versioned as `LEXICOGRAPHIC_SYMBOL`, and must be frozen before research. No parallel shard independently allocates portfolio capital. Each variant enforces its global cash, three-position, open-risk and daily-loss limits across every symbol.

Phase 3 behavior remains: no signal-close fills, no zero-volume fills, minimum turnover/participation gates, adverse spread/slippage, old-stop-before-new-trail ordering, gap-through stops, same-session EOD only, and separate expectancy-excluded incomplete trades.

## Checkpoint and recovery design

`replay_checkpoints` stores one compressed state blob per deterministic run ID. It contains:

- data, configuration and code hashes;
- last fully committed global timestamp and batch count;
- all per-symbol rolling and session indicator/candidate states;
- current marks and market date;
- A/B/C cash, positions, active stops, peaks, pending intents, consumed legs, entry/daily-risk counters, drawdown/high-water marks, incomplete/resolved totals and deterministic IDs.

Checkpoint replacement and all preceding ledger writes share one SQLite transaction and commit. Checkpoints occur only after the entire simultaneous timestamp batch. A crash within a timestamp therefore leaves neither a partial checkpoint nor committed partial-batch events. Resume skips through the last committed timestamp and starts at the next one. SQLite uniqueness constraints remain a second duplicate-event/fill barrier.

The default checkpoint interval is 15 timestamp batches; tests use every batch to target exact interruption points. Completion deletes the checkpoint only after final summary events are written. A stale checkpoint with a different run ID causes a fail-closed incompatibility error instead of silently restarting against altered inputs.

Verified interruptions include before the first actionable region, mid-session, while the fixture has active execution state, late-session, and minute 360 near EOD. Because simultaneous symbols are one atomic batch, “between simultaneous events” is recovered by rolling back/replaying that entire uncommitted timestamp; no partial simultaneous allocation is a valid checkpoint.

## Memory and runtime benchmarks

Command: `python -m paper_trading.benchmark_streaming --symbols N --minutes 375`. Each run was a fresh process using a deterministic one-session synthetic engineering source, constant liquidity, no strategy optimization and zero resolved trades. RSS includes Python, SQLite and engine state. These are not profitability results.

| Symbols | Observations | Checkpoints | Runtime | Peak RSS |
|---:|---:|---:|---:|---:|
| 9 | 3,375 | disabled for steady-state measure | 0.166 s | 23,556 KiB (23.0 MiB) |
| 100 | 37,500 | disabled | 1.437 s | 31,196 KiB (30.5 MiB) |
| 500 | 187,500 | disabled | 10.045 s | 66,616 KiB (65.1 MiB) |
| 2,335 | 875,625 | disabled | 54.127 s | 226,964 KiB (221.6 MiB) |
| 2,335 | 875,625 | one final full checkpoint | 57.622 s | 566,816 KiB (553.5 MiB) |

The full checkpoint transient is bounded but material because pickle plus compression temporarily coexist with live state. Frequent 2,335-symbol full snapshots will add I/O/CPU; the 15-minute default was not included in the steady-state timing table. A delta/page-oriented checkpoint is recommended before multi-month broad-universe production research, although current full checkpoints are functionally correct.

No broad real-data replay was run. The existing approximately 2,335 current-snapshot EQ matches are not a valid historical universe and the cached NIFTY benchmark remains sparse.

## Broad-universe interface

`universe.py` provides four explicit research contracts:

- `FixedStartUniverse`: freezes all supplied names at a documented start date and retains unsuccessful, renamed, merged or later-delisted members in the denominator.
- `PointInTimeUniverse`: selects only the latest evidenced snapshot effective on or before each market date; future snapshots cannot affect earlier dates.
- `CurrentSnapshotResearchUniverse`: labels every decision `CURRENT_SNAPSHOT_SELECTION_BIAS`; it never masquerades as point-in-time eligibility.
- `causal_liquidity_decisions`: screens the broad NSE EQ set using only caller-supplied prior-session turnover and records pass/fail reasons, including symbols with zero trades.

`universe_eligibility` has a unique row for every `(run, date, symbol)`, with eligibility, reason, cohort label and evidence date. The engine does not silently substitute current NSE membership when point-in-time evidence is absent. Historically eligible renamed/delisted/merged stocks require explicit dated identity links; absence is an exclusion record, not silent deletion.

## Data limitations and approval gates

1. NIFTY 50 one-minute data is complete on only 10 of 68 cached stock dates. Strategy C must fail closed elsewhere.
2. The approximately 2,335-symbol latest EQ match is suitable only for engineering load tests/current-snapshot-biased research—not historical claims.
3. Official dated NSE EQ masters, listings/suspensions/delistings, symbol/ISIN successor mappings, series changes and corporate actions are still required.
4. Historical per-instrument tick sizes remain unresolved; the current global tick default is not sufficient for a definitive broad study.
5. Corporate-action raw/adjusted price policy and discontinuity gates remain unresolved.
6. The SQLite normalizer preserves full issue rows on disk while returning at most 1,000 warnings in memory. Invalid/conflicting input remains fail-closed/deterministically first-source.
7. Full-state checkpoint serialization has a 553.5 MiB measured transient at 2,335 symbols; delta checkpoints are the next scaling improvement.

Recommended next phase: freeze an evidence-backed historical universe protocol and identity/tick/corporate-action tables; optimize checkpoint deltas; obtain separate approval for the two-request benchmark backfill; rebuild coverage; then validate NIFTY 100 followed by NIFTY 500 and a broad point-in-time cohort. Profitability replay requires another explicit approval after those gates.

## Tests and warnings

- Focused paper/streaming suites: **58 passed**.
- Full repository offline suite: **1,303 passed**, 15 warnings in 85.92 seconds (final run).
- Warnings are the existing Discord `audioop` deprecation, multiprocessing fork warning and parsed legacy escape-sequence warnings.
- Paper doctor: `paper_only=true`, zero broker mutation paths, no network sender and live paper disabled.
- Tests cover batch/stream formula and candidate equivalence, prior-engine execution equivalence, four mid-session resumes, near-EOD resume, simultaneous ordering, portfolio position limits, partition continuity, future-data append invariance, missing benchmark failure, zero-volume behavior, EOD/incomplete treatment, incompatible checkpoints, disk source streaming, universe modes and broker mutation tripwires.

## Changed files

- `paper_trading/streaming.py`: incremental indicators/candidates, normalized disk source and checkpointable global replay.
- `paper_trading/benchmark_streaming.py`: offline synthetic scale benchmark.
- `paper_trading/universe.py`: explicit fixed, point-in-time, current-snapshot and causal-liquidity universe contracts.
- `paper_trading/engine.py`: bounded streaming trade totals and session-state pruning.
- `paper_trading/journal.py`: checkpoints and per-date universe eligibility.
- `paper_trading/cli.py`: opt-in `replay --streaming` and normalized source database.
- `tests/paper_trading/test_streaming_checkpoint.py`: Phase 4 deterministic regressions.
- This report.

The feature commit is the commit containing this report on `codex/ut-sync-abc-paper-20261010`; its immutable SHA is reported after commit because a commit cannot contain its own final hash.

## Protected systems and safety

Before and after read-only process snapshots show unchanged production PIDs ${SERVICE_PIDS} for the protected emerging-leader/UT-SYNC processes. No production source, configuration, scheduler, auth/session file or process was modified or restarted.

No Kite endpoint was called. No historical data was downloaded. No real/paper broker order, modification, cancellation or GTT operation occurred. No Discord message was sent. Nothing was pushed, merged, deployed, scheduled or activated.

Final status: **bounded-memory streaming and deterministic checkpoint recovery implemented and internally verified in the isolated feature worktree; broad historical eligibility/data and independent TradingView parity remain blocked; no profitability replay; not deployed; zero Kite requests and zero broker orders.**

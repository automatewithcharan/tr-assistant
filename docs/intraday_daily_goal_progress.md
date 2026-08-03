# Intraday Daily Goal Progress

> **Historical research/evidence log.** Labels such as accepted, validated,
> promotable, or production-ready do not authorize real orders. Current runtime
> safety status is in `docs/intraday_real_trading_start.md`; no strategy is on
> the live execution allowlist.

Last updated: 2026-07-27

## Goal

Build and validate an intraday trading assistant for NSE/BSE stocks with a daily portfolio objective.

- Starting capital: approximately Rs 5,00,000.
- Initial research and promotion target: at least Rs 10,000 net per market
  day on average after spread, slippage, fees, and an additional stress cost.
- At Rs 5,00,000, Rs 10,000 is a 2% daily account return. It is a strict
  evidence hurdle, not a guaranteed income claim.
- Planned external contribution: Rs 2,00,000 at the end of each month.
- Compounding objective: profits remain in capital so the next day starts from the larger capital base.
- Upside objective: preserve gains above the daily hurdle on strong days when
  a forward-validated exit policy supports doing so.
- Do not cap upside unnecessarily; preserve unusually strong days if trailing/partial exits improve total portfolio return.
- Position style: multiple stocks per day, not a fixed 4-stock cap.
- Capital use: deploy a daily basket from the same capital pool; optionally compound profits or add external capital later.
- Trading style: intraday only, with exits by target, stop, or end of day.
- Research scope: compare multiple predeclared strategy families, entry times,
  exits, ranking rules, and capital allocations. Do not promote a lane because
  it found historical top gainers; only executable forward P&L counts.

Important principle: the system must prove that it can identify likely winners before the move. Hindsight opportunities do not count as a live edge.

## Grade-B Real Automation Disabled 2026-07-11

Decision:

- Real `grade_b_confirmation` automation is disabled.
- `ENABLE_GRADE_B_AUTOTRADE=0` is explicit in the live `tr-assistant/.env.dev`.
- Both live launchers now default `ENABLE_GRADE_B_AUTOTRADE` to `0`, so a missing environment setting cannot silently re-enable broker orders.
- Scanner alerts and research recording remain available. This lane must return through paper trading and forward validation before any real-order re-enable decision.

Reason:

- Broker fills from 2026-06-30 through 2026-07-10 produced about `Rs -4,746` gross before charges.
- There were `10` closed executed trades: `4` winning trades and `6` losing trades, but only `1` profitable day.
- The four individual winners were `SIKA` (`Rs +417`), `DJML` on 2026-07-03 (`Rs +131`), `DJML` on 2026-07-09 (`Rs +27`), and `SAGCEM` (`Rs +431`).
- 2026-07-09 still lost about `Rs 422` because the `THANGAMAYL` loss of about `Rs 880` exceeded the two winners that day.
- Average gross winner was about `Rs 252`; average gross loser was about `Rs 959`. The `+0.7%` target and `-2.5%` stop require roughly a `78%` win rate before charges, but live win rate was `40%`.
- The old documented `17/17` result is not accepted as validation. The retained replay artifact has only three confirmed evaluable rows, of which one reached the `+0.7%` target; most historical rows lacked usable intraday candles.
- At the time of this audit, the guardrail read `livetrade_main_trades_latest.csv`, which was absent from the live runtime data directory, so autotrade losses were not feeding the advertised daily-loss and consecutive-loss controls. The paper-lane work below repairs this fallback.

Required before paper trading is promoted:

- Replace the single-tick `+0.25%` trigger with a replayable entry decision using candle hold, VWAP, post-alert volume, price extension, pullback, spread, and order-book stability.
- Rank all waiting candidates instead of buying the first symbol that crosses its trigger.
- Evaluate targets, stops, slippage, charges, and event ordering with walk-forward data.
- Connect paper and real guardrails to the same append-only execution ledger.
- Require a minimum sample and positive out-of-sample expectancy before real orders can be explicitly re-enabled.

## Ranked Confirmation Paper Lane 2026-07-11

Status:

- Enabled for the next live scanner session with
  `SIGNALMONITOR_ENABLE_RANKED_CONFIRMATION_PAPER=1`.
- Paper only. The implementation does not import or call `KiteOrderManager`.
- Real Grade-B automation remains explicitly disabled.

Entry experiment `ranked_sustained_grade_b_v1`:

- Registers Grade-B alerts but does not enter at the alert or first crossing tick.
- Observes at least `60` seconds and at least `4` live observations.
- Requires the confirmation level to remain held during the recent observation window.
- Accepts only an early move of approximately `+0.5%` to `+3.0%` from the open,
  with total day change no higher than `+5.0%`.
- Requires price above live VWAP, pullback no more than `0.75%` from the day high,
  growing post-alert volume, current B/S at least `1.5x`, median B/S at least
  `1.3x`, minimum observed B/S at least `0.9x`, and spread no wider than `20` bps.
- Ranks all simultaneously eligible candidates using strength, stable buyer
  pressure, post-alert volume growth, pullback, extension, and spread.
- Simulates the highest-ranked candidates instead of buying whichever symbol
  crosses first.

Paper execution assumptions:

- Budget per trade: `Rs 50,000`.
- Maximum open paper positions: `2`; maximum entries per day: `5`.
- Initial experiment target: `+1.0%`; stop: `-0.75%`.
- Simulated slippage: `5` bps per side; estimated fees: `0.05%` per side.
- Failed-confirmation exit may close a trade that falls below entry and VWAP
  with sustained sell pressure after the minimum holding period.
- Paper circuit breakers stop new entries after `Rs 1,000` daily net loss or
  `2` consecutive paper losses.

Evidence files on the live runtime:

- `results/Data/ranked_confirmation_paper/observations_YYYY-MM-DD.csv`: replay-complete
  live tick tape including bid/ask, buy/sell quantities, VWAP, volume, price,
  open, previous close, and day high for candidates and open paper trades.
- `results/Data/ranked_confirmation_paper/candidates_YYYY-MM-DD.csv`: every
  observed Grade-B candidate, its final status/reason, and live context.
- `results/Data/ranked_confirmation_paper/trades_YYYY-MM-DD.csv`: simulated
  fills, target/stop, MFE/MAE, costs, and net P&L.
- `results/Data/ranked_confirmation_paper/events.csv`: append-only candidate,
  entry, and exit lifecycle events.
- Discord entry/exit messages are explicitly labelled `PAPER ONLY`; they do not
  correspond to broker orders.

Guardrail repair:

- `LiveTradingGuardrails` now falls back to reconstructing realized bot P&L
  from `autotrade_event_journal.csv` when the old portfolio trade CSV is absent.
- It reads only completed `order_result` rows, preventing duplicate counting of
  companion entry/protective-stop journal events.

Verification:

- Research repository focused suite: `63 passed`.
- Live `tr-assistant` focused deployment suite: `45 passed`.
- Scenarios cover sustained confirmation, late-mover rejection, unstable buyer
  pressure, concurrent-candidate ranking, cost-adjusted target exit, and
  consecutive-loss circuit blocking.

## Honest Backtest and Rs 10,000 Goal Audit 2026-07-11

### Revoked on 2026-07-12 after leakage audit

The historical selector described below is retained for forensic history but is
no longer accepted evidence.

- `enrich_intraday_liquidity_features.py` looked up liquidity at `entry_time`,
  while the simulated fill was the **open** of that five-minute entry bar. The
  model therefore saw that bar's final volume before placing the entry.
- Event enrichment also used the completed OR15 high for signals before 09:30.
- The previous `+0.5027%` average and naive `Rs 2,011/day` scaling are revoked.
- `SignalMonitor` now rejects every selector report lacking:
  `evidence_version >= 2`, `feature_availability=signal_close_or_earlier`, and
  `leakage_audit_passed=true`.
- Paper candidate recording now runs before the promotion gate, so evidence
  collection continues while Discord goal promotion and real-order logic remain
  blocked.

Corrected causal rebuild:

- Raw events: `195,826`, across `493` days.
- Signal-time liquidity uses only the signal bar and earlier cumulative value;
  projected day value uses the fixed 75-bar NSE schedule.
- Prior-day-only walk-forward scoring produced `163,712` candidates across the
  same `413` scored days.
- Best train+validation-selected compact slice: about `+0.0412%` full-period,
  but zero validation-period trades.
- After the former replacement/early-failure/runner exits, best full-period
  result was about `+0.0447%`; validation still had zero trades.
- Broader variants were negative in validation, as low as about `-0.2980%`.

Causal six-model grid:

- Tested unified and event-family specialist models with three labels:
  MFE-top, target-before-stop, and positive fixed-exit return.
- Each model retrained every five sessions from only the preceding 160 days.
- `select_causal_event_ranker_grid.py` selected configuration and threshold from
  train plus validation only; the final 104 days were read after selection.
- Selected model: event-family `target_before_stop`, threshold `0.40`.
- Train average: about `+0.0319%`; validation: `+0.0165%`; untouched final:
  `+0.0053%`; full average: `+0.0214%` across only 10 traded days.
- Replaying those trades through replacement, early-failure, and runner exits
  reduced the full average to about `+0.0052%` and made validation negative.
- The configuration is explicitly `accepted=false` and
  `real_orders_allowed=false` in
  `results/Data/causal_event_ranker_grid_v1_selected.json`.

Cross-sectional causal attempt:

- Added same-signal-time percentile features for move, volume, recent relative
  strength, signal traded value, VWAP distance, pullback, candidate count, and
  event-family count.
- Historical and live enrichment produce identical values in parity tests.
- Unified target model selected threshold `0.25` from train/validation, but the
  untouched final period was about `-0.0944%` average and full average was
  about `-0.0319%`.
- Event-family target model selected threshold `0.40`; train and validation were
  slightly positive, but untouched final was about `-0.0008%` and only 12 days
  traded.
- Cross-sectional ranks did not create a stable edge and are not promoted.
  They remain in the shadow recorder because they are causal and may become
  useful when exact live order-book outcomes accumulate.

Exit-contract and exhaustion-fade audit (2026-07-13):

- Added `scripts/select_causal_intraday_exit_grid.py` to replay every causal
  event through a target/stop surface with conservative stop-first handling,
  `0.15%` cost, and explicit train/validation/final separation.
- Replayed all `195,826` events against `36` long contracts. Every contract
  remained negative under the existing heuristic rank in train, validation,
  and final. The least-bad `+1.0% / -0.5%` contract averaged about `-0.218%`,
  `-0.237%`, and `-0.217%` respectively.
- Direct expected-return ranking, target classification, equal-day weighting,
  cross-sectional ranks, and 16 strictly prior-session trend/gap/compression/
  liquidity features did not repair the bullish entry family. Sparse results
  with one or a few trades are rejected as non-evidence.
- The bullish event family is therefore retired as a long-entry candidate. No
  further long threshold tuning should be done on this event definition.
- Testing the falsifiable opposite hypothesis initially made the same bullish
  events look like exhaustion/fade candidates. That result exposed a second
  causality bug: portfolio simulation selected the highest-scoring event from
  the entire day, including candidates that did not exist at the earlier entry
  time. The all-day-best short result is revoked.
- Exit-grid portfolio selection now defaults to `first_batch`: it can rank only
  candidates present in the earliest live signal batch. The walk-forward model
  now enters the first threshold-eligible batch instead of inspecting all later
  candidates and selecting the eventual daily maximum.
- Under corrected first-batch execution, short contracts weakened through the
  pre-final blocks and lost about `0.19%` to `0.25%` per day in final for the
  leading variants. The fade lane is not promoted.
- Re-auditing saved causal model predictions with first-eligible execution left
  the best broad result at about `+0.053%` per day: positive train and
  validation, but approximately flat/slightly negative final. Other models
  were negative or traded too rarely to matter.
- Added a separate EOD `fade_outcomes_YYYY-MM-DD.csv` evaluator using short
  `+2.0% / -0.5%`, conservative same-candle ordering, and costs. It is shadow
  evidence only: no Discord alert, no paper fill during the session, no broker
  order, and no connection to the existing long model trainer.
- Exit-grid metadata always writes `candidate_strategy_accepted=false` and
  `real_orders_allowed=false`.
- Selector evidence is bumped to version `3` and must certify candidate
  availability as `first_threshold_eligible_signal_batch` or
  `signal_time_or_earlier`. Every version-2 all-day-best selector is rejected
  by `SignalMonitor` even if an old CSV says it was accepted.
- Moved the heuristic event score into the shared `IntradayEventEngine`, so
  historical event generation and live shadow recording cannot drift. EOD
  outcomes persist exactly one `first_batch_selected=true` candidate and add a
  `__first_batch_portfolio__` summary row. This is the forward-executable
  candidate identity used for future validation.
- The automatic shadow trainer now reads only complete
  `first_batch_selected=true` rows, uses first-eligible execution, requires at
  least `100` forward sessions/events, and writes evidence version `3` with the
  candidate-time certificate. It may accept a model only for paper shadow
  scoring; `real_orders_allowed` remains false.

New evidence files:

- `results/Data/causal_intraday_exit_grid_v1_summary.csv`
- `results/Data/causal_intraday_exit_grid_v1_selected_outcomes.csv`
- `results/Data/causal_intraday_short_exit_grid_v2_summary.csv`
- `results/Data/causal_intraday_short_exit_grid_v2_selected_outcomes.csv`
- `results/Data/causal_intraday_short_exit_grid_v3_first_batch_summary.csv`
- `results/Data/*_first_eligible_selected.json`
- live after EOD:
  `results/Data/intraday_event_shadow/fade_outcomes_YYYY-MM-DD.csv`

## 2026-07-13 - Early Pre-Breakout Ignition Hypothesis

Hypothesis:

- Detect leaders before the full breakout, while price is only `+0.2%` to
  `+1.5%` from the open.
- Require completed-bar volume acceleration, positive relative strength,
  price above VWAP, a strong range position, limited pullback, and price within
  `0.35%` below to `0.10%` above the prior intraday high.
- Enter only at the next five-minute candle open and use causal first-batch
  portfolio selection.

Implementation:

- Added `scripts/backtest_prebreakout_ignition.py`.
- Added a NumPy candidate generator with parity against the shared pandas
  formulas and eight-worker offline execution.
- Found `11,456` candidates across `489` sessions and `498` symbols, so the
  detector had broad coverage rather than a sample-size problem.

Result:

- All 36 long target/stop contracts were negative in train, validation, all
  four pre-final blocks, and final. The selected long contract was about
  `-0.163%` train, `-0.142%` validation, and `-0.349%` final per market day.
- The symmetric short/fade surface also failed. Every selected short contract
  was negative in train and validation; isolated positive final variants were
  not eligible because selection did not use final.
- The hypothesis is rejected on both sides and is not connected to live
  shadow recording, Discord, paper orders, or broker orders.

Artifacts:

- `results/Data/prebreakout_ignition_candidates_v1.csv`
- `results/Data/prebreakout_ignition_exit_grid_v1_summary.csv`
- `results/Data/prebreakout_ignition_short_exit_grid_v1_summary.csv`

Decision:

- The old accepted selector is revoked and must not be frozen, alerted, or
  traded.
- No current causal historical model supports `Rs 10,000/day` or even the old
  `0.5%` average target.
- Continue exact live shadow and ranked Grade-B paper collection with broker
  orders disabled while developing a new causal selector.

## 2026-07-13 - Live Microstructure / Kite-Minute Replay

Question:

- Did stable live buyer depth plus broad sector participation identify a
  tradeable continuation setup in the July 6-10 live feature recordings?

Implementation:

- Added `scripts/evaluate_live_microstructure_tape.py`.
- Candidate features are read only from the recorded second-level snapshot:
  two-sided quote, spread, traded value, day move, pullback, persistent B/S
  ratio, and sector breadth.
- The first qualifying row per symbol/rule is frozen. Daily portfolio selection
  uses only the first available signal batch.
- A diagnostic tape mode buys the recorded ask and sells only into a later
  observed bid. It never substitutes last traded price for a missing quote.
- The decision-grade replay excludes the signal minute, enters at the next
  complete Kite one-minute candle open plus `5` bps adverse slippage, resolves
  ambiguous target/stop candles against the trade, and charges another `0.10%`
  in round-trip fees.
- Target/stop selection uses July 6-8 only. July 9-10 is exposed afterward as
  the fixed chronological holdout.

Data audit:

- Live input: `357,802` snapshots over `5` completed sessions.
- Stable-sector hypothesis: `63` causal candidates across `56` symbols.
- Read-only Kite backfill returned complete minute coverage for `49/56`
  symbols (`87.5%`). The seven incomplete symbols were not filled from another
  provider and did not receive synthetic zero outcomes.
- `60` candidates had complete post-signal Kite-minute outcomes.

Result:

- Training selected a `+1.5% / -0.5%` contract.
- Training average was about `+0.406%` per day across only three trades.
- Both fixed holdout days lost; holdout average was about `-0.536%` per day.
- The broad candidate average was about `-0.255%` per trade.
- Every tested broad candidate target/stop contract had negative average
  expectancy after delayed entry, slippage, and fees.

Decision:

- Reject stable order-book plus sector breadth as a standalone long entry.
- The earlier positive result from conditionally recorded future bid snapshots
  is diagnostic only; that recorder is not a continuous exit tape.
- Keep the evaluator and exact live recordings as research infrastructure.
- `EvidenceAccepted=false` and `RealOrdersAllowed=false` are written on every
  summary row. No Discord, paper-entry, or broker-order lane was added.

## 2026-07-13 - Two-Stage Leader State Machine

Hypothesis:

- Stop buying the initial burst.
- First create a leader watch from a completed five-minute bar.
- Enter long only after a real pullback followed by a completed-bar break over
  the prior peak.
- Enter short only after the watched leader fails, closes below VWAP, breaks a
  recent two-bar low, and shows expanding bearish volume.
- In both directions, fill only at the next bar open.

Implementation:

- Added `scripts/backtest_leader_state_machine.py`.
- Added separate causal candidate files for continuation longs and confirmed
  failed-breakout shorts.
- Extended `scripts/select_causal_intraday_exit_grid.py` with an optional
  benchmark calendar. Zero-candidate market days now count as cash/zero return
  instead of disappearing from the denominator.
- Fixed first-batch candidate selection, conservative same-bar stop ordering,
  `0.20%` friction, and train/validation/final separation remained mandatory.

Coverage:

- `6,990` delayed confirmations over `493` sessions and `498` symbols.
- `5,854` leader-continuation candidates.
- `1,136` confirmed failed-breakout short candidates.
- Every candidate matched its post-entry Kite candle history.

Continuation result:

- All leading long target/stop contracts were negative in train, validation,
  every pre-final block, and final.
- The selected `+0.5% / -0.5%` contract averaged about `-0.268%` in train,
  `-0.203%` in validation, and `-0.203%` in final per market day.
- Waiting for pullback plus a new peak did not repair the long continuation
  expectancy.

Failed-breakout result:

- A fixed `+3.0% / -1.5%` short contract was positive in the broad train,
  validation, and final summaries: approximately `+0.081%`, `+0.039%`, and
  `+0.092%` per day.
- It still failed robustness: one pre-final block averaged about `-0.112%`,
  only `62.5%` of pre-final months were positive, and another `0.10%` cost
  stress made pre-final expectancy negative.
- A Nifty non-positive/weakening gate did not survive final and was rejected.

ML attempt:

- Added `scripts/walk_forward_leader_state_ranker.py`.
- The expected-return model uses only watch/confirmation-time features, trains
  on the prior 160 market sessions, retrains every five sessions, and enters
  the first threshold-eligible signal batch.
- The selected threshold had about `+0.086%` train return but validation fell
  to about `-0.020%` and final to about `-0.175%` per day. Higher thresholds
  were also negative in validation/final.

Decision:

- Reject both state-machine directions for promotion.
- Keep the confirmed-failure generator as a research component because it was
  more informative than immediate fading, but do not paper-promote it yet.
- Do not add another price-only ranker to this candidate family. The next
  hypothesis must introduce information available before or independently of
  the price burst, such as point-in-time exchange catalysts.
- Real orders remain disabled.

## 2026-07-13 - Point-in-Time NSE Catalyst Research

Goal:

- Introduce information available before or independently of the price burst,
  instead of building another indicator score from the same price move.

Data layer:

- Added `pkscreener/classes/NseCatalystClassifier.py`.
- Added resumable `scripts/backfill_nse_catalysts.py`.
- Archived `105` non-overlapping seven-day NSE announcement chunks from
  `2024-05-30` through `2026-05-29` with no failed chunks.
- Raw exchange rows: `356,866`.
- Classified point-in-time catalysts inside the 505-symbol Kite universe:
  `6,671` across `636` dates and `500` symbols.
- Exchange `sort_date`/dissemination time is the event timestamp. Filing text,
  category, and timestamp are preserved; no later price is used to classify a
  catalyst.

Classifier:

- Concrete categories include order wins, capacity/commissioning, regulatory
  approvals, rating upgrades, buybacks, acquisitions, agreements/MOUs,
  financial results, dividends, and keyword-qualified material press releases.
- Routine compliance certificates, newspaper copies, investor-meeting notices,
  takeover disclosures, and vague updates are excluded.
- Unknown-direction events such as results or acquisitions rely on later tape
  direction; text classification never pretends to know whether results were
  good.

Intraday confirmation test:

- Added `scripts/backtest_nse_catalyst_confirmation.py`.
- The filing's partially overlapping five-minute candle is excluded.
- Confirmation uses only complete later bars; entry is the next bar open.
- Concrete-positive categories produced `37` strict causal candidates over
  `33` days after a modest, outcome-blind funnel relaxation.
- Including results/acquisitions/agreements produced `85` candidates over
  `74` days.
- Both variants were negative in train, validation, final, and the leading
  pre-final blocks after `0.20%` friction.

Immediate intraday test:

- Concrete-positive filings entered at the first untouched five-minute bar
  open after dissemination, before inspecting any post-filing candle.
- Coverage: `246` candidates over `185` days.
- Every leading long contract was negative. The selected long contract was
  about `-0.092%` train, `-0.044%` validation, and `-0.153%` final per market
  day.
- The fixed opposite/sell-the-news short surface was also negative in
  validation and final.

After-close / next-session test:

- After-close and premarket concrete-positive filings were mapped to the first
  tradable session.
- The first opening five-minute bar had to confirm controlled gap, positive
  body, near-high close, liquidity, and volume above the symbol's own prior
  20-session opening baseline; entry was the second bar open.
- Coverage: `54` candidates over `50` sessions.
- The selected long contract remained negative in train and validation.

Decision:

- Public NSE catalyst timing alone did not create a tradeable long or short
  edge under these causal contracts.
- Keep the archive/classifier/backtest for future richer parsing, but do not
  schedule catalyst alerts, paper entries, or broker orders from this work.
- No catalyst result supports the daily profit goal.

## 2026-07-13 - Ranked Paper Negative-Day Gate

Live finding:

- The ranked Grade-B paper lane entered `DMART` at about `Rs 4,003.30` while
  the stock was `+1.30%` from its gap-down open but still `-1.95%` versus the
  previous close.
- It stopped at about `Rs 3,970.71` for roughly `Rs -438.89` net paper P&L.
- This was a gap-down rebound, not a positive-day top-gainer continuation.

Fix:

- Added `RankedPaperConfig.min_day_change_pct`, default `+0.50%`.
- Added environment override
  `SIGNALMONITOR_RANKED_PAPER_MIN_DAY_CHANGE_PCT`.
- A candidate below the floor is rejected as
  `day_move_below_positive_floor`.
- Runtime commit `7e7665b` is deployed and pushed. The signal monitor, shared
  ticker, and Discord bot restarted successfully with fresh PIDs.
- Runtime flags remain:
  `KITE_AUTO_TRADE=0`, `ENABLE_GRADE_B_AUTOTRADE=0`, ranked paper enabled.

Verification:

- Ranked paper suite: `7 passed` in development and runtime.
- No broker order path was enabled or changed.

## 2026-07-13 - Ranked Paper EOD Safety and Daily Summary

Live audit:

- The paper executor closed open positions at its `15:10` EOD cutoff, then
  continued into the entry-ranking step in the same cycle.
- Freed paper slots could therefore admit a brand-new simulated trade seconds
  before market close. This did not place broker orders, but it corrupted the
  forward paper evidence.

Fix:

- Added a hard paper-entry cutoff at `14:45` IST, configurable with
  `SIGNALMONITOR_RANKED_PAPER_LATEST_ENTRY_MINUTES`.
- The EOD exit remains `15:10` and is now configurable with
  `SIGNALMONITOR_RANKED_PAPER_EOD_EXIT_MINUTES`.
- Observing candidates are marked `latest_entry_cutoff_reached`; they cannot
  reuse slots released by EOD exits.
- Added one durable `paper_daily_summary` event with trades, wins, losses,
  target/stop/failed-confirmation/EOD counts, and net paper P&L.
- The summary-sent flag is persisted in `state.json`, so a monitor restart does
  not send the Discord summary twice.

Verification:

- Ranked paper, tape replay, and SignalMonitor Discord suites: `57 passed`.
- New tests cover the late-entry block and exactly-once EOD summary after state
  reload.
- This code is paper-only. It does not import the broker order manager, and all
  real-order flags remain disabled.

## 2026-07-13 - Automated Live Evidence EOD Labels

Problem found:

- `scripts/evaluate_live_feature_outcomes.py` defaulted to
  `results/Data/kite_history_cache/5minute`, but that runtime directory was
  empty.
- Missing candle rows were written as zero MFE, zero MAE, and zero return. A
  post-market run could therefore make unavailable evidence look like flat
  trades.
- Raw recorder states repeat every monitor cycle, so evaluating every row also
  gave long-lived states disproportionate weight.

Repair:

- Missing candles are now `OutcomeAvailable=false` with unavailable returns;
  they are excluded from performance averages and exposed through
  `EvaluatedRows` and `CoveragePct`.
- Added first-state freezing by date, symbol, lane/stage/decision/reason (or
  feature bucket), while preserving every raw append-only recorder row.
- Added decision-grade one-minute evaluation: exclude the signal minute, enter
  at the next untouched minute open, apply `5` bps adverse slippage, charge
  `0.10%` fees, and resolve same-minute target/stop ambiguity against the trade.
- Added `scripts/run_live_evidence_eod.py` to collect only that day's recorded
  symbols, read-only backfill their Kite minute candles, enforce a default
  `95%` outcome-coverage gate, and write per-day feature/decision summaries.
- Added `scripts/live_evidence_eod.sh` with locking, idempotency, environment
  loading, durable logs, and one Discord completion/failure message.
- Added an idempotent cron installer for `15:35` IST Monday-Friday. The job is
  independent of the market-window service, so the assistant's later stop does
  not kill a long evidence backfill.

Verification:

- Evaluator, microstructure, and EOD orchestration suites: `18 passed`.
- A smoke run against the intentionally empty runtime minute cache produced
  `coverage_gate_failed`, `0/4,856` evaluated first-state rows, and `NaN`
  performance values rather than false zero returns.
- All generated status files hard-code `evidence_accepted=false`,
  `paper_promotion_allowed=false`, and `real_orders_allowed=false`.

Historical live-tape bootstrap:

- Added `scripts/bootstrap_live_evidence_range.py` for existing recorder dates.
- It unions the recorded symbols across a date range and requests the whole
  range once per symbol instead of once per symbol/day.
- Caches are saved after every symbol, so an interrupted run resumes without
  repeating complete downloads.
- Coverage is audited per symbol/date, while evaluation remains per date and
  combines only first-state, full-horizon, cost-adjusted outcomes.
- A no-API smoke replay used the earlier 49-symbol minute cache. It evaluated
  `227/5,448` eligible July 6-10 rows (`4.17%`), correctly failed the coverage
  gate, and did not promote isolated small buckets.

## 2026-07-13 - Sector Leader/Laggard Pair Research

Hypothesis:

- Remove broad market and sector direction by trading an equal-notional pair.
- At the fixed completed `10:00` bar, identify the strongest and weakest liquid
  stock inside each sector, then test both convergence and continued dispersion.
- Select at most one sector pair per day using the largest signal-time
  dispersion. No later candidate or future return participates in selection.

Implementation:

- Added `scripts/backtest_sector_dispersion_pairs.py` and focused unit tests.
- Entry is the `10:05` bar open, after the cross-section is known.
- A target or stop may be observed only on a completed five-minute close. The
  simulated exit is the following bar open, never that already-completed close.
- Both legs receive `5` bps adverse entry and exit slippage, plus `0.10%`
  additional round-trip fees. Zero-trade market sessions remain in the daily
  denominator.
- The short-leg return formula is normalized by short entry price. This was
  caught during the audit, fixed, covered by a unit test, and the entire replay
  was rerun before using any result.

Coverage:

- `185,610` valid symbol-days from the two-year five-minute cache.
- `6,756` same-time sector pairs across `493` sessions.
- One outcome-selected pair was available on almost every scored session;
  this was not a sparse-sample failure.

Result:

- Every cost-aware mean-reversion and momentum target/stop contract was
  negative in train, validation, final, and the leading pre-final blocks.
- The least-bad mean-reversion family was still roughly `-0.09%` train,
  `-0.18%` validation, and `-0.25%` final per market day.
- Fixed 60-minute mean reversion before all costs looked like `+0.104%` train,
  then decayed to `+0.004%` validation and `-0.022%` final. With modeled
  execution it became approximately `-0.095%`, `-0.193%`, and `-0.222%`.
- Momentum was also negative after costs in every temporal split.

Decision:

- Reject fixed-time same-sector convergence and momentum as standalone lanes.
- The gross relationship decayed out of sample and was smaller than executable
  friction; widening stops or increasing size cannot repair that.
- Every summary row writes `evidence_accepted=false` and
  `real_orders_allowed=false`. No alert, paper-entry, or broker-order path was
  added.

Artifacts:

- `results/Data/sector_dispersion_pairs_v1_candidates.csv`
- `results/Data/sector_dispersion_pairs_v1_outcomes.csv`
- `results/Data/sector_dispersion_pairs_v1_summary.csv`
- fixed-horizon cost and gross diagnostic variants with the same prefix.

## 2026-07-13 - Fixed-Time Whole-Universe Leader Research

Question:

- Can we distinguish future top gainers while they are only modestly positive,
  instead of waiting until the scanner notices them near the peak?

Causal design:

- Added `scripts/backtest_fixed_time_leader_rank.py`.
- Formed whole-universe cross-sections from completed bars at `09:30`, `09:45`,
  `10:00`, `10:30`, and `11:00`.
- Each symbol's cumulative-volume surprise uses only its previous 20 sessions.
- Ranking uses only same-batch move, prior-session volume surprise, current-bar
  acceleration, Nifty-relative strength, range position, turnover, pullback,
  VWAP distance, and sector breadth.
- Entry is the next bar open with `5` bps adverse slippage. Another `0.15%`
  covers exit slippage/fees. Zero-trade market sessions remain in the daily
  denominator.
- Tested top-1 and equal-weight top-3 portfolios over `30`, `60`, and `120`
  minute horizons with `36` target/stop contracts per signal time.

Coverage and fixed-rule result:

- `67,697` causal candidates across `482` sessions, `499` symbols, and `360`
  complete portfolio configurations.
- Every cost-aware configuration was negative in train, validation, and the
  leading pre-final blocks. None passed the research gate.
- The pre-final-selected fixed rule was the `10:30`, 60-minute,
  `+1.5% / -0.7%`, top-1 configuration. It averaged about `-0.151%` train,
  `-0.125%` validation, and `-0.195%` final per market day.
- Removing all modeled costs made that same rule only about `+0.025%` train,
  `+0.055%` validation, and `+0.013%` final, with one negative pre-final block.
  The raw relationship is too small and unstable to survive execution.

Walk-forward ML check:

- Added `scripts/walk_forward_fixed_time_leader_ranker.py`.
- Froze the `10:30`, 60-minute, `+1.5% / -0.7%` contract before modeling.
- The equal-day-weighted expected-return model trains only on the preceding
  160 sessions, retrains every five sessions, and ranks only the current fixed
  signal batch.
- It scored `11,589` rows from `13,222` candidates over `415` market sessions.
- The adequately sampled selected threshold had `91` train trades and averaged
  about `-0.077%` per train day; `38` validation trades averaged about
  `-0.070%`. Extra `0.10%` cost stress reduced validation to about `-0.106%`.
- Final happened to be slightly positive at about `+0.010%` across 29 trades,
  but it cannot rescue negative train, validation, and pre-final blocks.

Decision:

- Reject both the hand-ranked and ML-ranked fixed-time leader lanes.
- This directly shows that detection delay is not the only problem: the early
  historical features contain a small gross continuation effect, but not an
  executable edge after costs.
- Historical metadata hard-codes `evidence_accepted=false`,
  `paper_promotion_allowed=false`, and `real_orders_allowed=false`.
- No scanner alert, paper entry, or broker order was added.

Artifacts:

- `results/Data/fixed_time_leader_rank_v1_candidates.csv`
- `results/Data/fixed_time_leader_rank_v1_summary.csv`
- `results/Data/fixed_time_leader_rank_v1_metadata.json`
- `results/Data/fixed_time_leader_ranker_v1_*`
- `results/Data/fixed_time_leader_rank_v1_gross_audit_*`

Evidence hierarchy:

1. Historical OHLCV tests are labelled `historical_ohlcv_candle_proxy`. They
   can test price/volume structure but cannot validate live order-book stability,
   exact spread, or tick persistence.
2. Exact strategy evidence must come from
   `observations_YYYY-MM-DD.csv` and the production-identical replay engine.
3. Paper-forward results remain separate from historical proxy results. They
   must never be pooled to inflate sample size or win rate.

Historical proxy audited:

- Source: the accepted 413-day Nifty-500 walk-forward daily result with an
  embedded `0.15%` cost assumption.
- Audit capital: `Rs 4,00,000`; requested daily goal: `Rs 10,000` (`2.5%`).
- Full-period average: `0.5027%`, or about `Rs 2,011/day` when naively scaled.
- Median day: `0%`; losing days: `47.22%`; maximum drawdown: `4.31%`.
- `Rs 10,000` goal frequency: `43/413`, or `10.41%` of market days.
- Validation slice average: `0.2698%`, or about `Rs 1,079/day`;
  `Rs 10,000` frequency: `6.80%`.
- Final slice average: `0.6098%`, but median `-0.05%` and a `12`-day longest
  losing streak. This is not daily-income behavior.

Double-friction stress test:

- Added another `0.15%` per traded day on top of the embedded `0.15%` cost.
- Full-period average falls to `0.3865%`, about `Rs 1,546/day`.
- Validation average falls to `0.1591%`, about `Rs 636/day`.
- Validation `Rs 10,000` frequency falls to `5.83%`.
- Worst day becomes about `Rs -5,200` at `Rs 4,00,000` capital.

Decision:

- `Rs 10,000` may remain an occasional strong-day outcome, not a daily promise
  or an optimization target for the current capital.
- The historical proxy is explicitly blocked from promoting the live lane.
- Do not increase leverage, order count, or stop width to force the requested
  rupee target.

Outputs:

- `results/Data/honest_intraday_goal_audit_summary.csv`
- `results/Data/honest_intraday_goal_audit_daily.csv`
- `results/Data/honest_intraday_goal_audit_stress2x_summary.csv`
- `results/Data/honest_intraday_goal_audit_stress2x_daily.csv`

## Multi-Trade Scaling Audit 2026-07-12

Question tested:

- Can the validated edge be repeated 10 or 20 times to turn several small
  winners into `Rs 10,000/day` on `Rs 4,00,000` capital?

Method:

- Added `scripts/audit_intraday_multi_trade_scaling.py`.
- Used the same 413-day replayed candidate outcomes and the accepted
  `vwap_dist_pct >= 0.70` replacement rule.
- Removed duplicate event signals for the same stock and ranked the remaining
  symbols by the existing adjusted score.
- Split `Rs 4,00,000` equally across the requested slots. Unfilled slots stayed
  in cash. The audit did not reuse capital intraday because the historical
  artifact lacks exact exit timestamps.
- Existing `0.15%` trade cost remained embedded in each replay outcome.

Result:

- One slot: about `Rs 2,011/day`; `Rs 10,000` on `43/413` days (`10.41%`).
- Two slots: about `Rs 1,141/day`; `Rs 10,000` on `11/413` days (`2.66%`).
- Three slots: about `Rs 826/day`; `Rs 10,000` on `6/413` days (`1.45%`).
- Five slots: about `Rs 582/day`; `Rs 10,000` on `2/413` days (`0.48%`).
- Ten slots: about `Rs 300/day`; no `Rs 10,000` days.

Interpretation:

- The current ranker has a useful first choice, not ten equal-quality choices.
- Adding lower-ranked trades dilutes expected return instead of multiplying it.
- Increasing the order cap is therefore not a profit fix. A second or later
  trade needs its own validated ranking/entry model and exact capital-release
  timestamps before sequential reinvestment can be tested honestly.
- Real broker automation remains disabled.

Outputs:

- `results/Data/intraday_multi_trade_scaling_400k_goal10k_summary.csv`
- `results/Data/intraday_multi_trade_scaling_400k_goal10k_daily.csv`

## Accepted-Strategy Parity Foundation 2026-07-12

Problem found:

- The accepted 413-day selector is built from five 5-minute event families,
  while the newly enabled paper lane observes Grade-B anomaly alerts.
- Those are different strategies. Grade-B paper outcomes therefore cannot
  validate or invalidate the accepted OR15/VWAP event selector.

Implemented:

- Added `pkscreener/classes/IntradayEventEngine.py` as a shared causal feature
  and event detector for `or15_breakout`, `or15_retest_hold`,
  `prior_high_continuation`, `vwap_pullback_hold`, and `vwap_reclaim`.
- Updated `scripts/backtest_intraday_event_candidates.py` to call the shared
  engine, establishing one source of truth for future backtest/live shadow use.
- Added a no-future-leak test: changing a later bar cannot change features on
  an earlier bar.
- Verified every shared feature and all detected events against the previous
  implementation on a real 75-bar cached day (`BHARATFORG`, 2024-05-30).

Rejected experiment:

- A prior-day-only meta-ranker predicting realized return was tested for the
  later-ranked candidates.
- On identical model-eligible days, the existing rank averaged about `0.5825%`;
  the predefined meta-label model averaged about `0.4482%`.
- The meta-ranker was rejected and was not added to production.

Next engineering step:

- Train and freeze the live event-family scoring artifacts from historical data,
  then score the recorded events in shadow mode. Do not route it to broker orders.

Live shadow recorder implemented:

- Added `pkscreener/classes/IntradayEventShadowRecorder.py`.
- `KiteTickerStream` now includes the NIFTY 50 benchmark token (`256265` by
  default) so relative-strength features use the same completed five-minute
  timestamp as each stock.
- The recorder aggregates cumulative-volume snapshots into completed five-minute
  OHLCV bars, persists bars and raw events under
  `results/Data/intraday_event_shadow/`, and restores completed bars/events after
  a process restart.
- Event evaluation waits for completed benchmark bars. It never sends Discord
  alerts and has no import or call path to `KiteOrderManager`.
- `SignalMonitor` enables it with `SIGNALMONITOR_ENABLE_EVENT_SHADOW=1`; the live
  launcher prints `evidence only, no alerts/orders` at startup.
- At/after 15:30 IST, `IntradayEventShadowOutcomes` automatically evaluates each
  event from the next five-minute bar open through EOD. It records target before
  stop, conservative same-bar ambiguity, `0.15%` cost, MFE, MAE, EOD return, and
  session completeness.
- Outcome files are written as `outcomes_YYYY-MM-DD.csv` and
  `outcome_summary_YYYY-MM-DD.csv`; incomplete symbols are retained for audit but
  excluded from summary statistics.
- After each EOD label pass, `train_intraday_event_shadow_model.py` refreshes a
  chronological 60/20/20 train/validation/final readiness report automatically.
  It refuses training before 20 complete sessions and 100 events, selects a
  probability threshold on validation only, and never sets
  `real_orders_allowed=true` even when accepted for paper shadow scoring.

Exact replay command after live tape exists:

```bash
./.venv/bin/python scripts/replay_ranked_confirmation_tape.py \
  --input '/home/charan_sai/tr-assistant/results/Data/ranked_confirmation_paper/observations_*.csv' \
  --capital 400000 \
  --daily-goal 10000 \
  --output-dir results/Data/ranked_confirmation_replay/exact_v1
```

The replay refuses missing fields and returns a warning status when any tape day
does not reach the configured EOD exit time. No forward promotion decision should
be made before at least `20` complete market sessions and `50` closed paper trades,
with parameters frozen during collection.

## Known-Good Live Checkpoint

As of 2026-06-29, the live Grade-A/B anomaly goal mode plus the no-auto-sell safety patch is the preferred setup. Keep this as the rollback anchor if future tuning makes alerts worse.

- `tr-assistant`: `c93818c` (`Disable automatic sell execution in alert mode`)
- `intraday-trading`: `5912b2c` (`Disable automatic sell execution in alert mode`)
- Monitor interval: `3s`
- Goal alert cap: `50`
- Goal lane: Grade-A/B anomaly goal alerts; direct Buy Now disabled
- Current exits: stop `1.0%`, Target 1 `1.5%`, Target 2 `3.0%`
- Manual execution: enter only if price is still valid, place stop immediately, book most size at Target 1, keep only a small runner.
- Safety rule: live service must run alert/ticker/Discord only. Do not start `--assistantautopilot` or any auto-sell loop unless the user explicitly changes the system from manual trading to broker automation.

### Rollback Applied 2026-06-29

Reason:

- Recent split-service and alert/radar experiments coincided with Kite auto-login failing with `Invalid CAPTCHA values`.
- User asked to return to the documented known-good Grade-A/B anomaly checkpoint before market open.

Actions taken:

- Restored `intraday-trading` runtime code to the documented checkpoint and pushed rollback commit `5d2caaf` (`Rollback runtime to known-good intraday goal checkpoint`).
- Restored `tr-assistant` runtime code to the documented checkpoint and pushed rollback commit `65c4ada` (`Rollback runtime to known-good intraday goal checkpoint`).
- Superseded the earlier rollback anchor after real sell orders were placed by the restored autopilot path. The safe rollback anchor is now `5912b2c` in `intraday-trading` and `c93818c` in `tr-assistant`, which remove `--assistantautopilot` from the live launcher and hard-block the auto-sell monitor when `KITE_AUTO_TRADE` is off.
- Preserved the pre-rollback state in backup branches:
  - `intraday-trading`: `backup-before-f5c56d1-rollback-20260629`
  - `tr-assistant`: `backup-before-e7be99f-rollback-20260629`
- Stashed uncommitted local `tr-assistant` changes as:
  `stash@{0}: backup before e7be99f rollback 2026-06-29`
- Disabled the separate `intraday-goal.service`; the safe checkpoint uses `trade-assistant.service` as the combined owner for Kite ticker, signal monitor, and Discord bot. Autopilot/auto-sell must remain off for manual trading.

Pre-open readiness after rollback:

- Kite TOTP auto-login succeeded after rollback.
- Kite doctor status: fresh session for `2026-06-29`, profile OK, holdings API OK, positions API OK.
- `trade-assistant.service`: active.
- `trade-assistant-watchdog.timer`: active.
- `intraday-goal.service`: inactive/disabled intentionally.
- Live ticker status at about `2026-06-29 09:08 IST`: connected, `2950/2990` symbols subscribed/snapshotted, `40` missing symbols.
- Signal monitor latest summary at about `2026-06-29 09:08 IST`: scanning `2990`, fresh alerts `0` before market movement.
- Prewarm coverage file from morning: combined cache `2652/2906`, NSE `2131/2367`, BSE tradeable `824/916`.

Operational note:

- If alerts stop again, first check `trade-assistant.service`, Kite doctor, `kiteticker_all_status_latest.json`, and `signalmonitor_all_summary_latest.csv` in `/home/charan_sai/tr-assistant/results/Data`.
- Do not re-enable the separate `intraday-goal.service` unless we deliberately move back to split ownership.
- Do not roll back only to `5d2caaf` or `65c4ada`; those commits predate the no-auto-sell patch and can restart the old autopilot sell path.

### Controlled Automation Lane 2026-06-29

User goal:

- Stop manually watching every alert.
- Allow broker automation only if the logic is explicit and bounded.
- Do not revive the old autopilot path that previously sold manual holdings.

Implemented lane:

- Strategy name: `grade_b_confirmation`.
- Signal source: only `Grade-B` anomaly/runner alerts from SignalMonitor.
- Entry rule: wait for live price to trade `+0.25%` above the alert price.
- Target rule: place Target 1 at `+0.7%` from executed entry.
- Stop rule: place stop at `-2.5%` from executed entry.
- Entry window: `15` minutes from the alert.
- Chase guard: skip if live price is more than `0.4%` above the confirmation trigger.
- State file: `signalmonitor_autotrade_state.json` in the PKScreener user-data output dir.
- Launcher: `scripts/start_grade_b_confirmation_autotrade.sh`.

Safety implementation:

- Real broker orders require all of:
  `SIGNALMONITOR_AUTOTRADE_ENABLED=1`,
  `SIGNALMONITOR_AUTOTRADE_REAL_ORDERS=1`,
  `KITE_AUTO_TRADE=1`,
  `KITE_AUTO_BUY=1`,
  `KITE_AUTO_SELL=1`.
- Positions created by this lane are marked `bot_owned=True` and `auto_exit_allowed=True`.
- `KiteOrderManager` now blocks non-manual sell orders for positions that are not marked `auto_exit_allowed`.
- `check_sell_signals()` skips manual/old tracked positions so auto-sell monitoring cannot sell them.
- The old `--assistantautopilot` path remains separate and should not be used for this lane.

Evidence behind the first automation rule:

- Stored alert-history audit had `165` completed alerts.
- `Grade-B` anomaly alerts with `+0.25%` confirmation and `+0.7%` target gave the best available small-sample result.
- With a `-2.5%` stop, the Grade-B subset was `17/17`; with `-2.0%`, it was `16/17`.
- This is a small sample, not proof of guaranteed profits. The automation lane starts capped so live results can validate or reject it.

### Autotrade Audit Journal 2026-07-02

Problem:

- Autotrade information was scattered across Kite order logs, tracked-position JSON, candidate state, Discord messages, and broker state.
- This made it hard to answer simple questions later, such as why a stock was skipped, why only one position was open, whether Kite rejected an order, or whether a stop was placed.

Implemented:

- Added an append-only autotrade decision journal in both `intraday-trading` and live `tr-assistant`.
- Live CSV: `/home/charan_sai/tr-assistant/results/Data/autotrade_event_journal.csv`.
- Live JSONL: `/home/charan_sai/tr-assistant/results/Data/autotrade_event_journal.jsonl`.
- Events recorded:
  candidate registered, candidate expired, entry skipped, entry armed without order, entry attempted, entry result, Kite order result, protective stop placed/failed/filled/inactive/cancelled, and position reconciled closed/adopted.

How to inspect quickly:

```bash
tail -n 50 /home/charan_sai/tr-assistant/results/Data/autotrade_event_journal.csv
tail -n 50 /home/charan_sai/tr-assistant/results/Data/autotrade_event_journal.jsonl
```

Operational note:

- This journal starts from the deployment time; it will not reconstruct old decisions retroactively.
- Keep Discord alert journals and Kite order logs too, but use this autotrade journal as the first source of truth for broker-automation decisions.

### Open-Position Cap Adjustment 2026-07-02

Observation:

- `DJML` registered as a Grade-B anomaly at `10:51:15 IST` and hit confirmation at `10:58:42 IST`.
- The buy was skipped because `TVSSRICHAK` was already open and the live automation cap was `KITE_INTRADAY_MAX_OPEN_POSITIONS=1`.
- Journal evidence:
  `entry_result,DJML,BUY,...,SKIPPED,...,Max intraday positions reached (1)`.

Change:

- Raised the live `grade_b_confirmation` automation cap from `1` to `2` simultaneous bot-owned MIS positions.
- Kept per-order cap at `50000`, target at `+0.7%`, stop at `-2.5%`, and daily loss guardrail at `3000`.
- Raised daily buy cap separately from `5` to `10` on 2026-07-03; max simultaneous open bot positions remains `2`.

Reason:

- A single open-position cap is too restrictive when two independent Grade-B confirmations appear close together.
- Two open positions still keeps approximate planned stop risk around `Rs 2,000` before slippage, inside the `Rs 3,000` daily guardrail.

### Stop Adjustment 2026-07-03

Observation:

- `DJML` nearly exited around the `-2.0%` stop area, but the order cancelled and the position later exited for a small profit.
- This matches the earlier small-sample audit where Grade-B confirmation performed slightly better with a `-2.5%` stop than with a `-2.0%` stop.

Change:

- Raised the controlled Grade-B confirmation autotrade stop from `-2.0%` to `-2.5%` for new buys.
- Target remains `+0.7%`, per-order cap remains `50000`, max open positions remains `2`, daily buy cap remains `10`, and daily loss guardrail remains `3000`.

Risk note:

- A wider stop can reduce premature exits, but it increases planned loss per failed `50000` trade from about `Rs 1,000` to about `Rs 1,250` before slippage.

### Pullback Recovery Audit 2026-06-29

Question tested:

- After an alert fires, how often does the stock fall to `-2%` from the alert price and then recover to the original alert Target 1?

Method:

- Input: completed buy/watch alerts from `2026-06-04` through `2026-06-25`.
- Excluded: sell/exit alerts and the still-open `2026-06-29` session.
- Price source: Kite 1-minute historical candles from alert time to market close.
- Definition: `-2%` means post-alert low <= `alert_price * 0.98`; recovery means a later candle high >= the alert's Target 1.
- Output files:
  - `results/Data/intraday_goal_alert_outcomes/alert_minus2_then_t1_audit_kite_1minute.csv`
  - `results/Data/intraday_goal_alert_outcomes/alert_minus2_then_t1_summary_kite_1minute.csv`

Result:

- Total evaluated alerts: `165`.
- Alerts that dropped to `-2%` after alert: `70/165` (`42.4%`).
- Alerts that dropped to `-2%` and later recovered to Target 1: `8/70` (`11.4%` of pullbacks, `4.8%` of all alerts).
- Alerts that hit Target 1 at any point after alert: `61/165` (`37.0%`).

By lane:

- Grade-A Runner: `18/33` dropped to `-2%`; `2/18` recovered to Target 1.
- Grade-B Anomaly: `9/28` dropped to `-2%`; `1/9` recovered to Target 1.
- Top Gainer Radar: `14/21` dropped to `-2%`; `2/14` recovered to Target 1.
- Top Gainer Radar Watch: `20/55` dropped to `-2%`; `3/20` recovered to Target 1.
- Engine Confirmed Watch: `3/15` dropped to `-2%`; `0/3` recovered to Target 1.
- Ignition Watch: `4/10` dropped to `-2%`; `0/4` recovered to Target 1.

Decision:

- Blindly waiting for `-2%` below alert and buying there is not validated by the available alert history. It catches many falling stocks, but only a small minority recover to Target 1 intraday.
- If we use pullback entries, they need a second confirmation trigger, such as reclaim of VWAP/open, fresh high-volume candle after the pullback, or buyer/seller pressure improving again.

Observed live trading model:

- Practical order size is usually Rs 45,000 to Rs 50,000 per trade.
- Daily P&L comes from multiple trades, often 4 to 6 attempts, not from deploying the entire account into one position.
- This changes the compounding math: account growth depends on number of quality trades, capital turnover, liquidity, and strict stop discipline.
- Do not assume a fixed 1% daily return on total account capital unless actual realized P&L supports it across many days.

Current reality as discussed:

- If realized net P&L averages roughly Rs 1,000 to Rs 2,000 per day after losses, charges, and taxes, the system is in a good early position.
- At 20 trading days, that is roughly Rs 20,000 to Rs 40,000 per month, depending on execution and losing days.
- If ticket size stays near Rs 45,000 to Rs 50,000 and only a few alerts are tradable each day, month 2 and month 3 may remain similar instead of compounding strongly.
- This is useful as an early monthly-income model, but not yet a crores-style compounding model.
- True compounding requires safely increasing ticket size, deployable daily turnover, or number of high-quality alerts without lowering alert quality.
- The next scaling path should be gradual, for example Rs 50,000 tickets to Rs 60,000, Rs 75,000, and then Rs 1,00,000 only after several live weeks show stable net results.

Compounding example:

- Day 1 starts with Rs 2,00,000.
- A 1% day ends near Rs 2,02,000.
- The next trading day uses the updated capital base, so another 1% day earns about Rs 2,020, not Rs 2,000.
- This compounding is powerful only if the average return is positive and drawdowns stay controlled.

## Current Findings

The market has enough intraday movers, but the current selector is still the weak link.

### ML/ranking dataset foundation

Started: 2026-06-15.

Purpose: build a learning layer that ranks alerts and candidates by what usually happens after the signal, instead of treating every top-gainer/radar/anomaly message as equally buyable.

Added:

- `scripts/build_intraday_ml_dataset.py`
- `scripts/intraday_ml_dataset_eod.sh`

Live output location:

- `/home/charan_sai/tr-assistant/results/Data/intraday_ml_dataset/intraday_ml_candidates_labeled.csv`
- `/home/charan_sai/tr-assistant/results/Data/intraday_ml_dataset/intraday_ml_candidates_daily_summary.csv`
- `/home/charan_sai/tr-assistant/results/Data/intraday_ml_dataset/intraday_ml_feature_readme.md`
- Log: `/home/charan_sai/tr-assistant/results/Data/intraday_ml_dataset/eod_cron.log`

Automation:

- Cron runs the ML dataset builder Monday-Friday at `11:20 UTC`, which is `16:50 IST`.
- This is after market close and after the live scanner has had time to write 1-minute candle archives.
- The job is read-only against alerts/candles and only writes ML dataset outputs, so it should not affect live alerts.

What the dataset labels:

- Final Discord goal/radar alerts.
- Top-gainer radar audit candidates, including rejected candidates.
- Anomaly audit candidates, including watch/reject candidates.
- Post-signal outcomes from local 1-minute ticker candles.
- Whether each candidate hit `+0.5%`, `+1.0%`, `+1.5%`, `+2.0%`, or `+3.0%` before a `-1.0%` stop.
- MFE/MAE and end-of-day return after the signal.

First useful live dataset run:

- Date: 2026-06-15.
- Raw observations: about 398k.
- Market-time sampled/labeled rows: 10,716.
- Candle-backed rows: 10,716.
- Overall `+1.5% before -1%` rate: about 10.0%.
- Final top-gainer radar alerts: 4 rows, 0 hit `+1.5%` before stop/end in this run.
- Anomaly-A watch rows: 7 rows, about 28.6% hit `+1.5%`, but sample is too small to trust alone.

Interpretation:

- This confirms why buying every top-gainer/radar candidate is dangerous.
- The next useful step is a ranking model or scoring report that learns which candidates have better odds of hitting Target 1 before stop.
- This is not a guaranteed buy engine. It is a probability/ranking layer to reduce bad entries and improve alert selection after enough labeled data accumulates.

### Alert-history backtest

The old anomaly outcome enrichment used full-day high/low. That was wrong for intraday alerts because a high or low before the alert could be counted as if it happened after entry.

Fix completed:

- `scripts/anomaly_outcomes.py` now computes post-alert forward outcomes from 5-minute bars when available.
- Daily-goal backtest now defaults to post-alert intraday outcomes and quality-ranked selection.

Best current alert-history result:

- Sample: 14 true forward-tested days.
- Daily goal: 0.5%.
- Goal hit rate: 50.0%.
- Average daily return: about +0.176%.
- Total P&L on Rs 2,00,000: about +Rs 4,930.

This is better than the original 1/15 result, but not enough evidence for real-money confidence.

### After-09:20 breakout opportunity test

We built a separate test for the user hypothesis: many stocks move slowly after 09:20 and can be caught before a 3% move.

Tool:

- `scripts/backtest_intraday_breakout_capture.py`

Result over available 2-year 5-minute data:

- Candidate trades found: 34,400.
- Trading days: 493.
- Trades that hit 3% target: about 5.94%.
- Positive trades: about 36.77%.

Simple live-like selector result:

- 0.5% daily goal hit: 25.35%.
- 1.0% daily goal hit: 9.53%.
- 2.0% daily goal hit: 0.41%.
- 3.0% daily goal hit: 0%.
- Total P&L: negative.

Oracle/hindsight diagnostic:

- 0.5% daily goal hit: 96.15%.
- 1.0% daily goal hit: 92.90%.
- 2.0% daily goal hit: 75.66%.
- 3.0% daily goal hit: 0%.

Interpretation: the opportunity pool exists, but the current live-usable selector cannot yet pick the right stocks consistently.

## Completed Code Changes

### Guardrails and live wiring

- Added daily profit target support to `pkscreener/classes/RiskGuardrails.py`.
- Wired daily target through:
  - `pkscreener/classes/SignalMonitor.py`
  - `pkscreener/classes/AssistantLiveRuntime.py`
  - `pkscreener/classes/AutoPilotInvestor.py`
  - `pkscreener/classes/DiscordTradeBot.py`
  - `pkscreener/pkscreenercli.py`
  - `scripts/start_live_assistant.sh`
  - `scripts/start_live_assistant.ps1`

### Daily-goal mode safety

- Goal-mode buy alerts are blocked unless a validated profitable selector exists.
- `SignalMonitor` can validate accepted tuner outputs or ranked-forward profitable backtest summaries.
- Default live goal exits now match the better current alert-history backtest:
  - Stop: 1%.
  - Target 1: 3%.
  - Target 2: 5%.

### Backtesting and research tools

Added:

- `scripts/backtest_daily_profit_goal.py`
- `scripts/tune_daily_profit_tool.py`
- `scripts/full_tune_daily_profit_tool.py`
- `scripts/oracle_gap_daily_goal.py`
- `scripts/backtest_intraday_breakout_capture.py`
- `scripts/tune_intraday_breakout_selector.py`
- `scripts/train_intraday_breakout_ranker.py`
- `scripts/walk_forward_intraday_breakout_ranker.py`

### Tests

Added or updated:

- `tests/test_risk_guardrails.py`
- `tests/test_signal_monitor_discord_message.py`

Latest focused verification:

- `21 passed`.

## Important Research Results

Fresh 2-year Kite research previously found no accepted strategy under stricter validation.

Best research strategy was still negative:

- Average daily return: about -0.229%.
- Profit factor: about 0.819.
- Rejected for negative train/final/walk-forward performance and unstable risk metrics.

Fresh daily-goal mapping from research candidates:

- Best 0.5% goal hit rate: about 11.28%.
- Average daily return: negative.

Oracle gap from fresh research candidates:

- 0.5% goal with Rs 50,000 tickets: about 48.07% hindsight hit rate.
- 1.0% goal: 0% hindsight hit rate in that candidate set.

## Current Blocker

The current system finds many candidates, but cannot yet rank them well enough before entry.

Simple features tried:

- Time of day.
- Move from open.
- Volume ratio.
- VWAP distance.
- Breakout over prior high.
- Candle body.
- Range position.
- Recent 3-bar return.
- NIFTY return from open.
- NIFTY VWAP distance.
- NIFTY recent 3-bar return.
- Relative strength versus NIFTY from open.
- Relative strength versus NIFTY over the recent 3-bar window.

Static and walk-forward ML rankers improved some ranking metrics but did not produce broad positive held-out daily P&L.

After adding NIFTY/relative-strength features, the high-confidence positive-label walk-forward ranker produced a small positive slice:

- Scored days: 413.
- Best threshold: 0.60.
- Traded days: 10.
- Goal hit rate on traded days: 20.0%.
- Positive day rate on traded days: 70.0%.
- Average daily return on traded days: about +0.2394%.
- Total P&L: about +Rs 4,788.

Interpretation: NIFTY context helps identify a small number of better days, but it does not yet provide enough trade coverage for a daily income system.

After adding sector/peer relative-strength features, the high-confidence positive-label walk-forward ranker produced:

- Scored days: 413.
- Best threshold: 0.60.
- Traded days: 14.
- Goal hit rate on traded days: 21.43%.
- Positive day rate on traded days: 42.86%.
- Average daily return on traded days: about +0.0429%.
- Total P&L: about +Rs 1,202.

Interpretation: sector context increased trade coverage slightly, but did not improve the edge enough. It is useful diagnostic information, but not a validated live selector.

After adding opening-range quality features, an initial result looked very strong, but that result was rejected because 30/60-minute opening-range values were being exposed before those ranges had completed. This was a future-data leak.

Leakage fix completed:

- 15-minute opening range is only available from 09:30 onward.
- 30-minute opening range is only available from 09:45 onward.
- 60-minute opening range is only available from 10:15 onward.
- Added explicit `or15_complete`, `or30_complete`, and `or60_complete` flags.

Corrected opening-range walk-forward result:

- Positive-label, high-confidence threshold 0.60.
- Scored days: 413.
- Traded days: 10.
- Goal hit rate on traded days: 30.0%.
- Positive day rate on traded days: 60.0%.
- Average daily return on traded days: about +0.1138%.
- Total P&L: about +Rs 2,275.

Interpretation: opening-range features help slightly, but still do not provide enough coverage or consistency for the daily Rs 1,000 to Rs 2,000 goal.

After adding structural risk fields and optional dynamic stops:

- Candidate generator now records `low_so_far`, `prior_high`, `structural_support`, `structural_stop_reason`, and `actual_stop_pct`.
- Dynamic stop mode chooses a bounded stop from nearest known support such as VWAP, prior high, opening-range high, or day low.
- It rejects trades where structural support is too far away.

Full dynamic-stop candidate result:

- Candidate trades: 32,719.
- 0.5% simple-selector goal hit: 22.31%.
- Simple-selector average daily return: about -0.1674%.
- Oracle 0.5% goal hit: 93.71%.
- Oracle 1.0% goal hit: 86.82%.

Walk-forward dynamic-stop result:

- Target-label best high-confidence slice: only 2 traded days, about +Rs 775 total.
- Positive-label best high-confidence slice: 4 traded days, about +Rs 1,583 total.

Interpretation: dynamic stops reduce some loss severity, but they currently cut too much opportunity and do not beat the corrected fixed-stop high-confidence slice. Keep the structural risk fields as model inputs, but do not treat dynamic stops as validated for live trading yet.

This means the next step is not simply tuning target/stop. The selector needs stronger market-context features.

## Next Technical Steps

Add stronger pre-entry features and retest:

1. NIFTY trend confirmation. Completed initial version.
   - NIFTY return from open at signal time.
   - NIFTY VWAP distance.
   - NIFTY recent 3-bar return.
   - Avoid long breakouts when index context is weak.

2. Sector trend confirmation. Completed initial version.
   - Sector/industry breadth if mapping exists.
   - At minimum, peer basket relative strength for same broad sector.

3. Opening range breakout quality. Completed initial version.
   - Breakout above first 15/30/60-minute high.
   - Distance from opening range high.
   - Avoid entries already too extended.

4. Relative strength versus index. Completed initial version.
   - Stock return minus NIFTY return at signal time.
   - Stock recent return minus NIFTY recent return.

5. Risk filters. Initial version completed.
   - Avoid candidates with too-large intraday range already used.
   - Avoid late-day entries unless momentum is strong.
   - Prefer candidates where stop distance is supported by structure, not just fixed 1%.

6. Next.
   - Tune target/stop/holding logic by regime instead of one universal 3% target and 1% stop.
   - Add profit-taking variants: partial at 0.75% to 1%, trail the rest toward 3%.
   - Validate with walk-forward daily portfolio P&L.

6. Walk-forward validation.
   - Train only on prior history.
   - Score today using only known-at-entry features.
   - Optimize for daily portfolio P&L and goal hit rate, not just trade classification accuracy.

## How To Continue Next Time

Start by reading this document, then inspect the latest outputs:

- `results/Data/intraday_breakout_capture_3pct_after_920_summary.csv`
- `results/Data/intraday_breakout_capture_3pct_after_920_oracle_summary.csv`
- `results/Data/intraday_breakout_ranker_walk_forward_target_goal05_summary.csv`
- `results/Data/intraday_breakout_ranker_walk_forward_positive_goal05_summary.csv`
- `results/Data/daily_profit_goal_anomaly_ranked_forward_summary.csv`

Then continue from the next technical step: test profit-taking variants. The fixed 3% target captures big movers, but many candidates may give 0.75% to 1.5% and then fade. Test partial/earlier exits before assuming every winning stock must reach 3%.

## Profit-Taking Variant Test

Added `--exit-mode partial` to `scripts/backtest_intraday_breakout_capture.py`.

Supported fields:

- `exit_mode`
- `partial_target_pct`
- `partial_fraction`
- `move_stop_to_breakeven`

Tested full universe with:

- 50% exit at +1.0%.
- Remaining quantity still targets +3.0%.
- Original stop retained.

Full partial-exit candidate result:

- Candidate trades: 34,153.
- Simple selector 0.5% goal hit: 18.05%.
- Simple selector average daily return: about -0.1979%.
- Oracle 0.5% goal hit: 96.75%.
- Oracle 1.0% goal hit: 88.64%.

Walk-forward partial-exit result:

- Positive-label best high-confidence slice: 20 traded days, about -Rs 113 total.
- Target-label best slice: negative.

Interpretation: partial profit-taking did not solve the issue. It reduced the max achievable daily upside and still depended on the same weak selector. The better path is not exit tweaking alone; the selector still needs stronger pre-entry edge.

Next recommended step:

- Inspect the best oracle trades versus the selected losing trades by feature distributions.
- Identify which feature differences separate winners from losers.
- Add explicit filters only when those differences persist across time splits.

## Feature Gap And Filter Test

Added:

- `scripts/analyze_intraday_feature_edges.py`
- `scripts/tune_intraday_feature_filters.py`
- `scripts/tune_intraday_exit_grid.py`

Feature-gap analysis compared:

- Oracle picks: best realized candidates per day.
- Selected losers: walk-forward-ranked candidates that lost money.

Notable descriptive gaps:

- Oracle winners tended to occur earlier in the day.
- Oracle winners had stronger candle body and relative strength.
- Oracle winners were less dependent on broad sector moves than the losing selected trades.
- Selected losers often looked like obvious opening-range/sector breakouts, but still failed after entry.

Then a train/test filter tuner searched simple one- and two-rule filters from these gaps.

Result:

- Input: 34,400 candidates across 493 days.
- Train days: 330.
- Test days: 163.
- Rule options searched: 64, up to 2 combined rules.
- No train-positive filters survived.

Interpretation: the feature gaps are not strong enough as simple rules. Do not add hand filters just because a feature looks different in hindsight. The next improvement probably needs better labels, better candidate generation, or additional real-time data such as order book / delivery / liquidity changes.

## Better Label Test

Added `oracle_top` label support to:

- `scripts/train_intraday_breakout_ranker.py`
- `scripts/walk_forward_intraday_breakout_ranker.py`

Instead of labeling only whether a trade hit the full 3% target, `oracle_top` labels the best few historical candidates per day, subject to a minimum return. This better matches the daily basket objective.

Best tested variant:

- Label: top 4 candidates per day with return >= 0.5%.
- Walk-forward scored days: 413.
- Best threshold: 0.30.
- Traded days: 14.
- Goal hit rate: 42.86%.
- Positive day rate: 50.0%.
- Average daily return on traded days: about +0.1599%.
- Total P&L: about +Rs 4,477.

Other label variants:

- Top 2 with return >= 0.5%: negative.
- Top 4 with return >= 0.25%: negative.
- Top 6 with return >= 0.25%: negative.

Interpretation: better labeling helps more than hand filters, but coverage is still too low. It is promising as a research direction, not yet a validated live selector.

## Exit Grid Runner

Added `scripts/tune_intraday_exit_grid.py`.

Purpose:

- Reuse the existing breakout backtester as the source of truth.
- Sweep target, stop, signal-time, move-window, volume, partial-exit, and dynamic-stop settings.
- Save one combined summary file for comparing daily-goal results across exit regimes.
- Support `--jobs` so independent backtests can run in parallel when extra CPU is available.

Smoke verification completed:

- `python -m py_compile scripts/tune_intraday_exit_grid.py`
- Limited-symbol single-run smoke completed and wrote summary files.
- Limited-symbol two-run parallel smoke completed with `--jobs 2`.

Early smoke result was still negative on the tiny limited-symbol sample, so it is only a correctness check, not a strategy result.

Recommended full run:

```bash
.venv/bin/python scripts/tune_intraday_exit_grid.py \
  --jobs 4 \
  --out-dir results/Data/intraday_exit_grid_full
```

Interpretation rule: accept an exit regime only if the live-like daily P&L improves out of sample. A better oracle result alone still does not prove we can trade it.

## Fast Exit Resimulation

Added `scripts/tune_intraday_exit_from_candidates.py`.

Why:

- The first exit-grid runner was correct but too slow for broad sweeps because it rediscovered entry candidates for every exit setting.
- The faster runner loads the already-generated candidate file once, loads each symbol's 5-minute bars once, then replays exits from the same entry points.

Focused single-exit test:

- Input candidates: `results/Data/intraday_breakout_capture_3pct_after_920_trades.csv`
- Tested latest signal cutoffs: 10:30, 11:30, 14:30.
- Tested targets: 1%, 1.5%, 2%, 3%.
- Tested stops: 0.6%, 0.8%.
- Best live-like average daily return: about -0.136%.
- Best setting by average return: latest signal 11:30, target 2%, stop 0.6%.
- 0.5% goal hit rate for that best setting: about 20.49%.
- Total P&L on Rs 2,00,000 daily capital over 493 days: about -Rs 1,34,085.

Focused partial-exit test:

- Tested 50% partial exit at 0.75% or 1.0%.
- Remaining quantity targeted 3%.
- Tested stops: 0.6%, 0.8%.
- Tested move stop to breakeven on/off.
- Best partial-exit average daily return: about -0.1515%.
- Best partial 0.5% goal hit rate: about 12.58%.

Interpretation:

- Exit tuning alone does not fix the system.
- Earlier profit-taking and tighter stops reduce some damage, but the selected basket is still negative.
- The next best work is entry/candidate generation and ranking, not more target/stop tuning.

Next recommended step:

- Build a stricter "quality candidate" generator before ML ranking.
- Compare candidate pools, not just exit rules:
  - first clean breakout after 09:20,
  - above VWAP,
  - above completed 15/30-minute opening range,
  - relative strength versus NIFTY positive,
  - avoid very extended range-used candidates,
  - require candle close strength and volume expansion.
- Then rerun oracle and walk-forward ranker on that smaller, cleaner candidate pool.

## Quality Candidate Pool Test

Added `scripts/evaluate_intraday_quality_pools.py`.

Why this matters:

- Many stocks do move 3% intraday, but the live system only gets paid if the stock moves after our detectable entry.
- In the current after-09:20 candidate file, only about 5.94% of candidate entries hit the 3% target after entry.
- About 36.77% of candidate entries finish positive after costs.
- The oracle result stays strong, which means opportunity exists, but the live score/ranker cannot separate the real continuation trades from similar-looking fades.

Quality pools tested:

- Early signals only until 11:30.
- Positive VWAP distance.
- Positive relative strength versus NIFTY.
- Positive candle body.
- Range not already exhausted.
- Clean 15-minute and 30-minute opening-range breakouts.
- Sector-confirmed moves.
- Combined OR + relative-strength pools.

Best raw score-based pool:

- Pool: early signals until 11:30.
- Score-based 0.5% goal hit: about 29.61%.
- Score-based average daily return: about -0.1812%.
- Oracle 0.5% goal hit: about 95.05%.

Most interesting train/test ML pool:

- Pool: clean 15-minute opening-range breakout + positive relative strength.
- Label: top 4 historical candidates per day with return >= 0.5%.
- High-confidence test slice:
  - Test days traded: 59.
  - 0.5% goal hit: about 27.12%.
  - Average daily return: about +0.0272%.
  - Total test P&L: about +Rs 3,212.
- Wider test slice:
  - Test days traded: 160.
  - 0.5% goal hit: about 34.38%.
  - Average daily return: about -0.0121%.
  - Total test P&L: about -Rs 3,879.

Interpretation:

- Cleaner candidate pools help a little, but not enough yet.
- The strongest clue is that OR15 + relative strength is closer to breakeven than the broad universe.
- The next improvement should focus on better candidate generation around first clean breakouts, not just filtering the old first-signal-per-stock candidates.
- Specifically, generate multiple event candidates per stock/day, such as first VWAP reclaim, first OR15 breakout retest, first high-volume continuation candle, and first pullback-hold after relative strength appears.

## Event Candidate Generation

Added `scripts/backtest_intraday_event_candidates.py`.

Why:

- The previous candidate generator created one broad "stock is up with volume" signal per stock/day.
- That helped prove opportunity exists, but it mixed many different setup types together.
- The event generator creates named, live-detectable setup families:
  - `or15_breakout`
  - `vwap_reclaim`
  - `prior_high_continuation`
  - `or15_retest_hold`
  - `vwap_pullback_hold`

Full 2-year event-candidate result:

- Event candidates: 68,179.
- Trading days: 493.
- Symbols: 178.
- Best raw score-based event family: `vwap_pullback_hold`.
- `vwap_pullback_hold` score-based 0.5% goal hit: about 30.63%.
- `vwap_pullback_hold` score-based average daily return: about -0.1268%.
- `vwap_pullback_hold` oracle 0.5% goal hit: about 95.23%.
- Combined all-event oracle 0.5% goal hit: about 96.71%.

Comparison versus old broad candidate generator:

- Old broad score-based average daily return: about -0.1968%.
- Best event-family score-based average daily return: about -0.1268%.
- This is an improvement, but still not profitable.

Cheap train/test ML checks:

- Combined event ranker with `oracle_top` label overfit train and stayed negative on test.
- Adding event-type feature flags improved test AUC slightly, but test daily P&L remained negative.
- Single-event rankers were closer to breakeven:
  - `vwap_pullback_hold`, high-confidence test slice: about -0.0105% average daily return over 117 test days.
  - `or15_breakout`, high-confidence test slice: about +0.0065% average daily return over 56 test days.
  - `vwap_reclaim`, high-confidence test slice: about +0.2125% average daily return, but only 2 test days, so not meaningful.

Are we improving or moving in circles?

- We are improving diagnostically and narrowing the bottleneck.
- We are not yet improving enough to trade live.
- The research moved from:
  - bad alert outcome measurement,
  - to fresh 2-year opportunity testing,
  - to proving oracle opportunity exists,
  - to ruling out exit tuning as the main fix,
  - to finding that event-specific candidate generation reduces losses.
- The next real improvement must come from better event-specific ranking and additional pre-entry confirmation, not from more generic filters.

Next recommended step:

- Build an event-specific ranker, not one combined ranker.
- Rank only within each setup family.
- Add event-specific features:
  - reclaim distance and reclaim volume for VWAP events,
  - retest depth and bounce strength for pullback-hold events,
  - breakout candle close position for OR events,
  - time since opening range completed,
  - whether NIFTY is also above VWAP or breaking its own short-term high.
- Then combine only the best event-family candidates into the daily portfolio.

## Event-Family Specialist Ranker

Added `scripts/train_intraday_event_family_ranker.py`.

Why:

- The combined event ranker mixed different setup types together.
- A VWAP pullback, an OR15 breakout, and a prior-high continuation do not fail for the same reasons.
- This script trains a separate model for each `event_type`, then combines the scored held-out candidates into one daily basket.

First specialist result on event candidates:

- Input: `results/Data/intraday_event_candidates_full_trades.csv`
- Train/test split: first 330 days train, final 163 days test.
- Best held-out setting:
  - Score threshold: 0.60.
  - Max per event: 1.
  - Test days traded: 140.
  - Average trades per day: 1.51.
  - 0.5% goal hit: about 28.57%.
  - Positive day rate: about 40.71%.
  - Average daily return: about +0.0338%.
  - Total test P&L: about +Rs 9,466.

Position-size check:

- Rs 20,000 and Rs 10,000 tickets reduced per-trade risk but did not improve the daily goal result.
- The Rs 50,000 ticket slice still had the best total held-out P&L among these quick checks.

Interpretation:

- This is the best broad-ish held-out result so far.
- It is still far below the Rs 1,000 to Rs 2,000 daily target.
- The improvement is real, but it is small: about Rs 68 per traded day on average in that held-out slice.

## Event Feature Enrichment

Added `scripts/enrich_intraday_event_features.py`.

Added pre-entry signal-candle and event-quality features:

- `signal_bar_range_pct`
- `signal_close_position`
- `signal_body_to_range`
- `signal_upper_wick_pct`
- `signal_lower_wick_pct`
- `pullback_from_high_pct`
- `distance_to_prior_high_pct`
- `distance_to_or15_high_pct`
- `distance_to_vwap_pct`
- `vwap_reclaim_strength_pct`
- `vwap_retest_depth_pct`
- `or15_retest_depth_pct`
- `minutes_since_or15_complete`

Enriched specialist result:

- Input: `results/Data/intraday_event_candidates_full_enriched_trades.csv`
- Best meaningful held-out setting:
  - Score threshold: 0.65.
  - Max per event: 3.
  - Test days traded: 58.
  - Average trades per day: 1.26.
  - 0.5% goal hit: about 32.76%.
  - Positive day rate: about 43.10%.
  - Average daily return: about +0.0807%.
  - Total test P&L: about +Rs 9,357.

Interpretation:

- Enriched features improved quality per traded day, but reduced coverage.
- The best enriched slice averages about Rs 161 per traded day, still below the daily-income goal.
- The next bottleneck is coverage: we need more high-confidence candidates per day without letting the loss rate explode.

Next recommended step:

- Build a walk-forward version of the event-family specialist ranker.
- Only if walk-forward remains positive, tune coverage:
  - allow 1-3 candidates per event family,
  - add market-day filters,
  - skip weak index regimes,
  - test lower ticket sizing only after edge is positive.

## Walk-Forward Event-Family Ranker

Added `scripts/walk_forward_intraday_event_family_ranker.py`.

Why:

- The train/test specialist result was encouraging, but one fixed split can overfit.
- The walk-forward version trains event-family models only on prior days, scores the next day, and then builds the daily basket from those scored candidates.

Enriched walk-forward result:

- Input: `results/Data/intraday_event_candidates_full_enriched_trades.csv`
- Lookback: 160 days.
- Minimum train days: 80.
- Scored days: 413.
- Best selective slice:
  - Score threshold: 0.55.
  - Traded days: 12.
  - Average daily return: about +0.2205%.
  - Total P&L: about +Rs 5,291.
  - Too few days to rely on.
- Best practical coverage slice:
  - Score threshold: 0.40.
  - Max per event: 1.
  - Traded days: 118.
  - Average trades per day: 1.14.
  - 0.5% goal hit: about 20.34%.
  - Positive day rate: about 41.53%.
  - Average daily return: about +0.0350%.
  - Total P&L: about +Rs 8,270.

Plain non-enriched walk-forward result:

- Input: `results/Data/intraday_event_candidates_full_trades.csv`
- Best average daily return was still negative, about -0.0386%.

Interpretation:

- Event-specific enriched features survive walk-forward better than plain event features.
- The edge is small and selective, not a daily-income edge yet.
- We now have a validated direction: event-specific entries plus event-quality features.
- The next bottleneck is still coverage and market-regime filtering.

## Compounded Return Tracking

Added `scripts/analyze_compounded_daily_returns.py`.

Why:

- The goal is percentage compounding from an initial capital base, not a fixed rupee target.
- Daily summaries should report the capital curve, return bands, and drawdown.
- Strong upside days should not be hidden or capped in reporting.

Return bands tracked:

- Loss days: `< 0%`.
- Small positive days: `0% to 0.5%`.
- Base target days: `0.5% to 1%`.
- Strong days: `1% to 3%`.
- Very strong days: `3% to 10%`.
- Extreme days: `10%+`.

Compounding check on latest practical walk-forward slice:

- Input: `results/Data/intraday_event_family_ranker_walk_forward_enriched_threshold040_max1_daily.csv`
- Initial capital: Rs 2,00,000.
- Traded days: 118.
- Ending compounded capital: about Rs 2,08,238.
- Total compounded return: about +4.12%.
- Compound average per traded day: about +0.0342%.
- 0.5%+ days: about 20.34%.
- 1%+ days: 0%.
- 3%+ days: 0%.
- Max drawdown on traded-day curve: about -3.10%.

Interpretation:

- This confirms the current edge is positive but still far below the 0.5% to 1% daily compounding goal.
- The current strategy does not yet preserve 3%+ or 10%+ portfolio days because the selected trades rarely create those days.
- The next work should increase 0.5%+ day frequency first, then preserve upside.

Next recommended step:

- Analyze the 118 traded walk-forward days versus skipped days.
- Identify market regimes where the specialist ranker works:
  - NIFTY trend up/down,
  - opening range direction,
  - broad market breadth,
  - volatility/range day,
  - time-of-day distribution.
- Add a day-level regime gate before taking event-family trades.

## Regime Gate Attempt

Added:

- `scripts/tune_intraday_event_regime_gates.py`
- `scripts/validate_intraday_event_regime_gates.py`

Why:

- The walk-forward specialist ranker is positive but nowhere near the 0.5% to 1% daily compounding target.
- The next fix attempt is to trade only on days where the top ranked setup has healthier regime/quality context.

Diagnostic two-way gate result:

- Base input: enriched event-family walk-forward scored trades.
- Base setting: score threshold 0.40, max one candidate per event.
- Best late-test diagnostic gate:
  - `top_range_pct_so_far <= 3`
  - `top_vwap_dist_pct <= 1`
  - Test days: 21.
  - Average daily return: about +0.2390%.
  - 0.5%+ hit rate: about 38.10%.
  - Compounded capital from Rs 2,00,000 over those traded days: about Rs 2,10,248.
- This is better, but it was selected by test performance, so it is diagnostic only.

Stricter three-way validation:

- Split: train 206 scored dates, validation 103, final 104.
- Rules must be positive in train and validation before final reporting.
- Best validation-selected final curve:
  - Final traded days: 13.
  - Average final daily return: about +0.1225%.
  - Compound average final daily return: about +0.1218%.
  - 0.5%+ final day hit rate: about 23.08%.
  - Ending capital from Rs 2,00,000: about Rs 2,03,191.

Interpretation:

- Regime gates improve selectivity, but they still do not fix the gap to 0.5% to 1% average daily compounding.
- The strategy is now finding small pockets of positive edge, but not enough trades and not enough high-return days.
- The current blocker is not just "avoid bad days"; it is also "find more true continuation candidates on good days."

Next recommended fix attempt:

- Expand the event generator to allow multiple staged entries per stock/day:
  - first breakout,
  - first pullback hold,
  - second continuation after pullback,
  - late-morning continuation if market regime stays strong.
- Add trailing/runner exits so strong trades can contribute more than the fixed target/stop result.
- Keep regime gates as a safety layer, but do not rely on them alone.

## Staged Event Entry Test

Updated `scripts/backtest_intraday_event_candidates.py`.

Added:

- `--max-events-per-type-per-day`
- `--min-event-gap-bars`
- `--progress-every`
- `event_stage` feature for later rankers.

Purpose:

- Test whether allowing later continuation entries after the first event per stock/day increases coverage and helps the compounding target.

Smoke/full-subset result:

- Tested 60-symbol staged subset with up to 2 events per setup family and at least 4 bars between same-family events.
- Staged candidates increased the opportunity pool, but also added noise.
- Simple score stayed negative.

Train/test specialist result on staged 60-symbol enriched data:

- Best fixed-split setting:
  - Score threshold: 0.60.
  - Max per event: 2.
  - Test days: 92.
  - Average daily return: about +0.0476%.
  - 0.5% goal hit: about 22.83%.

Walk-forward specialist result on staged 60-symbol enriched data:

- Best setting:
  - Score threshold: 0.50.
  - Max per event: 2.
  - Traded days: 107.
  - Average daily return: about +0.0007%.
  - 0.5% goal hit: about 13.08%.
  - Total P&L: about +Rs 154.

Interpretation:

- Staged entries do not currently fix the gap.
- The extra staged candidates add too much noise in walk-forward validation.
- Do not run the full-universe staged test unless the staged logic is improved first.
- The next better fix attempt is exits that preserve upside on genuinely strong trades, or stronger real-time features such as breadth/order-flow/liquidity confirmation.

## Runner Exit And Forward-Upside Label Test

Added:

- `scripts/enrich_intraday_forward_excursions.py`
- `scripts/tune_intraday_runner_exits.py`
- `--label-return-col` support in:
  - `scripts/train_intraday_event_family_ranker.py`
  - `scripts/walk_forward_intraday_event_family_ranker.py`

Why:

- The user's thesis depends on finding stocks that continue after entry and may reach 1%, 3%, 5%, or more.
- The old labels were based on the existing fixed exit return, which can teach the model the current exit rule instead of true upside potential.
- Forward MFE/MAE lets us label candidates by post-entry upside directly.

Forward-upside opportunity over the current local 179-symbol 5-minute universe:

- Candidates: 68,179 across 493 days.
- Median post-entry max favorable excursion: about 0.78%.
- 75th percentile MFE: about 1.58%.
- 90th percentile MFE: about 2.78%.
- 95th percentile MFE: about 3.85%.
- Max MFE: about 17.91%.

Interpretation:

- The opportunity exists even in the small current universe.
- The hard part remains identifying the top tail before the move, not proving that movers exist.

MFE-3 walk-forward label:

- Label: top candidates with at least 3% post-entry MFE.
- Best broad-ish result was still not acceptable.
- High-confidence slices traded too few days.

MFE-1 walk-forward label:

- Label: top candidates with at least 1% post-entry MFE.
- Best broad-ish result:
  - Score threshold: 0.55.
  - Max per event: 3.
  - Traded days: 148.
  - Average daily return: about +0.0062%.
  - 0.5% goal hit: about 21.62%.
- This is slightly positive but still far below the 0.5% to 1% compounding goal.

Runner/partial exit sweep on MFE-1 scores:

- Tested partial exits, runner targets, trailing stops, and breakeven moves.
- Best focused runner setup was still slightly negative.
- Conclusion: exits are not the main blocker yet. The selector still needs to find better candidates.

Validated regime gates on MFE-1 scores:

- Best validation-selected final result:
  - Final days: 34.
  - Average final daily return: about +0.0473%.
  - 0.5% goal hit: about 26.47%.
- A diagnostic final rule reached about +0.1630% average daily on 20 final days, but it was not the validation-selected rule and should not be treated as accepted.

Interpretation:

- MFE labels and regime gates are directionally useful, but still nowhere near the user's goal.
- Current validated result remains far from 0.5% to 1% average daily.
- The next meaningful lever is universe expansion and better live-valid features, not more exit tweaking.

## Universe Expansion Attempt

Important finding:

- The current two-year 5-minute backtest universe is only about 179 files, not thousands of stocks.
- This is a major mismatch with the user's original assumption that the selector can choose from thousands of intraday stocks.

Code fix:

- `scripts/research_intraday_strategies_2y.py` now accepts case-insensitive `symbol` column names.
- This allows the backfill tools to read `results/Indices/NSE_BSE_COMBINED.csv`, which uses `Symbol`.

Dry run and expansion attempt:

- Tested top 500 symbols from `results/Indices/NSE_BSE_COMBINED.csv`.
- All 501 requested symbols including NIFTY mapped to Kite instruments.
- Existing complete 5-minute coverage was only 167/500 stocks.
- Real backfill attempt failed for 333 symbols with:
  - `Incorrect api_key or access_token.`

Code fix:

- `scripts/kite_historical_backfill.py` now validates the Kite session with `client.profile()`.
- Before this fix, the tool considered the session available if a local token file existed.
- Now it fails early when the saved token is rejected.

Current blocker:

- A fresh valid Kite access token is required before expanding the two-year 5-minute universe beyond the current local cache.
- Without that, backtests remain limited to the small 179-symbol local universe.

Next recommended step:

- Refresh Kite login/access token.
- Re-run the top-500 or top-1000 5-minute backfill.
- Re-run event generation, forward MFE enrichment, MFE-label walk-forward ranker, and regime validation on the larger complete universe.

## Expanded N500 Universe Result

Kite access was refreshed and validated with the stricter `client.profile()` check.

Backfill result:

- Universe source: `results/Indices/NSE_BSE_COMBINED.csv`
- Requested top symbols: 500 plus NIFTY.
- 5-minute period: 2024-05-30 to 2026-05-29.
- Complete-ish stocks: 435/500.
- NIFTY complete: yes.
- Quality gate: passed.

Expanded event generation:

- Output: `results/Data/intraday_event_candidates_n500_trades.csv`
- Candidates: 195,826.
- Days: 493.
- Symbols: 505.
- Simple score remained negative, but the oracle was very strong:
  - Oracle 0.5% daily goal hit: about 99.80%.
  - Oracle average daily return: about +0.7113%.

Expanded forward-upside distribution:

- Output: `results/Data/intraday_event_candidates_n500_enriched_forward_trades.csv`
- Median post-entry MFE: about 0.87%.
- 75th percentile MFE: about 1.78%.
- 90th percentile MFE: about 3.11%.
- 95th percentile MFE: about 4.27%.
- Max MFE: about 19.71%.

Interpretation:

- Expanding the universe helped the opportunity pool.
- The user's idea that many stocks move enough intraday is supported by the data.
- The remaining challenge is ranking and sizing, not opportunity existence.

Walk-forward MFE-1 ranker on expanded universe:

- Output: `results/Data/intraday_event_family_ranker_walk_forward_n500_mfe1_scored_trades.csv`
- Scored rows: 163,712.
- Scored days: 413.
- With Rs 50,000 tickets, the best broad result was still below target:
  - Score threshold: 0.40.
  - Traded days: 44.
  - Average traded-day return: about +0.2222%.
- High-confidence slices were better but too sparse.

Position sizing test:

- Added: `scripts/analyze_intraday_ranker_sizing.py`
- Reason: prior tests used a fixed Rs 50,000 ticket, so one high-confidence trade only deployed 25% of the Rs 2,00,000 capital.
- Larger ticket sizing materially changes portfolio-level daily percentage return.

Best full-period sizing result over all 413 scored days:

- Ticket: Rs 2,00,000.
- Score threshold: 0.35.
- Traded days: 99/413.
- Traded-day average return: about +0.6432%.
- Calendar/scored-day average return counting no-trade days as 0%: about +0.1542%.
- Worst traded day: about -1.15%.

Interpretation:

- Full-period traded days can now exceed the 0.5% target.
- Full-period calendar average still does not meet the 0.5% to 1% daily objective because coverage is too low.

Best recent 104-day sizing result:

- Input: expanded N500 MFE-1 walk-forward scores.
- Ticket: Rs 2,00,000.
- Score threshold: 0.35.
- Max per event: 1.
- Traded days: 46/104.
- Idle days counted as 0%.
- Average calendar daily return: about +0.5780%.
- Compound average daily return: about +0.5688%.
- Starting capital: Rs 2,00,000.
- Ending capital: about Rs 3,60,737.
- Total return: about +80.37%.
- Max drawdown: about -2.35%.
- 0.5%+ day hit rate across all 104 days: about 27.88%.

Important caution:

- The recent 104-day result reaches the base 0.5% daily compounding target.
- The full 413 scored-day result still does not.
- Therefore this is a recent-regime success, not a fully validated two-year live edge yet.

Next recommended step:

- Add a regime detector that decides when the recent N500 setup is active.
- Validate that regime detector without using the final period to choose the rule.
- Only after that should the live tool allow this sizing profile automatically.

## Discord Alert Safety Status

Added:

- `scripts/build_intraday_goal_selector_report.py`

Purpose:

- Convert the expanded N500 sizing research into a single live-eligibility CSV.
- Make the Discord goal-mode decision explicit instead of relying on memory or optimism.

Current report:

- Output: `results/Data/intraday_goal_selector_n500_mfe1_validation.csv`
- Selector: `n500_mfe1_walk_forward_sizing`
- Accepted: `False`
- Live Discord goal alerts allowed: `False`
- Reason:
  - Recent 104-day result passes the base 0.5% daily objective.
  - Full 413 scored-day result fails the base 0.5% daily objective.

Implication:

- Do not enable automatic goal-mode Discord buy alerts yet.
- It is acceptable to use the recent result as a research watchlist idea, but not as a fully validated live alert engine.
- SignalMonitor should continue to block daily-goal buy promotion unless a selector report has `accepted=True` or the user deliberately overrides with `SIGNALMONITOR_GOAL_SELECTOR_VALIDATED=1` for a controlled paper-only experiment.

Next research step:

- Build a live-valid regime detector that can identify when the recent N500 behavior is active.
- The detector must be based only on information available before/during the trading day, not final-period returns.
- Only if that detector passes train/validation/final should `accepted=True` be generated for Discord goal-mode alerts.

## Regime Detector Attempt

Added:

- `scripts/walk_forward_intraday_regime_model.py`

Purpose:

- Learn a day-level activation gate from past day-level candidate context.
- Inputs are same-day candidate/ranker context and prior-day training only.
- The model tries to decide whether the N500 MFE-1 selector should be active.

Best result so far:

- Base selector: N500 MFE-1 walk-forward score, min score 0.35, Rs 2,00,000 ticket.
- Best regime-model full-period calendar average: about +0.1765%.
- Best active-day average: about +1.3471%.
- Active days: 41.
- 0.5%+ hit on active days: about 63.41%.
- Worst active day: about -1.15%.

Interpretation:

- The regime model finds high-quality active days.
- It still does not raise full-period calendar average to 0.5%.
- Therefore it is not enough to enable automatic Discord goal-mode buys.

Current Discord answer:

- I am not confident enough to send automatic goal-mode Discord buy alerts from this selector yet.
- The correct behavior is to keep Discord goal-mode alerts blocked until a future selector report has `accepted=True`.

## Sector Context Attempt

Added:

- `scripts/enrich_intraday_sector_features.py`

Purpose:

- Test whether sector/peer confirmation helps the N500 event ranker catch the slower 1% to 3% movers more reliably.
- Add sector return, sector breadth, peer count, and stock-vs-sector relative strength at the signal bar.

Result:

- Input rows: 195,826 expanded N500 forward-enriched candidates.
- Sector-confirmed candidates: about 30.76%.
- The sector-enriched ranker did not improve the target result.

Best sector-enriched sizing result over all 413 scored days:

- Ticket: Rs 2,00,000.
- Score threshold: 0.30.
- Traded days: 203/413.
- Average calendar daily return: about +0.0754%.
- Average traded-day return: about +0.1534%.

Best sector-enriched recent 104-day result:

- Ticket: Rs 2,00,000.
- Score threshold: 0.25.
- Traded days: 93/104.
- Average calendar daily return: about +0.4716%.

Interpretation:

- Sector context helped describe the market, but it did not improve the edge.
- The previous non-sector N500 MFE-1 result remains better, especially in the recent 104-day period where it reached about +0.5780% calendar average daily return.
- Do not use the sector-enriched ranker for live Discord goal-mode buy alerts.

Next recommended step:

- Move away from broad static context like sector confirmation.
- Focus on live-compatible microstructure and confirmation features: fresh volume bursts, spread/order-book quality if available, price continuation after alert, and early failure exits.

## Confirmation Entry Attempt

Added:

- `scripts/analyze_intraday_confirmation_entries.py`

Purpose:

- Test a live-compatible idea before changing Discord alerts:
  wait briefly after a ranker signal and only enter if price confirms continuation.

Quick check:

- Input: expanded N500 MFE-1 walk-forward scored trades.
- Capital/ticket: Rs 2,00,000.
- Score floor: 0.35.
- Tested original entry vs one-bar continuation confirmation.
- Calendar days: 413.

Result:

- Original-entry eod-style quick check: about +0.0084% calendar average daily return.
- One-bar confirmation variants were negative in this eod-style check.

Interpretation:

- Simple delayed continuation confirmation is not enough by itself.
- This quick test does not replace the earlier non-sector MFE-1 sizing result, because it uses eod-style returns and does not yet model proper target/runner exits.
- The next useful code step is to speed up runner/target exit replay so we can test 3%, 5%, 8%, and 12% upside capture without very slow sweeps.

## Runner Exit Replay Update

Updated:

- `scripts/tune_intraday_runner_exits.py`

Purpose:

- Make target/runner exit sweeps faster by precomputing each trade's future high/low/close path once.
- Report calendar-day results with no-trade days counted as 0%, which matches the daily compounding goal better than traded-day-only averages.

Focused N500 MFE-1 runner test:

- Input: expanded N500 MFE-1 walk-forward scored trades.
- Capital/ticket: Rs 2,00,000.
- Best focused setup:
  - Min score: 0.35.
  - Stop: 0.8%.
  - First target: 1.0%.
  - First fraction: 25%.
  - Runner target: 8% or 12%.
  - Trail: 1.0%.
- Full 413-day result:
  - Traded days: 99/413.
  - Idle days: about 76.03%.
  - Calendar average daily return: about +0.1119%.
  - 0.5%+ calendar-day hit rate: about 12.35%.
  - Worst day: about -0.95%.
  - Best day: about +4.303%.
- Recent 104-day result for the same best daily file:
  - Traded days: 46/104.
  - Calendar average daily return: about +0.3912%.
  - 0.5%+ calendar-day hit rate: about 27.88%.
  - Worst day: about -0.95%.
  - Best day: about +4.303%.

Interpretation:

- Runner exits helped control downside and allowed days above 3%.
- They did not beat the earlier recent-period eod/MFE sizing result, which was about +0.5780% average calendar daily return.
- The current bottleneck is still selection/coverage, not only exit handling.
- Automatic Discord goal-mode alerts should remain blocked.

Next recommended step:

- Build a faster vectorized/compiled runner sweep or narrow the candidate universe before exit replay.
- More importantly, improve selection coverage: find more valid days without admitting low-quality trades.

## Liquidity Capacity / Crore Scaling Attempt

Added:

- `scripts/analyze_intraday_liquidity_capacity.py`

Purpose:

- Estimate how much capital the strategy can realistically deploy as the account grows.
- Cap each trade by actual intraday traded value available up to the entry bar.
- Test larger capital levels without pretending every signal can absorb unlimited money.

Focused setup:

- Input: expanded N500 MFE-1 walk-forward scored trades.
- Score threshold: 0.35.
- Participation cap: 1% of traded value available up to entry.
- Max per event: 1.
- Capital levels tested: Rs 2L, Rs 10L, Rs 50L, Rs 1Cr, Rs 5Cr.

Liquidity distribution for the 168 high-score trades:

- Median traded value by entry: about Rs 17.02Cr.
- 75th percentile traded value by entry: about Rs 50.61Cr.
- 90th percentile traded value by entry: about Rs 121.16Cr.
- Median full-day traded value: about Rs 76.50Cr.

Focused capacity result, using max position 100% and 1% participation cap:

- Rs 2L capital:
  - Full 413-day average: about +0.1441%.
  - Recent 104-day average: about +0.5111%.
  - Recent traded days: 46/104.
  - Recent average capital deployed: about 42.45%.
- Rs 10L capital:
  - Full 413-day average: about +0.1160%.
  - Recent 104-day average: about +0.3616%.
  - Recent average capital deployed: about 35.04%.
- Rs 50L capital:
  - Full 413-day average: about +0.0455%.
  - Recent 104-day average: about +0.1825%.
  - Recent average capital deployed: about 26.46%.
- Rs 1Cr capital:
  - Full 413-day average: about +0.0399%.
  - Recent 104-day average: about +0.1689%.
  - Recent average capital deployed: about 20.28%.
- Rs 5Cr capital:
  - Full 413-day average: about +0.0095%.
  - Recent 104-day average: about +0.0503%.
  - Recent average capital deployed: about 5.74%.

Interpretation:

- The current signal set can still work around the Rs 2L scale in the recent period under a 1% liquidity participation rule.
- As capital grows, the same number of high-score trades cannot deploy enough money, so percentage returns fall sharply.
- This answers the crore question: crores are possible only with more liquid signals and more parallel positions, not by simply increasing one trade size forever.
- The strategy needs a capacity-aware selector before any live compounding claim is realistic.
- Automatic Discord goal-mode alerts should remain blocked.

Next recommended step:

- Improve selection coverage specifically in liquid names.
- Add a live alert sizing rule based on intraday traded value:
  max trade size should be capped by participation percentage, account max-position percentage, and an absolute per-stock cap.

## Liquidity-Aware Ranker Attempt

Added:

- `scripts/enrich_intraday_liquidity_features.py`

Updated:

- `scripts/train_intraday_breakout_ranker.py`
- `scripts/walk_forward_intraday_event_family_ranker.py`

Purpose:

- Give the ranker live-compatible liquidity/capacity features instead of only measuring capacity after scoring.
- Added features use information available by entry:
  - bars to entry,
  - entry-bar traded value,
  - traded value accumulated up to entry,
  - estimated full-day value from bars seen so far,
  - entry-bar share of value so far.
- Added progress logging to the walk-forward ranker so long research jobs are visible.

Liquidity enrichment:

- Input rows: 195,826 expanded N500 forward candidates.
- Median traded value by entry across candidates: about Rs 14.60Cr.
- 75th percentile traded value by entry: about Rs 39.32Cr.
- 90th percentile traded value by entry: about Rs 91.89Cr.

Liquidity-aware walk-forward ranker:

- Input: `results/Data/intraday_event_candidates_n500_enriched_forward_liquidity_trades.csv`
- Output: `results/Data/intraday_event_family_ranker_walk_forward_n500_mfe1_liquidity_scored_trades.csv`
- Scored rows: 163,712.
- Scored days: 413.

Best sizing result, full 413 scored days:

- Ticket: Rs 2,00,000.
- Score threshold: 0.20.
- Max per event: 1.
- Traded days: 405/413.
- Calendar average daily return: about +0.3243%.
- Traded-day average return: about +0.3307%.
- Worst day: about -1.15%.

Best sizing result, recent 104 days:

- Ticket: Rs 2,00,000.
- Score threshold: 0.20.
- Max per event: 1.
- Traded days: 104/104.
- Calendar average daily return: about +0.9207%.
- Worst day: about -1.15%.
- Total PnL on Rs 2L fixed capital: about Rs 1,91,500.

Capacity check at 1% participation, score threshold 0.20:

- Rs 2L capital:
  - Full 413-day average: about +0.3527%.
  - Recent 104-day average: about +0.9650%.
- Rs 10L capital:
  - Full 413-day average: about +0.3275%.
  - Recent 104-day average: about +0.8798%.
- Rs 50L capital:
  - Full 413-day average: about +0.2467%.
  - Recent 104-day average: about +0.7127%.
- Rs 1Cr capital:
  - Full 413-day average: about +0.1880%.
  - Recent 104-day average: about +0.5018%.
- Rs 5Cr capital:
  - Full 413-day average: about +0.0722%.
  - Recent 104-day average: about +0.1286%.

Validation:

- Output: `results/Data/intraday_goal_selector_n500_mfe1_liquidity_s020_validation.csv`
- Accepted: `False`.
- Live Discord goal alerts allowed: `False`.
- Reason: recent 104-day period passes, but full 413-day period is still below the 0.5% daily goal.

Interpretation:

- This is a real improvement, not a circle:
  - Previous recent 104-day result was about +0.5780%.
  - Liquidity-aware recent 104-day result is about +0.9207%.
  - Full-period result improved to about +0.3243%, but still does not pass.
- The strategy is now much closer and more scalable up to about Rs 1Cr in the recent regime.
- The remaining problem is regime detection or older-period adaptation, not simply adding more stocks.
- Automatic Discord goal-mode alerts should remain blocked until the full-period selector passes.

Next recommended step:

- Build a liquidity-aware regime detector using the new scored trades.
- Goal: keep the strong recent behavior while filtering older weak regimes without using future information.

## Liquidity-Aware Regime Detector Attempt

Updated:

- `scripts/walk_forward_intraday_regime_model.py`

Purpose:

- Add progress output and optional daily-feature caching for long regime-model runs.
- Test whether a live-valid day-level activation model can keep the strong recent liquidity-aware behavior while filtering weak older periods.

Setup:

- Input: liquidity-aware N500 MFE-1 scored trades.
- Base selector: score threshold 0.20, max per event 1, Rs 2,00,000 ticket.
- Labels tested:
  - day hits 0.5% goal,
  - day is positive.
- Lookbacks tested: 40, 60, 80, 120, 160, 240 days.
- Minimum train days tested: 30, 40, 60, 80, 100.

Best regime-model row:

- Label: positive day.
- Lookback: 240 days.
- Minimum train days: 100.
- Regime threshold: 0.25.
- Active days: 227.
- Active-day average return: about +0.5423%.
- Active-day 0.5% hit rate: about 44.49%.
- Worst active day: about -1.15%.

Important calendar interpretation:

- The summary calendar average from the script is about +0.3933%, but that is over model-eligible post-warmup days.
- Counting all 413 scored days, including warmup/no-active days as 0%, the same active-day PnL is about +0.2981%.
- Therefore this regime model does not pass the 0.5% full-period daily goal.

Recent-period check:

- The best active-day file has 104 most recent active days averaging about +0.8707%.
- This is still strong, but it does not solve full-period validation.

Interpretation:

- The regime detector improves active-day quality, but loses too much coverage and still does not beat the full-period goal.
- The remaining problem is not just "when to trade"; it is also older-period signal adaptation.
- Automatic Discord goal-mode alerts should remain blocked.

Next recommended step:

- Diagnose the weak historical months/quarters for the liquidity-aware selector.
- Compare event families and feature distributions between weak older periods and the strong recent regime.
- Use that diagnosis to either add an older-regime specialist or a safer fallback selector.

## Liquidity-Aware Weak Period Diagnosis

Added:

- `scripts/diagnose_intraday_selector_periods.py`

Updated:

- `scripts/analyze_intraday_ranker_sizing.py`

Purpose:

- Diagnose why the liquidity-aware selector is still below 0.5% over the full period.
- Break results down by month, quarter, and event family.
- Add an option to exclude weak event families from sizing analysis.

Base selector diagnosed:

- Input: liquidity-aware N500 MFE-1 scored trades.
- Score threshold: 0.20.
- Max per event: 1.
- Ticket: Rs 2,00,000.

Weakest months:

- 2025-09: about -0.3933% average daily return.
- 2025-06: about -0.3422%.
- 2025-08: about -0.3302%.
- 2025-07: about -0.2482%.
- 2025-01: about -0.1242%.

Strongest months:

- 2026-04: about +1.5648%.
- 2026-02: about +1.3749%.
- 2025-03: about +0.9426%.
- 2026-05: about +0.8224%.
- 2025-04: about +0.6631%.

Event-family diagnosis:

- `vwap_pullback_hold`: average trade return about +0.4452%.
- `prior_high_continuation`: about +0.3198%.
- `or15_retest_hold`: about +0.2057%.
- `or15_breakout`: about +0.1387%.
- `vwap_reclaim`: about -0.0309%.

Event-family exclusion test:

- Excluding only `vwap_reclaim` improved the full 413-day calendar average:
  - Before: about +0.3243%.
  - After: about +0.3996%.
- Recent 104-day calendar average remained above target:
  - Before: about +0.9207%.
  - After: about +0.6810%.
- Validation still rejected the selector:
  - Output: `results/Data/intraday_goal_selector_n500_mfe1_liquidity_no_vwap_reclaim_s020_validation.csv`
  - Accepted: `False`.
  - Reason: full-period average is still below 0.5%.

Interpretation:

- This is another real improvement, not enough yet.
- The weakest lane is currently `vwap_reclaim`; removing it helps the older period but reduces recent upside.
- The main historical drag is concentrated in mid-2025, especially June through September.
- Automatic Discord goal-mode alerts should remain blocked.

Next recommended step:

- Build a specialized filter for the mid-2025 failure pattern, or train event-family-specific thresholds where `vwap_reclaim` is disabled unless extra confirmation is present.
- Test whether `vwap_reclaim` can be rescued with stricter liquidity/price-action filters instead of removing it completely.

## Multi-Target Exit Attempt

Added:

- `scripts/tune_intraday_multi_target_exits.py`

Purpose:

- Test the practical exit idea:
  - stop loss,
  - book partial profit around 1%,
  - book another partial around 3%,
  - trail the remaining runner for 5%, 8%, 12% style upside.
- This directly addresses the issue where stocks rise intraday and then drop before close.

Focused setup:

- Input: liquidity-aware N500 MFE-1 scored trades.
- Excluded event family: `vwap_reclaim`.
- Ticket: Rs 2,00,000.
- Score thresholds tested: 0.20, 0.25, 0.30.
- Stop: 0.8% or 1.0%.
- First target: 1.0%.
- First partial: 25% or 33%.
- Second target: 3.0%.
- Second partial: 25% or 33%.
- Runner target: 5%, 8%, or 12%.
- Trail: 1.0% or 1.5%.

Best focused result:

- Stop: 1.0%.
- First target: 1.0%.
- First partial: 25%.
- Second target: 3.0%.
- Second partial: 25%.
- Runner target: 8% or 12%.
- Trail: 1.0%.
- Move stop after first target: +0.2%.
- Score threshold: 0.20.

Full 413-day result:

- Traded days: 366/413.
- Calendar average daily return: about +0.3729%.
- 0.5%+ hit rate: about 45.52%.
- Positive-day rate: about 64.16%.
- Worst day: about -1.15%.
- Best day: about +4.4253%.

Recent 104-day result:

- Traded days: 98/104.
- Calendar average daily return: about +0.5084%.
- 0.5%+ hit rate: about 54.81%.
- Positive-day rate: about 70.19%.
- Worst day: about -1.15%.
- Best day: about +4.0140%.

Interpretation:

- Multi-target exits are realistic and protect profits better than waiting blindly until close.
- However, this focused sweep did not beat the simpler no-`vwap_reclaim` selector result:
  - no-`vwap_reclaim` full-period average: about +0.3996%.
  - multi-target full-period average: about +0.3729%.
  - no-`vwap_reclaim` recent 104-day average: about +0.6810%.
  - multi-target recent 104-day average: about +0.5084%.
- Therefore the exit logic is useful, but the current parameter set gives up too much upside.
- Automatic Discord goal-mode alerts should remain blocked.

Next recommended step:

- Try a less restrictive exit shape:
  - smaller first partial,
  - wider trailing stop after the second target,
  - or target-only partial exits without aggressive trailing.
- Continue improving selection/event-family filtering because exits alone still do not reach the full-period 0.5% goal.

## Loose Multi-Target Exit Attempt

Purpose:

- Reduce the upside drag from the first multi-target test.
- Keep only a small first partial and let the remaining position breathe with a wider trail.

Focused setup:

- Input: liquidity-aware N500 MFE-1 scored trades.
- Excluded event family: `vwap_reclaim`.
- Ticket: Rs 2,00,000.
- Score threshold: 0.20.
- Best loose exit:
  - Stop: 1.0%.
  - First target: 1.0%.
  - First partial: 10%.
  - Second target: 3.0%.
  - Second partial: 0%.
  - Runner target: 12%.
  - Trail: 2.5%.
  - Move stop after first target: no breakeven move.

Full 413-day result:

- Traded days: 366/413.
- Calendar average daily return: about +0.4016%.
- 0.5%+ hit rate: about 26.39%.
- Positive-day rate: about 31.72%.
- Worst day: about -1.15%.
- Best day: about +11.7888%.

Recent 104-day result:

- Traded days: 98/104.
- Calendar average daily return: about +0.5622%.
- Worst day: about -1.15%.
- Best day: about +7.8258%.

Comparison:

- Previous no-`vwap_reclaim` selector full-period average: about +0.3996%.
- Loose multi-target full-period average: about +0.4016%.
- This is a small full-period improvement and catches much bigger upside days.
- It still does not pass the 0.5% full-period goal.

Remaining weak months after loose exits:

- 2024-09: about -0.4900%.
- 2025-09: about -0.2528%.
- 2025-12: about -0.1308%.
- 2025-06: about -0.0753%.
- 2025-05: about -0.0262%.
- 2026-05: about -0.0072%.

Interpretation:

- Wider runner exits are better than the first multi-target version.
- Exits alone still do not solve the full-period gap.
- The next improvement needs weak-month filtering/adaptation, especially around the remaining bad months.

## Event-Specific Threshold Attempt

Added:

- `scripts/tune_intraday_event_thresholds.py`

Purpose:

- Tune separate score thresholds for each remaining event family after removing `vwap_reclaim`.
- A single global threshold was too blunt because event families have different quality.

Setup:

- Input: loose multi-target best trades.
- Excluded event family: `vwap_reclaim`.
- Capital/ticket: Rs 2,00,000.
- Event families tuned:
  - `or15_breakout`,
  - `or15_retest_hold`,
  - `prior_high_continuation`,
  - `vwap_pullback_hold`.

Best threshold result:

- `or15_breakout`: 0.30.
- `or15_retest_hold`: 0.00 or 0.20.
- `prior_high_continuation`: 0.00 or 0.20.
- `vwap_pullback_hold`: 0.25.

Full 413-day result:

- Traded days: 339/413.
- Calendar average daily return: about +0.4118%.
- Recent 104-day average: about +0.6078%.
- Worst day: about -1.15%.
- Best day: about +11.7888%.

Interpretation:

- Event-specific thresholds improved the full-period result from about +0.4016% to +0.4118%.
- The full-period result is still below the 0.5% goal.
- This confirms the path: event-family adaptation helps, but we need one more meaningful filter or model improvement.

## Selective VWAP Reclaim Re-Entry Attempt

Purpose:

- `vwap_reclaim` was weak at low thresholds, but removing it entirely reduced recent upside.
- Test whether high-score `vwap_reclaim` can be selectively re-enabled.

Setup:

- Input: loose multi-target exits with all event families included.
- Event-specific threshold tuning included:
  - `or15_breakout`,
  - `or15_retest_hold`,
  - `prior_high_continuation`,
  - `vwap_pullback_hold`,
  - `vwap_reclaim`.

Best threshold result:

- `or15_breakout`: 0.30.
- `or15_retest_hold`: 0.00 or 0.20.
- `prior_high_continuation`: 0.00 or 0.20.
- `vwap_pullback_hold`: 0.25.
- `vwap_reclaim`: 0.40.

Result:

- Traded days: 345/413.
- Full 413-day calendar average daily return: about +0.4395%.
- Recent 104-day average daily return: about +0.7181%.
- Worst day: about -1.15%.
- Best day: about +11.7888%.

Interpretation:

- Selective high-score `vwap_reclaim` re-entry is better than removing the family completely.
- This is the best full-period result so far, but still below the 0.5% goal.
- The remaining gap is about 0.0605 percentage points per day.

Rejected follow-up exit variants:

- Wider stops at 1.15%, 1.30%, and 1.50% did not help.
  - Best wide-stop threshold result was about +0.4300% full-period average.
  - Worst day worsened to about -1.30%.
- No-partial exits did not help.
  - Best no-partial full-period average was about +0.3360%.
- Faster first partial at 0.75% did not help.
  - Best fast-partial full-period average was about +0.3123%.
- Splitting capital into smaller tickets also did not help this selector.
  - The current best still uses one high-conviction Rs 2,00,000 position per selected day.

Current best known research configuration:

- Liquidity-aware N500 MFE-1 ranker.
- Loose multi-target exit:
  - stop 1.0%,
  - 10% partial at 1.0%,
  - no second partial,
  - runner target 12%,
  - 2.5% trailing stop.
- Event thresholds:
  - `or15_breakout`: 0.30,
  - `or15_retest_hold`: 0.00 or 0.20,
  - `prior_high_continuation`: 0.00 or 0.20,
  - `vwap_pullback_hold`: 0.25,
  - `vwap_reclaim`: 0.40.
- Result:
  - Full 413-day average: about +0.4395%.
  - Recent 104-day average: about +0.7181%.
  - Still below the full-period 0.5% goal, so Discord goal-mode alerts remain blocked.

## Three Percent Upside Objective Check

User question:

- Can we do the same disciplined optimization for 3% days as well?

Added:

- `scripts/diagnose_intraday_big_days.py`
- `--objective` support in `scripts/tune_intraday_event_thresholds.py`
  - `avg`: optimize average daily return first.
  - `big3`: optimize 3%+ day hit rate first.
  - `balanced3`: optimize average daily return first, then 3%+ day hit rate.

Current best balanced selector:

- Full 413-day average: about +0.4395%.
- Recent 104-day average: about +0.7181%.
- 1%+ days: 97/413, about 23.49%.
- 3%+ days: 29/413, about 7.02%.
- Recent 3%+ days: 8/104, about 7.69%.
- Best day: about +11.7888%.
- Worst day: about -1.15%.

Pure 3%-first threshold search:

- 3%+ days improved slightly to 31/413, about 7.51%.
- Recent 3%+ days improved to 9/104, about 8.65%.
- Full average dropped to about +0.4231%.
- Recent average dropped to about +0.6690%.

Interpretation:

- Yes, the code can optimize for 3% days explicitly.
- But the first 3%-first search is not better for the actual compounding goal because it lowers the average daily return.
- The right target is balanced upside preservation:
  keep the base 0.5% to 1% compounding objective first, while allowing runners to capture 3%, 10%, or 15% days when the selected trade is genuinely strong.
- Do not replace the current best selector with the pure 3%-first selector yet.
- Use 3% hit rate as a secondary metric and diagnostic, not the primary acceptance rule.

## Event Selection Weight Attempt

Added:

- `scripts/tune_intraday_event_selection_weights.py`

Purpose:

- Keep the current best event thresholds and loose runner exit.
- Instead of changing trade eligibility, add small event-family bonuses or penalties to the rank score when choosing the single best eligible setup for the day.
- This tests whether the selector was choosing the wrong setup family on a small number of days, especially around 3% upside preservation.

Best fine balanced result:

- Input: `results/Data/intraday_multi_target_exits_n500_mfe1_liquidity_with_vwap_loose_focus_best_trades.csv`
- Base thresholds:
  - `or15_breakout`: 0.30
  - `or15_retest_hold`: 0.00
  - `prior_high_continuation`: 0.00
  - `vwap_pullback_hold`: 0.25
  - `vwap_reclaim`: 0.40
- Selection bonuses:
  - `or15_breakout`: +0.02
  - `or15_retest_hold`: +0.04
  - `prior_high_continuation`: +0.04
  - `vwap_pullback_hold`: +0.06
  - `vwap_reclaim`: -0.08

Result:

- Traded days: 345/413.
- Full 413-day calendar average daily return: about +0.4663%.
- Recent 104-day average daily return: about +0.7589%.
- Compound average daily return from Rs 2,00,000: about +0.4529%.
- Ending compounded capital over the 413 scored days: about Rs 12,92,806.
- 0.5%+ days: 114/413, about 27.60%.
- 1%+ days: 98/413, about 23.73%.
- 3%+ days: 32/413, about 7.75%.
- Best day: about +11.7888%.
- Worst day: about -1.15%.

Comparison to previous best:

- Full average improved from about +0.4395% to about +0.4663%.
- Recent average improved from about +0.7181% to about +0.7589%.
- 3%+ days improved from 29 to 32.
- The weighted selector changed only 18/413 daily choices.
- It gained three 3%+ days and lost zero 3%+ days.

Interpretation:

- This is the new best research checkpoint.
- It is a real improvement because it changes only selection priority after live-valid threshold eligibility.
- It is still below the full-period 0.5% daily objective, so Discord goal-mode alerts remain blocked.
- The remaining full-period gap is about 0.0337 percentage points per day.

Guarded fine result:

- Added a raw-event protection option to `scripts/tune_intraday_event_selection_weights.py`.
- Rule tested:
  protect raw top `vwap_reclaim` if its raw rank score is at least 0.46.
- Reason:
  the weighted selector's largest giveback came from switching away from a high-score `vwap_reclaim` into a weaker `or15_retest_hold`.
- Protected days: 3/413.

Guarded result:

- Full 413-day calendar average daily return: about +0.4687%.
- Recent 104-day average daily return: about +0.7686%.
- Compound average daily return from Rs 2,00,000: about +0.4553%.
- Ending compounded capital over the 413 scored days: about Rs 13,05,839.
- 0.5%+ days: about 27.85%.
- 1%+ days: about 23.73%.
- 3%+ days: 32/413, about 7.75%.
- Losing days: 207/413, about 50.12%.
- Best day: about +11.7888%.
- Worst day: about -1.15%.

Interpretation:

- This is the new best research checkpoint.
- The guarded weighted selector improves the previous best full-period average from about +0.4395% to about +0.4687%.
- It still does not pass the full-period +0.5% daily objective.
- The remaining full-period gap is about 0.0313 percentage points per day.
- Discord goal-mode alerts remain blocked.

Next recommended step:

- Focus on the remaining loss days, not broad retuning.
- The next likely useful filter is an early-failure guard for selected trades whose post-entry adverse move expands quickly before favorable movement develops.
- Keep the current guarded weighted selector as the reference daily file:
  `results/Data/intraday_event_selection_weights_n500_mfe1_liquidity_with_vwap_loose_guarded_fine_balanced3_best_daily.csv`

## Early-Failure Exit Attempt

Added:

- `scripts/tune_intraday_early_failure_exits.py`

Purpose:

- Keep the guarded weighted selector fixed.
- Replay only the selected daily trades.
- Test a live-valid early-failure exit before the existing loose runner exit.
- Goal: cut trades that fail immediately after entry without giving away 3%+ winners.

Important implementation note:

- The replay must use `--move-stop-to-breakeven` to match the current loose runner artifact.
- Without that flag, the replay baseline does not match the reference daily file and should not be compared.

Best fine result:

- Reference selector:
  `results/Data/intraday_event_selection_weights_n500_mfe1_liquidity_with_vwap_loose_guarded_fine_balanced3_best_daily.csv`
- Best early-failure rule:
  - Watch first 3 bars after entry.
  - If price falls 0.18% before moving at least 0.10% in favor, exit early.
  - Existing trade cost remains 0.15%, so an early-failure exit is about -0.33% net.
  - Then normal loose runner exit continues for trades that do not trigger early failure.

Result:

- Full 413-day calendar average daily return: about +0.5087%.
- Recent 104-day average daily return: about +0.7882%.
- Compound average daily return from Rs 2,00,000: about +0.4961%.
- Ending compounded capital over the 413 scored days: about Rs 15,43,971.
- 0.5%+ days: about 27.12%.
- 1%+ days: about 23.00%.
- 3%+ days: 32/413, about 7.75%.
- Best day: about +11.7888%.
- Worst day: about -1.15%.
- Early-failure exits: 36 selected trades.

Comparison to guarded weighted selector:

- Full average improved from about +0.4687% to about +0.5087%.
- Recent average improved from about +0.7686% to about +0.7882%.
- 3%+ days stayed unchanged at 32/413.
- The early-failure rule did not lose any 3%+ days.
- It did clip a few 1% to 3% winners, but it cut enough -1.15% full-stop losers to more than compensate.

Segment check:

- First 206 days: improved from about +0.4919% to about +0.5558%.
- Middle 103 days: improved from about +0.1195% to about +0.1325%, still weak.
- Final 104 days: improved from about +0.7686% to about +0.7882%.

Interpretation:

- This is the first full-period research result that crosses the 0.5% average daily objective.
- It is an important checkpoint, but it is not yet enough to enable confident Discord goal-mode alerts.
- Reason: the early-failure threshold was selected by sweeping the same full 413-day result, so it needs a stricter validation report before live promotion.
- The middle-period weakness remains the next risk to address.

Next recommended step:

- Build a validation report specifically for the guarded weighted selector plus early-failure exit.
- The report should show train/validation/final segment performance and keep Discord goal-mode blocked unless the selected rule passes the validation criteria.

## Daily Selector Validation Report

Added:

- `scripts/build_intraday_daily_selector_validation.py`

Purpose:

- Validate a daily selector CSV directly.
- Require the selected daily-rule result to pass full, train, validation, and final segments before Discord goal-mode alerts can be allowed.
- This avoids accepting a rule only because it was tuned on the same full 413-day result.

Validation input:

- `results/Data/intraday_early_failure_exits_n500_mfe1_liquidity_with_vwap_loose_guarded_breakeven_fine_best_daily.csv`

Validation output:

- `results/Data/intraday_goal_selector_n500_mfe1_liquidity_guarded_early_failure_validation.csv`

Report result:

- `accepted`: `False`
- `live_discord_goal_alerts_allowed`: `False`
- Full 413-day average: about +0.5087%, passes the base target.
- Train 206-day average: about +0.5558%, passes.
- Validation 103-day average: about +0.1325%, fails the validation threshold.
- Final 104-day average: about +0.7882%, passes.

Interpretation:

- The early-failure selector is the first full-period result above the user's 0.5% average daily target.
- It still fails strict validation because the middle 103-day segment is weak.
- Discord goal-mode alerts remain blocked.
- The next best step is to diagnose and improve the weak validation/middle segment without damaging the strong train and final segments.

Weak segment diagnostic:

- The validation/middle period runs from 2025-07-24 to 2025-12-23.
- Median selected rank score is lower in the weak period than the train/final periods.
- Validation selected trades also show weaker relative strength and deeper adverse movement after entry.
- Single live-valid skip gates were tested on selected trades:
  - minimum rank score,
  - minimum relative strength,
  - minimum VWAP distance,
  - maximum range used,
  - maximum entry value share,
  - minimum volume ratio.
- Simple one-rule and two-rule skip gates did not solve the problem.
- No tested skip gate passed full, train, validation, and final criteria together.

Updated interpretation:

- The weak period is not fixed by simply skipping more selected trades.
- The next useful path is replacement selection or a regime model:
  if the current top selected trade is weak, choose a better alternate candidate for that day or stand aside based on day-level context.

## Replacement Selection Validation Pass

Added:

- `scripts/tune_intraday_replacement_selection.py`

Purpose:

- Keep the same N500 liquidity-aware ranker, guarded event weights, and early-failure exit idea.
- Instead of only skipping weak selected trades, test replacement selection:
  if the top adjusted candidate does not meet a live-valid quality rule, try the next eligible candidate for the same day.

Best validated replacement rule:

- Candidate must have `vwap_dist_pct >= 0.70`.
- Event thresholds remain:
  - `or15_breakout`: 0.30
  - `or15_retest_hold`: 0.00
  - `prior_high_continuation`: 0.00
  - `vwap_pullback_hold`: 0.25
  - `vwap_reclaim`: 0.40
- Event selection bonuses remain:
  - `or15_breakout`: +0.02
  - `or15_retest_hold`: +0.04
  - `prior_high_continuation`: +0.04
  - `vwap_pullback_hold`: +0.06
  - `vwap_reclaim`: -0.08
- Raw `vwap_reclaim` protection remains:
  - protect top raw `vwap_reclaim` if score is at least 0.46.
- Early-failure exit:
  - watch first 4 bars after entry,
  - if price falls 0.14% before moving at least 0.15% in favor, exit early,
  - then use the loose runner exit with breakeven stop after first partial.

Validated daily file:

- `results/Data/intraday_replacement_selection_n500_mfe1_liquidity_guarded_early_failure_vwap70_exitfine_best_best_daily.csv`

Validation report:

- `results/Data/intraday_goal_selector_n500_mfe1_liquidity_replacement_vwap70_early_failure_validation.csv`

Strict validation result:

- `accepted`: `True`
- `live_discord_goal_alerts_allowed`: `True`
- Full 413-day average: about +0.5027%.
- Train 206-day average: about +0.5651%.
- Validation 103-day average: about +0.2698%.
- Final 104-day average: about +0.6098%.
- Compound average daily return from Rs 2,00,000: about +0.4911%.
- Ending compounded capital over 413 scored days: about Rs 15,12,332.
- 0.5%+ days: about 25.42%.
- 1%+ days: about 22.03%.
- 3%+ days: 31/413, about 7.51%.
- Best day: about +11.7888%.
- Worst day: about -1.15%.
- Max drawdown on the compounded curve: about -4.31%.

Interpretation:

- This is the first selector report that passes the strict full/train/validation/final gate.
- It trades fewer days than the early-failure-only selector, but it fixes the weak validation segment.
- It meets the user's base 0.5% average daily objective in the full 413-day scored period.
- This does not guarantee future profit, but it is now strong enough for controlled paper/live-alert monitoring under the validation report.

Recommended live-safety stance:

- Use the accepted report for Discord goal-mode only if the live runtime is pointed at:
  `results/Data/intraday_goal_selector_n500_mfe1_liquidity_replacement_vwap70_early_failure_validation.csv`
- Start with paper or watch-only Discord alerts before real order automation.
- Keep daily risk guardrails active because worst-day and drawdown risk still exist.

## Live Top-Gainer Visibility Lane

Date: 2026-06-12

Reason:

- The Grade-A/B anomaly goal lane is the user's preferred trade lane and should not be disturbed without explicit review.
- Tradeable top-gainer radar was running, but it stayed quiet when leaders failed the live filters such as buyer/seller pressure, pullback from day high, liquidity, volume, RSI, or score.
- This made it look like the scanner was missing top gainers, when in many cycles it was rejecting them as unsafe or stretched.

Change:

- Added a separate Discord message: `Top Gainer Visibility - NOT A BUY SIGNAL`.
- It runs independently from the 50 goal-alert cap and independently from the tradeable top-gainer radar cap.
- Default cadence: every 15 minutes during market hours.
- Default content: top 10 movers above +1.0%, with live price, volume ratio, buyer/seller ratio, pullback from high, score, and a short status such as `clean watch; wait tradeable alert` or a reject reason.

Safety stance:

- This is visibility only, not a buy instruction.
- Real-buy style alerts still come from the hold-confirmed Grade-A/B anomaly lane or from a separate tradeable radar alert.
- `KITE_AUTO_TRADE=0` remains the required live stance; user trades manually.

Verification:

- Live repo focused test suite passed: `40 passed`.
- GitHub mirror focused test suite passed: `48 passed`.

## One-Year Top-Gainer Catchability Audit

Date: 2026-06-12

Reason:

- User is unhappy with alert quality and especially that the system is not catching enough top gainers.
- We needed to stop guessing and test whether final top gainers were actually detectable early intraday.

Added:

- `scripts/analyze_top_gainer_catchability.py`

Data:

- Local 5-minute Kite backfill:
  `results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29`
- Universe size in this backfill: 506 symbols.
- Audit selection: last 252 trading days, daily top 20 gainers with at least +3% close gain and at least Rs 5 crore traded value.
- Selected top-gainer symbol-days: 4,269.
- Replay rule: first visibility before 11:30 at +1.0%, +1.5%, +2.0%, or +3.0% from open; enter next 5-minute bar; test +1.5% target before -1.0% stop.

Output files:

- `results/Data/top_gainer_catchability_1y_day_summary.csv`
- `results/Data/top_gainer_catchability_1y_top_gainers.csv`
- `results/Data/top_gainer_catchability_1y_signals.csv`
- `results/Data/top_gainer_catchability_1y_summary.csv`
- `results/Data/top_gainer_catchability_1y_feature_edges.csv`
- `results/Data/top_gainer_catchability_1y_filter_grid.csv`
- `results/Data/top_gainer_catchability_1y_all_early_rule_candidates.csv`
- `results/Data/top_gainer_catchability_1y_all_early_rule_daily_top5.csv`
- `results/Data/top_gainer_catchability_1y_all_early_rule_practical_filters.csv`

Findings on final top gainers:

- At +1.0% before 11:30, 90.75% of final top gainers were visible.
- From that +1.0% visibility point, 78.47% hit +1.5% before a -1% stop.
- At +1.5% before 11:30, 86.18% were visible, and 76.81% hit +1.5% before stop.
- At +2.0%, 79.53% were visible, and 73.20% hit +1.5% before stop.
- At +3.0%, only 64.93% were still catchable before 11:30, and target-first dropped to 62.48%.

Best hindsight entry shape among final top gainers:

- Threshold around +1.0%.
- Do not chase above roughly +1.8% for the first visibility alert.
- Volume ratio at least 1.0x.
- Pullback from high at or below 0.5%.
- VWAP distance positive, around +0.3% or better.
- This subset had about 994 top-gainer entries with about 87.93% target-first and 11.87% stop-first.

Critical all-stock noise check:

- Applying the same early rule across all 506 stocks produced 39,499 candidates across 493 days.
- Raw result across all early movers:
  - target-first: 27.99%
  - stop-first: 46.17%
  - EOD/no target or stop first: 25.84%
  - average close return from entry: about -0.0012%
  - average MFE: about +1.3994%
  - average MAE: about -1.2612%
- Daily top 5 by low pullback/high volume was still weak:
  - average scalp return/trade: about -0.0944%
  - positive days: about 37.32%.

Interpretation:

- The user's core observation is correct: many top gainers are detectable around +1% to +1.5%, before they become +10% or +20%.
- But buying all early top-gainer-looking movers is not profitable; it creates too many false positives.
- Therefore the missing part is not visibility. The missing part is a ranker/selector that predicts which early movers are likely to become final top gainers.

Next recommended research step:

- Build an early-mover ranker using the 39,499 all-stock early-rule candidates.
- Label candidates that hit +1.5% before -1.0% stop and/or later became top gainers.
- Train/walk-forward rank by features available at alert time:
  signal move %, volume ratio, 5-bar volume ratio, pullback from high, VWAP distance, range position, candle body, recent 3-bar return, time of day, liquidity, sector/market context.
- Only after the ranker is positive out-of-sample should we turn it into a real buy alert lane.

Live stance:

- Do not convert top-gainer visibility messages into buy alerts yet.
- The current Grade-A/B anomaly lane should remain protected while the new early-top-gainer ranker is researched.

## Early Top-Gainer Ranker Attempt

Date: 2026-06-12

Added:

- `scripts/walk_forward_top_gainer_early_ranker.py`
- `scripts/enrich_top_gainer_early_candidates.py`
- `scripts/enrich_top_gainer_market_sector.py`
- `scripts/build_top_gainer_early_ranker_candidates.py`

Purpose:

- Train an honest walk-forward model on early-mover candidates.
- Each scoring date trains only on prior dates.
- Features are available at signal time only.
- Simulated trade rule remains +1.5% target before -1.0% stop; EOD return used only if neither target nor stop hit.

Inputs:

- Base candidates:
  `results/Data/top_gainer_catchability_1y_all_early_rule_candidates.csv`
- Enriched candidates:
  `results/Data/top_gainer_early_ranker_candidates_enriched.csv`
- Market/sector enriched candidates:
  `results/Data/top_gainer_early_ranker_candidates_enriched_sector.csv`

Model attempts:

1. Basic HGB model, label `target_first`.
   - Output: `results/Data/top_gainer_early_ranker_walk_forward_summary.csv`
   - Scored rows: 33,245 across 413 walk-forward days.
   - Best broad result was not deployable.
   - Example: score >= 0.45, max 3 trades: 46 days, avg daily return about +0.0278%, but median negative and too few days.

2. Enriched HGB model, label `target_first`.
   - Output: `results/Data/top_gainer_early_ranker_walk_forward_enriched_summary.csv`
   - Added 5-bar volume, range position, candle body, recent return, value-to-signal, OR15 features.
   - Best broad result: score >= 0.40, max 4/5 trades:
     - 196 days
     - avg daily return about +0.0167%
     - median daily return about -0.0153%
   - Still not deployable.

3. Market/sector enriched HGB model, label `target_first`.
   - Output: `results/Data/top_gainer_early_ranker_walk_forward_enriched_sector_summary.csv`
   - Added Nifty move, relative strength vs Nifty, sector breadth, sector relative strength.
   - Best high-score result traded only 7 days, avg about +0.0714%, not enough sample.
   - Broad settings remained negative or near zero.

4. Market/sector enriched HGB model, label `positive`.
   - Output: `results/Data/top_gainer_early_ranker_walk_forward_enriched_sector_positive_summary.csv`
   - Best setting: score >= 0.50, max 3 trades:
     - 142 days
     - avg daily return about +0.0036%
     - median about +0.0206%
     - total PnL about +Rs 1,008.66 over the tested period
   - This is too small to trust after slippage, taxes, and live execution delay.

Conclusion:

- The user's observation remains true: many final top gainers are visible around +1% to +1.5%.
- But the first ranker attempt cannot reliably separate future top gainers from fake early spikes.
- This lane is not ready for live buy alerts.

Decision:

- Do not deploy the early-top-gainer ranker into Discord buy alerts.
- Keep the outputs for research and continue using them to diagnose missed leaders.
- Next improvement must change the event definition, not just the model:
  require a stronger live event such as opening-range breakout/retest, pre-breakout accumulation, sector rotation confirmation, or order-book pressure before the +1% early mover becomes a buy candidate.

## Universal Discord Outbox Journal

Date: 2026-06-12

Reason:

- User wants every Discord message recorded so we can audit what was actually sent and whether the alert content was correct.
- The existing `intraday_goal_alerts` journal covered trade-style monitor alerts, but did not cover every heartbeat, top-gainer visibility message, prewarm alert, or watchdog alert.

Change:

- Added a universal append-only Discord outbox journal:
  `results/Data/discord_outbox/discord_outbox_YYYY-MM-DD.jsonl`
- Python monitor messages are journaled from `_send_discord_alert()`.
- Shell prewarm/watchdog messages are also journaled from their `send_discord()` helpers.

Fields recorded:

- `id`
- `sent_at`
- `source`
- `universe`
- `status`
- `http_status`
- `error`
- `message_length`
- `message`

Usage:

- This becomes the ground truth of what the VM attempted to send to Discord.
- Trade-specific structured journals still remain in:
  `results/Data/intraday_goal_alerts/`
- The outbox lets future analysis compare:
  generated alert, posted Discord text, HTTP acceptance, market outcome, and user action.

## Top-Gainer Visibility Audit And Scalp Watch Fix

Date: 2026-06-12

User concern:

- Top-gainer visibility messages were showing real market leaders, but the tradeable top-gainer radar was not surfacing enough of them.
- The user specifically asked whether the top-gainer alerts could have produced better 1.0% to 1.5% scalp exits instead of only watching the final high.

Evidence checked:

- Parsed the user's pasted Discord top-gainer visibility trail from 2026-06-12.
- Parsed 190 top-gainer visibility rows across 19 visibility messages and 24 unique symbols.
- Compared first seen visibility price against later visible prices from the same pasted trail.

Key finding:

- Several rejected visibility names still had useful upside after first visibility:
  - `IFCI`: about +9.0% visible later from first visibility.
  - `AIIL`: about +5.3% visible later from first visibility.
  - `CLSEL`: about +3.3% visible later from first visibility.
  - `ROTO`: about +3.4% visible later from first visibility.
  - `TBZ`: about +2.5% visible later from first visibility.
  - `GOLDIAM`: about +1.7% visible later from first visibility.
- A narrow relaxed tape rule on the pasted data:
  `price >= 50`, `volume >= 2x`, `pullback <= 1.5%`, and `(B/S >= 1.2x or volume >= 8x)`
  caught `AIIL` and `CLSEL`; both later had more than +1.5% available in the pasted visibility trail.

Root cause:

- The top-gainer visibility lane was seeing the leaders.
- The tradeable radar was rejecting some useful leaders because the older scanner quality score `Q` was weak, even when live tape was strong.
- This is separate from the Grade-A/B anomaly goal lane; anomaly logic should remain protected.

Code change:

- Added a separate top-gainer radar tape override:
  - hot mover only,
  - price >= 50,
  - high relative volume by default `>= 8.0x`,
  - pullback from high by default `<= 1.5%`,
  - buyer pressure by default `B/S >= 1.2x`, or extreme volume by default `>= 12.0x`.
- If this override is true, the top-gainer radar can pass the candidate even when old scanner `Q` is weak.
- The intraday goal selector also allows this separate radar override, so the candidate can reach Discord instead of being blocked by the normal anomaly stretch/RSI guards.
- Top-gainer visibility now labels this pattern as:
  `scalp watch: high-volume leader`
- The change does not relax the Grade-A/B anomaly basket.

Files changed:

- `pkscreener/classes/SignalMonitor.py`
- `tests/test_signal_monitor_discord_message.py`

Validation:

- Repo test:
  `scripts/runtime_python.sh -m pytest -q tests/test_signal_monitor_discord_message.py`
  passed: 52 tests.
- Live `tr-assistant` test:
  `scripts/runtime_python.sh -m pytest -q tests/test_signal_monitor_discord_message.py`
  passed: 44 tests.

Important caution:

- This is a scalp/watch improvement, not proof that every top gainer is buyable.
- Do not raise the hot-mover cap above 12% yet without more evidence; doing that may catch more winners like `CLSEL`, but it also increases chase risk.

## Live Loss Review And Guardrails

Date: 2026-06-12

User concern:

- The user reported live Kite losses after following alerts.
- Priority is capital protection first, then signal improvement.

Read-only Kite snapshot:

- Snapshot file:
  `/home/charan_sai/tr-assistant/results/Data/kite_live_exposure_snapshot_latest.json`
- Open exposure count: 5.
- Total open PnL at snapshot: about `-Rs 8,475`.
- Main loss drivers:
  - `GSLSU`: CNC holding, qty 899, avg 55.00, LTP 48.89, PnL about `-Rs 5,493`.
  - `RNITAI`: CNC holding, qty 346, avg 72.00, LTP 63.06, PnL about `-Rs 3,093`.
- Smaller open items:
  - `JBMA`: about `-Rs 158`.
  - `SHANTIGOLD`: about `-Rs 96`.
  - `PGEL`: about `+Rs 365`.

Root cause:

- Intraday goal/scalp alerts are supposed to be treated as same-day attempts, but live positions were being carried as CNC holdings.
- Discord output also mixed several concepts:
  anomaly alerts, engine-confirmed watch items, top-gainer radar/watch items, and current Buy Now board rows.
- That made some non-vetted watch output look too close to buyable alerts.

Guardrails added:

- Live launcher now forces the real-money Discord lane to:
  - `SIGNALMONITOR_GOAL_ANOMALY_ONLY=1`
  - `SIGNALMONITOR_GOAL_ALLOW_DIRECT_BUY=0`
  - `SIGNALMONITOR_ENGINE_WATCH_ENABLED=0`
- Fresh intraday goal entries are blocked after `SIGNALMONITOR_GOAL_LAST_ENTRY_TIME=14:30`.
- Exit/sell rows are still allowed through after the cutoff.
- In anomaly-only mode with direct buys disabled, the Discord monitor no longer shows the misleading `Current Buy Now board`.

Validation:

- Repo focused test:
  `scripts/runtime_python.sh -m pytest -q tests/test_signal_monitor_discord_message.py`
  passed: 54 tests.
- Live `tr-assistant` focused test:
  `scripts/runtime_python.sh -m pytest -q tests/test_signal_monitor_discord_message.py`
  passed: 46 tests.

Operating decision:

- The current live mode is alerts-only and anomaly-only.
- Top-gainer/radar research can continue, but it must not share the same count or visual treatment as the protected Grade-A/B anomaly lane.
- Any CNC carryover from an intraday alert must be considered a strategy break, not a normal goal-mode outcome.

## Real-Money Proof Gate

Date: 2026-06-12

User requirement:

- The tool should not be considered a real-money system until we can honestly say it makes money after live execution delays, slippage, taxes, and bad alerts.

Change:

- Added structured live alert outcome auditor:
  `scripts/audit_intraday_goal_alert_outcomes.py`
- The auditor reads:
  `results/Data/intraday_goal_alerts/intraday_goal_alerts_YYYY-MM-DD.csv`
- It evaluates outcomes after the alert was actually sent, not from hindsight high/low before the user could enter.

What it measures:

- Alert send delay.
- Whether the alert price was inside the sent minute candle.
- Realistic entry model:
  - default `next_open`, using the next candle open after the alert.
  - optional `alert_price` for optimistic comparison.
- First event after entry:
  - stop first,
  - Target 1 first,
  - Target 2 first,
  - or EOD.
- MFE/MAE after entry.
- Net return after estimated buy cost, sell cost, and slippage.
- Results by lane:
  - Grade-A anomaly,
  - Grade-B anomaly,
  - top-gainer radar,
  - engine watch,
  - other.

Usage:

```bash
scripts/runtime_python.sh scripts/audit_intraday_goal_alert_outcomes.py \
  --data-dir /home/charan_sai/tr-assistant/results/Data \
  --start-date 2026-06-04 \
  --end-date 2026-06-12 \
  --entry-model next_open \
  --out /home/charan_sai/tr-assistant/results/Data/intraday_goal_alert_outcomes/intraday_goal_alert_outcomes_latest.csv
```

Validation:

- Repo focused tests passed:
  `59 passed`.
- Live `tr-assistant` focused tests passed:
  `51 passed`.

Current blocker:

- A run on 2026-06-04 to 2026-06-12 found 87 structured alerts, but Kite historical fetch returned login `400/403` errors for all rows.
- The script now records those fetch errors instead of crashing.
- It can also fall back to local cached/backfilled candles with:
  `--prefer-local --local-interval 5minute`
  when local data exists.

Decision:

- Do not claim real-money profitability until the proof gate shows positive net results by lane.
- Engine-watch and top-gainer radar remain research/watch lanes unless their own audited net outcomes are positive.

## Pullback Entry / Fixed Exit What-If Dashboard

Date: 2026-06-12

User question:

- Would we have made more if we waited to buy below the alert price, such as `-1%` or `-2%`, and sold quickly at `+0.5%`, `+1%`, `+1.5%`, or `+2%`?

Decision:

- This is worth building.
- It directly tests whether the user's real execution style is better than chasing the alert price.
- It also shows when a lower entry never happened, which prevents false assumptions like "I could always buy at -2%."

Change:

- Added offline dashboard builder:
  `scripts/build_intraday_alert_whatif_dashboard.py`

What it does:

- Reads structured alert journals.
- Fetches or loads post-alert candles.
- Simulates entry offsets:
  `0`, `-0.5`, `-1`, `-1.5`, `-2` by default.
- Simulates fixed sell targets:
  `+0.5`, `+1`, `+1.5`, `+2`, `+3` by default.
- Uses a fixed stop, default `1%`.
- Applies estimated costs/slippage through the same cost helper as the alert outcome audit.
- Summarizes by alert lane and entry/target pair.
- Writes:
  - `whatif_trades.csv`
  - `whatif_summary.csv`
  - `whatif_dashboard.html`

Usage:

```bash
scripts/runtime_python.sh scripts/build_intraday_alert_whatif_dashboard.py \
  --data-dir /home/charan_sai/tr-assistant/results/Data \
  --start-date 2026-06-04 \
  --end-date 2026-06-12 \
  --entry-offsets '0,-0.5,-1,-1.5,-2' \
  --targets '0.5,1,1.5,2,3' \
  --stop-pct 1.0 \
  --ticket 50000 \
  --out-dir /home/charan_sai/tr-assistant/results/Data/intraday_alert_whatif
```

Current output:

- Dashboard generated at:
  `/home/charan_sai/tr-assistant/results/Data/intraday_alert_whatif/whatif_dashboard.html`
- Current run had no evaluated entries because Kite historical fetch is still returning login `400/403` errors and local June minute candles are not cached.

Validation:

- Focused tests passed:
  `8 passed`.

Next requirement:

- Fix/restore Kite historical auth or persist enough live candles locally.
- Then rerun this dashboard daily and compare:
  - chase at alert,
  - wait for `-0.5%`,
  - wait for `-1%`,
  - wait for `-2%`,
  - sell at `+0.5%` to `+2%`.

## +1.5% Scalp Ranker / ML Bridge

Date: 2026-06-16

Goal:

- Build a separate evidence layer for the user's main practical target: quick `+1.5%` scalps with a `-1%` stop.
- Do not disturb the current live Grade-A/B anomaly goal alerts.
- Use actual Discord-final alerts plus radar/anomaly audit rows to learn which alerts are worth buying, watching, or skipping.

Change:

- Added transparent ranker:
  `scripts/build_intraday_scalp_ranker.py`
- Added after-market automation wrapper:
  `scripts/intraday_scalp_ranker_eod.sh`
- Cron installed for live VM:
  `25 11 * * 1-5 /home/charan_sai/tr-assistant/scripts/intraday_scalp_ranker_eod.sh`
  which is `4:55 PM IST`, after the ML dataset job.
- Live outputs are written under:
  `/home/charan_sai/tr-assistant/results/Data/intraday_scalp_ranker/`

Outputs:

- `intraday_scalp_ranked_candidates.csv`
- `intraday_scalp_ranker_summary.csv`
- `intraday_scalp_ranker_first_signal_summary.csv`
- `intraday_scalp_ranker_notes.md`

Important behavior:

- The ranker is research/paper evidence only for now.
- It does not send Discord alerts and does not modify the live scanner.
- It adds a practical `first_symbol_lane_signal` view so repeated watch rows for the same symbol do not inflate results.
- Rejected audit rows are capped to `Skip`, and Grade-C anomaly rows are capped below A/B promotion.

Early evidence from 2026-06-15 to 2026-06-16:

- Buying every alert is still not good.
- On Discord-final alerts only, strict A caught `MANINFRA` and `RPTECH`; both hit `+1.5%` before `-1%` in this tiny sample.
- Grade-B/fuzzy alerts such as `TIPCO` and weaker top-gainer radar alerts remained B/C/Skip and were not good enough for automatic buying.
- Raw anomaly audit rows are noisy; simply loosening anomaly gates would likely create more losing trades.

Decision:

- Keep current live anomaly alerts untouched.
- Use the ranker after each market day to decide what rules deserve promotion.
- Do not promote top-gainer radar or Grade-C anomaly into real buy alerts until their first-signal summary shows positive results over more days.

## Top-Gainer Coverage Widening

Date: 2026-06-16

Problem:

- Live top-gainer visibility was showing only the top `10` movers.
- Tradeable top-gainer radar was capped at `12%`, so several `15%+` leaders could be hidden or rejected before the user could even see why.
- This made it look like the scanner was missing obvious top gainers.

Change:

- Kept Grade-A/B anomaly goal logic untouched.
- Increased top-gainer visibility from top `10` to top `25`.
- Increased top-gainer radar cap from `12%` to `20%`.
- Increased radar watch capacity from `15/day` to `30/day`.
- Increased radar watch per cycle from `3` to `5`.

Live verification:

- Live assistant relaunched with:
  `Top-gainer watch: 30/day, 1.2% to 20.0% live movers, 5/cycle`
- Visibility now says:
  `top 25 movers above 1.0%`
- First post-change widened-radar alert observed:
  `LLOYDSENGG` at `+12.61%`.

Important:

- Top-gainer visibility is still not a buy signal.
- Radar watch is a separate watch/scalp lane, not the trusted Grade-A/B anomaly buy lane.
- This change improves coverage and diagnosis; it does not guarantee the late high movers are safe to buy.

## Full Live Top-Gainer Leaderboard Fix

Date: 2026-06-16

Problem:

- Top-gainer visibility was still built from the scanner board.
- In fast live mode, only a small changed-symbol slice is fully rescanned each cycle, so the visibility message could show only a few leaders even when hundreds of stocks were up on live ticks.
- Discord messages longer than `1900` characters were silently truncated.

Change:

- Top-gainer visibility now reads directly from all live Kite ticks.
- Scanner rows are used only for extra metrics such as volume ratio, score, RSI, and action when available.
- The full live leaderboard is saved every visibility cycle at:
  `/home/charan_sai/tr-assistant/results/Data/signalmonitor_top_gainers_live_latest.csv`
- Discord webhook messages are split into multiple chunks instead of being truncated.

Live verification:

- Current live tick leaderboard had:
  `893` movers >= `1%`,
  `307` movers >= `3%`,
  `101` movers >= `5%`,
  `15` movers >= `10%`,
  `5` movers >= `15%`.
- The new visibility message includes those counts before listing top rows.

Important:

- This is a visibility/diagnostic fix, not a buy-signal relaxation.
- Radar buy/watch alerts remain filtered so the system does not spam hundreds of untradeable movers.

## Live Leader Suitability Score

Date: 2026-06-16

Problem:

- Seeing all top gainers is not enough.
- The real question is which leaders are still suitable for a scalp after they start moving, and which are already near/after peak.

Change:

- Added `LeaderScore` (`0-100`) to live top-gainer visibility.
- The score rewards:
  - real live turnover,
  - staying near day high,
  - strong position inside the day range,
  - positive move from open,
  - buyer/seller pressure,
  - volume ratio and scanner strength when available,
  - non-extreme RSI.
- The score penalizes/rejects:
  - penny/low-price stocks,
  - low live turnover,
  - large pullback from high,
  - weak B/S,
  - RSI extreme,
  - weak scanner confirmation.
- Discord now includes `Best suitability watches` before the raw leaderboard rows.
- The full CSV now includes:
  `TurnoverCr`, `DayPositionPct`, `FromOpenPct`, `LeaderScore`, and `Status`.

Live verification:

- New suitability output identified a small candidate set from hundreds of gainers:
  `SONATSOFTW`, `AFCOM`, `GVPIL`, `ARFIN`.
- Example strong candidate:
  `AFCOM` score `100`, `+12.6%`, `₹38.6Cr` live turnover, `2.0x` B/S, `0.4%` pullback, `96%` day-position.

Important:

- This is still a watch/scoring layer, not a guaranteed buy system.
- Next required evidence step is to paper-track the `LeaderScore >= 65` / non-reject candidates and compare +0.5%, +1%, +1.5%, and -1% outcomes.

## Anomaly Goal Gate Fix

Date: 2026-06-17

Problem:

- The anomaly detector was running and producing pass rows, but final goal mode could still suppress them.
- Example: `DOMS` was a Grade-B anomaly with open-move `+3.12%`, volume `251x`, and B/S `16x`, but display change versus previous close was `-1.69%`.
- The final goal gate used display change and wrongly rejected it as `day move -1.7% too flat`.

Change:

- For Grade-A/B/pre-move anomaly goal decisions, final goal mode now extracts the anomaly move-from-open token from `Checks` and uses that for stretch/flat gates.
- The broker-facing display change remains previous-close based.
- Added `signalmonitor_goal_gate_audit_YYYY-MM-DD.csv` and `signalmonitor_goal_gate_audit_latest.csv` so every anomaly/radar final-gate pass/reject is explainable.

Validation:

- A DOMS-shaped Grade-B row now evaluates to:
  `True`, `Grade-B anomaly burst setup`.
- Known-good rollback anchor remains documented, but rollback was not needed for this issue.

## Late Detection / Chase Problem

Date: 2026-06-23

Problem:

- The user clarified that Discord delivery is not the core issue.
- The real issue is late detection in the price move:
  `100 -> 105 -> alert BUY at 105 -> stock reverses`.
- This means the tool is often detecting after the easy part of the move, then presenting a normal buy alert with T1/T2 near the exhaustion zone.

Evidence:

- Structured Discord alert audit from `2026-06-09` to `2026-06-23` found `116` alerts.
- Median generated-to-sent delay was only about `3s`, so Discord queue delay is not the primary problem.
- Many alerts fired after the stock was already up `4%+`, `5%+`, or more.
- Evaluated rows with minute data showed weak post-alert behavior:
  - Alert-time day move `4-5%`: average net about `-0.61%`, win rate about `26.7%`.
  - Alert-time day move `6-8%`: average net about `-0.52%`, stop-hit rate about `50%`.
  - Top-gainer radar had many stop/fade cases after alert.
- Several Grade-A/B anomaly alerts contained stale confirmations like:
  `Confirm✓ held 1805s above first anomaly tick`.
  That means the anomaly was first detected around 30 minutes earlier, but later promoted as a buy alert.

Change Made:

- Stashed the broad Cline experimental bundle instead of committing it:
  `stash@{0}: cline-experimental-intraday-guardrails-before-anti-chase-fix`.
- Kept only the narrow anti-chase fix:
  anomaly confirmations now expire after `SIGNALMONITOR_ANOMALY_CONFIRM_MAX_AGE_SEC` default `300s`.
- Final goal selection also rejects stale confirmation text such as `Confirm✓ held 1805s`.
- Focused SignalMonitor tests passed after the minimal fix.

Next Architecture Direction:

- Stop treating `100 -> 105` as a normal buy.
- Build an ignition/pre-blast lane:
  - detect early moves around `+0.3%` to `+1.5%`,
  - require volume burst, buyer pressure, liquidity, and near-high behavior,
  - send watch first, not immediate buy,
  - promote to buy only if the next scan confirms continuation while entry is still not chased.
- If a stock is already up around `4-6%` without fresh breakout confirmation, mark it as:
  `MISSED / WAIT FOR PULLBACK`, not a normal buy alert.
- Future audits should compare:
  buy-now, buy-after-pullback, and wait-for-reclaim outcomes at `+0.5%`, `+1%`, and `+1.5%` targets.

## Early Ignition Watch Lane

Date: 2026-06-23

User Clarification:

- The goal is not only to reject late bad alerts.
- The real goal is to catch future top gainers earlier, before the move becomes obvious.
- Example raised: `SANSERA` moved from about `2986` to `3138`, but top-gainer radar only appeared around `10:32`, after much of the move was already done.

Change Made:

- Added a separate `🌱 Ignition Watch` lane from anomaly candidates.
- This lane can surface a candidate before full Grade-A/B confirmation when:
  - move from open is early: default `+0.5%` to `+2.5%`,
  - time-adjusted volume is high: default `>= 5.0x`,
  - quality score is at least `35`,
  - pullback from day high is small: default `<= 1.0%`,
  - price is not a penny stock: `>= ₹50`,
  - B/S is at least `0.75x`, or sector/range compression confirms.
- This specifically handles cases where the old B/C anomaly gate would silently reject/watch because B/S was not yet strong enough.
- Ignition watch has its own daily cap:
  `SIGNALMONITOR_IGNITION_MAX_ALERTS_PER_DAY`, default `10`.
- It does not consume the main Grade-A/B goal basket and does not block a later stronger Grade-A/B confirmation.

Important Behavior:

- `🌱 Ignition Watch` is not a blind buy signal.
- It means: abnormal volume is appearing early; watch for reclaim/continuation and use smaller size if acting.
- Existing Grade-A/B anomaly code was not disturbed.
- Hot top-gainer radar still has the anti-chase guard from the previous fix.

Validation:

- Added a SANSERA-shaped regression test:
  `+1.3%` from open, `8.0x` volume, `0.87x` B/S, Grade-C quality around `41`.
- Old behavior: candidate could fail the B/C gate and disappear until much later.
- New behavior: emits `🌱 Ignition Watch`.
- Focused test suite:
  `PYTHONPATH=. .venv/bin/pytest -q tests/test_signal_monitor_discord_message.py`
  passed with `61 passed`.

## EM CURE Late Runner Postmortem

Date: 2026-06-23

Observed Issue:

- `EMCURE` moved early from roughly `1819` into the `1850+` zone.
- The user observed it reached about `+5.96%` around `09:51`, then faded toward the `1845` area.
- Discord was not late:
  generated `09:55:05`, sent `09:55:07`, age `2s`.
- Detection was late:
  the Grade-A alert fired at `1890.10` after much of the move had already happened.

Audit Evidence:

- Anomaly audit showed the first useful early row at `09:25`:
  `EMCURE`, `watch`, B-tier, quality `59.2`, last price `1857.0`,
  open move `+2.08%`, previous-close move `+3.69%`,
  volume `3.68x`, B/S `2.22x`, pullback `0.36%`, sector confirmed.
- The later Grade-A row at `09:55` had:
  quality `71.5`, last price `1890.1`, open move `+3.9%`,
  previous-close move `+5.53%`, volume `8.31x`,
  and stale confirmation text `Confirm✓ held 1805s`.
- That is exactly the late detection problem:
  first useful signal around `09:25`, buy-style alert only around `09:55`.

Change Made:

- The ignition watch lane now has two paths:
  - high-volume ignition:
    volume `>= 5.0x` and B/S `>= 0.75x`,
  - pressure ignition:
    volume `>= 3.0x`, B/S `>= 1.5x`, and sector/range/quality confirmation.
- This means EM CURE-style rows are no longer hidden just because volume is under `5x`.
- Added an EM CURE-shaped regression test:
  B-tier, quality `59.2`, open move `+2.08%`, volume `3.68x`,
  B/S `2.22x`, sector `PHARMA`, pullback under `1%`.
- Focused test suite now passes with:
  `62 passed`.

## Live Process Ownership Split

Date: 2026-06-23

Problem:

- We were editing the `intraday-trading` repo, but the VM's old
  `trade-assistant.service` was still starting the live `SignalMonitor` from
  `/home/charan_sai/tr-assistant`.
- That made fixes confusing: a change could be correct in this repo but not
  affect the live intraday alerts unless the same change was copied into
  `tr-assistant`.
- This is the wrong architecture for daily intraday work.

Decision:

- `intraday-trading` owns intraday alert logic and runs the live intraday
  `SignalMonitor`.
- `tr-assistant` continues to own swing/assistant processes plus the shared
  Kite ticker stream.
- We keep only one Kite ticker stream because multiple live ticker streams on
  the same VM/account are fragile and can fight each other.

Current Services:

- `trade-assistant.service`
  - repo: `/home/charan_sai/tr-assistant`
  - owns: assistant supervisor, shared Kite ticker stream, Discord trade bot,
    autopilot/swing lane
  - does not own intraday `SignalMonitor`
  - guarded by `ASSISTANT_DISABLE_SIGNALMONITOR=1`
- `intraday-goal.service`
  - repo: `/home/charan_sai/intraday-trading`
  - owns: live intraday `SignalMonitor`, Grade-A/B anomaly alerts, ignition
    watch, radar logic, and intraday Discord alert formatting
  - consumes the shared ticker snapshots through symlinks in
    `intraday-trading/results/Data`

Important Rule:

- Future intraday alert fixes should be made in `intraday-trading` first.
- Do not copy fixes into `tr-assistant` as the normal workflow.
- Only touch `tr-assistant` when changing shared ticker, swing, assistant,
  Discord command bot, or service supervision behavior.

Pre-Push Fake Test:

- Added a dry-run mode for `scripts/start_intraday_goal_service.sh`:
  `INTRADAY_GOAL_DRY_RUN=1`.
- Added tests proving:
  - intraday can read fake shared Kite ticks through the same symlink pattern
    used in production,
  - the intraday service launcher starts only `--signalmonitor all`,
    not a second `--kitetickerstream`.
- Validation command:
  `PYTHONPATH=. .venv/bin/pytest -q tests/test_intraday_goal_service_split.py tests/test_signal_monitor_discord_message.py`
- Result:
  `64 passed`.

## Synthetic Profit Scenario Tests

Date: 2026-06-23

Clarification:

- The first fake shared-ticker test only proved plumbing:
  intraday can read a fake `kiteticker_all_ticks_latest.json` through the same
  symlink pattern used in production.
- It did not prove profitability.
- The production-equivalent test pattern should be closer to:
  create a fake market incident, replay it, and assert the fix changes the
  outcome.

New Test:

- Added `tests/test_intraday_profit_scenarios.py`.
- Fake incident modeled after the EM CURE late-detection problem:
  - early row: `🌱 Ignition Watch` around `+2.1%` from open, volume `3.7x`,
    B/S `2.2x`, sector confirmed,
  - late row: `🔥 Grade-A Runner` with `Confirm✓ held 1805s`.
- Expected behavior:
  - early ignition row is accepted by goal mode,
  - stale late runner is rejected,
  - fake candle replay hits Target 1 from the early entry,
  - fake candle replay hits stop from the late chase entry.
- Costs/slippage are included through `apply_costs` from
  `scripts/audit_intraday_goal_alert_outcomes.py`.

Validation:

- Command:
  `PYTHONPATH=. .venv/bin/pytest -q tests/test_intraday_profit_scenarios.py tests/test_intraday_goal_service_split.py tests/test_signal_monitor_discord_message.py`
- Result:
  `65 passed`.

Important:

- This still does not prove guaranteed real-money profitability.
- It proves the code now handles a known failure mode:
  late chase rows should not be promoted, and earlier ignition rows can be
  tested against target/stop outcomes before pushing.

## Real-Money Promotion Gate

Date: 2026-06-23

Reason:

- We cannot use "guaranteed real money" as an honest standard. Markets do not
  allow guarantees.
- We can require a production-style promotion gate:
  recent replay/paper outcomes must be profitable after costs, stops, win-rate,
  positive-day, and drawdown checks.

Change Made:

- Added `scripts/check_intraday_profit_promotion.py`.
- It simulates current live-style candidates from alert/anomaly history:
  Grade-A/B anomaly, ignition watch, and radar-style rows.
- It applies:
  - target/stop logic,
  - conservative stop-first ordering when both target and stop are possible,
  - estimated costs,
  - daily ticket sizing,
  - recent-day pass/fail thresholds.
- Added `tests/test_intraday_profit_promotion_gate.py`.
- Real-money readiness now calls this gate from
  `scripts/check_intraday_goal_readiness.py`.
- The gate can be skipped only by explicit operator override:
  `--skip-profit-gate`.

Current Result:

- Command:
  `.venv/bin/python scripts/check_intraday_profit_promotion.py --input results/Data/anomaly_history.csv --out-prefix results/Data/intraday_profit_promotion_gate_current`
- Result:
  `Accepted: False`
- Current metrics:
  - days: `12`,
  - trades: `63`,
  - average daily return: `-1.3733%`,
  - positive days: `0.0%`,
  - worst day: `-1.875%`,
  - trade win rate: `4.76%`,
  - total P&L on simulation: `-32960`.

Tuning Check:

- Existing tuner also failed to find an accepted configuration on current
  anomaly history.
- Best displayed configurations were still negative on test.

Decision:

- Current intraday alerts may continue for observation/paper tracking.
- Current strategy is **not ready for real-money promotion**.
- Next tuning work must improve the profit gate before we increase confidence
  or treat alerts as buyable production signals.

## Strategy Pivot: Alert Is Candidate, Not Entry

Date: 2026-06-23

Problem:

- The current code often treats an alert as an immediate buy.
- Recent evidence shows this is the wrong assumption:
  - median favorable move after alert is only about `+0.93%`,
  - median adverse move after alert is about `-2.61%`,
  - with `+1.5%` target and `-1.0%` stop, many alerts stop out before proving
    continuation.
- The issue is not mainly Discord delay.
- The issue is late/chasing entry and missing post-alert confirmation.

New Direction:

- Keep anomaly/radar/ignition alerts as **candidate discovery**.
- Do not mark them as production buy signals until a second entry condition
  confirms.
- Candidate examples:
  - Grade-A/B anomaly,
  - ignition watch,
  - top-gainer radar watch.
- Entry confirmation examples to test:
  - pullback from alert price by `0.5%` to `1.0%`, then reclaim,
  - VWAP reclaim,
  - next 2-3 candles form higher low,
  - price stays near high without `1%` adverse move,
  - volume remains elevated after the first spike,
  - market/sector remains supportive.

Next Required Build:

- Build a minute/5-minute replay that takes each alert as a watch event and
  tests entry models:
  - immediate buy,
  - buy only after pullback/reclaim,
  - buy only after next-candle confirmation,
  - skip if first adverse move is too fast.
- Compare target/stop outcomes after costs.
- Promote only the entry model that passes
  `scripts/check_intraday_profit_promotion.py`.

TradingView:

- TradingView can help later as an extra confirmation source.
- It is not required for the first fix because Kite minute candles can test the
  same core idea: VWAP, candle confirmation, pullback/reclaim, and continuation.

## Post-Alert Confirmation Replay

Date: 2026-06-23

Question:

- If the alert itself is often early/late/noisy, can we fix the outcome by
  waiting for confirmation after the alert instead of buying immediately?

Change Made:

- Added `scripts/tune_post_alert_confirmation_entries.py`.
- It replays actual alert history and tests live-compatible post-alert entry
  modes:
  - immediate next-bar entry,
  - pullback then reclaim,
  - next green/continuation candle,
  - hold for N bars without early failure.
- Added tests:
  `tests/test_post_alert_confirmation_entries.py`.

Validation:

- Unit tests:
  `PYTHONPATH=. .venv/bin/pytest -q tests/test_post_alert_confirmation_entries.py tests/test_intraday_profit_promotion_gate.py tests/test_intraday_profit_scenarios.py`
  passed with `7 passed`.
- Replay command:
  `.venv/bin/python scripts/tune_post_alert_confirmation_entries.py --input results/Data/anomaly_history.csv --out-prefix results/Data/post_alert_confirmation_entries_current --min-quality 45 --max-change 7 --target-pct 1.5 --stop-pct 1.0 --cost-pct 0.25 --capital 200000 --ticket 50000 --goal-pct 0.5 --max-trades-per-day 6`

Result:

- Best mode was still negative:
  - `hold_no_fail`, `2` bars, fail threshold `0.4%`,
  - average daily return: `-0.0419%`,
  - positive day rate: `46.15%`,
  - trade win rate: `37.16%`,
  - total simulated P&L: `-1088.25`.
- Immediate buy was worse:
  - average daily return: `-0.0588%`,
  - trade win rate: `34.75%`,
  - total simulated P&L: `-1882.55`.

Decision:

- Post-alert confirmation alone does not rescue the current anomaly alert
  source.
- The next fix must change candidate discovery/ranking, not only entry timing.
- Continue researching/promoting the stronger event-family/liquidity ranker
  lane, because old walk-forward summaries show it is materially better than
  the current anomaly-as-buy lane.

## Early ML Ranker V1

Date: 2026-06-23

Goal:

- Stop buying late anomaly/top-gainer moves directly.
- Build an ML/ranker lane that scores early event candidates by the probability
  of hitting `+1.5%` before `-1.0%`.
- Keep this lane paper/research only until it passes a profit gate.

Change Made:

- Added `scripts/train_intraday_early_ml_ranker.py`.
- Added `scripts/intraday_early_ml_ranker_eod.sh`.
- Added tests in `tests/test_intraday_early_ml_ranker.py`.

Training Data:

- Input:
  `results/Data/intraday_event_family_ranker_walk_forward_n500_mfe1_liquidity_scored_trades.csv`.
- Rows: `163712`.
- Days: `413`.
- Features used: `65`.
- Future/outcome fields such as `max_favorable_pct`, `max_adverse_pct`,
  `trade_return_pct`, `exit_reason`, `eod_return_pct`, `label`, and old
  `rank_score` are excluded from model features.

Label:

- Positive only when price hit `+1.5%` and did **not** hit `-1.0%`.
- If both target and stop were possible after entry, the row is treated as
  unproven/negative to avoid optimistic same-candle assumptions.

Validation:

- Unit tests:
  `PYTHONPATH=. .venv/bin/pytest -q tests/test_intraday_early_ml_ranker.py`
  passed with `3 passed`.
- Compile check:
  `.venv/bin/python -m py_compile scripts/train_intraday_early_ml_ranker.py`
  passed.

HGB Result:

- Command output prefix: `results/Data/intraday_early_ml_ranker_v1`.
- Model artifact: `results/Data/models/intraday_early_ml_ranker_v1.joblib`.
- Train AUC: `0.6892`.
- Test AUC: `0.6077`.
- Best tested threshold: `0.60`.
- Test active days: `4`.
- Test trades: `5`.
- Positive day rate: `75.00%`.
- Average daily return on active days: `0.2898%`.
- Worst active day: `-0.0908%`.

Forest Result:

- Command output prefix: `results/Data/intraday_early_ml_ranker_v1_forest`.
- Model artifact:
  `results/Data/models/intraday_early_ml_ranker_v1_forest.joblib`.
- Train AUC: `0.7412`.
- Test AUC: `0.6053`.
- Best tested threshold: `0.70`.
- Test active days: `9`.
- Test trades: `16`.
- Positive day rate: `66.67%`.
- Average daily return on active days: `0.2183%`.
- Worst active day: `-0.625%`.

Decision:

- ML V1 is useful as a ranking/filtering direction, but it is not ready for
  real-money buy alerts.
- The strict profitable region is sparse; this confirms that more alerts is
  not automatically better.
- Next step is to run the ML lane as paper/watch only and collect live outcome
  data before promotion.

## Early ML Watch Lane

Date: 2026-06-23

Goal:

- Score live/local 1-minute candle event candidates with ML without touching
  anomaly/goal alerts.
- Keep this as a paper-only watch lane.

Change Made:

- Added `scripts/score_intraday_early_ml_watch.py`.
- Added `scripts/run_intraday_early_ml_watch.sh`.
- Added `tests/test_intraday_early_ml_watch.py`.

How It Works:

- Reads shared 1-minute candle files from
  `/home/charan_sai/tr-assistant/results/Data/kiteticker_candles/1minute`
  by default.
- Reuses event definitions from `scripts/backtest_intraday_event_candidates.py`
  so live watch candidates match the research families:
  - `or15_breakout`,
  - `vwap_reclaim`,
  - `prior_high_continuation`,
  - `or15_retest_hold`,
  - `vwap_pullback_hold`.
- Applies the saved early ML model.
- Writes:
  - `results/Data/intraday_early_ml_watch/early_ml_watch_candidates_DATE.csv`,
  - `results/Data/intraday_early_ml_watch/early_ml_watch_ranked_DATE.csv`,
  - `results/Data/intraday_early_ml_watch/early_ml_watch_ranked_latest.csv`.

Validation:

- Unit tests:
  `PYTHONPATH=. .venv/bin/pytest -q tests/test_intraday_early_ml_watch.py tests/test_intraday_early_ml_ranker.py`
  passed with `4 passed`.
- Compile check for scorer/ranker passed.

2026-06-23 Baseline:

- HGB model scored `139` event candidates.
- HGB max score was only about `0.307`, below watch threshold `0.45`.
- Forest model max score was about `0.510`, below its validated threshold
  `0.70`.
- This means ML V1 did not see a validated high-confidence early setup on the
  available 2026-06-23 candles.

Decision:

- Do not loosen thresholds and call them buy alerts.
- For observation, a lower watch threshold can be used, but those rows remain
  paper-only.
- Next step is to add an outcome scorer for `early_ml_watch` rows so we know
  whether lower-score observations are useful or just noise.

## Early ML Watch Outcome Scorer

Date: 2026-06-23

Goal:

- Close the feedback loop for the early ML watch lane.
- Measure whether paper-watch rows actually hit `+1.5%` before `-1.0%`.

Change Made:

- Added `scripts/score_intraday_early_ml_watch_outcomes.py`.
- Added `scripts/run_intraday_early_ml_watch_outcomes.sh`.
- Added `tests/test_intraday_early_ml_watch_outcomes.py`.

How It Works:

- Reads:
  `results/Data/intraday_early_ml_watch/early_ml_watch_ranked_DATE.csv`
  or all scored candidates.
- Uses shared 1-minute candles.
- Enters at the watch row `entry_time` / `entry_price`.
- Conservative same-candle rule: if target and stop are both touched in the
  same minute, count stop first.
- Writes:
  - `early_ml_watch_outcomes_SOURCE_DATE.csv`,
  - `early_ml_watch_outcome_summary_SOURCE_DATE.csv`,
  - latest copies for dashboard/quick checks.

Validation:

- Unit tests:
  `PYTHONPATH=. .venv/bin/pytest -q tests/test_intraday_early_ml_watch_outcomes.py tests/test_intraday_early_ml_watch.py tests/test_intraday_early_ml_ranker.py`
  passed with `7 passed`.
- Compile check passed.

2026-06-23 Relaxed Watch Result:

- Input: relaxed HGB paper-watch rows generated with low observation threshold.
- Rows: `12`.
- Target hit rate: `8.33%`.
- Stop hit rate: `83.33%`.
- Average net return: `-0.9701%`.
- Simulated P&L using `50000` ticket: `-5820.70`.

2026-06-23 All Candidate Result:

- Rows: `139`.
- Target hit rate: `15.11%`.
- Stop hit rate: `71.94%`.
- Average net return: `-0.7594%`.
- Simulated P&L using `50000` ticket per row: `-52778.75`.

Decision:

- Lowering the ML threshold to get more alerts is not acceptable.
- The watch lane correctly proved that low-score/relaxed candidates are bad on
  this day.
- Next work should improve candidate features/filters, especially liquidity,
  price floor, pullback depth, and avoiding weak low-quality symbols, before
  sending any ML alerts to Discord.

## Early ML Watch Hygiene Filters

Date: 2026-06-23

Goal:

- Avoid flooding the paper watch lane with low-quality relaxed ML candidates.
- Test whether simple hygiene filters can reduce the obvious losers before we
  collect more live data.

Change Made:

- Added `scripts/tune_intraday_early_ml_watch_filters.py`.
- Added hygiene gates to `scripts/score_intraday_early_ml_watch.py`:
  - `--watch-min-price`,
  - `--watch-min-vol-ratio`,
  - `--watch-max-pullback-pct`,
  - `--watch-max-move-pct`,
  - `--watch-events`.
- Exposed those gates through `scripts/run_intraday_early_ml_watch.sh` env vars.

2026-06-23 Filter Tuning Result:

- Broad relaxed ML watch was bad:
  - `12` ranked rows,
  - target hit `8.33%`,
  - stop hit `83.33%`,
  - simulated P&L `-5820.70`.
- All scored candidates were also bad:
  - `139` rows,
  - target hit `15.11%`,
  - stop hit `71.94%`.
- A tiny filtered subset looked better:
  - filters:
    `vwap_pullback_hold`, `vol_ratio >= 2`, `pullback_from_high_pct <= 0`,
    `move_from_open_pct <= 2`,
  - rows: `3`,
  - target hit `66.67%`,
  - stop hit `33.33%`,
  - simulated P&L `+625`,
  - daily return on `200000` capital: `+0.3125%`.

Decision:

- This is not enough proof for real-money alerts because it is only one day and
  only three rows.
- But it is a better paper-watch filter than the broad relaxed lane.
- Next step is to run this filtered ML watch lane across multiple available
  candle days and compare daily stability before any Discord promotion.

## Early ML Watch Multi-Day Replay

Date: 2026-06-23

Goal:

- Test whether the filtered early ML watch lane that looked better on
  2026-06-23 holds up across all available local 1-minute candle days.

Change Made:

- Added `scripts/replay_intraday_early_ml_watch_days.py`.
- Added `tests/test_intraday_early_ml_watch_replay.py`.
- Fixed empty watch/outcome files so no-trade days become zero-row days instead
  of crashing the replay.

Replay Settings:

- `watch_events = vwap_pullback_hold`
- `watch_min_vol_ratio = 2`
- `watch_max_pullback_pct = 0`
- `watch_max_move_pct = 2`
- `min_score = 0`
- `top_n = 20`
- target `+1.5%`, stop `-1.0%`, cost `0.25%`.

Available Candle Days:

- `2026-06-12`
- `2026-06-15`
- `2026-06-16`
- `2026-06-17`
- `2026-06-18`
- `2026-06-19`
- `2026-06-22`
- `2026-06-23`

Result:

- Dates tested: `8`
- Traded dates: `3`
- Total paper rows: `39`
- Positive day rate: `12.5%` across all dates
- Average daily return: `-0.5052%`
- Worst day: `-3.3231%`
- Total simulated P&L: `-8082.75`

Daily Detail:

- `2026-06-12`: no rows
- `2026-06-15`: `16` rows, daily return `-1.0308%`
- `2026-06-16`: no rows
- `2026-06-17`: no rows
- `2026-06-18`: no rows
- `2026-06-19`: no rows
- `2026-06-22`: `20` rows, daily return `-3.3231%`
- `2026-06-23`: `3` rows, daily return `+0.3125%`

Top-N Check:

- Taking only the top `1`, `2`, or `3` candidates per active day was still
  negative:
  - top 1: `-1589.25` P&L,
  - top 2: `-365.15` P&L,
  - top 3: `-310.55` P&L.

Decision:

- The filtered ML watch lane is **not stable enough**.
- The current early event definitions still catch many weak setups that fade.
- Next improvement should change candidate generation/features, not merely
  thresholding:
  - add stronger liquidity/value gates,
  - avoid very low-quality symbols,
  - add market/sector confirmation to live watch features,
  - add order-book pressure if available,
  - consider a stricter “breakout then retest hold” event rather than raw
    `vwap_pullback_hold`.

## Doji Ashi / Oversold Bounce TradingView Replay

Date: 2026-06-23

Goal:

- Test the shared TradingView/Pine “Doji Ashi” idea as a separate intraday
  research lane.
- This is a mean-reversion oversold bounce idea, not a top-gainer breakout
  idea:
  - 5-minute RSI oversold,
  - 10-minute RSI also weak,
  - volume spike,
  - suppress extreme volume spikes,
  - avoid simple price/volume exhaustion,
  - enter next 5-minute bar,
  - exit at target, stop, or max holding bars.

Change Made:

- Added `scripts/replay_intraday_doji_ashi_bounce.py`.
- Added `tests/test_intraday_doji_ashi_bounce.py`.
- The script is paper/research only. It does not send Discord alerts or place
  orders.

Verification:

- `PYTHONPATH=. .venv/bin/pytest -q tests/test_intraday_doji_ashi_bounce.py`
  passed: `3 passed`.
- `.venv/bin/python -m py_compile scripts/replay_intraday_doji_ashi_bounce.py`
  passed.

Replay Command:

```bash
.venv/bin/python scripts/replay_intraday_doji_ashi_bounce.py \
  --target-pct 1.0 \
  --stop-pct 1.0 \
  --max-bars 6 \
  --max-trades-per-day 6 \
  --min-price 20
```

Available Candle Days:

- `2026-06-12`
- `2026-06-15`
- `2026-06-16`
- `2026-06-17`
- `2026-06-18`
- `2026-06-19`
- `2026-06-22`
- `2026-06-23`

Result:

- Dates with trades: `4`
- Selected trades: `24`
- Raw signal rows: `420`
- Positive selected days: `0.0%`
- Average selected daily return: `-0.2216%`
- Worst selected day: `-0.4896%`
- Total selected simulated P&L: `-1773.10`

Daily Detail:

- `2026-06-15`: `6` trades, target hit `16.67%`, stop hit `0.00%`,
  daily return `-0.2781%`, P&L `-556.30`.
- `2026-06-17`: `6` trades, target hit `16.67%`, stop hit `16.67%`,
  daily return `-0.0124%`, P&L `-24.90`.
- `2026-06-22`: `6` trades, target hit `16.67%`, stop hit `0.00%`,
  daily return `-0.1064%`, P&L `-212.70`.
- `2026-06-23`: `6` trades, target hit `16.67%`, stop hit `16.67%`,
  daily return `-0.4896%`, P&L `-979.20`.

Raw Signal Check:

- All raw rows: `420`
- Target first: `50`
- Stop first: `33`
- Time exit: `337`
- Average raw net return: `-0.1607%`

Decision:

- The shared Doji Ashi idea is **not profitable as-is** on our available
  NSE/BSE intraday candles.
- Do not promote this to Discord or real trading.
- It is still useful as a separate research lane because it tests the opposite
  behavior from the current failing chase-after-run alerts: it tries to buy
  oversold bounce setups instead of buying after a stock already moved.
- Next useful work, if we continue this lane:
  - add market/sector filters,
  - require VWAP reclaim after oversold,
  - avoid weak market-wide selloff periods,
  - test smaller targets like `0.5%` and `0.75%`,
  - train ML/rules on raw bounce rows to learn which oversold signals actually
    recover instead of timing out.

## 2026-07-03 - Leader Ignition Shadow Lane

Problem:

- The old top-gainer/radar paths often detect leaders after the stock has
  already moved from the useful early zone into a chase zone.
- The scanner-backed radar depends on `current_simple_df`, `Buy Now`/momentum
  state, older score `Q`, changed-symbol caps, and later filters. That can make
  the alert arrive at the "105 after starting at 100" problem instead of the
  "100.5 beginning to ignite" problem.
- Goal is not only to reject bad leaders. Goal is to record whether we can catch
  early leaders and whether buying immediately or waiting for a pullback/reclaim
  works better.

Architecture:

- Added a separate raw-tick lane in `SignalMonitorRunner`:
  `leader_ignition_shadow`.
- It reads `actionable_runner.live_ticks` directly and does not require the
  scanner simple board, `Buy Now`, anomaly, or top-gainer radar.
- It creates two states:
  - `Leader Ignition Watch`: early raw-tick leader window.
  - `Leader Pullback Reclaim`: same watched leader pulls back healthily, then
    reclaims with buyer pressure.
- It records pass/reject evidence under:
  `results/Data/leader_ignition/leader_ignition_YYYY-MM-DD.csv`
- It persists active watch state under:
  `results/Data/signalmonitor_leader_ignition_state.json`

Safety:

- Enabled in the live launcher as shadow/audit mode:
  `SIGNALMONITOR_ENABLE_LEADER_IGNITION=1`
- Discord is off by default:
  `SIGNALMONITOR_LEADER_IGNITION_DISCORD=0`
- It does not feed goal-mode alerts and does not feed real autotrade orders.
- Anomaly Grade-A/B and Grade-B autotrade logic were not changed.

Initial Filters:

- Early move window:
  - from open: `+0.5%` to `+2.8%`
  - from previous close/day change: `+0.5%` to `+4.5%`
- Rejects price below `₹50`, stretched moves, pullbacks before watch,
  insufficient traded value, and cases with neither buyer pressure nor volume
  acceleration.
- Pullback/reclaim tracks candidates for `45` minutes by default.

Fake-Market Tests:

- `test_leader_ignition_watch_uses_raw_ticks_without_simple_board`
- `test_leader_ignition_rejects_already_stretched_mover`
- `test_leader_pullback_reclaim_after_watch`

Verification:

- `intraday-trading`: `./.venv/bin/python -m pytest tests/test_signal_monitor_discord_message.py -q`
  passed: `41 passed`.
- `intraday-trading`: `./.venv/bin/python -m py_compile pkscreener/classes/SignalMonitor.py`
  passed.
- `tr-assistant`: `./.venv/bin/python -m pytest tests/test_signal_monitor_discord_message.py -q`
  passed: `33 passed`.
- `tr-assistant`: `./.venv/bin/python -m py_compile pkscreener/classes/SignalMonitor.py`
  passed.

## 2026-07-03 - Leader Ignition Backtest

Purpose:

- Validate the new leader idea before promoting it to Discord or real orders.
- Historical data has 5-minute candles, not raw order-book depth, so this
  backtest covers the backtestable parts only:
  - early move from open/previous close,
  - volume acceleration,
  - traded value/liquidity,
  - pullback from day high,
  - pullback/reclaim behavior.
- No lookahead: signal is detected on a completed 5-minute candle and entry is
  simulated on the next candle open.

Files:

- Script: `scripts/backtest_leader_ignition.py`
- Unit tests: `tests/test_leader_ignition_backtest.py`
- Output directory: `results/Data/leader_ignition_backtest/`

Command:

```bash
./.venv/bin/python scripts/backtest_leader_ignition.py \
  --progress-every 100 \
  --out-prefix results/Data/leader_ignition_backtest/leader_ignition
```

Full-cache baseline result:

- Data: `results/Data/kite_history_backfill/5minute/2024-05-30_2026-05-29`
- Symbols replayed: `506`
- Simulated alert entries: `116,171`
- Trading days: `493`
- Cost assumption: `0.15%` per completed trade.
- Target/stop assumption: `+1.0%` target, `-1.0%` stop.

Event summary:

- `ignition_immediate`: `65,202` trades, `45.33%` win rate, average return
  `-0.1673%`, median return `-0.2259%`.
- `pullback_reclaim`: `50,969` trades, `42.19%` win rate, average return
  `-0.1697%`, median return `-0.2195%`.

Chronological portfolio summary:

- `ignition_immediate`, `0.5%` daily goal: positive day rate `33.67%`,
  average daily return `-0.1922%`, total P&L `-189,534.40` on the configured
  `2L` capital / `50k` ticket model.
- `pullback_reclaim`, `0.5%` daily goal: positive day rate `31.50%`,
  average daily return `-0.2037%`, total P&L `-200,462.40`.

Decision:

- The naive leader ignition rule is **not profitable as-is**.
- Keep the live lane in shadow/audit mode only.
- Do not turn on Discord buys or real orders for this lane.
- The useful next step is selector tuning, not deployment:
  - filter by score/volume/time-of-day,
  - test different target/stop/cost assumptions,
  - test market-regime and sector-leader filters,
  - compare "buy immediately" versus "wait for -0.5%/-1.0% pullback then reclaim".

Verification:

- `./.venv/bin/python -m pytest tests/test_leader_ignition_backtest.py -q`
  passed: `3 passed`.
- `./.venv/bin/python -m py_compile scripts/backtest_leader_ignition.py tests/test_leader_ignition_backtest.py`
  passed.

## 2026-07-03 - Leader Ignition Selector Tuning

Purpose:

- Search stricter filters on top of the loose leader ignition backtest.
- Avoid fooling ourselves with one-period overfit:
  - first `70%` of dates are train,
  - final `30%` of dates are test,
  - selectors are ranked on train-side event quality,
  - only the shortlist is evaluated with portfolio/day-goal logic on test.

Files:

- Script: `scripts/tune_leader_ignition_selector.py`
- Unit tests: `tests/test_leader_ignition_tuner.py`
- Output: `results/Data/leader_ignition_backtest/leader_ignition_selector_tuning.csv`

Command:

```bash
./.venv/bin/python scripts/tune_leader_ignition_selector.py \
  --progress-every 10000 \
  --out results/Data/leader_ignition_backtest/leader_ignition_selector_tuning.csv
```

Search space:

- Entry type: `ignition_immediate`, `pullback_reclaim`
- Minimum score: `560` to `700`
- Minimum volume ratio: `1.5x` to `8x`
- Minimum range position: `0.55` to `0.92`
- Maximum pullback: `0.1%` to `0.8%`
- Maximum open move: `1.2%` to `2.8%`
- Latest signal time: `09:45` to `13:30`

Result:

- Full trade history scanned: `116,171` baseline trades.
- Selector rows surviving train-side gates and portfolio evaluation: `693`.
- Strict accepted selectors: `1`.
- Accepted selector:
  - `pullback_reclaim`
  - `score >= 700`
  - `vol_ratio >= 2.0`
  - `range_position >= 0.85`
  - `pullback <= 0.6%`
  - `open_move <= 2.4%`
  - `signal before 10:00`
- Train:
  - `101` active days,
  - `123` trades,
  - average daily return `0.000043%`,
  - positive day rate `52.48%`.
- Test:
  - `38` active days,
  - `56` trades,
  - average daily return `0.01205%`,
  - positive day rate `52.63%`,
  - worst day `-0.3625%`,
  - total P&L `915.80`.

Decision:

- This is **not enough edge** for live trading or automation.
- The only accepted selector is basically break-even and too small to trust
  after slippage, taxes, API/fill issues, and live-market differences.
- Do not promote leader ignition to Discord buys or real orders.
- Continue using the leader lane only as shadow/audit evidence.

Next useful research:

- Add features that are not present in 5-minute candle-only replay:
  - live buyer/seller depth stability,
  - sector breadth/leader confirmation,
  - index regime,
  - first 15-minute relative strength rank,
  - pullback entry price tests like `alert -0.5%`, `alert -1.0%`, then reclaim.
- Re-run selector tuning only after new features are added.

Verification:

- `./.venv/bin/python -m pytest tests/test_leader_ignition_tuner.py tests/test_leader_ignition_backtest.py -q`
  passed: `6 passed`.
- `./.venv/bin/python -m py_compile scripts/tune_leader_ignition_selector.py tests/test_leader_ignition_tuner.py`
  passed.

## 2026-07-03 - Leader Pullback Entry Matrix

Purpose:

- Test the recurring live observation: alerts often drop after firing, so maybe
  buying below alert price works better than buying immediately.
- Replays the same loose leader ignition signal, then places lower limit entries
  after the signal:
  - `alert -0.5%`
  - `alert -1.0%`
  - `alert -1.5%`
- Each limit order gets `45` minutes to fill.
- Exit matrix:
  - targets: `0.5%`, `0.8%`, `1.0%`
  - stops: `0.5%`, `1.0%`
  - target basis: from actual entry price, or from original signal price.

Files:

- Script: `scripts/backtest_leader_pullback_entries.py`
- Unit tests: `tests/test_leader_pullback_entries.py`
- Outputs:
  - `results/Data/leader_ignition_backtest/leader_pullback_entries_event_summary.csv`
  - `results/Data/leader_ignition_backtest/leader_pullback_entries_portfolio_summary.csv`
  - `results/Data/leader_ignition_backtest/leader_pullback_entries_daily.csv`

Command:

```bash
./.venv/bin/python scripts/backtest_leader_pullback_entries.py \
  --progress-every 100 \
  --out-prefix results/Data/leader_ignition_backtest/leader_pullback_entries
```

Full-cache result:

- Signals evaluated per variant: `65,202`
- Variants tested: `36`
- Filled variant trades: `501,528`
- `alert -0.5%` fill rate: `43.14%`
- `alert -1.0%` fill rate: `15.42%`
- `alert -1.5%` fill rate: `5.54%`

Best event-level variant:

- `alert -0.5%`, target `+1.0%` from signal price, stop `-0.5%`.
- Trades: `28,131`
- Win rate: `26.41%`
- Average trade return: `-0.1803%`

Best portfolio-level variant:

- `alert -0.5%`, target `+1.0%` from signal price, stop `-0.5%`,
  `1.5%` daily goal row.
- Active days: `493`
- Trades: `1,968`
- Positive day rate: `27.79%`
- Average daily return: `-0.1560%`
- Total P&L: `-153,803.80`

Decision:

- Buying below alert price **reduces the loss versus immediate entry**, but it
  does not create a profitable edge.
- Baseline immediate leader ignition was about `-0.1912%` average daily return;
  best pullback matrix result improved to `-0.1560%`, still clearly negative.
- Do not automate this pullback-entry idea.
- Keep it as evidence that "wait for lower price" is not sufficient by itself;
  we still need better stock selection before entry logic matters.

Verification:

- `./.venv/bin/python -m pytest tests/test_leader_pullback_entries.py tests/test_leader_ignition_backtest.py -q`
  passed: `6 passed`.
- `./.venv/bin/python -m py_compile scripts/backtest_leader_pullback_entries.py tests/test_leader_pullback_entries.py`
  passed.

## 2026-07-03 - Leader Candidate Context Selection

Purpose:

- Test whether the problem is simply "too many alerts" and whether selecting
  only the best alert inside each date/minute batch helps.
- Adds fast candidate-batch context:
  - score rank inside the same signal minute,
  - open-move rank inside the same signal minute,
  - volume-ratio rank inside the same signal minute,
  - same-sector candidate count,
  - sector average score/change among candidates.
- Also added a heavier full-universe context feature builder for later research:
  `scripts/enrich_leader_context_features.py`.

Files:

- Candidate context tuner: `scripts/tune_leader_candidate_context.py`
- Full-universe context enrichment utility: `scripts/enrich_leader_context_features.py`
- Tests:
  - `tests/test_leader_candidate_context.py`
  - `tests/test_leader_context_features.py`
- Output:
  - `results/Data/leader_ignition_backtest/leader_candidate_context_tuning.csv`

Command:

```bash
./.venv/bin/python scripts/tune_leader_candidate_context.py \
  --portfolio-eval-limit 150 \
  --progress-every 10000 \
  --out results/Data/leader_ignition_backtest/leader_candidate_context_tuning.csv
```

Result:

- Baseline leader trades scanned: `116,171`.
- Shortlisted candidate-context selectors portfolio-tested: `150`.
- Accepted selectors: `0`.
- Positive test-average selectors: `0`.
- Positive train+test selectors: `0`.

Best ranked selector:

- `pullback_reclaim`
- `score >= 700`
- `sector candidate count >= 2`
- `signal before 09:45`
- Train average daily return: `-0.043203%`
- Test average daily return: `-0.000278%`
- Test positive day rate: `43.75%`
- Test P&L: `-26.70`

Decision:

- Selecting the top-ranked alert within the same alert batch is **not enough**.
- Sector clustering among current leader candidates is also **not enough**.
- This reinforces the core conclusion: the current leader signal family is not
  selecting the right stocks, even before entry/exit tuning.
- Do not promote leader ignition, pullback entry, or candidate-rank filtering to
  Discord buys or real orders.

Next useful step:

- Stop tuning this same 5-minute candle signal family.
- The next useful feature class must come from data not captured in this replay:
  - live order-book stability over multiple ticks,
  - real-time sector breadth across the full live universe,
  - index regime at alert time,
  - actual top-gainer list comparison/missed-runner labeling,
  - broker-fill and spread quality.

Verification:

- `./.venv/bin/python -m pytest tests/test_leader_candidate_context.py tests/test_leader_context_features.py -q`
  passed: `5 passed`.
- `./.venv/bin/python -m py_compile scripts/tune_leader_candidate_context.py tests/test_leader_candidate_context.py`
  passed.

## 2026-07-03 - Live Feature Recorder For Missed-Runner Evidence

Purpose:

- The leader ignition backtests proved the replayable 5-minute candle signal is
  not profitable enough.
- The next useful evidence must come from live-only data that historical candles
  do not contain:
  - stable buyer/seller depth across multiple ticks,
  - spread/fill quality,
  - sector breadth across the full live universe,
  - whether a stock became a top gainer without any alert lane catching it.

Architecture:

- Added `pkscreener/classes/LiveFeatureRecorder.py`.
- Integrated it into `SignalMonitorRunner` as a shadow recorder only.
- Enabled by default in `scripts/start_live_assistant.sh`:
  - `SIGNALMONITOR_ENABLE_LIVE_FEATURE_RECORDER=1`
  - `SIGNALMONITOR_LIVE_FEATURE_INTERVAL_SECONDS=60`
  - `SIGNALMONITOR_LIVE_FEATURE_TOP_GAINER_PCT=5.0`
  - `SIGNALMONITOR_LIVE_FEATURE_MAX_ROWS_PER_CYCLE=200`
- Output path:
  `results/Data/live_feature_recorder/live_features_YYYY-MM-DD.csv`

What It Records:

- Price/open/prev-close/day-high context.
- Day change %, open change %, pullback from day high.
- Volume and traded value.
- Total buy/sell depth, B/S ratio, rolling B/S median/min window.
- `OrderBookStable` when B/S stays above the configured floor for multiple
  samples.
- Best bid/ask and spread bps from Kite depth.
- Sector count, sector average change, sector breadth, sector top-2/top-5
  counts.
- `MissedRunnerCandidate` when a stock is already a top gainer but was not
  surfaced by an alert lane today.

Safety:

- This does not create Discord buy alerts.
- This does not feed goal-mode alerts.
- This does not call the order manager or autotrade.
- Anomaly Grade-A/B logic and Grade-B automation logic were not changed.

Verification:

- `./.venv/bin/python -m pytest tests/test_live_feature_recorder.py -q`
  passed: `4 passed`.
- `./.venv/bin/python -m pytest tests/test_signal_monitor_discord_message.py tests/test_live_feature_recorder.py -q`
  passed: `45 passed`.
- `./.venv/bin/python -m pytest tests/test_leader_ignition_backtest.py tests/test_leader_ignition_tuner.py tests/test_leader_pullback_entries.py tests/test_leader_candidate_context.py tests/test_leader_context_features.py tests/test_live_feature_recorder.py -q`
  passed: `18 passed`.
- `./.venv/bin/python -m py_compile pkscreener/classes/LiveFeatureRecorder.py pkscreener/classes/SignalMonitor.py tests/test_live_feature_recorder.py`
  passed.

Decision:

- Keep this as live research infrastructure.
- After one or more live market sessions, use this CSV to answer the real
  question: which missed runners had stable depth, tight spreads, sector
  support, and low pullback before they continued?
- Do not promote any new buy lane from this until the recorded live data shows
  a positive, repeatable edge after costs.

## 2026-07-05 - Weekend Alert / Heartbeat Spam Fix

Incident:

- Discord showed `Intraday Goal Scanner Alert` and repeated heartbeat messages
  on `2026-07-05`.
- `2026-07-05` is a Sunday, so those alerts were **not buyable live-market
  signals**.
- The listed Grade-A names were generated from stale/cache/scanner state while
  the code only checked time-of-day, not whether the exchange was actually open.

Root Cause:

- `SignalMonitorRunner._is_regular_market_hours()` checked only `09:15-15:30`
  IST and did not reject weekends or exchange holidays.
- Heartbeat throttling used only process memory (`_last_discord_heartbeat_ts`).
  If the monitor runner was recreated each cycle, the throttle reset and Discord
  received a heartbeat every cycle.
- `scripts/daily_prewarm.sh`, `scripts/start_live_assistant.sh`, and
  `scripts/watchdog.sh` did not independently skip non-trading days.

Fix:

- `SignalMonitorRunner._is_regular_market_hours()` now rejects:
  - Saturday/Sunday,
  - dates reported by `PKDateUtilities.isHoliday()`.
- Heartbeat throttle state is now persisted under:
  `results/Data/signalmonitor_<universe>_heartbeat_state.json`
- Live assistant, daily prewarm, and watchdog scripts now skip non-trading days
  unless explicitly overridden:
  - `ASSISTANT_ALLOW_NON_TRADING_DAY=1`
  - `PREWARM_ALLOW_NON_TRADING_DAY=1`
  - `WATCHDOG_ALLOW_NON_TRADING_DAY=1`

Decision:

- Treat the `2026-07-05` alerts as invalid/no-trade signals.
- No strategy conclusion should be drawn from those Sunday messages.

Verification:

- `./.venv/bin/python -m pytest tests/test_signal_monitor_discord_message.py tests/test_live_feature_recorder.py -q`
  passed: `48 passed`.
- `./.venv/bin/python -m py_compile pkscreener/classes/SignalMonitor.py`
  passed.
- `bash -n scripts/start_live_assistant.sh scripts/daily_prewarm.sh scripts/watchdog.sh`
  passed.

## 2026-07-05 - Live Feature Outcome Evaluator

Purpose:

- The live feature recorder captures missed runners, stable order-book depth,
  spread quality, and sector breadth during the day.
- This evaluator is the post-market judge: it labels what happened after each
  recorded live-feature row using local 5-minute candles.

File:

- `scripts/evaluate_live_feature_outcomes.py`

Default Inputs/Outputs:

- Input:
  `results/Data/live_feature_recorder/live_features_*.csv`
- Candle directory:
  `results/Data/kite_history_cache/5minute`
- Outputs:
  - `results/Data/live_feature_recorder/live_feature_outcomes_rows.csv`
  - `results/Data/live_feature_recorder/live_feature_outcomes_summary.csv`

What It Labels:

- Bars available after the recorded moment.
- Max favorable move after the row.
- Max adverse move after the row.
- EOD return after the row.
- Target-before-stop outcomes for:
  - targets: `0.5%`, `0.7%`, `1.0%`, `1.5%`, `3.0%`
  - stops: `0.5%`, `1.0%`, `2.5%`
- Feature buckets:
  - `missed_stable_tight_sector`
  - `missed_stable_depth`
  - `missed_runner`
  - `alerted_today`
  - `stable_order_book`
  - `other`

Command:

```bash
./.venv/bin/python scripts/evaluate_live_feature_outcomes.py
```

Decision:

- This is not a strategy yet.
- This is the scoreboard that tells us which live microstructure patterns
  actually worked after costs and which ones were just noise.
- No new buy lane should be promoted until this summary shows repeated positive
  outcomes over multiple real market sessions.

Verification:

- `./.venv/bin/python -m pytest tests/test_live_feature_outcome_evaluator.py tests/test_live_feature_recorder.py tests/test_signal_monitor_discord_message.py -q`
  passed: `51 passed`.
- `./.venv/bin/python -m py_compile scripts/evaluate_live_feature_outcomes.py tests/test_live_feature_outcome_evaluator.py`
  passed.

## 2026-07-05 - Repo / Runtime / Storage Map

Problem:

- We were maintaining both `intraday-trading` and `tr-assistant`, and it was
  unclear which repo was actually live.
- This created risk that fixes could be made in the wrong repo, or that a repo
  could be deleted while still holding Kite/session/runtime state.

Current Finding:

- `/home/charan_sai/tr-assistant` is the production/runtime repo today.
- `/home/charan_sai/intraday-trading` is the research/development repo today.
- The machine has about `23G` free on a `48G` root disk (`54%` used).
- The configured GCS bucket is active for archive/cleanup, but runtime services
  still depend on local `tr-assistant/results/Data` state.

Documentation:

- Added `docs/repo_runtime_storage_map.md`.

Decision:

- Do not delete either repo yet.
- Treat `tr-assistant` as production and `intraday-trading` as research until a
  tested migration plan updates systemd, timers, cron, env, tokens, and state
  paths.

## 2026-07-05 - Lane Decision Journal Started

Problem:

- We were arguing about whether top-gainer/radar/leader decisions were good or
  late, but the system mostly recorded alerts, not every pass/reject decision.
- Without rejected candidates and the live tape context at that moment, we
  cannot prove which filters are helping and which filters are making us miss
  runners.

Change:

- Extended `LiveFeatureRecorder` with `record_decisions()`.
- New output:
  `results/Data/live_feature_recorder/lane_decisions_YYYY-MM-DD.csv`
- It records lane, stage, symbol, pass/reject/watch decision, reason, price,
  day change, pullback, score, volume ratio, B/S ratio, live day high, buy/sell
  quantities, best bid/ask, spread, checks, and why text.
- Connected it to:
  - `top_gainer_radar`
  - `leader_ignition`

Decision:

- This is still shadow/research only.
- It does not create new alerts, orders, auto-buy rules, or auto-sell rules.
- Next useful step is to label these lane decisions against forward outcomes,
  so we can see which rejected/pass buckets later hit `+0.7%`, `+1.0%`,
  `+1.5%`, or stopped first.

Verification:

- `./.venv/bin/python -m pytest tests/test_live_feature_recorder.py tests/test_signal_monitor_discord_message.py -q`
  passed: `49 passed`.
- `./.venv/bin/python -m py_compile pkscreener/classes/LiveFeatureRecorder.py pkscreener/classes/SignalMonitor.py tests/test_live_feature_recorder.py tests/test_signal_monitor_discord_message.py`
  passed.

## 2026-07-05 - Lane Decision Outcome Labels

Problem:

- `lane_decisions_YYYY-MM-DD.csv` was recording pass/reject/watch evidence, but
  we still needed the forward outcome label to know whether those decisions
  were actually good.

Change:

- Extended `scripts/evaluate_live_feature_outcomes.py` with lane-decision mode.
- Existing live-feature mode remains the default.
- New inputs/outputs:
  - input: `results/Data/live_feature_recorder/lane_decisions_*.csv`
  - output rows:
    `results/Data/live_feature_recorder/lane_decision_outcomes_rows.csv`
  - output summary:
    `results/Data/live_feature_recorder/lane_decision_outcomes_summary.csv`
- The summary now separates buckets by:
  `Lane | Stage | Decision | Reason`

Command:

```bash
./.venv/bin/python scripts/evaluate_live_feature_outcomes.py --mode decisions
```

Decision:

- This lets us compare, for example:
  - `top_gainer_radar | pass | radar candidate passed`
  - `top_gainer_radar | reject | strength below radar floor`
  - `leader_ignition | ignition_watch | pass | early leader watch`
- The useful question is no longer just "did the alert work?" It is also:
  "which rejects would have worked, and which pass reasons are noise?"

Verification:

- `./.venv/bin/python -m pytest tests/test_live_feature_outcome_evaluator.py tests/test_live_feature_recorder.py tests/test_signal_monitor_discord_message.py -q`
  passed: `54 passed`.
- `./.venv/bin/python -m py_compile scripts/evaluate_live_feature_outcomes.py tests/test_live_feature_outcome_evaluator.py`
  passed.

## 2026-07-13 - Executable Live Evidence and Readiness Gate

Safety state:

- Real auto-buy and auto-sell remain disabled.
- Research and ranked confirmation remain paper-only.
- No result below is permission to create a broker order.

Infrastructure completed:

- Added an automated post-market evidence job at `15:35 IST` on weekdays.
- Added a resumable multi-day Kite minute-candle bootstrap.
- Added a causal readiness report that:
  - freezes the first eligible signal minute before looking at outcomes,
  - includes cash/no-trade sessions,
  - uses next-minute open, adverse slippage, fees, and stop-first same-bar handling,
  - requires coverage, sample size, positive confidence bounds, positive time
    blocks, and bounded drawdown,
  - reports the `₹4,00,000` capital / `₹10,000` goal as a required `2.5%`
    daily return,
  - can only label a result as a shadow candidate,
  - always leaves evidence acceptance, paper promotion, and real orders false.
- Fixed nested evidence discovery for paths such as `eod/*/...`.
- Fixed persisted string booleans so `"False"` cannot be interpreted as true.
- Fixed a causal-window bug: states recorded at `09:14` are now excluded before
  first-state deduplication, so stale pre-open state cannot replace the real
  in-market onset.

Five-session live bootstrap:

- Evidence dates: `2026-07-06` through `2026-07-10`.
- Symbols requested: `1,928`.
- Frozen in-market feature rows: `5,616`.
- Full 60-minute eligible rows: `5,252`.
- Executable evaluated rows: `3,818`.
- Coverage: `72.70%`; the required `95%` gate failed.
- All-state cost-adjusted 60-minute returns:
  - `missed_runner`: `-0.8311%` average trade and `-0.9153%` equal-day average.
  - `missed_stable_depth`: `-1.1295%` average trade.
  - `missed_stable_tight_sector`: `-1.0008%` average trade.
  - `alerted_today`: `-0.1765%` average trade.
- No candidate bucket had a positive equal-day result across the five sessions.
- The best first-batch result was
  `missed_stable_tight_sector / forward_horizon` at `+0.0626%` before the extra
  stress charge, but `-0.0374%` after stress, only seven evaluated candidates,
  `87.5%` coverage, and a negative confidence bound.
- Shadow candidates: `0`.
- `₹10,000` goal-evidence passes: `0`.

Outputs:

- `results/Data/live_feature_recorder/bootstrap/2026-07-06_2026-07-10/status.json`
- `results/Data/live_feature_recorder/bootstrap/2026-07-06_2026-07-10/live_features_rows.csv`
- `results/Data/live_feature_recorder/bootstrap/2026-07-06_2026-07-10/live_features_summary.csv`
- `results/Data/live_feature_recorder/bootstrap/2026-07-06_2026-07-10/evidence_readiness.csv`
- Accumulated daily report:
  `results/Data/live_feature_recorder/evidence_readiness.csv`

Decision:

- Reject immediate buying of `missed_runner`, stable-depth, and tight-sector
  states. The evidence confirms that these labels mostly identify exhausted
  moves rather than early continuation entries.
- Do not tune thresholds around the seven-row positive-looking slice.
- Keep collecting full live decision evidence automatically after each market
  session.

## 2026-07-13 - Two-Stage Leader State Machine Audit

Purpose:

- Test a genuinely different architecture instead of adding more score to the
  same immediate top-gainer buy.
- A completed five-minute bar creates only a leader watch.
- A later completed bar must confirm either:
  - pullback then continuation for a long, or
  - failed breakout below VWAP and a recent low for an intraday short.
- Entry is the following bar open; same-bar ambiguity favors the stop.

One-year candidate set:

- Candle range: `2024-05-30` through `2026-05-29`.
- Market sessions: `493`.
- Symbols: `498`.
- Long continuation candidates: `5,854`.
- Failed-breakout short candidates: `1,136`.

Results after `0.20%` costs:

- Long continuation was rejected. Every tested target/stop contract was
  negative in train, validation, and final periods.
- The failed-breakout short was closer but still not robust:
  - fixed `3.0%` target / `1.5%` stop,
  - train `+0.0806%` average daily return,
  - validation `+0.0390%`,
  - final `+0.0920%`,
  - one pre-final time block `-0.1116%`,
  - the extra `0.10%` cost stress makes train and validation negative.
- Adding causal Nifty move and watch-to-confirmation regime features did not
  repair it. The walk-forward ML selector chose a `0.30` threshold but produced
  final `-0.1888%` and stress-final `-0.2278%` per day.
- Transparent one-variable filters for failure depth, VWAP distance, volume,
  candle body, Nifty move, Nifty weakening, relative strength, score, and time
  also produced no positive cost-stressed train/validation/time-block rule.

Decision:

- Reject the long continuation lane.
- Keep the failed-breakout short as a research hypothesis only; do not enable
  alerts, paper promotion, MIS shorts, or real orders from this result.
- ML is not the missing answer here. It amplified a regime-specific historical
  pattern and failed the untouched final block.

Verification:

- Live evidence focused suite: `32 passed`.
- Market-window correction suite: `29 passed`.
- Leader state machine, causal exit grid, and walk-forward ranker: `15 passed`.

## 2026-07-17 - Ranked Forward Tape Rejection and Automatic Replay Gate

Forward-live result:

- Ranked Grade-B paper trades from `2026-07-13` through `2026-07-17`: `7`.
- Winners: `0`; losses: `7`.
- Net simulated P&L after configured costs: `-₹3,136.66`.
- The current sustained-confirmation entry is rejected for promotion. Repeated
  confirmation did not solve the original problem: brief strength was still
  followed by reversal, and no trade reached its target.
- Real broker-order flags remain disabled.

Replay architecture:

- Exact recorded candidate ticks are replayed in memory; offline replay no
  longer writes production state on every historical tick.
- Replay carries the latest tick for every observed symbol, matching the full
  production snapshot contract rather than passing a sparse one-symbol batch.
- The report explicitly calls this a counterfactual current-configuration
  replay. Historical production trades can differ when the code/configuration
  changed during the collection window. In particular, the actual ledger has
  seven losses, while the current configuration replay excludes the early
  `DMART` trade that preceded the negative-day entry gate.
- A fixed registry of nine configurations is tested. The registry is small and
  predeclared to reduce threshold fishing.
- Results use chronological train/validation segments, cash/no-trade days,
  configured fees/slippage, an additional `0.10%` stress cost, a one-sided
  confidence bound, profit factor, positive-day rate, and drawdown.
- Shadow eligibility requires at least `20` complete days, `30` closed trades,
  `14` train days, `5` validation days, positive stressed train and validation,
  a positive lower confidence bound, profit factor at least `1.20`, and at
  least `55%` positive days.
- The `₹10,000/day` gate additionally requires stressed average daily P&L of
  at least `₹10,000` and at least `50%` goal-hit sessions.
- Real orders are always false in this research artifact.

First completed replay (`2026-07-13` through `2026-07-16`):

- All nine fixed configurations had negative stressed average daily P&L.
- Current baseline: five trades, zero winners, `-₹634/day` stressed average,
  and `-₹773/day` lower confidence bound.
- Least-bad fixed variant (`patient_strong_tight_exit`): four trades, one
  winner, `-₹235/day` stressed average, `-₹390/day` lower confidence bound,
  and profit factor `0.08`.
- Shadow candidates: `0`; `₹10,000/day` passes: `0`.

Automation and outputs:

- The weekday `15:35 IST` live-evidence job now also runs the accumulated
  ranked replay after the tape is complete.
- Readiness CSV:
  `results/Data/ranked_confirmation_replay/readiness.csv`.
- Machine summary:
  `results/Data/ranked_confirmation_replay/readiness.json`.
- Dashboard: `http://127.0.0.1:8501` shows the best fixed configuration,
  complete days, stressed daily P&L, lower bound, and promotion verdict.

## 2026-07-17 - Live Leader Direction Tests Rejected

Purpose:

- Test two genuinely different uses of the live-only order-book archive rather
  than continuing to tune the already rejected static leader signal.
- Use only completed sessions through `2026-07-16`; the open `2026-07-17`
  session was excluded from the fixed audit.
- Preserve causal execution: consecutive snapshots form the signal, entry is
  the next complete minute open, slippage is adverse, fees and an additional
  `0.10%` stress cost apply, and ambiguous candles hit the stop first.

Completed evidence:

- Input: `625,849` live feature rows across `9` completed sessions.
- Failed-leader short/fade audit:
  - four fixed failure rules and four fixed target/stop contracts,
  - all `16` combinations had negative stressed average daily P&L,
  - least-bad result was `failure_pb1_5_bs1 / 0.7% target / 0.5% stop`,
    averaging `-Rs 222/day` with a `-Rs 525/day` lower confidence bound.
- Temporal long-ignition audit:
  - requires two prior live snapshots before testing acceleration, fresh
    buyer-depth surge, stable depth, spread, liquidity, and proximity to high,
  - all `16` fixed rule/contract combinations were negative,
  - least-bad result averaged `-Rs 483/day` with a `-Rs 798/day` lower
    confidence bound and only `11.11%` positive sessions.
- Shadow candidates: `0`; `Rs 10,000/day` evidence passes: `0`.

Decision:

- Reject both immediate temporal continuation and confirmed failed-leader fade.
- Do not create a Discord lane, paper entry lane, MIS short lane, or broker
  order rule from either result.
- The evidence indicates that choosing long versus short after a stock is
  already a visible leader does not solve candidate quality.
- Keep collecting and re-running both fixed registries after market close. The
  weekday evidence job now writes accumulated reports under:
  - `results/Data/live_runner_fade/`
  - `results/Data/live_microstructure_ignition/`
- Both status artifacts always set real orders to false.

## 2026-07-17 - Causal Event Shadow Timezone Repair

Incident:

- The shared event-family shadow recorder had produced `2,551` apparent event
  outcomes across July 13-16, but every row had `complete_session=false`.
- Persisted five-minute bars were timestamped around `02:20-09:50` while the
  event engine and completeness gate used IST session times.
- KiteTicker timestamps on this VM are UTC-naive strings. The recorder treated
  them as IST, shifting event windows by 5 hours 30 minutes and comparing the
  last UTC-like bar against an IST `15:25` requirement.
- The old event-shadow outcomes are invalid for strategy conclusions and remain
  only as forensic source files.

Fix:

- `IntradayEventShadowRecorder` now localizes naive Kite exchange/last-trade
  timestamps as UTC and converts them to naive IST before five-minute bucketing.
- Explicit fallback clocks remain interpreted as IST, preventing a second
  timezone shift.
- Auto-repair handles a same-day monitor restart that mixes old UTC-naive bars
  with new IST bars: only the pre-09:15 clock portion is shifted, while the
  already-correct afternoon portion is preserved.
- Outcome evaluation now builds a per-symbol bar lookup instead of rescanning
  the full bar table for every event.
- The reliable completeness boundary is `15:20`; the active `15:25` bucket is
  not guaranteed to be persisted without a later tick.
- Added `scripts/rebuild_intraday_event_shadow_evidence.py`:
  - source archives are never overwritten,
  - UTC-naive bars are shifted to IST,
  - stale pre-market bars are removed before calculating day open/VWAP,
  - events are regenerated with the shared causal engine,
  - cross-sectional ranks are computed within the actual signal timestamp,
  - conservative long and short outcomes are rebuilt separately.
- The weekday EOD job now rebuilds the current event-shadow day and refreshes
  the paper-only model/readiness report automatically.
- The shadow trainer now learns from the full complete candidate cross-section;
  it no longer trains only on the already-selected one-row-per-day baseline.

Recovered July 13-16 evidence:

- Complete sessions: `4`.
- Repaired bars: roughly `178k-182k` per session across about `2,900` symbols.
- Regenerated events: `9,163` total.
- Complete long outcomes: `9,056`; complete short outcomes: `9,056`.
- Broad event buying remained poor: `-0.3834%` average net return per event.
- Broad event fading averaged `+0.1454%`, but the frozen first-batch fade lost
  all four sessions and is not a strategy candidate.
- Frozen first-signal-batch top-one long result:
  - `CCCL +1.35%`, `IFGLEXPOR +1.35%`, `BBTCL -1.15%`, `DJML +1.35%`,
  - embedded-cost average `+0.725%`,
  - after an additional `0.10%` stress cost: `+0.625%`, about `Rs 2,500/day`
    at `Rs 4,00,000`,
  - positive sessions: `75%`,
  - one-sided lower confidence bound: `-0.4031%`,
  - `Rs 10,000` hit sessions: `0%`.
- Expanding to top two reduced average return to `+0.10%`; top three and larger
  baskets were negative. More orders again diluted the strongest candidate.

Decision:

- Keep the frozen top-one causal event heuristic as forward evidence only.
- Minimum sample and performance gates fail; paper promotion and real orders
  remain false.
- The model still requires at least `100` complete sessions. Current status is
  `insufficient_forward_evidence` with `4` sessions and `9,056` candidates.
- Dashboard now exposes the rebuilt baseline average, P&L, confidence bound,
  positive-session rate, and explicit no-promotion/no-order verdict.

## 2026-07-17 - Historical ML to Repaired Live Holdout

Purpose:

- Test whether the pre-existing historical causal event model actually
  transfers to repaired live market evidence.
- Prevent another circular backtest by freezing the model family, label,
  feature set, threshold, lookback, execution rule, target/stop contract, and
  costs before inspecting July outcomes.

Contract:

- Historical training boundary: `2025-10-01` through `2026-05-29`, the latest
  `160` complete historical sessions and `65,912` candidate events.
- Live holdout boundary: July 13-16, `2026`; no date overlap is permitted.
- Five event-family `HistGradientBoostingClassifier` models use the `39`
  signal-close-or-earlier features genuinely present in both datasets.
- Newer cross-sectional features are excluded because the historical artifact
  did not contain them; filling historical rows with zeros would create a
  false schema match.
- Label: target before stop. Threshold: frozen at `0.40`, selected before the
  live holdout. Execution: highest model score in the first eligible signal
  timestamp only, one trade per session.
- Live `net_return_pct` includes the outcome evaluator's `0.15%` cost; the
  transfer report subtracts another `0.10%` stress.

Strict future result:

- Scored live candidates: `9,056` across `4` complete sessions.
- Picks: `BRNL +1.25%`, `HEADSUP -1.25%`, `RUCHIRA -1.25%`, `DJML +1.25%`
  after stress.
- Average and total stressed P&L at `Rs 4,00,000`: `Rs 0`.
- Positive sessions: `50%`; `Rs 10,000` hit sessions: `0%`.
- Two-sided 95% lower confidence bound: `-2.2967%`.
- Winner mean model score was only `0.110` versus `0.104` for losers. The
  historical model has almost no live ranking separation.
- Every diagnostic score cutoff from `0.20` through `0.45` had negative mean
  return over the full live candidate set. These cutoffs are diagnostics only;
  none was selected or promoted from the four-day holdout.

Decision and automation:

- Historical ML transfer is rejected for promotion. Paper promotion and real
  orders remain false.
- `scripts/evaluate_historical_event_model_live_holdout.py` enforces the date
  boundary, shared feature schema, first-batch execution, cash days, stress,
  confidence gate, and explicit no-order result.
- A frozen model bundle is stored outside Git under
  `results/Data/research_models/historical_event_family_target.joblib` so the
  EOD job scores new repaired sessions without retraining or moving rules.
- The weekday EOD evidence job refreshes the strict transfer report when that
  bundle exists. The dashboard shows the transfer result separately from the
  simpler frozen event heuristic.
- A future model must beat both this rejected historical transfer and the
  frozen heuristic on untouched forward sessions before any paper lane is
  promoted. Real-order permission is not part of this evaluator.

## 2026-07-17 - Frozen Event Exit Contract Registry

Question:

- Determine whether the fixed `1.5%` target / `1.0%` stop is hiding a better
  exit shape, and whether one or two simultaneous positions can approach the
  `Rs 10,000/day` objective without changing candidate selection.

Frozen registry and evidence boundary:

- `48` contracts combine targets `0.8, 1.0, 1.5, 2.5, 3.0, 5.0%`, stops
  `0.5, 1.0, 1.5, 2.5%`, and one or two first-batch positions.
- Candidate ranking remains the shared causal event score. Entry remains the
  next complete five-minute bar open. Same-bar target/stop ambiguity loses at
  the stop. Embedded cost is `0.15%` plus `0.10%` extra stress.
- The registry and rules are frozen through `2026-07-16`.
- July 13-16 results are exploratory only and can never promote a contract.
  Forward evidence begins July 17 and requires at least `20` sessions plus a
  positive 95% lower confidence bound.

Exploratory result:

- No contract averaged `Rs 10,000/day`.
- Highest mean: `t5_s0.5_n1`, about `Rs 3,767/day`, but only `50%` positive
  sessions and a `-3.2084%` lower confidence bound.
- The smoothest positive one-position result was `t0.8_s2.5_n1`, about
  `Rs 2,200/day` with all four sessions positive, but this is a tiny in-sample
  observation and still far below the goal.
- `t1.5_s0.5_n2` was positive in all four sessions but averaged only
  `Rs 1,000/day`; adding the second position diluted returns again.
- Ten wide-target configurations hit `Rs 10,000` on one of four sessions, but
  none did so consistently.

Decision and automation:

- No contract is selected from the exploratory period. Paper candidates: `0`;
  real orders: false.
- `scripts/assess_event_shadow_exit_contracts.py` rebuilds every contract from
  repaired bars and events, stores trade/day/readiness evidence, and keeps the
  exploratory and forward partitions separate.
- The weekday EOD evidence job refreshes the frozen registry after rebuilding
  the current session. The dashboard shows forward-session count, current
  forward evidence, and the non-selectable exploratory reference.

## 2026-07-17 - Frozen Same-Timestamp Candidate Selectors

Purpose:

- Test early leader selection directly, without waiting for a stock to become
  a late top gainer.
- Compare every candidate only with stocks available in the same first signal
  timestamp, preserving causal cross-sectional execution.

Frozen registry:

- Six selectors: shared causal `event_score`, move rank, volume rank, traded
  value rank, a balanced move/volume/relative-strength/value/pullback score,
  and an event-plus-liquidity score.
- Each selector is tracked with the current `1.5%` target and a goal-sized
  `3.0%` target. Both use a `1.0%` stop, `0.15%` embedded cost, and `0.10%`
  extra stress.
- Twelve configurations are frozen through July 16. July 13-16 is exploratory;
  forward promotion evidence starts July 17 and requires at least 20 sessions
  with a positive 95% lower confidence bound.

Exploratory result:

- Existing causal event score remained best: about `Rs 2,500/day` with the
  `1.5%` target, still below the objective and statistically inconclusive.
- Its goal-sized `3%` target averaged only about `Rs 767/day` and hit
  `Rs 10,000` on one of four sessions.
- Highest move, traded value, balanced leader, and event-plus-liquidity
  selectors were negative. Highest volume averaged `Rs 0/day`.
- This rejects the naive idea that selecting the largest same-time move or
  volume rank is enough to catch a profitable early top gainer.

Decision and automation:

- No selector is promoted; paper candidates: `0`; real orders: false.
- `scripts/assess_event_shadow_candidate_selectors.py` records immutable
  per-trade, per-day, and readiness evidence for all twelve configurations.
- The weekday EOD evidence job refreshes the selector registry after rebuilding
  each repaired live session. Future sessions, not these four exploratory
  dates, determine whether any selector survives.

## 2026-07-17 - Historical Selector and Exit Joint Replay

Question:

- Determine whether jointly choosing an early same-timestamp leader selector,
  target, stop, and position count rescues the existing event family over two
  years, instead of tuning each component against four July sessions.

Honest replay contract:

- Replayed `195,826` causal candidates against `505` exact five-minute symbol
  files with `0` unmatched candidates.
- Selection uses only `206` training and `103` validation sessions, including
  four sequential pre-final stability blocks. The final `104` sessions remain
  untouched until one configuration is frozen.
- Entry is the next complete five-minute bar open. Same-bar target/stop
  ambiguity loses at the stop. Results include `0.15%` trading cost and
  `0.10%` extra stress, cash days, symbol deduplication, and equal capital
  across one or two simultaneous positions.
- Six selectors were crossed with `1.5%` and `3.0%` targets, a `1.0%` stop,
  and one or two positions.

Result:

- Every one of the `24` configurations had negative combined pre-final
  evidence. The least-bad frozen choice was
  `volume_leader_t1.5_s1_n1`.
- That choice averaged about `Rs -1,121/day` in training, `Rs -182/day` in
  validation, and `Rs -521/day` in the untouched final period.
- Final 95% lower confidence bound was `-0.3648%`; `Rs 10,000` final-day hit
  rate was `0%`.
- Accepted: false. Paper promotion: false. Real orders: false.

Decision:

- Stop tuning selectors, targets, stops, or position counts inside this event
  family. The honest replay rejects the family rather than merely one exit.
- `scripts/walk_forward_event_selector_exit_registry.py` is the reproducible
  evidence generator. Its final-period data is never used for selection.

## 2026-07-17 - Broad Universe Research Data Architecture

Problem found:

- Live scanning covers approximately `2,990` NSE/BSE symbols, while the exact
  two-year five-minute research cache covers only `505` stocks. This mismatch
  can systematically exclude the smaller names that become intraday leaders.
- The current universe is still survivor-biased: it cannot restore delisted
  historical members. Results from the expanded cache must retain that warning.

Coverage plan:

- Current manifest: `2,990` live symbols, `2,955` mapped to cached Kite
  instruments, `35` stale/unmapped names, `505` existing exact-range files,
  and `2,450` mapped symbols needing fetch.
- Estimated work is `31,850` API calls in `30` deterministic batches of `100`.
  Estimated additional local storage is about `5.1 GB`; estimated total range
  size is about `6.2 GB`. The VM currently has about `18 GB` free.
- `scripts/plan_broad_universe_backfill.py` performs a broker-free audit and
  writes the stable manifest, summary, and pending commands.
- `scripts/kite_intraday_backfill.py` now supports stable `--batch-size` and
  `--batch-index` operation. `--limit 0` explicitly means the whole universe.
- `scripts/run_broad_universe_backfill_batch.sh` processes one missing batch
  after market, retries symbols whose latest audit recorded a fetch error,
  and syncs the exact range to the configured GCS archive bucket.
- The runtime invokes the lock-protected runner at 17:30, 18:30, and 19:30
  IST on weekdays. Overlapping invocations exit, so at most three complete
  batches run per evening without concurrent Kite downloads.
- Runtime was seeded from the dev repo's `505` exact-range files using hard
  links. Cache writes are atomic replacements, so any repaired runtime file
  safely detaches from its seed inode. This avoided about `1,015 MB` of
  duplicate storage and reduced pending runtime work to `2,410` symbols and
  approximately `31,330` Kite calls.
- Every scheduled batch now refreshes the manifest after fetching, writes an
  atomic `broad_universe_backfill_latest.json` with audit status counts, and
  sends a Discord completion or needs-attention message. Missing audits,
  fetch errors, planning failures, and bucket-sync failures all fail closed.

Research boundary:

- Broad history acquisition is data infrastructure, not a strategy promotion.
- Once coverage is large enough, the next experiment must define a genuinely
  new early-leader signature and freeze it before its untouched final period.
- Real broker orders remain disabled throughout acquisition, research, and
  paper validation.

## 2026-07-17 - Frozen Live Microstructure Selector

Why this is different:

- Historical candle-only ignition, fixed-time leader, pre-breakout, pullback,
  and leader-state families were already rejected. This selector uses data
  unavailable in those replays: persistent order-book depth, executable
  bid/ask spread, and same-time sector breadth from the full live tape.
- The audited rule is `missed_stable_executable_known_sector`: a current missed-runner
  state with stable buyer depth, a tight executable spread, and at least two
  sector leaders. The contract is the executable next-minute-open return over
  the following `60` minutes.

Evidence boundary:

- July 6-16 is selection-only and can never promote the rule.
- July 17 onward is untouched forward evidence. The rule and contract do not
  move when new outcomes arrive.
- A pre-forward audit found that the old `missed_stable_tight_sector` label
  treated zero bid and zero ask as a zero-width spread. Those rows were not
  executable. The classifier and readiness loader now require a positive bid,
  an ask above bid, and a measured spread in `(0, 20]` basis points. Legacy
  evidence is reclassified from its raw fields instead of trusting saved labels.
- A second audit found that unmapped stocks were all grouped into one
  `UNKNOWN` pseudo-sector, producing fake sector counts above `150`. Unknown
  sectors are now excluded from sector breadth, and the rule also requires a
  price of at least `Rs 50` and at least `Rs 3 crore` traded value.
- Fully corrected selection has `9` sessions, `5` trade sessions, and `5`
  first-batch candidates. It averages `-0.437892%`, about `Rs -1,752/day` on
  `Rs 4L`; its 95% lower bound is `-1.661181%` and block floor is
  `-1.046428%`.

Forward gate and automation:

- The selection gate now fails permanently for this hypothesis. Future rows
  remain forensic evidence and cannot rescue or promote a rule that failed its
  selection period.
- `scripts/assess_frozen_microstructure_selector.py` also requires at least `20`
  forward sessions, `15` trade sessions, and `30` candidates, plus positive
  raw and stressed confidence bounds and block floors.
- Even a statistically positive rule must average at least `Rs 10,000/day`
  after stress and hit that goal in at least `50%` of forward sessions.
- The weekday EOD job updates the frozen status and daily ledger. Current
  accepted status: false; paper promotion: false; real orders: false.

## 2026-07-17 - Early Pre-Runner Microstructure Audit

Architecture correction:

- The prior temporal ignition researcher allowed entries after moves as large
  as `8%`, selected up to three symbols per rule, and recomputed its final
  dates whenever a new session arrived. That was neither early nor frozen.
- The causal base is now restricted to `0.5%-3.0%` day change, 09:18-14:30,
  price at least `Rs 50`, turnover at least `Rs 3 crore`, pullback at most
  `0.35%`, a real bid below a real ask, and spread at most `20` basis points.
- Each rule gets only its earliest highest-ranked candidate per session.
  Selection is permanently July 6-16; July 17 onward cannot change it.

Honest result:

- All `16` acceleration/order-flow and target/stop combinations are negative
  after fees, slippage, extra stress, and cash days.
- The least-negative configuration has only `2` candidates and averages about
  `Rs -25/day`; its lower bound is about `Rs -102/day`.
- The nine-candidate `pure_accel` configurations average between roughly
  `Rs -88/day` and `Rs -179/day`, with negative lower bounds.
- No configuration freezes. Selection rejected: true; paper promotion: false;
  real orders: false.

Decision:

- Do not tune this small live sample into apparent profitability. Continue
  recording live microstructure, but move primary research to the scheduled
  broad-universe two-year history so the next family has enough independent
  sessions for an honest walk-forward test.

## 2026-07-17 - Cross-Sectional Rank Acceleration

New hypothesis:

- Test stocks rapidly improving their same-time market rank while still only
  `0.5%-3.0%` above the open. Features use the current completed scan and one
  prior completed scan for the same symbol. Entry remains the next bar.
- Three fixed emergence rules were crossed with existing 60-minute contracts.
  The earliest qualifying batch is selected and ranked without outcomes.
- Configuration selection uses training, validation, and four pre-final
  blocks. The final `105` sessions are untouched until selection is complete.

Result on the current 505-symbol cache:

- Long continuation failed every period. The selected configuration averaged
  about `Rs -444/day` in training, `Rs -609/day` in validation, and
  `Rs -754/day` in untouched final before the extra stress charge.
- An exact short replay was then run over `592` candidates from `305` symbol
  files with zero missing paths. Short entry includes adverse slippage and
  uses stop-first same-bar handling over a 60-minute horizon.
- Short mean reversion also failed every period: approximately `Rs -388/day`
  training, `Rs -500/day` validation, and `Rs -503/day` untouched final before
  extra stress.
- Both sides are rejected; paper promotion and real orders remain false.

Broad-universe continuation:

- `scripts/run_broad_rank_acceleration_checkpoint.sh` reruns the complete
  causal long and exact-short registry only at `1,000`, `2,000`, and `2,900`
  cached-symbol milestones. Each checkpoint is immutable and separately
  statused; no checkpoint can enable orders automatically.
- A checkpoint reaches the stated goal only when the pre-final selection gate
  passes and the stressed untouched final period has at least `30` trade days,
  a 95% lower confidence bound of at least `Rs 10,000/day`, at least `50%`
  `Rs 10,000` days, and a worst day above `-1.5%`. Passing that research gate
  still cannot enable paper promotion or real broker orders automatically.
- The `1,000`- and `2,000`-symbol checkpoints are explicitly diagnostic-only
  because their cached universes are partial and non-random. Historical goal
  evidence additionally requires at least `2,900` cached symbols and `98%` of
  the mapped universe. Even then, current live-universe membership has
  survivorship bias, so forward validation remains mandatory.
- Checkpoint candidate generation runs at low OS priority and caps its process
  pool at the VM CPU count, with a hard maximum of `16`, so research does not
  oversubscribe the always-on monitor host.
- Each checkpoint also audits denominator-based top-gainer recall: all liquid
  `5%` and `10%` intraday runners, how many crossed before 09:30, how many the
  fixed early pipeline detected before crossing, median lead time, and whether
  detected names subsequently hit the independent `+1.5%` target contract.
  This is diagnostic only and cannot promote a strategy.

Current 505-symbol baseline:

- `11,680` liquid 5% runner events were found; `2,015` had already crossed by
  09:30. Recall among the remaining catchable runners was `16.32%` by 09:30
  and only `34.14%` by 10:30. Detected events subsequently hit the independent
  60-minute `+1.5%` target about `53%-55%` of the time.
- `1,685` liquid 10% runner events were found; `149` had crossed by 09:30.
  Catchable recall was `9.70%` by 09:30 and `21.88%` by 10:30. Detected events
  had stronger `+1.5%` follow-through, roughly `61%-67%`.
- This proves two separate gaps: the eligibility pipeline misses most eventual
  leaders while they are early, and generic candidate ranking cannot reliably
  separate continuation from fade. The next frozen family targets rare
  eventual-runner classification rather than generic expected return.

Eventual-runner model contract:

- Labels require a liquid candidate to cross `+10%` strictly after its
  completed signal bar. Walk-forward training uses prior dates only and
  class-balanced, equal-day weights.
- The selected trade contract is frozen at 60 minutes, target `+1.5%`, stop
  `-1.0%`, with adverse entry slippage, modeled costs, and an extra stress
  charge. Thresholds, per-signal picks, and one/two unleveraged slots are
  selected from training, validation, and pre-final blocks only.
- Fixed-time replay now records exact conservative exit bars and timestamps.
  Capital can be reused only after an earlier exit bar, never within the same
  candle, and same-symbol re-entry is blocked for the day.
- The untouched final contract still requires the stressed 95% lower daily
  P&L bound to exceed `Rs 10,000`, at least 50% goal days, and controlled
  downside. Historical success remains research-only until forward validation.
- `scripts/run_current_eventual_runner_research.sh` reproducibly rebuilds the
  current 505-symbol baseline after market and posts the selected untouched
  final average, lower bound, and goal-day rate to Discord. It shares the
  history-backfill lock and cannot overlap cache mutation.

Opportunity and label diagnostic:

- The classifier now reports a deliberately clairvoyant contract upper bound.
  It selects positive future outcomes, ignores overlapping positions, and can
  never promote paper or real trading. Its only purpose is to separate a weak
  opportunity set from a weak causal selector.
- On the deterministic 100-symbol interface smoke, the stressed one-slot upper
  bound averaged about `Rs 11,289/day` with a `Rs 10,073` lower confidence
  bound. The causal eventual-runner model still averaged about `Rs -249/day`.
  This says the fixed contract contains opportunity in hindsight, but current
  features and ranking do not identify it before entry.
- A sibling prior-only model now predicts the actual `target before stop`
  outcome instead of the later 10% runner label. On the same small smoke it was
  worse: roughly `Rs -1,135/day` stressed final P&L and `21.6%` selected-label
  precision. This is diagnostic, not a strategic conclusion; the scheduled
  505-symbol replay and broad checkpoints evaluate both labels independently.
- The upper bound uses future leakage explicitly, both model lanes keep the
  final period out of selection, and every generated status keeps paper
  promotion and real broker orders false.
- A causal temporal-feature experiment added prior-signal rank improvement,
  move acceleration, volume change, relative-strength change, pullback change,
  and VWAP change. It slightly reduced training and validation losses but made
  untouched final performance worse (`Rs -427/day` versus `Rs -249/day`, with
  a `Rs -684/day` lower bound). The experiment was rejected and removed before
  deployment; the scheduled replay retains the simpler frozen feature set.
- The read-only dashboard now presents the eventual-runner model, the direct
  target-before-stop model, and the clairvoyant opportunity bound separately.
  This makes label, selection, and opportunity failures visible without
  treating hindsight diagnostics as a tradeable result.
- A second feature experiment rebuilt the same 11,768-row smoke with causal
  signal-bar wicks and close location, prior-high breakout distance, six-bar
  persistence/efficiency/volatility, and recent volume concentration. The
  runner model's untouched final result worsened to about `Rs -290/day`; the
  direct model improved only from about `Rs -1,135/day` to `Rs -1,038/day`
  while training and validation worsened. It was also rejected and removed.
- Existing live microstructure research already evaluates selected order-book
  acceleration and fade candidates with 100% outcome coverage. Across the nine
  completed sessions, every frozen configuration remains negative after
  stress. The broader EOD coverage warning (roughly 83%-91% combined) includes
  many irrelevant/illiquid recorder states; it does not make the selected
  strategy evidence positive. The live selector stays rejected.
- The direct classifier now evaluates frozen cumulative signal windows using
  pre-final periods only. On the 100-symbol smoke it chose 09:30 only, improving
  selected-label precision from `21.6%` to `25.9%` and stressed final P&L from
  about `Rs -1,135/day` to `Rs -667/day`. This is still far below break-even,
  so the lane remains rejected; the window dimension is retained only to test
  whether the 505 and broad samples confirm that later scans dilute quality.

### 2026-07-17 forward-paper and scheduler audit

- The ranked sustained-confirmation lane has seven closed forward-paper trades
  across five sessions (`2026-07-13` through `2026-07-17`): zero winners and
  seven losers, for `Rs -3,136.66` net. Mean favorable excursion was only about
  `0.24%`; five trades hit the configured stop, one used the tighter stop, and
  one remained negative at EOD. The counterfactual exact-tick replay is also
  negative for every tested confirmation configuration. Repeated B/S strength
  after an alert is therefore treated as an exhaustion selector, not an entry
  edge, unless substantially broader frozen evidence later contradicts it.
- The underlying five-session exact-tick candidate tape contains `123` alerts.
  From alert price, only `22.0%` reached `+1%` before `-1%`, and only `11.4%`
  reached `+1.5%` before `-1%`. The 09:00-hour cohort was strongest but still
  reached `+1%` first only `29.9%` of the time; the 11:00-hour cohort had zero
  `+0.5%`, `+1%`, or `+1.5%` wins. This supports early-session discovery plus
  an independent causal ranker, not additional waiting on the same B/S signal.
- The one-shot 505-symbol systemd research service was found with
  `TimeoutStartSec=90s`, although the dual-label walk-forward run takes many
  minutes. Before the scheduled run, the transient timer/service was recreated
  for the same `16:10 IST` start with a three-hour start timeout and `Nice=10`.
  Its first launch then exposed a second scheduler defect: root could not
  truncate a user-owned lock in sticky `/tmp` because of protected-regular-file
  semantics. All three broad-history entrypoints now share an append-open,
  world-writable lock under repository `results/Data`, with an environment
  override for controlled runs. Cross-user lock acquisition was tested in both
  directions before restarting the frozen job. These changes affect offline
  research scheduling only; the live scanner was not restarted and all
  real-order switches remained disabled.

### 2026-07-17 frozen 505 replay and next objective

- The repaired replay produced `54,904` fixed-time candidates across `482`
  dates and `498` cached symbols. The eventual-runner model again selected only
  the 09:30 window, but stressed untouched-final performance was about
  `Rs -199/day` with a `Rs -458/day` lower 95% bound and `2.5%` selected-runner
  precision. Its selection gate and final goal contract both failed.
- The target-before-stop classifier selected signals through 10:00 and was
  worse: about `Rs -693/day` stressed final P&L, `Rs -1,279/day` lower 95%,
  `28.54%` selected target precision, zero goal days, and a `-1.875%` worst
  day. Its pre-final block floor was also negative, so failure does not depend
  on the untouched final period.
- The deliberately clairvoyant one-slot opportunity bound on the identical
  contract averaged about `Rs 17,312/day` stressed final with a `Rs 16,371/day`
  lower 95% bound. It cannot promote anything, but it isolates the architecture
  gap: the candidate/exit contract contains ample hindsight opportunity while
  the current causal classifier cannot rank the winning candidate.
- A separate walk-forward nonlinear expected-return ranker was implemented and
  tested against the deterministic 100-symbol smoke, using prior-date training,
  equal-day weights, exact capital reuse, and pre-final selection. It failed in
  train, validation, every pre-final block, and final: selected through 09:45,
  stressed final P&L was about `Rs -185/day`, lower 95% about `Rs -481/day`,
  and target-before-stop precision only `17.33%`. The family was rejected and
  removed before deployment. This rules out a simple pointwise expected-return
  objective on the same feature set; the next architecture must add genuinely
  new causal information or a different cross-sectional representation rather
  than relabeling the same weak inputs. Paper promotion and real orders remain
  disabled.

### 2026-07-18 broad-history listing audit fix

- The first four broad-history batches advanced the exact-range cache from
  `505` to `593` symbols with zero Kite fetch errors, but each job returned a
  failed quality gate. The failures were false negatives caused by demanding
  candles from `2024-05-30` for stocks that listed later, including GROWW,
  MEESHO, SWIGGY, ATHERENERG, OLAELEC, LENSKART, PINELABS, and other recent
  listings. Their files were continuous through the requested end date, but
  the audit classified the unavailable pre-listing period as missing data.
- Intraday cache auditing now retains requested-range coverage while also
  computing coverage from the first available candle. A file is marked
  `listing_limited` only when it has at least 20 expected trading sessions,
  reaches the requested end, has at least `95%` session and `85%` bar coverage
  after its first candle, has few internal missing sessions, and has no fetch
  error. Stale endings, sparse internal history, very short new listings, and
  fetch failures still fail closed.
- The nine affected names in batch 0 each have `100%` available-session
  coverage and `99.6%-99.9%` available-bar coverage through `2026-05-29`.
  Re-auditing all four downloaded batches produces no incomplete file:
  listing-limited counts are `9`, `4`, `19`, and `18` respectively. Synthetic
  tests independently cover a valid recent listing, sparse late-start history,
  and a listing with fewer than 20 sessions.
- This correction changes offline data eligibility only. It does not change a
  strategy, promote paper trading, or enable any broker order switch.
- The first corrected production batch (batch 4) completed and bucket-synced
  with no fetch errors, growing the cache to `652` physical files. It reported
  `57` complete, `22` listing-limited, `21` warning, and one incomplete stock.
  GCSL remains audit-rejected because its post-listing file has only `68.03%`
  expected-bar coverage and `3,681` zero-volume bars; the 99% batch gate may
  pass, but that individual file is not research eligible.
- Planning now publishes both physical cache count and quality-eligible cache
  count plus a consolidated eligible-symbol file. Frozen checkpoint milestones
  use the eligible count, and candidate generation filters its file list by
  that exact symbol file instead of globbing all cached CSVs. An audit-rejected
  file can therefore remain available for repair/forensics without contaminating
  strategy evidence or prematurely advancing a coverage milestone.
- A repository-drift audit found that dev already validated the saved Kite
  token with an authenticated profile request, while the runtime copy only
  checked whether a token string existed. The validated check is now restored
  in runtime and covered by valid-profile and expired-token tests. Future
  offline batches abort before symbol iteration when Zerodha rejects a token,
  instead of turning one stale credential into thousands of failed requests.
- Zerodha's official Kite Connect rate table currently allows three historical
  candle requests per second. The broad runner's post-request pause is now
  configurable through `BROAD_BACKFILL_REQUEST_SLEEP` and defaults to `0.4`
  seconds, still below the documented ceiling after request latency. Existing
  quadratic retry backoff remains active for 429, timeout, and connection
  failures.

Pairwise ranking diagnostic:

- A prior-days-only pairwise ranker was tested in memory on the frozen
  505-symbol candidate set. It learned winner-versus-loser feature differences
  within each historical date and completed scan time, then scored each future
  candidate by estimated head-to-head strength against that scan's peers.
  Training was walk-forward, final dates were not used for configuration
  selection, and execution reused capital only after exact exit timestamps.
- The best pre-final configuration still failed: stressed training averaged
  about `Rs -617/day`, validation `Rs -233/day`, and its weakest selection
  block was negative. Untouched-final stressed performance was about
  `Rs -726/day`, with a `Rs -1,079/day` lower 95% bound, zero goal days, and
  only `21.77%` target-before-stop precision. No code or model artifact was
  promoted from this experiment.
- This rejects pairwise/listwise reshaping as a solution on the same historical
  feature set. Broader OHLCV remains useful for robustness and denominator
  coverage, but the next selector must rely on genuinely new causal inputs
  captured by the live recorder, such as temporal order-book stability,
  spread, volume acceleration, and market/sector state.

Live top-gainer recorder denominator:

- A new EOD diagnostic joins each live feature-recorder observation only to
  later reconstructed bars from the same symbol and session. The completed-day
  5% and 10% runner labels are explicitly hindsight labels: the output cannot
  promote paper trading or real orders. It measures discovery coverage, not a
  tradeable strategy result.
- Across the five rebuilt sessions available on `2026-07-18`, there were `377`
  liquid 5% runner events and `83` liquid 10% runner events after applying the
  `Rs 50` price and `Rs 3 crore` turnover floors. The recorder saw `45.89%` of
  5% runners and `91.57%` of 10% runners before their threshold crossing.
- For eventual 10% runners, median first visibility was at `+3.416%` from the
  open with `83.74` minutes of lead time. The old `+0.5%` to `+3%` discovery
  band covered only `39.76%`; extending observation through `+5%` covered
  `68.67%`. This shows that the scanner usually has time to see the largest
  eventual leaders, but the fixed narrow band excludes many and the current
  causal ranking still cannot distinguish continuation from reversal.
- `scripts/audit_live_top_gainer_recorder_recall.py` now runs after each EOD
  event-shadow rebuild and writes durable events, summary, and status outputs.
  It merges repeated recorder fragments, avoids duplicate event counting, and
  remains a denominator audit until a frozen forward selector proves positive
  net economics.

Temporal leader continuation tape:

- The EOD pipeline now builds a separate causal continuation tape from the
  first executable live-recorder state in each `+0.5%-1%`, `+1%-2%`,
  `+2%-3%`, and `+3%-5%` move band. It records two-snapshot move acceleration,
  velocity, B/S change, spread, pullback, and sector/market context, then enters
  only from the next untouched reconstructed five-minute bar with adverse slippage and
  fees. It contains no future-runner label and cannot promote alerts, paper
  trading, or real orders.
- The first implementation incorrectly treated these reconstructed files as
  one-minute bars and demanded an exact next-minute timestamp, leaving a
  timestamp-selected `131/625` states. The files are actually five-minute
  bars. Correct next-untouched-bar execution now covers `599/625` states
  (`95%-97%` by band), excludes the partial signal bar, and requires a complete
  60-minute horizon.
- After correction, every move band is negative for the `+1%/-1%` contract.
  The `+3%-5%` band covered `205/212` states, hit target first only `36.10%`,
  averaged `-0.2035%` net, and had a `-0.2637%` lower daily bound. No raw move
  band is accepted as a blind entry rule.
- The durable candidate tape is the input for the next prior-only
  cross-sectional ranker once enough live sessions accumulate. This addresses
  the observed architecture gap directly: discovery visibility is high, but
  continuation must be distinguished from exhaustion before capital is used.
- A deliberately clairvoyant weighted interval schedule now measures the
  contract ceiling on the same corrected tape. With one reusable `Rs 50,000`
  slot, the best future-known non-overlapping `+1.5%/-1%` trades averaged
  `Rs 10,776/day`, had an `Rs 8,848` lower daily bound, and reached `Rs 10,000`
  on `60%` of the five sessions. This proves only that enough hindsight
  opportunity exists in the candidate tape; because it selects using future
  returns, it can never promote alerts, paper trading, or real orders.
- An explicitly post-hoc acceleration/B/S slice initially looked positive only
  in the erroneous timestamp-selected subset. Correct five-minute evaluation
  covers `61/62` states and rejects it: `37.70%` target hits, `-0.2856%` mean
  net, a `-0.3455%` lower daily bound, and zero positive sessions for the
  `+1%/-1%` contract.
- A second outcome source then evaluated the same causal states across all ten
  recorder sessions using the observed ask as entry and later observed bids as
  exits. It covered `113/114` extension states and independently agrees with
  the corrected five-minute result: the `+1%/-1%` contract averaged `-0.2083%` net,
  its equal-session mean was `-0.2495%`, its lower daily bound was `-0.4200%`,
  and only `30%` of sessions were positive. The acceleration/B/S hypothesis is
  therefore selection-rejected before Monday. It may continue to be recorded
  for diagnostics, but it is not a forward paper candidate.
- The read-only dashboard now shows this denominator and forward registry in a
  dedicated leader-discovery section, including 10% runner visibility, median
  lead time, retrospective economics, and untouched forward state count. It
  does not expose the retrospective result as an accepted strategy.

## Frozen full-session decisions and live ranker (2026-07-22)

Full-session long and short decisions:

- The broad long candidate tape contains `390,557` causal states across `483`
  sessions and `1,057` symbols. None of the `544` static configurations was
  positive in both train and validation, and none passed the pre-final block
  floor. The 16-contract nonlinear registry also rejected every contract. Its
  pre-final-selected `+1.5%/-1.5%` contract mostly chose cash; the untouched
  stressed final period averaged `Rs -62.18/day`, had a `Rs -184.07/day` lower
  95% bound, and reached the `Rs 10,000` goal on zero days.
- A distinct regime-aware long model added only signal-time Nifty state: one-
  and three-bar returns, intraday range and VWAP distance, prior-day return,
  and prior rolling volatility. The frozen `+1.5%/-1.5%` model selected no
  train or validation trades, failed its sample and selection gates, and then
  lost `Rs 62.18/day` stressed on its lone final trade. Market regime did not
  repair the late-leader OHLCV selector.
- The short-exhaustion tape contains `490,654` causal states across `483`
  sessions and `1,056` symbols. Its frozen `+1.0%/-1.5%` expected-return model
  had `129` stressed train trades at `Rs -375.19/day`, `41` validation trades
  at `Rs -207.40/day`, and a negative `-0.108624%` selection-block floor. The
  final period was still negative (`Rs -15.26/day`) with a `Rs -293.09/day`
  lower 95% bound and zero goal days. The idea that late alerts can simply be
  inverted into shorts is rejected.
- Both frozen status artifacts set evidence accepted, paper promotion, and
  real orders to false. These results close further threshold/exit tuning on
  the same OHLCV late-leader family. They do not prove that no intraday edge
  exists; they prove that this causal feature family cannot select it.

Live evidence architecture:

- The EOD job was falsely reporting operational failure because its 95%
  coverage denominator mixed broad diagnostic snapshots and rejected lane
  rows with candidates that could be promoted. Sparse stocks often have no
  exact next-minute trade, which makes those diagnostic rows non-executable;
  it must not hide missing outcomes for actual candidates or invalidate an
  otherwise complete session. The status now reports overall diagnostic
  coverage separately and gates only fully executable microstructure states
  plus lane-pass candidates. A July 21 production replay measured `70.19%`
  broad feature coverage, `97.48%` lane-decision coverage, and `98.0%`
  promotion-candidate coverage. The gate remains `95%`.
- `assess_live_evidence_readiness.py` now inserts the repository root before
  importing `scripts.*`. This fixes the timer-only `ModuleNotFoundError` that
  made the EOD wrapper return code 3 after otherwise completing its research
  outputs.
- Snapshot outcomes now persist exact entry and exit timestamps from observed
  asks and later observed bids. This permits honest slot release and capital
  reuse instead of assuming every candidate can use the full account at once.
- A new frozen prior-session-only expected-return ranker uses spread, temporal
  buyer/seller persistence, volume/move acceleration, sector breadth, market
  breadth, pullback, and move-band state. Its contract is frozen at
  `t1.5_s1.0`, score threshold `+0.20%`, one reusable unleveraged slot, at most
  ten entries per session, `Rs 500,000` capital, and `Rs 10,000` daily goal.
  The model selects within each visible minute batch and releases capital only
  at the recorded exit timestamp.
- The selector was frozen through `2026-07-21`; untouched forward evidence
  starts `2026-07-22`. It cannot promote before at least `20` forward sessions,
  `15` traded sessions, and `30` trades, and it additionally requires positive
  stressed block and lower-confidence bounds, worst day no lower than `-2.5%`,
  at least `50%` goal-hit sessions, and stressed average P&L of at least
  `Rs 10,000/day`. Real orders remain hard-coded false even if the paper gate
  eventually passes.
- The Monday service environment remains safe: `KITE_AUTO_TRADE=0` and
  `ENABLE_GRADE_B_AUTOTRADE=0`. Current work is evidence collection and
  deterministic shadow replay only.

### Prior-session live shadow executor

- The frozen ranker now writes a versioned model artifact after the completed
  EOD tape is rebuilt. The artifact records its training cutoff, first valid
  session, exact feature list, contract, equal-session weighting, source-data
  SHA-256, and `real_orders_allowed: false`.
- The live monitor loads that artifact on the following session and reproduces
  the same candidate construction in real time: third observed state, one-to-
  five-minute span, first executable state per movement band, observed ask
  entry, and observed bid exit. A model trained through the current date is
  rejected before scoring, which prevents same-day outcome leakage.
- Live predictions and paper trades are written under
  `results/Data/frozen_microstructure_paper/`. This lane has no Discord path,
  does not import the broker order manager, uses one unleveraged `Rs 500,000`
  slot, and keeps the frozen `+0.20%` expected-return threshold,
  `+1.5%/-1.0%` contract, 60-minute horizon, and ten-trade ceiling.
- An integration replay of the July 22 tape through the model trained only
  through July 21 produced `42` timestamped candidates by late morning. The
  maximum expected return was only `+0.131%`, below the frozen threshold, so
  the correct decision was no trade. This is operational validation, not
  acceptance evidence; the untouched EOD forward ledger remains authoritative.

### Leader pullback/reclaim audit

- The existing leader-ignition lane was tested as a separate causal strategy:
  first observe ignition, wait for its configured healthy pullback, then enter
  only after reclaim. Candidates were ranked by the already-recorded signal
  score within each visible minute, with one unleveraged `Rs 500,000` slot,
  exact recorded exit-time capital reuse, and at most ten entries per day.
- Executable EOD outcomes currently provide `280` reclaim candidates across
  seven calendar sessions. Under the `+1.5%/-1.0%` contract, the rule took `26`
  trades and averaged `Rs -6,306.32/day` before the additional stress charge.
  Its raw lower 95% bound was `-2.868%`, only `28.57%` of sessions were
  positive, and it reached `Rs 10,000` on zero sessions.
- Adding another `0.10%` per trade for execution stress reduced the result to
  `Rs -8,163.46/day`, a `-3.328%` lower 95% bound, `14.29%` positive sessions,
  and zero goal sessions. The worst stressed day was `-3.7485%`.
- The current pullback/reclaim rule is therefore rejected. Waiting for a
  pullback by itself does not repair late detection or distinguish a healthy
  continuation from a fading leader. The EOD timer reruns the audit as evidence
  accumulates, and the dashboard reports its status, but paper promotion and
  real orders remain disabled.

### Full-universe rank migration evidence

- Candidate-relative rank migration was added as a separate frozen research
  family. It compares each recorded state only with candidates observed in the
  preceding 15 minutes, measures rank and rank velocity, and trains with prior
  sessions only. Its model is frozen through `2026-07-21`, uses the fixed
  `+0.20%` expected-return threshold and `+1.5%/-1.0%` contract, and starts
  untouched evidence on `2026-07-22`.
- The candidate-tape diagnostic contains `1,313` rows over 12 sessions, but only
  four sessions have enough prior history for a causal walk-forward prediction.
  At the frozen threshold it traded nine times on one session. After an extra
  `0.10%` execution charge it averaged `Rs 1,367.57/day`, had a `-0.5969%`
  lower confidence bound, and reached `Rs 10,000` on zero sessions. This is not
  accepted evidence and cannot enable orders.
- That diagnostic also exposed a larger architecture problem: ranking only the
  existing candidate tape cannot discover a stock the scanner never recorded.
  The live recorder now ranks every valid Kite tick across the complete live
  universe once per minute. It persists the top 75 positive leaders plus rapid
  climbers that improve at least 25 rank places while their day move accelerates
  by at least `0.15%`, including rank, prior rank, velocity, spread, depth,
  liquidity, sector breadth, and market breadth.
- Full-universe ranking is computed over roughly 2,990 ticks in about `0.08s`
  in a synthetic benchmark, well below the one-minute recorder interval. The
  EOD evaluator preserves the first occurrence of each rank/move state and
  labels it with next-minute executable outcomes. This recorder sends no
  Discord alerts, imports no broker order manager, and writes
  `real_orders_allowed: false` in its operational status.
- The EOD job now rebuilds both the candidate-rank audit and full-universe rank
  outcomes automatically. The dashboard shows the latest universe size,
  recorded rows, rapid climbers, diagnostic economics, and untouched forward
  counts. A full-universe selector will be frozen only after enough newly
  recorded sessions exist; historical candidate-only results cannot promote it.
- The first full-universe deployment began around 12:13 IST on `2026-07-22`.
  Its status is explicitly marked as a partial session, so this date can test
  plumbing and produce diagnostics but cannot count toward a full-session
  selector or acceptance denominator.
- A dedicated full-universe rank judge is now frozen before its first complete
  session. Generation 6 requires the raw 15-second recorder to cover at least
  1,000 snapshots
  from no later than 09:20 through at least 15:10, and independently requires
  at least 95% EOD outcome coverage for buyable rank states. Missing Kite labels
  cannot enter selection; legitimate complete days with zero buyable states are
  preserved as zero-trade days.
- Its strategy contract is fixed at `+1.5%/-1.0%`, model score `>= +0.20%`, one
  reusable unleveraged `Rs 500,000` slot, at most ten trades, and another
  `0.10%` execution stress. Only price `>= Rs 50`, traded value `>= Rs 3 Cr`,
  valid bid/ask, spread `<= 20 bps`, move `+0.5%` to `+5%`, and a recorded rank
  state can enter the model.
- The first 12 complete sessions form the immutable selection period. A model
  is frozen only if its prior-only selection replay already averages at least
  `Rs 10,000/day` stressed with positive block and confidence bounds, at least
  50% goal days, and controlled downside. It then needs 20 untouched forward
  sessions, 15 traded sessions, and 30 trades under the same hard goal gates.
  Selection, paper promotion, and real-order status are written separately;
  real orders are hard false.

### Exact full-universe rank paper outcomes

- The live full-universe rank tape now has a stateful execution observer. It
  labels the predefined leader-pool and rapid-rank-climber cohorts only when
  price is at least `Rs 50`, traded value is at least `Rs 3 Cr`, both bid and
  ask exist, spread is no more than `20 bps`, and the stock is between `+0.5%`
  and `+5%` for the day.
- Each paper event enters at the displayed ask and is watched on every monitor
  cycle against the displayed bid. The contract was fixed before collection at
  `+1.5%/-1.0%`, a 60-minute timeout, forced exit near EOD, one standalone
  unleveraged `Rs 500,000` label, and another `0.10%` execution stress charge.
- Active events and seen cohort keys are saved atomically, so watchdog restarts
  do not duplicate entries or lose open observations. Entries, exact outcomes,
  and operational status are stored under
  `results/Data/full_universe_rank_paper/` and shown on the dashboard.
- This observer is a broad label collector, not a portfolio strategy. It sends
  no Discord alerts, imports no broker order manager, and has both alerts and
  real orders hard-disabled. Its outcomes will replace optimistic candle-touch
  assumptions when the frozen full-rank selector has enough complete sessions.
- A separate exact-capital judge now prevents the broad label ledger from being
  mistaken for achievable portfolio P&L. It replays one reusable unleveraged
  `Rs 500,000` slot by recorded entry and exit timestamps, rejects overlapping
  opportunities, limits trading to ten entries per day, and preserves
  legitimate zero-trade full sessions.
- Four causal orderings were declared before the first complete session:
  visible rank first, rank acceleration first, earliest positive move first,
  and a fixed balanced rank/acceleration/depth score. The first 12 complete
  sessions may freeze at most one only if it already clears the stressed
  `Rs 10,000/day`, confidence, block, downside, and goal-frequency gates. The
  frozen ordering then needs 20 untouched forward sessions under the same
  gates. The selector file is immutable once written and real orders remain
  hard-disabled.
- Exit selection is now tested independently instead of assuming that every
  valid entry should use `+1.5%/-1.0%`. A parallel exact observer follows nine
  contracts from the same displayed ask: fast `0.5%-1.0%` scalps, balanced
  `1.0%-2.0%` continuations, and `3%-5%` runners, with stops from `0.5%-2.0%`
  and horizons from 15 to 120 minutes. Each contract exits against the observed
  bid and carries the same additional `0.10%` stress charge.
- The contract judge evaluates all nine contracts with all four entry
  orderings, but still permits only one actual capital slot in replay. Its 36
  combinations were fixed before the first complete session. At most one pair
  can freeze after 12 complete sessions, and it still needs 20 untouched
  forward sessions. This separates a bad entry model from a mismatched exit
  contract without pretending all parallel paper labels could be funded.
- Entry timing is also isolated as a separate exact experiment. Six variants
  follow every base rank signal: resting limits at `-0.25%`, `-0.5%`, and
  `-1%`; confirmation entries at `+0.25%` and `+0.5%` with a slippage guard;
  and a `-0.5%` pullback followed by a `+0.25%` reclaim. Dip fills use the
  resting limit rather than a better later ask, which keeps their simulation
  conservative. Confirmation and reclaim fills use the observed ask.
- Entry windows are fixed at 15 or 30 minutes. Every unfilled variant is
  persisted and remains in coverage, while filled variants use observed bid
  exits under the canonical `+1.5%/-1.0%`, 60-minute contract. The timing judge
  again allows one capital slot, freezes at most one of 24 predeclared
  timing/ranking combinations after 12 complete sessions, and requires 20
  untouched forward sessions. This directly tests whether buying below the
  alert, buying above it, or waiting for reclaim actually improves results.
- Candidate filtering is a fourth independently judged dimension. Nine causal
  gates were declared before the first full session: all buyable rank labels,
  rapid climbers, top-10, top-25, early rank acceleration, sector rotation,
  stable buyer depth, tight high-turnover books, and balanced quality. These
  use only fields visible at entry; no future return or day-final rank enters a
  gate.
- The gate judge crosses those nine filters with the four fixed capital-order
  selectors, again through one reusable `Rs 500,000` slot and at most ten daily
  trades. Its 36 combinations use the same 12-session selection and 20-session
  untouched-forward contract. This identifies whether losses come from broad
  candidate intake even when timing and exits are otherwise correct.

### Capital growth and monthly contributions

- The target is normalized to capital rather than treated as a constant signal
  threshold. `Rs 10,000` on the initial `Rs 500,000` requires `2%` net per
  trading day. With `Rs 200,000` added after each month and no assumed trading
  profit, starting capital is `Rs 700,000` in month two, `Rs 900,000` in month
  three, and `Rs 1,100,000` in month four; the daily percentage required falls
  to at most `1%` in month four.
- An automated capital planner writes 12-month arithmetic scenarios for 0%,
  0.25%, 0.5%, 1%, and 2% daily returns with 20 trading days per month and the
  planned monthly contribution. These are explicitly labeled mathematical
  scenarios, not predictions or evidence.
- Compounding from a strategy is disabled unless its status is accepted after
  at least 20 untouched forward sessions, its average return is positive, and
  its lower 95% bound is also positive. Unaccepted paper results cannot drive a
  capital projection, regardless of how attractive their point estimate is.

### 2026-07-22: Operational evidence-pipeline readiness

- Added `scripts/check_intraday_evidence_pipeline.py` to distinguish operational
  collection readiness from strategy profitability. It verifies hard-off broker
  flags, systemd start/prewarm controls, disk headroom, fresh Kite ticks, the
  full-universe recorder, exact ask-to-bid observer, nine-contract exit matrix,
  six-variant entry matrix, and the 15:35 IST EOD judges.
- Added a five-minute weekday scheduler and dashboard status. The report always
  keeps `strategy_accepted=false`, `paper_promotion_allowed=false`, and
  `real_orders_allowed=false`; a green operational report only means tomorrow's
  evidence can be trusted.
- July 22 remains a partial plumbing session because the runtime restarted after
  market open. The first eligible full-session selection day is July 23 if the
  08:30 prewarm, 08:45 service start, 60-second recorder cadence, and EOD
  coverage gates all pass.
- Added an exact full-rank opportunity ceiling. For every exit contract it uses
  future-known outcomes to solve the best non-overlapping one-slot schedule,
  capped at ten trades and Rs 500,000 per trade. This tells us whether the
  candidate universe or the causal selector is the bottleneck. Partial sessions
  are displayed but excluded, and the ceiling can never promote paper or real
  trading.
- The same audit now computes the causal-selector gap for all 36 fixed
  contract/selector combinations. It classifies whether the current bottleneck
  is candidate-universe capacity or causal selection and reports how much of
  the first-observation ceiling the best causal combination captures.
- Partial-day causal gaps are written separately as
  `partial_selector_gap.csv`. They are explicitly marked incomplete and cannot
  enter selection, promotion, or order gates; this permits mechanical debugging
  during the day without introducing survivorship bias into accepted evidence.
- Split eventual-runner recall by recorder generation. New full-rank recall only
  counts a session when a 2,500+ symbol ranked snapshot exists near the open,
  and separately reports visibility before +5%/+10% and visibility while the
  stock was still between +0.5% and +3%. Legacy selective-recorder rows cannot
  inflate this denominator.
- Operational readiness is now market-phase aware. Live tick and observer
  freshness are mandatory only from 09:15 to 15:30 IST; after close, stale feeds
  are expected and the checker instead requires the day's EOD evidence status
  to be complete by 16:10 IST.
- Added a post-close integrity manifest. The session is processed only when the
  base EOD labeler plus all eight authoritative strategy, exact-execution,
  timing, feature, opportunity, capital, and runner-recall status artifacts are
  freshly rewritten after 15:30. Coverage failures are recorded as processed
  but ineligible; stale, missing, or unsafe artifacts fail the pipeline.
- Before the first eligible full session, expanded the frozen causal feature
  registry from nine to twelve gates. The added families test opening leaders,
  morning acceleration, and fresh afternoon acceleration so stocks beginning
  their move later in the day are represented. This registry expansion occurred
  before any complete selection session; July 22 remains excluded partial data.
- The excluded July 22 diagnostic also exposed a capital-allocation bottleneck:
  one long-held selection could block later leaders. Before the first eligible
  session, added a separately frozen 144-combination allocation matrix covering
  9 exit contracts, 4 causal selectors, and 1/2/3/5 equal concurrent slots.
  Total capital remains capped at Rs500,000, execution is exact ask-to-bid with
  stress costs, and acceptance still requires 12 selection plus 20 untouched
  forward sessions. The strict one-slot judge remains the baseline.
- Fixed a live-data continuity problem discovered during the July 22 excluded
  session: the assistant supervisor restarted the healthy Kite ticker for every
  Git commit, including research scripts and documentation. It now keeps live
  workers running for non-live changes and restarts conservatively only for
  `pkscreener/`, dependency, or runtime-config changes. Readiness also reports a
  critical five-minute ticker continuity gate during market hours.
- Full-session eligibility now also rejects any generation-6 recorder day with
  a snapshot gap above 45 seconds. A day can no longer enter selection merely
  by having 1,000 total snapshots plus opening/closing coverage while hiding an
  intraday feed interruption.
- Added ten complete predeclared integrated policies before the first eligible
  session. Each policy fixes its feature gate, causal selector, exit contract,
  and 1/2/3/5-slot capital allocation together. This prevents independently
  optimized components from being stitched into an untested final strategy;
  the selected whole policy must pass the same 20 untouched forward sessions.
- Corrected EOD eligibility semantics after July 22: completed EOD labeling no
  longer implies a complete trading session. The integrity manifest now
  requires a fresh rank-session coverage row and marks evidence eligible only
  when that row proves uninterrupted opening-to-close recorder coverage.

### 2026-07-23: Opening session exposed and fixed leader-recall loss

- Fixed a fast-mode lifecycle defect before market open. When no live symbols
  changed, the monitor's quiet-cycle branch called the intentionally disabled
  scorecard tracker and the top-level CLI converted the exception into a
  clean-looking process exit. The supervisor then repeatedly reloaded all 2,929
  symbols. The branch now treats the scorecard as optional, remains in the same
  process, and preserves in-memory rank history across quiet cycles. A
  production soak confirmed unchanged ticker and monitor PIDs across repeated
  empty cycles.
- The shared service was deliberately restarted once at 08:54:29 IST after the
  live-code fix. Kite and the monitor were stable well before market open.
- The first ranked snapshot for July 23 was captured at 09:15:02 IST over a
  2,860-symbol live universe. The next snapshot arrived at 09:16:08, giving an
  initial 66-second gap and every recorded row had rank fields populated.
  However, a live recall audit at 09:33 found that the 200-row cap prioritized
  supplemental stable-depth and rapid-climber rows before the guaranteed leader
  pool: only 54 of ranks 1-75 were present, including only 9 of ranks 1-10.
- Recorder generation 2 now reserves the leader pool before supplemental
  cohorts. Its first production snapshot captured 10/10, 25/25, 50/50, and
  75/75 current leaders. Regression coverage proves lower-ranked stable-depth
  rows cannot crowd those leaders out again.
- This is now a live readiness invariant rather than a one-time audit. Each
  recorder status writes the number of rank-qualified leader-pool candidates,
  the number retained after the row cap, and recall percentage. Market-hours
  readiness requires generation 6 plus exact candidate/recorded equality at
  100% recall; the dashboard fails immediately if even one leader is dropped.
- A second opening-load audit found that generation 2 could still truncate
  rapid rank climbers when the guaranteed leader pool plus the rapid cohort
  exceeded the fixed 200-row snapshot cap. Generation 3 makes that limit a
  soft cap: every current leader and every causal rapid climber is mandatory,
  and only supplemental context rows are capped. Status and readiness now
  independently require 100% rapid-climber candidate recall. A regression
  test proves that 20 rapid climbers survive a configured 10-row cap.
- Before the first eligible session, the guaranteed discovery pool was widened
  from the top 75 to the top 200. This fixes a concrete architecture mismatch:
  the frozen search included a top-100 opening gate while ranks 76-100 were not
  guaranteed under load, and a steady climber could otherwise remain invisible
  until it was already a top-75 leader. The 200-row setting is now a soft cap:
  all top-200 movers at or above +0.5% plus every rapid climber outside that
  pool are retained. A production generation-4 snapshot recorded 200/200.
  The next busy live snapshot expanded the soft cap to 335 rows and retained
  both 200/200 discovery-pool leaders and 150/150 rapid climbers.
  This generation-4 breadth proof remains diagnostic.
- Before the first eligible session, generation 5 reduced the causal snapshot
  interval from 60 seconds to 15 seconds. Ranking all 2,865 live symbols
  benchmarked at about 54 ms average and 60 ms maximum, uses only the local
  ticker stream, and adds no Kite API traffic. Production delivered five
  snapshots with 15-27 second gaps (18.8 seconds average) while retaining
  200/200 leaders and 18/18 rapid climbers. At the measured row size and
  density, the complete 32-session experiment should remain near 3 GB against
  63 GB free.
- Strategy eligibility was initially pinned to recorder generation 5 after
  the cadence migration. The exchange-cost provenance audit below explicitly
  superseded that contract with generation 6 before the first eligible
  session. Older complete-looking sessions remain diagnostic only, and a
  future generation cannot silently mix with the frozen sampling policy.
- Exact entry ledgers now persist `RecorderGeneration`, include it in the
  event identity, and the downstream loader independently accepts only
  the current required generation (generation 6 after the exchange migration).
  Rank cohorts are separated into 76-100, 101-200, and 201+
  rather than hiding all ranks above 75 in one bucket. Live validation showed
  tagged top-200 entries with generation-specific IDs; older untagged rows in
  the excluded transition file are ignored by selection.
- Recorder lineage now propagates through contract registrations/outcomes and
  timing registrations/fills/nonfills/outcomes. Both derivative writers use
  atomic same-day schema widening; all six July 23 ledgers were migrated and
  reread without changing row counts. Legacy transition rows have blank
  generation and remain ineligible. Rows created before the exchange-cost
  migration carried generation 5; current rows carry generation 6.
- The live migration exposed legacy active state without the new generation
  key, initially stalling derivative status refresh. Contract, waiting,
  filled, nonfill, and exit paths now normalize missing lineage to generation
  0 instead of raising. Regression tests cover those transitions, and
  production contract/timing status resumed at a 5-12 second age.
- Generation-aware status and coverage deliberately reset the session start
  when sampling policy changes. Generation 5 started at 10:11:57 IST, so July
  23 is an excluded recorder-transition session and cannot enter strategy
  selection. A complete generation-5 day would still have required a start by
  09:20, at least 1,000 snapshots, no gap above 45 seconds, coverage through
  15:10, and complete post-close outcomes.
- The eventual-runner recall diagnostic is generation-aware as well. Its
  “seen before +5%/+10%” denominator now uses only the newest recorder
  generation for each date, so pre-fix rows from July 23 cannot inflate leader
  visibility or early-band recall on the dashboard.
- Eventual-runner recall now separates discovery from contemporaneous
  executability. “Buyable before +5%/+10%” additionally requires the same
  minimum price, Rs3Cr turnover, valid displayed bid/ask, and at-most-20-bps
  spread used by the exact paper observer. The event audit records first
  buyable time, buyable lead time, early-band buyable recall, and the first
  observation's blockers. This prevents an illiquid stock that was merely
  visible from being counted as a realistically catchable opportunity.
- A July 23 transition-session diagnostic illustrated the distinction. The
  generation-5 recorder caught ASIANTILES, NATCAPSUQ, and PATELRMART around
  +1% to +2.5% while they ranked 220, 514, and 548, and each later crossed
  +5%. None was executable then: ASIANTILES had only Rs2.26Cr turnover,
  PATELRMART remained below Rs1Cr with frequently excessive spreads, and
  NATCAPSUQ had extremely low turnover and roughly 130-285-bps spreads. The
  rapid-climber discovery path worked; treating those observations as fills
  would have introduced liquidity and slippage hindsight.
- Exact ask-to-bid, nine exit-contract, and six entry-timing observers all
  started receiving opening events. These remain paper evidence only. Raw
  observer P&L is not a portfolio result because each event is independently
  normalized to Rs500,000; the EOD capital-allocation judge enforces one shared
  Rs500,000 account, concurrent slot limits, and no duplicate symbol per day.
- Operational readiness now reads the complete current-day feature tape on
  every five-minute audit. It reports the cumulative snapshot count, opening
  timestamp, latest timestamp, and largest historical gap. This catches a
  hidden interruption even when the latest status file has become fresh again;
  any opening later than 09:20 or gap above 45 seconds fails live readiness.
- Readiness also enforces proportional generation-6 density toward the
  1,000-snapshot close requirement and rejects a latest snapshot older than 45
  seconds. All eight post-close strategy judges use the same 1,000-snapshot
  default. Synthetic full-day fixtures now contain 1,440 causal 15-second
  snapshots, and boundary coverage proves that 999 otherwise continuous
  snapshots remain ineligible.
- A pre-selection transaction-cost audit found that the exact ask-to-bid
  replay's additional 0.10% haircut is conservative for ordinary NSE
  intraday cash trades, but cannot safely represent every BSE security group.
  Zerodha currently publishes ordinary intraday brokerage of 0.03% or Rs20
  per executed order, 0.025% sell-side STT, NSE/BSE transaction charges,
  18% GST on service charges, Rs10/crore SEBI charges, and 0.003% buy-side
  stamp duty. It also documents materially higher exchange charges for
  several BSE groups:
  https://zerodha.com/charges/
- Recorder generation 6 therefore preserves `Exchange` and `InstrumentToken`
  from Kite token resolution through every live feature, exact entry,
  contract, timing, fill, nonfill, and outcome ledger. Dual-listed symbols
  continue to prefer NSE. BSE-only candidates remain visible in the broad
  discovery tape, but cannot enter promotion-eligible exact or reconstructed
  strategy evidence until group-specific BSE costs are modeled.
- Downstream exact and reconstructed loaders independently require exchange
  `NSE`; they do not trust the observer alone. Production validation recorded
  2,040 NSE and 825 BSE live symbols, 631/631 generation-6 feature rows with
  nonzero tokens, 226 retained BSE diagnostic rows, and 57/57 generation-6
  exact entries marked NSE. New contract and timing registrations also
  preserved NSE provenance. This migration occurred during the already
  excluded July 23 transition day, so no eligible selection session was lost.
- The capital-growth planner now includes both the 144-way shared-capital
  allocation judge and the ten complete integrated-policy judge. Previously a
  future accepted winner from either new family would have been ignored by the
  monthly contribution and reinvestment projection. Inclusion still requires
  at least 20 untouched forward sessions, positive average return, and a
  positive lower 95% confidence bound.

### 2026-07-27: Buy-alert lock diagnosis and shared-capital ceiling

- The absence of promoted buy alerts was not a ticker, prewarm, universe, or
  scan failure. At 09:33 IST the operational gate passed all 19 checks: live
  ticks covered 2,875 symbols, the scanner covered 2,929, generation-6 rank
  evidence had started at 09:15:01, and leader/rapid-climber recall was
  200/200 and 119/119.
- The launcher had no accepted selector artifact, so
  `goal_selector_validated` correctly remained false. The goal lane detected
  candidates but suppressed them with
  `no validated profitable daily-goal selector`; the independent ranked lane
  continued to emit clearly labeled paper-only entries. Real-order flags
  remained off.
- That lock is supported by the current ranked-confirmation evidence, not just
  configuration: 15 closed simulated trades produced 3 winners, 12 losses,
  a 20% win rate, and Rs -4,589.10 net. Promoting the same lane as a buy signal
  would repeat a rejected strategy.
- Heartbeats now identify the launcher as `intraday-evidence-paper` and state
  either `eligible - validated selector loaded` or
  `BLOCKED - selector not validated; paper evidence continues`. This removes
  the misleading `unknown launcher` label without weakening the gate or
  enabling broker orders. The running scanner was intentionally not restarted
  during the session, preserving the first possible complete generation-6
  tape; the launcher text takes effect at the next normal start.
- The opportunity-ceiling audit now solves an exact binary mixed-integer
  schedule for 1/2/3/5 equal capital slots, one use per symbol, no more than ten
  daily trades, real entry asks, observed exit bids, and the fixed stress
  haircut. It writes `slot_daily.csv`, `slot_summary.csv`, and
  `slot_selected_intervals.csv`.
- Slot capacity does not manufacture capital. Under a fixed Rs500,000 total,
  a clairvoyant equal-slot result cannot exceed the best concentrated one-slot
  schedule; slots can still improve a causal strategy by preventing one open
  position from blocking later candidates. The dashboard now shows both
  ceilings separately.
- In the excluded partial tapes, the best causal contract/selector averaged
  Rs2,579.76 against a Rs67,007.88 first-observation future-known ceiling,
  capturing only 3.85%. This is diagnostic only, but it identifies causal
  selection and entry timing as the current research bottleneck rather than
  stock-universe visibility or nominal trade count.
- July 27 is only a potential first eligible selection session while the
  market is open. It enters the 12-session denominator only if post-close
  evidence confirms at least 1,000 generation-6 snapshots, opening by 09:20,
  coverage through 15:10, no gap over 45 seconds, and all required EOD
  artifacts.
- The broad two-year history scheduler had a separate throughput defect. It
  selected fixed original 100-symbol blocks, so a run could spend its nightly
  slot re-auditing 98 cached symbols while fetching only two missing files.
  The planner now writes a compact CSV containing the next 100 symbols that
  genuinely need data. The downloader accepts a separate monotonic output
  batch ID, preserving unique audit artifacts while always selecting slice
  zero from that compact pending universe.
- A broker-free rehearsal proved that the compact batch contains 100 unique
  symbols and every selected manifest row has `needs_fetch=true`. No historical
  API request was made during market hours. Runtime coverage remains the
  authoritative count because the development checkout intentionally does not
  contain the full runtime cache.
- The exact opportunity judge now writes a selector-regret ledger for all
  9 exit contracts x 4 causal selectors. It preserves each actual selection
  and every future-profitable observation skipped because capital was already
  occupied, a different candidate ranked first in the same minute, the symbol
  had already traded, or the ten-trade cap was full. Same-minute winners ranked
  behind a selected loser are called out separately.
- Regret P&L is explicitly future-informed, non-additive, diagnostic-only, and
  unable to promote paper or real trading. Zero-decision sessions remain in
  the average-day denominator. The dashboard shows the best causal trace,
  selected win rate, distinct skipped profitable symbols, and dominant miss
  reason.
- A real-ledger rehearsal over four excluded partial/transition sessions found
  the current best partial causal combination at about Rs6,926/day with a 55%
  selected win rate, versus a Rs70,161/day first-observation future ceiling
  (9.87% capture). Its dominant regret was an already occupied position; 31
  profitable observations also ranked behind a selected loser. These numbers
  are not strategy evidence and will be recomputed from complete sessions.

### 2026-07-27: Standalone causal evidence worker

- A live architecture audit found that full-rank feature recording and all
  exact paper exits were advanced at the end of the SignalMonitor scan cycle.
  That coupled feature cadence and target/stop observation latency to scanner
  work. It also made future heavier scanner cycles capable of degrading the
  evidence tape used to judge the strategy.
- `scripts/run_full_rank_evidence_recorder.py` now provides a separate
  paper-only worker. It reads the ticker's atomic local snapshot, records
  full-universe rank features every 15 seconds, and advances ask-to-bid,
  nine-contract, and six-entry-timing observers every 5 seconds. It creates no
  Kite client, sends no alert, and has no broker-order route.
- The assistant supervisor starts that worker after the first ticker snapshot
  and before SignalMonitor when
  `SIGNALMONITOR_STANDALONE_EVIDENCE=1`. SignalMonitor keeps the recorder
  object only for causal lane-decision journaling, skips duplicate feature
  snapshots, and does not instantiate duplicate exact observers.
- The normal live launcher defaults to one standalone owner with a 5-second
  poll. The health monitor requires its process and session log only in that
  mode. Operational readiness adds a twentieth critical check requiring a
  fresh `owner=standalone` status, at least 2,500 tick symbols, at-most
  20-second feature cadence, at-most 5-second observer cadence, and immutable
  `paper_only=true`, `alerts_allowed=false`, `real_orders_allowed=false`
  assertions.
- The worker refuses to advance any recorder or observer when the shared tick
  file is older than 15 seconds (or three poll intervals). It publishes
  `waiting_for_fresh_tick_snapshot` instead, preventing a dead ticker from
  fabricating repeated causal observations.
- Synthetic orchestration, stale-feed, ownership, launcher, health, and
  no-order-path tests pass alongside the existing scanner and all four
  evidence components. The production runtime is intentionally unchanged
  during July 27 market hours so the first potential generation-6 session is
  not interrupted. Deployment belongs after the session closes.

### 2026-07-27: Full-session delayed-runner historical experiment

- The broad historical checkpoint had a concrete discovery blind spot: it
  generated candidates only at 09:30, 09:45, 10:00, and 10:30. A stock that
  began its move later was absent from both model training and portfolio
  evaluation, even though delayed starters are an important part of the
  user's observed opportunity set.
- Before the 2,000-symbol checkpoint, the next experiment was frozen at 20
  completed-bar snapshots every 15 minutes from 09:30 through 14:15. Entry is
  always the next five-minute bar open. The final 14:15 observation enters at
  14:20 and its fixed 60-minute contract ends by 15:20, avoiding an assumed
  overnight hold.
- The candidate model comparison now includes eventual-runner classification,
  direct target-before-stop classification, and direct expected-return
  regression. Each objective is tested with snapshot, same-session temporal,
  completed-bar path, and causal Nifty regime context where available. The
  family registry contains 15 predeclared families including deterministic
  long/short rank acceleration and the existing fixed-time expected-return
  registry.
- Walk-forward models train only on prior sessions. Threshold, signal-window,
  top-per-snapshot, slot count, and strategy-family selection use only the
  train and validation periods. The final chronological period remains
  untouched until one family has already been selected.
- Portfolio replay uses observed next-bar entry and contract exit timestamps,
  one or two unleveraged equal-capital slots, strict same-bar cash reuse,
  no repeat symbol in a session, no-trade cash days, and at most ten executed
  trades per day. The extra 0.10% stress haircut remains on top of the base
  replay cost. The future-informed opportunity bound is also capped at ten
  trades so it cannot inflate the ceiling merely because the candidate tape
  now has 20 snapshots.
- A broad historical goal result can be reported only after at least 2,800
  quality cache files and 98% mapped-universe coverage. The exact final
  requirement is the larger of those two values. It still cannot
  promote paper or real orders: current-live-universe survivorship bias and
  a separate untouched live-forward phase remain mandatory.

### 2026-07-27: Bounded rolling search and reachable broad gate

- The 20-snapshot delayed-runner tape initially created 20 cumulative strategy
  windows. Combined with five thresholds, two per-snapshot ranks, two slot
  counts, and raw/stress replay, that produced hundreds of largely redundant
  calendar simulations per model family. All 20 causal observations remain in
  the candidate tape, but strategy selection is now frozen to three
  economically distinct cumulative windows ending at 10:30, 12:30, and 14:15.
  This preserves opening, midday, and delayed-runner participation while
  reducing multiple-comparison overfit.
- Registry evaluation no longer retains the daily and trade DataFrames for
  every search configuration. It rebuilds raw and stress artifacts only for
  the configuration selected on train plus validation data. This changes
  memory use, not selection metrics or the untouched final-period contract.
- Exact capital replay now normalizes timestamps, returns, ordering, and date
  groups once per calendar simulation instead of once per day. Empty cash days
  use a constant-time path and remain in every average-day denominator.
  Regression selection labels remain floating point rather than being
  truncated to integers.
- On the real 5,679-row, 436-session, 20-snapshot smoke tape, the full
  `bar_path_regime` expected-return command fell from a timeout beyond 180
  seconds to 37.10 seconds with 306 MB peak RSS. The smoke universe contains
  only ten stocks and is therefore a performance/correctness check, not
  profitability evidence.
- A diagnostic fallback can no longer choose a zero-trade threshold merely
  because all tested returns are negative. Gate-passing configurations retain
  the same selection order; only failed-sample reporting now prefers a
  configuration with actual pre-final observations.
- The fixed 2,900-file gate was unreachable after the current broker map
  settled at 2,889 symbols. The final checkpoint remains labelled `2900`, but
  its immutable trigger and status now require
  `max(2,800, ceil(0.98 * mapped_symbols))`. At the current map that is 2,832
  quality-eligible files. A small fully cached universe still fails the 2,800
  absolute floor, and the final checkpoint still cannot enable paper or real
  orders by itself.

### 2026-07-27: Twenty-snapshot historical result

- A broker-free diagnostic reran the full historical candidate builder over
  the real 1,085-file checkpoint cache with all 20 completed-bar observations
  from 09:30 through 14:15. It produced 467,650 causal candidates over 483
  sessions and 1,132 symbols. The runtime live process and generation-6 tape
  were not modified.
- Delayed observations materially improve discovery coverage but do not create
  entry edge. Recall of catchable future 5% runners rose from 18.29% at 09:30
  to 51.48% by 14:15. Roughly half of detected runners still reached the
  configured post-detection target, showing why a top-gainer label alone is
  not a buy decision.
- All four eventual-runner classifiers failed. Their stressed validation
  averages ranged from about Rs -1,287 to Rs -1,970 per Rs500,000 day.
  Predicting eventual top-gainer membership is therefore retained only as an
  opportunity/recall label, never as a trade label.
- All four direct target-before-stop classifiers failed with only 25-28%
  selected precision and stressed validation near Rs -2,598 to Rs -3,119 per
  day. Snapshot, temporal, completed-bar path, and Nifty regime context did
  not rescue the family.
- All four cumulative expected-return models failed their sample or selection
  gate. The least harmful variants selected only 7-8 pre-final trade days and
  remained statistically indistinguishable from zero. The long and short
  deterministic rank-acceleration baselines also failed, at about Rs -1,220
  and Rs -947 stressed validation P&L respectively.
- Phase-specific expected-return models at 09:30, 10:30, 12:30, and 14:15 all
  failed. The least-bad 14:15 bar-path variant averaged about Rs -383/day in
  stressed validation and Rs -110/day in the untouched final period, with a
  negative lower 95% confidence bound.
- This closes threshold, label, and signal-time tuning on the current
  historical OHLCV/rank information set. More candidate visibility is useful,
  but does not separate continuation from exhaustion. The next independent
  evidence source is the frozen generation-6 live microstructure tape:
  executable bid/ask, rank velocity, spread, depth, fill, and exact exit
  outcomes. It must accumulate the predeclared 12 selection sessions and 20
  untouched forward sessions before any paper promotion.
- The fixed-time registry previously spent all four phase comparisons between
  09:30 and 10:30. Its frozen phase observations are now 09:30, 10:30, 12:30,
  and 14:15 so the next broad checkpoint measures opening, late morning,
  midday, and delayed-runner behavior without increasing the family count.

### 2026-07-27: Prior-session paper ranker forward judge

- The live frozen-microstructure paper lane is now judged separately from the
  rejected retrospective ranker rules. Its model is rebuilt only from sessions
  before the trading date, and every selected prediction must match an exact
  observed-ask paper entry. Exits use observed bids and the frozen
  `t1.5_s1.0` contract.
- The first July 27 observation closed three paper trades for a net
  `Rs +2,968.65`: KPITTECH `Rs -5,666.56`, ACUTAAS `Rs +6,982.23`, and ARVIND
  `Rs +1,652.98`. This is encouraging but is one incomplete sample, not
  evidence that the strategy is profitable.
- `scripts/assess_prior_session_ranker_paper.py` accepts only complete
  generation-6 sessions beginning July 27. It verifies prior-session model
  provenance and hashes, selected-prediction/trade correspondence, closed
  positions, valid timestamps, one-slot non-overlap, unique symbols, and the
  ten-trade daily cap. Invalid sessions are excluded and reported.
- Every eligible market day remains in the denominator, including days with
  predictions but no selected trade. The judge applies an additional 0.10%
  cost stress to deployed capital beyond the paper lane's embedded costs.
- Promotion requires at least 20 complete forward sessions, ten sessions with
  trades, 20 trades, stressed average daily P&L of at least `Rs 10,000`, at
  least 50% `Rs 10,000` goal-hit days, a positive lower 95% confidence bound,
  and a positive block floor. The output always keeps
  `paper_promotion_allowed=false` and `real_orders_allowed=false`; a separate
  safety decision is required even if the statistical gate eventually passes.
- The EOD pipeline, readiness checker, dashboard, and capital-growth projection
  now include this judge. Missing code or missing EOD wiring fails operational
  readiness, and the dashboard shows aggregate stressed forward evidence
  separately from the current day's paper P&L.

### 2026-07-27: Frozen-ranker ownership and exact contract expansion

- A deployment review found that standalone evidence ownership would have
  stopped the frozen-ranker lane. SignalMonitor skips inline feature recording
  when `SIGNALMONITOR_STANDALONE_EVIDENCE=1`, but the prior-session paper
  ranker was still called only from that skipped method. The standalone worker
  now owns the ranker whenever it owns the feature recorder. Legacy inline
  mode retains the same lane, so there remains exactly one owner.
- Operational readiness now assumes the launcher's standalone and frozen
  ranker defaults. During market hours it requires the standalone status to
  report `frozen_ranker_enabled=true`, in addition to fresh ticks, generation-6
  recording cadence, paper-only assertions, and no alert/order path.
- The one-slot ranker remains the primary frozen paper policy. In parallel,
  every prediction scoring at least `0.20` is registered at its observed ask
  and tracked against live bids across the nine predeclared exit contracts.
  Each outcome preserves expected return, move band, model training cutoff,
  and model hash. This increases evidence coverage without generating a buy,
  alert, or broker order.
- `scripts/assess_frozen_ranker_prediction_matrix.py` replays those exact
  outcomes with one, two, and three unleveraged equal-capital slots, strict
  slot occupancy, same-timestamp score ranking, same-time capital reuse after
  an observed exit, no repeat symbol, and no more than ten trades per day.
  Another 0.10% cost is deducted beyond the observer's spread and 0.10%
  stress charge.
- Contract and slot count are selected only on the first 12 complete
  generation-6 sessions beginning July 28. The selected policy is then frozen
  for at least 20 untouched sessions. Acceptance requires at least ten forward
  trade sessions, 30 trades, stressed average daily P&L of `Rs 10,000`, at
  least 50% goal-hit sessions, a positive lower 95% confidence bound, and a
  positive chronological block floor.
- This matrix is now mandatory in EOD automation, pipeline integrity,
  dashboard reporting, and capital-growth projection. Both paper promotion
  and real orders remain hard-disabled regardless of an intermediate result.

### 2026-07-27: Frozen prediction score threshold selection

- The first prediction-matrix implementation recorded all prior-session model
  scores at or above `0.20`, but replayed only that one threshold. That mixed
  weaker predictions with stronger ones and left a meaningful entry-quality
  decision untested.
- `0.20` remains the immutable recording floor, preserving every candidate and
  exact contract outcome. The selection matrix now predeclares score
  thresholds `0.20`, `0.25`, `0.30`, and `0.40` alongside all nine exit
  contracts and one, two, or three unleveraged capital slots.
- The first 12 complete generation-6 sessions select threshold, contract, and
  slot count jointly. All three are then frozen before the minimum 20-session
  untouched forward period. A threshold cannot erase a session from the
  denominator; a day with no qualifying score remains a zero-P&L cash day.
- A candidate policy must trade in at least six of the 12 selection sessions
  and execute at least six trades. Otherwise selection is rejected instead of
  allowing a zero-trade threshold to outrank an active but losing policy.
- Coverage validation still compares the complete `>=0.20` prediction set with
  exact observed-ask registrations and all nine observed-bid outcomes. Higher
  selection thresholds filter only portfolio replay, never source evidence.
- The acceptance gate remains unchanged: at least ten forward trade sessions,
  30 trades, Rs10,000 stressed average daily P&L, 50% goal-hit sessions, a
  positive lower 95% confidence bound, and a positive chronological block
  floor. Paper promotion and real orders remain disabled.

### 2026-07-27: Capacity-bounded compounding

- The capital projection previously applied an accepted strategy's daily
  return percentage to the entire future account balance. That silently
  assumed every compounded profit and Rs2,00,000 monthly contribution could be
  deployed with unchanged fills and liquidity.
- Evidence-based projections now require an explicit tested capital value in
  the accepted strategy artifact. Daily strategy return applies only to the
  lesser of current equity and that validated capacity.
- Capital above the tested capacity is reported as
  `unvalidated_excess_capital` and earns no projected strategy return. It can
  enter the projection only after a separate larger-capital forward test
  validates execution capacity.
- The 0%, 0.25%, 0.50%, 1%, and 2% whole-account rows remain labelled
  mathematical scenarios rather than predictions. No projection enables real
  orders.

### 2026-07-27: Displayed-quantity execution evidence

- The ticker snapshot previously preserved best bid and ask prices but dropped
  the displayed quantities at those prices. Paper trades could therefore
  claim the full Rs5,00,000 filled at one price even when the top level showed
  only a small number of shares.
- Compact Kite ticks now preserve `best_bid_quantity` and
  `best_ask_quantity`. Full-universe features record both values, and exact
  paper entries/outcomes persist requested quantity, displayed entry ask
  quantity, displayed exit bid quantity, and separate entry/exit top-level
  fillability flags.
- The frozen prior-session paper trader caps its simulated entry quantity at
  displayed best-ask quantity. Its strict aggregate judge rejects a session if
  any selected trade was not fully executable at both the recorded entry ask
  and exit bid.
- The all-prediction contract matrix still records unfillable candidates so
  opportunity coverage is not hidden. Portfolio replay excludes them from P&L
  and causal ranking. A high-scoring but unfillable candidate cannot block a
  lower-scoring executable candidate.
- July 27 frozen-paper P&L predates this quantity evidence and is diagnostic
  only. Strict quantity-aware selection begins with the July 28 files after
  the post-close deployment. This change records evidence and cannot place or
  enable broker orders.

### 2026-07-27: Post-close deployment rehearsal

- The complete paper-evidence change set was cherry-picked onto a detached
  worktree from the live runtime commit `1735582b5`. Runtime-only test
  fixtures were reconciled with recorder generation 6, NSE exchange
  provenance, and displayed-quantity fields; production behavior was not
  changed by those fixture edits.
- The focused runtime rehearsal passed `60/60` tests. The full runtime suite
  passed `503` tests with `16` failures, all inherited from the known runtime
  baseline. The displayed-quantity change introduced no new failure.
- A file-level parity audit found all changed production modules equal to the
  development branch except the runtime's pre-existing omission of the
  optional assistant daily-profit-stop field. The standalone evidence owner,
  frozen ranker, exact contract matrix, EOD judges, readiness checks, and
  hard-off broker flags match the development build.
- At `12:54 IST`, the uninterrupted July 27 tape had `725` generation-6
  snapshots from `09:15:01`, a maximum observed snapshot gap of `43s`,
  `200/200` leader-pool recall, `10/10` rapid-climber recall, and fresh ticks
  for about `2,875` symbols. Real-order flags remained disabled.
- The old runtime reported `16/20` operational checks because it does not yet
  contain the two frozen-ranker judges or standalone recorder script, does not
  wire those judges into EOD, cannot report the predeclared score-threshold
  matrix, and still records evidence inside SignalMonitor. These are expected
  deployment-state gaps. The market-open process must not be restarted to fix
  them; deploy the rehearsed build only after the July 27 EOD evidence run.
- The runtime deployment must preserve its intentional documentation/test
  deletions and generated `results/Indices/NSE_BSE_COMBINED.csv` modification,
  push the resulting runtime commit to its own `tr-assistant` remote, keep all
  real-order flags off, and pass post-deployment operational readiness before
  the next market session.

### 2026-07-27: Executable primary-paper fallback

- The first quantity-aware primary policy could cap an entry to the displayed
  ask quantity after selecting it. That partial fill would occupy the only
  slot, and the strict judge would then exclude the whole session because the
  requested Rs5,00,000 was not executable at the recorded ask.
- Primary selection now ranks only candidates whose displayed best-ask
  quantity can fill the complete requested quantity. A higher-scoring
  unfillable candidate is recorded with
  `insufficient_top_level_ask_quantity`, while the next fully executable
  candidate remains eligible for the slot.
- The all-prediction contract matrix continues to record both candidates and
  marks fillability explicitly, so this execution rule does not hide
  opportunity coverage. Development passed `624` tests; the updated runtime
  rehearsal passed `503` tests with the same `16` inherited baseline failures.

### 2026-07-27: Opening-gap family audit

- A genuinely separate historical family tested overnight information plus
  the first completed 15-minute range, rather than another late-leader
  threshold. The fixed setups were gap-up hold, gap-down reclaim, gap-up
  rejection short, and gap-down breakdown short.
- Signals use only the 09:15, 09:20, and 09:25 completed bars. Entry is the
  09:30 bar open. Opening-volume ratios use only the preceding 20 sessions,
  with at least ten prior sessions, Rs50 minimum price, Rs1 crore opening
  turnover, five basis points adverse entry slippage, and another 0.15% cost.
- The predeclared matrix crossed each direction-compatible setup with raw or
  1.25x prior opening volume, five 60/120/180-minute target-stop contracts,
  and one, two, or three equal-capital slots. Cash assigned to an unused slot
  remains cash. There were 120 configurations; none could enable paper or
  broker orders.
- The 505-symbol exact cache produced `19,692` candidates over `483` sessions.
  Selection used the first `297` calendar sessions for training, the next `99`
  for validation, and four chronological pre-final blocks.
- No configuration was positive in training, validation, and every selection
  block. The diagnostic least-bad row was gap-up rejection short with 1.25x
  opening volume, two slots, and the 180-minute `+3%/-1.5%` contract. It
  averaged only `+0.036169%` in training and `+0.025265%` in validation, while
  its weakest pre-final block was `-0.034387%`.
- Because selection failed, the final `99` sessions were not opened or scored.
  The family is rejected before final, with historical goal, paper promotion,
  and real orders all false. Do not tune its thresholds against the withheld
  period. A later broad-universe run may replicate the same frozen matrix at
  the immutable final cache milestone, but cannot rewrite this result.
- Reproducible code is in
  `scripts/backtest_opening_gap_registry.py`; focused tests cover mutually
  directional setup classification, prior-only volume, 09:30 next-bar
  execution, unused-slot cash, final-period withholding, and hard-off orders.

### 2026-07-27: Eventual-runner capture funnel

- Added `scripts/audit_live_runner_capture_funnel.py` to join the existing
  hindsight runner-recall labels to the causal live pipeline: recorder
  visibility, contemporaneous quote/liquidity buyability, the +0.5%-3% early
  band, prior-session frozen-model scoring, the predeclared 0.20 score floor,
  exact top-level quantity registration, and primary paper selection.
- Every runner is assigned its first failed stage. All timestamps must precede
  the runner's first threshold-crossing bar; a later prediction cannot receive
  early-capture credit. Opening gaps that crossed before the recorder started
  are excluded from conversion-rate denominators rather than counted as
  detection failures.
- A provisional isolated July 27 audit used the still-open session only as a
  diagnostic. Of 50 stocks that had reached +5% from the open, four crossed at
  the opening bar and 46 were catchable after recorder startup. The recorder
  saw 45/46 before +5%; 29/46 were contemporaneously buyable, 21/46 were
  buyable while still between +0.5% and +3%, 20/46 were scored by the frozen
  model, seven cleared the fixed 0.20 score threshold, and one was selected
  before crossing +5%.
- The dominant July 27 loss stage was contemporaneous buyability: 16 catchable
  runners never simultaneously passed NSE quote, spread, price, and Rs3 crore
  turnover gates before +5%. This proves the system is no longer primarily
  missing eventual leaders at the recorder; the larger bottlenecks are
  executable liquidity and causal ranking.
- July 27 did not contain the new displayed-quantity contract matrix, so its
  six score-qualified but unselected runners are not evidence of fillable
  missed profit. Starting July 28, the funnel consumes
  `frozen_ranker_prediction_contract_matrix` registrations and requires exact
  top-level ask capacity before calling a candidate executable.
- The audit also credits the independent broad exact first-observation lane.
  CAPLIPOINT, CARTRADE, CONCOR, RATNAVEER, and THYROCARE were not scored early
  by the three-sample frozen continuation model, but the broad lane registered
  them at observed asks before they crossed +5%. Fast first-observation bursts
  therefore remain a separate selector family; they must not be forced into
  the continuation model or counted as architecture-wide detection misses.
- Broad registrations are also replayed through the feature gates and
  integrated policies declared before outcomes. A hindsight runner only
  counts as policy covered when a frozen policy could have selected its
  point-in-time registration; otherwise it is labelled
  `broad_lane_not_covered_by_policy_registry`.
- The funnel is mandatory in EOD integrity and visible on the dashboard. It
  uses future runner labels, is diagnostic only, and has paper promotion and
  real orders hard-disabled.

### 2026-07-27: Fixed rank-first untouched-forward hypothesis

- The incomplete July 27 exact contract matrix exposed one causal combination
  with provisional shared-capital P&L above the Rs10,000 hurdle:
  `rank_first`, contract `t1_0_s1_0_h60`, one reusable Rs5 lakh slot, and at
  most ten trades. Its provisional result was Rs10,652.75 from six winners and
  four stops. This is a hypothesis-selection observation, not accepted
  evidence.
- Added `scripts/assess_fixed_rank_first_forward.py`. The selector, contract,
  capital, risk terms, July 27 selection cutoff, and July 28 forward start are
  immutable in source. July 27 is permanently excluded from forward evidence.
- The fixed judge runs at EOD and requires 20 untouched complete generation-six
  sessions, at least 75% traded sessions, at least 50 trades, average daily P&L
  of Rs10,000 or more, a positive 95% lower confidence bound, a positive
  four-block floor, at least 50% goal-hit days, and no day below -2.5%.
- The lane is paper-only during collection. Future outcomes cannot retune the
  hypothesis, and real orders remain hard-disabled even if the forward gate
  eventually passes.

### 2026-07-29: Executable evidence correction

- The July 28 raw generation-six session is complete: 1,445 snapshots from
  09:15:04 through 15:30:54, with 99.4% outcome coverage for promotion
  candidates. The EOD judges initially failed to recognize it because direct
  cron execution could not import the runner-funnel package and the frozen
  prediction matrix compared timezone-aware entries with timezone-naive exits.
  Both defects now have direct-command regression coverage.
- Exact full-rank coverage was also blocked by four GALLANTT labels. Their
  displayed asks held only 3-17 shares while a Rs5 lakh simulation requested
  about 880 shares. They were not trades and never produced a later quote.
  Exact one-slot judges now causally exclude labels that could not fill at the
  displayed entry ask. The 100% exit-coverage requirement remains unchanged
  for every executable entry.
- This correction invalidates the July 27 provisional `rank_first` result.
  Its apparent Rs10,652.75 used non-executable entries. The frozen artifact is
  retained with status `invalidated_non_executable_selection`; it can never
  enable paper promotion or real orders.
- July 28 initially appeared to be the first complete top-level-entry session.
  The invalidated rank-first `t1_0_s1_0_h60` rule lost Rs18,051 over ten
  trades. Across the predeclared matrix, acceleration-first with the same exit
  contract made Rs5,265 and the sector-rotation policy made Rs5,975. These
  remain diagnostic because only the best bid quantity, not the complete
  executable exit book, was captured.
- The existing delayed-entry matrix showed Rs10,880 for a +0.25% confirmation
  variant, but its old observer did not prove displayed ask quantity at the
  delayed fill. That result is therefore diagnostic, not executable evidence.
  New delayed fills require enough displayed ask quantity for the complete
  simulated order and persist ask quantity, requested quantity, and the
  fillability decision. Historical timing outcomes without those fields cannot
  enter selection or forward evidence.
- The hindsight opportunity ceiling for July 28 was about Rs67,000, while the
  best causal direct selector captured Rs5,265. Profitable movement existed;
  causal ranking and entry confirmation remain the principal research
  bottlenecks. No conclusion may be promoted from this single session.
- Development verification passed 644 tests. All broker-order flags and every
  real-order capability remain disabled.

### 2026-07-29: Post-recorder EOD scheduling

- The live-evidence EOD judge previously started at 15:35 IST while the live
  recorder could continue writing until about 16:20 IST. This allowed the
  judge to race the final session flush and temporarily classify a complete
  session as incomplete.
- The weekday EOD job now runs at 16:25 IST, after the recorder has stopped.
  Its installer replaces stale schedules instead of treating any existing
  `live_evidence_eod.sh` cron entry as current.

### 2026-07-29: Five-level execution evidence

- A July 28 liquidity audit found that only 10-40% of causally selected trades,
  depending on slot count and exit contract, could liquidate their simulated
  quantity at the single captured best bid. Selecting only those liquid exits
  after seeing them would be look-ahead bias, so all historical top-level-only
  results remain diagnostic and cannot qualify for promotion.
- The paper, contract-matrix, and delayed-entry observers now persist up to
  five displayed ask levels at entry and five bid levels at exit under
  execution schema version 2. They retain top-level fields for compatibility.
- Added a separate five-level execution judge covering nine fixed exit
  contracts, four causal selectors, and one/two/three/five capital slots: 144
  predeclared combinations. Entry selection requires at least 80% of a slot to
  be deployable through the contemporaneous ask book. A selected trade must
  later liquidate its complete quantity through the captured bid book; any
  failure makes that strategy-session incomplete instead of disappearing from
  P&L.
- The new lane still requires 12 complete selection sessions followed by 20
  untouched forward sessions, positive confidence and block floors, at least
  50% Rs10,000 goal-hit days, and no day below -2.5%. It is paper-only and all
  broker-order flags remain disabled.

### 2026-07-29: Ranked Grade-B protective-exit audit

- The July 29 `SASKEN` paper trade reached `+0.9998%` MFE without crossing its
  exact `+1.00%` target, then reversed into a stop. This raised a causal
  hypothesis that profit locks or trailing stops might repair the lane.
- The ranked paper executor now supports disabled-by-default protective stops.
  A stop can move to a fixed profit floor only after the activation return is
  observed, or trail an already-observed high watermark by a fixed distance.
  Exits still use the contemporaneous best bid, adverse slippage, and fees.
- Five policies were declared before replay: activate at `+0.50%` and lock
  `+0.15%`; activate at `+0.75%` and lock `+0.25%`; activate at `+0.90%` and
  lock `+0.50%`; and two fixed trailing variants. They were evaluated with the
  existing nine configurations across all 12 complete ranked-confirmation
  tapes at Rs5 lakh capital and a Rs10,000 daily goal.
- None was profitable after the standard extra execution stress. Baseline
  averaged approximately `-Rs414/day`; the least-bad profit lock averaged
  approximately `-Rs406/day`; the tightest trailing variant averaged
  approximately `-Rs593/day`. The best configuration in the whole registry
  was the existing early-session entry filter at approximately `-Rs106/day`,
  still negative.
- Therefore no protective-exit policy is promoted. Exit handling alone does
  not repair the ranked Grade-B lane; candidate selection and entry quality
  remain the primary failure. The historical tapes contain top-of-book rather
  than five-level depth, so this audit is diagnostic and cannot authorize real
  orders in any case.

### 2026-07-29: Cross-lane evidence scoreboard

- Added `scripts/build_intraday_strategy_evidence_scoreboard.py` to compare
  ranked confirmation, ignition, runner fade, frozen continuation,
  prior-session ranking, full-universe capital allocation, and the new
  five-level lane in one artifact.
- Every row carries its evidence class. Top-book counterfactuals, partial
  sessions, and minute-outcome research remain diagnostic even when their
  average P&L is positive. Only an accepted schema-version-two five-level
  forward result can set `CapitalAllocationComparable=true`.
- The provisional scoreboard has zero accepted or capital-comparable lanes.
  Frozen rank migration is the strongest live-forward diagnostic at
  approximately `Rs3,404/day` after stress over five sessions and 21 trades,
  but its 95% bound remains negative and it lacks five-level execution proof.
  Frozen leader continuation averages approximately `Rs937/day` after stress
  with the same sample limitation. The partial capital-allocation result
  averages approximately `Rs3,190/day`, but is explicitly excluded because
  its sessions are top-book-only and incomplete.
- The scoreboard is rebuilt after the individual EOD judges and is now a
  mandatory EOD integrity artifact. It always hard-codes real orders off.

### 2026-07-29: Frozen rank-migration executable forward boundary

- The positive frozen rank-migration result remains diagnostic because its
  July 22-29 observations were inspected while choosing this family and used
  only top-of-book prices.
- A separate untouched forward contract is frozen before the next session:
  selection cutoff `2026-07-29`, forward start `2026-07-30`, model training
  cutoff `2026-07-21`, score threshold `0.25`, and exit contract
  `t1_5_s1_0_h60`.
- Before freezing, 24 top-book diagnostics were compared across six score
  floors (`0.15` through `0.40`) and one/two/three/five slots. The one-slot
  `0.25` floor was best at approximately `Rs5,701/day` after stress over the
  five inspected sessions, versus approximately `Rs3,404/day` for the broader
  `0.20` floor. Multi-slot variants were worse. These inspected results are
  selection evidence only and cannot count in the July 30 forward test.
- The replay uses one Rs5 lakh slot and at most 10 sequential trades per day.
  At least 80% of the slot must be deployable through the displayed five-level
  ask book. The complete quantity must later be sellable through the captured
  five-level bid book; otherwise the whole session is incomplete.
- The rank-migration contract observer now receives the live entry depth and
  writes a dated safe-status heartbeat. This lets a genuine zero-trade session
  count as zero while excluding days when the lane was absent.
- `scripts/assess_frozen_rank_migration_depth_forward.py` selects the highest
  executable model score in each causal minute batch while the slot is free.
  It requires 20 complete untouched sessions before acceptance can be
  evaluated and always keeps `real_orders_allowed=false`.
- The judge is required by the nightly pipeline, EOD integrity manifest, and
  cross-lane scoreboard. Only an accepted schema-v2 result may become
  capital-allocation comparable; the earlier top-book result cannot.

### 2026-07-29: Broad opening-family replication

- Re-ran the frozen opening-gap registry against the complete runtime history:
  `1,770` stock files, `39,242` causal candidates, `483` candidate sessions,
  and `495` Nifty calendar sessions. The setup definitions, costs, contracts,
  capital slots, chronological split, and final-period withholding were not
  changed after the earlier 505-symbol audit.
- The result strengthened the rejection. No configuration was positive across
  training, validation, and all pre-final blocks. The diagnostic least-bad
  configuration was gap-up rejection short, `1.25x` opening volume, three
  slots, and the 120-minute `+3%/-1.5%` contract. It averaged approximately
  `-Rs515/day` in training and `-Rs361/day` in validation; its weakest
  selection block averaged `-0.136289%`.
- Because selection failed, the final 99 sessions remained withheld. Historical
  goal, paper promotion, and real-order flags are false. The broad result is
  stored at runtime under `results/Data/opening_gap_registry_v1/`.
- Also generated `98,715` fixed-time leader candidates across 482 sessions and
  tested 360 cost-aware timestamp/target/stop/slot configurations. None passed;
  the highest-ranked fixed rules still lost roughly `0.15%` to `0.20%` of
  capital per day.
- Tested a 09:30 walk-forward expected-return model twice: once with snapshot
  features and once with the completed opening bar path. Every model was
  trained only on prior dates and entered at the next five-minute bar open.
  Both feature sets were negative in the selectable train/validation periods;
  neither passed the sample, stress-cost, or chronological-block gates.
- Decision: do not create another opening-gap, fixed early-leader, ORB, or
  failed-leader fade paper lane from these results. The only material but
  unproven continuation result remains frozen rank migration. Its new July 30
  forward test must use contemporaneous five-level depth and cannot inherit
  any inspected July 22-29 outcome.

### 2026-07-29: Stop-avoidance selection lane

- Expected-return regression and direct target classifiers did not separate
  profitable rank-migration entries. A distinct downside-aware objective was
  therefore tested: estimate the probability that the fixed
  `+1.5%/-1.0%/60-minute` contract exits for any reason other than its stop.
  This is a selection model, not a wider-stop change; the executable contract
  remains fixed.
- The predeclared diagnostic used prior-session walk-forward training,
  equal-session weights, one Rs5 lakh capital slot, at most five sequential
  trades per day, score threshold `0.78`, and an extra `0.10%` execution-cost
  stress. Over nine selectable sessions it chose 45 trades, averaged
  `+1.071473%` or approximately `+Rs5,357/day`, produced eight positive
  sessions, had a `+0.065642%` 95% lower bound and a `+0.658360%` weakest
  chronological block. The worst session was `-1.509464%`; only two sessions
  reached the Rs10,000 goal.
- These figures are selection evidence only. The threshold and five-trade cap
  were chosen after comparing a small diagnostic grid, the sample has only
  nine scored sessions, and the result does not prove executable five-level
  fills. It cannot authorize alerts, paper promotion, or real orders.
- The frozen artifact is explicitly typed
  `prior_session_rank_migration_stop_avoidance`, emits `P(no stop)` rather
  than a class label, rejects unsafe artifact metadata, and carries
  `paper_only=true`, `alerts_allowed=false`, and
  `real_orders_allowed=false`.
- The separate untouched boundary starts `2026-07-30`. Its paper recorder
  enters at the contemporaneous displayed ask, requires enough ask depth,
  exits against contemporaneous bid depth, records costs, and writes an
  independent nine-contract matrix. The judge requires 20 complete sessions
  with schema-version-two five-level evidence before acceptance is even
  evaluated. Missing lane heartbeats, unfillable entries, or missing exits
  make a session incomplete rather than silently improving P&L.
- The lane is now part of the standalone recorder, EOD pipeline, integrity
  manifest, and cross-lane scoreboard. It has no Discord alert path and no
  broker-order path.

### 2026-07-29: Runner-qualified stop-avoidance challenger

- A live midday attribution audit found 64 stocks above `+5%` and ten above
  `+10%` in the 2,862-symbol tick universe. All 64 were present in the
  generation-six feature tape. Forty-two had been seen between `+0.5%` and
  `+3%`, but only two could absorb a complete Rs5 lakh order at the single
  displayed ask level. This confirms that discovery is no longer the dominant
  failure; selection and executable depth are.
- Fixed smaller concurrent allocations did not repair the economics. Across
  the same stop-avoidance scores, two/three/five/ten capital slots were flat or
  negative after stress. The only promising selection remained one full
  capital slot reused sequentially. Five-level depth collection is therefore
  needed to learn whether concentrated entries are actually executable.
- A standalone early-runner classifier was trained only on prior sessions. It
  had modest ranking power (`AUC 0.6671`) but no score/cap combination passed
  the positive confidence and chronological-block gates. Early-only
  `+0.5%`-to-`+3%` filters, buyer/seller filters, pullback limits, and
  noon/13:00 cutoffs were also materially worse. This rejects the assumption
  that buying every low-percentage mover is an easy profitable strategy.
- Daily loss stops at `-0.5%`, `-1.0%`, or `-1.5%`, plus two/three consecutive
  loss limits, reduced average P&L because early losses were often followed
  by recoveries. None repaired the negative block. These risk policies are
  rejected for this selection family.
- The only improved diagnostic is a two-stage objective: require
  `P(no stop) >= 0.78`, then rank accepted candidates by the probability of
  reaching or continuing through the `+5%` leader state. With one Rs5 lakh
  slot and at most ten sequential trades, its nine-session stressed selection
  result was 76 trades, `+1.603134%` or approximately `+Rs8,016/day`, a
  `+0.146011%` 95% lower bound, seven positive sessions, three Rs10,000 goal
  days, and a worst day of `-0.818795%`.
- The challenger is not robust enough for promotion: its weakest chronological
  block is still `-0.595901%`. It is frozen only as a separate diagnostic
  forward collector starting `2026-07-30`, with a distinct artifact type,
  five-level contract ledger, and 20-session untouched judge. Its prediction
  method emits `-1` for every candidate below the stop-safety floor, preventing
  the continuation score from bypassing the first-stage risk gate.
- `paper_promotion_allowed`, `alerts_allowed`, and `real_orders_allowed` are
  false regardless of the selection average. The primary stop-only lane and
  this challenger have separate ledgers so forward comparison cannot rewrite
  either frozen hypothesis.

### 2026-07-29: Guarded runtime rollout for July 30 forward collection

- The two frozen selector implementations are at development commit `66844e7`;
  the matching runtime feature rehearsal is `a18cc10`. Runtime-only
  test-fixture alignment is `048f4a2`. The balanced portfolio judge is at
  development commit `f6bf66e`. The causal margin and fixed-notional forward
  lane is at development commit `60fa751`. The frozen rank-displacement lane
  is at development commit `a7fe839`, advancing the exact deployment target
  to runtime commit `d2905ab`.
- The complete development suite passed `696/696`; the isolated runtime suite
  passed `587/587`. The six aligned fixture
  files now use recorder generation six and explicit NSE provenance, matching
  the production evidence contract; no production behavior was weakened to
  make the suite pass.
- The live July 29 service remains on the prior runtime revision through the
  session. A guarded post-market job waits until `16:35 IST`, after the normal
  `16:20 IST` stop, and refuses to deploy if the service is active, runtime
  HEAD or worktree changed unexpectedly, or any real-order flag is enabled.
- After the fast-forward, the job reruns focused tests and the complete EOD
  evidence pipeline. It then requires both frozen model binaries and metadata,
  checks `valid_from=2026-07-30`, and verifies `paper_only=true`,
  `alerts_allowed=false`, and `real_orders_allowed=false`.
- July 30 is the first untouched five-level forward session for both the
  stop-only selector and runner-qualified challenger. Selection-period results
  remain diagnostic and cannot be mixed into the 20-session forward decision.

### 2026-07-29: Fixed balanced stop portfolio

- Session attribution showed that the stop-only and runner-qualified selectors
  had daily-return correlation of approximately `-0.10`: the stop-only lane's
  loss was concentrated on July 23, while the challenger losses were on July
  21-22. This suggested a fixed portfolio split rather than another
  post-hoc signal filter.
- A pre-forward `50/50` diagnostic assigns Rs2.5 lakh permanently to each
  frozen selector. Across the same nine selectable sessions it averaged
  `+1.337303%`, or approximately `+Rs6,687/day`, after the extra `0.10%`
  execution stress. All nine sessions were positive; the worst was
  `+0.013762%`, the 95% lower bound was `+0.493950%`, and the weakest
  chronological block was `+0.031229%`.
- This improves stability but does not establish the Rs10,000 goal: only one
  of nine selection sessions reached it. Linear projections at Rs7 lakh or
  Rs9 lakh are not evidence because market depth may not scale with capital.
- Added an untouched portfolio judge that replays the two existing five-level
  candidate ledgers with separate Rs2.5 lakh pools. It recomputes entry and
  exit VWAP from captured depth; it does not average the full-capital output
  files. Either component's missing heartbeat, stale model, invalid evidence,
  or unfillable exit makes the entire portfolio session incomplete.
- The allocation, model hashes, contract, and July 30 boundary are frozen.
  Acceptance still requires 20 complete sessions and the standard
  cost/capacity/goal gates. Alerts and real orders are unconditionally off.

### 2026-07-29: Causal MIS margin and fixed-notional forward test

- The fixed 50/50 portfolio averages approximately `Rs6,687/day` in inspected
  selection data, so merely projecting it to the Rs10,000 goal would not be
  evidence. A new calculation-only recorder queries Kite's basket-margin API
  for one-share NSE MIS orders with `consider_positions=false`. It requests
  the full response so each symbol is matched to its own `orders[].total`
  margin. The recorder has no order-placement method, emits no alerts, retries
  a failed symbol at most three times, and persists every quote or failure.
- A signal cannot reuse the depth snapshot that existed before its margin
  response. It is armed after the quote and may enter only on a strictly later
  tick using a fresh five-level ask book. Quotes expire after five minutes;
  candidates expire after three. Required margin must fit the fixed
  Rs2.5 lakh lane allocation.
- Two notional variants were declared before forward collection:
  `1.50x` is the primary Rs10,000/day hypothesis and `1.25x` is a safety
  diagnostic. The diagnostic cannot be selected for promotion from these
  forward outcomes. Both keep the same `+1.5%/-1.0%/60-minute` exit contract
  and separate Rs2.5 lakh stop-only and runner-qualified pools.
- The leverage judge requires the original selector heartbeat, the post-margin
  contract heartbeat, a causal leveraged registration for every original
  score-qualified candidate, full entry and exit depth, and complete exits for
  both halves. Missing API evidence, expired candidates, missing registrations,
  or unfillable exits make the session incomplete rather than zero-P&L.
- The untouched boundary remains `2026-07-30`. The primary `1.50x` contract
  requires 20 complete sessions and the existing positive confidence,
  chronological-block, goal-hit, execution-coverage, and worst-day gates.
  It remains paper-only; paper promotion is false until those gates pass, and
  alerts and real orders are always false.

### 2026-07-29: Frozen causal rank-displacement challenger

- The opportunity-ceiling audit identified capital occupancy as the largest
  remaining selection loss: a one-slot replay ignores stronger candidates
  while an earlier weak position remains open. Discovery itself is no longer
  the main limitation because generation-six recording covers the complete
  live leader pool.
- A post-hoc diagnostic tested one causal replacement rule on the prior-session
  stop-avoidance scores. After at least three minutes, an incumbent may be
  replaced only when a new candidate's frozen score is at least `0.05` higher
  and the incumbent has gained no more than `+0.5%`. The exit uses the latest
  recorded bid no more than 30 seconds old and charges the existing fee plus
  the extra `0.10%` stress cost.
- Across the same nine selection sessions, only two replacements occurred.
  The baseline remained 45 trades and approximately `Rs5,357/day`; the
  displacement replay remained 45 trades and increased to approximately
  `Rs7,288/day`. All nine sessions were positive, the worst was `+0.400532%`,
  and the incremental diagnostic P&L was approximately `Rs17,371`.
- This result is explicitly insufficient for promotion. The rule was chosen
  after inspecting historical alternatives, only two switches support it, and
  older tapes do not provide complete five-level depth. The historical status
  therefore sets `post_hoc_hypothesis_only=true`,
  `can_promote_strategy=false`, `paper_promotion_allowed=false`,
  `alerts_allowed=false`, and `real_orders_allowed=false`.
- The frozen forward policy is
  `rank_displacement_v1_20260729`, SHA-256
  `f0d5c1ff278787a802920bba3bf7a206fd5645dd8b0975808f1255d1af2d8df5`.
  It uses one Rs5 lakh slot, stop-score floor `0.78`, score advantage `0.05`,
  three-minute minimum hold, `+0.5%` incumbent progress ceiling,
  `+1.5%/-1.0%/60-minute` exits, five trades per day, and at least 95% capital
  utilization through displayed depth.
- Forward entries require generation-six NSE provenance, a model trained
  strictly before the session, a candidate no more than 30 seconds old, and
  complete five-level ask execution. Natural and displacement exits require a
  complete five-level bid fill. Every trade, event, and heartbeat carries the
  immutable policy hash and safety flags.
- The untouched judge starts `2026-07-30`, requires 20 complete sessions,
  at least 40 trades, uninterrupted lane heartbeats, valid five-level entry
  and exit evidence, average P&L of at least `Rs10,000/day`, positive 95%
  lower bound and chronological block, at least 70% positive sessions, at
  least 50% goal-hit sessions, and no day below `-2.5%`.
- The observer is separate from the original stop-only lane, so it cannot
  rewrite that baseline. It has no Discord or broker interface. It is wired
  into the standalone recorder, nightly evidence run, EOD integrity manifest,
  and cross-lane scoreboard.

### 2026-07-29: Broad intraday decliner-continuation rejection

- Tested a genuinely separate short-momentum family against all available
  broad-universe one-minute history: 17 sessions, roughly 1,700-2,500 symbols
  per session, and `105,949` causal candidate snapshots. This did not reuse
  scanner alerts, so stocks omitted by the current long scanner were included.
- Signals used only completed bars and required price below the causal session
  VWAP, a current decline from the open, proximity to the running day low,
  negative five-minute momentum, causal volume acceleration, at least three
  recent red bars, Rs3 crore cumulative turnover, and a Rs50 price floor.
  Entry was the next minute open with adverse slippage.
- The predeclared matrix crossed 48 signal policies, five
  target/stop/horizon contracts, and one/three/five fixed-capital slots:
  `720` configurations. Same-minute target/stop ambiguity was resolved as a
  stop, every exit paid adverse slippage and `0.15%` costs, reused slots
  remained occupied until the causal exit time, and unused slots remained
  cash.
- Chronological selection used the first seven sessions for training and the
  next five for validation. The last five sessions were diagnostic only.
  No configuration was profitable in both training and validation. In fact,
  all `720` configurations lost money in training; 45 later appeared positive
  in validation, demonstrating regime dependence rather than a selectable
  edge.
- The family is rejected. Historical minute bars do not prove short
  availability or executable order-book depth, and no apparent later-period
  winner may be promoted. `paper_promotion_allowed=false` and
  `real_orders_allowed=false`.
- Reproducible code is in
  `scripts/research_decliner_continuation.py`; regression tests cover adverse
  same-bar resolution, slippage/cost deduction, causal policy fields,
  unused-slot cash, and chronological split isolation.

### 2026-07-29: Nifty intraday regime-family rejection

- Tested an independent index-level family on `493` NIFTY 50 five-minute
  sessions from May 2024 through May 2026. The matrix contained symmetric
  trend continuation, VWAP-reference reversal, and previous-day range-break
  signals at 10:00 and 11:00, with 12 target/stop/horizon contracts:
  `240` configurations.
- Each signal used a completed five-minute bar and entered at the next bar
  open. Intrabar target/stop ambiguity was adverse, both sides paid slippage,
  the base cost was `0.05%`, and validation was repeated at `1.5x` cost.
  Nifty spot candles contain zero volume, so the code explicitly uses a causal
  expanding typical-price reference instead of mislabelling it as exchange
  VWAP.
- The first half of the 493 sessions was training, the next quarter
  validation, and the last quarter withheld from configuration selection.
  Seven configurations were positive in training, none were positive in
  validation, and no configuration had a positive four-block/cost-stress
  floor. The withheld period therefore was not used to rescue a rule.
- The index regime family is rejected before executable forward collection.
  Spot returns do not establish futures basis, lot size, margin, or five-level
  fills. No paper or broker lane was added; promotion and real-order flags
  remain false.
- Reproducible code is in
  `scripts/backtest_nifty_intraday_regime.py`; tests cover next-bar entry,
  reversal reference reclaim, adverse ambiguous-bar handling, and
  chronological split isolation.

### 2026-07-29: Live recorder pre-deployment audit

- At 13:40 IST the production generation-six recorder had run continuously
  since 09:15:03 across `2,857` live instruments. The status reported
  `100%` leader-pool recall and `100%` rapid-rank-climber recall, with alerts
  and real orders false.
- The existing top-level frozen rank-migration lane had generated 602 scored
  predictions but no simulated trade. Its maximum score was `0.216249` for
  `BALKRISIND`; that candidate was correctly rejected because the displayed
  top ask could not fill the requested quantity.
- This is an execution-evidence limitation, not a discovery outage. The old
  runtime does not yet contain the five-level stop-avoidance,
  runner-qualified, margin, or displacement recorders. Their rehearsed runtime
  commit remains scheduled for guarded post-close deployment, after the live
  service stops, so today's uninterrupted evidence is not disturbed.

### 2026-07-29: Shared five-level depth contract correction

- A pre-deployment audit found that KiteTicker subscribed every instrument in
  official `full` mode but `_compact_tick()` discarded depth levels two
  through five before writing the shared JSON snapshot. The new five-level
  observers read that shared file, so deploying them without correcting the
  producer would have produced permanent missing-depth sessions.
- The shared tick now preserves up to five buy and five sell levels, each with
  price, displayed quantity, and order count, while retaining the existing
  best-bid/best-ask compatibility fields. The ticker status declares
  `stream_mode=full` and `persisted_depth_levels_per_side=5`.
- A synthetic 2,900-symbol benchmark produced a 2.67 MB atomic JSON snapshot
  in approximately 0.013 seconds, comfortably below the live two-second save
  interval. The file is overwritten atomically rather than accumulated, so
  this does not create an unbounded storage stream.
- Focused tests verify exact five-level preservation and compatibility with
  the execution parser, full-rank worker, frozen ranker, and displacement
  trader. This producer fix must be included in the guarded post-close runtime
  target before July 30; otherwise the forward five-level boundary is not
  valid.
- The isolated runtime reconciliation preserves its intentional documentation
  and test deletions, relocates the producer regression into the runtime-owned
  order-book test, and passes `588` tests. Runtime commit
  `6c34305600c28eead510cac4bd458ae49d7ac3c4` is pushed to the
  `tr-assistant` main branch and is the exact guarded post-close target.
- The standalone evidence worker also fails closed on the producer contract.
  At least 80% of shared ticks must structurally contain buy and sell depth
  lists before any feature, paper, contract, margin, or displacement observer
  advances. Otherwise it writes
  `waiting_for_five_level_depth_snapshot` with measured coverage and leaves all
  observers untouched. Operational readiness now requires
  `depth_contract_valid=true` and at least 80% coverage, preventing a
  depth-less zero-trade day from being misclassified as valid evidence.

### 2026-08-02: Five-level EOD replay repair and capacity finding

- The July 31 EOD pipeline refreshed only 19 of 25 required artifacts. Raw
  generation-six evidence was intact, but three judge defects prevented a
  valid replay: mixed timezone encodings crashed entry/exit comparisons, the
  two frozen stop wrappers could not import `scripts` when cron executed them
  directly, and EOD integrity checked a nonexistent entry-timing directory.
- Timestamp loading now preserves naive IST wall-clock values while converting
  aware values to the same naive IST representation. Both direct wrappers add
  the repository root before importing, and EOD integrity checks
  `full_universe_rank_entry_timing/status.json`. Regression tests execute the
  wrappers from outside the repository, matching cron behavior.
- Frozen stop completeness now uses the same declared NSE scope as the
  five-level execution loader. Eight BSE records were incorrectly invalidating
  otherwise complete NSE sessions; old-schema or wrong-model NSE records still
  fail the session.
- Replaying the immutable July 30-31 files now recognizes both sessions for
  the stop, runner-qualified, balanced, and rank-migration forward judges.
  It does **not** manufacture a profitable result: allocating a full Rs 5 lakh
  slot produced five unfillable observed exits in rank migration, while the
  stop and runner lanes produced eight and thirteen respectively. The frozen
  50/50 Rs 2.5 lakh-per-lane portfolio still had fourteen exit-depth failures.
  These sessions therefore remain execution-incomplete and cannot promote a
  strategy.
- The first complete five-level selection session supports the architectural
  conclusion but is not enough for selection: five concurrent Rs 1 lakh slots
  were executable for the balanced rank/acceleration selector, whereas larger
  slots frequently were not. This is selection-period evidence only. The next
  challenger must freeze capacity-aware sizing and concurrent capital slots
  before collecting untouched forward outcomes; real orders remain disabled.
- The repair was reconciled into the runtime repository as commit `8f05c1d`.
  Its complete runtime suite passed `599` tests, the live runtime checkout was
  fast-forwarded to that exact commit while preserving only the generated
  combined-index CSV, and the service remained inactive on Sunday.

### 2026-08-02: Paper-ledger reconciliation gate

- The ranked confirmation ledger contains `29` persisted paper entries and
  `29` persisted exits over 15 sessions. Every session with an entry has an
  EOD summary; there are no orphaned entries. The July 27 `DIVISLAB` example
  is stored as an entry at Rs 7,342.6695 and a target exit at Rs 7,413.7913,
  with Rs 382.46 net after simulated costs.
- EOD integrity now verifies this automatically. It requires a daily summary,
  matching entry/exit symbol multisets, a summary trade count equal to the
  entry count, and summary net P&L equal to the sum of persisted exits. A
  missing exit or inconsistent summary makes the EOD pipeline incomplete
  instead of silently overstating paper performance.
- This is an accounting guard only. It does not change candidate selection,
  targets, stops, capital allocation, alerts, or broker behavior; real orders
  remain disabled.

### 2026-08-02: Predeclared capacity challenger for the next session

- The next paper experiment is frozen before the 2026-08-03 session. It uses
  five concurrent slots of Rs 100,000 from the Rs 500,000 account budget and
  permits up to ten entries per day, recycling a slot only after its observed
  exit. It evaluates the same causal `acceleration_first` ranking under two
  fixed contracts: 1.5% target / 1.0% stop and 1.5% target / 1.5% stop.
- Both variants use the preserved five-level entry/exit depth and are judged
  at executable VWAP, including the configured stress cost. The code never
  picks the better stop after seeing Monday's results; it reports both rows
  independently. There are no alerts, broker orders, or promotion flags.
- The EOD job writes the comparison to
  `results/Data/predeclared_capacity_challenger/` and the integrity checker
  requires that status artifact to be refreshed. Before the forward boundary,
  the real-data replay correctly reports zero eligible sessions rather than
  treating old sessions as evidence.
- This directly tests the current leading hypothesis: the tool's missed
  opportunity is partly a one-slot/capital-capacity problem. It remains an
  experiment, not a claim that the Rs 10,000 daily goal is achieved.

### 2026-08-02: Sector-rotation and rapid-climber challengers frozen

- The same forward-only challenger now includes six additional predeclared
  policies. `opening_ranked_t0_8_s0_5` and
  `early_rank_acceleration_t2_0_s1_0` target the capture funnel's early-stage
  gap, while `sector_rotation_t1_5_s1_0`,
  `sector_rotation_acceleration_t1_5_s1_0`,
  `sector_rotation_acceleration_t1_5_s1_5`, and
  `sector_rotation_t1_5_s1_5` apply the causal sector-breadth gate before
  balanced or acceleration-first selection, with three Rs 166,666.67 slots and
  1.5% target / 1.0% or 1.5% stop variants. `rapid_climber_t1_0_s1_0`
  applies the causal rapid-rank-climber gate across five Rs 100,000 slots,
  with a 1.0% target and 1.0% stop.
- These were selected as research challengers because the historical integrated
  policy table showed sector rotation as the strongest short sample and rapid
  climber as a separate diversification hypothesis. The acceleration-first
  sector variant was the strongest selector within that same short sector sample.
  That table covered only four complete sessions, so none of these results is
  treated as validation.
- Gate filtering happens before replay and uses only entry-visible fields.
  All challengers are paper-only, produce no Discord buy alerts, cannot be
  promoted automatically, and cannot place broker orders. The forward boundary
  remains 2026-08-03; before that boundary all nine policies correctly report
  zero eligible sessions.
- A pre-boundary diagnostic exposed and fixed a replay-isolation bug: each
  policy now filters its declared exit contract before applying its feature
  gate, so a result cannot accidentally inherit another policy's target/stop.
- Runtime commit `dbc2b48` contains the same challenger logic, EOD guard,
  pre-session registry check, and scoreboard rows; the full runtime test suite
  passed `604` tests. Auto-buy and auto-sell remain disabled.
- The EOD integrity checker now requires exactly these nine policy names,
  `paper_only=true`, no alerts, no forward-outcome selection, and all live-order
  flags false before marking the challenger artifact healthy.

### 2026-08-02: Full evidence audit baseline

- The runtime scoreboard was regenerated from the current registry and now
  contains 25 distinct evidence rows, including all nine capacity-aware paper
  challengers. No row is accepted, capital-allocation comparable, or eligible
  for the Rs 10,000 goal evidence gate; real orders remain disabled.
- The strongest measured historical row is `historical_rank_displacement`,
  with a stressed average of Rs 5,491.38/day and a positive lower-95 estimate,
  but it is explicitly a post-hoc top-book diagnostic rather than an executable
  forward strategy. It cannot authorize alerts, paper promotion, or orders.
- The frozen leader-continuation row is positive on average but has a negative
  lower-95 estimate and a materially negative worst session. The ranked
  confirmation and runner-fade lanes are also negative after stress. These
  results support collecting untouched forward evidence, not increasing risk.
- The nine capacity-aware policies remain at zero eligible sessions before the
  `2026-08-03` boundary. Their results will be compared independently with
  fixed contracts, fixed slots, costs, and executable depth; no policy may be
  selected after seeing its forward outcome.
- Validation after the regeneration passed 35 focused tests and the operational
  readiness check remains `23/23`. The next valid action is the first complete
  forward session, followed by the scheduled EOD judge.

### 2026-08-02: Runtime ownership check

- The authoritative live owner is `/home/charan_sai/tr-assistant` through
  `trade-assistant.service` and `scripts/start_live_assistant.sh`. That launcher
  starts the shared ticker, standalone full-rank evidence recorder, ranked paper
  lanes, and alert monitor from one runtime process.
- The legacy `intraday-goal.service` on the VM is disabled and points to a
  launcher that is no longer present in the development repository. It must not
  be enabled: doing so would create a second owner for Kite/session state and
  could duplicate scans or login attempts.
- Host timers remain the schedule authority: prewarm at 08:30 IST, assistant at
  08:45 IST, watchdog at 08:50 IST, and stop at 16:20 IST on weekdays. The
  runtime `.env.dev` keeps `KITE_AUTO_TRADE=0` and
  `ENABLE_GRADE_B_AUTOTRADE=0`.

### 2026-08-02: Anomaly alert-only lane and historical target audit

- Anomaly notifications can now bypass the unvalidated-selector suppression as
  explicit watch alerts only. Discord labels them `ANOMALY ALERT ONLY`, shows a
  watch range and the 1% / 2% / 3% research ladder, forces broker quantity to
  zero, and states that auto-buy is disabled. The separate auto-trade candidate
  registry also rejects every row carrying the alert-only marker.
- The runtime launcher enables this notification lane with a maximum of 20
  unique symbols per day. Daily symbol persistence prevents repeated alerts;
  direct Buy Now and other unvalidated lanes remain blocked.
- The old 2,908-row anomaly history was re-evaluated without daily-high
  lookahead. Timestamped 5-minute bars produced 1,429 post-alert outcomes over
  15 sessions. A fixed grid tested Rs 50,000 and Rs 100,000 tickets, 1% / 2% /
  3% daily goals, 1% / 1.5% / 2% / 3% trade targets, 0.5% through 2% stops,
  quality thresholds, ranking orders, 0.15% costs, and conservative stop-first
  handling when both barriers appeared in the available bar path.
- No configuration demonstrated a 1%-3% daily strategy. The highest historical
  average was 0.0634% per session (about Rs 317 on Rs 500,000), with 40% positive
  sessions, a negative median, a -1.1436% worst session, and zero 2% or 3% daily
  goal hits. The best 1% goal-hit rate among non-negative-average configurations
  was only 13.33%. Therefore these alerts collect forward evidence and support
  manual observation; they do not authorize automatic buying or a profit claim.

### 2026-08-02: Expanded anomaly audit and return architecture decision

- The first anomaly audit accidentally excluded the newer one-minute outcome
  paths because `post_alert_intraday` accepted only source names containing
  `5minute`. The filter now accepts both minute and five-minute caches while
  continuing to reject daily-candle outcomes that can contain pre-alert
  lookahead.
- The corrected replay contains 2,140 post-alert outcomes across 35 sessions,
  including nearly complete July coverage. Among the 5,280 fixed configurations
  with at least 20 sessions, none had a non-negative average. The least-negative
  row averaged `-0.0228%` per session, had a negative median, reached the 1%
  daily goal on 4% of sessions, and lost Rs 3,000 in the replay. Tightening the
  stop to 0.5% reduced the worst day but did not create positive expectancy.
- Conclusion: anomaly quality, more trades, larger tickets, or leverage cannot
  be used as the return engine. Anomaly remains an alert-only discovery and
  labeling stream with forced quantity zero.
- The return-improvement path is the causal full-universe microstructure
  architecture: observe every stock, rank early acceleration and rank
  displacement using only information visible at entry, require executable
  depth, allocate only to the best candidate, and release capital at the
  observed exit. The historical rank-displacement diagnostic averaged
  Rs 5,491.38/day after stress, but it is post-hoc and therefore cannot be
  traded. Its predeclared untouched forward variants remain the promotion test.
- The 16:25 IST EOD job now rebuilds a separate anomaly audit file from minute
  then five-minute data and reruns the 1%-3% fixed target grid automatically.
  It never modifies `anomaly_history.csv`, never enables broker orders, and
  keeps the alert research synchronized with each newly completed session.

### 2026-08-02: Rs 50,000 reusable-slot forward experiment

- The requested capital model is now a predeclared paper experiment rather
  than an assumption: Rs 500,000 total capital is divided into ten independent
  Rs 50,000 slots. A slot becomes available again only after its recorded exit,
  and the experiment permits at most twenty entries per session. It never uses
  leverage and cannot deploy more than the account capital concurrently.
- Two policies use the same causal `early_rank_acceleration` gate and
  `acceleration_first` selector so the exit comparison is isolated:
  `reusable_50k_early_acceleration_t1_0_s0_5` tests a 1.0% target / 0.5% stop
  over 30 minutes; `reusable_50k_early_acceleration_t2_0_s1_0` tests a 2.0%
  target / 1.0% stop over 60 minutes.
- Both consume observed five-level ask and bid depth, affordable VWAP-sized
  quantities, and stress costs. A unit replay proves that ten simultaneous
  slots are released at their observed exits and can fund later entries.
- This does not promise zero losses. The economic comparison includes loss
  size: after costs, frequent small wins can still lose money when stopped
  trades are larger. Neither policy can send an alert, promote itself, or place
  a broker order. The untouched forward boundary remains 2026-08-03.
- The capacity registry and EOD integrity invariant now require exactly eleven
  policies, including both Rs 50,000 variants, before Monday's session is
  considered healthy.

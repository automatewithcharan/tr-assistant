# Intraday Real Trading Start

> **Safety status (authoritative runtime contract):** Repository launchers are
> paper-only. A real broker mutation requires all of
> `KITE_EXECUTION_MODE=live`, `KITE_DRY_RUN=0`, `KITE_AUTO_TRADE=1`, the exact
> acknowledgement token enforced by `KiteOrderManager`, and independently
> matching canonical IDs in `KITE_LIVE_CALLER_ALLOWLIST` and
> `KITE_LIVE_STRATEGY_ALLOWLIST`. Missing, malformed, padded, case-variant, or
> duplicate identities deny execution. `grade_b_confirmation` is permanently
> paper-only. Historical examples below do not override the runtime gate.
>
> New entries additionally require current-day `risk_evidence_<account>.json`,
> current broker reconciliation, no unresolved durable mutation intent, and
> confirmed protection for every bot-owned open position. Risk-reducing exits
> use a separate policy: they still require acknowledgement, both identities,
> durable intent, broker reconciliation, and proof of bot ownership, but are
> not blocked by entry P&L evidence or an obsolete entry strategy.
> Execution reservation is serialized account-wide with `flock` on Linux and
> `msvcrt.locking` on Windows; the repository concurrency proof runs on POSIX,
> so Windows deployments require a platform-specific integration check.
>
> `kite_execution_journal.jsonl` is the authoritative fsynced mutation journal;
> CSV logs are derived compatibility artifacts. If an intent is `unresolved` or
> `recovery_required`, or protection/reconciliation fails, stop new entries,
> inspect broker positions/orders/GTTs against the stable `PK...` tag, and only
> clear/retry after a unique terminal outcome is established. Never replace an
> ambiguous order or GTT. Risk-reducing exits may reduce confirmed bot-owned
> exposure; unrelated holdings cannot be sold by that policy.
> Ambiguous responses remain unresolved unless broker history reaches a
> terminal state or five consistent absence snapshots, one second apart, are
> observed (the retry policy is injectable in tests). No replacement is sent
> while uniqueness is unresolved. The ambiguous-order policy remains
> paper-only: no current strategy is live-authorized, and an attribute-only
> GTT match is never adopted. The temporary bot position model is one
> bot-owned position per symbol; a second entry is rejected before broker
> reconciliation.

The live account namespace is selected by canonical `KITE_RISK_ACCOUNT` and
stores the lock, execution state, and authoritative journal together under
`KITE_LIVE_NAMESPACE_ROOT` (or its per-output-dir fallback). Journal replay
and the risk-evidence acceptance watermark fail closed on truncation,
corruption, sequence rollback, sequence reuse, or state divergence.

Use this only after the accepted selector report exists:

`results/Data/intraday_goal_selector_n500_mfe1_liquidity_replacement_vwap70_early_failure_validation.csv`

## Current Status

Updated on 2026-07-22:

- The older strict validation report remains an archived research result; the
  subsequent live Grade-B automation ledger was negative and supersedes it for
  deployment decisions.
- Automatic broker order placement is disabled:
  `KITE_AUTO_TRADE=0` and `ENABLE_GRADE_B_AUTOTRADE=0`.
- The current frozen ranker and leader pullback/reclaim work is paper/research
  only. It cannot send broker orders.
- Live Discord visibility may continue, but no alert lane is approved for
  unattended real-money execution.
- Do not re-enable automatic buying until a newly frozen selector passes its
  untouched forward-session gates and a separate deployment decision is made.
- Discord webhook and Kite session preflight must still pass before the live
  monitor starts.

## Temporary Research VM Upgrade

Recorded on 2026-07-18:

- Current temporary machine type: `e2-highcpu-32` (`32` vCPUs, `32` GB RAM).
- Current boot disk: `100` GB `pd-balanced`; keep this disk size after the CPU rollback.
- Previous live machine type and rollback target: `e2-custom-16-29696`
  (`16` vCPUs, `29,696` MB RAM).
- Reason for the upgrade: concurrent broad-universe walk-forward and ML jobs
  saturated all 16 prior vCPUs while memory and disk I/O still had substantial
  headroom.
- Roll back to the 16-vCPU type when the current CPU-heavy broad research and
  backtest campaign is complete and routine live scanning is again the primary
  workload. Do not leave the 32-vCPU research size running indefinitely.
- Machine-type changes require the VM to be stopped. Run the following from
  Google Cloud Shell, another machine with `gcloud`, or the Google Cloud
  console; an ordinary command running inside this VM cannot safely complete
  its own stop/change/start sequence.

```bash
gcloud compute instances stop test-vm --zone=europe-west3-c
gcloud compute instances set-machine-type test-vm \
  --zone=europe-west3-c \
  --machine-type=e2-custom-16-29696
gcloud compute instances start test-vm --zone=europe-west3-c
```

After restart, verify the Kite session, prewarm coverage, ticker, signal
monitor, Discord bot, and all expected systemd timers before relying on live
alerts. The resize changes compute capacity only; it must not change strategy
flags or re-enable real broker automation.

## Known-Good Live Goal Mode

As of 2026-06-29, the current Grade-A/B anomaly goal mode plus the no-auto-sell safety patch is the preferred live setup and should be treated as a rollback anchor if later changes make alerts worse.

- `tr-assistant`: `c93818c` (`Disable automatic sell execution in alert mode`)
- `intraday-trading`: `5912b2c` (`Disable automatic sell execution in alert mode`)
- Monitor interval: `3s`
- Goal alert cap: `50`
- Goal lane: Grade-A/B anomaly goal alerts, direct Buy Now disabled
- Target plan: Target 1 at `1.5%`, Target 2 at `3.0%`, stop at `1.0%`
- Execution style: manual entries only, book most quantity at Target 1, keep only a small runner
- Safety rule: live service must not start `--assistantautopilot`; auto-sell must stay disabled while trades are executed manually.

Do not weaken this setup without recording a new rollback commit and the reason for the change.

## Controlled Broker Automation Lane

Current safety status as of 2026-07-11:

- Real broker automation is disabled with `ENABLE_GRADE_B_AUTOTRADE=0`.
- Both launchers default this flag to `0`; a missing environment variable no
  longer enables orders.
- The replacement ranked-confirmation lane is paper-only and must complete
  forward validation before any explicit real-order re-enable decision.
- The historical setup below is retained for audit context, not as the current
  operating mode.

Added 2026-06-29:

- New lane: `grade_b_confirmation`.
- It does not use the old `--assistantautopilot` path.
- It only considers `Grade-B` anomaly/runner alerts.
- The alert itself is not a buy. The lane waits for live price confirmation above the alert price.
- Default confirmation: `+0.25%` above alert price.
- Default target: `+0.7%` from executed entry.
- Default stop: `-2.5%` from executed entry.
- Default entry window: `15` minutes after the alert.
- Default chase limit: skip if entry is more than `0.4%` above the confirmation trigger.
- Real orders are off unless all real-order flags are explicitly enabled.
- Auto-sell is only allowed for positions created by this lane and marked `auto_exit_allowed`; manual/old tracked holdings are ignored.

Updated 2026-06-30:

- Fill handling improved after APLLTD/CGCL confirmed but did not fill.
- The lane now uses the best ask when available, otherwise a tiny `0.10%` buy-limit buffer.
- The existing chase guard still applies: skip if the buyable entry is more than `0.4%` above the confirmation trigger.
- Target and stop are now calculated from the expected entry order price, not the stale alert/last price.
- Fill polling increased to `8` seconds before cancelling an unfilled limit order.
- Commits: `e1511ea` in `intraday-trading`, `1acc555` in `tr-assistant`.

Updated again 2026-06-30 after the `TTKPRESTIG` stop slipped:

- Bot-owned MIS buys now place a broker-side protective stop order immediately after the buy fill.
- Default protective stop order type: `SL-M`; if the broker rejects `SL-M`, the bot retries as `SL`.
- Kite will show the protective stop order, but target exits remain software-managed to avoid separate target+stop orders accidentally creating a short position.
- If a software target/reversal/EOD sell fires, the bot cancels the protective stop first.
- If a partial sell leaves remaining quantity, the bot places a fresh protective stop for the remainder.
- The software stop remains as fallback, but an active broker stop is preferred.

Updated 2026-07-02 for audit tracking:

- Every broker-automation decision now writes to an append-only autotrade journal.
- CSV audit file: `/home/charan_sai/tr-assistant/results/Data/autotrade_event_journal.csv`.
- JSONL audit file: `/home/charan_sai/tr-assistant/results/Data/autotrade_event_journal.jsonl`.
- Logged events include candidate registration, candidate expiry, skip reasons, entry attempts, Kite order results, protective stop placement/fill/failure, and position reconciliation.
- This is the first place to check when asking: why did the bot buy, why did it skip, why did it not buy, or why did a position close.

Launcher:

```bash
bash scripts/start_grade_b_confirmation_autotrade.sh
```

Important default limits in that launcher:

- `KITE_INTRADAY_MAX_ORDER_VALUE=50000`
- `KITE_INTRADAY_MAX_OPEN_POSITIONS=2`
- Standalone launcher default: `KITE_MAX_DAILY_ORDERS=3`
- Live service default through `scripts/start_live_assistant.sh`: `AUTOTRADE_KITE_MAX_DAILY_ORDERS=10`

Required real-order flags:

- `SIGNALMONITOR_AUTOTRADE_ENABLED=1`
- `SIGNALMONITOR_AUTOTRADE_REAL_ORDERS=1`
- `KITE_AUTO_TRADE=1`
- `KITE_AUTO_BUY=1`
- `KITE_AUTO_SELL=1`

Why this lane:

- The alert-history audit showed that blindly buying all alerts is weak.
- The strongest available small-sample combination was Grade-B only, wait for `+0.25%` confirmation, then target about `+0.7%` from entry.
- The sample is still small, so treat this as controlled automation, not a guarantee.

## Tomorrow Automation Setup

Updated 2026-06-29 after market close:

- The normal `trade-assistant.service` now starts the controlled automation lane automatically through `scripts/start_live_assistant.sh`.
- No separate laptop action is needed in the morning if Kite auto-login/session refresh succeeds.
- The systemd timer starts `trade-assistant.service` at `03:15 UTC`, which is `08:45 IST`.
- Prewarm timer runs before that at `03:00 UTC`, which is `08:30 IST`.
- Stop timer stops the service at `10:50 UTC`, which is `16:20 IST`.

Tomorrow's default automation caps:

- `ENABLE_GRADE_B_AUTOTRADE=1`
- Product: `MIS`
- Intraday capital pool: `400000`
- Max per buy: `50000`
- Max open bot positions: `2`
- Max bot buy entries per day: `10`
- Daily loss guardrail: `3000`
- Consecutive loss guardrail: `2`
- Target from executed entry: `+0.7%`
- Stop from executed entry: `-2.5%`
- Buy limit buffer: `0.10%` or best ask, still capped by the `0.4%` chase guard
- Broker-side protective stop: enabled for bot-owned MIS positions

Important:

- The generic `.env.dev` may contain `KITE_PRODUCT_TYPE=CNC` and `KITE_MAX_DAILY_ORDERS=12`, but the autotrade launcher overrides those for this lane using `AUTOTRADE_*` defaults.
- Auto-sell still only applies to positions created by this bot lane. Manual or old tracked holdings are not auto-sold.
- To disable real automation while keeping alerts, set `ENABLE_GRADE_B_AUTOTRADE=0` before service start.

## 2026-06-29 Pre-Open Rollback Status

Rollback reason:

- Kite auto-login started failing with `Invalid CAPTCHA values` after later changes.
- User requested reverting to the documented known-good live mode before market open.

Rollback commits now on `main`:

- Unsafe rollback commits, now superseded: `5d2caaf` in `intraday-trading` and `65c4ada` in `tr-assistant`. These restored the old autopilot path and can place sells.
- Safe rollback commits: `5912b2c` in `intraday-trading` and `c93818c` in `tr-assistant`. These include the no-auto-sell guard and remove `--assistantautopilot` from the live launcher.

Current live ownership:

- `trade-assistant.service` owns the combined live stack again:
  Kite ticker, signal monitor, and Discord trade bot.
- Autopilot/auto-sell is intentionally off for manual trading.
- `intraday-goal.service` is disabled and inactive intentionally because that split service was created after the known-good checkpoint.

Pre-open checks completed:

- Kite TOTP auto-login succeeded.
- Kite doctor: fresh session for `2026-06-29`, profile OK, holdings OK, positions OK.
- `trade-assistant.service`: active.
- `trade-assistant-watchdog.timer`: active.
- Ticker status around `09:08 IST`: connected, `2950/2990` symbols subscribed/snapshotted, `40` missing.
- Signal monitor latest summary around `09:08 IST`: scanning `2990`, no fresh alerts yet.
- Morning prewarm coverage: combined `2652/2906`, NSE `2131/2367`, BSE tradeable `824/916`.

Before touching code again:

- Keep this rollback state as today's operational baseline.
- Do not enable split ownership or experimental radar/ML lanes during market hours.
- Do not roll back only to `5d2caaf` or `65c4ada`; use the safe rollback commits above.
- If alerts are silent, check service/session/data freshness first, not strategy code.

## Real Capital Model Observed

The original compounding idea assumed the full daily capital compounds at roughly `0.5%` to `1%` per day. Live usage is different:

- Practical ticket size is often Rs 45,000 to Rs 50,000 per trade.
- Daily result comes from several separate trades, often 4 to 6 attempts, not one full-capital position.
- The real metric is deployed-capital turnover and realized daily P&L, not just account-size percentage.
- Scaling requires more independent high-quality alerts, enough liquidity, strict stops, and avoiding overtrading after early losses.

This means compounding is still possible, but it will not scale linearly forever from the same small-ticket approach.

Current reality:

- Averaging roughly Rs 1,000 to Rs 2,000 net per day is a good early result if it is after losing trades, charges, and taxes.
- That can produce roughly Rs 20,000 to Rs 40,000 per month over 20 trading days.
- If actual trade size remains around Rs 45,000 to Rs 50,000 and only a few alerts are usable, later months may stay near the same monthly P&L instead of compounding strongly.
- Treat this first as a consistency and monthly-income phase.
- Scale only after several live weeks of stable net P&L, using gradual ticket-size increases rather than forcing more trades.

## First Real-Money Rules

- Start smaller than the research capital.
- Recommended first-day order budget: Rs 20,000 to Rs 50,000.
- Max goal trades per day: 1.
- Stop after one losing trade.
- Stop after daily realized loss reaches Rs 1,000.
- Do not average down.
- Intraday only. Exit before market close.

## Paper + Alerts First

Use this before real orders when you want the same Discord alerts, but only a
paper ledger entry in the system:

```bash
INTRADAY_PAPER_BUDGET=200000 \
INTRADAY_PAPER_MAX_DAILY_LOSS=2000 \
SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY=3 \
bash scripts/start_intraday_goal_paper_alerts.sh
```

What happens:

- Discord receives the same alert card.
- No broker order is placed.
- The alert is logged into the paper trade tracker.
- Around 3:10 PM IST, open paper trades are closed from the latest Kite tick snapshot.
- Paper records are written under the local PKScreener user data `paper_trades` folder.

## Manual Exit Rules

Research rule:

- If the trade falls 0.14% within the first 4 bars before moving at least 0.15% in favor, exit early.
- Target 1 is the profit-lock scalp target, currently 1.5%.
- Book 70-80% of quantity at Target 1.
- Keep only the remaining runner for Target 2, currently 3%.
- After Target 1 hits, move the runner stop near entry/breakeven.
- Keep the hard stop.
- If a runner is active after partial profit, protect breakeven.

## Start Command

First check readiness:

```bash
python scripts/check_intraday_goal_readiness.py --send-discord-test
```

This checks the accepted selector, Discord, usual Kite live-data environment,
market timing, and whether another signal monitor is already running.

The swing backend can stay running if it is intentionally separate. The warning
only matters if it is sending duplicate intraday alerts for the same account.

Then check Discord directly if needed:

```bash
python scripts/test_discord_webhook.py
```

If either check returns an HTTP error, fix `DISCORD_WEBHOOK_URL` before starting.
The launcher runs readiness and Discord preflight automatically.

```bash
bash scripts/start_intraday_goal_monitor.sh
```

Optional safer first-day sizing:

```bash
INTRADAY_REAL_BUDGET=20000 \
INTRADAY_REAL_MAX_DAILY_LOSS=1000 \
SIGNALMONITOR_GOAL_MAX_TRADES_PER_DAY=3 \
bash scripts/start_intraday_goal_monitor.sh
```

## Important Limitation

The accepted research selector is validated on historical event candidates.
The live monitor sends Discord candidates using live alert features and the accepted report as the safety gate.
Treat the first live days as controlled monitoring, not proof that the backtest will repeat exactly.

Do not run this validated monitor alongside an older assistant/signals stack unless
you intentionally want duplicate alerts. Prefer one live stack per account while
capital is small and results are being verified.

## After Each Day

Record:

- Alert time.
- Entry price.
- Quantity.
- Exit price.
- Exit reason.
- Slippage.
- Whether the early-failure rule triggered.
- Final daily return.

Only increase capital after several live days match the expected behavior.

## Early Breakout Fix

June 4, 2026 forensic check:

- Several final top gainers were detectable around +1% to +3% before becoming +10% to +20%.
- The old goal alerts often saw them only after they were already too extended, then correctly suppressed them as `already ran`.
- Added a separate `🌱 Early Breakout` lane instead of weakening the late-chase protections.

New lane rules:

- Day move must be roughly +1% to +3.5%.
- Requires live Buy Now or Momentum Buy context.
- Requires volume or buy/sell pressure confirmation.
- Blocks overbought RSI, nearby 52-week resistance, and pullbacks from day high.
- One alert per symbol per day, capped per cycle.
- Goal mode tunes it to the current intraday plan: stop, Target 1, Target 2.

Expected alert label:

```text
🌱 Early Breakout
EARLY BREAKOUT SCALP
```

Purpose:

- Catch names earlier like PANACEABIO, RUBYMILLS, RICOAUTO, KWIL, PWL, SAIPARENT, GKENERGY, and JNKINDIA when they are still in the +1% to +3% zone.
- Keep old protections for stocks already stretched above the safe entry window.
- Paper-track these alerts separately as `signal_monitor_early_breakout`.

## Grade-C Pre-Move Watch

June 10, 2026 live update:

- Preserve the working Grade-A/B anomaly goal mode.
- Enable Grade-C only through `SIGNALMONITOR_GOAL_ALLOW_GRADE_C=1`.
- Grade-C appears as `👀 Pre-Move Watch`.
- It is lower confidence than Grade-A/B and should be treated as watch/small-size only.
- It must still pass the pre-move gate: strength >= 55, day move <= 4%, and either strong volume, buy/sell pressure, sector confirmation, or compression.
- Direct `Buy Now` remains disabled for goal mode.

# NG P5 EARLY WATCH publisher

This standalone process tails the full-universe live feature recorder and
publishes the frozen P5 human-review observation. It does not import, call, or
write SignalMonitor state. An EARLY WATCH is not a buy signal.

## Frozen eligibility contract

The time boundary is not a prose interpretation. The original P1-P5 research
execution was recovered from the 2026-09-09 Codex rollout transcript (ordinal
1069, ephemeral `fresh_accel_extract.py`). Its Pandas expressions were:

```python
d['minute'] = d.When.dt.hour*60 + d.When.dt.minute + d.When.dt.second/60
opening = set(d[d.minute.between(555, 570, inclusive='left')
                & d.DayChangeRank.between(1, 200)
                & (d.DayChangePct >= .5)].Symbol)
post = d.minute.between(570, 900, inclusive='left')
```

The source recorder writes naive IST wall-clock timestamps; timezone-aware
inputs are converted to IST. Therefore the frozen intervals are exactly
`[09:15:00, 09:30:00)` for opening-leader membership and
`[09:30:00, 15:00:00)` for P5 observations. Exactly 09:30:00 is eligible and
exactly 15:00:00 is not.

The remaining conditions are inclusive at their stated boundary:

- observation from 09:30:00 through 14:59:59 IST;
- symbol was not recorded before 09:30 as both rank 1–200 and at least +0.5%;
- day change +1.0% through +3.5%;
- NSE, price at least ₹50, cumulative turnover at least ₹3 crore;
- current rank 1–200, rank improvement at least 25 positions;
- current move delta at least +0.5 percentage point.

Buy/sell ratio, spread, pullback, market breadth, and sector fields are display
context only. There is no daily cap. Deduplication is one P5 observation per
symbol and IST trading session.

## Isolation and evidence

Inputs are read-only:

- `results/Data/live_feature_recorder/live_features_YYYY-MM-DD.csv`
- `results/Data/live_feature_recorder/full_universe_rank_status.json`
- `results/Data/full_universe_rank_strategy/session_coverage.csv` as the
  historical fallback for authoritative session coverage;
- only with the experimental `--enable-upgrades` option,
  `results/Data/alert_timing_observability/YYYY-MM-DD.jsonl`.

Before P5 evaluation, opening coverage must be `OPENING_COVERAGE_VALID`. The
authoritative recorder metadata must identify the same session, assert
`full_session_eligible`, use recorder generation 6, start no later than 09:20
IST, and exactly agree with the first complete source-row timestamp. The source
must also reach the 09:30 boundary with at least 15 distinct positive-rank
snapshots and no consecutive snapshot gap greater than the research contract's
45-second maximum. Historical fallback rows must additionally pass their
stored `continuity_ok`, maximum-gap, and minimum-snapshot criteria. Missing evidence is
`OPENING_COVERAGE_UNKNOWN`; false/inconsistent/damaged evidence is
`OPENING_COVERAGE_INVALID`. Both fail closed and are journaled. A late
publisher start remains safe because it replays the preserved opening rows.

The publisher writes only beneath
`results/Data/ng_p5_early_watch/` by default:

- `state.json`: atomic restart cursor, opening-leader set, dedupe, and pending queue;
- `evidence/events_YYYY-MM-DD.jsonl`: append-only OBSERVED, ATTEMPTED,
  DELIVERED, FAILED, EXPIRED_STALE_SESSION, opening-coverage, quarantine,
  and safe-replay transitions;
- `.publisher.lock`: prevents two publishers sharing the same state directory.

The source row SHA-256 and byte offsets are retained with every qualification.
No future outcome is calculated by the publisher. A retryable event remains in
the local queue across same-session restarts until delivery succeeds or the
bounded total attempt count is reached. An event from a prior IST session is
never posted: it becomes durably terminal as `EXPIRED_STALE_SESSION` with its
original session, observation time, expiry time, reason, and previous delivery
state. A permanent HTTP 4xx is terminal; HTTP 429, 5xx, network failures, and
unexpected client exceptions are bounded and retryable. Discord cannot provide
an idempotency key, so the deterministic event ID is included in the message
and journal; a process
death after Discord accepts a request but before its response is persisted can
still result in an at-least-once duplicate delivery.

Source rows and local evidence are streamed. The durable cursor advances only
after a complete row is safely interpreted, and retains device/inode plus a
bounded pre-cursor checkpoint. It also maintains a SHA-256 line-hash chain over
every consumed byte. Normal appends extend that chain incrementally; process
startup/restart verifies the consumed prefix once before trusting its cursor.
Thus an earlier same-inode rewrite is detected even if it preserves the final
checkpoint. Malformed rows, incompatible schemas, prefix damage, replacement,
and truncate/regrow conditions fail closed or trigger a safe replay. The full
file is not rehashed on normal polls.

Candidate upgrades are experimental and **off by default**. P5-only operation
does not initialize, open, or scan the timing journal. When explicitly enabled,
upgrades use only existing `SOURCE_QUALIFIED` events. `Buy Now` is identified
explicitly. Exact `Momentum Buy` is not labeled because the current timing
payload does not preserve that distinction.

## Manual review command

Set a publisher-specific webhook without putting it on the command line:

```bash
export NG_EARLY_WATCH_DISCORD_WEBHOOK_URL='...'
.venv/bin/python scripts/ng_p5_early_watch_publisher.py \
  --no-upgrades
```

For a single non-delivering ingestion check, omit the webhook and use an
isolated state directory. The observation will remain queued there:

```bash
.venv/bin/python scripts/ng_p5_early_watch_publisher.py \
  --once \
  --no-upgrades \
  --state-dir /tmp/ng-p5-review
```

Starting the publisher mid-session replays complete source rows from that
session so it can reconstruct opening leaders, but only after authoritative
coverage proves that those rows include the required opening evidence. Existing
publisher evidence and dedupe prevent the same P5 observation from being
created twice.

## Proposed systemd unit

Review and install separately; this repository does not install or start it:

```ini
[Unit]
Description=NG P5 EARLY WATCH publisher
After=trade-assistant.service

[Service]
Type=simple
User=charan_sai
WorkingDirectory=/home/charan_sai/tr-assistant
EnvironmentFile=/home/charan_sai/tr-assistant/.env.dev
ExecStart=/home/charan_sai/tr-assistant/.venv/bin/python /home/charan_sai/tr-assistant/scripts/ng_p5_early_watch_publisher.py --no-upgrades
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

This is a separate service. Installing or restarting it does not require a
restart of `trade-assistant.service`.

## Write isolation and shutdown

The only publisher writes are the configured state directory's `state.json`,
`evidence/events_YYYY-MM-DD.jsonl`, and `.publisher.lock` (plus an atomic
temporary state file during replacement). It never imports or writes
SignalMonitor state, goal/radar/anomaly sets, Experiments 1/2/3, lifecycle
evidence, paper trades, selector validation, `_daily_alerted`, or Buy
Now/Momentum state. SIGINT/SIGTERM stops the standalone poll loop; stopping this
publisher cannot alter scanner behavior.

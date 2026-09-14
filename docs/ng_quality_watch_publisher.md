# NG QUALITY WATCH — Leader Ignition publisher

This standalone subscriber publishes existing Leader Ignition `PASS` evidence
for human chart review. It is informational only. Pine remains responsible for
entry qualification.

The subscriber does not import, invoke, or write `SignalMonitor`. It does not
change Leader Ignition selection, scoring, limits, or state. Its sole input is:

```text
results/Data/leader_ignition/leader_ignition_YYYY-MM-DD.csv
```

Only rows whose `Decision` is exactly `pass` (case-insensitive) are eligible.
The earliest PASS for a symbol is retained; later PASS rows, including a later
pullback/reclaim PASS, are deduplicated for that IST session. Reject and watch
rows are ignored.

## Live-only startup

First startup is deliberately non-replaying. If today's source file already
exists, every complete older PASS row is durably recorded as baseline-seen and
is never delivered. A later reclaim PASS for the same symbol/session remains
suppressed. If the publisher is armed before the file exists, it consumes rows
written after creation. A past `--session-date` is never ingested or delivered.

After the baseline, the subscriber tails only newline-terminated rows. A
partial final row present at startup retains a durable marker containing its
starting offset, pre-arm byte length, SHA-256 prefix hash, source/session
identity, and arming identity/time. If that exact prefix later completes, the
row remains pre-arm: a PASS is added to baseline-seen without delivery, while a
reject is ignored. Subsequent rows are processed normally as live. A prefix
mismatch, file replacement, inode change, truncation, or same-inode rewrite
clears the marker through safe source reset so an unrelated row at the same
offset is not suppressed. A malformed complete row, invalid PASS timestamp, or
incompatible header quarantines that source and stops delivery from it.
Durable baseline-seen and observed-symbol dedupe prevent historical or
delivered PASS rows from being resurrected.

## Identity and evidence

The deterministic event ID is SHA-256 over:

```text
ng_quality_watch_v1|leader_ignition|SESSION_DATE|SYMBOL
```

Every observation preserves the original timestamp, complete causal source
mapping, absolute source path, byte offsets, and SHA-256 of the source row.
Rank is displayed as `UNKNOWN` because it is absent from the authoritative
Leader Ignition audit row. No display enrichment affects eligibility.

All writes are beneath the configured state directory (default
`results/Data/ng_leader_ignition_watch/`):

- `state.json`: atomic cursor checkpoint, baseline-seen/observed-symbol dedupe,
  and pending queue;
- `evidence/events_YYYY-MM-DD.jsonl`: append-only operational, observation,
  attempt, delivery, failure, expiry, and quarantine evidence;
- `.publisher.lock`: standalone process lock created by the CLI.

Undelivered events survive a same-session restart. Once the IST session
changes, old pending events become `EXPIRED_STALE_SESSION` and can never be
posted. Permanent HTTP 4xx responses are terminal. HTTP 429, HTTP 5xx, network
errors, and unexpected transport exceptions are retried with a bounded attempt
budget. Discord failure cannot propagate into trade-assistant because this is a
separate process.

## Discord destination

Provide the dedicated human-review webhook only through:

```bash
export NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL='...'
```

The webhook is never accepted as a command-line value and is never persisted
in publisher state or evidence. When it is absent, new live observations remain
queued without delivery.

## Forward-session command

Start before the new session's source rows are created:

```bash
.venv/bin/python scripts/ng_leader_ignition_watch_publisher.py \
  --session-date YYYY-MM-DD \
  --source-dir results/Data/leader_ignition \
  --state-dir results/Data/ng_leader_ignition_watch
```

Do not start this command against a historical session. The code also refuses
to ingest or deliver a session that differs from the current IST date.

P5 can use the same dedicated webhook without a P5 code change:

```bash
.venv/bin/python scripts/ng_p5_early_watch_publisher.py \
  --session-date YYYY-MM-DD \
  --state-dir results/Data/ng_p5_early_watch_shadow/YYYY-MM-DD \
  --webhook-env NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL \
  --no-upgrades
```

Use a new state directory/session date for a new forward run. No historical
P5 event should be replayed.

## Message

```text
NG QUALITY WATCH
NOT A BUY SIGNAL

Source: OPENING LEADER
Symbol: <symbol>
Qualified: <original timestamp> IST

LTP: <price>
Day: <day percent>
From open: <open percent>
Turnover: <turnover>
Volume / volume delta: <volume> / <delta> (<delta percent>)
B/S: <ratio>
Pullback: <percent>
Rank: UNKNOWN

Reason: Leader Ignition PASS
Open TradingView
Event ID: <deterministic ID>
```

There is no quantity, entry instruction, stop, target, profitability claim,
outcome calculation, or feedback into detection.

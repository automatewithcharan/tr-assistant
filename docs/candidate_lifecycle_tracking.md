# Candidate lifecycle tracking

This subsystem is analytics-only. It does not alter scanner qualification,
candidate scoring, caps, cooldowns, Discord selection, Pine decisions, or
broker execution. Automatic scanner integration is disabled by default; set
`SIGNALMONITOR_LIFECYCLE_TRACKING_ENABLED=1` to opt in.

## Data flow

After `SignalMonitorRunner` has deduplicated, scored, ranked, cooldown-filtered,
and capped direct candidates, each selected row is written to:

`<PKScreener user data>/candidate_lifecycle/candidate_lifecycle.sqlite3`

The write occurs before the Discord attempt. Delivery outcomes from the existing
durable Discord journal update the same candidate record. A compact candidate ID
is included in the Discord message.

Live price outcomes use only `last_price` observations already received by the
scanner. Day-high/day-low fields are not used because they may include prices
from before candidate detection. Missing observations remain missing; prices are
never interpolated.

At or after 15:30 IST, the monitor finalizes same-day candidate outcomes and
writes JSON and text reports under `candidate_lifecycle/reports/`.

## Storage schema

- `candidates`: immutable scanner snapshot, score, configuration hash, delivery
  status, and candidate/Pine status.
- `candidate_price_observations`: timestamped observed prices.
- `candidate_price_outcomes`: post-candidate high/low, MFE/MAE, time to extrema,
  and EOD result.
- `candidate_price_horizons`: configurable horizon observations and explicit
  `PENDING`, `OBSERVED`, or `MISSING` state.
- `pine_events`: normalized event plus redacted raw payload.
- `candidate_pine_links`: match method/confidence, confirmation delay,
  pre-entry price change, and pre-entry MFE.
- `trade_lifecycle`: theoretical Pine entry, stop updates, exit, P&L, and R.

All machine timestamps are stored as timezone-aware UTC strings. Trading dates
and user-facing scanner reports use Asia/Kolkata.

## Pine event ingestion

No public webhook receiver existed in the project, so this change does not open
or deploy an HTTP endpoint. Trusted Pine payloads can be ingested locally:

```bash
.venv/bin/python scripts/candidate_lifecycle.py \
  --output-dir /path/to/pkscreener-user-data \
  ingest-pine --payload-file pine-event.json
```

Supported normalized events are `BUY_READY`, `ENTRY` (or `BUY`),
`LOW_LIQUIDITY_SKIP`, `STOP_UPDATE`, and `EXIT`. Unknown fields remain in the
redacted raw payload for later parser upgrades.

Preferred matching uses `candidate_id`. Without it, the matcher uses normalized
symbol, event time, and the configured maximum age. It selects the most recent
eligible candidate deterministically and records alternatives. Candidates tied
at the newest timestamp are classified `ambiguous` and are not silently linked.
Stop/exit events without an ID may match the most recent open Pine lifecycle for
the symbol.

A future webhook adapter should call
`CandidateLifecycleTracker.ingest_pine_event()` only after authentication,
request-signature validation, body-size/rate limits, replay protection, and TLS
termination. Webhook credentials must never be included in the payload store.

## Inspect and report

Find the candidate ID in its Discord alert, then run:

```bash
.venv/bin/python scripts/candidate_lifecycle.py \
  --output-dir /path/to/pkscreener-user-data \
  inspect NSE_RELIANCE_20260927_093102_7F3A
```

Generate a daily descriptive report:

```bash
.venv/bin/python scripts/candidate_lifecycle.py \
  --output-dir /path/to/pkscreener-user-data \
  report --date 2026-09-27
```

Add `--json` for machine-readable output. To finalize a day manually:

```bash
.venv/bin/python scripts/candidate_lifecycle.py \
  --output-dir /path/to/pkscreener-user-data \
  finalize-eod --date 2026-09-27
```

## Limitations

- Scanner tick observations are not one-minute candles and are not treated as
  such. Horizon prices are the first real observation at or after each horizon,
  with its actual observation timestamp retained.
- MFE/MAE are based on observed `last_price` samples, so moves between samples
  may be absent.
- Discord webhook delivery normally returns no message ID; that field remains
  null unless a transport supplies one.
- Pine prices are theoretical signal/assumed prices, not broker fills.
- `UNKNOWN` and null values are preserved when RVOL, depth, or price data is not
  available.

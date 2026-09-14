# Alert timing observability schema

The live monitor appends causal events to:

`results/Data/alert_timing_observability/YYYY-MM-DD.jsonl`

The directory is resolved from the monitor's existing user-data output
directory. Files are append-only; the runtime never reads them and does not
backfill historical events.

Every JSON object has:

- `schema_version`: integer `1`
- `trading_date`: NSE trading date (`YYYY-MM-DD`)
- `event_timestamp`: timezone-aware ISO-8601 timestamp
- `symbol`: normalized exchange symbol
- `source`: `anomaly`, `pre_move`, `early_breakout`,
  `top_gainer_radar`, or `buy_now_board`
- `event_type`: one of the events below

`observation_id` and `candidate_id` appear only when a stable identifier
already exists. Optional signal-time fields are copied from values already in
the active decision path. Missing fields remain absent or null.

Implemented event types:

- `SOURCE_EVALUATED`: the source's actual evaluation path executed
- `SOURCE_REJECTED`: that evaluation returned an existing rejection reason
- `SOURCE_QUALIFIED`: the existing qualification path passed
- `DISCORD_ATTEMPTED`: immediately before the existing send/HTTP call
- `DISCORD_SUCCESS`: only after the existing client reports success
- `DISCORD_FAILED`: configuration, HTTP, or send failure, with a sanitized
  category

`SCANNER_VISIBLE` and `OPERATIONALLY_ELIGIBLE` are reserved schema names but
are not currently emitted. The monitor has no common persisted boundary that
can distinguish those states without inference.

Optional context fields are: `price`, `day_change_pct`, `volume_ratio`,
`signal_strength`, `rsi`, `entry_gap_pct`, `buy_above`, `vwap`, `vwap_state`,
`liquidity`, `traded_value`, `buy_sell_ratio`, `near_high_pct`,
`pullback_pct`, `confidence`, `rejection_reason`, `qualified_at`,
`http_status`, `delivery_duration_ms`, `failure_category`, `attempt_number`,
and `alert_batch_id`.

The writer uses one append-mode operating-system write per event batch.
Failures are logged by exception class and swallowed. Webhook URLs, tokens,
credentials, message bodies, and raw exception text are outside the schema.
Telemetry is never consulted by qualification, ranking, suppression, alert,
paper-trading, or order decisions.

## Direct TradingView candidate delivery durability

Early-breakout and top-gainer-radar Discord delivery uses a separate append-only
journal. Every HTTP attempt is reserved and fsynced before the request. If that
reservation cannot be persisted, the request is not sent and the delivery
subsystem is disabled for the process while scanner work continues.

An `attempt_reserved` row with no later outcome is reconstructed as
`delivery_uncertain` and is never retried. This is an intentional at-most-once
choice: a crash can occur after reservation but before HTTP, so an uncertain
candidate may have been delivered or may have been lost. Retrying that state
could duplicate a message accepted immediately before a crash. Normal transient
HTTP failures remain retryable within the configured TTL and total-attempt cap.
A network timeout can occur after Discord accepted a request; retrying that
persisted transient result therefore retains a bounded at-least-once ambiguity,
with the total-attempt cap providing the hard duplicate bound.

When lifecycle persistence cannot return an ID, direct lanes use a deterministic
fallback identity derived from the causal trading date, lane, and symbol. This
matches their existing one-alert-per-symbol/session boundary and survives a
monitor restart.

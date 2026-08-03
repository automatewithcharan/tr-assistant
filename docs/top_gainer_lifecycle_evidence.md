# Canonical top-gainer lifecycle evidence

`TopGainerLifecycleRecorder` is a paper-only evidence adapter. It joins live
candidate observations, rank/alert/confirmation/paper events, and later session
outcomes without creating a signal, entry, exit, or broker path.

## Storage

The authoritative append-only log is:

```text
results/Data/top_gainer_lifecycle/events.jsonl
```

The derived replay snapshot is:

```text
results/Data/top_gainer_lifecycle/joined_snapshot.csv
```

Both paths are relative to the existing evidence output directory. Writes use a
flush/fsync/replace pattern for derived files, schema version `1`, and stable
hash identifiers. Corrupt JSONL records fail replay rather than being skipped.

## Causal boundary

`candidate_observed` stores only fields available at the observation timestamp.
`session_outcome_finalized` stores future labels separately. Future labels are
never returned by the observation API and are not available to live ranking.
Feature timestamps later than observation time are rejected.

Multiple observations of the same symbol remain distinct because the lifecycle
identity includes date, symbol, timestamp, source lane, and experiment version.

## Integration status

The top-gainer-radar and early-breakout qualification functions now emit
canonical observations directly at their pass boundary using only their local
tick/scanner values. Their alert rows carry the resulting
`LifecycleObservationId` before ranked-paper processing. The live-feature
boundary continues to emit canonical observations, rank events, and signal-time
rows. The ranked confirmation paper boundary maps explicit IDs to confirmation
armed/passed/failed, paper-entry, and paper-exit events.

No event is inferred from the newest symbol observation. Missing IDs are
rejected rather than guessed.

Current direct producers are therefore: top-gainer radar, early breakout, live
feature recorder, aggregate alert compatibility boundary, and ranked
confirmation paper. V2 anomaly, Grade-B discovery, full-universe ranking,
leader ignition, runner continuation, and independent paper observers still
require explicit boundary adapters.

When the same symbol qualifies in both directly integrated lanes, the raw log
keeps both lane-specific observations. Existing SignalMonitor symbol alert
deduplication remains unchanged, so only the selected aggregate alert receives
`alert_emitted`; the suppressed lane is retained as multi-lane discovery
overlap without a false alert event.

## Replay and analysis

Use:

```bash
python scripts/analyze_top_gainer_lifecycle.py
```

The analysis reports counts, lane/event coverage, future-threshold rates, and
MFE/MAE summaries. Precision is unique alerted observations reaching the 5%
runner label divided by unique alerted observations with complete outcomes.
Recall is reported as `not_available` unless an unbiased eligible universe and
actual-runner labels are present. Detection latency is measured from the
observation timestamp to the first alert. Lane overlap uses a documented
60-second date/symbol window. Historical or incomplete rows are excluded from
causal metrics and reported separately. The script does not calculate
profitability acceptance or authorize promotion.

EOD enrichment is an explicit offline call to `enrich_from_bars()`; it is not
called during live ranking. Only bars strictly after the observation timestamp
contribute to future labels. Missing post-observation data emits
`data_incomplete`, and repeated enrichment is idempotent.

## Limitations

Historical live snapshots are incomplete unless the recorder was running. The
dataset does not reconstruct queue position, latency, halted/upper-circuit
execution, or missing tick intervals. It is evidence infrastructure, not a
claim that the system reliably predicts top gainers.

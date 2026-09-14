# NG Experiment 4: traded-value shadow recorder

Experiment 4 observes the frozen NG P5 contract without changing it. A source
row is recorded only when the real P5 evaluator rejects it for turnover and the
same evaluator would qualify the same row with `TradedValue` set exactly to the
existing production requirement. The counterfactual is used only to establish
that traded value is the sole failed gate.

The publisher remains authoritative for real P5 qualification, deduplication,
Discord delivery, and return counts. Experiment 4 has no alert, transport,
trade, ranking, selector, or promotion interface. Recorder exceptions are
contained and cannot interrupt the publisher.

## Isolation and causality

Signal-time observations are written beneath:

`results/Data/experiment_4_traded_value_shadow/observations/`

Only an explicit allow-list of causal live-recorder columns is copied. Missing
features are recorded as `null`; the recorder does not backfill them from later
rows. Future-looking or newly added source columns are excluded until reviewed.

Post-session labels are written separately beneath:

`results/Data/experiment_4_traded_value_shadow/outcomes/`

The finalizer never rewrites observation files. For sub-minute observations it
starts excursions at the first complete one-minute bar after the timestamp, so
pre-observation extremes from the containing bar cannot enter MFE or MAE.
Within-bar threshold ordering is explicitly marked indeterminate.

Example post-session command:

```bash
.venv/bin/python scripts/finalize_ng_traded_value_shadow_outcomes.py 2026-09-14 \
  --minute-dir results/Data/kite_history_backfill/minute/2026-09-14_2026-09-14 \
  --live-feature-path results/Data/live_feature_recorder/live_features_2026-09-14.csv
```

This command must be run only after authoritative session data is available.
It does not score, select, alert, or promote observations.

## Publisher health

Both NG publisher entry points derive operational snapshots from their existing
append-only delivery evidence. Snapshots live under each publisher state
directory at `health/health_DATE.json` and report qualification count, delivery
attempts, successful deliveries, HTTP 403/429/5xx counts, and delivery success
rate. Snapshot failures are contained and do not affect publisher processing.

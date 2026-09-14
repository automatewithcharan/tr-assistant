# Manual Discord NSE-EQ delivery guard

The NG intraday runtime keeps its combined scanner universe and all detector,
research, lifecycle, paper, and order state unchanged. Immediately before a
human/manual Discord candidate is formatted or delivered, the runtime now
requires an instrument identity with `exchange=NSE`, `segment=NSE`, and
`instrument_type=EQ`.

Eligibility comes from the existing Kite NSE instrument dump at
`results/Data/kite_instruments_nse_latest.csv` (or the runtime user-data
directory equivalent). A dual-listed symbol remains eligible when this dump
contains its NSE EQ row. A BSE row or BSE existence alone never disqualifies a
symbol; absence of a valid NSE EQ row does.

The check fails closed when the NSE instrument dump is missing, unreadable, or
does not contain the required metadata columns. Suppressions are deduplicated
in memory and appended to
`results/Data/manual_discord_delivery_guard/YYYY-MM-DD.jsonl`. This telemetry
is delivery-only and does not alter canonical candidate or evidence files.

Limitations: eligibility is only as current as the broker instrument dump. A
new listing will remain suppressed until that dump is refreshed. Kite labels
cash-equity series through `instrument_type=EQ`; the dump does not expose a
separate NSE `SERIES` column.

## Read-only 09:25 investigation

The live launcher starts the assistant before market open, starts KiteTicker,
waits up to 20 seconds for an initial snapshot, and launches SignalMonitor at
the configured interval (3 seconds in `scripts/start_live_assistant.sh`).
SignalMonitor permits general intraday candidate evaluation from 09:15 IST.

Anomaly and pre-move candidates have a separate deterministic gate in
`VolumeAnomalyDetector.update_from_ticks`: `MIN_MINUTES_AFTER_OPEN = 10` and
the detector returns no candidates while `minutes_since_open < 10`. Because
the elapsed-minute calculation is based on the 09:15 IST open, their first
eligible minute is 09:25 IST. Both anomaly types pass through the same gate;
the opening-burst threshold mode also begins on that first eligible minute.

Early-breakout, top-gainer-radar, core Buy Now, and the optional leader shadow
do not use the detector's ten-minute warmup. They use SignalMonitor's regular
09:15-15:30 candidate/Discord windows and may start after 09:15 when their own
source inputs and qualification conditions are ready. Therefore a 09:25 first
anomaly/pre-move alert is intentional; a 09:25 first alert from another source
would be incidental to polling, scan duration, tick/history readiness, or that
source's qualification state.

# Storage optimization benchmark — v1.1

## V1 profile

The representative V1 SQLite file was 2,084,864 bytes for 2,319 snapshots,
5,435 rank events, and one market-context row. `dbstat` attributed 937,984
bytes to `rank_events` (including duplicated JSON context), 679,936 to the
wide snapshots table, and 270,336 to the wide snapshot primary/index keys.
The V1 projected footprint was 0.79 GB/session and 15.5 GB/20 sessions.

## V1.1 design

`session_metadata`, `symbols`, `session_symbol_metadata`, and
`snapshot_times` hold repeated text/context once. `features` has numeric data
only and only for fresh symbols. It uses integer IDs and epoch milliseconds.
Rank events have explicit numeric context and require a genuine prior observed
rank, so initial/restart cycles do not write fabricated crossings. There is one
research-useful secondary index: `(symbol_id, snapshot_id)`.

No V1 research-contract field was dropped. Missing feature rows explicitly mean
no valid fresh observation at that snapshot; quality remains in `snapshot_times`.

## Measurement

On the same 2,319-symbol atomic snapshot, V1.1 wrote 2,319 fresh rows in
354.380 ms. First-snapshot size was 872,448 bytes; second fresh snapshot grew
the database by 589,824 bytes. Projected 375-minute session size is
221,466,624 bytes (about 0.221 GB/session, 4.43 GB/20 sessions): a 72.0%
reduction from V1, while meeting the <=0.30 GB/session target.

V1.1's one-cycle processing latency was 354.380 ms versus V1's 385.806 ms.
The recorder-only disk guard pauses new feature snapshots below configurable
`NG_EMERGING_LEADER_MIN_FREE_GB` (5 GB default) and resumes above
`NG_EMERGING_LEADER_RESUME_FREE_GB` (8 GB default). It never cleans disk,
stops the watcher, or changes trading behavior.

## Compression audit

Already available: `pyarrow`, `pandas`, `gzip`, and `/usr/bin/zstd`.
`zstandard` Python is unavailable. V1.1 remains SQLite because it supports
transactional writes and the required symbol/time reads without an added
runtime dependency. No compressed columnar replacement was adopted merely for
its format; normalized SQLite meets the target with lower integration risk.

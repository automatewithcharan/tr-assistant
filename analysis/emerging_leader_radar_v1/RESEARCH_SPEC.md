# Emerging Leader Radar v1 — preregistered research specification

Question: can an NSE EQ stock that is outside the current Top100 be identified
causally early enough that meaningful forward movement remains after detection?
This is shadow-only data collection, not a signal, recommendation, or trading system.

## Population and rank semantics

The primary population is fresh NSE EQ observations with `broad_rank > 100`.
Current Top100 rows are retained as context/control and are never counted as
early Top100-entry successes. Broad rank is descending current day percent
change, `(last_price / previous_close - 1) * 100`, among fresh NSE EQ symbols.
The recorder retains provisional ranks; later labels use coverage-qualified
snapshots (the existing fixed 80% operational completeness rule) where stated.

## Hypotheses, not edges

- H1: faster outside-Top100 rank improvement and rank acceleration may precede
  later Top100/50/20 entry.
- H2: cumulative-volume and approximate-turnover acceleration may add early
  participation information before a stock is a leading gainer.
- H3: improving return relative to a fresh live NIFTY 50 value may distinguish
  durable leadership from temporary rank jumps.
- H4: simultaneous cross-sectional movement in momentum, participation,
  turnover, and relative strength may be more informative than rank alone.
- H5: moderate current price movement with improving participation/rank/RS may
  leave more forward opportunity than an already extended move.

No hypothesis is an edge claim. No weights, composite score, RSI, MACD,
oscillator, machine learning, qualification rule, alert, or order is created.

## Causal feature contract

At T, all features are calculated from the atomic Kite snapshot and rolling
observations timestamped at or before T. For each horizon, the recorder selects
the most recent observation at/before `T - horizon` only when it is within a
30-second tolerance; it never interpolates or substitutes a future value.
Rank improvement is `rank_at_T-horizon - rank_at_T`; rank velocity is that
improvement divided by elapsed horizon minutes. Rank acceleration is
`(velocity_1m - velocity_5m) / 4` where both values exist.

`approx_turnover = last_price * cumulative_volume`. `causal_vwap` records
Kite's contemporaneous `average_traded_price` field; it is not reconstructed
from completed/future bars. RVOL-at-time is NULL: the repository does not have
a validated per-symbol historical time-of-day baseline. NIFTY-relative fields
are NULL unless a fresh `NIFTY 50` tick with last price and prior close exists.

## Storage and labels

Schema `emerging_leader_radar_v1_1` normalizes immutable session metadata,
symbols, and snapshot times. `features` contains only fresh symbols, keyed by
integer `(snapshot_id, symbol_id)`; no feature row means the symbol did not
have a valid fresh observation at that timestamp. Token mapping is session
metadata with separate mapping-transition events. Rank events use numeric
columns only. Snapshots are roughly once/minute; rolling history and threshold
crossings are updated every watcher cycle. First observation/restart does not
fabricate a crossing: an observed persisted prior rank is required.
The offline labeler writes a separate labels SQLite database and never updates
the feature database. Future rank labels use only coverage-qualified snapshots.
EOD rank is valid only from a >=80%-coverage snapshot in the 15:28–15:30 IST
window; otherwise it is UNKNOWN/NULL. Persistence is real elapsed time, not a
snapshot count. The recorder has a fail-open disk guard: it pauses new rows
below `NG_EMERGING_LEADER_MIN_FREE_GB` (default 5) and resumes only above
`NG_EMERGING_LEADER_RESUME_FREE_GB` (default 8).

## V1.1 correctness hardening

The live recorder is explicitly gated to 09:15:00–15:30:00 IST inclusive.
Outside that interval it returns `outside_market_session` and writes no
research rows or state; rolling rank history is cleared at the next session
date. A disk pause returns `research_paused_low_disk` before reading or
mutating research state. Paused time is intentionally missing evidence and
cannot create reconstructed crossings. Atomic snapshot failures return
`source_unavailable`, do not count as completed cycles, and are persisted as
source failures when the SQLite metadata store is available.

For every rank outcome level, `eligible_top{N}_entry` is 1 only for a rank
strictly greater than N, 0 for a current rank at/better than N, and NULL when
rank is unavailable. An ineligible observation has NULL `entered_top{N}_later`.
For eligible rows, a later entry is 1 only from a >=80%-coverage future
snapshot. A zero requires complete future qualified evidence for that symbol;
otherwise it is NULL. Labels expose qualified future snapshot count, final
qualified timestamp, maximum gap, and `future_label_valid` so missing data is
never silently converted into a negative.

EOD uses the final >=80%-coverage market snapshot in the inclusive
15:28:00–15:30:00 IST interval. Symbol EOD rank is read only from the feature
row with that exact `snapshot_id`; a stale/missing symbol is NULL with
`symbol_eod_rank_valid=0`. Top100 persistence is observed elapsed minutes
between consecutive qualified fresh symbol observations where both ranks are
<=100, with gaps over two minutes not bridged. The manifest records objective
cycle, snapshot, coverage, near-close, failure, and database row counters;
it does not collapse quality into a simplistic boolean.

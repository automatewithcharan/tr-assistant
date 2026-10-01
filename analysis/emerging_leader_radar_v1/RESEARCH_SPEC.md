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

Each session has SQLite tables for immutable feature snapshots, descriptive
rank crossing events, and market context. Snapshots are roughly once/minute;
rolling history and threshold crossings are updated every watcher cycle.
The offline labeler writes a separate labels SQLite database and never updates
the feature database. EOD rank is the last coverage-qualified snapshot at or
before the session end; insufficient EOD coverage is UNKNOWN/NULL.

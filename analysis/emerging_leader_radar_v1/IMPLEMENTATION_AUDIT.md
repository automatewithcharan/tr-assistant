# Implementation audit — Emerging Leader Radar v1

Audit performed against merged main `d4894935963ad756072e62bb9b6462a982335d79`.

## Existing observation facts

- `MarketObservationLedger` uses the atomic sibling JSON snapshot before its
  CSV fallback. It treats an NSE EQ tick as fresh only when its same-session
  trade timestamp and received timestamp are within the explicit `-10..120s`
  clock/age window.
- Broad rank sorts fresh NSE EQ stocks by descending current percentage change:
  `(last_price / close - 1) * 100`; symbol is the deterministic tie-breaker.
  It is not a liquidity, anomaly, RVOL, or BUY ranking. The existing broad
  coverage qualification is a fixed 80% operational-completeness threshold.
- Current normal watcher/ledger cadence is 15 seconds. The official NSE Top20
  HTTP publisher remains independently throttled to 60 seconds.
- `EQUITY_L.csv` currently contains 2,319 NSE EQ symbols (header excluded).
  Current atomic snapshot had 2,940 records, with 2,037 fresh NSE EQ records
  at benchmark time.

## Actual atomic Kite fields

Observed fields include `last_price`, `close`, `open`, `high`, `low`,
`volume_traded`, `best_bid`, `best_ask`, full `depth`, `received_at`,
`last_trade_time`, `exchange_timestamp`, `average_traded_price`, and exchange/
symbol metadata. Thus cumulative volume, OHLC location, bid/ask, and a live
average-traded-price/VWAP-like field are available without future bars.

A fresh `NIFTY 50` tick is currently present in the atomic snapshot with last
price and prior close, so NIFTY percent change and stock-minus-NIFTY relative
strength can be causal when that exact tick passes freshness checks. If absent
or stale, those fields are NULL. No validated per-symbol historical
time-of-day volume baseline exists in this observer path; `rvol_at_time` is
therefore NULL. Direct turnover is not provided, so `approx_turnover` is
explicitly `last_price * cumulative_volume`.

## V1 storage and performance

Snapshots are SQLite, one row per expected NSE EQ symbol approximately once per
minute: `2,319 * 375` ≈ **869,625 rows/session** for a 375-minute NSE session.
Rank crossings are stored separately at 15-second cadence, first per
symbol/milestone/session. The full-universe benchmark wrote 2,319 rows plus
5,435 initial descriptive crossing records in **385.806 ms**, producing a
2,084,864-byte SQLite database. Linear snapshot-only projection is about
**0.79 GB/day** and **15.5 GB/20 sessions**; actual size can vary with WAL
checkpointing and crossing events. This is accepted only as a shadow recorder;
if production measurements materially exceed the benchmark it must be stopped,
not made less causal.

The data root is selected only when `NG_EMERGING_LEADER_RESEARCH_ENABLED=1`.
With its default `0`, no recorder is instantiated or called.

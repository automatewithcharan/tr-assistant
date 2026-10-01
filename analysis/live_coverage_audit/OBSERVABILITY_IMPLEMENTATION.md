# Observation-only coverage implementation

`MarketObservationLedger` reads the raw Kite snapshot before any scanner, anomaly, liquidity, RVOL, Buy, Discord, or broker path. It appends state transitions, fresh-state evidence, raw change ranks, and first Top100/50/20/10/5 timestamps for every NSE EQ symbol. NIFTY50 is a subset of this raw lane, so it cannot be removed by trading gates.

The checked-in systemd unit uses the canonical `/home/charan_sai/tr-assistant` checkout and a 15-second watch/observation poll. It is not installed or restarted by this change.

Top100 delivery remains WATCH-only and disabled unless the local environment contains exactly:

`NG_TOP100_WATCH_DISCORD_ENABLED=1`

It has no connection to anomaly quotas, qualification, or orders. The existing Top100 dedupe and retry state remains in `NGTop100GainersWatch`.

## Trading-parity report

No files in PSAR, NGScalp, entry/exit logic, sizing, broker execution, or SignalMonitor trading qualification were changed. The ledger imports no trading class and exposes no order or alert method. It only writes `results/Data/ng_nse_top_gainers_watch/observation_ledger/<day>/` evidence.

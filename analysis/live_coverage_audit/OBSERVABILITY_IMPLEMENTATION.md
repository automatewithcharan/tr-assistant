# Observation-only coverage implementation

`MarketObservationLedger` reads the atomic Kite JSON snapshot (with a stable-read CSV fallback) before any scanner, anomaly, liquidity, RVOL, Buy, Discord, or broker path. It rejects future timestamps beyond 10 seconds of explicit skew allowance.

Raw ranks are always persisted as **provisional**, with rank universe size, expected universe size, and coverage percentage. Confirmed NSE Top100/50/20/10/5 milestones are written only when fresh NSE EQ coverage is at least 80%; provisional milestones are never rewritten as historical confirmed events. NIFTY50 has a separate fresh-constituent-only rank, top10/top5 milestones, improvement, and elapsed-minute velocity.

The checked-in systemd unit uses the canonical `/home/charan_sai/tr-assistant` checkout. Raw observation and Kite Top100 run every 15 seconds; the external official NSE Top20 HTTP publisher is independently scheduled at 60 seconds. State is atomically replaced; JSONL evidence remains append-only. It is not installed or restarted by this change.

Top100 delivery remains WATCH-only and disabled unless the local environment contains exactly:

`NG_TOP100_WATCH_DISCORD_ENABLED=1`

It has no connection to anomaly quotas, qualification, or orders. The existing Top100 dedupe and retry state remains in `NGTop100GainersWatch`.

## Trading-parity report

No files in PSAR, NGScalp, entry/exit logic, sizing, broker execution, or SignalMonitor trading qualification were changed. The ledger imports no trading class and exposes no order or alert method. It only writes `results/Data/ng_nse_top_gainers_watch/observation_ledger/<day>/` evidence.

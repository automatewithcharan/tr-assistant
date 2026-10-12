# Safety, parity, and known limitations

- `paper_trading` contains no call to Kite order/GTT placement, modification, or cancellation. A static AST scanner and injected tripwire cover these names. The read-only facade exposes only historical data and quotes.
- Offline replay requires no credentials or network. The current `live-paper` CLI always exits with code 3; therefore it is not yet safely implementable as an active process. A future quote adapter requires explicit approval, an outbound Kite mutation deny layer, stale-quote/spread tests, and a dedicated paper webhook review.
- The notifier writes an outbox preview file only. Delivery state remains null and no production webhook is read.
- Historical A is a formula reconstruction, not Pine realtime AUTO SIGNALS. Pine `barstate.isrealtime`, `varip`, alert snapshot creation, live drift, and tick-observed peak/stop ordering are irreducible without timestamped tick and alert evidence.
- Conventional PSAR and Wilder indicators are tested for deterministic behavior. Numeric Pine parity remains unverified until a user-supplied TradingView per-bar export is compared with the parity CSV at declared tolerances.
- OHLC fills use `bar_open_zero_latency_proxy`: half of the configured 10 bps spread plus 5 bps per-side slippage, adverse tick rounding, and a 1% volume cap. This may still be optimistic if candle publication is delayed. Stress reruns can raise slippage and set `fill_delay_bars` to 2 for an additional observation of delay.
- The baseline trailing policy checks the old stop first, then makes any bar-high ratchet effective on the next bar. Gap-through stops fill below the stop. Confirmed UT/PSAR/EOD exits wait for the next observation.

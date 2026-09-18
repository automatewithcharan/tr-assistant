# NG live Top 100 gainer coverage

The official NSE Top 20 Gainers alert continues to read NSE's All Securities board. The same publisher also ranks the fresh NSE `EQ` quotes available in the shared Kite ticker snapshot and reports new members of its **Top 100** list.

- Default coverage: 100 ranked symbols. Set `--top-n 50` or `--top-n 200` on `ng_nse_top_gainers_watch.py` to change the coverage. The supported range is 21–200.
- Only today's traded shares with a recent Kite quote enter the ranking. Stale files, prior-day trades, missing prices, non-`EQ` series, and non-NSE quotes are rejected. Both feeds also require at least **1,000 shares and ₹10 lakh cumulative traded value** before an alert. These are operational noise filters, not evidence of a profitable entry. A name can qualify and alert later once its volume and value cross both minimums.
- Each symbol is delivered once per session across the official Top 20 and broad Top 100 feeds. The initial broad list is grouped into at most 30 symbols per Discord message; subsequent new entrants are sent when observed. Failed batches remain pending and are retried.
- Broad messages say **WATCH ONLY** and show the Kite source, price gain, and traded volume. Ranking measures the rise from the previous close; it is not a BUY recommendation or an official NSE Top 100 list. A symbol absent from the live Kite ticker cannot enter the broad ranking; the independent NSE Top 20 feed can still cover it.

The publisher runs from `ng-nse-top-gainers-watch.service` once per minute between 09:15 and 15:30 IST. It waits until at least 500 NSE EQ shares have traded with fresh quotes before publishing the broad ranking. On a weak market day, fewer than 100 stocks may be positive and liquid enough, so the Top 100 list can contain fewer than 100 names. The thresholds can be changed with `--min-shares` and `--min-traded-value` (rupees).

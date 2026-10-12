# Fee assumptions

Checked 2026-10-10 against Zerodha's official charges page and NSE circular `NSE/FA/73061` (effective 2026-03-01).

| Component | Model |
|---|---|
| Brokerage | min(0.03% of leg notional, ₹20), each executed leg |
| STT | 0.025% sell-side intraday, nearest whole rupee |
| NSE transaction | 0.00307% each side |
| SEBI | ₹10/crore each side |
| Stamp | 0.003% buy side |
| NSE IPFT | ₹0.01/crore each side |
| GST | 18% of brokerage + transaction + SEBI + IPFT |
| DP | excluded for normal same-day square-off |

Sources: <https://zerodha.com/charges/> and <https://nsearchives.nseindia.com/content/circulars/FA73061.pdf>.

The model rounds component calculations deterministically per simulated order. Real contract notes may aggregate or round some levies differently; contract-note parity remains an explicit blocker.

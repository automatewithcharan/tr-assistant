# NG Pine v0.8.18 Lane-C gate-ablation study

This is research telemetry only. The immutable comparison baseline is commit
`992245806edb33a103596d93e44f7a7173ecfc9c`. The added code is enclosed in three
explicit `RESEARCH GATE ABLATION` blocks. Removing those blocks reproduces the
baseline file byte for byte; the static test enforces that invariant.

## Current Lane-C and F0 logic

Lane C's confirmed momentum-bar predicate evaluates:

1. confirmed bar, entry window, and `contextGood` (5-minute chart, rising EMA20
   with close above it, close above VWAP, positive intraday relative strength);
2. bullish candle;
3. close above the prior completed 8-bar high;
4. valid ATR and range at least 1.0 ATR;
5. body fraction at least 65%;
6. close position at least 75%;
7. current completed-bar volume ratio at least 2.0x;
8. intraday relative strength at least 1%; and
9. close no more than 2.0 ATR above EMA20.

Arming separately requires Lane C to be scanning, a fresh post-trade setup, no
active A/B/D/E trade, risk no greater than 0.90 ATR, and at least 1R room to the
next known PDH/PWH. An actionable Lane-C BUY is later still: a subsequent
confirmed bullish close must clear the expansion high while context, overshoot,
risk, and quantity checks pass.

F0 is a different population, not a Lane-C ablation. It uses EC0/Fast-Mover
context: 5-minute EMA trend, VWAP tolerance, liquidity, day RS at least 3%,
intraday RS at least 0.5%, and `fastVolStrength` at least 2x. It then requires a
confirmed bullish 8-bar breakout, no live lane trade, freshness, ATR, and sizing
viability. In particular, F0 volume is not Lane C's current
`completedBarVolRatio`.

The dashboard and remembered-rejection strings are nested first-match chains.
They stop at the first failure in the order context, breakout, range, body,
close position, volume, RS, extension. A visible `No 8-bar breakout` or `weak
close` therefore does not imply that later gates passed.

## S0-S5 instrumentation

The common base holds every Lane-C momentum-bar gate fixed except breakout,
close position, and volume. The sampled candidate universe is the union of any
S0-S4-qualified bar, an existing Lane-C breakout attempt, and an existing
F0/EC0-style confirmed bullish candidate before its breakout requirement. This
broader pre-gate universe ensures bars with multiple simultaneous failures are
retained rather than observing only the qualifying subset. The variants are:

- S0: breakout + close position + volume (current full momentum-bar gate set)
- S1: ignore breakout only
- S2: ignore close position only
- S3: ignore volume only
- S4: ignore breakout and close position, retain volume
- S5: diagnostic subset that passes breakout and close position but fails volume

S5 is classification only; it does not create eligibility beyond S3. Every
candidate entry record contains the complete gate vector, the dashboard-style
first blocker, and all simultaneous failures.

The deterministic identity is `symbol-time_close_epoch`. The study retains the
first event per symbol/session and does not rearm that day. This deliberately
prevents a run of consecutive momentum candles from becoming correlated pseudo-
samples. It also allows the one retained event to remain under observation
through EOD. The trade-off is that distinct later same-day setups are not
sampled.

Signal-time snapshots include date/time, symbol, candidate ID, A/B/C/D/E state,
entry close, day change, day and intraday RS, ATR, EMA extension, VWAP distance,
prior high, range/ATR, body fraction, close position, current-bar volume ratio,
liquidity state, session high, distance from session high, variants, first
blocker, and the complete pass/fail vector.

Forward tracking begins after the confirmed reference close. It records
+0.5/+1/+2/+3 and -0.5/-1/-2 reaches from later bar highs/lows, MFE, MAE, time
to each extreme, target/stop ordering at 0.5%, 1%, and 2%, close returns after
5/15/30 minutes, and return at the configured force-exit mark. If target and
stop occur in the same 5-minute bar, the result is
`AMBIGUOUS_SAME_BAR`; OHLC bars cannot establish intrabar order.

## Enablement and collection

Both settings are under `14. Lane C Gate Ablation Shadow`:

1. enable `Track S0-S5 Lane C gate ablations`;
2. enable `Send S0-S5 research telemetry alerts`; and
3. create a dedicated TradingView `Any alert() function call` alert/webhook for
   the research chart instance.

Results are emitted as `NG RESEARCH GA ENTRY` and `NG RESEARCH GA RESULT` alert
records. TradingView Pine cannot append to a local CSV or database, so the alert
log or a dedicated webhook receiver is the durable record. Turning either input
off stops telemetry; both default off. No plots or dashboard rows are added.

Use a separate research-only TradingView chart/alert instance and webhook. Pine
`alert()` calls share the instance's `Any alert() function call` stream, so this
separation avoids routing research messages into a paper/action consumer. The
study is prospective/realtime only, one symbol per chart instance, depends on
TradingView keeping the alert alive through force-exit, and cannot resolve
same-bar event order. It is logically isolated from A/B/C/D/E action behavior,
but Pine compilation and operational alert routing still need to be checked in
TradingView before prospective collection.

## ELLEN forensic check

For O/H/L/C `360.60 / 366.00 / 360.05 / 364.50`, range is 5.95, body fraction
is `3.90 / 5.95 = 65.546%` (passes 65%), and close position is
`4.45 / 5.95 = 74.790%` (fails 75%). The reported 1.02x current-bar volume also
fails Lane C's 2.0x gate. Relaxing weak close alone therefore still leaves the
candle blocked by volume. Breakout, range/ATR, context, RS, and extension cannot
be verified from those four prices alone, and this example says nothing about
forward profitability.

## Named forensic cases

- [INDIAMART — 2026-09-16, confirmed 10:30 IST bar](../research/cases/2026-09-16_indiamart_1030.md)

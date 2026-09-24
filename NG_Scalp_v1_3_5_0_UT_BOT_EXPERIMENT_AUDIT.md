# NG Scalp v1.3.5.0 — NG + UT Bot experiment audit

## Scope and immutable baseline

- Baseline: `NG_Scalp_Alerts_v1_3_4_4_0915_ENTRY_START.pine`
- Baseline SHA-256: `ad501e5fe25bf60986d027dd9379f5ce1b8df8f540677e50eb14e52f72a9a173`
- Experiment: `NG_Scalp_Alerts_v1_3_5_0_UT_BOT_EXPERIMENT.pine`
- Pine language: v6
- Production baseline modified: no

The baseline has no shared candidate, lane, armed, or ready state before BUY.
Its final `qualified` boolean immediately creates the live trade. Consequently,
this experiment uses that exact final qualification boundary as the NG arm event.
`NGSource` is `SUPERTREND_FLIP` or `BULLISH_BREAKOUT`; the baseline does not
provide a separate scanner lane identifier.

## Approved existing causal HTF exception

Existing NG 5m `request.security` `[1] + lookahead_on` pattern preserved
unchanged. The requested series references only the previous completed 5m bar;
no current/future 5m value is consumed by the UT experiment.

The regression test compares the complete call byte-for-byte, including the
symbol, `"5"` timeframe, every expression and offset, gaps setting, and
`lookahead=barmerge.lookahead_on`. The experiment adds no `request.security`
call and UT uses only the chart's one-minute series.

## Entry architecture

The existing NG qualification population and thresholds remain unchanged:

1. Existing confirmed one-minute NG qualification occurs.
2. If a confirmed UT BUY occurred zero through three completed bars earlier and
   UT remains bullish, entry occurs at the current NG qualification close.
3. Otherwise the qualification is frozen as an arm event.
4. A fresh confirmed UT BUY on the next first, second, or third one-minute bar
   enters at that UT bar's close.
5. The arm expires before bar four and is also cleared by entry, exit, the entry
   cutoff, daily reset, active-trade state, or daily trade cap.

An old bullish UT state is insufficient. A transition 20 bars earlier cannot
enter. `utFreshBuyConfirmed` is `barstate.isconfirmed and
ta.crossover(close, utTrail)`; `utFreshSellConfirmed` is
`barstate.isconfirmed and ta.crossunder(close, utTrail)`.

At every actual entry, including delayed entry, the script recalculates:

- entry from the actual confirmed bar close;
- hard stop as `min(low - ATR * stopBufferAtr, close - ATR * minStopAtr)`;
- quantity through the existing `calcQty`, retaining ₹500 default risk and
  ₹300,000 default exposure caps;
- cost viability from the actual entry, current stop geometry, and quantity.

No price, stop, or quantity from the earlier arm bar is reused.

## Faithful UT Bot port

The supplied v4 recurrence is ported directly to Pine v6 using `ta.atr`,
`math.max`, `math.min`, `ta.crossover`, and `ta.crossunder`. Defaults are:

- Enable UT-managed mode: true
- Key Value: 3.0
- ATR Period: 10
- Confirmation Window: 3 bars

The UT trail and position recurrence consume only current and previous chart
values. Crossover functions execute on every calculation, while entry and
normal exit require `barstate.isconfirmed`. No lookahead or future bar is used.

## Exit architecture

A UT-managed position is closed in this priority order:

1. `HARD_STOP`: existing structural stop touched; realtime protection retained.
2. `UT_SELL`: fresh confirmed close crossing below the UT trail.
3. `EOD`: existing confirmed 15:15 force exit.

The entire quantity is closed. The live UT exit block has no target-hit branch,
no 1.5R partial, no runner transition, and no Supertrend exit. Reaching 1.5R,
2R, 3R, or 5R only updates measurement fields and cannot close the position.

The legacy A/B/C/D engine remains research-only and begins at the actual UT
entry. Its fixed-target and partial calculations are counterfactual reference
models; none feeds the UT-managed live decisions.

## Alerts and fire control

The experiment emits action messages through the existing two action call sites:

- BUY: `alert(buyMessage, alert.freq_once_per_bar_close)`
- SELL: `alert(sellMessage, alert.freq_once_per_bar)`

BUY starts `🟢 NG+UT BUY | Schema=1` and includes symbol, actual entry time/price, hard
stop, full quantity, UT trail, NG source, NG qualification time, UT BUY time,
initial rupee risk, and qualification cost evidence.

SELL starts `🔴 NG+UT SELL | Schema=1` and includes the final reason, exit, all entry
provenance, MFE/MAE/peak R, 0.5/1/1.5/2/3/5R milestones, captured R, MFE capture
percentage, and giveback R. The existing intrabar-persistent exit latch prevents
duplicate realtime SELL alerts. An active position prevents duplicate BUYs.
Existing SHADOW_RESULT/SHADOW_EOD fire-control remains intact.

## Measurement-only outcomes

Per-trade state records NG qualification, UT BUY, actual entry, entry UT trail,
hard stop, quantity, initial rupee risk, MFE, MAE, peak unrealized R, milestone
attainment, exit timestamp/price, captured R, capture percentage, giveback, and
one of `UT_SELL`, `HARD_STOP`, or `EOD`.

Cumulative data-window outputs record completed trades, hard stops before UT
SELL, trades reaching the old 1.5R level, average winner R, and average loser R.
These values do not participate in any decision predicate.

## Display additions

All default to true:

- Show UT trail
- Show UT BUY labels
- Show UT SELL labels
- Show UT-managed entry/exit labels

The trail is green above/bullish and red below/bearish. Raw transitions use
`UT BUY`/`UT SELL`; action labels use `NG+UT BUY`/`NG+UT SELL`. Candle colors
are not changed.

## Verification matrix

1. Baseline file unchanged: SHA-256 assertion.
2. Separate experimental file: yes.
3. Supplied recursive UT logic ported directly: static assertion.
4. Key Value default 3.0: asserted.
5. ATR Period default 10: asserted.
6. Window default 3: asserted.
7. BUY confirmed and one-minute-gated: asserted and modeled.
8. SELL confirmed: asserted and modeled.
9. No new lookahead/future input: UT source audit and unchanged HTF-call test.
10. No live fixed 1.5R exit: live-exit section assertion.
11. No live 1.5R partial: live-exit section assertion.
12. Holding through 1.5R/3R while bullish: modeled.
13. Confirmed UT SELL closes full position: modeled.
14. Hard stop operational and has priority: modeled.
15. EOD exit operational: modeled.
16. Rupee-risk helper and default unchanged: static comparison.
17. Exposure cap and sizing helper unchanged: static comparison.
18. Delayed entry uses current close and recalculated quantity: static assertion.
19. Delayed entry recomputes current stop geometry: static assertion.
20. Active-position and exit-latch dedupe: static and modeled.
21. Arm expires after three completed bars: static and modeled.
22. Arm cleared by daily initialization/reset: static assertion.
23. One active UT-managed position per symbol: modeled.
24. Baseline behavior/file untouched: SHA-256 assertion.

Modeled scenarios cover A through L from the experiment specification, including
same-bar and prior-UT entries, delayed entry, expiry, stale UT state, holds at
1.5R and 3R, all three exits, disappearing intrabar BUY/SELL crosses, and
duplicate-entry prevention.

TradingView compilation must still be performed manually. After approval, a
new “Any alert() function call” alert is required because TradingView alerts
retain a snapshot of the script used when they were created.

## Local validation

- Focused NG Scalp suite: **79 passed**.
- Full offline repository suite: **1192 passed, 15 existing warnings**.
- `git diff --check`: clean.
- TradingView compilation: pending manual validation.

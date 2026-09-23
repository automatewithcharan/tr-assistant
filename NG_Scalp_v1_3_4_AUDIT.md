# NG Scalp v1.3.4 forward-research persistence audit

## Scope and baseline

The supplied `NGScalp1.3.3Shadow` source was used as the immutable baseline.

- Baseline title: `NG Scalp Alerts v1.3.3 - Causal Shadow Measurement`
- Baseline line count: 1,375
- Baseline SHA-256: `16c623a89aa715a0975f517a3f86c397a2b9bf008db9d31fd6d7afdf4c2862bf`
- Baseline focused audit: `9 passed`
- Existing shadow: Model A is the current live full exit; B is 50% at 1.5R plus the existing defended-structure runner; C is 50% at 2R plus that runner; D is the full structural runner. Same-bar ambiguity, completed-5m/next-1m-open precedence, cost estimates, and EOD fallback already existed.

The baseline file was not overwritten. The new Pine source has 1,678 lines and SHA-256 `cabdf515e739bd5d6ad4c410e998e51618c7b7bc5446ab1daa05942a774a2ee5`.

## Behavioral result

### Live before vs live after

| Behavior | v1.3.3 | v1.3.4 |
|---|---|---|
| BUY qualification and trigger | Existing confirmed-1m expression | Unchanged |
| Entry reference and timing | Confirmed close | Unchanged |
| Stop A | Signal low/ATR construction | Unchanged |
| Live target | 1.5R default/current `targetR` input | Unchanged |
| Live quantity | ₹500-risk/max-exposure `calcQty` | Unchanged |
| Cost viability gate | Existing gate | Unchanged |
| Stop/target ordering | Stop-conservative on ambiguous bar | Unchanged |
| Supertrend and EOD exits | Existing definitions | Unchanged |
| Cooldown/max trades per day | Existing definitions | Unchanged |
| `NG SCALP BUY` / `NG SCALP SELL` messages | Existing payloads | Byte-identical |

Expected and audited live result: **NO CHANGE**.

The tests freeze six causally relevant v1.3.3 regions by SHA-256: all live inputs, signal/core calculations, exit decision predicates, entry qualification, BUY payload, and SELL payload.

### Research before vs after

v1.3.4 adds durable, machine-readable per-anchor and daily evidence:

- one `SHADOW_RESULT` after an anchor's full-day A/B/C/D study is finalized;
- a per-anchor `resultEmitted` latch and `alert.freq_all`, preserving multiple anchors that finish on the same EOD bar;
- one optional `SHADOW_EOD` per symbol/day, including a delayed first-next-session fallback for symbols without a 15:15-or-later print;
- exact Stop A/Stop B levels, hit flags/times, pre-Stop-A excursion, post-Stop-A MFE, and later 1.5R/2R/3R/5R/8R reachability;
- research Stop B quantity uses the existing `riskBudget` (₹500 default) and maximum-exposure cap; it never writes `tradeQty` or `tradeStop`;
- first-entry/re-entry identity, prior exit evidence, elapsed seconds/bars, and four causal reset observations;
- entry-time VWAP, Supertrend, ATR, impulse/expansion, pullback, recent-high, and running-HOD measurements;
- entry economics and per-model gross/net/cost results;
- existing structural Model B, including partial and runner timestamps/prices, three execution legs after a partial, and runner giveback.

No RS/RVOL filter, extension veto, re-entry veto, new confirmation, wider live stop, live runner, larger target, or other live hypothesis was activated.

## New input and alert contracts

New input:

- `Send one SHADOW_RESULT per completed anchor` (`emitShadowResultAlert`, default `true`)

Existing input retained:

- `Send EOD shadow summary alert` (`emitResearchSummaryAlert`, default `false`), now controls the compact `SHADOW_EOD` record rather than mislabeled per-trade EOD messages.

Prefixes are exactly:

- `NG SCALP BUY`
- `NG SCALP SELL`
- `SHADOW_RESULT`
- `SHADOW_EOD`

Representative shortened per-trade record:

```text
SHADOW_RESULT | Schema=1 | Version=1.3.4 | Symbol=NSE:ABC | TradingDate=2026-09-22 | TradeId=ABC|...|1 | Trade=1 | EntryTime=2026-09-22T10:14:00+05:30 | EntryRef=100.5 | Target=101.7 | R0=0.8 | LiveQty=625 | StopA=99.7 | StopB=99.2 | StopAHit=Y | StopBHit=N | StopBQty=384 | LiveOutcomeR=-1 | LiveReason=STOP | AGrossR=-1 | BGrossR=-1 | CGrossR=-1 | DGrossR=2.41 | MFEBeforeStopA_R=0.7 | PostStopA_MFE_R=3.2 | ATargetFirst=N | Reach15=Y | Reach3=Y | FirstEntry=Y | VWAPAtr=0.72 | CostGrossPct=18.4 | ExactA=Y | ExactB=Y | ExactC=Y | ExactD=Y
```

Representative daily record:

```text
SHADOW_EOD | Schema=1 | Version=1.3.4 | Symbol=NSE:ABC | TradingDate=2026-09-22 | EmittedAt=2026-09-22T15:15:00+05:30 | Anchors=3 | Completed=3 | FirstEntries=1 | Reentries=2 | StopAHits=2 | StopBHits=1 | StopASurvivedB=1 | ATargetFirst=1 | Reached3R=2 | Reached5R=1 | Reached8R=0 | AvgNetR_A=0.12 | AvgNetR_B=0.44 | AvgNetR_C=0.31 | AvgNetR_D=0.53 | PairedAB=3 | PairedAC=3 | PairedAD=3
```

The actual `SHADOW_RESULT` includes all A/B/C/D reasons, exits, timestamps, exactness flags, gross/net R, costs, Model B partial/runner evidence, ambiguity, reset telemetry, entry extension, and economics. It is intentionally one record rather than several noisy companion alerts.

## Causality notes

- Entry telemetry is frozen only at the confirmed BUY reference. The running HOD includes only bars known through that confirmed bar.
- Later bars are read only by the shadow outcome engine.
- The sole `request.security` call still requests completed 5m values using `[1]`; `lookahead_on` therefore does not import the active/future 5m candle.
- A same-1m stop plus milestone remains ambiguous. Favorable sequencing is not assumed.
- The existing completed-5m structural exit retains next-1m-open precedence.
- Historical OHLC cannot provide the true intrabar tick time. Shadow stop/milestone timestamps identify the containing 1m bar; confirmed-close events use `time_close`. A realtime live exit history uses `timenow` at the first alert boundary.
- “Bull expansion bars” includes the fully confirmed signal/entry candle at offset zero because it is known at the BUY reference.

## Cost model review

The existing Groww helper is not duplicated or changed. The shadow leg helper deliberately mirrors its brokerage, tax/fee, and per-leg slippage assumptions. v1.3.4 exports those estimates and derives cost/gross-target and cost/planned-risk percentages. These remain modeled costs, not broker contract-note reconciliation.

## Static and test results

- Constructor audit: 161 `ShadowStudy` fields and 161 constructor arguments.
- Delimiter/string-aware structural scan: balanced.
- Focused v1.3.4 plus existing Pine audits: `18 passed in 1.09s`.
- Full repository suite through the required offline launcher: `1112 passed, 15 warnings in 158.48s`.
- A direct raw `pytest` attempt was rejected during collection by the repository's fail-closed subprocess guard; rerunning with `scripts/test_offline.sh -q` produced the passing result above.
- No Pine compiler is installed locally. A true TradingView compile/add-to-chart check remains required. The local checks validate shape and invariants but cannot substitute for TradingView's compiler/runtime.

## TradingView setup

1. Open Pine Editor, paste `NG_Scalp_Alerts_v1_3_4_FORWARD_RESEARCH_PERSISTENCE.pine`, save it as a new script, compile it, and add it to a 1-minute chart.
2. Keep `Mode = LIVE ALERTS` and `Enable A/B/C/D shadow exit study = true`.
3. Keep `Send one SHADOW_RESULT per completed anchor = true`.
4. Enable `Send EOD shadow summary alert` if the daily aggregate is wanted.
5. Create an alert whose condition is this indicator's **Any alert() function call**. Configure expiry and webhook/log delivery as required.
6. Confirm the new alert is active, then disable the old v1.3.3 alert. Running both creates duplicate live BUY/SELL notifications.
7. Export the TradingView alert log after the session and parse records by their exact prefixes and `key=value` fields.

**Old TradingView alerts must be recreated.** TradingView alerts retain a server-side snapshot of the script and inputs from creation time; editing or adding a new script does not update an existing running alert.

## Remaining limitations

- TradingView compilation and one live/replay smoke test are still needed.
- OHLC-only ambiguity cannot be resolved without finer/tick data.
- `SHADOW_RESULT` intentionally arrives after full-day finalization so later reachability is complete; it is not an immediate exit notification.
- On an illiquid symbol with no bar at/after the configured force-exit time, the finalization and EOD summary are delayed until the first next-session bar and marked by fallback model reasons.
- Pine state and TradingView delivery are not an external database. Durability depends on a correctly configured running TradingView alert and retained/exported alert logs or a webhook receiver.
- Arrays remain cumulative during the script instance, as in v1.3.3; extremely long-running chart instances are still subject to TradingView resource limits.

## Files and diff summary

- `NG_Scalp_Alerts_v1_3_4_FORWARD_RESEARCH_PERSISTENCE.pine`: new version; v1.3.3 untouched.
- `NG_Scalp_v1_3_3_to_v1_3_4.diff`: CRLF-normalized unified diff; 313 meaningful additions and 10 deletions.
- `NG_Scalp_v1_3_4_AUDIT.md`: this audit and deployment guide.
- `tests/test_ng_scalp_v1_3_4_research_persistence.py`: nine static invariant/payload/causality checks.

No commit or push was performed.

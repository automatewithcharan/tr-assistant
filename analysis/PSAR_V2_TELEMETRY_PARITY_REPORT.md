# PSAR V2 Telemetry Parity Report

## Verdict

```text
Trading behavior changed: NO
```

The telemetry version preserves the reviewed V2 trading predicates, equations, priority order, state transitions, and alert call frequencies. Changes are limited to measurement-only state, diagnostic calculations, and alert payload text. The original source file was not modified.

The alert payload schema intentionally changed from prose to `key=value`. This does not change Pine decisions, but an external executor that parses the old prose would be incompatible. Deploy this telemetry script as a shadow/measurement alert, or update and validate the downstream parser before routing it to any execution path.

This verdict is supported by byte-identical frozen-block comparison and decision-state assignment comparison. TradingView's Pine compiler/runtime is not present in this repository, so compilation in TradingView remains a deployment gate; see Limitations.

## Source of truth

| Item | Value |
|---|---|
| Source file | `PSAR_Liquidity_Trade_Manager_V2_INDICATOR.pine` |
| Latest source commit | `7ac3281411d0c68beeb737bae6ed511b2f0d3cb3` (`Harden PSAR liquidity trade manager`) |
| Current fetched `origin/main` | `b6202324b79f5b65440a0277af99b917824ec799` |
| Source SHA-256 | `a00ec8efe06eaecf205a59e265db35172e6a871793ae871012cc034d0f6878d5` |
| New file | `PSAR_Liquidity_Trade_Manager_V2_TELEMETRY.pine` |
| Schema | `PSAR_LIQ_V2_TLM_1` |

The source schema was also independently tied to the Sep 28 CSV in `analysis/PSAR_V2_SEP28_FORENSIC_REPORT.md`.

## Behavior-freeze inventory

| Behavior | Frozen executable rule | Source locations |
|---|---|---|
| Timeframe/session | 1-minute only; entry window uses bar-close time from 09:15 through 14:50 in configured timezone | 123–145, 297–311 |
| PSAR entry | `ta.sar(0.02, 0.02, 0.20)` inputs; confirmed bearish-to-bullish direction flip | 33–45 |
| Entry gate | Flat slot, no exit latched this bar, confirmed PSAR BUY, optional filters pass, sizing valid, liquidity PASS | 297–313 |
| Liquidity status | DISABLED→PASS; not-ready→UNKNOWN; otherwise volume, turnover, capacity and optional RVOL conjunction | 147–165, 197–224 |
| Liquidity baseline | Full lookback of prior completed current-session bars; uses `volume[1]` and `(close*volume)[1]` | 147–165 |
| Optional filters | Existing EMA, VWAP, ADX and entry-RVOL clauses; default OFF | 102–117, 167–171 |
| Sizing | Minimum of floor(risk budget/risk per share), floor(exposure/entry), and max quantity | 208–213 |
| Initial stop | Rounded-down minimum of signal-low minus 0.1 ATR and entry minus 0.5 ATR | 297–303 |
| Target | Rounded-up entry plus configured 2R when enabled | 320–322 |
| Break-even | Existing enable/activation/offset logic; default disabled | 83–85, 226–235 |
| Profit lock | Existing +1R activation and +0.25R floor | 87–89, 236–240 |
| ATR trail | Existing +1R activation, highest price minus 1.5 ATR, monotonic ratchet | 91–99, 242–248, 411–418 |
| Realtime stop | Current observed close at/below active stop; theoretical exit is close minus configured tick | 408–438 |
| Historical stop | Stop carried into bar; open-or-stop reference minus configured tick; ambiguous SL/target retained | 448–485 |
| Exit priority | Protective stop, target, EOD, PSAR fallback | 430–475 |
| PSAR fallback | Existing confirmed fallback setting and realtime/historical branches | 37–45, 426–446, 470–475 |
| EOD | Confirmed bar at/after 15:15; checked before PSAR fallback | 142–145, 424, 439–446 |
| Re-entry | No daily entry limit; exit makes state flat; same-bar exit latch blocks re-entry | 287–292, 306–313, 487–513 |
| Alert timing | BUY and SKIP remain once-per-bar-close; EXIT remains `alert.freq_all` after the same exit latch | 350, 383, 496–509 |

Locations above refer to the 627-line frozen source. Line numbers differ in the instrumented copy because telemetry was inserted.

## Instrumentation placement and equivalence

### Entry

The original assignments from `if buyReady` through `stopAtBarOpenIp := activeStopIp` remain byte-for-byte and in the same order. Trade identity and entry snapshots are initialized only after those assignments. The existing BUY alert call remains once per bar close; only its message string changed.

### Management and exits

MFE/MAE variables are separate from `highestSinceEntryIp`. They are never passed into `f_stopCandidate` and never read by a stop, target, fallback, session, or entry predicate. The original realtime and historical exit branches remain byte-for-byte.

Exit calculations are performed only after `shouldExit and not exitLatchedThisBarIp` has already latched the unchanged reason, exit price, and stops. Previous-trade telemetry is written after the EXIT alert is constructed and before the original `inTradeIp := false`; no decision reads it.

### Liquidity diagnostics

`f_liquidityReason` recomputes human-readable failure components from the already-existing measurements. `buyReady`, `lowLiquiditySkip`, and `unknownLiquiditySkip` continue to use only the original integer `liquidityStatusPreview`. Diagnostic reason text has no path back into a predicate.

### PSAR churn

The flat-state flip counter runs after all entry, management, exit, and marker logic. It is excluded on an exit-latched execution. Its only consumer is the next BUY payload.

## Telemetry added

- Deterministic per-symbol/day `trade_seq` and `trade_id`, preserved from BUY through EXIT.
- Entry-bar OHLC, current/previous PSAR values and directions, flip flag, PSAR distance and ATR-normalized distance.
- Initial risk geometry, all sizing caps, notional, intended rupee risk, target and management activation thresholds.
- ATR and signal-bar range context.
- Full liquidity baseline, current measurements, thresholds, capacity, state, sample count, and diagnostic reason.
- Existing optional-filter configuration, values, and clause outcomes.
- Prior exit reason/R, bars/seconds since exit, and confirmed flat-state PSAR flip count.
- Causal post-entry MFE, MAE, R-normalized excursions, and +0.25R/+0.5R/+1R/+1.5R/+2R milestones.
- Management activation flags, bars held, observed elapsed seconds, and HARD STOP timing.
- HARD STOP alert stop-cross overshoot in price, R, and rupees.
- Measurement-only exact-managed-stop shadow P&L and R.

The complete 117-field event dictionary is in `analysis/PSAR_V2_TELEMETRY_FIELD_DICTIONARY.md`.

## Static validation

Run:

```bash
python3 analysis/verify_psar_v2_telemetry_parity.py
```

Validated result:

```text
PASS: trading behavior changed: NO
source_sha256=a00ec8efe06eaecf205a59e265db35172e6a871793ae871012cc034d0f6878d5
frozen_blocks_verified=15
decision_assignments_verified=141
alert_sites_verified=4
unique_payload_fields_documented=117
delimiter_balance_verified=true
```

The checker fails if:

- the frozen source SHA changes;
- any of 15 executable source blocks is no longer present byte-for-byte;
- any assignment to the 80+ enumerated decision-driving variables is added, removed, reordered, or textually changed; or
- BUY, SKIP, or EXIT alert call frequency/site counts change.
- emitted field counts change, a payload's static literal portion reaches 1,800 characters, or a field is undocumented.

Additional validation performed:

- All 117 emitted keys occur in the field dictionary.
- Static payload literal sizes are approximately 1,498 characters for BUY, 567 for SKIP, 881 for EXIT, and 173 for STOP_UPDATE before dynamic values.
- The telemetry/source diff is 299 insertions and 54 deletions; deletions are the original human-readable BUY/SKIP/EXIT/STOP_UPDATE payload construction and indicator title, replaced by machine-readable payload construction. Core trading blocks are retained.

## Pine and evidence limitations

1. **No TradingView compiler in the repository.** Static syntax/parity checks pass, but the file must be pasted into TradingView and compiled before an alert is created. This is an external validation gate, not evidence of a behavior difference.
2. **Realtime observation is not an exchange tick tape.** MFE/MAE use TradingView's accumulated `high`/`low` on executions after the entry bar. They are causal observations but cannot prove exchange tick ordering or broker fills.
3. **Reload cannot recreate realtime tick order.** Historical bars expose OHLC only. The existing strategy already distinguishes realtime and historical management; telemetry cannot recover lost intrabar order after reload.
4. **Entry-bar excursion is deliberately excluded.** Entry occurs on confirmed bar close, so the signal bar's earlier high/low was not post-entry movement. Excursion begins when the existing manager becomes eligible on a later bar.
5. **Elapsed seconds are observation time.** Realtime uses `timenow`; this is suitable for the requested under-30-second and minute buckets, but it is not exchange or broker time. Historical fallback uses `time_close` and is not presented as tick timing.
6. **Stop overshoot is not broker slippage.** `alert_stop_cross_overshoot` is active stop minus Pine's observed `close`. The existing theoretical exit may additionally subtract the configured exit tick. Neither is an actual fill.
7. **Trade sequence includes reconstructed entries.** On script load, Pine processes historical entry episodes deterministically. A mid-session alert may therefore begin with `trade_seq > 1`; this is intentional and makes the ID reflect the script's same-day state, but it does not imply all earlier BUYs were delivered as realtime alerts.
8. **Raw PSAR churn while in a trade is not emitted.** The requested churn counter measures confirmed flips after an exit while flat, including the next entry flip. Entry and exit payloads still expose the actual entry flip and PSAR fallback outcome.
9. **Alert payload delivery is not proven locally.** The CSV exporter/webhook may impose its own storage or transport limits. Static literal sizes retain headroom, but a forward smoke alert should verify full, untruncated delivery.

## Telemetry that cannot be collected reliably from this indicator alone

- Actual broker fill price, broker latency, exchange timestamp, spread, and order-book depth.
- True broker slippage versus a gap versus TradingView feed/update cadence.
- Intrabar event ordering after a chart/script reload.
- Counterfactual outcomes after an actual exit without adding a separate shadow study.
- Whether a FAIL/UNKNOWN candidate would have become profitable; SKIP records now contain enough causal entry/liquidity context for a separate forward shadow-outcome collector.

## Files and diff scope

Created:

- `PSAR_Liquidity_Trade_Manager_V2_TELEMETRY.pine`
- `analysis/PSAR_V2_TELEMETRY_PARITY_REPORT.md`
- `analysis/PSAR_V2_TELEMETRY_FIELD_DICTIONARY.md`
- `analysis/verify_psar_v2_telemetry_parity.py` (automated parity guard)

Unchanged:

- `PSAR_Liquidity_Trade_Manager_V2_INDICATOR.pine` at the source ref
- All strategy parameters and decision logic

No commit was created.

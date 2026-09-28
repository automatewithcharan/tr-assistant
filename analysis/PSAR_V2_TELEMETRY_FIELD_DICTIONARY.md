# PSAR V2 Telemetry Field Dictionary

Schema identifier: `PSAR_LIQ_V2_TLM_1`. Alerts are newline-delimited `key=value` records. `NA` means the value is disabled, unavailable, or inapplicable to that event. Prices and rupee values use the instrument's TradingView precision unless noted.

## Identity and time

| Field | Event type | Definition | Units | Calculation | Why we need it |
|---|---|---|---|---|---|
| `schema` | BUY, SKIP, EXIT, STOP_UPDATE | Payload schema version | text | Constant `PSAR_LIQ_V2_TLM_1` | Safe downstream parsing and migrations |
| `event` | BUY, SKIP, EXIT, STOP_UPDATE | Lifecycle event | enum | Literal event name | Route records |
| `symbol` | BUY, SKIP, EXIT, STOP_UPDATE | Exchange-qualified TradingView symbol | text | `syminfo.tickerid` | Cross-alert identity |
| `timestamp_bar_close_ms` | BUY, SKIP | Signal bar close timestamp | Unix ms | `time_close` | Reconstruct signal time |
| `timestamp_observed_ms` | BUY, EXIT, STOP_UPDATE | Pine observation time | Unix ms | Realtime `timenow`; historical fallback `time_close` | Measure observed elapsed time without claiming exchange time |
| `trade_id` | BUY, EXIT, STOP_UPDATE | Deterministic symbol/day/sequence ID | text | `tickerid\|YYYYMMDD\|trade_seq` | Exact lifecycle pairing |
| `trade_seq` | BUY, EXIT, STOP_UPDATE | Entry number for the symbol's session-calendar day | integer | Incremented after each actual BUY; reset on timezone calendar-day change | First-entry versus re-entry analysis |
| `bars_since_previous_exit` | BUY | Chart bars since prior exit | bars | `bar_index - previousExitBar` | Churn/cooldown measurement |
| `seconds_since_previous_exit` | BUY | Observed elapsed time since prior exit | seconds | Difference of observation timestamps | Short-horizon churn measurement |
| `previous_exit_reason` | BUY | Prior completed trade's exit reason | enum | Stored at prior exit; `NONE` if absent | Re-entry path attribution |
| `previous_trade_r` | BUY | Prior trade's theoretical realized R | R | Prior theoretical P&L / prior intended risk | Test loss-following re-entry behavior |
| `psar_flips_since_previous_exit` | BUY | Confirmed bullish and bearish PSAR flips while flat, including the entry flip | count | Flat-state confirmed flip counter + current BUY flip | Quantify PSAR churn |

## Entry price, PSAR, risk, and volatility

| Field | Event type | Definition | Units | Calculation | Why we need it |
|---|---|---|---|---|---|
| `signal_price` | BUY, SKIP | Rounded signal close | price | `round_to_mintick(close)` | Reconcile signal and assumed entry |
| `assumed_entry` | SKIP | Hypothetical assumed entry for an untraded signal | price | Signal plus configured entry ticks, rounded up | Compare skipped candidate geometry |
| `entry_price` | BUY, EXIT, STOP_UPDATE | Frozen assumed entry | price | Existing `assumedEntryIp` | P&L and excursion base |
| `bar_open` | BUY | Signal-bar open | price | `open` at BUY | Entry-bar shape |
| `bar_high` | BUY | Signal-bar high known at signal time | price | `high` at BUY | Entry context |
| `bar_low` | BUY | Signal-bar low known at signal time | price | `low` at BUY | Stop geometry context |
| `bar_close` | BUY | Signal-bar close | price | `close` at BUY | Entry context |
| `psar_value` | BUY, SKIP | Current PSAR value | price | Existing `psar` | Diagnose trigger geometry |
| `previous_psar_value` | BUY, SKIP | Previous bar's PSAR | price | `psar[1]` | Diagnose flip |
| `psar_direction` | BUY, SKIP | Current encoded PSAR state | `1` bullish, `-1` bearish | Existing `psarDir` | Trigger state |
| `previous_psar_direction` | BUY, SKIP | Previous encoded PSAR state | `1` or `-1` | `psarDir[1]` | Confirm transition |
| `did_psar_flip_this_bar` | BUY, SKIP | Bearish-to-bullish flip flag | boolean | Existing `psarBuySignal` | Prove entry trigger |
| `distance_entry_to_psar` | BUY | Entry minus PSAR | price | `assumedEntryIp - psar` | Absolute trigger extension |
| `distance_entry_to_psar_atr` | BUY | Entry-to-PSAR distance normalized by ATR | ATR | Distance / ATR | Cross-symbol comparison |
| `initial_stop` | BUY, SKIP, EXIT | Frozen initial protective stop | price | Existing initial-stop calculation/state | Initial risk geometry |
| `stop_distance` | BUY | Initial entry-to-stop distance | price | Entry minus initial stop | Risk denominator |
| `stop_distance_atr` | BUY | Initial stop distance normalized by ATR | ATR | Stop distance / ATR | Identify tight/wide initial geometry without changing it |
| `risk_based_qty` | BUY, SKIP | Quantity allowed by ₹500 budget | shares | `floor(riskBudget / riskPerShare)` | Audit sizing constraint |
| `exposure_based_qty` | BUY, SKIP | Quantity allowed by exposure cap | shares | `floor(maxExposure / entry)` | Audit sizing constraint |
| `intended_qty` | BUY, SKIP | Minimum of risk, exposure, and max-quantity limits | shares | Existing `f_intendedQty` | Audit pre-liquidity size |
| `final_qty` | SKIP | Executed quantity for skipped signal | shares | Constant zero | Make non-trade explicit |
| `qty` | BUY, EXIT, STOP_UPDATE | Frozen actual intended trade quantity | shares | Existing `quantityIp` | P&L and rupee exposure |
| `position_value` | BUY | Entry notional | ₹ | Entry × quantity | Cost/capacity analysis |
| `intended_rupee_risk` | BUY, EXIT | Initial planned risk | ₹ | Risk/share × quantity | Compare intended and realized risk |
| `initial_target` | BUY, SKIP, EXIT | Frozen fixed target, or `NA` | price | Existing target calculation/state | Target-path attribution |
| `target_distance` | BUY | Entry-to-target distance | price | Target minus entry | Geometry audit |
| `target_r` | BUY | Configured target multiple, or `NA` | R | Existing `targetR` if enabled | Configuration attribution |
| `breakeven_trigger_price` | BUY | BE activation level, or `NA` when disabled | price | Entry + risk/share × BE activation R | Explain management opportunity |
| `profit_lock_trigger_price` | BUY | Profit-lock activation level, or `NA` | price | Entry + risk/share × lock activation R | Explain management opportunity |
| `atr_trail_activation_price` | BUY | ATR-trail activation level, or `NA` | price | Entry + risk/share × trail activation R | Explain management opportunity |
| `atr` | BUY | Entry-time ATR used by the script | price | Existing `ta.atr(atrLength)` | Volatility normalization |
| `bar_range` | BUY | Signal-bar high minus low | price | `high - low` | Entry-bar expansion |
| `bar_range_atr` | BUY | Signal-bar range normalized by ATR | ATR | Bar range / ATR | Compare entry impulse |

## Liquidity and optional-filter context

| Field | Event type | Definition | Units | Calculation | Why we need it |
|---|---|---|---|---|---|
| `skip_reason` | SKIP | High-level skip classification | enum | LOW LIQUIDITY or LIQUIDITY UNKNOWN | Separate evaluated failure from immature baseline |
| `liquidity_state` | BUY, SKIP | Existing liquidity outcome | PASS/FAIL/UNKNOWN/DISABLED | Existing status | Verify gate behavior |
| `liquidity_reason` | BUY, SKIP | Diagnostic failed component(s) | text | PASS, DISABLED, BASELINE_NOT_READY, or semicolon list of volume/value/capacity/RVOL failures | Explain gate result without feeding it back |
| `liquidity_lookback` | BUY, SKIP | Configured prior-bar lookback | bars | Existing input | Attribute chart configurations |
| `liquidity_baseline_sample_count` | BUY, SKIP | Completed current-session bars | bars | Existing `completedSessionBars` | Prove readiness |
| `liquidity_baseline_avg_volume` | BUY, SKIP | Prior-bar session volume SMA | shares/minute | Existing `avgVolume` | Actual capacity input |
| `liquidity_baseline_avg_traded_value` | BUY, SKIP | Prior-bar traded-value SMA | ₹/minute | Existing `avgTradedValue` | Actual turnover input |
| `current_volume` | BUY, SKIP | Current signal-bar volume | shares | `volume` | RVOL numerator/context |
| `current_traded_value` | BUY, SKIP | Current close × volume proxy | ₹ | `close * volume` | Current liquidity context |
| `liquidity_capacity_qty` | BUY, SKIP | Participation capacity | shares | `floor(avgVolume × maxParticipationPct / 100)` | Identify capacity failures |
| `liquidity_required_capacity_qty` | BUY, SKIP | Required capacity | shares | `ceil(intendedQty × minCapacityMultiple)` | Capacity threshold |
| `liquidity_max_participation_pct` | BUY | Configured volume participation limit | percent | Existing input | Configuration audit |
| `liquidity_min_avg_volume` | BUY | Minimum baseline volume | shares/minute | Existing input | Threshold audit |
| `liquidity_min_avg_traded_value` | BUY | Minimum baseline turnover | ₹/minute | Existing input | Threshold audit |
| `liquidity_min_capacity_multiple` | BUY | Required capacity/intended quantity | ratio | Existing input | Threshold audit |
| `relative_volume` | BUY, SKIP | Current volume / prior baseline | ratio | Existing `relativeVolume` | RVOL context |
| `liquidity_rvol_gate_enabled` | BUY | Whether RVOL participates in liquidity PASS | boolean | Existing input | Interpret PASS reason |
| `liquidity_min_rvol` | BUY | RVOL threshold | ratio | Existing input | Threshold audit |
| `session_bar_close_minute` | BUY | Local bar-close minute of day | minutes after midnight | Existing `barCloseMinute` | Session placement |
| `minutes_since_session_start` | BUY | Minutes since configured 09:15-style start | minutes | Bar-close minute − configured start | Time-of-day analysis |
| `optional_filters_pass` | BUY | Combined existing optional-filter result | boolean | Existing predicate | Prove gate state |
| `ema_filter_enabled` | BUY | EMA filter setting | boolean | Existing input | Configuration audit |
| `ema_value` | BUY | Entry-time EMA | price | Existing `ema` | Filter context |
| `ema_filter_pass` | BUY | EMA clause result | boolean | Disabled OR close > EMA | Explain optional gate |
| `vwap_filter_enabled` | BUY | VWAP filter setting | boolean | Existing input | Configuration audit |
| `vwap_value` | BUY | Entry-time session VWAP | price | Existing `sessionVwap` | Filter context |
| `vwap_filter_pass` | BUY | VWAP clause result | boolean | Disabled OR close > VWAP | Explain optional gate |
| `adx_filter_enabled` | BUY | ADX filter setting | boolean | Existing input | Configuration audit |
| `adx_value` | BUY | Entry-time ADX | index | Existing `adx` | Filter context |
| `adx_minimum` | BUY | Configured minimum ADX | index | Existing input | Threshold audit |
| `adx_filter_pass` | BUY | ADX clause result | boolean | Disabled OR ADX ≥ minimum | Explain optional gate |
| `entry_rvol_filter_enabled` | BUY | Entry RVOL filter setting | boolean | Existing input | Configuration audit |
| `entry_rvol_minimum` | BUY | Entry RVOL threshold | ratio | Existing input | Threshold audit |
| `entry_rvol_filter_pass` | BUY | Entry RVOL clause result | boolean | Disabled OR available RVOL ≥ minimum | Explain optional gate |

## Exit, excursion, and management path

| Field | Event type | Definition | Units | Calculation | Why we need it |
|---|---|---|---|---|---|
| `exit_price_theoretical` | EXIT | Existing simulated exit reference | price | Target price or observed close adjusted by configured exit ticks | Preserve current P&L semantics |
| `observed_price_at_exit` | EXIT | Pine's current observed `close` when the exit latched | price | `close` | Separate observation from simulated exit reference |
| `exit_reason` | EXIT | Selected existing exit path | enum | Existing priority chain | Mechanism attribution |
| `current_managed_stop` | EXIT | Active stop when exit latched | price | Existing `activeStopIp` | Stop-path audit |
| `old_managed_stop_this_execution` | EXIT | Stop before this execution's ratchet | price | Existing `oldStopThisExecution` | Detect same-update ratchets |
| `gross_pnl_theoretical` | EXIT | Existing theoretical gross P&L | ₹ | (theoretical exit − entry) × quantity | Performance attribution |
| `realized_r_theoretical` | EXIT | Existing theoretical P&L normalized by intended risk | R | Gross theoretical P&L / intended rupee risk | Comparable outcomes |
| `bars_held` | EXIT | Chart-bar difference | bars | `bar_index - entryBar` | Holding-time bucket |
| `observed_elapsed_seconds` | EXIT | Pine observation elapsed time | seconds | Exit observation ms − entry observation ms | Rapid-exit analysis |
| `bars_to_stop` | EXIT | Bars to HARD STOP; otherwise `NA` | bars | `bars_held` for HARD STOP | Stop timing |
| `seconds_to_stop` | EXIT | Observed seconds to HARD STOP; otherwise `NA` | seconds | Observed elapsed seconds for HARD STOP | Requested timing buckets |
| `breakeven_activated` | EXIT | Whether post-entry high reached configured BE trigger while BE was enabled | boolean | Latched causal threshold observation | Management-path classification |
| `profit_lock_activated` | EXIT | Whether post-entry high reached lock trigger while enabled | boolean | Latched causal threshold observation | Management-path classification |
| `atr_trail_activated` | EXIT | Whether post-entry high reached trail trigger while enabled | boolean | Latched causal threshold observation | Management-path classification |
| `psar_fallback_active` | EXIT | Whether PSAR fallback was the selected exit | boolean | Exit reason equals PSAR FALLBACK | Fallback attribution |
| `mfe_price` | EXIT | Highest price observed after the entry bar and through exit | price | Running maximum initialized at entry | Favorable excursion |
| `mfe_price_move` | EXIT | Non-negative favorable move from entry | price | max(0, MFE price − entry) | Absolute excursion |
| `max_favorable_excursion_r` | EXIT | Favorable move normalized by initial risk/share | R | MFE move / risk per share | Separate entry quality from management |
| `mae_price` | EXIT | Lowest price observed after the entry bar and through exit | price | Running minimum initialized at entry | Adverse excursion |
| `mae_price_move` | EXIT | Signed adverse move from entry | price | MAE price − entry; zero or negative | Absolute adverse path |
| `max_adverse_excursion_r` | EXIT | Signed adverse move normalized by initial risk/share | R | MAE move / risk per share | Entry/stop diagnosis |
| `mfe_before_stop_r` | EXIT | MFE R for HARD STOP exits; otherwise `NA` | R | Current trade MFE R | Distinguish never-worked from worked-then-failed |
| `reached_025r` | EXIT | Trade observed at least +0.25R | boolean | Running high ≥ entry + 0.25R | Path milestone |
| `reached_05r` | EXIT | Trade observed at least +0.5R | boolean | Running high ≥ entry + 0.5R | Path milestone |
| `reached_1r` | EXIT | Trade observed at least +1R | boolean | Running high ≥ entry + 1R | Path milestone |
| `reached_15r` | EXIT | Trade observed at least +1.5R | boolean | Running high ≥ entry + 1.5R | Path milestone |
| `reached_2r` | EXIT | Trade observed at least +2R | boolean | Running high ≥ entry + 2R | Path milestone |

## HARD STOP observation and shadow outcome

These fields are populated only when the existing exit reason is exactly `HARD STOP`; otherwise they are `NA`. They describe Pine's alert observation and are explicitly **not broker slippage or fill evidence**.

| Field | Event type | Definition | Units | Calculation | Why we need it |
|---|---|---|---|---|---|
| `managed_stop_at_cross` | EXIT/HARD STOP | Active stop when the stop exit latched | price | Existing `activeStopIp` | Stop reference |
| `alert_stop_cross_observed_price` | EXIT/HARD STOP | Pine `close` observed at stop detection | price | Current `close` | Observation reference |
| `alert_stop_cross_overshoot` | EXIT/HARD STOP | Amount observed price was below stop | price | max(0, managed stop − observed close) | Quantify alert stop-cross overshoot |
| `alert_stop_cross_overshoot_r` | EXIT/HARD STOP | Overshoot normalized by initial risk/share | R | Overshoot / risk per share | Cross-symbol severity |
| `alert_stop_cross_overshoot_rupees` | EXIT/HARD STOP | Overshoot × quantity | ₹ | Overshoot × quantity | Risk-budget impact |
| `theoretical_stop_fill_pnl` | EXIT/HARD STOP | Measurement-only P&L if filled exactly at managed stop | ₹ | (managed stop − entry) × quantity | Shadow out stop-cross overshoot |
| `theoretical_stop_fill_r` | EXIT/HARD STOP | Exact-stop shadow P&L normalized by intended risk | R | Theoretical stop-fill P&L / intended risk | Answer whether expectancy remains weak at magical stop fills |

## Optional stop-update event

The existing `emitStopUpdateAlerts` input and its decision/timing are unchanged. When enabled, its payload now uses the same schema and trade identity.

| Field | Event type | Definition | Units | Calculation | Why we need it |
|---|---|---|---|---|---|
| `highest_since_entry` | STOP_UPDATE | Existing management high | price | `highestSinceEntryIp` | Explain ratchet context |
| `old_stop` | STOP_UPDATE | Stop carried into the confirmed bar | price | Existing `baseStop` | Measure stop movement |
| `new_stop` | STOP_UPDATE | Current ratcheted stop | price | Existing `activeStopIp` | Measure stop movement |
| `stop_update_reason` | STOP_UPDATE | Active stop mechanism | enum | Existing `activeStopReasonIp` | Attribute BE/lock/trail changes |

## Interpretation constraints

- MFE/MAE excludes the signal/entry bar because its earlier intrabar path predates the confirmed close entry. It begins on the first later chart bar, matching when the existing manager becomes active.
- On realtime bars, `high` and `low` are TradingView's accumulated bar observations. They are causal but not an exchange tick tape. Historical reload cannot recover intrabar ordering.
- `observed_elapsed_seconds`, stop overshoot, and theoretical P&L are Pine/alert measurements. None is an actual broker fill, broker latency, or exchange timestamp.
- `liquidity_reason` is diagnostic recomputation only. The existing integer liquidity status remains the sole input to trade decisions.

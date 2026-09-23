# NG Scalp v1.3.4.4 — 09:15 entry-start audit

## Scope and provenance

- Immutable base: `NG_Scalp_Alerts_v1_3_4_3a_OVERLAP_TELEMETRY_FIX.pine`
- Base SHA-256: `c9ed65ab1b1ade0a8233ed5859573234f206ac1bb3341137aaef968bea2fa756`
- Revised file: `NG_Scalp_Alerts_v1_3_4_4_0915_ENTRY_START.pine`
- Revised SHA-256: `ad501e5fe25bf60986d027dd9379f5ce1b8df8f540677e50eb14e52f72a9a173`
- Patch: `NG_Scalp_v1_3_4_3a_to_v1_3_4_4.diff`

The sole behavioral change is the default `entryStartMinute`, from 20 to 15. The other three changed lines are identity/comment/dashboard metadata. `EntryStartCohort=0915` was deliberately not inserted into BUY, SELL, `SHADOW_RESULT`, or `SHADOW_EOD`: those payloads were required to remain byte-identical. The new filename, indicator title, short title, and version comment identify the 09:15 cohort without altering alert schemas.

## Session-gate audit

The entry helper reads `hour(time_close, "Asia/Kolkata")` and `minute(time_close, "Asia/Kolkata")`. Its only call is `entryWindowOpen(time_close)`. The start defaults are 09:15; the strict 14:50 cutoff is unchanged. The 15:15 force-exit helper and its call sites are unchanged. A source scan found no remaining `09:20`, `9, 20`, or second entry-start gate.

Entry still requires a 1-minute chart (`timeframe.isminutes and timeframe.multiplier == 1`) and `if canLookForBuy and barstate.isconfirmed`. Consequently, on an ordinary one-minute chart the first eligible opening bar is the 09:15–09:16 IST bar, and the earliest theoretical BUY is after that bar confirms, normally at 09:16 IST—not blindly at 09:15:00.

The dated debug-reference inputs are telemetry reference bounds, not live entry gates. No date/session condition was changed.

## Invariance evidence

Focused structural tests compare the revised source to the pinned v1.3.4.3a SHA and require byte-identical regions for:

- live qualification, confirmed-bar entry, cooldown, trade limit, sizing, ₹500 risk, exposure, Stop A, and 1.5R target;
- STOP, TARGET, Supertrend, and EOD exits;
- BUY and SELL alert construction;
- v1.3.4.2 intrabar-persistent fire-control/dedupe blocks;
- FIRST / LIVE_OVERLAP / REENTRY telemetry and A/B/C/D research payloads.

The source-diff test permits exactly four changed line pairs and identifies the only non-metadata pair as `entryStartMinute` 20 → 15.

## Validation

Focused command:

```bash
pytest -q tests/test_ng_scalp_v1_3_4_4_0915_entry_start.py \
  tests/test_ng_scalp_v1_3_4_3a_overlap_telemetry_fix.py \
  tests/test_ng_scalp_v1_3_4_3_overlap_telemetry.py \
  tests/test_ng_scalp_v1_3_4_2_alert_fire_control.py
```

Result: **48 passed**.

TradingView compilation has been completed successfully. Runtime validation should still use fresh alerts from this version on a 1-minute NSE chart: verify no alert fires during the unconfirmed 09:15 bar, the first possible BUY is after that bar closes, the last entry boundary remains 14:50, the force exit remains 15:15, and BUY/SELL plus shadow payloads match v1.3.4.3a.

## TradingView validation status

The user confirmed this v1.3.4.4 Pine version compiled successfully in TradingView before requesting the commit. That external validation establishes TradingView compiler acceptance; the repository tests below establish the recorded source delta and invariants.

No commit or push was made.

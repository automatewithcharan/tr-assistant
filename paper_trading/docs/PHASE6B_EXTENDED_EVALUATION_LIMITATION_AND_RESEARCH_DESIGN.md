# Phase 6B — Extended evaluation limitation and entry/exit research design (sanitized)

## Decision: no extended replay was run

The requested minimum of 20 defensible evaluation sessions cannot be frozen from the currently verified local evidence. Accordingly, no Phase 6B profitability replay, strategy change, parameter adjustment, market-data request, broker action, or production operation occurred.

## Frozen-cohort feasibility check

The Phase 6A cohort is 78 `CURRENT_EXACT` symbols from the fixed 2026-04-22 snapshot, selected before outcome review for complete data. It has ten valid July warm-up sessions (July 13–24) and only five valid pre-CAS evaluation sessions (July 27–31). The CAS transition is effective August 3.

Post-transition, local evidence verifies dated CAS eligibility for only two symbols. There are 9,918 CAS-consistent but unverified stock-sessions across 210 symbols, representing 148,770 apparent tail cells. A 15:14 final continuous candle cannot be called complete merely from its shape: verified CAS expects 360 continuous minutes, verified non-CAS expects 375, and unknown/inferred classifications fail closed. The documented classification-independent repair floor also retains 2,335 unresolved gap sessions and 34,464 cells.

Therefore a 20-session post-July cohort would either silently accept unverified CAS sessions, exclude a performance-dependent set of names/dates, or mix incompatible session calendars. Each violates the Phase 6B prerequisite. The five Phase 6A dates remain the only frozen, fully defensible evaluation block available under the current evidence standard.

Remaining biases even after repair would include the April fixed-snapshot membership bias, current-token identity continuity risk, data-completeness selection, a limited NIFTY-100 engineering universe rather than all historical NSE EQ, and absent TradingView realtime alert parity.

## Required evidence before a future 20-session evaluation

1. Obtain separately authorized, dated NSE cash-security master evidence for August 3 onward and classify every stock/date as verified CAS, verified non-CAS, or unavailable.
2. Repair only the predeclared classification-independent missing windows; preserve unresolved identities and censored positions.
3. Freeze a point-in-time universe and a causally known liquidity eligibility rule before inspecting outcomes.
4. Lock at least ten prior valid sessions for RVOL warm-up and 20 consecutive, calendar-valid evaluation sessions. Hash all inputs, use the repaired benchmark, and use only `FROZEN_ORIGINAL` closed-bar/next-open proxy execution.
5. Report all data exclusions, unresolved CAS classifications, rejected candidates, unfilled attempts, and incomplete positions separately. Do not use the known defective broker-realistic sensitivity.

## Future research designs — not implemented

### Leader-first selection study

Pre-register a daily universe before the first eligible intraday signal. At each completed minute, rank only observations then known: intraday relative strength versus NIFTY, same-minute RVOL, causal return/momentum horizon, price above VWAP/confirmed EMA, and liquidity/turnover. Freeze a ranking formula, tie-break, maximum names, rebalance cadence, and missing-data policy before outcome review. Compare the frozen ranker with equal data access and independent portfolios, using chronological holdout sessions. This is a selection-policy study, not a modification of A/B/C.

### Stop/profit-management sensitivity study

Keep entry candidates fixed and pre-register separately labelled exit policies. Use conservative one-minute intrabar ordering: old stop is evaluated before any same-bar ratchet; gap opens fill adversely; a high/low path cannot support both favorable ratcheting and a later favorable stop unless ordering evidence exists. Report a bracket of pessimistic/optimistic path assumptions where outcomes are indeterminate, retain all censored positions, and never present a sensitivity as proof of a superior executable strategy.

## TradingView parity evidence required

For a representative, timestamped sample of candidate and non-candidate bars, obtain exported 1-minute OHLCV, UT trail/cross and leg age, PSAR, session VWAP, stop ATR/ref-stop, prior completed 5-minute EMA20 and predecessor, DMI/ADX, QQC components, CHOP, RVOL history/sample count, NIFTY-aligned RS, ORH/hold count, and Pine realtime alert/virtual-position timestamps. Include chart symbol, exchange timezone, indicator version/input settings, and whether values were completed-bar or realtime. These fields are the minimum to isolate formula mismatch from irreducible `varip`, tick ordering, alert timing, and fill-model differences.

## Conclusion

Phase 6A’s negative result remains exploratory and cannot yet be tested over 20 defensible sessions. The correct next action is evidence acquisition/validation authorization—not selecting a shorter convenient period or changing trading thresholds.

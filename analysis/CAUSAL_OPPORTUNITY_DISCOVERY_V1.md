# Causal Opportunity Discovery v1

## Why this exists

PSAR Liquidity V2 and the audited NGScalp v1.3.4.1 cohort did not demonstrate deployable positive expectancy. This branch therefore does **not** tune either live strategy. It changes the research question from "which indicator setting rescues the strategy?" to:

> Which information available at a completed 1-minute bar is associated with a favorable intraday path after a realistically delayed entry?

The output is research evidence only. No Pine or live alert behavior is changed by this work.

## Frozen evidence boundary

The split is hard-coded and fingerprinted:

| Split | Sessions |
|---|---|
| Train | 2026-07-06 through 2026-08-25 |
| Validation | 2026-08-26 through 2026-09-10 |
| Untouched holdout | 2026-09-11 through 2026-09-30 |

Discovery mode loads only train + validation. It asserts that no holdout row enters discovery. Holdout mode is separate, requires `--unlock-holdout`, requires the exact frozen manifest/config fingerprints, marks the manifest `holdout_used=true`, and refuses a second peek.

## Causal contracts

1. Features at timestamp `T` use only bars completed at or before `T`.
2. The simulated entry is the **next 1-minute bar open**, not the signal close.
3. The standardized risk unit is `1 x ATR(14)` by default. This is a research normalization, not a proposed broker stop.
4. Future outcomes are labels only. They never feed feature construction.
5. If target and stop are touched in the same 1-minute bar, the label assumes **stop first**.
6. Same-time cross-sectional ranks use only candidates that exist at that exact signal timestamp.
7. Model score cutoffs are derived from **train prediction quantiles** and then applied unchanged to validation. Validation is not re-ranked using the full validation day.
8. Candidate execution is chronological with fixed caps and symbol cooldown. No future candidate can change an earlier selection.

## Opportunity population

The research intentionally starts broader than PSAR or Supertrend. Every five minutes a baseline candidate is available, and structurally interesting bars are also tagged:

- 5-minute opening-range breakout
- 10-minute opening-range breakout
- 15-minute opening-range breakout
- first 15-minute opening-range retest/hold
- prior-20-bar breakout
- VWAP reclaim
- VWAP pullback/hold
- compression to expansion
- failed-breakdown reclaim

The event flags are features. They are not treated as proven edges.

A fixed data-quality/liquidity sanity gate requires, by default, price >= ₹20 and the **prior** 20-bar median one-minute turnover >= ₹2.5 lakh. These values are not optimized inside this script.

## Feature families

Only stock-local features are required, so the first pass is not blocked by incomplete NIFTY/sector history. Features include:

- 1/3/5/10/20-bar ATR-normalized returns
- VWAP distance and 5-bar VWAP slope
- distance to session high/low and pullback from high
- candle range, close position, body and wicks
- 5-bar and 20-bar relative volume
- volume acceleration
- same-time-of-day relative volume using **prior sessions only**
- current and prior rolling turnover
- 5/10-bar compression
- prior-20-bar breakout distance and recent breakout count
- 5/10/15-minute opening-range distance
- event-family flags
- time-local cross-sectional percentile ranks

NIFTY-relative strength and sector-relative strength are intentionally absent from v1 because benchmark coverage is incomplete. They can become a later, separately versioned feature set after the benchmark dataset is complete.

## Labels

For each candidate, the harness stores:

- 5/15/30-minute MFE in R
- 5/15/30-minute MAE in R
- 5/15/30-minute close outcome in R
- 0.5R / 1R / 1.5R / 2R / 3R target-before-1R-stop labels
- conservative path result and bars-to-target-or-stop
- base-cost and stressed-cost strategy R
- one-extra-bar delayed-entry stress result

The first model target is `+1.5R before -1R`, but validation acceptance also requires positive realized R after modeled costs and after a one-extra-bar entry delay.

## Discovery model

The first ranker is deliberately simple:

- median imputation
- standard scaling
- L2 logistic regression
- balanced classes

This is a ranker/veto layer, not an unrestricted trade generator.

Train-derived score cutoffs are generated for the top 1%, 2%, 5%, 10%, and 20% of train predictions. Validation receives the fixed numeric thresholds. The selected threshold is the **broadest** bucket that still passes all gates, reducing preference for tiny lucky tails.

Default validation gates:

- at least 30 selected validation trades
- positive base-cost net expectancy
- positive stressed-cost net expectancy
- positive one-extra-bar-delay net expectancy

The report also records best-trade removal, symbol concentration, positive-session rate, and worst/best session R.

## Running the research locally

The raw minute cache is intentionally ignored by Git and remains under `results/Data` on the research machine. From the repository root:

```bash
python scripts/causal_opportunity_discovery.py \
  --mode discovery \
  --data-root results/Data/kite_history_backfill/minute \
  --output-dir results/Data/causal_opportunity_v1
```

For a fast plumbing check before the full 3,316-symbol run:

```bash
python scripts/causal_opportunity_discovery.py \
  --mode discovery \
  --symbol-limit 50 \
  --output-dir results/Data/causal_opportunity_v1_smoke
```

A smoke run must never be used to choose a production strategy.

If and only if discovery freezes a candidate and the decision is explicitly made to consume the untouched holdout once:

```bash
python scripts/causal_opportunity_discovery.py \
  --mode holdout \
  --data-root results/Data/kite_history_backfill/minute \
  --output-dir results/Data/causal_opportunity_v1 \
  --manifest results/Data/causal_opportunity_v1/FROZEN_CANDIDATE_MANIFEST.json \
  --unlock-holdout
```

Do not run that command during discovery.

## Promotion ladder

A positive validation or holdout result is not permission to trade normal capital. The required ladder remains:

`train discovery -> frozen validation -> one-shot holdout -> forward paper -> tiny live -> execution/slippage validation -> gradual scaling`

If no score bucket survives validation costs and delay, the correct result is to reject this 1-minute long-only opportunity model, not to tune the validation set until it turns green.

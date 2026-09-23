# Discord Phase 2 shadow review priority

Phase 2 adds a provisional A/B/C ordering to direct `early_breakout` and
`top_gainer_radar` Discord candidates. It answers only “which chart should I
inspect first?” It is not a BUY instruction, confidence estimate, or validated
profitability model.

## Frozen behavior

Phase 2 does not participate in detector qualification, candidate ordering,
caps, daily limits, dedupe, retries, or HTTP eligibility. It is calculated at
the existing Discord-attempt boundary after the candidate has already qualified.
Scoring and persistence exceptions are swallowed and delivery continues.

Phase 1 timestamps and the durable delivery journal remain authoritative. No
Pine file or experiment semantics are changed.

## Provisional score

The score is the sum of named integer components. Missing measurements receive
no invented value; seven or more missing ranking features receive a transparent
`missing_data = -1` component.

| Component | Positive | Negative |
|---|---|---|
| Liquidity | +2 at ≥₹5 Cr; +1 at ≥₹1 Cr | -1 below ₹1 Cr; -2 warning |
| RVOL | +2 at ≥2.5x; +1 at ≥1.5x | -1 below 1x; -2 warning |
| Buyer/seller | +1 at ≥2x | -1 below 1x |
| Explicit stock-vs-NIFTY RS | +1 at ≥+0.5% | -1 at ≤-0.5% |
| Benchmark/sector context | +1 when positive, each | -1 when negative, each |
| HOD pullback | +1 from 0–0.75% | -1 above 1.5% |
| VWAP extension | +1 from 0–1% | -1 below VWAP; -2 above 2% |
| Signal strength | +1 at ≥55 | -1 below 40 |
| First seen→qualified | +1 at ≤15s | -1 above 60s |
| Tick age | +1 at ≤3s | -2 above 15s |
| Price consumed before Discord | — | -1 above 0.5%; -2 above 1% |
| MULTI-LANE | +2 | — |
| Bearish upstream warning | — | -2 |

Priority bands are provisional: **A ≥ 6**, **B = 2–5**, **C < 2**. They must be
frozen during a forward evaluation period. Do not tune them on the session used
to judge them.

`BenchmarkStrength` is retained separately from true stock-minus-NIFTY RS. It
does not masquerade as the latter. `nifty_relative_strength_pct` remains null
unless an explicit causal upstream field exists.

## Record schema

One first-attempt record is appended to:

`<output_dir>/research/discord_shadow_ranking/YYYY-MM-DD.jsonl`

Schema: `discord-shadow-ranking-v2`.

Each record contains identity/date/lane, priority and named components, all
available ranking features, warnings and missing fields, Phase 1 causal times
and prices, stage latencies, stage price drifts, and the largest latency/drift
stage. Persistence occurs after the HTTP outcome, so a successful first attempt
also records Discord success time and attempt→success latency without adding
pre-send latency. The existing delivery journal remains authoritative for all
Discord success/failure,
attempt→success latency, caps, retry, and dedupe evidence. Neither journal is
overwritten.

## Offline evaluation

The evaluator is read-only with respect to source evidence:

```bash
PYTHONPATH=. .venv/bin/python scripts/evaluate_discord_shadow_ranking.py \
  --records results/Data/research/discord_shadow_ranking \
  --bars 'results/Data/intraday_event_shadow/bars_*.csv' \
  --pine-alerts tradingview-alerts.csv \
  --output results/forensics/discord-shadow-phase2
```

It attaches post-attempt 5/15/30/60-minute and EOD MFE/MAE, time to +0.5%, and
optional later Pine BUY conversion/latency/drift. Outcomes are joined only
offline and can never feed the live priority calculation.

Reports compare A/B/C and segment by lane, MULTI-LANE, time of day, liquidity,
RVOL, NIFTY RS, sector RS, and VWAP extension. MFE is excursion, not executable
profit, and OHLC does not establish favorable/adverse intrabar ordering.

## Known uncertainties

- The score is an unvalidated hypothesis with deliberately simple rules.
- Several upstream rows expose market breadth but not true stock-minus-NIFTY
  relative strength; that feature will often remain missing.
- VWAP distance in ATR is retained only when already supplied. It is not derived
  from unavailable ATR data.
- Spread and tick age depend on live tick/depth availability.
- A first-lane alert cannot know that the same symbol will qualify in a second
  lane later. Existing behavior is retained: the later alert is marked
  `MULTI-LANE`; neither is suppressed.

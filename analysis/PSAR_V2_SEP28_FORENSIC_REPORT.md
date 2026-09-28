# PSAR V2 Sep 28 Forensic Report

Generated only from the Sep 28 TradingView CSV and the corresponding Pine source. No candle, broker, or future market data was used.

## 1. Executive summary

The export contains **557 alerts across 46 symbols**: 154 BUY, 159 EXIT, 244 SKIP, and 0 other. Reconstruction yields **154 closed trades**, 5 orphan EXITs, and 0 open BUYs.

Closed trades produced **₹-21,147.14 theoretical gross P&L**, with 48 wins, 106 losses, 0 flats, a 31.2% win rate, ₹-137.32 average trade, and 0.41 profit factor. This is a one-session theoretical alert result, not fill-verified deployable P&L.

The primary performance result excludes orphan lifecycles because their entry timestamps and initial stops are absent. Their EXIT payloads sum to ₹154.97; the sum across all 159 EXIT payloads is ₹-20,992.17. Both totals are reported so the exclusion is explicit.

First entries contributed ₹-4,781.75 over 27 trades; re-entries contributed ₹-16,365.39 over 127 trades. Re-entries added most of the day's loss because there were 127 of them, but their per-trade loss (₹-128.86) was smaller than first entries (₹-177.10) and profit factor was the same. Thus 're-entry is worse' is not proven; the second and third entries were the weakest cohorts, while fourth+ was less negative.

The worst realized-loss/intended-risk ratio was **9.00×** (20260928-STYLEBAAZA-03: intended ₹250.00, realized ₹-2,250.00). The code explains overshoot: realtime stops trigger on the observed `close <= activeStop`, then record current close minus one tick—not a fill at the stop level.

Why activity was so high is mechanically clear: there is no one-entry-per-day guard, so every later confirmed bullish PSAR flip can re-enter after an exit. Twenty-four symbols re-entered and 127 of 154 trades were re-entries. The day had 107 exits inside five minutes. Targets and ATR trails created positive logged P&L, but HARD STOP and PSAR FALLBACK exits destroyed more; raw PSAR telemetry is insufficient to assign the entire loss to entry versus management.

## 2. Data integrity and source of truth

| Item | Value |
|---|---|
| Fetched main SHA | b6202324b79f5b65440a0277af99b917824ec799 |
| CSV source | origin/main:results/TradingView_Alerts_Log_2026-09-28.csv |
| Pine source | origin/main:PSAR_Liquidity_Trade_Manager_V2_INDICATOR.pine |
| CSV SHA-256 | ac62841dbfe43f300301e6dfdc0ad8a17c6268e1d58b2f74713c1cc792f82880 |
| Pine SHA-256 | a00ec8efe06eaecf205a59e265db35172e6a871793ae871012cc034d0f6878d5 |
| Raw rows | 557 |
| Time range UTC | 2026-09-28T04:10:05+00:00 to 2026-09-28T09:45:00+00:00 |
| Time range IST | 2026-09-28T09:40:05+05:30 to 2026-09-28T15:15:00+05:30 |
| Exact duplicate rows | 0 |
| Exact timestamp + message duplicates | 0 |
| Logical duplicates | 0 |
| Same symbol/event/second collisions | 3 |
| Distinct TradingView alert definitions | 47 |

Repository state observed at investigation start:

```text
branch: codex/ng-v0818-f0-observability-20260916
working-tree HEAD: 3723774abbf16a64acb1e95f9a74c0949d0f357d
fetched origin/main: b6202324b79f5b65440a0277af99b917824ec799
status: dirty with pre-existing modified and untracked files; preserved
```

`origin/main` is the evidence source: `b620232` adds the Sep 28 CSV; `7ac3281` is the latest commit touching the V2 Pine file. The alert payloads use `Assumed entry`, include exposure/intended/required-capacity telemetry, and include current-session bars on SKIPs—the schema introduced by `7ac3281`, not the original `ce85b7e` version.

All rows identify `PSAR_LIQ_V2` and share that hardened payload schema. They are the same Pine code version, but **not the same input configuration**:

| Liquidity lookback | Alerts |
|---|---|
| 10 | 17 |
| 15 | 355 |
| 20 | 185 |

Observed field schemas: 3 (the expected BUY, EXIT and SKIP shapes; no payload-version field or explicit source SHA exists). `Alert ID` identifies a TradingView alert definition and repeats across its events; it is not an event ID. `Webhook status` is blank on all 557 rows. Same-code attribution is therefore strong schema evidence, not cryptographic proof from the CSV itself.

## 3. Pine state machine

| Stage | Executable rule | Pine location |
|---|---|---|
| Session | 1-minute chart; confirmed PSAR BUY bar close from 09:15 through 14:50 IST | 131–145, 297–311 |
| Liquidity baseline | Only prior completed current-session bars; UNKNOWN until lookback 10/15/20 is ready | 147–165 |
| Liquidity | PASS requires turnover, volume, capacity and optional liquidity RVOL; FAIL/UNKNOWN SKIP | 197–224, 303–383 |
| PSAR entry | Confirmed bearish→bullish flip; flat slot; valid sizing; optional filters | 40–45, 167–171, 306–313 |
| Sizing | floor(₹500/risk-share), exposure cap, 500-share cap; minimum of all | 208–213 |
| Initial stop | min(signal low − 0.1 ATR, entry − 0.5 ATR), rounded down | 297–303 |
| Target | entry + 2R, rounded up | 320–322 |
| Break-even | Disabled by these inputs | 83–85, 230–235 |
| Profit lock | At 1R high, candidate entry + 0.25R | 87–89, 236–240 |
| ATR trail | At 1R high, candidate highest − 1.5 ATR; stop only ratchets upward | 91–99, 242–248, 411–418 |
| Realtime stop | After entry bar only; current observed close ≤ active stop; exit at close − one tick | 401–433 |
| Target/PSAR/EOD | Priority after stop: target, EOD, confirmed PSAR fallback | 423–446 |
| Reset/re-entry | Exit sets flat immediately; same-bar re-entry blocked by exit latch; later confirmed flip may re-enter | 287–292, 308, 487–513 |

There is no once-per-symbol/day guard. Multiple BUYs are allowed after an EXIT, but not on the same chart bar as that EXIT. Because entry requires a new confirmed bearish→bullish PSAR flip, a later bar can re-enter. State is `varip`: it survives realtime rollback, but script/chart reload reconstructs historical state from OHLC. Historical BUY alert calls do not deliver old alerts; a historically reconstructed open state can later emit a realtime EXIT with no realtime BUY in this export.

`isNewTradingDay` resets only the liquidity-session baseline (155–159). There is no explicit new-day reset of `inTradeIp`; trade state resets only when an exit path executes. A confirmed bar at/after 15:15 normally triggers EOD before PSAR fallback (424, 439–446), but a chart with no qualifying realtime EOD execution could retain state until a later execution. The CSV does not prove that this occurred.

Realtime stops are intrabar in the sense that Pine recalculates on feed updates and compares the current `close`, but this indicator does not place a resting stop order. A jump through the level records the newly observed price minus one tick. Historical processing differs: it tests the bar low against the stop carried into the bar and uses open-or-stop minus one tick (253–255, 448–485). The CSV contains live alerts, so the realtime path governs the logged exits.

## 4. Trade reconstruction

| Classification | Count |
|---|---|
| Normal closed entry episodes | 154 |
| Symbols with re-entry | 24 |
| Re-entry trades (entry #2+) | 127 |
| Orphan EXIT | 5 |
| Open/unmatched BUY | 0 |
| Overlapping BUY anomalies | 0 |
| Logical duplicate alerts | 0 |
| Possible duplicate candidate alerts | 3 |
| BUY→EXIT <30 sec | 23 |
| BUY→EXIT <1 min | 41 |
| BUY→EXIT <5 min | 107 |
| BUY→EXIT in same 5m clock bucket | 81 |

Orphan EXITs:

| Symbol | Time IST | Reason | Entry carried in EXIT | P&L |
|---|---|---|---|---|
| SYNCOMF | 09:47:29 | PROFIT LOCK | ₹22.63 | ₹25.00 |
| UTTAMSUGAR | 09:59:05 | PSAR FALLBACK | ₹302.00 | ₹-407.70 |
| AEQUS | 10:02:40 | TARGET | ₹252.00 | ₹997.92 |
| KPIGREEN | 10:47:00 | PSAR FALLBACK | ₹365.75 | ₹-380.25 |
| STLNETWORK | 11:08:00 | PSAR FALLBACK | ₹53.09 | ₹-80.00 |

These are not automatically defects. The export begins at 09:40:05 IST, not session open, and `varip`/historical reconstruction can legitimately carry an in-trade state into the realtime alert window. The CSV has no alert-creation timestamp, reload marker, or prior-day data, so the exact cause cannot be distinguished.

Possible duplicate candidate alerts:

| Symbol | Time IST | Event | Alert definitions | Lookbacks | Assessment |
|---|---|---|---|---|---|
| ARVEE | 10:33:36 | SKIP_FAIL | 5713565435, 5713555421 | 10, 20 | Same PSAR candidate from parallel 10/20-bar configurations; payload measurements differ |
| ARVEE | 10:55:12 | SKIP_FAIL | 5713565435, 5713555421 | 10, 20 | Same PSAR candidate from parallel 10/20-bar configurations; payload measurements differ |
| ARVEE | 12:06:06 | SKIP_FAIL | 5713565435, 5713555421 | 10, 20 | Same PSAR candidate from parallel 10/20-bar configurations; payload measurements differ |

These three ARVEE pairs are not byte-identical duplicates: two concurrently active TradingView alert definitions evaluated the same PSAR event with different liquidity baselines. Both concluded FAIL, so no duplicate trade resulted. They are operational duplicates if the intended deployment was one alert definition per symbol.

## 5. Performance

| Metric | Value |
|---|---|
| Closed trades | 154 |
| Winning / losing / flat | 48 / 106 / 0 |
| Win rate | 31.2% |
| Gross P&L | ₹-21,147.14 |
| Average / median trade | ₹-137.32 / ₹-97.50 |
| Average winner / loser | ₹309.60 / ₹-339.70 |
| Profit factor | 0.41 |
| Expectancy/trade | ₹-137.32 |
| Expectancy in R | -0.47 |
| Best trade | 20260928-CLEANMAX-09 ₹1,011.50 (2.02R) |
| Worst trade | 20260928-STYLEBAAZA-03 ₹-2,250.00 (-9.00R) |
| Max consecutive wins / losses | 3 / 9 |
| Logged-vs-computed P&L mismatches > ₹0.01 | 0 |

Intraday theoretical cumulative equity by exit:

| Exit IST | Trade | P&L | Cumulative |
|---|---|---|---|
| 09:40:20 | 20260928-SHRINGARMS-01 | ₹-425.00 | ₹-425.00 |
| 09:43:10 | 20260928-MUKANDLTD-01 | ₹5.00 | ₹-420.00 |
| 09:47:40 | 20260928-SUNFLAG-01 | ₹124.20 | ₹-295.80 |
| 09:50:46 | 20260928-AASTHA-01 | ₹100.00 | ₹-195.80 |
| 09:51:32 | 20260928-SSDL-01 | ₹-310.00 | ₹-505.80 |
| 09:52:01 | 20260928-SSRETAIL-01 | ₹473.85 | ₹-31.95 |
| 10:01:30 | 20260928-TURTLEMINT-01 | ₹90.00 | ₹58.05 |
| 10:01:38 | 20260928-CLEANMAX-01 | ₹-553.00 | ₹-494.95 |
| 10:02:06 | 20260928-ZYDUSLIFE1!-01 | ₹-658.80 | ₹-1,153.75 |
| 10:02:27 | 20260928-POLICYBZR-01 | ₹-672.70 | ₹-1,826.45 |
| 10:02:31 | 20260928-SBC-01 | ₹-195.00 | ₹-2,021.45 |
| 10:04:57 | 20260928-SHRINGARMS-02 | ₹-485.00 | ₹-2,506.45 |
| 10:05:07 | 20260928-SMCGLOBAL-01 | ₹840.00 | ₹-1,666.45 |
| 10:09:44 | 20260928-TFCILTD-01 | ₹-200.00 | ₹-1,866.45 |
| 10:17:36 | 20260928-POLICYBZR-02 | ₹-863.20 | ₹-2,729.65 |
| 10:17:45 | 20260928-SYNCOMF-01 | ₹-155.00 | ₹-2,884.65 |
| 10:18:00 | 20260928-CLEANMAX-02 | ₹-538.20 | ₹-3,422.85 |
| 10:20:09 | 20260928-SHANTIGOLD-01 | ₹-606.90 | ₹-4,029.75 |
| 10:28:50 | 20260928-ZYDUSLIFE1!-02 | ₹269.50 | ₹-3,760.25 |
| 10:29:04 | 20260928-SEDEMAC-01 | ₹-387.60 | ₹-4,147.85 |
| 10:30:27 | 20260928-SMCGLOBAL-02 | ₹999.44 | ₹-3,148.41 |
| 10:30:27 | 20260928-POLICYBZR-03 | ₹-625.00 | ₹-3,773.41 |
| 10:32:30 | 20260928-TURTLEMINT-02 | ₹5.00 | ₹-3,768.41 |
| 10:34:37 | 20260928-SSRETAIL-02 | ₹-668.30 | ₹-4,436.71 |
| 10:35:08 | 20260928-SBC-02 | ₹70.00 | ₹-4,366.71 |
| 10:37:22 | 20260928-DAMCAPITAL-01 | ₹570.00 | ₹-3,796.71 |
| 10:40:45 | 20260928-AEQUS-01 | ₹-575.00 | ₹-4,371.71 |
| 10:41:01 | 20260928-CLEANMAX-03 | ₹-396.00 | ₹-4,767.71 |
| 10:42:36 | 20260928-SUNFLAG-02 | ₹-631.20 | ₹-5,398.91 |
| 10:46:23 | 20260928-CLEANMAX-04 | ₹-537.50 | ₹-5,936.41 |
| 10:48:05 | 20260928-SYNCOMF-02 | ₹-55.00 | ₹-5,991.41 |
| 10:49:03 | 20260928-TFCILTD-02 | ₹-300.00 | ₹-6,291.41 |
| 10:49:21 | 20260928-LLOYDSENGG-01 | ₹-100.00 | ₹-6,391.41 |
| 10:53:00 | 20260928-SSRETAIL-03 | ₹-369.20 | ₹-6,760.61 |
| 10:54:29 | 20260928-DAMCAPITAL-02 | ₹-510.00 | ₹-7,270.61 |
| 10:56:16 | 20260928-GESHIP-01 | ₹998.40 | ₹-6,272.21 |
| 10:57:29 | 20260928-SMCGLOBAL-03 | ₹-380.00 | ₹-6,652.21 |
| 10:58:07 | 20260928-STYLEBAAZA-01 | ₹-1,695.75 | ₹-8,347.96 |
| 10:58:15 | 20260928-TFCILTD-03 | ₹-285.00 | ₹-8,632.96 |
| 10:58:27 | 20260928-SHANTIGOLD-02 | ₹400.00 | ₹-8,232.96 |
| 10:59:30 | 20260928-ZYDUSLIFE1!-03 | ₹245.00 | ₹-7,987.96 |
| 11:03:19 | 20260928-AEQUS-02 | ₹-320.00 | ₹-8,307.96 |
| 11:04:09 | 20260928-SYNCOMF-03 | ₹-35.00 | ₹-8,342.96 |
| 11:04:10 | 20260928-SBC-03 | ₹-120.00 | ₹-8,462.96 |
| 11:04:45 | 20260928-CLEANMAX-05 | ₹998.40 | ₹-7,464.56 |
| 11:05:40 | 20260928-KPIGREEN-01 | ₹-570.00 | ₹-8,034.56 |
| 11:06:32 | 20260928-POLICYBZR-04 | ₹-612.00 | ₹-8,646.56 |
| 11:08:14 | 20260928-REMSONSIND-01 | ₹-245.00 | ₹-8,891.56 |
| 11:12:24 | 20260928-STYLEBAAZA-02 | ₹-25.00 | ₹-8,916.56 |
| 11:14:27 | 20260928-SSRETAIL-04 | ₹71.40 | ₹-8,845.16 |
| 11:15:33 | 20260928-GESHIP-02 | ₹-562.60 | ₹-9,407.76 |
| 11:18:21 | 20260928-TFCILTD-04 | ₹15.00 | ₹-9,392.76 |
| 11:23:33 | 20260928-ZYDUSLIFE1!-04 | ₹245.00 | ₹-9,147.76 |
| 11:24:27 | 20260928-POLICYBZR-05 | ₹51.00 | ₹-9,096.76 |
| 11:27:15 | 20260928-TURTLEMINT-03 | ₹-75.00 | ₹-9,171.76 |
| 11:27:17 | 20260928-SYNCOMF-04 | ₹-50.00 | ₹-9,221.76 |
| 11:27:36 | 20260928-SBC-04 | ₹40.00 | ₹-9,181.76 |
| 11:28:24 | 20260928-TFCILTD-05 | ₹-100.00 | ₹-9,281.76 |
| 11:28:35 | 20260928-CLEANMAX-06 | ₹885.60 | ₹-8,396.16 |
| 11:29:06 | 20260928-DAMCAPITAL-03 | ₹-505.31 | ₹-8,901.47 |
| 11:30:21 | 20260928-GESHIP-03 | ₹58.50 | ₹-8,842.97 |
| 11:34:15 | 20260928-AEQUS-03 | ₹221.85 | ₹-8,621.12 |
| 11:35:28 | 20260928-STLNETWORK-01 | ₹60.00 | ₹-8,561.12 |
| 11:36:13 | 20260928-REMSONSIND-02 | ₹-159.80 | ₹-8,720.92 |
| 11:41:15 | 20260928-KPIGREEN-02 | ₹-575.00 | ₹-9,295.92 |
| 11:42:59 | 20260928-SMCGLOBAL-04 | ₹-105.00 | ₹-9,400.92 |
| 11:46:07 | 20260928-SSRETAIL-05 | ₹1,000.80 | ₹-8,400.12 |
| 11:49:01 | 20260928-LLOYDSENGG-02 | ₹-75.00 | ₹-8,475.12 |
| 11:49:41 | 20260928-STYLEBAAZA-03 | ₹-2,250.00 | ₹-10,725.12 |
| 11:51:01 | 20260928-SYNCOMF-05 | ₹-75.00 | ₹-10,800.12 |
| 11:51:52 | 20260928-POLICYBZR-06 | ₹-47.60 | ₹-10,847.72 |
| 11:54:29 | 20260928-SMCGLOBAL-05 | ₹-190.00 | ₹-11,037.72 |
| 11:56:08 | 20260928-CLEANMAX-07 | ₹-595.20 | ₹-11,632.92 |
| 11:56:34 | 20260928-DAMCAPITAL-04 | ₹-506.97 | ₹-12,139.89 |
| 11:57:38 | 20260928-SBC-05 | ₹-55.00 | ₹-12,194.89 |
| 12:00:26 | 20260928-STYLEBAAZA-04 | ₹-675.00 | ₹-12,869.89 |
| 12:01:17 | 20260928-SSRETAIL-06 | ₹997.10 | ₹-11,872.79 |
| 12:02:49 | 20260928-ZYDUSLIFE1!-05 | ₹183.75 | ₹-11,689.04 |
| 12:03:15 | 20260928-BHARATCOAL-01 | ₹-5.00 | ₹-11,694.04 |
| 12:04:56 | 20260928-STLNETWORK-02 | ₹-65.00 | ₹-11,759.04 |
| 12:07:30 | 20260928-KPIGREEN-03 | ₹-536.50 | ₹-12,295.54 |
| 12:09:04 | 20260928-TFCILTD-06 | ₹-85.00 | ₹-12,380.54 |
| 12:09:08 | 20260928-LLOYDSENGG-03 | ₹305.00 | ₹-12,075.54 |
| 12:10:20 | 20260928-SYNCOMF-06 | ₹-25.00 | ₹-12,100.54 |
| 12:12:12 | 20260928-DAMCAPITAL-05 | ₹-495.00 | ₹-12,595.54 |
| 12:12:36 | 20260928-CLEANMAX-08 | ₹-712.80 | ₹-13,308.34 |
| 12:14:25 | 20260928-AEQUS-04 | ₹35.00 | ₹-13,273.34 |
| 12:17:14 | 20260928-KPIGREEN-04 | ₹-300.00 | ₹-13,573.34 |
| 12:18:42 | 20260928-SSRETAIL-07 | ₹-621.15 | ₹-14,194.49 |
| 12:23:12 | 20260928-POLICYBZR-07 | ₹34.40 | ₹-14,160.09 |
| 12:24:45 | 20260928-SYNCOMF-07 | ₹25.00 | ₹-14,135.09 |
| 12:26:06 | 20260928-SMCGLOBAL-06 | ₹-200.00 | ₹-14,335.09 |
| 12:26:40 | 20260928-ZYDUSLIFE1!-06 | ₹-526.75 | ₹-14,861.84 |
| 12:27:43 | 20260928-SBC-06 | ₹-35.00 | ₹-14,896.84 |
| 12:28:00 | 20260928-GESHIP-04 | ₹-138.60 | ₹-15,035.44 |
| 12:31:33 | 20260928-LLOYDSENGG-04 | ₹-100.00 | ₹-15,135.44 |
| 12:36:32 | 20260928-STLNETWORK-03 | ₹-95.00 | ₹-15,230.44 |
| 12:44:54 | 20260928-TFCILTD-07 | ₹-120.00 | ₹-15,350.44 |
| 12:46:34 | 20260928-AEQUS-05 | ₹-673.40 | ₹-16,023.84 |
| 12:51:55 | 20260928-GESHIP-05 | ₹-390.00 | ₹-16,413.84 |
| 12:51:56 | 20260928-DAMCAPITAL-06 | ₹370.00 | ₹-16,043.84 |
| 12:58:20 | 20260928-SMCGLOBAL-07 | ₹-115.00 | ₹-16,158.84 |
| 13:00:52 | 20260928-POLICYBZR-08 | ₹-544.80 | ₹-16,703.64 |
| 13:03:34 | 20260928-KPIGREEN-05 | ₹-375.00 | ₹-17,078.64 |
| 13:09:02 | 20260928-DAMCAPITAL-07 | ₹-200.00 | ₹-17,278.64 |
| 13:11:42 | 20260928-POLICYBZR-09 | ₹828.80 | ₹-16,449.84 |
| 13:14:00 | 20260928-SYNCOMF-08 | ₹-40.00 | ₹-16,489.84 |
| 13:16:58 | 20260928-AEQUS-06 | ₹20.00 | ₹-16,469.84 |
| 13:20:08 | 20260928-SSRETAIL-08 | ₹-516.00 | ₹-16,985.84 |
| 13:20:57 | 20260928-LLOYDSENGG-05 | ₹-65.00 | ₹-17,050.84 |
| 13:25:06 | 20260928-ZYDUSLIFE1!-07 | ₹-61.50 | ₹-17,112.34 |
| 13:27:07 | 20260928-SSRETAIL-09 | ₹-188.10 | ₹-17,300.44 |
| 13:29:55 | 20260928-AEQUS-07 | ₹-155.00 | ₹-17,455.44 |
| 13:31:14 | 20260928-SMCGLOBAL-08 | ₹-330.00 | ₹-17,785.44 |
| 13:41:13 | 20260928-CLEANMAX-09 | ₹1,011.50 | ₹-16,773.94 |
| 13:46:07 | 20260928-AEQUS-08 | ₹-165.00 | ₹-16,938.94 |
| 13:47:29 | 20260928-SMCGLOBAL-09 | ₹185.00 | ₹-16,753.94 |
| 13:48:11 | 20260928-SBC-07 | ₹30.00 | ₹-16,723.94 |
| 13:49:24 | 20260928-SYNCOMF-09 | ₹50.00 | ₹-16,673.94 |
| 13:50:04 | 20260928-DAMCAPITAL-08 | ₹-78.20 | ₹-16,752.14 |
| 13:51:05 | 20260928-TFCILTD-08 | ₹-210.00 | ₹-16,962.14 |
| 13:52:41 | 20260928-ONIXSOLAR-01 | ₹-648.45 | ₹-17,610.59 |
| 13:55:22 | 20260928-SSRETAIL-10 | ₹-29.40 | ₹-17,639.99 |
| 13:58:27 | 20260928-ZYDUSLIFE1!-08 | ₹-282.90 | ₹-17,922.89 |
| 14:04:01 | 20260928-POLICYBZR-10 | ₹234.00 | ₹-17,688.89 |
| 14:06:00 | 20260928-SHUKRAPHAR-01 | ₹-40.00 | ₹-17,728.89 |
| 14:09:10 | 20260928-BHARATCOAL-02 | ₹-20.00 | ₹-17,748.89 |
| 14:10:23 | 20260928-LLOYDSENGG-06 | ₹-60.00 | ₹-17,808.89 |
| 14:13:59 | 20260928-GESHIP-06 | ₹-117.00 | ₹-17,925.89 |
| 14:14:54 | 20260928-SYNCOMF-10 | ₹-45.00 | ₹-17,970.89 |
| 14:18:05 | 20260928-SMCGLOBAL-10 | ₹-90.00 | ₹-18,060.89 |
| 14:19:07 | 20260928-POLICYBZR-11 | ₹-575.00 | ₹-18,635.89 |
| 14:21:26 | 20260928-SHUKRAPHAR-02 | ₹40.00 | ₹-18,595.89 |
| 14:23:33 | 20260928-LLOYDSENGG-07 | ₹-20.00 | ₹-18,615.89 |
| 14:24:15 | 20260928-SEDEMAC-02 | ₹-495.60 | ₹-19,111.49 |
| 14:27:20 | 20260928-KPIGREEN-06 | ₹375.00 | ₹-18,736.49 |
| 14:27:42 | 20260928-ZYDUSLIFE1!-09 | ₹222.30 | ₹-18,514.19 |
| 14:29:04 | 20260928-AEQUS-09 | ₹-345.00 | ₹-18,859.19 |
| 14:29:09 | 20260928-SHANTIGOLD-03 | ₹-350.00 | ₹-19,209.19 |
| 14:33:29 | 20260928-SYNCOMF-11 | ₹-35.00 | ₹-19,244.19 |
| 14:34:28 | 20260928-SSRETAIL-11 | ₹448.50 | ₹-18,795.69 |
| 14:34:37 | 20260928-DAMCAPITAL-09 | ₹-504.70 | ₹-19,300.39 |
| 14:36:47 | 20260928-ONIXSOLAR-02 | ₹83.00 | ₹-19,217.39 |
| 14:38:33 | 20260928-TURTLEMINT-04 | ₹-180.00 | ₹-19,397.39 |
| 14:43:10 | 20260928-SBC-08 | ₹-95.00 | ₹-19,492.39 |
| 14:43:52 | 20260928-POLICYBZR-12 | ₹-576.40 | ₹-20,068.79 |
| 14:45:21 | 20260928-TURTLEMINT-05 | ₹155.00 | ₹-19,913.79 |
| 14:48:13 | 20260928-CLEANMAX-10 | ₹-580.00 | ₹-20,493.79 |
| 14:50:59 | 20260928-SYNCOMF-12 | ₹-55.00 | ₹-20,548.79 |
| 14:51:20 | 20260928-BHARATCOAL-03 | ₹25.00 | ₹-20,523.79 |
| 14:52:09 | 20260928-SMCGLOBAL-11 | ₹50.00 | ₹-20,473.79 |
| 14:52:28 | 20260928-ZYDUSLIFE1!-10 | ₹-455.10 | ₹-20,928.89 |
| 14:53:22 | 20260928-ONIXSOLAR-03 | ₹-532.65 | ₹-21,461.54 |
| 15:15:00 | 20260928-AEQUS-10 | ₹314.40 | ₹-21,147.14 |

## 6. Risk-budget audit

For every BUY, intended risk was recomputed as `abs(entry − initial stop) × final qty`. Range: ₹10.00 to ₹500.00; median ₹280.00. The ₹500 input is a ceiling before integer flooring and the exposure/500-share caps, not a promise that every trade risks exactly ₹500.

35 closed trades lost more than the configured ₹500 budget in theoretical alert P&L. Worst ratios:

| Trade | Entry | Initial SL | Qty | Intended risk | Exit | Loss | Loss/risk | Reason |
|---|---|---|---|---|---|---|---|---|
| 20260928-STYLEBAAZA-03 | ₹394.55 | ₹394.05 | 500 | ₹250.00 | ₹390.05 | ₹-2,250.00 | 9.00× | HARD STOP |
| 20260928-STYLEBAAZA-01 | ₹394.55 | ₹393.15 | 357 | ₹499.80 | ₹389.80 | ₹-1,695.75 | 3.39× | HARD STOP |
| 20260928-ZYDUSLIFE1!-08 | ₹1,217.30 | ₹1,216.95 | 246 | ₹86.10 | ₹1,216.15 | ₹-282.90 | 3.29× | HARD STOP |
| 20260928-STYLEBAAZA-04 | ₹394.40 | ₹393.80 | 500 | ₹300.00 | ₹393.05 | ₹-675.00 | 2.25× | HARD STOP |
| 20260928-CLEANMAX-08 | ₹1,383.50 | ₹1,381.80 | 216 | ₹367.20 | ₹1,380.20 | ₹-712.80 | 1.94× | HARD STOP |
| 20260928-AEQUS-08 | ₹254.87 | ₹254.70 | 500 | ₹85.00 | ₹254.54 | ₹-165.00 | 1.94× | HARD STOP |
| 20260928-KPIGREEN-05 | ₹354.25 | ₹353.85 | 500 | ₹200.00 | ₹353.50 | ₹-375.00 | 1.88× | HARD STOP |
| 20260928-LLOYDSENGG-04 | ₹97.17 | ₹97.06 | 500 | ₹55.00 | ₹96.97 | ₹-100.00 | 1.82× | HARD STOP |
| 20260928-POLICYBZR-02 | ₹1,182.00 | ₹1,177.20 | 104 | ₹499.20 | ₹1,173.70 | ₹-863.20 | 1.73× | HARD STOP |
| 20260928-SHRINGARMS-01 | ₹224.89 | ₹224.38 | 500 | ₹255.00 | ₹224.04 | ₹-425.00 | 1.67× | HARD STOP |
| 20260928-GESHIP-05 | ₹1,534.90 | ₹1,533.70 | 195 | ₹234.00 | ₹1,532.90 | ₹-390.00 | 1.67× | HARD STOP |
| 20260928-SMCGLOBAL-07 | ₹99.31 | ₹99.17 | 500 | ₹70.00 | ₹99.08 | ₹-115.00 | 1.64× | HARD STOP |
| 20260928-AEQUS-07 | ₹255.14 | ₹254.95 | 500 | ₹95.00 | ₹254.83 | ₹-155.00 | 1.63× | HARD STOP |
| 20260928-SYNCOMF-01 | ₹22.62 | ₹22.43 | 500 | ₹95.00 | ₹22.31 | ₹-155.00 | 1.63× | HARD STOP |
| 20260928-TFCILTD-01 | ₹141.15 | ₹140.90 | 500 | ₹125.00 | ₹140.75 | ₹-200.00 | 1.60× | HARD STOP |

Mechanism proven by code and payload: quantity is sized correctly from the initial stop (208–213, 297–303), but live stop detection uses current `close` and the logged market-exit reference is that observed price minus one tick (250–251, 420–433). Therefore a feed update below the stop creates a realized theoretical loss beyond intended risk. The CSV cannot distinguish a true exchange gap from sparse feed updates or alert latency, and contains no broker fills; it can prove stop-reference overshoot, not its market-microstructure cause.

## 7. Exit analysis

| Reason | Trades | Win rate | Gross | Average | Avg R | Profit factor | Avg hold |
|---|---|---|---|---|---|---|---|
| HARD STOP | 83 | 0.0% | ₹-32,924.83 | ₹-396.68 | -1.45 | 0.00 | 00:03:05 |
| TARGET | 25 | 100.0% | ₹12,055.59 | ₹482.22 | 2.04 | inf | 00:03:32 |
| BREAK EVEN | 0 | NA | ₹0.00 | NA | NA | NA | NA |
| PROFIT LOCK | 16 | 62.5% | ₹-32.70 | ₹-2.04 | -0.10 | 0.92 | 00:03:20 |
| ATR TRAIL | 13 | 84.6% | ₹1,795.80 | ₹138.14 | 0.41 | 24.32 | 00:07:23 |
| PSAR FALLBACK | 16 | 6.2% | ₹-2,355.40 | ₹-147.21 | -0.49 | 0.09 | 00:12:36 |
| EOD | 1 | 100.0% | ₹314.40 | ₹314.40 | 0.63 | inf | 00:28:00 |
| other | 0 | NA | ₹0.00 | NA | NA | NA | NA |

On this day, value by logged exit mechanism ranks: TARGET ₹12,055.59; ATR TRAIL ₹1,795.80; EOD ₹314.40; PROFIT LOCK ₹-32.70; PSAR FALLBACK ₹-2,355.40; HARD STOP ₹-32,924.83. Attribution is descriptive: the counterfactual P&L under another exit is unavailable without future candles and is intentionally not invented.

The exit payload arithmetic is internally correct, target exits land at the configured target, and stop reasons follow the recorded active stop reason. Trigger correctness against the actual tick path cannot be verified from alert messages alone. BREAK EVEN is absent because it was disabled; no `other`/ambiguous live exits occurred.

## 8. First entry vs re-entry

| Cohort | Trades | Win rate | Gross | Average | Avg R | Profit factor | Avg hold |
|---|---|---|---|---|---|---|---|
| FIRST ENTRY | 27 | 33.3% | ₹-4,781.75 | ₹-177.10 | -0.51 | 0.41 | 00:05:55 |
| ALL RE-ENTRIES | 127 | 30.7% | ₹-16,365.39 | ₹-128.86 | -0.46 | 0.41 | 00:04:26 |
| SECOND ENTRY | 24 | 29.2% | ₹-4,481.96 | ₹-186.75 | -0.40 | 0.29 | 00:04:36 |
| THIRD ENTRY | 19 | 26.3% | ₹-5,699.31 | ₹-299.96 | -0.90 | 0.13 | 00:04:14 |
| FOURTH+ | 84 | 32.1% | ₹-6,184.12 | ₹-73.62 | -0.39 | 0.59 | 00:04:26 |

Symbols with the most entries:

| Symbol | Entries | Wins | Losses | Gross P&L |
|---|---|---|---|---|
| POLICYBZR | 12 | 4 | 8 | ₹-3,368.50 |
| SYNCOMF | 12 | 2 | 10 | ₹-495.00 |
| SMCGLOBAL | 11 | 4 | 7 | ₹664.44 |
| SSRETAIL | 11 | 5 | 6 | ₹599.50 |
| AEQUS | 10 | 4 | 6 | ₹-1,642.15 |
| CLEANMAX | 10 | 3 | 7 | ₹-1,017.20 |
| ZYDUSLIFE1! | 10 | 5 | 5 | ₹-819.50 |
| DAMCAPITAL | 9 | 2 | 7 | ₹-1,860.18 |
| SBC | 8 | 3 | 5 | ₹-360.00 |
| TFCILTD | 8 | 1 | 7 | ₹-1,285.00 |
| LLOYDSENGG | 7 | 1 | 6 | ₹-115.00 |
| GESHIP | 6 | 2 | 4 | ₹-151.30 |
| KPIGREEN | 6 | 1 | 5 | ₹-1,981.50 |
| TURTLEMINT | 5 | 3 | 2 | ₹-5.00 |
| STYLEBAAZA | 4 | 0 | 4 | ₹-4,645.75 |
| BHARATCOAL | 3 | 1 | 2 | ₹0.00 |
| ONIXSOLAR | 3 | 1 | 2 | ₹-1,098.10 |
| SHANTIGOLD | 3 | 1 | 2 | ₹-556.90 |
| STLNETWORK | 3 | 1 | 2 | ₹-100.00 |
| REMSONSIND | 2 | 0 | 2 | ₹-404.80 |
| SEDEMAC | 2 | 0 | 2 | ₹-883.20 |
| SHRINGARMS | 2 | 0 | 2 | ₹-910.00 |
| SHUKRAPHAR | 2 | 1 | 1 | ₹0.00 |
| SUNFLAG | 2 | 1 | 1 | ₹-507.00 |
| AASTHA | 1 | 1 | 0 | ₹100.00 |
| MUKANDLTD | 1 | 1 | 0 | ₹5.00 |
| SSDL | 1 | 0 | 1 | ₹-310.00 |

## 9. Rapid BUY→SELL investigation

| Trade | BUY IST | Buy | SELL IST | Sell | Duration | Reason | P&L | Initial SL |
|---|---|---|---|---|---|---|---|---|
| 20260928-ZYDUSLIFE1!-08 | 13:58:27 | ₹1,217.30 | 13:58:27 | ₹1,216.15 | 00:00:00 | HARD STOP | ₹-282.90 | ₹1,216.95 |
| 20260928-DAMCAPITAL-07 | 13:09:00 | ₹151.69 | 13:09:02 | ₹151.29 | 00:00:02 | HARD STOP | ₹-200.00 | ₹151.30 |
| 20260928-SMCGLOBAL-06 | 12:26:01 | ₹98.29 | 12:26:06 | ₹97.89 | 00:00:05 | HARD STOP | ₹-200.00 | ₹97.96 |
| 20260928-STLNETWORK-03 | 12:36:27 | ₹52.79 | 12:36:32 | ₹52.60 | 00:00:05 | HARD STOP | ₹-95.00 | ₹52.64 |
| 20260928-BHARATCOAL-02 | 14:09:05 | ₹32.71 | 14:09:10 | ₹32.67 | 00:00:05 | HARD STOP | ₹-20.00 | ₹32.68 |
| 20260928-CLEANMAX-07 | 11:56:02 | ₹1,396.50 | 11:56:08 | ₹1,393.40 | 00:00:06 | HARD STOP | ₹-595.20 | ₹1,393.90 |
| 20260928-AEQUS-08 | 13:46:00 | ₹254.87 | 13:46:07 | ₹254.54 | 00:00:07 | HARD STOP | ₹-165.00 | ₹254.70 |
| 20260928-SSRETAIL-08 | 13:20:00 | ₹716.75 | 13:20:08 | ₹715.25 | 00:00:08 | HARD STOP | ₹-516.00 | ₹715.30 |
| 20260928-LLOYDSENGG-06 | 14:10:13 | ₹95.65 | 14:10:23 | ₹95.53 | 00:00:10 | HARD STOP | ₹-60.00 | ₹95.57 |
| 20260928-BHARATCOAL-01 | 12:03:04 | ₹32.53 | 12:03:15 | ₹32.52 | 00:00:11 | PROFIT LOCK | ₹-5.00 | ₹32.51 |
| 20260928-DAMCAPITAL-01 | 10:37:09 | ₹149.21 | 10:37:22 | ₹150.35 | 00:00:13 | TARGET | ₹570.00 | ₹148.64 |
| 20260928-SHRINGARMS-01 | 09:40:05 | ₹224.89 | 09:40:20 | ₹224.04 | 00:00:15 | HARD STOP | ₹-425.00 | ₹224.38 |
| 20260928-TURTLEMINT-03 | 11:27:00 | ₹84.00 | 11:27:15 | ₹83.85 | 00:00:15 | HARD STOP | ₹-75.00 | ₹83.87 |
| 20260928-SMCGLOBAL-07 | 12:58:03 | ₹99.31 | 12:58:20 | ₹99.08 | 00:00:17 | HARD STOP | ₹-115.00 | ₹99.17 |
| 20260928-STYLEBAAZA-02 | 11:12:06 | ₹393.10 | 11:12:24 | ₹393.05 | 00:00:18 | PROFIT LOCK | ₹-25.00 | ₹392.70 |
| 20260928-TFCILTD-04 | 11:18:03 | ₹140.04 | 11:18:21 | ₹140.07 | 00:00:18 | PROFIT LOCK | ₹15.00 | ₹139.84 |
| 20260928-ZYDUSLIFE1!-03 | 10:59:11 | ₹1,223.50 | 10:59:30 | ₹1,224.50 | 00:00:19 | TARGET | ₹245.00 | ₹1,223.00 |
| 20260928-AEQUS-02 | 11:03:00 | ₹264.75 | 11:03:19 | ₹264.11 | 00:00:19 | HARD STOP | ₹-320.00 | ₹264.17 |
| 20260928-CLEANMAX-04 | 10:46:00 | ₹1,374.20 | 10:46:23 | ₹1,369.90 | 00:00:23 | HARD STOP | ₹-537.50 | ₹1,370.20 |
| 20260928-TFCILTD-05 | 11:28:01 | ₹140.23 | 11:28:24 | ₹140.03 | 00:00:23 | HARD STOP | ₹-100.00 | ₹140.09 |
| 20260928-POLICYBZR-03 | 10:30:01 | ₹1,175.70 | 10:30:27 | ₹1,173.20 | 00:00:26 | HARD STOP | ₹-625.00 | ₹1,173.70 |
| 20260928-SMCGLOBAL-09 | 13:47:03 | ₹97.29 | 13:47:29 | ₹97.66 | 00:00:26 | TARGET | ₹185.00 | ₹97.11 |
| 20260928-TURTLEMINT-04 | 14:38:07 | ₹82.14 | 14:38:33 | ₹81.78 | 00:00:26 | HARD STOP | ₹-180.00 | ₹81.87 |
| 20260928-LLOYDSENGG-07 | 14:23:03 | ₹95.26 | 14:23:33 | ₹95.22 | 00:00:30 | PROFIT LOCK | ₹-20.00 | ₹95.19 |
| 20260928-POLICYBZR-04 | 11:06:01 | ₹1,172.40 | 11:06:32 | ₹1,170.00 | 00:00:31 | HARD STOP | ₹-612.00 | ₹1,170.70 |
| 20260928-CLEANMAX-06 | 11:28:00 | ₹1,383.80 | 11:28:35 | ₹1,387.90 | 00:00:35 | TARGET | ₹885.60 | ₹1,381.80 |
| 20260928-CLEANMAX-08 | 12:12:01 | ₹1,383.50 | 12:12:36 | ₹1,380.20 | 00:00:35 | HARD STOP | ₹-712.80 | ₹1,381.80 |
| 20260928-CLEANMAX-01 | 10:01:00 | ₹1,440.30 | 10:01:38 | ₹1,433.30 | 00:00:38 | HARD STOP | ₹-553.00 | ₹1,434.00 |
| 20260928-ZYDUSLIFE1!-09 | 14:27:03 | ₹1,213.50 | 14:27:42 | ₹1,214.40 | 00:00:39 | TARGET | ₹222.30 | ₹1,213.05 |
| 20260928-TFCILTD-01 | 10:09:03 | ₹141.15 | 10:09:44 | ₹140.75 | 00:00:41 | HARD STOP | ₹-200.00 | ₹140.90 |
| 20260928-POLICYBZR-09 | 13:11:01 | ₹1,154.50 | 13:11:42 | ₹1,157.70 | 00:00:41 | TARGET | ₹828.80 | ₹1,152.90 |
| 20260928-SYNCOMF-01 | 10:17:03 | ₹22.62 | 10:17:45 | ₹22.31 | 00:00:42 | HARD STOP | ₹-155.00 | ₹22.43 |
| 20260928-SSRETAIL-07 | 12:18:00 | ₹729.10 | 12:18:42 | ₹727.05 | 00:00:42 | HARD STOP | ₹-621.15 | ₹727.45 |
| 20260928-AEQUS-01 | 10:40:00 | ₹267.99 | 10:40:45 | ₹266.84 | 00:00:45 | HARD STOP | ₹-575.00 | ₹267.01 |
| 20260928-ZYDUSLIFE1!-05 | 12:02:04 | ₹1,222.10 | 12:02:49 | ₹1,222.85 | 00:00:45 | TARGET | ₹183.75 | ₹1,221.75 |
| 20260928-STLNETWORK-02 | 12:04:10 | ₹53.11 | 12:04:56 | ₹52.98 | 00:00:46 | HARD STOP | ₹-65.00 | ₹53.01 |
| 20260928-POLICYBZR-06 | 11:51:01 | ₹1,172.50 | 11:51:52 | ₹1,172.30 | 00:00:51 | ATR TRAIL | ₹-47.60 | ₹1,170.40 |
| 20260928-SMCGLOBAL-04 | 11:42:05 | ₹99.06 | 11:42:59 | ₹98.85 | 00:00:54 | HARD STOP | ₹-105.00 | ₹98.87 |
| 20260928-TFCILTD-06 | 12:08:09 | ₹139.90 | 12:09:04 | ₹139.73 | 00:00:55 | HARD STOP | ₹-85.00 | ₹139.76 |
| 20260928-AEQUS-07 | 13:29:00 | ₹255.14 | 13:29:55 | ₹254.83 | 00:00:55 | HARD STOP | ₹-155.00 | ₹254.95 |
| 20260928-SYNCOMF-08 | 13:13:01 | ₹22.03 | 13:14:00 | ₹21.95 | 00:00:59 | HARD STOP | ₹-40.00 | ₹21.98 |
| 20260928-CLEANMAX-02 | 10:17:00 | ₹1,425.80 | 10:18:00 | ₹1,421.90 | 00:01:00 | HARD STOP | ₹-538.20 | ₹1,422.20 |
| 20260928-POLICYBZR-10 | 14:03:01 | ₹1,151.00 | 14:04:01 | ₹1,151.90 | 00:01:00 | PSAR FALLBACK | ₹234.00 | ₹1,150.00 |
| 20260928-SBC-02 | 10:34:05 | ₹54.51 | 10:35:08 | ₹54.65 | 00:01:03 | TARGET | ₹70.00 | ₹54.44 |
| 20260928-ZYDUSLIFE1!-01 | 10:01:02 | ₹1,224.70 | 10:02:06 | ₹1,222.00 | 00:01:04 | HARD STOP | ₹-658.80 | ₹1,222.70 |
| 20260928-AEQUS-09 | 14:28:00 | ₹254.71 | 14:29:04 | ₹254.02 | 00:01:04 | HARD STOP | ₹-345.00 | ₹254.10 |
| 20260928-SYNCOMF-03 | 11:03:03 | ₹22.20 | 11:04:09 | ₹22.13 | 00:01:06 | HARD STOP | ₹-35.00 | ₹22.15 |
| 20260928-SSRETAIL-09 | 13:26:01 | ₹717.55 | 13:27:07 | ₹717.10 | 00:01:06 | PROFIT LOCK | ₹-188.10 | ₹716.55 |
| 20260928-POLICYBZR-11 | 14:18:01 | ₹1,148.20 | 14:19:07 | ₹1,145.90 | 00:01:06 | HARD STOP | ₹-575.00 | ₹1,146.20 |
| 20260928-SBC-07 | 13:47:04 | ₹54.14 | 13:48:11 | ₹54.20 | 00:01:07 | TARGET | ₹30.00 | ₹54.11 |
| 20260928-REMSONSIND-01 | 11:07:04 | ₹129.50 | 11:08:14 | ₹129.01 | 00:01:10 | HARD STOP | ₹-245.00 | ₹129.18 |
| 20260928-DAMCAPITAL-05 | 12:11:00 | ₹151.70 | 12:12:12 | ₹150.71 | 00:01:12 | HARD STOP | ₹-495.00 | ₹150.97 |
| 20260928-BHARATCOAL-03 | 14:50:06 | ₹32.63 | 14:51:20 | ₹32.68 | 00:01:14 | TARGET | ₹25.00 | ₹32.61 |
| 20260928-TFCILTD-03 | 10:57:00 | ₹141.50 | 10:58:15 | ₹140.93 | 00:01:15 | HARD STOP | ₹-285.00 | ₹141.07 |
| 20260928-SSRETAIL-06 | 12:00:00 | ₹726.00 | 12:01:17 | ₹731.90 | 00:01:17 | TARGET | ₹997.10 | ₹723.05 |
| 20260928-ZYDUSLIFE1!-02 | 10:27:32 | ₹1,221.40 | 10:28:50 | ₹1,222.50 | 00:01:18 | TARGET | ₹269.50 | ₹1,220.85 |
| 20260928-DAMCAPITAL-02 | 10:53:01 | ₹151.65 | 10:54:29 | ₹150.63 | 00:01:28 | HARD STOP | ₹-510.00 | ₹150.81 |
| 20260928-SMCGLOBAL-03 | 10:56:01 | ₹99.48 | 10:57:29 | ₹98.72 | 00:01:28 | HARD STOP | ₹-380.00 | ₹98.75 |
| 20260928-KPIGREEN-05 | 13:02:03 | ₹354.25 | 13:03:34 | ₹353.50 | 00:01:31 | HARD STOP | ₹-375.00 | ₹353.85 |
| 20260928-SBC-06 | 12:26:03 | ₹54.69 | 12:27:43 | ₹54.62 | 00:01:40 | HARD STOP | ₹-35.00 | ₹54.63 |
| 20260928-AASTHA-01 | 09:49:01 | ₹38.69 | 09:50:46 | ₹38.89 | 00:01:45 | TARGET | ₹100.00 | ₹38.59 |
| 20260928-SYNCOMF-07 | 12:23:00 | ₹21.95 | 12:24:45 | ₹22.00 | 00:01:45 | ATR TRAIL | ₹25.00 | ₹21.81 |
| 20260928-SYNCOMF-10 | 14:13:06 | ₹22.09 | 14:14:54 | ₹22.00 | 00:01:48 | HARD STOP | ₹-45.00 | ₹22.02 |
| 20260928-POLICYBZR-12 | 14:42:00 | ₹1,141.40 | 14:43:52 | ₹1,139.20 | 00:01:52 | HARD STOP | ₹-576.40 | ₹1,139.90 |
| 20260928-TFCILTD-07 | 12:43:01 | ₹140.48 | 12:44:54 | ₹140.24 | 00:01:53 | HARD STOP | ₹-120.00 | ₹140.30 |
| 20260928-SYNCOMF-12 | 14:49:03 | ₹22.11 | 14:50:59 | ₹22.00 | 00:01:56 | HARD STOP | ₹-55.00 | ₹22.01 |
| 20260928-GESHIP-06 | 14:12:02 | ₹1,531.00 | 14:13:59 | ₹1,530.40 | 00:01:57 | PROFIT LOCK | ₹-117.00 | ₹1,530.00 |
| 20260928-SHANTIGOLD-03 | 14:27:02 | ₹308.75 | 14:29:09 | ₹308.05 | 00:02:07 | HARD STOP | ₹-350.00 | ₹308.15 |
| 20260928-MUKANDLTD-01 | 09:41:02 | ₹171.52 | 09:43:10 | ₹171.53 | 00:02:08 | PROFIT LOCK | ₹5.00 | ₹170.94 |
| 20260928-TURTLEMINT-05 | 14:43:10 | ₹81.93 | 14:45:21 | ₹82.24 | 00:02:11 | TARGET | ₹155.00 | ₹81.78 |
| 20260928-CLEANMAX-10 | 14:46:02 | ₹1,393.90 | 14:48:13 | ₹1,388.90 | 00:02:11 | HARD STOP | ₹-580.00 | ₹1,389.60 |
| 20260928-LLOYDSENGG-01 | 10:47:04 | ₹97.28 | 10:49:21 | ₹97.08 | 00:02:17 | HARD STOP | ₹-100.00 | ₹97.14 |
| 20260928-KPIGREEN-06 | 14:25:02 | ₹354.60 | 14:27:20 | ₹355.35 | 00:02:18 | TARGET | ₹375.00 | ₹354.25 |
| 20260928-SSRETAIL-10 | 13:53:00 | ₹718.35 | 13:55:22 | ₹718.20 | 00:02:22 | ATR TRAIL | ₹-29.40 | ₹715.80 |
| 20260928-AEQUS-04 | 12:12:01 | ₹258.80 | 12:14:25 | ₹258.87 | 00:02:24 | PROFIT LOCK | ₹35.00 | ₹258.37 |
| 20260928-SHANTIGOLD-02 | 10:56:02 | ₹310.85 | 10:58:27 | ₹311.65 | 00:02:25 | TARGET | ₹400.00 | ₹310.45 |
| 20260928-SSRETAIL-04 | 11:12:01 | ₹712.20 | 11:14:27 | ₹712.55 | 00:02:26 | PROFIT LOCK | ₹71.40 | ₹709.75 |
| 20260928-SMCGLOBAL-05 | 11:52:01 | ₹99.23 | 11:54:29 | ₹98.85 | 00:02:28 | HARD STOP | ₹-190.00 | ₹98.88 |
| 20260928-GESHIP-02 | 11:13:04 | ₹1,541.30 | 11:15:33 | ₹1,538.40 | 00:02:29 | HARD STOP | ₹-562.60 | ₹1,538.90 |
| 20260928-SBC-05 | 11:55:04 | ₹54.91 | 11:57:38 | ₹54.80 | 00:02:34 | HARD STOP | ₹-55.00 | ₹54.81 |
| 20260928-POLICYBZR-02 | 10:15:00 | ₹1,182.00 | 10:17:36 | ₹1,173.70 | 00:02:36 | HARD STOP | ₹-863.20 | ₹1,177.20 |
| 20260928-SUNFLAG-01 | 09:45:00 | ₹441.65 | 09:47:40 | ₹442.55 | 00:02:40 | ATR TRAIL | ₹124.20 | ₹438.05 |
| 20260928-LLOYDSENGG-05 | 13:18:03 | ₹95.60 | 13:20:57 | ₹95.47 | 00:02:54 | HARD STOP | ₹-65.00 | ₹95.48 |
| 20260928-GESHIP-05 | 12:49:00 | ₹1,534.90 | 12:51:55 | ₹1,532.90 | 00:02:55 | HARD STOP | ₹-390.00 | ₹1,533.70 |
| 20260928-ZYDUSLIFE1!-04 | 11:20:35 | ₹1,222.30 | 11:23:33 | ₹1,223.30 | 00:02:58 | TARGET | ₹245.00 | ₹1,221.80 |
| 20260928-SSRETAIL-05 | 11:43:01 | ₹717.70 | 11:46:07 | ₹724.65 | 00:03:06 | TARGET | ₹1,000.80 | ₹714.25 |
| 20260928-SHANTIGOLD-01 | 10:17:01 | ₹315.25 | 10:20:09 | ₹313.55 | 00:03:08 | HARD STOP | ₹-606.90 | ₹313.85 |
| 20260928-KPIGREEN-02 | 11:38:01 | ₹359.05 | 11:41:15 | ₹357.90 | 00:03:14 | HARD STOP | ₹-575.00 | ₹358.25 |
| 20260928-ZYDUSLIFE1!-10 | 14:49:13 | ₹1,216.30 | 14:52:28 | ₹1,214.45 | 00:03:15 | HARD STOP | ₹-455.10 | ₹1,214.70 |
| 20260928-GESHIP-03 | 11:27:01 | ₹1,537.40 | 11:30:21 | ₹1,537.70 | 00:03:20 | PROFIT LOCK | ₹58.50 | ₹1,535.80 |
| 20260928-TURTLEMINT-02 | 10:29:04 | ₹84.68 | 10:32:30 | ₹84.69 | 00:03:26 | PROFIT LOCK | ₹5.00 | ₹84.56 |
| 20260928-SSDL-01 | 09:48:00 | ₹83.81 | 09:51:32 | ₹83.19 | 00:03:32 | HARD STOP | ₹-310.00 | ₹83.33 |
| 20260928-DAMCAPITAL-09 | 14:31:01 | ₹153.77 | 14:34:37 | ₹152.74 | 00:03:36 | HARD STOP | ₹-504.70 | ₹152.75 |
| 20260928-SSRETAIL-02 | 10:31:00 | ₹711.55 | 10:34:37 | ₹707.45 | 00:03:37 | HARD STOP | ₹-668.30 | ₹708.50 |
| 20260928-SSRETAIL-01 | 09:48:01 | ₹724.30 | 09:52:01 | ₹741.85 | 00:04:00 | ATR TRAIL | ₹473.85 | ₹706.15 |
| 20260928-CLEANMAX-03 | 10:37:00 | ₹1,374.90 | 10:41:01 | ₹1,363.90 | 00:04:01 | PSAR FALLBACK | ₹-396.00 | ₹1,361.10 |
| 20260928-KPIGREEN-04 | 12:13:02 | ₹358.00 | 12:17:14 | ₹357.40 | 00:04:12 | HARD STOP | ₹-300.00 | ₹357.50 |
| 20260928-SEDEMAC-02 | 14:20:02 | ₹3,539.40 | 14:24:15 | ₹3,533.50 | 00:04:13 | HARD STOP | ₹-495.60 | ₹3,535.20 |
| 20260928-SYNCOMF-04 | 11:23:00 | ₹22.05 | 11:27:17 | ₹21.95 | 00:04:17 | HARD STOP | ₹-50.00 | ₹21.97 |
| 20260928-SYNCOMF-06 | 12:06:00 | ₹21.98 | 12:10:20 | ₹21.93 | 00:04:20 | HARD STOP | ₹-25.00 | ₹21.94 |
| 20260928-ONIXSOLAR-03 | 14:49:02 | ₹346.60 | 14:53:22 | ₹338.65 | 00:04:20 | HARD STOP | ₹-532.65 | ₹339.15 |
| 20260928-SYNCOMF-11 | 14:29:05 | ₹22.00 | 14:33:29 | ₹21.93 | 00:04:24 | HARD STOP | ₹-35.00 | ₹21.94 |
| 20260928-SHUKRAPHAR-02 | 14:17:01 | ₹59.26 | 14:21:26 | ₹59.34 | 00:04:25 | PROFIT LOCK | ₹40.00 | ₹58.87 |
| 20260928-POLICYBZR-05 | 11:20:00 | ₹1,172.50 | 11:24:27 | ₹1,172.70 | 00:04:27 | PROFIT LOCK | ₹51.00 | ₹1,171.20 |
| 20260928-SUNFLAG-02 | 10:38:01 | ₹445.45 | 10:42:36 | ₹443.05 | 00:04:35 | HARD STOP | ₹-631.20 | ₹443.55 |
| 20260928-KPIGREEN-01 | 11:01:00 | ₹365.35 | 11:05:40 | ₹362.50 | 00:04:40 | HARD STOP | ₹-570.00 | ₹362.85 |
| 20260928-POLICYBZR-08 | 12:56:00 | ₹1,161.90 | 13:00:52 | ₹1,159.50 | 00:04:52 | HARD STOP | ₹-544.80 | ₹1,159.70 |

All BUYs are confirmed PSAR flips at a 1-minute bar close. Management is disabled on the entry bar (`bar_index > entryBarIp`), then begins on the next bar. A sub-minute EXIT is therefore possible on the first update of the next 1-minute bar if current price is already at/below the stop. PSAR FALLBACK requires a confirmed opposite flip under these inputs, so its fastest normal timing is about one bar. These are code-legitimate invalidations; repeated clusters and their P&L are evidence of sensitivity/chop, but the CSV lacks raw PSAR values and candles needed to quantify chop independently.

## 10. Time-of-day analysis

| Entry bucket IST | Trades | Win rate | Gross | Average | Avg R | Profit factor | Avg hold |
|---|---|---|---|---|---|---|---|
| 09:15–09:30 | 0 | NA | ₹0.00 | NA | NA | NA | NA |
| 09:30–10:00 | 11 | 54.5% | ₹-454.65 | ₹-41.33 | -0.09 | 0.78 | 00:05:19 |
| 10:00–11:00 | 32 | 28.1% | ₹-6,654.91 | ₹-207.97 | -0.40 | 0.41 | 00:04:06 |
| 11:00–12:00 | 34 | 32.4% | ₹-5,455.33 | ₹-160.45 | -0.70 | 0.35 | 00:04:29 |
| 12:00–13:00 | 26 | 23.1% | ₹-4,138.75 | ₹-159.18 | -0.71 | 0.28 | 00:03:48 |
| 13:00–14:00 | 23 | 26.1% | ₹-1,349.25 | ₹-58.66 | -0.36 | 0.61 | 00:07:15 |
| 14:00–15:00 | 28 | 35.7% | ₹-3,094.25 | ₹-110.51 | -0.30 | 0.39 | 00:04:06 |
| 15:00–EOD | 0 | NA | ₹0.00 | NA | NA | NA | NA |

## 11. Liquidity analysis

| Observed liquidity payload | Alerts |
|---|---|
| PASS | 154 |
| FAIL | 244 |
| UNKNOWN | 0 |
| SKIP total | 244 |

Every actual BUY reports PASS; there are no UNKNOWN alerts. PASS-vs-UNKNOWN trade performance therefore cannot be compared. FAIL signals are deliberately untraded, so the CSV also cannot measure their counterfactual returns. The gate emitted 244 SKIP rows representing 241 unique symbol-timestamp candidates after collapsing three parallel ARVEE configurations. Most-skipped symbols: BPL (12), AASTHA (12), SHRINGARMS (12), SUNFLAG (11), MUKANDLTD (10), UTTAMSUGAR (10), MAHLOG (10), REPRO (9), KITEX (9), ARTEMISMED (9).

The baseline implementation is causal: it waits for a full lookback of completed current-session bars and uses `volume[1]`/`(close×volume)[1]` (147–165). The three chart alerts used lookbacks 10, 15 and 20. No UNKNOWN events appear because the export starts at 09:40 IST, by which time even the 20-bar baseline can be ready. The CSV provides no evidence of prior-day leakage under the hardened code.

## 12. PSAR-entry analysis

The CSV contains 398 BUY/SKIP alert rows but only **395 unique symbol-timestamp candidate episodes**, after collapsing the three parallel ARVEE configuration collisions. It proves at least that many flat-state confirmed PSAR BUY candidates. It does **not** log every PSAR flip while already in a trade, nor raw PSAR level/direction, ADX, EMA or VWAP values. Exact flip frequency and movement-before-entry are therefore data limitations.

Observed confirmed opposite flips caused 16 PSAR FALLBACK exits. 2 of those occurred within five minutes. Across all exits, 107 trades reversed/invalidated inside five minutes. This isolates short-lived entries, but cannot by itself separate 'bad PSAR entry' from an appropriately tight initial stop: stop exits with loss/risk near 1R are designed invalidations, whereas loss/risk materially above 1R is specifically exit-reference overshoot.

## 13. Symbol-level forensic table

| Symbol | Trades | First P&L | Re-entry P&L | Wins | Losses | Win rate | Gross | Avg R | Worst R | Main exit | Rapid |
|---|---|---|---|---|---|---|---|---|---|---|---|
| STYLEBAAZA | 4 | ₹-1,695.75 | ₹-2,950.00 | 0 | 4 | 0.0% | ₹-4,645.75 | -3.69 | -9.00 | HARD STOP | 1 |
| POLICYBZR | 12 | ₹-672.70 | ₹-2,695.80 | 4 | 8 | 33.3% | ₹-3,368.50 | -0.53 | -1.73 | HARD STOP | 10 |
| KPIGREEN | 6 | ₹-570.00 | ₹-1,411.50 | 1 | 5 | 16.7% | ₹-1,981.50 | -0.76 | -1.88 | HARD STOP | 5 |
| DAMCAPITAL | 9 | ₹570.00 | ₹-2,430.18 | 2 | 7 | 22.2% | ₹-1,860.18 | -0.31 | -1.36 | HARD STOP | 5 |
| AEQUS | 10 | ₹-575.00 | ₹-1,067.15 | 4 | 6 | 40.0% | ₹-1,642.15 | -0.70 | -1.94 | HARD STOP | 6 |
| TFCILTD | 8 | ₹-200.00 | ₹-1,085.00 | 1 | 7 | 12.5% | ₹-1,285.00 | -1.02 | -1.60 | HARD STOP | 6 |
| ONIXSOLAR | 3 | ₹-648.45 | ₹-449.65 | 1 | 2 | 33.3% | ₹-1,098.10 | -0.73 | -1.30 | HARD STOP | 1 |
| CLEANMAX | 10 | ₹-553.00 | ₹-464.20 | 3 | 7 | 30.0% | ₹-1,017.20 | -0.23 | -1.94 | HARD STOP | 8 |
| SHRINGARMS | 2 | ₹-425.00 | ₹-485.00 | 0 | 2 | 0.0% | ₹-910.00 | -1.42 | -1.67 | HARD STOP | 1 |
| SEDEMAC | 2 | ₹-387.60 | ₹-495.60 | 0 | 2 | 0.0% | ₹-883.20 | -1.09 | -1.40 | PSAR FALLBACK | 1 |
| ZYDUSLIFE1! | 10 | ₹-658.80 | ₹-160.70 | 5 | 5 | 50.0% | ₹-819.50 | 0.24 | -3.29 | TARGET | 8 |
| SHANTIGOLD | 3 | ₹-606.90 | ₹50.00 | 1 | 2 | 33.3% | ₹-556.90 | -0.13 | -1.21 | HARD STOP | 3 |
| SUNFLAG | 2 | ₹124.20 | ₹-631.20 | 1 | 1 | 50.0% | ₹-507.00 | -0.51 | -1.26 | ATR TRAIL | 2 |
| SYNCOMF | 12 | ₹-155.00 | ₹-340.00 | 2 | 10 | 16.7% | ₹-495.00 | -0.82 | -1.63 | HARD STOP | 9 |
| REMSONSIND | 2 | ₹-245.00 | ₹-159.80 | 0 | 2 | 0.0% | ₹-404.80 | -0.93 | -1.53 | HARD STOP | 1 |
| SBC | 8 | ₹-195.00 | ₹-165.00 | 3 | 5 | 37.5% | ₹-360.00 | -0.05 | -1.17 | HARD STOP | 4 |
| SSDL | 1 | ₹-310.00 | ₹0.00 | 0 | 1 | 0.0% | ₹-310.00 | -1.29 | -1.29 | HARD STOP | 1 |
| GESHIP | 6 | ₹998.40 | ₹-1,149.70 | 2 | 4 | 33.3% | ₹-151.30 | -0.26 | -1.67 | HARD STOP | 4 |
| LLOYDSENGG | 7 | ₹-100.00 | ₹-15.00 | 1 | 6 | 14.3% | ₹-115.00 | -0.84 | -1.82 | HARD STOP | 4 |
| STLNETWORK | 3 | ₹60.00 | ₹-160.00 | 1 | 2 | 33.3% | ₹-100.00 | -0.74 | -1.30 | HARD STOP | 2 |
| TURTLEMINT | 5 | ₹90.00 | ₹-95.00 | 3 | 2 | 60.0% | ₹-5.00 | -0.01 | -1.33 | HARD STOP | 4 |
| BHARATCOAL | 3 | ₹-5.00 | ₹5.00 | 1 | 2 | 33.3% | ₹0.00 | 0.22 | -1.33 | PROFIT LOCK | 3 |
| SHUKRAPHAR | 2 | ₹-40.00 | ₹40.00 | 1 | 1 | 50.0% | ₹0.00 | 0.03 | -0.14 | PSAR FALLBACK | 1 |
| MUKANDLTD | 1 | ₹5.00 | ₹0.00 | 1 | 0 | 100.0% | ₹5.00 | 0.02 | 0.02 | PROFIT LOCK | 1 |
| AASTHA | 1 | ₹100.00 | ₹0.00 | 1 | 0 | 100.0% | ₹100.00 | 2.00 | 2.00 | TARGET | 1 |
| SSRETAIL | 11 | ₹473.85 | ₹125.65 | 5 | 6 | 45.5% | ₹599.50 | 0.11 | -1.34 | ATR TRAIL | 9 |
| SMCGLOBAL | 11 | ₹840.00 | ₹-175.56 | 4 | 7 | 36.4% | ₹664.44 | -0.12 | -1.64 | HARD STOP | 6 |

Worst 10 trade episodes:

| Trade | Entry / initial SL | Highest | Old / new SL | Exit | Hold | P&L | R |
|---|---|---|---|---|---|---|---|
| 20260928-STYLEBAAZA-03 | ₹394.55 / ₹394.05 | ₹394.55 | ₹394.05 / ₹394.05 | ₹390.05 | 00:09:40 | ₹-2,250.00 | -9.00 |
| 20260928-STYLEBAAZA-01 | ₹394.55 / ₹393.15 | ₹394.55 | ₹393.15 / ₹393.15 | ₹389.80 | 00:15:06 | ₹-1,695.75 | -3.39 |
| 20260928-POLICYBZR-02 | ₹1,182.00 / ₹1,177.20 | ₹1,182.10 | ₹1,177.20 / ₹1,177.20 | ₹1,173.70 | 00:02:36 | ₹-863.20 | -1.73 |
| 20260928-CLEANMAX-08 | ₹1,383.50 / ₹1,381.80 | ₹1,383.80 | ₹1,381.80 / ₹1,381.80 | ₹1,380.20 | 00:00:35 | ₹-712.80 | -1.94 |
| 20260928-STYLEBAAZA-04 | ₹394.40 / ₹393.80 | ₹394.55 | ₹393.80 / ₹393.80 | ₹393.05 | 00:07:08 | ₹-675.00 | -2.25 |
| 20260928-AEQUS-05 | ₹258.84 / ₹256.91 | ₹258.97 | ₹256.91 / ₹256.91 | ₹256.24 | 00:06:33 | ₹-673.40 | -1.35 |
| 20260928-POLICYBZR-01 | ₹1,193.50 / ₹1,191.20 | ₹1,194.30 | ₹1,191.20 / ₹1,191.20 | ₹1,190.40 | 00:08:27 | ₹-672.70 | -1.35 |
| 20260928-SSRETAIL-02 | ₹711.55 / ₹708.50 | ₹714.30 | ₹708.50 / ₹708.50 | ₹707.45 | 00:03:37 | ₹-668.30 | -1.34 |
| 20260928-ZYDUSLIFE1!-01 | ₹1,224.70 / ₹1,222.70 | ₹1,224.80 | ₹1,222.70 / ₹1,222.70 | ₹1,222.00 | 00:01:04 | ₹-658.80 | -1.35 |
| 20260928-ONIXSOLAR-01 | ₹354.70 / ₹349.65 | ₹354.70 | ₹349.65 / ₹349.65 | ₹348.15 | 00:24:57 | ₹-648.45 | -1.30 |

All ten worst episodes exited on the HARD STOP path. In each, `Old SL` equals `New SL`, so the loss was not created by a newly ratcheted profit-lock/trailing stop. The most pathological STYLEBAAZA entries never exceeded their entry in the logged `Highest since entry`, then first observed price crossed materially below the unchanged initial stop: exact evidence of stop-reference overshoot layered on failed entries.

Best 10 trade episodes:

| Trade | Entry / target | Highest | Exit | Reason | Hold | P&L | R |
|---|---|---|---|---|---|---|---|
| 20260928-CLEANMAX-09 | ₹1,375.90 / ₹1,384.40 | ₹1,384.60 | ₹1,384.40 | TARGET | 00:20:12 | ₹1,011.50 | 2.02 |
| 20260928-SSRETAIL-05 | ₹717.70 / ₹724.65 | ₹725.00 | ₹724.65 | TARGET | 00:03:06 | ₹1,000.80 | 2.01 |
| 20260928-SMCGLOBAL-02 | ₹97.05 / ₹99.53 | ₹99.70 | ₹99.53 | TARGET | 00:09:27 | ₹999.44 | 2.00 |
| 20260928-GESHIP-01 | ₹1,539.20 / ₹1,545.60 | ₹1,545.70 | ₹1,545.60 | TARGET | 00:05:16 | ₹998.40 | 2.00 |
| 20260928-CLEANMAX-05 | ₹1,375.00 / ₹1,390.60 | ₹1,390.60 | ₹1,390.60 | TARGET | 00:06:45 | ₹998.40 | 2.00 |
| 20260928-SSRETAIL-06 | ₹726.00 / ₹731.90 | ₹733.30 | ₹731.90 | TARGET | 00:01:17 | ₹997.10 | 2.00 |
| 20260928-CLEANMAX-06 | ₹1,383.80 / ₹1,387.90 | ₹1,387.90 | ₹1,387.90 | TARGET | 00:00:35 | ₹885.60 | 2.05 |
| 20260928-SMCGLOBAL-01 | ₹94.74 / ₹96.42 | ₹96.48 | ₹96.42 | TARGET | 00:07:07 | ₹840.00 | 2.00 |
| 20260928-POLICYBZR-09 | ₹1,154.50 / ₹1,157.70 | ₹1,158.60 | ₹1,157.70 | TARGET | 00:00:41 | ₹828.80 | 2.00 |
| 20260928-DAMCAPITAL-01 | ₹149.21 / ₹150.35 | ₹150.40 | ₹150.35 | TARGET | 00:00:13 | ₹570.00 | 2.00 |

All ten best episodes reached TARGET and realized approximately +2R, exactly matching the configured fixed-target path. The contrasting tails show that the target mechanism behaves as encoded while hard-stop losses can extend well past −1R. Case-study interpretation is bounded to the logged state path: without candle/tick history, no claim is made about what price did afterward or whether another entry rule would have been better.

## 14. Cost sensitivity

| Metric | Value |
|---|---|
| Gross theoretical result | ₹-21,147.14 |
| Round-trip traded turnover | ₹36,439,677.84 |
| Gross per closed trade | ₹-137.32 |
| Break-even total costs | Not applicable: gross result is non-positive |
| Break-even cost per trade | NA |
| Break-even basis points on turnover | NA |

No Groww/net result is fabricated. The CSV has enough theoretical price/quantity data for turnover, but not actual fills, brokerage plan, statutory charge bases, or order-level taxes. If gross is positive, the displayed break-even friction is the maximum all-in round-trip cost the day could absorb; if gross is negative, no positive cost assumption can restore profitability.

## 15. Code-to-evidence mapping and classified findings

### Risk-budget overshoot

- **Evidence:** 35 trades lost more than ₹500; worst was 9.00× intended risk.
- **Responsible Pine behavior:** Realtime stop checks current close and exits at close minus a tick (420–433), not at the stop.
- **Classification:** EXPECTED PINE/TRADINGVIEW BEHAVIOR + STRATEGY DESIGN WEAKNESS
- **Next falsifiable test:** Log ticks/alert receipt/broker fills to separate gaps, feed cadence and delivery latency.
- **Status:** NOT YET A RECOMMENDED CHANGE.

### Repeated entries

- **Evidence:** First entries ₹-4,781.75; re-entries ₹-16,365.39 across 127 trades.
- **Responsible Pine behavior:** No daily lockout; a new later confirmed PSAR flip may buy after exit (306–313).
- **Classification:** INTERESTING HYPOTHESIS — NEEDS MORE DAYS
- **Next falsifiable test:** Repeat the same frozen cohort comparison across forward sessions.
- **Status:** NOT YET A RECOMMENDED CHANGE.

### Rapid exits

- **Evidence:** 107 exits inside five minutes, including 23 inside 30 seconds.
- **Responsible Pine behavior:** Entry-bar management is suppressed, but next-bar realtime updates can immediately hit stop (401–433).
- **Classification:** STRATEGY DESIGN WEAKNESS / EXPECTED BEHAVIOR
- **Next falsifiable test:** Capture tick-level first-next-bar path and raw PSAR/ATR at entry.
- **Status:** NOT YET A RECOMMENDED CHANGE.

### Orphan exits

- **Evidence:** 5 EXIT alerts lack an earlier BUY in the export.
- **Responsible Pine behavior:** Historical state may be reconstructed before realtime alerts; export starts 09:40 IST.
- **Classification:** DATA LIMITATION / EXPECTED PINE BEHAVIOR
- **Next falsifiable test:** Record alert creation/reload timestamps and persistent trade IDs.
- **Status:** NOT YET A RECOMMENDED CHANGE.

### Liquidity gating

- **Evidence:** PASS=154, FAIL=244, UNKNOWN=0; all BUYs PASS.
- **Responsible Pine behavior:** Causal prior-bar session baseline at 147–165; only PASS enters at 309.
- **Classification:** NO ISSUE FOUND; COUNTERFACTUAL DATA MISSING
- **Next falsifiable test:** Track hypothetical outcomes for FAIL/UNKNOWN without trading them.
- **Status:** NOT YET A RECOMMENDED CHANGE.

### Exit alert correctness

- **Evidence:** 0 logged P&L values differ from price×qty by >₹0.01 after exact CSV reconstruction.
- **Responsible Pine behavior:** Exit P&L is `(exitPrice-assumedEntry)*qty` at 494–507.
- **Classification:** NO ISSUE FOUND
- **Next falsifiable test:** Continue schema-level reconciliation.
- **Status:** NOT YET A RECOMMENDED CHANGE.

### PSAR quality

- **Evidence:** At least 395 unique eligible flat-state bullish-flip episodes; 16 exits on bearish fallback.
- **Responsible Pine behavior:** Confirmed flip definitions at 40–45; no raw PSAR telemetry in payload.
- **Classification:** DATA LIMITATION / INTERESTING HYPOTHESIS
- **Next falsifiable test:** Log PSAR level, direction, flip sequence and chop-regime features causally.
- **Status:** NOT YET A RECOMMENDED CHANGE.

### Parallel alert definitions

- **Evidence:** 3 same-symbol/event/second collisions, all ARVEE SKIPs from 10/20-bar lookbacks.
- **Responsible Pine behavior:** Independent TradingView alert definitions run the same script with different inputs.
- **Classification:** EXPECTED OPERATIONAL BEHAVIOR / POSSIBLE CONFIGURATION ISSUE
- **Next falsifiable test:** Inventory intended alert definition per symbol and flag parallel configurations prospectively.
- **Status:** NOT YET A RECOMMENDED CHANGE.

### Day/session state resilience

- **Evidence:** No Sep 28 lifecycle proves an overnight carry, but five orphans cannot be fully attributed.
- **Responsible Pine behavior:** New-day detection resets liquidity only; position state depends on an executed exit, normally EOD.
- **Classification:** STRATEGY DESIGN WEAKNESS — NOT OBSERVED AS A CONFIRMED FAILURE
- **Next falsifiable test:** Force a missed-EOD/reload forward test with run IDs and verify next-session state.
- **Status:** NOT YET A RECOMMENDED CHANGE.

No confirmed Pine logic defect is established by this one-day export. The strongest mechanical issue is not a sizing error: it is the expected difference between intended stop risk and an alert-based market reference after price has crossed that stop.

## 16. Ranked research questions for next forward sessions

1. Do stop-reference overshoots persist with tick receipt time and broker-fill evidence, and are they concentrated by symbol/liquidity?
2. Does re-entry expectancy remain different from first-entry expectancy across frozen forward days?
3. Are sub-five-minute losses associated with high recent PSAR flip counts when that count is logged causally?
4. What are shadow outcomes of FAIL and UNKNOWN liquidity candidates under the same exit model?
5. Which exit mechanisms remain positive after actual fills and all-in costs, not theoretical references?
6. How often do alert/chart reloads create orphan lifecycles, and can a persistent run/trade ID resolve them?

# CANDIDATE CHANGES — NOT IMPLEMENTED

| Observed problem | Evidence | Proposed hypothesis | Potential benefit | Potential downside | Evidence needed |
|---|---|---|---|---|---|
| Realized losses can exceed intended risk | Loss/risk ratios above 1× and the current-close exit path | Use execution-aware stop handling or explicitly model alert slippage | Tighter correspondence between budget and deployed risk | Resting orders can fill differently; more implementation complexity | Broker/tick evidence showing systematic actionable overshoot |
| Re-entry expectancy may differ from first entries | First ₹-4,781.75 vs re-entry ₹-16,365.39 today | Add re-entry telemetry/cooldown shadow variants | Can identify chop without changing production behavior | One-day selection bias and missed recovery moves | Several frozen forward sessions with stable cohort gap |
| Rapid invalidations may indicate PSAR chop | 107 trades under five minutes | Log prior flip count, PSAR distance, ATR and causal regime state | Separates bad entry from correctly managed noise | More payload volume; telemetry may not generalize | Prospective relationship between entry context and rapid loss |
| Orphan lifecycle attribution is ambiguous | 5 orphan EXITs | Emit run ID, trade ID, script build ID and alert-created timestamp | Deterministic pairing across reloads | Operational/schema migration overhead | Replay/reload tests and zero unexplained orphans |
| Liquidity gate value is unmeasured | 244 FAIL candidates skipped, no outcomes | Add non-trading shadow outcome tracking for skips | Measures selection value without removing the gate | Requires causal outcome collector | Forward PASS-vs-FAIL expectancy with identical horizon/exit semantics |

None of these changes was made. No Pine file, parameter, stop width, re-entry rule, indicator set, or liquidity rule was modified.

## Reproducibility

```bash
python3 analysis/psar_v2_sep28_forensics.py
```

Primary per-trade evidence is in `analysis/PSAR_V2_SEP28_TRADES.csv`.

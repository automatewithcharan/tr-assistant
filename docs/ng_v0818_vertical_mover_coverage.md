# NG v0.8.18 vertical-mover coverage audit

Scope: NPST and INDIAMART on 2026-09-16, with the screenshots treated only as
supporting evidence. No later candle is used to invent missing Pine state.

## Finding

This is primarily intended strategy behavior plus an observability gap. Lane A
is a pullback strategy. Lane C is stricter than its dashboard label suggests:
the first candle must pass every momentum, risk, and room gate, then a later
confirmed bullish candle must close at or above `cEntry`. Lane D ending at
10:30 IST does not close the general entry window. Lane E remains shadow-only
unless its existing actionable switch is deliberately enabled.

F0 had a research-coverage mismatch, not an actionable trading bug. Its
`f0StudyStartedToday`/`f0StudyDone` latch allowed exactly one viable observation
per runtime day. An earlier observation could therefore suppress a distinct
later vertical episode. The available screenshots and local bars do not reveal
whether that actually happened on either symbol; only prospective F0 telemetry
can establish it.

The shadow study now permits sequential distinct episodes. Once an F0 sample
finishes, it requires the existing `FAST_REARM_BARS` count of non-qualifying
bars and respects the existing `FAST_MAX_WATCHES_DAY` cap. Bars while a sample
is active do not count toward rearm. This changes F0 research collection only.

## Causal bar boundary

- At NPST 10:33 IST, the live 10:30-10:35 candle was still forming. The last
  admissible decision candle was the 10:25-10:30 candle, closing `1891.00`.
- At INDIAMART 10:29 IST, the live 10:25-10:30 candle was still forming. The
  last admissible decision candle was the 10:20-10:25 candle, closing `1683.60`.
- INDIAMART's subsequently confirmed 10:30 close was `1698.60`; that value
  cannot be moved backward into the 10:29 decision.

The local external bar capture is useful for OHLC arithmetic but is not a
record of TradingView's ATR, EMA, VWAP, benchmark series, completed-volume
baseline, `varip` state, or alert snapshot.

## Decision paths

### NPST around 10:33

For the confirmed 10:25-10:30 candle: O/H/L/C was
`1883.00/1894.00/1882.50/1891.00`. It closed above the preceding eight-bar high
(`1885.50`), its body was `69.57%` of range, and its close position was
`73.91%`. Therefore the first demonstrable Lane-C candle-quality blocker is
the frozen 75% close-position gate. Exact earlier predicates such as
`contextGood`, TradingView ATR, and completed-volume ratio are not captured, so
this does not prove that weak close was the runtime's first blocker.

The path by lane is:

- A: an expansion can only move A to `WAIT PB`; it cannot buy the vertical
  expansion itself. A later controlled first pullback and rebreak are required.
- C: `SCAN -> momentum gates -> risk/room -> ARMED -> later confirmed close >=
  cEntry -> BUY`. The known candle fails close position before arm; if a prior
  candle armed C, the next close still had to satisfy the separate continuation
  trigger and entry-time context/overshoot/risk/quantity checks.
- D: at 10:33, `OPEN DONE` is expected because D ends at 10:30. It is not a
  global entry cutoff; `entryWindowOpen` continues to 15:00.
- E: after 10:30 it may seek its existing pullback/base re-acceleration setup.
  With the default configuration it is shadow-only.
- F0: a direct shadow launch would require confirmed bullish close, open entry
  window, `ec0CoreNoDayMove`, fresh eight-bar breakout, no live trade,
  `freshSetupAfterBar`, valid ATR, a positive structural risk, and quantity at
  least one. The OHLC proves bullish close and breakout only. The screenshot
  cannot establish the remaining signal-time state or whether an earlier F0
  sample had consumed the old daily latch.

For completeness, the later 10:30-10:35 NPST candle in the local capture closed
`1912.00`, but it was not confirmed at 10:33 and is excluded from that decision.
It too had a `72.39%` close position, below Lane C's frozen 75% gate.

### INDIAMART around 10:29

For the confirmed 10:20-10:25 candle: O/H/L/C was
`1659.70/1687.80/1659.70/1683.60`. It closed above the preceding eight-bar high
(`1665.60`); body and close position were both `85.05%`. Those observable gates
pass. The first runtime Lane-C blocker remains unknown because no prospective
Pine record captured context, ATR/range, completed-volume ratio, RS, EMA
extension, competing lane state, risk, or room. `C: SCAN` alone does not encode
which one failed.

The path by lane is:

- A: the screenshot's `WAIT PB` is intended. A had recognized expansion and
  was waiting for the controlled first pullback; the strategy deliberately
  does not chase a vertical candle.
- C: the observable breakout/body/close gates pass, but no record proves all
  remaining gates or an arm. If armed, C would still wait for a later confirmed
  bullish close at or above `high + mintick`.
- D: at 10:29 its opening window had not yet ended, but `SCAN`/no BUY means its
  independent opening-drive state machine had not produced an actionable
  trigger. The screenshot cannot identify its first failed predicate.
- E: its window begins when D's 10:30 window ends, so it was not the 10:29
  direct-entry route. It remains shadow-only by default afterward.
- F0: the candle is a plausible direct-shadow candidate, but qualification and
  prior-consumption status are unknown without the missing prospective record.

The forming 10:25-10:30 INDIAMART candle later closed `1698.60`. Its body was
`62.77%`, below Lane C's frozen 65% body gate. That fact describes the 10:30
decision only, not the earlier 10:29 screen.

## Gate-by-gate forensic ledger

`UNKNOWN` means the value was not captured at signal time. It is intentionally
not reconstructed from the next candle or from a later dashboard state.

| Lane-C gate/state | NPST confirmed 10:30 | INDIAMART confirmed 10:25 |
|---|---|---|
| `barstate.isconfirmed` / entry window | PASS / PASS | PASS / PASS |
| `contextGood` | UNKNOWN | UNKNOWN |
| bullish close | PASS | PASS |
| close above prior eight-bar high | PASS (`1891 > 1885.5`) | PASS (`1683.6 > 1665.6`) |
| `cAttemptBar` | UNKNOWN: context missing | UNKNOWN: context missing |
| ATR available | UNKNOWN | UNKNOWN |
| range >= 1 ATR | UNKNOWN | UNKNOWN |
| body >= 65% | PASS (`69.57%`) | PASS (`85.05%`) |
| close position >= 75% | FAIL (`73.91%`) | PASS (`85.05%`) |
| confirmed-bar volume >= 2x | UNKNOWN | UNKNOWN |
| intraday RS >= 1% | UNKNOWN | UNKNOWN |
| EMA extension <= 2 ATR | UNKNOWN | UNKNOWN |
| `cMomentumQuality` | FAIL regardless of unknowns: weak close | UNKNOWN |
| `freshSetupAfterBar` | NOT REACHED / UNKNOWN | UNKNOWN |
| competing A/B/D/E live trade | NOT REACHED / UNKNOWN | UNKNOWN |
| structural risk <= 0.90 ATR | NOT EVALUATED | UNKNOWN |
| room to resistance >= 1R | NOT EVALUATED | UNKNOWN |
| arm transition to `cState == 2` | NO from this candle | UNKNOWN |
| later bullish close >= `cEntry` | NOT APPLICABLE to this failed arm | UNKNOWN; required only if armed |

| F0 gate/state | NPST confirmed 10:30 | INDIAMART confirmed 10:25 |
|---|---|---|
| confirmed bullish action bar / entry window | PASS / PASS | PASS / PASS |
| `ec0CoreNoDayMove` | UNKNOWN | UNKNOWN |
| fresh eight-bar breakout | PASS | PASS |
| no live A/B/C/D/E trade | UNKNOWN | UNKNOWN |
| after `freshSetupAfterBar` | UNKNOWN | UNKNOWN |
| old `startedToday` / `active` / `done` latch | UNKNOWN | UNKNOWN |
| ATR and positive structural stop risk | UNKNOWN | UNKNOWN |
| structural `calcQty >= 1` | UNKNOWN | UNKNOWN |
| final F0 launch | UNKNOWN | UNKNOWN |

Thus NPST has an OHLC-provable Lane-C failure, while INDIAMART does not. Neither
F0 result can be labeled qualified, blocked, or previously consumed from the
available evidence.

## State/reset audit

- `freshSetupAfterBar` is reset on a new day and advanced by actual lane trade
  events. It prevents same/older-bar reuse; no defect was found.
- `cState` arms only after the full momentum plus risk/room checks, and the live
  BUY remains a second confirmed-close trigger. Fresh-load protection discards
  a stale READY whose trigger was already crossed; no defect was found.
- Old `f0StudyStartedToday` and `f0StudyDone` correctly implemented one sample
  per runtime day, but that policy was unsuitable for multi-episode research.
- F0 and the retained confirmed diagnostics reset on a new session or non-5m
  chart. A script/chart reload creates a new Pine runtime. Because F0 is
  deliberately realtime-only, a reload cannot reconstruct an earlier sample
  from historical bars and can allow a duplicate later observation. A
  server-side alert snapshot has its own independent runtime. This is a Pine
  lifecycle limitation, not evidence about either screenshot.

## New prospective output

The volume row now distinguishes unlike quantities:

```text
Pace 5.27x | Conf 1.42x | Peak 16.74x
```

The Why row retains the important confirmed fast-mover decision instead of
letting a forming candle replace it. Examples include:

```text
C BLOCKED: EXT 3.40 ATR @ 10:30 | F0 BLOCKED: EC0 CONTEXT
C BLOCKED: CONF VOL 1.42x @ 10:30 | F0 BLOCKED: REARM 1/3 / LAST 09:35
C ARMED @ 1882.10 / WAIT 2ND CLOSE | F0 DIRECT SHADOW @ 1810.40 / EP 1
```

The strings are examples of format, not reconstructed NPST/INDIAMART results.
F0 research alerts now include the episode number. Thresholds and all
actionable A/B/C/D/E transitions remain unchanged.

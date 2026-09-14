# Prospective timing funnel research contract v1

This offline experiment asks where causal evidence first appeared, how long
each later stage took, and how much opportunity remained. It compares both
explanations: early evidence may be useful and delayed downstream, or early
evidence may be noisy enough that later qualification is necessary.

The frozen stage order is:

1. `SOURCE_EVALUATED`: first proven SignalMonitor source evaluation.
2. `PRECURSOR_QUALIFIED`: first existing anomaly, pre-move, early-breakout, or
   top-gainer-radar qualification.
3. `BUY_NOW_BOARD_QUALIFIED`: first Buy Now board `SOURCE_QUALIFIED` event.
4. `GOAL_MODE_DECISION`: explicit goal-mode acceptance or rejection. The
   current CSV proves selected/accepted rows only; an absent row is unknown.
5. `DISCORD_ATTEMPTED`: explicit transport attempt evidence.
6. `DISCORD_SUCCESSFUL`: explicit successful transport evidence. Message kind
   distinguishes a board digest, a trade notification, and a manual
   TradingView-candidate message.
7. `TRADINGVIEW_CANDIDATE_ELIGIBLE`: explicit evidence that the operational
   Pine/TradingView path became eligible. No current journal proves this stage.
8. `TRADINGVIEW_ALERT`: an optional authoritative TradingView/Pine receipt.
   Candidate delivery never manufactures this stage.

The opportunity identity is a hash of contract version, NSE trading date, and
normalized symbol. Stronger lifecycle IDs remain attached to events. No join
crosses dates, no raw repeat is discarded, and first-per-stage rows exist only
in derived analysis. Research deduplication creates no runtime trading rule.

Every stage uses its explicit causal price where recorded. Otherwise its
reference may be the close of the last one-minute candle that had fully
completed by the event time. The containing candle is forbidden. Outcomes use
only minute bars with timestamps strictly after the evaluated event. Complete
coverage must be continuous through the 15:29 IST candle; missing or gapped
coverage makes excursion and threshold labels unavailable.

Runtime coverage is independent of funnel absence. Timing and live-feature
journals can prove observed intervals and gaps. Missing board, goal-mode, or
Pine evidence remains `unknown`; it is never counted as a strategy rejection.

Current evidence limitations:

- goal-mode rejection reasons are transient and lack a complete immutable
  decision journal;
- the goal alert CSV is written before transport and cannot prove delivery;
- direct candidate `detected_at` is treated as precursor discovery. Its delivery
  proves a Discord manual-review message, not Pine eligibility or a later alert;
- no authoritative Pine receipt journal is currently present;
- logs can help a forensic audit but are not silently promoted into structured
  stage evidence.

Sessions inspected while building or tuning this analyzer are exploratory.
Freeze code and inputs before the next clean eligible NSE session, then examine
confirmation results only after the preregistered collection window ends.

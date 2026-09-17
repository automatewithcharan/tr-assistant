# NG Discord Persistent Guard v2.3c

Use [NG_Discord_Persistent_Guard_v2_3c_MOMENTUM_WATCH_FIX.pine](../NG_Discord_Persistent_Guard_v2_3c_MOMENTUM_WATCH_FIX.pine) on a **1-minute NSE chart**. This is a replacement for the user-supplied v2.3b script.

## What caused the silence

The v2.3b script only sent BUY and SELL by default. Its REARM and SKIP notifications were disabled. A strong, extended 1-minute impulse was routed to pullback mode, which demanded a completed pullback below the seed close followed by a reclaim within five bars. A stock could keep climbing without satisfying that sequence, so no TradingView message was expected. We cannot attribute AWHCL's exact bars to one gate without its TradingView alert log and 1-minute chart at the time.

There was also a separate exit delivery bug: SELL decisions could occur during a live bar, while the `alert()` call allowed delivery only on the bar's closing iteration. The script could change to cooldown before that closing iteration and never send the SELL.

## Changes

- One **NG MOMENTUM WATCH** per alert instance per trading day after a confirmed 1-minute new high, a green candle, price above VWAP or the confirmed 5-minute EMA, and a gain of at least 1% from the prior close. This observation bypasses the BUY-only extension, pullback, room, and economics gates. It is explicitly *not a buy signal*.
- A BUY still requires the original risk and entry rules. No extended-price BUY is forced.
- Intrabar SELL uses `alert.freq_all` with the state machine's own one-time exit logic, so an exit that occurs before the bar close is sent.
- Exit management starts only on the bar after BUY. Stop and TP1 checks use the current traded price, avoiding earlier highs/lows in the same bar falsely triggering a newly raised stop or target.

## TradingView setup

1. Paste v2.3c into Pine Editor, save it, add it to an **NSE:AWHCL 1-minute** chart, and confirm the dashboard says `v2.3c` and `1m ENGINE / 5m CONTEXT`.
2. Delete the old v2.3b alert, then create a new alert with **Condition → NG Discord Persistent Guard v2.3c → Any alert() function call**. Keep `Enable script alerts` and `Alert on fresh run-up` enabled.
3. Set the alert's expiration and notifications/webhook in TradingView. Check that it is active. Each other symbol needs its own alert.

TradingView saves a copy of the script and settings when the alert is created. Updating the Pine Editor does not update an already running alert. The script runs from its alert creation time and does not replay earlier AWHCL opportunities.

The source has been reviewed for the relevant state transitions and alert frequencies. A TradingView Pine compiler and an active user TradingView session were unavailable here, so the script must be compiled and its new alert created in TradingView before it can deliver messages.

# NG review shortlist

The `ng-quality-watch` Discord channel previously received one raw alert per
Leader Ignition and P5 observation. On September 16, 2026, that meant 120
opening alerts in about five minutes and another 69 after 09:30 IST. The
shortlist changes notification delivery without changing scanner qualification.

## Live delivery

Run both existing publishers with `--archive-only`. They still record every
qualified event in their existing evidence journals, mark its raw notification
`ARCHIVED_FOR_DIGEST`, and never post that individual message. This terminal
state prevents a restart from resurrecting the raw notification.

Run `scripts/ng_watch_digest.py` separately using the existing
`NG_QUALITY_WATCH_DISCORD_WEBHOOK_URL`. At 09:20 IST it reads Leader Ignition
`OBSERVED` events and sends at most two opening watch symbols. At 09:45 IST and
every 30 minutes until 15:15, it reads post-09:30 P5 `OBSERVED` events and
sends at most one digest with new symbols. The opening symbols consume two of
the five distinct-symbol slots available per IST session. A slot with no
eligible events sends nothing. Both source journals are retained in full.

## Review filter

Each candidate must have at least ₹5 Cr turnover, a spread of at most 20 basis
points, a buyer/seller ratio of at least 1.2, and a move from the open between
0% and 3%. Candidates older than 30 minutes are not included. A deterministic
score orders eligible candidates by rank improvement, buyer/seller ratio,
turnover, move acceleration, spread, and distance from the open. Missing inputs
fail the review filter. The opening digest requires at least ₹5 Cr turnover,
buyer/seller ratio at least 1.4, a 0.5–2% move from the open, day change no
more than 3%, and pullback no more than 0.5%. It uses the scanner's existing
Leader Ignition score to rank opening candidates. These rules manage attention;
they have **not** been
validated as a profitable entry strategy. The Discord message says
`NOT A BUY SIGNAL · PAPER / SKIP`.

On a replay of the September 15 and 16 evidence, the policy selected two
opening symbols on each day, then zero and three later symbols respectively.
This replay checks notification volume, not investment performance.

## State and safety

The digest writes a daily `state.json` and append-only `events.jsonl` under
`results/Data/ng_watch_digest/YYYY-MM-DD/`. It records selected event IDs,
delivery outcome, and sent symbols. It never places orders or edits Pine alerts.
If the webhook is missing or delivery fails, the slot is not consumed and the
digest can retry. A Discord success followed by a process crash before state
save may cause one repeated message; the journal makes that visible.

The two archive publishers and the digest should be launched before 09:15 IST.
Start the digest with:

```bash
.venv/bin/python scripts/ng_watch_digest.py \
  --p5-state-root results/Data/ng_p5_early_watch_shadow \
  --leader-state-root results/Data/ng_leader_ignition_watch \
  --state-root results/Data/ng_watch_digest
```

The publisher commands gain `--archive-only`. Remove the old raw Discord
webhook routing from their service units when enabling the digest. Stop the
digest with the publishers after market close.

# One Process May Freeze a Funded Contract

**Status:** Design for operator approval, 2026-08-26. Not built.

Question 3 answered (EVM: same treatment). Question 1 (manual-buy latency) is with the operator and
BLOCKS the build — Alpha recommends accepting ~one poll loop, ~5s. Question 2 (TTL) follows from it.

## The defect

A funded contract is immutable by design: it freezes the exit rails a position will live under, so
no later config edit can change what an open position does. That guarantee holds only if every
process that writes a contract reads the same policy.

Two do not.

```
44704  follow_activity  14:45:39  follow-30-50-v2  max_hold 14400  voldeath 60
44705  follow_activity  14:53:47  follow-30-50-v2  max_hold 14400  voldeath 60
44706  manual           15:01:01  follow-30-50-v1  max_hold   780  voldeath —
```

44706 opened 7 minutes after 44705, under yesterday's rules. The timecap lift activated at 14:33.

**Root cause:** `sniper/settings/memecoin.py` captures every knob from `os.getenv` at IMPORT time,
so a long-lived process holds a snapshot taken when it started. Deploys restart the runner. Nothing
restarts the web server, and `manual_buy_route` builds contracts inside it. That web process had
been up since 2026-08-25 10:56, so every manual entry after any config change froze stale rails.

The same staleness explains the 14:57Z "insufficient bankroll" refusal on a wallet holding $108:
the balance read that collapsed to zero was the WEB process's, and the fix for it (`4bad5e5c`)
only takes effect in a process that has restarted since.

**This is not a one-off.** It recurs on every config change, silently, and only on the manual path:

| | |
|---|---|
| real positions, manual path | 14 of 417 (3.4%) |
| manual entries 2026-08-26 | 5 |
| manual entries 2026-08-24 | 5 |

Low volume is the only reason this has not cost money. It is not a control.

**Scope note.** `build_funded_contract` has exactly ONE call site in code
(`memecoin_risk.py:447`), plus the EVM sibling in `memecoin_evm_risk`. The single construction
POINT was achieved. The missing property is a single construction PROCESS.

## Why "restart the web server too" is the wrong fix

Option (b) — teach the deploy script to restart the web server when funded-relevant settings change
— requires a maintained list of which settings are funded-relevant. That list is a second copy of a
fact the code already knows, free to drift, and every drift is silent. This session found four
defects of exactly that shape: an accepted-band tuple that never matched the writer's domain, a
censor calibrated on a distribution the tape does not have, a hard-coded policy-id tuple a rename
invalidated, and a quality column the replay did not filter. Adding a fifth to fix the fourth is
not a trade worth making.

It also leaves the real failure untouched: the web process has its own RPC path, and that is what
actually broke at 14:57.

Option (c) — re-read settings per request on the funded surface — keeps two readers, so it removes
staleness but not divergence, and it needs a live settings reload inside a serving process.

## Proposal: manual intents enqueue, only the runner builds contracts

The web server stops constructing funded contracts. `manual_buy_route` writes an INTENT row and
returns immediately. The runner claims intents in its own loop, with its own settings, its own RPC
pool and its own submission stack, and builds the contract exactly as it does for a feed signal.

**The mechanism already exists and is already funded-proven.** `onchain_wallet_detections` is a
table the detection daemon writes and the runner sweeps by monotonic id
(`sweep_onchain_detections`, `memecoin_runner_loop.py:552`), handing each row to `real_hook` — the
same hook the feed path uses. A manual intent is the same shape. No new IPC, no socket, no new
transport.

### One deliberate difference from the onchain lane

The onchain sweep advances its cursor past a row whose hook raised: *"a detection is a moment, not
a queue item worth retrying late."* Correct there — a stale detection is worthless.

**A manual intent is the opposite.** The operator asked for it explicitly, and silently dropping it
is worse than filling it late. So the manual queue is DURABLE STATE, not a cursor:

| state | meaning |
|---|---|
| `queued` | written by the web, not yet claimed |
| `claimed` | one runner pass owns it; a claim id and a claim timestamp |
| `filled` | a position exists; the row carries its id |
| `refused` | the gate said no; the row carries the exact reason string |
| `expired` | not claimed inside its own TTL; never silently retried |

The web UI reads the row back, so the operator sees `refused: m5 volume under floor` instead of a
spinner. That is strictly better than today, where the refusal reaches a log and not the person who
clicked.

### Properties to hold

1. **The web never imports the contract builder.** Enforced by a grep guard test, the same shape as
   the versioned-policy-id guard from `1fb00741`. This is the property; everything else is
   mechanism.
2. **An intent carries the operator's INPUTS, never a decision** — mint, size override, wallet.
   No policy, no clip, no rails. If the web could name a rail, the divergence would come back
   wearing a different hat.
3. **A TTL, and expiry is a state.** An intent the runner never claims must expire visibly. A
   manual buy that vanishes because the runner was down is the failure mode this design introduces,
   so it gets a named state and an operator-visible one, not a silent drop.
4. **Latency is bounded by the runner's loop, and is stated.** Manual entries stop being instant.
   That is the cost, and it must be quoted honestly before approval rather than discovered.
5. **The EVM sibling gets the same treatment or an explicit exemption.** `memecoin_evm_risk` has
   its own `prepare_funded_contract` path; a fix that covers Solana only leaves the same hole on a
   dormant lane, which is how dormant lanes come back armed and wrong.

### What this does NOT fix

Any OTHER long-lived process that imports `sniper.settings.memecoin` still holds a snapshot. This
proposal removes the one that writes funded contracts; it does not make the settings module
reload-safe. If a second stale-config defect appears on a different surface, the general fix is a
settings accessor rather than module constants, and that is a much larger change than this one.

## Verification before it is believed

- A test that a manual intent placed while the web holds STALE settings still produces a contract
  built from the runner's CURRENT settings. That is the whole point and it must fail if the wiring
  regresses.
- A grep guard: no route module imports `build_funded_contract` or `prepare_funded_contract`.
- Claim-once under two concurrent runner passes.
- Expiry is reported, never silently dropped.
- The refusal reason reaches the row, not only the log.

## Open questions for the operator

1. **Is bounded latency acceptable on a manual buy?** It becomes one runner loop rather than
   immediate. If manual entries must stay instant, option (a) is wrong and the honest fallback is
   (b) plus accepting the drift risk.
2. **TTL length.** How long may an unclaimed manual intent stay valid before it expires rather than
   fills a coin that has moved?
3. ~~**EVM now or later**, given the lane is dormant.~~ **ANSWERED 2026-08-26 (Alpha): SAME
   TREATMENT, no exemption.** A dormant lane with a known hole is a scheduled incident — today's
   `out_of_scope` rails defect is the proof pattern, a dormant path that came back armed. The
   WRITER side ships now (the web never builds an EVM contract). If the EVM queue consumer is dead
   code today, it may be the same table with a consumer the EVM runner grows when that lane
   revives.

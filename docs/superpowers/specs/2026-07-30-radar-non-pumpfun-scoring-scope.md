# Radar non-pump.fun scoring — scope, not a build

**Status:** Scope only. Nothing here is approved for implementation. Written 2026-07-30 after the
observation that every alert the Radar has ever scored is pump.fun.

## The finding that prompted this

4,991 retained alerts. **All pumpfun_solana. Zero from bags_solana, bags_robinhood_4663, or
pons_v1_4663** — while Pons alone has grown past 7,900 captured launches.

So "do the launchpads behave the same?" is not a question the Radar can currently answer, and every
conclusion in this repo about launch behaviour is a pump.fun conclusion wearing a general label.

## Why the other venues produce nothing — three independent blocks

1. **The arms are hard-gated to pump.fun.** `evaluate_forward_arm` and `evaluate_confirmation_arms`
   both return immediately unless `venue_id == "pumpfun_solana"`
   (`directional.py:132`, `directional.py:209`). By design — Pump thresholds were never meant to be
   inherited by other venues.
2. **The quote path is separate for each venue.** The remote probe routes `PUMPFUN_SOLANA` and
   `BAGS_SOLANA` to Jupiter. `BAGS_ROBINHOOD_4663` and Pons use their 4663 quote paths.
   **2026-08-07 amendment:** The Pons venue design replaced the prior
   `REMOTE_SIMULATION_UNAVAILABLE` limit.
3. **Pons has no curve at all.** It is an immediately active Uniswap V3 pool, lifecycle
   `active_pool`. The approved readiness design forbids synthesizing curve reserves, progress,
   migration or launch arms, and holds its rows at `UNSUPPORTED_CURVE_MATH` until a separately
   designed V3 state and executable-quote model exists
   (`2026-07-29-radar-pons-readiness-design.md`).

Removing block 1 without solving 2 and 3 would produce alerts with no executable price behind them.
That is worse than no coverage.

## So why capture them at all?

Not churn, but the value is currently potential rather than realised:

- **bags_robinhood_4663** — has a working probe and is the nearest to scoreable. Its block is the
  arm gate plus the absence of a venue-appropriate rule.
- **pons_v1_4663** — deliberately capture-only per its readiness design. The capture is the
  prerequisite for the V3 model, and it is cheap: it rides the same worker and writes canonical
  evidence. It buys the option to answer the venue question later without a historical gap.
- **bags_solana** — thin (tens of launches). Hard to justify on its own; it exists because the
  Solana lane already carried it.

The honest summary: capture is earning an option, not a result. That is defensible while the option
is actually exercised. It stops being defensible if the V3 model is never designed — at which point
the capture should be retired rather than left running as decoration.

## What a scoped build would need

**Stage 1 — make one non-Pump venue scoreable end to end.** Pick `bags_robinhood_4663`: it already
has a probe, so it isolates the rule problem from the quote problem.

- A venue-appropriate entry rule. Pump thresholds (curve progress ≥20%, 10–60s age bands) are
  meaningless off a bonding curve and must not be inherited.
- A control for that venue, disjoint in the same way `route-control-v2` is.
- Its own evaluator version and its own clock. Never pooled with pump.fun — the whole regime-split
  lesson applies with more force across venues than across time.

**Stage 2 — Pons V3 state and executable quote.** Per its readiness design, needs: pool state from
registered-pool Swap evidence (sqrt price, liquidity, tick, token ordering — already captured), an
executable quote model against that pool, and an exit-side simulation. Only then can a rule exist.
`progress_bps` has no meaning here and no synthetic substitute is permitted.

**Stage 3 — only then** ask whether venues behave alike, with per-venue cohorts that are never
pooled.

## What this scope explicitly rejects

- Un-gating the Pump arms onto other venues. Different microstructure, different thresholds; the
  thresholds are not transferable and inheriting them would manufacture noise.
- Synthesizing curve progress for Pons to make existing rules typecheck.
- Treating a captured launch count as coverage. Capture without an executable quote is evidence for
  research, not a scoreable opportunity.

## Open operator decisions (added 2026-08-03)

Neither of these has an owner or a date. Recorded so they are refused deliberately rather
than forgotten.

1. **No deadline exists for Stage 1/2.** This document says the capture "stops being
   defensible if the V3 model is never designed" but sets no date at which that judgement
   gets made. Until an operator sets one, the capture continues by default, which is the
   failure mode the sentence was written to prevent. This is a prioritisation call, not a
   technical blocker.
2. **`bags_solana` launch capture cannot use the live socket.** *(Corrected 2026-08-03 —
   the first version of this item said Bags had produced no event since 2026-07-30, that
   its 900-second event expectation was correct, and that the cause was unknown. All three
   were wrong; see below and PRs #303/#304.)*

   Measured 2026-08-03 before any change: the transport, the pool subscriptions and the
   feed were all healthy — 25/25 subscriptions accepted, feed returning 100 launches each
   carrying a `dbcPoolKey`. What was actually true is that **Bags is a low-cadence venue**:
   0 of 12 sampled feed pools had traded within the hour and their last on-chain activity
   ranged from 3 to 17 hours earlier. The 900-second expectation was therefore guaranteed
   to trip, and it reported a working socket as a dead one. (The Meteora DBC *program* was
   busy over the same window — 317 notifications in 75s — but that program serves several
   launchpads and says nothing about whether the monitored Bags pools were active.)

   The real limitation is structural and remains open: **a pool-level subscription can
   never observe the launch that creates the pool**, because the pool address is only
   learned from the feed afterwards. Launches are recoverable only through
   `opportunity:bags_feed_recovery`, which backfills by `launchSignature`. That path is
   confirmed working — a launch at 2026-08-03T19:38:28Z was backfilled and its subsequent
   trades were then captured by the socket.

   Program-level subscription is the obvious alternative and is **not** a drop-in: the DBC
   program's measured ~4.2 notifications/s each require a `getTransaction`, against a
   0.25 rps governor. Sizing that is part of any redesign, not an afterthought.

   Bags still contributes zero scoreable alerts. Retire or redesign is an operator
   decision; the health grading is now honest either way.

## Cost note

Stage 1 is bounded and worth scoping properly before committing. Stage 2 is a genuine subsystem —
a V3 pricing model with exit simulation — and should not be started on the strength of "we already
capture the events".

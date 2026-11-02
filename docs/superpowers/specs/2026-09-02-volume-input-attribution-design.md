# Volume-input attribution — design

Date: 2026-09-02
Author: Beta
Status: DESIGN ONLY. No implementation, no gate change proposed here.
Requested by: Alpha, as the surviving half of `_route_volume_refusal`.

## The question

A funded entry gate reads an m5 volume figure. **Does that volume belong to the pool the trade
would actually route through?** Nobody has been able to answer it, and the guard that once tried
was deleted for good reason (see "What not to rebuild").

## Why the two can differ, structurally

`select_dex_pair` (`sniper/execution/solana_quote.py`) picks **one** DexScreener pair per mint,
ranked by quote-side depth, filtered only on chain identity and `baseToken == mint`. Its
`volume.m5` becomes the gate input.

The executed route is chosen separately, later, by the venue quote ladder (V3/V2/V4, Relay, GMGN,
Pons on 4663; Jupiter on Solana). Nothing connects the two selections. The gate reads the deepest
pool; the trade uses whichever pool the router prices best. **They agree by coincidence, not by
construction.**

## What is measurable today, and what is not

Both halves are recorded, but only on one chain do they denominate the same thing.

| | recorded | comparable to a pool? |
|---|---|---|
| gate's pair | `classification.facts.liquidity_pair_address` | yes |
| executed route, solana | `memecoin_entry_attempts.quote_route_target` | yes — comma-joined AMM keys |
| executed route, 4663 | same column | **no — it holds the ROUTER** |

On 4663 that column carries **6 distinct values across 110 mints** (universal_router `0x8876…`,
Relay proxy `0xccc88a9d`, GMGN `0x65050a9b`, …). Comparing a router address to a pair address is a
category error and returns 100% "disagreement" by construction. An earlier cut of this analysis did
exactly that and had to be thrown away.

### Measured, Solana, funded attempts since 2026-08-19 (n=631)

    gate pair APPEARS in the executed route legs   592   93.8%
    gate pair ABSENT from the route                 39    6.2%

Every one of the 39 is a **multi-leg** route (2–3 legs). Seven further rows were skipped because
`quote_route_target` held a venue NAME (`"jupiter"`, `"raydium"`) rather than pool keys — a third
semantic in one column.

**4663 is unmeasurable today.** That is the headline, not the 6.2%.

## Why this got worse, not better

Three independent findings land on the same input:

1. A DexScreener mid froze at one value for 21 marks / 24 seconds while the executable price rose
   51% (position 48109, 2026-09-02). The volume figure comes from the same provider and the same
   response.
2. Gamma: for the coin the operator flagged, every pool is unenriched and the busiest — 481 swaps
   in 3 minutes — prices against a non-standard quote token. Our tape recorded 334 trades of it and
   can price none.
3. `MEMECOIN_EXACT_QUOTE_ALWAYS` now quotes every open position every second, so route selection
   moves faster while the gate input still refreshes on the provider's cadence.

## Requirements

1. **Record the routed pool identity, per chain, in one semantic.** Today one column holds pool
   keys, router addresses and venue names. Until that is one thing, no attribution metric is
   trustworthy — including any number in this document.
2. **Attribute, do not veto.** The output is a labelled input (`volume_pool_matches_route`,
   or an explicit `unknown`), never a refusal. See below.
3. **`unknown` must be a first-class value**, distinct from "matched" and "did not match". On 4663
   every row is `unknown` today and must read that way rather than defaulting either direction.
4. **No new remote call on the entry critical path.** Both facts are already in hand at different
   moments; this is a plumbing and recording problem, not a data-acquisition one.

## Options

| | approach | cost | verdict |
|---|---|---|---|
| A | Record routed pool identity; report the match rate; change no gate | low | **recommended first step** |
| B | Prefer the routed pool's volume when it is known, fall back to deepest | medium; needs the route before the gate, which inverts the current order | later, and only on A's evidence |
| C | Refuse when they disagree | low | **rejected — this is the deleted guard** |

## What not to rebuild

`_route_volume_refusal` compared the provider's volume pool against the route pool and REFUSED on
mismatch. It was deliberately removed in `7c528d1f` ("allow valid EVM routes across pools") twenty
four minutes into its own nine live firings, and origin/main carries two tests locking the removal
in. A 4663 token legitimately has several V4 pools at different fee tiers on our own PoolManager, so
routing through a pool other than the volume-reporting one is ordinary. **Disagreement is not
evidence of danger.** Option C is that guard wearing a new name.

## Open questions for the operator / Alpha

1. Is a 6.2% Solana mismatch worth acting on at all, or is it only interesting once 4663 is visible?
2. Does the routed-pool recording belong with Gamma's `evm_position_pools` work rather than as a
   separate path? It is the same fact, and that table is at 0 rows only because no funded fill has
   landed since the cutover.
3. Option B inverts gate/quote ordering on the entry critical path. That is a latency decision on a
   path already under scrutiny for being 3.8x Solana, and it is not mine to make.

## What this design does NOT claim

That mis-attributed volume has cost money. No such case is demonstrated. The argument is that the
input is unverified on the chain that trades most of our size, and that the cheap fix is to make it
verifiable before anyone reasons further about it.

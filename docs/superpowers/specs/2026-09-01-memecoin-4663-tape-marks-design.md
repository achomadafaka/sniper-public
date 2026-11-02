# Subsecond funded marks for chain 4663 from the tick tape

Date: 2026-09-01
Author: Alpha
Status: DESIGN — not implemented, needs Protocol C review before any funded code changes
Scope: funded 4663 position MARKS only. No exit decision, gate, or policy change.

## The complaint

The operator reports that price reading for Robinhood positions is "way too slow" and "super
unreliable".

## What the measurements say

Mark cadence, `memecoin_position_marks`, 2026-09-01:

| | 4663 | solana |
|---|---|---|
| p50 | 33.4 s | 42.5 s |
| p90 | 53.8 s | 45.1 s |
| p99 | 123.0 s | — |
| max | 6702 s | 148 s |

The median is not the problem. The TAIL is: 1.1 % of gaps exceed 120 s, and the worst reach
1.9 hours.

Mark sources over 24 h:

| chain | sources |
|---|---|
| 4663 | dexscreener 18 087, fastlane_rpc 2 741, geckoterminal 1 646 |
| solana | dexscreener 11 438, fastlane_sol_swap 1 525, fastlane_sol_state 27, fastlane_mid 156 |

4663 takes 82 % of its marks from third-party HTTP. Solana has a direct on-chain source.

## The tail is per-coin, not an outage

For the first 60 long gaps (> 300 s), the number of OTHER mints marked inside the same window:

    OUTAGE   (no other mint marked either) :  0 of 60
    PER-COIN (other mints marked fine)     : 60 of 60

The runner never stopped. Specific coins stopped being priceable while the sweep kept running. A
faster sweep therefore does not fix this; a source that can price those specific coins does.

## The tape can price most of them

For 25 long gap windows, whether `pool_ticks` held swaps for that mint while the mark source
returned nothing:

    tape HAD ticks (dexscreener gave nothing) : 15 of 25
    tape also empty                           : 10 of 25

The tape closes about 60 % of the long gaps. The remaining 40 % are coins with no swaps at all in
the window: no trade-derived source can price them, and they need a pool-state read instead.

## Why the existing fast lane does not apply

`HOT_CHAINS = ("4663", "8453")`, so 4663 IS in the hot lane. But `build_evm_hot_book` keeps only
positions whose frozen policy id equals `engine-envelope-v1`, and every funded 4663 position carries
`follow-30-50-v4`. The log shows "hot exit lane: no engine-envelope Solana/EVM positions; polling"
374 times and ZERO "pool unresolved" exclusions. The hot lane is running, finding nothing, and its
exclusion is the policy filter rather than pool resolution.

So the 33-44 s pull sweep is the only price path a funded 4663 position has.

## The tape we already pay for

`pool_ticks` on 4663: 12.35 M rows, roughly 35 500 blocks of coverage per hour against the ~35 294
blocks an hour contains at 0.102 s per block. Effectively gapless. Nothing on the funded path reads
it. `tape_mid_for_position` exists but is reached only from the `momentum_a` PAPER arm.

## Design

Two tiers, in order, with the existing source kept as the last fallback.

1. TAPE MID. For a funded 4663 position, resolve its pool with the funded resolver
   (`resolve_pool_for_position`, which now carries Gamma's route tie-break) and derive a mid from
   the most recent `pool_ticks` swap for that pool.
2. POOL STATE. When the tape holds no recent swap, read pool state over RPC for a mid. This is the
   mechanism that covers the quiet 40 %.
3. DEXSCREENER. Unchanged, as the final fallback.

### Non-negotiable constraints

- A STALE TAPE IS ABSENT, NEVER A PRICE. A tape observation older than the freshness bound must
  produce no mark at all. Drawing a flat line from an absent observation is the defect that produces
  a 1.9-hour "price", and it is worse than a gap because a gap is visibly a gap.
- SEPARATE `source` AND `quality`. Tape marks get their own values so the two writers stay
  separable in the ledger forever. `source_drift_pct` already carries the one-column-two-writers
  problem and it has cost real time.
- NO DECISION CHANGE IN THIS COMMIT. Exit rails, gates, and policy read the mark exactly as they do
  today. The downstream `stale_s` freshness cap (180 s, and note it is an INERT knob — see the
  twelve-inert-knobs finding) still applies unchanged.
- FAIL CLOSED ON AMBIGUITY. If the resolver cannot name exactly one pool, no tape mark is produced
  and the existing path runs. The tie-break already narrows this.

### Cadence

Tier 1 costs no external call, so marks can be written at tape cadence rather than sweep cadence.
That is where "subsecond" actually comes from. Tier 2 costs one RPC read (~120-140 ms measured on
4663 via a dedicated endpoint) and must stay rate-aware: the chain-owner public endpoint 403s under
burst, so tier 2 needs its own budget and must never be issued per-position-per-tick.

## Open questions for review

1. Should tier 1 run inside the existing sweep, or as its own writer at tape cadence? The second is
   what delivers subsecond; it is also the larger change.
2. What freshness bound makes a tape observation usable? It must be derived from measured tick
   arrival, not chosen.
3. Tier 2's RPC budget, given the endpoint's burst sensitivity.

## Not in scope

Exit rail changes, the `engine-envelope-v1` policy filter, the twelve inert knobs, and the custody
probe retry budget. Each is filed separately.

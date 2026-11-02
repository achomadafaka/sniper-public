# fastlane_sol_state coverage for funded Solana pools — design

Date: 2026-09-02
Author: Gamma (measurement desk)
Status: design only. No wiring. No deploy.
Requested by: Alpha.

## The question

Alpha asked what it would take to give `fastlane_sol_state` coverage of the pools that
funded positions hold. The constraints are:

1. Do not add a remote call to the entry path.
2. Keep `uncovered` a first-class result.
3. Keep the rail frozen when it does not know.

## Why it matters

`state_witness` in `sniper/services/memecoin_volume_death.py` is the second witness for the
volume-death rail. When it returns `uncovered`, the caller returns
`state_uncovered_frozen` and the rail does not fire. A pool with no state marks therefore
disables the rail for that whole hold.

## What I measured

Population: funded Solana positions opened at or after 2026-08-30, read from
`sniper_tracker.db` and `data/memecoin_sol_tape.db`. Read-only. No RPC call.

| quantity | value |
|---|---|
| funded Solana holds | 49 |
| holds with at least one `fastlane_sol_swap` mark in the window | 40 |
| of those, holds with ZERO `fastlane_sol_state` marks | 20 (50%) |
| distinct mints uncovered | 19 of 30 |

Alpha measured 45 swap-covered and 18 uncovered on the same window. My counts are 40 and
20. The two definitions of the window boundary differ. The phenomenon is the same size in
both readings, near 40% to 50%. Do not treat either count as exact until one window
definition is agreed.

## What the cause is NOT

I excluded four candidate causes. Each exclusion is a measurement, not an opinion.

1. **The pool binding is not missing.** `build_price_book` needs a row in
   `sol_position_pools` and a row in `sol_pool_vaults`. All 40 swap-covered holds have
   both rows. Pool-row missing = 0. Vault-row missing = 0.
2. **The stream was not absent.** Position 45597 carries 185 swap marks and 0 state marks.
   The stream delivered rows for that pool through the whole hold.
3. **Late pool resolution does not explain it.** The median delay from `opened_at` to
   `sol_position_pools.resolved_at` is +5.0 s for covered holds and +5.4 s for uncovered
   holds. The two groups are the same.
4. **Venue kind and quote mint do not explain it.** All 40 holds carry
   `venue_kind='amm'`. WSOL is the quote mint on 17 covered holds AND on 14 uncovered
   holds.

The condition is permanent per pool, not transient. Of 24 uncovered mints, 18 have never
produced a `fastlane_sol_state` mark at any time, inside or outside a hold window.

## What the cause IS

`SwapPriceBook.record` calls `_record_state` only when the decoded row carries
`kind == "sol_sync"`. Every other gate in `_record_state` is arithmetic on a frame that
already arrived. So a pool produces state marks only when its program emits an account
update that our decoder labels `sol_sync`.

Coverage is therefore a DECODER and PROGRAM question. It is not a subscription question.
A pool whose program does not emit that frame can never produce a state mark, however
correctly we subscribe to it.

## The reporting defect this exposes

The docstring of `state_witness` contains this claim:

> A hold with swaps but no state marks proves the lane was not watching that pool, because
> a swap necessarily moves reserves and would have emitted.

The measurement above falsifies that claim. Position 45597 has 185 swap marks, a pool row,
a vault row, and zero state marks. The lane was watching. The inference is wrong.

The consequence is not cosmetic. `uncovered` currently reads as "we were blind for a
while". It actually means, for most of this population, "this pool can never answer".
The rail is not intermittently frozen on 50% of the book. It is permanently disabled
there, and the audit rows do not say so.

## What it would take

### Step 1 — record the pool program id. Free. Prerequisite for everything else.

`memecoin_solana_tape.py` already resolves the pool owner. Line 289 reads:

```python
_owner, vault, qmint, quote_vault = resolved
```

The owner is discarded. It is the program that owns the pool account, which is exactly
the field that decides whether a sync frame arrives.

Add one column to `sol_pool_vaults` and write `_owner` into it. This adds no RPC call, no
entry-path work, and no new failure mode. It converts an unknown-size problem into a
counted one: after one day of rows we can say which programs cover and which do not, and
how much of the funded book sits behind each.

Do not attempt step 2 or step 3 before this. Neither can be scoped without it.

### Step 2 — decide per program, once the population is named.

For each program that does not emit a decodable sync frame, there are three options. The
counts from step 1 decide which is worth the work.

- Write a decoder for that program's reserve-update frame. Cost depends on the program.
- Leave the program uncovered and label it (step 3).
- Read the two vault balances directly (step 4).

### Step 3 — split `uncovered` into two results. Cheap. Do it regardless.

`state_witness` should return `uncovered_structural` when the pool's program is known not
to emit sync frames, and `uncovered_transient` otherwise. Both keep the rail frozen, so
this changes no funded decision. It changes what the operator reads. A permanent hole and
a temporary one need different responses, and today they print the same word.

This satisfies constraint 2 and constraint 3 exactly: `uncovered` stays first class, and
the rail keeps freezing on not-knowing.

### Step 4 — a vault-balance read, only if steps 1 and 2 say the population justifies it.

For a pool with no sync frame, two `getTokenAccountBalance` calls on the coin vault and
the quote vault give the same reserve ratio `_record_state` computes. This is a remote
call. It is NOT on the entry path: `state_witness` runs on the exit and rail path, per
position per sweep. Constraint 1 permits it. The funded RPC budget does not obviously
permit it, because this would run per position per sweep, so it needs a rate measurement
before anyone proposes it seriously.

Treat step 4 as the last resort. Prefer a decoder.

## What I did not establish

I did not identify which specific programs are affected. I could not, because the program
id is not recorded anywhere. That is the whole argument for step 1.

I also did not measure the dollar cost of the frozen rail on this population. The
earlier comparison of `vol_death` exits at -$7.97 against `b_stop` exits at -$49.52 is
suggestive, but those are different cohorts and the difference is not a causal estimate.

## Recommendation

Do step 1 and step 3. Both are small, neither touches a funded decision, and step 1 is the
only thing that makes step 2 a decidable question. Hold step 4 until the counts exist.

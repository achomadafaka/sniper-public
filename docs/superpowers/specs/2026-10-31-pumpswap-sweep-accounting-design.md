# PumpSwap sweep accounting: design (Lambda, 2026-10-31)

Status: hunt done (go/no-go MET, 7 of 7), recorder BUILT on branch lambda/sweep-recorder for review; NOT deployed (no worker repin until the operator is back, Alpha). Outcome exemption (`ok_swept`) is NOT built and needs its own reviewed change after the 500-of-500 validation on new tape. Old tape is unchanged.

## Problem

`SweepPoolFeeEvent` (discriminator `82a42461e48287a5`, 185 bytes) is one of five events in `_STATE_CHANGES`
(`sniper/services/memecoin_desk_pumpswap.py`). `events()` raises `DecodeLoss('pumpswap_virtual_quote_unproven')`, the worker
records a `gaps_ps` row, a `ps_decode_losses` row and a complete-scope `ps_loss_attribution` row naming the swept pools, and any outcome window
holding that loss is `censored_gap`. Last 24 h: 30,638 sweep loss transactions. In the v6 outcome table, sweep-kind losses appear in 47 of
1,375 checked 60 s windows and 408 of 1,312 censored 300 s windows (reasons overlap; not a decomposition).

## Facts (real transactions, research RPC lane, 2026-10-30/31)

- In the two sampled transactions a sweep is not a trade: they hold no buy or sell, so no print was lost in them (NOT shown for all sweep-bearing transactions) (fixtures: `tests/fixtures/memecoin/pumpswap_boost_20261030/sweep_pool_fee.json`).
- Event fields: timestamp i64, pool, base_mint, quote_mint, recipient, payer (32 bytes each), amount u64, bucket u8.
- In two transactions (8 events, 4 of them WSOL-quote pools) the pool's quote vault fell by exactly `amount`. Bucket was 1 in 8 of 8 there.
- A second, independent check on 8 real gaps (stored buy before, stored swap after, the sweep in between, no other attributed loss naming the pool in the gap) found bucket values 0 and 1.
  Exact closure `prev.post_quote - next.pre_quote == amount` held in 1 of 8. The 7 others had residuals of both signs (+1.6 M to +46 M, and -4.5 M).
  No attributed or routed-scope loss names those pools in those gaps, so the residual is NOT explained by a known loss. Candidate causes, none proven:
  other vault flows with no pool-scoped loss row (creator-fee collection/transfer instructions are unknown scope), unstored swaps, deposits.
- Sweep-bearing gaps are wide: median 678 slots between the stored neighbours; 10 percent of gaps are 10 slots or less.

## Residual hunt result (2026-10-31, supersedes the 1-of-8 preliminary above)

The preliminary above was wrong on two counts, both mine. (1) A sweep transaction emits ONE event PER BUCKET for the same pool (bucket 0 and 1 are two fee components), and I used the first event only: per pool, the sum of all its events in the transaction equals minus the vault delta in 8 of 8 real transactions
(`~/.cache/sniper-lambda/sweep_hunt.py` and `boostfx/sweep_hunt*.json`, 18 research-lane calls). (2) Gap neighbours were not chain-adjacent. With every vault-touching transaction between the stored neighbours (the sweep transactions and the stored swaps in between), all 7 non-closing gaps close by exact
integer equality (`next_pre_quote - prev_post_quote` equals the sum of the deltas). Flow kinds observed: SweepProtocolFee / SweepCreatorFee sweeps and stored buys/sells. No creator-fee collect, deposit or hidden swap was needed. Limits: 7 gaps, one slot range (463468666 to 463468816), about one hour; base reserve and virtual quote
equality across the gap not tested; a gap holding a sweep AND an unrecorded multi-hop leg not tested; non-WSOL pools are outside the tape.

Gap definition (Gamma): (A) slot bracket: prev = last stored print with slot < sweep slot, next = first stored print with slot > sweep slot, and the sum includes EVERY vault-touching amount strictly between them, including all stored swaps in the sweep's own slot and any slot between (order inside a slot is irrelevant for a sum);
or (B) `(slot, tx_index)` adjacency, only where `tx_index` is non-NULL on every stored print involved. A stored print with NULL `tx_index` in the sweep's slot forces (A); mixing (B) with NULL `tx_index` is invalid and censors. Closure is always exact integer equality on `next_pre_real_quote`;
base and virtual-quote equality across the gap are checked in the same test, and any failure censors.

## What a sweep fix can and cannot do

Gamma's R7 (revised) lets a window whose sweep gaps close by exact integer equality be kept ONLY as a separate stratum, `ok_swept`, never merged with `ok`: the return is exact from two stored endpoint prints, observed peak is a lower bound, trough and max drawdown (stored negative) have magnitudes that are lower bounds, collapsed=1 is valid and collapsed=0 is unknown (NULL), rail contacts inside the gap are unknown, `sweep_reconciled_n` is recorded, and the PASS replay gets NO relaxation. That is a reviewed RULE, separate from this design: endpoint quote closure proves only an observed accounting identity, never that the path inside the gap was benign (offsetting missing flows can close it, and base reserve, virtual quote and ordering can still differ). The recorder and the closure metric prove nothing about extrema or rail validity. Exact closure of a gap also needs EVERY vault flow in the gap accounted for.
The 1-of-8 result says that today most gaps carry other unaccounted flow, so accounting for the sweep alone is unlikely to close them. This must be
measured on new tape before any window is exempted. Therefore the build is ADDITIVE and FAIL-CLOSED:

1. Keep the loss. `events()` still raises for a sweep; the `gaps_ps`, `ps_decode_losses` and `ps_loss_attribution` rows are still written. Nothing that censors today stops censoring in the writer.
2. Add `PS.sweep_events(logs)`: pure parser of the sweep events in a transaction's logs (pool, base_mint, quote_mint, amount, bucket, ordinal), rejecting any length other than 185.
3. In the worker's `except PS.DecodeLoss` path, insert EVERY sweep event (both buckets) into `ps_sweep_events(signature, ordinal, pool, base_mint, quote_mint, recipient, payer, amount TEXT, bucket, event_time, slot, tx_index, received_at, recorder_version)`, primary key `(signature, ordinal)`, INSERT OR IGNORE, index `(pool, slot)`. No swaps_ps row, no price. A failed insert is counted (`ps_sweep_record_failed`) and never loses the loss rows. Closure is per `(tx, pool)`: the sum of all its events; a transaction with any event missing fails closure and censors.
4. No tape decoder version boundary: `swaps_ps` content is unchanged, so `tape_decoder_version` stays. The new table carries its own `recorder_version` (`sweep-rec-v1`); windows may use sweep data only from the first recorded sweep onward (the table's first `received_at` is its retention start, as for the loss tables).
5. Outcome rule R7 (Gamma) is applied ONLY after the validation in R7 clause 3: on the first 500 sweep-bearing gaps with both neighbours stored, at least 99.9 percent exact closures. If the rate is lower, sweeps keep censoring and
   the table is the evidence of why.
6. A separate offline script measures the closure rate (reads `swaps_ps` and `ps_sweep_events`, read-only) and reports the rate with residual sizes, not only pass/fail. No funded path changes; the worker pin and restart are Alpha's.

## Edge cases

- Money path: none. The desk tape worker only.
- Fill path speed: the extra work runs only inside the existing DecodeLoss branch, one INSERT per sweep event, same transaction scope as the loss insert; no new provider call. Measure the worker's per-tx time p50/p90 before and after on a canary pin.
- Concurrency: the worker already owns this connection; INSERT OR IGNORE is idempotent on replay of the same transaction.
- Absent vs zero vs unknown: amount 0 is recorded as 0; an unparsable or wrong-length event records nothing and the loss row remains (unknown stays censored). Bucket is stored raw; unproven buckets (anything but the sampled values) never exempt a window.
- Dependency failure: a failed insert must not lose the loss row; wrap separately and count `ps_sweep_record_failed`.
- Units: amounts are atomic quote units (lamports for WSOL). Non-WSOL pools are recorded with quote_mint and are outside our tape.
- The sample: windows exempted by R7 carry `sweep_reconciled_n`; the rate of exact closures is reported, the exempted set is not self-selected by outcome.
- Rollback: repin the worker to the previous release; the table stays, rows are unused.

## Implementation contract (Zeta review)

- The parser reads only `Program data` lines inside a complete, successful pAMM invocation stack (same provenance as `events()`), never arbitrary program data.
- Row identity is `(signature, ordinal)`; `tx_index` stays NULL when unknown (never fabricated 0); `amount` is stored as atomic TEXT (u64 exceeds SQLite's signed range); bucket stored raw.
- Before any future exemption, base reserve and virtual quote must also be equal across the gap and the order checked; the closure metric reports those separately.
- The first-500 validation needs a frozen selection and order, with exclusions and unknowns reported; it passes only at 500 of 500 exact closures among the first 500 sweep-bearing gaps with both neighbours stored (zero non-closures).
- The per-bucket rule is replaced (Gamma) by per-(tx, pool) closure over the sum of all bucket events. Any failure keeps sweeps censoring, and the closure rate is reported with the residual sizes.
- The exemption (`ok_swept`) is switched on only by a separate reviewed change after validation; the additive recorder is not an exemption and needs no rule.

## Decisions (Alpha 2026-10-31, Gamma)

- Recorder built for review (Alpha GO after the hunt met 7 of 7); not deployed until the operator returns. Disk estimate: about 105 thousand pool-events a day at roughly 250 bytes, under 30 MB a day, no retention pruning (the loss tables are kept).
- Residual hunt (bounded, Gamma): list every transaction touching each pool's quote vault between the two stored swaps for the 7 non-closing gaps (about 60 research-lane calls), classify the flows (creator-fee collect, other sweeps, deposit/withdraw, unstored swaps), test whether adding their exact amounts closes the gap.

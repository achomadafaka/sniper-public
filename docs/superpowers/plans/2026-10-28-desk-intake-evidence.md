# Desk intake: collect evidence before the hard filter (order B, 2026-10-28)

Operator order B (relayed by Alpha 2026-10-28): intake tickets reached paper with age, flow, price-vs-60s and
mcap absent and with holders, cluster and dump_group `unknown:not_collected`. Cohort U:5504f88a's two paper trades
are invalid for that reason. Paper lane only. No funded path, no .env change.

## Root cause (tickets 61e29b51 and a26188ee, read from the stored vet sheets)

| Field | What JUDGE saw | Cause | This branch |
|---|---|---|---|
| flow_60s, mcap_usd | present: ps_facts, 60 s coverage complete | none; only the immutable emission snapshot carries "absent" placeholders | no change |
| age_s | null at sheet top level; ps_facts.ages.provider_reported KNOWN (3.4 and 6.1 days) | on-chain creation not retained; the sheet uses on-chain age only | not here (sheet/ps_facts owners) |
| price_vs_60s_* | null on a26188ee with zero sells | ps_facts nulls the edge when any window row lacks a proven price | not here (Delta's ps_facts) |
| holders, cluster, dump_group | `unknown:not_collected` | never collected on the intake path | built here |

## What this branch builds

1. **Frozen requirement.** `hard_filter.intake.require_collected = ["cluster","dump_group","holders"]`, set by the
   provisioner's `--intake-require-collected` flag. That flag also implies `--cluster`, so the cohort freezes the
   cluster definition the rows must match. A cohort without the key keeps today's soft rule. The key changes the
   policy hash, so this is a new cohort; Gamma writes its preregistration first.
2. **Contract.** For such a cohort, `hard_filter` refuses with `unknown:<name>:<reason>` when a collected fact is
   not pass or kill. dump_group is read through `checked_fact`, so a stale or forged cache is unknown, never pass.
   A kill is not a refusal here: it stays a JUDGE fact, as today, and dump_group kill keeps its existing gate.
3. **Holders** (`memecoin_desk_intake_assembler.read_holders`):
   - Two paced research reads, no retry: getTokenLargestAccounts, then one getMultipleAccounts.
   - Then `memecoin_desk_safety._holders`, with the PumpSwap pool as the excluded owner (it owns the base vault).
   - The creator is the trigger print's on-chain `coin_creator` from `swaps_ps`, never the listing's creator field.
   - A missing creator or supply spends no call.
4. **Cluster.** The cluster worker also reads `candidate_emissions_intake`, under the same per-minute and per-day
   caps. `safety.verify_intake_emission` re-derives the opportunity id, the canonical pool, the snapshot hash and the
   age window. The assembler waits for the bound row, at most the cohort's `evidence_wait_s` from the emission.
5. **Dump group.** `SnapshotCache.lookup`, as in the band assembler.
6. **Wait and restart.** The hard-filter log row records `wait_s` (from the emission to the decision). A SIGTERM
   during the wait raises `Stopped` before any verdict is persisted; the line is re-read on restart.

## Edge cases

- **Money path:** none. These facts are read only by paper cohorts; there is no funded reader.
- **Speed:** two extra paced research reads plus up to `evidence_wait_s` of waiting, all in the assembler. The tape
  write path and fill paths are untouched.
- **Concurrency:** one assembler under flock. The dump cache has its own reader thread, and `lookup` reads an
  immutable snapshot.
- **Absent, zero or unknown:**
  - an RPC timeout, 429 or malformed reply gives `holders:unknown:<slug>`, never an empty book;
  - creator below the page stays unknown (the `_holders` rule);
  - a cluster row that has not arrived gives `cluster_not_ready`.
- **Sample:** the wait and the requirement change which coins are admitted. `wait_s` and the refusal reasons are
  logged per ticket for Gamma.
- **Rollback:** provision without the flag, or repin. intake-v1 cohorts without the key are byte-identical.

## Chart facts (second commit)

`require_collected` now also names `chart`. The assembler computes `ps_facts.facts_for` at the evidence's own market cut
and `chart_check` passes only when flow_60s, both price_vs_60s edges and an age are proven:

- age: on-chain coin age, else the Pump.fun provider age, else the pool's create_pool age. The sheet's top-level
  `age_s` stays on-chain only, as Delta's tests pin.
- `ps_facts`: a proven quiet 60 s window with a proven current price gives price_vs 0, not absent. The current price
  is a point of the window. A trailing unpriced sell or unproven coverage still gives absent.
- mcap: the intake `mcap_safety` gate already refuses an unknown mcap. Nothing added.
- Refusal reasons: `unknown:chart:flow_60s_<coverage reason>`, `unknown:chart:price_vs_60s_low_pct_unproven`,
  `unknown:chart:price_vs_60s_high_pct_unproven`, `unknown:chart:age_unknown`, `unknown:chart:facts_unavailable`.
- Not handled: the intake flow does not exclude our own fills. The chart check only tests presence; the JUDGE
  recomputes the facts with our fills excluded.

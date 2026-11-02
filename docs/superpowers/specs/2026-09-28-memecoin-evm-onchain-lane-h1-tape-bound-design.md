# EVM on-chain lane: judge the h1 floor on the same tape bound as J7 (design + rule 5)

Status: SPEC, nothing built. Author: Lambda, 2026-09-28. Order: Alpha, 12:40Z ("both lanes judge
one number").

## The defect

Same 4663 buy, DopamineFeenFr1 `0x2285b544…`, 0.2 s apart:

| attempt | lane | origin_event_id | onchain_volume_m5 | h1 verdict |
|---|---|---|---|---|
| 99043164 | onchain_watch | `onchain:4663:0x2285…:0xfaf8…` | `source_tx_unknown`, usd None | "h1 organic volume $0 under floor $3000" |
| 28211dc9 | follow_activity (J7) | `swap:raybot:0x2285…:<trader>:<mint>` | `ok`, usd 2171.33, excluded_tx = 0x2285… | "h1 organic volume $2171 under floor $3000" |

Since 61c78a2b the 4663 h1 gate decides on max(provider h1 over listed pairs, tape m5 lower bound).
Both lanes read the same provider h1 ($0 here: every listed pair was dust). Only J7 gets the tape
bound.

Cause: `memecoin_volume_writer.gate_evidence` (the tape read for the gate) parses the source
transaction from the origin id. On chain 56 it accepts both `swap:raybot:<tx>` and the on-chain
lane's composite `onchain:<chain>:<tx>:<mint>`. On every other chain (the
MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS branch, `.env` = 4663,8453) it accepts only
`^(swap:raybot:|onchain:)(0x<64 hex>)`. The composite form puts the chain id first, so it never
matches, and the lane gets `source_tx_unknown`: no bound, and the h1 gate decides on the provider
figure alone.

Scope: every onchain_watch attempt on 4663 and 8453, i.e. the EVM fast lane. There is 1 such
attempt since the watchers went live (this one). The lane is young, and the defect is structural.

## The fix

One source-tx parse for every chain in `gate_evidence`, lifted from the 56 branch. The composite
form counts only when its chain equals the gate's chain and its mint equals the gate's mint;
otherwise the source tx stays unknown, as today. Nothing else changes: the same `read_for_gate`
call, the same exclusion of the source's own transaction, the same h1 rule.

## Rule 5

1. **Money path.** Yes: admission. The on-chain lane's h1 input rises from the provider figure to
   the same max(provider, tape bound) J7 already gets, so it can admit what J7 admits. It never
   lowers a figure.
2. **Speed.** The tape read is a local SQLite read, no provider call.
   - Measured on J7's own 4663 attempts since 09-21 (`onchain_volume_m5` duration_ms, n=177):
     p50 12.1 ms, p90 26.3 ms. 8453 (n=54): p50 4.4, p90 11.8.
   - The on-chain lane now pays the same. Today its early return costs 0.1 ms.
3. **Concurrency.** A pure regex parse. No state.
4. **Absent vs zero vs unknown.**
   - An unparseable, foreign-chain or foreign-mint origin stays `source_tx_unknown` (bound None,
     i.e. unknown), never a zero.
   - A manual buy (no tx) stays unknown.
5. **Failure modes.** `read_for_gate`'s own reasons are unchanged (feed_stale, header_missing,
   window_incomplete, ...). A tape failure still leaves the provider figure deciding, as it does
   for J7.
6. **Units and identity.**
   - The chain id in the composite is compared as a string against the gate's chain.
   - Mints are compared lowercased.
   - Tx hashes are passed through as parsed; the tape's exclusion and J7 already use the same
     form.
7. **Sample.** Not a measurement. After deploy, count onchain_watch EVM attempts whose
   onchain_volume_m5 reason is `source_tx_unknown`. Expected: 0, except manual and non-M5 chains.
8. **Rollback.** Revert the commit. There is no half-applied state; the parse is per call.

## Check left behind

A pytest on the two real origins of this row: `gate_evidence` calls `read_for_gate` with
exclude_tx 0x2285… for BOTH. Plus a foreign-chain and a foreign-mint composite that stay unknown.
Mutant: restoring the old regex fails the on-chain case.

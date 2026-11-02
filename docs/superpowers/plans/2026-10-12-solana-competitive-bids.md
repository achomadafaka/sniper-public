# Solana competitive bids and Frankfurt Nozomi

Operator approved implementation and live activation on 2026-10-12. This amends the
2026-10-11 Shyft migration's prohibition on Nozomi only. Yellowstone/Shyft remain
the streaming/read/custody infrastructure; Helius Sender and public fallback stay absent.

Completed and activated on `716ff09539b677cad2c848f3ddf29bd49c79a998`.
See [live verification and remaining limits](../../audits/2026-10-12-solana-competitive-bids-live.md).

2026-10-13 operator-approved amendment: the default-OFF
`MEMECOIN_NOZOMI_FEE_SPLIT_AB` comparison retains this combined cap while allocating
6M to tip and at most 520K to priority in its treatment. Control retains this
dynamic policy. See [preparation and fee-split contract](../../audits/2026-10-13-solana-pool-preparation-fee-split.md).

## Contract

- Restore same-signed-bytes Frankfurt Nozomi alongside Shyft custody and TPU.
- Capped Nozomi entries POST once per retained identity; its server retries.
  Existing custody/TPU resends remain. Dedup ends at 256-row eviction or restart.
- No entry-path network reads, waits, new shared blocking locks, or retries.
- Preserve source proof, min-output/copy caps, admission, sizing, signed identity,
  ambiguous-send recovery, exits, and the operator kill switch.
- Combined priority fee plus tip is at most 6,520,000 lamports, matching the old
  1,520,000 priority fee + 5,000,000 tip. Base fee remains separate. No unbounded bids.
- Account-filtered priority data and Nozomi tip percentiles refresh asynchronously;
  stale/malformed/transport/rate-limit results never become fresh zero observations.
- Native CU limits require successful receipt evidence for token/account variants.
  Unknown variants retain 400,000; exhausted classes demote through existing recovery.
- Measure actual submissions and successful/failed execution separately. An ACK is
  not landing; source-slot execution is not a successful fill. No artificial trades.
- Focused TDD and deploy gates only, always at most two pytest workers. No full suite,
  database backup, research restart, or speculative fee-cap enlargement.
- Validated explicit focused deploy manifests skip the full-suite host lock, per
  operator `AGENTS.md`; default selection keeps that lock and all resource flags.

## Tasks

1. Submission: permit and construct only explicitly enabled Frankfurt Nozomi with
   the Shyft profile; preserve public/Sender exclusion, key secrecy, parallel dispatch,
   untipped/exit neutrality, and rollback flags. Own backend/config/tests.
2. Native CU: classify successful native PumpAMM receipts, validate creation/reuse
   variants, extend existing class lookup with measured limits and demotion coverage.
   Own native builders/CU tests; root coordinates shared settings changes.
3. Bids: bounded background account-filtered fee/tip cache, immutable snapshot reads,
   cold/stale fallback, explicit lamport/CU units, ceiling-safe integer cap. Root wires
   selection into native builders and pre-sign Nozomi arm. Own new cache/tests.
4. Integration: lifecycle/status/traces, actual signed fee/tip validation, focused
   regressions, independent review, merged/pinned deployment with environment rollback,
   fresh runtime and provider proof. Same-slot improvement requires organic evidence.

## Edge cases and limits

Missing key fails explicit activation; a Nozomi outage cannot block custody/TPU.
Packet overflow follows existing untipped fallback. Unknown ATA variants never inherit
another variant's CU cap. Pool identity and units must match cached samples. Concurrent
entries share read-only snapshots, not mutable selected bids. Partial deployment restores
environment/release. This cannot reverse earlier pool swaps, guarantee ordering, establish
a causal provider win from fanout, or guarantee a profitable fill.

## Preflight interface review

| Tasks | Shared interface | Ruling |
|---|---|---|
| 1 / 3 | Nozomi provider tip validation | Root extends the exact stamp to validated capped dynamic tips after backend restoration. |
| 2 / 3 | Builder CU then fee selection | CU worker owns builder edits; inject one callback after CU selection, no root concurrent edits. |
| 3 / 4 | Cache lifecycle and heartbeat | Root wires startup/background/status; cache worker owns isolated cache module/tests. |
| 1 | Old Shyft-only tests versus allowed Nozomi | Superseded by operator approval; Sender/public still prohibited. |
| 2 | Native samples versus unknown account variants | Validate successful variants first; retain default where evidence is insufficient. |
| 3 | Market bids versus spend ceiling | Market estimate is capped, never an admission gate or promise of inclusion. |
| 4 | Live proof versus organic trading | Verify routing/caps/runtime now; report outcome improvement as unproved until observed. |

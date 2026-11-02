# Funded Solana Cluster Admission Gate

**Date:** 2026-08-22  
**Status:** APPROVED DESIGN, not implemented  
**Policy version:** `solana-funded-cluster-admission-v3`

## Decision

No funded Solana entry can start without a clean, fresh, supply-weighted cluster decision.

This rule applies to every funded Solana entry path. It has no wallet or operator bypass.

The research scanner and its paper arms stay outside this policy. They can record clustered coins and open paper arms.

The gate can block before all owners resolve. The gate can allow only after all eligible owners resolve.

The gate blocks an entry when one condition is true:

- Clustered owners hold at least 25% of attempted supply.
- One funder controls at least 50% of attempted supply.
- At least one eligible owner remains unresolved at the deadline.
- Fewer than four eligible owners exist.
- The snapshot, owner data, funder data, or gate service is unavailable.

The threshold comparisons are inclusive. A value of exactly 25% or 50% blocks the entry.

The gate runs after all cheap funded checks accept the entry. It runs before the first funded reservation or entry quote.

## Evidence

Signal `#3660` for `$AAS` separates concentration from funder linkage.

The holder view showed 14.7% for the top-five share after rank one. The funder probe showed 45.3% clustered supply in the resolved book.

Both Golden paper arms opened before the probe. The hold arm lost 41.1%, and the scalp arm lost 2.8%.

The existing probe resolved 110 of 132 selected signals. This is 83.3% coverage for the selected probe cohort.

A cached buyer-tail lookup took 0.0001 seconds and used no RPC call. This measurement does not include the holder snapshot or owner resolution.

For `$faucat`, one batch resolved 20 holder accounts in about 0.1 seconds. Its cold funder traces took about 3.5 seconds per wallet.

Holder-owner reuse is 7.9% by wallet count across 53 recent coins.

Holder-owner reuse is 0.0% median by supply. The mean is 4.0%.

Only 75 of 678 owners appear on two or more coins. A cache-only holder decision cannot reach full resolution.

The hot path must trace cold holder owners. The cache removes only repeated work.

`$EIGENSLUR` resolved 14 of 19 owners in 11.5 seconds. Its resolved book was 68.2% clustered.

This result proves at least 43.9% clustered attempted supply. The gate can block it before full resolution.

The funded path opened 3 entries during the prior 24 hours. This is 0.12 entries per hour.

The lifetime funded rate is 0.28 entries per hour. The funded-only quota cost is small at these rates.

The current trace uses at least two RPC calls per cold owner. A deep owner can use up to 22 calls.

At the recent funded rate, the lower-bound use is about 5 RPC calls per hour and 120 calls per day.

The material cost is latency. A cold decision can add up to 12 seconds before the entry quote.

The `$Bitknight` funded entry arrived after 23.3 seconds and still returned 67.7%. This is one example, not a latency guarantee.

The system must record cluster latency and the blocked opportunity path. The operator will review both after activation.

## Current State Audit

Commit `296964d9` contains the correct asymmetric research decision function.

The function uses a 12-second deadline and eight parallel workers. It permits early blocks and requires full resolution for an allow.

No funded entry path calls that function.

The approved gate requires supply weights from holder owners. Buyer counts cannot replace holder supply.

The current scanner probe stays a research label. Its Telegram warning is not a funded admission gate.

The active scanner and funded runner predate every cluster commit. A restart alone cannot add a missing caller.

This design supersedes only funded specifications that define cluster data as a label.

## Definitions

### Eligible holder book

The gate reads the current largest token accounts for the mint. It stores each balance as atomic decimal `TEXT`.

The gate resolves each token account to its owner wallet. A token account address is not a wallet owner.

The gate excludes only an explicitly identified bonding curve, pool vault, or program account. It does not use a balance threshold for exclusion.

The gate does not exclude the creator. A creator-controlled holder is part of the ownership risk.

If the gate cannot identify a suspected program account, it keeps that account unresolved. The unresolved balance lowers coverage.

### Funder identity

The funder is the source of the first valid funding transfer to the owner wallet.

The cache stores the source signature, parser version, status, and resolution time. A parser change creates a new cache version.

The gate uses only the dedicated research RPC. It never uses a funded custody RPC or a funded RPC pool key.

### Supply-weighted metrics

Let `attempted_supply` be the sum of eligible holder balances submitted for funder resolution.

Let `resolved_supply` be the sum of balances for owners with a resolved funder.

Let a cluster be a funder that funds at least two distinct eligible owners.

Let `clustered_supply` be the sum of resolved balances held by owners in a cluster.

Let `largest_funder_supply` be the largest resolved balance sum for one funder.

The gate computes these ratios:

```text
coverage = resolved_supply / attempted_supply
clustered_of_attempted = clustered_supply / attempted_supply
top_funder_of_attempted = largest_funder_supply / attempted_supply
```

All calculations use atomic integer balances. Display rounding cannot change a decision.

The numerators can only increase as owners resolve. Therefore, an early block cannot return to an allow.

An early allow is unsafe. Unresolved owners can share one funder and change both ratios.

Buyer-tail cluster counts remain diagnostic fields. They never grant admission.

## Architecture

### Shared cluster store

One worker owns `data/memecoin_cluster_gate.db` under `SNIPER_STATE_ROOT`.

The database uses WAL. All consumers open the database with a read-only connection.

The worker serves decisions through a local Unix socket under `SNIPER_STATE_ROOT`.

A client sends the mint, source path, and request time. The service returns the decision ID, result, reason, metrics, and snapshot time.

The service has a hard 12-second trace deadline. The client has a 13-second request deadline.

A service timeout returns `cluster_store_unavailable` and blocks the entry.

<!-- schema:external=cluster_funder_cache -->
<!-- schema:external=cluster_snapshots -->
<!-- schema:external=cluster_snapshot_holders -->
<!-- schema:external=cluster_decisions -->
<!-- schema:external=cluster_work_queue -->

The store contains these tables:

| Table | Purpose |
|---|---|
| `cluster_funder_cache` | Versioned owner-to-funder evidence |
| `cluster_snapshots` | Immutable point-in-time mint snapshots |
| `cluster_snapshot_holders` | Atomic holder balances, owners, funders, and exclusions |
| `cluster_decisions` | Immutable policy results and exact reasons |
| `cluster_work_queue` | Durable cache work for unresolved owners |

The main funded ledger stores the selected `cluster_decision_id` with each entry attempt and reservation.

Clients never write the shared cluster store. The service writes each snapshot and decision before it sends the response.

### Hot-path decision

The gate obtains a fresh holder snapshot and resolves token-account owners in one bounded batch.

The gate reads cached funders first. It traces every remaining holder owner on the entry path.

The trace uses eight workers. It processes owners in descending supply order.

The service stops early when a block becomes mathematically final. It does not stop early for an allow.

An unknown owner blocks the entry. It never counts as an independent owner.

An allowed decision is valid for five seconds after completion. The initial holder snapshot can be at most 17 seconds old.

A later entry attempt must obtain a new snapshot and decision.

The service persists each new funder result before it returns. It adds unresolved owners to the durable retry queue.

The service uses only `MEMECOIN_RESEARCH_HELIUS_RPC_URL`. It never uses a custody or funded-pool RPC.

The service stops on an HTTP 429 response. It returns `cluster_unknown_quota` and blocks the funded entry.

### Cache worker

The cache worker processes the newest holder owners first.

The worker stops on the first HTTP 429 response. It uses bounded backoff and keeps prior resolved rows.

An unresolved or deep result can retry later. A resolved result stays bound to its parser version and source signature.

The cache improves latency. It cannot grant admission when one eligible holder owner remains unresolved.

## Funded Admission Order

The cluster decision must exist before any funded reservation, quote, signature, submission, or position write.

The system uses this order:

1. The source records the signal or buy observation.
2. The funded path runs the wallet, chain, staleness, mcap, chase, volume, age, band, and capacity checks.
3. The funded path stops before cluster work when an existing check rejects the entry.
4. The cluster service creates a fresh snapshot and decision for an otherwise eligible Solana buy.
5. The funded path stops when the decision blocks or is unknown.
6. The funded path requests an entry quote and creates a reservation only after the decision allows.

The system tracks each blocked funded attempt and its later price path. It uses the existing V2 capture at 15, 30, 60, and 300 seconds.

The outcome key is `cluster-block:<attempt_id>`. The reference price is a fresh market read after the block decision.

If the reference price is unavailable, the attempt stores `coverage_missing_ref_price`. It must not store a flat return.

A blocked attempt creates no funded reservation or position.

## Covered Entry Paths

The shared gate applies to these funded Solana paths:

- The funded global-feed path.
- The funded follow-activity path.
- The funded on-chain wallet lane.
- The fresh-pool entry retry lane.
- The manual funded-entry endpoint.
- Each future funded Solana entry path.

The implementation must place the funded check in the common buy path. A caller cannot bypass it with a direct `real_entry_hook()` call.

The retry lane must obtain a new decision after five seconds. It cannot reuse a blocked or stale decision.

The gate does not change scanner paper entry, sell, mirror-exit, stop, trail, timecap, or manual-close behavior.

## Failure Rules

All cluster-data failures block buys. They do not stop the runner process or its exit loop.

The gate uses these stable reason codes:

| Reason | Meaning |
|---|---|
| `cluster_no_holders` | No eligible holder book exists |
| `cluster_no_rpc` | The dedicated research RPC is unavailable |
| `cluster_unknown_deadline` | The hard 12-second deadline expired |
| `cluster_unknown_unresolved` | At least one eligible owner did not resolve |
| `cluster_unknown_trace_error` | At least one trace raised an error |
| `cluster_unknown_quota` | The research RPC returned 429 or its request budget was unavailable |
| `cluster_too_few_owners` | Fewer than four eligible owners exist |
| `cluster_share_high` | Clustered supply is at least 25% |
| `cluster_top_funder_high` | One funder controls at least 50% |
| `cluster_store_unavailable` | The shared store is unavailable |
| `cluster_decision_stale` | The decision is older than five seconds |

A gate outage blocks new buys only. The funded runner must continue all open-position exit work.

## Audit and Operator View

Each decision stores the mint, source path, policy version, snapshot time, and decision time.

It also stores all supply sums, both attempted-supply ratios, the owner count, and the reason code.

Each funded entry attempt stores `cluster_decision_id`, `cluster_completed_at`, and `cluster_ms`.

The operator view shows cache health, queue depth, decision age, coverage, and block counts by reason.

Telegram can include a blocked funded decision. It must not send a funded-entry alert for a blocked mint.

## Tests

The implementation uses TDD and focused tests.

Required unit tests:

- The `$AAS` supply fixture blocks from attempted-supply linkage.
- The `$EIGENSLUR` fixture blocks at a 43.9% attempted-supply lower bound.
- A clean distributed holder book passes.
- Exactly 25% clustered share blocks.
- Exactly 50% top-funder share blocks.
- Exactly 50% coverage cannot pass.
- One unresolved eligible owner blocks an allow.
- Fewer than four eligible owners blocks.
- A deadline cancels pending work and returns an unknown block.
- A trace error is visible and returns an unknown block.
- Each new trace result reaches the durable cache.
- An unknown rank-one account cannot use a balance heuristic for exclusion.
- Buyer-count metrics cannot satisfy the supply gate.
- A five-second decision passes, and an older decision blocks.

Required integration tests:

- Global-feed, follow, on-chain, retry, and manual funded buys share one gate.
- Scanner paper arms do not call the funded cluster gate.
- A blocked funded attempt creates no reservation, signature, submission, or position.
- A gate outage blocks buys while the exit sweep continues.
- A retry obtains a new decision after the validity period.
- Each allowed funded row stores the exact decision ID.
- A cheap funded rejection makes no cluster request.
- An HTTP 429 result returns `cluster_unknown_quota` and creates no reservation.

A source scan test must list each funded Solana entry constructor. The test fails when a constructor lacks cluster admission.

## Deployment

This document grants no deployment or funded activation.

The implementation must use an isolated worktree. It must not change exit policy, sizing, roster rules, or other entry gates.

The deploy must use the pinned memecoin runtime procedure. The deploy must preserve all open-position exit authority.

Before activation, the operator must verify these items:

1. The hot trace and cache worker use only the research RPC.
2. The shared store heartbeat is current.
3. The funded runner can read the shared store.
4. A blocked smoke event creates no funded entry.
5. A clean fixture reaches the next existing gate.
6. The exit sweep stays healthy during a gate outage.

Activation requires funded-only scope. Scanner paper paths must stay unchanged.

Trace failures, quota failures, or timeouts will block funded entries after activation. This result is intentional and visible.

## Non-Goals

- This design does not prove that the thresholds predict rugs.
- This design does not change the Golden roster or Candidate rules.
- This design does not use a Bubblemap image at entry.
- This design does not treat holder concentration as funder linkage.
- This design does not change any funded exit rail.
- This design does not gate or change scanner paper arms.

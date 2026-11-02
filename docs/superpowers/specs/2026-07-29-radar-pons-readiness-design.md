# Radar Pons Readiness Design

**Status:** Approved on 2026-07-29. This is a forward-only research-capture change.

## Goal

Make Pons V1 the primary Robinhood 4663 launchpad observed by Radar, preserve the existing Bags
4663 collector, expose event silence without confusing it with process health, and label eligible
non-firing Radar samples from their existing executable counterfactual entries.

Radar remains paper research. This design adds no funded entry, signer, sizing, reservation, V6,
or promotion authority.

## Pons evidence contract

The source identity is `pons_v1_4663`. It starts at factory
`0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB` and block `8991118`.

Capture recognizes only:

- the factory's immutable `TokenLaunched` event; and
- Uniswap V3 `Swap` events emitted by pools learned from those launch events.

Each launch stores token, deployer, pair token, pool, DEX/config/position identifiers,
restrictions end block, and initial buy amount. Each registered-pool swap stores signed token and
pair deltas, sender, recipient, square-root price, liquidity, tick, and token ordering. Unknown
pools and other factories are ignored.

Pons V1 has an immediately active V3 pool, not a bonding curve. Its lifecycle is `active_pool`.
The collector does not synthesize curve reserves, progress, or migration.

Amendment 2026-08-07: `2026-08-07-radar-venue-scoring-design.md` adds the separate V3 state,
exact quote, paper arm, and result contract. The new contract does not change old evidence.

## Bounded transport

The existing 4663 lane remains one paced lane under the shared RPC governor. One bounded JSON-RPC
batch reads Bags topics, Pons launches, and swaps from already registered Pons pools. A second swap
query is allowed only for pools first seen inside that same block range, preventing the launch
transaction's initial swaps from being skipped. The registered-pool set is capped at 2,048 newest
pools, a combined catch-up query spans at most 1,000 blocks, and block headers are fetched in
batches of at most 100. Because both venue filters are enumerated in every combined range, restart
resumes from the newest venue event frontier; a silent Bags frontier cannot replay already proven
Pons ranges.

There is no per-pool task, subscription, timer, or unbounded recovery. The current poll cadence,
queue cap, storage cap, and adaptive rate limiter remain authoritative. Bags identities and rows
are not renamed or repointed.

## Source health

Heartbeat freshness measures transport/process liveness. Event freshness measures the age of the
last decoded event's chain timestamp, never its local receipt time. HTTP replay therefore remains
event-stale until it reaches recent chain evidence. The API exposes both fields.

The paced 4663 HTTP lane declares a 60-second transport deadline because one bounded poll requires
several calls through the 0.25 RPS governor. The default five-second deadline remains unchanged for
other sources. Typed provider failures still degrade immediately.

**SUPERSEDED 2026-08-19.** Event silence is not a source failure AT ALL. Event age is activity
data; it does not grade capture health. Pons still declares `expected_event_max_age_s`, and the
projection still reports `event_stale`, but that value no longer makes a lane unhealthy.

Capture health is transport liveness, finalized scan lag, typed failures, and open gaps. The
change came from a live case the old contract could not express: Pons produced no event after
2026-08-12 while its shared scan cursor was current and its open-gap count was zero, so the
five-minute expectation graded a working lane degraded and no amount of backfill could clear it.

A current scan cannot prove correct topics or decoding. That needs an independent known-log
canary, not a fixed event-age limit.

### Pons evidence status, 2026-08-20

State each line at its own confidence. They are NOT the same claim.

| claim | status |
|---|---|
| Pons V1 scan | HEALTHY. Finalized lag 0, open gaps 0, and the worker issues the V1 factory + `TokenLaunched` query in every range. |
| Pons V1 matching events since 2026-08-12 | ZERO. |
| Historical SWAP decoder | VERIFIED. The retained 2026-08-12 real swap decodes today as `event_kind=buy` with no blocker. |
| Historical LAUNCH decoder against a real fixture | UNVERIFIED. No real `TokenLaunched` log is stored locally; the launch tests use synthetic addresses. |
| Current Pons platform activity across versions | UNKNOWN. |

**What a canary can and cannot prove.** A historical V1 launch canary proves exactly one thing:

> Current code still recognizes the historical Pons V1 launch format.

It does NOT prove Pons still uses that factory or that format. A new factory address or a new
contract version is invisible to this lane BY DESIGN, because the query names one factory and one
topic. So a green canary plus eight days of silence still leaves "the platform moved" open.

### Minimum canary specification

1. One REAL V1 `TokenLaunched` log, stored as a fixture with FULL chain identity: `chain_id`
   (4663) or `network_id` (`robinhood_4663`), plus block number, block hash, transaction hash,
   transaction index and log index. Block and transaction fields alone are not chain-complete --
   the same numbers exist on every chain.

   The existing synthetic logs are not useless: they test decoder MECHANICS, which is a real
   thing to test. What they cannot show is compatibility with a production log, because their
   addresses and field layout were written to satisfy the decoder rather than observed from the
   chain.
2. An exact decoder assertion: token, creator, pair and pool all equal the known values.
3. An exact query assertion: the emitted `eth_getLogs` names `PONS_FACTORY` and `TOKEN_LAUNCHED`.
4. Runs ONCE at startup and reports a health result. Not a continuous probe.

   **Failure contract.** A canary failure is SCOPED to Pons and nothing else. It raises a typed
   blocker, `PONS_DECODER_CANARY_FAILED`. It must not stop the worker, must not stop Pump
   capture, must not touch the shared 4663 scan cursor, and must not mark any other source. Pump
   breadth is the live measurement; a Pons diagnostic may never take it down.

   **The state must be STICKY, and it cannot live on `pons_v1_4663:ws`.** That row is rewritten
   by every normal poll heartbeat, and `runtime.heartbeat(source_key)` upserts
   `blocker_code=None`, so a one-shot blocker written there is erased by the next healthy
   heartbeat -- typically within seconds. The failure would vanish while the cause remained.

   The canary therefore owns its OWN runtime status row, `pons_v1_4663:canary`, written only by
   the startup canary and never by the poll loop. That is sticky by construction rather than by
   every future heartbeat caller remembering to preserve a flag. Rules:

   - a FAIL writes `PONS_DECODER_CANARY_FAILED` and it persists across restarts and across any
     amount of normal Pons progress;
   - only a later startup canary PASS clears it;
   - normal Pons events do not clear it, because delivery says nothing about whether the decoder
     still reads the format correctly;
   - the row participates in dashboard status, so the failure is visible rather than buried.

   **Static diagnostic semantics.** This row stores a result, not a transport heartbeat. The
   projection must not apply `WORKER_HEARTBEAT_STALE_AFTER_S` or transport freshness to it. The
   API must include a FAIL in `failed_paths` and grade the Radar `degraded`. It must exclude the
   row from the capture-source denominator. An old PASS is neutral. An old FAIL stays degraded.
   The row keeps the original check time so the operator can see its age.

   Required regressions:

   > canary failure -> normal Pons progress -> Pons still degraded, Pump still healthy, shared
   > 4663 scan cursor unchanged.

   > T+1 day, PASS row plus healthy capture sources -> overall healthy, no canary failed path.

   > T+1 day, FAIL row plus healthy capture sources -> overall degraded, canary in failed paths.
5. A SEPARATE current-contract check before any claim about Pons beyond V1. The canary answers
   "can we still read V1"; only this answers "is V1 still what Pons emits".

   **STATUS: UNAVAILABLE.** This item has no pass rule, so "item 5 passes" is not testable today
   and must not be claimed. A pass rule needs three things this record cannot yet supply:

   - an EVIDENCE SOURCE that authoritatively states Pons's current factory address and event
     signature -- an on-chain registry, a published deployment record, or a verified contract --
     and it must be named, not assumed;
   - a FRESHNESS LIMIT on that evidence, because a deployment record read once decays exactly
     like the event-age bound this spec already retired;
   - a VERSION PROOF that distinguishes "V1 is current" from "V1 exists and something newer is
     also live", since the two produce identical silence on a V1-only lane.

   Until those exist, the honest reading of a green canary and a silent lane is: current Pons
   activity across versions is UNKNOWN. Do not upgrade that to "Pons is quiet".

**What a PASS licenses, precisely.** Items 1-4 must PASS, not merely exist. A pass proves
historical V1 compatibility and nothing more:

- items 1-4 pass  ->  "current code still reads the historical V1 launch format"
- item 5 passes   ->  "V1 is still what Pons emits"  (UNAVAILABLE: no pass rule exists)

Only both together license "Pons is genuinely quiet". Item 5 has no pass rule, so that phrase is
NOT AVAILABLE at any confidence today, however green the canary is. Current Pons activity across
versions stays UNKNOWN, and the correct report is the five-line table above, not a summary
sentence.

The original rule follows, for the record. A source became event-stale only when its
runtime detail declared `expected_event_max_age_s`; Pons declared that expectation because its
launch flow is high cadence. Quiet Bags sources show event age as telemetry without falsely
censoring outcomes or claiming the transport is down.

## Counterfactual labels

The engine already creates a local executable counterfactual entry for each eligible landmark.
Insight projection v2 links every selected sample, including `closest` and `control`, to the outcome
whose entry timestamp and launch match that prediction. Fired samples keep their exact alert-entry
link.

No outcome is invented for an ineligible launch or a sample without an executable entry. Historical
insight rows remain unchanged. New rows carry `selected-outcome-v2`; old rows migrate with
`selected-outcome-v1`. Scored comparisons must filter or stratify by label version.

The v4 path remains decision-time baseline, captured state changes, and exact horizon. It does not
write repeated carry-forward grid rows: a baseline loss can correctly terminate with one observation,
while a surviving path has more. Equal row counts would add no market information, increase writes,
and would not change that terminal ordering. The missing-label fix is exact-entry linkage above, not
synthetic path observations.

## Operational fixes

- Importing `scripts/screen_rising_mechanics.py` must not parse pytest arguments. CLI parsing moves
  inside `main()`.
- Existing `journal_size_limit` settings are deployed with this release; maintenance remains
  PASSIVE-only.
- No new Radar arm or threshold is introduced. The current v4 cohort continues forward-only.

## Verification

TDD must prove exact Pons decoding, unknown-pool rejection, same-transaction launch/swap capture,
bounded batch construction, restart identity restoration, typed event freshness, non-fire outcome
linkage, historical schema migration, and import-safe CLI behavior. Focused Radar tests, schema
validation, JavaScript syntax, import boundaries, diff checks, pinned-release deployment, process
count, authenticated health/API, RPC metrics, source progress, storage, and logs must pass.

## Knowledge sync

Knowledge delta: new Robinhood research venue and corrected sample-label/source-freshness evidence.
Funded and V6 doctrine is unchanged.

# Desk PumpSwap phase 2

Approved source build, 2026-10-22. No push, deploy, restart, provider/model call or production-state
write. The existing 5-open / $200 daily cash-loss halt / −$200 cost-inclusive kill / $25 clip remain.
Curve study F0 `1b79444ddacb718be88fb0592356fc9b18cd3015` remains unchanged.

## Boundaries

1. Census uses the existing research subscription, off by default. PumpSwap-only frames are counted
   before the curve ring. A separate offline-tested guard refuses unavailable/stale funded metrics
   and aborts its owned canary child at funded head-lag p99 above baseline +30 ms. Alpha owns the
   live 30-minute canary; its baseline and metric source must be explicit.
2. Capture is separately off by default. Authenticated, successful PumpSwap CreatePool/Buy/Sell
   events go to `pools` / `swaps_ps` in `pumpswap/tape.db`, never curve `swaps`. Unknown pools remain recorded
   and unmapped. Conflicting maps refuse eligibility. No account reads or historical inferred maps.
3. Emission is separately off by default. Only causally mapped canonical migrated WSOL pools with
   complete pricing metadata and fees qualify. New versioned candidates bind venue and pool into
   identity. Curve v1 output, hashes, cohort policy and ranker remain unchanged.

## Facts and clocks

Raw amounts/reserves are unsigned decimal TEXT, including zero; missing is NULL. Trade reserves are
explicitly **pre-trade**, and post-trade reserves exist only when the shared parser proves them.
Each event retains raw payload, source timestamp, nullable block time, slot, signature, ordinal,
nullable transaction index, receipt-before-queue time, processing time and binding availability.
Proto3 omitted/default index zero is unknown, not evidence of the first transaction.

Pool maps retain base mint, quote mint, pool creator (not economic coin creator), canonical proof,
creation slot and known_at. Canonical means index zero, Pump authority PDA, WSOL quote and exact pool
PDA. Binding availability must precede the swap and snapshot. Restarts reload persisted maps while
recording a gap; gaps invalidate crossing state. Duplicate delivery does not advance state twice.

Pricing requires separately proven base decimals, supply and virtual quote; zero virtual quote is a
fact, not a default. Missing or incoherent metadata/fees keeps capture but refuses emission. Snapshot
`pool_state` carries base/quote reserves (atomic), quote mint, base decimals, supply atomic, virtual
quote atomic, LP/protocol/creator bps, slot and mapping known_at. Every field carries causal stamps.
The band is an explicit PumpSwap setting; it is not fitted or pooled with curve results here.
The ordinary worker supplies no metadata hook, so enabling emission alone still yields capture-only.

## Consumers

PumpSwap candidates use a new schema with `venue='pumpswap'`; their ticket recipe also binds pool.
The cohort's immutable policy binds that venue. Global active-coin protection remains, but evidence
cannot attach to a head from another cohort. The curve assembler and cluster reader refuse new
venue envelopes before any provider budget. Beta owns pool sell-probe and verdict-price dispatch.

PumpSwap feature extraction reads only its pool tape and writes `ranker_ps/candidates.db`, version
`pumpswap-features-v1`; curve labels never read that registry. First features are immutable; score and
shortlist stay NULL until the graduated stratum freezes its own rule. Pool features never use curve progress or virtual-curve
completion. Gamma's graduated-stratum skeleton `45e184cb2` remains a separate, unfrozen protocol;
this build does not select admission/score/outcome thresholds or authorize funding.
PumpSwap cohort previews carry zero admission/SR probabilities and unregistered selection/labels;
`--confirm --venue=pumpswap` refuses. Beta's probe and pricing dispatch remain a separate integration.

## Storage and resources

The single PS database keeps global `(signature, ordinal)` identities across midnight/restart and
indexed pool mapping without scanning historical files. UTC day is derived from `received_at` for
selection, never identity. Old unreleased daily PS files cause explicit startup refusal, not migration.
The single writer uses the existing one-second commit cadence; curve rollover commits before close.
Recognized decoder losses create typed PS gaps; reader overflow only queues a coalesced gap and never
writes SQLite. No funded code or service is changed.

The curve desk limits remain: 15 GB soft / 20 GB hard / 20 GB minimum free, checked every 60 seconds;
PS storage, emissions and feature registry are excluded from that size sum. PS has its own conservative
1 GB default hard cap (`MEMECOIN_DESK_PUMPSWAP_DISK_HARD_CAP_GB`, finite and positive). Its size cap or
free space below the minimum plus that headroom disables PS capture/emission only, clears its queue
and records/defer-persists `disk_cap`. The existing connection keeps discarding PS-only frames, so they
cannot spill into the curve ring. An absolute free-space breach or curve hard cap still stops the whole
research worker; the existing minimum-free rail is not weakened. There is
no automatic deletion/VACUUM or retention policy; retention needs the operator's decision after actual
disk/day is measured. Any maintenance checkpoint must use the existing PASSIVE-only tool; never
RESTART/TRUNCATE. The live canary must report DB+WAL growth/day and decode p50/p90 before activation.

## Edge cases and limits

Checks cover failed/truncated/foreign-program events, duplicate delivery, conflicting bindings,
uint64 extremes, absent/zero metadata, unknown transaction order, dual-filter transactions,
day rollover/restart, stale price/heartbeat, ring pressure and cross-venue/cohort isolation.
No repair for unseen old pools, missing metadata, provider outages or actual shared-provider load.
No synchronous work is added to funded entry, sizing, signing or exit paths.

## Metadata/snapshot integration amendment (operator approved 2026-10-22)

Finish the local metadata/snapshot and price-cap seams, without activating admission or making live
calls. Lambda owns the separate default-OFF metadata worker, its durable pacing ledger and local
PS refresher; Beta owns price-cap validation/dispatch; Codex owns the candidate contract, emission
book, universe hookup, sheet and merged proof. Curve F0, hashes, knobs and admission stay unchanged.

Mint decimals and supply-at-read come from one owner/slot-validated response, stored immutably with
the response receipt. Failed reads are unknown; call budgets survive crashes and are charged after
pacer admission immediately before send. A successful mint read cannot strand pending pool work.
Only newly mapped pools after the worker's persisted marker qualify. No provider calls run here.

The official pump_amm IDL SHA256 is
`b7d8c57a4d9c4dd0109a9ab893052352333252d4eedced10ac091d4d66cab89b`.
It includes signed i128 virtual quote in Pool at byte 245 and BuyEvent after buyback_fee. Virtual
quote is mutable; it must never be synthesized as zero or carried from a cached account through a
later swap. The research decoder may price proved legacy buy/exact-quote event state; unsupported
v2 post-state, negative/out-of-u64 virtual quote, partial events and intervening sweeps/boost/liquidity
events refuse pricing. Raw payloads remain retained. Funded parsers/execution are untouched.

PSv1 keeps its legacy required state keys. Additive optional keys are `reserve_known_at`, `sol_usd`,
`sol_age_s`, `virtual_quote_known_at`, `virtual_quote_slot`, `virtual_quote_source`, `cut_slot`. New decision
pricing needs all seven; old envelopes retain their hashes but cannot acquire invented provenance. Emission clocks
come from the original swap receipt; mint metadata must already be known then. Missing facts keep
the source swap and a typed per-day pricing diagnostic, never silently shrink the denominator.

The collector publishes `ps_committed_through` only after all tape/day-file commits finish, with both
receive queues drained and no pending gap or new write failure. It captures the watermark before
commit. Receive timestamp-to-enqueue and capture-to-commit run without an await on the same event
loop; adding a thread or await requires new in-flight accounting. Startup has no watermark.

The decision reads an explicit committed prefix: `receipt_cut = min(as_of, ps_committed_through)`.
The watermark must be finite, no later than the heartbeat, and at most 5 s old. Tape reads use one
read-only attached transaction; per-file WAL visibility is safe because publication follows every
commit. Cut is `max(blocks.slot WHERE received_at < receipt_cut) + 1`; both swap slot < cut_slot and receipt
< receipt_cut are strict. Meta persists the cutoff, watermark and receipt lag for replay; it never
claims the tape includes every receipt through decision-now. Gap scans extend through decision-now
for conservative refusals. Missing blocks, gaps, latest unpriced sell/v2, ambiguous same-slot order, unreadable
DBs, nonfinite/future heartbeat or SOL data refuse. Reserve and virtual quote ages are at most 60 s;
effective SOL age includes heartbeat age and is at most 120 s. Metadata keeps its own receipt/slot.
An as-of decision can use mint metadata read after the reserve swap but before its own cut; its
supply is labelled at-read, and the combined field is stamped at the decision time. This never
backfills the frozen source crossing, which still needs metadata known by its swap receipt.
Refreshed candidates change only emitted_at/snapshot and get a new hash; originals remain immutable.
The sheet and queue use the same frozen PS state and shared cap function. Missing cap stays refused.
The PS cohort, curve assembler/ranker isolation and zero admission probabilities remain intact.

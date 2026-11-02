# Known-group launch funded canary

Status: operator-approved experimental policy; implementation design for review.
Not enabled or validated as profitable.

## Authority and purpose

The operator requests direct funded testing, $25 per trade, manual stopping,
and no new strategy-wide loss budget or fill-count ceiling. The fresh order is
brain decision `8bc38132-d380-4cbe-ad91-165f85b061fc`, relayed directly by Zeta.
Existing account controls, token safety, route/exposure/duplicate checks, the
operator-only `--confirm` boundary, and no `.env` changes remain in force.
Office seats stay paused. Research watch membership never arms a copy wallet.

This tests whether a prior-known funding network predicts a usable launch
window. Cheap genesis inventory is the operator group's advantage; this
strategy does not claim it can buy inside an already received transaction or
replicate that cost basis. Later returns by the 590 Dgk buyers are retrospective
evidence and cannot qualify an entry or add members to an open position.

## Experimental policy

The operator subsequently approved the displayed proposal and requested live
execution through authenticated Zeta instructions. These are experimental
thresholds, not validated optimal parameters. The independent proposal is frozen at
`/home/bigpapi/.cache/zeta-linked-launch-funded-candidate-2638720a1.md`, SHA256
`2638720a13c8168d0890c9de8130649460a93cfdb8317b5a30a0fdfde8b33128`.

- Separate strategy/cohort namespace `known-group-launch-v1`; never use the
  `large-buyer-*` prefix or pool outcomes with CROWD.
- SOL/PumpSwap only, fixed $25, no operator sizing or safety override.
- Prior-known approved root/hub set backed by verified host funding receipts.
  A developer's final funding hop is at least 0.01 SOL and occurs within
  60 seconds before creation. Every path hop must bind actual addresses,
  payment signature and event time. Locally known proof must precede decision.
  A sponsored account setup, imported creator field, dust paid after launch,
  or shared funding alone is insufficient to qualify the developer.
- Validate a live Pump creation and its signing developer, mint, decimals,
  curve and exact transaction identity. Retrospective discovery is ineligible.
- Enter immediately after the first usable tradable state. Original creation
  receipt plus five seconds is an immutable deadline; retries, graph catch-up
  and process restarts never reset it. No inherited ten-second CROWD delay.
- Refuse if a qualifying member's same-mint sell is already observed. Absence
  of a sell is observed-history evidence, not a completeness guarantee.
- Freeze membership and proof cut at entry: developer, qualifying pre-create
  funded members, and proven mint-credited genesis signers known by that cut.
  Later graph growth never expands this position's exit cohort.
- First fresh verified same-mint sell from that frozen set nominates a full
  close. Proposed source receipt-to-decision freshness limit is one second.
  Existing exact quote, close claim, signed-identity and recovery machinery
  remains authoritative. No second signer or broadcast path.
- Fallback rails: TP40, SL30, no trail, 30-second filled-position max hold.
  All are frozen before signing. Existing catastrophe/unquotable protections
  remain active. A failed exit can exceed the nominated hold or loss level.

## Execution integration and authority

Use the existing `enqueue(..., signal_source="desk")` execution boundary and
automated independent book. Do not disguise automated decisions as manual
orders or introduce a new execution origin merely to avoid desk controls.

The current generic desk registry hardcodes a $200 cohort stop, while every
`large-buyer-*` cohort inherits CROWD's flag and 30-admission cap. Neither is
the approved new strategy policy. Add an explicit, validated stop mode for
this new cohort. Keep all old cohort hashes, default $200 budgets, trip latches,
account-book controls and behavior unchanged.

Selected additive representation:

- Add `stop_mode` with existing loss-limit behavior as the default. Only an exactly
  validated known-group policy may register manual-stop mode with numeric zero
  as an explicit sentinel. Arbitrary old zero rows never mean disabled stops.
- Add a separate `admission_enabled` field defaulting OFF for this policy. The runner
  rechecks it in Python and the atomic reservation predicate. Old cohort
  authority continues through its existing path and is not implicitly disabled.
- The operator-only `--confirm` command authorizes the exact registered policy
  and enables admission; a producer flag or direct queue insertion is not
  sufficient. It also supports admission stop without stopping open exits.
- No `.env` edit is needed for this durable per-policy authority. Do not add
  a synchronous file read or network call to funded admission.
- Register and hash manual-stop semantics in both canonical cohort hash
  implementations. Update Python/atomic SQL twins, trip latch and dashboard
  so the policy displays manual stop rather than a misleading $0 threshold.

Dedicated closed proof metadata must bind this cohort, policy hash, original
receipt/deadline, mint/creation signature, funding path, causal group-membership
snapshot and observation coverage. Old CROWD metadata shapes stay unchanged.
Reservation, pending intent, frozen funded contract and filled position must
agree byte-for-byte on the new fields. Same-ticket different-payload replay
refuses; identical replay is idempotent.

## Fresh observation and exits

The lagged research graph and historical paper ledger cannot drive protective
exits. A dedicated data-only worker tails the existing capture's fresh committed
raw confirmed receipts using its own short read-only snapshots. It reuses pure
native transaction/funding/create decoders; it does not wait for graph replay.
This is observed confirmed history, with selective coverage and measured
capture/commit delay, not a promise to see every first sell. No new provider or
funded-native callback/subscription is introduced.

At first activation the worker records a tail baseline and an honest unknown
history marker; it never submits historical entries. Pending slot metadata is
retried by exact flow identity within the original deadline. Raw receipt clock,
signature, slot and explicit ordering fields stay bound. Missing ordering is
unknown; a zero index must not be invented from an absent protobuf default.
Clock regression, file/anchor replacement and queue overflow refuse eligibility.
Registry seed proof can be read from prior-known strict records, but imported
creator fields or facts discovered after this entry cut cannot qualify it.

The worker persists preparation/create/cohort state independently of the bulk
graph. Preparatory watch expansion uses existing research observation authority
only. It must preserve explicit removals and cannot arm copy wallets. Root/hub
discovery must be committed before claiming local availability. Additional
groups require a reviewed prior-known registry version and exact entry proof;
catalogue/import membership alone is never a funded signal.

Freeze the position-to-cohort mapping in the funded contract because desk
positions deliberately clear `source_user_id`. Extend the existing durable
manual-exit transport with a truthfully named automated full-close action and
closed proof payload. Do not label strategy exits manual, use
`close_any_price`, or call a signer from the observer. The existing exit worker
validates exact position/mint/cohort/entry identity and source sell evidence,
then calls the existing venue exit closure and durable claim/recovery path.
Deterministic exit-request replay must compare the full payload; it cannot
silently accept a different nomination under the same ID.

When a sell is observed before entry, invalidate the candidate durably. Queue
admission and atomic reservation both check current candidate eligibility,
original expiry and enabled policy. A stale enqueue cannot race that
invalidation into a new position. Admission stop never disables open-position
sell monitoring or the fallback exit rails.

No new paid provider, egress path, custody client or signing implementation.
Queue failure, stale source events and unknown observer health must be visible;
no backdated events or invented historical coverage. Record source receipt,
decoder-ready, committed-row-observed, decision and close-handoff clocks. The
one-second sell-decision limit may reject delayed events; runtime measurement
must report this, not reset the source clock.

## Edge cases and acceptance

- Money path: no added synchronous RPC, network retry, shared connection or
  process-global mutation. Native callbacks only enqueue bounded work.
  Measure affected callback and admission p50/p90; source tests are not latency
  proof or live activation.
- Age/identity: original receipt clock, event chain order, seconds/slot units,
  exact mint/decimals, malformed or future facts, and reopened old launches.
- Truth: absent/zero/unknown differ; imports and fee beneficiaries do not prove
  signing developers; later family evidence cannot become an earlier signal.
- Concurrency: duplicate producers, current CROWD/copy reservations, restart
  after enqueue/signing, already-closing position and ambiguous confirmation.
  Existing atomic cross-lane exclusion remains in both directions.
- Stops: manual admission stop rejects pending/new entries at the runner, but
  does not disable monitoring or exits for open positions. No hidden new $200,
  30-fill, or other strategy-wide ceiling; existing account controls remain.
- Failure: source gap, observer overflow, timeout, RPC rejection/rate limit,
  malformed provider data, failed sell quote, and unsellable token remain typed
  outcomes. No quote is a guarantee of future sellability.
- Rollback: default OFF first. Old runtimes must fail closed on new policy rows;
  old cohorts and their hashes remain unchanged. Open-position frozen rails
  and ordinary close recovery continue. No database backup or `.env` mutation.

Required checks include a positive synthetic end-to-end enqueue/runner-admission
case and meaningful refusals for stale/restarted receipt, future path evidence,
unbound creator claim, unknown decimals, late membership, known prior sell,
disabled/revoked authority, mutated policy, duplicate cross-lane admission,
stale/wrong-mint sell hint, concurrent full close, and old-cohort compatibility.
No test may force a real trade.

## Delivery boundary

Independent exact-SHA money-path review, focused funded regression tests,
immutable release and reconciliation checks precede a reviewable operator
command. Codex may prepare/deploy default-OFF code within existing authority.
The operator executes the money-enabling `--confirm` command. No new strategy
is live until its exact authority, runtime, source health and first organic
intent/fill evidence are separately verified.

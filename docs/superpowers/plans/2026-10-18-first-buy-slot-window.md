# First Source Buy Slot Window Implementation Plan

> Historical implementation plan. The 2026-10-20 operator amendment in the
> [current spec](../specs/2026-10-18-first-buy-slot-window-design.md) removes slot deadlines
> from new automatic entries while retaining first-buy selection and all other quality gates.

> **For agentic workers:** Use superpowers:subagent-driven-development for the independent adapter and transport tasks. Steps use checkbox syntax.

**Goal:** Automatic Solana copies use the first source opportunity and stop sending after S+2,
with explicit evidence-qualified wallet exceptions.

**Architecture:** One durable first-source claim and policy resolver serves native and shared
admission. Immutable request deadlines guard every transport leg using the existing local head.

**Tech Stack:** Python, SQLite, existing Solana transports and pytest with two workers.

**Spec:** `docs/superpowers/specs/2026-10-18-first-buy-slot-window-design.md`

## Global Constraints

- No synchronous entry RPC, new providers, or strategy/sizing/safety bypasses.
- Preserve signed ambiguity, fill booking, exits, and independent operator authority changes.
- Keep default live maximum source-slot delta at 2 and live exception map empty.
- Run pytest with `-n 2`; preserve deploy validation and `--defer-backup`.

## Review Focus

- Duplicate observations and new signatures must not reset a missed first opportunity.
- Unknown pre-balances and synthetic zero must not prove a first buy.
- Build/DB/worker waits must not turn a timely candidate into a late provider send.
- Restart, source failure, sells, and retry queues must not erase the first claim.
- Timely broadcast followed by later landing must retain real fill/recovery accounting.

### Task 1: Source evidence and transport deadline

- [x] Trace all source adapters, shared send seams, queued workers and resend ownership.
- [x] Write and observe failing transport deadline tests.
- [x] Preserve source traded-account pre-balances with adapter RED/GREEN tests.
- [x] Enforce immutable request maximum slot before/after bind and per provider/resend.
- [x] Run focused transport/adapter and existing late-landing/recovery regressions.

### Task 2: Durable first opportunity and admission

- [x] Write failing persistence, policy, exact-wallet exception, retry and admission tests.
- [x] Add the first-claim table/helper and indexed historical seed.
- [x] Resolve default and explicit exact-wallet exception rules.
- [x] Wire native/shared admission and original deadline through both Solana executors.
- [x] Block entry/dip/revival retry scheduling; preserve receipt reconciliation.
- [x] Expose effective policy and readiness in the existing runtime heartbeat.

### Task 3: Review and activation

- [ ] Independently review bypasses, races, parsing, manual/EVM/exit isolation and test quality.
- [ ] Run focused regressions, schema references, knowledge sync and deploy core gates.
- [ ] Create/attach a reviewable PR, merge and deploy the approved operator policy.
- [ ] Verify release/PIDs, effective +2/default-first policy, empty exceptions, J7 cache23,
  all requested current wallet authorities and native/confirmed acknowledgements.

Offline temporary WAL/NORMAL ledger benchmark: new claim p50/p90 0.127/0.135 ms; duplicate
0.092/0.097 ms (300/1,000 samples). Direct valuation p50/p90 0.169/0.179 → 0.604/0.620 ms.
These fixture measurements add no provider work and are not live dispatch or landing proof.

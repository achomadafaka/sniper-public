# Wallet Campaign Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task by task.

**Goal:** Address the six research gaps with causal preparation/campaign/inventory/history/timing records and honest exact-quantity profit measurement.

**Architecture:** Extend the current graph consumer with token/timing facts; materialize preparation and launch records independently of buys. The existing quote owner supplies a separate campaign namespace and a read-only report exposes evidence and unresolved coverage.

**Tech Stack:** Python, SQLite WAL/FULL, existing protobuf decoder and capture-only quote client; no new dependencies.

**Spec:** docs/superpowers/specs/2026-11-01-wallet-campaign-research-design.md

## Global Constraints

- No funded changes, orders, .env/provider/cap increases or Office restarts.
- Explicit path ownership, isolated worktrees, crew_commit; pytest -n 2 nice19.
- New facts are append-only/idempotent and obey existing graph row/disk caps; no fabricated prehistory.
- Existing family-v1 remains unchanged. Unproved net profits/holdings/coverage remain unknown.

## Review Focus

- Services and mixed same-minute customers: temporal membership cannot assert control.
- Token accounts missing on one side or fee extensions: preserve known observations, no invented zeros.
- Future discovery or replay: cannot become prior history or early timing.
- Missing quotes/costs/landing times: censor execution verdict, not zero-fill.
- Restart, quota and failed commits: no duplicate events or successful uncommitted evidence.

### Task 1: Preparation, campaigns and prior-only histories — Epsilon
- [ ] RED tests for preparation without BUY, actual launch binding, unknown mint, mixed service customers, future facts, restart, no-launch denominator, capped writes and prior-only history.
- [ ] Implement new sniper/office/wallet_campaign.py and new focused tests; no edits to collector/codec/CLI.
- [ ] Publish stable materialize_campaigns(net, desk_dir, now, limit=100) and immutable launch record interface for quote consumer.
- [ ] GREEN focused tests and exact owned-path commit.

### Task 2: Token roles/inventory and fact timing — Delta
- [ ] RED tests for supported transfers, new/closed accounts, owner/mint/decimal mismatches, unknown Token-2022 semantics, clocks, gaps, atomic commits and restart.
- [ ] Extend wallet_stream_proof.py additively; implement wallet_token_flow.py; integrate bounded token/timing writes in wallet_watch_collect.py with existing receipt/proof and cap transactions.
- [ ] Preserve existing movements API and funded proof behavior. Transfer lineage remains evidence, not a sale or common control.
- [ ] GREEN focused tests and exact owned-path commit.

### Task 3: Existing worker integration, executable quote study and report — Codex
- [ ] RED tests for default-off campaign quote namespace, actual validated mint/source, strict timing, own exact quantities, shared caps, late/failure censoring and unknown net costs/landing.
- [ ] Integrate materializer into office_wallet_watch_collect.py; add campaign probe within existing dex_boost_collector.py owner using existing client/buckets/accounting only.
- [ ] Add bounded read-only research report linking six evidence surfaces, explicit costs/feasibility and denominators; freeze study identity and preserve family-v1.
- [ ] GREEN combined focused tests; source review by Zeta; owned commits and merge.

### Task 4: Review, research-only activation and delivery — Codex/Zeta
- [ ] Adversarial review of causal keys, roles, time/atomic units, durability, budget accounting, and measurement claims.
- [ ] Activate only immutable research collector/quote pins after PASS; never funded/Office units or .env.
- [ ] Verify exact PID/source, fresh heartbeat, graph/quote tables, explicit coverage/censors and unchanged funded processes/caps.
- [ ] Record-work with precise source/runtime/organic limits; do not mark profitable strategy achieved without evidence.

## Implemented interface files

- `sniper/office/wallet_token_flow.py`: explicit per-account observations and instruction transfers; source/flow-id provenance binds the graph commit timing row.
- `sniper/office/wallet_campaign.py`: preparation records, immutable revisions, strict creation attachments and bounded read projection.
- `sniper/office/wallet_campaign_history.py`: read-only actual event sequence recurrence using strict prior revision cutoffs.
- `sniper/office/wallet_campaign_quotes.py`: separate launch study scheduler through the existing quote owner; exact quantities, durable counted attempts, quote-only gross and receipt cash reconciliation.
- `sniper/office/wallet_campaign_report.py` and `scripts/research/wallet_campaign_report.py`: bounded read-only six-surface report, censors, conditional returns and optional local receipt cash legs.
- `scripts/office_wallet_watch_collect.py`: one existing research worker invokes the new materializer, independent of BUY candidates.

The independent design review is `/home/bigpapi/.cache/zeta-six-gap-design-review.json`. Its atomic-phase, cross-window attachment, exact source/flow inventory join, commit availability, shared quota and known-cost counterexamples are acceptance gates, not optional followup.

# Wallet Family Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Tasks use TDD and independent review.

**Goal:** Connect existing wallet-universe receipt proofs, forensic graph and wallet watch to timely causal launch/funding observations and validate a separate earlier-entry hypothesis.

**Architecture:** Extend existing modules and graph store. A separate bounded research worker observes without funded callbacks; family reads filter by known_at cutoff.

**Tech Stack:** Python, SQLite, current research ChainData and PumpSwap tape.

**Spec:** docs/superpowers/specs/2026-11-01-wallet-family-detection-design.md

## Global Constraints
Office stays paused. No funded/provider/env changes. Pytest -n 2. Existing research caps remain. Preserve source/known clocks and unknown states.

## Review Focus
- Fee-payer/source confusion and ambiguous WSOL lifecycles.
- Same-slot/same-transaction buyer duplication.
- Pagination crashes, repeated boundary rows and backlog starvation.
- Future-known graph edges leaking into historical candidate evidence.
- Missing routes, fees and censored exits overstating an earlier-entry strategy.

### Task 1: Receipt proof repair
Files: sniper/services/wallet_identity.py and focused funding tests. Epsilon owns.
- [x] Write failing temporary WSOL funding and rejection cases; verify RED.
- [x] Extend verified_sol_funding using ordered validated instructions and balance proof.
- [x] Run focused tests -n 2; independent review and commit.

### Task 2: Causal family evidence
Files: sniper/office/wallet_watch.py, tests/office/test_wallet_watch.py. Codex owns.
- [x] Add read-only family evidence at a known_at cutoff with verified-only links and infrastructure exclusions.
- [x] RED/GREEN tests: future/claimed edges excluded, missing graph unknown, case family linked after actual discovery only.
- [x] Verify existing watch behavior unchanged; commit exact paths.

### Task 3: Existing watch collection and evidence delivery
Delta owns isolated worker after feed/capacity reconnaissance; exact files assigned before edits.
- [x] Reuse graph/watch list and existing research transport; preserve bounded durable pagination and typed gaps.
- [x] Replay failure/restart/duplicate/budget cases before enabling independent collector.
- [x] Verify actual observations and measured lag; no Office/funded restart.

### Task 4: Strategy evidence
Zeta read-only. Reuse current arm/tape/outcomes with prior-known family evidence. Count atomic batches separately; compare earliest executable observations with current timing, retain unknown costs/censoring. Promote no rule without independent review and adequate evidence.

### Task 5: Earlier setup observations on the existing stream
Codex owns scripts/memecoin_desk_universe_worker.py, sniper/services/memecoin_desk_wallet_setup.py and tests/test_memecoin_wallet_setup_capture.py. Validate direct Pump/PumpSwap init_user_volume_accumulator instructions against program ID, signer/writability, fixed accounts and PDAs. Persist sponsored setup separately from funding in existing tape wallet_setup_events. A 120-second, 500-address-capped capture-only watch catches the first swap while the durable watch collector catches up. Failed transactions, ambiguous CPI and invalid layouts are not proof. Respect capture disk stops, retain unknown block time, count errors and never discard swap processing. Benchmark offline parser before wiring and require independent review before source deployment.

## Live verification and remaining evidence
Core release405c5b1a7 is live as memecoin-wallet-family-watch.service; capture417069b41 remains separate. Independent runtime audit verified26 watch entries and one organic research emission1.688 seconds after source receipt. This is detection proof, not an executable entry or edge.

Paired quote probe extends the existing dex_boost_collector behind --family-probe:25,000,000 atomic USDC, early response before host source+10; baseline request at+10/response by+15; own-quantity sell quotes5/30/300 seconds after each response,10percent exit windows. Same capture key/client, global/Jupiter/day budgets, legacy priority and0.25SOL/+30s cohorts preserved. Host/corrected clocks and unknown fee-net are explicit. Daily caps survive completed-day restart and enable/disable transitions. Source reviewed231021a53; runtime pin/guard/observation checks follow integration.

- [ ] Establish a repeatable net executable edge across independent launches before any new funded strategy promotion. The retained initial case is one atomic batch; earlier-entry profitability was not estimable. Workers continue collecting; source/runtime readiness is separate from performance evidence.

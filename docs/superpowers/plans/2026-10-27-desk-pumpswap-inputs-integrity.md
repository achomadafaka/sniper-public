# PumpSwap Desk inputs and integrity implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development; execute continuously with explicit non-overlapping ownership and review each deliverable.

**Goal:** Repair the known Desk input, tape, provenance and paper-risk gaps without inventing missing observations.

**Architecture:** Add backward-compatible low-level integrity receipts/readers; build one bound PS-facts block at a frozen market cut; route it only through a new v27 policy/cohort. Enforce paper loss and trusted intake provenance in the owning local stores. Reuse existing structure/context and collectors.

**Tech Stack:** Python, bounded indexed SQLite reads, existing gRPC decoder and subscription JUDGE.

**Spec:** `docs/superpowers/specs/2026-10-27-desk-pumpswap-inputs-integrity.md`.

## Global constraints

- Preserve funded a2 runtime, all money/.env/confirm limits, existing feed cadence, $25/five/shared40h/40SOL/12s and frozen c699.
- No retired curve capture or GPT narrative-arm activation, strategy thresholds/ranker, outcome comparisons or historical repairs by timestamp substitution.
- Host tests -n2; one full suite under shared host lock. Shared worktree commits use exact owned paths and crew lock; no stash/reset/amend/add-A.
- Reader ceiling20000 rows, scope128 invocations/32pools, optional diagnostic2048bytes and20/min; overflow unknown. Inclusion writes are deltas.

## Review focus

- Unknown/legacy/multipool losses and partial commit may never exempt the target pool.
- Both seed lineage and rows need receipt/slot cuts; no future metadata/FX.
- Coverage is actual included time; first observation never becomes coin birth or full60s.
- A caller-supplied proof hash cannot authenticate a row; verifier-issued receipt persists with admission.
- Kill/admit and final kill/fill transitions must be atomic; quotes run outside write locks.

### Task 1: Tape integrity (ps_tape_integrity_build)

**Files:** decoder, universe worker, intake book, ps_tape, risk_tape, new ps_losses and two dedicated test files.

**Interfaces:** `ps_losses.losses(desk_dir,pool,lo,hi,*,cut_slot=None,max_rows=20000)` returns individual applicable receipt times or raises `LossUnreadable`; `pool_coverage(desk_dir,pool,lo,hi)` returns proven intervals or raises. `load_ps_swaps` adds `base_decimals`, `cut_slot`, `max_rows`, `integrity` with explicit legacy defaults; new callers set integrity True and validated decimals.

- [ ] RED scoped/unknown/multipool/coalesced/pending/schema/overflow/partial-commit cases and decimal/cut/seed/late-sell cases.
- [ ] Implement additive complete-scope/batch proof, bounded diagnostics, real inclusion deltas and strict readers; preserve old global rows.
- [ ] GREEN focused decoder/tape/writer/RISK tests and treewalk; commit exact paths.

### Task 2: Paper/provenance integrity (paper_integrity_build)

**Files:** paper engine, new intake_proof, dedicated paper/source tests.

**Interfaces:** `paper.cohort_refusal(conn,cohort_id,*,now_iso)` for pre-spend and internal checks; `verify_source(candidate,*,intake_dir,policy_hash,now_fn)` produces a verifier-issued receipt; `source_proof_refusal(proof,candidate,*,policy_hash,as_of)` checks binding only, not authenticity.

- [ ] RED cumulative days/threshold/cohort/latch/unknown accounting, admission/close/fill races and duplicate transitions; source identity/poll/path/clock/delayed-restart and forged-proof cases.
- [ ] Implement paper-only latch/atomic admission/final-fill checks, continuing exits; implement indexed local source verifier with typed optional reported metadata.
- [ ] GREEN focused paper and provenance tests. Report incomplete legacy fixture setup before changing it; no fallback policy invented.

### Task 3: PS facts (ps_model_facts_build)

**Files:** structure_tape, new ps_facts, dedicated fact/causality tests.

**Interface:** `facts_for(candidate,*,desk_dir,as_of,receipt_cut,cut_slot,source_metadata=None,ours=frozenset(),ours_sigs=frozenset(),bounds=None,params=None)` returns closed `pumpswap-facts-v1`; `validate` strictly validates/deepcopies it. Claim knowledge time is as_of; market windows end at receipt_cut. Corrected UTC is host minus source offset.

- [ ] RED actual6/9decimals, futurecut/seed/witness, exclusions, short/partial coverage, true/pool/provider/observed ages and future creation metadata.
- [ ] Reuse structure and strict low-level APIs; report unknown histories honestly; bind output to mint/pool/refreshed snapshot/cuts.
- [ ] GREEN real offline fact/structure/schema tests; commit exact paths.

### Task 4: Integration and study version (root)

**Files:** contract renderer/validation, provisioning, JUDGE and worker, asof/evidence adapter integration, Desk store/assembler, context, new v27 playbook/preregistration and dedicated integration tests.

- [ ] RED newpolicy gating/legacy byte identity, scoped loss API at each reader, trusted proof before ingestion/RPC and replay after pruning, bound safety/PSfacts and healthy/stale PS context.
- [ ] Require trusted source-root verification for new admissions or matching stored verifier receipt; attach receipt atomically. Hard filter independently validates identity/time; no supplied passblob bypass.
- [ ] Forward separate bound safety and recomputed pure sell math; never extend intake_mint_of. Pass exact receipt_cut/slot/FX to facts and serialize/hash the actual sheet.
- [ ] Add explicit PS input enrollment; legacy default bytes unchanged. Permit Dex/X/crawler context while retired curve aggregates remain unknown. Create v27 with unchanged E/W/S grammar, timing/rail bounds and new fact semantics.
- [ ] Freeze new preregistration including actual known-time kill/cancellation and source/input/loss/coverage versions; do not recut old history or compare outcomes.

### Task 5: Review and runtime proof (root)

- [ ] Independent exact-SHA review and Knowledge Sync Gate; fix findings with focused RED/GREEN.
- [ ] Required pinned-release stage, schema/ledger validation and reconciliation, no backup/funded switch. Optional one bounded full-sheet subscription smoke only for the new input-size concern, no trade ticket.
- [ ] If generic decoder detail is still unidentified, activate reviewed diagnostic capture without changing model inputs, inspect bounded new receipts and reproduce the actual cause before claiming its repair.
- [ ] Present any new exact operator registration step only after concrete review/stage. After authority, verify both stores, drain prior paper work, cut over compatible paper pins only and verify actual sheet/receipt/refusals, open-position management and unchanged funded intents.
- [ ] Leave genuine unavailable-history/partial-site/unknown-scope limitations explicit. No claim of profitability or fabricated organic success.

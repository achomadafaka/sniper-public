# Routed PumpSwap Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete, causal pool membership for newly observed routed PS losses; no routed swaps or prices.

**Architecture:** A pure routed helper binds parent event logs to immediately following event-CPI bytes, invocation stacks, and each pinned route pool role. The recorder preserves the original loss and writes a separate immutable scope proof. Readers apply validated target relevance in SQL before LIMIT. Per-sheet loss reads are passed explicitly and never reused across sheets.

**Tech Stack:** Existing protobuf frames, solders, Borsh parsers, SQLite, functools bounded pure-proof LRU, pytest -n 2.

**Ownership:** Delta owns routed helper, universe-worker loss hook, ps_losses/ps_tape/ps_facts, focused tests and six retained public fixtures. Beta retains pumpswap.py and reviews integration (no Delta edit). Zeta independently reviews the exact tip. Codex owns release stamping/staging; Alpha leads.

**Authoritative rescope:** Alpha's attribution-only ruling supersedes the earlier leg-row design. Post-vault phases are unproven. No on_pumpswap recovery, event rows, reserve calculation, state mutation, or pricing follows a successful proof.

## Contract and edge cases

- prove_pools(logs, *, account_keys, instructions, inner_complete) returns sorted exact PS pools or DecodeLoss. Complete resolved ALT keys and ordered outer_ix/stack_height records are mandatory.
- Pair each parent's ordered Program-data log to its own next event-CPI instruction by exact bytes and successful invocation stack. No orphan child/log; reject reentrant PS trade invocations, mismatched parents, wrong roles, incomplete logs or missing groups.
- All route groups must be proven PS BuyEvents or explicitly bound non-PS Pump child trades. Exact-quote gross fee identity, mint chaining and adjacent PS base-out/next-quote-in chaining are consistency gates, never post-state proofs.
- Original ps_decode_losses remains four columns. New ps_loss_scope(signature,loss_key,pool_keys_json,complete,proof,decoder_version,known_at) binds signature/cause/receipt/slot/venue/IDL/protocol/checksum. Missing table/row, incomplete proof, malformed identity and future knowledge retain losses.
- Only complete target-absent membership excludes a loss. Target-present, global/unknown, unscoped fee/init/OptionBool cases and legacy NULL remain losses. Original batches must validate every member.
- Original writer14e remains compatible because its table is untouched. MEMECOIN_DESK_PS_ROUTED_SCOPE=0 disables new proof generation; source data and old proofs are never rewritten.
- No provider/production writes, runtime/environment/funded changes, cap increase, historical backfill, cohort rewrite or forced model call.

## Verified tasks

- [x] Reproduce global row-cap defect: >20,000 valid foreign proofs plus8target swaps. Preserve target/unknown overflow and a single target receipt amid foreign volume.
- [x] SQL validated relevance precedes LIMIT; full batch verification streams without charging foreign members to target cap.
- [x] Bounded full-content proof LRUs retain current DB reads and knowledge-cut checks.
- [x] Six actual retained frames: routed helper RED18; exact membership GREEN6 plus negative corruption cases.
- [x] Worker/store RED9 missing-table cases; separate scope table, no swap recovery, old four-column writer regression, malformed/partial/future/ALT/legacy cases.
- [x] Per-facts query reuse RED3queries vs1; explicit short-lived loss_reads, fresh next call, original extended seed interval (at most one extra read when a pre-window seed exists; its original knowledge cut is preserved).
- [x] Focused decoder/worker/loss/facts/treewalk suite214passed6.84s -n2 before final review.
- [x] Actual one-hour logical-copy measurement (21,197loss/gap rows,26target swaps): baseline p50/p90=3127.80/3183.01ms; single-read cold730.41ms, warm494.14/506.25ms. Real flow remains unknown/tape_loss.
- [x] Recorder42actual-frame replays: p50/p90=1.38/2.61ms,42complete scope proofs,0PS swap rows; DDL excluded. Alpha accepted warm facts timing and ruled no new scan budget.
- [x] Review regressions RED4 (struct/overflow/runtime parser failures and later-known seed proof), GREEN51focused2.82s including treewalk. Optional proof failures preserve loss/pending/witness/curve processing; log once per exception cause.
- [ ] Exact-SHA Beta/Zeta review; Codex stages only after PASS.

**Evidence limit:** Six retained full-frame witnesses are not the14,273 historical multi-hop receipts. No exact hour-wide conversion count is identifiable from Bloom-only history. Report fixture success separately; a forward decoder epoch is required. Phase2 post-vault proof remains unbuilt.

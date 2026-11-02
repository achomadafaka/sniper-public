# Memecoin V6 Evidence and Solana Capture Remediation Plan

> **Superseded 2026-07-26:** Historical completion record only. The approved live-paper design
> supersedes its replay goal and V5-as-funded framing. Do not execute new work from this plan; use
> `2026-07-26-memecoin-v6-live-paper-engine-design.md` and the live-paper plans.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and superpowers:test-driven-development task-by-task.

**Goal:** Make the frozen V6 evaluation reproducible and no-peek, while making Solana capture bounded, transaction-grounded, non-blocking to the exit-state materializer, and capable of observing native-SOL pump.fun curves.

**Architecture:** Keep funded execution unchanged. Research replay consumes immutable episode inputs and refuses unavailable prereg evidence. Solana RPC traffic uses one shared transport limiter, while a dedicated worker owns the Solana tape DB. New-vault capture starts at an explicit evidence floor rather than walking to genesis. AMM and pump.fun curve events carry explicit venue/provenance classes.

**Tech stack:** Python 3.13, SQLite, pytest, Solana JSON-RPC, existing memecoin exit feature/replay modules.

## Global Constraints

- No funded-policy authority changes, flag activation, deploy, service restart, live RPC test, or production DB mutation.
- Preserve V5 as the only funded engine. V6 remains replay/paper evidence only.
- Missing prereg evidence fails closed; it is never silently neutralized.
- All behavior changes are RED-first and use focused tests before broader memecoin verification.
- Preserve foreign `data/backups/` and `data/research/` files.

---

### Task 1: Replay and prospective-cohort truth

**Files:**
- Modify: `scripts/research/memecoin_v5_blind_replay.py`
- Modify: `scripts/research/memecoin_v5_feature_study.py`
- Modify: `scripts/research/memecoin_v6_action_replay.py`
- Modify: `docs/superpowers/evidence/v6_discovery_manifest.json`
- Create: `tests/test_memecoin_v6_action_replay.py`

**Interfaces:**
- `simulate(walk, policy)` fills at `priced_path_index + 1`.
- `evaluate_prospective(..., minimum_episodes=50)` returns status only below the floor.
- Fidelity is `entry_pool_exact`, `single_mapped_pool`, or `dominant_pool_proxy`; only the first is claimable as exact.
- A manifest binds canonical episode-input JSON SHA-256 values, not database size/count metadata.

- [x] Add failing tests for next-priced-block fill, permanent recovery cancellation, second-break partial confirmation, every-priced-block oracle, missing prereg evidence, no-peek floor, content fingerprint sensitivity, and non-exact single-pool labeling.
- [x] Run the focused tests and confirm each fails for the named defect.
- [x] Implement the smallest pure helpers and replay changes that make them pass.
- [x] Replace the quadratic grouped AUC loop with a weighted rank implementation; bind the result artifact to the local discovery CSV hash.
- [x] Regenerate the discovery manifest from the sealed pre-cutoff IDs and canonical episode inputs.
- [x] Run replay self-checks and focused tests.

### Task 2: One shared Solana RPC limiter

**Files:**
- Modify: `sniper/services/memecoin_solana_blocktime.py`
- Modify: `sniper/services/memecoin_solana_amounts.py`
- Modify: `sniper/services/memecoin_solana_tape.py`
- Modify: `tests/test_memecoin_solana_tape.py`

**Interfaces:**
- `build_read_rpc_call(url, min_call_s, max_429_retries, clock, sleep)` owns one cadence state for every method.
- `get_signatures_fn(call=...)`, `get_transaction_fn(call=...)`, and largest-account lookup share that call object.

- [x] Add failing tests proving aggregate spacing across mixed RPC methods, one 429 retry, second-429 failure, and finite positive cadence validation.
- [x] Confirm adapters currently swallow the retry and independent limiters permit back-to-back calls.
- [x] Move retry/pacing below the adapters into the common raw-call seam.
- [x] Run the focused transport and tape tests.

### Task 3: Bounded, correct vault resolution and bootstrap

**Files:**
- Modify: `sniper/services/memecoin_solana_amounts.py`
- Modify: `sniper/services/memecoin_solana_tape.py`
- Modify: `sniper/analysis/tick_features.py`
- Modify: `tests/test_memecoin_sol_tape_paper.py`
- Modify: `tests/test_tick_feature_mint_resolution.py`

**Interfaces:**
- Pool resolution requires positive post-event inventory for every candidate and records source transaction/provenance.
- Seed search returns `(transaction, get_transaction_count)` and distributes its bound across candidate accounts.
- Transfer fallback returns `(candidates, complete)`; any failed chunk makes the mapping attempt incomplete.
- New cursors carry an explicit floor derived from the earliest relevant position time minus required feature history.
- Only entry/manual-buy fills establish entry identity. Unbound positions never inherit mint-level
  venue caches, NULL signature times cannot be skipped at the floor, and invalid position times
  are not allowed to trigger a genesis walk.
- Hot and pull consumers resolve the same persisted `sol_position_pools` binding.

- [x] Add failing tests for a single zero-balance router, later-account seed success, exact resolution-budget accounting, failed transfer chunks, entry-fill identity, stale mint caches, NULL block times, hot/pull pool parity, and no-genesis bootstrap.
- [x] Confirm the defects, then implement minimal fail-closed fixes.
- [x] Run focused resolver/pagination tests.

### Task 4: Solana worker isolation

**Files:**
- Create: `scripts/memecoin_sol_tape_worker.py`
- Create: `scripts/memecoin_sol_tape_watchdog.sh`
- Modify: `scripts/memecoin_exit_state_worker.py`
- Modify: `scripts/wal_checkpoint.py`
- Modify: `scripts/engine_smoke.py`
- Modify: `tests/test_exit_materializer_wiring.py`
- Create: `tests/test_memecoin_sol_tape_worker.py`

**Interfaces:**
- Exit-state worker never imports or calls `solana_tape_pass`.
- Solana worker owns its ledger read handle, tape write handle, shared RPC call, period gate, and bounded pass loop.
- Both processes remain default-OFF under validated configuration.

- [x] Add a failing structural test proving the materializer has no tape/RPC execution surface.
- [x] Add worker gate/connection/period tests.
- [x] Move tape orchestration into the dedicated worker and update smoke/checkpoint surfaces.
- [x] Run focused worker tests.

### Task 5: Native-SOL pump.fun curve capture

**Files:**
- Modify: `sniper/services/memecoin_solana_amounts.py`
- Modify: `sniper/services/memecoin_solana_tape.py`
- Create: `tests/test_memecoin_pump_curve_tape.py`

**Interfaces:**
- A curve event is authorized only by the known pump.fun program plus a mint-matching token-account delta and the curve PDA's native-lamport delta in the same confirmed transaction.
- Rows use `sol_curve_swap`/`sol_curve_sync`, explicit `venue_kind=pump_curve`, transaction signature, slot, and block time.
- Ambiguous owners, fee-only lamport movements, missing balances, or unsupported programs return no rows.

- [x] Add synthetic transaction fixtures for curve buy, curve sell, unrelated native transfer, ambiguous curve owners, and graduation to AMM.
- [x] Confirm current decoder rejects legitimate curve events.
- [x] Implement the program-specific decoder and tape routing without weakening AMM validation.
- [x] Run focused curve and Solana tape tests.

### Task 6: Verification, rereview, and publication

**Files:**
- Modify only evidence/docs made stale by the final behavior.

- [x] Run all focused tests from Tasks 1-5.
- [x] Run the broad affected memecoin test selection, schema validator, compileall, and `git diff --check`; do not run the full unfiltered suite.
- [x] Reread the complete diff against every finding and verify default-OFF/funded-path invariants.
- [x] Run the Knowledge Sync Gate and record the knowledge delta.
- [x] Stage explicit paths, commit logical changes, verify clean tracked state, and push `main` without touching foreign data.

## Knowledge Sync Gate

- Doctrine changed: yes — exact replay evidence now requires entry-pool identity and point-in-time
  prereg inputs; mutable DB metadata is not a sealed cohort.
- Architecture changed: yes — `scripts/memecoin_sol_tape_worker.py` is the sole Solana pull-tape
  writer; the exit-state worker is a read-only consumer.
- New module/table: dedicated worker/watchdog and `sol_position_pools` position-to-venue binding.
- Research finding changed: yes — V6 superiority retracted; the former “exact” discovery subset is
  single-mapped-pool proxy evidence, and exact V6 is unscorable on historical inputs.
- Synced surfaces: `AGENTS.md`, V6 prereg, review handoff, canonical discovery manifest/bundle, and
  grouped-AUC hash artifact.
- Supabase memory write: not performed; production persistence is outside this remediation's
  authority. The committed docs are the in-repo source of truth for this direct-main fix.

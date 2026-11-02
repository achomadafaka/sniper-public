# HL Alt-Short Regime Replication Implementation Plan

**Goal:** Prevent one bearish tape from machine-promoting the standalone HL alt-short candidate, and quantify the historical exit-observation gap.

**Architecture:** Persist causal hourly regime observations beside the existing paper ledger. Assign conservative episode IDs in a pure helper. Feed episode evidence into the existing verdict spine. Preserve v2 and emit a superseding v3 report from the same sealed inputs.

**Tech:** Python, SQLite, pytest, existing cache-only study harness.

## Task 1: Correct provenance and empty-regime semantics

**Files:**

- Modify: `scripts/hl_altshort_study.py`
- Modify: `sniper/hyperlive/altshort_signal_paper.py`
- Modify: `data/research/altshort_study/analyze.py`
- Test: `tests/test_hl_altshort_study.py`
- Test: `tests/test_hl_altshort_signal_paper.py`

1. Add failing tests proving unrelated tracked changes do not dirty the study pin and empty/NaN baskets label `unknown`.
2. Run the focused tests and confirm RED.
3. Scope `_git_state` to `code_paths`; centralize the regime label helper and use it in the legacy analyzer.
4. Run the focused tests and confirm GREEN.

## Task 2: Persist hourly regime observations

**Files:**

- Modify: `sniper/hyperlive/intel_store.py`
- Modify: `scripts/run_hl_altshort_signal_paper.py`
- Test: `tests/test_hl_altshort_signal_store.py`
- Test: `tests/test_run_hl_altshort_signal_paper.py`

1. Add failing schema/store tests for immutable, epoch-bounded regime rows.
2. Add a failing runner test proving a processed hour is stored even without a crossing.
3. Add `hl_altshort_signal_regimes`, store/list helpers, and scanner persistence before crossing decisions.
4. Run focused tests.

## Task 3: Assign and gate on regime episodes

**Files:**

- Modify: `sniper/hyperlive/altshort_signal_paper.py`
- Modify: `scripts/run_hl_altshort_signal_paper.py`
- Modify: `sniper/hyperlive/altshort_evidence.py`
- Modify: `scripts/hl_altshort_signal_arm.py`
- Test: `tests/test_hl_altshort_signal_paper.py`
- Test: `tests/test_hl_altshort_verdict.py`
- Test: `tests/test_hl_altshort_signal_arm_report.py`
- Test: `tests/test_run_hl_altshort_signal_paper.py`

1. Add failing pure tests for one episode, 24-hour separation, and unknown interruption.
2. Add failing verdict tests for one-episode continuation and negative replicated-episode rejection.
3. Add failing report tests for per-episode residual evidence.
4. Implement episode assignment, store `episode_id` on decisions, and extend the verdict/report contract.
5. Run focused tests.

## Task 4: Add the sampled-exit historical bridge

**Files:**

- Modify: `scripts/hl_altshort_study.py`
- Test: `tests/test_hl_altshort_study.py`

1. Add failing tests for hourly-close stop, take-profit, time-cap, and matched exit clocks.
2. Implement the cache-only bridge without changing frozen primary metrics.
3. Surface bridge signal, basket, residual, and gap in the report.
4. Run focused tests.

## Task 5: Seal v3 and roll the paper arm

**Files:**

- Add: `data/research/altshort_study/manifest.v3.json`
- Add: `data/research/altshort_study/standalone_forward_arm.v2.json`
- Add: `data/research/altshort_study/MEMO.v3.md`
- Modify: `data/research/altshort_study/MEMO.v2.md`
- Modify: `scripts/watchdogs/hl_altshort_signal_watchdog.sh`
- Modify: `tests/test_hl_altshort_watchdog_config.py`
- Modify: `HANDOFF.md`

1. Commit the tested runtime/study code so manifests can pin an immutable SHA.
2. Add v3 manifest and v2 forward-arm manifest with the new thresholds.
3. Generate v3 outputs cache-only; verify the verdict is `paper_continue` for one episode.
4. Back up the paper DB, roll only the standalone paper scanner, and verify one process, current pin, post-epoch rows, and unchanged dry-run real leg.
5. Run the Knowledge Sync audit-only gate and record the required research-memory proposal without writing Supabase.

## Task 6: Final verification

1. Run the focused HL alt-short suite.
2. Run the broader HL tests selected by file inventory.
3. Run `python3 scripts/validate_schema_refs.py` and `git diff --check`.
4. Verify v2 report hash is unchanged and v3 output reproduces byte-identically.
5. Inspect process, heartbeat, cron, DB epochs, funded dry-run heartbeat, and branch status.

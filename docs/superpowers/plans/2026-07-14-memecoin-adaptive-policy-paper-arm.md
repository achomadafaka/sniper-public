# Memecoin Adaptive Policy Paper Arm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the interrupted adaptive-policy work by recording the full signal-time feature vector and running an immutable, mcap-conditioned `scalp_c` paper arm on the same curated entries as the existing controls.

**Architecture:** The existing entry stats call supplies market, volatility, volume, and transaction facts without another network request. Each `scalp_c` position freezes its signal-time mcap and exact serialized `ExitPolicy` in `memecoin_positions`; the existing policy version and assignment tables receive matching audit rows. The paper exit sweep evaluates only that frozen policy. No live entry gate, real position, funded execution path, or real exit rail changes.

**Tech Stack:** Python 3, SQLite, pytest, existing memecoin copy/exit services.

## Global Constraints

- `scalp_c` is paper-only and uses the same entry fill as all other paper arms.
- No new HTTP request may be added to the copy decision path.
- Missing features remain `null`; they are never coerced to zero or a favorable band.
- Every open `scalp_c` position freezes policy ID, version, and canonical JSON at entry.
- Existing `control`, `momentum_a`, `alpha_b`, `real_shadow`, and real-money behavior remain unchanged.
- The curated paired study is descriptive until at least 30 distinct big-cap mints have usable forward paths; the current two-mint sample cannot select production parameters.

---

### Task 1: Complete signal-time feature persistence

**Files:**
- Modify: `sniper/services/memecoin_features.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Test: `tests/test_memecoin_signal_features.py`
- Test: `tests/test_memecoin_entry_snapshot.py`

**Interfaces:**
- Consumes: the existing `stats_fn(mint, network_id=...) -> dict | None` call already made during entry processing.
- Produces: `build_snapshot(..., extra_features: dict | None)` and positions with immutable `entry_mcap`.

- [ ] Add a failing integration test that calls `copy_event_to_arms`, asserts `stats_fn` is called once, and verifies `memecoin_entry_snapshots.features_json` contains `price_change_*`, `volume_*`, `txns_m5_*`, feed mcap, DEX mcap, first-buy, lag, and the raw trader profile when available.
- [ ] Add a failing migration/write test proving `memecoin_positions.entry_mcap` exists and uses feed mcap first, DEX mcap only as fallback, and `NULL` when both are unknown.
- [ ] Run the two tests and verify failures are caused by discarded stats and the missing column.
- [ ] Extend `build_snapshot` with `extra_features=None`; merge only JSON-safe values into `features_json` while keeping promoted columns authoritative.
- [ ] Return the existing entry stats dict plus derived token age from `_entry_stats`; pass the complete allowed feature subset to `_log_entry_snapshot` and `entry_mcap` to `_open_arm`.
- [ ] Add the additive `entry_mcap REAL` migration and `open_position(..., entry_mcap=None)` insert binding.
- [ ] Run focused tests and commit as one capture-only change.

### Task 2: Freeze and audit `scalp_c` policy assignments

**Files:**
- Modify: `sniper/services/memecoin_policy.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Test: `tests/test_memecoin_scalp_arm.py`
- Test: `tests/test_memecoin_observation_schema.py`

**Interfaces:**
- Produces: `resolve_policy(snapshot, arm="scalp_c") -> tuple[ExitPolicy, str]`, `serialize_policy(policy) -> str`, and `deserialize_policy(payload) -> ExitPolicy`.
- Persists: `memecoin_positions.assigned_policy_id`, `assigned_policy_version`, `assigned_policy_json`; matching immutable rows in `memecoin_policy_versions` and `memecoin_policy_assignments`.

- [ ] Port Claude's resolver tests first and verify they fail because `scalp_c` is unknown.
- [ ] Add failing tests proving policy JSON round-trips, invalid/non-finite values are rejected, and changing settings after entry cannot change the assigned policy.
- [ ] Add additive position-policy columns and `policy_version INTEGER` on assignments; update schema expectations.
- [ ] Implement the four launch bands as an explicitly unvalidated `scalp_c_v1` experiment. Unknown mcap falls back to the fixed control rail and still opens.
- [ ] Canonically serialize finite policy values (`max_hold_s=null` means no time cap), hash the JSON, and reject policy-ID/hash collisions.
- [ ] Insert the policy columns in the same `memecoin_positions` row as the position. Write version/assignment audit rows idempotently after the position opens; audit failure logs but never changes other arms.
- [ ] Run focused tests and commit.

### Task 3: Run the paper arm through the frozen policy

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Modify: `sniper/services/memecoin_exits.py`
- Test: `tests/test_memecoin_scalp_arm.py`
- Test: `tests/test_memecoin_fanout.py`
- Test: `tests/test_memecoin_runner.py`
- Test: `tests/test_memecoin_exits.py`

**Interfaces:**
- Consumes: position `assigned_policy_json` created in Task 2.
- Produces: `policy_exit_reason(policy, pnl_pct, held_s) -> str | None` and paper close reasons `scalp_take_profit`, `scalp_stop`, `scalp_timecap`.

- [ ] Add failing tests proving the default paper fanout opens `scalp_c` at the identical fill; sell mirroring includes it; TP, stop, and time cap close it; no-quote time cap uses the existing stale/noquote close path.
- [ ] Add `scalp_c` to the default paper arms and mirror set only.
- [ ] Implement the generic frozen-policy reason function and a `scalp_c` sweep loop that reads assigned JSON, updates quote/peak state, and closes via existing `_close`/`_timecap_no_quote` helpers.
- [ ] Verify existing arms' outcomes and constants are unchanged.
- [ ] Run focused tests and commit.

### Task 4: Preserve the honest study verdict and close verification

**Files:**
- Create: `scripts/research/memecoin_bigcap_scalp_policy_study.py`
- Create: `docs/research/2026-07-14-memecoin-bigcap-scalp-policy-study.md`
- Modify: generated brain index files only if the repository validator requires it.

**Interfaces:**
- Consumes: paper positions, entry events, shared position marks, and the existing slippage haircut.
- Produces: a read-only, mint-deduplicated paired study and a report labeled `INSUFFICIENT DATA`.

- [ ] Port and review the read-only study script; keep the experimental unit at distinct mint and the policy fit separate from evaluation.
- [ ] Reproduce against `sniper_tracker.db` in read-only mode. Deduplicate before simulation and use a fixed horizon independent of existing-arm close times. Record the corrected result: 9 usable earliest-mint paths and zero in `>=30M`; no production parameter selection is possible.
- [ ] Write the report with exact cohort, coverage, pairing, slippage, and power limitations. Do not commit binary matrices or treat 53 repeated positions as 53 independent samples.
- [ ] Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables` and the focused memecoin suite.
- [ ] Run the repository's non-stress pytest suite if focused verification is green.
- [ ] Run `git diff --check`, inspect the final diff, and commit the report/verification change.

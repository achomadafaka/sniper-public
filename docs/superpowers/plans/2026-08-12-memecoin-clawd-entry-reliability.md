# Memecoin CLAWD Entry Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Reduce armed-wallet receipt delay and remove the false CLAWD entry refusals.

**Architecture:** Keep Fomo requests outside SQLite work. Use the existing reverse quote to verify a Jupiter impact sentinel.

**Tech Stack:** Python, SQLite, pytest, the Fomo REST API, and Jupiter Swap V1.

## Global Constraints

- Do not change the market-cap limit.
- Do not weaken identity, reservation, token safety, signing, submission, confirmation, or accounting rules.
- Run focused tests only.
- Use the pinned runtime deployment script.

---

### Task 1: Follow graph fallback

**Files:**

- Modify: `sniper/services/memecoin_follow_sync.py`
- Test: `tests/test_memecoin_follow_sync.py`

**Interfaces:**

- Consumes: `_default_following_ids_fn(auth) -> set[str]`
- Produces: `_default_following_fn(auth, you) -> tuple[list[dict], bool, str | None]`

- [ ] Add a test where the paginated endpoint fails and `followingIds` succeeds.
- [ ] Run the named test and confirm the expected failure.
- [ ] Add the ID-only fallback and mark it with `following_ids_fallback`.
- [ ] Prevent the ID-only fallback from the pause pass.
- [ ] Run `tests/test_memecoin_follow_sync.py`.

### Task 2: Fast follow receipt and exact lag

**Files:**

- Modify: `sniper/services/memecoin_follow_activity.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Test: `tests/test_memecoin_follow_fast_lane.py`
- Test: `tests/test_memecoin_follow_activity.py`

**Interfaces:**

- Consumes: `follow_activity_pass(..., fetch_workers: int = 1)`
- Produces: four bounded request workers and serialized event processing

- [ ] Add a test that blocks one wallet and permits a second wallet response.
- [ ] Add a test that derives `lag_s` from the response receipt time.
- [ ] Run both named tests and confirm the expected failures.
- [ ] Add the bounded fetch iterator with four workers on the fast lane.
- [ ] Keep all database reads and writes on the caller thread.
- [ ] Use the response receipt timestamp for the event age and stored lag.
- [ ] Run the two named test files.

### Task 3: Fresh follow chase rule

**Files:**

- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Test: `tests/test_memecoin_translation_gates.py`

**Interfaces:**

- Consumes: `CopyEvent.signal_source` and `CopyEvent.lag_s`
- Produces: `is_fresh_follow_event(ev, max_lag_s) -> bool`

- [ ] Add a test where a 40-second follow event uses the event-price cap.
- [ ] Add a test where a 61-second follow event keeps the first-seen cap.
- [ ] Run both named tests and confirm the expected failures.
- [ ] Add the 60-second and 40-percent settings.
- [ ] Select one chase anchor from the measured receipt lag.
- [ ] Run `tests/test_memecoin_translation_gates.py`.

### Task 4: Jupiter impact sentinel

**Files:**

- Modify: `sniper/execution/solana_swap_executor.py`
- Test: `tests/test_memecoin_entry_impact_cap.py`

**Interfaces:**

- Consumes: the existing entry quote and planned-quantity reverse quote
- Produces: `roundtrip_cost_pct` for a sentinel quote

- [ ] Add a test where `100%` impact has a safe exact sell-back quote.
- [ ] Add tests for a costly or missing sell-back quote.
- [ ] Run the named tests and confirm the expected failures.
- [ ] Defer only impact values at or above 99.9 percent.
- [ ] Permit the route only when sell-back cost is at most twice the impact cap.
- [ ] Run `tests/test_memecoin_entry_impact_cap.py` and `tests/test_memecoin_entry_executor.py`.

### Task 5: Merge, deploy, and arm

**Files:**

- Modify through the operator flow: `sniper_tracker.db`
- Deploy through: `scripts/deploy_memecoin_runtime.sh`

**Interfaces:**

- Consumes: the merged commit on local `main`
- Produces: one pinned runner and Solana authority for `runitbackghost`

- [ ] Run `git diff --check` and the four focused test files.
- [ ] Commit the implementation with a Simplified Technical English message.
- [ ] Run the Knowledge Sync Gate.
- [ ] Merge the feature branch into local `main` without a rebase or squash.
- [ ] Confirm zero unresolved reservations and one current runner.
- [ ] Deploy the merged commit with the deployment script.
- [ ] Verify the release link, manifest, process path, heartbeat, and logs.
- [ ] Arm `runitbackghost` on Solana with the approved force flag.
- [ ] Verify Fomo follow state and effective Solana authority.

# Memecoin funded mirror-exit consensus guard implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a source wallet's partial sell or a sell against fresh armed-wallet consensus from blindly full-closing the exact funded coin.

**Architecture:** Add two local SQLite evidence readers over immutable v2 feed observations, then apply them only inside `real_mirror_on_sell()`. Evidence can suppress the mirror executor but cannot suppress any independent price, time, manual, or safety exit rail. Reuse `memecoin_exit_trigger_audit` for deterministic decisions.

**Tech Stack:** Python 3, SQLite/JSON1, pytest, existing Sniper memecoin runtime.

## Global Constraints

- Robinhood is network `4663`, never generic EVM.
- Coin identity is `(network_id, mint)`; never borrow holder evidence across chains.
- Missing or malformed evidence preserves the existing mirror behavior.
- No new network request, balance cache, safety cache, or quote cache.
- Paper exits, sizing, reservations, capital accounting, and duplicate-submit behavior do not change.
- Stop-loss, take-profit, mcap target, de-risk, time-cap, unquotable, manual, and stuck-bag exits remain active.
- Source and holder evidence must use exact upstream IDs and our local observed timestamp domain.

---

### Task 1: Exact holding-evidence readers

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Test: `tests/test_memecoin_real_runner.py`

**Interfaces:**
- Produces: `source_remaining_usd_for_event(conn, upstream_event_id, *, source_user_id, network_id, mint) -> float | None`.
- Produces: `active_armed_holders_for_coin(conn, *, network_id, mint, exclude_user_id, observed_at, window_s, min_remaining_usd) -> list[dict]`.
- The holder dict contains `source_user_id`, `source_handle`, `remaining_usd`, and `observed_at`.

- [ ] **Step 1: Write failing persistence tests**

Add v2 observations through `insert_feed_observation_v2()` and assert:

```python
identity = {"source_user_id": "u1", "network_id": "4663", "mint": "0xcoin"}
assert m.source_remaining_usd_for_event(c, "partial-sell", **identity) == pytest.approx(2394.31)
assert m.source_remaining_usd_for_event(c, "full-sell", **identity) == 0.0
assert m.source_remaining_usd_for_event(c, "missing", **identity) is None
```

Create same-mint observations for active armed, paused, excluded, unarmed, stale, cross-chain, and
latest-full-sell wallets. Assert the result contains only the active, fresh, exact-chain wallet whose
latest explicit remaining position is at least `$1`.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_real_runner.py -q
```

Expected: failure because both persistence helpers are absent.

- [ ] **Step 3: Implement strict raw-evidence parsing**

Add a private parser that returns an explicit non-negative USD value only from:

```python
body["currentSizeUsd"]
# or, only for single_user_sell:
body["humanUsdAmount"]
```

Reject missing keys, booleans, non-finite numbers, and malformed JSON as `None`. Preserve explicit
zero. Clamp tiny negative floating-point residue to zero.

Implement `source_remaining_usd_for_event()` as one exact
`(upstream_event_id, source_user_id, network_id, mint)` lookup.

Implement `active_armed_holders_for_coin()` as an indexed query of
`memecoin_feed_observations_v2` joined to the current `memecoin_wallets` roster with:

```sql
real_approved=1 AND paused=0 AND excluded=0
```

Filter by exact normalized network, exact mint, other user, `normalized_side IN ('buy','sell')`, and
our `observed_at` window. Order newest first per user; only each user's latest explicit observation
may establish a holding.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
python3 -m pytest tests/test_memecoin_real_runner.py -q
```

Expected: all tests pass.

### Task 2: Funded mirror decision and audit

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Test: `tests/test_memecoin_real_runner.py`
- Test: `tests/test_memecoin_risk_settings.py`

**Interfaces:**
- Consumes: the two Task 1 persistence helpers.
- Produces: held result `{"status": "mirror_held", "reason": str, "position_id": int, "source_remaining_usd": float | None, "consensus_count": int, "consensus_handles": list[str]}`.

- [ ] **Step 1: Write failing funded-behavior tests**

Add separate tests proving:

```python
# Exact source partial sell
assert result["status"] == "mirror_held"
assert result["reason"] == "source_still_holds"
assert exit_calls == []

# Exact full sell plus another fresh active armed holder
assert result["status"] == "mirror_held"
assert result["reason"] == "armed_consensus"
assert result["consensus_count"] == 1
assert exit_calls == []

# Full sell without qualifying evidence
assert result["status"] == "closed"
assert exit_calls == [(position_id, "mirror")]
```

For each decision, query `memecoin_exit_trigger_audit` and assert `reason='mirror'`, correct
`allowed`, exact upstream event ID, exact network, and deterministic detail code. Preserve the
existing missing-v2-evidence and `mirror_disabled` tests.

Add settings validation tests for non-finite/non-positive window and remainder floor.

- [ ] **Step 2: Run behavior tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_real_runner.py tests/test_memecoin_risk_settings.py -q
```

Expected: new mirror-hold assertions fail because the executor still fires.

- [ ] **Step 3: Implement the minimal funded guard**

Add defaults:

```python
MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S = float(os.getenv(
    "MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S", "3600"))
MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD = float(os.getenv(
    "MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD", "1"))
```

Validate both as finite and strictly positive in `validate_real_exit_policy()`.

In `real_mirror_on_sell()`, after the existing position/operator/context guards:

1. Sample `decision_at = now_fn()` once.
2. Read exact source remainder and fresh other-holder evidence.
3. Choose `source_still_holds`, `armed_consensus`, or `mirror_allowed`.
4. Write `memecoin_exit_trigger_audit` with sorted handles and `json.dumps(..., sort_keys=True)`.
5. Return `mirror_held` for either hold reason; otherwise call the existing `exit_fn` with the same
   `decision_at`.

Do not call wallet demotion on a held result.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
python3 -m pytest tests/test_memecoin_real_runner.py tests/test_memecoin_real_deploy.py tests/test_memecoin_risk_settings.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the implementation**

```bash
git add sniper/persistence/db_memecoin.py sniper/settings/memecoin.py \
  sniper/services/memecoin_real_runner.py tests/test_memecoin_real_runner.py \
  tests/test_memecoin_risk_settings.py
git commit -m "fix(memecoin): guard funded mirror exits with holder consensus"
```

### Task 3: Adversarial verification and release

**Files:**
- Verify all changed files.
- Update only the Knowledge Sync material required by the branch-close gate.

**Interfaces:**
- Consumes: the complete feature branch.
- Produces: clean merged main and exact-SHA pinned funded runtime.

- [ ] **Step 1: Review the complete diff adversarially**

Check chain identity, exact-event attribution, observed-clock windows, current arming, stale evidence,
missing JSON, explicit zero, dust, audit ordering, operator override ordering, executor call count, and
unchanged paper behavior.

- [ ] **Step 2: Run the required verification suite**

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/test_memecoin*.py -q
python3 -m compileall -q sniper scripts
git diff --check main...HEAD
```

Expected: zero failures and no diff errors.

- [ ] **Step 3: Run the Knowledge Sync Gate**

Record the new funded exit doctrine in the branch-close report. No Supabase write is required unless
the gate identifies a durable-memory delta that is not represented by this spec.

- [ ] **Step 4: Merge and push**

Verify main and origin/main have not diverged, fast-forward merge the feature branch, push main, and
confirm local/remote SHA equality. Do not rewrite history.

- [ ] **Step 5: Deploy the exact merged SHA**

Confirm `memecoin_pending_entries.status='reserved'` is zero, then run:

```bash
scripts/deploy_memecoin_runtime.sh <exact-merged-sha>
```

The deploy script's mandatory consistency snapshot may be created transiently; remove only that new
snapshot after successful verification because the operator requested no retained backup.

- [ ] **Step 6: Verify and clean**

Confirm one process runs from `.runtime/memecoin/current`, runtime verifier `ok=true`, heartbeat and
exit sweep are current, reservations remain zero, and no duplicate process exists. Remove the feature
worktree and local/remote feature branches. End with clean `main...origin/main` and no retained deploy
backup from this release.

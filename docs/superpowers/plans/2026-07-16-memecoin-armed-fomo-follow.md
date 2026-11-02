# Armed Wallet Fomo Follow Invariant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee every effective armed memecoin wallet is followed on Fomo while never unfollowing wallets or weakening the existing promotion gate.

**Architecture:** Add one authenticated Fomo JSON-write seam, extract the existing real-approval gate into a reusable read-only preview, enforce Fomo follow before a new approval is persisted, and repair missing follows in the existing five-minute follow-sync pass. Network work stays outside the entry and exit paths. Existing armed wallets remain armed on Fomo failure.

**Tech Stack:** Python 3, `urllib.request`, Flask, SQLite, pytest.

## Global constraints

- The invariant is one-way: effective armed implies followed; disarm/pause/exclude never unfollows.
- No Fomo request may run from funded entry, reservation, quote, safety, fill, or exit code.
- A new wallet is approved only after its Fomo follow exists.
- A Fomo outage never revokes an existing approval.
- Promotion thresholds, force semantics, audit evidence, and fast-lane membership remain unchanged.
- All network seams are injectable; tests make no external requests.

---

## Task 1: Add an authenticated Fomo JSON request seam

**Files:**

- Modify: `sniper/feeds/fomo_feed.py`
- Modify: `tests/test_memecoin_auth.py`

- [ ] Add failing tests proving a POST sends bearer auth and the exact JSON body.
- [ ] Add a failing test proving HTTP 401/403 refreshes once and retries with the rotated token.
- [ ] Run `python3 -m pytest tests/test_memecoin_auth.py -q` and confirm the new tests fail for the missing seam.
- [ ] Implement `fomo_request(auth, method, path, *, body=None, http=None, retry=True)` by sharing the existing `fomo_get()` authentication and single-refresh behavior.
- [ ] Keep `fomo_get()` as a compatibility wrapper around `fomo_request()`.
- [ ] Run the focused test and commit.

Expected interface:

```python
status, payload, date_header = fomo_request(
    auth,
    "POST",
    "/follows",
    body={"user_id": operator_id, "following_id": target_id},
)
```

Verification:

```bash
python3 -m pytest tests/test_memecoin_auth.py -q
git diff --check
git commit -am "feat(memecoin): add authenticated Fomo request seam"
```

---

## Task 2: Extract the real-approval gate preview

**Files:**

- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `tests/test_memecoin_real_approval_gate.py`

- [ ] Add failing tests for a read-only `real_approval_preview()` covering qualified, refused, and forced approvals without changing `real_approved` or writing audit rows.
- [ ] Add a failing test for a public Fomo-follow refusal audit helper with action `approve_refused_fomo_follow` and bounded failure text.
- [ ] Run the focused test and confirm RED.
- [ ] Extract the current threshold calculation without changing results.
- [ ] Make `set_wallet_real_approved()` consume the same preview result.
- [ ] Add the narrow audit helper used only when an otherwise eligible new arm cannot establish its Fomo follow.
- [ ] Run the approval suite and commit.

Expected preview shape:

```python
{
    "ok": True,
    "forced": False,
    "reason": "qualified",
    "stats": {"closes": 8, "total_pnl": 143.2},
}
```

Verification:

```bash
python3 -m pytest tests/test_memecoin_real_approval_gate.py -q
git diff --check
git commit -am "refactor(memecoin): share real approval gate preview"
```

---

## Task 3: Repair missing follows in the periodic sync

**Files:**

- Modify: `sniper/services/memecoin_follow_sync.py`
- Modify: `tests/test_memecoin_follow_sync.py`

- [ ] Add failing tests for exact armed counts, successful missing-follow repair, and no duplicate POST for already-followed wallets.
- [ ] Add failing tests proving one failed follow does not stop later wallets or revoke the failed wallet.
- [ ] Add failing tests proving an armed `source='follow'` row is repaired before pause reconciliation and remains unpaused.
- [ ] Add failing tests proving paused, excluded, and unapproved rows are not reverse-followed and an incomplete graph performs no reverse mutation.
- [ ] Run `python3 -m pytest tests/test_memecoin_follow_sync.py -q` and confirm RED.
- [ ] Add default helpers for current user, following IDs, and one Fomo follow mutation.
- [ ] Snapshot effective armed IDs, repair missing follows from the existing complete graph, update the in-memory graph on success, then run the existing inbound reconciliation.
- [ ] Isolate failures per target and return deterministic counts plus failed IDs.
- [ ] Run the follow-sync and fast-lane suites and commit.

Expected result fields:

```python
{
    "armed": 31,
    "armed_already_followed": 11,
    "armed_followed": 20,
    "armed_follow_failed": 0,
    "armed_follow_failures": [],
}
```

Verification:

```bash
python3 -m pytest tests/test_memecoin_follow_sync.py tests/test_memecoin_follow_fast_lane.py -q
git diff --check
git commit -am "feat(memecoin): repair Fomo follows for armed wallets"
```

---

## Task 4: Enforce follow-before-arm in the operator API

**Files:**

- Modify: `sniper/routes/api_memecoin.py`
- Modify: `tests/test_memecoin_follow_sync.py`
- Modify: `tests/test_memecoin_real_approval_gate.py`

- [ ] Add failing route tests proving a qualified wallet is followed before approval is persisted.
- [ ] Add failing tests for already-followed idempotency, gate refusal with zero Fomo calls, and Fomo failure leaving the wallet unarmed with `fomo_follow_failed`.
- [ ] Add a failing test proving disarm makes no Fomo request.
- [ ] Run the route-focused tests and confirm RED.
- [ ] Wire injected followed-ID and follow-target functions into the blueprint context.
- [ ] On approval, preview the gate, establish/confirm the follow, then call the existing audited approval writer.
- [ ] On Fomo failure, write `approve_refused_fomo_follow`, return a named service error, and keep approval off.
- [ ] Keep disarm on the current local-only path.
- [ ] Pass the injected follow mutation into the manual and scheduled sync surfaces.
- [ ] Run the focused suites and commit.

Verification:

```bash
python3 -m pytest tests/test_memecoin_follow_sync.py tests/test_memecoin_real_approval_gate.py tests/test_memecoin_follow_fast_lane.py -q
git diff --check
git commit -am "feat(memecoin): follow wallets before arming"
```

---

## Task 5: Add operator visibility and adversarial checks

**Files:**

- Modify if required: `scripts/memecoin_runner_loop.py`
- Modify if required: relevant runner/runtime tests

- [ ] Ensure the scheduled sync logs nonzero repaired and failed counts without logging credentials.
- [ ] Add or update a focused runner test only if the logging contract changes.
- [ ] Adversarially review all call sites for accidental unfollow, entry-hot-path access, approval-before-follow ordering, and pause-before-repair ordering.
- [ ] Verify concurrent reservation and duplicate-submit behavior is untouched.
- [ ] Commit any visibility-only change separately.

Verification:

```bash
rg -n "DELETE|unfollow|fomo_request|sync_followed|set_wallet_real_approved" sniper scripts tests
python3 -m pytest tests/test_memecoin_follow_sync.py tests/test_memecoin_follow_fast_lane.py tests/test_memecoin_real_approval_gate.py tests/test_memecoin_real_deploy.py -q
```

---

## Task 6: Full verification and Knowledge Sync Gate

**Files:**

- Update only if the gate requires it: current knowledge records or operator docs

- [ ] Run the schema-reference validator.
- [ ] Run every `tests/test_memecoin*.py` test.
- [ ] Run relevant concurrency, runtime-deploy, and pinned-runtime tests.
- [ ] Compile Python and check whitespace.
- [ ] Review the full branch diff against `origin/main`.
- [ ] Run the Knowledge Sync Gate and record the required knowledge delta.
- [ ] Commit any required knowledge update separately.

Verification:

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/test_memecoin*.py -q
python3 -m pytest tests/test_memecoin_chain_identity_concurrency.py tests/test_memecoin_dup_open.py tests/test_memecoin_exec_lock.py tests/test_memecoin_reserve_entry.py tests/test_memecoin_runtime_deploy.py tests/test_memecoin_runtime_verify.py -q
python3 -m compileall -q sniper scripts
git diff --check origin/main...HEAD
git status --short
```

---

## Task 7: Push, merge, deploy, reconcile, and clean

- [ ] Confirm zero pending funded reservations and preserve the active kill switch.
- [ ] Push the feature branch and fast-forward `main` only after every verification passes.
- [ ] Push `main` and deploy the exact merged SHA with `scripts/deploy_memecoin_runtime.sh`.
- [ ] Run one explicit armed-follow reconciliation from the pinned release.
- [ ] Re-read Fomo following IDs and require every current effective armed wallet to be present.
- [ ] Verify one healthy pinned runner, current heartbeat, zero reservations, unchanged open positions, and no new errors.
- [ ] Remove the merged feature worktree and branch; leave every checkout clean. Do not retain deployment backups.

Release checks:

```bash
git rev-parse main origin/main HEAD
readlink -f .runtime/memecoin/current
pgrep -af memecoin_runner_loop.py
git status --short --branch
git worktree list
```

Stop before release only if Fomo rejects a target, reservations are nonzero, tests fail, the kill-switch state would change, or the exact-SHA runtime gate cannot be proven.

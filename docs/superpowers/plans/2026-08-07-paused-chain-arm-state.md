# Paused Chain Arm State Implementation Plan

**Status:** Superseded by `2026-08-07-memecoin-admission-unlock.md`.

The current plan clears funded authority when an operator pauses a wallet.

**Goal:** Make a Robin arm action clear pause and create effective authority atomically.

**Architecture:** The persistence transaction will add the chain and clear pause together. The table will send `armed=true` from a configured paused row.

**Tech Stack:** Python, SQLite, Flask, JavaScript, pytest, Playwright.

## Global Constraints

- Pause stays authoritative until the operator selects arm.
- Arm clears pause only after the approval and Fomo checks pass.
- Exclusion stays authoritative.
- The UI must use `robin` while storage uses `4663`.

---

### Task 1: Make arm clear pause atomically

**Files:**
- Modify: `sniper/persistence/db_memecoin.py:1956-2038`
- Test: `tests/test_memecoin_per_chain_arming.py`

**Interfaces:**
- Consumes: `set_wallet_chain_armed(conn, uid, chain, True, force=True)`.
- Produces: one transaction with `real_approved=1`, the chain list, and `paused=0`.

- [ ] **Step 1: Write the failing persistence test**

Create a paused wallet with `real_chains='4663'`.
Arm Robin again.
Assert that pause is zero and Robin authority is effective.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py -q`
Expected: FAIL because pause remains one.

- [ ] **Step 3: Add the stored chain map**

Update `paused=0` inside the existing arm transaction.
Keep disarm behavior unchanged.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py -q`
Expected: PASS.

### Task 2: Render resume plus arm

**Files:**
- Modify: `static/app.js:6792-6815`
- Test: `tests/test_memecoin_ui_render_runtime.py`

**Interfaces:**
- Consumes: `configured_chains`, `armed_chains`, and `paused`.
- Produces: a `data-armed="1"` arm control with the text `resume + arm`.

- [ ] **Step 1: Write the failing browser test**

Stub a paused Robin row with `configured_chains=['4663']` and `armed_chains=[]`.
Assert that the button text is `resume + arm` and `data-armed == '1'`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_ui_render_runtime.py -q`
Expected: FAIL because the table sends a disarm action.

- [ ] **Step 3: Use the configured map for the control**

Render `resume + arm` when the chain is configured and the wallet is paused.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py tests/test_memecoin_ui_render_runtime.py -q`
Expected: PASS.

### Task 3: Verify and deploy

**Files:**
- No source changes.

**Interfaces:**
- Consumes: the committed branch SHA.
- Produces: a pinned web release and served asset proof.

- [ ] **Step 1: Run scoped API tests**

Run: `python3 -m pytest tests/test_memecoin_api_armed_and_screen_targets.py tests/test_memecoin_real_approval_gate.py -q`
Expected: PASS.

- [ ] **Step 2: Commit the change**

Commit the spec, plan, tests, and source as one scoped bug fix.

- [ ] **Step 3: Deploy the web release**

Use the repository web deployment script with the committed SHA.

- [ ] **Step 4: Verify the release**

Verify the web symlink, process path, API result, and served `app.js`.

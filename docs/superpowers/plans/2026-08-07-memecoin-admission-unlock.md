# Memecoin Admission Unlock Implementation Plan

**Goal:** Admit nano and micro funded copies, disable the consensus blocker, and make pause mutually exclusive with funded authority.

**Architecture:** Keep the existing band and consensus configuration seams for rollback. Change their defaults, enforce wallet-state invariants in the persistence transaction, and deploy the exact merged commit.

**Tech Stack:** Python, SQLite, Flask, JavaScript, pytest, Playwright, Bash.

## Global Constraints

- `out_of_scope` remains refused.
- Quote, route, safety, chase, freshness, risk, and exit checks remain unchanged.
- An arm action clears pause only after the existing approval and Fomo checks pass.
- A pause or exclusion clears all funded authority.
- A final disarm leaves paper collection active.
- The live runner must start from `.runtime/memecoin/current`.

---

### Task 1: Change Funded Admission Defaults

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `tests/test_memecoin_funded_band_floor.py`
- Modify: `tests/test_memecoin_micro_policy.py`

**Interfaces:**
- Consumes: `MEMECOIN_REAL_RESERVABLE_BANDS` and `MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP`.
- Produces: default admission for nano through mid without a consensus refusal.

- [ ] **Step 1: Change the default-band test**

Change the clean-interpreter assertion to this value:

```python
assert out.stdout.strip() == "nano,micro,small,mid"
```

- [ ] **Step 2: Change the solo-mid behavior test**

Use a solo `$1.5M` buy and assert that the funded hook returns `filled`.

```python
assert hook(c, ev)["status"] == "filled"
```

- [ ] **Step 3: Run the tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_funded_band_floor.py tests/test_memecoin_micro_policy.py -q`

Expected: the old `small,mid` default and the consensus refusal cause failures.

- [ ] **Step 4: Change the defaults**

Set these default values in `sniper/settings/memecoin.py`:

```python
MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP = float(os.getenv(
    "MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", "0"))
MEMECOIN_REAL_RESERVABLE_BANDS = tuple(
    b.strip() for b in os.getenv(
        "MEMECOIN_REAL_RESERVABLE_BANDS", "nano,micro,small,mid").split(",")
    if b.strip())
```

- [ ] **Step 5: Run the focused tests and confirm GREEN**

Run: `python3 -m pytest tests/test_memecoin_funded_band_floor.py tests/test_memecoin_micro_policy.py -q`

Expected: PASS.

### Task 2: Enforce Wallet Authority States

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `tests/test_memecoin_per_chain_arming.py`
- Modify: `tests/test_memecoin_exclusion.py`

**Interfaces:**
- Consumes: `set_wallet_paused`, `set_wallet_excluded`, and `set_wallet_chain_armed`.
- Produces: atomic armed, watch-only, and paused states.

- [ ] **Step 1: Add the pause regression test**

Arm a wallet, pause it, and assert this state:

```python
assert dict(row) == {"paused": 1, "real_approved": 0, "real_chains": None}
```

- [ ] **Step 2: Add the exclusion regression test**

Arm a wallet, exclude it, and assert that the funded authority fields are empty.

- [ ] **Step 3: Add the excluded-arm regression test**

Attempt to arm an excluded wallet.
Assert `ok is False` and `action == "excluded_requires_reobserve"`.

- [ ] **Step 4: Run the tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py tests/test_memecoin_exclusion.py -q`

Expected: the stored authority survives pause and exclusion.

- [ ] **Step 5: Change the persistence transactions**

Use these state transitions:

```sql
UPDATE memecoin_wallets
SET paused=1, real_approved=0, real_chains=NULL
WHERE source_user_id=?
```

```sql
UPDATE memecoin_wallets
SET excluded=?, paused=?,
    real_approved=CASE WHEN ? THEN 0 ELSE real_approved END,
    real_chains=CASE WHEN ? THEN NULL ELSE real_chains END
WHERE source_user_id=?
```

Refuse a new arm when `excluded=1`.

- [ ] **Step 6: Run the focused tests and confirm GREEN**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py tests/test_memecoin_exclusion.py -q`

Expected: PASS.

### Task 3: Synchronize Doctrine and Verify

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-08-07-memecoin-admission-unlock-design.md`
- Modify: `docs/superpowers/plans/2026-08-07-memecoin-admission-unlock.md`

**Interfaces:**
- Consumes: the implemented behavior.
- Produces: current funded-admission instructions and verification evidence.

- [ ] **Step 1: Add the current policy to `AGENTS.md`**

State that nano through mid are reservable and consensus is diagnostic only.
State that pause clears all funded authority.

- [ ] **Step 2: Run focused verification**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_funded_band_floor.py \
  tests/test_memecoin_micro_policy.py \
  tests/test_memecoin_per_chain_arming.py \
  tests/test_memecoin_exclusion.py \
  tests/test_memecoin_real_runner.py \
  tests/test_memecoin_real_deploy.py -q
```

- [ ] **Step 3: Run repository checks**

Run:

```bash
python3 scripts/validate_schema_refs.py
python3 -m compileall -q sniper scripts
```

- [ ] **Step 4: Run the Knowledge Sync Gate review**

Record the new spec and the `AGENTS.md` policy update in the PR body.

### Task 4: Merge, Deploy, and Verify Live State

**Files:**
- Modify outside Git: `.env`
- No additional source files.

**Interfaces:**
- Consumes: the merged commit from `origin/main`.
- Produces: one pinned runner, one pinned web release, and consistent wallet states.

- [ ] **Step 1: Update the live environment**

Set these values before deployment:

```text
MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP=0
MEMECOIN_REAL_RESERVABLE_BANDS=nano,micro,small,mid
```

- [ ] **Step 2: Resolve the merged SHA and run the runtime dry run**

Run:

```bash
git fetch origin main
sha=$(git rev-parse origin/main)
scripts/deploy_memecoin_runtime.sh "$sha" --restart --dry-run
```

Expected: the SHA is on `origin/main`, reservations are zero, and validation passes.

- [ ] **Step 3: Deploy the runtime and web release**

Run the repository runtime and web deployment commands for the same merged SHA.

- [ ] **Step 4: Reconcile paused configured arms**

Use the authenticated arm API for each inconsistent wallet.
Do not update the database directly.

- [ ] **Step 5: Verify live state**

Verify the release links, process paths, process counts, heartbeat, reservations, kill switch, logs, APIs, served asset hash, and zero armed-paused rows.

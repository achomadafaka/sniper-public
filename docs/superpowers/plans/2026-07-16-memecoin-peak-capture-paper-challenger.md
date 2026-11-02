# Memecoin Peak-Capture Paper Challenger Implementation Plan

> Execute after the observation spine is green. This plan adds one paper arm and no funded behavior.

**Goal:** Add a pre-registered peak-then-stall paper challenger using the existing shared entry quote and shared exit sweep.

**Architecture:** Add one pure exit reason, one paper strategy branch, and one default paper fanout arm. Reuse existing peak columns and quote maps.

**Tech:** Python 3, SQLite, pytest.

---

### Task 1: Specify peak-stall behavior with failing tests

**Files:**

- Modify: `tests/test_memecoin_exits.py`
- Modify: `tests/test_memecoin_fanout.py`
- Modify: `tests/test_memecoin_scalp_arm.py`

1. Add failing pure tests for pre-activation hold, high reset, activated stall exit, catastrophe-stop precedence, and timecap.
2. Add a failing integration test that verifies two no-new-high sweeps close the arm after activation.
3. Add failing fanout tests for default-paper membership, shared fill, and mirror membership.
4. Run the focused tests and confirm failure.

### Task 2: Implement the minimal paper arm

**Files:**

- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/memecoin_brackets.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Modify: `sniper/services/memecoin_exits.py`

1. Add `peak_lock_e` settings derived from already-running paper parameters.
2. Implement `peak_lock_e_exit_reason()` as a pure function.
3. Add `peak_lock_e` to default paper fanout and paper mirror arms only.
4. Add a sweep branch that resets/increments `no_new_high_sweeps`, persists the peak, and exits on the pure reason.
5. Rerun focused tests and commit.

### Task 3: Regression validation

1. Run `tests/test_memecoin_exits.py`, `tests/test_memecoin_fanout.py`, `tests/test_memecoin_scalp_arm.py`, and policy tests.
2. Confirm no real-runner file or real policy selects `peak_lock_e`.
3. Include the arm in the full memecoin suite and branch-close validation.

# Pagination and Span-Sensitivity Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drain an exact-page-bound Solana backlog without false completeness and make the predictive study emit fully labeled with-floor and without-floor result arms.

**Architecture:** `_new_signatures()` uses one evidence-only probe after its bounded page walk. The research script separates feature-frame construction from model evaluation, then runs both preregistered arms over the same loaded source frames.

**Tech Stack:** Python 3, SQLite, pandas, scikit-learn, pytest.

## Global Constraints

- Keep the exit worker default OFF and log-only.
- Do not call live APIs, read or mutate production databases, inspect replay output, or change funded runner behavior.
- Preserve `_MAX_PAGES * _SIG_LIMIT` as the maximum signatures returned for decoding by one enumeration call.
- Use explicit-path staging; never `git add -A`.
- Do not interpret study results in this change.

---

### Task 1: Exact-bound Solana pagination

**Files:**
- Modify: `tests/test_memecoin_solana_tape.py`
- Modify: `sniper/services/memecoin_solana_tape.py`

**Interfaces:**
- Consumes: `_new_signatures(vault, cursor_sig, get_sigs_fn, *, start_before=None)`.
- Produces: the existing `(signatures, reached_cursor)` contract with exact-bound proof.

- [ ] **Step 1: Write the failing lifecycle regression**

Add a test that drives `n = _MAX_PAGES * _SIG_LIMIT + 300` signatures until the cursor reaches the
newest signature. Record every `get_tx_fn` call and assert exact oldest-to-newest coverage:

```python
def test_exact_bound_remainder_is_decoded_before_vault_becomes_complete():
    tc = _cursor_db()
    n = st._MAX_PAGES * st._SIG_LIMIT + 300
    universe, get_sigs = _sig_universe(n)
    decoded = []

    def get_tx(sig):
        decoded.append(sig)
        slot = int(sig[1:])
        return {"meta": {"preTokenBalances": [], "postTokenBalances": []},
                "blockTime": 1_700_000_000 + slot}

    for _ in range(40):
        st.tape_pass_for_vault(tc, "MINT", "V", get_sigs, get_tx,
                               "2026-01-01T00:00:00Z", tx_budget=200)
        row = tc.execute(
            "SELECT last_sig, frontier_before FROM sol_sig_cursor WHERE vault='V'"
        ).fetchone()
        if row["last_sig"] == universe[-1]["signature"] and not row["frontier_before"]:
            break

    assert row["last_sig"] == universe[-1]["signature"]
    assert row["frontier_before"] is None
    assert decoded == [s["signature"] for s in universe]
```

- [ ] **Step 2: Run the regression and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_solana_tape.py::test_exact_bound_remainder_is_decoded_before_vault_becomes_complete -q
```

Expected: failure with cursor stuck at `s0300` and only 300 decoded signatures.

- [ ] **Step 3: Add the evidence-only probe**

When the `for` loop exhausts `_MAX_PAGES`, request one row below `before` with the same `until`:

```python
else:
    probe_params = {"limit": 1}
    if cursor_sig:
        probe_params["until"] = cursor_sig
    if before:
        probe_params["before"] = before
    probe = get_sigs_fn(vault, probe_params)
    if probe is None:
        return None, False
    reached = not probe
```

Do not append the probe row to `out`.

- [ ] **Step 4: Run focused pagination tests and verify GREEN**

```bash
python3 -m pytest tests/test_memecoin_solana_tape.py -q
```

- [ ] **Step 5: Commit explicit paths**

```bash
git add tests/test_memecoin_solana_tape.py sniper/services/memecoin_solana_tape.py
git commit -m "fix(memecoin): drain exact-bound Solana backlog"
```

### Task 2: Two-arm span-floor study

**Files:**
- Modify: `tests/test_memecoin_predictive_study_floors.py`
- Modify: `scripts/research/memecoin_tape_predictive_study.py`
- Modify: `docs/research/assets/README-predictive-study-2026-07-24-VOID.md`

**Interfaces:**
- Produces: `build_pool_block_features(..., *, apply_span_floor: bool = True)`.
- Produces: `_study_arms() -> tuple[tuple[str, bool], ...]`.
- Produces: labeled `*_with_span_floor_2026-07-24.csv` and `*_without_span_floor_2026-07-24.csv` outputs.

- [ ] **Step 1: Write failing tests for cohort inclusion and arm registration**

```python
def test_underfloor_pool_is_retained_in_without_floor_arm(study):
    out = study.build_pool_block_features(
        _swaps(200, 1), True, None, apply_span_floor=False
    )
    assert not out.empty
    assert out.attrs["clears_span_floor"] is False


def test_sensitivity_run_registers_both_labeled_arms(study):
    assert study._study_arms() == (
        ("with_span_floor", True),
        ("without_span_floor", False),
    )
```

- [ ] **Step 2: Run both tests and verify RED**

```bash
python3 -m pytest \
  tests/test_memecoin_predictive_study_floors.py::test_underfloor_pool_is_retained_in_without_floor_arm \
  tests/test_memecoin_predictive_study_floors.py::test_sensitivity_run_registers_both_labeled_arms -q
```

Expected: unexpected keyword argument and missing `_study_arms`.

- [ ] **Step 3: Parameterize the floor and preserve metadata**

Add `apply_span_floor=True`; return empty only when the arm applies the floor. Reattach `span_s` and
`clears_span_floor` to the final frame after pandas operations so tests and outputs retain them.

- [ ] **Step 4: Extract one-arm analysis and loop over both arms**

Extract the existing frame-building and model/output body without changing model definitions. Use:

```python
def _study_arms():
    if REPORT_SPAN_FLOOR_SENSITIVITY:
        return (("with_span_floor", True), ("without_span_floor", False))
    return (("with_span_floor", True),)


for arm, apply_span_floor in _study_arms():
    full = _build_feature_frame(
        swaps, syncs, qside, qualifying,
        apply_span_floor=apply_span_floor,
    )
    _run_study_arm(full, arm=arm)
```

All CSV paths and console headings must include `arm`. Raise an explicit `RuntimeError` naming the
arm if no frames qualify.

- [ ] **Step 5: Update the preregistration README**

Document the two output suffixes and state that one process loads the source once before both arms.

- [ ] **Step 6: Run focused research tests and verify GREEN**

```bash
python3 -m pytest tests/test_memecoin_predictive_study_floors.py -q
```

- [ ] **Step 7: Commit explicit paths**

```bash
git add tests/test_memecoin_predictive_study_floors.py \
  scripts/research/memecoin_tape_predictive_study.py \
  docs/research/assets/README-predictive-study-2026-07-24-VOID.md
git commit -m "fix(research): report both span-floor sensitivity arms"
```

### Task 3: Safe verification

**Files:**
- Verify only.

**Interfaces:**
- Consumes both completed fixes.
- Produces current evidence for branch handoff.

- [ ] **Step 1: Run focused combined tests**

```bash
python3 -m pytest tests/test_memecoin_solana_tape.py tests/test_exit_materializer.py \
  tests/test_memecoin_predictive_study_floors.py -q
```

- [ ] **Step 2: Run safe broader checks**

```bash
python3 scripts/validate_schema_refs.py
python3 -m compileall -q sniper/services/memecoin_solana_tape.py \
  scripts/research/memecoin_tape_predictive_study.py
git diff --check b065d639..HEAD
```

- [ ] **Step 3: Report branch state without pushing**

```bash
git status --short --branch
git log --oneline b065d639..HEAD
```

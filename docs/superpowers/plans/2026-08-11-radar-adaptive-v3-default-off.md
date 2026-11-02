# Radar Adaptive v3 Default-Off Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Disable all adaptive v3 runtime paths unless one default-off setting enables them.

**Architecture:** One validated setting controls the engine, startup activation, and lifecycle drive. The Pons quote-binding fix stays outside the gate.

**Tech Stack:** Python 3, `dataclasses`, SQLite, and pytest.

## Global Constraints

- Use TDD for each behavior change.
- Do not change the v2 policy, cadence, outcome, or trajectory logic.
- Do not gate the Pons quote-binding fix.
- Do not set `MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED=1` in any deployment file.
- Do not change funded authority or funded execution.
- Use ASD-STE100 Simplified Technical English.
- Run focused tests only. Do not run a full test suite.

---

### Task 1: Add the validated default-off setting

**Files:**

- Modify: `sniper/settings/memecoin_opportunity.py:13-55`
- Modify: `sniper/settings/memecoin_opportunity.py:155-330`
- Test: `tests/test_memecoin_opportunity_settings.py:270-320`

**Interfaces:**

- Produces: `MemecoinOpportunitySettings.adaptive_v3_enabled: bool`
- Consumes: `MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED`

- [ ] **Step 1: Write the failing settings tests**

Add tests for the false default, accepted true values, and the invalid adapter combination.

```python
def test_adaptive_v3_is_default_off():
    settings = load_memecoin_opportunity_settings(env=_base_env())
    assert settings.adaptive_v3_enabled is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_adaptive_v3_accepts_explicit_true_values(value):
    settings = load_memecoin_opportunity_settings(
        env=_base_env(
            MEMECOIN_OPPORTUNITY_PAPER_ADAPTER="1",
            MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED=value,
        )
    )
    assert settings.adaptive_v3_enabled is True


def test_adaptive_v3_requires_the_paper_adapter():
    with pytest.raises(ValueError, match="adaptive v3 requires the paper adapter"):
        load_memecoin_opportunity_settings(
            env=_base_env(MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED="1")
        )
```

- [ ] **Step 2: Confirm the tests fail for the missing field**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_opportunity_settings.py::test_adaptive_v3_is_default_off \
  tests/test_memecoin_opportunity_settings.py::test_adaptive_v3_accepts_explicit_true_values \
  tests/test_memecoin_opportunity_settings.py::test_adaptive_v3_requires_the_paper_adapter -q
```

Expected: The field tests fail because `adaptive_v3_enabled` does not exist.

- [ ] **Step 3: Add the setting and validation**

Parse the environment value with the existing Boolean value set.

Reject `adaptive_v3_enabled=True` when `paper_adapter_enabled=False`.

- [ ] **Step 4: Confirm the settings tests pass**

Run the same focused command. Expected: all tests pass.

- [ ] **Step 5: Commit the settings change**

```bash
git add sniper/settings/memecoin_opportunity.py tests/test_memecoin_opportunity_settings.py
git commit -m "fix(radar): default adaptive v3 setting off"
```

---

### Task 2: Gate adaptive v3 engine evaluation

**Files:**

- Modify: `sniper/research/memecoin_opportunity/engine.py:271-315`
- Modify: `sniper/research/memecoin_opportunity/engine.py:1011-1028`
- Modify: `tests/test_memecoin_opportunity_engine.py:489-525`
- Modify: `tests/test_memecoin_opportunity_engine.py:2585-2685`

**Interfaces:**

- Consumes: `OpportunityEngine(..., adaptive_v3_enabled: bool = False)`
- Produces: No adaptive v3 evaluator call, alert, or fill request when false.

- [ ] **Step 1: Write the failing default-off engine test**

Make `evaluate_adaptive_v3` fail if the engine calls it.

Drive a valid v6 completion with the paper adapter enabled.

Assert that the v6 request exists and no adaptive v3 alert exists.

- [ ] **Step 2: Confirm the engine test fails**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_opportunity_engine.py::test_default_engine_skips_adaptive_v3_evaluation -q
```

Expected: The test fails because the engine calls `evaluate_adaptive_v3`.

- [ ] **Step 3: Add the constructor gate**

Add `adaptive_v3_enabled: bool = False` to `OpportunityEngine.__init__`.

Call `evaluate_adaptive_v3` only when the field is true.

- [ ] **Step 4: Add and run the enabled-path test**

Use a fixed adaptive verdict in the valid completion fixture.

Assert that the enabled engine stores the adaptive alert and fill request.

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_opportunity_engine.py::test_default_engine_skips_adaptive_v3_evaluation \
  tests/test_memecoin_opportunity_engine.py::test_enabled_engine_can_store_an_adaptive_v3_request -q
```

Expected: both tests pass.

- [ ] **Step 5: Run the Pons quote-binding tests**

```bash
python3 -m pytest tests/test_radar_pons_quote_binding.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit the engine gate**

```bash
git add sniper/research/memecoin_opportunity/engine.py \
  tests/test_memecoin_opportunity_engine.py
git commit -m "fix(radar): gate adaptive v3 evaluation"
```

---

### Task 3: Gate startup activation and lifecycle drive

**Files:**

- Modify: `scripts/memecoin_opportunity_worker.py:1264-1325`
- Modify: `scripts/memecoin_opportunity_worker.py:3774-3780`
- Modify: `scripts/memecoin_opportunity_worker.py:3871-3895`
- Modify: `scripts/memecoin_opportunity_worker.py:4205-4230`
- Modify: `tests/test_radar_v3_worker_wiring.py`

**Interfaces:**

- Consumes: `settings.adaptive_v3_enabled`
- Produces: `_activate_paper_lifecycles(..., adaptive_v3_enabled: bool)`
- Produces: `_drive_paper_lifecycles_safely(..., adaptive_v3_enabled: bool)`

- [ ] **Step 1: Write the failing activation tests**

Use a real temporary Radar database.

Assert that the default helper creates only the v2 activation.

Assert that the enabled helper creates the v2 and v3 activations.

- [ ] **Step 2: Write the failing drive tests**

Assert that the default helper returns v2 counters and a disabled v3 state.

Assert that the enabled helper returns counters for both lifecycles.

- [ ] **Step 3: Confirm the worker tests fail**

Run:

```bash
python3 -m pytest tests/test_radar_v3_worker_wiring.py -q
```

Expected: the new helper argument and activation behavior do not exist.

- [ ] **Step 4: Add the worker gates**

Gate v3 activation and drive with `settings.adaptive_v3_enabled`.

Pass the same setting into `OpportunityEngine`.

Keep the v2 calls outside the v3 gate.

- [ ] **Step 5: Confirm the worker tests pass**

Run the same focused command. Expected: all tests pass.

- [ ] **Step 6: Commit the worker gates**

```bash
git add scripts/memecoin_opportunity_worker.py tests/test_radar_v3_worker_wiring.py
git commit -m "fix(radar): gate adaptive v3 worker paths"
```

---

### Task 4: Close the gate-only branch

**Files:**

- Modify: `AGENTS.md`
- Verify: `docs/superpowers/specs/2026-08-11-radar-adaptive-v3-default-off-design.md`
- Verify: `docs/superpowers/plans/2026-08-11-radar-adaptive-v3-default-off.md`

**Interfaces:**

- Produces: one gate-only branch with no trajectory behavior change.

- [ ] **Step 1: Update the project rule**

State that adaptive v3 stays default off behind `MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED`.

- [ ] **Step 2: Run focused regression tests**

```bash
python3 -m pytest \
  tests/test_memecoin_opportunity_settings.py \
  tests/test_memecoin_opportunity_engine.py \
  tests/test_radar_v3_worker_wiring.py \
  tests/test_radar_pons_quote_binding.py \
  tests/test_memecoin_opportunity_worker_paper_adapter.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run static validation**

```bash
python3 scripts/validate_schema_refs.py
python3 -m py_compile \
  sniper/settings/memecoin_opportunity.py \
  sniper/research/memecoin_opportunity/engine.py \
  scripts/memecoin_opportunity_worker.py
```

Expected: both commands pass.

- [ ] **Step 4: Run the Knowledge Sync Gate**

Use the audit-only mode. Confirm that the `AGENTS.md` change records the new rule.

- [ ] **Step 5: Confirm branch scope**

```bash
git diff --check
git diff --stat 03ab7c3c...HEAD
git status --short --branch
```

Expected: the diff has only the gate, tests, spec, plan, and `AGENTS.md`.

- [ ] **Step 6: Commit the branch-close files**

```bash
git add AGENTS.md docs/superpowers/specs/2026-08-11-radar-adaptive-v3-default-off-design.md \
  docs/superpowers/plans/2026-08-11-radar-adaptive-v3-default-off.md
git commit -m "docs(radar): set adaptive v3 default-off rule"
```

- [ ] **Step 7: Merge and deploy after verification**

Use the repository deployment process for the merged SHA.

Do not enable adaptive v3.

Verify the release pin, process path, activation rows, alert rows, fill requests, v2 status, and Pons status.

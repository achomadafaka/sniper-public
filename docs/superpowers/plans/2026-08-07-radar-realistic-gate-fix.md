# Radar Realistic Gate Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Radar funding gate and checkpoint receipts use only valid `radar-realistic-v2` outcomes.

**Architecture:** The hot database owns the realistic outcomes. The insight rows supply frozen alert features for matched-control comparisons.

**Tech Stack:** Python, SQLite, and pytest.

## Global Constraints

- Keep the arm rules and `GATE_MATCH_SCALE = 2` unchanged.
- Exclude censored and missing realistic outcomes.
- Do not use legacy alert labels as a fallback.
- Run focused tests only.

---

### Task 1: Select Realistic Gate Evidence

**Files:**
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Test: `tests/test_memecoin_opportunity_insights.py`

**Interfaces:**
- Consumes: Hot `op_paper_fill_requests` and `op_paper_outcomes` rows.
- Produces: `_realistic_gate_evidence(hot, alerts) -> list[dict[str, object]]`.

- [ ] **Step 1: Write the failing tests**

Add tests that supply different legacy and realistic returns. Assert that the gate uses the realistic values.

- [ ] **Step 2: Verify the tests fail**

Run: `python3 -m pytest tests/test_memecoin_opportunity_insights.py -q -k realistic_gate`

Expected: The gate uses the legacy values or has no realistic source field.

- [ ] **Step 3: Add the minimum selector**

Join valid lifecycle outcomes to their paper requests. Overlay the result and keep the frozen alert features.

- [ ] **Step 4: Verify the focused tests pass**

Run: `python3 -m pytest tests/test_memecoin_opportunity_insights.py -q -k realistic_gate`

Expected: All selected tests pass.

### Task 2: Use the Same Evidence for Checkpoints

**Files:**
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Modify: `tests/test_memecoin_opportunity_insights.py`

**Interfaces:**
- Consumes: `_realistic_gate_evidence(hot, alerts)`.
- Produces: Immutable 25, 50, and 100 pair receipts from realistic outcomes.

- [ ] **Step 1: Write the failing checkpoint test**

Assert that 26 legacy pairs do not seal without 25 realistic pairs.

- [ ] **Step 2: Verify the checkpoint test fails**

Run the named checkpoint test with pytest.

- [ ] **Step 3: Apply the selector to the sealer**

Use the shared realistic evidence before `_match_v4_controls`.

- [ ] **Step 4: Run the focused Radar tests**

Run: `python3 -m pytest tests/test_memecoin_opportunity_insights.py tests/test_memecoin_opportunity_paper_lifecycle.py tests/test_memecoin_opportunity_api.py -q`

Expected: All focused tests pass.

### Task 3: Close and Deploy the Fix

**Files:**
- Modify: `docs/superpowers/specs/2026-08-07-radar-v6-measured-fill-contract-design.md`

**Interfaces:**
- Consumes: The tested gate behavior.
- Produces: A merged release and live evidence from the same outcome source.

- [ ] **Step 1: Record the implementation rule**

State that the gate and checkpoint receipts use valid lifecycle outcomes only.

- [ ] **Step 2: Run schema and focused checks**

Run `python3 scripts/validate_schema_refs.py` and the focused tests.

- [ ] **Step 3: Complete the Knowledge Sync Gate**

Record the corrected evidence boundary and add the required PR block.

- [ ] **Step 4: Open and merge one pull request**

Do not include unrelated files.

- [ ] **Step 5: Deploy the merged opportunity and web releases**

Verify the release paths, processes, source health, gate values, and checkpoint count.

# Radar Forward Arms v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add forward-only Pump ignition and buyer-confirmation arms, preserve a route-only control, and collect honest 120/300-second executable evidence.

**Architecture:** Pure candidate classification lives beside Level-1 directional rules. The engine persists one non-actionable route control and at most one actionable candidate arm per remote completion using the existing immutable alert/entry ledger. Source capture extends to 300 seconds only for candidate launches, while diagnostics continue after the primary 60-second outcome.

**Tech Stack:** Python 3.13, SQLite WAL, pytest, vanilla JavaScript.

## Global Constraints

- Paper/read-only only; no funded execution imports or mutations.
- Pump thresholds do not apply to Bags Solana or Robinhood 4663.
- Existing immutable rows and rule versions are never rewritten.
- Preserve decode-gap rejection and storage fail-closed behavior.
- Run focused Radar tests only; no full repository suite.

---

### Task 1: Pure forward-arm classifier

**Files:**
- Modify: `sniper/research/memecoin_opportunity/directional.py`
- Test: `tests/test_memecoin_opportunity_directional.py`

**Interfaces:**
- Produces: `evaluate_forward_arm(current, prior) -> DirectionalVerdict | None`.
- Rule versions: `pump-ignition-v1`, `pump-confirmation-v1`.
- Follow-up forward probes: `pump-clean-start-v1`, `pump-curve-break-v1`.

- [ ] Write RED cases for ignition, confirmation, wrong venue, low progress, bad quote drag, positive quote return, and missing shared Level-1 conditions.
- [ ] Run the exact directional test and confirm the new cases fail.
- [ ] Implement the minimal pure classifier with the approved thresholds.
- [ ] Run the exact directional test and confirm it passes.
- [ ] Commit the classifier and tests.

### Task 2: Remote completion control and candidate receipts

**Files:**
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Test: `tests/test_memecoin_opportunity_engine.py`
- Test: `tests/test_memecoin_opportunity_projection.py`

**Interfaces:**
- Consumes: `evaluate_forward_arm(current, prior)`.
- Produces: immutable `route-control-v1` entries plus optional candidate alert/entry.

- [ ] Write a RED production-shaped test proving a successful remote route with invalid cost stays INNER and creates only a control receipt.
- [ ] Write a RED test proving one qualifying completion creates separate control and candidate entries idempotently.
- [ ] Run the exact tests and confirm the failures.
- [ ] Refactor remote receipt insertion into one immutable helper and rerun full candidate evaluation after completion.
- [ ] Run engine and projection tests.
- [ ] Commit remote completion behavior.

### Task 3: Candidate-only 300-second path

**Files:**
- Modify: `sniper/services/memecoin_opportunity_sources.py`
- Modify: `sniper/research/memecoin_opportunity/outcome_tracker.py`
- Modify: `sniper/research/memecoin_opportunity/diagnostics.py`
- Modify: `sniper/settings/memecoin_opportunity.py`
- Test: `tests/test_memecoin_opportunity_sources.py`
- Test: `tests/test_memecoin_opportunity_outcomes.py`
- Test: `tests/test_memecoin_opportunity_diagnostics.py`
- Test: `tests/test_memecoin_opportunity_settings.py`

**Interfaces:**
- Candidate rule versions extend event capture and diagnostic observation through 300,000 ms.
- Primary competing outcome remains 60,000 ms.

- [ ] Write RED tests for ordinary 120-second cutoff and candidate 300-second allowance.
- [ ] Write RED tests proving candidate observations continue after the primary outcome but stop after a >=300-second receipt.
- [ ] Write RED target-ladder cases for 120/300 seconds and drawdown-before-target.
- [ ] Implement bounded candidate capture, diagnostic continuation, and settings validation.
- [ ] Run source, outcome, diagnostics, settings, and retention tests.
- [ ] Commit extended evidence behavior.

### Task 4: Readable API/UI arm labels

**Files:**
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_opportunity_projection.py`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- API emits `arm_key`, `arm_label`, `action_class`, quote drag, launch age, progress, and effective buyers.

- [ ] Write RED API and UI contract tests for `FIRE NOW — IGNITION`, `FIRE NOW — CONFIRMED`, `ROUTE CONTROL`, and `WAIT`.
- [ ] Implement API projection and stable incremental rendering without replacing open detail elements.
- [ ] Run projection/UI contract tests and `node --check static/app.js`.
- [ ] Commit the operator surface.

### Task 5: Integration and live activation

**Files:**
- Modify only files proven necessary by focused failures.

- [ ] Run `python3 -m pytest tests/test_memecoin_opportunity_*.py -q`.
- [ ] Run Python compile, JavaScript syntax, schema reference, and `git diff --check` checks.
- [ ] Restart only `scripts/memecoin_opportunity_worker.py` using its exact PID and inherited environment.
- [ ] Verify the live API, zero open gaps, healthy source lanes, bounded queues, and storage below the soft cap.
- [ ] Confirm new receipts are forward-only; do not reinterpret historical alerts as V3 results.
- [ ] Report early arm counts honestly; promising results are not required before the first forward sample exists.

### Task 6: Retained-arm comparison and follow-up probes

**Files:**
- Modify: `sniper/research/memecoin_opportunity/directional.py`
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Modify: `sniper/research/memecoin_opportunity/outcome_tracker.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `static/app.js`
- Test: focused opportunity directional, engine, insight, outcome, projection, source, and UI tests.

- [x] Compare every retained candidate arm against the permissive executable-route control.
- [x] Reject ignition/confirmation as current edge claims after they failed to separate.
- [x] Register clean-start and curve-break as forward-only paper probes from the retained hot slices.
- [x] Keep all four candidate rules on the 300-second evidence path.
- [x] Render full retained arm counts instead of the API's newest-50 detail window.

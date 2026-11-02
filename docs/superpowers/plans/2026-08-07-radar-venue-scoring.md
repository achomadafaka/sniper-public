# Radar Venue Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Add separate Bags and Pons scoring paths and show realistic results for each launchpad.

**Architecture:** A bounded EVM quote queue protects capture. Venue-specific rules use common confirmation facts and separate controls.

**Tech Stack:** Python 3.13, SQLite, Flask, JavaScript, pytest, EVM JSON-RPC, and QuoterV2.

## Global Constraints

- Radar stays paper-only.
- Pump, Bags, and Pons results never pool.
- The Pons route binds one registered pool and one EVM block identity.
- Tests stay focused. Do not run the full suite.
- Use TDD for each behavior change.

---

### Task 1: Venue authority and common hypothesis

**Files:**
- Modify: `sniper/research/memecoin_opportunity/venue_policy.py`
- Modify: `sniper/research/memecoin_opportunity/directional.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Test: `tests/test_memecoin_opportunity_venue_policy.py`
- Test: `tests/test_memecoin_opportunity_directional.py`

**Interfaces:**
- Produces: `evaluate_venue_confirmation(current, prior)` and venue rule constants.

- [ ] Write tests that require Bags and Pons scoreable authority.
- [ ] Run the tests and confirm the expected failures.
- [ ] Write tests for separate Bags and Pons confirmation rules.
- [ ] Run the tests and confirm the expected failures.
- [ ] Add the minimum authority and rule code.
- [ ] Run both focused test files.

### Task 2: Exact Pons quote path and bounded EVM queue

**Files:**
- Modify: `sniper/research/memecoin_opportunity/pons_4663.py`
- Modify: `sniper/research/memecoin_opportunity/state.py`
- Modify: `sniper/services/memecoin_opportunity_live.py`
- Create: `sniper/research/memecoin_opportunity/evm_probe.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_pons_4663.py`
- Create: `tests/test_memecoin_opportunity_evm_probe.py`
- Modify: `tests/test_memecoin_opportunity_engine.py`

**Interfaces:**
- Produces: `make_pons_4663_remote_probe(rpc_call, quoter)`.
- Produces: `OpportunityEvmProbe(quote_fn, queue_max, completion_max)`.

- [ ] Write tests for the Pons pool identity and quote token state.
- [ ] Run the tests and confirm the expected failures.
- [ ] Add the state fields and strict Pons identity checks.
- [ ] Write exact QuoterV2 buy and sell ABI tests.
- [ ] Run the tests and confirm the expected failures.
- [ ] Add the exact quote provider with typed failures.
- [ ] Write bounded queue and completion tests.
- [ ] Run the tests and confirm the expected failures.
- [ ] Add the queue and worker dispatch.
- [ ] Run the focused Pons, queue, engine, and worker tests.

### Task 3: Venue-specific realistic paper lifecycle

**Files:**
- Modify: `sniper/research/memecoin_opportunity/paper_adapter.py`
- Modify: `sniper/research/memecoin_opportunity/paper_lifecycle.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_paper_adapter.py`
- Test: `tests/test_memecoin_opportunity_paper_lifecycle.py`
- Test: `tests/test_memecoin_opportunity_worker_paper_adapter.py`

**Interfaces:**
- Produces: `PAPER_RULE_BY_VENUE` and `PAPER_CONTROL_BY_VENUE`.
- Produces: `realistic_result_summary(conn)["by_venue"]`.

- [ ] Write tests for venue-specific paper request eligibility.
- [ ] Run the tests and confirm the expected failures.
- [ ] Add venue rule and control maps.
- [ ] Write tests for exact EVM entry and exit dispatch.
- [ ] Run the tests and confirm the expected failures.
- [ ] Add non-blocking EVM paper quote dispatch.
- [ ] Write tests for separate lifecycle summaries.
- [ ] Run the tests and confirm the expected failures.
- [ ] Generalize the lifecycle query and summary by venue.
- [ ] Run the focused adapter and lifecycle tests.

### Task 4: Per-launchpad API and UI results

**Files:**
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_opportunity_api.py`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Consumes: `paper_results.by_venue`.
- Produces: three separate launchpad result rows.

- [ ] Write API and UI tests for three separate venue rows.
- [ ] Run the tests and confirm the expected failures.
- [ ] Add the unavailable payload shape and UI rows.
- [ ] Run the focused API and UI tests.

### Task 5: Verification and live cutover

**Files:**
- Modify: `docs/superpowers/specs/2026-08-07-radar-active-capture-venues-design.md`
- Create: `docs/superpowers/evidence/2026-08-07-radar-venue-scoring-ksg.md`

**Interfaces:**
- Produces: a merged and pinned opportunity runtime release.

- [ ] Amend the old capture authority text with the new approved design.
- [ ] Run focused Radar tests only.
- [ ] Run `python3 scripts/validate_schema_refs.py`.
- [ ] Run JavaScript syntax validation.
- [ ] Complete the Knowledge Sync Gate.
- [ ] Commit one logical change.
- [ ] Push the feature branch and open the PR.
- [ ] Merge the approved PR.
- [ ] Deploy the merged SHA to the pinned opportunity runtime.
- [ ] Verify one worker, source health, quote health, API rows, storage, and logs.

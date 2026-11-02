# Radar Active Capture Venues Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Radar capture for Pump, Bags Robinhood 4663, and Pons V1 only.

**Architecture:** A pure venue-policy module separates active capture authority from historical identity. The worker, market inputs, probes, and health projection consume this policy. Historical Bags Solana decode and report paths remain unchanged.

**Tech Stack:** Python 3.13, asyncio, SQLite, pytest.

## Global Constraints

- Keep `VenueId.BAGS_SOLANA` for historical decode and report use.
- Keep Pump as the only scoreable venue.
- Keep Bags Robinhood and Pons V1 as capture-only venues.
- Do not start a Bags Solana source, recovery, confirmation, pricing, or health path.
- Do not add an arm, a threshold, a funded action, or V6 authority.
- Use focused tests only. Do not run the full suite.
- Use ASD-STE100 Simplified Technical English in new text.

---

### Task 1: Add venue authority

**Files:**
- Create: `sniper/research/memecoin_opportunity/venue_policy.py`
- Create: `tests/test_memecoin_opportunity_venue_policy.py`

**Interfaces:**
- Produces: `VenueAuthority`, `VENUE_AUTHORITY`, `active_capture_venues()`, `scoreable_venues()`, `venue_authority()`, and `is_active_capture_venue()`.
- Consumes: `VenueId` from `types.py`.

- [ ] **Step 1: Write the failing authority tests**

```python
def test_authority_covers_each_known_venue_once():
    assert set(VENUE_AUTHORITY) == set(VenueId)


def test_active_and_scoreable_sets_match_the_approved_contract():
    assert active_capture_venues() == frozenset({
        VenueId.PUMPFUN_SOLANA,
        VenueId.BAGS_ROBINHOOD_4663,
        VenueId.PONS_V1_ROBINHOOD_4663,
    })
    assert scoreable_venues() == frozenset({VenueId.PUMPFUN_SOLANA})
    assert venue_authority(VenueId.BAGS_SOLANA) is VenueAuthority.HISTORICAL_ONLY
```

- [ ] **Step 2: Run the tests and verify the import failure**

Run: `python3 -m pytest tests/test_memecoin_opportunity_venue_policy.py -q`

Expected: FAIL because `venue_policy.py` does not exist.

- [ ] **Step 3: Add the minimal immutable authority map**

```python
class VenueAuthority(str, Enum):
    SCOREABLE = "scoreable"
    CAPTURE_ONLY = "capture_only"
    HISTORICAL_ONLY = "historical_only"


VENUE_AUTHORITY = MappingProxyType({
    VenueId.PUMPFUN_SOLANA: VenueAuthority.SCOREABLE,
    VenueId.BAGS_SOLANA: VenueAuthority.HISTORICAL_ONLY,
    VenueId.BAGS_ROBINHOOD_4663: VenueAuthority.CAPTURE_ONLY,
    VenueId.PONS_V1_ROBINHOOD_4663: VenueAuthority.CAPTURE_ONLY,
})
```

- [ ] **Step 4: Run the focused authority tests**

Run: `python3 -m pytest tests/test_memecoin_opportunity_venue_policy.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the authority contract**

```bash
git add sniper/research/memecoin_opportunity/venue_policy.py tests/test_memecoin_opportunity_venue_policy.py
git commit -m "feat(radar): define active venue authority"
```

### Task 2: Remove Bags Solana runtime configuration and pricing authority

**Files:**
- Modify: `sniper/settings/memecoin_opportunity.py`
- Modify: `sniper/research/memecoin_opportunity/market_inputs.py`
- Modify: `sniper/research/memecoin_opportunity/solana_probe.py`
- Modify: `tests/test_memecoin_opportunity_settings.py`

**Interfaces:**
- Consumes: `active_capture_venues()` from Task 1.
- Produces: runtime settings and market inputs with no Bags Solana authority.

- [ ] **Step 1: Change the settings tests first**

Add an old Bags URL to the fixture. Assert that the returned settings have no `bags_solana_ws_url` attribute.

Add a complete price fixture. Assert that `ready` and `blocked` contain no `VenueId.BAGS_SOLANA` key.

- [ ] **Step 2: Run the two exact tests and verify failures**

Run: `python3 -m pytest tests/test_memecoin_opportunity_settings.py::test_enabled_runtime_requires_at_least_one_read_only_source_url tests/test_memecoin_opportunity_settings.py::test_runtime_market_inputs_cover_each_active_venue -q`

Expected: FAIL because the settings and market input maps still include Bags Solana.

- [ ] **Step 3: Remove the Bags Solana settings fields and filter runtime market inputs**

Remove `bags_solana_ws_url` and `bags_coverage_interval_s` from `MemecoinOpportunitySettings`.

Remove their parse and validation code. Do not reject old environment keys. They have no authority after this change.

Build `specs` only for venues in `active_capture_venues()`. Remove Bags Solana from `SOLANA_VENUES` in `solana_probe.py`.

- [ ] **Step 4: Run the focused settings tests**

Run: `python3 -m pytest tests/test_memecoin_opportunity_settings.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the runtime configuration change**

```bash
git add sniper/settings/memecoin_opportunity.py sniper/research/memecoin_opportunity/market_inputs.py sniper/research/memecoin_opportunity/solana_probe.py tests/test_memecoin_opportunity_settings.py
git commit -m "fix(radar): remove Bags Solana runtime authority"
```

### Task 3: Apply authority to worker sources and restored state

**Files:**
- Modify: `scripts/memecoin_opportunity_worker.py`
- Modify: `tests/test_memecoin_opportunity_confirmation.py`
- Modify: `tests/test_memecoin_opportunity_engine.py`

**Interfaces:**
- Consumes: `active_capture_venues()`, `is_active_capture_venue()`, and `scoreable_venues()` from Task 1.
- Produces: one Pump Solana source and one shared Bags Robinhood/Pons 4663 source.

- [ ] **Step 1: Write failing worker behavior tests**

Replace the split Solana transport test with this contract:

```python
def test_active_solana_transport_is_pump_only():
    settings = SimpleNamespace(
        solana_rpc_url="https://pump-read.invalid/rpc",
        solana_ws_url="",
    )
    assert worker._solana_ws_transport(settings) == "wss://pump-read.invalid/rpc"
```

Add a restore fixture with one Pump state and one Bags Solana state. Assert that `engine.states` contains only the Pump launch.

Add a pending-confirmation fixture. Assert that the worker selects only active venue rows.

- [ ] **Step 2: Run the exact tests and verify failures**

Run: `python3 -m pytest tests/test_memecoin_opportunity_confirmation.py::test_active_solana_transport_is_pump_only tests/test_memecoin_opportunity_engine.py::test_restore_pipeline_excludes_historical_only_state -q`

Expected: FAIL because the old transport returns Pump and Bags, and restore accepts all venue states.

- [ ] **Step 3: Remove the Bags Solana producer and apply the active set**

Remove `_bags_coverage_loop()` and its worker imports. Do not create Bags feed, Bags shard, or Bags recovery tasks.

Replace `_solana_ws_transports()` with `_solana_ws_transport()`.

Use `active_capture_venues()` for price rows, outcome source health, state restore, and pending confirmation selection.

Keep the shared 4663 poll. Its existing filters continue to capture Bags Robinhood and Pons V1.

Keep historical Bags decoders and schema values.

- [ ] **Step 4: Run the worker and restore tests**

Run: `python3 -m pytest tests/test_memecoin_opportunity_confirmation.py -q -k "transport or evm_poll or pons or confirmation"`

Run: `python3 -m pytest tests/test_memecoin_opportunity_engine.py -q -k "restore_pipeline or forward_arm"`

Expected: PASS.

- [ ] **Step 5: Commit the source change**

```bash
git add scripts/memecoin_opportunity_worker.py tests/test_memecoin_opportunity_confirmation.py tests/test_memecoin_opportunity_engine.py
git commit -m "fix(radar): run only active capture sources"
```

### Task 4: Exclude retired rows from current health

**Files:**
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Modify: `tests/test_memecoin_opportunity_projection.py`
- Modify: `tests/test_memecoin_opportunity_api.py`

**Interfaces:**
- Consumes: `venue_authority()` and `is_active_capture_venue()` from Task 1.
- Produces: current health rows with `venue_authority` for venue sources.

- [ ] **Step 1: Write failing current-health tests**

Insert stored runtime rows for Pump, Bags Solana, Bags Robinhood, and Pons.

Assert that the snapshot excludes these keys:

```python
assert "bags_solana:ws" not in by_key
assert "bags_solana:pricing" not in by_key
assert "opportunity:bags_coverage" not in by_key
assert "opportunity:bags_feed_recovery" not in by_key
```

Assert these authority values:

```python
assert by_key["pumpfun_solana:ws"]["venue_authority"] == "scoreable"
assert by_key["bags_robinhood_4663:ws"]["venue_authority"] == "capture_only"
assert by_key["pons_v1_4663:ws"]["venue_authority"] == "capture_only"
```

- [ ] **Step 2: Run the exact projection test and verify failure**

Run: `python3 -m pytest tests/test_memecoin_opportunity_projection.py::test_current_health_excludes_historical_only_sources -q`

Expected: FAIL because stored Bags Solana rows still appear.

- [ ] **Step 3: Filter current health and add authority labels**

Add a pure source-key classifier. Exclude Bags Solana market rows and its two support rows.

Add `venue_authority` only to venue source and pricing rows. Keep historical launch detail queries unchanged.

Remove the retired Bags recovery map and the top-level `bags_coverage` current payload.

- [ ] **Step 4: Run focused projection and API tests**

Run: `python3 -m pytest tests/test_memecoin_opportunity_projection.py tests/test_memecoin_opportunity_api.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the health change**

```bash
git add sniper/research/memecoin_opportunity/projection.py sniper/routes/api_memecoin_opportunity.py tests/test_memecoin_opportunity_projection.py tests/test_memecoin_opportunity_api.py
git commit -m "fix(radar): report only active capture health"
```

### Task 5: Verify the scoped result and close the branch

**Files:**
- Modify only if required: `docs/superpowers/specs/2026-08-07-radar-active-capture-venues-design.md`
- Modify only if required: PR knowledge records.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a release-ready active capture change.

- [ ] **Step 1: Run the focused capture set**

```bash
python3 -m pytest \
  tests/test_memecoin_opportunity_venue_policy.py \
  tests/test_memecoin_opportunity_settings.py \
  tests/test_memecoin_opportunity_projection.py \
  tests/test_memecoin_opportunity_api.py \
  tests/test_memecoin_opportunity_bags_coverage.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run narrow worker tests**

```bash
python3 -m pytest tests/test_memecoin_opportunity_confirmation.py -q \
  -k "transport or evm_poll or pons or confirmation"
python3 -m pytest tests/test_memecoin_opportunity_engine.py -q \
  -k "restore_pipeline or forward_arm"
```

Expected: PASS.

- [ ] **Step 3: Run static checks**

```bash
python3 scripts/validate_schema_refs.py
python3 -m py_compile \
  sniper/research/memecoin_opportunity/venue_policy.py \
  sniper/research/memecoin_opportunity/projection.py \
  scripts/memecoin_opportunity_worker.py
git diff --check origin/main...HEAD
```

Expected: all commands exit zero.

- [ ] **Step 4: Run the Knowledge Sync Gate**

Complete all 11 questions from `.github/pull_request_template.md`. Record the new venue-authority doctrine.

- [ ] **Step 5: Review branch scope**

Run: `git status --short --branch && git diff --stat origin/main...HEAD && git log --oneline origin/main..HEAD`

Expected: only this specification, plan, policy, worker, settings, projection, API, and focused tests appear.

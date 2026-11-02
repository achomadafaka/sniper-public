# Radar Actionability v2 Implementation Plan

**Goal:** Produce fresh, executable, trajectory-ranked Radar candidates and durable exploratory evidence without touching funded execution.

**Architecture:** Add an opportunity-owned asynchronous Solana quote cache, enrich engine features and receipts, persist compact diagnostic evidence, expose fresh active state separately from history, and audit Bags feed coverage independently.

**Tech:** Python, asyncio, urllib, SQLite, Flask JSON routes, vanilla JavaScript, pytest.

---

### Task 1: Solana round-trip quote adapter

**Files:**
- Create: `sniper/research/memecoin_opportunity/solana_probe.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_solana_probe.py`

1. Write failing parser, acquired-quantity sell, timeout, dedupe, cache-expiry, and pacing tests.
2. Implement a read-only two-quote Jupiter adapter with no swap-transaction endpoint.
3. Implement a bounded non-blocking request queue and result cache.
4. Wire Pump.fun and Bags/Solana requests into the opportunity engine; keep provider failures explicit and fail closed.
5. Run the focused probe and engine tests.

### Task 2: Trajectory and fresh active state

**Files:**
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Test: `tests/test_memecoin_opportunity_directional.py`
- Test: `tests/test_memecoin_opportunity_api.py`

1. Write failing tests for inward velocity, zone transitions, fresh-only active results, stale archive count, and ranking.
2. Persist prior-distance delta and inward velocity as point-in-time features.
3. Return only fresh opportunities in the active list; retain bounded stale history and archive counts.
4. Rank by distance, inward velocity, quality, then freshness.

### Task 3: Target ladder and near misses

**Files:**
- Create: `sniper/research/memecoin_opportunity/diagnostics.py`
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Test: `tests/test_memecoin_opportunity_insights.py`

1. Write failing tests for +2/+4/+6/+8/+12 percent across 5/10/20/60 seconds, first passage, and loss/exit failure ordering.
2. Evaluate the ladder from immutable executable observations.
3. Add compact target summaries and bounded near-miss rows to `read_live_insights`.
4. Include per-venue results without pooling fitted probabilities.

### Task 4: Deduped forward-paper alert receipts

**Files:**
- Modify: `sniper/research/memecoin_opportunity/store.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Test: `tests/test_memecoin_opportunity_store.py`
- Test: `tests/test_memecoin_opportunity_engine.py`

1. Write failing migration, first-CENTER, redelivery, and no-funded-import tests.
2. Add immutable `op_forward_alerts` receipts with a unique launch/rule/clip identity.
3. Insert only on a fresh CENTER transition with exact remote entry and sell evidence.
4. Expose bounded recent receipts as `EXPLORATORY PAPER ALERT`.

### Task 5: Independent Bags/Solana coverage

**Files:**
- Create: `sniper/research/memecoin_opportunity/bags_coverage.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_bags_coverage.py`

1. Write failing tests for verified, missing, quiet, and unmeasured coverage.
2. Parse the documented Bags launch feed without treating it as a trigger.
3. Poll at low cadence only when an API key is present.
4. Persist coverage runtime status without degrading the chain WebSocket when unmeasured.

### Task 6: API and UI

**Files:**
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Modify: `static/app.js`
- Modify: Radar CSS/template files discovered by `rg`
- Test: `tests/test_memecoin_opportunity_api.py`

1. Add API assertions for active/history counts, trajectory, target ladder, near misses, coverage, and alerts.
2. Render only fresh dots on the active Radar.
3. Show archived count, trajectory, target-ladder matrix, near misses, and recent paper alerts.
4. Keep all labels explicitly exploratory.

### Task 7: Operational verification and integration

1. Run only focused opportunity tests and `git diff --check`.
2. Scan opportunity imports and changed paths for funded/signing/execution authority.
3. Commit one logical change per commit.
4. Merge the feature branch into local `main` after verifying the current main SHA and dirty state.
5. Start the Radar fresh under its existing default-OFF opt-in, then verify process health, authenticated API shape, bounded storage, provider throttling, fresh active filtering, and alert dedupe.

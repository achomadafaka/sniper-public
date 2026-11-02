# Radar Opportunity Correctness and Insight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Radar produce price-sensitive executable outcomes, preserve every recoverable source gap, terminate stale confirmations, persist comparable pattern features, and expose honest per-venue insight in the UI.

**Architecture:** Keep the paper-only event ledger and venue isolation. Correct Pump/Bags-4663 constant-product replay with a versioned evaluator, enrich Bags-4663 state through its read-only Lens, and leave Meteora DBC execution fail-closed until its segmented curve config is available. Derive one point-in-time feature payload per landmark and summarize one selected matured sample per launch against a venue baseline.

**Tech Stack:** Python 3.13, SQLite/WAL, Flask, vanilla JavaScript, pytest.

## Global Constraints

- No funded, signing, broadcast, custody, V6, X, or wallet-trigger changes.
- Capture remains default OFF in configuration; live activation uses the existing explicit operator setting.
- Use corrected real UTC for all age and recovery decisions.
- No full test suite; run focused opportunity tests and static checks only.
- Meteora DBC is segmented and configurable; never apply Pump constant-product math without exact config state.
- Existing invalid hot and insight evidence is discarded on activation, per operator authorization; no backup.

---

### Task 1: Price-sensitive executable outcomes and explicit evaluator versions

**Files:**
- Modify: `sniper/research/memecoin_opportunity/curve_math.py`
- Modify: `sniper/research/memecoin_opportunity/outcome_tracker.py`
- Modify: `sniper/research/memecoin_opportunity/outcomes.py`
- Modify: `sniper/research/memecoin_opportunity/store.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Test: `tests/test_memecoin_opportunity_curve_math.py`
- Test: `tests/test_memecoin_opportunity_outcomes.py`
- Test: `tests/test_memecoin_opportunity_store.py`

**Interfaces:**
- Produces: `CONSTANT_PRODUCT_QUOTE_VERSION = "constant-product-v2"`.
- Produces: persisted `op_competing_outcomes.evaluator_version`.
- Preserves: unchanged-pool immediate reverse equals the original immediate reverse quote.

- [ ] **Step 1: Add a moved-reserve regression that expects appreciation and depreciation**

```python
up = constant_product_exit_quote(current_virtual_token_reserves=900_000,
    current_virtual_quote_reserves=560_000, acquired_token_atomic=entry.tokens_out_atomic,
    hypothetical_quote_in_atomic=entry.quote_in_atomic, fee_bps=200)
down = constant_product_exit_quote(current_virtual_token_reserves=1_100_000,
    current_virtual_quote_reserves=460_000, acquired_token_atomic=entry.tokens_out_atomic,
    hypothetical_quote_in_atomic=entry.quote_in_atomic, fee_bps=200)
assert up.net_quote_out_atomic > entry.immediate_exit_quote_atomic
assert down.net_quote_out_atomic < entry.immediate_exit_quote_atomic
```

- [ ] **Step 2: Run the regression and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_opportunity_curve_math.py -q`

- [ ] **Step 3: Preserve the entry-overlay invariant and version the quote/evaluator paths**

```python
invariant = token_reserves * quote_reserves
```

Use the v2 quote constant for quote rows and counterfactual entries. Add `evaluator_version` to the outcome dataclass, immutable row, schema, and migration.

- [ ] **Step 4: Verify outcome math and migration**

Run: `python3 -m pytest tests/test_memecoin_opportunity_curve_math.py tests/test_memecoin_opportunity_outcomes.py tests/test_memecoin_opportunity_store.py -q`

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity tests/test_memecoin_opportunity_curve_math.py tests/test_memecoin_opportunity_outcomes.py tests/test_memecoin_opportunity_store.py
git commit -m "fix(radar): restore price-sensitive executable outcomes"
```

### Task 2: Durable queue and processing gaps

**Files:**
- Modify: `sniper/services/memecoin_opportunity_runtime.py`
- Test: `tests/test_memecoin_opportunity_runtime.py`

**Interfaces:**
- Consumes: `record_source_failure(conn, source_key, observed_at, detail)`.
- Produces: every queue drop and unexpected processing exception opens `op_source_frontiers.has_open_gap=1` and an unresolved `op_data_gaps` row.

- [ ] **Step 1: Extend overflow and processing-failure tests to assert durable gaps**

```python
frontier = conn.execute("SELECT has_open_gap FROM op_source_frontiers WHERE source_key=?",
                        ("source-a",)).fetchone()
assert frontier[0] == 1
assert conn.execute("SELECT count(*) FROM op_data_gaps WHERE resolved_at IS NULL").fetchone()[0] == 1
```

- [ ] **Step 2: Run the runtime tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_opportunity_runtime.py -q`

- [ ] **Step 3: Record a source failure before publishing degraded runtime status**

Call `record_source_failure` with stable details `bounded_queue_overflow` and `runtime_processing_failure:<ExceptionType>` so recovery resumes from the prior durable frontier and repeated failures remain idempotent.

- [ ] **Step 4: Verify runtime and frontier behavior**

Run: `python3 -m pytest tests/test_memecoin_opportunity_runtime.py tests/test_memecoin_opportunity_frontier.py -q`

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_opportunity_runtime.py tests/test_memecoin_opportunity_runtime.py
git commit -m "fix(radar): persist runtime event gaps"
```

### Task 3: Bounded Solana confirmation reconciliation and reorg rebuild

**Files:**
- Modify: `sniper/settings/memecoin_opportunity.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Modify: `.env.example`
- Test: `tests/test_memecoin_opportunity_confirmation.py`
- Test: `tests/test_memecoin_opportunity_settings.py`

**Interfaces:**
- Produces: `solana_orphan_after_s: float`, default `120`.
- Produces: `_solana_confirmation_target(status, received_at, now_iso, orphan_after_s)` returning `FINALIZED`, `ORPHANED`, or `None`.
- Produces: one pipeline rebuild per confirmation batch containing any orphan.

- [ ] **Step 1: Add tests for finalized, explicit error, recent null, expired null, pending cleanup, and one rebuild**

```python
assert _solana_confirmation_target({"err": None, "confirmationStatus": "finalized"}, ...) is ConfirmationState.FINALIZED
assert _solana_confirmation_target({"err": {"InstructionError": [0, "Custom"]}}, ...) is ConfirmationState.ORPHANED
assert _solana_confirmation_target(None, recent, now, 120) is None
assert _solana_confirmation_target(None, expired, now, 120) is ConfirmationState.ORPHANED
```

- [ ] **Step 2: Run focused confirmation/settings tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_opportunity_confirmation.py tests/test_memecoin_opportunity_settings.py -q`

- [ ] **Step 3: Implement bounded terminal decisions and batch rebuild**

Include `received_at` in pending-row reads, reconcile terminal states, remove terminal IDs, open a source recovery gap for orphans, and invoke an injected rebuild callback once after the batch. Apply the same rebuild rule to EVM orphan receipts.

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_memecoin_opportunity_confirmation.py tests/test_memecoin_opportunity_engine.py tests/test_memecoin_opportunity_settings.py -q`

- [ ] **Step 5: Commit**

```bash
git add sniper/settings/memecoin_opportunity.py scripts/memecoin_opportunity_worker.py .env.example tests/test_memecoin_opportunity_confirmation.py tests/test_memecoin_opportunity_settings.py
git commit -m "fix(radar): bound tentative confirmation lifetime"
```

### Task 4: Exact Bags-4663 state enrichment and honest trade-state reduction

**Files:**
- Modify: `sniper/research/memecoin_opportunity/decoders.py`
- Modify: `sniper/research/memecoin_opportunity/bags_4663.py`
- Modify: `sniper/research/memecoin_opportunity/state.py`
- Modify: `sniper/services/memecoin_opportunity_live.py`
- Modify: `sniper/services/memecoin_opportunity_sources.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_decoders.py`
- Test: `tests/test_memecoin_opportunity_live_sources.py`
- Test: `tests/test_memecoin_opportunity_sources.py`
- Test: `tests/test_memecoin_opportunity_state.py`

**Interfaces:**
- Produces: read-only `read_bags_4663_token_state(rpc_call, token_id)` using `BagsLens.getTokenState(address)`.
- Produces: `progress_bps` on `DecodedVenueEvent` and exact Lens reserves/progress on enriched events.
- Consumes: optional `decoded_enricher(envelope, decoded)` in `process_envelope`.

- [ ] **Step 1: Add fixture tests for the 13-field Lens tuple and exact progress propagation**

Encode `exists,migrated,curve,feeShare,poolId,thresholdQuote,realQuoteReserves,realTokenReserves,virtualTokenReserves,virtualQuoteReserves,priceQuotePerToken,bondingProgressPct,totalRaised`; assert `bondingProgressPct=37` becomes `progress_bps=3700`.

- [ ] **Step 2: Add reducer tests proving zero reserves do not reuse stale values and missing trade amounts block**

```python
assert zeroed.virtual_token_reserves == 0
assert "UNSUPPORTED_CURVE_MATH" in zeroed.blocker_codes
assert "DECODER_FAILURE" in missing_trade.blocker_codes
```

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_opportunity_decoders.py tests/test_memecoin_opportunity_live_sources.py tests/test_memecoin_opportunity_sources.py tests/test_memecoin_opportunity_state.py -q`

- [ ] **Step 4: Implement Lens enrichment through the shared 2-rps governor**

Enrichment is read-only and best-effort. A failed Lens call leaves the decoded event intact and records missing state; it never invents reserves. Meteora DBC remains `UNSUPPORTED_CURVE_MATH` without config state.

- [ ] **Step 5: Verify**

Run: `python3 -m pytest tests/test_memecoin_opportunity_decoders.py tests/test_memecoin_opportunity_live_sources.py tests/test_memecoin_opportunity_sources.py tests/test_memecoin_opportunity_state.py tests/test_memecoin_opportunity_engine.py -q`

- [ ] **Step 6: Commit**

```bash
git add sniper/research/memecoin_opportunity sniper/services/memecoin_opportunity_live.py sniper/services/memecoin_opportunity_sources.py scripts/memecoin_opportunity_worker.py tests/test_memecoin_opportunity_decoders.py tests/test_memecoin_opportunity_live_sources.py tests/test_memecoin_opportunity_sources.py tests/test_memecoin_opportunity_state.py
git commit -m "feat(radar): enrich Robinhood curve state"
```

### Task 5: Persist pattern derivatives and build launch-clustered insight summaries

**Files:**
- Modify: `sniper/research/memecoin_opportunity/state.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `sniper/research/memecoin_opportunity/insights.py`
- Modify: `sniper/routes/api_memecoin_opportunity.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_opportunity_state.py`
- Test: `tests/test_memecoin_opportunity_projection.py`
- Test: `tests/test_memecoin_opportunity_insights.py`
- Test: `tests/test_memecoin_opportunity_api.py`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Produces persisted landmark features: interval duration, capital/buyer velocity and acceleration, HHI change, sell-pressure bps, creator-sale delta.
- Produces `read_pattern_insights(conn)` with one selected matured sample per launch, per-venue coverage, outcome distribution, executable-return spread, and activity-matched pattern-vs-venue baselines.
- Produces API `insights` payload and an exploratory UI evidence panel.

- [ ] **Step 1: Add RED tests for persisted derivatives and outcome vocabulary**

Pin the actual outcome enum values: `profit_first`, `loss_first`, and `persistent_exit_failure_first`; reject legacy `JUMP_FIRST/DUMP_FIRST/EXIT_FAILURE_FIRST` counters.

- [ ] **Step 2: Add RED API/UI tests for venue coverage and degeneracy guard**

The UI must show `LEARNING`, sample count, outcome spread, and per-venue executable coverage. If all executable returns occupy a narrow fee-only band, status is `DEGENERATE — NO PRICE INFORMATION` and no top pattern is ranked.

- [ ] **Step 3: Implement point-in-time derivative persistence**

Pass the exact feature mapping used by `evaluate_level1` into `persist_state_evaluation`; include it in immutable snapshot identity and `state_json`.

- [ ] **Step 4: Implement launch-clustered pattern summaries and UI**

Select at most one matured sample per launch, compare each transparent pattern to its venue baseline, require no hard sample floor, and label sparse rows with `sample_count`. Never claim supported edge.

- [ ] **Step 5: Verify**

Run: `python3 -m pytest tests/test_memecoin_opportunity_state.py tests/test_memecoin_opportunity_projection.py tests/test_memecoin_opportunity_insights.py tests/test_memecoin_opportunity_api.py tests/test_memecoin_opportunity_ui_contract.py -q`

Run: `node --check static/app.js`

- [ ] **Step 6: Commit**

```bash
git add sniper/research/memecoin_opportunity sniper/routes/api_memecoin_opportunity.py static/app.js tests/test_memecoin_opportunity_state.py tests/test_memecoin_opportunity_projection.py tests/test_memecoin_opportunity_insights.py tests/test_memecoin_opportunity_api.py tests/test_memecoin_opportunity_ui_contract.py
git commit -m "feat(radar): surface executable pattern insight"
```

### Task 6: Integrate, activate fresh, and prove live behavior

**Files:**
- Modify if required: `docs/superpowers/specs/2026-07-26-memecoin-executable-launch-opportunity-engine-design.md`

**Interfaces:**
- Produces: merged main commit, fresh v2 hot/insight ledgers, restarted opportunity worker and web UI.

- [ ] **Step 1: Run focused opportunity verification**

Run all `tests/test_memecoin_opportunity_*.py` files touched by Tasks 1-5, boundary tests, `git diff --check`, `node --check static/app.js`, and `python3 scripts/validate_schema_refs.py --warn-unknown-tables`. Do not run the full suite.

- [ ] **Step 2: Run a production-shaped local replay smoke**

Prove a moved reserve state creates at least two distinct executable returns and can produce both `profit_first` and `loss_first` fixtures under evaluator v2.

- [ ] **Step 3: Merge to main with one logical merge commit after reviewing branch status**

Confirm unrelated main work remains untouched. Complete the Knowledge Sync block as `bugfix plus approved Radar evidence-surface behavior` and update the approved spec only if the implementation changes its contract.

- [ ] **Step 4: Fresh activation without backup**

Stop only the opportunity worker, remove `data/memecoin_opportunity.db{,-wal,-shm}` and `data/memecoin_opportunity_insights.db{,-wal,-shm}`, start the existing worker/watchdog path, then restart the web process only if needed for code freshness.

- [ ] **Step 5: Verify live API and storage**

Check authenticated snapshot, health, and evidence endpoints; source freshness; no open gaps after recovery; evaluator/quote v2 rows; non-degenerate executable-return range after fresh outcomes mature; insight payload/UI rendering; RPC rate-limit count; queue depth; and DB size/cleanup heartbeat.

- [ ] **Step 6: Report exact residual risk**

Meteora DBC remains capture-only until exact config/segment quote support is implemented. This is visible as venue coverage, not hidden or scored as executable evidence.

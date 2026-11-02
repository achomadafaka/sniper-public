# Memecoin Entry Speed and Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exact funded-entry traces and attribution, preserve `(network_id, mint)` identity, and remove measured repeated EVM immutable-metadata reads without changing entry gates or execution policy.

**Architecture:** Add one terminally immutable attempt ledger and propagate its identifiers through the existing reservation and executor seams. Keep execution order and safety decisions intact; optimize EVM balance conversion through one bounded per-context decimals cache and restrict quote reuse to one safety invocation.

**Tech Stack:** Python 3.13, SQLite, pytest, existing Jupiter/0x/RPC seams.

**Current-main audit:** This plan now runs on source base `7f1f2aa8`. Reuse `b0c80554` network provenance, preserve `3d4b4315` transient slot-scan semantics, preserve `8f5d0735` mcap-tiered Solana impact caps, and retain `7f1f2aa8` global arming plus explicit venue-context refusal. Do not rebuild or regress those landed changes. The funded runtime baseline remains pinned at `0f2bc6b5`.

## Global Constraints

- Robinhood is network `4663`, never generic EVM.
- Coin identity is `(network_id, mint)`; address shape distinguishes only Solana-family from EVM-family.
- Explicit event network identity is authoritative and must reach reservation, execution, position, fill, latency, and trace rows.
- No new hot-path network request.
- Never cache balances, executable quotes, safety verdicts, kill-switch state, available capital, gas prices, or nonces.
- Preserve atomic reservation, one-position-per-coin, kill switch, token safety, exact-amount approval, and no-resubmit behavior.
- Do not change paper verdicts, exits, sizing, or adaptive policy behavior.

---

### Task 1: Add the immutable attempt ledger

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Create: `sniper/services/memecoin_entry_trace.py`
- Create: `tests/test_memecoin_entry_trace.py`
- Modify: `index/schema_registry.json`

**Interfaces:**
- Produces: `EntryAttemptTrace.start(conn, ev, decision_at, clock) -> EntryAttemptTrace`
- Produces: `EntryAttemptTrace.mark(stage)`, `EntryAttemptTrace.finish(status, reason, **fields)`
- Produces: `db_memecoin.start_entry_attempt(...)` and `db_memecoin.finish_entry_attempt(...)`

- [ ] **Step 1: Write failing schema and clock-domain tests**

```python
def test_attempt_trace_keeps_claimed_source_separate_and_derives_monotonic_stages():
    trace = EntryAttemptTrace.start(conn, event, "2026-07-15T12:00:00+00:00", clock)
    clock.advance(0.025)
    trace.mark("chain_resolution")
    clock.advance(0.010)
    trace.finish("refused", "not approved")
    row = dict(conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone())
    assert row["source_created_at"] == event.created_at
    assert row["source_timestamp_trust"] == "claimed_untrusted"
    assert row["clock_domain"] == "observed_real_utc"
    assert row["chain_resolution_completed_at"] >= row["decision_started_at"]
    assert row["chain_resolution_ms"] == pytest.approx(25.0)
    assert row["reservation_completed_at"] is None
    assert row["terminal_reason"] == "not approved"

def test_finalized_attempt_is_immutable():
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    assert trace.finish("refused", "not approved") is True
    assert trace.finish("filled", "confirmed") is False
```

- [ ] **Step 2: Run the tests and confirm the missing-table/import failures**

Run: `python3 -m pytest tests/test_memecoin_entry_trace.py -q`

Expected: fail because `memecoin_entry_attempts` and `EntryAttemptTrace` do not exist.

- [ ] **Step 3: Add the table and trace object**

Create `memecoin_entry_attempts` with identity, source/clock, stage timestamp, stage duration, quote/drift, linkage, and terminal columns from the design. `start_entry_attempt()` inserts and commits the shell row. `finish_entry_attempt()` uses:

```sql
UPDATE memecoin_entry_attempts
SET terminal_status=?, terminal_reason=?, completed_at=?, trace_json=?
WHERE attempt_id=? AND terminal_status IS NULL
```

Derive every absolute stage timestamp from the single decision anchor plus `perf_counter()` elapsed time. Unreached stages remain SQL `NULL`.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/test_memecoin_entry_trace.py tests/test_memecoin_observation_schema.py -q`

Expected: all pass.

- [ ] **Step 5: Regenerate the schema registry and commit the instrumentation-only change**

Run: `python3 scripts/brain_index.py generate`

Commit:

```bash
git add sniper/persistence/db_memecoin.py sniper/services/memecoin_entry_trace.py \
  tests/test_memecoin_entry_trace.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(memecoin): add immutable real-entry attempt traces"
```

### Task 2: Propagate exact event and resolved network identity

**Files:**
- Modify: `sniper/feeds/fomo_feed.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/services/memecoin_real_leg.py`
- Modify: `sniper/services/memecoin_evm_leg.py`
- Modify: `sniper/services/memecoin_risk.py`
- Modify: `sniper/services/memecoin_evm_risk.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `tests/test_memecoin_evm_unknown_resolution.py`
- Create: `tests/test_memecoin_entry_identity.py`

**Interfaces:**
- Produces: `resolve_entry_identity(conn, ev, resolve_fn=None) -> ResolvedEntryIdentity`
- Extends: `reserve_entry(..., origin_event_id, attempt_id, signal_source, source_price, source_market_cap, observed_at, clock_domain)`
- Extends: `reserve_entry_evm(...)` with the same provenance fields.

- [ ] **Step 1: Write failing identity tests**

```python
def test_explicit_4663_never_calls_resolver_and_reaches_pending_row():
    resolver = Mock(side_effect=AssertionError("resolver must not run"))
    result = resolve_entry_identity(conn, event(network_id="4663"), resolve_fn=resolver)
    assert result.network_id == "4663"
    assert result.source == "explicit_event"
    assert resolver.call_count == 0

def test_conflicting_event_history_stays_ambiguous_without_external_guess():
    insert_event_network(conn, mint="0xabc", network_id="4663")
    insert_event_network(conn, mint="0xabc", network_id="8453")
    resolver = Mock(return_value="4663")
    result = resolve_entry_identity(conn, event(network_id="evm-unknown"), resolve_fn=resolver)
    assert result.status == "ambiguous"
    assert result.network_id == "evm-unknown"
    assert resolver.call_count == 0

def test_reservation_carries_exact_origin_fields():
    decision, pending_id = reserve_entry_evm(conn, network_id="4663", **entry_fields)
    row = db.pending_row(conn, pending_id)
    assert row["origin_event_id"] == "event-1"
    assert row["attempt_id"] == "attempt-1"
    assert row["source_price"] == 0.25
    assert row["signal_source"] == "global_feed"
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `python3 -m pytest tests/test_memecoin_entry_identity.py tests/test_memecoin_evm_unknown_resolution.py -q`

Expected: fail on the missing resolver result and pending provenance columns.

- [ ] **Step 3: Implement one resolution and provenance path**

Use explicit event network directly. For unresolved EVM, query indexed distinct concrete event networks once; one value resolves locally, multiple values return `ambiguous`, and zero values may call the existing resolver once. Pass the result into `real_entry()` so route selection cannot resolve again.

Extend `CopyEvent` defaults with `received_at_normalized` and `clock_domain`; global feed and follow activity stamp `observed_real_utc` without changing `created_at` trust.

- [ ] **Step 4: Finish every early real-hook return**

Start the trace before approval routing. Finalize `not_approved`, `unresolved_network`, `ambiguous_network`, `skipped_mcap`, `stale`, `skipped_chase`, `disarmed`, and unexpected exceptions with one reason. Pass the trace into the venue leg for accepted attempts.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m pytest tests/test_memecoin_entry_identity.py tests/test_memecoin_evm_unknown_resolution.py tests/test_memecoin_network_router.py tests/test_memecoin_4663_routing.py tests/test_memecoin_real_leg.py tests/test_memecoin_evm_leg.py -q`

Commit:

```bash
git add sniper/feeds/fomo_feed.py sniper/services/memecoin_real_runner.py \
  sniper/services/memecoin_real_leg.py sniper/services/memecoin_evm_leg.py \
  sniper/services/memecoin_risk.py sniper/services/memecoin_evm_risk.py \
  sniper/persistence/db_memecoin.py tests/test_memecoin_entry_identity.py \
  tests/test_memecoin_evm_unknown_resolution.py
git commit -m "fix(memecoin): preserve exact entry event and network identity"
```

### Task 3: Attribute quote and fill drift to the exact event

**Files:**
- Modify: `sniper/execution/solana_swap_executor.py`
- Modify: `sniper/execution/evm_swap_executor.py`
- Modify: `sniper/execution/solana_real_seams.py`
- Modify: `sniper/execution/evm_real_seams.py`
- Modify: `sniper/execution/solana_wallet.py`
- Modify: `sniper/execution/evm_wallet.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `tests/test_memecoin_entry_executor.py`
- Modify: `tests/test_memecoin_evm_entry_executor.py`
- Create: `tests/test_memecoin_entry_drift.py`

**Interfaces:**
- Produces: `buy_drift(source_price, quote_price, fill_price) -> dict[str, float | None]`
- Produces: quote metadata keys `_sniper_quote_price`, `_sniper_quote_observed_at`, `_sniper_quote_id` on bound quote responses.
- Extends: fill and exec-latency rows with `attempt_id`, `origin_event_id`, `network_id`, quote fields, and drift fields.

- [ ] **Step 1: Write failing exact-source and formula tests**

```python
def test_buy_drift_signs_are_explicit():
    assert buy_drift(100.0, 110.0, 121.0) == {
        "source_drift_pct": pytest.approx(10.0),
        "execution_slippage_pct": pytest.approx(10.0),
        "total_copy_drift_pct": pytest.approx(21.0),
    }

def test_executor_uses_pending_exact_price_not_latest_trader_event():
    pending_id = reserve_with_source_price(conn, source_price=2.0, origin_event_id="exact")
    insert_newer_same_trader_buy(conn, their_price=9.0)
    result = execute_confirmed_entry(conn, pending_id, quote_price=2.2, fill_price=2.4)
    position = db.get_position(conn, result["position_id"])
    assert position["origin_event_id"] == "exact"
    assert position["their_price"] == 2.0
    assert position["fill_gap_pct"] == pytest.approx(20.0)
```

- [ ] **Step 2: Run tests and confirm legacy resolver failure**

Run: `python3 -m pytest tests/test_memecoin_entry_drift.py tests/test_memecoin_entry_executor.py tests/test_memecoin_evm_entry_executor.py -q`

Expected: the executor selects the newer event or lacks quote metadata.

- [ ] **Step 3: Implement quote metadata without extra requests**

Use amounts already returned by Jupiter/0x and decimals already available from the first balance/metadata read. Hash only canonical non-secret fields. Store quote age as confirmation time minus quote observation time. Do not cache the quote.

- [ ] **Step 4: Persist exact attribution**

Read provenance from `pending_row()`. Use legacy `resolve_mint_their_price()` only when `source_price` is absent, and stamp `price_attribution='legacy_source_chain'`; exact pending price stamps `exact_event`. Pass `origin_event_id` to `open_position()` and `insert_fill()`.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m pytest tests/test_memecoin_entry_drift.py tests/test_memecoin_entry_executor.py tests/test_memecoin_evm_entry_executor.py tests/test_memecoin_fill.py tests/test_memecoin_fill_honesty.py -q`

Commit:

```bash
git add sniper/execution/solana_swap_executor.py sniper/execution/evm_swap_executor.py \
  sniper/execution/solana_real_seams.py sniper/execution/evm_real_seams.py \
  sniper/execution/solana_wallet.py sniper/execution/evm_wallet.py \
  sniper/persistence/db_memecoin.py tests/test_memecoin_entry_drift.py \
  tests/test_memecoin_entry_executor.py tests/test_memecoin_evm_entry_executor.py
git commit -m "feat(memecoin): split exact source quote and fill drift"
```

### Task 4: Make duplicate protection chain-aware

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_risk.py`
- Modify: `sniper/services/memecoin_evm_risk.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Modify: `tests/test_memecoin_dup_open.py`
- Modify: `tests/test_memecoin_reserve_entry.py`
- Create: `tests/test_memecoin_chain_identity_concurrency.py`

**Interfaces:**
- Produces: `coin_identity(network_id, mint) -> tuple[str, str]`
- Produces: `real_open_coin_ids(conn)` and `pending_open_coin_ids(conn)`.

- [ ] **Step 1: Write failing identity and race tests**

```python
def test_same_address_on_two_networks_is_two_coin_identities():
    a = reserve(conn_a, network_id="8453", mint="0xabc")
    b = reserve(conn_b, network_id="4663", mint="0xabc")
    assert a.pending_id != b.pending_id

def test_global_and_follow_race_same_network_has_one_winner():
    results = race_reservations(network_id="4663", mint="0xabc")
    assert sum(result.pending_id is not None for result in results) == 1
```

- [ ] **Step 2: Run tests and confirm mint-only uniqueness failure**

Run: `python3 -m pytest tests/test_memecoin_chain_identity_concurrency.py tests/test_memecoin_dup_open.py tests/test_memecoin_reserve_entry.py -q`

- [ ] **Step 3: Migrate indexes and risk held sets**

Replace the pending partial unique index with `(COALESCE(network_id, normalized_legacy_network), mint) WHERE status='reserved'`. Replace the open-position partial unique index with network identity included. Risk contexts compare `coin_identity(network_id, mint)`, while capacity/exposure remain per-chain and unchanged.

- [ ] **Step 4: Run concurrency and risk tests and commit**

Run: `python3 -m pytest tests/test_memecoin_chain_identity_concurrency.py tests/test_memecoin_dup_open.py tests/test_memecoin_reserve_entry.py tests/test_memecoin_atomic_sizing.py tests/test_memecoin_fraction_sizing.py tests/test_memecoin_hardening_integration.py -q`

Commit:

```bash
git add sniper/persistence/db_memecoin.py sniper/services/memecoin_risk.py \
  sniper/services/memecoin_evm_risk.py sniper/services/memecoin_copy_trader.py \
  tests/test_memecoin_chain_identity_concurrency.py tests/test_memecoin_dup_open.py \
  tests/test_memecoin_reserve_entry.py
git commit -m "fix(memecoin): key duplicate guards by chain and mint"
```

### Task 5: Remove repeated EVM immutable metadata reads

**Files:**
- Create: `sniper/execution/metadata_cache.py`
- Modify: `sniper/execution/evm_wallet.py`
- Modify: `sniper/execution/evm_real_seams.py`
- Modify: `sniper/execution/evm_token_safety.py`
- Modify: `sniper/services/memecoin_evm_leg.py`
- Create: `tests/test_memecoin_evm_metadata_cache.py`
- Modify: `tests/test_memecoin_evm_token_safety.py`

**Interfaces:**
- Produces: `BoundedLru(maxsize).get_or_load(key, loader)` for immutable metadata only.
- Extends: `erc20_balance(..., decimals=None, decimals_fn=None)`.
- Extends: `build_evm_token_safety(..., decimals_fn=None)`.

- [ ] **Step 1: Write failing request-count and quote-lifetime tests**

```python
def test_repeated_balance_reads_load_decimals_once():
    calls = Counter()
    balance = bound_balance_reader(http=counting_http(calls), cache_size=8)
    balance("0xtoken")
    balance("0xtoken")
    assert calls["decimals"] == 1
    assert calls["balanceOf"] == 2

def test_safety_quote_is_shared_within_attempt_but_not_across_attempts():
    safety("0xtoken")
    first = calls["zerox_quote"]
    safety("0xtoken")
    assert calls["zerox_quote"] > first
```

- [ ] **Step 2: Run tests and confirm repeated reads/stale reuse**

Run: `python3 -m pytest tests/test_memecoin_evm_metadata_cache.py tests/test_memecoin_evm_token_safety.py -q`

- [ ] **Step 3: Implement bounded immutable cache and per-attempt quote reuse**

Create one cache per `(wallet, chain)` context. Seed the quote token from `CHAIN_QUOTE`. Share `decimals_fn` with the safety and executor bindings. Move `_quote_cache` inside `token_safety_fn()` so each invocation starts empty and discards quotes on return. Keep storage-slot layout caching bounded because layout is immutable metadata.

Preserve the landed `3d4b4315` distinction: transient slot-scan transport failures are never cached; only a clean completed scan may store the negative sentinel.

- [ ] **Step 4: Run EVM entry/safety tests and commit**

Run: `python3 -m pytest tests/test_memecoin_evm_metadata_cache.py tests/test_memecoin_evm_token_safety.py tests/test_memecoin_evm_seams.py tests/test_memecoin_evm_entry_executor.py tests/test_memecoin_evm_leg.py -q`

Commit:

```bash
git add sniper/execution/metadata_cache.py sniper/execution/evm_wallet.py \
  sniper/execution/evm_real_seams.py sniper/execution/evm_token_safety.py \
  sniper/services/memecoin_evm_leg.py tests/test_memecoin_evm_metadata_cache.py \
  tests/test_memecoin_evm_token_safety.py
git commit -m "perf(memecoin): cache bounded immutable EVM metadata"
```

### Task 6: Add the read-only report and close the branch

**Files:**
- Create: `scripts/report_memecoin_entry_baseline.py`
- Create: `tests/test_memecoin_entry_report.py`
- Modify: `docs/superpowers/specs/2026-07-15-memecoin-entry-speed-coverage-design.md`
- Modify: `index/dependency_index.json`

**Interfaces:**
- Produces: `build_entry_report(conn, since=None) -> dict` and CLI JSON/table output.

- [ ] **Step 1: Write failing report tests**

Assert separate `global_feed`/`follow_activity` rows, chain rows for `solana`, `1`, `56`, `8453`, and `4663`, explicit unknown values for missing legacy funnel stages, p50/p95 from trace durations, terminal reason counts, and exact drift coverage.

- [ ] **Step 2: Implement the read-only reporter**

Open with `get_readonly_connection()`. Never call `ensure_memecoin_tables()`. Query trace rows when the table exists and expose legacy evidence separately rather than nearest-event attribution.

- [ ] **Step 3: Run focused report tests and regenerate indexes**

Run: `python3 -m pytest tests/test_memecoin_entry_report.py -q`

Run: `python3 scripts/brain_index.py generate`

- [ ] **Step 4: Run the required verification set**

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/test_memecoin*.py -q
python3 -m pytest tests/test_memecoin_auth_race.py tests/test_memecoin_chain_identity_concurrency.py \
  tests/test_memecoin_exec_lock.py tests/test_memecoin_runtime_deploy.py tests/test_memecoin_runtime_verify.py -q
python3 -m compileall -q sniper scripts
git diff --check origin/main...HEAD
```

`node --check static/app.js` is not required because this plan changes no UI file.

- [ ] **Step 5: Run the Knowledge Sync Gate**

Answer all 11 questions from `.github/pull_request_template.md`. This work has a knowledge delta: the new trace schema, exact identity contract, and live-canary requirement belong in the design doc and branch-close report. Do not write Supabase memory without explicit user approval.

- [ ] **Step 6: Commit the reporter and final documentation**

```bash
git add scripts/report_memecoin_entry_baseline.py tests/test_memecoin_entry_report.py \
  docs/superpowers/specs/2026-07-15-memecoin-entry-speed-coverage-design.md \
  index/dependency_index.json index/schema_registry.json
git commit -m "chore(memecoin): report real-entry funnel and latency"
```

- [ ] **Step 7: Adversarial review and push**

Review `git diff origin/main...HEAD` for identity loss, cross-chain joins, fake zero timings, quote reuse, added network calls, release-on-unknown, extra commits inside reservation transactions, and paper-policy changes. Fix proven defects with regression tests and separate commits. Push `feat/memecoin-entry-speed-coverage`; do not merge or deploy.

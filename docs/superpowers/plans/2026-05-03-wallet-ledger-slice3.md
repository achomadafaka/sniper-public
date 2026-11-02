# Wallet Ledger Slice 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land shared price data: batch CLOB price-history helper, append-only `market_price_points`, interval-merge coverage detection, per-wallet lookup requests, and per-run analysis manifests. Eliminate every remaining single-asset price loop from the ledger path.

**Architecture:** All historical price data is shared and lives in `market_data.duckdb`. Per-wallet analyzer obligations (lookup requests, manifests) stay in the wallet warehouse. `market_price_points` is append-only, versioned by `source_version` in the PK; `market_price_coverage_ranges` records explicit captured intervals so coverage checks merge ranges instead of scanning min/max+gaps. Failures are tracked at the range/request level, never as point rows. The shared warehouse lock from Slice 1 serializes writers; reads are unlocked.

**Tech Stack:** Python, DuckDB, pytest, `httpx` (or existing `polymarket_api` HTTP client).

---

## Task 1: Batch Price-History Helper

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/price_api.py`
- Test: `tests/test_wallet_ledger_price_api.py`

- [ ] Write failing tests for: chunking input asset ids into batches of ≤20; serializing `(token_id, fidelity_minutes, range_start_ts, range_end_ts)` request payload; parsing the `/batch-prices-history` response into `(token_id, ts, price)` tuples; surfacing `api_error` at the *batch* level (not as fake point rows); preserving response order; respecting a configurable `source_version` constant.
- [ ] Run `python3 -m pytest tests/test_wallet_ledger_price_api.py -q`; expected failure: module missing.
- [ ] Implement a pure helper `fetch_batch_price_history(client, requests, source_version) -> BatchPriceResult` with no DuckDB writes inside. Mock the HTTP client at the `httpx` boundary in tests; do not hit live APIs.
- [ ] Add a constant `PRICE_HISTORY_SOURCE_VERSION = "batch-prices-history-v1"` and document the migration rule: bump on payload-shape or normalization change.
- [ ] Run `python3 -m pytest tests/test_wallet_ledger_price_api.py -q` and commit locally.

## Task 2: Shared Price Points Writer

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/prices.py`
- Test: `tests/test_wallet_ledger_prices.py`

- [ ] Write failing tests for: idempotent insert into `market_price_points` keyed by `(token_id, fidelity_minutes, ts, source_version)`; preserving `first_capture_run_id` on conflict; setting `point_status='requested_no_data'` for ranges where the API returned an empty series; refusing to write any row with `point_status NOT IN ('captured', 'requested_no_data')` (CHECK already enforces this — assert the CHECK fires when bypassed).
- [ ] Implement `upsert_price_points(conn, points, *, source_version, capture_run_id, captured_at)` with `ON CONFLICT (token_id, fidelity_minutes, ts, source_version) DO NOTHING` so versioned reruns are append-safe and `first_capture_run_id` is sticky.
- [ ] `content_hash` per row hashes the canonical serialized payload `(token_id, fidelity_minutes, ts, price, source_version)`. Use `json.dumps(..., sort_keys=True, separators=(",",":"))` then sha256 — match the convention in `events.py:_sha256_text`.
- [ ] Acquire `shared_warehouse_lock` from `index.duckdb` for the duration of the write; release on success or failure. Add a regression test that two writers cannot both hold the lock.
- [ ] Run focused tests and commit locally.

## Task 3: Coverage Range Merge

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/coverage.py`
- Test: `tests/test_wallet_ledger_coverage.py`

- [ ] Write failing tests for: inserting `fully_captured`/`partial`/`failed` rows into `market_price_coverage_ranges`; merging overlapping/adjacent fully-captured ranges into a coverage view; computing the *uncovered intervals* of a requested `[A, B]` against existing coverage; rejecting `coverage_status` values outside the spec's three-value enum (CHECK assertion).
- [ ] Implement `compute_uncovered(conn, *, token_id, fidelity_minutes, source_version, requested_start, requested_end) -> list[tuple[int, int]]` using SQL interval merge (sort by `range_start_ts`, walk, merge `[s,e]` if `next_start <= cur_end + 1`).
- [ ] Implement `record_coverage_range(conn, *, token_id, fidelity_minutes, source_version, range_start_ts, range_end_ts, coverage_status, source, api_error_msg, content_hash, first_capture_run_id, captured_at)` with `ON CONFLICT DO NOTHING`. `api_error_msg` is non-null only when `coverage_status='failed'`.
- [ ] Add a regression test that proves API errors do NOT create point rows: a failed batch records exactly one `coverage_ranges` row with `coverage_status='failed'` and zero new `market_price_points` rows.
- [ ] Run focused tests and commit locally.

## Task 4: Per-Wallet Lookup Requests

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/lookups.py`
- Test: `tests/test_wallet_ledger_lookups.py`

- [ ] Write failing tests for: idempotent insert keyed by `(wallet, capture_run_id, token_id, target_ts, fidelity_minutes, request_reason)`; matching a request to the nearest `market_price_points` row by `source_version` and `|ts - target_ts| <= match_window_seconds`; recording `lookup_status='no_match_in_window'` when no point qualifies; recording `lookup_status='pending'` when no `coverage_ranges` row spans `target_ts`; recording `lookup_status='api_error'` when the most recent overlapping coverage range failed.
- [ ] Implement `record_lookup_request(conn, *, wallet, capture_run_id, token_id, target_ts, fidelity_minutes, request_reason, match_window_seconds, source, source_version, captured_at)`. Match logic queries `market_data.duckdb` (read-only) and writes back to the wallet warehouse.
- [ ] `match_lag_seconds = matched_price_ts - target_ts` (signed). `match_source_version` records which `source_version` produced the match — must match the row's PK component.
- [ ] Run focused tests and commit locally.

## Task 5: Analysis Price Manifest

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/manifest.py`
- Test: `tests/test_wallet_ledger_manifest.py`

- [ ] Write failing tests for: writing one row per `(wallet, capture_run_id, window_id, token_id, range_start_ts, range_end_ts)`; computing `content_hash` over the canonical-ordered `(token_id, fidelity_minutes, ts, source_version)` sequence used inside `[range_start_ts, range_end_ts]`; refusing to write a manifest row when at least one underlying `market_price_points` row would be missing for the requested fidelity (the manifest is a *reproducibility contract*, not a partial promise).
- [ ] Implement `record_analysis_price_manifest(conn_wallet, conn_market, *, wallet, capture_run_id, window_id, token_id, fidelity_minutes, range_start_ts, range_end_ts, source_version, source, captured_at)`.
- [ ] Add an integration test that replays a manifest by selecting `market_price_points` rows by `source_version` and range, recomputes the content_hash, and asserts equality.
- [ ] Run focused tests and commit locally.

## Task 6: Kill Single-Asset Loops In Ledger Path

**Files:**
- Modify (audit): `sniper/analysis/wallet_protocol/**/*.py` — the ledger subtree only. Out of scope: `scripts/record_fetch_snapshot.py` paths that already gated `--from-fixture`; live runner code outside the ledger.
- Test: `tests/test_ledger_no_single_asset_price_loops.py`

- [ ] Write a failing test that walks the ledger module subtree, parses each `.py` with `ast`, and fails if it sees any HTTP call to a single-asset price endpoint (`/prices-history` without `/batch-`). Allow `polymarket_api` boundary modules and explicit allowlist constants only.
- [ ] If the test finds offenders, port them to `fetch_batch_price_history`. If a caller genuinely needs only one asset, it still goes through the batch helper with a 1-element list.
- [ ] Run the focused regression test plus the full ledger suite and commit locally.

## Task 7: Slice 3 Verification

**Files:**
- Test: `tests/test_wallet_ledger_slice3_integration.py`

- [ ] Write an integration test that exercises the full Slice 3 path with a mocked HTTP client: request three tokens × one fidelity × one range, assert (a) one batch HTTP call (chunk size proven); (b) `market_price_points` populated; (c) `market_price_coverage_ranges` has one `fully_captured` row per token; (d) a follow-up overlapping-range request only fetches the uncovered tail; (e) `analysis_price_manifest` content_hash is stable across two replays.
- [ ] Run `python3 -m pytest tests/test_wallet_ledger_*.py -q`; assert all green.
- [ ] Run `python3 scripts/validate_schema_refs.py`; assert clean.
- [ ] Commit locally.

---

## Tail Items From Slice 2 Audit

These three items are active implementation debt carried from the Slice 2 audit. None block Task 1; all must be resolved before Slice 3 close (i.e. before Task 7's verification commit).

### TI-1: `event_uid` ordinal fallback — spec-amend or hard-fail

**Origin:** `sniper/analysis/wallet_protocol/ledger/events.py:60-69`. When `log_index` is missing, the code substitutes `event_row_ordinal` into the third component of `event_uid`. The spec at `docs/superpowers/specs/2026-05-02-wallet-fact-ledger-design.md:226` strictly requires `{source}:{transaction_hash}:{log_index}:{token_id_or_outcome_index}` — the ordinal substitution is undocumented.

**Resolution path (pick one and land it):**
- [ ] **Option A — spec-amend.** Update spec line 226 to: `{source}:{transaction_hash}:{log_index_or_event_row_ordinal}:{token_id_or_outcome_index}`. Add a sentence: "When the source row lacks a chain `log_index`, `event_row_ordinal` substitutes to keep events from the same transaction distinguishable. The ordinal is stable per source ingest call." Commit the spec change before Slice 3's first task.
- [ ] **Option B — hard-fail.** Change `events.py:_event_uid` to raise `ValueError` when `log_index` is None and the source claims chain identity. Add `ingest_status='rejected'` for rows without log_index from sources that should carry it. Add a regression test that a Goldsky-shaped row without log_index is rejected.

**Default:** Option A. Data API activity rows do not carry `log_index`; rejecting them would break the existing `upsert_activity_events` contract with no benefit.

### TI-2: ambiguous re-ingest no-op edge

**Origin:** `sniper/analysis/wallet_protocol/ledger/events.py:155-247`. If a row is re-ingested as a PK no-op (`ON CONFLICT DO NOTHING`), and a *later* row creates a new ambiguous canonical match, the `UPDATE … SET dedup_status='ambiguous'` at line 247 only fires when the insert succeeded. The existing siblings' `dedup_status` won't be flipped.

**Resolution path (pick one and land it):**
- [ ] **Option A — accept with rationale + regression.** Add a one-line comment in `events.py` next to the `if result is None: continue` branch: `# canonical_event_hash is deterministic from the row payload; a no-op re-ingest cannot introduce new ambiguity. The sweep at line 247 only needs to fire on successful inserts, which is exactly when a *new* canonical-hash sibling appears.` Land a regression test that proves the asserted invariant: insert row X (canonical), re-ingest row X (no-op), insert row Y (same canonical_hash, different source_event_uid). Assert both surviving rows end up `dedup_status='ambiguous'`. If the invariant ever fails, the test catches it and we flip to Option B.
- [ ] **Option B — defensive fix.** Move the ambiguous UPDATE outside the `if result is None` guard so it fires regardless. Adds one redundant UPDATE per no-op ingest, but is failure-tolerant if the invariant in Option A ever breaks (e.g. canonical-hash function changes mid-flight).

**Default:** Option A. The invariant holds today; pinning it with a regression test is the right cost/safety tradeoff. Only flip to B if the regression test ever needs an `xfail`.

### TI-3: `collateral_token='unknown'` provenance — fix before price/manifest depends on collateral

**Origin:** `sniper/analysis/wallet_protocol/ledger/positions.py:80, 156` and `events.py:80`. When the source row lacks `collateralToken`, the code defaults to the literal string `"unknown"`. Spec section 13 mandates explicit collateral provenance (USDC.e vs pUSD); the current default silently loses the signal. Slice 3 introduces price points and analysis manifests; if any downstream consumer assumes `collateral_token != 'unknown'` while computing `usd_equivalent_amount` or per-fidelity manifests, the silent default becomes a correctness bug.

**Resolution path:**
- [ ] Implement a single helper `resolve_collateral(row, *, capture_ts) -> tuple[str, str | None, dict]` returning `(collateral_token, collateral_token_address, provenance_note)` where:
  - If the row carries `collateralToken` or `collateralTokenAddress`, use those and stamp `provenance_note['collateral_source']='row_explicit'`.
  - Else, derive from `collateral_token_address` if present (look up against a small registry of known addresses: USDC.e and pUSD).
  - Else, use `capture_ts` against the CLOB V2 cutoff `2026-04-28T00:00:00Z` (USDC.e before, pUSD after) and stamp `provenance_note['collateral_source']='cutoff_inferred'` with the cutoff timestamp.
  - Never default to the literal string `"unknown"`. If all three branches fail, set `collateral_token=None` and `provenance_note['collateral_source']='unresolved'` — let downstream consumers decide whether to fail closed.
- [ ] Wire `resolve_collateral` into `events.py:_collateral_token` and `positions.py:upsert_position_snapshots`. Persist the `provenance_note` into `wallet_events.normalization_notes_json` (add column if missing — check spec section 5 column list first) and `wallet_positions.normalization_notes_json` (already present at `positions.py:140`).
- [ ] Add regression tests: explicit-token row, address-only row pre-cutoff, address-only row post-cutoff, missing-everything row.
- [ ] Backfill is out of scope; this affects new ingest only. Existing rows with `collateral_token='unknown'` keep that value and are findable by query.

**Land before:** Task 4 (lookup requests) and Task 5 (manifest), since `usd_equivalent_amount` and any manifest content_hash that touches collateral economics depends on it. Suggested ordering: TI-1 + TI-2 + TI-3 ship as one cleanup commit between Task 3 and Task 4.

---

## Slice 3 Verification

Required checks before merging this slice:
- All Slice 3 task tests pass: `python3 -m pytest tests/test_wallet_ledger_*.py -q`.
- Schema validator clean: `python3 scripts/validate_schema_refs.py`.
- Tail items TI-1, TI-2, TI-3 all resolved (commit refs cited in this plan's task list).
- No single-asset `/prices-history` calls remain in the ledger subtree (Task 6 regression test green).
- `market_price_coverage_ranges` interval-merge fuzzed against random non-overlapping/overlapping/adjacent fixtures.
- Schema reference validator passes after any spec-amend in TI-1.

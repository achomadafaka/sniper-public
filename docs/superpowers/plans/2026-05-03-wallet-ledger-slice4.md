# Wallet Ledger Slice 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Slice 4 of the wallet fact ledger — execution probes and reconstructed flips. Make per-fill probes a public primitive shared by the legacy H4 path and the ledger writer, then add the flip writer that reads `wallet_events` with strict FIFO ordering and canonical dedup.

**Architecture:** Spec sections 11 and 12 already lock the schema and grain. This slice adds three writers and one refactor: (1) extract `iter_fill_probes(...)` from `compute_execution_profile()` so per-fill records become the canonical primitive both legacy aggregation and the ledger consume; (2) `ledger/probes.py` upserts `wallet_execution_probes` + `wallet_execution_probe_horizons`; (3) `ledger/flips.py` reads `wallet_events` (FIFO by `(ts, event_row_ordinal, event_uid)`, group by `(condition_id, token_id)`, canonical hash dedup, ambiguous → `unverified`) and writes `wallet_reconstructed_flips`. No live API calls in any task; all tests are offline against fixtures.

**Tech Stack:** Python, DuckDB, pytest.

---

## File Structure

| Path | Slice | Responsibility |
|---|---|---|
| `scripts/execution_profile.py` | modify | Add `FillProbeRecord` + `iter_fill_probes(...)`; rewire `compute_execution_profile()` to consume it. Keep public aggregate API stable. |
| `sniper/analysis/wallet_protocol/ledger/probes.py` | create | `upsert_execution_probes(conn, wallet, capture_run_id, fill_probes, ...)` — writes both probe and horizon rows; idempotent on PK. |
| `sniper/analysis/wallet_protocol/ledger/flips.py` | create | `reconstruct_and_upsert_flips(conn, wallet, capture_run_id, ...)` — reads `wallet_events`, FIFO-matches, writes `wallet_reconstructed_flips`. |
| `tests/test_execution_probes_iter.py` | create | Per-fill iterator regression suite + legacy aggregate parity. |
| `tests/test_wallet_ledger_probes.py` | create | Probe writer suite — deterministic `probe_uid`, idempotency, horizon long-format, status retention. |
| `tests/test_wallet_ledger_flips.py` | create | Flip writer suite — FIFO ordering, group key, canonical dedup, ambiguous → `unverified`, mixed-collateral PnL NULL, versioned method hash. |
| `tests/test_wallet_ledger_slice4_integration.py` | create | End-to-end: capture activity → probe → flip → assert all three Slice-4 tables populated. |

---

## Task 1: Per-Fill Probe Iterator

**Files:**
- Modify: `scripts/execution_profile.py`
- Test: `tests/test_execution_probes_iter.py`

**Constraints:**
- `compute_execution_profile()` public signature and return contract MUST stay identical. Legacy H4 callers (`exec_profile_adapter.py`, protocol `_resolve_single_branch`) must see no change.
- `iter_fill_probes(...)` is the new public primitive. It yields one `FillProbeRecord` per BUY fill that survives gating.
- `FillProbeRecord` is a frozen dataclass with the exact column shape spec §11 requires for `wallet_execution_probes` plus a `horizons: list[FillProbeHorizon]` field for `wallet_execution_probe_horizons`.
- `DEFAULT_PROBE_CONFIG` in this module is the locked spec §11 dict. `probe_config_hash = sha256(canonical_json(probe_config))` with `sort_keys=True, separators=(",", ":")`.
- `price_source_version` field of the config defaults to `"batch-prices-history-v1"` and MUST be passed through to the resulting record so writes can be partitioned by source version.
- `reference_rule = "preceding_bar_30min_lookback"`. `iter_fill_probes` MUST use the existing `_preceding_bar(history, trade_ts)` picker — same lookback (1800 s) and same selection rule (last bar with `timestamp < trade_ts`). Do NOT introduce `reference_offset_seconds` or `reference_match_window_seconds`; those describe a different rule.
- The refactor is value-preserving on aggregate output. Capture an aggregate baseline from the current `compute_execution_profile()` against a deterministic fixture, then assert the post-refactor output equals the baseline byte-for-byte. Any drift is a regression.

- [ ] **Step 1.1:** Write failing tests in `tests/test_execution_probes_iter.py`:
  - `test_iter_fill_probes_yields_one_record_per_buy_fill` — fixture activity with 3 BUY + 2 SELL → iterator yields 3 records, all `side='BUY'`.
  - `test_iter_fill_probes_horizon_records_match_default_config` — record has `len(horizons) == 3` matching `[3600, 21600, 86400]`.
  - `test_probe_config_hash_changes_when_price_source_version_changes` — same probe_version + horizons but different `price_source_version` → distinct `probe_config_hash`.
  - `test_probe_config_hash_is_canonical_json_sha256` — hash matches `hashlib.sha256(json.dumps(DEFAULT_PROBE_CONFIG, sort_keys=True, separators=(",", ":")).encode()).hexdigest()`.
  - `test_default_probe_config_uses_preceding_bar_30min_lookback` — `DEFAULT_PROBE_CONFIG["reference_rule"] == "preceding_bar_30min_lookback"` and `DEFAULT_PROBE_CONFIG["reference_lookback_seconds"] == 1800`. No `reference_offset_seconds` or `reference_match_window_seconds` keys present.
  - `test_iter_fill_probes_reference_price_matches_preceding_bar_picker` — fixture history with a known preceding bar; record's `reference_price` equals `_preceding_bar(history, fill_ts)` of the same input.
  - `test_compute_execution_profile_aggregate_unchanged_after_refactor` — golden parity: capture aggregate output from a deterministic mocked-client fixture BEFORE the refactor (commit baseline as `tests/fixtures/execution_profile/aggregate_baseline_v1.json`); assert post-refactor output equals the baseline byte-for-byte.
- [ ] **Step 1.2:** Run failing tests:

  ```
  python3 -m pytest tests/test_execution_probes_iter.py -q
  ```

  Expected: ImportError on `iter_fill_probes` / `FillProbeRecord` / `DEFAULT_PROBE_CONFIG`.
- [ ] **Step 1.3:** Implement `FillProbeRecord` (frozen dataclass), `FillProbeHorizon` (frozen dataclass), `DEFAULT_PROBE_CONFIG` (dict matching spec §11), `_canonical_probe_config_hash(config) -> str`, and `iter_fill_probes(wallet, activity, *, archetype=None, client=None, max_trades=500, force=False, probe_config=None) -> Iterator[FillProbeRecord]` in `scripts/execution_profile.py`. The new function reuses the existing `_probe_fill()` private routine but wraps each result into a `FillProbeRecord` with all spec columns populated. `_probe_fill()` stays internal — it has too much CLOB-specific glue to be a public surface.
- [ ] **Step 1.4:** Refactor `compute_execution_profile()` to consume `iter_fill_probes()` for the per-fill loop. Aggregation logic (drift bucketing, status decision) stays where it is. Verify the public return shape is byte-identical against the captured baseline.
- [ ] **Step 1.5:** Run failing tests + the legacy execution-profile test file together:

  ```
  python3 -m pytest tests/test_execution_probes_iter.py tests/test_execution_profile.py -q
  ```

  Expected: all green. Legacy aggregate tests must not regress.
- [ ] **Step 1.6:** Commit locally as `feat(execution-profile): extract iter_fill_probes per-fill primitive`.

## Task 2: Execution Probe Writer

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/probes.py`
- Test: `tests/test_wallet_ledger_probes.py`

**Constraints:**
- `upsert_execution_probes(conn, *, wallet, capture_run_id, fill_probes: Iterable[FillProbeRecord], source: str, source_version: str, captured_at: int) -> int` writes one row per probe to `wallet_execution_probes` and one row per horizon to `wallet_execution_probe_horizons`. Returns the count of probe rows written (not horizon rows).
- Wallet is normalized to lowercase before write.
- `probe_uid = sha256(wallet, fill_event_uid, probe_config_hash)` — recomputed at write time from the record's PK fields, never trusted from the record.
- Idempotent on PK `(wallet, fill_event_uid, probe_config_hash)`. Re-upserts of the same record must be a no-op for probe rows AND horizon rows. Use `INSERT ... ON CONFLICT DO NOTHING` on probe rows; horizons use the same pattern keyed by `(wallet, fill_event_uid, probe_config_hash, horizon_seconds)`.
- `return_pct = drift / reference_price` only when `reference_price > 0`. Otherwise `return_pct=NULL` and `horizon_status='invalid_reference'` regardless of what the iterator returned. This is a write-time invariant; the iterator can pass through whatever it computed but the writer enforces the spec rule.
- Failed probes (no_history, truncated, error) are written, not skipped. They are data-quality facts.

- [ ] **Step 2.1:** Write failing tests in `tests/test_wallet_ledger_probes.py`:
  - `test_upsert_writes_probe_row_and_horizon_rows` — one probe with three horizons → 1 row in `wallet_execution_probes`, 3 rows in `wallet_execution_probe_horizons`.
  - `test_probe_uid_is_deterministic_sha256_of_pk` — `probe_uid` matches `hashlib.sha256(f"{wallet}|{fill_event_uid}|{probe_config_hash}".encode()).hexdigest()` — confirm separator is documented.
  - `test_upsert_is_idempotent_under_repeat` — calling `upsert_execution_probes` twice with identical input produces the same row count, no duplicates.
  - `test_invalid_reference_horizon_overrides_return_pct_to_null` — record with `reference_price=0` + non-null `drift` → horizon row has `return_pct=NULL`, `horizon_status='invalid_reference'`.
  - `test_failed_probe_rows_are_retained` — record with `probe_status='no_history'` → probe row written, horizon rows written with `horizon_status='no_history'`.
  - `test_wallet_normalization_lowercases_input` — uppercase wallet input → row has lowercase wallet.
- [ ] **Step 2.2:** Run failing tests:

  ```
  python3 -m pytest tests/test_wallet_ledger_probes.py -q
  ```

  Expected: ImportError on `upsert_execution_probes`.
- [ ] **Step 2.3:** Implement `sniper/analysis/wallet_protocol/ledger/probes.py` with `upsert_execution_probes(...)`. Use parameterized inserts; never f-string SQL. Wallet normalization via existing `_normalize_wallet` helper from `ledger.runs` (move to a shared `ledger._common` if not already shared).
- [ ] **Step 2.4:** Run focused tests + the existing ledger schema tests:

  ```
  python3 -m pytest tests/test_wallet_ledger_probes.py tests/test_wallet_ledger_schema.py -q
  ```

  Expected: all green.
- [ ] **Step 2.5:** Commit locally as `feat(wallet-ledger): add execution probe writer`.

## Task 3: Reconstructed Flip Writer

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/flips.py`
- Test: `tests/test_wallet_ledger_flips.py`

**Constraints:**
- `reconstruct_and_upsert_flips(conn, *, wallet, capture_run_id, source: str, source_version: str, captured_at: int, reconstruction_method: str = "fifo_event_pair_v1", reconstruction_version: str = "v1") -> int` reads `wallet_events` for the wallet, FIFO-matches BUY→SELL pairs, and writes `wallet_reconstructed_flips`. Returns the count of flip rows written.
- Reader contract (spec §12 amended):
  - `WHERE wallet = ? AND ingest_status = 'normalized'`
  - Group key: `(condition_id, token_id)`
  - Order within group: `ORDER BY ts ASC, event_row_ordinal ASC, event_uid ASC`
  - Dedup: never count two rows with the same `canonical_event_hash`. When a flip's buy or sell side touches an ambiguous canonical group (`dedup_status = 'ambiguous'`), the flip row is still written but with `derivation_check_status = 'unverified'`. Do NOT silently pick one row.
- `flip_uid = sha256(wallet|reconstruction_method|reconstruction_version|buy_event_uid|sell_event_uid|matched_size_tokens)` with `matched_size_tokens` formatted as `f"{value:.18f}"` to avoid float repr drift.
- Collateral handling (spec §13):
  - Per side, `collateral_token` and `collateral_token_address` come from `wallet_events.collateral_token` and `wallet_events.collateral_token_address` of the source row.
  - If `buy_collateral_token != sell_collateral_token`: `flip_pnl_collateral = NULL`. `flip_pnl_usd_equivalent` is always populated using the rule recorded in `collateral_conversion_assumption` (literal string e.g. `"both_sides_usd_via_pusd_one_to_one"`).
  - If both sides match: `flip_pnl_collateral = sell_notional_collateral - buy_notional_collateral` and `flip_pnl_usd_equivalent` = same value when collateral is 1:1 USD-pegged (pUSD or USDC.e); otherwise applies `usd_equivalent_amount` from event rows.
- `derivation_check_status`:
  - `ok` — clean FIFO match, no ambiguous siblings, sizes balance.
  - `drift` — sizes don't reconcile within `1e-9` tolerance after FIFO consumption.
  - `unverified` — at least one side touched a `dedup_status = 'ambiguous'` group OR a side row had `ingest_status != 'normalized'` (should be filtered, but defense-in-depth).
- `input_event_uids_json` is the canonical JSON list of all `event_uid`s that contributed to this flip (in time order). Always at least 2 entries.
- Writer is idempotent on PK `(wallet, flip_uid)`.

- [ ] **Step 3.1:** Write failing tests in `tests/test_wallet_ledger_flips.py`:
  - `test_basic_buy_sell_pair_writes_one_flip_row` — single BUY then single SELL on same `(condition_id, token_id)` → 1 flip row, `derivation_check_status='ok'`, `flip_roi` matches expected.
  - `test_fifo_partial_consumption_produces_correct_flip_size` — BUY 100 then SELL 30 → flip with `size_tokens=30`, residual 70 stays unmatched.
  - `test_fifo_ordering_uses_ts_then_row_ordinal_then_event_uid` — three rows with identical `ts` but different `event_row_ordinal`/`event_uid` produce a deterministic match order; assert via stable `flip_uid`.
  - `test_group_key_is_condition_id_and_token_id_not_outcome_label` — two events on same `condition_id` + outcome_label="Yes" but different `token_id` (multi-outcome market) → no match across token_ids.
  - `test_canonical_hash_dedup_skips_duplicate_within_same_source` — two rows with identical `canonical_event_hash` on `polymarket_data_api` → only the canonical (non-ambiguous) row participates; flip is `ok`.
  - `test_ambiguous_canonical_group_marks_flip_unverified` — buy side has `dedup_status='ambiguous'` (two normalized rows with same canonical hash from different sources) → flip row written with `derivation_check_status='unverified'`.
  - `test_mixed_collateral_sets_flip_pnl_collateral_to_null` — buy side `collateral_token='USDC.e'`, sell side `collateral_token='pUSD'` → `flip_pnl_collateral IS NULL`, `flip_pnl_usd_equivalent` populated.
  - `test_flip_uid_includes_method_and_version` — same activity, two different `reconstruction_version` values → distinct `flip_uid` for the same buy/sell pair.
  - `test_upsert_is_idempotent` — call twice with identical state → no duplicate rows.
  - `test_input_event_uids_json_lists_contributors_in_time_order` — multi-row flip → JSON array sorted by `ts` ASC matching FIFO order.
- [ ] **Step 3.2:** Run failing tests:

  ```
  python3 -m pytest tests/test_wallet_ledger_flips.py -q
  ```

  Expected: ImportError on `reconstruct_and_upsert_flips`.
- [ ] **Step 3.3:** Implement `sniper/analysis/wallet_protocol/ledger/flips.py`. The FIFO matcher is a fresh implementation against `wallet_events` rows (not a port of `flip_reconstruction.py` — that operates on raw activity dicts). Internal helpers:
  - `_load_normalized_events(conn, wallet) -> list[EventRow]`
  - `_dedup_by_canonical_hash(rows) -> tuple[list[EventRow], set[str]]` — returns `(deduped_rows, ambiguous_hashes)` where `ambiguous_hashes` are hashes whose `dedup_status='ambiguous'` so flip-side checks can mark `unverified`.
  - `_group_and_sort(rows) -> dict[tuple[str, str], list[EventRow]]`
  - `_match_fifo(group_rows, ambiguous_hashes) -> list[FlipRecord]`
  - `_compute_flip_uid(...) -> str`
  - Then the public writer batches the resulting `FlipRecord`s into one `INSERT ... ON CONFLICT DO NOTHING`.
- [ ] **Step 3.4:** Run focused tests + Slice 2 events suite (regression on the events the flip writer reads):

  ```
  python3 -m pytest tests/test_wallet_ledger_flips.py tests/test_wallet_ledger_events.py tests/test_wallet_ledger_schema.py -q
  ```

  Expected: all green.
- [ ] **Step 3.5:** Commit locally as `feat(wallet-ledger): add reconstructed flip writer`.

## Task 4: Slice 4 Integration Verification

**Files:**
- Create: `tests/test_wallet_ledger_slice4_integration.py`

**Constraints:**
- End-to-end test: schema fresh → insert 5–8 fixture activity rows → call `upsert_activity_events` → call `iter_fill_probes` over the same activity (with a stub price client returning deterministic history) → call `upsert_execution_probes` → call `reconstruct_and_upsert_flips` → assert all four expected tables populated with consistent IDs.
- Cross-table invariants asserted in the test:
  - Every `wallet_execution_probes.fill_event_uid` exists in `wallet_events.event_uid`.
  - Every `wallet_execution_probe_horizons` row has a matching `(wallet, fill_event_uid, probe_config_hash)` in `wallet_execution_probes`.
  - Every `wallet_reconstructed_flips.buy_event_uid` and `.sell_event_uid` exist in `wallet_events.event_uid`.
  - At least one row each in `wallet_events`, `wallet_execution_probes`, `wallet_execution_probe_horizons`, `wallet_reconstructed_flips`.
  - `flip.input_event_uids_json` is non-empty and every uid resolves in `wallet_events`.

- [ ] **Step 4.1:** Write the integration test stub. Inputs are seeded with stable timestamps and known token_ids so the asserted IDs are deterministic.
- [ ] **Step 4.2:** Run failing tests:

  ```
  python3 -m pytest tests/test_wallet_ledger_slice4_integration.py -q
  ```

  Expected: stub fails until all wiring is in place.
- [ ] **Step 4.3:** Wire the stub to actually call the three writers in sequence with a stub price client.
- [ ] **Step 4.4:** Run the full ledger suite + the new integration test:

  ```
  python3 -m pytest tests/test_wallet_ledger_*.py tests/test_execution_probes_iter.py tests/test_ledger_no_single_asset_price_loops.py -q
  ```

  Expected: all green.
- [ ] **Step 4.5:** Commit locally as `test: verify wallet ledger slice four integration`.

## Slice 4 Verification

Required checks before declaring Slice 4 complete:

- [ ] `python3 -m pytest tests/ -q` — full suite green; no Slice 1–3 regressions.
- [ ] `python3 scripts/validate_schema_refs.py` — clean.
- [ ] `python3 scripts/brain_index.py generate` — runs without diff (Slice 4 adds DuckDB tables that are already in schema; SQLite registry should be untouched).
- [ ] Spec section 11 + 12 reflect locked decisions: probe_config_hash JSON shape; FIFO ordering `(ts, event_row_ordinal, event_uid)`; group key `(condition_id, token_id)`; ambiguous → `unverified`; `price_source_version` participates in probe_config_hash.
- [ ] No live API calls anywhere in Slice 4 paths. Verify by grep: `git -C . grep -l 'PolymarketClient\|requests\.\|httpx\.' sniper/analysis/wallet_protocol/ledger/probes.py sniper/analysis/wallet_protocol/ledger/flips.py` returns empty.
- [ ] No raw activity dicts are FIFO-matched by `flips.py` — the only data source is `SELECT ... FROM wallet_events`. Verify by grep that `flips.py` does not import `flip_reconstruction.py`.

## Out Of Scope (Tracked For Slice 5+)

- `materialize_wallet_data` consuming the four tables together.
- Protocol H4/H5 wiring through ledger-derived probe rows. Until Slice 5 lands the materializer, the legacy `compute_execution_profile()` aggregate still feeds H4 directly. The ledger writers run in parallel as a fact store.
- Live capture orchestration that calls `iter_fill_probes` over fresh activity and writes ledger rows during a real wallet capture run. This belongs to the runner refactor in Slice 5.
- Re-baselining the 10 calibration wallets against ledger-derived flips. Deferred to Slice 6 per spec §17.

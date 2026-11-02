# Wallet Position Builder — Phase 1 Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` and `superpowers:subagent-driven-development` (or `superpowers:executing-plans`) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is test-first: write the failing test, observe the failure, implement, observe pass, then commit.

**Surface:** `position_building_v1` (W2)
**Phase:** 1 — Core Only (pure compute, no wiring)
**Authoritative spec:** `docs/superpowers/specs/2026-05-10-wallet-position-builder-design.md` at commit `b3088aa` on branch `wallet-protocol/position-builder-design`. The spec is the contract. Do not invent fields, thresholds, caveats, or behaviors that aren't in the spec. If the spec is silent on something, the relevant task is marked **spec gap — flag to operator before implementation**, not papered over.

**Architecture:** Pure-compute module (`position_building.py`) with no I/O, mirroring the disciplinary pattern of `timing.py` and `exit_attribution.py`. No DB, no ledger imports, no live API, no price history, no simulator. The core builder consumes normalized activity rows and optional `closed_positions` context and returns a JSON-shaped dict (or `None` when there is nothing usable).

**Tech Stack:** Python 3.x, pytest, dataclasses/typed dicts (no new dependencies).

**Branch:** `wallet-protocol/position-builder-design`. Working tree must remain clean other than this plan file (and, in implementation, the two allowed source files).

---

## Hard scope guards (non-negotiable — do not violate, do not override, do not relax)

The following are forbidden in Phase 1 and must remain forbidden until each is explicitly approved as a separate phase:

- No ProtocolReport wiring
- No `ProtocolReport.position_building_surface`
- No `analyze_wallet.py` changes
- No report rendering
- No DB-backed ledger adapter
- No ledger imports inside the core
- No PR #6 ledger materialization changes
- No simulator logic
- No trigger recommendations
- No price-history calls
- No live API calls
- No analyzer batches
- No DB writes
- No service restarts
- No verdict / gate / `primary_edge_type` / `copyability_note` mutation
- No legacy low-buy stream dependency

If any of these is touched during implementation, stop and flag to operator. The plan reviewer will reject any PR that violates them.

---

## Critical preservation: `source_bounded_ordinal` caps at `partial`

The locked spec (§2.6) defines `observation_window_quality=good` as requiring **both**:
- `identity_quality` in `{stable_event_uid, stable_transaction}`, **and**
- `source_quality` in `{ledger_enriched, activity_rows_complete}`.

Earlier discussion considered allowing `source_bounded_ordinal` at the `good` tier. **The locked spec rejects that.** This plan explicitly preserves that rejection: a dedicated test (Task N2 in Group N — the "test 25" addition) asserts that even with otherwise-perfect inputs (ledger_enriched source, no caveats, valid BUY path, no cap-hit, no fallback), `identity_quality=source_bounded_ordinal` cannot produce `observation_window_quality=good`. The maximum is `partial`.

Implementers must not silently widen the `good` tier. If anyone proposes loosening this rule during implementation review, route the request back to the spec author — do not patch it locally.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `sniper/analysis/wallet_protocol/position_building.py` | **NEW** | Pure-compute module. Public entry point: `compute_position_building_surface(activity_rows, closed_positions=None, *, wallet=None, activity_cap_hit=False, subgraph_fallback_used=False, walk_full_history=False)`. No I/O. No imports from `sniper.analysis.wallet_protocol.ledger`. |
| `tests/test_wallet_protocol_position_building.py` | **NEW** | All Phase-1 core tests (24 spec tests + the explicit `source_bounded_ordinal` cap test = 25 total) plus structural / boundary-lock / no-HTTP / fixture-replay smoke. |

No other file is created or modified in Phase 1.

---

## Test grouping overview

Groups are ordered to allow each task's test to be written, fail, then pass with minimal scaffolding. Earlier groups deliberately leave many fields at `None` / `unavailable` until later groups expand the surface. Each task lists which spec test it covers; the final coverage table maps every spec test (1–24) plus test 25 to a task.

- **Group A** — Module skeleton, public-signature lock, Phase-1 boundary lock.
- **Group B** — Row normalization, alias resolution, side normalization, notional handling, size↔notional mismatch.
- **Group C** — Identity derivation chain.
- **Group D** — Source-quality derivation and downgrade rules.
- **Group E** — Per-position fill stream ordering and BUY/SELL counters.
- **Group F** — Cumulative size path, peak_observed_size, milestones.
- **Group G** — Average entry path and `AVERAGE_ENTRY_INFERRED_AFTER_SELL`.
- **Group H** — Sell ladder (no last-sell collapse) and `sell_ladder_summary`.
- **Group I** — `SELL_EXCEEDS_OBSERVED_SIZE` and `LIKELY_STARTED_MID_POSITION`.
- **Group J** — Reaccumulation detection (`0.10 * peak_observed_size`).
- **Group K** — Pattern classification (precedence-aware, dust rule).
- **Group L** — Closed-positions context-only (`NO_USABLE_BUY_PATH`, label_fallback for context-only).
- **Group M** — Field provenance per-position.
- **Group N** — Observation window quality + `source_bounded_ordinal` cap test.
- **Group O** — Wallet summary (locked denominators, medians, decision rules).
- **Group P** — Top-level distributions and caveat aggregation.
- **Group Q** — Forbidden output keys and surface isolation.
- **Group R** — No-HTTP regression.
- **Group S** — Fixture-replay smoke.

---

## Group A — Module skeleton, signature lock, Phase-1 boundary lock

### Task A1: Create module file with public signature only

**Files:**
- Create: `sniper/analysis/wallet_protocol/position_building.py`
- Create: `tests/test_wallet_protocol_position_building.py`

**Test-first steps:**
- [ ] Write `test_compute_position_building_surface_signature_lock`: imports `compute_position_building_surface` from `sniper.analysis.wallet_protocol.position_building`; uses `inspect.signature` to assert positional parameters `(activity_rows, closed_positions=None, *, wallet=None, activity_cap_hit=False, subgraph_fallback_used=False, walk_full_history=False)`.
- [ ] Write `test_returns_none_when_no_activity_rows_and_no_closed_positions`: calling with `activity_rows=[]` and `closed_positions=None` returns `None`.

**Expected failing assertion:** `ImportError` (module does not exist yet) or signature mismatch.

**Implementation steps:**
- Create `position_building.py` with the public function whose body returns `None` when both `activity_rows` is empty and `closed_positions` is empty/None.
- Add module-level docstring referencing the spec path and commit.
- Add module-level guard comment: "No ledger imports. No DB. No I/O. No live API. No price history."
- No imports from `sniper.analysis.wallet_protocol.ledger`. No imports of `requests`, `httpx`, `urllib`, `aiohttp`. No imports of `db`, `db_*`, `Supabase` modules.

**Expected passing test:** Both tests pass.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py::test_compute_position_building_surface_signature_lock tests/test_wallet_protocol_position_building.py::test_returns_none_when_no_activity_rows_and_no_closed_positions -q
```

**Commit message:**
```
feat(wallet-protocol): scaffold position_building core with public signature
```

---

### Task A2: Phase-1 boundary lock — no ProtocolReport field (spec test 23)

**Files:**
- Modify: `tests/test_wallet_protocol_position_building.py`

**Test-first steps:**
- [ ] Write `test_phase1_boundary_lock_no_protocol_report_field`: imports `ProtocolReport` from `sniper.analysis.wallet_protocol.types`; asserts `position_building_surface` is **not** a field on `ProtocolReport` (use `dataclasses.fields(ProtocolReport)` or `hasattr` against an instance — match whichever style `types.py` uses; preferred: `assert "position_building_surface" not in {f.name for f in dataclasses.fields(ProtocolReport)}`).
- [ ] Write `test_phase1_boundary_lock_analyze_wallet_does_not_emit_field`: build a minimal `ProtocolReport` via the existing analyze_wallet path (or construct directly with required args) and assert `getattr(report, "position_building_surface", "<missing>") == "<missing>"`. Use `getattr` default to allow this test to function regardless of whether the field is present.

**Expected failing assertion:** None at first — the field genuinely shouldn't exist. The test is a guard. If the test fails, someone has wired Phase 2 prematurely; stop and flag to operator.

**Implementation steps:** None — the test is a guard, not a driver. It must pass without modifying `types.py` or `analyze_wallet.py`.

**Expected passing test:** Both tests pass on first run with no implementation changes.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k boundary_lock -q
```

**Commit message:**
```
test(wallet-protocol): lock Phase-1 boundary — no ProtocolReport field
```

---

## Group B — Row normalization, aliases, side, notional

### Task B1: Alias resolution + required-field detection (covers parts of spec test 8)

**Files:**
- Modify: both
- Test: `tests/test_wallet_protocol_position_building.py::test_alias_resolution_*`

**Test-first steps:**
- [ ] `test_alias_conditionId_to_condition_id`: a row with `conditionId` (camelCase) produces a position keyed by that condition_id.
- [ ] `test_alias_token_id_to_asset`: a row with `token_id` is normalized to `asset` and used in the position key.
- [ ] `test_alias_ts_to_timestamp`: a row with `ts` is treated as `timestamp`.
- [ ] `test_alias_usdcSize_to_notional`: a row with `usdcSize` becomes the row's notional.
- [ ] `test_alias_usd_equivalent_amount_to_notional`: same for `usd_equivalent_amount`.
- [ ] `test_missing_required_fields_increment_row_quality_counters`: rows missing one or more of `condition_id`, `asset`/`outcome`, `side`, `price`, `size`, `timestamp` increment the appropriate counter (`malformed_rows`, `missing_price_rows`, `missing_size_rows`, `missing_timestamp_rows`).

**Expected failing assertion:** Position is empty / counters at zero / KeyError.

**Implementation steps:**
- Implement an internal `_normalize_row(raw)` that resolves aliases per spec §1.1, returns a typed dict, and reports per-field availability.
- Track row-level errors as counters; do not raise.

**Expected passing test:** All alias tests and missing-field counter tests pass.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "alias or missing_required" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building row normalization and aliases
```

---

### Task B2: Side normalization

**Test-first steps:**
- [ ] `test_side_normalization_case_insensitive`: `"buy"`, `"Buy"`, `"BUY"` all produce a BUY fill; `"sell"`, `"SELL"`, `"Sell"` all produce a SELL fill.
- [ ] `test_side_malformed_is_ignored_and_counted`: side values like `"unknown"`, `""`, `None`, `"long"` cause the row to be ignored for reconstruction and counted in `malformed_rows`.

**Implementation steps:**
- Normalize side case-insensitively to `BUY` or `SELL`. Anything else: drop and increment `malformed_rows`.
- Do **not** infer side from price/size signs (no simulator-style interpretation).

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k side_normalization -q
```

**Commit message:**
```
feat(wallet-protocol): position_building side normalization
```

---

### Task B3: Notional handling and `SIZE_NOTIONAL_MISMATCH` (spec tests 8 partial, 9)

**Test-first steps:**
- [ ] `test_notional_preserved_when_provided`: row with `notional=42.5` keeps that value.
- [ ] `test_notional_derived_from_size_times_price_when_missing`: row with `size`, `price`, no `notional` → derived `abs(size*price)`.
- [ ] `test_notional_unavailable_when_units_ambiguous`: row with `notional=None` and either `size=None` or `price=None` → notional remains `None`; counters still update.
- [ ] `test_size_notional_mismatch_emitted_when_above_epsilon`: row where `abs(size*price - notional) / max(abs(notional),1) > epsilon` adds `SIZE_NOTIONAL_MISMATCH` to the position's caveats and increments `size_notional_mismatch_rows`.
- [ ] `test_size_notional_within_epsilon_no_mismatch`: small USDC 6-decimal-rounding noise does not trigger the mismatch.

**Implementation steps:**
- Pick an explicit float epsilon (e.g. `1e-3` relative); document the value at top of module as a constant `SIZE_NOTIONAL_EPSILON` and reference §1.1 of the spec.
- Implement the mismatch check exactly as written in §1.1: `abs(size * price - notional) / max(abs(notional), 1) > epsilon`.

**Expected passing test:** All notional tests pass.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "notional or mismatch" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building notional handling and mismatch caveat
```

---

## Group C — Identity derivation chain (spec tests 4, 5, 6, 7, 21)

### Task C1: Identity preference order — stable identifiers

**Test-first steps:**
- [ ] `test_identity_event_uid_marks_stable_event_uid`: row with unique `event_uid` produces position whose `identity_quality == "stable_event_uid"`.
- [ ] `test_identity_raw_event_id_marks_stable_event_uid`: same for `raw_event_id` when `event_uid` absent.
- [ ] `test_identity_tx_plus_logIndex_marks_stable_transaction`: row with `transaction_hash + logIndex` (no event_uid / raw_event_id) → `stable_transaction`.
- [ ] `test_identity_log_index_alias`: `log_index` (snake_case) is accepted as alias for `logIndex`.

**Implementation steps:**
- Walk preferred identity order from §1.2 of the spec exactly. Use the strongest available identifier per row.
- Record the chosen identity tier on each fill; aggregate to a position-level `identity_quality` (worst-of among the position's fills).

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "identity_event_uid or identity_raw_event_id or identity_tx" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building stable identity derivation
```

---

### Task C2: Source-bounded ordinal and local-order-only

**Test-first steps:**
- [ ] `test_identity_source_ref_plus_row_index_marks_source_bounded_ordinal`: row with `source_ref + row_index` (no stable identifier) → `source_bounded_ordinal`.
- [ ] `test_identity_capture_run_id_plus_row_index_marks_source_bounded_ordinal`: same for `capture_run_id + row_index`.
- [ ] `test_identity_bare_row_index_marks_local_order_only`: row with only `row_index` (no source/capture scope) → `local_order_only`.
- [ ] `test_identity_unavailable_when_no_usable_identifier`: row with no usable identifier → `unavailable`.

**Implementation steps:**
- Honor §1.2 strictly: bare `row_index` is **not** durable; mark `local_order_only`. Only `source_batch_id/source_ref/capture_run_id + row_index` reaches `source_bounded_ordinal`.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "source_bounded_ordinal or local_order_only or identity_unavailable" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building source-bounded ordinal identity
```

---

### Task C3: Same-second handling (spec tests 4, 5)

**Test-first steps:**
- [ ] **Spec test 4** — `test_same_second_distinct_event_uids_remain_separate`: two rows with the same `(condition_id, asset, timestamp)` but different `event_uid`s produce two distinct fills (not collapsed); both retain `identity_quality=stable_event_uid`.
- [ ] **Spec test 5** — `test_same_second_no_discriminator_marked_ambiguous_same_second`: two rows with same `(condition_id, asset, timestamp)` and no stable discriminator → both fills marked `identity_quality=ambiguous_same_second`; position caveats include `ORDER_AMBIGUOUS_SAME_SECOND`; rows are preserved separately when possible.
- [ ] `test_same_second_unseparable_marks_inferred`: when rows truly cannot be separated, the affected position reconstruction is marked `inferred`/`ambiguous` (per §1.2 spec language).

**Implementation steps:**
- Group fills by `(condition_id, asset, timestamp)`; within a group, preserve distinct rows when any stable discriminator (event_uid / raw_event_id / tx+logIndex / source_batch+row_index) differs.
- When no discriminator separates them, mark `ambiguous_same_second` and emit `ORDER_AMBIGUOUS_SAME_SECOND`.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k same_second -q
```

**Commit message:**
```
feat(wallet-protocol): position_building same-second discrimination
```

---

### Task C4: Position key — asset over outcome label (spec tests 6, 7)

**Test-first steps:**
- [ ] **Spec test 6** — `test_position_key_asset_wins_over_outcome_text`: rows with both `asset` (token id) and `outcome` text produce a single position keyed by `(condition_id, asset)`; `position_key.type == "asset"`; `identity_quality` not `label_fallback`; no `LABEL_FALLBACK_IDENTITY` caveat.
- [ ] **Spec test 7** — `test_position_key_outcome_fallback_marks_label_fallback`: rows with only `outcome` (no `asset`) produce a position keyed by `(condition_id, outcome)`; `position_key.type == "label_fallback"`; `identity_quality == "label_fallback"`; caveats include `LABEL_FALLBACK_IDENTITY`.

**Implementation steps:**
- Primary key: `(condition_id, asset)` when `asset` is non-null/non-empty.
- Fallback: `(condition_id, outcome)` only when `asset` is unavailable.
- Set `position_key.type` and emit `LABEL_FALLBACK_IDENTITY` only on the fallback path.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k position_key -q
```

**Commit message:**
```
feat(wallet-protocol): position_building position-key asset preference
```

---

### Task C5: Wallet round-trip (spec test 21)

**Test-first steps:**
- [ ] **Spec test 21** — `test_wallet_parameter_round_trip`: `compute_position_building_surface([], wallet="0xabc")` returns a surface (because closed_positions is None and rows is empty — actually returns None per §1; revise: provide one usable BUY row so the function returns a surface, then assert `result["wallet"] == "0xabc"`). Also assert `result["wallet"] is None` when called without `wallet`.

**Implementation steps:**
- Pass `wallet` through into the top-level dict's `wallet` field unchanged.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k wallet_parameter_round_trip -q
```

**Commit message:**
```
feat(wallet-protocol): position_building wallet round-trip
```

---

## Group D — Source-quality derivation and downgrade rules

### Task D1: Source-quality labels and base derivation

**Test-first steps:**
- [ ] `test_source_quality_ledger_enriched_when_ledger_fields_present`: rows carrying `event_uid + capture_run_id + source + source_ref` produce `source_quality=ledger_enriched` per-position.
- [ ] `test_source_quality_activity_rows_complete_when_no_cap_no_fallback`: rows without ledger fields, but `activity_cap_hit=False` and no fallback flags → `activity_rows_complete`.
- [ ] `test_source_quality_activity_rows_ambiguous_when_identity_ambiguous_same_second`: positions whose identity quality is `ambiguous_same_second` should not claim better than `activity_rows_ambiguous`.
- [ ] `test_source_quality_unavailable_when_no_usable_rows`: position with no usable rows → `unavailable`.

**Implementation steps:**
- Derive base source quality from the ledger-field presence, then apply downgrades.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k source_quality -q
```

**Commit message:**
```
feat(wallet-protocol): position_building source-quality derivation
```

---

### Task D2: `activity_cap_hit` downgrade rule (spec test 20 — partial)

**Test-first steps:**
- [ ] **Spec test 20 (part 1)** — `test_activity_cap_hit_without_fallback_caps_quality_at_partial`: with `activity_cap_hit=True`, `subgraph_fallback_used=False`, `walk_full_history=False`, no position can carry `source_quality=activity_rows_complete`; max claim is `activity_rows_partial`. Top-level surface emits caveat `ACTIVITY_CAP_HIT`. Per-position reconstruction caveat `ACTIVITY_TRUNCATED_POSITION_PATH` (already defined in spec §2.2) is added separately when the path itself is affected. The two are distinct: `ACTIVITY_CAP_HIT` = top-level/wallet-level input-capture warning; `ACTIVITY_TRUNCATED_POSITION_PATH` = per-position reconstruction caveat.
- [ ] `test_activity_cap_hit_top_level_caveat_only_when_no_fallback`: `activity_cap_hit=True` with `subgraph_fallback_used=True` does NOT emit `ACTIVITY_CAP_HIT` at the top level; same for `walk_full_history=True`.
- [ ] `test_activity_cap_hit_with_subgraph_fallback_does_not_downgrade`: `activity_cap_hit=True` and `subgraph_fallback_used=True` does not force the partial cap.
- [ ] `test_activity_cap_hit_with_walk_full_history_does_not_downgrade`: `activity_cap_hit=True` and `walk_full_history=True` does not force the partial cap.

**Implementation steps:**
- Implement the §1.3 downgrade rule exactly: cap-hit + neither fallback flag → downgrade `activity_rows_complete` → `activity_rows_partial`; preserve `ledger_enriched` (per spec wording — only the `activity_rows_complete` claim is downgraded).

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k activity_cap -q
```

**Commit message:**
```
feat(wallet-protocol): position_building activity_cap_hit downgrade
```

---

## Group E — Per-position fill stream ordering and BUY/SELL counters

### Task E1: Fill stream ordering rule (spec §2.1)

**Test-first steps:**
- [ ] `test_fill_stream_sorted_by_timestamp_then_row_index`: fills with mixed timestamps and row_indices are emitted in `(timestamp, row_index)` order.
- [ ] `test_fill_stream_stable_identity_only_as_tiebreaker_not_temporal`: same timestamp, same row_index, distinct event_uids: order is deterministic but the surface does not claim event_uid implies temporal truth (no implicit promotion).
- [ ] `test_buy_and_sell_counters`: a position with three BUYs and two SELLs reports `n_buy_fills=3`, `n_sell_fills=2`.

**Implementation steps:**
- Sort fills by `(timestamp, source-bounded row_index/input_order, stable identity tiebreaker)` exactly per §2.1. Treat stable identity as deterministic tiebreaker only.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "fill_stream or counters" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building fill ordering and counters
```

---

## Group F — Cumulative size, peak, milestones (spec tests 1, 2, 3)

### Task F1: Single BUY produces one position (spec test 1)

**Test-first steps:**
- [ ] **Spec test 1** — `test_single_buy_reconstructs_one_position`: one BUY row produces exactly one position with `n_buy_fills=1`, `final_observed_size == row.size`, `peak_observed_size == row.size`, `cumulative_size_path` length 1.

**Implementation steps:**
- Implement initial `cumulative_size_path` with one entry per BUY/SELL.
- Track `peak_observed_size` and `peak_observed_size_ts`.
- Path entry shape per §3.1.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py::test_single_buy_reconstructs_one_position -q
```

**Commit message:**
```
feat(wallet-protocol): position_building single-buy reconstruction
```

---

### Task F2: Cumulative size path with repeated BUYs (spec test 2 partial)

**Test-first steps:**
- [ ] **Spec test 2 (part 1)** — `test_repeated_buys_produce_cumulative_size_path`: three BUYs at sizes [10, 20, 30] produce `cumulative_size_path` observed sizes `[10, 30, 60]`; final observed size `60`; peak `60`.

**Implementation steps:**
- BUY adds `+size` to observed; SELL adds `-size`. Use the shared running observed size.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py::test_repeated_buys_produce_cumulative_size_path -q
```

**Commit message:**
```
feat(wallet-protocol): position_building cumulative size path
```

---

### Task F3: Milestones use `peak_observed_size` not final (spec test 3)

**Test-first steps:**
- [ ] **Spec test 3** — `test_milestones_use_peak_not_final`: BUYs grow to peak 100, then SELL reduces to 60; `time_to_50pct_size_ms` measured against peak (50 of 100), not 30 of 60.
- [ ] `test_n_buys_before_milestones`: ensure `n_buys_before_50pct` and `n_buys_before_75pct` reflect peak-anchored thresholds.

**Implementation steps:**
- Compute milestone thresholds against `peak_observed_size`, not the running observed size or final size.
- `time_to_25pct_size_ms`, `time_to_50pct_size_ms`, `time_to_75pct_size_ms`, `time_to_peak_size_ms`, `time_to_final_observed_size_ms`.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k milestones -q
```

**Commit message:**
```
feat(wallet-protocol): position_building peak-anchored milestones
```

---

## Group G — Average entry path and `AVERAGE_ENTRY_INFERRED_AFTER_SELL`

### Task G1: Average entry path (spec test 2 partial)

**Test-first steps:**
- [ ] **Spec test 2 (part 2)** — `test_average_entry_path_weighted_pre_sell`: three BUYs at (price=0.4, size=10), (0.5, 20), (0.6, 30) produce `average_entry_path` whose final pre-sell entry is the size-weighted average `(0.4*10 + 0.5*20 + 0.6*30) / 60 = 0.5333...`; provenance `factual` for all three entries.
- [ ] `test_average_entry_factual_when_no_sell`: with no SELL, all average-entry entries are `factual`.

**Implementation steps:**
- `average_entry_path` entries shape per §3.1 (`ts`, `avg_entry_price`, `observed_inventory`, `method="average_cost_observed"`, `provenance`).
- Method literal `"average_cost_observed"` only.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k average_entry -q
```

**Commit message:**
```
feat(wallet-protocol): position_building average entry path
```

---

### Task G2: `AVERAGE_ENTRY_INFERRED_AFTER_SELL` after first SELL (spec §2.3)

**Test-first steps:**
- [ ] `test_average_entry_inferred_after_first_sell`: BUY → SELL → BUY produces `average_entry_path` whose entries after the first SELL have `provenance="inferred"`; position's `caveats` include `AVERAGE_ENTRY_INFERRED_AFTER_SELL`; `fields_provenance.average_entry_path == "inferred"`.
- [ ] `test_no_average_entry_caveat_before_any_sell`: BUY-only positions do not emit the caveat.

**Implementation steps:**
- Track a per-position `_seen_sell` flag; once true, switch new average-entry entries to `inferred` and emit caveat.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k average_entry_inferred -q
```

**Commit message:**
```
feat(wallet-protocol): position_building AVERAGE_ENTRY_INFERRED_AFTER_SELL caveat
```

---

## Group H — Sell ladder (no last-sell collapse)

### Task H1: Partial sell ladder preserves all SELLs (spec test 11)

**Test-first steps:**
- [ ] **Spec test 11** — `test_partial_sell_ladder_emits_multiple_sell_entries`: position with three SELLs produces `partial_sell_ladder` length 3; entries in temporal order; each carries `ts`, `event_uid`, `price`, `size`, `notional`, `observed_size_after`, `order_ambiguous`. No "last sell" collapse.

**Implementation steps:**
- Append every usable SELL to `partial_sell_ladder`; do not collapse.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py::test_partial_sell_ladder_emits_multiple_sell_entries -q
```

**Commit message:**
```
feat(wallet-protocol): position_building partial sell ladder preservation
```

---

### Task H2: `sell_ladder_summary`

**Test-first steps:**
- [ ] `test_sell_ladder_summary_shape`: per §3.1 shape: `n_sell_fills`, `total_sell_size`, `total_sell_notional`, `first_sell_ts`, `last_sell_ts`, `size_weighted_sell_price`, `sell_price_method`.
- [ ] `test_sell_ladder_summary_size_weighted_when_all_sizes_valid`: all SELL sizes valid → `sell_price_method="size_weighted"`; `size_weighted_sell_price` is exact size-weighted mean.
- [ ] `test_sell_ladder_summary_partial_when_any_size_invalid`: one SELL with invalid size → `sell_price_method="partial"`.
- [ ] `test_sell_ladder_summary_unavailable_when_no_sells`: no SELLs → summary present but `n_sell_fills=0`, `sell_price_method="unavailable"`, weighted price `None`.

**Implementation steps:**
- Compute size-weighted sell price across the ladder; degrade method when sizes are missing.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k sell_ladder_summary -q
```

**Commit message:**
```
feat(wallet-protocol): position_building sell_ladder_summary
```

---

## Group I — `SELL_EXCEEDS_OBSERVED_SIZE` and `LIKELY_STARTED_MID_POSITION` (spec test 10)

### Task I1: SELL exceeds observed size (spec test 10)

**Test-first steps:**
- [ ] **Spec test 10** — `test_sell_exceeds_observed_size_emits_caveats`: a SELL with no prior usable BUYs whose size > current observed size clamps observed size at zero AND emits BOTH `SELL_EXCEEDS_OBSERVED_SIZE` and `LIKELY_STARTED_MID_POSITION`.
- [ ] `test_sell_overshoot_with_prior_buys_emits_only_sell_exceeds`: prior BUYs existed but a later SELL still exceeds observed size → emit `SELL_EXCEEDS_OBSERVED_SIZE` only; do NOT auto-emit `LIKELY_STARTED_MID_POSITION` (overshoot after prior BUYs may also be partial-capture / missing-fills / malformed-sizes / source-gap; only SELL-without-prior-BUY is the cleanest mid-position-start signal).
- [ ] `test_observed_size_clamped_at_zero_after_sell_overshoot`: observed size in the path entry is non-negative.

**Implementation steps:**
- After processing each SELL, if running observed size goes below zero, clamp to zero and add caveats.
- Maintain a parallel "raw" observed (signed) for diagnostics if needed; only clamped value goes into `cumulative_size_path[*].observed_size`.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k sell_exceeds -q
```

**Commit message:**
```
feat(wallet-protocol): position_building SELL_EXCEEDS_OBSERVED_SIZE
```

---

## Group J — Reaccumulation detection (spec test 12)

### Task J1: Material delta and reaccumulation (spec test 12)

**Test-first steps:**
- [ ] **Spec test 12** — `test_reaccumulation_detected_with_material_delta`: BUY size 100 (peak=100), SELL size 15 (>= 0.10 * 100), later BUY size 15 → `reaccumulation_detected=true`.
- [ ] `test_reaccumulation_cumulative_sells_then_cumulative_buys`: BUY 100 (peak=100), SELL 5, SELL 5, SELL 5 (cumulative SELL = 15 ≥ 10), then BUY 5, BUY 5, BUY 5 (cumulative BUY = 15 ≥ 10) → `reaccumulation_detected=true`. Locks the cumulative reading and prevents future regressions to single-event-only logic.
- [ ] `test_reaccumulation_below_material_delta_not_detected`: BUY 100, SELL 5 (cumulative < 10), BUY 5 (cumulative < 10) → `reaccumulation_detected=false`.
- [ ] `test_reaccumulation_dust_ignored`: BUY 100 (peak=100), tiny SELLs of size 0.5 each (each ≤ `dust_delta = max(1e-9, 0.01 * peak)`) summing to 5, then tiny BUYs summing to 5 → `reaccumulation_detected=false` (dust ignored even when cumulative sum is non-trivial).
- [ ] `test_reaccumulation_below_min_token_floor_not_detected`: when peak is so small that `0.10 * peak < 1e-9`, `material_delta` falls back to `1e-9`.

**Implementation steps:**
- `material_delta = max(1e-9, 0.10 * peak_observed_size)`.
- Use **cumulative** material movement (NOT single-event). After reaching positive observed size, one or more SELLs cumulatively reduce observed size by ≥ `material_delta`; then one or more later BUYs after the sell-down cumulatively increase observed size by ≥ `material_delta`. Ignore dust changes (per `dust_delta` rule in spec §4) for this calculation; keep them in the raw `cumulative_size_path`. Reason: wallets often sell down or re-accumulate through multiple small fills — single-event-only detection would miss exactly the behavior W2 is supposed to reveal.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k reaccumulation -q
```

**Commit message:**
```
feat(wallet-protocol): position_building reaccumulation detection
```

---

## Group K — Pattern classification (spec tests 16, 17, 18, 19)

### Task K1: Pattern precedence and dust rule

**Test-first steps:**
- [ ] **Spec test 19** — `test_dust_ignored_for_pattern_classification`: tiny sell/buy fills (`<= max(1e-9, 0.01 * peak_observed_size)`) remain in raw `cumulative_size_path` but do not influence pattern label or reaccumulation.
- [ ] `test_pattern_unavailable_when_no_buy_path`: position with no usable BUY → `accumulation_pattern="unavailable"`.
- [ ] `test_pattern_fragmented_or_ambiguous_when_identity_ambiguous`: position whose identity is `ambiguous_same_second` or `unavailable`, or source `activity_rows_ambiguous` → `fragmented_or_ambiguous`.
- [ ] `test_pattern_single_fill_when_one_buy`: `n_buy_fills==1` → `single_fill` (regardless of SELL count).

**Implementation steps:**
- Classify in precedence order from §4: `unavailable`, `fragmented_or_ambiguous`, `single_fill`, `front_loaded`, `gradual_accumulation`, `small_buy_accumulation`, `repeated_buy`.
- Apply dust rule via `dust_delta = max(1e-9, 0.01 * peak_observed_size)`. Dust fills are kept in raw path; ignored for pattern classification and reaccumulation detection.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "pattern_unavailable or fragmented_or_ambiguous or single_fill or dust_ignored" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building pattern precedence and dust rule
```

---

### Task K2: Front-loaded pattern (spec test 16)

**Test-first steps:**
- [ ] **Spec test 16** — `test_front_loaded_small_buys`: four BUYs, no single buy >=70% of peak, reaches 75% of peak within 5 minutes → `accumulation_pattern="front_loaded"` and `small_buy_accumulation_detected=true`.
- [ ] `test_front_loaded_first_buy_alone_75pct`: a single BUY that already covers 75% of peak still triggers `front_loaded` when followed by additional buys (`n_buy_fills>=2`).
- [ ] `test_front_loaded_window_default_5min`: confirm the default `FRONT_LOADED_WINDOW_MS = 5 * 60 * 1000` constant exists at module level and is used.

**Implementation steps:**
- Define `FRONT_LOADED_WINDOW_MS = 5 * 60 * 1000` as a module constant.
- Classify `front_loaded` per §4.1.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k front_loaded -q
```

**Commit message:**
```
feat(wallet-protocol): position_building front_loaded pattern
```

---

### Task K3: Gradual accumulation (spec test 17)

**Test-first steps:**
- [ ] **Spec test 17** — `test_gradual_small_buys`: four BUYs, no single buy >=70%, reaches 75% of peak after >5 minutes → `accumulation_pattern="gradual_accumulation"` and `small_buy_accumulation_detected=true`.

**Implementation steps:**
- Classify `gradual_accumulation` per §4.1: `n_buy_fills>=3`, `time_to_75pct_size_ms not null and > FRONT_LOADED_WINDOW_MS`, `largest_buy_share_of_peak < 0.70`, identity/order quality sufficient.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k gradual_accumulation -q
```

**Commit message:**
```
feat(wallet-protocol): position_building gradual_accumulation pattern
```

---

### Task K4: Small-buy fallback and `small_buy_accumulation_detected` (spec test 18)

**Test-first steps:**
- [ ] **Spec test 18** — `test_small_buy_fallback`: 3+ smaller BUYs but milestone timing unavailable (e.g., timestamps tied) → `accumulation_pattern="small_buy_accumulation"` and `small_buy_accumulation_detected=true`.
- [ ] `test_small_buy_accumulation_detected_independent_of_pattern`: a `front_loaded` position that meets the small-buy criteria has `small_buy_accumulation_detected=true`; a `single_fill` position has it `false`.
- [ ] `test_repeated_buy_fallback`: 2+ BUYs that don't meet front_loaded / gradual / small_buy → `accumulation_pattern="repeated_buy"`.

**Implementation steps:**
- `small_buy_accumulation_detected` is the independent boolean per §4.1.
- `repeated_buy` is the catchall per spec.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "small_buy or repeated_buy" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building small_buy and repeated_buy patterns
```

---

## Group L — Closed positions context-only (spec test 13)

### Task L1: `NO_USABLE_BUY_PATH` for context-only positions (spec test 13)

**Test-first steps:**
- [ ] **Spec test 13** — `test_closed_position_only_emits_no_usable_buy_path`: `closed_positions` contains a key not seen in `activity_rows`. Surface emits a position object with `accumulation_pattern="unavailable"`, fill-path `fields_provenance` all `unavailable`, caveats include `NO_USABLE_BUY_PATH`. The position is counted in `n_context_only_positions`, not `n_reconstructed_positions`.

**Implementation steps:**
- After processing activity rows, walk `closed_positions`. For each closed-position key not seen in rows, emit a context-only position object per §1.5.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py::test_closed_position_only_emits_no_usable_buy_path -q
```

**Commit message:**
```
feat(wallet-protocol): position_building NO_USABLE_BUY_PATH context-only
```

---

### Task L2: Context-only key uses `(condition_id, asset)` then label fallback

**Test-first steps:**
- [ ] `test_context_only_uses_asset_key_when_present`: closed-position row with `asset` → `position_key.type="asset"`, `identity_quality="unavailable"` (no fill-level identity exists when no fills exist; the asset key is stable enough to identify the position, but there is no fill identity), `source_quality="unavailable"` or `"activity_rows_partial"` depending on global input state, caveats include `NO_USABLE_BUY_PATH`. Do NOT emit `LABEL_FALLBACK_IDENTITY` because asset is present.
- [ ] `test_context_only_falls_back_to_label_when_no_asset`: closed-position row with no `asset` → `position_key.type="label_fallback"`, `identity_quality="label_fallback"`, caveats include `LABEL_FALLBACK_IDENTITY` AND `NO_USABLE_BUY_PATH`.

**Implementation steps:**
- Honor §1.5 exactly.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k context_only -q
```

**Commit message:**
```
feat(wallet-protocol): position_building context-only label fallback
```

---

## Group M — Field provenance per-position

### Task M1: `fields_provenance` per position

**Test-first steps:**
- [ ] `test_fields_provenance_factual_first_buy_ts`: a position with a clean BUY row → `fields_provenance.first_buy_ts == "factual"`.
- [ ] `test_fields_provenance_unavailable_first_buy_ts`: a position with no BUY rows → `fields_provenance.first_buy_ts == "unavailable"`.
- [ ] `test_fields_provenance_inferred_cumulative_when_truncated`: position whose source carries `ACTIVITY_TRUNCATED_POSITION_PATH` or `PARTIAL_FILL_PATH` → `fields_provenance.cumulative_size_path == "inferred"`.
- [ ] `test_fields_provenance_inferred_average_entry_after_sell`: covered by Task G2.
- [ ] `test_fields_provenance_observation_window_quality_inferred_or_unavailable`: must never be `factual` per §2.5.

**Implementation steps:**
- Build `fields_provenance` per spec §2.5 examples.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k fields_provenance -q
```

**Commit message:**
```
feat(wallet-protocol): position_building fields_provenance
```

---

## Group N — Observation window quality (spec test 20 + test 25)

### Task N1: Quality labels and base classification (spec test 20)

**Test-first steps:**
- [ ] `test_observation_quality_good_when_ledger_enriched_and_stable_event_uid`: ledger-enriched source + stable_event_uid + at least one BUY + no forbidden caveats + at most one `SIZE_NOTIONAL_MISMATCH` row → `observation_window_quality="good"`.
- [ ] `test_observation_quality_partial_when_caveat_present`: same as above plus `STARTS_MID_POSITION` → `partial`.
- [ ] `test_observation_quality_partial_when_two_size_notional_mismatch_rows`: two mismatch rows downgrades to `partial`.
- [ ] `test_observation_quality_poor_when_first_entry_or_milestones_missing`: position exists but first entry / milestone path mostly missing/ambiguous → `poor`.
- [ ] `test_observation_quality_unavailable_when_no_buy_path`: no usable BUY → `unavailable`.
- [ ] **Spec test 20 (part 2)** — `test_observation_quality_max_partial_when_activity_cap_hit_no_fallback`: `activity_cap_hit=True`, no fallback flags → no position can be `good`; max is `partial`.
- [ ] `test_observation_quality_partial_when_label_fallback_identity`: identity_quality `label_fallback` caps quality at `partial`.
- [ ] `test_observation_quality_partial_when_ambiguous_same_second_identity`: identity_quality `ambiguous_same_second` caps quality at `partial`.
- [ ] `test_observation_quality_partial_when_unavailable_identity`: identity_quality `unavailable` caps quality at `partial`.

**Implementation steps:**
- Implement §2.6 rules in order: max-quality rules first, then caveat-based forbidden-good list, then `SIZE_NOTIONAL_MISMATCH` count rule.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k observation_quality -q
```

**Commit message:**
```
feat(wallet-protocol): position_building observation_window_quality
```

---

### Task N2: `source_bounded_ordinal` caps at `partial` (spec test 25 — preservation lock)

**Test-first steps:**
- [ ] `test_source_bounded_ordinal_caps_at_partial`: build a position with `source_quality=ledger_enriched` (or `activity_rows_complete`), all fills carrying `source_ref + row_index` so `identity_quality=source_bounded_ordinal`, no caveats, `activity_cap_hit=False`, no fallback flags, valid BUY path. Assert `observation_window_quality=="partial"` (NOT `good`). Add an explanatory comment in the test pointing to spec §2.6 and to this plan's Critical Preservation note.
- [ ] `test_local_order_only_caps_at_partial`: same, but with `identity_quality=local_order_only`. Same assertion.

**Implementation steps:**
- Confirm §2.6's `good` rule literally requires `identity_quality in {stable_event_uid, stable_transaction}`. Anything else (including `source_bounded_ordinal` and `local_order_only`) is excluded from `good`.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "source_bounded_ordinal_caps_at_partial or local_order_only_caps_at_partial" -q
```

**Commit message:**
```
test(wallet-protocol): lock source_bounded_ordinal at partial (preservation)
```

---

## Group O — Wallet summary (locked denominators)

### Task O1: Position-count denominators (spec §3.2)

**Test-first steps:**
- [ ] `test_wallet_summary_n_positions_includes_context_only`: rows producing 2 reconstructed positions + 1 context-only closed position → `n_positions=3`, `n_reconstructed_positions=2`, `n_context_only_positions=1`, `n_unavailable_positions=1` (the context-only position has `observation_window_quality="unavailable"`).
- [ ] `test_wallet_summary_n_unavailable_includes_row_rooted_unusable`: when activity rows exist for a position but BUYs are missing required fields (e.g., no price/size), that position is counted in `n_unavailable_positions` (NOT in `n_reconstructed_positions`, NOT in `n_context_only_positions`). Definition: `n_unavailable_positions = count(observation_window_quality == "unavailable")`. Includes (a) context-only positions, (b) positions with rows but no usable BUY path, (c) positions where rows exist but are unusable due to missing required fields.
- [ ] `test_wallet_summary_context_only_counted_in_both_unavailable_and_context_only`: a context-only position is counted in BOTH `n_unavailable_positions` AND `n_context_only_positions` (these counters are NOT mutually exclusive). `n_context_only_positions` is the subset of `n_unavailable_positions` that originated from `closed_positions` rather than activity rows.

**Implementation steps:**
- Per §3.2 rules.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k wallet_summary_n_ -q
```

**Commit message:**
```
feat(wallet-protocol): position_building wallet summary position counts
```

---

### Task O2: Shape shares (denominator = `n_reconstructed_positions`)

**Test-first steps:**
- [ ] `test_share_repeated_buy_uses_reconstructed_denominator`: with 4 reconstructed (2 repeated-buy) and 1 context-only → `share_repeated_buy_positions = 2/4 = 0.5`.
- [ ] `test_shape_shares_none_when_no_reconstructed`: `n_reconstructed_positions==0` → all four shape shares are `None`.
- [ ] `test_share_sell_ladder_positions_requires_n_sell_fills_ge_2`: `share_sell_ladder_positions = count(reconstructed positions with n_sell_fills >= 2) / n_reconstructed_positions`. A single SELL is an exit, not a ladder. Position objects always preserve `n_sell_fills` so future consumers can distinguish 0 / 1 / ≥2.
- [ ] `test_share_small_buy_accumulation_uses_reconstructed_denominator`: numerator = reconstructed positions with `small_buy_accumulation_detected=true`.
- [ ] `test_share_reaccumulation_positions_uses_reconstructed_denominator`: numerator = reconstructed positions with `reaccumulation_detected=true`.

**Implementation steps:**
- Per §3.2.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k share_ -q
```

**Commit message:**
```
feat(wallet-protocol): position_building shape shares
```

---

### Task O3: Quality / availability shares (denominator = `n_positions`)

**Test-first steps:**
- [ ] `test_share_positions_with_notional_available_uses_n_positions`: 4 positions, 3 with notional available → `0.75`.
- [ ] `test_share_positions_with_stable_identity`: positions whose `identity_quality` ∈ `{stable_event_uid, stable_transaction}` → numerator.
- [ ] `test_share_positions_with_ambiguous_same_second`: numerator = positions with identity `ambiguous_same_second`.
- [ ] `test_share_positions_with_no_usable_buy_path`: numerator = positions with `NO_USABLE_BUY_PATH` caveat.
- [ ] `test_observation_window_coverage`: `count(quality in {good, partial}) / n_positions`; `None` when `n_positions==0`.

**Implementation steps:**
- Per §3.2.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "share_positions_with or observation_window_coverage" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building quality/availability shares
```

---

### Task O4: Medians and decision rules

**Test-first steps:**
- [ ] `test_medians_exclude_none`: positions with `time_to_50pct_size_ms` of `[100, None, 300, None, 500]` → median `300`.
- [ ] `test_dominant_accumulation_pattern_most_frequent_among_reconstructed`: 3 `repeated_buy`, 2 `front_loaded` → `repeated_buy`.
- [ ] `test_dominant_accumulation_pattern_tiebreak_earlier_in_list_wins`: 2 `repeated_buy`, 2 `single_fill` → tie. Precedence list order is `unavailable, fragmented_or_ambiguous, single_fill, front_loaded, gradual_accumulation, small_buy_accumulation, repeated_buy`. Earlier-in-list wins → result is `single_fill`. Reason: the precedence list is already ordered from most structurally specific / conservative to broad fallback. Earlier-in-list wins avoids selecting `repeated_buy` over a more specific tied pattern.
- [ ] `test_dominant_accumulation_pattern_tiebreak_three_way`: 1 `front_loaded`, 1 `gradual_accumulation`, 1 `repeated_buy` → tie. Earlier-in-list wins → `front_loaded`.
- [ ] `test_dominant_limitation_most_frequent_caveat`: most frequent caveat across positions and `top_level_caveats` wins; ties lex.
- [ ] `test_high_low_confidence_position_counts`: `high = count(quality=="good")`; `low = count(quality in {"poor", "unavailable"})`.
- [ ] `test_dominant_accumulation_pattern_none_when_no_reconstructed`: `n_reconstructed_positions==0` → `None`.
- [ ] `test_dominant_limitation_none_when_no_caveats`.

**Implementation steps:**
- Per §3.2 decision rules.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "medians or dominant_ or confidence_position_count" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building medians and decision rules
```

---

## Group P — Top-level distributions and caveat aggregation

### Task P1: `source_quality_distribution` and `identity_quality_distribution`

**Test-first steps:**
- [ ] `test_source_quality_distribution_count_and_share`: distribution has `{label: {n, share}}` shape per §3.
- [ ] `test_identity_quality_distribution_count_and_share`: same shape, identity labels.
- [ ] `test_observation_window_quality_distribution_in_wallet_summary`: same shape under wallet_summary.

**Implementation steps:**
- Aggregate per-position labels into top-level distributions.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k distribution -q
```

**Commit message:**
```
feat(wallet-protocol): position_building distributions
```

---

### Task P2: `top_level_caveats` aggregation and `row_quality_summary`

**Test-first steps:**
- [ ] `test_top_level_caveats_aggregates_unique_caveats`: emit each unique caveat once at top level (sorted).
- [ ] `test_row_quality_summary_aggregates_position_counters`: top-level `row_quality_summary` is the sum of per-position `row_quality` counters.
- [ ] `test_generated_from_block_carries_inputs`: `generated_from.activity_rows`, `closed_positions`, `activity_cap_hit`, `subgraph_fallback_used`, `walk_full_history` round-trip from inputs.

**Implementation steps:**
- Per §3 top-level shape.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "top_level_caveats or row_quality_summary or generated_from" -q
```

**Commit message:**
```
feat(wallet-protocol): position_building top-level aggregation
```

---

## Group Q — Forbidden output keys and surface isolation

### Task Q1: Forbidden output keys (spec test 15)

**Test-first steps:**
- [ ] **Spec test 15** — `test_no_forbidden_output_keys`: build a non-trivial surface; recursively walk all dicts; assert none of the following keys appear anywhere: `recommended_trigger`, `trigger_threshold`, `copy_after_first_buy`, `copy_after_n_buys`, `expected_copy_pnl`, `simulated_copy_result`, `copy_strategy`, `copy_signal`. Test asserts the absence with key-by-key messages.

**Implementation steps:**
- The forbidden list is enforced solely by not emitting those keys in implementation. The test is a guard.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py::test_no_forbidden_output_keys -q
```

**Commit message:**
```
test(wallet-protocol): lock forbidden simulator/trigger output keys
```

---

### Task Q2: Surface isolation — no Timing/Exit/verdict mutation (spec test 14)

**Test-first steps:**
- [ ] **Spec test 14** — `test_w2_does_not_read_or_mutate_other_surfaces`: implementation-friendly approach: import the module text and assert it does not import `timing`, `exit_attribution`, `held_analyst`, `flipper`, `flip_reconstruction`, `stage1_gates`, `mode_router`, `tag_inference`, `independence`, `analyze_wallet`, `report`, `types.ProtocolReport`, or anything that would cause cross-surface reads/mutations. Use `ast.parse(open(module).read())` and walk import statements.
- [ ] `test_w2_does_not_import_db_or_ledger`: same AST-walk approach asserting no `db`, `db_*`, `sniper.analysis.wallet_protocol.ledger`, `sniper.analysis.wallet_protocol.persistence`, no `requests`, `httpx`, `urllib`, `aiohttp` imports.

**Implementation steps:**
- The module imports are restricted by hand. The test enforces it.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -k "isolation or does_not_import" -q
```

**Commit message:**
```
test(wallet-protocol): lock surface isolation and forbidden imports
```

---

## Group R — No-HTTP regression (spec test 22)

### Task R1: Monkeypatched `requests.Session.get/post` records zero invocations

**Test-first steps:**
- [ ] **Spec test 22** — `test_no_http_calls_during_compute`: monkeypatch `requests.Session.get` and `requests.Session.post` with sentinels that increment a counter; call `compute_position_building_surface(...)` over a non-trivial fixture; assert both counters are `0`. Also monkeypatch `requests.get`, `requests.post` for the bare-function entry points.

**Implementation steps:**
- No code change needed beyond keeping the module HTTP-free. Test is a guard.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py::test_no_http_calls_during_compute -q
```

**Commit message:**
```
test(wallet-protocol): lock no-HTTP-call regression
```

---

## Group S — Fixture-replay smoke (spec test 24)

### Task S1: Calibration fixture replay

**Test-first steps:**
- [ ] **Spec test 24** — `test_fixture_replay_smoke_swissmiss_or_yatsen`: implementation must search existing fixture paths first — `tests/calibration/`, `tests/fixtures/`, `tests/data/`, plus any path already used by wallet-protocol replay or calibration tests (e.g., wherever `test_wallet_protocol_completeness_surface.py` and the timing/exit-attribution suites read their fixtures). Prefer SwissMiss `0xdbade4c8...9c95` (central to the R9.5 reanalysis thread); fall back to YatSen `0x5bffcf56...ffbe` if SwissMiss is unavailable. If neither has a replayable fixture providing BOTH `activity_rows` AND `closed_positions` locally, mark the test `pytest.skip("No replayable SwissMiss/YatSen fixture with activity_rows and closed_positions available locally")`. Do NOT call live APIs. Do NOT capture a new fixture. Do NOT fabricate one. When a fixture is found: load `activity_rows` + `closed_positions`; run W2; assert `n_positions > 0`, every position's `accumulation_pattern` ∈ known enum (`{"unavailable", "fragmented_or_ambiguous", "single_fill", "front_loaded", "gradual_accumulation", "small_buy_accumulation", "repeated_buy"}`), no forbidden output keys appear, `surface_version == "position_building_v1"`.

**Implementation steps:**
- None — this is a pure-test guard over an existing fixture.

**Verification:**
```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py::test_fixture_replay_smoke_swissmiss_or_yatsen -q
```

**Commit message:**
```
test(wallet-protocol): fixture-replay smoke for SwissMiss/YatSen
```

---

## Final-pass verification (after all tasks land)

Run the full new-test file plus repo-wide sanity:

```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -q
python3 -m pytest tests/ -q --ignore=tests/integration --ignore=tests/contract -x   # (optional sanity; only the new file is required)
python3 scripts/validate_schema_refs.py
git status --short
```

Expectations:
- All Phase-1 tests pass.
- Schema validator clean.
- Working tree clean except the two new files.
- `ProtocolReport` unchanged.
- No `analyze_wallet.py` change.

---

## Spec-test → plan-task coverage table

| Spec test (§4.2) | Description | Plan task |
|---:|---|---|
| 1 | Single BUY reconstructs one position | F1 |
| 2 | Repeated BUYs produce cumulative size + average entry path | F2, G1 |
| 3 | Milestones use peak_observed_size, not final | F3 |
| 4 | Same-second distinct event_uids stay separate | C3 |
| 5 | Same-second no discriminator → ambiguous_same_second | C3 |
| 6 | (condition_id, asset) wins over outcome | C4 |
| 7 | (condition_id, outcome) fallback marks label_fallback | C4 |
| 8 | Missing side/price/size/timestamp counters | B1, B2 |
| 9 | Provided notional mismatch → SIZE_NOTIONAL_MISMATCH | B3 |
| 10 | SELL exceeds observed size + LIKELY_STARTED_MID_POSITION | I1 |
| 11 | Partial sell ladder, no last-sell collapse | H1 |
| 12 | Reaccumulation with 0.10 * peak material delta | J1 |
| 13 | Closed-position-only → NO_USABLE_BUY_PATH | L1 |
| 14 | W2 does not read or mutate other surfaces | Q2 |
| 15 | No forbidden simulator output keys | Q1 |
| 16 | Front-loaded small buys | K2 |
| 17 | Gradual small buys | K3 |
| 18 | Small-buy fallback | K4 |
| 19 | Dust ignored for pattern classification | K1 |
| 20 | Observation quality max downgrade (cap-hit) | D2, N1 |
| 21 | Wallet parameter round-trip | C5 |
| 22 | No-HTTP regression | R1 |
| 23 | Phase-1 boundary lock — no ProtocolReport field | A2 |
| 24 | Fixture-replay smoke (SwissMiss / YatSen) | S1 |
| **25 (added)** | **`source_bounded_ordinal` caps at `partial`** | **N2** |

All 25 tests are mapped.

---

## Spec gap resolutions (operator-locked, 2026-05-10)

These eight points were flagged during initial plan review (commit `68fa10b`). Operator answered each on 2026-05-10 (this commit). Implementer must follow the locked answers below; no further guessing allowed. The answers are also folded into the affected task descriptions above.

1. **D2 — top-level caveat tag for `activity_cap_hit`.** Use `ACTIVITY_CAP_HIT` at the top level when `activity_cap_hit=True and not (subgraph_fallback_used or walk_full_history)`. Use `ACTIVITY_TRUNCATED_POSITION_PATH` (already in spec §2.2) at the position level when the path itself is affected. The two are distinct: `ACTIVITY_CAP_HIT` = top-level / wallet-level input-capture warning; `ACTIVITY_TRUNCATED_POSITION_PATH` = per-position reconstruction caveat.

2. **I1 — `LIKELY_STARTED_MID_POSITION` heuristic.** Emit `SELL_EXCEEDS_OBSERVED_SIZE` whenever a SELL would make observed size negative. Emit `LIKELY_STARTED_MID_POSITION` only when (a) `SELL_EXCEEDS_OBSERVED_SIZE` was emitted AND (b) there were no prior usable BUYs for that position in the observed fill stream. If prior BUYs existed but a later SELL still exceeds observed size, emit `SELL_EXCEEDS_OBSERVED_SIZE` only — do NOT auto-emit `LIKELY_STARTED_MID_POSITION` (overshoot after prior BUYs may also be partial-capture / missing-fills / malformed-sizes / source-gap). SELL-without-prior-BUY is the cleanest mid-position-start signal.

3. **J1 — reaccumulation cumulative vs single-event.** Use **cumulative** material movement, not single-event-only. After reaching positive observed size, one or more SELLs cumulatively reduce observed size by ≥ `material_delta`, then one or more later BUYs after the sell-down cumulatively increase observed size by ≥ `material_delta`. Ignore dust changes (per spec §4 dust rule) for this calculation; keep them in the raw `cumulative_size_path`. `material_delta = max(1e-9 tokens, 0.10 * peak_observed_size)`. Reason: wallets often sell down or re-accumulate through multiple small fills — single-event-only detection would miss exactly the behavior W2 is supposed to reveal.

4. **L2 — context-only asset-keyed position `identity_quality`.** `identity_quality="unavailable"` for context-only positions even when the key is asset-backed. Reason: the asset key is stable enough to identify the position, but there is no fill-level identity because no usable fill path exists. Set: `position_key.type="asset"`, `identity_quality="unavailable"`, `source_quality="unavailable"` or `"activity_rows_partial"` depending on global input state, `caveats` includes `NO_USABLE_BUY_PATH`. Do NOT emit `LABEL_FALLBACK_IDENTITY` — that caveat is only for the outcome-fallback path when `asset` is absent.

5. **O1 — `n_unavailable_positions` definition.** `n_unavailable_positions = count(positions whose observation_window_quality == "unavailable")`. Includes (a) context-only positions, (b) positions with no usable BUY path, (c) positions where rows exist but are unusable due to missing required fields. `n_context_only_positions = count(positions originating from closed_positions but not reconstructed from activity rows)` — a subset of `n_unavailable_positions`. Context-only positions are counted in BOTH counters (NOT mutually exclusive). Reason: `n_unavailable_positions` is the user-facing reconstruction-availability count; `n_context_only_positions` explains why some unavailable positions exist.

6. **O2 — `share_sell_ladder_positions` numerator.** `share_sell_ladder_positions = count(reconstructed positions with n_sell_fills >= 2) / n_reconstructed_positions`. A single SELL is an exit, not a ladder. Always preserve `n_sell_fills` in every position object so future consumers can distinguish 0 / 1 / ≥2.

7. **O4 — `dominant_accumulation_pattern` tiebreak direction.** Earlier-in-list wins. Sort by descending count, then by earliest position in the precedence list (`unavailable, fragmented_or_ambiguous, single_fill, front_loaded, gradual_accumulation, small_buy_accumulation, repeated_buy`). Reason: the precedence list is already ordered from most structurally specific / conservative to broad fallback. Earlier-in-list wins avoids selecting `repeated_buy` over a more specific tied pattern.

8. **S1 — fixture path for SwissMiss / YatSen replay smoke.** Implementation must search existing fixture paths first (`tests/calibration/`, `tests/fixtures/`, `tests/data/`, plus any path already used by wallet-protocol replay/calibration tests). Prefer SwissMiss (central to R9.5 reanalysis); fall back to YatSen. If neither has a replayable fixture providing BOTH `activity_rows` AND `closed_positions` locally, mark the test `pytest.skip("No replayable SwissMiss/YatSen fixture with activity_rows and closed_positions available locally")`. Do NOT call live APIs. Do NOT capture a new fixture. Do NOT fabricate one.

All eight gaps are locked. Implementer proceeds without further operator confirmation on these points.

---

## Out-of-scope reminders (for the implementer at PR time)

When opening the implementation PR, the description must explicitly confirm:

- No `ProtocolReport.position_building_surface` field added.
- No `analyze_wallet.py` modifications.
- No `report.py` modifications.
- No DB-backed ledger adapter.
- No `sniper.analysis.wallet_protocol.ledger` imports inside the core.
- No PR #6 ledger materialization changes.
- No simulator logic, no trigger fields, no copy fields.
- No price-history calls.
- No live API calls.
- No analyzer batches run.
- No DB writes.
- No service restarts.
- Verdict / gate / `primary_edge_type` / `copyability_note` byte-for-byte unchanged.
- No legacy low-buy stream dependency introduced.

If any check above cannot be confirmed, the PR must be parked and surfaced to operator before merge.

# Copy-Latency Decay — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add the copy-latency capture grid + the display-only `copy_latency_surface` — measures how much of a wallet's entry edge survives to a copier entering 5/15/60 min late.

**Architecture:** A shared pure helper `copy_latency_grid_pairs` derives post-entry `(asset, ts)` pairs; the capture unions them into `price_history_lookups`; the pure surface reads spot-at-entry vs spot-at-entry+h. Surface mirrors the four shipped display-only surfaces (`capital_risk`, `sizing_outcome`, `null_baseline`, `survivorship`).

**Tech Stack:** Python, pytest. No new deps. The surface is pure; the capture change reuses the existing point-lookup path.

**Spec (authoritative detail):** `docs/superpowers/specs/2026-05-22-copy-latency-surface-design.md` — source of truth for field names, horizons, thresholds, the metric, eligibility.

**Discipline:** TDD — red suite first, then green. Driven inline. Commit per task. **No live capture is run** — all tests use synthetic `price_history_lookups`; the feature ships dormant (spec §2).

---

## File structure

- Create: `sniper/analysis/wallet_protocol/copy_latency.py` — owns `copy_latency_grid_pairs` (capture helper) + `compute_copy_latency_surface` (the surface). One responsibility: copy-latency grid + decay.
- Create: `tests/test_wallet_protocol_copy_latency_surface.py` — surface acceptance suite (spec §10, tests 1-14).
- Create: `tests/test_copy_latency_grid_pairs.py` — capture-helper test (spec §10, test 15).
- Modify: `sniper/analysis/wallet_protocol/types.py` — `ProtocolReport.copy_latency_surface` field + hard-wall comment.
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py` — one producer call.
- Modify: `sniper/analysis/wallet_protocol/report.py` — `_copy_latency_block` + append.
- Modify: `scripts/record_fetch_snapshot.py` — union the grid pairs into the price-history lookup set; `--no-copy-latency-grid` flag; `replay_surface_meta` entry.
- Modify: `tests/test_wallet_protocol_honesty_floor.py`, `tests/test_wallet_protocol_tag_grain_rescue_floor.py` — scope-guard field sets.

---

## Task 1: Failing test suites

**Files:** Create `tests/test_wallet_protocol_copy_latency_surface.py` + `tests/test_copy_latency_grid_pairs.py`

- [ ] **Step 1:** Write all 15 tests per spec §10 — builders (`_pos` closed position with `conditionId`/`asset`/`avgPrice`/`curPrice`; `_buy` activity BUY row; `price_history_lookups` dict keyed `"<asset>|<ts>"`; `_data` WalletData). Surface tests import `compute_copy_latency_surface`; the grid test imports `copy_latency_grid_pairs`. Mirror `tests/test_wallet_protocol_null_baseline_surface.py` structure.
- [ ] **Step 2: Run — verify RED** (`ModuleNotFoundError: copy_latency`). Run: `python3 -m pytest tests/test_wallet_protocol_copy_latency_surface.py tests/test_copy_latency_grid_pairs.py -q`
- [ ] **Step 3: Commit** — `test(copy-latency): failing acceptance suites`

## Task 2: Implement the module

**Files:** Create `sniper/analysis/wallet_protocol/copy_latency.py`

- [ ] **Step 1:** Implement `copy_latency_grid_pairs(closed_positions, activity_rows) -> set[tuple[str,int]]` per spec §4 (first-BUY entry, emit entry + entry+h*60 per horizon) and `compute_copy_latency_surface(closed_positions, activity_rows, price_history_lookups) -> dict` per spec §5-§7 (eligibility, per-horizon drift/decay, aggregation over matched sets, `copyability` label, `data_state` worst-wins). Module hard-wall docstring. Constants: `HORIZONS_MIN=(5,15,60)`, `PRIMARY_HORIZON=15`, `MIN_ELIGIBLE=20`, `MIN_PER_HORIZON=10`, `COVERAGE_FLOOR=0.50`, resolution/band constants.
- [ ] **Step 2: Run — verify GREEN** (pure-function + grid tests pass). Run: `python3 -m pytest tests/test_wallet_protocol_copy_latency_surface.py tests/test_copy_latency_grid_pairs.py -q`
- [ ] **Step 3: Commit** — `feat(copy-latency): grid helper + surface producer`

## Task 3: Wire ProtocolReport + analyze_wallet

**Files:** Modify `types.py` (field near `survivorship_surface`, hard-wall comment), `analyze_wallet.py` (producer call beside the `survivorship_surface` call, fed `data.closed_positions`, `data.activity_rows`, `data.price_history_lookups`; function-local import; comment states the hard wall is the guarantee).

- [ ] **Step 1:** Add `copy_latency_surface: dict | None = None` to `ProtocolReport` + comment. Add the `compute_copy_latency_surface` call in `analyze_wallet`.
- [ ] **Step 2: Run** — integration tests (field-present, hard-wall, determinism, backward-compat) pass. Run: `python3 -m pytest tests/test_wallet_protocol_copy_latency_surface.py -q`
- [ ] **Step 3: Commit** — `feat(copy-latency): wire surface into ProtocolReport + analyze_wallet`

## Task 4: Render block

**Files:** Modify `report.py` — `_copy_latency_block(report)` (after `_survivorship_block`) + append.

- [ ] **Step 1:** Add `## Copy-latency decay` markdown block — data_state, copyability, primary horizon, per-horizon rows (horizon_min, n, mean_drift, entry/copier ew_roi, mean_decay, edge_retained), caveats, the display-only + dormant-until-capture Note (spec §9).
- [ ] **Step 2: Run** — `python3 -m pytest tests/test_wallet_protocol_copy_latency_surface.py tests/test_wallet_protocol_explanatory_render.py -q`
- [ ] **Step 3: Commit** — `feat(copy-latency): markdown render block`

## Task 5: Capture-layer grid

**Files:** Modify `scripts/record_fetch_snapshot.py` — import `copy_latency_grid_pairs`; add `--no-copy-latency-grid` flag; when enabled, union the grid pairs into the `(asset, ts)` set passed to `_capture_price_history_lookups`; add a `replay_surface_meta["copy_latency_grid"]` entry (status/source/pair-count).

- [ ] **Step 1:** Implement the capture wiring per spec §4. The grid pairs join the existing price-history lookup set so they resolve through the same `price_fn` into `price_history_lookups`.
- [ ] **Step 2: Run** — `python3 -m pytest tests/test_record_fetch_snapshot_v2.py tests/test_copy_latency_grid_pairs.py -q` (no live capture — the helper is unit-tested; the capture wiring is exercised by the existing snapshot tests).
- [ ] **Step 3: Commit** — `feat(copy-latency): capture-layer post-entry grid`

## Task 6: Scope-guard field-set updates

**Files:** Modify `tests/test_wallet_protocol_honesty_floor.py`, `tests/test_wallet_protocol_tag_grain_rescue_floor.py` — add `"copy_latency_surface"` next to `"survivorship_surface"`.

- [ ] **Step 1:** Add the field to both expected sets.
- [ ] **Step 2: Run** — `python3 -m pytest tests/test_wallet_protocol_honesty_floor.py tests/test_wallet_protocol_tag_grain_rescue_floor.py -q`
- [ ] **Step 3: Commit** — `test(copy-latency): scope-guard field-set updates`

## Task 7: Full regression + adversarial review

- [ ] **Step 1:** `python3 -m pytest tests/test_wallet_protocol_*.py tests/test_record_fetch_snapshot_v2.py tests/test_copy_latency_grid_pairs.py tests/calibration/ -q` — expect all green.
- [ ] **Step 2:** `python3 scripts/validate_schema_refs.py` + `python3 scripts/validate_knowledge_sync.py` — expect clean / all_ok.
- [ ] **Step 3:** Adversarial review (platform-reviewer) — hard wall; the drift/decay/edge-retained math; eligibility + matched-set apples-to-apples; the capture-helper key derivation matching the surface's; `data_state` honesty incl. the dormant case; division guards; determinism. Fix findings with regression tests.
- [ ] **Step 4:** Merge `feat/copy-latency-surface` → main (`--no-ff`), KSG attestation in the merge commit. Note in the report that the feature ships dormant — activation needs an operator-run grid-enabled capture.

---

## Self-review

**Spec coverage:** §3 horizons → Task 2. §4 capture → Task 2 (helper) + Task 5 (wiring). §5 metric → Task 2. §6 schema → Task 2/3. §7 data_state → Task 2. §8 hard wall → Task 2/3 + Task 1 (mutation-proof test). §9 wiring → Task 3/4/5. §10 tests (all 15) → Task 1 + Task 6. No gaps.

**Placeholder scan:** Tasks reference the committed spec for detailed content — precise pointers, not placeholders. Constants + paths explicit.

**Type consistency:** `copy_latency_grid_pairs` and `compute_copy_latency_surface` signatures + `copy_latency_surface` field name consistent across Tasks 1-6; mirror the verified sibling-surface shapes.

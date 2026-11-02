# Null-Strategy Baseline Surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add the display-only `null_baseline_surface` to `ProtocolReport` — compares a wallet's hold-to-resolution side-selection return against always-favorite / always-underdog / random null strategies on the markets it traded.

**Architecture:** Pure producer (`compute_null_baseline_surface`) over `closed_positions` only; ProtocolReport field; `analyze_wallet` call; `report.py` render block. Mirrors the shipped `capital_risk_surface` (`1eda504`) and `sizing_outcome_surface` (`5062316`) exactly.

**Tech Stack:** Python, pytest. No new deps — pure price/resolution arithmetic, no randomness, no sort.

**Spec (authoritative detail):** `docs/superpowers/specs/2026-05-22-null-baseline-surface-design.md`. Each task points to the spec section that fully specifies its content; the spec is committed and is the source of truth for field names, thresholds, eligibility, and the ROI algorithm.

**Discipline:** TDD — red suite first, then green. Driven inline (no isolated worktree — per the 90ca3d9 worktree-isolation lesson). Commit per task.

---

## File structure

- Create: `sniper/analysis/wallet_protocol/null_baseline.py` — the producer. One responsibility: build the surface dict from `closed_positions`.
- Create: `tests/test_wallet_protocol_null_baseline_surface.py` — acceptance suite (spec §9, 16 tests).
- Modify: `sniper/analysis/wallet_protocol/types.py` — `ProtocolReport.null_baseline_surface` field + hard-wall comment.
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py` — one producer call beside the `sizing_outcome_surface` call.
- Modify: `sniper/analysis/wallet_protocol/report.py` — `_null_baseline_block` + its append.
- Modify: `tests/test_wallet_protocol_honesty_floor.py`, `tests/test_wallet_protocol_tag_grain_rescue_floor.py` — add `null_baseline_surface` to the scope-guard field sets.

---

## Task 1: Failing acceptance suite

**Files:** Create `tests/test_wallet_protocol_null_baseline_surface.py`

- [ ] **Step 1: Write the 16 acceptance tests** per spec §9 — builders (`_pos` for a closed position with `conditionId`/`avgPrice`/`curPrice`/optional `negRisk`; `_data` with `closed_positions`), pure-function tests for the producer, plus integration (`analyze_wallet`) for field-present / hard-wall / determinism / backward-compat. Mirror `tests/test_wallet_protocol_sizing_outcome_surface.py` structure. Import `from sniper.analysis.wallet_protocol.null_baseline import compute_null_baseline_surface`.
- [ ] **Step 2: Run — verify RED** (`ModuleNotFoundError: null_baseline`). Run: `python3 -m pytest tests/test_wallet_protocol_null_baseline_surface.py -q`
- [ ] **Step 3: Commit** — `test(null-baseline): failing acceptance suite`

## Task 2: Implement the producer

**Files:** Create `sniper/analysis/wallet_protocol/null_baseline.py`

- [ ] **Step 1:** Implement `compute_null_baseline_surface(closed_positions) -> dict` per spec §4–§6: eligibility (§5.1 — fields-present, resolved, price band, negRisk exclusion); per-market ROI (§5.2 — `roi(p, won) = (1-p)/p if won else -1`, favorite/underdog/wallet/random); aggregation (§5.3); `comparison` label (§5.4 — `inconclusive` whenever `data_state != ok`); `data_state` worst-wins with explicit severity order (§6). Module-level hard-wall docstring. Constants per spec §4: `RESOLVED_WIN=0.97`, `RESOLVED_LOSS=0.03`, `PRICE_BAND=(0.02, 0.98)`, `MIN_ELIGIBLE=20`, `COVERAGE_FLOOR=0.50`, `FAVORITE_DOMINANCE=0.90`, `EDGE_BAND=0.03`.
- [ ] **Step 2: Run — verify GREEN** (pure-function tests pass). Run: `python3 -m pytest tests/test_wallet_protocol_null_baseline_surface.py -q`
- [ ] **Step 3: Commit** — `feat(null-baseline): producer greens the pure-function tests`

## Task 3: Wire ProtocolReport + analyze_wallet

**Files:** Modify `types.py` (field near `sizing_outcome_surface`, hard-wall comment), `analyze_wallet.py` (producer call beside the `sizing_outcome_surface` call, fed `data.closed_positions`; function-local import; comment states the hard wall is the guarantee, not call ordering).

- [ ] **Step 1:** Add `null_baseline_surface: dict | None = None` to `ProtocolReport` + comment. Add the `compute_null_baseline_surface` call in `analyze_wallet`.
- [ ] **Step 2: Run** — integration tests (field-present, hard-wall, determinism, backward-compat) pass. Run: `python3 -m pytest tests/test_wallet_protocol_null_baseline_surface.py -q`
- [ ] **Step 3: Commit** — `feat(null-baseline): wire surface into ProtocolReport + analyze_wallet`

## Task 4: Render block

**Files:** Modify `report.py` — `_null_baseline_block(report)` (after `_sizing_outcome_block`) + its append in the report builder.

- [ ] **Step 1:** Add `## Null-strategy baseline` markdown block — data_state, comparison, wallet/favorite/underdog/random ew_roi, edge_vs_favorite, favorite_hit_rate, n_on_favorite/n_on_underdog, coverage, caveats, and the display-only + binary-assumption Note (spec §8).
- [ ] **Step 2: Run** — `python3 -m pytest tests/test_wallet_protocol_null_baseline_surface.py tests/test_wallet_protocol_explanatory_render.py -q`
- [ ] **Step 3: Commit** — `feat(null-baseline): markdown render block`

## Task 5: Scope-guard field-set updates

**Files:** Modify `tests/test_wallet_protocol_honesty_floor.py`, `tests/test_wallet_protocol_tag_grain_rescue_floor.py` — add `"null_baseline_surface"` to the expected `ProtocolReport` field sets (next to `"sizing_outcome_surface"`).

- [ ] **Step 1:** Add the field to both expected sets.
- [ ] **Step 2: Run** — `python3 -m pytest tests/test_wallet_protocol_honesty_floor.py tests/test_wallet_protocol_tag_grain_rescue_floor.py -q`
- [ ] **Step 3: Commit** — `test(null-baseline): scope-guard field-set updates`

## Task 6: Full regression + adversarial review

- [ ] **Step 1:** `python3 -m pytest tests/test_wallet_protocol_*.py tests/calibration/ -q` — expect all green.
- [ ] **Step 2:** `python3 scripts/validate_schema_refs.py` + `python3 scripts/validate_knowledge_sync.py` — expect clean / all_ok.
- [ ] **Step 3:** Adversarial review (platform-reviewer) — hard wall; ROI / favorite-derivation correctness; eligibility (missing fields, negRisk, band edges); `comparison`/`data_state` honesty; division guards; determinism. Fix findings with regression tests.
- [ ] **Step 4:** Merge `feat/null-baseline-surface` → main (`--no-ff`), KSG attestation in the merge commit.

---

## Self-review

**Spec coverage:** §3 schema → Task 2/3. §4 constants → Task 2. §5 eligibility/ROI/aggregation/label → Task 2. §6 data_state → Task 2. §7 hard wall → Task 2/3 (comment) + Task 1 (mutation-proof test). §8 wiring → Task 3/4. §9 tests (all 16) → Task 1 + Task 5 (scope-guard). No gaps.

**Placeholder scan:** Tasks reference committed spec sections for detailed content — precise in-repo pointers, not placeholders. Constants and file paths explicit.

**Type consistency:** `compute_null_baseline_surface` signature and `null_baseline_surface` field name consistent across Tasks 1–5; mirror the verified `capital_risk_surface` / `sizing_outcome_surface` shapes.

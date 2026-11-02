# Sizing & Outcome Surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add the display-only `sizing_outcome_surface` to `ProtocolReport` — measures whether a wallet's entry sizing is associated with outcome quality, controlling the market-mix and one-big-winner confounds.

**Architecture:** Pure producer (`compute_sizing_outcome_surface`) over already-fetched data; ProtocolReport field; `analyze_wallet` call after verdict/gate finalize; `report.py` render block. Mirrors the shipped `capital_risk_surface` exactly (`1eda504`).

**Tech Stack:** Python, pytest. No new deps — Spearman ρ + permutation p-value implemented inline (fixed seed).

**Spec (authoritative detail):** `docs/superpowers/specs/2026-05-22-sizing-outcome-surface-design.md`. Each task below points to the spec section that fully specifies its content; the spec is committed and is the source of truth for field names, thresholds, and algorithm.

**Discipline:** TDD — red suite first, then green. Driven inline (no isolated worktree — per the 90ca3d9 worktree-isolation lesson). Commit per task.

---

## File structure

- Create: `sniper/analysis/wallet_protocol/sizing_outcome.py` — the producer. One responsibility: build the surface dict from closed positions + activity + tag lookups.
- Create: `tests/test_wallet_protocol_sizing_outcome_surface.py` — acceptance suite (spec §9, 12 tests).
- Modify: `sniper/analysis/wallet_protocol/types.py` — `ProtocolReport.sizing_outcome_surface` field + hard-wall comment.
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py` — one producer call after verdict/gate.
- Modify: `sniper/analysis/wallet_protocol/report.py` — `_sizing_outcome_block` + its append.
- Modify: `tests/test_wallet_protocol_honesty_floor.py`, `tests/test_wallet_protocol_tag_grain_rescue_floor.py` — add `sizing_outcome_surface` to the scope-guard field sets.

---

## Task 1: Failing acceptance suite

**Files:** Create `tests/test_wallet_protocol_sizing_outcome_surface.py`

- [ ] **Step 1: Write the 12 acceptance tests** per spec §9 — builders (`_trade`, `_pos`, `_ledger`-free; `_data` with `closed_positions`/`activity_rows`/`leafs_by_cid`/`nav_lookup`), pure-function tests for the producer, plus integration (`analyze_wallet`) for field-present / hard-wall / determinism / backward-compat. Mirror `tests/test_wallet_protocol_capital_risk_surface.py` structure. Tests import `from sniper.analysis.wallet_protocol.sizing_outcome import compute_sizing_outcome_surface`.
- [ ] **Step 2: Run — verify RED** (`ModuleNotFoundError: sizing_outcome`). Run: `python3 -m pytest tests/test_wallet_protocol_sizing_outcome_surface.py -q`
- [ ] **Step 3: Commit** — `test(sizing-outcome): failing acceptance suite`

## Task 2: Implement the producer

**Files:** Create `sniper/analysis/wallet_protocol/sizing_outcome.py`

- [ ] **Step 1:** Implement `compute_sizing_outcome_surface(closed_positions, activity_rows, leafs_by_cid, nav_lookup) -> dict` per spec §4–§6: datapoint construction with the `(cid,asset)`→`(cid,outcome)` join fallback (§4.1), guards (§4.2), deterministic category (§4.3), global + stratified Spearman ρ (§5.1–5.2), leave-one-out (§5.3), `association` label (§5.4), buckets + late conviction (§5.5), `data_state` worst-wins with explicit severity order (§6). Module-level hard-wall docstring. Constants: `_PERM_ITERS=2000`, `_PERM_SEED=1`, `MIN_STRATUM_N=8`, `MIN_DATAPOINTS=20`.
- [ ] **Step 2: Run — verify GREEN** (pure-function tests pass). Run: `python3 -m pytest tests/test_wallet_protocol_sizing_outcome_surface.py -q`
- [ ] **Step 3: Commit** — `feat(sizing-outcome): producer greens the pure-function tests`

## Task 3: Wire ProtocolReport + analyze_wallet

**Files:** Modify `types.py` (field near `capital_risk_surface`, hard-wall comment), `analyze_wallet.py` (producer call beside the `capital_risk_surface` call, after verdict/gate, fed `data.closed_positions`, `data.activity_rows`, resolved `leafs_by_cid`, `nav_lookup`).

- [ ] **Step 1:** Add `sizing_outcome_surface: dict | None = None` to `ProtocolReport` + comment. Add the `compute_sizing_outcome_surface` call in `analyze_wallet` (function-local import, same pattern as `capital_risk_surface`).
- [ ] **Step 2: Run** — integration tests (field-present, hard-wall, determinism, backward-compat) pass. Run: `python3 -m pytest tests/test_wallet_protocol_sizing_outcome_surface.py -q`
- [ ] **Step 3: Commit** — `feat(sizing-outcome): wire surface into ProtocolReport + analyze_wallet`

## Task 4: Render block

**Files:** Modify `report.py` — `_sizing_outcome_block(report)` (after `_capital_risk_block`) + its append in the report builder.

- [ ] **Step 1:** Add `## Sizing & outcome` markdown block — data_state, association, global/stratified ρ, leave-one-out + robustness, size buckets, by-category, late_conviction_ratio, caveats, the display-only note. Append it after the capital-risk block.
- [ ] **Step 2: Run** — `python3 -m pytest tests/test_wallet_protocol_sizing_outcome_surface.py tests/test_wallet_protocol_explanatory_render.py -q`
- [ ] **Step 3: Commit** — `feat(sizing-outcome): markdown render block`

## Task 5: Scope-guard field-set updates

**Files:** Modify `tests/test_wallet_protocol_honesty_floor.py`, `tests/test_wallet_protocol_tag_grain_rescue_floor.py` — add `"sizing_outcome_surface"` to the expected `ProtocolReport` field sets (next to `"capital_risk_surface"`).

- [ ] **Step 1:** Add the field to both expected sets.
- [ ] **Step 2: Run** — `python3 -m pytest tests/test_wallet_protocol_honesty_floor.py tests/test_wallet_protocol_tag_grain_rescue_floor.py -q`
- [ ] **Step 3: Commit** — `test(sizing-outcome): scope-guard field-set updates`

## Task 6: Full regression + adversarial review

- [ ] **Step 1:** `python3 -m pytest tests/test_wallet_protocol_*.py tests/calibration/ -q` — expect all green.
- [ ] **Step 2:** `python3 scripts/validate_schema_refs.py` + `python3 scripts/validate_knowledge_sync.py` — expect clean / all_ok.
- [ ] **Step 3:** Adversarial review (platform-reviewer) — hard wall, statistic correctness (Spearman ties, permutation determinism, stratified-weighting math, leave-one-out rule), confound-control correctness, degradation honesty, division guards. Fix findings with regression tests.
- [ ] **Step 4:** Merge `feat/sizing-outcome-surface` → main (`--no-ff`), KSG attestation in the merge commit.

---

## Self-review

**Spec coverage:** §3 schema → Task 2/3. §4 datapoints → Task 2. §5 statistic → Task 2. §6 data_state → Task 2. §7 hard wall → Task 2/3 (comment) + Task 1 (mutation-proof test). §8 wiring → Task 3/4. §9 tests (all 12) → Task 1 + Task 5 (scope-guard). No gaps.

**Placeholder scan:** Tasks point to committed spec sections for detailed content — not placeholders; the spec is the authoritative, in-repo reference. Constants and file paths are explicit.

**Type consistency:** `compute_sizing_outcome_surface` signature and `sizing_outcome_surface` field name consistent across Tasks 1–5; mirror the verified `capital_risk_surface` shapes.

# Position Reconciliation Surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add the display-only `position_reconciliation_surface` to `ProtocolReport` — reconciles `/closed-positions` against CTF Redemption events from the activity-subgraph, reports the undercount, and names the observed acquisition rails (orderbook / redemption / split / merge).

**Architecture:** Pure producer (`compute_position_reconciliation_surface`) over `closed_positions + activity_rows + redemptions + splits + merges + activity_cap_hit + n_unmapped_fills`. Three new `WalletData` event-stream fields with strict `None`-vs-`[]` preservation through the replay round trip. New fetch helpers wired into `fetch.py` and `record_fetch_snapshot.py` (helpers themselves already exist at `subgraph_reader.py:206-264`). ProtocolReport field, `analyze_wallet` call, `report.py` render block. Mirrors the shipped `null_baseline_surface` (`docs/superpowers/plans/2026-05-22-null-baseline-surface.md`) and `copy_latency_surface` (`docs/superpowers/plans/2026-05-22-copy-latency-surface.md`) patterns; closest analog is `copy_latency` because both need capture-layer additions.

**Tech Stack:** Python, pytest. No new deps — the subgraph readers are already present.

**Spec (authoritative detail):** `docs/superpowers/specs/2026-05-23-position-reconciliation-surface-design.md` (commit `9d9085b` on `spec/position-reconciliation-surface`). Each task points to the spec section that fully specifies its content; the spec is the source of truth for field names, thresholds, condition-set arithmetic, and the caveat split.

**Branch:** `feat/position-reconciliation-surface` (forked from `spec/position-reconciliation-surface`).

**Discipline:** TDD — red suite first, then green. Execute implementation on
`feat/position-reconciliation-surface` forked from this spec branch; do not
commit implementation work on `main`. Commit per task.

---

## File structure

- Create: `sniper/analysis/wallet_protocol/position_reconciliation.py` — the producer. One responsibility: build the surface dict from the seven declared inputs (spec §5).
- Create: `tests/test_wallet_protocol_position_reconciliation_surface.py` — surface acceptance suite (spec §10, excluding replay-only test 12b, which belongs in an existing replay/fixture test file so Task 2 can run before the producer module exists).
- Modify: `sniper/analysis/wallet_protocol/types.py` — `ProtocolReport.position_reconciliation_surface` field (next to `copy_latency_surface` at `types.py:485`) and three new `WalletData` optional fields placed after `n_unmapped_fills:227` (spec §8.2 — placement matters because `WalletData`'s non-default fields run through `fetched_at:218`).
- Modify: `sniper/analysis/wallet_protocol/replay.py` — serialize/deserialize for `redemptions`/`splits`/`merges` with STRICT literal preservation of `None` (no `or []` coercion in serialize; `.get()` no-default in deserialize). Spec §8.4.
- Modify: `sniper/analysis/wallet_protocol/fetch.py` — three try/except-wrapped fetch calls outside the `activity_cap_hit` branch (spec §8.3).
- Verify: `scripts/record_fetch_snapshot.py` persists the three new fields through `save_fetch_snapshot()` / `replay.py`. Current script calls `save_fetch_snapshot(data, args.out)` at line 548; only modify it if inspection finds a custom serialization path bypassing `replay.serialize_wallet_data`.
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py` — one producer call beside the `copy_latency_surface` call at `analyze_wallet.py:1506`. Read `data.n_unmapped_fills` directly (NOT string-parse `fetch_quality_flags`); spec §9.
- Modify: `sniper/analysis/wallet_protocol/report.py` — `_position_reconciliation_block` + its append. Block renders BEFORE the other quantitative surfaces when `downstream_lower_bound=True` (spec §9).
- Modify: `tests/test_wallet_protocol_honesty_floor.py:562`, `tests/test_wallet_protocol_tag_grain_rescue_floor.py:640` — add `position_reconciliation_surface` to the scope-guard field sets.
- Modify: `tests/calibration/labels.yaml` — bump `calibration_version: 3 → 4`; pin the new surface's `data_state` and `undercount_ratio` per recaptured fixture (Task 8 only).

---

## Task 1: Failing acceptance suite

**Files:** Create `tests/test_wallet_protocol_position_reconciliation_surface.py`

- [ ] **Step 1: Write the surface acceptance tests** per spec §10 — builders (`_closed` for a closed-position row with `conditionId`; `_red`/`_split`/`_merge` for CTF event rows with `condition`; `_act` for an activity_rows TRADE row with `asset`/`conditionId`/`type`; `_data` assembling a `WalletData`). Pure-function tests for the producer (tests 1, 1b, 2, 3, 4, 4b, 5, 6, 7, 8, 9), integration tests (10 mutation-proof, 11 grep guard, 12 determinism, 13 backward-compat, 16 note rendering, 17 paired-run no-mutation-of-other-surfaces). Scope-guard updates (14) live in Task 7; calibration assertion (15) lives in Task 8; replay round-trip test 12b lives in Task 2. Import `from sniper.analysis.wallet_protocol.position_reconciliation import compute_position_reconciliation_surface, UNDERCOUNT_PROXY_ONLY_THRESHOLD, ACQUISITION_GAP_NOTABLE_THRESHOLD`.
- [ ] **Step 2: Run — verify RED** (`ModuleNotFoundError: position_reconciliation`). Run: `python3 -m pytest tests/test_wallet_protocol_position_reconciliation_surface.py -q`
- [ ] **Step 3: Commit** — `test(position-reconciliation): failing acceptance suite`

## Task 2: WalletData event-stream fields + replay round-trip

**Files:** Modify `sniper/analysis/wallet_protocol/types.py`, `sniper/analysis/wallet_protocol/replay.py`, and add replay-focused tests to `tests/test_fixture_v2_schema.py` or `tests/test_replay_determinism.py`. Do NOT put test 12b in `tests/test_wallet_protocol_position_reconciliation_surface.py`: that file imports the producer module, which intentionally does not exist until Task 3.

- [ ] **Step 1:** Add three optional fields to `WalletData` AFTER `n_unmapped_fills:227`, NOT adjacent to `activity_rows` (spec §8.2): `redemptions: list[dict] | None = None`, `splits: list[dict] | None = None`, `merges: list[dict] | None = None`. Include the spec §8.2 comment naming the `None`-vs-`[]` semantics.
- [ ] **Step 2:** Modify `replay.py` serialize (around line 65) — persist the three fields LITERALLY (`"redemptions": data.redemptions`, not `list(data.redemptions or [])`). Modify deserialize (around line 120) — `payload.get("redemptions")` with no default so missing keys → `None`. Spec §8.4 explicitly forbids the `or []` coercion in serialize.
- [ ] **Step 3: Run** — the replay-only None-survives-round-trip test plus backward-compat replay tests pass. Run the exact test added in Step 2, e.g. `python3 -m pytest tests/test_fixture_v2_schema.py tests/test_replay_determinism.py -q -k "redemptions or splits or merges or fixture"`; do not run `tests/test_wallet_protocol_position_reconciliation_surface.py` in this task because it is still expected to fail until Task 3 creates the producer.
- [ ] **Step 4: Commit** — `feat(wallet-protocol): add redemptions/splits/merges to WalletData + replay`

## Task 3: Implement the producer

**Files:** Create `sniper/analysis/wallet_protocol/position_reconciliation.py`

- [ ] **Step 1:** Implement `compute_position_reconciliation_surface(closed_positions, activity_rows, redemptions, splits, merges, activity_cap_hit, n_unmapped_fills) -> dict` per spec §5: build the four condition sets (§5.1, including `orderbook_cond` from `activity_rows` TRADE rows; iteration-local `(redemptions or [])` is the safe-default pattern §5.1 explicitly allows); count derivation (§5.2 including `closed_condition_count` distinct from `closed_positions_count`); rails observed with the `ORDERBOOK_RAIL_INCOMPLETE_n=<n>` caveat when `activity_cap_hit and n_unmapped_fills > 0` (§5.3); `downstream_lower_bound` boolean and `note` rendering (§5.4); `data_state` worst-wins with the split caveats — `RECONCILIATION_INPUTS_MISSING` (fetch failure: `redemptions is None`) vs `NO_CLOSED_OR_REDEEMED_CONDITIONS` (present-but-empty); the `UNTRACED_ACQUISITION_RAIL` caveat at `acquisition_rail_gap_share > ACQUISITION_GAP_NOTABLE_THRESHOLD`. Constants per spec §4: `SURFACE_VERSION = 1`, `UNDERCOUNT_PROXY_ONLY_THRESHOLD = 0.05`, `ACQUISITION_GAP_NOTABLE_THRESHOLD = 0.10`. Module-level hard-wall docstring per spec §7 mirroring `null_baseline.py`'s preamble verbatim shape.
- [ ] **Step 2: Run — verify GREEN on pure-function tests** (tests 1, 1b, 2, 3, 4, 4b, 5, 6, 7, 8, 9, 16). Run: `python3 -m pytest tests/test_wallet_protocol_position_reconciliation_surface.py -q -k "not mutation_proof and not grep_guard and not paired_run"`
- [ ] **Step 3: Commit** — `feat(position-reconciliation): producer greens the pure-function tests`

## Task 4: Wire ProtocolReport + analyze_wallet

**Files:** Modify `sniper/analysis/wallet_protocol/types.py` and `sniper/analysis/wallet_protocol/analyze_wallet.py`.

- [ ] **Step 1:** Add `position_reconciliation_surface: dict | None = None` to `ProtocolReport` near `copy_latency_surface:485` with the standard hard-wall comment.
- [ ] **Step 2:** Insert the producer call in `analyze_wallet` AFTER the `copy_latency_surface` call (around `analyze_wallet.py:1506`), with the same `from sniper.analysis.wallet_protocol.position_reconciliation import compute_position_reconciliation_surface` function-local import pattern that `copy_latency` uses. Pass `data.closed_positions, data.activity_rows, data.redemptions, data.splits, data.merges, data.activity_cap_hit, data.n_unmapped_fills` — read `n_unmapped_fills` from the typed field at `types.py:227`, NOT by string-parsing `data.fetch_quality_flags` (spec §9 explicitly forbids this).
- [ ] **Step 3: Run** — integration tests pass (10 mutation-proof, 11 grep guard, 12 determinism, 13 backward-compat, 17 paired-run no-mutation). Run: `python3 -m pytest tests/test_wallet_protocol_position_reconciliation_surface.py -q`
- [ ] **Step 4: Commit** — `feat(position-reconciliation): wire surface into ProtocolReport + analyze_wallet`

## Task 5: Fetch.py + record_fetch_snapshot.py wiring

**Files:** Modify `sniper/analysis/wallet_protocol/fetch.py`; verify `scripts/record_fetch_snapshot.py` uses `save_fetch_snapshot()`; add fetch-side tests per spec §8.7 to `tests/test_wallet_protocol_fetch_subgraph_fallback.py` (closest existing `fetch_wallet_data` test file).

- [ ] **Step 1: Add fetch tests first (TDD red)** — three tests per spec §8.7: (1) happy-path round trip with mocked subgraph readers returning non-empty lists, (2) failure isolation where ONE of the three readers raises and only that field becomes `None` (the other two remain populated; `fetch_quality_flags` gains a named flag), (3) old-fixture compatibility where deserializing a pre-surface fixture without the three keys yields all three as `None`. Run: `python3 -m pytest tests/test_wallet_protocol_fetch_subgraph_fallback.py -q -k "redemptions or splits or merges"` — expect RED (module attributes/fields don't exist).
- [ ] **Step 2: Wire fetch.py** — add `from sniper.analysis.wallet_protocol.subgraph_reader import fetch_redemptions, fetch_splits, fetch_merges` and three try/except blocks OUTSIDE the `activity_cap_hit` branch (spec §8.3 — they run for every wallet, not just cap-hit ones). Use local variables (`redemptions`, `splits`, `merges`) initialized before the final `return WalletData(...)`; on success assign the returned list to the local, on `Exception` log via `logger.exception`, set only that local to `None`, and append a named `fetch_quality_flags` entry like `REDEMPTIONS_FETCH_FAILED`. Pass the locals into the final `WalletData(...)` constructor. Do not write `data.redemptions = ...` in `fetch_wallet_data`; there is no `data` object until the constructor return.
- [ ] **Step 3: Verify record_fetch_snapshot.py persistence path** — inspect the writer and confirm it persists through `save_fetch_snapshot(data, args.out)` / `replay.serialize_wallet_data`. If so, no direct edit is needed; the replay serializer from Task 2 owns persistence. Only modify `record_fetch_snapshot.py` if it bypasses `save_fetch_snapshot()` with a custom dict writer.
- [ ] **Step 4: Run — verify GREEN on fetch tests + replay round-trip + full surface suite.** Run: `python3 -m pytest tests/test_wallet_protocol_fetch_subgraph_fallback.py tests/test_fixture_v2_schema.py tests/test_wallet_protocol_position_reconciliation_surface.py -q`
- [ ] **Step 5: Commit** — `feat(position-reconciliation): fetch event streams + persist in snapshots`

## Task 6: Render block

**Files:** Modify `sniper/analysis/wallet_protocol/report.py` — `_position_reconciliation_block(report)` + its conditional insertion in the report builder.

- [ ] **Step 1:** Add `## Position reconciliation` markdown block — `data_state`, `closed_positions_count` vs `closed_condition_count` (with the row-vs-condition annotation), `redeemed_condition_count`, `redeemed_not_in_closed_count`, `undercount_ratio`, `redeemed_orderbook_traced_count`, `untraced_redemption_count`, `acquisition_rail_gap_share`, `rails_observed`, `condition_set_summary`, the `note`, and the `caveats` list. When `downstream_lower_bound = True`, the block renders BEFORE the quantitative surfaces (`capital_risk` / `sizing_outcome` / `null_baseline` / `copy_latency`) in the report so the reader sees the caveat first; when `False`, render in its declared position alongside the other display-only surfaces. Spec §9.
- [ ] **Step 2: Run** — test 16 (note rendering) plus the existing explanatory-render test pass. Run: `python3 -m pytest tests/test_wallet_protocol_position_reconciliation_surface.py tests/test_wallet_protocol_explanatory_render.py -q`
- [ ] **Step 3: Commit** — `feat(position-reconciliation): markdown render block`

## Task 7: Scope-guard field-set updates

**Files:** Modify `tests/test_wallet_protocol_honesty_floor.py:562`, `tests/test_wallet_protocol_tag_grain_rescue_floor.py:640` — add `"position_reconciliation_surface"` to the expected `ProtocolReport` field sets (next to `"copy_latency_surface"` in both files).

- [ ] **Step 1:** Add the field to both expected sets.
- [ ] **Step 2: Run** — `python3 -m pytest tests/test_wallet_protocol_honesty_floor.py tests/test_wallet_protocol_tag_grain_rescue_floor.py -q`
- [ ] **Step 3: Commit** — `test(position-reconciliation): scope-guard field-set updates`

## Task 8: Calibration v4 re-baseline (operator-gated)

**Files:** All 10 fixtures under `tests/calibration/fixtures/*.json` (recapture); `tests/calibration/labels.yaml` (version + labels).

- [ ] **Step 1: Confirm operator authorization for the live recapture.** This task hits the Goldsky activity-subgraph for `redemptions`/`splits`/`merges` for every fixture wallet. Cost per spec §8.6: ~30–100 paginated subgraph requests in addition to the existing REST traffic. Halt and surface to operator if the authorization isn't already explicit in the calling context.
- [ ] **Step 2: Recapture fixtures** — run `python3 scripts/record_fetch_snapshot.py <wallet>` (or the project's existing recapture entry point — `grep -l record_fetch_snapshot scripts/` to confirm) for each of the 10 calibration wallets. Each new fixture must contain populated (or empty-but-present) `redemptions`/`splits`/`merges` keys.
- [ ] **Step 3: Bump labels.yaml** — `calibration_version: 3 → 4`. Add per-fixture pinned labels `position_reconciliation_surface_data_state` and `position_reconciliation_surface_undercount_ratio` (exact label names by inspection of `labels.yaml`'s existing surface-label convention — match the format of the v3 copy-latency labels added in the 2026-05-15 v3 re-baseline). The 0x20ad75 fixture, if present, is the calibration target for test 2 in spec §10.
- [ ] **Step 4: Run** — `python3 -m pytest tests/calibration/ -q`. Expect green (all 10 fixtures + pinned labels reconcile).
- [ ] **Step 5: Commit** — `chore(calibration): v3→v4 recapture for position_reconciliation_surface`

## Task 9: Full regression + adversarial review

- [ ] **Step 1:** `python3 -m pytest tests/test_wallet_protocol_*.py tests/calibration/ -q` — expect all green.
- [ ] **Step 2:** `python3 scripts/validate_schema_refs.py` + `python3 scripts/brain_index.py generate` (if schema/dep index needs refresh; brain index regenerate only if files changed it triggers on) + `python3 scripts/validate_knowledge_sync.py` — expect clean / all_ok.
- [ ] **Step 3:** Adversarial review (platform-reviewer): hard wall (no other consumer reads the field — grep + mutation-proof); condition-set arithmetic correctness (set difference, intersection, the 13.7% vs 15.9% denominator clarity); replay strict `None`-preservation (no `or []` smuggled into serialize); fetch failure isolation (one stream's failure doesn't poison the other two); caveat split (`RECONCILIATION_INPUTS_MISSING` distinct from `NO_CLOSED_OR_REDEEMED_CONDITIONS`); render-block ordering when `downstream_lower_bound = True`. Fix findings with regression tests.
- [ ] **Step 4: Operator-gated integration** — do not merge autonomously. Present the reviewed branch, KSG attestation, calibration status, and full verification output to the operator. Merge `feat/position-reconciliation-surface` → main (`--no-ff`) only after explicit operator approval. Confirm `calibration_version: 4` reflects in `main` after the approved merge.

---

## Self-review

**Spec coverage:**
- §1 problem → context for Tasks 3, 6.
- §2 scope (inputs) → Task 3 signature + Task 4 wiring.
- §3 schema (13 fields) → Task 3 producer body, asserted by Task 1 tests.
- §4 constants → Task 3 module-level names; tests import them, not numbers.
- §5 algorithm (5.1–5.4) → Task 3.
- §6 `data_state` worst-wins + caveat split → Task 3 + Task 1 tests 6/7.
- §7 hard wall → Task 3 docstring + Task 4 wiring comment + Task 1 tests 10 (mutation-proof), 11 (grep guard), 17 (paired-run no-mutation).
- §8.1 readers (already present) → no-op, cited.
- §8.2 WalletData field placement → Task 2 Step 1 explicit on `n_unmapped_fills:227` adjacency.
- §8.3 fetch.py wiring → Task 5 Step 2.
- §8.4 replay strict `None` preservation → Task 2 Step 2 + Task 1 test 12b.
- §8.5 snapshot persistence → Task 5 Step 3.
- §8.6 calibration v4 → Task 8 (operator-gated).
- §8.7 fetch tests → Task 5 Step 1.
- §9 wiring → Task 4 + Task 6 render-ordering rule.
- §10 tests (1, 1b, 2, 3, 4, 4b, 5, 6, 7, 8, 9, 10, 11, 12, 12b, 13, 14, 15, 16, 17) → 1–13/16/17 in Task 1; 14 in Task 7; 15 in Task 8.
- §11 out-of-scope → no tasks (correctly nothing to build).
- §12 evidence appendix → calibration target for Task 8 + test 2 builder values in Task 1.

No gaps.

**Placeholder scan:** Every task names exact file paths, exact symbols (functions, fields, constants), exact line refs (`types.py:218`/`:227`/`:485`; `analyze_wallet.py:1506`; scope-guard tests `:562`/`:640`), and exact pytest commands. Spec sections cited per task content. No "TBD" / "as appropriate" / "similar to" handoffs.

**Type consistency:** `compute_position_reconciliation_surface` signature is `(closed_positions, activity_rows, redemptions, splits, merges, activity_cap_hit, n_unmapped_fills)` in Tasks 1, 3, 4 — identical. Surface field name `position_reconciliation_surface` consistent across Tasks 1, 4, 7, 8. Caveat names `RECONCILIATION_INPUTS_MISSING` / `NO_CLOSED_OR_REDEEMED_CONDITIONS` / `UNTRACED_ACQUISITION_RAIL` / `ORDERBOOK_RAIL_INCOMPLETE_n=<n>` / `CLOSED_POSITIONS_UNDERCOUNT_*` consistent between Tasks 1 and 3 and spec §6/§5.3. Module constants `UNDERCOUNT_PROXY_ONLY_THRESHOLD` / `ACQUISITION_GAP_NOTABLE_THRESHOLD` / `SURFACE_VERSION` named identically in Tasks 1 and 3.

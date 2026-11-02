# Memecoin exit engine — V5-only runtime, V2/V3 retirement record

**Date:** 2026-07-25
**Status:** implemented (branch `cleanup/memecoin-v5-only-runtime`), NOT activated/deployed.
**Precondition verified read-only:** 0 open real positions, 0 `reserved` memecoin pending entries
(88 confirmed / 98 failed), all 29 stored funded contracts closed with no `decision_engine_version`.
No in-place policy migration was required.

## Decision

The memecoin exit **decision** engine is **V5-only**. V2 (`exit-brain-v2`, `propose`) and V3
(`exit-brain-v3`, `propose_v3`) executable code, the process selector, the shadow-comparison surface,
and the dual-engine replay are removed. `propose_v5()` is the single installed decision entry point
across the hot lane, materializer, funded envelope, smoke, and replay paths.

This does NOT touch the **market-state** version. `memecoin_exit_market_state.ENGINE_VERSION`
(`"exit-brain-v2"`) and `FEATURE_VERSION` are the state-schema identity, unrelated to the decision
engine, and are preserved unchanged.

## What was deleted (executable surface)

- `memecoin_exit_brain`: `propose` (v2), `scaled_trail_pct`, `ENGINE_VERSION` (v2),
  `ENGINE_VERSION_V3`, `propose_v3`, `_PARTIAL_FRACTION`, `_EDGE_DURATION_S`, the `TIGHTEN`/`EXIT`
  actions, and the `CandidateDecision.proposed_trail_pct` field. `exit_engine()` now returns
  `(ENGINE_VERSION_V5, propose_v5)` for `"v5"` and raises `ValueError` for every other value.
- `exit_flex_v5`: `horizon_window_blocks`, `assert_cadence`, `shadow_comparison` (all were unwired —
  only their own tests referenced them; the real cadence guard, `assert_cadence_matches_db`, lives in
  `scripts/research/memecoin_tape_predictive_study.py` and is untouched). `market_cap_band` is kept —
  the V5 replay shares those exact band boundaries.
- Materializer: the `v5_shadow` / `shadow_comparison` block. The `v5_shadow_json` column is preserved
  and written null.
- Dual-engine replay collapsed to V5: `scripts/research/memecoin_v3_blind_replay.py` →
  `memecoin_v5_blind_replay.py` (single engine); test renamed likewise; `test_exit_engine_v3.py` →
  `test_exit_engine_v5.py`. `test_exit_brain_engine.py` (v2) and `test_exit_shadow.py` deleted.

## Invariants installed

1. **Selector retired.** `MEMECOIN_EXIT_ENGINE_VERSION` defaults to `v5` (absence ⇒ v5). An explicit
   non-v5 value fails the armed-startup gate (`validate_real_exit_policy` raises) — never a silent
   fallback to a retired engine.
2. **Freeze v5.** Every new engine-envelope funded contract freezes `decision_engine_version="v5"`
   (`_engine_envelope_policy`), not read from a selector.
3. **Refuse non-v5 open contract.** `frozen_policy_from_position` refuses to open an engine-envelope
   contract whose frozen decision version is not `v5` — never a silent upgrade of a retired/absent
   version. (No such live contract exists; precondition above.)
4. **Fail-closed factory.** `exit_engine()` raises on anything but `"v5"`, so any retired frozen
   version reaching the runner falls back to envelope-only rather than executing.

## What is preserved (history is not an installed engine)

- Closed historical V2/V3 database rows (`memecoin_exit_candidate_decisions.engine_version`,
  `funded_contract_json`) are untouched.
- Nullable historical columns (`v5_shadow_json`, `decision_engine_version`) remain in schema.

## Verification

- New retirement contract: `tests/test_memecoin_v5_only_runtime.py` (22 tests).
- Touched exit suite green: 266 tests across `test_exit_engine_v5`, `test_exit_flex_v5`,
  `test_exit_engine_hot`, `test_memecoin_fast_v5_parity`, `test_exit_materializer`,
  `test_memecoin_funded_policy`, `test_memecoin_real_policy_validation`, `test_memecoin_v5_blind_replay`,
  `test_memecoin_hot_supervisor`, `test_memecoin_engine_authoritative_exit`, `test_memecoin_solana_tape`,
  `test_memecoin_risk_settings`, `test_memecoin_real_runner`, `test_engine_decision_report`,
  `test_engine_cockpit`.

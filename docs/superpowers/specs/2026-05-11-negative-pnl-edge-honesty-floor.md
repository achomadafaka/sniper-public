# Negative-PnL Edge-Detected Honesty Floor

**Date:** 2026-05-11
**Status:** spec draft for /ship pipeline
**Issue:** #24 — ANALYZER TRUTH BUG (Pestle)
**Severity:** highest — doctrine violation

---

## Problem

The wallet analyzer rendered `overall_verdict = "edge_detected"` for wallet
Pestle `0x241f...ef50` despite:

- `net_pnl = -727729.04` (negative — wallet is losing money in the analyzed
  window)
- `n_with_resolved_attribution = 0 / 5000` (zero attribution coverage on the
  explanatory layer)

This is a doctrine violation. The wallet analyzer is a strategy-intelligence
system; its job is to extract reusable behavioral theses **AND reject
weak/noisy patterns**. A wallet that loses money in the very window the
report measures, with zero supporting explanation from the surfaces, must
not be promoted to `edge_detected`.

---

## Fix

Add a **report-level honesty floor** that runs after `overall_verdict` is
assigned in `analyze_wallet.py` (around line 919, after the verdict-assembly
block and before `_compute_v3_flags`).

### Logic

```text
if report.overall_verdict == "edge_detected":
    pnl_floor_satisfied   = report.net_pnl > 0
    coverage_floor_satisfied = (
        attribution_coverage_nontrivial(report.data_completeness)
        or timing_coverage_nontrivial(report.timing_surface)
        or position_coverage_nontrivial(report.position_building_surface)
    )
    if not (pnl_floor_satisfied or coverage_floor_satisfied):
        report.data_quality_flags.append("NEGATIVE_PNL_EDGE_UNEXPLAINED")
        report.overall_verdict = "underpowered"
        report.copyability_note = (
            "Honesty floor: edge_detected was inconsistent with net_pnl <= 0 "
            "and no non-trivial explanatory-surface coverage. Verdict downgraded; "
            "see NEGATIVE_PNL_EDGE_UNEXPLAINED."
        )
```

### Coverage helpers

Define three small predicates on the surface dicts. They are intentionally
strict ("non-trivial" requires real signal, not just field presence):

```text
attribution_coverage_nontrivial(data_completeness):
    if data_completeness is None: return False
    ac = data_completeness.get("attribution_coverage") or {}
    n_with_resolved = int(ac.get("n_with_resolved_attribution") or 0)
    n_total = int(ac.get("n_total") or 0)
    return n_with_resolved > 0 and n_total > 0

timing_coverage_nontrivial(timing_surface):
    if timing_surface is None: return False
    band = timing_surface.get("confidence_band")
    # only partial/good/high count as non-trivial coverage
    return band in {"partial", "good", "high"}

position_coverage_nontrivial(position_building_surface):
    if position_building_surface is None: return False
    ws = position_building_surface.get("wallet_summary") or {}
    cov = ws.get("observation_window_coverage")
    return isinstance(cov, (int, float)) and cov > 0.30
```

The disjunction means: if *any* one of the three surfaces has non-trivial
coverage, that's enough to keep the verdict. The PnL floor (positive
realized PnL) is a separate sufficient condition.

### When NOT to fire

The floor only inspects `overall_verdict == "edge_detected"`. All other
verdicts (`no_edge`, `underpowered`, `data_insufficient`, `operator_review`)
pass through untouched.

The floor does not fire when:

- `net_pnl > 0` (positive realized PnL is sufficient even with weak coverage
  — the wallet did make money, even if we can't explain why)
- At least one explanatory surface has non-trivial coverage (we have a real
  basis for the edge claim, even if PnL is negative — could be a survivor
  case mid-bad-period)
- Verdict is not `edge_detected` (the floor scopes only the overclaim path)

---

## Hard scope guards

The implementation must NOT:

- Touch `gate_result`, `primary_edge_type` (only `overall_verdict`,
  `copyability_note`, and `data_quality_flags` change here)
- Touch any branch-level verdict logic (held_analyst_branch /
  flipper_branch hypotheses untouched)
- Change the mode-router (`classify_mode` is unrelated)
- Add new surface compute calls — pure read over existing fields
- Add copy-trigger / simulator / forward-prediction language
- Read `low_buy_trades` or any legacy stream
- Mutate Position Builder, Timing Surface, Exit Attribution, or
  Completeness compute logic
- Add price-history calls or live API calls

---

## Acceptance criteria (verbatim from Issue #24)

1. Add the honesty floor to `analyze_wallet.py` (or wherever
   `overall_verdict` is set)
2. New test: synthetic ProtocolReport with negative `net_pnl` + zero
   attribution_coverage + branch-supported `edge_detected` → asserts verdict
   gets downgraded and caveat appears
3. Re-run live sampling on Pestle → verdict no longer `edge_detected` (or
   carries the new caveat explicitly)
4. Update `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` with a
   "Fixed" note linking this issue

Implementation handles (1) and (2). The audit-doc update (4) is a separate
follow-up step done after the PR merges (the audit doc references the merge
commit). Re-running live sampling (3) is verification but not part of the
code change.

---

## Test matrix (write all of these as failing tests in Phase 1)

| # | Verdict | net_pnl | attribution_coverage | timing band | pos coverage | Expected |
|---|---------|---------|----------------------|-------------|--------------|----------|
| 1 | `edge_detected` | -100k | empty / 0 of N | None / `underpowered` | None / 0.0 | **downgrade → `underpowered`; caveat `NEGATIVE_PNL_EDGE_UNEXPLAINED`** |
| 2 | `edge_detected` | +100k | empty | None | None | unchanged (positive PnL is enough) |
| 3 | `edge_detected` | -100k | `n_with_resolved=5/10` | None | None | unchanged (attribution coverage is enough) |
| 4 | `edge_detected` | -100k | empty | `partial` | None | unchanged (timing coverage is enough) |
| 5 | `edge_detected` | -100k | empty | None | 0.50 | unchanged (position coverage is enough) |
| 6 | `edge_detected` | -100k | empty | None | 0.25 | **downgrade → `underpowered`; caveat** (below 0.30 threshold) |
| 7 | `edge_detected` | 0 | empty | None | None | **downgrade → `underpowered`; caveat** (zero PnL is not "positive") |
| 8 | `underpowered` | -100k | empty | None | None | unchanged (floor only scopes `edge_detected`) |
| 9 | `no_edge` | -100k | empty | None | None | unchanged (floor only scopes `edge_detected`) |
| 10 | `data_insufficient` | -100k | empty | None | None | unchanged (floor only scopes `edge_detected`) |
| 11 | `edge_detected` | -100k | `data_completeness=None` (field absent) | None | None | **downgrade → `underpowered`; caveat** (None counts as no coverage) |
| 12 | `edge_detected` | -100k | empty | `low` (not in {partial,good,high}) | None | **downgrade → `underpowered`; caveat** |

### Caveat-content tests

13. Caveat string is exactly `"NEGATIVE_PNL_EDGE_UNEXPLAINED"` (no version
    suffix, no parameters). Constant lives in the same module as the floor.
14. Caveat is appended to `report.data_quality_flags` (not replaced).
    Existing flags are preserved.
15. `report.copyability_note` is populated when the floor fires; previous
    `copyability_note` is overwritten.
16. `report.primary_edge_type` is **not modified** by the floor (it stays as
    set by the verdict assembler).

### Doctrine guards

17. Forbidden output keys / phrasing test: the new code path emits no
    copy/trigger/simulator strings.
18. The floor reads only existing fields on `ProtocolReport`; no new field
    is added to `types.py` (only the caveat-key string constant).

---

## Files in scope

| File | Change |
|---|---|
| `sniper/analysis/wallet_protocol/analyze_wallet.py` | Add honesty floor after line 917; add 3 coverage-helper functions or inline checks. |
| `tests/test_wallet_protocol_analyze_wallet.py` (or existing file) | Tests 1-18 above. If a more specific test file exists for verdict logic, use that. |

No other file should be touched in this PR. **Specifically: no changes to
`types.py`, `report.py`, `analyze_wallet.py` outside the honesty-floor
block, the surface modules, or any test other than the verdict tests.**

---

## Verification (after Phase 2 lands)

```bash
python3 -m pytest tests/test_wallet_protocol_analyze_wallet.py -q
python3 -m pytest tests/test_wallet_protocol_edge_surface.py \
                  tests/test_wallet_protocol_exit_attribution.py \
                  tests/test_wallet_protocol_completeness_surface.py \
                  tests/test_wallet_protocol_timing_surface.py \
                  tests/test_wallet_protocol_position_building.py \
                  tests/test_wallet_protocol_held_analyst.py \
                  tests/test_wallet_protocol_flipper.py \
                  tests/calibration/ -q
python3 scripts/validate_schema_refs.py
```

Expected: all green; calibration fixtures don't regress; existing edge_detected
verdicts on healthy wallets still emit; the new downgrade path fires only
for the doctrine-violation case.

---

## Out of scope (track as future work, NOT in this PR)

- Re-running live sampling on Pestle to confirm behavior change in
  production (post-merge verification, not test).
- Updating the audit doc with "Fixed" note (post-merge documentation step).
- Considering whether to relax/tighten the 0.30 `observation_window_coverage`
  threshold (a future tuning issue).
- Adding a separate `operator_review_required` verdict label (the Issue #24
  suggested this as an alternative; we're choosing `underpowered` because
  the evidence is genuinely insufficient — `operator_review` is reserved
  for held/flipper conflict, not coverage gaps).

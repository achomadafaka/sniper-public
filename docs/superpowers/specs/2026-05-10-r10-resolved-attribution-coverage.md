# Followup R10 — Resolved-attribution coverage field (Option A)

**Status:** fixed (2026-05-10)
**Severity:** medium
**Source:** Codex PR #9 review, item 2 (deferred from R9 follow-up triage)
**Owner:** unassigned
**Cross-references:**
- Timing Surface v1 spec (§5 Notes block):
  `docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md`
- Wallet-protocol semantic audit:
  `docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md`

---

## 1. Why

Codex flagged that `n_attributed` (Timing Surface r2 §5) is doing two
jobs at once:

1. **Sample-size floor** for timing classification — gates
   `force_underpowered`. By design (per §5 Notes) it includes
   `unresolved_or_unknown` so that a wallet dominated by unresolved
   positions lands in `unclear`, not `underpowered`.
2. **Reliability signal** for the operator-visible
   `ATTRIBUTION_COVERAGE_LOW` caveat in the Completeness Surface and
   for downstream W2 copyability consumers.

Conflating these is a problem: a wallet with 5 held + 25 unresolved
trades has `n_attributed = 30` (high — Timing classifies as `unclear`)
but only 5 positions of resolved-attribution evidence (low — operators
should see a coverage warning before treating any held vs preres ratio
as a copyable signal).

## 2. Required fix (Option A — operator's chosen path)

> Keep Timing Surface `n_attributed = held + preres + unresolved`.
> Add `n_with_resolved_attribution = held + preres`.
> Recompute/report `ATTRIBUTION_COVERAGE_LOW` for copyability/reliability
> from `n_with_resolved_attribution / n_total`.
> Make W2 consume the resolved-attribution coverage field, not Timing's
> sample-floor field.
> Update labels/docs so `n_attributed` means "timing sample floor" and
> `n_with_resolved_attribution` means "resolved exit/hold evidence."

Three pieces of code change:

### 2.1 `exit_attribution.compute_attribution_summary` — additive

The returned `coverage` dict gains:

```python
coverage["n_with_resolved_attribution"] = (
    summary["held_to_resolution_win"]["n"]
    + summary["held_to_resolution_loss"]["n"]
    + summary["pre_resolution_profitable_exit"]["n"]
    + summary["pre_resolution_loss_cut"]["n"]
)
```

Existing fields unchanged. Existing per-bucket summary unchanged.

### 2.2 `report.py:_data_completeness_block` — caveat trigger swap

Before R10:
```python
n_attributed = max(n_total - insuf_n, 0)
if n_total > 0 and (n_attributed / n_total) < 0.50:
    capture_state_caveats.append(
        f"ATTRIBUTION_COVERAGE_LOW: only {n_attributed}/{n_total} ..."
    )
```

After R10:
```python
n_attributed = max(n_total - insuf_n, 0)               # sample floor
n_with_resolved = int(coverage.get("n_with_resolved_attribution", 0) or 0)
if n_total > 0 and (n_with_resolved / n_total) < 0.50:
    coverage_pct = _fmt_pct(n_with_resolved / n_total)
    capture_state_caveats.append(
        f"ATTRIBUTION_COVERAGE_LOW: only {n_with_resolved}/{n_total} ..."
    )
```

The "Attribution coverage" rendered section also gets a new line for
`n_with_resolved_attribution` — but ONLY when it differs from
`n_attributed` (no unresolved positions → identical → render only
`n_attributed` to keep the section compact).

### 2.3 `analyze_wallet.py` — DO NOT change derivation, add comment

The kwarg `attribution_coverage_low` passed into `compute_timing_surface`
keeps its old `(n_total - insufficient_data) / n_total < 0.50` formula.
This preserves Timing Surface r2 §5 behavior — unresolved-heavy wallets
remain `unclear`, not `underpowered`. A comment block explains the
intentional split.

## 3. Spec doc update

A new bullet was added to the §5 Notes block of the Timing Surface
v1 design doc, explicitly reserving:
- `n_attributed` / `attribution_coverage_low` kwarg = sample-size floor
  (Timing Surface gate)
- `n_with_resolved_attribution` = reliability signal (Completeness
  Surface caveat + W2 consumer)

§5 classification rule and §6 returned-dict shape were NOT modified.

## 4. Required tests

### 4.1 `tests/test_wallet_protocol_exit_attribution.py`
Add `test_compute_attribution_summary_includes_n_with_resolved_attribution`
asserting the new coverage field equals `held_w + held_l + pre_w + pre_l`
and excludes both `unresolved_or_unknown` and `insufficient_data`.

### 4.2 `tests/test_wallet_protocol_completeness_surface.py`
Add `test_attribution_coverage_low_fires_when_resolved_attribution_below_50pct`
locking the new caveat trigger: wallet with 5 held + 25 unresolved
positions must surface `ATTRIBUTION_COVERAGE_LOW` because
`5/30 = 17% < 50%`, even though `n_attributed/n_total = 100%`.

### 4.3 `tests/test_wallet_protocol_timing_surface.py`
Add `test_r10_timing_surface_classification_invariant_under_resolved_coverage_change`
locking that Timing Surface output is INVARIANT under the same fixture:
- `ts["n_attributed"] == 30` (sample floor preserved)
- `ts["classification"] == "unclear"` (held_share = 17% — unresolved
  dominates; neither bucket clears 70%)
- `ATTRIBUTION_COVERAGE_LOW NOT in ts["coverage_caveats"]` (Timing's
  kwarg derivation unchanged → no Timing-side caveat)

## 5. Acceptance criteria

- `compute_attribution_summary(...)` returned `coverage` dict carries
  `n_with_resolved_attribution` matching `held_n + preres_n`.
- Completeness Surface `ATTRIBUTION_COVERAGE_LOW` caveat fires when
  `n_with_resolved_attribution / n_total < 0.50`.
- Rendered "Attribution coverage" section labels `n_attributed` as the
  sample-size floor and shows `n_with_resolved_attribution` as a
  separate line whenever the two numbers differ.
- Timing Surface r2 §5 classification rule UNCHANGED — verified by the
  invariance regression test.
- `compute_timing_surface(...)` `n_attributed` formula UNCHANGED.
- `analyze_wallet.py` `attribution_coverage_low` kwarg derivation
  UNCHANGED.

## 6. Out of scope

- Workstream 2 (W2) consumer wiring. R10 only exposes the field; W2 is
  the next ticket and will adopt `coverage["n_with_resolved_attribution"]`
  as its copyability gate input.
- Tightening Timing Surface's `force_underpowered` to use the resolved
  signal — would change §5 classification behavior; explicitly
  forbidden by R10's contract.
- Re-running the historical reanalysis batch from R9.5; R10 is purely
  additive at the coverage-dict and caveat layers, no per-wallet
  classification changes.

## 7. Resolution

Single commit on `wallet-protocol/timing-v1-r-tickets`:
- `sniper/analysis/wallet_protocol/exit_attribution.py` — add field.
- `sniper/analysis/wallet_protocol/report.py` — swap caveat trigger.
- `sniper/analysis/wallet_protocol/analyze_wallet.py` — comment only.
- `docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md`
  — §5 Notes bullet.
- `docs/superpowers/specs/2026-05-10-r10-resolved-attribution-coverage.md`
  — this file (new).
- 3 new tests; 1 existing test (in
  `test_wallet_protocol_exit_attribution.py`) reviewed and confirmed
  unchanged: `test_analyze_wallet_walk_full_history_does_not_false_low_coverage_timing`
  asserts only the Timing Surface `coverage_caveats` list, which R10
  preserves — green.

# Followup R8 — `exit_attribution` does not OR `walk_full_history` into `has_full_history`

**Status:** fixed locally (2026-05-06)
**Severity:** medium (parallels audit R2's class of latent bug)
**Source:** discovered while patching Timing Surface v1 plan (2026-05-06)
**Owner:** unassigned
**Cross-reference:** Timing Surface v1 r2 spec (commit `d598cf2`); Timing Surface v1 plan patch
**Pinned by:** plan header in `2026-05-06-wallet-protocol-timing-surface-v1.md`

---

## 1. The bug

`sniper/analysis/wallet_protocol/exit_attribution.py:197` (in
`compute_attribution_summary`) and the parallel derivation in
`attribute_subset` both compute:

```python
has_full_history = (not activity_cap_hit) or subgraph_fallback_used
```

This **omits `walk_full_history`**. When a wallet was captured with
`walk_full_history=True`, the activity stream is the full unwalked
history regardless of the REST cap, so `has_full_history` should be
True. The current derivation reports it as False, which downgrades
positions whose curPrice is mid-band and have no SELL match to
`insufficient_data` instead of `unresolved_or_unknown`.

## 2. Timing Surface impact

Timing Surface v1's `_classify_one_position` wrapper computes
`has_full_history` correctly, including `walk_full_history`. So the
timing-surface per-position categorization was right even when the upstream
`exit_attribution.compute_attribution_summary` was wrong.

However, `analyze_wallet` derived `attribution_coverage_low` from the upstream
exit-attribution summary before calling `compute_timing_surface`. That meant
the R8 mismatch could still force Timing Surface to report
`ATTRIBUTION_COVERAGE_LOW` on a walked full-history capture. The fix must
therefore cover both upstream attribution helpers and caller plumbing.

## 3. Required fix

In `sniper/analysis/wallet_protocol/exit_attribution.py`:

```python
# In compute_attribution_summary (~line 197):
def compute_attribution_summary(
    closed_positions: Sequence[Mapping],
    activity_rows: Sequence[Mapping],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool = False,            # NEW
) -> dict | None:
    ...
    has_full_history = (
        (not activity_cap_hit)
        or subgraph_fallback_used
        or walk_full_history                     # NEW
    )
    ...
    return {
        "summary": summary,
        "coverage": {
            ...,
            "walk_full_history": bool(walk_full_history),    # NEW
            "history_assumed_full": has_full_history,
        },
    }


# In attribute_subset:
def attribute_subset(
    rows: Sequence[Mapping],
    activity_rows: Sequence[Mapping],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool = False,            # NEW
) -> dict[str, dict]:
    ...
    has_full_history = (
        (not activity_cap_hit)
        or subgraph_fallback_used
        or walk_full_history                     # NEW
    )
    ...
```

Update callers (mainly `analyze_wallet.py`'s
`compute_attribution_summary(...)` call and the per-bucket
`attribute_subset(...)` calls in `_compute_outcome_side_breakdown`)
to pass `walk_full_history=data.walk_full_history`.

The `walk_full_history: bool = False` default keeps callers that
predate this fix backwards-compatible (`False` reproduces the
existing behavior).

## 4. Required regression test

```python
def test_compute_attribution_summary_treats_walk_full_history_as_full_history():
    """When the activity capture used walk_full_history, has_full_history
    must be True even if activity_cap_hit is also True (capture used
    multiple windows; final window may have hit the per-window cap)."""
    closed = [
        {"conditionId": "cid_a", "outcome": "Yes",
         "avgPrice": 0.30, "curPrice": 0.50, "realizedPnl": 0.0,
         "size": 100, "timestamp": 2000}                        # mid-band, no SELL
    ]
    activity = [
        {"conditionId": "cid_a", "outcome": "Yes",
         "side": "BUY", "price": 0.30, "size": 100, "timestamp": 1000,
         "asset": "tok_yes_a"},
    ]
    # walk_full_history=True suppresses the "missed SELL" downgrade
    # to insufficient_data even though activity_cap_hit=True.
    summary = compute_attribution_summary(
        closed, activity,
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=True,
    )
    # The position is unresolved_or_unknown (genuine ambiguity),
    # not insufficient_data (would imply we likely missed a SELL).
    assert summary["summary"]["unresolved_or_unknown"]["n"] == 1
    assert summary["summary"]["insufficient_data"]["n"] == 0
    assert summary["coverage"]["history_assumed_full"] is True
    assert summary["coverage"]["walk_full_history"] is True
```

Also add a test for `attribute_subset` mirroring the same pattern.

## 5. Acceptance criteria

- `compute_attribution_summary` and `attribute_subset` both accept a
  `walk_full_history: bool = False` kwarg and OR it into
  `has_full_history`.
- `coverage.walk_full_history` and `coverage.history_assumed_full`
  surface the new state.
- Callers in `analyze_wallet.py` pass `walk_full_history=data.walk_full_history`.
- Timing Surface no longer receives a false `attribution_coverage_low`
  derived from stale upstream attribution counts.
- Regression tests above pass.
- Full repo suite green.
- Calibration suite green (existing behavior preserved when
  `walk_full_history=False`).
- Audit cross-reference: this ticket is closed in any future
  followup tracker that lists R1/R2/R3.

## 6. Out of scope

- Refactoring exit_attribution beyond this single derivation.
- Touching held_analyst's call sites for `_has_pre_resolution_sell`
  or `filter_held_to_resolution` — they don't currently consume
  `has_full_history`.
- Changing Timing Surface v1's local fix (it should remain — defense
  in depth and decoupling from upstream).

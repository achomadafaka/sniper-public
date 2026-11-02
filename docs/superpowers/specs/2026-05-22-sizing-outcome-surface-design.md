# Spec — Sizing & Outcome Surface (`sizing_outcome_surface`)

**Date:** 2026-05-22
**Status:** design — approved, ready for implementation plan
**Origin:** Backlog item from the analyzer "shortsightedness" review — the
analyzer measures endpoint PnL and per-trade ROI but never asks whether a
wallet **sizes its bets in line with outcome quality**. Sibling of
`capital_risk_surface`; same display-only contract.

## 1. Problem

"Larger bets won more" is the question — and it is easy to get wrong.
A naive `corr(size, pnl)` is meaningless (PnL is mechanically ~size) and
even `corr(size, roi)` is confounded by:

- **Late conviction** — a wallet that adds to a position as it moves
  favorably inflates the *final* size of winners. The sizing *decision* is
  the **first** entry, before the outcome is known.
- **Market mix / liquidity** — big bets may only happen in one category
  whose base rate differs; a global correlation then reflects category
  mix, not sizing.
- **Correlated markets** — many positions on one event slate are ~one bet;
  they inflate apparent sample and apparent signal.
- **One outsized winner** — the whole signal can be a single lucky big bet.

The surface measures the size↔outcome association **honestly**: it controls
the confounds it can (market mix via stratification, one-big-winner via
leave-one-out) and flags the rest. It reports *association*, never "skill" —
only thesis-specific simulation can promote an association to skill.

## 2. Scope

One display-only surface, `sizing_outcome_surface`, on `ProtocolReport`.
Pure function of data the analyzer already fetches. **Out of scope**
(backlogged): null-strategy baselines, copy-latency decay, survivorship —
and any promotion of "association" to "skill".

## 3. Surface schema

`report.sizing_outcome_surface: dict | None` — `None` only on the
pre-surface backward-compat path. When produced, a dict:

| Key | Type | Meaning |
|---|---|---|
| `surface_version` | int | schema version (1) |
| `data_state` | str | `ok` \| `proxy_only` \| `underpowered` \| `data_unavailable` (§6) |
| `association` | str | `larger_entries_better` \| `larger_entries_worse` \| `no_association` \| `inconclusive` — the display headline (§5.4) |
| `global_rho` | float \| None | Spearman ρ(entry_size, roi) over all datapoints |
| `global_rho_p` | float \| None | permutation p-value (fixed seed — §5.1) |
| `stratified_rho` | float \| None | sample-weighted mean of within-category ρ — the **honest** headline; neutralizes market mix |
| `n_datapoints` | int | closed positions with a valid entry size + ROI |
| `leave_one_out_rho` | float \| None | global ρ recomputed with the largest-entry position dropped |
| `robustness_holds` | bool \| None | whether the global ρ survives leave-one-out (§5.3) |
| `size_buckets` | list[dict] | entry-size quartiles: `{quartile, n, mean_entry_size_usdc, win_rate, mean_roi}` |
| `by_category` | list[dict] | per stratum meeting the floor: `{category, n, rho}` |
| `total_entry_size_usdc` | float \| None | Σ first-entry cost across datapoints |
| `total_cost_basis_usdc` | float \| None | Σ all-BUY cost across datapoints |
| `late_conviction_ratio` | float \| None | `total_cost_basis / total_entry_size` (1.0 = no adds; > 1 = adds-after-entry common) |
| `added_after_entry_usdc` | float \| None | `total_cost_basis − total_entry_size` (absolute) |
| `n_positions_dropped` | int | closed positions excluded — no derivable entry size / bad price |
| `caveats` | list[str] | surface-local honesty caveats (NOT `data_quality_flags`) |

Numeric fields are `None` (unknown), never a false `0.0`.

## 4. Datapoint construction — `compute_sizing_outcome_surface()`

`sniper/analysis/wallet_protocol/sizing_outcome.py`:

```
compute_sizing_outcome_surface(
    closed_positions: list, activity_rows: list,
    leafs_by_cid: dict | None, nav_lookup: dict | None,
) -> dict
```

Pure; no I/O; deterministic.

### 4.1 Join closed position → its BUYs

Index BUY trade rows two ways: `buys_by_cid_asset[(conditionId, asset)]` and
`buys_by_cid_outcome[(conditionId, outcome)]`. For each closed position:
- Prefer the `(conditionId, asset)` group **when the closed position has an
  `asset` AND that group is non-empty**; otherwise fall back to the
  `(conditionId, outcome)` group. **When the closed position has an `asset`,
  the fallback adopts only BUY rows that are asset-less or carry the same
  `asset`** — a BUY carrying a different non-empty `asset` is a different
  position and must never be mis-joined (it would corrupt entry size / ROI).
  When the closed position has no `asset`, the whole `(conditionId, outcome)`
  group is adopted. This covers a closed row missing `asset` AND activity
  BUY rows missing `asset` (which land only in the outcome index) without
  cross-asset contamination.
- BUYs are grouped per **asset** (outcome token) — YES and NO of the same
  market are separate positions with separate entry sizes.

Per group:
- `entry_size_usdc` = cost of the **earliest** BUY by timestamp.
- `total_cost_basis_usdc` = Σ cost of all BUYs.
- cost per row = `usdcSize` if present and > 0, else `price * size`.
- `roi` = `realizedPnl / total_cost_basis_usdc`.

### 4.2 Guards (drop → `n_positions_dropped`)

A closed position is dropped from the correlation when:
- no BUY group is found (capture gap), OR
- `entry_size_usdc <= 0` (zero / negative entry price or cost), OR
- `total_cost_basis_usdc <= 0` (ROI undefined).

Dropped positions never crash the surface; they increment
`n_positions_dropped` and feed degradation (§6).

### 4.3 Category (deterministic)

`category` for a position = `nav_lookup[leaf]` where `leaf` is the **first
leaf in sorted order** of `leafs_by_cid[conditionId]` that has a
`nav_lookup` mapping. Sorting makes the choice independent of list order.
If the condition's leafs map to **more than one distinct nav category**,
add a `CATEGORY_AMBIGUOUS` caveat (the sorted-first rule still picks one
deterministically). If no leaf maps, `category = None` (uncategorized).

## 5. Statistic + controls

### 5.1 Global Spearman ρ

`global_rho` = Spearman rank correlation of `(entry_size, roi)` over all
datapoints. Implemented as Pearson on ranks (average-rank tie handling).
`global_rho_p` = permutation p-value: shuffle `roi` against `entry_size`
`_PERM_ITERS` times (fixed `_PERM_ITERS = 2000`, `random.Random(_PERM_SEED)`
with `_PERM_SEED = 1` — deterministic / replay-safe), `(hits + 1) /
(_PERM_ITERS + 1)` where `hits` counts shuffles with `|ρ| >= |global_rho|`.
The `+1` correction counts the observed arrangement, so the p-value can
never be an (improper) `0.0`. `None` when `n_datapoints < 3`.

### 5.2 Stratified ρ — the honest headline

For each category stratum with `>= MIN_STRATUM_N` datapoints
(`MIN_STRATUM_N = 8`), compute within-category Spearman ρ. `stratified_rho`
= Σ(`n_cat` · `ρ_cat`) / Σ`n_cat` over those strata. This neutralizes the
market-mix confound — a global ρ driven purely by "big bets live in
category X" collapses because no within-category ρ is non-zero.
`by_category` lists each qualifying stratum. `None` when no stratum
qualifies (→ `proxy_only`, cannot stratify).

When both `global_rho` and `stratified_rho` are non-`None` and
`|global_rho − stratified_rho| >= 0.30`, add a `GLOBAL_STRATIFIED_DIVERGENCE`
caveat — the global figure is inflated by category mix and the stratified
figure is the honest one. This is a **caveat only**; it does NOT change
`data_state` (the surface is working as designed — it caught the mix).
The `association` headline always derives from `stratified_rho`.

### 5.3 Leave-one-out robustness

Drop the datapoint with the largest `entry_size_usdc`; recompute global ρ →
`leave_one_out_rho`. `robustness_holds` is:
- `True` when `|global_rho| < 0.10` (nothing material to break), OR
- `True` when `sign(leave_one_out_rho) == sign(global_rho)` **and**
  `|leave_one_out_rho| >= 0.5 * |global_rho|`;
- `False` otherwise (a single big bet carries the signal).

### 5.4 `association` label

- `inconclusive` — `data_state ∈ {underpowered, data_unavailable}`, OR
  `robustness_holds` is `False`, OR `stratified_rho is None`.
- `no_association` — `|stratified_rho| < 0.10`.
- `larger_entries_better` — `stratified_rho >= 0.10`.
- `larger_entries_worse` — `stratified_rho <= -0.10`.

Rendered as "larger entries were associated with better / worse outcomes" —
never "skill".

### 5.5 Size buckets + late conviction

`size_buckets`: datapoints sorted by `entry_size_usdc`, split into 4
near-equal quartiles; each `{quartile (1..4), n, mean_entry_size_usdc,
win_rate (share with roi > 0), mean_roi}`. `late_conviction_ratio` and
`added_after_entry_usdc` per §3 (guard `total_entry_size > 0`).

## 6. `data_state` — worst-wins

Explicit severity order (worst wins): `data_unavailable` > `underpowered`
> `proxy_only` > `ok`. The reported `data_state` is the worst applicable.

- `data_unavailable` — no closed positions, OR zero valid datapoints.
- `underpowered` — `< 20` valid datapoints.
- `proxy_only` — any of: `n_positions_dropped > 0`; no category data
  (`leafs_by_cid` empty/None → cannot stratify); `stratified_rho is None`;
  category imbalance (one stratum > 70% of datapoints); eventSlug
  clustering (> 50% of datapoints in events with ≥ 3 of the wallet's
  positions — a position with a missing `eventSlug`/`event_slug` is its own
  singleton cluster, matching `independence.py`; missing-slug positions are
  never lumped together); high PnL concentration (top-5 positions > 60% of
  gross profit); `robustness_holds is False`. Each adds a named caveat.
- `ok` — none of the above.

## 7. Hard wall — non-negotiable

Identical to `capital_risk_surface`:
- **Display-only.** Read ONLY by the renderer. MUST NOT feed
  `overall_verdict`, `gate_result`, `primary_edge_type`,
  `copyability_note`, branch/hypothesis logic.
- **No `data_quality_flags` append.** Caveats live in the surface's own
  `caveats` list.
- **No live I/O.** Pure function of already-fetched data.
- `types.py` carries the field with the standard hard-wall comment.

## 8. Wiring

- `types.py` — `sizing_outcome_surface: dict | None = None` on
  `ProtocolReport`, hard-wall comment.
- `analyze_wallet.py` — one call alongside the `capital_risk_surface`
  call, fed `data.closed_positions`, `data.activity_rows`, the resolved
  `leafs_by_cid`, `nav_lookup`. The display-only guarantee is the **hard
  wall** (§7) — no verdict/gate code reads `sizing_outcome_surface` — NOT
  the call's position in the function; it is a pure function of `data`.
- `report.py` — a `## Sizing & outcome` markdown block + JSON passthrough.

## 9. Acceptance tests — `tests/test_wallet_protocol_sizing_outcome_surface.py`

1. **One big winner** — a single huge-entry winning position flips
   `global_rho` positive, but `robustness_holds` is `False` and
   `association` is `inconclusive`.
2. **Market mix** — category A = big entries + high ROI, category B =
   small entries + low ROI, size uncorrelated *within* each → `global_rho`
   strongly positive but `stratified_rho ≈ 0`; surface reports the
   stratified value and flags category imbalance.
3. **Late conviction** — entry sizes flat across positions but total cost
   grows via post-entry adds on winners → `late_conviction_ratio > 1`,
   `added_after_entry_usdc > 0`; the correlation (on entry size) is
   unaffected.
4. **Missing tags** — `leafs_by_cid` empty → cannot stratify →
   `data_state = proxy_only`, caveat present, `stratified_rho is None`.
5. **Zero / negative entry price** — positions with `entry_size <= 0` are
   dropped (counted in `n_positions_dropped`), no crash, no divide error.
6. **Hard wall** — a garbage-stub producer leaves verdict / gate /
   primary_edge_type / copyability_note / `data_quality_flags`
   byte-identical (mutation-proof).
7. **YES/NO asymmetry** — same `conditionId`, different `asset` (YES vs
   NO) → two separate datapoints with separate entry sizes; never merged.
8. **Category ambiguity** — one `conditionId` whose leafs map to two
   distinct nav categories → `category` is the sorted-first deterministic
   choice (independent of leaf list order) AND a `CATEGORY_AMBIGUOUS`
   caveat is present.
9. Field present + dict-shaped on the live path; `None` only pre-surface.
10. Replay determinism — same input → byte-identical surface JSON across
    two runs (including the seeded permutation p-value).
11. Backward-compat — a `ProtocolReport` built without the field defaults
    to `None`.
12. Scope-guard field-set tests (`test_wallet_protocol_honesty_floor.py`,
    `test_wallet_protocol_tag_grain_rescue_floor.py`) updated to include
    `sizing_outcome_surface`.

## 10. Out of scope

Null-strategy baselines · copy-latency decay · survivorship correction ·
any promotion of `association` to "skill" · any verdict / gate /
Strategy-Pattern-Audit coupling. The surface is self-contained and stops
at display.

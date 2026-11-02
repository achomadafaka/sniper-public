# G6 — `category_clusters` producer on `outcome_side_breakdown`

**Date:** 2026-05-17
**Status:** spec → tests → implementation
**Origin:** `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` §"Gaps / mistakes" → G6
**Scope:** Make `_compute_outcome_side_breakdown` emit a top-level `category_clusters` list shaped to match what `category_specialist_v1` already consumes. Consumer (rule) stays as-is; producer fills the gap. No analyzer-API change, no rule rewrite, no schema mutation.

## 0. Doctrine

Strategy Pattern Audit's `category_specialist_v1` is the cross-wallet thesis primitive most useful for strategy extraction. Today it consumes a `category_clusters` field that no producer emits, so it always returns INCONCLUSIVE on real reports and only matches when tests inject the field synthetically. G6 closes the producer/consumer gap. This is a transparency fix — the wallet analyzer is still strategy-intelligence, not a copy bot; the surface just becomes consultable on real wallets.

## 1. Problem

```text
Consumer (sniper/analysis/strategy_pattern_audit/seed_rules.py:319):
  def _first_category_cluster(report):
      clusters = report.outcome_side_breakdown.get("category_clusters") or []
      return clusters[0] if clusters else None

Producer (sniper/analysis/wallet_protocol/analyze_wallet.py:643):
  _compute_outcome_side_breakdown(...)
  → emits buckets {yes, no, other}, top_nav_categories (under OTHER only),
    tag_coverage, etc.  Never emits `category_clusters`.
```

Result on every real wallet:
- `category_specialist_v1.pattern_predicate(report, priors)` → False
- Rule output: INCONCLUSIVE (`reason="coverage"` or sample-size)
- Audit promotes nothing
- Operator never sees category-specialization theses for any real wallet

The audit (2026-05-10) confirmed this on Pestle/swisstony/Oddn/rank1 — Strategy Audit correctly promoted nothing in part because `category_clusters` was never available to evaluate.

## 2. What ships

### 2.1 New top-level field on `outcome_side_breakdown`

```python
outcome_side_breakdown["category_clusters"] = [
    {"category": str, "n": int, "pnl": float, "total_pnl": float},
    ...
]
```

- Each entry is one nav category that at least one closed position maps to.
- `n` = count of closed positions in that category (a position with two nav-mapped tags counts in both clusters).
- `pnl` = sum of `realizedPnl` across closed positions in that category.
- `total_pnl` = wallet-wide sum of `abs(realizedPnl)` across all closed positions. Repeated on every cluster row as the share-denominator (this is what `_category_concentration` reads).
- Sorted by `n` descending. Tie-break: higher `abs(pnl)`, then category name ascending.
- Emitted only when both `leafs_by_cid` and `nav_lookup` are non-`None` AND at least one closed position has a nav-mapped tag.

### 2.2 When the field is absent

- `leafs_by_cid is None` or `nav_lookup is None` → key omitted entirely (preserves existing schema for callers that don't have tag data).
- Both present but no closed position maps to any nav → key omitted (the empty-list case adds no signal).
- The G4 `tag_lookup_state == "unavailable"` case naturally lands in branch 1 (None inputs) → no `category_clusters` key → `_first_category_cluster` returns `None` → rule INCONCLUSIVE. Correct existing behaviour.

### 2.3 Concentration semantics

Existing consumer logic:

```python
def _category_concentration(report):
    cluster = _first_category_cluster(report)
    total = abs(cluster.get("total_pnl"))
    return abs(cluster.get("pnl")) / max(total, 1.0)
```

`total_pnl` is the wallet-wide `sum(abs(realizedPnl))`, repeated per cluster. So `_category_concentration` reads "primary cluster's net PnL share of the wallet's gross PnL". This is well-defined even when individual positions have negative PnL.

### 2.4 Position double-counting (multi-tag)

A closed position can carry multiple leaf tags, each mapping to a nav category. Producer logic:

```python
nav_set = {nav_lookup[t] for t in leafs if t in nav_lookup}
for nav in nav_set:
    cluster_n[nav] += 1
    cluster_pnl[nav] += pnl
total_pnl += abs(pnl)   # ONCE per position
```

`sum(cluster_n.values())` can exceed `len(closed_positions)` when positions are multi-categorized. `total_pnl` is added once per position to keep the share denominator coherent. The rule's "primary cluster ≥ 60% share" check holds when one category clearly dominates.

### 2.5 Non-changes

- `category_specialist_v1` rule: unchanged.
- Existing `outcome_side_breakdown` fields (yes/no/other buckets, tag_coverage, top_nav_categories under OTHER, outcome_labels, etc.): unchanged.
- `ProtocolReport` schema: unchanged.
- `_compute_outcome_side_breakdown` signature: unchanged (already takes `leafs_by_cid` and `nav_lookup` kwargs).

## 3. Acceptance tests

`tests/test_wallet_protocol_outcome_side_breakdown_category_clusters.py` (new file). 10 tests.

### 3.1 Field presence under tag availability

```text
test_category_clusters_emitted_when_tag_data_available
  Build positions with explicit leafs_by_cid + nav_lookup so 5 positions
  map to "politics" and 2 to "sports". Assert outcome_side_breakdown
  contains "category_clusters" with both entries.

test_category_clusters_absent_when_leafs_by_cid_is_none
  Same positions, leafs_by_cid=None → "category_clusters" not in dict.

test_category_clusters_absent_when_nav_lookup_is_none
  leafs_by_cid populated, nav_lookup=None → key omitted.

test_category_clusters_absent_when_no_positions_map_to_nav
  leafs_by_cid has leafs but nav_lookup has no mapping for those leafs
  → key omitted.
```

### 3.2 Shape correctness

```text
test_category_clusters_shape_matches_rule_consumer
  Each cluster has exactly {category, n, pnl, total_pnl} keys with the
  correct types (str, int, float, float).

test_category_clusters_sorted_by_n_descending_then_pnl_then_name
  5 positions in "politics" (n=5, pnl=300), 5 positions in "sports"
  (n=5, pnl=200), 3 positions in "geo" (n=3). Assert order is
  [politics, sports, geo] — politics first by abs(pnl) tie-break,
  then sports, then geo by n.
```

### 3.3 Concentration math

```text
test_cluster_total_pnl_is_wallet_wide_abs_pnl_sum
  5 positions: pnl values [100, -50, 200, -30, 80]. Assert every
  cluster row has total_pnl == 460 (= 100+50+200+30+80).

test_cluster_pnl_is_in_category_sum
  3 positions in "politics" with pnl [100, -50, 200]. Cluster
  "politics".pnl == 250.

test_multi_category_position_counts_in_both_clusters
  1 position tagged as both "politics" and "sports". cluster_n
  for politics == 1 AND for sports == 1.  Position's |pnl|
  contributes to total_pnl exactly ONCE.
```

### 3.4 Integration with the rule

```text
test_category_specialist_v1_now_matches_real_report
  Build a real ProtocolReport via analyze_wallet with leafs_by_cid +
  nav_lookup that produces a wallet with 6 positions in "politics"
  out of 8 total. Run category_specialist_v1.pattern_predicate;
  assert True. Without this slice, the rule returned False because
  category_clusters was never produced.
```

### 3.5 Acceptance ceiling

10 tests. 4 in §3.1, 2 in §3.2, 3 in §3.3, 1 in §3.4.

## 4. Implementation surface

| File | Change |
|---|---|
| `sniper/analysis/wallet_protocol/analyze_wallet.py` | In `_compute_outcome_side_breakdown`, after computing buckets, add a top-level `"category_clusters"` key when both `leafs_by_cid` and `nav_lookup` are non-`None` AND aggregation produces ≥ 1 cluster. Aggregate per the algorithm in §2.4. |

No CLI change. No test widening on existing files (existing rule tests inject synthetic `category_clusters` and continue to pass).

## 5. Risks

1. **Multi-tag double-counting inflates `n`.** Documented in §2.4. Acceptable: a wallet that's truly specialized in one category will have most positions in one cluster regardless of multi-tagging. Borderline cases (wallets that trade many crossover markets) may inflate concentration; the rule's `pattern_predicate` has `≥0.60` threshold which gives margin.
2. **Concentration share can exceed 1.0 for heavy multi-tag.** A position in 3 categories adds to 3 clusters' `pnl`, but `total_pnl` adds once. So `sum(cluster_pnl) / total_pnl` can exceed 1. Per-cluster shares stay well-defined (each is one category's share). The rule reads only the primary cluster.
3. **Performance.** O(N closed positions × M leaf tags). Even for 5000 closed positions × 5 leaf tags average = 25K ops. Negligible.
4. **The producer is now gated on tag data quality.** When G4 says `tag_lookup_state == "unavailable"`, this producer correctly omits the key (because leafs_by_cid is None). Strategy Audit then rejects the wallet's category claim with INCONCLUSIVE. Aligned.
5. **Existing tests that synthesize `category_clusters` keep working.** Those tests set `report.outcome_side_breakdown = {"category_clusters": [...]}` directly without going through the producer. They don't intersect with this slice.

## 6. Out of scope

- `category_specialist_v1` rule edits.
- Auto-rejecting the rule when `tag_lookup_state == "unavailable"`. The current rule's `coverage_predicate` already returns False when `outcome_side_breakdown` is None; an absent `category_clusters` key naturally yields `_first_category_cluster() is None`. Both paths converge on INCONCLUSIVE / no match.
- Production of `category_clusters` based on **all** positions (open + closed). G6 sticks with closed positions only — that's what `_compute_outcome_side_breakdown` operates on.
- Per-bucket nav categorization (currently nav is computed only for OTHER bucket's `top_nav_categories` per its docstring; G6 adds a cross-bucket aggregate at the top level).
- Renderer integration.

## 7. Knowledge sync expectations

- `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` G6 status → resolved with commit ref.
- No AGENTS.md surface change.

## 8. Done definition

- 10 acceptance tests in `tests/test_wallet_protocol_outcome_side_breakdown_category_clusters.py` red, then green.
- `python3 -m pytest tests/test_wallet_protocol_*.py tests/test_strategy_pattern_audit_*.py -q` passes.
- `python3 scripts/validate_schema_refs.py` passes.
- `git diff --stat` shows only the analyzer file + spec + test.

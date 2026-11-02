# Wallet Dossier — Market Structure v2

**Date:** 2026-05-26
**Status:** implementation slice
**Supersedes:** §7.1 of `2026-05-25-wallet-dossier-semantics-v0.md`
**Schema version:** bumps `DOSSIER_SCHEMA_VERSION` from `"v1_market_semantics"` to `"v2_market_structure"`

## 0. Why This Exists

`v1_market_semantics` resolves category when `leafs_by_cid` and `nav_lookup` are present, but it still derives `market_type` only from asset IDs the wallet personally traded.

That misses a common Polymarket fixture shape: `/closed-positions` gives the selected `asset` and its `oppositeAsset`. A wallet can trade only one side of a binary condition while the fixture still proves the condition has two CTF tokens. Treating that row as `market_type="unknown"` is false uncertainty.

This slice repairs condition-level market structure without live API calls. Event-level grouping, event family, sibling prices, and favorite/longshot rank remain out of scope.

## 1. Scope

### In Scope

- `derive_market_type` consumes `closed_positions[*].oppositeAsset`.
- A single-sided closed position with `asset` + nonzero distinct `oppositeAsset` derives:
  - `market_type = "binary"`
  - `market_type_source = "closed_position_opposite_asset"`
  - `n_outcomes_in_market = 2`
- Existing observed-token derivation stays unchanged:
  - 2 distinct observed assets -> `binary`
  - >2 distinct observed assets -> `multi_outcome`
  - 1 distinct asset and no usable `oppositeAsset` -> `unknown`
- Lineage records the v2 derivation rule.
- The `multi_outcome_undercounted_when_wallet_one_sided` data gap is count-based. It is emitted only when residual `market_type="unknown"` rows remain.

### Out of Scope

- Gamma `/events` event grouping.
- Distinguishing a binary condition from a multi-condition event family.
- Event family / market subtype classification.
- Favorite/longshot rank.
- Sibling token price snapshots.
- Strategy Pattern Audit ingestion.
- Simulation or paper validation.

## 2. Derivation Rule

For a target `condition_id`:

1. Collect observed asset IDs from:
   - `WalletData.closed_positions[*].asset`
   - `WalletData.activity_rows[*].asset`
   - `assetId`, `makerAssetId`, `takerAssetId` when present
2. Collect structural opposite asset IDs from matching closed positions:
   - `WalletData.closed_positions[*].oppositeAsset`
   - ignore null, empty, and `"0"`
3. Compute `n_outcomes = len(observed_asset_ids ∪ opposite_asset_ids)`.

Then:

```text
if n_outcomes is None:
  market_type = "unknown"
  market_type_source = "no_observations"

elif n_outcomes == 1:
  market_type = "unknown"
  market_type_source = "outcome_token_count_ambiguous"

elif n_outcomes == 2 and one observed token was repaired by oppositeAsset:
  market_type = "binary"
  market_type_source = "closed_position_opposite_asset"

elif n_outcomes == 2:
  market_type = "binary"
  market_type_source = "outcome_token_count"

else:
  market_type = "multi_outcome"
  market_type_source = "outcome_token_count"
```

`oppositeAsset` is condition-level CTF structure evidence. It does not prove event family, favorite status, or market-selection skill.

## 3. Acceptance

1. Unit test: one matching closed position with `asset="1001"` and `oppositeAsset="1002"` derives binary with source `closed_position_opposite_asset`.
2. Existing unit tests for binary, multi-outcome, unknown, empty, and case-insensitive condition IDs still pass.
3. Dossier schema version is `v2_market_structure`.
4. Focused dossier suite passes.
5. Schema reference validator passes.
6. Canonical real-shape fixture has no zero-count `multi_outcome_undercounted_when_wallet_one_sided` gap after all rows repair to `binary`.
7. No live API calls, DB writes, service restarts, or verdict logic changes.

## 4. Carry Forward

This slice fixes false `unknown` market structure for one-sided binary conditions. It does not solve these remaining analyzer gaps:

- event family/subtype (`NBA game winner`, `player prop`, `tournament winner`, etc.)
- multi-condition event grouping from Gamma `/events`
- favorite/longshot/outcome rank at entry
- selected-vs-unselected market-selection baselines
- cross-wallet Strategy Pattern Audit over dossiers
- durable thesis lifecycle and simulation validation

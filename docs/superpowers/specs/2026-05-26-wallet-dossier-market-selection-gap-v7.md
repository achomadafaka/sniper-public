# Wallet Dossier Market-Selection Gap v7

**Status:** implemented on `feat/wallet-dossier-market-structure-v1`
**Schema version:** bumps `DOSSIER_SCHEMA_VERSION` from `"v2_market_structure"` to `"v2_market_structure"` in the PR-A cut
**Parent specs:** `2026-05-10-strategy-pattern-audit-design.md`, `2026-05-26-wallet-dossier-market-structure-v2.md`

## Purpose

The dossier can explain what happened inside markets the wallet traded. It still cannot prove market-selection skill, because that requires the market universe the wallet could have selected from.

v7 makes that blocker explicit.

## Data Gap

`data_gaps.gap_name = "market_selection_universe_baseline_missing"`

| Field | Rule |
| --- | --- |
| `affected_columns` | `["mechanism_evidence.market_selection_driven"]` |
| `affected_grain_count` | Count of `mechanism_evidence` rows where `mechanism="market_selection_driven"` and `not_promoted_reason="market_selection_driven_deferred"` |
| `unblocking_spec` | `docs/superpowers/specs/2026-05-10-strategy-pattern-audit-design.md` |
| `severity` | `high` |

## Required Future Data

Market-selection skill needs external or pre-captured market-universe data:

- selected markets,
- comparable unselected markets,
- category-local baselines,
- market-type-local baselines,
- timestamp-valid market availability snapshots.

The dossier must not infer this from the wallet's traded markets alone.

## Acceptance

1. The gap appears whenever market-selection mechanism rows are deferred.
2. `affected_grain_count` equals the number of deferred market-selection mechanism rows.
3. Existing data-gap lineage coverage still passes.
4. No thesis candidate is created from this gap alone.

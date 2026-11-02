# Strategy Thesis Validation Workflow v1

**Status:** ported from `feat/wallet-dossier-market-structure-v1` onto main 2026-05-27
**Surface version:** `strategy_thesis_validation_workflow_v1`
**Parent specs:** `2026-05-10-strategy-pattern-audit-design.md`, `2026-05-27-strategy-pattern-audit-dossier-adapter-v1.md`

## Purpose

Promoted cross-wallet theses still are not deployable strategies. They need a thesis-specific dataset, offline replay, paper validation, and separate operator approval before any live validation.

This surface is the deterministic handoff from thesis catalog to validation workflow.

## API

```python
build_thesis_validation_workflow(
    audit_catalog: Mapping[str, Any],
    *,
    dataset_inventory: Sequence[str] | None = None,
) -> dict
```

## Output

```text
surface_version
source_surface_version
validation_plans
data_gaps
top_level_caveats
```

Each validation plan contains:

```text
thesis_key
mechanism
grain_key
supporting_wallets
required_test_data
missing_test_data
status
workflow_steps
```

## Status Rules

| Condition | Status |
| --- | --- |
| Any required dataset missing from `dataset_inventory` | `needs_data` |
| All required datasets present | `simulation_ready` |
| No promoted theses | no plans; top-level caveat `NO_PROMOTED_THESES_TO_VALIDATE` |

## Workflow Steps

1. `dataset_build`
2. `offline_replay_simulation`
3. `paper_validation`
4. `live_validation`

`live_validation` is always `out_of_scope` in v1 and requires separate operator authorization. This surface never emits triggers, sizing, expected PnL, or live signals.

## Acceptance

1. Empty thesis catalog produces no plans and an explicit caveat.
2. Missing dataset requirements block dataset build.
3. Complete dataset inventory marks offline replay as ready.
4. Paper validation remains blocked until simulation review.
5. Live validation remains out of scope.
6. Audit data gaps are preserved in the workflow output.

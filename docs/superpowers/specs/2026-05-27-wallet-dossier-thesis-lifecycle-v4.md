# Wallet Dossier Thesis Lifecycle v4

**Date:** 2026-05-27
**Status:** design locked; ready for /ship
**Renumbered from v6:** v4 event-family and v5 mechanism-grain extension are intentionally not shipped, so this is the actual next physical slice past v3_binary_outcome_rank.
**Schema version:** bumps `DOSSIER_SCHEMA_VERSION` from `"v3_binary_outcome_rank"` to `"v4_thesis_lifecycle"`
**Parent specs:** `2026-05-25-wallet-dossier-semantics-v0.md`, `2026-05-26-wallet-dossier-market-semantics-v1.md`, `2026-05-26-wallet-dossier-binary-outcome-rank-v3.md`
**Empirical justification:** 6 promoted theses produced across the v4 calibration set under v3 (see `docs/findings/2026-05-27-v0-to-v3-overnight-empirical-pass.md`). These rows currently live only in `mechanism_evidence.candidate_thesis_evidence`; v4 adds the lifecycle table so they have durable identity and can be tracked across reruns.

## Purpose

`mechanism_evidence.candidate_thesis_evidence` is an evidence field, not a durable thesis object. v4 adds the first lifecycle table so promoted mechanism rows can be tracked, tested, and later aggregated without letting reports invent theses.

This is still not a simulator and not a trade signal layer.

## Table

`thesis_candidates`

| Column | Type | Rule |
| --- | --- | --- |
| `thesis_id` | TEXT PRIMARY KEY | Deterministic hash of `grain_id`, `mechanism`, and schema version. |
| `grain_id` | TEXT NOT NULL | Direct mirror from the promoted `mechanism_evidence` row. |
| `mechanism` | TEXT NOT NULL | Direct mirror from the promoted `mechanism_evidence` row. |
| `status` | TEXT NOT NULL | v4 emits only `candidate`. |
| `thesis_text` | TEXT NOT NULL | Direct mirror of `mechanism_evidence.candidate_thesis_evidence`. |
| `supporting_metrics` | TEXT NOT NULL | Direct mirror of `mechanism_evidence.supporting_metrics`. |
| `required_test_data` | TEXT NOT NULL | JSON array selected by mechanism. |
| `simulation_plan` | TEXT NOT NULL | Deterministic JSON object naming replay constraints before paper validation. |
| `source_schema_version` | TEXT NOT NULL | `DOSSIER_SCHEMA_VERSION`. |

`thesis_candidates` is included in `all_dossier_tables()`, SQLite output, JSON export, and lineage coverage.

## Promotion Rule

Only rows with non-null `mechanism_evidence.candidate_thesis_evidence` produce thesis candidates.

Rows with `evidence_strength="metric_supported"` but no candidate text remain in `mechanism_evidence` only. Underpowered rows, diagnostic-only rows, and deferred market-selection rows do not produce thesis candidates.

## Required Test Data

| Mechanism | Required data |
| --- | --- |
| `prediction_driven` | `resolution_outcomes`, `market_metadata`, `price_history` |
| `trading_driven` | `fill_sequence`, `counterfactual_resolution`, `market_timestamps` |
| `risk_management_driven` | `fill_sequence`, `counterfactual_resolution`, `risk_cut_policy` |
| `mixed` | `mechanism_specific_dataset`, `counterfactual_resolution` |
| other | `mechanism_specific_dataset` |

The mapping is intentionally conservative. It names what would be needed to test a thesis later; it does not claim deployability.

## Simulation Plan

v4 writes a deterministic stub:

```json
{
  "status": "simulation_ready_after_dataset_build",
  "replay_constraints": [
    "no_live_signal",
    "use_only_timestamp-valid data",
    "model fees/slippage before paper validation"
  ]
}
```

The simulator itself is out of scope. Paper/live validation remains a later layer.

## Lineage

Every non-key `thesis_candidates` cell has a `data_lineage` row.

| Column | Source detail |
| --- | --- |
| `grain_id`, `mechanism`, `supporting_metrics` | matching `mechanism_evidence` column |
| `thesis_text` | `mechanism_evidence.candidate_thesis_evidence` |
| `status` | deterministic lifecycle status |
| `required_test_data` | mechanism requirement map |
| `simulation_plan` | fixed v4 simulation-readiness plan |
| `source_schema_version` | `DOSSIER_SCHEMA_VERSION` |

## Acceptance

1. `DOSSIER_SCHEMA_VERSION == "v4_thesis_lifecycle"`.
2. `dossier_meta` includes `n_thesis_candidates`.
3. Synthetic dossiers with no promoted mechanism evidence produce zero thesis candidates.
4. A promoted mechanism row produces exactly one deterministic thesis candidate.
5. Unpromoted mechanism rows produce no thesis candidate.
6. `thesis_candidates` is written to SQLite and JSON export.
7. Schema refs validate.

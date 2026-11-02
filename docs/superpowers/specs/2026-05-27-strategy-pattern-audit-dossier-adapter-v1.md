# Strategy Pattern Audit Dossier Adapter v1

**Status:** ported from `feat/wallet-dossier-market-structure-v1` onto main 2026-05-27
**Surface version:** `strategy_pattern_audit_dossier_v1`
**Parent specs:** `2026-05-10-strategy-pattern-audit-design.md`, `2026-05-27-wallet-dossier-thesis-lifecycle-v4.md`

## Purpose

The original Strategy Pattern Audit consumes `ProtocolReport` objects. After wallet dossiers, the safer cross-wallet input is the deterministic dossier table set.

This adapter is deliberately narrow:

1. It aggregates only durable `thesis_candidates` rows.
2. It consolidates per-wallet `data_gaps`.
3. It does not read files, query DBs, call APIs, or run analyzer batches.
4. It does not promote raw `mechanism_evidence` rows, even when `evidence_strength="metric_supported"`.

## API

```python
compute_dossier_strategy_pattern_audit(
    dossiers: Sequence[DossierTables | Mapping[str, Any]],
    *,
    audit_run_id: str | None = None,
    min_supporting_wallets: int = 2,
) -> dict | None
```

Returns `None` for zero dossiers. Otherwise returns:

```text
audit_run_id
surface_version
generated_from
theses
rejected_theses
data_gaps
top_level_caveats
```

## Offline CLI

```bash
python3 scripts/build_dossier_strategy_audit.py \
  --dossier-json /path/to/wallet_1_json_export \
  --dossier-json /path/to/wallet_2_json_export \
  --out-json /tmp/strategy_audit.json \
  --out-validation-json /tmp/strategy_validation_workflow.json \
  --dataset-inventory price_history,resolution_outcomes
```

The CLI reads only JSON exports produced by `scripts/build_wallet_dossier.py --json-export`. It does not read SQLite, call APIs, query Supabase, mutate DBs, or run analyzer batches.

## Thesis Grouping

Candidate rows group by:

```text
mechanism + full result_semantics.grain_key
```

The full grain key currently includes:

```text
category_source
category
market_type
outcome_role
event_family
favorite_status
entry_price_bucket
exit_mode
position_pattern
```

This avoids merging materially different behaviors such as sports favorites held to resolution and crypto longshots sold pre-resolution.

## Honesty Bar

v1 promotes a cross-wallet thesis only when:

```text
n_supporting_wallets >= min_supporting_wallets
```

The lower bound is clamped to 2. Single-wallet thesis candidates are retained in `rejected_theses` with `support_floor`.

Population-level thresholds, archetype diversity, and anti-pattern symmetry remain future work. They require a larger dossier corpus and should not be faked on the current fixture set.

## Data Gaps

The adapter consolidates `data_gaps` by `gap_name`:

```text
affected_wallets
n_affected_wallets
severity
affected_grain_count
```

Severity uses the maximum severity observed across supporting dossiers.

## Non-Goals

- No auto-discovery.
- No simulator.
- No trade recommendations, triggers, sizing, or expected PnL.
- No promotion from broad diagnostic buckets.
- No promotion from raw `metric_supported` mechanism rows.

## Acceptance

1. Empty input returns `None`.
2. Two matching thesis candidates on the same semantic grain produce one cross-wallet thesis.
3. One matching thesis candidate is rejected with `support_floor`.
4. Raw `metric_supported` mechanism evidence without `thesis_candidates` produces no thesis.
5. Dossier data gaps consolidate across wallets.

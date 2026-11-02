# Wallet Fact Ledger And Resumable Capture Design

Date: 2026-05-02
Status: draft for operator review
Replaces: Phase 2C single-shot fixture re-record plan

## 1. Goal And Scope

The wallet analyzer must stop treating an in-memory `WalletData` blob or one JSON fixture as the source of truth. The source of truth becomes a resumable, per-wallet fact ledger backed by local analytical storage plus a Supabase control plane for shared dimensions, run results, findings, and artifact registry entries.

The protocol consumes materialized windows from the ledger. Fixtures become export-only replay artifacts.

### In Scope

- Local per-wallet fact warehouse using DuckDB.
- Shared local market-data warehouse using DuckDB.
- Cross-wallet local index warehouse.
- Supabase control-plane tables for canonical shared dimensions, analysis results, findings, and artifact metadata.
- Resumable shard capture with native cursors per surface.
- Batch CLOB price-history adoption using `/batch-prices-history`.
- Outcome-aware ledger from day one, keyed by `token_id` and `outcome_index` where available.
- Immediate protocol fail-closed guard at the hypothesis level for unsupported non-binary assumptions.
- `materialize_wallet_data(wallet, window)` compatibility layer for existing protocol code.
- Re-baseline of the 10 calibration wallets against ledger-derived fixture exports.

### Out Of Scope

- LLM interpretation layer. Numeric findings come first; interpretation is Phase 2D.
- Natural regime-window detector.
- Copy-trading or reverse-engineering action layer.
- Full multi-outcome edge models. The first slice prevents wrong binary-assumption math; richer multi-outcome inference comes after the ledger shows the real surface.

### Success Criteria

- Capture resumes deterministically after interruption.
- Completed immutable shards are not refetched unless `--refresh`, source-version change, or capture-config change requires it.
- Progress is monotonic: each run either adds facts, records a known unreachable boundary, or records a terminal failure reason.
- Completion is tracked per surface, not as a vague wallet-level flag.
- Binary-market verdicts remain unchanged when materialized from equivalent ledger inputs.
- Non-binary rows fail closed only for hypotheses that cannot score them correctly.
- Findings and analysis results are written to Supabase for every completed analysis run.

## 2. Storage Topology

### Local Wallet Warehouse

Path: `wallet_warehouse/<wallet_address>.duckdb`

Per-wallet file isolation keeps write contention low, makes wallet lifecycle simple, and allows one wallet's full history to be moved or archived independently.

Tables:

- `capture_runs`
- `capture_shards`
- `wallet_events`
- `wallet_event_raw_payloads`
- `wallet_positions`
- `wallet_price_lookup_requests`
- `wallet_execution_probes`
- `wallet_execution_probe_horizons`
- `wallet_reconstructed_flips`
- `analysis_windows`
- `analysis_price_manifest`

### Local Market Data Warehouse

Path: `wallet_warehouse/market_data.duckdb`

Shared market facts are not wallet facts. Price points and market metadata should not be duplicated in every wallet file.

Tables:

- `market_outcome_dim_snapshot`
- `market_metadata_snapshot`
- `market_price_points`
- `market_price_coverage_ranges`

Writes are serialized through `index.duckdb.shared_warehouse_lock`. Reads are unlocked.

### Local Index Warehouse

Path: `wallet_warehouse/index.duckdb`

Tables:

- `wallets_index`
- `shared_warehouse_lock`

`wallets_index` tracks wallet file path, latest run id, schema version, last successful capture timestamp, latest finding summary, and capture completeness.

### Supabase Control Plane

Supabase stores shared canonical dimensions and durable outputs, not millions of raw per-wallet facts.

Tables:

- `market_outcome_dim`
- `wallet_analysis_results`
- `wallet_findings`
- `artifact_registry`

Raw local artifacts are referenced from Supabase by content hash, path, byte count, schema version, and capture run id.

## 3. Pipeline Boundaries

Pipeline order is fixed:

1. Capture raw facts.
2. Normalize facts.
3. Enrich and derive deterministic analytical facts.
4. Analyze windows.
5. Persist numeric results and findings.
6. Interpret with LLM in a later phase.

LLM access defaults to `wallet_findings` only. Raw facts access requires explicit debug/audit mode and must be logged. No LLM-generated finding can be persisted unless it cites numeric finding ids, run ids, or source rows.

Every local table carries audit columns where applicable:

- `source`
- `source_version`
- `capture_run_id` or `first_capture_run_id`
- `captured_at`
- `normalization_version` where normalization exists

## 4. Capture Runs And Shards

### `capture_runs`

One row per analyzer/capture invocation for one wallet.

Required columns:

- `run_id`
- `wallet`
- `started_at`
- `finished_at`
- `status`
- `config_hash`
- `schema_version`
- `source_versions_json`
- `surfaces_requested_json`
- `surfaces_complete_json`
- `error_class`
- `error_message`

### `capture_shards`

One row per resumable work unit.

Required columns:

- `wallet`
- `capture_run_id`
- `surface`
- `shard_uid`
- `cursor_kind`
- `cursor_value_json`
- `range_start_ts`
- `range_end_ts`
- `budget_json`
- `status`
- `rows_written`
- `next_cursor_json`
- `source`
- `source_version`
- `started_at`
- `finished_at`
- `error_class`
- `error_message`
- `content_hash`

Shard cursors are native to each surface:

- Data API activity: offset/end-ts walk and optional `start_ts`.
- Goldsky fills: `id_gt`.
- CLOB price history: token batch plus time range.
- Execution probes: ordered fill-event batches.
- Positions: endpoint plus offset and capture timestamp.

Caps limit one shard's work only. Caps never define truth.

## 5. `wallet_events`

Grain: one row per observed wallet event and affected outcome token.

Multi-token events write one row per affected token. They do not invent chain log indexes; the expanded row identity includes the token/outcome component.

Primary key:

`PRIMARY KEY (wallet, event_uid)`

Required columns:

- `wallet`
- `event_uid`
- `source_event_uid`
- `canonical_event_hash`
- `event_row_ordinal`
- `source_event_type`
- `ts`
- `block_number`
- `condition_id`
- `token_id`
- `outcome_index`
- `outcome_label`
- `side`
- `size_tokens`
- `collateral_amount`
- `collateral_token`
- `collateral_token_address`
- `collateral_display_unit`
- `usd_equivalent_amount`
- `price`
- `fee_collateral`
- `transaction_hash`
- `log_index`
- `maker`
- `taker`
- `ingest_status`
- `dedup_status`
- `normalization_notes_json`
- `normalization_version`
- audit columns

`event_uid` format:

`{source}:{transaction_hash}:{log_index_or_event_row_ordinal}:{token_id_or_outcome_index}`

When the source row lacks a chain `log_index`, `event_row_ordinal` substitutes to keep events from the same transaction distinguishable. The ordinal is stable per source ingest call.

Derived analytical constructs do not go in `wallet_events`.

### Dedup

Dedup uses two keys:

- `source_event_uid`: stable inside a source.
- `canonical_event_hash`: normalized best-effort cross-source identity.

Goldsky is preferred over Data API only when canonical hashes match confidently. Ambiguous matches keep both rows with `dedup_status='ambiguous'`; nothing potentially unique is dropped.

### Raw Payload

Raw payloads live in `wallet_event_raw_payloads`, not inline in `wallet_events`.

Indexes:

- `(wallet, ts DESC)`
- `(wallet, condition_id, token_id, ts)`
- `(wallet, condition_id)`
- `(wallet, token_id)`
- `(wallet, transaction_hash)`

## 6. `wallet_event_raw_payloads`

Primary key:

`PRIMARY KEY (wallet, event_uid)`

Required columns:

- `wallet`
- `event_uid`
- `raw_json`
- `raw_sha256`
- `raw_payload_bytes`
- audit columns

This table is joined only for forensic re-parsing, re-normalization, or audit.

## 7. Market Dimensions

### Supabase `market_outcome_dim`

Canonical shared outcome registry.

Primary key:

`PRIMARY KEY (token_id)`

Unique constraints:

- `UNIQUE(condition_id, token_id)`
- `UNIQUE(condition_id, outcome_index)` when `outcome_index IS NOT NULL`

Required columns:

- `token_id`
- `condition_id`
- `outcome_index`
- `outcome_label`
- `outcome_count`
- `is_binary`
- `market_structure`
- `negative_risk`
- `market_slug`
- `event_slug`
- `title`
- `end_date`
- `resolved`
- `resolved_at`
- `resolution_price`
- `resolution_source`
- `dim_quality_flags_json`
- `raw_market_json`
- audit columns

`outcome_index` may be NULL. It is derived only when `clobTokenIds` and `outcomes` align exactly. If unresolved, `dim_quality_flags_json` includes `outcome_index_unresolved`.

Resolution freezes per token when `resolution_price` is stable or source-confirmed. Market-level `resolved=True` is not enough.

### Local `market_outcome_dim_snapshot`

The local snapshot uses an explicit column copy from canonical rows for a run's condition/token set. It is physically materialized and never a view. Analysis reads snapshots, not live canonical Supabase rows.

Additional columns:

- `capture_run_id`
- `snapshot_at`
- audit columns

No `SELECT *` copies are allowed.

## 8. `wallet_positions`

Position snapshots, not one mutable live row.

Primary key:

`PRIMARY KEY (wallet, position_uid, capture_run_id)`

Logical key:

`position_uid = sha256(wallet, condition_id, token_id)`

Required columns:

- `wallet`
- `position_uid`
- `condition_id`
- `token_id`
- `outcome_index`
- `outcome_label`
- `status`
- `is_latest`
- `avg_price`
- `size_tokens`
- `cost_basis_collateral`
- `total_bought_collateral`
- `total_sold_collateral`
- `realized_pnl_collateral`
- `unrealized_pnl_collateral`
- `current_price`
- `collateral_token`
- `collateral_token_address`
- `api_total_bought_raw`
- `api_initial_value_raw`
- `opened_at`
- `opened_at_source`
- `closed_at`
- `closed_at_source`
- `is_finalized`
- `finalized_at`
- `finalization_reason`
- `raw_position_json`
- `normalization_notes_json`
- audit columns

Refresh policy:

- Open positions refresh every run.
- Closed positions refresh until finalized.
- Finalized positions are immutable unless `--refresh`.

`closed_at` → `timestamp` alias boundary (Slice 6 TI-2):

The DuckDB surface stores `closed_at`. The Polymarket-API-shape
`timestamp` alias is added at **read time** by the materializer's
`_normalize_for_walletdata`, not at write time by the position
writer. Stage 1 dormancy / `_recent_90d_fraction` and the
held-branch CUSUM post-regime sort key both read `timestamp`; the
materializer is the single translation point.

Future callers that need the persisted alias (e.g. a query path
that reads `wallet_positions` directly without going through the
materializer) MUST propose lifting the rule into the writer in a
dedicated migration. Slice 6 currently has no such caller.

Finalized means:

1. Closed status.
2. Per-token market outcome is resolved.
3. `realized_pnl_collateral` is stable within tolerance across two compatible runs.

## 9. Shared Price Data

Official CLOB price history returns timestamp/price points, not OHLCV bars. The new pipeline uses `/batch-prices-history`, which accepts up to 20 asset ids per request.

### `market_price_points`

Shared append-only table in `market_data.duckdb`.

Primary key:

`PRIMARY KEY (token_id, fidelity_minutes, ts, source_version)`

Required columns:

- `token_id`
- `fidelity_minutes`
- `ts`
- `source_version`
- `price`
- `point_status`
- `point_source`
- `content_hash`
- `source`
- `first_capture_run_id`
- `captured_at`

Valid `point_status` values:

- `captured`
- `requested_no_data`

Absence means not requested. API errors are range/request failures and do not create point rows.

`content_hash` hashes the original point payload plus normalization version.

### `market_price_coverage_ranges`

Explicit captured range rows.

Primary key:

`PRIMARY KEY (token_id, fidelity_minutes, range_start_ts, range_end_ts, source_version)`

Required columns:

- `token_id`
- `fidelity_minutes`
- `range_start_ts`
- `range_end_ts`
- `source_version`
- `coverage_status`
- `source`
- `api_error_msg`
- `content_hash`
- `first_capture_run_id`
- `captured_at`

Valid `coverage_status` values:

- `fully_captured`
- `partial`
- `failed`

Coverage checks must merge all fully captured ranges overlapping the requested `[A, B]`, detect gaps, and fetch only uncovered intervals. A single enclosing range is not required.

`content_hash` hashes the canonical ordered sequence by `(token_id, fidelity_minutes, ts, source_version)`.

## 10. Price Lookup Requests And Manifests

### `wallet_price_lookup_requests`

Per-wallet analyzer obligations, not shared market data.

Primary key:

`PRIMARY KEY (wallet, capture_run_id, token_id, target_ts, fidelity_minutes, request_reason)`

Required columns:

- `wallet`
- `capture_run_id`
- `token_id`
- `target_ts`
- `fidelity_minutes`
- `request_reason`
- `lookup_status`
- `matched_price_ts`
- `matched_price`
- `match_lag_seconds`
- `match_source_version`
- audit columns

Valid `lookup_status` examples:

- `matched`
- `no_match_in_window`
- `pending`
- `api_error`

### `analysis_price_manifest`

Per-wallet, per-run, per-window record of the exact market price data used.

Primary key:

`PRIMARY KEY (wallet, capture_run_id, window_id, token_id, range_start_ts, range_end_ts)`

Required columns:

- `wallet`
- `capture_run_id`
- `window_id`
- `token_id`
- `fidelity_minutes`
- `range_start_ts`
- `range_end_ts`
- `source_version`
- `content_hash`
- audit columns

Old runs reproduce by loading their manifest and selecting `market_price_points` rows by source version and range. Content hashes must match before analysis.

## 11. Execution Probes

Execution probes are derived/enrichment facts. They compare wallet fills against market price points.

### `wallet_execution_probes`

Primary key:

`PRIMARY KEY (wallet, fill_event_uid, probe_config_hash)`

Required columns:

- `wallet`
- `fill_event_uid`
- `probe_config_hash`
- `probe_uid`
- `probe_version`
- `horizons_seconds_json`
- `reference_rule`
- `condition_id`
- `token_id`
- `fill_ts`
- `fill_price`
- `side`
- `size_tokens`
- `reference_price`
- `reference_ts`
- `probe_status`
- `probe_error_msg`
- `ref_tape` — tape-midpoint reference price from spec §6.2
- `tape_n` — count of foreign trades in the tape window
- `in_play` — fill happened within `IN_PLAY_THRESHOLD_SEC` of resolution
- `thin_market` — `tape_n < TAPE_CONFIDENCE_MIN`
- `resolution_ts_unknown` — resolution timestamp not resolvable at probe time
- audit columns

The five gating-decision fields above are required so a Slice 5
materializer can losslessly re-aggregate persisted probe rows back into
the legacy `compute_execution_profile()` envelope. Without them the
`thin_market_excluded_from_skew` and `in_play_excluded_from_skew` data-
quality counters cannot be reconstructed and ledger-derived H4 output
drifts from live-API output.

`probe_uid = sha256(wallet, fill_event_uid, probe_config_hash)`

`probe_config_hash` is the sha256 of canonical JSON (`sort_keys=True`,
`separators=(",", ":")`) of the following object. Every key listed below is
required; missing or extra keys must produce a different hash so reruns
under different probe semantics never overwrite each other:

```json
{
  "probe_version": "v1",
  "horizons_seconds": [3600, 21600, 86400],
  "reference_rule": "preceding_bar_30min_lookback",
  "reference_lookback_seconds": 1800,
  "min_history_seconds": 300,
  "tolerance_rule": "max(300, offset_sec // 12)",
  "fidelity_minutes": 1,
  "price_source_version": "batch-prices-history-v1"
}
```

`reference_rule = "preceding_bar_30min_lookback"` selects the most recent
bar with `timestamp < fill_ts` within `reference_lookback_seconds` of the
fill. This matches the legacy `_preceding_bar` picker in
`scripts/execution_profile.py` and is preserved here so the ledger probe
reference and the legacy aggregator reference stay equal value-for-value.

`price_source_version` MUST track `market_price_points.source_version`. If
the price source version changes, the probe rerun produces distinct
`probe_config_hash` rows and old rows remain as a historical record rather
than being overwritten.

### `wallet_execution_probe_horizons`

Long format. One row per fill/config/horizon.

Primary key:

`PRIMARY KEY (wallet, fill_event_uid, probe_config_hash, horizon_seconds)`

Required columns:

- `wallet`
- `fill_event_uid`
- `probe_config_hash`
- `horizon_seconds`
- `reference_price`
- `reference_ts`
- `target_ts`
- `target_price`
- `drift`
- `return_pct`
- `horizon_status`
- audit columns

`return_pct = drift / reference_price` only when `reference_price > 0`. Otherwise `return_pct=NULL` and `horizon_status='invalid_reference'`.

Failures are retained. They are data quality facts.

## 12. Reconstructed Flips

`wallet_reconstructed_flips` is interpretation/derived data, not observed fact.

Primary key:

`PRIMARY KEY (wallet, flip_uid)`

`flip_uid = sha256(wallet, reconstruction_method, reconstruction_version, buy_event_uid, sell_event_uid, matched_size_tokens)`

Required columns:

- `wallet`
- `flip_uid`
- `condition_id`
- `token_id`
- `outcome_index`
- `outcome_label`
- `buy_event_uid`
- `sell_event_uid`
- `buy_ts`
- `sell_ts`
- `buy_price`
- `sell_price`
- `size_tokens`
- `buy_notional_collateral`
- `sell_notional_collateral`
- `buy_collateral_token`
- `sell_collateral_token`
- `flip_roi`
- `hold_duration_seconds`
- `flip_pnl_collateral`
- `flip_pnl_usd_equivalent`
- `collateral_conversion_assumption`
- `derivation_check_status`
- `reconstruction_method`
- `reconstruction_version`
- `input_event_uids_json`
- audit columns

If buy and sell collateral tokens differ, `flip_pnl_collateral=NULL`. `flip_pnl_usd_equivalent` is always populated using the recorded collateral conversion assumption.

`derivation_check_status` is:

- `ok`
- `drift`
- `unverified`

### FIFO ordering and dedup

Flip reconstruction reads from `wallet_events` (not from raw activity rows
and not by recomputing `event_uid`). The reader contract is:

- Read only rows with `ingest_status = 'normalized'`.
- Group by `(condition_id, token_id)`. The group key is NEVER the outcome
  label string — multi-outcome markets and label collisions make outcome
  text unsafe.
- Inside each group, FIFO order is `ORDER BY ts ASC, event_row_ordinal ASC,
  event_uid ASC`. Tiebreakers in this order are required so the reconstruction
  is deterministic across capture reruns and across sources whose timestamps
  collide on the same wallet/condition/token.
- Dedup by `canonical_event_hash`: never count two rows with the same
  hash. When a single canonical event has more than one source row in the
  ledger (`dedup_status = 'ambiguous'`), the entire flip whose buy or sell
  side touches that ambiguous group is written with
  `derivation_check_status = 'unverified'`. Do not silently pick one row.

## 13. Collateral Semantics

Polymarket CLOB V2 is live as of 2026-04-28 and replaces USDC.e collateral with pUSD. Money columns must be collateral-neutral.

Use:

- `collateral_amount`
- `fee_collateral`
- `collateral_token`
- `collateral_token_address`
- `collateral_display_unit`
- `usd_equivalent_amount`

Historical rows may use `USDC.e`; post-migration rows may use `pUSD`. The economic 1:1 assumption is explicit and auditable, not implicit in a `usdc_*` column name.

## 14. Materialization And Protocol Guards

`materialize_wallet_data(wallet, window)` reads ledger facts and returns a `WalletData` compatible object for existing protocol code.

Materialization must:

- Use only rows in the selected analysis window.
- Read market outcome snapshots, not live canonical dimensions.
- Populate `replay_surface_meta` with ledger source, window id, source versions, and manifest hashes.
- Preserve `None` vs empty-captured surface semantics.
- Provide price-history closures from `market_price_points`, not live CLOB calls.
- Provide execution profile from `wallet_execution_probes`, not live per-fill probes.

Protocol guards must fail closed at the hypothesis level:

- Binary-only hypotheses reject non-binary or unresolved outcome-index rows with a clear limitation.
- Token-price drift hypotheses may run on named outcomes when they only require token price history.
- Unsupported market structure does not invalidate the whole wallet unless every active hypothesis depends on unsupported structure.

## 15. Supabase Outputs

### `wallet_analysis_results`

One row per `(wallet, run_id, window_id)`.

Store:

- full `ProtocolReport` JSON
- indexed summary columns: verdict, gate result, trading mode, primary edge type, sample sizes, key confidence flags
- artifact hashes
- local warehouse path/pointer
- schema and code versions

### `wallet_findings`

Structured numeric findings. These are the only default LLM input in Phase 2D.

Store:

- finding id
- wallet
- run id
- window id
- finding type
- severity/confidence
- numeric evidence JSON
- source hypothesis/result ids
- recommended next action class
- created_at

Every completed analysis writes findings, even if the finding is "no edge", "data insufficient", or "unsupported market structure".

### `artifact_registry`

Store paths and content hashes for:

- wallet DuckDB files
- market data warehouse snapshots
- fixture exports
- CSV/Parquet exports
- reports

## 16. Fixture Exports

Fixture v2 remains useful, but only as an export/replay format.

Export rules:

- Exports are produced from ledger materialization.
- Exports include window metadata and source manifests.
- Exports never fetch live data.
- Replay remains no-live-fallback.

Calibration fixtures are golden replay inputs derived from the ledger. Live Supabase regression is a separate suite.

## 17. Implementation Phases

### Slice 1: Ledger Foundation

- Use the existing DuckDB dependency and add one import boundary module.
- Create warehouse path/config helpers.
- Add schema creation for local wallet, market, and index warehouses.
- Add tests for schema, primary keys, and audit columns.

### Slice 2: Capture And Resume

- Implement `capture_runs` and `capture_shards`.
- Capture activity and positions into the ledger.
- Capture market dimensions into Supabase canonical and local snapshots.
- Prove interrupted capture resumes without repeating completed shards.

### Slice 3: Price Data

- Add `polymarket_api` batch price-history helper.
- Implement shared `market_price_points`, coverage range merge, lookup requests, and analysis manifests.
- Prove no single-asset price loop remains in ledger path.

### Slice 4: Execution And Flips

- Refactor execution profile code to expose per-fill probe rows.
- Store probe meta and horizon rows.
- Store reconstructed flips with versioned method hash.

### Slice 5: Materialization

- Implement `materialize_wallet_data`.
- Wire protocol to run from ledger-derived windows.
- Add fail-closed guards for unsupported market structure.

### Slice 6: Supabase Outputs And Calibration

- Persist results/findings/artifacts.
- Export fixture v2 from ledger materialization.
- Re-baseline 10 calibration wallets.

## 18. Verification

Required checks:

- Unit tests for every schema creation helper.
- Unit tests for dedupe keys and ambiguous-source retention.
- Unit tests for market outcome snapshot physical copy and no `SELECT *`.
- Unit tests for interval-merge coverage detection.
- Unit tests for no `api_error` point rows.
- Unit tests for mixed-collateral flip handling.
- Unit tests for non-binary fail-closed at hypothesis level.
- Resume test: interrupted wallet capture resumes and does not repeat completed immutable shards.
- Materialization parity test: binary fixture output matches current protocol results for equivalent input.
- Supabase write tests gated by live-only env variables.

## 19. Source Notes

- Polymarket API surfaces are split across Gamma, Data API, and CLOB.
- CLOB V2 is live on production as of 2026-04-28 and uses pUSD collateral.
- `/batch-prices-history` returns historical price data for up to 20 market asset ids in a single request.
- `/prices-history` and `/batch-prices-history` return timestamp/price points, not OHLCV bars.

# HL Local Research Bootstrap Design

## Purpose

Make the Hyperliquid research workflow runnable from this repo on a laptop without
depending on the main app database or on undocumented VPS state.

The local workflow is for repeatable research iteration:

- rebuild funding, price, and OI history into a dedicated SQLite DB
- build the hourly research dataset
- run the audited analysis scripts against that DB
- save artifacts locally for comparison and follow-up

It is not intended to prove exact equivalence to the VPS unless the same DB snapshot is
copied down and used as input.

## Why This Matters

The current repo state has the research code but not the research dataset. That means:

- audits happen against a remote artifact that is not present locally
- fixes to the research contract cannot be validated end-to-end on this machine
- future agents inherit prose conclusions without a runnable local rebuild path

That is tolerable for one audit and bad for ongoing research.

## Goals

1. Use a dedicated local research database.

- Default path: `data/hl_research.db`
- Do not write research tables into `sniper_tracker.db`

2. Provide one bootstrap entrypoint.

- Command: `python3 scripts/run_hl_research_local.py`
- Runs the existing backfill and research scripts in a stable order

3. Keep outputs inspectable.

- Default artifact directory: `output/hl_research_local/`
- Store report text and JSON artifacts there

4. Support strict and loose OI modes.

- Loose mode: allow snapshot fallback when Coinalyze is unavailable
- Strict mode: `--require-coinalyze` for audited reruns

## Non-Goals

- Replacing the VPS workflow
- Writing research results to Supabase automatically
- Guaranteeing byte-for-byte equivalence with prior VPS studies
- Solving the PM-native backtest in the same step

## Workflow

### Database

Use a dedicated SQLite file:

- `data/hl_research.db`

This isolates research tables from runtime tables and makes reset/rebuild safe.

### Step Order

1. `funding`
   - `scripts/backfill_hl_funding.py`
2. `price`
   - `scripts/backfill_binance_klines.py`
3. `oi`
   - `scripts/backfill_oi_history.py`
4. `dataset`
   - `scripts/build_research_dataset.py`
5. `v2`
   - `scripts/hl_research_v2.py`
6. `combo`
   - `scripts/run_signal_research.py`
7. `regression`
   - `scripts/hl_regression_analysis.py`

### Outputs

- `output/hl_research_local/manifest.json`
- `output/hl_research_local/hl_research_v2.txt`
- `output/hl_research_local/signal_research_summary.txt`
- `output/hl_research_local/signal_research_results.json`
- `output/hl_research_local/hl_regression_analysis.txt`

## Interface Requirements

The bootstrap script should support:

- `--db`
- `--output-dir`
- `--months`
- `--assets`
- `--steps`
- `--reset-db`
- `--dry-run`
- `--require-coinalyze`

The analysis scripts should accept `--db` so they can be pointed at the dedicated local
research DB instead of relying on `DB_PATH`.

## OI Contract

The local bootstrap must preserve the audited OI contract:

- request Coinalyze OI with `convert_to_usd=true`
- store `oi_units` and `source_detail`
- only use `usd_notional` rows in research
- never compute OI deltas across a source handoff

## Expected Benefit

### Immediate

- lets this repo validate OI contract changes end-to-end
- lets future audits rerun locally without guessing at the VPS state

### Ongoing

- makes alternative research sweeps cheap
- keeps agent reasoning grounded in runnable code instead of terminal history
- reduces dependence on the VPS for every serious HL research change

## Risk Notes

- local rebuilds are not identical to historical VPS runs unless the same raw dataset is
  used
- Coinalyze availability and API keys remain an external dependency
- snapshot fallback is acceptable for exploratory local work, but strict audit reruns
  should use `--require-coinalyze`


# Ledger calibration fixtures (Slice 6 Task 8)

This directory holds **ledger-materialized** envelopes for the 10
calibration wallets pinned in `tests/calibration/labels.yaml`. Each
envelope is the JSON output of
`sniper.analysis.wallet_protocol.ledger.fixture_export.export_calibration_fixture`
run against a seeded wallet warehouse.

Naming convention:
`<wallet>_2026-04-30.json` (mirrors the live-API fixture filenames).

## Why this directory may be empty

Slice 6 lands the runner
(`tests/calibration/test_ledger_calibration_set.py`) and the exporter,
but it does NOT seed the wallet warehouses. Live capture is operator-
supervised work and sits outside Slice 6 (D4: live-capture path is a
separate runbook, not a Slice 6 task).

When the envelopes are missing, every Axis A / Axis B parametrised
case skips with `fixture_missing` and a clear reason — that is the
documented expected state until the seeding step runs.

## How to seed an envelope (operator workflow)

For each wallet in `labels.yaml`:

1. Run the Slice 1 / Slice 2 / Slice 3 capture pipeline against the
   live Polymarket APIs to populate the wallet's DuckDB warehouse.
2. Materialize:
   ```python
   from pathlib import Path
   from sniper.analysis.wallet_protocol.ledger.materialize import (
       materialize_wallet_data,
   )
   from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow
   from sniper.analysis.wallet_protocol.ledger.fixture_export import (
       export_calibration_fixture,
   )

   wallet = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"
   window = AnalysisWindow(
       window_id="calibration_2026_04_30",
       capture_run_ids=("<run-id-from-capture>",),
       start_ts=...,
       end_ts=...,
       source_versions=(("price_v", "v1"),),
   )
   bundle = materialize_wallet_data(
       wallet=wallet,
       window=window,
       ledger_root=Path("/path/to/ledger/root"),
   )
   export_calibration_fixture(
       bundle,
       Path("tests/calibration/fixtures_ledger") / f"{wallet}_2026-04-30.json",
   )
   ```
3. Commit the envelope plus the trimmed `<wallet>_2026-04-30.duckdb`
   warehouse snapshot alongside it. Keep the snapshot small — only the
   rows the analysis window touches.
4. Re-run `pytest tests/calibration/test_ledger_calibration_set.py` —
   the parametrised cases for the seeded wallets stop skipping.

## Divergence handling

If a wallet's ledger verdict differs from `labels.yaml`'s `expected`
(Axis B) or from the live-fixture report (Axis A), append the row to
`divergences.md` with:

* Wallet
* Axis (A or B)
* Diverging field
* Live / yaml value
* Ledger value
* One-sentence hypothesis

DO NOT silently update `labels.yaml`. DO NOT silently update the live
fixture. Mark the test `pytest.mark.skip` with a reason citing
`divergences.md` and leave the rest for operator review.

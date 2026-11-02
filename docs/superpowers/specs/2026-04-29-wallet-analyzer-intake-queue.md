# Wallet Analyzer Intake Queue

Status: implementation note for Phase 3 progressive runner.

## Decision

The new wallet analyzer uses `wallet_analyzer_intake_queue`.

`wallet_analysis_queue` is legacy-only. It belongs to the old discovery /
low-buy / `analyze_users.py` pipeline and must not be used as intake for the
new wallet analyzer.

## Clean Intake Sources

`scripts/run_wallet_protocol_batch.py` may read wallets only from:

- explicit `--wallet`
- explicit `--wallet-file`
- explicit `--from-queue`, where queue means `wallet_analyzer_intake_queue`

There is no implicit fallback to low buys, `discovery_events`,
`candidate_users`, followed wallets, watchlists, old `wallet_analysis_queue`,
or `analyze_users.py`.

## Guardrails

`backfill_polymarket_low_buys.py` is **retired, no override**. The script
writes the legacy `low_buy_trades` table that fed the old discovery / insider-
score pipeline. Its `main()` returns exit code 2 unconditionally and never
opens the DB or instantiates `BackfillRunner`. There is no flag, env var, or
runtime condition that re-arms it. Tests in
`tests/test_backfill_polymarket_low_buys_guard.py` lock this behavior.

If historical low-buy data is genuinely needed, restore from a DB backup or
query the archive — do not regenerate via this path.

## Legacy Classification

| Area | Classification | Reason |
|---|---:|---|
| `stream_low_buys.py` | retire | Old active stream; must not feed analyzer workflow. |
| `backfill_polymarket_low_buys.py` | retire | Backfills old source. |
| `low_buy_trades` | isolate | Historical/debug only; not analyzer intake. |
| `wallet_analysis_queue` | isolate | Contaminated by old discovery / `analyze_users.py` pipeline. |
| `discovery_events` | isolate | Contains `low_buy`; cannot be trusted as analyzer intake. |
| `db_discovery.py` | isolate / repurpose later | Queue mechanics may be useful, but tied to old states and discovery scoring. |
| `analyze_users.py` | retire | Legacy scoring core. |
| `candidate_users` | isolate | Old discovery staging. |
| `sniper/persistence/db_leaderboards.py` low-buy fields | retire from primary UI | Derived from old signal. |
| `routes/api_core.py` recent low-buy endpoints | isolate | Historical surface only. |
| `health_check.py` low-buy freshness | retire / replace | Should not define new analyzer health. |
| `market_outcome_poller.py` low-buy-driven backfill | unknown / needs review | May still be useful for historical market outcome fill, but not as analyzer source. |
| `feeds/polymarket_trade_tap.py` | repurpose later | Possible clean raw feed later if it stays separated from `stream_low_buys.py`. |
| `scripts/run_wallet_protocol_batch.py` | keep, modify | Owns progressive analyzer execution. |
| `wallet_analysis_batch*` | keep | Analyzer-owned batch/result tracking. |

## Separation

Discovery finds candidate wallets and inserts them into
`wallet_analyzer_intake_queue`.

The progressive runner claims intake rows, creates `wallet_analysis_batch_item`
rows, analyzes one wallet at a time, writes summaries immediately, and mirrors
final outcome back to the intake queue.

The analyzer itself remains source-agnostic. Source metadata is stored as
`source`, `source_ref`, and `metadata_json`; it does not control analyzer logic.

Each wallet may be bounded with `--per-wallet-timeout-s`. A timeout marks only
that wallet failed and the batch continues to the next wallet.

## Future Discovery Job

A future scheduled discovery job may insert roughly 10 candidate wallets per
run into `wallet_analyzer_intake_queue`.

That job must:

- avoid duplicate active queue rows,
- record `source='discovery_agent'`,
- record source-specific details in `source_ref` and `metadata_json`,
- not call `run_wallet_protocol.py` directly,
- not read from low buys, `discovery_events`, or old `wallet_analysis_queue`
  unless that source is first redesigned and explicitly approved as analyzer
  compatible.

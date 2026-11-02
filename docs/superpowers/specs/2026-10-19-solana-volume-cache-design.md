# Solana volume cache gate

Operator request: build a background cache; reject only fresh complete coverage; unknown passes; no entry RPC/DB reads; prove p50/p90 before activation. Operator threshold: five-minute organic volume >= 2% of effective market cap.

Default OFF. No deployment, restart, runtime setting or trading-policy activation in this build.

## Contract

A bounded background reader publishes immutable snapshots keyed by (mint, venue, market_pool), where a curve uses its mint as market_pool and PumpSwap uses its on-chain pool address. The entry path performs a lock-free snapshot lookup, validates freshness and complete 300-second event-time coverage, subtracts the exact copied signature if present, converts supported quote atomics with the already-held SOL/USD price, and compares against 0.02 * effective market cap before reservation. Unknown passes. Both native and ordinary Solana entry paths consume the same cache. Volume refusals remain policy refusals and cannot authorize fallback. Existing ordinary volume controls remain authoritative.

The cache never uses the legacy pool_ticks aggregate as proof: its timestamps are collection time and net per-transaction deltas are not gross swap volume. A separate versioned producer receipt and coverage table in the existing Solana tape DB records verified per-swap quote atomics, case-sensitive signatures, event indices, makers and actual block times from transactions already fetched by the pull worker. No extra RPC calls. Start coverage only at a clean successful head check after this code is installed; retain it only through complete contiguous clean passes. Errors, partial enumeration/decode, malformed/truncated logs, unsupported events/quotes, floor resets or identity changes invalidate coverage. A full clean 300-second interval must accumulate before any rejection; legacy rows never become proof automatically.

Initial exact event support is native-SOL Pump curves and PumpSwap events where emitter, pool and quote binding can be proven. Other venues/quote assets remain explicitly unknown. Token reserve changes alone never prove traded volume. Own-wallet swaps are excluded in background; missing own identity means unknown. Own event actors and funded fee payers are both excluded, including routed swaps whose event actor is a router. Source transaction subtraction is exact from amounts actually in the frozen window. The frozen window ends at the successful poll start, with max age 5 seconds; it does not pretend to cover the later tail up to entry.

## Edge cases and limits

- Money: admission only; no changes to sizing, contracts, signing or exits.
- Speed: no entry RPC, SQLite, wait, retry or new lock. Budget: enabled check p90 <20 microseconds, measured with p50/p90 for miss/stale/allow/refuse and concurrent publication. Activation remains OFF even after benchmark.
- Threads: one background writer publishes immutable references; readers never mutate or lock. Bound pool/transaction work and keep collection off funded providers.
- Missing vs zero: fully covered zero can reject; absent/stale/gapped/malformed/wrong-pool/unpriced is unknown and passes.
- Failure: background errors clear publication; stale snapshots expire independently if the worker dies. Reconnects/backlog/floor resets cannot preserve eligibility without a clean proof. A successful fully decoded contiguous catch-up may preserve history; normal polling cadence is not itself a gap.
- Identity and units: case-sensitive Solana keys/signatures; exact mint/pool/quote binding; TEXT atomic quantities; chain block seconds; monotonic reader expiry. Migrations cannot reuse a curve snapshot for an AMM.
- Sample: only pools already covered by the existing pull tape; this does not add universal pre-entry discovery or improve missing feed coverage. No new research RPC demand.
- Rollback: default-OFF gate flag; stopping reader expires snapshots; legacy tape/exit semantics are unchanged. Partial installs fail open.

## Interfaces

Persistence in sniper/persistence/db_sol_volume.py: ensure_tables(conn); coverage_rows(conn, *, now, limit=256) -> list[dict]; swap_rows(conn, pool, *, since, through) -> list[dict]. Rows use pool=coin-vault key; coverage rows expose mint, venue, market_pool, quote_mint, verified_since, through, complete, reason. Swap rows expose signature, event_index, block_ts, quote_atomic (decimal TEXT), maker, payer. Read functions never create schema.

Producer in sniper/services/memecoin_solana_volume_evidence.py: begin_pass(conn, mint, vault, now_iso, *, reset=False) -> token; finish_pass(conn, token, *, transactions, complete) writes receipt/coverage using already fetched transactions and cursor-completeness result. Every pass invalidates persisted eligibility before doing network work; only a successful complete validation republishes it. Failed calls leave it invalid. No exceptions escape into existing tape capture; helper failure leaves unknown.

Cache in sniper/services/memecoin_solana_volume_cache.py: VolumeCache(max_age_s=5.0, min_ratio=0.02, clock=time.monotonic); refresh(conn, *, now, own_owners, published_mono=None); check(*, mint, venue, market_pool, source_signature, mcap_usd, sol_usd, now_mono=None) -> immutable VolumeDecision(state, reason, volume_usd, required_usd), state in unknown/allow/refuse. start_volume_cache(*, own_owners, connect=None, clock=time.monotonic) -> cache or None starts only if cfg.MEMECOIN_SOLANA_VOLUME_GATE_ENABLED (default False). Worker owns its read-only connection and publishes status. Snapshot window=300s.

## Validation and activation

Default remains `MEMECOIN_SOLANA_VOLUME_GATE_ENABLED=0`; ratio defaults to `0.02`, max age to `5`. No runtime settings were changed. Activation requires the normal separately authorized pinned deployment. This build does not claim universal pre-entry coverage or a strategy improvement.

The added fallback-policy check reads the existing decision registry through a lock-free immutable tuple accessor. Registry replacement uses assignment plus OrderedDict recency refresh so a concurrent read never sees a temporary removed key. Existing funded-owner/in-flight precedence remains authoritative.

See `docs/audits/2026-10-19-solana-volume-cache.md` and the checked-in latency JSON for RED/GREEN, benchmark and existing baseline test limitations.

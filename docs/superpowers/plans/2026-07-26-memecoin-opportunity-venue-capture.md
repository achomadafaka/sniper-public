# Memecoin Opportunity Venue Capture Plan

**Goal:** Decode and durably capture Pump.fun/Solana, Bags/Meteora DBC/Solana, and Bags/Robinhood 4663 pre-migration launch events without signing or broadcasting.

**Architecture:** Pure, versioned decoders live in `sniper.research.memecoin_opportunity`. Network transports and RPC recovery live in `sniper.services`. Each adapter emits the existing `RawEvent` and `CanonicalEvent` contracts. Unknown program/contract versions and incomplete recovery open a source gap and block directional output.

**Constraints:** Default OFF; no live calls in tests; no funded/V6 imports; injected transports; immutable event identity; EVM identities lowercase; Solana identities preserved.

## Tasks

1. Add fixture-driven Pump.fun decoding for create, buy/sell trade, complete/migration, including current v2 instruction/event variants and explicit unsupported-version output.
2. Add fixture-driven Meteora DBC decoding for pool creation, swap, and migration using the official DBC program identity and versioned account/event maps.
3. Add ABI-word decoding for Bags Robinhood `TokenCreated`, `TokensBought`, `TokensSold`, `Migrated`, and `ClaimersUpdated`; pin official proxy/singleton identities and treat beacon/proxy upgrades as a version interrupt.
4. Add Solana program-log and 4663 log source adapters with injected RPC/WebSocket clients, bounded queues, deterministic source cursors, confirmation reconciliation, and recovery pagination that never advances across a gap.
5. Persist raw envelopes before decoding, append canonical corrections for reorg/orphan events, and expose per-source health facts.
6. Verify fixture decoding, ordering/dedupe, unsupported versions, reorgs, bounded recovery, and package import boundaries.

## Verification

```bash
python3 -m pytest tests/test_memecoin_opportunity_decoders.py tests/test_memecoin_opportunity_sources.py tests/test_memecoin_opportunity_live_sources.py tests/test_memecoin_opportunity_frontier.py -q
python3 -m pytest tests/test_memecoin_opportunity_boundaries.py -q
```

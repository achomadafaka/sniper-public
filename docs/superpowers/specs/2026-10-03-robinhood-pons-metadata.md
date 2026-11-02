# Robinhood Pons metadata recovery

SPORE ref `530c00dd` had source market cap zero and no attributed Pons venue. Its successful source receipt names curve `0xbdb19132df796efc36f7f893b8231d113e68ca6a`; `token()` and the registered factory's `info(token)` agree on SPORE. The counter is ERC-20 `0xaa07a0e9209e16ac99708c3ec70159c6ef3128a3`, not native ETH. Current `totalSupply()` is 10^27 atomic units at 18 decimals. Recorded source USD price times that supply is $15,952.47; this is a current-supply calculation, not an assertion of historical supply.

## Contract

- Warm metadata and supply on launch before the first swap, and recover curves from retained `pons_v2_swap` ticks even when their launch event was missed. A launch event alone proves the token and curve, not the counter currency.
- Verify the curve's factory against `MEMECOIN_EVM_V4_LAUNCHPADS`, then require factory `info(token)` to agree on both token and curve before accepting its counter. ABI addresses require canonical zero padding.
- Use actual counter currency and decimals for quote-side USD volume. Missing counter decimals or USD price remains unpriced, never native by assumption.
- Run contract reads on the volume observer's existing resolver thread with per-family private sockets and retry clocks. Bound each pass to 16 curves. No funded RPC, synchronous retry, or shared mutable connection is added to entry.
- Store supply as atomic decimal TEXT in a separate `volume_token_supply` table. Refresh after 300 seconds for retained active curves; entry accepts it for at most 600 seconds. Invalid, missing, future, or stale rows remain unknown.
- Robinhood's existing unresolved-mcap branch first tries source USD fill price times fresh local cached supply, with basis `evm_source_cached`. Existing cap, floor, fallback, and frozen-contract invariants remain authoritative. DexScreener still does not set blind-chain bands.
- 2026-10-09 OP amendment: an otherwise admitted Robinhood event/history cap also checks that same fresh cached supply calculation against its effective ceiling. A lower feed cap cannot bypass an on-chain ceiling refusal. This check only adds ceiling refusals; successful entries keep their original band, floor, sizing and contract. Unknown cache data keeps the existing decision. Explicit scalp admission and wallet ceilings remain authoritative; the existing big-cap lift evaluates the verified cap, not the lower feed value.
- Read the indexed cache with read-only SQLite and zero busy timeout; preserve typed failure and measured lookup time in the attempt trace.
- Provider rejection, transport failure, malformed reply, identity mismatch, and partial replies never establish verified metadata. Retry unknown curves after 30 seconds. No database transaction spans an RPC call.
- Additive side-store schema remains compatible with rollback. Never rewrite an existing funded contract or replay the refused buy.

## Limits

Expired tape history is not reconstructed. Spoofed/unregistered venues stay unknown. Supply changes between refreshes are not detected immediately. Total supply times source price is the project's existing supply-based market-cap convention, not independently measured circulating supply. The new cache helps coins already observed on Pons; other missing venues require their own verified attribution.

## Acceptance

Behavioral tests must fail before implementation, then prove ERC-20 and native counters, missed launch recovery, identity rejection, transient and partial failures, stale supply, exact atomic storage, entry fallback/cap preservation, and no RPC on the new entry path. Benchmark local lookup p50/p90. Deploy the exact merged SHA with `--defer-backup`, verify runner/web releases and observer process separately, then inspect the live side-store and authenticated status. Organic traces are required before claiming improved live fill latency.

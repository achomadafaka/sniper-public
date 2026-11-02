# Memecoin 0x Retirement Design

**Date:** 2026-07-16
**Status:** Implemented on `fix/memecoin-retire-zerox`
**Scope:** Active memecoin pricing, capture, token-safety, execution, readiness, and configuration paths

## Problem

The funded runtime still contains an active 0x integration even though the configured API key is unusable. Current evidence is internally inconsistent:

- Robinhood chain `4663` executes through the verified direct V3/V2 routes and prices through GeckoTerminal.
- The blind-chain price and capture paths still call 0x after a GeckoTerminal miss.
- Base `8453` has an RPC and is advertised as an armed context, but its configured execution venue is still 0x. It has no real positions or fills.
- Ethereum `1` and BSC `56` default to 0x but have no configured RPC, so they do not produce funded contexts.
- Historical `source='zerox'` rows remain valid provenance and must not be rewritten.

This creates failed network calls, misleading readiness, and a false claim of Base coverage. Keeping a dead provider as a default is worse than explicitly reporting the chain unsupported.

## Decision

Retire 0x from every active runtime path. The EVM execution venue becomes either `direct` or `disabled`; there is no provider fallback and no implicit 0x default.

Robinhood `4663` remains funded through its existing direct routes. Base, BSC, and Ethereum remain observable through their existing feeds and capture sources, but funded execution is disabled until a separate, chain-specific direct-routing design proves an executable buy and sell path.

Historical 0x provenance remains readable. Retirement does not delete or rename database rows, schema values, old specs, or fixtures whose purpose is to verify historical display.

## Alternatives Rejected

### Delete everything immediately and leave Base context construction unchanged

Rejected. The context would appear armed while its quote function could never succeed.

### Keep 0x as a dormant fallback

Rejected. The key is known unusable, fallback calls add latency and failure noise, and dormant defaults silently reactivate if configuration drifts.

### Build Base, BSC, and Ethereum direct routing in this branch

Rejected. Those chains need separate route discovery, verified router/quoter addresses, multi-hop policy, quote-token handling, and sell-path evidence. Guessing those contracts inside a provider-removal change would weaken funded execution correctness.

## Runtime Contract

### Venue selection

- `MEMECOIN_EVM_EXEC_VENUE_<network>` accepts `direct` or `disabled` only.
- Missing configuration defaults to `disabled`.
- An unknown value fails closed and produces no funded context.
- A `direct` context requires an RPC, keypair, quote-token metadata, and verified direct router/quoter configuration for that exact network.
- Network identity remains explicit. No address-shape chain inference is introduced.

### Robinhood chain 4663

- Entry and pre-entry sell-route checks continue through direct V3 with the existing V2 fallback where configured.
- Exits continue through the same direct routes.
- Pricing and blind capture use GeckoTerminal only.
- A GeckoTerminal miss remains honestly unpriced; it does not trigger a 0x request.

### Base, BSC, and Ethereum

- Market observation remains unchanged where DexScreener or existing non-0x sources cover the chain.
- Funded execution returns no context while the venue is `disabled` or direct contracts are missing.
- Operator/UI readiness must show the chain as unsupported or disabled, not configured.
- Re-enabling funded execution requires a separate reviewed direct-routing spec and tests for both entry and exit.

## Code Changes

Remove active 0x ownership from:

- `sniper/execution/evm_real_seams.py`: delete the 0x endpoint and quote client; bind the executor only to direct routes.
- `sniper/execution/evm_token_safety.py`: require an injected executable direct quote; remove the 0x fallback and API-key parameter.
- `sniper/services/memecoin_evm_leg.py`: refuse context construction unless the venue is supported and direct.
- `sniper/services/memecoin_evm_price.py`: GeckoTerminal-only blind-chain pricing; remove 0x quote fallback.
- `sniper/services/memecoin_capture_v2.py`: GeckoTerminal-only blind capture; no second provider call after a miss.
- `sniper/settings/memecoin.py`: remove `MEMECOIN_ZEROX_API_KEY`; default EVM venues to `disabled`.
- `scripts/memecoin_evm_smoke_test.py`: validate direct configuration and direct quotes only.
- API/readiness surfaces: remove 0x-key readiness and report the actual venue/configuration state.

Active tests and comments must use provider-neutral terms such as `direct`, `probe`, or `historical_zerox` as appropriate. Historical database-display fixtures may retain `zerox` because changing them would falsify provenance.

## Data and Compatibility

No schema migration is required.

- Existing `memecoin_position_marks.source='zerox'` and `memecoin_quote_observations_v2.dex_id='zerox'` rows remain unchanged.
- Readers continue accepting arbitrary source strings.
- No new writes use `zerox` as a source, route label, pair address, or readiness field.
- Environment cleanup removes `MEMECOIN_ZEROX_API_KEY` from the runtime state after the merged release no longer imports it.

## Failure Semantics

- Missing direct venue: no context, no reservation, no transaction.
- Missing GeckoTerminal price: `NULL`/unpriced capture result, not zero and not fabricated fallback data.
- Direct quote or sell-route failure: preserve the existing deterministic `route_failed`/token-safety terminal reason.
- Historical source display: unchanged.

No balances, quotes, safety verdicts, kill-switch state, or capital values are cached as static truth.

## Testing

Tests must prove:

1. No active module references the 0x endpoint, API-key setting, or quote function.
2. `4663` builds a direct context and never calls a provider fallback after a GeckoTerminal miss.
3. `8453`, `56`, and `1` do not build funded contexts without explicit supported direct configuration.
4. Unknown venue values fail closed.
5. Token safety cannot silently construct a 0x route.
6. Capture writes no new `dex_id='zerox'` rows from active pricing.
7. Historical `zerox` marks still render with their original provenance.
8. Concurrent reservation, duplicate-submit, chain identity, and exit tests remain green.

Required validation:

- focused EVM seams, safety, price, capture, API/readiness, and runtime tests;
- `python3 scripts/validate_schema_refs.py`;
- full `tests/test_memecoin*.py`;
- `python3 -m compileall -q sniper scripts`;
- `node --check static/app.js` if the readiness payload changes UI code;
- `git diff --check`.

## Deployment

The implementation lands as a feature branch with one logical change per commit. After merge, deploy the exact merged `main` SHA through `scripts/deploy_memecoin_runtime.sh`.

Before restart, remove the obsolete 0x key from the state environment without printing it. Verify after restart:

- one pinned healthy runner;
- `4663` context present and direct;
- Base/BSC/Ethereum absent from funded contexts until direct support exists;
- no outbound `api.0x.org` references in the deployed release;
- GeckoTerminal capture continues with honest missing rows on misses.

## Follow-up Boundary

Direct Base, BSC, and Ethereum execution is a separate project. Each chain must define and verify router/quoter ownership, quote-token and multi-hop policy, entry and exit parity, token-safety simulation, and exact attribution before being funded. This retirement must not guess those details.

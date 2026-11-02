# Memecoin Solana RPC Credit Rotation Design

**Date:** 2026-07-29
**Status:** Approved by operator directive

## Goal

Keep funded Solana reads and same-bytes submission available when one Helius free-tier project
exhausts its monthly credits. Use three operator-owned Helius RPC URLs in order, then the existing
keyless `api.mainnet-beta.solana.com` endpoint as the final fallback.

This changes provider availability only. It does not change admission, sizing, quotes, transaction
bytes, claims, confirmation, reconciliation, or funded authority.

## Configuration

- `MEMECOIN_SOLANA_RPC_POOL_URLS` contains exactly three distinct HTTPS RPC URLs, comma-separated.
- Existing `MEMECOIN_SOLANA_RPC_URL` remains required and is appended as the final fallback.
- When the pool variable is absent, the existing single-RPC behavior is unchanged.
- Pool URLs remain environment-only and must never appear in logs, status, DB rows, or manifests.
- A configured pool with the wrong count, a duplicate, a non-HTTPS URL, or no final fallback fails
  startup validation.

The code consumes credentials only. It does not create provider accounts, projects, or API keys.

## Rotation Contract

One process-local, thread-safe HTTP callable owns the active index.

1. Every caller sends through the current endpoint.
2. Only a JSON-RPC error whose exact numeric code is `-32429` marks that endpoint credit-exhausted.
3. The identical method, parameters, and signed bytes are retried on the next endpoint.
4. HTTP 429, 503, timeout, malformed response, and every other JSON-RPC error do not rotate.
5. The final public endpoint is subject to the same fail-closed caller behavior; there is no hidden
   endpoint after it.
6. A quota-refused `sendTransaction` may retry unchanged signed bytes because `-32429` is an explicit
   provider refusal before method execution. An ambiguous send never retries through this mechanism.
7. Exhausted endpoints receive a read-only mainnet genesis probe after a one-hour cooldown. A correct
   genesis response restores that endpoint, allowing monthly credit refill without a process restart.

Concurrent callers cannot skip two keys for one exhausted response: advancement compares the index
that produced the refusal with the current active index under a lock.

## Wiring

The same rotating callable is passed to:

- Solana wallet and token-balance reads;
- exact full-balance exit previews;
- entry and exit submission;
- signature confirmation and recovery;
- custody identity validation and keep-alive warming.

Observation-only tape and read RPCs remain on `MEMECOIN_SOLANA_READ_RPC_URL` /
`MEMECOIN_SOL_TAPE_RPC_URL`. The Solana arrival WebSocket keeps the existing keyless fallback.
TPU reads default to that fallback; the isolated read-source exception below permits separate
HTTP+WS read credentials and capacity without consuming funded-RPC quotas.

## Observability

Rotation logs only `from_index`, `to_index`, pool size, and reason `credit_exhausted`. When the final
fallback becomes active the runner emits a bounded high-severity log. A sanitized snapshot exposes
active index, endpoint count, exhausted count, and fallback-active state; never URLs or keys.

## Tests

- Parse exactly three distinct HTTPS pool URLs and append the existing fallback.
- Preserve legacy single-URL behavior when the pool is absent.
- Rotate and retry the identical request only on `-32429`.
- Do not rotate on transport, HTTP, malformed, or other JSON-RPC failures.
- Concurrent exhaustion advances once.
- Read-only cooldown probe restores a refilled key.
- Real-context wiring uses the shared rotating callable and keeps sidecar resolution on the fallback.
- Status and logs contain no URL or credential material.

## Deployment

Code may merge and deploy before the two missing keys exist because the absent pool variable preserves
the current public-RPC behavior. Activation is a separate atomic environment change after all three
independent quotas exist: write the three URLs to the pool variable, retain the public fallback, run
the pinned deploy validation, restart through `scripts/deploy_memecoin_runtime.sh`, then verify exact
release ownership, one runner, heartbeat, authenticated health, zero reservations, clear kill switch,
TPU status, rotation status, logs, and backup.

### Isolated TPU read-source exception (2026-10-11)

`MEMECOIN_TPU_RPC_URL` may select a separate same-provider/region HTTP+WS read credential and capacity after the manifest-bound health-only control verifies endpoint-reported mainnet genesis, SDK schedule bootstrap, cached leaders and clock advance. Never reuse custody/watch credentials or quota. Rust derives WS through the existing SDK; no separate WS setting is required. Genesis does not independently verify bank/epoch/schedule correctness; organic native-head evidence must establish the two-slot relation. Isolated errors, 429, shared capacity or a frozen schedule block activation. Tape and other public-fallback policies remain unchanged; this grants no paid submission, tips or funded authority.

# Memecoin Live Safety v3 - Design

**Date:** 2026-07-11
**Status:** Approved for implementation by operator directive
**Scope:** Real-money Solana/EVM reservation recovery, price identity, executable exit validation, and terminal exits

## Goal

Prevent a stale reservation, ambiguous chain response, bad market-data pair, or non-terminal exit policy from creating duplicate exposure or an unmanaged real position.

## Invariants

1. A broadcast transaction is never retried or released because an RPC call failed.
2. A Solana reservation is released as expired only after repeated, successful, explicit `not_found` responses after the blockhash expiry window.
3. `processed` is not `not_found`; it remains reserved.
4. A reservation with `broadcast_at` but no signature is ambiguous and remains reserved for manual reconciliation.
5. Every real price is keyed by `(network_id, normalized_mint, pair_address)`.
6. TP, stop, and de-risk decisions are confirmed using executable proceeds for the exact held balance. Display prices cannot directly authorize an on-chain sale.
7. Mirror, manual, time-cap, and unquotable exits remain executable without a profit test.
8. Every autonomous real position has a positive hard time cap. Startup fails closed when the real leg is enabled with `MEMECOIN_REAL_MAX_HOLD_S <= 0`.
9. A `manual-unmanaged-v1` position has no automatic exit. The operator must add an override or close it.

## Confirmation Contract

`rpc_confirm_once()` returns exactly one of:

- `confirmed`: terminal success.
- `failed`: terminal on-chain failure.
- `pending`: the signature exists but is not terminal.
- `not_found`: HTTP 200, valid JSON-RPC shape, and the requested signature slot is explicitly null.
- `unavailable`: transport error, non-200 response, JSON-RPC error, or malformed payload.

The blocking executor poll may return the same states. Entry execution treats `pending`, `not_found`, and `unavailable` as unresolved and leaves the reservation intact.

For Solana expiry recovery, `memecoin_pending_entries` stores `not_found_checks`, `first_not_found_at`, and `last_recovery_check_at`. Release requires:

- reservation age at least 20 minutes;
- three explicit `not_found` checks;
- at least 120 seconds between first and final qualifying check.

Checks less than 60 seconds after the last qualifying check do not increment. `pending` resets the proof counter. `unavailable` neither increments nor releases. `confirmed` marks the row orphaned and alerts once. `failed` releases it. EVM reservations are never released by this Solana expiry rule.

## Canonical Market Price

DexScreener selection filters before ranking:

1. pair chain equals the expected chain for `network_id`;
2. base-token address equals the requested mint with EVM-only case normalization;
3. price is positive and finite;
4. pair identity is present.

Among valid pairs, choose highest USD liquidity, then highest 24-hour volume, then lexicographically smallest pair address. A missing expected network for an EVM token is allowed only in the chain-resolution function; normal pricing fails closed. Solana base58 tokens infer the Solana network.

The selected observation carries price, market cap, liquidity, volume, chain, pair address, DEX id, source timestamp, and quality. Batch APIs return keys by `(network_id, normalized_mint)`, not mint alone.

## Executable Exit Validation

Each venue context exposes `preview_exit_fn(position) -> ExitPreview | None`, where `ExitPreview` contains expected USDC proceeds, token atomic amount, route identity, and quote time.

Before TP, stop, or de-risk execution, the runner computes total projected proceeds:

`prior_real_exit_notional + preview.expected_usdc`

and compares it with entry notional. This handles a position that was previously partially de-risked.

- TP requires executable PnL at or above the configured TP.
- Stop requires executable PnL at or below the configured stop after the existing confirmation count.
- De-risk computes the sold fraction from executable full-balance proceeds, not display mid.
- Missing previews suppress price-triggered execution and record a reason. They do not affect mirror/time/manual/unquotable exits.
- A rejected stop preview resets `stop_hits`; a rejected TP/de-risk does not mutate position state.

The executor remains authoritative for realized PnL through before/after chain balances.

## Deployment Safety

This batch changes code only in the isolated worktree. Live cutover requires zero pending reservations, a database backup, the full focused suite, and an explicit deployment SHA. Existing open positions may be carried through cutover because the new exit policy is stricter and terminal, but their venue and mint identities must reconcile first.

## Tests

- Explicit null, processed, malformed, RPC error, failed, and confirmed response shapes.
- Recovery spacing, counter reset, unavailable response, EVM exclusion, and ambiguous broadcast-without-signature.
- Wrong chain, quote-side token, shallow first pair, equal-liquidity tie, zero/NaN price, and EVM case normalization.
- TP/stop preview agreement and disagreement, missing preview, prior partial proceeds, and de-risk fraction.
- Startup rejection for armed real mode with a zero time cap.

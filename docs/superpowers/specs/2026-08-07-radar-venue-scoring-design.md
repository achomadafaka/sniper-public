# Radar Venue Scoring Design

**Status:** Approved by the operator on 2026-08-07.

## Goal

Radar will test the buyer-confirmation hypothesis separately on Pump.fun, Bags Robinhood, and Pons V1.

Each venue will keep its own rule identity, control identity, forward clock, and result summary.

Radar stays paper research. This design adds no funded authority, signer, reservation, sizing, or V6 candidate source.

## Common hypothesis

The hypothesis is that broad, clean buyer demand can predict a positive 60-second route result.

Each venue rule requires these decision-time facts:

- capital velocity increases;
- buyer velocity increases;
- buyer concentration does not increase;
- at least ten eligible buyers exist;
- at least two new eligible buyers exist;
- sell pressure is not more than 2,500 bps;
- the creator has no sale in the interval;
- an exact entry and exact acquired-quantity exit route exists; and
- the measured immediate round trip is at least -500 bps.

The target is +1,200 bps. The stop is -800 bps. The time exit is 60 seconds.

## Venue-specific readiness

Pump.fun keeps `pump-confirmation-truth-v6` and `route-control-v2` unchanged.

Bags uses `bags-confirmation-v1` and `bags-route-control-v1`. It also requires curve progress of at least 2,000 bps.

Pons uses `pons-confirmation-v1` and `pons-route-control-v1`. It requires an active registered V3 pool.

The venue rules do not share observations or controls. A result from one venue cannot satisfy another venue gate.

## Pons exact quote contract

The Pons launch event supplies the token, pair token, pool, and token order.

The V3 Swap recipient supplies the buyer identity. The shared Pons router does not count as a buyer.

The scorer accepts only the verified Pons WETH pair. Another pair token fails closed.

The quote path reads the registered pool fee at one EVM block identity.

The path verifies the pool through the QuoterV2 factory at the same block.

The path calls the verified chain-4663 QuoterV2 for WETH-to-token and token-to-WETH quotes.

The sell quote uses the exact token quantity from the buy quote.

The receipt binds the factory, pool, fee, token order, token identities, block identity, and state fingerprint.

A malformed response, a zero amount, a pool mismatch, or an RPC error returns a typed blocked receipt.

The quote work uses a bounded background queue. The shared 4663 capture loop never waits for a quote.

## Bags exact quote contract

Bags keeps its block-scoped BagsLens state and `quoteBuy` plus `quoteSell` receipt.

The Bags receipt uses the same bounded background queue as Pons.

## Realistic paper results

Each qualifying venue alert creates an immutable paper fill request.

The entry adapter dispatches by venue and preserves the exact venue route.

The exit adapter sells the exact expected entry quantity on the same venue route.

The existing measured 122 bps execution haircut applies to each leg. The provider floor stays a safety bound.

The lifecycle stores valid, censored, and open results under the existing append-only tables.

## Display contract

The API returns one result row for each active scoreable venue.

Each row shows valid results, average net return, target-first, stop-first, time-exit, censored, and open counts.

The Radar panel renders Pump.fun, Bags Robinhood, and Pons Robinhood as separate rows.

The panel does not pool the venue averages. It does not show a promotion verdict.

## Capture and storage

Pump.fun, Bags Robinhood, and Pons remain active capture venues because all three now feed scoring.

Pons rows without a valid V3 route remain explicit blocked evidence. They do not create an alert or paper request.

Historical capture-only rows stay immutable. The new forward clocks start at the deployed release activation.

## Verification

Focused tests must prove venue authority, separate rules, exact Pons ABI calls, route binding, queue bounds, and fail-closed results.

Focused tests must prove venue-specific entry and exit dispatch, separate summaries, API output, and UI output.

The cutover must verify the merged release, one worker process, fresh source health, quote health, and live per-venue rows.

## Knowledge sync

Knowledge delta: Pons and Bags become scoreable paper venues with separate forward evidence clocks.

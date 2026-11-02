# Radar Pons Sector and Pump Transport Design

## Goal

Make Pons visible as a first-class Robinhood venue in the Radar graphic and reduce brief Pump capture gaps without hiding real source failures or increasing provider call volume.

## Verified problem

- `pons_v1_4663` is present in source diagnostics and the venue evidence table, but the SVG has only three named sectors. Its fallback angle overlaps Pump.
- Recent intermittent capture gaps belong to `pumpfun_solana:ws`. Persisted failures include WebSocket HTTP 429, connection closes, and recovery HTTP 429 from Solana's public endpoint.
- The configured PublicNode endpoint passed one bounded HTTP health request and one Pump program-subscription handshake.

## UI contract

The radar uses four explicit, non-overlapping sectors:

- Pump.fun Solana: `[-130, -50]`
- Bags Solana: `[-40, 40]`
- Bags Robinhood: `[50, 130]`
- Pons Robinhood: `[140, 220]`

Each sector has a visible SVG label. Pons remains raw evidence only: the UI shows its capture count, but it does not invent a scored dot, signal, outcome, or funded action.

Real source gaps continue to downgrade only the affected venue. The UI must not suppress or smooth a genuine transport or continuity failure.

## Transport contract

Use the existing configured PublicNode service for Pump transport isolation:

- Set `MEMECOIN_OPPORTUNITY_SOLANA_WS_URL` to the WebSocket form of `MEMECOIN_SOL_TAPE_RPC_URL`.
- Set `MEMECOIN_OPPORTUNITY_SOLANA_RECOVERY_RPC_URL` to `MEMECOIN_SOL_TAPE_RPC_URL`.
- Leave `MEMECOIN_OPPORTUNITY_BAGS_SOLANA_WS_URL` unchanged.
- Leave primary Solana transaction/block truth and every RPC governor rate unchanged.

This is provider separation, not fanout. The worker opens one Pump subscription and makes the same bounded recovery calls it already made; it does not duplicate requests across providers.

## Cutover and rollback

1. Add failing UI contract assertions for the Pons sector and four explicit angle ranges.
2. Implement the smallest static SVG/mapping change and run focused UI/API/runtime checks.
3. Merge before deployment and create pinned opportunity/web releases from the merge SHA.
4. Back up `.env`, change only the two approved opportunity transport variables, gracefully restart the opportunity worker, and restart the pinned web service.
5. Verify exact process ownership, source freshness, zero open gaps/blockers after bounded recovery, healthy/non-throttled RPC, Pons capture growth, and the served JavaScript contract.

Rollback restores the prior `.env` values and prior pinned release links, then restarts only the opportunity worker and web service. Funded runners are untouched.

## Verification limits

Use focused tests only. Do not run Playwright or the full test suite. Do not add arms, scoring, execution authority, provider fanout, or extra polling.

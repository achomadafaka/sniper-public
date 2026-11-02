# Intent: V4 N-leg measured-cost route on chain 4663

- **Author:** Alpha (sniper-fc), drafted from the 2026-09-05 conversation with the operator.
- **Status:** approved by the operator 2026-09-05 ("start scoping and building this").

## Problem

Funded entries on Robinhood chain refuse coins whose only real pool is paired with a tokenized
stock or USDG. The two-leg route needs a fixed-fee ETH pool on the bridge; many stocks have none
or a thin one. 43 of 99 route-refused mints in 7 days had no sane ETH/WETH pool.

## Proposed outcome

The route is an ordered leg list, admitted by measured round-trip cost. Three-leg routes through
USDG are searched next to two-leg routes. PH routes at -3.4% instead of -12.7%.

## Affected users and systems

Funded 4663 entries and exits (`evm_v4_multihop.py`, `evm_v4_multihop_tx.py`,
`evm_real_seams.py`), the exit lane, refusal ledger, settings.

## Constraints

- No change to the ETH -> first-bridge fixed-fee rule.
- Every leg quoted both directions before a route exists.
- Default inert (`MAX_LEGS=2`); activation is a deploy-time `.env` change by Alpha only.
- Hook `0x0310cfeb…` stays out.

## Open questions

- Per-leg gas: measured by the canary, not assumed.

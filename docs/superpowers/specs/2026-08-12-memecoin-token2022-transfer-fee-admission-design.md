# Token-2022 Transfer-Fee Admission Design

**Date:** 2026-08-12

## Problem

The Solana funded gate rejects every positive Token-2022 transfer fee. The rule does not separate a bounded immutable fee from a mutable fee.

The gate refused three signals for mint `2ySgxFuhgjAJdjG8GSdfQ3L1GMyKCkghjKmP8umGVUD5`. The `hopa` signal failed at `2026-08-12T13:55:35Z`.

A read-only mint check at slot `438825945` showed these facts:

- The older fee is `300` bps.
- The newer fee is `300` bps.
- The fee-change authority is null.
- The mint authority is null.
- The freeze authority is null.
- The permanent delegate is absent.

The current sell-route probe uses a fixed `1_000_000` atomic units. This amount does not represent the planned position for tokens with different decimal values and prices.

## Contract

- Set the funded immutable transfer-fee limit to `300` bps by default.
- Permit a positive fee only when both fee schedules are valid and do not exceed the limit.
- Permit a positive fee only when the fee-change authority is null.
- Reject a live permanent delegate.
- Reject a malformed fee schedule.
- Keep the mint-authority and freeze-authority refusals.
- Keep all risk, age, chase, exposure, loss, and kill-switch controls.
- Do not use a mint allowlist.
- Do not make a quote for the refused historical signal.

The limit uses `MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS`. The valid range is `0` through `10000`.

## Entry Flow

1. The runner reserves the approved funded clip.
2. The token gate reads the mint account.
3. The token gate checks the authorities and both fee schedules.
4. The executor requests the normal entry quote for the approved clip.
5. The executor reads the positive `otherAmountThreshold` token quantity.
6. The executor requests an ExactIn sell quote for that quantity.
7. The executor releases the reservation if the sell quote is absent or malformed.
8. The executor reads balances only after both quotes pass.
9. The executor signs only after both quotes pass.
10. The existing write-ahead identity and submit rules continue without change.

The entry quote remains the transaction quote. The code does not request a second entry quote.

## Evidence

The entry trace records the effective fee, the fee limit, and the immutable-authority result. The trace also records the planned-quantity reverse-quote result.

A fee refusal names the observed fee and the limit. A mutable fee refusal names the live fee-change authority condition. A route refusal remains separate from a fee refusal.

## Failure Rules

- If either fee schedule is absent or invalid, the gate fails closed.
- If either fee exceeds the limit, the gate fails closed.
- If the fee-change authority is live, the gate fails closed.
- If the permanent delegate is live, the gate fails closed.
- If `otherAmountThreshold` is absent, invalid, or zero, the executor fails closed.
- If the reverse quote fails, the executor releases the unsigned reservation.
- No failure in this flow can sign, submit, or open a position.

## Tests

Focused tests must prove these cases:

- An immutable `300` bps fee passes the authority gate.
- A `301` bps fee fails.
- A live fee-change authority fails.
- A live permanent delegate fails.
- A malformed older or newer schedule fails.
- A clean legacy token still passes.
- The reverse quote uses `otherAmountThreshold`, not a fixed atomic amount.
- An absent reverse route releases the reservation before the signature step.
- A valid reverse route keeps the normal entry quote for the signature step.

## Scope

This change affects Solana funded admission only. It adds no schema, dependency, mint allowlist, paper behavior, funded sizing, exit policy, or funded wallet authority.

Deployment remains separate. A deployment must use a merged SHA and `scripts/deploy_memecoin_runtime.sh`.

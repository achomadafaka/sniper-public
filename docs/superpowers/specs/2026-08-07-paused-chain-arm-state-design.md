# Paused Chain Arm State Design

**Status:** Superseded by `2026-08-07-memecoin-admission-unlock-design.md`.

This document records the interim state. The current contract clears funded authority on pause.
The current UI shows `resume + arm` for a paused wallet without stored chain authority.

## Problem

The Chains trader table reads `armed_chains` from the combined roster endpoint.
That field contains effective authority only.
The endpoint removes configured chains when a wallet is paused.

A paused wallet can keep `real_chains=4663`.
The table then shows `arm` after a successful Robin arm request.
Each new click writes another forced arm audit row.

## Rejected Design

The combined roster endpoint will add `configured_chains`.
This field will contain the stored chain list for an approved wallet.
The existing `armed_chains` field will keep its effective-authority meaning.

The first repair used `configured_chains` for the control state.
It showed `armed - paused` and offered disarm.
This state was honest but useless.
An operator arm action must create effective authority.

## Corrected Design

`set_wallet_chain_armed(..., armed=True)` will clear `paused` in the same transaction.
The approval gate and the required Fomo follow will still run before this transaction.
Exclusion will remain unchanged and will continue to block effective authority.

A configured paused row will show `resume + arm`.
This action will send `armed=true` for the same chain.
The server will keep the configured chain and clear pause atomically.

Disarm will remove only the selected chain.
Disarm will not change pause.

## Tests

A persistence test will prove that arm clears pause and makes Robin authority effective.
A browser test will prove that a paused Robin row sends `armed=true` through `resume + arm`.

## Scope

This change does not change the funded runner or the approval gate.
This change changes pause only when the operator arms a wallet.
This change does not write production data.

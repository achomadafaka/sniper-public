# EVM Safety Requote and Quote Attribution Design

## Problem

The funded sell simulation reduces every JSON-RPC revert to `False`. The safety layer then labels
that result `honeypot_sim_reverted`, even when the router explicitly reports a stale min-out. The
entry executor also binds a post-approval or retry quote to the first quote's timestamp, corrupting
quote ID, quote age, and source-to-quote-to-fill attribution.

## Contract

- Decode the funded sell simulation's revert payload with the same positive router classifier used
  for mined entry reverts.
- On a proven min-out only, fetch one fresh nominal sell quote and simulate it once. Keep the safety
  gate fail-closed if the fresh simulation reverts, is unknown, or cannot be funded.
- Never retry an unclassified simulation revert. Report it as
  `sell_sim_reverted_unknown`, not as a proven honeypot.
- Persist the timestamp and deterministic quote ID of the final executable quote. Compute quote age
  from that timestamp to the matching confirmation observation in the trace's reconciled local
  clock domain.
- Do not change slippage, route-loss cap, reservation semantics, sizing, stop/TP policy, or the
  unknown-confirmation no-resubmit rule.

## Evidence

Live router failures contained known V2/V3/V4 min-out markers. A fresh quote already fixes the same
failure after a mined revert. The pre-entry simulation currently discards that marker before the
safety decision. Code inspection also proves the post-approval quote is passed the old
`quote_observed_at` value.

## Non-goals

- No generic retry of router or token reverts.
- No safety bypass and no wider slippage.
- No static caching of quotes, balances, allowances, or safety verdicts.
- No hot-path API, UI, notification, or LLM work.


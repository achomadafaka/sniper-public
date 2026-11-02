# Wallet-scoped exact source mirror with +100% take-profit

Status: implementation authorized by the operator's request to copy tontheneko, followed by
"You can find everything you need." The operator delegated discovery and routine choices.

## Contract

Use the verified account/chain identity and its normal existing per-buy cap, currently $50.
Only proven, supported addresses may be armed. Public labels alone do not grant authority.
No historical purchase is replayed into a new funded entry.

New entries for explicitly configured `(source_user_id, canonical_chain)` pairs freeze
`source-mirror-tp100-v1`: hard TP +100%, mirror each proven source sale proportionally, and
no stop, catastrophe, trail, automatic de-risk, volume-death, unquotable timeout or time cap.
Holds can last indefinitely. Source-integrity unwinds, operator actions, custody/signing safety,
ordinary admission, sizing, exposure and loss controls remain authoritative.

The take-profit compares the executable remaining-position unit price with twice its original
entry unit price. Source trims can occur below that price; +100% is not a promise of total trade
profit. If the source sells 25% of its then-current holding, sell 25% of our then-current
owned remainder. A subsequent 25% sell leaves 56.25% of the original inventory. Source adds
change the denominator for later source sales, never the already captured sale ratio.

## Existing mechanisms and chosen scope

Reuse the existing immutable named-wallet policy configuration pattern and existing frozen
funded contracts. Do not expand the general wallet editor, widen its ordinary stop bounds,
or impersonate a manual position. The new explicit policy has a narrow validated exception
to the automatic downside/positive-time-limit rules. All other policy families and old
contracts retain their behavior. Replacing a policy removes copy-arm/engine/trial ownership
and provenance that could independently add an exit.

Reuse the runner-owned exit request journal and signed exit-intent ledger. Add a distinct
repeatable partial claim sharing position exclusion with full closes and one-shot de-risk.
Do not clear `derisked_at`, borrow a full-close claim, or infer a settled sale from a timeout.
Exact integer source sold/pre quantities are retained as decimal strings. Quote input is
`floor(owned_atomic * source_sold_atomic / source_pre_atomic)` and stays bound through signing.
Receipt accounting uses actual sold atoms/proceeds; failed reads or missing receipt prices
remain unknown. Solana ratios require signed confirmed watcher evidence, so source sells may
wait for confirmation; processed hints and USD feed amounts alone cannot size these exits. A partial never finalizes the remaining position as fully closed.

## Edge cases and limits

- Money path: no new synchronous RPC, retry or lock on entry. Policy selection uses immutable
  configuration membership and existing reads. Exit observation/claim/recovery stays off entry.
  Measure the selection helper p50/p90; record benchmark scope, not live fill claims.
- Identity/units: canonical chain, exact source owner/mint/signature and integer atoms. Unknown
  source pre-balance cannot become a 100% sale. Unsigned account drops are not source sale proof.
- Duplicates: the durable request key binds chain, position, source transaction and event/coin
  identity. Delivery through another feed, strengthened proof and restart reuse the request.
- Races: source trims, source full sell, TP, manual close and one-shot de-risk share exclusion.
  After a prior trim settles, later requests use the fresh owned remainder. A full sale already
  completed makes later requests no-ops. A trim rounding to zero atoms finishes without a fill.
  A later full source sale supersedes queued unsigned trims, including route dust, but never
  bypasses claimed or signed work. Other trims retain FIFO order; a failed full sale preserves
  the earlier requests. An unsigned blocked request retries without signing twice.
- Dependencies: timeout, transport, 401/403/429, malformed, partial or wrong identity hold with
  typed diagnostics. A signed/ambiguous action retains ownership until exact confirmation,
  explicit failed receipt or protocol-validity plus exact atomic balance proof resolves it.
  Solana expiry and balance must share a context watermark; EVM nonce, time and balance share
  one block. Recheck the original signature after these proof reads; any reappearance holds.
- Lifetime: source requests must not inherit a manual queue lease that blindly re-arms signed
  work. Restart recovery must find a second partial even after the first partial fill exists.
- Policy: the frozen profile suppresses price-loss/time/volume/stall exits. A later wallet config
  change does not rewrite existing positions. The source-not-landed integrity unwind stays live.
- Rollback: new readers retain old contract support; old readers cannot correctly run indefinite
  holds. Managed deployment refuses an incapable target before stopping the current runner when
  the profile is configured, an open profile position exists, or a repeatable partial is pending.
  Test the target's actual code, not an import shadowed by the invoking checkout.
- Not handled: guaranteed source fill price, guaranteed liquidity, fabricated missing source
  inventory, profitable long-hold claims, or protection from losing the full stake. Arbitrarily
  launching an unsupported historical binary outside the managed deployment path is unsupported.

## Activation and proof

Prove defect tests RED, then focused GREEN with two workers under one task-wide test lock.
No full suite. Verify default-policy byte compatibility, exact profile round-trip, duplicate and
concurrent sales, repeated partials, signed unknown/restart recovery, and incompatible rollback.
Independently review the integrated money path before merge and pinned deployment. Preserve
`--defer-backup`. Do not arm the new account until identity, policy, callback and recovery support
are all live. Read back chain-scoped authority, effective cap/policy, subscriptions and audit.
Do not manufacture a trade to test deployment; organic fill/latency improvement remains unproven.

Deployment scope: tontheneko UID `2251aa65-06e0-5845-bacb-75bdbae5dd31`, Solana and Robinhood
4663 only. See `docs/audits/2026-10-20-tontheneko-identity.md`. Other observed chains remain
unarmed until their own supported receipt evidence is established. Register explicit operator
receipt attestations through the existing audited helper; do not impersonate discovery promotion.
The EVM operator-registry reader is enabled only on background roster refreshes, preserving
entry-side lookup behavior and all per-chain initiation/payment gates.

Selection microbenchmark (1000 isolated calls, not funded execution): disabled p50/p90
0.094/0.120 microseconds; configured hit 0.419/0.439 microseconds. Background EVM roster read
on a bounded current database snapshot: default 3.730/3.808 ms, operator opt-in 3.748/3.869 ms.
No organic fill or landing improvement is claimed.

# Office ticketless inspection: read-only release

Epsilon owns this slice; Beta owns the future host evidence producer; Gamma checks source/clock semantics; Zeta checks walls; Alpha merges/deploys.

Alpha ruling 2026-10-30T17:28Z: ticketless ships read-only until the producer exists. This release exposes `self_sheet_read(network_id,mint,pool?)` only, behind the existing paper-service tool boundary. No `self_paper_order` schema/handler or `PaperService.enter_self` ships. Existing feed admission, risk, policy, atomic labels, expiry-at-submit and retries remain unchanged. Prepared admission code stays in `epsilon/office-ticketless-admission-checkpoint`, not this release.

- Host assigns identity from the socket. No source or evidence arguments. Require current roster epoch/incarnation, a logged definition hash, prior candidate registration and a valid corrected clock.
- Read-only, explicitly coin-indexed desk lookup, max201 stage rows and a100ms SQLite progress deadline. Any feed stage known by the host decision cut redirects to its real ticket. Future tickets do not rewrite the earlier cut. Missing database/schema/stamps, a head with no readable stages, or an unresolved truncated lookup stays typed unknown; never defaults to self.
- First candidate registration keeps its original actor, pool and host time. Append its original validated corrected-clock sample as a hash-chained receipt in the same transaction. Duplicate calls return the same receipt; invalid/unbound legacy registrations cannot borrow a later clock. Reuse existing receipt retention; no new writer table.
- The default self sheet is a readable unknown report: missing mint/freeze authorities, top holders excluding the pool, sells, sell probe and pool state; missing lenses labelled. No provider, hard-filter PASS, price, fake ticket or order receipt is invented.
- Log `ticketless_sheets` (builder/service/registration code and read-tool schema hash) before constructing the paper service. Prompts enabling inspection are separately reviewed/logged. Rollback removes the read tool; original registrations/receipts remain immutable.

Edge cases: absent/zero/unknown, failed/partial/limited reads, future or malformed timestamps, duplicate registration, clock changes, identity/pool mismatch, foreign/stale seats, missing log. Paper-only and no order capability: no entry/exit/signing or funded fill latency change, no global environment mutation. Tests use private SQLite/actual producer schemas; no live APIs, orders, runtime or .env.

Not handled: evidence collection, new venues/chains, opening self orders or prompt rollout. Beta must reuse the exact hard-filter path and thresholds; all required facts known-pass, earliest fact sets sheet age, unknown/not_run sell probe refuses. Opening orders is a later logged change; Gamma reports self separately with no applicable feed null arm.

Verification: rebase preserved main0fe6e752d tool/watch/results/taint/startup changes. Any-causal-feed-stage witness was RED (`self` instead of `feed`) before removing the hard_filter-only predicate. Registration tests cover first clock/actor/pool and duplicate/invalid/unbound states. Dispatch witness requires socket identity and absence of every self-order capability. Focused gates include current seat-host tests with two workers.

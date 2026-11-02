# Trusted crew brain inventory provider

Delta delegates bot/brain_graph/handover_inventory.py and tests to Codex. No live brain writes, hooks or installation. Delta owns mail/action ledger and writer transport. Store injection keeps credentials outside this module. The host derives role from its authenticated socket/seat binding, never model tool arguments.

Policy: actor:ROLE links own tasks regardless of writing author. All READ_STATUSES plus valid_to null remain open, including disputed/evidence_broken/judge_pending; completion eligibility remains stricter. Orders are decision and settled facts linked to actor:operator, same open statuses, without hiding expired verification dates. These are quoted brain data, never authenticated orders by virtue of inventory membership. Crew/Office provenance boundaries remain mandatory.

Read complete bounded keyset pages until EMPTY, never stop on a short server page. Validate identity, version, kind, actor, status and projection. Bound each stream, total requests and bytes; unknown/capped/replayed/wrong rows refuse. Fetch referenced entities/sections in bounded projections, refuse missing or Office provenance. Compare two complete canonical scans and refuse observed change. Return brain-only task/order metadata, brain_content_digest and fresh cut_id with complete/pagination_complete. Digest includes full functional fact content and entity/section policy so same-ID edits change it. No embeddings or source prose returned.

This is an optimistic stable read, NOT a database snapshot. Delta still compares provider reads before/after the note and atomically protects its own ledger/fences; continuously adversarial same-UID writers remain outside this guarantee. Host combines explicit brain_content_digest (provider returns no final content_digest) with full mail/action/terminal ledger content into the checkpoint digest; absent host ledger is never an empty proved ledger.

Edge pass: no money/fill path, environment mutation, shared connection mutation, SQL/schema edits or persistent writes. Missing actors, versions or dates are not zero/empty proof; valid literal version0 is permitted by common schema. HTTP timeout/rejection/partial/misordered/repeated/capped pages and changed content refuse. Unit fields are UUID identities, integer versions and opaque SHA256/cut UUIDs. No denominator or latency improvement claim. Rollback removes caller import; keep writer MANUAL until reviewed/provider wired. Tests use fake store only.

RED witnesses: server short pages fully consumed, malformed/capped/read error refusal, same-ID content/version change digest, changed scan refusal, disputed/task and settled/order retention, writer-author vs actor-owner distinction, excluded closed rows, missing actor/reference and Office boundary failure, consumer accepts matching full-open inventory. Implement provider then minimal consumer policy alignment; focused tests -n2; send exact SHA to Delta/Gamma before integration.

## Fixed legacy pointer targets (Gamma contract review)

The six known operator-linked pointers carry only an old fact id. Preserve the
originals. The provider pins those exact six pairs in LEGACY_ORDER_REFERENCES;
never extract UUIDs or instructions from fact prose. One hop, at most64 pairs,
no cycles or duplicate pointer ids. Exact-id target paging shares the existing
32-request /4MiB cap and requires EMPTY to prove completion. Unknowns refuse.
Target kind must be decision/settled, with valid identity/version/status/author and
entity/section provenance. Targets and bindings enter BOTH full snapshot digests;
statement, detail, status, version and provenance edits move the digest. Retired
(retracted/superseded/refuted/closed) targets remain hashed and are reported with
id/status/version and state retired, without blocking solely due to retirement.
Only live targets join operator_order_ids beside the pointer ids. No prose is
returned; membership still carries no authenticated authority. The CLI consumer
accepts an otherwise-unlinked target only through the same pinned pair and a live
operator-linked pointer included in the inventory. Arbitrary targets still fail.

Scope: provider, existing CLI consumer, focused tests, this plan only. No new
relation schema, generic reference resolver, recursive expansion, data rewrite,
retraction, secrets, live install or money-path work. Rollback restores the old
provider/consumer together and labels the six references ID-only again. Gamma's
sequencing recommendation allows Lambda only with that limit explicitly written
in its install note; Alpha decides. Wider rollout requires this seam reviewed.

## Pointer retirement edge
Read the exact pinned pointer/target ids regardless of live linkage. A live target
with no active linked pointer refuses inventory_order_pointer_missing, so retiring
a pointer cannot silently drop an order. Retired targets and retired pointer facts
remain fully hashed and provenance-checked. Both absent means that migration pair
is not instantiated in this supplied graph. This is still one hop and shares all
existing caps. Two RED witnesses: pointer retired/target live refuses; pair retired
remains hashed and does not block solely on retirement. No fact mutation.

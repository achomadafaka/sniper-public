# PumpSwap Desk inputs and integrity remediation

Operator request: **"Address all the gaps"** after the Desk input/coverage explanation. Base `f0a39bc63cab01edecee5d12fb4c786df2d48215`; current three-feed cohort `c6990fd8…` remains frozen.

## Goal and bounds

Expose truthful chart/flow/range/age/context/safety inputs using existing captured data; repair loss attribution/readers, intake provenance and paper cumulative-stop enforcement. Keep feeds Pump.fun/Fomo/DexScreener, Solana graduated canonical PumpSwap only, and paper execution. No funded service, signing, money, `.env`, backup or unrelated `--confirm` action is authorized. Preserve $25 clip, five positions, shared40 JUDGE calls/hour, 40realSOL, $5k–$50M safety, round trip8%, 12s deadline and existing rail bounds.

No new provider request, lock, retry or disk scan enters funded filling. New capture bookkeeping stays on the existing paper universe thread; local input reads are indexed/bounded and performed before the one model call. Collectors keep existing cadence. GPT narrative arm and retired curve capture stay off. No outcome comparisons or strategy tuning.

## 1. Loss truth and continuity

Existing `gaps_ps` coalesces decode causes and JUDGE ignores individual `ps_decode_losses`; this can both overblock unrelated pools and miss a later loss in the same batch. Retained generic errors have lost detail: do not invent a historical parser repair or delete/reclassify old rows.

- Record immutable detailed machine reason plus a **proven complete affected-pool set** for newly discarded transactions. Account for every PumpSwap invocation in an all-or-nothing discarded transaction. Truncation, malformed/unknown scope and legacy receipts remain global. Known other-pool scope may exempt only a different pool; never clear same/unknown scope by a coincidental reserve match.
- Scope proof consumes complete top-level and CPI instructions with account lists and resolved loaded addresses, pinned instruction/event layouts and complete invocation coverage. Log-only parsing or the first offending event is insufficient. New individual receipts correlate explicitly with legacy coalesced batches; an absent/incomplete batch attestation remains global, including after a partial commit.
- Keep legacy gap rows readable and conservative. Additive scoped metadata must not make old readers silently accept a loss. A shared bounded/indexed reader accounts for individual losses and stream/storage/reset gaps. Missing schema/index, malformed evidence, overflow or commit failure refuses explicitly.
- Preserve pending loss timing: a pending loss after a frozen cut does not alter earlier facts; missing/malformed pending timing cannot be treated as outside the window. No clock rewriting.
- Retain bounded decoder diagnostics (typed reason/discriminator/length/invocation scope/signature/slot), not credentials or unbounded frames. Generic decoder repairs require a reproduced failing fixture; diagnostic availability is not proof every cause is fixed.
- Budgets: at most128 inspected invocations/transaction and32 proven affected pools; overflow loses scope and stays global. Optional raw event diagnostics are at most2048bytes/sample and20samples/minute; mandatory compact loss receipts are never sampling-dependent. Existing retention governs source day files. Inclusion records are deltas, not one write per unchanged reload. Every indexed reader has an explicit20000-row ceiling; exceeding it produces unknown/refusal, not a truncated complete answer.
- Record actual per-pool inclusion intervals for the intake-selected set. Global heartbeat alone does not prove full60s pool observation. New input readers require the new coverage protocol or report partial/unknown. Startup/reconnect/storage gaps and loss intervals clip coverage; first swap is never birth.

## 2. Price and feature readers

- Extend the existing canonical-pool tape reader with validated base decimals and one explicit receipt/slot cut. New readers never assume six decimals. Legacy interfaces remain compatible where their six-decimal contract is explicit.
- Filter rows **and pre-window virtual-quote seeds** by receipt and slot cut. A final sell whose next-reserve witness is beyond the cut stays unpriced. Proven virtual reserves affect price, never real-liquidity floor.
- Preserve seed identity/time and invalidate a carried virtual-quote value across same/unknown-pool loss from seed receipt onward, including losses before the requested window. Only a new independent proof restores price. A seed that merely passes the timestamp cut is not sufficient.
- Our trades, agent trades and price-only loss witnesses never count as ordinary flow/makers. Agent and loss witnesses may affect prices when proven. Unknown actor/price/coverage stays distinguishable from zero.
- Produce the existing six-field60s flow and price-range facts only with their required coverage; otherwise top-level full-window fields stay null and structure reports observed partial figures/coverage. Chart windows and bars stay bounded under existing structure semantics; overflow is a typed partial/unknown result.
- Reuse one frozen claim candidate/cut/slot/FX across pool, chart, flow, range and safety. No wall-clock/latest reread during re-rendering; persist the exact serialized sheet and hash.
- Initial hard-filter validation has a later knowledge clock: Yellowstone block metadata can arrive after the original print. Keep swap receipts bounded by the original print/emission, prove its slot using committed block context received by actual evaluation, and audit `market_cut={receipt_cut,cut_slot,block_known_at}`. Evidence knowledge must include that later block receipt; never fabricate a producer cut slot or backdate validation. JUDGE refresh retains its single claim-time cut.
- Keep source-health/frontier checks current, while quote dependencies end at the frozen print/quote inclusively. A later loss cannot invalidate earlier proven evidence. After a later source reset, historical proof requires recorded protocol-1 pool selection and committed capture coverage; missing/partial/legacy proof stays unknown.

## 3. Ages and attention

- True coin age requires a causally known coin-creation record. Retained launch lookups must require receipt-before-cut. Pool creation is pool age; first captured swap/listing is observed history. Never overload coin age with either. Creator priors require proven historical coverage; otherwise explicitly unknown.
- Creation `block_time` is backfilled after metadata arrives, without advancing the row's original receipt. Resolve the exact slot against retained indexed block records and require both creation and block-metadata receipts before the frozen cut. Knowledge is their maximum; an inline timestamp alone cannot prove when birth was known. This also handles retained midnight creation rows whose inline timestamp stayed NULL.
- Public creation timestamps already retained in Pump.fun intake may be exposed separately as **provider-reported age**, carrying source/known-at/basis, never as on-chain birth proof. No new network dependency is required for this first repair. Missing metadata stays unknown with a named reason.
- Provider `created_timestamp` is UTC milliseconds, normalized explicitly to seconds; occurrence time and host receipt/knowledge time remain separate. Use the recorded finite clock offset for the source observation, reject future creation times and unknown/malformed clocks, and test a nonzero offset. A retained launch's host receipt is not its block-time birth occurrence.
- Trusted observation/host/offset clocks survive all three source adapters. Fomo/Dex clocks can support retained on-chain ages; provider-reported creation remains Pump.fun-only.
- Expose observed history/coverage and proven pool age separately where available. Existing source data cannot establish missing historical creation or creator coverage; do not fabricate values to make every field non-null.
- Enroll existing PumpSwap structure code and venue-independent Dex/X/crawler attention facts. Preserve causal source freshness and chain/mint binding. Retired-curve board/theme aggregates remain explicitly unknown; no replacement with zero or dormant GPT narrative activation.

## 4. Intake provenance

Add an indexed read-only verifier of the referenced collector row and its successful parent poll. Split reference at the first colon into validated day and opaque source_ref; allowlisted feed determines the directory. Match feed/filter/network/mint/rank/rank-kind/observation/host-receipt/offset, poll identity and strict post-listing print ordering.

Validate eligibility at original emission time, not delayed assembler time. Missing/corrupt/unreadable/forged/failed-poll rows refuse by typed reason, with no network fallback. Persist a candidate/policy-bound verification receipt so restart recovery survives later source pruning. New direct admissions must execute indexed source verification; replay may reuse only a matching trusted receipt retrieved from the Desk DB. Receipt and admission persist atomically. A caller-supplied "pass" blob plus a valid hash authenticates nothing and must never bypass verification. Caller supplies an explicit trusted local source root rather than accidentally opening production state in tests.

## 5. Paper stop and concurrency

- Read the registered negative finite cumulative limit and sum closed, priced paper PnL **for that cohort**. Null/nonfinite closed accounting refuses new admission. Unpriced/open marks never become realized zero. Keep model costs in the separate existing ledger gate; do not mislabel this as combined net-of-model accounting.
- At/below the limit, durably latch the cohort killed. Later gains/restarts never clear it. Preserve global daily-loss/concurrency/clip/chain/rail gates.
- Schema setup precedes any transaction because `executescript` commits. Latch/check/position insertion share one paper-only write transaction, including atomic concurrent admissions. Do not add a funded-ledger lock.
- Every priced close and its cumulative-stop check commit atomically. A negative close crossing the limit must permanently latch before a later profitable close can recover the aggregate; duplicate closers cannot overwrite the first result.
- Compute fill quote evidence outside the write lock, then atomically recheck kill/accounting and transition the still-pending row to open inside one paper-only `BEGIN IMMEDIATE`. Concurrent close-versus-fill and duplicate transitions must not cross the kill or create a second fill.
- New cohort semantics: still-unfilled requests are cancelled when a kill becomes known, even if their intended modeled landing was earlier; preserve original request/landing clocks and a typed cancellation reason. Record the actual knowledge/enforcement time, never backdate a kill or rewrite recorded fills. Existing open exits and PAPER RISK continue.
- Gate before actual model spend where possible; final admission/fill checks remain authoritative. Duplicate ticket replay stays idempotent.

## 6. Safety on the model sheet

Project the ticket's own hard-filter PASS proof into a separate typed input, bound to network/mint/pool/original snapshot hash and known_at<=claim. Do not extend `intake_mint_of`'s exact five-key receipt. Mint/freeze authorities may be forwarded; fresh modeled round-trip arithmetic may be recomputed on the refreshed pool without RPC. Label proof times; never relabel old proofs as fresh. Existing `sell_probe` grammar is `ok`/`fail`, not `pass`. LP lock/holder/cluster/creator/sell-flow facts remain unknown unless independently proven.

## 7. Versioning and release

New enriched inputs require a **new PS-facts version, prompt v27, frozen policy/cohort and preregistration**. Keep c699 unchanged and retain all old evidence. Canonical current-v26 serialized policy bytes/ID must remain unchanged when new features are omitted. Wait remains recorded/not fired, no watch/standing/re-entry activation, no new gate thresholds or ranker.

Before a new single-primary cutover, drain/account for claims/tails/heads/outbox and preserve cursors/cooldowns/positions. Source input changes never retroactively modify the current study. Exact reviewed pinned staging, reservations/reconciliation and no-backup policy remain. Present any new registration command concretely; the earlier explicit `--confirm` exception covered only the previous script.

Rollback changes paper pins/routing only. Additive receipts/coverage/latches remain; old readers stay conservative. Keep management of any open new-cohort positions on compatible code, and never replay emissions into another cohort. Genuine missing external history, partial website coverage and uncertain decoder causes remain disclosed data limits rather than invented success.

## Acceptance

TDD witnesses must fail first for: 6/9/unknown decimals; future rows/slots/VQ seeds; late sell; our/agent/price-only flow; missing/partial inclusion history; same/other/unknown/legacy/multipool loss and coalesced second loss; pending timing; schema/index/overflow/commit/reconnect failures; causal launch metadata and explicit age basis; healthy/stale Dex/X; bound/foreign/future safety proof; forged/missing/failed-poll source rows and delayed/restarted provenance; cumulative threshold across days, cohort separation, permanent latch, unknown accounting, concurrent admissions, pending-fill cancellation and continuing open exits. Verify the actual stored serialized sheet and hash. Tests stay -n2; one full suite uses the shared host lock. No profitability claim follows from these checks.

## 8. Event-account proof amendment — 2026-10-27

The dominant prospective refusal is a pinned multi-hop instruction nested inside routers. The new proof contract is `docs/audits/2026-10-27-desk-event-account-envelope-proof.md`. It implements the existing pinned-ABI account-attestation rule without pricing multi-hop or changing the frozen v27 policy/CID/prompt. Preserve old receipts/decisions and record the actual activation epoch because availability changes prospectively.

- In addition to complete affected-pool attribution, a separate immutable event-account envelope can prove that a newly discarded transaction could not emit a recognized official AMM event for a target pool. It covers **all** resolved keys, including readonly accounts; its Bloom representation may exempt only a definitely absent target. Account keys never become `pools_json` members. Original unknown losses remain intact, and duplicate/historical losses cannot acquire this new proof.
- Complete closed-handler/instruction/log/CPI/ALT proof, bound checksum/provenance and actual proof knowledge at/before the cut are mandatory. Unknown handlers/events, upgrades, incomplete batches, malformed/future proofs and generic source gaps remain conservative. This is captured-event absence, not physical-vault/current-fee independence. Bounded opaque event extensions remain uninterpreted; known prefixes and exact CPI/log bytes must match.
- The optional diagnostic limit in section 1 gains one explicit exception: at most **eight first-only full transaction witnesses**, each encoded file at most256KiB, for fixed multi-hop shapes in the existing stream. No new provider call, daily repetition or overwrite is allowed. This replaces the2048-byte per-sample limit only for those eight finite slots; other diagnostic limits remain. Diagnostics grant no model, price, scope or funded authority by themselves.
- The new envelope table is additive. Rollback leaves it and the original losses intact; legacy readers ignore it. Existing reserve mismatches, safety, timing, paper-stop and storage guards remain. Mandatory compact proof payload is512bytes per qualifying loss plus provenance/index overhead, not free storage.

## 9. Bounded state-event diagnostics amendment — 2026-10-27

Alpha separately approved the state-witness plan `docs/superpowers/plans/2026-10-27-desk-state-witnesses.md`. This adds a second explicit exception to ordinary 2048-byte diagnostics and RAM-only raw-frame handling; the eight first-only multi-hop slots in section 8 remain unchanged.

- The existing Universe stream may publish diagnostic full frames in `data/desk/pumpswap/state_witnesses/<host-UTC-day>/`. Six fixed event kinds and three exact event/instruction subtype keys have 20 slots per key/day and a hard 200-slot daily ceiling. Each encoded file is at most256KiB; retain three host-UTC days. Nine current keys bound owned data to135MiB over three days; the hard daily ceiling bounds it to150MiB. Foreign files are outside that bound and are never deleted by this writer.
- Published slot accounting survives restart. Failed/oversized attempts may conservatively consume process-local slots. Only the writer's exact owned namespace is pruned; symlinks/foreign names remain protected. Same-directory atomic no-replace publication prevents overwriting or treating a partial file as evidence. The startup-only kill knob is `MEMECOIN_DESK_PS_STATE_WITNESSES` (0 disables capture).
- Preserve raw frame bytes/hash, original reason/event identity, receipt/knowledge clocks and decoder root. An unproven proto3 transaction-index zero remains unknown; a genuinely supplied integer zero is not inherently invalid. All initialization, capture and status failures are contained and counted while original loss/pending/other-frame processing continues. No extra provider call or funded hook is added.
- This is bounded first-N sampling, not exhaustive capture. Common Sweep and same-pool virtual-quote failures can share a bucket; no particular future receipt is guaranteed. Diagnostics never clear a loss, create swap rows, recover reserves/prices, prove effects or change a cohort/model/funded policy. Any later effect proof needs its own reviewed contract. Rollback replaces only the producer code/routing and preserves witnesses and original losses.

## 10. Explicit v28 partial-observation amendment — 2026-10-27

The complete-only flow and prompt-v27 rules above continue for default `pumpswap-facts-v1` and existing v27 cohorts. A separately registered `desk-judge-v28` cohort explicitly selects `pumpswap-facts-v2` and binds its exact system-prompt digest. Its executed contract is `docs/audits/2026-10-27-three-feed-desk-inputs-v28-preregistration.md` at `45895eb569bece68c527cb6438944f5530b1013e`, including Amendment 3; implementation and interpretation are in `2026-10-27-desk-judge-playbook-v28.md` and `docs/superpowers/plans/2026-10-27-desk-v28-policy-and-glue.md`.

- V2 totals include all causal usable seen swaps in the whole half-open 60-second window and carry `lower_bound:true`. `covered_s` is the continuous proven-complete suffix after the last loss, not the span used for totals. Seen totals remain present with zero suffix; no usable seen swaps or unknown tape gives null. Missing buys or sells can reverse observed net flow and direction; never extrapolate uncovered time.
- The 24 five-second eight-column chart bars retain seen partial demand and have aligned `chart_coverage` entries. Unseen or uncovered demand stays null, never zero-filled. Three disjoint retained-receipt counts carry `loss_classes_exclusive:true`; counts are not estimates of missed swaps. Partial zero observed sells remains unknown, while a seen sell proves existence.
- Safety and complete price/range/rail proofs remain conservative. Partial evidence alone does not force a model SKIP. V1/v2 or prompt-digest mismatches refuse before dispatch/admission; both versions keep strict PS paper/RISK quantity and quote handling. One stored unsupported cohort version is returned blind for RISK without hiding other positions; it still refuses entry.
- Preserve old policy/CID bytes, decisions and evidence. Code availability and read-only preview do not register or activate a cohort. New registration needs its own explicit operator execution or exact `--confirm` exception, followed by the existing single-primary drain and pinned paper cutover gates. No funded authority follows from this amendment.

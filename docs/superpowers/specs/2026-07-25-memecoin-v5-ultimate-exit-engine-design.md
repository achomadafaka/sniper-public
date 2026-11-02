# Memecoin V5 Ultimate Exit Engine — combined hardening design

> **SUPERSEDED 2026-07-26 as the go-forward engine design.** V5 is no longer "final"; `V6_BREAK_MANAGER`
> is the single candidate lineage — see the banner on
> `docs/superpowers/plans/2026-07-25-memecoin-v5-final-engine-plan.md`. The engine-agnostic capture/
> execution plumbing described here (evidence contracts, tx-grounded authority, LP severity, quote-
> failure typing, EVM hot lane, V3 depth, fill model) is **retained** as V6's foundation; only
> "V5 as the final decision engine" is retired. V6's current evaluation contract is realistic live
> paper, not replay: `docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md`.

**Date:** 2026-07-25
**Status:** design consensus — approved direction. Phase 5 (legacy deletion) executed early on
2026-07-25 (merge of `cleanup/memecoin-v5-only-runtime`): both preconditions re-verified against
the live DB (0 open real, 29 closed contracts, 0 engine-envelope, 0 frozen engine versions), and
the retired live v3-vs-v5 shadow gate is superseded by old-SHA-vs-new-SHA replay (Phase 4 §9).
Phases 1–4 remain open work.
**Inputs:** GPT Pro external review, Codex code audit, Claude code audit (all 2026-07-25),
each checked against the actual tree at `97ac5558`.
**Builds on:** `docs/superpowers/specs/2026-07-25-memecoin-fast-v5-design.md` (implemented, gated)

## Verdict

One engine named v5, hardened in place. The three reviews converge on four real defects and
one architecture invariant. Everything else GPT proposed is either already built (and often
built better than proposed) or rejected below with reasons.

## Consensus findings — verified in code, all three reviews agree

1. **The global readiness gate blocks the most urgent rule.**
   `memecoin_hot_supervisor.py` `state_fn` requires all four families (price/flow/vol/depth)
   fresh ≤ `MEMECOIN_EXIT_STALE_S` before ANY action. Price/flow/vol age derives from
   `latest_swap_ts`, so after ~3 min of quiet tape a fresh LP pull cannot fire the hot CUT —
   quiet-tape rugs are exactly the target scenario. The 30 s legacy sweep still covers it, but
   the hot lane is defeated precisely when it matters.

2. **Any burn → CUT, no severity.**
   `memecoin_exit_brain.py` CUTs on `depth_pull_flag`; the flag is any `sol_burn`/`v2_burn`
   in the 60 s window (`exit_engine_hot.py`). CLMM fee claims and small LP withdrawals decode
   as same-sign-negative → `sol_burn` → false CUT proposal. Execution is still exact-quote
   gated, so damage is bounded to exiting healthy positions — but at fee-claim frequency that
   bleeds the book.

3. **WS vault-delta evidence is not transaction-grounded.**
   `memecoin_solana_ws.py` `VaultTape` nets per slot with no tx signature. Multi-tx slots can
   net to same-sign → phantom `sol_burn`; mixed slots can contaminate flow sign. The pull lane
   is already tx-grounded (`memecoin_solana_amounts.py` decodes `getTransaction` balance
   deltas, vault-address-keyed, fail-closed on ambiguity, sign rule live-verified 07-24).
   Only the hot lane has this gap — by design ("zero getTransaction on the hot path"), which
   is the design being revised.

4. **Quote failures collapse provider outage and genuine unsellability.**
   `memecoin_real_runner.py` unsellable counter already requires "chart alive but bag won't
   quote" (better than GPT assumed), but a Jupiter-wide outage with healthy DexScreener mids
   advances every position's counter simultaneously. Mostly fails safe (a CUT can't execute
   without a quote either; unactionable-CUT remap cooldown exists) — the recovery-edge case
   is real but narrow.

## Already built — do not rebuild

- Transaction-decoded pull tape (`getTransaction` + `vault_rows()`), program-agnostic —
  deliberately better than GPT's per-program decoder treadmill.
- Exact whole-bag previews gating every execution; hot prewarm every 3 s, 5 s max age
  (`PrewarmedExits`); funded trail ratchets `executable_peak_price`; nominal is display-only.
- Partial executors already request exact action-sized quotes.
- One-shot claims, signed-tx identity persisted pre-broadcast, crash-after-broadcast →
  `submitted_unknown`, no blind replacement.
- Explicit `"confirmed"` commitment; dual-source reconciliation (push WS + 30 s pull verifier
  + divergence counter + 60 s decision-gap fallback + ring rebuild after socket loss).
- Money path is local SQLite only; zero synchronous remote writes on the decision path
  (verified: no supabase refs in runner/materializer/supervisor). **No outbox needed.**
- Evaluation harness: prereg regime, backtest through production policy (`1823eb27`), engine
  attribution in evidence rows, one-row state/outcome linkage, n≥30 promotion gate.

## Disagreements between the audits — resolutions

| Point | Codex | Claude | Resolution |
|---|---|---|---|
| Delete v3 + selector now | Yes, one engine immediately | Keep until gate reads | **Phase 5, not Phase 1.** Contracts freeze `decision_engine_version` at reservation (`memecoin_funded_policy.py:140-152`); deleting v3 code while any open contract froze v3 (or predates the field → parses v3) strands those positions on the envelope rails. Delete after (a) hardened-v5 promotion passes and (b) zero open pre-v5 contracts. End state = Codex's. |
| `BLOCK` replaces `DEFER_TO_LEGACY` | Rename now | Rename at cutover | **At cutover.** Today DEFER routes to the full live rails (trail/TP/derisk/catastrophe), not just the envelope. The rename is only truthful once v5 IS the policy and "legacy" = the immutable envelope. |
| Tx-ground scope | ALL sale-authorizing evidence | Burns only | **Codex's invariant, adopted:** vault-delta-only evidence may wake the engine and prewarm quotes; it may never authorize CUT / DERISK / TAKE_PROFIT. Slot-netted deltas match the frozen per-slot VWAP grain, so they remain fine for RIDE/wake/structure display. Sales are principal actions; the getTransaction round-trip (~100-300 ms) is paid only when a sale is proposed, in parallel with the already-prewarmed quote. |
| Discrete 25/50/75 tiers | Adopt | Keep continuous (preregistered) | **Neither is settled by argument.** The hardening re-cuts the prereg anyway; sizing form (continuous conviction vs 3-tier) is decided by the same replay harness that registers LP-severity boundaries. Register the winner. |
| vol-None → 0.5 fraction | Remove, "should be impossible" | Unreachable today (vol is a required family) | **Both partly right.** Unreachable under the current global gate — but per-rule evidence contracts (Phase 1) make it reachable for time-DERISK. Fix by construction: each rule's sizing inputs are part of its evidence contract; time-DERISK gets its own registered fraction, never a vol-fallback. |
| Analytics outbox | Keep (softened) | Reject | **Reject.** Decision path is local SQLite end-to-end. Revisit only if a synchronous remote write ever lands on the money path. |
| Token-authority monitor | Excluded from immediate | Take, observation-only | **Take, observation-only** (Phase 4). ≤7 held mints × one `getAccountInfo` per poll is nearly free; shadow-first matches doctrine. No funded authority without collected evidence — consistent with Codex's exclusion. |

## Hard invariants (the ultimate engine)

```text
1. Vault-delta-only evidence may wake v5 and prewarm quotes;
   it may never authorize CUT, DERISK, or TAKE_PROFIT.
2. Each rule names its own required evidence families, freshness caps, and sizing inputs.
   No rule is blocked by a family it does not read. No sizing input falls back across rules.
3. LP withdrawal is classified by severity (removed fraction + executable route degradation),
   never CUT on existence alone. Boundaries preregistered from decoded historical burns.
4. Only token/route-specific quote failures advance unsellability
   (NO_ROUTE, INSUFFICIENT_LIQUIDITY, TOKEN_REJECTED, SIMULATION_REVERT).
   Infrastructure failures advance a separate operational counter and alert.
5. The funded envelope (catastrophe, unquotable, timecap), exact-quote confirmation,
   one-shot claims, and signed-tx identity remain unchanged and unbypassable.
6. Stale evidence never gains money authority because the market is quiet
   (hard per-family freshness caps; no soft decay).
```

## Phased plan

### Phase 1 — evidence truth (blockers before hot-lane / v5 activation)

1. **Per-rule evidence contracts.** Replace the single all-families gate in the decision path
   with per-rule requirements:
   - LP CUT: depth family fresh + tape complete + route health. NOT fresh swaps.
   - Unsellable CUT: classified route-failure streak (see 4).
   - Structure CUT: fresh tx-grounded price, confirmed break, negative decoded flow.
   - TAKE_PROFIT / vol-DERISK: structure + vol + arrival + executable route.
   - Time DERISK: position age + no-new-executable-high (age alone is insufficient).
   - RIDE: trusted structure + flow + sellability.
   - Anything missing/contradictory → defer (envelope + live rails remain authoritative).
2. **Transaction-ground sale authority on the hot lane.** Capture the triggering signature
   (logsSubscribe per vault if the free endpoint supports it — measure first; fallback:
   `getSignaturesForAddress` + `getTransaction`, both already exist in the pull tape) and
   decode through the existing `vault_rows()` before any hot decision becomes executable.
   Account deltas keep doing wake + prewarm. Phantom-burn and flow-sign contamination die here.
3. **LP-removal severity.** `liquidity_removed_fraction` from burn delta vs pre-burn reserves
   (both already in the rows; `sol_sync` carries post-balances) + whole-bag route degradation
   from the prewarm quote we already fetch. minor → observe/refresh route; material → DERISK
   if executable depth materially worsened; catastrophic → CUT. Boundaries preregistered from
   the decoded historical burn tape — no invented 20/50/80.
4. **Typed quote-failure classification** at the two seams feeding the unsellable counter and
   `executable_exit_allowed()` (typed result instead of `None`). Categories per Codex §8.
   Only the four token-specific categories advance unsellability. Defense in depth: if every
   held position's exact quote failed in the same sweep, classify as infrastructure and
   advance nothing.

### Phase 2 — sizing and controller state

5. **Per-rule sizing contracts.** DERISK sized by risk evidence (persistence, route
   degradation, remaining depth); TAKE_PROFIT by gain/exhaustion/route fragility; time-DERISK
   by its own registered fraction. Continuous-vs-tier form settled by replay, then registered.
   Final fraction = min(policy fraction, max-fraction-inside-slippage-cap, remaining partial
   budget). One partial exit per position stays until replay proves multiple tranches.
6. **Minimal persisted controller state** — extend the position row (pattern already exists:
   `derisked_at`, `exact_quote_misses`, `executable_peak_price`), not a new subsystem:
   `structure_break_since`, `consecutive_break_events`, `negative_flow_since`,
   `route_degradation_since`, `route_failure_streak`, `infra_failure_streak`. Survives
   restarts by construction. Structure CUT requires two decoded events or a registered
   persistence window; catastrophic rules stay immediate.

### Phase 3 — execution binding (verification-first)

7. **Audit the claim→sign chain bindings** against Codex's attempt-record list
   (decision_state_id, position revision, balance snapshot, quote digest, min_output,
   blockhash validity, lifecycle). Most exist; add missing fields to the existing claim/
   attempt persistence. Before signing: re-read position, verify claim ownership + revision +
   balance, reject superseded decision state. Rebroadcast only same signed bytes while valid.

### Phase 4 — observation and evaluation

8. **Token-authority monitor, observation-only:** mint/freeze authority, Token-2022
   transfer-fee/permanent-delegate changes on held mints, logged as risk events. Promotion to
   CUT rules only after paired evidence review.
9. **Replay old-SHA vs new-SHA** from isolated worktrees on the same immutable tape (no
   side-by-side engines in the final tree). Metrics: net executable exit value,
   catastrophic-loss rate, false/late CUT rate, upside retained after partial, route-failure
   classification accuracy, submitted-unknown rate, decision-to-sign and broadcast-to-confirm
   latency, rule/fraction attribution. Then paired forward paper → risk-bounded canary →
   authority.

### Phase 5 — legacy deletion (cutover-gated)

10. **Preconditions:** hardened-v5 promotion passed AND zero open contracts with frozen
    `decision_engine_version` != v5 (checkable at deploy; contracts freeze at reservation).
    **Then:** delete `propose`, `propose_v3`, `ENGINE_VERSION_V2/V3`, `exit_engine(version)`,
    `MEMECOIN_EXIT_ENGINE_VERSION`, shadow comparison, dual-version replay. Rename
    `DEFER_TO_LEGACY` → `BLOCK` (envelope-only authority). Auditability via immutable
    `policy_revision` / decision hash, not engine names. Closed v2/v3 DB rows remain readable
    history.

## Preregistration requirements (before funded authority)

From replay over the decoded historical tape, register: LP-severity boundaries, structure-CUT
persistence window, per-rule sizing form and values, time-DERISK fraction and no-progress
window, unsellable streak thresholds per failure class.

## Explicitly excluded

- No V5.1/V6 naming; one engine, revisioned by `policy_revision`.
- No five simultaneous fraction quotes per tick (whole-bag prewarm + lazy exact partial
  quote after a proposal).
- No soft freshness decay; no action on stale evidence.
- No per-program decoder framework for ordinary swaps (balance-delta decode is authoritative);
  instruction-level proof only where semantics demand it (LP removal, fees, migration).
- No round-number rewrites of measured thresholds (per-band time-to-peak medians stay
  data-linked).
- No multiple partial tranches, wallet-graph/regime/LLM inputs, or emergency
  processed-commitment path (confirmed push is median 404 ms).
- No deploy, activation, `.env`, or flag change as part of implementation. Hot lane and v5
  authority stay OFF until the promotion sequence passes.

## Risks

- logsSubscribe availability on free endpoints is unmeasured — probe before committing;
  the getSignaturesForAddress fallback is already proven in the pull tape.
- Transaction confirmation adds latency to sale authority — measure the true
  decision-to-sign delta after grounding; wake+prewarm parallelism should absorb most of it.
- LP-severity boundaries depend on sufficient decoded historical burn volume; if the tape is
  thin, extend collection before registering.

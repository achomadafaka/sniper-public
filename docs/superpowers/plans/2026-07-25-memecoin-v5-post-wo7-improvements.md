# V5 Post-Batch Improvements — V5-WO-11 .. V5-WO-16 (rev 2)

> **SUPERSEDED 2026-07-26.** These are V5-decision-engine improvements; V5 is no longer the go-forward
> engine (`V6_BREAK_MANAGER` is the single candidate — see the banner on
> `docs/superpowers/plans/2026-07-25-memecoin-v5-final-engine-plan.md`). Any work order here that is
> pure capture/execution plumbing (not a V5 decision rule) may be re-homed as a V6 prerequisite; the
> V5-policy-tuning items are dropped. V6 evaluation is the live-paper position lifecycle in
> `docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md`, not replay or a V5 arm.

**Date:** 2026-07-25 (rev 2 — supersedes rev 1 in place after Codex design review; all corrections
verified against code before adoption)
**Numbering:** prefixed `V5-WO-` — `2026-07-25-engine-improvement-workorders.md` already owns
plain WO-11..13.
**Ground rules:** identical to the main plan — one engine, all invariants, degenerate-until-
registered, branch-per-WO, reviewer merges, glob-sweep verification
(`tests/test_exit_* tests/test_memecoin_*`). No deploy/flags/.env/production-DB mutation.

## Operator authorization record (2026-07-25)

The operator explicitly authorized **read-only RPC** for this program: `getTransaction`,
`getSignaturesForAddress`, `eth_getLogs`, block headers, and WS subscription probes, on read
endpoints only — never signing, broadcasting, approvals, or `MEMECOIN_SOLANA_RPC_URL`. Scope:
fixture capture, historical backfill, and transport measurement. Live *measurement sessions*
(V5-WO-12B) are additionally announced to the operator before they run. This is a recorded
operator decision, not a standing self-grant; it does not extend to any state-mutating call.

## Sequencing (revised)

1. **V5-WO-11 + V5-WO-12A fold into the CURRENT builder batch, before its WO-7 work** — WO-11
   changes retained evidence (replay/live parity depends on it) and both touch files the builder
   already owns this batch. Handing them to a parallel branch would recreate the
   shared-`real_exit_sweep` landmine.
2. Current batch review + merge.
3. WO-7 replay/readiness (with WO-11's retention contract in place).
4. V5-WO-12B (measurement-gated; operator announced).
5. V5-WO-13 policy registry + evidence cadence.
6. V5-WO-14, then V5-WO-15 (research-only), then V5-WO-16 (after append-only evidence exists).

---

### V5-WO-11 — Ring retention as a completeness contract (BEFORE WO-7)

**Why:** `exit_engine_hot._MAX_EVENTS = 4000` count-bounds the ring on an assumed cadence while
features are time-windowed. But naive time-eviction ("largest window × margin") is wrong in four
ways found in review, so the retention rule is a contract, not a formula tweak.
**Change:**
- Introduce a **registered finite structure window** first (prereg'd constant): pivots currently
  consume the entire retained path, so any eviction silently changes `prior_low`/`prior_high` and
  therefore structure-CUT behavior. No eviction lands before the structure window is registered.
- Retention:
  `REQUIRED_HISTORY_S = max(VOL_WINDOW_S, 2 × ARRIVAL_WINDOW_S, DEPTH_WINDOW_S,`
  `REGISTERED_STRUCTURE_WINDOW_S)` — the 2× on arrival is not margin: arrival *acceleration*
  compares two adjacent windows (`rate_now` vs `rate_prev`). No arbitrary safety multipliers.
- Evict against the **maximum observed event-time watermark**, not the incoming event's
  timestamp — corroboration legally inserts older decoded rows.
- `_MAX_EVENTS` survives only as a memory backstop. **If the backstop ever fires, set
  `history_incomplete` on the engine and block sale authority until the ring is rebuilt from the
  pull-complete tape** — a burst that overruns memory must degrade to the pull lane, never to
  silently truncated evidence.
- `window_slots()`/provisional accounting audited against the new retention (it currently treats
  every provisional event in the ring as potentially influential).
**Tests:** burst overrun → `history_incomplete` → no sale authority → rebuilt → authority
restored; corroboration inserting older rows never advances eviction; per-window starvation cases
for vol / double-arrival / depth / structure.
**Why before WO-7:** replay/live parity requires identical retained evidence on both sides.

### V5-WO-12A — Non-blocking hot-lane scheduler (UNCONDITIONAL — the defect is already proven)

**Why:** `memecoin_hot_supervisor.py:523-530` calls every `preview_fn` **synchronously inside the
asyncio housekeeping loop**. One slow quote HTTP call blocks WebSocket frame processing AND the
pull-fallback trigger — the same defect class already fixed in the runner pre-pass, still live in
the supervisor. No stress test is needed to justify fixing structurally wrong code.
**Change:** previews off the event loop (bounded thread concurrency), hard per-call deadlines,
stale-prewarm coalescing (skip a refresh if a newer one is queued), and **priority lanes**:
submitted-transaction reconciliation → exact sale confirmation → transaction decode → pull
fallback → prewarm. Prewarm must never starve the fallback.
**Tests:** a preview stalled at the deadline does not delay WS frame processing or the fallback
trigger; priority inversion cases; coalescing under backlog.

### V5-WO-12B — Provider diversity + logsSubscribe (measurement-gated)

**Why:** correlated stress (multiple positions rugging together) can rate-limit the shared read
path exactly when speed matters. Degradation today is safe (pull + envelope, WO-4 outage scope);
this buys the speed back — but only where measurement says it binds.
**Change:**
1. Stress measurement (announced to operator; read-only): all 7 held positions firing
   simultaneous decode+prewarm+preview bursts; record rate-limit onset and which **call class**
   saturates first.
2. If it binds: provider diversity **per call class** — Solana RPC, quote aggregation, and EVM
   RPC are different failure domains with different budgets. **Never continue one
   signature-pagination walk across providers** (cursors are provider-consistent only); a
   provider switch restarts the walk or fails closed.
3. `logsSubscribe` fast-path for signature capture: one mentioned pubkey per subscription,
   multiple subscriptions per socket; current `getSignaturesForAddress` path stays as fallback.
**Acceptance:** decode-to-decision p50/p95 under the burst, before vs after; all fail-closed
paths unchanged.

### V5-WO-13 — Policy registry + evidence cadence (statistics made safe)

**Why:** rules read at different speeds and constants must graduate from degenerate mode on a
defined cadence — but the rev-1 procedure ("fail held-out → wait → try again") was **optional
stopping**: repeated testing until something passes. That is how false constants get registered.
**Change:**
1. **One held-out read per immutable policy revision.** A revision = the COMPLETE policy (every
   registered constant + which rules remain degenerate), frozen with a canonical policy hash. A
   failed revision is REJECTED — permanently. A later attempt is a NEW preregistered hypothesis
   and revision on a non-overlapping evidence window. If genuinely repeated reads are wanted,
   use a predefined sequential-testing rule (registered alpha-spending), not ad-hoc retries.
2. Replay evaluates the **full policy with independent inventory** — never isolated rules;
   precedence and partial-exit interactions are part of the policy under test. "Tranches" mean
   which constants a revision moves out of degenerate mode, not per-rule holdout reads.
3. **Contracts freeze the policy hash and every registered parameter** — `decision_engine_
   version="v5"` alone is insufficient once constants exist. A position is governed by exactly
   the revision it reserved under.
4. The floors/evidence report (cron or script, read-only) may DETECT that floors are met; it
   must never automatically read holdout data or mutate policy. A read is a deliberate,
   operator-visible act.
5. Acceptance explicitly allows `CODE_COMPLETE_EVIDENCE_BLOCKED`; "cadence exists" never forces
   a premature read.
6. Pre-registered question for the first revision, updated to builder commit `73cbcc57` (which
   already implements: one prior partial suppresses further material-LP DERISKs; catastrophic
   still CUTs): **should material-LP-removal after a prior partial escalate to CUT instead of
   being suppressed?** Test both arms in replay; register the winner.
7. **Multi-tranche partials are OUT of this WO** — a future standalone work order: it changes
   persistence, claims, recovery, executor behavior, and replay inventory, not just a constant.
8. Runtime selection is two-file and fail-closed under the state root: the append-only accepted
   revision ledger is `data/memecoin_v5_policy_registry.jsonl`; the operator pointer is
   `data/memecoin_v5_active_policy.json` containing only `{"policy_sha256":"..."}`. The pointer
   cannot manufacture acceptance. An armed runner with an engine-authoritative chain but no accepted
   active revision, or with parameter drift, fails startup; reservation also fails closed. Each
   engine-envelope contract freezes the accepted hash and complete
   manifest; missing pointer, changed active hash, or live registered-parameter drift demotes an
   existing envelope to fixed envelope rails only. The runtime never silently evaluates an old
   position under new V5 constants and never installs a second engine implementation.

### V5-WO-14 — Token-authority monitor (observation-only; unchanged verdict, sharpened spec)

**Why:** entry-time token safety is checked once; freeze/mint authority, Token-2022 transfer-fee
config, and permanent delegates (which can transfer/burn from any token account of the mint) can
change mid-hold. No rule without an event corpus — collect first. ≤7 mints × one read per poll.
**Change:** **reuse the existing parsing in `sniper/execution/solana_token_safety.py` — no second
Token-2022 parser.** Store: one deduplicated snapshot per mint + **append-only change events**
linked to affected positions, each carrying RPC context slot, explicit commitment, provider,
corrected observation time, and a raw-response hash. The initial observation is a **baseline**,
never a change event. An unreadable response is an **evidence gap**, never an inferred change.
Worker structurally absent from the decision brain and funded adapter (grep-provable).
**Promotion path:** collected events paired with position outcomes → a CUT rule may be proposed
through V5-WO-13. Not before.

### V5-WO-15 — Entry-quality attribution CANDIDATE (research-only; does NOT replace the gate)

**Correction adopted:** the current arming input `alpha_b_copy_stats()` is not naive — it uses
actual closed paper positions and already excludes fomo-priced fills. It stays the approval
metric.
**Change:** build the WO-7 fill-modeled per-wallet attribution as a **candidate metric displayed
beside** the honest paper PnL: fixed notional, explicit entry-lag treatment, chain/band
stratification, censored-position handling, sample floors, and uncertainty bounds. No automatic
approve/revoke; no production arm/revoke cycle in acceptance. Decay *surfacing* (a wallet whose
trailing candidate metric turns negative gets flagged for review) is allowed — surfacing only.
**Promotion path:** replacing the approval input requires separate forward validation and
explicit operator approval. Until then this is analytics.

### V5-WO-16 — V5 future-context observation corpus (renamed; deferred until honest history exists)

**Correction adopted:** the current holder-evidence persistence **overwrites current state by
primary key** — it cannot reconstruct whether a co-holder had exited at a historical decision
time, so joining it to past decisions would fabricate context.
**Change (prerequisites first):** append-only holder-evidence history; point-in-time joins using
only observations at-or-before the decision; freshness, provenance, and explicit unknown values;
a verified causal SOL-market feed or `blocked_missing_feed` stored in its place. Only then:
source-wallet exit state (referenced from the mirror path, not recomputed), trusted co-holder
exit counts, and market context recorded on candidate-decision evidence rows — same
never-read-by-rules discipline as episode/regime/deployer.
**Hard rule:** observation-only; any rule wiring goes through a future prereg. Keep this last.

---

## Explicitly out (unchanged)

Per-token engine processes/sockets; dynamic freshness decay; fitted flow magnitudes;
round-number rewrites of measured constants; any new engine version (the observation corpus is
V5's, not a "V6"); multi-tranche partials (future standalone WO); any activation as part of
implementation.

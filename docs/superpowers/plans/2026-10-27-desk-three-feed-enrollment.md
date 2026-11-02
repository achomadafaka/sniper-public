# Three-feed paper intake implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task.

**Goal:** Let Pump.fun, Fomo and DexScreener supply candidates to one new frozen paper cohort.

**Architecture:** Freeze optional `hard_filter.intake.feeds`; absence preserves legacy Pump.fun-only policy bytes. Resolve emitter sources from the active cohort and enforce enrollment before ingestion and again at the hard filter. Keep the existing v26 prompt, collection cadence and one primary cohort.

**Tech Stack:** Python, SQLite, existing collectors and pinned worker watchdogs.

**Spec:** `docs/superpowers/specs/2026-10-27-desk-intake-candidate-path-design.md`, amended by the direct operator request to enroll Fomo and DexScreener; new preregistration `docs/audits/2026-10-27-three-feed-desk-cohort-preregistration.md`.

## Global constraints

- Paper only; $25 clip, five positions, 40 JUDGE calls/hour shared across feeds, 12s deadline, 40 real SOL minimum, $5k–$50M cap safety and 8% round-trip bound unchanged.
- No funded path call/lock/retry or provider polling increase. No funded restart, signing, money, `.env` edit or database backup.
- Registration `--confirm` is operator-only. Prepare the exact reviewed preview and command; do not run it.
- Preserve old cohort and all cursors/cooldowns/claims. Do not replay prior emissions into the new cohort. Drain old pending work before the primary switch.
- New source enrollment changes the policy hash and study denominator. Preserve feed/filter/list-position context and coin-clustered inference; paid Dex promotion is not trending evidence.
- Existing tape-gap behavior and cumulative-paper-loss enforcement defect are separate, unresolved work; do not silently change either.

## Review focus

- Legacy cohort must reject newly enrolled feeds even if a producer is misconfigured.
- Empty, duplicate, unknown, malformed or noncanonical feeds must fail before either registry writes.
- Resolver failures inside the worker disable intake without stopping capture; earlier malformed registry/band startup guards remain authoritative.
- Non-Solana/stale/future/unknown-clock rows and pre-listing prints must not emit.
- Overlapping feeds retain separate identities, cooldowns and provenance; restart replay does not create another primary ticket.

### Task 1: Freeze and enforce enrollment (root)

**Files:** contract, provision script, `memecoin_desk.py`, `db_memecoin_desk.py`, new `tests/test_desk_intake_feed_policy.py`, and focused assembler/seam tests.

**Interface:** `contract.intake_feeds(hard_policy) -> tuple[str, ...]`; legacy valid intake returns `('pumpfun',)`, malformed/no-intake raises `ValueError`. New nested `feeds` is a nonempty sorted unique list drawn from the existing three feed enums.

- [ ] RED: legacy rejects Fomo/Dex; explicit all-three passes; malformed registration leaves both stores empty; CLI permutations give one CID and omitted option preserves legacy bytes.
- [ ] Implement minimum shared validation, ingest guard and hard-filter guard; add `--intake-feeds FEED ...` only with `--intake`.
- [ ] GREEN: focused policy/contract/store/provision/assembler checks with `-n 2`.

### Task 2: Read sources from the active cohort (worker)

**Files:** intake book, universe service/worker, new `tests/test_desk_intake_feed_emitter.py`.

**Consumes:** Task 1 `intake_feeds(hard_policy)`. **Produces:** startup-selected explicit feeds for the existing off-path local reload.

- [ ] RED: explicit three-feed cohort reads all sources; legacy remains Pump.fun-only; invalid configured cohort fails closed.
- [ ] Implement startup cohort resolution and pass feeds to `IntakeBook`, retaining the narrow legacy module default.
- [ ] GREEN: emitter/universe/overlap/clock/restart tests and deployment treewalk with `-n 2`.

### Task 3: Review, stage and prepare cutover (root)

- [ ] Extend the offline real intake→mint evidence→JUDGE-sheet seam across all three feeds.
- [ ] Freeze the new source set and study rules in the new preregistration before registration.
- [ ] Independent exact-SHA review; schema/ledger checks and Knowledge Sync Gate.
- [ ] Push merged source, stage required pinned release checks without funded cutover or backup, retain release.
- [ ] Produce exact operator registration command and bounded paper-only cutover/rollback record. Activate only after the operator registration step and fresh old-work drain checks.

Rollback restores only the paper role pins/cohort routing. Keep all audit data, old/new registry rows and original clocks; never delete/relabel prior tickets to make a cutover pass.

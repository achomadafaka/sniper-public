# Engine Work-order Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the funded full-close crash invariant and turn the incomplete WO-1..12 surfaces into honest, production-shaped implementations without activation.

**Architecture:** Real full exits use a durable claim-token state machine parallel to the proven de-risk lifecycle, with write-ahead signed identity and atomic fill/finalize. Deployment verification is split into clearly labelled synthetic checks and a mandatory pinned-release mode. Shadow, hot-lane, tiering, and cockpit data cross explicit adapters and remain default-OFF or zero-authority.

**Tech Stack:** Python 3, sqlite3/WAL, pytest, Flask, asyncio/websockets, existing Sniper runner/executor seams.

## Global Constraints

- No flag flips, `.env` edits, service restart, deployment, live RPC, or production DB mutation.
- Use corrected UTC for persisted times.
- TDD: observe RED before each production change.
- Explicit-path staging; one logical change per commit.
- No full suite; run funded-path and changed-surface suites plus schema/diff checks.

---

### Task 1: Durable real full-close state machine

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/execution/solana_exit_executor.py`
- Modify: `sniper/execution/evm_exit_executor.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Test: `tests/test_crash_windows.py`
- Test: `tests/test_memecoin_exit_executor.py`
- Test: `tests/test_memecoin_evm_exit_executor.py`

**Interfaces:**
- Produces: `claim_position_close`, `mark_position_close_submitted`,
  `reset_position_close_submission`, `release_position_close_claim`, and
  `book_full_exit_and_finalize`.
- Invariant: any durable `close_sig` blocks every later claim and broadcast.

- [ ] Add Solana and EVM crash-after-broadcast tests that restart on a new connection and assert a
  durable signature, `submitted_unknown`, zero claim release, and exactly one total broadcast.
- [ ] Run those tests and confirm failure because full exits currently persist no signed identity.
- [ ] Add close-state columns and token-qualified persistence transitions.
- [ ] Move real executor claim before signing, persist signature before send, and atomically book/finalize.
- [ ] Replace orphan reopening with quarantine/finalize-only recovery.
- [ ] Run crash and executor suites; commit explicit paths.

### Task 2: Pinned-release deployment smoke

**Files:**
- Modify: `scripts/engine_smoke.py`
- Modify: `scripts/memecoin_exit_state_worker.py`
- Test: `tests/test_engine_smoke.py`

**Interfaces:**
- Produces: `engine_smoke.py --synthetic` and mandatory deployment mode requiring
  `--release-dir PATH --state-root PATH`.
- Deployment verdict fails on missing release import, child launch, descriptor mode, chain freshness,
  flag, kill-switch, snapshot, or evidence-chain checks.

- [ ] Add failing tests for skipped-live PASS, missing release-dir, imports from the wrong checkout,
  and manually fabricated evidence rows.
- [ ] Implement distinct synthetic/deployment verdicts and fail mandatory skips.
- [ ] Launch the release worker in bounded no-RPC smoke mode against temp copies and inspect its FDs.
- [ ] Run smoke tests and the synthetic command; commit explicit paths.

### Task 3: Wire shadow feedback and correct research semantics

**Files:**
- Modify: `scripts/memecoin_exit_state_worker.py`
- Modify: `sniper/services/memecoin_exit_materializer.py`
- Modify: `sniper/analysis/episode_context.py`
- Modify: `sniper/analysis/market_breadth.py`
- Modify: `sniper/analysis/exit_flex_v5.py`
- Modify: `sniper/analysis/memecoin_wallet_graph.py`
- Modify: `scripts/research/engine_decision_report.py`
- Modify relevant focused tests.

**Interfaces:**
- Produces one decision/context row per position/pass in the exit-state DB.
- Context functions accept an as-of boundary; unavailable identity remains null with a data gap.

- [ ] Add failing integration tests proving the worker emits episode/regime/v5 context and uses the
  Solana tape DB.
- [ ] Add a failing CUT counterfactual test whose future comes only from continuing pool ticks.
- [ ] Make episode context causal and include LP adds/removes.
- [ ] Remove the non-router-maker-as-deployer claim and pass the actual pool into `sig_fetch(pool)`.
- [ ] Implement the worker adapter and tape-backed full-exit counterfactual.
- [ ] Run research/materializer tests; commit explicit paths.

### Task 4: Wire default-OFF hot lane and execution tiering

**Files:**
- Modify: `sniper/services/memecoin_hot_supervisor.py`
- Modify: `sniper/services/memecoin_solana_ws.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Modify: `sniper/execution/solana_exit_executor.py`
- Modify: `sniper/execution/evm_exit_executor.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/settings/memecoin.py`
- Modify relevant focused tests.

**Interfaces:**
- Hot lane is entered only when `MEMECOIN_HOT_EXIT_ENABLED=1`; disabled behavior is byte-identical.
- Executors accept tier settings through existing seams and persist one exit-attempt measurement row.

- [ ] Add failing tests for enforced pull fallback, receipt-time latency, prewarm consumption, runner
  default-OFF behavior, tier arguments reaching executors, and exit telemetry persistence.
- [ ] Implement the smallest adapters and dedicated exit telemetry table.
- [ ] Run hot/tiering/runner/executor tests; commit explicit paths.

### Task 5: Correct cockpit semantics and dashboard rendering

**Files:**
- Modify: `sniper/services/memecoin_engine_cockpit.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify the existing memecoin dashboard asset identified by the route/template search.
- Test: `tests/test_engine_cockpit.py`

**Interfaces:**
- Returns engine-envelope positions only.
- `distance_to_catastrophe` uses executable price and frozen catastrophe percentage; missing exact data
  returns null with an explicit status.

- [ ] Add failing service and route tests for cohort filtering, threshold distance, missing exact data,
  and rendered decision rows.
- [ ] Implement service/route/UI changes without a new auth surface.
- [ ] Run cockpit/API tests; commit explicit paths.

### Task 6: Verification, review, and integration

**Files:** all changed files plus Knowledge Sync artifacts required by repository policy.

- [x] Run changed-surface and funded-path pytest commands.
- [x] Run `python3 scripts/validate_schema_refs.py`, warning mode, and `git diff --check`.
- [x] Re-run the direct crash reproduction and deployment smoke modes.
- [x] Review the complete diff against this design and original work orders; fix every Critical/Important finding.
- [x] Complete Knowledge Sync with an explicit delta statement.
- [ ] Commit, merge to `main`, push, and remove only this owned worktree/branch.

## Knowledge Sync

1. Did this branch change project doctrine? **y** — `AGENTS.md` now requires durable signed full-close identity before broadcast.
2. Did it add or retire a module or subsystem? **y** — `db_memecoin.py` adds the durable close lifecycle and exit-attempt ledger.
3. Did it change analyzer or strategy-audit behavior? **n** — wallet analyzer and strategy-audit modules are untouched.
4. Did it change tests or validation commands? **y** — crash, smoke, materializer, hot-lane, tiering, report, and cockpit regressions changed.
5. Did it introduce a new workflow or skill? **y** — `scripts/engine_smoke.py` now defines explicit synthetic and pinned-release deployment modes.
6. Did it supersede existing docs/specs/plans? **n** — the remediation contract extends the existing work orders without retiring them.
7. Did it create a new bug lesson or semantic fix? **y** — signed identity must be durable before broadcast; hot authority requires current complete depth evidence.
8. Did it affect Supabase schema/write surface/RLS/migrations? **n** — only local SQLite schemas changed.
9. Did it create a new wallet insight, thesis, research finding, or simulation candidate? **n** — no live study or strategy claim was added.
10. Did it change what agents should know before working in this area? **y** — the new spec and funded-close invariant are indexed in `AGENTS.md`.
11. Did it change legacy/frozen status of any unit? **n** — no legacy unit was reactivated or retired.

**Delta:** yes
**Local file updates:** `AGENTS.md`; this design and implementation plan
**Supabase entries created:** none; audit-only mode cannot cross the explicit write gate
**Supabase entries proposed:** `architecture/memecoin/durable-exit-engine-remediation-2026-07-25`; `bug_lesson/memecoin/signed-identity-before-broadcast-2026-07-25`
**Supabase entries superseded:** none
**Open questions surfaced:** proposed memory upserts require a separate explicit `--mode supabase_commit_entries` operator invocation
**Attestation:** I ran the 11-question Knowledge Delta Report; the answers above are the result.

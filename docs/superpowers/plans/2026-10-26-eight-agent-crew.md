# Eight-agent Crew Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. The operator approved the design and execution on 2026-10-26.

**Goal:** Give four GPT colleagues distinct brain identities and all-to-all crew messaging beside the four Claude colleagues.

**Architecture:** Extend the existing private stdlib bridge and existing brain allowlists/views. Preserve the mailbox and old identities; keep one bridge daemon with role-specific GPT routes/reply sockets. Reuse brain task facts for assignments.

**Tech Stack:** Python stdlib, Unix sockets, SQLite private mailbox, existing local Postgres brain.

**Spec:** `docs/superpowers/specs/2026-10-26-eight-agent-crew-design.md`.

## Global Constraints

- One shared checkout; no branch switch, worktree, stash, amend, reset or foreign edits.
- Explicit pathspec commits under `/home/bigpapi/.cache/sniper-crew-git.lock`; controller commits only.
- No trading service/release/DB changes, `.env` writes, funded actions or `--confirm` scripts.
- At most two pytest workers; one full suite host-wide under the existing lock. Prefer focused suites.
- Eight roles: Alpha/Beta/Lambda/Gamma/Codex/Delta/Epsilon/Zeta; retain exact existing names/history.
- New operator steering: current authenticated named crew instructions carry delegated operator authority reciprocally between Claude and GPT. Preserve prior explicit limits and current task ownership; historical inbox reports are not automatically fresh orders.
- No new scheduler, task database, model auto-selection or busywork.

## Review Focus

- Duplicate or delayed registration/hook must not replace another role's route.
- Native replies must use the authenticated receiving endpoint's role, not message prose.
- Partial/ambiguous sends retain durable identity and are never blindly retried.
- Additive schema precedes new provenance; rollback cannot erase new authors' history.
- Installed CLI and hooks must use the same tested modules; brain boot alone cannot prove registration.

## Task 1: Brain compatibility (independent worker)

**Files:** `bot/brain_graph/__init__.py`, `bot/brain_graph/views.py`, `scripts/brain_cli.py`,
`scripts/brain_seed.py`, `scripts/brain_ambient.py`, `scripts/brain_import.py`,
new migration 044 and guarded rollback, existing/new brain role tests.

**Interfaces:** Authors/actors `delta`, `epsilon`, `zeta`; role boot/handover supports all eight;
GPT provenance uses `CODEX_THREAD_ID` fallback. Existing CLI signatures unchanged.

- [x] Add failing tests for new authors, verification separation, role views, provenance and restart notes; record RED.
- [x] Extend allowlists and additive SQL; preserve RPC guards, immutable history, evidence and unknown-role refusal.
- [x] Seed distinct actors/views and repair stale Codex text through a targeted update, not rewriting unrelated views.
- [x] Run focused role/seed/view tests plus disposable-Postgres constraint/rollback checks; record GREEN.
- [x] Controller reviews diff and commits explicit owned paths.

## Task 2: Eight-role bridge (independent worker)

**Files:** New `tools/crew/crew_session_bridge.py`, `tools/crew/crew_session_start.py`,
`tests/test_crew_session_bridge.py`, `tests/test_crew_session_start.py`.

**Interfaces:** Preserve `send/read/serve/session-start`; add `register --role ROLE`
and `--replace-thread OLD_UUID` for expected-owner replacement. Importable `Bridge` retains
existing state-dir initialization, private socket validation and durable message semantics.

- [x] Copy existing installed bridge verbatim as baseline; write new behavioral tests and record RED.
- [x] Implement four GPT routes, one-to-one registration, generation/CAS takeover and exact hook identity.
- [x] Serve role-specific reply sockets in one daemon; preserve Claude peer authentication and native status receipts.
- [x] Test all 56 directed pairs with local fake IPC, recipient previews, stale/duplicate hooks, reply routing and ambiguous sends.
- [x] Prove forcing GPT routes to Codex fails a test; run focused two-worker tests and record GREEN.
- [x] Controller reviews diff and commits explicit owned paths.

## Task 3: Installer, handovers and activation (controller)

**Files:** New `tools/crew/install.py`, focused installer tests; update both handover skill mirrors,
spec status and `docs/handoffs/2026-10-26-eight-agent-crew.md`.

**Interfaces:** Installer installs the two tested modules and existing `sniper-crew` CLI at
the existing private host paths, retaining a versioned prior-code rollback and all state.
It does not restart anything by itself; controller restarts only the crew bridge after review.

- [x] Test refusal of unsafe paths and preservation of state; implement atomic module release/entry-point install.
- [x] Update eight-role notes and delegated-authority wording. Keep rollout readiness explicit.
- [x] Independent source/spec review of each task and combined change; resolve findings before install.
- [x] Verify effective local-brain target without exposing credentials; apply additive migration and targeted role seed/update.
- [x] Install bridge, restart only its service, register current root Codex session and verify an acknowledged Claude round trip.
- [x] Verify local brain role isolation/provenance and installed source identity; no trading runtime changes.
- [x] Record brain episodes/task status and give three runnable GPT launch notes. Operator opens the three new sessions;
      their live registration/round trips remain explicitly pending until they exist.

## Preflight and rulings

| Tasks | Interface or overlap | Resolution |
| --- | --- | --- |
| 1 | Allowlist, seed, tests and SQL agree on three lowercase names | One owner; additive-first rollout |
| 2 | Route registration, hooks and native replies agree on four GPT mailbox names | One owner; no role inferred from title |
| 3 | Installed source must match task 2; migration/seed must match task 1 | Controller activates only reviewed outputs |
| 1 + 2 | Role name case mapping | Fixed eight-name roster; brain lowercase |
| 1 + 3 | Brain migration and seed | Worker prepares/tests; controller applies local target only |
| 2 + 3 | Module filenames, register CLI, state compatibility | Installer preserves import filenames and old state |

Ruling: execute the approved implementation without a second approval round; the operator
already approved wiring the bridge and brain. Use non-overlapping workers and a final independent
review. Keep the user-requested shared checkout rather than skill-default worktrees.

## Completion evidence

Brain source: `3f8d31889`; bridge: `a63f21f46`; installer/handovers: `d0dd8a707`.
Controller combined focused tests: 232 passed; disposable PostgreSQL: 24 passed; installer
after the review fix: 10 passed (includes two additional first-install recovery tests).
Final independent spec/quality review PASS, including three independent installer failure checks.
Installed bytes match the reviewed bridge; service active; local migration044 and repeat seed
verified; registered Codex hook resolves its own identity; Alpha ACKed nonce
`eight-crew-20261026-01`. Subsequent onboarding registered all four GPT roles with acknowledged delivery; initial
assignment ownership is now Alpha-only, with Codex distributing explicitly delegated scopes. Details: `docs/audits/2026-10-26-eight-agent-crew-activation.md`.

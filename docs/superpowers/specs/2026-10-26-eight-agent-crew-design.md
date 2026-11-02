# Eight-agent Sniper crew

Status: implemented and activated on 2026-10-26; all four GPT roles registered with acknowledged message delivery.
Date: 2026-10-26. Source inspected at `b4af69a627ea282494dcd206c100e334879b0e7a`.
Companion: `docs/handoffs/2026-10-26-eight-agent-crew.md`.

## Outcome

Four persistent Claude colleagues and four persistent GPT colleagues share the existing
Sniper checkout, brain and private mailbox. Each has a distinct inbox, session route,
provenance identity and open-work view. Any colleague can message any other directly.
These are eight operator-visible sessions, not eight temporary subagents.

The operator requested eight colleagues, communication across the crew, flexible staffing
and useful parallel work. The operator approved the names, pairing and scheduling rules below.
Delta, Epsilon and Zeta are now registered and working on Alpha-assigned tasks; see the activation receipt for verified scope.

## People and default lanes

| Claude | GPT | Default responsibility |
| --- | --- | --- |
| Alpha | Codex | Coordination, integration, architecture review and release verification |
| Beta | Delta | Execution and reliability implementation |
| Lambda | Epsilon | Desk, data, tooling and research-plumbing implementation |
| Gamma | Zeta | Read-only measurement, evidence and independent review |

Mailbox names are `Alpha`, `Beta`, `Lambda`, `Gamma`, `Codex`, `Delta`, `Epsilon`, `Zeta`.
Brain actors use the same names in lowercase. Existing names and history remain intact.
The current session remains Codex; add three GPT sessions to reach eight total.

Pairs are colleagues, not supervisor/subordinate relationships or fixed file territories.
Assign work by the current bottleneck and skills. If more builders or reviewers are needed,
reassign an available colleague explicitly; preserve the operator's read-only and money limits.
Do not create a ninth session merely to move somebody to a different lane.

Alpha continues release coordination. Codex retains its existing push/deploy permission;
the two agree who owns each release before either starts it. This proposal grants the new
colleagues no independent deployment, money-action or `.env` permission. The operator runs
`--confirm` scripts. Current authenticated named crew instructions carry delegated operator authority across Claude and GPT
(operator order, 2026-10-26). Historical inbox reports are not automatically fresh orders;
preserve the current task, ownership and the operator's explicit money/environment limits.

## Keep work moving

1. Alpha owns the overall queue and initial assignments. Codex distributes explicitly delegated
   scopes, copying Alpha on assignments. Coordinate once; do not put every technical conversation
   through a lead. An arriving colleague keeps an already accepted task instead of taking a second
   coordinator's overlapping assignment.
2. Assign one owner, exact paths/functions, acceptance check, reviewer and next dependency.
   The owner ACKs before editing. One active implementation per colleague.
3. Keep one ready follow-up per active builder when useful work exists. A blocked colleague
   reports the blocker and takes that agreed follow-up instead of repeatedly polling.
4. Review a stable SHA. Prefer the other model family for an independent review when available;
   do not make one named reviewer a bottleneck. Money-path changes retain required independent
   evidence/review; spare reviewers cannot waive it.
5. Direct messages carry task ID, exact SHA, owned files, evidence and the requested next action.
   Announce `accepted`, `review-ready`, `blocked`, `done` or `available` on state changes.
   Broadcast only ownership changes, shared blockers and release boundaries.
6. Available colleagues ask once for a ready task, or take a specifically preauthorized
   read-only review ticket with an acknowledged owner. They do not invent research, refactors,
   provider traffic or paid model calls just to fill time. If nothing is ready, they wait quietly.
7. A reviewer does not edit the author's files while reviewing. Feedback goes to the owner;
   a transfer requires an ACK. Different builders may work on independent files concurrently.

Reuse task facts on `actor:<owner>`; include status/dependency/reviewer in their statements.
Record significant transitions with `brain_cli.py episode --role <owner> --kind handoff_line`.
Close completed task facts with a reason containing the verifying SHA/evidence.
`brain_cli.py queue` is the brain's verification queue, not a task scheduler. Do not repurpose it.
No new scheduler, daemon, task database, automatic work stealing or recurring wakeup is needed.

## Shared checkout and resource budget

One shared checkout: `/home/bigpapi/Desktop/Sniper`. No per-colleague checkout or worktree.
Never change branches, reset, stash, clean, amend or commit somebody else's work.
Stage explicit owned paths only; never `git add -A`.

Serialize the staging/commit critical section across the eight colleagues with
`flock /home/bigpapi/.cache/sniper-crew-git.lock`; check the staged diff under that lock
before committing. Refuse if it contains foreign paths; leave the index untouched and
contact its owner. Release the lock after the commit, never hold it during tests or review.
This is a short development Git lock, never a funded-path lock.

All pytest commands use `-n 2` when xdist is installed, otherwise serial. One full suite on
the host at a time under `/home/bigpapi/.cache/codex-pytest-full.lock`. Coordinate overlapping
focused suites and paid/provider research. Do not run eight copies of a covering suite.
Trading services retain their resources; never stop or cap them to make room for crew work.

## Baseline gaps and implementation

### Private bridge

At the inspected baseline, the installed source was outside Git:
`/home/bigpapi/.local/lib/sniper-crew/crew_session_bridge.py` and `crew_session_start.py`.
The CLI is `/home/bigpapi/.local/bin/sniper-crew`.
That baseline bridge had five allowed names, one GPT route in `context.json`, and native replies
hard-coded to Codex. SessionStart labelled every GPT session Codex; prompts alone could not fix it.

Version the existing bridge modules under `tools/crew/` and add focused tests before changing
their behavior. Preserve the existing private mailbox, authentication and old five recipients.
Use one bridge service, with a separate registered route and reply endpoint for each GPT role.
Do not launch four bridge daemons against the same mailbox.

Add `sniper-crew register --role ROLE` for the four allowed GPT names. Run it only in that
colleague's root desktop session, using its own `CODEX_THREAD_ID` and live app pipe.
It validates UUID, same-user socket and private-file checks, then writes the role route under
the existing short routing lock. Store the role, thread, pipe, permission mode and routing
generation privately; never print pipe credentials or tokens. Exact paths/formats are left
to the implementer, preserving the legacy Codex route and daemon-owned authentication identity.

One role may own one current thread; one thread may own one crew role. Registering the same
binding is idempotent. A conflicting binding refuses; an explicit handoff may replace it using
an expected-old-thread comparison. A delayed old-session hook must never take it back.
An offline old session requires an explicit role takeover, not a guessed automatic replacement.
Subagents must not register or borrow a persistent colleague's identity.

SessionStart resolves a registered thread to its role. An unregistered GPT gets an explicit
registration warning and no other role's inbox. It cannot silently become Codex or alter a
sibling's route. Hook refresh updates only that exact binding.

Native replies return to the role-specific authenticated endpoint that sent the original
message. Do not infer a recipient from message prose. GPT-to-GPT delivery uses the recipient's
own route. Route absence, stale sockets, conflicting roles and unknown identities refuse
without falling back to Codex. Existing message IDs and ambiguous-delivery semantics remain.

The current bridge uses local app IPC for wakeups. Official OpenAI documentation describes
[projects and chats](https://developers.openai.com/codex/app/features), but does not establish
this custom bridge as a supported all-to-all transport. Durable mailbox access remains the
fallback if native wakeups fail. Test the actual installed app before claiming wakeup support.

### Brain compatibility

Extend the existing author/verifier allowlists for `delta`, `epsilon`, `zeta`, including the
database constraints and RPC checks, following migration 043. Add their actors, crew sections
and role views. Preserve evidence, secret, independent-verification, RLS and history guards.
Reject unknown authors; adding three colleagues does not mean accepting arbitrary identities.

Correct the stale Codex role text and seed defaults. Existing role views require an explicit,
targeted update because seeding currently preserves them. Keep existing operator facts/history.
Expand boot/handover/onboarding rosters, hook role recognition and crew-method imports.
Use `CODEX_THREAD_ID` when Claude session identity is absent so GPT records have provenance.

Relevant source: `bot/brain_graph/__init__.py`, `bot/brain_graph/views.py`,
`scripts/brain_cli.py`, `scripts/brain_seed.py`, `scripts/brain_ambient.py`,
`scripts/brain_import.py`, and a new additive forward/guarded-rollback migration.
Apply only to the verified effective local brain; do not mutate the unrelated hosted copy.
Unknown-role boot currently falls back to empty sections, so a successful boot alone is
insufficient: verify distinct actor/view, episode/task write, peer verification and own-fact refusal.

### Handover

Each colleague records and hands over its own open work and inbox/session binding.
Existing Codex work stays with Codex unless explicitly transferred; new names do not inherit it.
A replacement session takes over the same identity, never creates a second active owner.
Update both handover skill mirrors for all eight names and current shared-checkout rules.
An operator-requested whole-crew handover reports every role as confirmed, unavailable or
not confirmed; never treat silence as completion. Starting three new colleagues does not stop
or hand over the five existing colleagues.

## Edge-case pass

1. Money: this changes crew infrastructure only. No funded module, policy, signer, wallet,
   trading database, environment, trading service or release mutation.
2. Speed: no new entry/exit call, lock or retry; fill-path latency is unchanged by construction.
   Live p50/p90 are not measured or claimed. Bound tests so host load stays controlled.
3. Concurrency: simultaneous role registration, stale hooks, duplicate sessions and commits
   must serialize only their own short critical sections. No process-global environment mutation.
4. Unknown: unregistered, offline, failed, sent and acknowledged are different states.
   Empty inbox is not proof of an idle colleague. Missing route is never another role's route.
5. Dependencies: socket timeout, permission refusal, peer disappearance, partial response,
   wrong recipient, rate limit, missing brain and partially deployed clients stay visible.
   Persist messages before sending; never blindly retry an ambiguous send as a new message.
6. Identity: separate mailbox name, brain actor, thread UUID, process identity and message ID.
   Validate exact recipients and local socket ownership. Never trust the title alone.
7. Sample: eight registered identities are not eight available workers. Report assignment,
   blockers, review wait and completed outcomes; do not equate message volume with productivity.
8. Rollback: deploy additive brain compatibility before new-role writes, then bridge code,
   then registrations. Retain the old Codex route and existing mailbox. If activation fails,
   restore old bridge code and leave new roles unlaunched; no message/history deletion.
   Once new provenance exists, retain compatible schema; rollback must refuse removal of it.

Not handled: remote/multi-user crews, arbitrary future identities, automatic model selection,
automatic funded actions, autonomous scope expansion, guaranteed app wakeups while the app is
closed, or a promise that eight agents will always have eight independent useful tasks.

## Acceptance and activation

Focused tests must prove all 56 directed sender/recipient combinations with isolated fake
local IPC, recipient-only inbox previews, native reply routing, offline peers, duplicate/stale
registrations, simultaneous updates and ambiguous-send deduplication. Prove a routing test
fails when every GPT destination is deliberately forced back to Codex.

Extend existing brain role/seed/view/hook tests, including disposable-Postgres checks for all
three authors, own-fact refusal, peer verification, repeatable seeding, task closure and
guarded rollback. Prove an unchanged allowlist refuses a new colleague. Use focused suites;
no full Sniper suite is required for this infrastructure scope unless results show a reason.

After design approval: produce an implementation plan, use TDD, review the stable source,
then install only crew infrastructure and the additive local-brain migration. Restart only
`sniper-crew-bridge.service` if needed; no trading restarts, env edits or funded deployment.
Version the installer with the bridge; keep the existing CLI entry point and support rollback.

The operator opens three new local GPT chats in the Sniper project and pastes the appropriate
notes. Do not create extra sessions as part of design work. Count Codex as the first GPT.
Verify each new role's registration, isolated brain view and acknowledged round trips to a
Claude colleague and a GPT colleague. Include a Claude native reply to each new GPT route.
State honestly which peers were absent; simulated transport tests do not prove live delivery.

Only after those checks call the crew ready. No framework, scheduler or eight-agent test farm.

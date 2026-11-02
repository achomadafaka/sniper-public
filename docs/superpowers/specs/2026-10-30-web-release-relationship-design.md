# Web/runner release relationship — 2026-10-30

Operator order via Alpha: an Office-only web release ahead of the funded runner gets a quiet note only when ancestry and changed paths prove it. Unknown remains an alarm. Advice must never roll the web backward.

## Contract

`scripts/deploy_web_runtime.sh` writes `.web_release_relation.json` in the candidate release before the atomic switch. The producer in `sniper/util/web_release_relation.py` reads the runner SHA from the shared-state heartbeat, compares both ancestry directions and the final trees using git, and writes through `atomic_write_json`. Git errors/timeouts, absent or malformed heartbeat data produce unknown fields, never a fabricated zero/false. Failure to write the file fails before the switch. Dry-run does not write it. The producer is anchored beside the deploy script, so a candidate predating this feature can still be deployed.

The web reads the file once at module import beside its resolved release code. Each status response binds that cached snapshot to both its served web SHA and the current heartbeat runner SHA. Changed runner identity invalidates the snapshot; no git command or retry runs on a status request or funded entry/exit path. `release_drift.match` remains literal SHA equality.

Only `state=known`, `runner_is_ancestor=true` and `funded_paths_changed=false` produce the quiet note. The non-funded allowlist is explicit in the producer: Office code/config/deploy/scripts and dedicated Office assets/routes/template, documentation and tests. Shared `static/app.js`, `sniper/web/server.py`, services, execution and every unlisted path count as funded. Renames are diffed as deletion plus addition, so moving funded code into docs cannot hide its original path. This is a source-path classification, not funded safety or config equality proof.

Known web-ahead funded changes: keep the warning and say deploy the runner forward. Known web-behind: say deploy the dashboard forward. Unknown or diverged: keep the warning and require review before a forward deployment. The banner emits no deployment command. The quiet note is shown separately from the action strip; other kill, runner, loss and reservation warnings are unchanged.

## Edge cases and limits

- No funded behavior, signing, sizing, policy, `.env` or provider changes. Only deployment metadata and status presentation change.
- All git work is before web activation, timeout-bounded; it never blocks fills. Reader state is immutable for the process lifetime.
- Missing sides, invalid JSON/schema, stale identity, git failure and half-applied deployments stay unknown/alarming.
- Exact 40-character SHAs, both ancestry directions and boolean unknowns remain distinct. No commit-count shortcut.
- A conservative alarm can remain on the current rollout because shared dashboard/server/service files changed. This does not authorize a runner deploy.
- Not handled: verifying runtime custody/config/organic effects, automatic deployment, changing the non-funded allowlist from operator input, refreshing metadata without a web restart.
- Rollback keeps each release's own cached metadata. The new file follows existing web-release retention (current, previous and newest others; default five), with one overwritten snapshot per release and no additional history or backup.

## Validation and knowledge sync

Real tiny git histories cover Office/docs/tests-only, funded/shared files, behind/diverged/unknown and funded-to-doc rename. Real no-cycle web deploy runs only in a disposable fixture repo and verifies the written snapshot and switch. Status tests invalidate a changed heartbeat identity. Real browser cockpit tests cover quiet-note rendering, funded/unknown alarms and forward-only advice. New behavior was observed RED before implementation.

KSG audit: module and validation commands added; this document defines the amended drift contract. No analyzer, funded execution, database/RLS, strategy, agent operating-rule or frozen-status change. No live deployment was performed by Epsilon.

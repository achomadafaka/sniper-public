# Office same-epoch repin implementation plan

> Implement inline with TDD; Zeta reviews the exact commit. Alpha owns live approval and activation.

**Goal:** Repin an installed discovery-2 Office without changing its prompts, tools, actor generations, or paper capital.

**Architecture:** Add an explicit repin mode to `scripts/office_pilot_activate.sh`, reusing its sealed-bundle, release, SDK, and unit validators. Require old and new externally pinned bundles, a stopped Office, an expired lease, and a read-only discovery-2 state witness. Journal only configuration bytes and link identity; atomically replace files and the current link, with explicit and failure rollback.

**Spec:** `docs/superpowers/specs/2026-10-29-office-dm-tasks.md`; Alpha's 2026-10-29 same-epoch repin order.

## Constraints and edges

- No funded path, provider calls, service start/stop, `.env`, DB initialization, DB backup, reseed, or epoch transition.
- Fail closed on changed tool/prompt/semantic configuration, unsafe files, unknown clock/lease, active units, or an inconsistent frozen epoch/roster/capital witness.
- Only release-root references and dependent file hashes may differ between sealed configurations; units and other policy/configuration stay identical.
- Validate both full SHAs, both external bundle pins, main ancestry, SDK artifacts, and exact installed configuration before mutation.
- Serialize with the existing activation lock; recheck preconditions while locked. Refuse foreign changes during recovery.
- A crash can leave mixed approved config versions; a durable journal allows explicit rollback. Do not delete either release or touch DB rows.
- Keep one current and one previous private repin journal; only rotate a completed journal. This bounds configuration-snapshot retention.
- Not handled: prompt/tool/epoch migrations, policy changes, concurrent live writers, automatic service recovery, or rollback of additive DB schema created by a later startup.

## Implementation and proof

1. Add hermetic RED tests for successful repin/rollback, sealed pins and semantic differences, active/leased Office, changed epoch/generation, interrupted writes, foreign changes, and repeat execution.
2. Add repin CLI arguments, reuse static planning with installed-file checks deferred to the repin transaction, and implement read-only state validation plus config/link journaling.
3. Prove each trust boundary and rollback with focused tests. Run the existing initial-installer suite to preserve its behavior.
4. Commit only owned files on `codex/office-repin-helper`; send Zeta exact SHA, commands, evidence, and residual limits. No live use in this task.

## Operator interface

Run the reviewed immutable helper with both externally approved bundle hashes. The default is a stopped-state dry-run:

```bash
bash <reviewed-helper>/scripts/office_pilot_activate.sh \
  --repo /home/bigpapi/Desktop/Sniper \
  --sha 47be25553968b3231757d2736710332391d8eb24 \
  --bundle <new-approved-bundle> --bundle-sha256 <new-manifest-sha256> \
  --repin-from-bundle <old-approved-bundle> \
  --repin-from-bundle-sha256 <old-manifest-sha256>
```

After Alpha's go, the same command with `--apply` prepares the repin. Before restarting Office, `--verify` checks the installed config/link and preserved state. Explicit recovery uses the same approved inputs plus `--rollback --apply`; interrupted operations refuse forward retries until recovered. The helper never starts, stops, enables, initializes, seeds, or transitions anything.

An approved old release and its SDK must remain present for rollback. Candidate releases/SDKs remain staged after failure; cleanup is a separate retired-release action. Rollback restores the last operation's configuration/link only, not additive schema created by a later runtime start. One previous journal is retained for audit, not automatic multi-hop rollback.

## Validation evidence

- Transaction tests cover both pins, policy/tool/prompt changes, lease/clock/state failures, lock contention, file and link failure, interrupted journals, foreign changes, idempotence, and bounded journal retention.
- A full synthetic CLI flow exercises real bundle validation and Git materialization with fake user services; no production state or provider is used.
- A real synthetic epoch-transition fixture verifies the read-only witness against `epoch_transition.adopted()` and the canonical capital hash.
- Existing initial-installer and SDK artifact tests remain in the focused gate. No live execution is part of this implementation.

## Restart order after verified repin

Alpha owns service starts and recovery. After successful prestart `--verify`:

1. Start `office-monitor.service` first. Read `data/office-control/health.json` and wait for a newly written, fresh `ts` after monitor start. Require valid telemetry; an old file or a telemetry error is not a fresh healthy sample.
2. Only then start `office-watchdog.service`. Confirm the guard state before starting the supervisor; child-launcher and paper-watcher remain under Alpha's start procedure.
3. If `telemetry_stale` or `guard_stale` has latched, use the approved explicit recovery after 900 seconds healthy. Do not bypass the latch or treat a later fresh sample as permission to start immediately.
4. After Alpha starts the supervisor, collect its actual PID and UTC `ps` start time, release identity and first successful Scout call. Fill the transport boundary log from that provenance; split every Claude seat at the verified start and report the discovery gap to Gamma/Delta.

Observed during the `5b20c039e` repin: starting watchdog before the monitor's first fresh sample latched `telemetry_stale`. Apply and prestart verification had passed; runtime recovery remains a separate gate. This procedure documents start order only; it changes no service, guard policy or automatic recovery behavior.

### Planned-stop recovery cost

During the `613bfaed4` repin, monitor-first startup avoided the telemetry race, but the stopped watchdog's previously `ready` latch was older than five seconds. `external_guard()` classified it as `guard_stale`; watchdog treats that as malformed authority, blocks automatic resume, and requires the 900-second healthy interval plus explicit `--initialize` recovery. A full planned Office stop that leaves this ready latch unrefreshed for more than five seconds therefore incurs this recovery delay even with correct monitor/watchdog startup order.

Budget this delay in the single-stop window and report it as part of the discovery gap. Do not delete, rewrite, refresh or bypass the latch to shorten recovery. Monitor-first order remains required; it does not remove the stale-authority gate. Alpha owns the explicit recovery and supervisor start. Delta's planned-deploy fast-recovery spec is pending operator approval; this note implements no recovery-policy change.

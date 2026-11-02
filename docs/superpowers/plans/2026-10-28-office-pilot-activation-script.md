# Office Pilot Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare one exact reviewed main release without starting Office services, canaries or seeding state.

**Architecture:** One `scripts/office_pilot_activate.sh` entry point, with a standard-library Python controller for pinned JSON, atomic file installation and a recovery journal. Only absent or identical owned files are accepted. Installation and verification are separate outcomes; no deployment manager or automatic upgrade path.

**Tech Stack:** Bash, Python standard library, Git worktrees, user systemd.

**Spec:** Alpha's authenticated 2026-10-28 queue order and verification-pending ruling; Delta's `/home/bigpapi/.cache/codex/delta-office-pilot-checklist.md` and `delta-office-config-drafts-20261028/README.md`.

## Global Constraints

- Default `--dry-run`; `--apply` is explicit. `--rollback` defaults to a read-only plan unless paired with `--apply`.
- Exact full SHA must be on local main; no fetch, branch switch, stash or force checkout. Release is `.runtime/office/releases/<sha>`; current is an entrypoint alias only.
- External SHA authenticates the bundle manifest and its finalized runtime/launcher/monitor/pin bytes. Draft/null fields refuse. Scratch drafts are never changed or self-approved.
- State directories are owned by the current UID and mode 0700. Configs are 0600. Only the fixed Office unit allowlist is installed/enabled; never `start`, `restart`, `--now`, canary, seed or DB initialization.
- Preparation may remain installed and enabled-not-started with exit 3 and named pending checks for missing live caps/guard/health. Exit 0 means plan or preparation verification only, not activation authority; refusal/error is exit 2.
- Rollback restores the original absent/identical file and enable state, preserves data/DBs, refuses active Office units and any tampered artifact. No funded service, credentials or `.env` changes. No database backups.

## Review Focus

- Foreign file/mode/symlink or non-main SHA: refuse before installation.
- Partial installation or failure after enable: journal precedes operations, rollback touches only verified owned artifacts.
- Missing telemetry: list each pending check, exit 3, preserve preparation.
- Unexpected config hashes/null drafts: refuse without changing the real targets.
- Tampered artifact or active Office service during rollback: refuse before rollback changes anything.

### Task 1: Preparation and rollback contract

**Files:** Create `scripts/office_pilot_activate.sh`; create `tests/office/test_pilot_activate.py`; document `deploy/office/README-pilot-prepare.md`.

**Interface:** `--repo PATH --sha HEX40 --bundle PATH --bundle-sha256 HEX64 [--dry-run|--apply|--verify] [--rollback]`. Bundle: schema `office-pilot-activation-v1`, `approved_by=alpha`, exact `main_sha`, `repo_root`, `release_root`, `guard_manifest_sha256`, and `files` hashes for `runtime.json`, `runtime.sha256`, `transport.json`, `launcher.json`, `launcher.sha256`, `monitor.json`. Bundle filename is `manifest.json`. No source filenames from arbitrary input.

- [x] Write RED refusal tests for default dry-run, wrong main identity/hash, symlink/mode mismatch and incomplete drafts.
- [x] Implement bounded regular-file reads, release/config/unit planning and the fixed allowlist. Dry-run installs nothing.
- [x] Write RED tests for pending live verification, repeat apply, partial failure recovery, rollback and rollback tamper/active refusal using temporary repos and fake systemd commands.
- [x] Implement the prewritten recovery journal, enable-only installation, exact verification and rollback. Keep generated data outside installer ownership.
- [x] Run `python3 -m pytest -n 2 tests/office/test_pilot_activate.py -q` plus `bash -n scripts/office_pilot_activate.sh`; commit owned paths with `scripts/crew_commit.sh` and request Delta/Zeta review. Do not execute against the live installation.

## Review status

Focused refusal/lifecycle tests and Bash syntax check are required before commit. Delta/Zeta source review is pending; no live preparation has been run.

The existing slice drop-in is mandatory, mode 0644, with the exact approved v2 hash above; the installer never rewrites it. The user manager currently reports `/user.slice/user-1000.slice/user@1000.service`; expected Office cgroup is `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/office.slice`. An absent leaf is `verification_pending`, never verified readiness. No start was used to derive this path.

Validation 2026-10-29: 39 focused tests passed with `-n 2`; Bash syntax and schema-ref checks passed. Runtime rejection tests were observed RED (invalid version/enabled/extra fields/timing were accepted before the validator). Live read-only recheck at 08:22Z found office.slice active with exactly the expected ControlGroup and kernel directory; no install/start by Epsilon. Runtime mount/child/canary proof remains external.

Zeta HOLD follow-up: the supervisor masks office-control with tmpfs; only approved transport.json is now rebound read-only after that mask, beside the existing guard/socket mounts. Real-unit argv-order regression was observed RED before the one-line mount fix. No broad office-control exposure or live namespace execution.

Integration follow-up 2026-10-29: rebased activation onto origin/main `0f1051700` per Alpha. Reproduced all three launcher-runtime failures before edits. Launcher mount allowlist now accepts only the exact transport.json read-only bind, checking both source and target against protected control roots; writable, redirected and whole-control variants refuse before auth state. The real bwrap fixture creates the required transport source and verifies it is visible while credentials/private children remain hidden. 192 connected launcher/runtime/auth/guard/activation tests passed with `-n 2`; Bash syntax, schema references and diff whitespace passed. No live install/start.


2026-10-29 identity correction (Alpha ruling): only the known unit's `current/scripts/office_supervisor.py` argv is accepted for matching runtime/launcher supervisor identity. Missing current is prospectively installer-owned; existing current must bind exactly to this release, never redirected. Apply reads back the created alias before enabling units; journaled rollback removes its own alias. Release/model/config roots stay literal immutable paths. Tests observed RED for correct alias rejected and wrong literal-release argv accepted before correction; current states, redirected components, non-supervisor argv and launcher/runtime mismatch covered. Real rendered unit argv is checked against unchanged validate_peer on a proc fixture. No live alias or release was created.

Identity correction validation: 201 connected launcher/runtime/auth/guard/activation tests passed with `-n 2`; schema references, Bash syntax and diff whitespace passed. This includes the real-unit argv/unchanged peer-validator witness and transactional alias apply/rollback.

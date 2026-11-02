# Planned recovery host implementation plan

> **For agentic workers:** Execute inline with RED/GREEN checks. No subagent delegation or host actions authorized by this plan.

**Goal:** Build an isolated automatic root host guardian with operator signing checkpoints, then prove its boundary before any key exists.

**Architecture:** Root-owned guard/publication/state and all root writers share one lock; observation occurs outside the critical section and is independently revalidated inside. Latest Alpha20:08:57 selects operator checkpoints for funded signing, superseding the funded orchestrator. Guardian writes guard state only; isolated recovery adapter alone writes replay/consume state. No deployment-trigger endpoint is needed. No arbitrary signing RPC, sudoers/polkit/user-bus endpoint or caller-supplied privileged config.

**Tech Stack:** Linux ownership, dirfd/no-follow opens, flock, fork/setgroups/setgid/setuid/no_new_privs, stdlib Python, public Ed25519 verification with OS OpenSSL.

- [ ] Revise host-boundary design for the operator's automatic root-service choice; Zeta reviews root publication exclusion, deterministic interleaving, parent/child lifecycle, independent verifier and trigger provenance. No keys.
- [ ] Add failing fixtures for protected writer/ancestor ownership, changed revision, pause concurrent with publication, and crash between consume and ready. Implement smallest root publication primitive, no live path fallback.
- [ ] Add failing process witnesses for retained parent UID, dropped child UID/groups, inability to regain privilege, no inherited private FDs and independent completion proof. Implement root-owned fixed-policy driver primitives; no checkout code as root.
- [ ] Build root-canary matrix harness using harmless fixture bytes and explicit context commands bound by trusted root policy. Root/operator execution remains separate; unprivileged results cannot claim root isolation PASS.
- [ ] Build service integration only after trigger/controller/target provenance and typed Office completion are approved. Privileged inputs must come from root policy and independent root-safe probes, not user success JSON or marker claims. No key provisioned to test.
- [ ] Run focused -n2 tests, submit exact source and root artifacts to Zeta, deliver final pinned sudo commands to Alpha; operator alone installs, executes canary and generates key last.

Host install/activation, production cgroup actions and funded deploys are not test actions. No root test result, key creation, runtime wiring or completed activation may be claimed without its actual evidence.

## Governing stage order (Alpha20:08:57, freeze small tests only)

1. Independent root-safe validator: root policy/artifact ownership and pins, bounded untrusted data reads, process birth/release, direct stopped-unit and read-only lease/epoch/capital validation. Never execute source/candidate code as root or trust user success JSON. Zeta review before downstream binding.
2. Root guardian: protected authoritative path+all ancestors/shared writer lock+revision, no replay/key access, no same-UID writer fallback. Deterministic pause/publication and crash witnesses. Zeta review.
3. Root operator checkpoint signer plus isolated automatic recovery adapter: root-issued fixed300s marker, typed Office-prepared completion, independent healthy5s/two-beat evidence, consume-before-ready. Adapter sole replay-state writer; guardian isolated from that state. Zeta review.
4. Installed root import graph/unit policy and harmless-canary matrix for every actual context (/proc/fd/ptrace/inherited descriptor/sudo/bus/key/state path). No private key until operator-run canary evidence and source review pass. Final exact commands to Alpha.

Earlier retained-parent steps are uninstalled experiments only and are superseded as an integration plan. No merges/shared-checkout edits/heavy tests during runner freeze; own-tree focused -n2 only.

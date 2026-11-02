# Wallet Universe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Wallet Map a chain-specific, evidence-backed wallet selection and observation surface with reliable Armed navigation.

**Architecture:** Three independently reviewable deliveries reuse existing authority, graph, measurement and policy code. Live authority overlays cached measurements; isolated research observation supplies future evidence without entering the funded lane. No additional execution engine.

**Tech Stack:** Python, SQLite, Flask, existing JavaScript/CSS, pytest and the existing Playwright harness. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-02-wallet-universe-design.md` (operator approved).

## Global Constraints

- No extra synchronous call/lock/retry is allowed on the entry path.
- No DexScreener prices may enter fill evidence.
- Policy changes, funded activation and service deployment are separate operator decisions.
- Unknown stays unknown. Atomic amounts are decimal TEXT; chains are canonical; EVM addresses normalize, Solana addresses retain case.
- No automatic arming or filter exemptions. Keep existing arming, gates, funded selector and frozen contracts authoritative.
- Production inspection is read-only. Tests use isolated fixtures. New workers remain disabled until separately activated.
- Before code edits, present the edge cases below to the operator. No new entry work means no promised latency improvement; fixture timings cannot establish organic fill p50/p90.

## Review Focus

1. Missing materialized file must still show live roster authority — Map plan Task 1.
2. Ambiguous arm POST must not be replayed — Map plan Task 3.
3. Missing relationship data must not imply independence — Map plan Task 2.
4. Observation duplicates, partial decoding and shared-provider pressure must not create false trades or reach funded execution — Observation plan Tasks 1–3.
5. Different chains, omitted losing/open origins and one dominant coin must not become a strategy-fit claim — Fit plan Tasks 1–2.

---

## Delivery plans and dependencies

1. [Map authority and per-chain universe](2026-10-02-wallet-universe-map.md): three tasks. Independently ships using existing facts.
2. [Isolated paper observation](2026-10-02-wallet-universe-observation.md): three tasks. Depends on the Map identity/coverage contract; requires a resource-isolation check before activation.
3. [Setup-conditioned strategy evidence](2026-10-02-wallet-universe-fit.md): two tasks. Can read existing paper facts before Observation is enabled; new watch-only measurements remain a separately labeled cohort.

Execute Map first. Fit may follow Map before Observation activation. Run the finite isolated provider check only after its explicit endpoint, caps and wallet manifest exist; missing capacity blocks collection, not Map/Fit delivery. Do not enable research providers while the funded provider budget is unknown.

## Edge cases and exclusions

Money: existing arm controls can grant authority. Changes reuse their writer and gate checks; no new bypass, sizing, signing, entry/exit callback or synchronous enrichment. Concurrency: per-request read handles, no temporary global/env/DNS mutation; snapshot publication uses a same-directory temporary file plus atomic replace. Unknown/zero: unavailable authority disables money controls; unavailable measurements stay null; a completed empty read may report zero. Dependency failures: preserve typed missing, timeout, rejected, rate_limited, banned, partial and identity_mismatch states. Identity: exact source event plus wallet/chain/mint; do not deduplicate separate buys by mint. Denominator: selection is frozen before forward collection and includes losing, blocked, open and unpriced origins. Rollback: revert read/UI code; stop only research worker; retain immutable observations/contracts. Half-published snapshots never replace the last complete file.

Not handled: historical recovery, causal influence, live subscription proof where no heartbeat exists, new cash/native routes, EVM research decoding, funding decisions, policy activation, or organic latency claims. Non-Solana research is explicitly unsupported until separately implemented; its existing stored universe remains usable.

## Execution preflight

- [x] Read the approved spec and all three plans. Inspect `git status --short --branch`, `git log -1 main` and the current versions of owned files. This worktree starts at `fc81186a5`; main advanced to `1146666c6` during planning. Refresh from current merged code before implementation and preserve other agents' work. Do not copy old code over newer fixes.
- [x] Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables`. Before naming existing SQL columns, run `python3 scripts/brain_index.py schema <table>` for each actual table touched. New research schema is declared in Observation Task 1, not inferred from production.
- [x] Prove the current reproduction can fail: `python3 docs/audits/2026-10-02-check-wallet-map.py --require-live-overlay`. Expected: missing live roster wallet assertion. After Map Task 1 the same command must pass.
- [x] Commit each completed task separately after its red/green checks; do not commit snapshots, credentials, state DBs or unrelated changes.

## Plan self-review

Spec coverage: live truth/navigation → Map 1/3; per-chain evidence, graph and matrix → Map 2/3; collection and budgets → Observation 1–3; immutable setups, strict cohorts, refusal outcomes and adjustment evidence → Fit 1/2. Each review-focus failure has a named check in its owning task. Interfaces are shared verbatim across plans. Collection is phased rather than fabricating unsupported routes or policies. All missing source data are typed gaps, not deferred silent success.

Completion proof is focused tests, the regression checker and browser fixtures. Deployment, live arming, watcher activation and policy promotion are outside this execution plan.

## Completion rulings, 2026-10-02

The eight code tasks are delivered with review corrections. Named example tests were adapted to actual repository interfaces; the final focused behavioral union passed 389 tests. Source clocks and paced completion clocks are now explicit. Frozen policies are scoped to the selected setup/window and ambiguous identities disable hypothesis export. Current proof helpers include verified watch-only Pump profiles. Seven existing chain ledgers materialized to scratch without production writes.

Provider isolation is an activation prerequisite, not a completed live test: configured host separation and explicit capacity declaration are checked, but different URLs alone cannot prove independent provider/key/IP capacity. No live provider smoke, service activation, continuous-rail policy verdict, new cash/native route or funded latency improvement is claimed. Actual scratch snapshot size required a shared 16 MiB producer/reader bound.

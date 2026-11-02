# Source Mirror TP100 Implementation Plan

> **For agentic workers:** use the existing subagents with non-overlapping ownership; tests precede
> implementation and one fresh integrated review follows.

**Goal:** Copy tontheneko under a frozen wallet/chain-specific exact-source-sell or TP100 policy.

**Architecture:** Immutable named-wallet profile selects strict frozen rails. A runner-owned
source request journal and distinct repeatable partial claim bind each sale to exact quantities,
signed identity, receipt accounting and recovery. Existing full-close coordination stays shared.

**Tech Stack:** Existing Python, SQLite, Solana/EVM executors; no new dependency.

**Spec:** `docs/superpowers/specs/2026-10-20-source-mirror-tp100-design.md`

## Global constraints

- No full suites. Every focused pytest command uses `flock /tmp/codex-ton-exact-mirror-pytest.lock
  python3 -m pytest -n 2 ...` so parallel workers do not multiply host load.
- No live mutation from workers. Root owns account activation and pinned deployment.
- No new synchronous entry RPC/lock/retry; no module/env capture-and-restore under threads.
- Keep other wallets and historical contracts unchanged; no automatic historical buys.
- No database backup. Managed deployment retains all other gates and rollback checks.

## Review focus

1. A second partial after an earlier settled fill must remain recoverable.
2. A signed-unknown action blocks competing partial/full actions across restart.
3. Missing or multi-account source balances must not invent an exact fraction.
4. Old candidate code must be rejected before the running exit owner stops.
5. Source adds, duplicate feeds and out-of-order completions must not reuse a stale remainder.

### Task 1: Resolve account and normal clip (identity worker)

- [x] Bind the official retained UID/handle to active chain addresses with primary proof.
- [x] Verify multiple exact historical receipt matches and record the automatic buy/control
  promotion requirements that this explicit operator attestation does not satisfy.
- [x] Read the current per-chain default cap; produce activation input, no mutations.

### Task 2: Frozen profile (policy worker)

Ownership: `sniper/settings/memecoin.py`, `sniper/services/memecoin_funded_policy.py`,
`sniper/services/memecoin_risk.py`, and new policy tests. Root owns runner suppression hooks.

- [x] Write RED tests for the explicit exact profile, canonical selector and unchanged defaults.
- [x] Implement `SOURCE_MIRROR_TP100_POLICY_ID = 'source-mirror-tp100-v1'`, pure selector
  `source_mirror_tp100_applies(source_user_id, network_id)` and validated policy constructor.
- [x] Freeze bound provenance in existing contract source; profile wins copy-arm/trial/numeric
  overrides, which must not remain as active provenance.
- [x] Validate strict shape on read/write. True max-hold zero is the narrow new exception.
- [x] Expose a pure profile/position predicate for source dispatch and suppression hooks.
- [x] Run focused GREEN and benchmark policy selector p50/p90.

### Task 3: Durable repeatable partial (execution worker)

Ownership: `sniper/persistence/db_memecoin.py`, `sniper/services/memecoin_exit_intent.py`,
`sniper/services/memecoin_manual_exit_intents.py`, `sniper/execution/solana_exit_executor.py`,
`sniper/execution/evm_exit_executor.py`, `sniper/execution/solana_real_seams.py`,
`sniper/execution/evm_real_seams.py`, and new repeatable-partial tests.

- [x] Write RED tests for deterministic source requests, claim exclusion, repeat trims and restart.
- [x] Extend the existing request journal with immutable integer ratio/source metadata and a
  separate partial owner; no new general operator feature is needed.
- [x] Add the partial ExitIntent action and exact atom quote/sign binding through both venues.
- [x] Book actual receipt quantities/proceeds atomically, preserving the open remainder and
  one-shot de-risk state. Recovery resolves this action independently of earlier partial fills.
- [x] Protect full/one-shot claims and write-off from this new ownership.
- [x] Provide worker/root callable enqueue, dispatch and recovery interfaces before integration.
- [x] Run focused GREEN, including failed receipt, unknown send and invalid quantity cases.

### Task 4: Source dispatch, exit suppression and deployment guard (root)

Ownership: `sniper/services/memecoin_source_sell_mirror.py`, source-profile branches and recovery
hooks in `sniper/services/memecoin_real_runner.py`, runner wiring in
`scripts/memecoin_runner_loop.py`, candidate capability checks in
`scripts/memecoin_runtime_verify.py`, and their focused tests.

- [x] Write RED tests for exact source ratio, duplicate paths, source adds and TP/trim races.
- [x] Send only configured frozen-profile positions to the new source request path. Keep global
  full-only mirroring unchanged. Unknown source amount holds; no decoded-unknown full-sale guess.
- [x] Wire runner-owned execution/recovery using the execution worker's explicit interfaces.
- [x] Suppress other discretionary exit reasons for the authenticated frozen profile; preserve
  manual and source-integrity exits and exact TP confirmation.
- [x] Reject incapable candidates for active configuration, open profile positions or pending
  repeatable partials before the old process is stopped.
- [x] Run focused integration tests, schema checks and independent whole-change review.

### Task 5: Merge, deploy and activate (root)

- [ ] Audit knowledge delta and prepare reviewable PR; merge through GitHub.
- [ ] Run the explicit affected/core deployment manifest with two workers, no full suite.
- [ ] Deploy the exact merged SHA using `--restart --defer-backup` and verify running binaries,
  native subscriptions, authority, pending actions and recovery readiness independently.
- [ ] Configure only proven account/chain pairs at the existing normal cap; arm last.
- [ ] Read back effective frozen profile, authority and audit. Do not trigger a funded test trade.

Validation completed before activation: 433 focused execution/wallet/seam cases, 151 policy
cases, 182 identity/roster cases, 174 deploy/runtime cases, and 197 integrated new cases passed
under the shared two-worker lock (sets overlap; do not sum them). Live state-root capability
preflight passed with the intended selector. A real preflight caught relative DB_PATH lookup;
59 guard/verifier cases passed after state-root binding was repaired. No full suite ran.

Independent review covered source FIFO/recovery, exact atoms and provider context, receipt
metadata, full-close races, cross-position signed identity reuse, operator holds and entry-call
isolation. Shared signature ownership benchmark with 50,000 scratch intents: missing
p50/p90 2.151/2.248 microseconds, existing 2.553/2.638 microseconds using the expression index.

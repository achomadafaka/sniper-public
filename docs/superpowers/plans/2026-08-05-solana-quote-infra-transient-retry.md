# Solana Exact-Quote Transient Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent one-off Solana exact-exit transport failures from producing outage/recovery pages while preserving fail-closed exit safety.

**Architecture:** Keep retry ownership inside the funded Solana preview seam. Retry only the failed idempotent read once. Preserve a boundary-accurate provider ID for diagnosis and use `solana:exact_quote` as the shared sweep/episode group so safety classification and recovery remain end-to-end.

**Tech Stack:** Python standard library, pytest, existing `QuoteFailure` and `PreviewResult` contracts.

## Global Constraints

- One retry after exactly 200 ms for `rpc_transport` and `provider_timeout` only.
- Never retry signing, submission, confirmation, rate limiting, malformed data, or token failures.
- No schema, dependency, setting, admission, sizing, or exit-policy changes.
- Use focused tests only; no full suite.
- Operator exception for this rollout: do not create a deployment backup; remove no live data.

---

### Task 1: Harden the Solana exact-preview boundaries

**Files:**
- Modify: `sniper/execution/exit_preview.py`
- Modify: `sniper/execution/solana_real_seams.py:365-394`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/services/memecoin_alerts.py`
- Test: `tests/test_memecoin_real_seams.py`
- Test: `tests/test_memecoin_engine_authoritative_exit.py`
- Test: `tests/test_memecoin_quote_infra_alerts.py`

**Interfaces:**
- Consumes: `QuoteFailure`, `PreviewResult`, `token_balance_raw`, `_typed_jupiter_exit_quote`.
- Produces: `preview_exit_fn(position) -> PreviewResult`; failed balance reads use provider `solana:balance_rpc`, failed quote reads use `solana:jupiter`, and every outcome uses provider group `solana:exact_quote`.

- [ ] **Step 1: Write failing boundary tests**

Add focused tests whose injected HTTP transport fails once then succeeds for the balance RPC and
Jupiter GET respectively. Assert a successful preview, exactly two attempts at the failed boundary,
one attempt at the other boundary, and one `0.2` sleep. Add double-failure assertions for the two
provider IDs and the shared provider group. Cover JSON-RPC `-32429` as a non-retryable rate limit.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m pytest -n 0 tests/test_memecoin_real_seams.py -q
```

Expected: the new transient tests fail because each boundary currently attempts once, and the
balance double-failure test reports `solana:jupiter`, and the result has no separate provider group.

- [ ] **Step 3: Implement the minimum retry**

Import `sleep` as `_sleep`, define the retryable set `{RPC_TRANSPORT, PROVIDER_TIMEOUT}` beside the
preview, and repeat only the failed balance or Jupiter read once after `_sleep(0.2)`. Return
`solana:balance_rpc` for a final balance failure and preserve `solana:jupiter` for a final Jupiter
failure. Carry `solana:exact_quote` as a separate provider group through sweep classification and
alert aggregation; expose the failed boundary in outage copy.

- [ ] **Step 4: Run focused verification**

Run:

```bash
python3 -m pytest -n 0 tests/test_memecoin_real_seams.py tests/test_memecoin_quote_infra_alerts.py tests/test_memecoin_engine_authoritative_exit.py -q
python3 scripts/validate_schema_refs.py
```

Expected: all focused tests pass and schema validation reports no violations.

- [ ] **Step 5: Commit the implementation**

```bash
git add sniper/execution/solana_real_seams.py tests/test_memecoin_real_seams.py
git commit -m "fix(memecoin): retry transient Solana exit previews"
```

### Task 2: Merge and deploy the pinned release

**Files:**
- Runtime state only: `.runtime/memecoin/current`, `data/memecoin_deploy.json`

**Interfaces:**
- Consumes: merged commit reachable from `origin/main`.
- Produces: one healthy funded runner pinned to that exact SHA.

- [ ] **Step 1: Run the Knowledge Sync Gate and open the PR**

Use a bugfix-only Knowledge Delta Report, push the branch, open the PR, and require schema plus
knowledge-sync checks.

- [ ] **Step 2: Build and verify the exact release without a backup or full suite**

Run the deploy gates in dry-run mode to materialize the immutable release, then run the focused
tests from that exact release:

```bash
MERGED_SHA=$(gh pr view --json mergeCommit --jq '.mergeCommit.oid')
SNIPER_DEPLOY_SKIP_VALIDATION=1 scripts/deploy_memecoin_runtime.sh "$MERGED_SHA" --dry-run --defer-backup
(cd ".runtime/memecoin/releases/$MERGED_SHA" && \
  python3 -m pytest -n 0 tests/test_memecoin_real_seams.py tests/test_memecoin_quote_infra_alerts.py tests/test_memecoin_engine_authoritative_exit.py tests/test_memecoin_quote_demand.py tests/test_memecoin_fastlane.py -q)
```

The explicit operator instruction forbids both the broad suite and backups. The exact-release
focused tests replace the skipped broad deploy suite; `--defer-backup` records the no-backup
exception.

- [ ] **Step 3: Deploy the already-verified exact release**

```bash
SNIPER_DEPLOY_SKIP_VALIDATION=1 scripts/deploy_memecoin_runtime.sh "$MERGED_SHA" --defer-backup
```

- [ ] **Step 4: Verify the running release**

Confirm manifest SHA, current symlink, one exact-SHA runner, fresh authenticated heartbeat, TPU
ready, zero pending reservations, unchanged open funded position, and no backup artifacts. Inspect
new quote-infrastructure alerts: a single transient attempt must no longer page; an exhausted failure
must report provider `solana:exact_quote` plus boundary `solana:balance_rpc` or `solana:jupiter`.

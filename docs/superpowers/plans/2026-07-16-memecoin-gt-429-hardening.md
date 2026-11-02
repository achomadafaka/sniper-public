# Memecoin GeckoTerminal 429 Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute inline; no subagent dispatch is authorized for this hotfix.

**Goal:** Stop GeckoTerminal retry amplification during 429 windows and repair the stale UI contract test.

**Architecture:** Add a nonblocking process-wide HTTP gate and bounded shared cooldown to the
existing GT client. Preserve cache-first behavior and all funded strategy semantics. Correct the UI
test to match the already-deployed Wallet x Arm matrix.

**Tech Stack:** Python 3.13, pytest, stdlib `threading`, existing JavaScript static contract tests.

## Global Constraints

- No new provider or network request.
- Never sleep in the GT client or entry/exit path.
- Missing prices remain missing; never emit zero.
- One logical change per commit.

---

### Task 1: Shared GT cooldown and request gate

**Files:**
- Modify: `sniper/execution/geckoterminal_quote.py`
- Modify: `sniper/settings/memecoin.py`
- Test: `tests/test_memecoin_geckoterminal_quote.py`

**Interfaces:**
- Consumes: existing `gt_stats_batch_keyed`, `gt_price_for`, and injected `http`/`clock` seams.
- Produces: identical public return types plus internal rate-limited sentinel handling.

- [ ] Add a failing 65-address test proving first-chunk 429 currently calls all three chunks.
- [ ] Add a failing test proving a second caller currently retries inside the cooldown window.
- [ ] Add a failing concurrency test proving overlapping callers currently overlap HTTP.
- [ ] Implement the minimal nonblocking gate, cooldown, `Retry-After` parsing, and batch abort.
- [ ] Run `python3 -m pytest tests/test_memecoin_geckoterminal_quote.py -q`.
- [ ] Commit the GT hardening change.

### Task 2: Current Performance-tab contract

**Files:**
- Modify: `tests/test_memecoin_ui_honest_copy.py`

**Interfaces:**
- Consumes: `/api/memecoin/traders/arm-matrix`, `wallet.by_arm`, and `alpha_b` already in `static/app.js`.
- Produces: a static test that guards current UI behavior rather than a retired endpoint payload.

- [ ] Replace the already-failing obsolete assertion with the current arm-matrix assertions.
- [ ] Run `python3 -m pytest tests/test_memecoin_ui_honest_copy.py -q`.
- [ ] Commit the test correction separately.

### Task 3: Verification and deployment

**Files:** no production additions.

- [ ] Run focused capture, pricing, UI, concurrency, runtime-deploy, schema, compile, node syntax,
  and full `tests/test_memecoin*.py` validation.
- [ ] Run the Knowledge Sync Gate in audit-only mode.
- [ ] Review `origin/main...HEAD`, push, open the PR, wait for checks, and merge.
- [ ] Deploy the exact merged current-main SHA through `scripts/deploy_memecoin_runtime.sh`.
- [ ] Verify pinned PID/path/heartbeat and observe at least one blind-capture and position-mark cycle.

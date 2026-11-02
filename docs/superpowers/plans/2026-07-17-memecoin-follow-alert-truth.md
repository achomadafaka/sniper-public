# Memecoin Follow-Alert Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove follow-alert notification latency from funded entry decisions and make every follow alert show chain-aware token metadata plus the exact real-entry outcome.

**Architecture:** Persist and execute exactly as today, but move follow notification construction after the real hook. Read metadata and attempts from local chain-aware indexed helpers, render explicit outcome semantics, and enqueue production Telegram sends on one bounded worker.

**Tech Stack:** Python 3, SQLite, pytest, existing memecoin runner and Telegram sender.

## Global Constraints

- Coin identity is `(network_id, mint)`; Robinhood remains network `4663`.
- Never infer a specific EVM chain from address shape.
- No new network request and no Telegram work before or inside the follow funded decision.
- Do not alter reservations, safety checks, duplicate-submit protection, sizing, TP, SL, exits, wallet arming, or paper strategy behavior.
- Missing facts remain explicit; never synthesize an attempt, stage, ticker, market cap, or successful entry.
- Keep `follow_activity` out of `memecoin_alert_latency`, which measures `global_feed` only.

---

### Task 1: Exact local alert attribution

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `tests/test_memecoin_entry_trace.py`
- Modify: `tests/test_memecoin_alert_reality.py`

**Interfaces:**
- Produces: `entry_attempt_for_origin(conn, origin_event_id) -> dict | None`.
- Produces: `resolve_mint_ticker(conn, mint, *, network_id) -> str | None`.
- Preserves: existing `resolve_mint_mcap(conn, mint, *, network_id=...)` behavior.

- [ ] **Step 1: Write failing exact-attempt tests**

Add tests proving `entry_attempt_for_origin` returns only the supplied `origin_event_id`, exposes terminal/reservation/submission fields, and has an `origin_event_id` index.

- [ ] **Step 2: Verify the tests fail for missing helper/index**

Run:

```bash
python3 -m pytest tests/test_memecoin_entry_trace.py -q
```

Expected: failures because `entry_attempt_for_origin` and `idx_mc_entry_attempt_origin` do not exist.

- [ ] **Step 3: Implement the indexed attempt lookup**

Create `idx_mc_entry_attempt_origin` in `ensure_memecoin_tables` and add the bounded lookup. Return `None` for blank IDs; otherwise return the newest exact-origin row as a dict.

- [ ] **Step 4: Write failing chain-aware ticker tests**

Add same-address Base/Robinhood events and assert each lookup returns only its own chain's latest non-empty ticker. Assert blank/unknown inputs return `None`.

- [ ] **Step 5: Verify the ticker tests fail**

Run:

```bash
python3 -m pytest tests/test_memecoin_alert_reality.py -q
```

Expected: failure because `resolve_mint_ticker` does not exist.

- [ ] **Step 6: Implement the chain-aware ticker lookup**

Use `COIN_NETWORK_SQL`, require explicit `network_id`, filter empty tickers, and order by observed receipt time then row ID. Do not add an address-shape fallback.

- [ ] **Step 7: Run focused persistence tests**

```bash
python3 -m pytest tests/test_memecoin_entry_trace.py tests/test_memecoin_alert_reality.py -q
python3 scripts/validate_schema_refs.py
git diff --check
```

- [ ] **Step 8: Commit**

```bash
git add sniper/persistence/db_memecoin.py tests/test_memecoin_entry_trace.py tests/test_memecoin_alert_reality.py
git commit -m "fix(memecoin): index exact follow attempt attribution"
```

### Task 2: Truthful entry-result message

**Files:**
- Modify: `sniper/services/memecoin_telegram.py`
- Modify: `tests/test_memecoin_alert_reality.py`
- Modify: `tests/test_memecoin_real_deploy.py`
- Modify: `tests/test_memecoin_follow_alert.py`

**Interfaces:**
- Consumes alert context keys: `entry_status`, `entry_reason`, `attempt_id`, `pending_id`, `tx_sig`, `position_id`.
- Produces deterministic `ARMED`, result, reason, attempt, stage, and color lines from those facts.

- [ ] **Step 1: Write failing message-semantic tests**

Cover: `REAL-ELIGIBLE` is absent; `ARMED` is present; a chase refusal renders orange, `REFUSED`, `skipped_chase`, exact reason, exact attempt ID, and `pre-reservation`; a fill with position ID renders green and `position persisted`; pending/unknown/not-attempted states render yellow without fabricated identifiers.

- [ ] **Step 2: Verify the tests fail against the current renderer**

```bash
python3 -m pytest tests/test_memecoin_alert_reality.py tests/test_memecoin_real_deploy.py tests/test_memecoin_follow_alert.py -q
```

Expected: failures on the old green title, `REAL-ELIGIBLE`, and missing result lines.

- [ ] **Step 3: Implement minimal pure rendering helpers**

Add private helpers that map persisted facts to outcome color and reached stage. Extend both follow renderers through the existing `context` argument. Keep the renderer DB-free and preserve legacy callers without entry context.

- [ ] **Step 4: Run focused renderer tests**

```bash
python3 -m pytest tests/test_memecoin_alert_reality.py tests/test_memecoin_real_deploy.py tests/test_memecoin_follow_alert.py tests/test_memecoin_telegram.py -q
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_telegram.py tests/test_memecoin_alert_reality.py tests/test_memecoin_real_deploy.py tests/test_memecoin_follow_alert.py
git commit -m "fix(memecoin): render exact follow entry outcomes"
```

### Task 3: Execute first and dispatch notification off-thread

**Files:**
- Modify: `sniper/services/memecoin_follow_activity.py`
- Modify: `sniper/services/memecoin_telegram.py`
- Modify: `tests/test_memecoin_follow_activity.py`
- Modify: `tests/test_memecoin_telegram.py`

**Interfaces:**
- Produces: `enqueue_telegram(text) -> bool`, a non-blocking bounded production dispatcher.
- Consumes: exact event attempt and chain-aware ticker/mcap helpers from Task 1.
- Preserves: injected `alert_fn(message)` as a synchronous test seam.

- [ ] **Step 1: Write failing ordering and outcome-context tests**

Use an injected real hook and alert callback to assert call order is `real_hook`, then `alert_fn`.
Persist a refused exact-origin attempt in the hook and assert the callback receives the refusal,
reason, attempt ID, enriched ticker/mcap, and chain. Add disabled-lane and hook-exception cases.

- [ ] **Step 2: Verify the follow tests fail**

```bash
python3 -m pytest tests/test_memecoin_follow_activity.py -q
```

Expected: alert callback runs before the hook and message lacks exact outcome/enrichment.

- [ ] **Step 3: Move the real hook before alert construction**

Capture its result/exception once, query the exact trace, build chain-aware local context, and only
then render the single follow alert. Keep paper and holder-evidence behavior unchanged.

- [ ] **Step 4: Write failing non-blocking dispatcher tests**

Inject a blocking sender into a test worker seam and assert `enqueue_telegram` returns before that
sender completes. Fill a size-one queue and assert the next enqueue returns `False` without raising.

- [ ] **Step 5: Verify dispatcher tests fail**

```bash
python3 -m pytest tests/test_memecoin_telegram.py -q
```

Expected: failure because `enqueue_telegram` does not exist.

- [ ] **Step 6: Implement the bounded sender**

Use `queue.Queue`, one lock-protected daemon worker, and `put_nowait`. Production follow alerts call
`enqueue_telegram`; explicit `alert_fn` remains direct for deterministic tests. Log queue saturation
and worker failures; never retry in the entry thread.

- [ ] **Step 7: Run focused integration tests**

```bash
python3 -m pytest tests/test_memecoin_follow_activity.py tests/test_memecoin_follow_alert.py tests/test_memecoin_alert_reality.py tests/test_memecoin_telegram.py tests/test_memecoin_real_alerts.py -q
git diff --check
```

- [ ] **Step 8: Commit**

```bash
git add sniper/services/memecoin_follow_activity.py sniper/services/memecoin_telegram.py tests/test_memecoin_follow_activity.py tests/test_memecoin_telegram.py
git commit -m "fix(memecoin): execute follow entries before alerts"
```

### Task 4: Adversarial verification and branch close

**Files:**
- Modify only if verification proves a defect.

**Interfaces:**
- Validates the full design and deployment contract.

- [ ] **Step 1: Review the complete branch diff**

Check ordering, exact-origin joins, chain scoping, queue bounds, exception paths, duplicate alerts,
and all unchanged funded safety/reservation calls.

- [ ] **Step 2: Run required verification**

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/test_memecoin*.py -q
python3 -m compileall -q sniper scripts
git diff --check origin/main...HEAD
```

Run the relevant runtime-deploy and concurrency tests named by `rg -l 'concurr|deploy' tests/test_memecoin*.py`.

- [ ] **Step 3: Run the Knowledge Sync Gate**

Record the new spec as the knowledge delta. This change does not alter doctrine, legacy status,
paper-strategy semantics, or funded strategy semantics.

- [ ] **Step 4: Push, merge, and deploy only after all gates pass**

Push the feature branch, open a PR with the 11-question Knowledge Delta Report, merge without force,
then run `scripts/deploy_memecoin_runtime.sh` from merged `main`. Verify pinned SHA, one runner,
authenticated heartbeat, and a completed first exit sweep.

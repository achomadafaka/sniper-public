# Memecoin Telegram Chain Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every memecoin Telegram alert state exact-chain funded authority, use measurable chain-scoped paper evidence, and leave a safe durable record of asynchronous delivery.

**Architecture:** Reuse the existing chain registry and `measurable_outcome_sql()` as the only truth sources. Keep rendering pure by passing an explicit context from the runner. Add a small delivery-audit table and make the existing dispatcher record redacted content and exact hashes from its worker thread, never from the funded caller.

**Tech Stack:** Python 3, SQLite, pytest, standard-library `hashlib`, existing memecoin chain and clock helpers.

## Global Constraints

- Funded authority is exact-chain; approval on another chain never confers authority.
- Unsupported chains show `paper gate: not applicable`.
- Signed one-tap URLs are never persisted.
- Telegram delivery and audit failures stay fail-soft and cannot block the funded loop.
- No strategy, routing, sizing, capital-control, reservation, or funded-authority mutation.
- Deploy only a merged SHA through `scripts/deploy_memecoin_runtime.sh` after its safety gates pass.

---

### Task 1: Measurable alpha-b evidence

**Files:**
- Modify: `sniper/persistence/db_memecoin.py:2045-2110`
- Test: `tests/test_memecoin_ui_honest_copy.py`
- Test: `tests/test_memecoin_funded_authority_scoping.py`

**Interfaces:**
- Consumes: `measurable_outcome_sql(table: str = "memecoin_positions") -> str`
- Produces: unchanged `alpha_b_copy_stats(conn, source_user_id, network_id=None, *, chain=None) -> dict` and `alpha_b_copy_stats_all(conn) -> dict`

- [ ] **Step 1: Write failing single and batched statistics regressions**

Create ledger rows with positive entry fills and these close shapes: executable route-quoted exit,
all-noquote exit, orphan close without exit fill, unquotable exit with zero mark, and Fomo provenance.
Assert only the executable row contributes to both helpers:

```python
assert m.alpha_b_copy_stats(c, "u1", chain="robin") == {
    "n_closes": 1,
    "pnl": 7.0,
}
assert m.alpha_b_copy_stats_all(c)["u1"] == {
    "n_closes": 1,
    "pnl": 7.0,
}
```

- [ ] **Step 2: Run the regressions and confirm they fail**

Run:

```bash
python3 -m pytest tests/test_memecoin_ui_honest_copy.py tests/test_memecoin_funded_authority_scoping.py -q
```

Expected: FAIL because censored rows still contribute to at least one helper.

- [ ] **Step 3: Use the shared measurable predicate in both queries**

Build both WHERE clauses from the existing predicate:

```python
q = (
    "SELECT COUNT(*) n, COALESCE(SUM(realized_pnl),0) pnl "
    "FROM memecoin_positions WHERE source_user_id=? AND strategy='alpha_b' "
    "AND COALESCE(execution_mode,'paper')!='real' AND "
    + measurable_outcome_sql()
)
```

Keep chain normalization and the public return shapes unchanged. Update docstrings from
`fomo-excluded` to `measurable` so callers do not describe censored outcomes as honest evidence.

- [ ] **Step 4: Run focused tests**

```bash
python3 -m pytest tests/test_memecoin_ui_honest_copy.py tests/test_memecoin_funded_authority_scoping.py tests/test_memecoin_real_approval_gate.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/persistence/db_memecoin.py tests/test_memecoin_ui_honest_copy.py tests/test_memecoin_funded_authority_scoping.py tests/test_memecoin_real_approval_gate.py
git commit -m "fix(memecoin): exclude censored closes from alpha evidence"
```

### Task 2: Exact-chain alert context and copy

**Files:**
- Modify: `sniper/services/memecoin_runner.py:92-160`
- Modify: `sniper/services/memecoin_telegram.py:110-265`
- Modify: `sniper/services/memecoin_follow_activity.py:102-140`
- Modify: `sniper/services/memecoin_evm_leg.py:145-156`
- Test: `tests/test_memecoin_alert_reality.py`
- Test: `tests/test_memecoin_real_deploy.py`
- Test: `tests/test_memecoin_follow_alert.py`

**Interfaces:**
- Consumes: `resolve_chain(value)`, `canonical_chain_id(value)`, `funded_chain_keys()`, `wallet_armed_chains(conn, uid)`, and chain-scoped `alpha_b_copy_stats()`
- Produces: alert context keys `authority_status`, `gate_applicable`, `chain`, `real_armed`, `paper_pnl`, `paper_n`, and `gate_ok`

- [ ] **Step 1: Replace stale global-authority tests with failing exact-chain tests**

Add assertions for three cases:

```python
assert "watch-only" in robin_signal_from_solana_armed_wallet
assert "ARMED" not in robin_signal_from_solana_armed_wallet
assert "unfunded chain - watch-only" in bsc_signal
assert "paper gate: not applicable" in bsc_signal
```

Insert measurable Solana and Robinhood closes with different PnL, then assert the Robinhood alert
shows only Robinhood figures. Assert every front-run alert includes:

```python
assert "source buy received - our entry decision pending" in message
```

- [ ] **Step 2: Run the alert regressions and confirm they fail**

```bash
python3 -m pytest tests/test_memecoin_alert_reality.py tests/test_memecoin_real_deploy.py tests/test_memecoin_follow_alert.py -q
```

Expected: FAIL on global badge, pooled evidence, unsupported-chain gate, and pending-entry copy.

- [ ] **Step 3: Build exact-chain context in `_prepare_alert()`**

Resolve the event network once and derive authority without a global roster:

```python
chain = resolve_chain(getattr(ev, "network_id", None))
funded = chain in funded_chain_keys()
armed_ids = set(m.wallet_armed_chains(conn, ev.source_user_id))
armed_on_chain = funded and canonical_chain_id(chain) in armed_ids
g = m.alpha_b_copy_stats(conn, ev.source_user_id, chain=chain) if funded else None
```

Set `gate_applicable=False` and omit paper figures when `funded` is false. Preserve `real_armed` as
the exact-chain boolean for existing renderers and consumers.

- [ ] **Step 4: Render explicit authority, gate applicability, and pending-entry copy**

Add pure helpers in `memecoin_telegram.py`:

```python
def _authority_label(context):
    status = (context or {}).get("authority_status")
    if status == "armed":
        return "ARMED ⚡"
    if status == "unfunded_chain":
        return "unfunded chain - watch-only"
    return "watch-only"
```

Make `_honest_record()` return `paper gate: not applicable` when `gate_applicable` is false. Add the
pending-entry line only when no attributed `entry_status` exists.

- [ ] **Step 5: Scope the other funded evidence consumers**

In `_follow_alert_context()`, derive the chain from `ev.network_id`, use exact-chain alpha evidence,
and mark unsupported chains not applicable. In `memecoin_evm_leg.py`, pass the reserved EVM chain to
`alpha_b_copy_stats(..., chain=resolve_chain(chain))` before granting the proven-wallet tax cap.

- [ ] **Step 6: Run focused tests**

```bash
python3 -m pytest tests/test_memecoin_alert_reality.py tests/test_memecoin_real_deploy.py tests/test_memecoin_follow_alert.py tests/test_memecoin_translation_gates.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sniper/services/memecoin_runner.py sniper/services/memecoin_telegram.py sniper/services/memecoin_follow_activity.py sniper/services/memecoin_evm_leg.py tests/test_memecoin_alert_reality.py tests/test_memecoin_real_deploy.py tests/test_memecoin_follow_alert.py
git commit -m "fix(memecoin): render exact-chain Telegram authority"
```

### Task 3: Asynchronous delivery audit

**Files:**
- Modify: `sniper/persistence/db_memecoin.py:1300-1330`
- Modify: `sniper/persistence/db_memecoin.py:5680-5720`
- Modify: `sniper/services/memecoin_telegram.py:1-110`
- Create: `tests/test_memecoin_telegram_delivery_audit.py`

**Interfaces:**
- Produces: `record_telegram_delivery(conn, fields) -> int`
- Produces: `redact_telegram_text(text: str) -> str`
- Produces: backward-compatible `enqueue_telegram(text, *, kind="generic", source_id=None) -> bool`

- [ ] **Step 1: Write failing schema, redaction, dispatcher-success, and dispatcher-failure tests**

Assert the schema contains `memecoin_telegram_delivery_audit`. Test redaction and hashing:

```python
raw = "BUY\n⚡ tap: https://example.test/secret\nmint: M"
assert redact_telegram_text(raw) == "BUY\n⚡ tap: [redacted]\nmint: M"
assert sha256(raw.encode()).hexdigest() == recorded["message_sha256"]
assert "secret" not in recorded["rendered_text_redacted"]
```

Inject a synchronous audit callback into `_TelegramDispatcher`, enqueue one message, join the queue,
and assert it records success. Repeat with a sender that raises and assert `error_class` is bounded to
the exception class and the worker remains alive. Fill a non-running queue and assert saturation
returns `False` without raising.

- [ ] **Step 2: Run the new test and confirm it fails**

```bash
python3 -m pytest tests/test_memecoin_telegram_delivery_audit.py -q
```

Expected: FAIL because the table and audit interfaces do not exist.

- [ ] **Step 3: Add the delivery-audit schema and insert helper**

Create the table in the existing atomic schema transaction:

```sql
CREATE TABLE IF NOT EXISTS memecoin_telegram_delivery_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_kind TEXT NOT NULL,
    source_id TEXT,
    queued_at TEXT NOT NULL,
    send_started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    send_ok INTEGER NOT NULL,
    send_ms REAL NOT NULL,
    rendered_text_redacted TEXT NOT NULL,
    message_sha256 TEXT NOT NULL,
    error_class TEXT
)
```

Add an index on `(completed_at, message_kind)` and an insert-only helper that commits its own row.

- [ ] **Step 4: Add redaction and worker-thread audit recording**

Queue a dictionary containing the raw text, kind, source ID, corrected-UTC queued timestamp,
redacted text, and exact SHA-256. The worker calls the sender, normalizes its `(ok, ms)` result, then
invokes the injected/default audit callback. The default callback opens its own short-lived writable
connection, inserts one row, and closes it. Both send and audit exceptions are logged and swallowed.

- [ ] **Step 5: Run the delivery-audit tests**

```bash
python3 -m pytest tests/test_memecoin_telegram_delivery_audit.py tests/test_memecoin_alerts.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sniper/persistence/db_memecoin.py sniper/services/memecoin_telegram.py tests/test_memecoin_telegram_delivery_audit.py
git commit -m "feat(memecoin): persist redacted Telegram delivery audit"
```

### Task 4: Full verification and release

**Files:**
- Modify: `docs/superpowers/plans/2026-08-05-memecoin-telegram-chain-truth.md` only for checked steps

**Interfaces:**
- Consumes: all prior task outputs
- Produces: merged and pinned running release with fresh local and live evidence

- [ ] **Step 1: Run static and focused verification**

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/test_memecoin_ui_honest_copy.py tests/test_memecoin_funded_authority_scoping.py tests/test_memecoin_real_approval_gate.py tests/test_memecoin_alert_reality.py tests/test_memecoin_real_deploy.py tests/test_memecoin_follow_alert.py tests/test_memecoin_translation_gates.py tests/test_memecoin_alerts.py tests/test_memecoin_telegram_delivery_audit.py -q
```

Expected: schema validation succeeds and every selected test passes.

- [ ] **Step 2: Run the relevant memecoin regression set**

```bash
python3 -m pytest tests/test_memecoin_arm_matrix.py tests/test_memecoin_paper_report.py tests/test_memecoin_copy_arm_selector.py tests/test_memecoin_chains.py -q
```

Expected: PASS.

- [ ] **Step 3: Inspect branch diff and commit plan completion**

```bash
git diff --check
git status --short --branch
git log --oneline --decorate -6
```

Commit the checked plan only if tracking marks changed.

- [ ] **Step 4: Merge without bypassing branch protection**

Push the feature branch, open a PR with the required Knowledge Sync block, wait for required checks,
and merge through GitHub. Do not push directly to `origin/main`, force-push, rebase, squash locally, or
amend. Resolve the merged SHA from the remote default branch before deployment.

- [ ] **Step 5: Deploy the merged SHA through the pinned runtime gate**

```bash
scripts/deploy_memecoin_runtime.sh <merged-sha>
```

Do not bypass backup, reservation, reconciliation, manifest, or process-count checks.

- [ ] **Step 6: Verify the running release**

Check the release symlink and manifest, actual funded process command/count, fresh heartbeat, logs,
kill switch, pending reservations, and schema presence. Run a read-only synthetic render against the
running release for Solana-armed/Robinhood-watch-only and BSC-not-applicable cases. Organic Telegram
proof is required when signals arrive; absence of an eligible signal is reported as pending, never
fabricated.

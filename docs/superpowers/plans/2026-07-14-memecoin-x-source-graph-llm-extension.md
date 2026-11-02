# Memecoin X Source Graph Optional LLM Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional, asynchronous, evidence-cited LLM enrichment for deterministic X events, scheduled digests, and operator-requested analysis without placing an LLM in capture, correlation, notification, or execution paths.

**Architecture:** Deterministic events enqueue frozen, hashed evidence snapshots. A tool-free provider interface receives only structured persisted evidence and returns a closed-schema annotation that is validated before storage. Provider failure, budget exhaustion, prompt injection, invalid output, or stale completion leaves deterministic events and notifications unchanged.

**Tech Stack:** Python 3.13, SQLite, JSON, hashlib, pytest. Provider SDK/HTTP adapter is injected at the worker boundary; core code has no vendor dependency.

## Global Constraints

- Start only after Phase-4 deterministic output passes its dry-run.
- LLM jobs are disabled by default. No per-card call.
- Modes: `event`, `digest`, `on_request` only.
- The LLM has no browser, shell, database, network-fetch, account, Telegram, or trading tools.
- Do not send live DOM/HTML, browser session state, secrets, arbitrary fetched URLs, execution state, or later evidence.
- Exact coin identity and every factual claim must cite frozen evidence IDs.
- Never accept token-safety verdicts, price/return forecasts, entries, exits, sizing, vetoes, or execution recommendations.
- On-request answers use a persisted `as_of` snapshot, not the live browser.

---

### Task 1: Add auditable LLM job persistence

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `tests/test_memecoin_x_store.py`
- Modify: `tests/test_brain_index.py`

**Tables:**

```sql
CREATE TABLE memecoin_x_llm_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_key TEXT UNIQUE NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('event','digest','on_request')),
    state TEXT NOT NULL CHECK (state IN
        ('pending','running','accepted','rejected','failed','timed_out','over_budget','stale')),
    as_of TEXT NOT NULL,
    network_id TEXT,
    mint TEXT,
    evidence_ids_json TEXT NOT NULL,
    input_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    output_schema_version TEXT NOT NULL,
    generation_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    token_input INTEGER,
    token_output INTEGER,
    budget_result TEXT,
    validation_result TEXT,
    bounded_error TEXT,
    raw_response_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE memecoin_x_llm_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER UNIQUE NOT NULL,
    summary TEXT NOT NULL,
    intent TEXT NOT NULL CHECK (intent IN ('promotion','warning','analysis','unknown')),
    uncertainty TEXT NOT NULL,
    risk_concerns_json TEXT NOT NULL,
    claims_json TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    display_only INTEGER NOT NULL DEFAULT 1 CHECK (display_only=1)
);

CREATE TABLE memecoin_x_llm_watermarks (
    mode TEXT PRIMARY KEY CHECK (mode IN ('digest')),
    last_event_id INTEGER NOT NULL,
    committed_at TEXT NOT NULL
);
```

- [ ] **Step 1: Write failing schema/transition tests**

Prove legal state transitions, immutable input hash/version fields after running, one accepted annotation per job, and digest watermark advances only after an accepted or explicitly empty digest completion.

- [ ] **Step 2: Verify red, implement, verify green, commit**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_brain_index.py -q
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
git add sniper/persistence/db_memecoin_x.py tests/test_memecoin_x_store.py tests/test_brain_index.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(memecoin-x): persist auditable llm jobs"
```

---

### Task 2: Freeze evidence and validate closed-schema output

**Files:**
- Create: `sniper/analysis/memecoin_x/llm_contract.py`
- Create: `tests/test_memecoin_x_llm_contract.py`

**Interfaces:**
- `freeze_event_evidence(conn, *, signal_event_id: int, as_of: str) -> dict`
- `freeze_digest_evidence(conn, *, after_event_id: int, through_event_id: int, as_of: str) -> dict`
- `freeze_on_request_evidence(conn, *, network_id: str | None, mint: str | None, as_of: str, window: str) -> dict`
- `canonical_input_hash(snapshot: dict) -> str`
- `validate_annotation(raw: object, *, snapshot: dict) -> dict`

**Accepted output:**

```python
{
    "summary": "max 600 characters",
    "intent": "promotion|warning|analysis|unknown",
    "uncertainty": "max 300 characters",
    "risk_concerns": ["max five strings, each max 200 characters"],
    "claims": [{
        "text": "max 300 characters",
        "evidence_ids": [123, 456],
        "network_id": "4663",
        "mint": "0x...",
    }],
}
```

- [ ] **Step 1: Write failing adversarial tests**

Cover prompt injection in text/handle/ticker/URL, unknown evidence IDs, mismatched coin tuples, extra keys, missing keys, oversized strings/arrays, NaN, invalid UTF-8 replacement, unsupported safety/forecast/action fields, and evidence that arrived after `as_of`.
For media-only cards, prove the snapshot contains only the stored diagnostic and text facts; it must not invent or request image/video content.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_llm_contract.py -q`

- [ ] **Step 3: Implement canonical snapshots and strict validation**

Use sorted-key compact JSON and SHA-256. Treat all captured text as quoted data inside the snapshot; never interpolate it into system instructions. Reject invalid output; never repair it with uncaptured information.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_memecoin_x_llm_contract.py -q
git add sniper/analysis/memecoin_x/llm_contract.py tests/test_memecoin_x_llm_contract.py
git commit -m "feat(memecoin-x): freeze and validate llm evidence"
```

---

### Task 3: Enqueue event, digest, and on-request jobs

**Files:**
- Create: `sniper/services/memecoin_x_llm_queue.py`
- Create: `tests/test_memecoin_x_llm_queue.py`

**Interfaces:**
- `enqueue_event_job(conn, *, signal_event_id, as_of, provider, model, versions) -> int | None`
- `enqueue_digest_job(conn, *, as_of, provider, model, versions) -> int | None`
- `enqueue_on_request_job(conn, *, request_id, as_of, window, network_id=None, mint=None, provider, model, versions) -> int`

- [ ] **Step 1: Write failing queue tests**

Event jobs only follow persisted `fast_consensus` or `tier2_scout` events. Digest returns `None` and makes no job when no events exist after the committed watermark. Duplicate logical triggers return the existing job. On-request requires a unique operator request ID and frozen `as_of`.

- [ ] **Step 2: Verify red, implement, verify green, commit**

```bash
python3 -m pytest tests/test_memecoin_x_llm_queue.py -q
git add sniper/services/memecoin_x_llm_queue.py tests/test_memecoin_x_llm_queue.py
git commit -m "feat(memecoin-x): queue bounded llm enrichment"
```

---

### Task 4: Add a tool-free worker with an injected provider

**Files:**
- Create: `sniper/services/memecoin_x_llm_worker.py`
- Create: `scripts/run_memecoin_x_llm_worker.py`
- Create: `tests/test_memecoin_x_llm_worker.py`

**Provider interface:**

```python
class LLMProvider(Protocol):
    name: str
    model: str

    def generate(self, *, system_text: str, snapshot: dict,
                 output_schema: dict, timeout_s: float) -> dict:
        """Return {'output': object, 'usage': {'input_tokens': int, 'output_tokens': int}}."""
```

- [ ] **Step 1: Write failing worker tests**

Use a fake provider. Cover success, timeout, provider exception, invalid schema, stale completion after version change, duplicate worker claim, daily request budget, daily token budget, context-size rejection, and process restart while a job is `running`.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_llm_worker.py -q`

- [ ] **Step 3: Implement the core worker**

Claim one job transactionally, increment attempt, enforce budgets before provider invocation, call with a hard timeout, validate output, persist raw structured response plus accepted annotation, and never alter the underlying signal event/outbox.

CLI defaults to `--dry-run`. Actual provider invocation requires `MEMECOIN_X_LLM_ENABLED=1`. The provider adapter is supplied through an importable callable in application context or CLI construction; core tests must not require a vendor SDK or network.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_memecoin_x_llm_worker.py -q
git add sniper/services/memecoin_x_llm_worker.py scripts/run_memecoin_x_llm_worker.py tests/test_memecoin_x_llm_worker.py
git commit -m "feat(memecoin-x): run tool-free llm enrichment"
```

---

### Task 5: Surface annotations and on-request queueing

**Files:**
- Modify: `sniper/routes/api_memecoin_x.py`
- Modify: `static/app.js`
- Modify: `tests/test_api_memecoin_x.py`
- Modify: `tests/test_memecoin_x_ui_contract.py`

**Endpoints:**

- `GET /api/memecoin/x/annotations?limit=50`
- `POST /api/memecoin/x/analysis` with `{request_id, as_of, window, network_id?, mint?}`
- `GET /api/memecoin/x/analysis/<request_id>`

- [ ] **Step 1: Write failing API/UI tests**

POST only queues a job; it never invokes the provider on the request thread. Reject future/naive `as_of`, invalid window/network/address, duplicate request IDs with conflicting inputs, and requests when LLM mode is disabled. UI shows deterministic event first and a later annotation labeled `LLM, display-only`; rejected/failed jobs never replace the event.

- [ ] **Step 2: Verify red, implement, verify green, commit**

```bash
python3 -m pytest tests/test_api_memecoin_x.py tests/test_memecoin_x_ui_contract.py -q
node --check static/app.js
git add sniper/routes/api_memecoin_x.py static/app.js tests/test_api_memecoin_x.py tests/test_memecoin_x_ui_contract.py
git commit -m "feat(memecoin-x): expose optional llm annotations"
```

---

### Task 6: LLM adversarial gate

```bash
python3 -m pytest \
  tests/test_memecoin_x_llm_contract.py \
  tests/test_memecoin_x_llm_queue.py \
  tests/test_memecoin_x_llm_worker.py \
  tests/test_api_memecoin_x.py \
  tests/test_memecoin_x_ui_contract.py -q
python3 scripts/validate_schema_refs.py
git diff --check
```

Run only fake-provider fixtures. Provider credentials, live calls, event enrichment, digests, and on-request execution remain disabled until the rollout plan's explicit operator gate.

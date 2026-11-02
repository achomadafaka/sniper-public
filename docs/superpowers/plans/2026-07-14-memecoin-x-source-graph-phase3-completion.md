# Memecoin X Source Graph Phase 3 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish crash-safe forward correlation and produce reconciled 24h/7d/30d raw source-graph read models for Solana, Robinhood Chain `4663`, and every other explicitly identified network.

**Architecture:** Read Fomo v2 buys from `sniper_tracker.db` through a read-only connection, persist only into the isolated `memecoin_x_*` database, and advance an audited cursor only after each idempotent correlation page completes. Read models are pure SQLite reads over observation-level evidence and forward links; they never read positions or execution tables.

**Tech Stack:** Python 3.13, SQLite, pytest, existing `db_memecoin_x`, `fomo_adapter`, `correlator`, and corrected-clock helpers. No new dependencies.

## Global Constraints

- Work in `/home/bigpapi/Desktop/Sniper/.worktrees/x-source-graph` on `feat/x-source-graph-design`.
- TDD every behavior: failing focused test, minimal implementation, focused pass, then broader pass.
- `x_observed_at` and Fomo `observed_at` must both be `observed_real_utc`; never use X `posted_at` or Fomo `source_ts` for lead time.
- Match only exact `(network_id, normalized mint)` tuples. Robinhood `4663` must never pool with Base `8453`, Ethereum `1`, BSC `56`, or generic EVM.
- Keep `amplified` and `coincidence` lanes separate. Raw counts only; no composite score.
- No imports from execution, strategy, funded-runtime, Telegram, or LLM modules.
- Source DB opens `mode=ro`; only the isolated X DB is writable.
- Do not run against the live X DB until the rollout plan's capture and manual-audit gates pass.

---

### Task 1: Add crash-safe correlator run state

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `tests/test_memecoin_x_store.py`
- Modify: `tests/test_brain_index.py`

**Interfaces:**
- Produces table `memecoin_x_correlator_runs`.
- Produces `start_correlator_run(conn, *, run_id, started_at, after_fomo_id) -> bool`.
- Produces `finish_correlator_run(conn, *, run_id, finished_at, status, through_fomo_id, source_rows, eligible_buys, links_created, links_upgraded, error_detail=None) -> None`.
- Produces `last_completed_fomo_id(conn) -> int`.

- [ ] **Step 1: Write failing schema and state tests**

Assert this exact table contract:

```sql
CREATE TABLE memecoin_x_correlator_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    after_fomo_id INTEGER NOT NULL,
    through_fomo_id INTEGER,
    source_rows INTEGER NOT NULL DEFAULT 0,
    eligible_buys INTEGER NOT NULL DEFAULT 0,
    links_created INTEGER NOT NULL DEFAULT 0,
    links_upgraded INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('running','ok','error')),
    error_detail TEXT
)
```

Tests must prove duplicate `run_id` is rejected idempotently, only an `ok` run advances `last_completed_fomo_id`, and an `error` run preserves its bounded error without advancing the cursor.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_store.py tests/test_brain_index.py -q`

Expected: failure because the table and functions do not exist.

- [ ] **Step 3: Implement the minimal schema and CRUD**

Use parameterized SQL, enum validation in Python, and a 500-character bound for `error_detail`. Add index:

```sql
CREATE INDEX IF NOT EXISTS idx_x_correlator_runs_status_cursor
ON memecoin_x_correlator_runs (status, through_fomo_id)
```

- [ ] **Step 4: Verify green and schema registration**

Run:

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_brain_index.py -q
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
```

Expected: tests pass; schema validator prints `No schema violations found.`

- [ ] **Step 5: Commit**

```bash
git add sniper/persistence/db_memecoin_x.py tests/test_memecoin_x_store.py tests/test_brain_index.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(memecoin-x): audit correlation cursor"
```

---

### Task 2: Page the Fomo source and run correlation safely

**Files:**
- Modify: `sniper/analysis/memecoin_x/fomo_adapter.py`
- Create: `sniper/services/memecoin_x_correlator.py`
- Create: `scripts/run_memecoin_x_correlator.py`
- Modify: `tests/test_memecoin_x_fomo_adapter.py`
- Create: `tests/test_memecoin_x_correlator_service.py`

**Interfaces:**
- Produces immutable `FomoBuyPage(buys: tuple[dict, ...], through_id: int, source_rows: int)`.
- Produces `fetch_eligible_fomo_buy_page(conn, *, after_id=0, limit=1000) -> FomoBuyPage`.
- Preserves existing `fetch_eligible_fomo_buys(...) -> list[dict]` as a compatibility wrapper.
- Produces `run_once(x_conn, fomo_conn, *, run_id, observed_at, page_size=1000, max_pages=100) -> dict`.

- [ ] **Step 1: Write failing paging tests**

Cover these exact cases:

- valid rows return normalized buys including source `id`;
- an invalid address row is excluded from `buys` but still advances `through_id`;
- an empty page leaves `through_id == after_id`;
- page size rejects values outside `1..10000`;
- source connection uses read-only URI mode in the CLI;
- a rerun after link persistence but before cursor completion creates no duplicate links;
- an exception records an `error` run and leaves the prior completed cursor unchanged.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_fomo_adapter.py tests/test_memecoin_x_correlator_service.py -q`

Expected: import failures for the page type and service.

- [ ] **Step 3: Implement the page adapter**

Fetch source rows with the existing eligibility SQL, compute `through_id` from the last SQL row before Python address validation, and return only validated buys. Never mutate the Fomo connection.

- [ ] **Step 4: Implement the service and CLI**

CLI contract:

```text
python3 scripts/run_memecoin_x_correlator.py \
  --x-db data/memecoin_x.db \
  --fomo-db /home/bigpapi/Desktop/Sniper/sniper_tracker.db \
  --once --page-size 1000 --max-pages 100
```

Defaults must be safe: `--once` only, explicit DB paths, no daemon loop, no table creation in the Fomo DB. Obtain `observed_at` from `x_observed_at_or_none`; fail without advancing when clock health is unavailable.

- [ ] **Step 5: Verify green**

Run: `python3 -m pytest tests/test_memecoin_x_fomo_adapter.py tests/test_memecoin_x_correlator.py tests/test_memecoin_x_correlator_service.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add sniper/analysis/memecoin_x/fomo_adapter.py sniper/services/memecoin_x_correlator.py scripts/run_memecoin_x_correlator.py tests/test_memecoin_x_fomo_adapter.py tests/test_memecoin_x_correlator_service.py
git commit -m "feat(memecoin-x): run incremental forward correlation"
```

---

### Task 3: Build reconciled rolling read models

**Files:**
- Create: `sniper/analysis/memecoin_x/read_models.py`
- Create: `tests/test_memecoin_x_read_models.py`

**Interfaces:**
- `build_source_graph(conn, *, as_of: str) -> dict`
- `build_window(conn, *, since: str, as_of: str, label: str) -> dict`
- Exact window labels: `24h`, `7d`, `30d` with seconds `86400`, `604800`, `2592000`.

**Output contract:**

```python
{
    "as_of": "aware ISO",
    "windows": {
        "24h": {
            "since": "aware ISO",
            "sources": [{
                "handle": "poptyedev",
                "role": "tier2_source",
                "network_id": "4663",
                "exact_calls": 3,
                "followed_calls": 2,
                "follow_through_rate": 2 / 3,
                "amplified_links": 1,
                "coincidence_links": 2,
                "distinct_tier1_buyers": 2,
                "direct_count": 1,
                "repost_count": 1,
                "quote_count": 0,
                "reply_count": 0,
            }],
            "suggestions": [],
            "official_context": [],
            "actor_cooccurrence": [{
                "actor_a": "goyimpnl",
                "actor_b": "binkieeefomo",
                "shared_exact_coins": 2,
                "actor_a_exact_coins": 3,
                "actor_b_exact_coins": 4,
            }],
            "coverage": {
                "capture_runs": 0,
                "healthy_clock_runs": 0,
                "coverage_gap_runs": 0,
                "eligible_observations": 0,
                "diagnostic_observations": 0,
                "eligible_tier1_buys": 0,
                "linked_tier1_buys": 0,
                "buy_coverage_rate": None,
            },
        }
    }
}
```

- [ ] **Step 1: Write failing read-model tests**

Fixtures must prove:

- `exact_calls` counts distinct `(canonical_post_id, network_id, mint)`;
- `followed_calls` requires at least one eligible forward link;
- follow-through denominator includes exact eligible calls with no later buy;
- amplified and coincidence counts never merge;
- distinct buyers dedupe multiple links from the same buyer;
- relationship counts remain separate;
- official-context rows appear only in `official_context`;
- unknown authors appear only in `suggestions` with `needs_review=true`;
- actor co-occurrence reports raw pair/shared-call counts separately and never changes ranking weights;
- capture gaps and clock failures remain visible beside rankings;
- zero denominators return `None`, never `0.0`;
- `4663` and `8453` rows remain separate for the same lowercase address;
- `posted_at` changes do not change any read model.
- `memecoin_x_backfill_candidates` rows never enter forward links, rankings, coverage denominators, or co-occurrence counts.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_read_models.py -q`

Expected: import failure.

- [ ] **Step 3: Implement pure parameterized queries**

Parse and validate aware ISO inputs. Use observation/link timestamps only. Sort source rows by `distinct_tier1_buyers DESC`, then `amplified_links DESC`, then `handle COLLATE NOCASE`; this is deterministic ordering, not a composite score.

- [ ] **Step 4: Verify green**

Run: `python3 -m pytest tests/test_memecoin_x_read_models.py tests/test_memecoin_x_correlator.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/memecoin_x/read_models.py tests/test_memecoin_x_read_models.py
git commit -m "feat(memecoin-x): add rolling source graph reads"
```

---

### Task 4: Add a read-only source-graph report CLI

**Files:**
- Create: `scripts/report_memecoin_x_sources.py`
- Create: `tests/test_memecoin_x_sources_report.py`

**Interfaces:**
- CLI arguments: `--db`, optional `--as-of`, `--clock-offset-file`, and `--json`.
- Opens the X DB with `get_readonly_connection` and never calls `ensure_memecoin_x_tables`.

- [ ] **Step 1: Write failing CLI tests**

Assert `--help` runs without `PYTHONPATH`, omitted `--as-of` requires a healthy corrected-clock snapshot, missing DB/table returns a bounded unavailable error, and JSON equals `build_source_graph` for the same fixture.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_x_sources_report.py -q`

Expected: script missing.

- [ ] **Step 3: Implement the CLI**

Use the same repo-root import bootstrap as `scripts/report_memecoin_x_capture.py`. Do not include raw card HTML or secrets in output.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_memecoin_x_sources_report.py -q
git add scripts/report_memecoin_x_sources.py tests/test_memecoin_x_sources_report.py
git commit -m "feat(memecoin-x): report source graph reads"
```

---

### Task 5: Phase-3 adversarial gate

**Files:**
- Modify only if a test proves a defect: Phase-3 files above.

- [ ] **Step 1: Run the full focused suite**

```bash
python3 -m pytest \
  tests/test_memecoin_x_store.py \
  tests/test_memecoin_x_normalize.py \
  tests/test_memecoin_x_correlator.py \
  tests/test_memecoin_x_fomo_adapter.py \
  tests/test_memecoin_x_correlator_service.py \
  tests/test_memecoin_x_read_models.py \
  tests/test_memecoin_x_sources_report.py -q
python3 scripts/validate_schema_refs.py
git diff --check
```

- [ ] **Step 2: Run a disposable-DB reconciliation**

Copy the X DB to a temporary path; never use the live X DB for this check. Read the main checkout's `sniper_tracker.db` in `mode=ro`, run the correlator against the copy, and verify with SQL that every eligible link has strict positive lead `<=21600`, exact network/mint equality, and both clock domains `observed_real_utc`.

- [ ] **Step 3: Verify Robinhood explicitly**

Report link/source counts for `network_id='4663'` separately from `8453`, `1`, and `56`. Zero links is acceptable when no X post matched; pooling is not.

- [ ] **Step 4: Commit only proven fixes**

Use one commit per defect. Do not add scoring, alerts, UI, LLM, or execution behavior in Phase 3.

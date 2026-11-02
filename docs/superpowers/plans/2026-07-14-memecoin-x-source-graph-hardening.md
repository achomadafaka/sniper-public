# Memecoin X Source Graph Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the proven Phase-2 capture and Phase-3 correlation defects before adding source-graph read models.

**Architecture:** Keep the selected read-only userscript plus loopback receiver adapter. Make delivery acknowledgement, capture-run audit, exact observation evidence, correlation convergence, and the Fomo-v2 boundary explicit and idempotent. Keep the entire lane display-only and isolated from funded execution.

**Tech Stack:** Python 3, SQLite, Flask, BeautifulSoup, pytest, vanilla JavaScript userscript, Node's built-in test runner.

## Global Constraints

- X capture never posts, likes, replies, reposts, messages, follows, or unfollows.
- The receiver binds to `127.0.0.1` and writes only `memecoin_x_*` tables.
- Forward evidence uses only `observed_real_utc`; X `posted_at` is metadata.
- Robinhood is network `4663`, never a pooled generic-EVM bucket.
- The Fomo source is `memecoin_feed_observations_v2`, filtered to version 2, buy, real-approved, healthy-clock rows.
- Every production behavior change starts with a failing regression test.
- No notification, LLM, position, execution, funded-runtime, or production-DB behavior is enabled.

---

### Task 1: Gate incomplete evidence and promote only later complete observations

**Files:**
- Modify: `sniper/analysis/memecoin_x/normalize.py`
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `tests/test_memecoin_x_normalize.py`
- Modify: `tests/test_memecoin_x_store.py`

**Interfaces:**
- Consumes: `normalize_card(card: dict) -> dict`, `persist_card(...) -> dict`
- Produces: incomplete observations with retained mentions and `eligible=False`; later complete same-parser observations promote eligibility and move `source_observation_id` forward.

- [x] **Step 1: Add normalization regression**

```python
def test_incomplete_robinhood_card_retains_ineligible_exact_mention():
    card = exact_robinhood_card(text_complete=False)
    mention = normalize_card(card)["mentions"][0]
    assert mention["network_id"] == "4663"
    assert mention["eligible"] is False
```

- [x] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_x_normalize.py::test_incomplete_robinhood_card_retains_ineligible_exact_mention -q`

Expected: failure because the current exact mention remains eligible.

- [x] **Step 3: Add store promotion and no-demotion regressions**

Persist incomplete, later complete, then later incomplete deliveries of the same exact mention. Assert the mention is initially ineligible, promotes at the complete delivery's observation ID/time, and is unchanged by the final incomplete delivery.

- [x] **Step 4: Run RED, implement the smallest normalization/store changes, then run GREEN**

Run: `python3 -m pytest tests/test_memecoin_x_normalize.py tests/test_memecoin_x_store.py -q`

Expected after implementation: pass.

- [x] **Step 5: Commit**

```bash
git add sniper/analysis/memecoin_x/normalize.py sniper/persistence/db_memecoin_x.py tests/test_memecoin_x_normalize.py tests/test_memecoin_x_store.py
git commit -m "fix(memecoin-x): quarantine incomplete mention evidence"
```

---

### Task 2: Make capture runs auditable and diagnostic

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `sniper/services/memecoin_x_receiver.py`
- Modify: `tests/test_memecoin_x_store.py`
- Modify: `tests/test_memecoin_x_receiver.py`
- Modify: `index/schema_registry.json`

**Interfaces:**
- Produces: `start_capture_run(conn, run) -> bool`
- Produces: `finish_capture_run(conn, run_id, **fields) -> None`
- Produces: `record_capture_diagnostic(conn, *, capture_run_id, observed_at, clock_domain, diagnostic_code, parser_version, card_index=None, canonical_post_id=None, details=None) -> None`
- Produces: `latest_capture_sentinel(conn) -> str | None`
- Produces: idempotent `process_batch(..., pass_meta=None) -> dict`

- [x] **Step 1: Add failing schema/lifecycle tests**

Cover the diagnostics table and allowlist, a `running` row created before card persistence, error finalization on a simulated parser/store exception, retry of a completed run ID returning the committed result, and latest-sentinel selection from completed healthy runs only.

- [x] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_receiver.py -q`

Expected: failures for the missing diagnostics table and lifecycle functions.

- [x] **Step 3: Implement lifecycle and diagnostics**

Create `memecoin_x_capture_diagnostics` with capture run, corrected observation metadata, bounded diagnostic code, card index, canonical post ID, and sorted JSON details. Insert `status='running'` before any card commit. Finish the same row as `ok`, `no_new`, `coverage_gap`, `wrong_feed`, `logged_out`, `challenge`, `rate_limited`, `clock_unhealthy`, `parser_error`, or `error`.

- [x] **Step 4: Make receiver input bounded**

Set Flask `MAX_CONTENT_LENGTH`, reject more than 50 cards, reject non-string cards, cap each card at 256 KiB, validate client run IDs, and store parser diagnostics rather than dropping them.

- [x] **Step 5: Run GREEN and schema validation**

Run: `python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_receiver.py -q`

Run: `python3 scripts/validate_schema_refs.py --warn-unknown-tables`

Expected: focused tests pass; no schema violations.

- [x] **Step 6: Commit**

```bash
git add sniper/persistence/db_memecoin_x.py sniper/services/memecoin_x_receiver.py tests/test_memecoin_x_store.py tests/test_memecoin_x_receiver.py index/schema_registry.json
git commit -m "fix(memecoin-x): audit capture lifecycle and diagnostics"
```

---

### Task 3: Acknowledge userscript delivery and report bounded coverage

**Files:**
- Modify: `scripts/memecoin_x_capture.user.js`
- Create: `tests/js/test_memecoin_x_capture_user.js`
- Modify: `tests/test_memecoin_x_receiver.py`

**Interfaces:**
- Userscript POST body: `{run_id, cards, status, prior_sentinel, newest_sentinel, sentinel_found, diagnostics, cards_scanned, stop_reason}`
- Receiver response: committed run summary with the same `run_id`.

- [x] **Step 1: Add failing Node tests**

Use Node's built-in `node:test` plus mocked document/storage/`GM_xmlhttpRequest`. Prove that a network error and non-2xx response retain the pending batch, a 2xx response marks fingerprints delivered, an empty pass still sends a heartbeat, wrong-feed state sends no cards, and a retry reuses the same run ID.

- [x] **Step 2: Run RED**

Run: `node --test tests/js/test_memecoin_x_capture_user.js`

Expected: failure because current userscript marks IDs seen before acknowledgement and exports no testable core.

- [x] **Step 3: Implement a testable userscript core**

Keep one in-memory pending batch, one in-tab lease, bounded fingerprints, stable run IDs, 2xx-only acknowledgement, 120-240 second jitter, at most 50 cards and three fixed-height scrolls, Following-tab/auth/challenge checks, and a heartbeat for every pass. Deduplicate by canonical ID plus a deterministic visible-card fingerprint so a later complete/edit delivery is observable.

- [x] **Step 4: Run GREEN and receiver protocol tests**

Run: `node --test tests/js/test_memecoin_x_capture_user.js`

Run: `python3 -m pytest tests/test_memecoin_x_receiver.py -q`

Expected: both pass.

- [x] **Step 5: Commit**

```bash
git add scripts/memecoin_x_capture.user.js tests/js/test_memecoin_x_capture_user.js tests/test_memecoin_x_receiver.py
git commit -m "fix(memecoin-x): acknowledge browser capture delivery"
```

---

### Task 4: Complete quote and reply relationship parsing

**Files:**
- Modify: `sniper/analysis/memecoin_x/dom_parser.py`
- Add: `tests/fixtures/memecoin_x/dom/reply.html`
- Add: `tests/fixtures/memecoin_x/dom/quote_with_status.html`
- Modify: `tests/test_memecoin_x_dom_parser.py`

**Interfaces:**
- Produces: a referenced `quote` or `reply` card only when the referenced canonical status ID is present in the captured DOM.
- Preserves: outer authored text as a separate direct card; no nested-text flattening.

- [x] **Step 1: Add failing quote/reply fixture tests**

Assert exact actor, source author, referenced post ID, relationship, text boundary, and completeness. Retain diagnostic-only behavior when a real visible quote/reply omits the referenced status ID.

- [x] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_x_dom_parser.py -q`

Expected: reply/visible-reference cases fail.

- [x] **Step 3: Implement referenced identity extraction**

Restrict `Replying to` and `Show more` checks to the outer card region. Extract a referenced card only from a nested status permalink. Emit allowlisted `missing_referenced_post_id` or `incomplete_text` diagnostics otherwise.

- [x] **Step 4: Run GREEN and remove fixture whitespace**

Run: `python3 -m pytest tests/test_memecoin_x_dom_fixture.py tests/test_memecoin_x_dom_parser.py -q`

Run: `git diff --check`

Expected: tests pass and no whitespace errors.

- [x] **Step 5: Commit**

```bash
git add sniper/analysis/memecoin_x/dom_parser.py tests/fixtures/memecoin_x/dom tests/test_memecoin_x_dom_parser.py
git commit -m "fix(memecoin-x): parse visible quote and reply relationships"
```

---

### Task 5: Make correlation convergent and add the strict Fomo-v2 adapter

**Files:**
- Modify: `sniper/analysis/memecoin_x/correlator.py`
- Create: `sniper/analysis/memecoin_x/fomo_adapter.py`
- Modify: `tests/test_memecoin_x_correlator.py`
- Create: `tests/test_memecoin_x_fomo_adapter.py`

**Interfaces:**
- Produces: `fetch_eligible_fomo_buys(conn, *, after_id=0, limit=1000) -> list[dict]`
- Tightens: `build_forward_links(...)` validates required eligible-buy fields.
- Convergence rule: an existing eligible coincidence link may upgrade to amplified when a qualifying relationship with `x_observed_at <= relationship.observed_at < fomo_observed_at` arrives later; it never downgrades.

- [x] **Step 1: Add failing convergence tests**

Create coincidence, insert the buyer's qualifying pre-buy repost, rerun, and assert one row upgraded to amplified. Add non-qualifying direct, post-buy, unhealthy-clock, and pre-mention relationship cases.

- [x] **Step 2: Add failing adapter tests**

Seed `memecoin_feed_observations_v2` with version-1, sell, unapproved, unhealthy, missing identity, Solana, Base, and Robinhood `4663` rows. Assert only exact version-2 real-approved buys with healthy clocks and complete identities are returned, preserving `4663` unchanged.

- [x] **Step 3: Run RED**

Run: `python3 -m pytest tests/test_memecoin_x_correlator.py tests/test_memecoin_x_fomo_adapter.py -q`

Expected: failures for frozen coincidence rows, missing validation, and missing adapter.

- [x] **Step 4: Implement adapter and monotonic UPSERT**

Filter in SQL and validate again at the correlator boundary. Restrict amplification relationships to `repost`, `quote`, and `reply`. Update only coincidence-to-amplified fields for the stable logical key; never change the authoritative X or Fomo observation timestamps.

- [x] **Step 5: Run GREEN**

Run: `python3 -m pytest tests/test_memecoin_x_correlator.py tests/test_memecoin_x_fomo_adapter.py -q`

Expected: pass.

- [x] **Step 6: Commit**

```bash
git add sniper/analysis/memecoin_x/correlator.py sniper/analysis/memecoin_x/fomo_adapter.py tests/test_memecoin_x_correlator.py tests/test_memecoin_x_fomo_adapter.py
git commit -m "fix(memecoin-x): enforce convergent v2 forward links"
```

---

### Task 6: Add the dry-run report and close the hardening gate

**Files:**
- Create: `scripts/report_memecoin_x_capture.py`
- Create: `tests/test_memecoin_x_capture_report.py`
- Modify: `docs/superpowers/specs/2026-07-13-memecoin-x-source-graph-design.md`
- Modify: `docs/superpowers/plans/2026-07-13-memecoin-x-source-graph-phase2-browser-capture.md`

**Interfaces:**
- Produces: read-only JSON keys `wall_hours`, `runs`, `status_counts`, `cards_seen`, `observations`, `relationships`, `parse_failure_rate`, `healthy_clock_rate`, `sentinel_found_rate`, `coverage_gaps`, `coverage_gap_rate`, `max_gap_seconds`, `diagnostic_counts`, `secret_scan_hits`, `forbidden_source_hits`, and `acceptance`.

- [x] **Step 1: Add failing report tests**

Cover accepted synthetic 24-hour data and rejection for less than 24 hours, missing heartbeat over 15 minutes, parser failure over 1%, unhealthy clock, unresolved wrong-feed/challenge state, secret hit, or forbidden import hit.

- [x] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_memecoin_x_capture_report.py -q`

Expected: import failure because the report does not exist.

- [x] **Step 3: Implement read-only reporting and documentation alignment**

Open SQLite in read-only URI mode, compute every denominator from capture runs, and emit explicit blocking reasons. Mark the prior Playwright Phase-2 plan as superseded for adapter mechanics while retaining its evidence and acceptance gates.

- [x] **Step 4: Run full verification**

Run: `python3 -m pytest tests/test_memecoin_x*.py -q`

Run: `node --test tests/js/test_memecoin_x_capture_user.js`

Run: `python3 scripts/validate_schema_refs.py --warn-unknown-tables`

Run: `git diff --check`

Expected: all tests pass, no schema violations, no whitespace errors.

- [x] **Step 5: Commit**

```bash
git add scripts/report_memecoin_x_capture.py tests/test_memecoin_x_capture_report.py docs/superpowers/specs/2026-07-13-memecoin-x-source-graph-design.md docs/superpowers/plans/2026-07-13-memecoin-x-source-graph-phase2-browser-capture.md docs/superpowers/plans/2026-07-14-memecoin-x-source-graph-hardening.md
git commit -m "docs(memecoin-x): close source-graph hardening contract"
```

The automated report can be implemented and fixture-verified locally. Phase 2 remains operationally incomplete until a real burner session produces a passing 24-hour report and the manual 25-observation audit has no unresolved identity or timestamp mismatch.

## Review closure (2026-07-14)

- [x] All-unparseable batches end as `parser_error` and cannot acknowledge fingerprints.
- [x] `clock_unhealthy` keeps fingerprints pending for a fresh healthy-clock run.
- [x] Client status/diagnostics are coherent before the DB is opened.
- [x] A changed sentinel card is delivered before the bounded pass stops.
- [x] Acceptance includes requested-window leading/trailing gaps, stale runs, and unresolved
  operator state.
- [x] Fomo identities are trimmed and addresses are validated against the explicit network.
- [x] Fomo buyer identity/Tier-1 facts are snapshotted before correlation and reused on replay.
- [x] Visible scanned-card count and bounded-pass stop reason survive delivered-card filtering.
- [x] Active `running` rows retain a receiver lease; only stale runs are recoverable.

# Funded Memecoin Forensics Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only funded-history report and two entry shadows with zero funded-decision effect.

**Architecture:** The report stays in one standard-library script. It reads the funded ledger through `get_readonly_connection()`. The two shadows stay in one service module. The real runner appends the immutable result to its existing trace event list before trace finalization.

**Tech Stack:** Python standard library, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-memecoin-funded-forensics-shadow-design.md`

## Global Constraints

- Use only funded `execution_mode='real'` rows for funded history and trader priors.
- Do not write from the report.
- Do not modify `classification_json`.
- Do not branch a funded reservation, quote, signing step, transaction, or exit on a shadow result.
- The preregistered report cutoff is `2026-08-23T00:00:00+00:00`.
- Use `SUM(realized_pnl) / SUM(entry_notional)` for every realized return.

---

### Task 1: Update the accepted contract

**Files:**
- Modify: `docs/superpowers/specs/2026-08-31-memecoin-funded-forensics-shadow-design.md`

**Interfaces:**
- Produces: the exact chart, prior, return, range-label, and snapshot constraints for Tasks 2-4.

- [ ] **Step 1: Add the accepted constraints**

Add the `2026-08-31T15:31:24Z` snapshot time and the 36 automatic-source count.

Add these prior predicates: real mode, closed state, finite PnL, finite positive notional, and current-position exclusion.

Add these chart predicates: finite positive entry price and `observed_at > entry time`.

Add the return formula, the best-two rank order, and the exploratory range label.

- [ ] **Step 2: Validate the document**

Run: `git diff --check && python3 scripts/validate_schema_refs.py --warn-unknown-tables`

Expected: exit code 0 and no schema violation from the new specification.

### Task 2: Build and test the pure shadows

**Files:**
- Create: `sniper/services/memecoin_entry_shadow.py`
- Create: `tests/test_memecoin_entry_shadow.py`

**Interfaces:**
- Produces: `evaluate_entry_shadows(conn, *, attempt_id: str, current_position_id: int | None) -> dict`
- Consumes: `memecoin_entry_attempts` and `memecoin_positions` from `db_memecoin.ensure_memecoin_tables()`.

- [ ] **Step 1: Write the failing shadow tests**

Add a test that stores a frozen `coin_band='small'` attempt and expects `small_band_v1.state == 'would_block'`.

Add a test that stores five eligible prior positions with a negative aggregate return and expects `prior_5_negative_v1.state == 'would_block'`.

Add one prior row for each forbidden case: paper, open, another network, later close, non-finite PnL, zero notional, and the current position ID.

Expect `prior_closed_n == 5` and `prior_roi_pct == -10.0` for `-50 / 500`.

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest tests/test_memecoin_entry_shadow.py -q`

Expected: FAIL because `sniper.services.memecoin_entry_shadow` does not exist.

- [ ] **Step 3: Add the minimal shadow module**

Implement `evaluate_entry_shadows()`.

Load the stored attempt by `attempt_id`.

Return JSON-safe `version`, `small_band_v1`, and `prior_5_negative_v1` values.

Filter priors in SQL by source, network, `execution_mode='real'`, `is_open=0`, close clock, and current position ID.

Filter numeric values with `math.isfinite()` in Python before aggregation.

Return `unknown` for invalid attempt fields or a read error.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m pytest tests/test_memecoin_entry_shadow.py -q`

Expected: PASS.

### Task 3: Add the trace event without a funded effect

**Files:**
- Modify: `sniper/services/memecoin_real_runner.py:4860-4890`
- Modify: `tests/test_memecoin_entry_identity.py`

**Interfaces:**
- Consumes: `evaluate_entry_shadows()` from Task 2 and `EntryAttemptTrace.mark_substage()`.
- Produces: one `funded_entry_shadow_v1` event before each trace finalization.

- [ ] **Step 1: Write the failing runner tests**

Add a traced real-hook test that reads `trace_json.substage_events` and expects one `funded_entry_shadow_v1` event.

Add a test that makes `evaluate_entry_shadows()` raise during a real-hook result.

Expect the original result status to stay `unknown` and trace finalization to complete.

- [ ] **Step 2: Run the focused runner tests and verify RED**

Run: `python3 -m pytest tests/test_memecoin_entry_identity.py -q`

Expected: FAIL because no shadow event exists and evaluator failure is not isolated.

- [ ] **Step 3: Add one best-effort trace helper**

Add `_record_entry_shadows_best_effort(trace, result)` beside `_finish_trace_for_result()`.

Call it before `trace.finish()` in both normal and exception trace paths.

The helper catches evaluator and trace-event errors. It records `unknown` shadow values after an evaluator error. It returns without changing `result`.

- [ ] **Step 4: Run the focused runner tests and verify GREEN**

Run: `python3 -m pytest tests/test_memecoin_entry_identity.py tests/test_memecoin_entry_shadow.py -q`

Expected: PASS.

### Task 4: Build and test the read-only HTML report

**Files:**
- Create: `scripts/render_memecoin_funded_forensics.py`
- Create: `tests/test_memecoin_funded_forensics_report.py`

**Interfaces:**
- Produces: `build_report(conn, since: str) -> dict`, `render_html(report: dict) -> str`, and `main(argv=None) -> int`.
- Consumes: funded position rows, frozen attempt classification, route-series rows, and recorded trace events.

- [ ] **Step 1: Write the failing report tests**

Create closed funded rows for three coin groups. Use equal PnL for two groups and different `(network_id, mint)` values.

Assert the report return uses `SUM(realized_pnl) / SUM(entry_notional)` and that tie order selects the lexicographically first two coin groups.

Create a route row before entry, a route row after entry, and a second position with a non-positive entry price.

Assert only the valid post-entry route becomes an eligible chart.

Call `main()` with a non-default aware `--since` and assert the HTML has `exploratory`.

- [ ] **Step 2: Run the report tests and verify RED**

Run: `python3 -m pytest tests/test_memecoin_funded_forensics_report.py -q`

Expected: FAIL because the report script does not exist.

- [ ] **Step 3: Add the minimal report script**

Use `get_readonly_connection()` in `main()`.

Require `--since` and `--output`. Allow `--db` for a test database.

Label the default cutoff `preregistered` and every other aware range `exploratory`.

Use one SQL read for closed funded positions. Use `html.escape()` and inline SVG. Do not read `memecoin_position_marks`.

Use aware timestamp parsing. Include a route point only when it is finite, positive, and after the selected entry clock.

Rank groups by `(-realized_pnl, network_id, mint)`. Remove the first two ranks in the displayed counterfactual.

- [ ] **Step 4: Run the report tests and verify GREEN**

Run: `python3 -m pytest tests/test_memecoin_funded_forensics_report.py -q`

Expected: PASS.

### Task 5: Verify and commit the one logical change

**Files:**
- Modify: the files in Tasks 1-4.

- [ ] **Step 1: Run focused verification**

Run: `python3 -m pytest tests/test_memecoin_entry_shadow.py tests/test_memecoin_entry_identity.py tests/test_memecoin_funded_forensics_report.py -q`

Expected: PASS.

- [ ] **Step 2: Run static verification**

Run: `git diff --check && python3 scripts/validate_schema_refs.py --warn-unknown-tables`

Expected: exit code 0 and no schema violation from this change.

- [ ] **Step 3: Inspect the scope**

Run: `git diff --stat && git status --short`

Expected: only the spec, plan, shadows, runner, report, and focused tests changed.

- [ ] **Step 4: Commit**

Run: `git add docs/superpowers/specs/2026-08-31-memecoin-funded-forensics-shadow-design.md docs/superpowers/plans/2026-08-31-memecoin-funded-forensics-shadow.md sniper/services/memecoin_entry_shadow.py sniper/services/memecoin_real_runner.py scripts/render_memecoin_funded_forensics.py tests/test_memecoin_entry_shadow.py tests/test_memecoin_entry_identity.py tests/test_memecoin_funded_forensics_report.py && git commit -m "feat: add funded forensic shadows"`

Expected: one local feature commit with no deploy, restart, or funded policy change.

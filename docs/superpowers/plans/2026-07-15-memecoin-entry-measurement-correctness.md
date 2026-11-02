# Memecoin Entry Measurement Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make funded-entry timestamps, eligibility coverage, receipt latency, and refusal reasons exact without changing any funded decision.

**Architecture:** Reuse the real hook's reconciled decision-start timestamp for the event decision stamp, and retain the corrected-clock helper as a direct-call fallback. Extend the read-only reporter with exact point-in-time global-feed observations and clock-validated derived receipt latency; follow-activity remains a separate recovery source and reports unavailable eligibility fields as unknown.

**Tech Stack:** Python 3, SQLite, pytest, existing memecoin trace and corrected-clock utilities.

## Global Constraints

- Robinhood is network `4663`, never generic EVM.
- Coin identity remains `(network_id, mint)`; no chain is guessed from an EVM address.
- Do not change reservation, safety, kill-switch, duplicate-submit, sizing, exit, or adaptive-policy behavior.
- Do not add network requests or mutate historical/live rows.
- Claimed upstream timestamps remain separate from locally observed timestamps.
- Derived latency is valid only for timezone-aware timestamps in `observed_real_utc` with a nonnegative delta.
- `global_feed` and `follow_activity` remain separate; follow activity is recovery coverage.

---

### Task 1: Reconcile real-decision timestamps and namespace mcap refusal

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Test: `tests/test_memecoin_real_decisions.py`
- Test: `tests/test_memecoin_real_mcap_gate.py`

**Interfaces:**
- Consumes: `sniper.util.clock.real_utc_now_iso()`, the hook's injected `now_fn()`.
- Produces: `set_event_real_decision(..., now_iso=None)` with corrected-clock fallback; `_record_real_decision(..., decision_at)`; terminal reason `mcap_unresolved`.

- [ ] **Step 1: Write failing timestamp tests**

```python
def test_set_event_real_decision_defaults_to_corrected_real_utc(monkeypatch):
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: "2026-07-15T17:00:00+00:00")
    c = _conn()
    m.insert_event(c, _ev())
    m.set_event_real_decision(c, "alice-1", "not_approved")
    assert c.execute(
        "SELECT real_decision_at FROM memecoin_events WHERE fomo_event_id='alice-1'"
    ).fetchone()[0] == "2026-07-15T17:00:00+00:00"


def test_hook_reuses_decision_start_for_real_decision_timestamp():
    c = _conn()
    m.insert_event(c, _ev(fid="e-clock", uid="nobody", mint="So1mint"))
    hook = R.make_real_hook(
        None, {"solana": _sol_ctx()}, now_fn=lambda: "2026-07-15T17:00:01+00:00",
        trade_size=10.0, max_age_s=120,
    )
    assert hook(c, _EvObj("e-clock", uid="nobody"))["status"] == "not_approved"
    assert c.execute(
        "SELECT real_decision_at FROM memecoin_events WHERE fomo_event_id='e-clock'"
    ).fetchone()[0] == "2026-07-15T17:00:01+00:00"
```

- [ ] **Step 2: Run timestamp tests and verify expected failures**

Run: `python3 -m pytest tests/test_memecoin_real_decisions.py -q`

Expected: the default uses host wall time, and the hook does not pass its decision timestamp.

- [ ] **Step 3: Implement one-clock propagation**

```python
from sniper.util.clock import real_utc_now_iso


def set_event_real_decision(conn, fomo_event_id, decision, reason=None, now_iso=None):
    if not fomo_event_id:
        return
    now_iso = now_iso or real_utc_now_iso()
    # existing UPDATE remains unchanged
```

Change the hook closure to compute `decision_at` once, pass it into `_hook_raw`, and call:

```python
_record_real_decision(conn, ev, res, decision_at=decision_at)
```

Then pass `now_iso=decision_at` to `set_event_real_decision`. Sell processing remains unchanged.

- [ ] **Step 4: Run timestamp tests and verify they pass**

Run: `python3 -m pytest tests/test_memecoin_real_decisions.py -q`

Expected: all tests pass.

- [ ] **Step 5: Write the failing deterministic-reason test**

```python
assert RR.mcap_entry_decision(None, None, C) == (False, None, "mcap_unresolved")
```

Also assert the hook's terminal trace reason is `mcap_unresolved` for a truly unpriceable token.

- [ ] **Step 6: Run the reason tests and verify the old `unresolved` result fails**

Run: `python3 -m pytest tests/test_memecoin_real_mcap_gate.py tests/test_memecoin_entry_identity.py -q`

Expected: only the new reason assertions fail.

- [ ] **Step 7: Return `mcap_unresolved` without changing the allow/deny result**

```python
if not mc:
    return False, None, "mcap_unresolved"
```

- [ ] **Step 8: Run focused decision and mcap tests**

Run: `python3 -m pytest tests/test_memecoin_real_decisions.py tests/test_memecoin_real_mcap_gate.py tests/test_memecoin_entry_identity.py tests/test_memecoin_alerts.py -q`

Expected: all tests pass; the missing-mcap signal remains conservatively refused.

- [ ] **Step 9: Commit the decision telemetry correction**

```bash
git add sniper/persistence/db_memecoin.py sniper/services/memecoin_real_runner.py \
  tests/test_memecoin_real_decisions.py tests/test_memecoin_real_mcap_gate.py \
  tests/test_memecoin_entry_identity.py tests/test_memecoin_alerts.py
git commit -m "fix(memecoin): reconcile entry decision telemetry"
```

### Task 2: Report exact eligibility and trace coverage

**Files:**
- Modify: `scripts/report_memecoin_entry_baseline.py`
- Modify: `tests/test_memecoin_entry_report.py`

**Interfaces:**
- Consumes: `memecoin_feed_observations_v2.upstream_event_id`, `normalized_side`, `network_id`, `mint`, `observed_at`, `was_curated`, `was_real_approved`, and `clock_domain`.
- Produces: per-chain/global-feed `curated_buys`, `eligible_curated_buys`, `eligible_buys_with_exact_trace`, and `eligible_trace_coverage_pct`.

- [ ] **Step 1: Add a failing exact-observation coverage test**

Create `memecoin_feed_observations_v2` in the report fixture. Insert two curated real-approved buys and one non-approved curated buy on Solana. Insert one attempt whose `origin_event_id` exactly matches one eligible observation. Assert:

```python
assert sol["funnel"]["signals_observed"] == 3
assert sol["funnel"]["curated_buys"] == 3
assert sol["funnel"]["eligible_curated_buys"] == 2
assert sol["funnel"]["eligible_buys_with_exact_trace"] == 1
assert sol["funnel"]["eligible_trace_coverage_pct"] == 50.0
```

Assert a follow-activity row keeps those point-in-time fields `None` and names `point_in_time_eligibility_unavailable_for_follow_activity`.

- [ ] **Step 2: Run the coverage test and verify it fails**

Run: `python3 -m pytest tests/test_memecoin_entry_report.py -q`

Expected: the reporter does not read v2 observations and eligibility remains `None`.

- [ ] **Step 3: Read exact observations and classify global feed**

Add `_read_observations(conn, tables, since)` with a required-column guard and `observed_at>=?` filtering. For `global_feed`, count buy observations and exact `upstream_event_id` membership in attempt `origin_event_id`; do not use mint/time proximity. For `follow_activity`, continue counting its event rows and retain `None` eligibility because that source has no point-in-time observation row.

- [ ] **Step 4: Run the report tests and verify they pass**

Run: `python3 -m pytest tests/test_memecoin_entry_report.py -q`

Expected: all tests pass.

### Task 3: Report same-clock receipt latency

**Files:**
- Modify: `scripts/report_memecoin_entry_baseline.py`
- Modify: `tests/test_memecoin_entry_report.py`

**Interfaces:**
- Consumes: attempt `observed_at`, `clock_domain`, `decision_started_at`, `transaction_submitted_at`, and `completed_at`.
- Produces: `receipt_latency_ms.receipt_to_decision`, `receipt_to_submission`, and `receipt_to_terminal`, each with p50/p95 and explicit exclusion reasons.

- [ ] **Step 1: Add failing valid/invalid clock tests**

Insert one `observed_real_utc` attempt with aware timestamps and assert 250 ms receipt-to-decision, 1,000 ms receipt-to-submission, and 2,000 ms receipt-to-terminal. Insert mixed-domain, naive, negative-delta, and unreached-stage rows. Assert no invalid row enters a percentile and the exclusion counters distinguish `invalid_clock_domain`, `naive_timestamp`, `negative_delta`, and `stage_not_reached`.

- [ ] **Step 2: Run the latency tests and verify they fail**

Run: `python3 -m pytest tests/test_memecoin_entry_report.py -q`

Expected: `receipt_latency_ms` is absent.

- [ ] **Step 3: Implement timezone-aware derived latency**

Parse ISO-8601 timestamps with `datetime.fromisoformat(value.replace("Z", "+00:00"))`. Reject missing/naive/invalid timestamps, any clock domain other than `observed_real_utc`, and negative deltas. Return `NULL` by exclusion rather than synthetic zero; aggregate accepted values with the existing `_distribution()` helper.

- [ ] **Step 4: Expose receipt latency in JSON and the table**

Add receipt-to-decision p50/p95 and eligible trace coverage to `_print_table`. Bump `schema_version` to `2` because the output contract gains exact fields.

- [ ] **Step 5: Run focused report tests**

Run: `python3 -m pytest tests/test_memecoin_entry_report.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the reporter upgrade**

```bash
git add scripts/report_memecoin_entry_baseline.py tests/test_memecoin_entry_report.py
git commit -m "feat(memecoin): report exact entry coverage latency"
```

### Task 4: Validate and close the branch

**Files:**
- Modify: `docs/superpowers/specs/2026-07-15-memecoin-entry-speed-coverage-design.md`

**Interfaces:**
- Consumes: verified test and live read-only report output.
- Produces: corrected implementation-status note; no live deployment claim.

- [ ] **Step 1: Run required validation**

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/test_memecoin*.py -q
python3 -m compileall -q sniper scripts
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 2: Run the reporter read-only against the live database**

```bash
python3 scripts/report_memecoin_entry_baseline.py \
  --db /home/bigpapi/Desktop/Sniper/sniper_tracker.db \
  --since 2026-07-15T00:00:00+00:00 --json
```

Expected: JSON schema v2; exact global-feed eligibility; follow-activity kept separate; no write occurs. Current active-chain latency may remain insufficient sample.

- [ ] **Step 3: Update the design status from verified evidence**

Replace the stale statement that point-in-time eligibility is unavailable with the exact v2 observation contract. State that no historical rows were backfilled and no funded before/after claim exists until the pinned canary produces submitted entries.

- [ ] **Step 4: Run the Knowledge Sync Gate audit**

Review `docs/superpowers/specs/2026-05-15-branch-close-knowledge-sync-design.md`. This is a measurement bugfix and reporter extension; record `Knowledge delta: none. Reason: bugfix-only telemetry correction` in the branch report unless the final diff changes doctrine.

- [ ] **Step 5: Adversarially review the complete diff and commit documentation**

```bash
git diff origin/main...HEAD --check
git status --short
git add docs/superpowers/specs/2026-07-15-memecoin-entry-speed-coverage-design.md \
  docs/superpowers/plans/2026-07-15-memecoin-entry-measurement-correctness.md
git commit -m "docs(memecoin): close measurement correctness batch"
```

Do not merge, deploy, restart the runner, alter approvals, or mutate the live database.

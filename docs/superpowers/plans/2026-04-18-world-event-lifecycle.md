# ⚠️ SUPERSEDED / DO NOT IMPLEMENT

**This document contains schema assumptions that were proven wrong.**
Replaced by the world-events lifecycle plan plus the `intel_tool`-based Step 2
implementation. See: commit `25f2f81` and the current VPS plan doc.

Specifically: this plan assumed `world_events` was a SQLite table with columns
like `lifecycle_status`, `last_reviewed_at`, `superseded_by`, etc. — it is a
Supabase table with a pre-existing `status` enum (`active|escalating|developing|
resolved`) and `last_updated`. See `scripts/brain_health.py:253` for the real
A3 check shape.

Kept for audit trail only. The `<!-- schema:validate-columns-for=... -->`
validator feature added in `d5d383e` would catch this class of error going
forward.

---

# World Event Lifecycle — Retire/Refresh Logic + A3 Enforcement

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `world_events` a first-class lifecycle-managed entity so A3 (brain health: stale world events) becomes a real enforcement layer. Analysts refresh live events on each run; they explicitly retire superseded ones. A3 flags live events whose `last_reviewed_at` is older than a TTL.

**Current state:** 71 stale events (40 `fin:*`, 31 other), all 72–120h old. Root cause: analysts emit new events but never touch/retire existing ones. A3 correctly fires on this — don't weaken A3.

**Non-goal:** Do not change A3 to "only flag events with no downstream references." Superseded events can still carry historical impact/idea links; downstream refs are not a freshness proxy.

**Tech Stack:** Python 3, SQLite or Supabase Postgres (verify first — `world_events` location unclear from local repo), pytest.

---

## Editorial rules (fixed — do not renegotiate)

These are owner decisions. Implement, don't re-litigate.

- **`lifecycle_status` is an enum**, not a free-text field. Values: `live | resolved | superseded | monitoring`.
  - `live`: analyst still considers this an open, relevant event requiring periodic review.
  - `resolved`: outcome is known; kept for historical reference, no longer needs review.
  - `superseded`: replaced by a newer/better event; `superseded_by` points at the replacement (nullable, but should usually be set).
  - `monitoring`: long-horizon event (e.g. "US 2026 elections by Jun"), still technically live but cadence of review is slow. Distinct from `live` so A3 can apply a longer TTL.
- **Refresh = timestamp touch, not new row.** Each analyst run sets `last_reviewed_at = now()` on every event it still considers relevant. No content change required.
- **Retire is explicit.** When an analyst swaps in a replacement event, it sets `lifecycle_status='superseded'` and `superseded_by = <new_event_id>` on the old row. Null `superseded_by` is allowed (event simply went stale) but discouraged.
- **A3 TTL is status-aware.**
  - `live`: flag if `last_reviewed_at < now() - 48h`.
  - `monitoring`: flag if `last_reviewed_at < now() - 14d`.
  - `resolved` / `superseded`: never flagged.
- **Per-domain warning quotas.** A3 should surface the worst-offender domain in the warning message (e.g. "fin:* cluster: 32 stale / world: 8"). Makes the editorial gap legible.

---

## Task 0: Verify table location and current schema

- [ ] **Step 0.1: Find `world_events`**

```bash
# Is it SQLite?
python3 scripts/brain_index.py deps --table world_events
sqlite3 sniper_tracker.db ".schema world_events" 2>&1

# Or Supabase?
# (run from VPS; PAT already in .env context — see CLAUDE.md Supabase Advisor block)
```

Document the answer at the top of this plan before proceeding. All schema changes below assume the authoritative location.

- [ ] **Step 0.2: Inventory current writers/readers**

Grep for `world_events` across `analyze_users.py`, `agent_pipeline.py`, `scripts/world_analyst.py`, `scripts/finance_analyst.py`, any `routes/*.py`. List every INSERT/UPDATE/SELECT site. Paste the list into this file before editing.

---

## Task 1: Schema migration — add lifecycle columns

**Files:**
- Migration script: `scripts/migrate_world_event_lifecycle.py` (new)
- Schema source of truth: whichever file is authoritative (likely `db.py` init_db block, or Supabase migration file — depends on Task 0.1)

Columns to add to `world_events`:

| Column | Type | Default | Notes |
|--------|------|---------|-------|
| `lifecycle_status` | TEXT / enum | `'live'` | CHECK constraint to enum values |
| `last_reviewed_at` | TIMESTAMP (UTC) | `now()` on insert | Updated on every analyst touch |
| `superseded_by` | TEXT / FK to `world_events.event_id` | NULL | Nullable |
| `resolved_at` | TIMESTAMP (UTC) | NULL | Set when status flips to `resolved` |

- [ ] **Step 1.1: Write the migration** — additive only, defaults backfill existing rows to `live` with `last_reviewed_at = now()`. No data deletion.
- [ ] **Step 1.2: Add rollback path** — migration records a companion `DROP COLUMN` script in `scripts/` but does NOT run it. Rollback is manual.
- [ ] **Step 1.3: Update `brain_index.py generate`** to pick up the new columns. Verify with `python3 scripts/brain_index.py schema world_events`.

---

## Task 2: One-shot backfill triage

**Purpose:** Clear the current 71-event backlog before turning on A3 enforcement with the new rules. Don't skip this — if the backfill is wrong, A3 will nuisance-fire on day 1.

**Files:**
- `scripts/backfill_world_event_lifecycle.py` (new, one-shot, idempotent)

Rules applied in order (first match wins):

1. If `end_date_iso < now()` → `lifecycle_status='resolved'`, `resolved_at=end_date_iso`.
2. If `fin:*` domain AND `created_at > now() - 7d` → `lifecycle_status='live'`, `last_reviewed_at=now()`.
3. If `fin:*` domain AND `created_at <= now() - 7d` → `lifecycle_status='superseded'`, `superseded_by=NULL`. (The fin cluster is the worst offender and these are almost certainly obsolete market color.)
4. Long-horizon keywords in title (`by June`, `2026`, `2027`, `elections`, `regime`) → `lifecycle_status='monitoring'`, `last_reviewed_at=now()`.
5. Default → `lifecycle_status='live'`, `last_reviewed_at=now()`. Analyst will re-touch on next run; if not, A3 will legitimately flag it at 48h.

- [ ] **Step 2.1: Write backfill script with `--dry-run` default.** Print counts per bucket.
- [ ] **Step 2.2: Review dry-run output with owner before `--apply`.** Do not auto-apply.
- [ ] **Step 2.3: Record backfill decisions in Supabase brain session log** — which rules fired, how many events went to each bucket.

---

## Task 3: Analyst contract — refresh + retire hooks

**Files:**
- `scripts/world_analyst.py` — add touch + retire calls
- `scripts/finance_analyst.py` — same
- Shared helper: `agents/world_event_lifecycle.py` (new) — `touch_event(event_id)`, `retire_event(old_id, new_id=None)`, `resolve_event(event_id)`

Contract each analyst run MUST follow (enforce via a new `agents/world_event_contract.py` checker that runs at end of each analyst invocation and logs violations):

- For every event in the analyst's current worldview: call `touch_event(event_id)`.
- For every event the analyst decided to replace: call `retire_event(old_id, new_id)` where `new_id` is the replacement.
- For every event whose real-world outcome has landed: call `resolve_event(event_id)`.
- At end of run, emit one INFO line: `world_event_lifecycle domain=<domain> touched=N retired=M resolved=K`.

- [ ] **Step 3.1: TDD — write contract tests first** in `tests/test_world_event_lifecycle.py`. Cover: touch updates timestamp, retire sets status + pointer, resolve sets resolved_at, idempotent calls don't duplicate.
- [ ] **Step 3.2: Implement the three helpers** in `agents/world_event_lifecycle.py`. All writes go through `db.py` (no raw SQL in analysts — see CLAUDE.md rule 5).
- [ ] **Step 3.3: Wire into `world_analyst.py`.** After the analyst produces its new worldview, diff against the previous run's live events: matches → touch, deletions → retire (with `new_id=None`), additions → plain INSERT.
- [ ] **Step 3.4: Same wiring for `finance_analyst.py`.** Prioritize this — fin:* is the worst offender per current A3 data.
- [ ] **Step 3.5: Deploy one analyst at a time.** Finance first. Observe one cycle. Only deploy world_analyst after fin:* stale count drops to 0.

---

## Task 4: Tighten A3 to use the new signal

**Files:**
- Wherever A3 lives (likely `scripts/brain_health.py` — verify in Task 0.2; if elsewhere, update this path).

- [ ] **Step 4.1: Read current A3 query.** Paste it here before editing.
- [ ] **Step 4.2: Replace with status-aware query:**

```sql
SELECT domain, COUNT(*) AS stale
FROM world_events
WHERE (lifecycle_status = 'live'       AND last_reviewed_at < datetime('now', '-48 hours'))
   OR (lifecycle_status = 'monitoring' AND last_reviewed_at < datetime('now', '-14 days'))
GROUP BY domain
ORDER BY stale DESC
```

- [ ] **Step 4.3: Warning message surfaces the worst-offender domain.**
  E.g. `A3: 12 stale world events (fin:* 8 / geo:* 3 / other 1)`.
- [ ] **Step 4.4: Threshold** — warn at ≥ 5 stale total (down from current 10; the new signal is cleaner and per-domain breakdown makes low counts legible). Owner confirms before merging.

---

## Task 5: Ship order + verification

Deploy in this order — each step waits for the previous to be green before starting.

- [ ] **5.1:** Migration (Task 1) → deploy to VPS → verify columns exist, defaults populated.
- [ ] **5.2:** Backfill dry-run (Task 2) → review with owner → `--apply`.
- [ ] **5.3:** Analyst contract (Task 3) — **finance only**. Deploy, watch one cycle, confirm fin:* stale count drops.
- [ ] **5.4:** Analyst contract — **world_analyst**. Same cycle watch.
- [ ] **5.5:** Tighten A3 (Task 4). Brain health should show: `✅ 42 / ⚠️ 0 / ❌ 0` (or a new real signal, but not the current 71-event backlog).
- [ ] **5.6:** Update HANDOFF.md and MEMORY.md with new editorial rules and the `lifecycle_status` enum values.

---

## What NOT to do

- ❌ Do not weaken A3 to filter on downstream-reference presence. Owner explicitly rejected this — hides stale state instead of fixing it.
- ❌ Do not skip the backfill and just let analysts naturally touch events as they run. The existing 71 events include genuinely-dead ones that no analyst will re-pick-up — they'd stay stale forever.
- ❌ Do not add `lifecycle_status` as a free-text field. Enum with CHECK constraint.
- ❌ Do not run `--apply` on the backfill without owner review of the dry-run counts.
- ❌ Do not introduce raw SQL in analysts. All writes go through `db.py` or the new `agents/world_event_lifecycle.py` helpers.

---

## Open questions (ask owner before coding if unclear)

1. Is `world_events` in SQLite or Supabase? (Task 0.1 answers.)
2. Does `world_events` already have an `end_date_iso`-like column, or does `resolved` status need to be set purely by analyst judgment? (Inspection-dependent.)
3. Should `fin:*` events older than 7d really auto-supersede in backfill (Task 2 rule 3), or should owner triage each one? Default: auto-supersede, but flag for owner review in dry-run output.

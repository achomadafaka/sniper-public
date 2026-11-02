---
date: 2026-04-20
topic: calibration-loop-v1
status: approved (brainstorming — awaiting writing-plans)
advisors: advisor (claude), gpt-5.4 (codex, out-of-band)
session_id: dfba1a29-8a31-48d0-a456-eecc232ea7a3
---

# Calibration Loop v1 — Design

## 1. Problem

`predict_tool` grades predictions against reality, but graded outcomes never flow back into agent prompts as belief weights. Agents keep making predictions with no awareness of how accurate they have been, so their "high confidence" label means the same thing today as it did when they were wrong 90% of the time last week.

Current per-agent accuracy (last 30 days, graded only):

| Agent | Graded | Accuracy | Verification-query coverage |
|---|---|---|---|
| insider_copy | 52 | 55.8% | 100% |
| strategy_reviewer | 11 | 54.5% | 100% |
| world_analyst | 31 | 51.6% | 33% |
| wallet_scout | 43 | 44.2% | 100% |
| insider_analyst | 4 | 25% | 67% |
| finance_analyst | 18 | **5.6%** ⚠ | 37% |

`finance_analyst` at 5.6% motivated this spec, but v1 starts with `insider_copy` as the proving ground (see Rollout).

## 2. Success criterion (v1)

> A target agent's prediction runs are deterministically conditioned on its recent calibration stats, and that conditioning is observable in prompt inputs, run artifacts, and emitted prediction metadata. If an agent has catastrophic recent accuracy or weak verification coverage, the system must surface that explicitly before new predictions are made.

Machine-checkable, not "language changed." Visible in three places: run row (`calibration_context` populated), child prediction rows (`context_snapshot` has calibration envelope), and the prompt block itself (logged).

## 3. Scope

### In v1

- One new JSONB column on `agent_runs`: `calibration_context`
- Three new `predict_tool` commands: `start-run`, `finish-run`, `predict --run-id` (upgrade to existing `predict`)
- One new `predict_tool` read: `cmd_calibration_stats` (computes stats just-in-time, no materialization)
- `insider_copy_agent.py` wired end-to-end as the proving ground
- One test asserting machine-checkable success
- One brain-health check (`CAL1`) — scoped to rollout-enabled agents, `WARN` severity

### Explicitly NOT in v1

- Brier scoring
- Domain / topic breakdown (no schema column; separate v2 question)
- Per-model breakdown (cross-model comparisons are noise at current volumes)
- Time-decay weighting (uniform window is fine)
- Mid-run stats re-query tool
- UI or dashboard surface
- Consumer API (knowledge-as-second-opinion — that is project #5 in the larger roadmap)
- GDELT ingest (project #1, pilot first)
- Embedding index (project #2, pilot first)
- Market-move features (project #3, spec after v1 lands)
- Cross-agent lesson propagation (`agent_lessons` stays per-agent)
- Regime-change detection beyond `status='success' AND turns > 0` filter
- Automated threshold tuning — thresholds fixed in `web_settings.py` for v1
- Migration of historical predictions (legacy rows with `run_id IS NULL` are exempt forever)

## 4. Data model

### 4.1 No new tables

Calibration stats are a **function**, not a table. Computed just-in-time from `agent_predictions` (~200 rows today, trivial scan cost). Nothing materialized.

### 4.2 One new column

```sql
ALTER TABLE public.agent_runs
  ADD COLUMN IF NOT EXISTS calibration_context JSONB NULL;
```

Nullable because legacy runs and runs from agents not-yet-migrated do not populate it. Indexed via JSONB operators only if query patterns require it (none expected in v1).

### 4.3 Reused existing columns

| Column | Used for |
|---|---|
| `agent_predictions.run_id` (UUID, already exists in schema) | Link child predictions to parent run. Currently NULL-populated — v1 makes the wrapper path write it. |
| `agent_predictions.context_snapshot` (TEXT) | Holds the versioned calibration envelope as JSON string. Already nullable; legacy rows unchanged. |
| `agent_runs.status` | Part of eligibility filter. |
| `agent_runs.turns` | Part of eligibility filter. |

## 5. Calibration math

### 5.1 Function

`predict_tool.cmd_calibration_stats(agent_type: str, window_days: int = 30) -> dict`

Reads from `agent_predictions` joined on `agent_runs` via `run_id` with eligibility filter applied. Returns the stats dict below.

### 5.2 Eligibility filter

A prediction counts toward calibration stats iff ALL of:

1. `agent_predictions.run_id IS NOT NULL`
2. Parent `agent_runs.status = 'success'`
3. Parent `agent_runs.turns > 0`

Rationale: excludes broken-budget / failed-to-execute runs from the sample without inventing a regimes table. Catches the specific class of contamination we just lived through (Opus-4.7 budget cap producing `cost=0 turns=0` runs). Does **not** catch subtler shifts like prompt/model changes within otherwise-successful runs — those are out of v1 scope.

### 5.2.1 Historical linkage backfill (one-time, before enabling)

Without backfill, every rollout agent starts with `graded=0` and `small_sample` red-flag on its first post-v1 run, defeating the "prove on healthy agent first" rationale (§7.3).

Backfill job `scripts/backfill_agent_predictions_run_id.py` — one-shot, idempotent, safe:

- **Scope:** processes only predictions whose `agent_type` is in `CALIBRATION_ENABLED_AGENTS` (§5.5). Required CLI flag `--agent-type <name>` (one at a time) with a validation guard that rejects any agent not currently in the allow-list. Prevents wasted ambiguity work on higher-cadence out-of-scope agents (world/finance at every-4h cadence produce denser `agent_runs` than insider_copy's 12h, inflating multi-match skips).
- For each in-scope `agent_predictions` row with `run_id IS NULL`, find `agent_runs` rows with matching `agent_type` whose `created_at` falls within a small window bracketing the prediction's `created_at` (suggested window: `[prediction.created_at - 1h, prediction.created_at]`, since predictions are emitted during a run that started earlier).
- **Exactly one match:** `UPDATE agent_predictions SET run_id = <uuid>`.
- **Multiple matches:** skip (ambiguous — log for manual review).
- **Zero matches:** skip (orphaned — leave NULL, exempt forever per §8.1(3)).
- Never overwrites an existing non-NULL `run_id`.
- Emits match statistics: `{"matched": N, "ambiguous": N, "orphaned": N}` for the single agent_type processed.

Run once per agent before `CAL1` is enabled for that agent. v1 deployment runs it for `insider_copy` only. Expected outcome: most of the 55 predictions match to one of ~30 runs (12h cadence, predictions emitted within the run body). The spec does **not** guarantee 100% match; orphaned rows remain exempt forever.

Re-running for future rollout agents (§7.2) repeats the same one-at-a-time call after their allow-list entry is added.

Legacy predictions still without `run_id` after backfill are excluded from calibration stats, permanently. That is acceptable — the worst case is slightly smaller sample on day one, not a broken argument.

### 5.3 Return shape

```json
{
  "agent_type": "insider_copy",
  "window_days": 30,
  "window_start": "2026-03-21T...",
  "computed_at": "2026-04-20T...",
  "total": 55,
  "graded": 52,
  "ungraded": 3,
  "graded_coverage_pct": 94.5,
  "verification_coverage_pct": 100.0,
  "accuracy_pct": 55.8,
  "by_confidence": {
    "high":   {"total": 15, "graded": 14, "correct": 9, "accuracy_pct": 64.3},
    "medium": {"total": 30, "graded": 28, "correct": 15, "accuracy_pct": 53.6},
    "low":    {"total": 10, "graded": 10, "correct": 5, "accuracy_pct": 50.0}
  },
  "red_flags": [],
  "sample_warning": false
}
```

`graded_coverage_pct` = graded / total. `verification_coverage_pct` = has_verification_query / total. They measure different failure modes: the first says "how much of the window is resolved yet," the second says "can this agent be auto-learned from at all."

### 5.4 Red-flag rules

| Flag | Trigger | Rationale |
|---|---|---|
| `catastrophic_accuracy` | `accuracy_pct < 20` AND `graded >= 10` | Worse than useful on typical binary-ish predictions; process is likely broken (hallucinated predictions, wrong queries, or broken grading). |
| `low_verification_coverage` | `verification_coverage_pct < 60` AND `total >= 10` | Can't auto-grade → calibration itself is fragile. |
| `high_confidence_miscalibrated` | `by_confidence.high.graded >= 5` AND `by_confidence.high.accuracy_pct < 50` AND (`by_confidence.medium.accuracy_pct` exists → `high.accuracy <= medium.accuracy`; else → `high.accuracy <= overall.accuracy`) | Monotonicity failure: agent's "high" label does not actually beat its other buckets. |
| `small_sample` | `graded < MIN_SAMPLE` | Prevents panic on 0% accuracy from 2 predictions. Stats returned but every downstream consumer treats them as untrustable. Appears in both `sample_warning` bool and `red_flags` list for downstream simplicity. |

### 5.5 Thresholds (in `web_settings.py`)

```python
CALIBRATION_WINDOW_DAYS = 30
CALIBRATION_MIN_SAMPLE = 10
CALIBRATION_CATASTROPHIC_THRESHOLD = 0.20
CALIBRATION_COVERAGE_THRESHOLD = 0.60
CALIBRATION_HIGH_CONF_THRESHOLD = 0.50
CALIBRATION_ENABLED_AGENTS = ["insider_copy"]  # extended one-at-a-time per Rollout §7.2
```

No runtime tuning in v1. If thresholds prove wrong, change the file, redeploy. `CALIBRATION_ENABLED_AGENTS` is the allow-list consumed by `CAL1` brain-health check (§8.2) to scope its enforcement.

## 6. Prompt consumption surface

### 6.1 Injection mechanism

Wrapper pre-computes the calibration block before calling the SDK `query()`. Block is prepended to the **initial user prompt** (not system prompt — system prompts are often cached, and we want calibration state to visibly change run-to-run).

Mirrors the existing pre-fetch pattern used by `finance_analyst.py` (news pre-fetch) and `world_analyst.py` (situation_model load).

### 6.2 Block the agent sees

```
## CALIBRATION STATE (last 30 days)
Predictions: 55 total | 52 graded (94.5%) | 3 pending
Overall accuracy: 55.8%
Verification coverage: 100%

By confidence:
  high    14 graded, 9 correct  (64.3%)
  medium  28 graded, 15 correct (53.6%)
  low     10 graded, 5 correct  (50.0%)

RED FLAGS: none

Before you make new predictions:
  1. Provide verification_query for EVERY prediction (auto-gradable only)
  2. Apply confidence monotonically: high > medium > low accuracy
  3. If confidence=high, include calibration_note explaining why
```

When red flags exist, replace the "none" line with a list, and the guidance changes tone:

```
RED FLAGS:
  catastrophic_accuracy       — 5.6% is worse than useful; predictions likely hallucinated or grading broken
  low_verification_coverage   — only 37% auto-gradable; calibration itself is fragile
  high_confidence_miscalibrated — your "high" label is 0% correct, worse than medium

Before you make new predictions:
  1. Provide verification_query for EVERY prediction (auto-gradable only)
  2. Downgrade confidence unless you have stronger evidence than last time
  3. If confidence=high, include calibration_note explaining why this is different from last N failures
```

Plain text. Cheap tokens. Grep-able.

### 6.3 Per-prediction emission envelope

Stored in `agent_predictions.context_snapshot` as a JSON string:

```json
{
  "schema_version": 1,
  "calibration": {
    "saw_red_flags": ["catastrophic_accuracy"],
    "confidence_rationale": "downgraded high→medium because last 10 high-conf calls were 0/10",
    "differs_from_prior": "query targets market resolution, not agent belief",
    "verification_query_provided": true
  }
}
```

Field required-ness:

- **Always required:** `schema_version`, `calibration.saw_red_flags`, `calibration.verification_query_provided`
- **Required when** `saw_red_flags` is non-empty OR `confidence == "high"`: `calibration.confidence_rationale`
- **Always optional:** `calibration.differs_from_prior`

`schema_version` exists so v2 can evolve without breaking the test for v1 envelopes.

### 6.4 Plumbing — new `predict_tool` commands

Three concrete deliverables. All accept `--json` flag for machine-readable output.

**`predict_tool start-run <agent_type>`**
- Inserts a new `agent_runs` row with `status='running'` — **requires migration** (§7.1 step 5.1) because committed `supabase_002_agent_runs.sql:31` defines `CHECK (status IN ('success', 'error', 'timeout', 'budget_exceeded'))`. Migration extends this to include `running`. `finish-run` replaces the value with a terminal state, so `running` is transient only.
- Computes calibration stats via `cmd_calibration_stats`
- Writes stats dict to `calibration_context` JSONB
- Emits run UUID + formatted calibration block on stdout (wrapper captures both)

**`predict_tool predict --run-id <uuid> ...`** (upgrade to existing `predict`)
- All existing flags unchanged
- Adds `--run-id` flag: **required** when wrapper-initiated, **optional** for legacy/manual calls (writes NULL, matching current behavior — enables the exemption in §8.1(3))
- Writes `run_id` onto the new `agent_predictions` row
- Adds `--calibration-note <json-string>` flag: **required** when `--run-id` is provided. Payload is the full envelope from §6.3. Validated before insert: the whole envelope must parse and carry `schema_version`, `calibration.saw_red_flags`, `calibration.verification_query_provided`; `calibration.confidence_rationale` must be present when either `saw_red_flags` is non-empty or `confidence == "high"`. Rejected otherwise with exit code 2.

**`predict_tool finish-run <uuid> [--status success|error|timeout|budget_exceeded] [--turns N] [--cost-usd F] [--findings-summary ...]`**
- Updates the `agent_runs` row created by `start-run`: writes `status`, `turns`, `cost_usd`, `duration_seconds` (computed from `created_at` to now), and `findings_summary`
- Does **not** write to `accuracy_pct` — that field exists live but is undeclared in committed migrations (schema drift flagged separately; out of v1 scope). v1 keeps accuracy inside `calibration_context.accuracy_pct` only.
- Called by wrapper at end of SDK session
- Shell-fallback version also callable from wrapper's error path to guarantee the row closes out (terminal status required)

### 6.5 Writes per run

| Write | Column | When |
|---|---|---|
| 1 × agent_runs row create | `status='running'`, `calibration_context` populated | At wrapper start |
| 0..N × agent_predictions rows | `run_id = <uuid>`, `context_snapshot = <envelope>` | As agent emits predictions |
| 1 × agent_runs row finalize | `status`, `turns`, `cost_usd`, `duration_seconds`, `findings_summary` | At wrapper end |

N can be 0 — some runs legitimately emit no predictions; that's fine.

## 7. Rollout

### 7.1 v1 spec deliverable — `insider_copy` only

One agent, end-to-end. Proving ground.

Concrete changes:

1. `agents/insider_copy_agent.py` — wrapper calls `predict_tool start-run` first, captures UUID, then the SDK `query()` with the pre-fetched calibration block prepended to the prompt. Shell-fallback heartbeat calls `finish-run` on both success and error paths.
2. `agents/insider_copy_agent.py` — system prompt updated to: (a) emit `calibration_note` per prediction, (b) always pass `--run-id` on `predict_tool predict` calls, (c) include `--calibration-note` with every prediction.
3. `agents/tools/predict_tool.py` — adds `cmd_start_run`, `cmd_finish_run`, `cmd_calibration_stats`; upgrades `cmd_predict` to accept `--run-id` and `--calibration-note`; applies eligibility filter in `cmd_calibration_stats`.
4. `web_settings.py` — six new constants from §5.5 (including `CALIBRATION_ENABLED_AGENTS`).
5. **Two migrations:**
   - `migrations/supabase_013_agent_runs_calibration_context.sql` — `ALTER TABLE agent_runs ADD COLUMN calibration_context JSONB NULL`.
   - `migrations/supabase_014_agent_runs_status_add_running.sql` — drop and re-add the `status` CHECK constraint to include `'running'`: `ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check; ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check CHECK (status IN ('success', 'error', 'timeout', 'budget_exceeded', 'running'));`
6. `scripts/backfill_agent_predictions_run_id.py` — one-shot backfill per §5.2.1. Required `--agent-type` flag; rejects agents not in `CALIBRATION_ENABLED_AGENTS`. For v1 deployment: run once for `insider_copy` only, before enabling `CAL1`.
7. `index/schema_registry.json` regenerated via `brain_index.py generate` (new column).
8. `tests/test_calibration_loop.py` — see §8.
9. `scripts/brain_health.py` — new `CAL1` check per §8.2.

### 7.2 Subsequent phases (tracked, not specified)

Out of this spec. Each is a small follow-up PR copying the pattern.

| Phase | Agent | Notes |
|---|---|---|
| v1.1 | `strategy_reviewer` | Clean copy-paste |
| v1.2 | `wallet_scout` | Clean copy-paste |
| v1.3 | `world_analyst` | First prompt-tighten to require verification_query; calibration wiring is same pattern |
| v1.4 | `finance_analyst` | Last. Applying here first would conflate "is loop working" with "is finance still broken." |
| deferred | `insider_analyst` | Apply whenever — sample size is chronically below MIN_SAMPLE. |

### 7.3 Why `insider_copy` first

- 55.8% accuracy, 100% verification coverage — any improvement attributable to calibration feedback, not to the agent finally un-breaking
- Already uses obs_tool / predict_tool / action_tool in a consistent pattern — low surface for the wrapper changes
- Runs 2x/day so feedback cycle is short (days, not weeks) without being so frequent that quota is a concern

## 8. Definition of done (v1)

### 8.1 Tests

`tests/test_calibration_loop.py` asserts:

1. For all migrated-agent `agent_runs` rows created in last 24h: `calibration_context IS NOT NULL`.
2. For any `agent_predictions.run_id = <that_run.id>`: `context_snapshot` parses as JSON AND contains `calibration.saw_red_flags` key.
3. Legacy rows (`run_id IS NULL`) are not asserted against — exempt forever.
4. Integration test forces at least one prediction through the linkage path so (2) is actually exercised rather than vacuously true on zero-prediction runs.

### 8.2 Brain health — `CAL1`

New check, scoped to rollout-enabled agents only (reads allow-list from `web_settings.CALIBRATION_ENABLED_AGENTS`).

- **PASS**: for each enabled agent, most recent `agent_runs` row has `calibration_context` populated.
- **WARN**: for any enabled agent, most recent `agent_runs` row has `calibration_context IS NULL` AND created within expected cadence window.
- **Never FAIL** in v1 — a single missed run is not a system-down condition.

Not-yet-enabled agents are silently ignored. Mirrors the `B5` pattern of scoped enforcement.

### 8.3 Eyeballed checks

- Manually trigger one `insider_copy` run post-deploy. Inspect log for the calibration block.
- Query `agent_runs WHERE agent_type='insider_copy' ORDER BY created_at DESC LIMIT 1` — `calibration_context` has all expected keys.
- Query linked `agent_predictions` — `context_snapshot` parses, envelope valid.

## 9. Risks & caveats

1. **Eligibility filter depends on `run_id` linkage landing first.** If any predict-path writes NULL run_id during or after v1 rollout, stats degrade silently. Mitigation: the test in §8.1(1) would catch it within the 24h window.
2. **Stationarity assumption within eligible runs.** The filter catches totally-broken runs but not prompt/model swaps that produce degraded-but-successful runs. v2 may need a regime-tag.
3. **Agents could game the envelope.** If we reward "saw red flags and downgraded," the agent may always claim red flags and always downgrade. Mitigation (out of v1): track whether confidence downgrade actually improves next-window accuracy. For v1, we just observe; we don't reward.
4. **Window size is uniform.** A 30-day window weights day-1 predictions same as today. Acceptable for v1; time-decay is v2.
5. **Rollout fatigue.** Six agents × ~half-day each = three days of mechanical copy-paste. If the pattern is wrong, we will have copied the wrong thing. Mitigation: v1.1 is the first copy — if the pattern fights us there, fix the pattern before continuing.
6. **Undeclared `agent_runs` schema drift.** Live schema has three columns not present in committed migrations: `predictions_made`, `predictions_graded`, `accuracy_pct`. This spec side-steps them (v1 writes only via `calibration_context`) but the drift is a separate hygiene issue — track as a follow-up to reconcile migrations with live schema, out of v1 scope.

## 10. Open questions (deferred, not blockers)

- Should `cmd_calibration_stats` cache for N minutes within a run to avoid double-counting a freshly-written prediction? Probably yes; deferred to implementation.
- Should `calibration_context` be indexed? No evidence yet; defer until query patterns demand it.
- How does Telegram bot (`bot/relay.py`) fit in? Interactive agent, different cadence — not a v1 target.

## 11. References

- Advisor feedback (in-session, 2026-04-20)
- GPT-5.4 / Codex feedback (out-of-band, 2026-04-20)
- Companion project: `memory/project_trade_idea_review_research.md` (adversarial reviewer research, parked for next session)
- Prior spec pattern: `docs/superpowers/specs/2026-04-19-research-batch1-volume-filtered-no.md`
- Feedback memory: `project_research_infrastructure.md`

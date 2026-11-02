# Branch Close Knowledge Sync — Design (v1)

**Date:** 2026-05-15
**Status:** design-only; Phase 0. No schema changes, no migrations, no scripts, no DB writes.
**Scope:** make project knowledge updates an explicit, reviewable phase of branch close instead of an optional afterthought.
**Top-level name:** Knowledge Sync Gate (KSG)
**Author note:** ratified by the 2026-05-15 brain-sweep audit which found ~26 closed analyzer-arc findings were never persisted to Supabase until the audit caught them.

---

## 0. Doctrinal framing (read first)

Every meaningful branch should leave the repo smarter than it found it. Not aspirationally — operationally. A branch closes with one of two outcomes:

1. **Knowledge delta recorded.** Local instruction files updated where agent behavior should change; Supabase memories created/superseded where structured knowledge moved; superseded entries explicitly marked, not silently abandoned.
2. **Knowledge delta: none.** An explicit one-line attestation that the branch was a bug fix, test-only change, or non-doctrinal edit. Recording the absence is part of the discipline.

Knowledge does not stop going stale because we add a workflow. It stops going *silently* stale: stale entries become visible, reviewable, and hard to ignore.

Wallet analyzer doctrine remains: **wallet analyzer is not a copy bot**. It is a strategy-intelligence system. The roadmap is `wallet intelligence → strategy extraction → thesis formation → thesis-specific dataset → thesis-specific simulation → paper / live validation`. The Knowledge Sync Gate must defend that doctrine — old `insider_score`, low-buy, and copy-pipeline framings must not creep back in as current truth via memory drift.

### Non-goals

- Not a documentation generator. KSG does not write docs you should have written; it surfaces that you didn't.
- Not a knowledge graph. We have markdown specs, `memories`, and `knowledge/` pages already — KSG connects them, doesn't replace them.
- Not an LLM-only workflow. The gate must be runnable as a checklist by a human reviewer with no agent.
- Not a production-mutation tool. Default mode is `audit_only`. Writes are gated.

---

## 1. Problem statement

The 2026-05-15 audit revealed that across the Q2 2026 analyzer arc (~50 logical units shipped: Edge/Exit/Completeness/Timing surfaces, Position Builder, Strategy Pattern Audit, Wallet Ledger Phases 1-7, M-series closures, R-tickets, calibration v1/v2/v3, capital-ledger plumbing, pUSD work, DW_EW audit), only the April 26-27 bulletproofing batch landed in Supabase contemporaneously. The remaining ~26 closures had to be backfilled manually after the user asked the obvious question: *does any of this reflect on Supabase?*

The drift pattern:

```
spec written → tests written → impl shipped → PR merged → next thing started
                                              ↑
                                              brain entry never written
```

This is not an agent-discipline failure that better prompting fixes. It's a workflow gap. There is no point in the branch lifecycle where someone is asked, with a checklist in front of them: *did this change project knowledge?*

KSG inserts that point.

---

## 2. Current state inventory

What exists that KSG must integrate with — not replace.

### 2.1 Local instruction files

| File | Lines | Role | Audience |
|---|---:|---|---|
| `AGENTS.md` | 97 | Codex/general-agent operating rules. Doctrine, repo map, authoritative docs, legacy guards, scope guards. | Codex |
| `CLAUDE.md` | 619 | Claude-specific operating rules. Doctrine, ADRs, bug log, runtime ops, ADR-001–ADR-007, parallel-work guardrails, schema-validator workflow. | Claude |
| `MEMORY.md` | 198 | Project state snapshot. Already at the 200-line auto-truncate cap; cannot grow. Pointers to topic files. | Claude (auto-loaded) |
| `knowledge/` | varies | Compiled wallet + strategy markdown, daily-regenerated from live DB. `<!-- HUMAN:START --> <!-- HUMAN:END -->` regions for operator notes. | Both |
| `STATUS.md` | n/a | Runtime status, frozen/live unit matrix. | Both |

### 2.2 Supabase brain

| Object | Purpose |
|---|---|
| `memories` table | Structured knowledge entries. Columns: `id`, `type`, `name`, `description`, `content`, `status`, `superseded_by`, `valid_as_of_commit`, `source`, `created_at`, `updated_at`. Status lifecycle: `active` / `superseded` / `retired` / `draft` / `archived` / `rejected` / `needs_review` (DB-enforced — see §5.3). `source` is constrained to `vps` / `local` / `agent`. |
| `agent_runs` | Per-agent invocation log (status: success/error/timeout/budget_exceeded). |
| `findings`, `world_events`, `market_boosts` | Domain-specific knowledge stores. |
| `scripts/brain_index.py` | Schema/dependency/pattern lookup CLI. |
| `scripts/brain_health.py` | Brain freshness + integrity checks. |
| `scripts/apply_memory_status_updates.py` | Reviewed supersession applier. |
| `scripts/sync_session_findings.py` | Session-end finding persistence. |
| `scripts/compile_knowledge.py` + `compile_knowledge_cron.sh` | Knowledge-layer markdown compilation. |
| `agents/brain_hygiene_agent.py` (via `.claude/agents/`) | Proposes status changes on stale memories (no auto-write). |

### 2.3 Spec/plan layout

`docs/superpowers/specs/` and `docs/superpowers/plans/` are the authoritative design contracts. Specs precede plans. Per-feature dated filenames (`YYYY-MM-DD-<slug>.md`). Specs are explicit about scope guards, doctrinal framing, non-goals.

### 2.4 Migration conventions

`migrations/supabase_NNN_*.sql` — idempotent, single-tenant, run via Supabase Dashboard SQL Editor. RLS scoped to `anon` per `supabase_005_rls_scope_all.sql`. No service-role usage from repo scripts.

### 2.5 Gaps KSG must close

1. No branch-close phase asking *did knowledge move?*
2. `memories.source` constraint is `vps`/`local`/`agent` — fine for provenance but doesn't capture *which branch/PR* an entry came from. We currently encode that in the `content` field by hand (brittle).
3. No formal supersession workflow at branch close. `brain_hygiene_agent` runs out-of-band, after drift has already accumulated.
4. Local AI files can grow without anyone asking *should this be in Supabase instead?*
5. `MEMORY.md` is at the 200-line cap — additions silently push older context past truncation.

---

## 3. Workflow design — Knowledge Sync Gate

KSG is a phase, not a tool. It runs between `tests pass` and `PR opens / merges`.

### 3.1 Lifecycle

```
branch work
  ↓
tests/validation (existing)
  ↓
scope-guard review (existing — AGENTS.md §Scope Guards)
  ↓
─── KNOWLEDGE SYNC GATE ──────────────────────
  3.2 Knowledge Delta Report
  3.3 Decide: local file updates needed?
  3.4 Decide: Supabase entries needed?
  3.5 Decide: anything superseded?
  3.6 Attestation: "Knowledge delta: <yes|none>" recorded in PR body
──────────────────────────────────────────────
  ↓
PR open / merge (existing)
```

### 3.2 Knowledge Delta Report

A short structured answer produced by the agent (or the human reviewer) before the PR description is finalized. Eleven questions, yes/no with one-line evidence:

```
1.  Did this branch change project doctrine?           [y/n] evidence:
2.  Did it add or retire a module or subsystem?        [y/n] evidence:
3.  Did it change analyzer or strategy-audit behavior? [y/n] evidence:
4.  Did it change tests or validation commands?        [y/n] evidence:
5.  Did it introduce a new workflow or skill?          [y/n] evidence:
6.  Did it supersede existing docs/specs/plans?        [y/n] evidence:
7.  Did it create a new bug lesson or semantic fix?    [y/n] evidence:
8.  Did it affect Supabase schema / write surface /
    RLS / migrations?                                  [y/n] evidence:
9.  Did it create a new wallet insight, thesis,
    research finding, or simulation candidate?         [y/n] evidence:
10. Did it change what agents should know before
    working in this area?                              [y/n] evidence:
11. Did it change legacy/frozen status of any unit?    [y/n] evidence:
```

"Evidence" = a path, a commit SHA, or a one-line claim. Yes-with-no-evidence is rejected.

### 3.3 Routing matrix

| Question yes → | Action |
|---|---|
| 1, 10 | Update `AGENTS.md` + `CLAUDE.md` (operating rules only — see §4) |
| 2, 3, 4 | Update `AGENTS.md` Authoritative-Docs section if a new spec/plan is now load-bearing; usually a new `memories` entry too |
| 5 | New skill spec or update existing one; entry_type `workflow` |
| 6 | Mark superseded `memories` rows; update doc references |
| 7 | `memories` entry, entry_type `bug_lesson` or `semantic_fix` |
| 8 | Migration + `STATUS.md` update + `memories` entry, entry_type `architecture` |
| 9 | `memories` entry, entry_type `wallet_insight` / `strategy_thesis` / `research_note` / `simulation_candidate` |
| 11 | Update `STATUS.md` + `AGENTS.md` Legacy section + `memories` entry, entry_type `legacy_retirement` |

All "no" → attestation `Knowledge delta: none. Reason: <bugfix-only | test-only | refactor-without-behavior-change | …>`.

### 3.4 PR-body block (mandatory section)

Every PR (manual or `/ship`-driven) ends with:

```markdown
## Knowledge Sync

**Delta:** yes | none
**Local file updates:** none | AGENTS.md §X | CLAUDE.md §Y | …
**Supabase entries created:** none | <slug-1>, <slug-2>
**Supabase entries superseded:** none | <slug-3> → superseded_by <slug-1>
**Open questions surfaced:** none | <one-line each>
**Attestation:** I ran the 11-question delta report; the answers are recorded above.
```

For `Delta: none`, the body is two lines (Delta + Attestation). The block is non-optional.

---

## 4. Local AI-file responsibility split

Strict separation. Each file has one job; duplication is a smell.

### `AGENTS.md` (Codex + general-agent operating rules)

- Current project purpose + roadmap (one paragraph).
- Repo map (which directories are current, which are frozen).
- Authoritative docs (current specs/plans + audit docs).
- Legacy/frozen list (guarded entrypoints, frozen tables).
- Scope guards.
- Test/validation commands.
- **Hard cap: 200 lines.** If you would exceed it, the new material belongs in Supabase or a referenced doc, not here. The line cap is not a hard formatting fetish; it is a forcing function. If a proposed update would exceed the cap, the agent must decide whether the content belongs in Supabase knowledge instead.

### `CLAUDE.md` (Claude-specific operating rules + project ADRs)

- ADRs (decisions that change behavior across files).
- Bug Fix Log.
- Runtime ops.
- Parallel-work guardrails.
- Honesty rules.
- Claude-specific subagent conventions.
- **Soft cap: 800 lines.** When you cross it, move ADR history > 6 months old to a `docs/adrs/` archive page and link from CLAUDE.md.

### `MEMORY.md` (Claude auto-loaded project state)

- Index entries ONLY: `- [Title](file.md) — one-line hook`.
- **Hard cap: 200 lines** (system truncates after 200 anyway).
- Topic detail lives in per-topic memory files under `memory/`.

### Supabase `memories` (structured queryable knowledge)

- Findings (`finding/<area>/<slug>-<date>`)
- Doctrine (`doctrine/<topic>`)
- Architecture (`architecture/<subsystem>`)
- Workflows (`workflow/<name>`)
- Bug lessons (`lesson/<bug>-<date>`)
- Wallet insights (`wallet-insight/<wallet>-<date>`)
- Strategy theses (`thesis/<slug>`)
- Simulation candidates (`simulation-candidate/<slug>`)
- Legacy retirements (`retirement/<unit>-<date>`)
- Open questions (`open-question/<slug>`)
- Research notes (`research-note/<area>-<date>`)

Rule: anything queryable, structured, or with explicit supersession lineage belongs in Supabase. Anything that an agent must read *before doing anything* belongs in `AGENTS.md`/`CLAUDE.md`.

### What does NOT go in local AI files

- Research history (→ Supabase `research-note`).
- Per-wallet dossiers (→ `knowledge/wallets/` markdown).
- Per-finding closure detail (→ `memories` `finding/*` entries).
- Strategy theses (→ `memories` `thesis/*`).
- Stale doctrine — once superseded, it goes to Supabase as `status='superseded'` with a `superseded_by` link, and the local file gets the new doctrine only.

---

## 5. Supabase schema design

### 5.1 Recommendation (Phase 0): extend `memories`, do not create `project_knowledge_entries`

**Status:** recommendation, not a schema change. Contingent on Phase 3 schema audit before any `ALTER TABLE` is applied.

The user's brief proposed `project_knowledge_entries`. The pragmatic call is to **extend the existing `memories` table** because:

- It already has `type`, `name` (slug), `description`, `content`, `status`, `superseded_by`, `valid_as_of_commit`, `source`, `created_at`, `updated_at`.
- It already has consumers: `brain_index.py`, `brain_health.py`, `brain_hygiene_agent`, `save_finding()`, `read_memories()` with `active_only=True` default.
- A parallel table creates the same drift KSG is supposed to prevent.
- The missing fields look additive — likely `ALTER TABLE ADD COLUMN`, no breaking change.

This is **not** a schema change yet. Before Phase 3 applies any migration, the schema audit must inspect:

- the live `memories` table columns, types, defaults, and constraints,
- existing RLS policies (`memories_select` / `memories_write` per `supabase_005`),
- existing indexes (don't shadow them with new GINs),
- every consumer that reads/writes `memories` (grep `bot.supabase_helper`, `save_finding`, `read_memories`, `search_memories`, `apply_memory_status_updates.py`),
- the relationship — if any — between `memories` and the separate `findings` table (Open Question §10 #4),
- any in-flight migrations under `migrations/supabase_*.sql` that would conflict.

If the audit reveals a structural conflict (e.g. `findings` already covers part of this surface and folding makes more sense than extending), Phase 3 may revise this recommendation.

Proposed additive columns (Phase 3 migration target — design only here, not applied):

```sql
ALTER TABLE public.memories
  ADD COLUMN IF NOT EXISTS slug             TEXT,                      -- normalized slug; mirrors name today, distinguishable later
  ADD COLUMN IF NOT EXISTS entry_type       TEXT,                      -- distinct from `type`; see §5.2 vocabulary
  ADD COLUMN IF NOT EXISTS summary          TEXT,                      -- one-paragraph render; description stays one-line
  ADD COLUMN IF NOT EXISTS tags             TEXT[] DEFAULT '{}',       -- query facet
  ADD COLUMN IF NOT EXISTS source_branch    TEXT,
  ADD COLUMN IF NOT EXISTS source_pr        INTEGER,
  ADD COLUMN IF NOT EXISTS source_files     TEXT[],                    -- file paths touched
  ADD COLUMN IF NOT EXISTS supersedes_ids   UUID[] DEFAULT '{}',       -- inverse of superseded_by, multi-entry batches
  ADD COLUMN IF NOT EXISTS valid_to_commit  TEXT,                      -- set when status flips to superseded
  ADD COLUMN IF NOT EXISTS confidence       TEXT CHECK (confidence IN ('low','medium','high','locked')),
  ADD COLUMN IF NOT EXISTS reviewed_by      TEXT,
  ADD COLUMN IF NOT EXISTS reviewed_at      TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS metadata         JSONB DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_memories_entry_type ON public.memories (entry_type) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_memories_source_pr  ON public.memories (source_pr) WHERE source_pr IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_tags_gin   ON public.memories USING GIN (tags);
```

RLS: existing `memories_*` policies cover this; new columns inherit. No new role/grant needed.

The existing `source` check constraint (`'vps'|'local'|'agent'`) stays — that's provenance of the *writer*, not the *content origin*. `source_branch`/`source_pr` cover branch provenance.

#### Migration safety constraints (binding on Phase 3)

- **Additive columns only.** No column rename, no type change, no constraint tightening on existing columns. Anything destructive is out of scope for this design.
- **Reviewed migration file.** Single `migrations/supabase_NNN_memories_ksg_extension.sql`, idempotent (`ADD COLUMN IF NOT EXISTS`), reviewed before apply.
- **Rollback path required.** A companion `migrations/supabase_NNN_rollback_memories_ksg_extension.sql` that `DROP COLUMN IF EXISTS` each added column. Tested against a dev branch before Phase 3 applies the forward migration.
- **No production Supabase mutation during design or planning.** Phase 0 (this spec) and any Phase 1/2 work writes nothing to the live database. Schema audit reads only.
- **No service-role credential exposure.** The migration runs from the Supabase Dashboard SQL Editor by the operator, following the convention in `supabase_005_rls_scope_all.sql`. The repo does not embed service-role keys.
- **RLS/write-policy review before commit-mode writes.** Phase 6 (skill mode escalation to `supabase_commit_entries`) cannot ship until the operator has reviewed how Phase 4's upsert script authenticates and which policies authorize its writes.

### 5.2 `entry_type` vocabulary (13 controlled values)

```
doctrine               -- changes how agents frame the work (e.g. "wallet analyzer is not a copy bot")
architecture           -- subsystem/module added, retired, or restructured
workflow               -- new or changed process (KSG itself is one)
test_command           -- new or changed validation / preflight check
bug_lesson             -- bug + how to avoid; references commit
semantic_fix           -- correctness fix where prior behavior was wrong, not just buggy (e.g. R9 _is_win NO branch)
wallet_insight         -- observed wallet behavior that matters for thesis formation
strategy_thesis        -- candidate behavioral thesis (pre-simulation)
simulation_candidate   -- thesis + named missing data for a future simulation
legacy_retirement      -- module/table/entrypoint marked frozen or removed
open_question          -- known unknown surfaced during work, parked for later
research_note          -- empirical investigation result (e.g. pUSD peg verification)
finding                -- closure/completion of a planned unit of work (most common)
```

Choose ONE per entry. If unsure, default to `finding`.

### 5.3 `status` vocabulary

```
active        -- current truth; loaded by default
superseded    -- replaced by another entry (carry `superseded_by`); readable for history
retired       -- intentionally withdrawn, no replacement; kept for context
archived      -- frozen snapshot kept for reference, not live work
draft         -- staged but not committed; not yet authoritative
needs_review  -- flagged by brain_hygiene_agent; awaiting human decision
rejected      -- proposed but explicitly not accepted; do not act on
```

The full 7-state vocabulary is enforced by the `memories_status_check` CHECK constraint (`migrations/supabase_022_memories_status_widen.sql`) and mirrored in both `bot.supabase_helper.VALID_STATUSES` and `scripts/knowledge_sync.VALID_STATUSES`. Drift between any of those three surfaces is a bug.

Default reads filter to `active`. History/audit reads must opt in via `active_only=False`.

### 5.4 Supersession discipline

Two rules, enforced by the upsert script (Phase 4):

1. **Never delete; supersede.** A row that's no longer true gets `status='superseded'`, `superseded_by=<new id>`, `valid_to_commit=<sha>`. The new row gets `supersedes_ids=[<old id>]`. Both stay queryable.

2. **No-orphan invariant.** An entry with `status='active'` whose `superseded_by` is non-null is illegal. An entry with `status='superseded'` whose `superseded_by` is null is also illegal. Validation script (Phase 5) catches both.

---

## 6. Branch-close checklist (the operational artifact)

Copy-paste block for PR bodies / branch-close ritual:

```markdown
## Knowledge Sync Checklist

[ ] Doctrine changed?
[ ] Architecture changed (module added/retired/restructured)?
[ ] Tests/validation commands changed?
[ ] Legacy/frozen status changed for any unit?
[ ] New workflow or skill?
[ ] New bug lesson or semantic fix?
[ ] New research finding, wallet insight, thesis, or simulation candidate?
[ ] Any existing docs/specs/plans/memories superseded?
[ ] Supabase schema or write-surface touched?
[ ] AGENTS.md / CLAUDE.md reviewed for staleness in the area you touched?
[ ] Supabase memories created/updated/superseded as appropriate?

**Delta verdict:** yes | none
**Reason (if none):** ___
```

Yes to any → mandatory PR-body Knowledge Sync section (§3.4).
All no → "Delta: none" with reason.

---

## 7. Validation (Phase 5; design only here)

Script: `scripts/validate_knowledge_sync.py` (proposed).

Checks (all advisory in Phase 5, enforcing in a later phase if adoption sticks):

1. **Local files exist** — `AGENTS.md`, `CLAUDE.md`, `MEMORY.md` present.
2. **Forbidden-current phrases absent from AGENTS.md / CLAUDE.md (case-insensitive):**
   ```
   "wallet analyzer is a copy bot"
   "copy this wallet"
   "recommended trigger"
   "expected pnl"
   "insider_score is source of truth"        # post 2026-03-25 ADR-006
   "low_buy_trades is the primary stream"    # post 2026-04-30 freeze
   ```
3. **Active-supersession invariant** — no `status='active'` row has non-null `superseded_by`; no `status='superseded'` has null `superseded_by`.
4. **Provenance completeness** — every entry created after the migration cutoff has either `source_branch` or `source_pr` set.
5. **PR body has Knowledge Sync block** — CI check on PRs to main. Block can say "Delta: none" but must be present.
6. **Schema-validator pre-existing check** still runs (`scripts/validate_schema_refs.py`) — KSG does not replace it.

---

## 8. Skill design

Name: `knowledge-sync` (skill slug, kebab-case).

Location: `.claude/skills/knowledge-sync/` (Claude); also referenced from `AGENTS.md` so Codex can run the same workflow manually.

### 8.1 Skill inputs

```yaml
branch_name: <required>
base_ref:    main                     # default
head_ref:    HEAD                     # default
pr_number:   <optional>
operator_notes: <optional, freeform>
mode:        audit_only               # default; see §8.3
```

### 8.2 Skill outputs

```yaml
knowledge_delta_report:
  doctrine_changed:     {y_or_n, evidence}
  architecture_changed: {y_or_n, evidence}
  ...                                  # 11 questions from §3.2
local_file_updates:
  - file: AGENTS.md
    section: "§Legacy / Historical Areas"
    rationale: ...
    diff: <unified-diff or path-to-patch>
supabase_entries_to_upsert:
  - slug: finding/...
    entry_type: finding
    summary: ...
    body_markdown: ...
    source_branch: ...
    source_pr: ...
    tags: [...]
    confidence: medium
superseded_entries:
  - slug: finding/old-thing
    superseded_by_slug: finding/new-thing
    valid_to_commit: <sha>
open_questions:
  - "Should _is_win NO-branch fix propagate to historical paper trades? (R9.5 follow-up)"
```

### 8.3 Skill modes

```
audit_only            # default — produces the report, writes nothing
local_docs_only       # writes the local-file diffs, no DB
supabase_draft_entries # writes JSON drafts to /tmp/ksg_<branch>.json, no DB
supabase_commit_entries # invokes the upsert script (Phase 4)
```

Default is **audit_only**. Every higher mode requires explicit per-invocation flag from the operator; an agent cannot silently choose a write-capable mode. Escalation to `supabase_commit_entries` additionally requires the upsert script to exist (Phase 4) and the §5.1 migration-safety review (Phase 3) to have been completed.

### 8.4 What the skill does NOT do

- Does not auto-write to Supabase in default mode.
- Does not infer doctrine changes from code alone — it asks the agent/human to attest.
- Does not generate new skill content. KSG produces drafts; the human approves.
- Does not edit `CLAUDE.md`/`AGENTS.md` line-by-line without showing the diff first.

---

## 9. Phased implementation

| Phase | Deliverable | Risk | Reversible? |
|---|---|---|---|
| 0 | This spec | none | n/a |
| 1 | Branch-close checklist text in `AGENTS.md` (one new section, ~15 lines) + `CLAUDE.md` (cross-reference). PR template addition. | low — text only | yes |
| 2 | Skill scaffold (`audit_only` mode only). Produces the 11-question report + JSON draft of proposed memory upserts. No DB writes. | low — read-only | yes |
| 3 | Reviewed Supabase migration adding columns from §5.1. Indexes. No data backfill yet. | medium — schema change, but additive | yes (DROP COLUMN) |
| 4 | `scripts/knowledge_sync.py` upsert tool. Reads the JSON draft, validates, upserts with supersession handling, writes an audit log. | medium — DB writes, but gated by reviewed script | yes (status flip back) |
| 5 | `scripts/validate_knowledge_sync.py` advisory checks + CI integration (PR-body block presence). | low — advisory at first | yes |
| 6 | Skill mode escalation: `supabase_commit_entries` mode wired to the upsert script. Default stays `audit_only`. | medium — full automation | yes (mode revert) |
| 7 | Backfill: walk closed PRs of the last 90 days, produce delta reports retroactively. Surfaces unrecorded knowledge that pre-dates KSG. | medium — manual review burden | yes |

Each phase has its own go/no-go review. **No phase auto-starts the next.** The user must approve each transition.

---

## 10. Risks and open questions

### Risks

1. **Friction → bypass.** If KSG feels like ceremony, agents will mark "Delta: none" reflexively. Mitigation: the checklist is short, evidence is one-liner, and the validator catches obvious cases (e.g. branch touched `sniper/analysis/wallet_protocol/` but declared no architecture change).
2. **`memories` table conflation.** Some existing entries don't fit cleanly into the §5.2 vocabulary (legacy March entries with `type='knowledge'`). Mitigation: leave them alone — backfill `entry_type` lazily only when an entry is next touched.
3. **`AGENTS.md` hard cap collision.** If a branch legitimately needs to add 30 lines of doctrine, the cap will collide with real work. Mitigation: cap forces a question (*does this belong in Supabase instead?*); cap can be raised once via explicit decision but each raise is itself a knowledge-delta event.
4. **CI block on missing PR-body section.** Could block urgent merges. Mitigation: validator is advisory through Phase 5; enforcement only after one full cycle of opt-in adoption.
5. **Supersession churn.** Some areas (calibration loop) may legitimately supersede entries every few weeks. Mitigation: that's a feature — the lineage is the value, not the singleton entry.

### Open questions

1. Should `entry_type='workflow'` entries also live as Markdown skills, or are skills a different artifact entirely? (Recommend: skill source-of-truth lives under `.claude/skills/`; Supabase entry is a pointer + change log.)
2. How does KSG interact with `/ship` pipeline? `/ship` already does test→impl→review; KSG should slot between review and PR-open. (Spec for the integration belongs in Phase 6.)
3. Does the `world-analyst` cron job's existing memory writes need to be re-classified into the new `entry_type` vocabulary? (Probably yes, but as a lazy backfill — out of KSG scope.)
4. Should `findings` (separate table) be folded into `memories` long-term? Out of KSG scope but worth noting — two tables that semantically overlap is the same drift problem one level deeper.
5. Branch-close timing: gate runs *before* PR open, or *before* merge? (Recommend before merge — accommodates iterations during review where knowledge state may shift.)

---

## 11. Acceptance criteria for Phase 0 (this spec)

- [x] Inventories existing local files, Supabase tables, and tooling.
- [x] Names the workflow (Knowledge Sync Gate) and its eleven-question report.
- [x] Defines responsibility split between local AI files and Supabase.
- [x] Proposes a concrete schema extension to the existing `memories` table rather than a parallel table.
- [x] Defines `entry_type` and `status` vocabularies.
- [x] Specifies supersession invariants.
- [x] Provides a copy-pasteable PR-body block.
- [x] Defines a phased implementation path, each phase independently reviewable.
- [x] Surfaces risks and open questions explicitly.
- [x] Includes no implementation: no SQL applied, no scripts written, no DB writes.

---

## 12. Cross-references

- `AGENTS.md` §Authoritative Docs (this spec will be added there in Phase 1)
- `CLAUDE.md` §Operating rules + ADR section
- `MEMORY.md` (auto-truncate cap — KSG enforces it)
- `docs/superpowers/specs/2026-05-13-post-analyzer-workflow-design.md` (the workflow this gate ultimately serves — strategy intelligence, not copy bot)
- `migrations/supabase_005_rls_scope_all.sql` (the RLS convention any future schema migration must follow)
- `scripts/brain_index.py`, `scripts/brain_health.py`, `scripts/apply_memory_status_updates.py` (existing tooling KSG composes with)
- 2026-05-15 brain-sweep backfill batch (`finding/wallet-protocol/*-2026-*` + `finding/calibration/v{1,2,3}-*`) — the audit that motivated this spec.
